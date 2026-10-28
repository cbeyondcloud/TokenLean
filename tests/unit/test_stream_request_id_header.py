"""A live stream carries x-tokenlean-request-id in its response headers.

The request id is known before the first chunk, so it is sent with the headers: a caller
that abandons a stream, or whose stream fails before the cost frame, still has the id to
find the call's usage row (POST /admin/usage-export). The routed model and the cost are not
headers on a live stream: failover may still change the model after the headers are sent,
and the cost is known only at the end (the opt-in `: x-tokenlean-cost` frame carries it).
"""
import pytest

import main
from protocols import ANTHROPIC
from tests.unit.test_stream_cost_event import _Ctx, _collect, _fake_acompletion, _CHUNKS


def _live(monkeypatch):
    monkeypatch.setattr(main.litellm, "acompletion", _fake_acompletion(_CHUNKS))
    monkeypatch.setattr(main, "_record_outcome", lambda *a, **k: None)
    return main._stream_response(_Ctx(), "gpt-4o-mini", {"api_key": "sk"}, {"stream": True},
                                 "req-1", 0.0, provider="openai", provider_key="sk")


@pytest.mark.asyncio
async def test_a_live_stream_sends_its_request_id_header(monkeypatch):
    resp = _live(monkeypatch)
    assert resp.headers["x-tokenlean-request-id"] == "req-1"
    assert "x-tokenlean-routed-model" not in resp.headers   # may still change on failover
    body = await _collect(resp)
    assert body.endswith("data: [DONE]\n\n")                # the stream itself is unchanged


@pytest.mark.asyncio
async def test_a_translated_live_stream_keeps_the_request_id_header(monkeypatch):
    resp = await main._translate_response(ANTHROPIC, _live(monkeypatch), want_stream=True)
    assert resp.headers["x-tokenlean-request-id"] == "req-1"
    assert resp.media_type == ANTHROPIC.stream_media_type
    await _collect(resp)
