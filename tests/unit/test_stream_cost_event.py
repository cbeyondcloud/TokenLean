"""A streamed call's actual cost, as a final SSE comment frame (opt-in).

Headers are sent before the first chunk, so a stream cannot carry the cost in a header; it
was priced only after ``[DONE]``, where no caller could see it. With
``observability.stream_cost_event`` on, a stream that completes ends with

    : x-tokenlean-cost {"request_id": ..., "cost_usd": "0.000123", "usage": "reported"}

right before its terminator. It is an SSE comment, which every SSE parser skips, so an SDK
sees the same events as before; a client reading the raw stream gets the cost. Off by
default: a stream is then byte-identical to what it was.
"""
import json
from datetime import datetime, timezone

import pytest
from fastapi.responses import JSONResponse

import main
from protocols import ANTHROPIC

_ON = {"observability": {"stream_cost_event": True}}


class _Ctx:
    def __init__(self, config=None):
        from savings.models import SavingsRecord
        self.messages = [{"role": "user", "content": "hi"}]
        self.config = config if config is not None else {}
        self.tenant_id = "default"
        self.request_id = "req-1"
        self.llm_elapsed_ms = 0.0
        self.routed_model = "gpt-4o-mini"
        self.provider_adapter = None
        self.redis_prefix = ""
        self.provider_attempts = []
        self.params = {}
        self.provider_calls = []
        self.savings = SavingsRecord(
            request_id="req-1", user_id="u", timestamp=datetime.now(timezone.utc),
            model_requested="gpt-4o-mini", routed_model="gpt-4o-mini", baseline_tokens=40,
        )
        self.savings.proxy_optimised_tokens = 30


_CHUNKS = [
    {"choices": [{"delta": {"content": "Hel"}}]},
    {"choices": [{"delta": {"content": "lo"}}]},
    {"choices": [], "usage": {"prompt_tokens": 500, "completion_tokens": 200}},
]


def _fake_acompletion(chunks):
    async def fake(**kwargs):
        async def gen():
            for c in chunks:
                yield c
        return gen()
    return fake


async def _collect(streaming_response):
    parts = []
    async for part in streaming_response.body_iterator:
        parts.append(part if isinstance(part, str) else part.decode())
    return "".join(parts)


def _frames(body):
    return [f for f in body.split("\n\n") if f.strip()]


def _cost(frame):
    prefix = ": x-tokenlean-cost "
    assert frame.startswith(prefix), frame
    return json.loads(frame[len(prefix):])


async def _run(monkeypatch, ctx, chunks=_CHUNKS, recorded=None):
    monkeypatch.setattr(main.litellm, "acompletion", _fake_acompletion(chunks))

    def fake_record(_ctx, start, status, response=None):
        if recorded is not None:
            recorded["status"] = status

    monkeypatch.setattr(main, "_record_outcome", fake_record)
    resp = main._stream_response(ctx, "gpt-4o-mini", {"api_key": "sk"}, {"stream": True},
                                 "req-1", 0.0, provider="openai", provider_key="sk")
    return await _collect(resp)


@pytest.mark.asyncio
async def test_stream_without_flag_is_unchanged(monkeypatch):
    body = await _run(monkeypatch, _Ctx())
    assert not any(f.startswith(":") for f in _frames(body))
    assert body.endswith("data: [DONE]\n\n")


@pytest.mark.asyncio
async def test_stream_with_flag_ends_with_cost_comment(monkeypatch):
    ctx = _Ctx(_ON)
    body = await _run(monkeypatch, ctx)
    frames = _frames(body)
    assert frames[-1] == "data: [DONE]"
    cost = _cost(frames[-2])
    assert cost["request_id"] == "req-1"
    assert cost["cost_usd"] == f"{ctx.savings.cost_actual_usd:.6f}"
    assert float(cost["cost_usd"]) > 0
    assert cost["usage"] == "reported"
    # the provider's chunks are relayed exactly as before, ahead of the comment
    assert frames[0].startswith("data: ") and "Hel" in frames[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("config", [{}, _ON])
async def test_stream_cost_priced_once(monkeypatch, config):
    calls = []
    real = main._price_stream

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(main, "_price_stream", counting)
    await _run(monkeypatch, _Ctx(config))
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_stream_cost_estimated_when_no_usage_chunk(monkeypatch):
    chunks = [{"choices": [{"delta": {"content": "Hello there"}}]}]
    body = await _run(monkeypatch, _Ctx(_ON), chunks=chunks)
    assert _cost(_frames(body)[-2])["usage"] == "estimated"


@pytest.mark.asyncio
async def test_failed_stream_has_no_cost_comment(monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("provider down")

    recorded = {}

    def fake_record(_ctx, start, status, response=None):
        recorded["status"] = status

    monkeypatch.setattr(main.litellm, "acompletion", boom)
    monkeypatch.setattr(main, "_record_outcome", fake_record)
    resp = main._stream_response(_Ctx(_ON), "gpt-4o-mini", {}, {"stream": True}, "req-2", 0.0,
                                 provider="openai", provider_key="sk")
    body = await _collect(resp)
    assert "x-tokenlean-cost" not in body
    assert recorded["status"] == "502"


def test_flag_must_be_true_not_truthy():
    """A config value that is merely truthy (e.g. an unexpanded "false" string) does not
    switch the frame on."""
    assert main._stream_cost_enabled(_ON) is True
    assert main._stream_cost_enabled({"observability": {"stream_cost_event": "false"}}) is False
    assert main._stream_cost_enabled({}) is False
    assert main._stream_cost_enabled(None) is False


@pytest.mark.asyncio
async def test_translate_stream_passes_cost_comment_last():
    comment = main._cost_comment("req-9", 0.0005, "reported")

    async def _source():
        yield 'data: {"id":"c1","model":"m","choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n'
        yield comment
        yield "data: [DONE]\n\n"

    out = "".join([line async for line in main._translate_stream(
        ANTHROPIC.stream_translator(), _source())])
    frames = _frames(out)
    assert _cost(frames[-1])["request_id"] == "req-9"
    assert any('"type":"message_stop"' in f for f in frames[:-1])


def _cached_json_response():
    body = {
        "id": "chatcmpl-cached", "object": "chat.completion", "created": 1700000000,
        "model": "gpt-4o-mini",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "Paris"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
    }
    return JSONResponse(content=body, headers={"x-tokenlean-cost-usd": "0.000000",
                                               "x-tokenlean-request-id": "req-c"})


@pytest.mark.asyncio
async def test_one_shot_stream_has_cost_comment_when_enabled(monkeypatch):
    monkeypatch.setattr(main, "get_config", lambda: _ON)
    body = await _collect(main._as_requested_stream(_cached_json_response(),
                                                    withhold_usage=False))
    frames = _frames(body)
    assert frames[-1] == "data: [DONE]"
    cost = _cost(frames[-2])
    assert cost == {"request_id": "req-c", "cost_usd": "0.000000"}


@pytest.mark.asyncio
async def test_one_shot_stream_unchanged_when_disabled(monkeypatch):
    monkeypatch.setattr(main, "get_config", lambda: {})
    body = await _collect(main._as_requested_stream(_cached_json_response(),
                                                    withhold_usage=False))
    assert "x-tokenlean-cost" not in body


@pytest.mark.asyncio
async def test_translated_one_shot_stream_has_cost_comment_last(monkeypatch):
    monkeypatch.setattr(main, "get_config", lambda: _ON)
    resp = await main._translate_response(ANTHROPIC, _cached_json_response(), want_stream=True)
    frames = _frames(await _collect(resp))
    assert _cost(frames[-1])["request_id"] == "req-c"
    assert any('"type":"message_stop"' in f for f in frames[:-1])
