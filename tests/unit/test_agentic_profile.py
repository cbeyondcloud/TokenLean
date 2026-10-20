"""The LITE profile (config/profiles/agentic.yaml): gateway on, behaviour-changing
optimisations off.

LITE serves callers that need the provider to receive exactly what they sent (agent
pipelines whose quality is measured on their own eval harness): auth, provider routing by
model name, failover, rate limits and spend caps, metering, provider prompt caching, exact
(L1) response caching and the trust & safety groups (in their shipped `flag` mode) stay; every
group that changes the prompt, the model or the answer is off. Optimisations are then turned
on one group at a time, after an A/B on the caller's own harness.

These tests read the profile exactly as a deployment does: config/config.yaml.template with
the profile applied through CONFIG_OVERLAYS.
"""
import copy
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

import config_loader

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_TEMPLATE = os.path.join(_REPO, "config", "config.yaml.template")
_PROFILE = os.path.join(_REPO, "config", "profiles", "agentic.yaml")

# Groups LITE runs. Anything else enabled in the effective config fails
# test_only_approved_groups_enabled: a group the template later ships enabled must be a
# deliberate LITE decision, not an inherited default.
APPROVED_ON = {
    "G5_cache",               # L1 exact-match only (L2 + step cache off below)
    "G18_observability",      # metrics, pricing, usage rows
    "G21_cache_alignment",    # provider prompt caching: same prompt, cheaper reads
    "G24_adaptive_bypass",    # can only switch groups OFF
    "G29_pii_redaction",      # trust & safety, shipped mode `flag` (records, never edits)
    "G30_guardrails",
    "G31_context_trust",
    "G32_tool_eligibility",
}

MUST_BE_OFF = [
    "G1_compression", "G2_template_registry", "G3_doc_pipeline", "G4_bypass", "G6_routing",
    "G7_retrieval", "G8_tools", "G9_context_schema", "G10_memory", "G11_output",
    "G12_reasoning", "G13_batch", "G14_tool_output", "G15_server_compute", "G16_agent_arch",
    "G17_loop", "G19_headroom", "g20_prompt_optimizer", "g22_deduplication",
    "G23_streaming_compression", "G25_adaptive_reasoning", "G26_context_budget",
    "G27_multimodal", "G28_ccr",
]


def _lite_config(monkeypatch):
    monkeypatch.chdir(_REPO)          # the template's params_dir is repo-relative
    monkeypatch.setenv("CONFIG_LOCAL_PATH", _TEMPLATE)
    monkeypatch.setenv("CONFIG_OVERLAYS", _PROFILE)
    monkeypatch.delenv("CONFIG_GCS_BUCKET", raising=False)
    monkeypatch.setattr(config_loader, "_config", {})
    monkeypatch.setattr(config_loader, "_sleep", lambda s: None)
    return copy.deepcopy(config_loader.load_config(required=True))


def test_profile_is_a_mapping_overlay():
    with open(_PROFILE, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    assert isinstance(data, dict) and set(data) <= {"groups", "observability"}


def test_profile_effective_config(monkeypatch):
    cfg = _lite_config(monkeypatch)
    groups = cfg["groups"]
    for key in MUST_BE_OFF:
        assert groups[key]["enabled"] is False, key
    g5 = groups["G5_cache"]
    assert g5["enabled"] is True
    assert g5["step_cache_enabled"] is False       # X-Step-Name would replay answers
    assert g5["l2_enabled"] is False               # near-match answers
    assert groups["G18_observability"]["enabled"] is True
    assert groups["G18_observability"]["langfuse_enabled"] is False   # optional, own overlay
    assert groups["G21_cache_alignment"]["enabled"] is True
    for key in ("G29_pii_redaction", "G30_guardrails", "G31_context_trust",
                "G32_tool_eligibility"):
        assert groups[key]["enabled"] is True
        assert groups[key]["mode"] == "flag", key   # records, never edits or blocks
    assert cfg["rate_limit"]["enabled"] is True     # spend caps and limits stay
    assert cfg["observability"]["stream_cost_event"] is True


def test_only_approved_groups_enabled(monkeypatch):
    groups = _lite_config(monkeypatch)["groups"]
    enabled = {k for k, v in groups.items() if isinstance(v, dict) and v.get("enabled")}
    assert enabled <= APPROVED_ON, sorted(enabled - APPROVED_ON)


def test_profile_names_no_provider():
    with open(_TEMPLATE, encoding="utf-8") as fh:
        names = {p["name"] for p in yaml.safe_load(fh)["providers"]}
    with open(_PROFILE, encoding="utf-8") as fh:
        text = "".join(line for line in fh if not line.lstrip().startswith("#")).lower()
    assert not [n for n in names if n.lower() in text]


# ── Pass-through: the provider receives the request the client sent ─────────────────────

_TOOLS = [
    {"type": "function", "function": {
        "name": "read_file", "description": "Read a file from the repository.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "Write a file in the repository.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"},
                                                        "content": {"type": "string"}},
                       "required": ["path", "content"]}}},
]

_MESSAGES = [
    {"role": "system", "content": "You are a careful coding agent. Follow the repo rules. " * 40},
    {"role": "user", "content": "Fix the failing test in tests/test_math.py. "
                                "Questions go to dev.lead@example.com."},
    {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call_1", "type": "function",
        "function": {"name": "read_file", "arguments": "{\"path\": \"tests/test_math.py\"}"}}]},
    {"role": "tool", "tool_call_id": "call_1",
     "content": "def test_add():\n    assert add(1, 2) == 4\n\n" * 30},
    {"role": "user", "content": "Propose the patch as a unified diff."},
]

_CLIENT_PARAMS = {"tools": _TOOLS, "tool_choice": "auto", "temperature": 0.2,
                  "top_p": 0.9, "max_tokens": 512, "seed": 7}

# Keys the proxy itself adds to the provider call. Transport (credentials, endpoint,
# timeouts/retries) and provider prompt caching only; nothing that changes what the model
# is asked or how it answers.
#   api_key / timeout / max_retries  the provider credential and the resilience call limits
#   prompt_cache_key                 G21's tenant-scoped provider prompt-cache key (where the
#                                    provider supports one)
#   stream_options                   streams only: the usage chunk the proxy prices from
_ALLOWED_ADDED = {"api_key", "timeout", "max_retries", "prompt_cache_key", "stream_options"}

_MODELS = ["gpt-4o-mini", "claude-haiku-4-5", "gemini-2.5-flash"]


def _response_mock(model):
    mock = MagicMock()
    mock.model_dump.return_value = {
        "id": "chatcmpl-lite", "object": "chat.completion", "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "--- a/x"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 900, "completion_tokens": 40, "total_tokens": 940},
    }
    return mock


def _stream_mock(chunks):
    async def fake(**kwargs):
        fake.kwargs = kwargs

        async def gen():
            for c in chunks:
                yield c
        return gen()
    fake.kwargs = None
    return fake


def _patches(lite_cfg, acompletion):
    from tests import conftest as c
    return [
        patch("auth.api_key_manager._fetch_secret", return_value=c._VALID_KEYS_JSON),
        patch("auth.api_key_manager._KEY_CACHE", c._setup_auth_keys()),
        patch("auth.api_key_manager._CACHE_LOADED_AT", 9999999999.0),
        patch("config_loader.load_config"),
        patch("config_loader.start_hot_reload"),
        patch("config_loader.get_config", return_value=lite_cfg),
        patch("litellm.acompletion", acompletion),
        patch("auth.api_key_manager._access_llm_key_secret", return_value="sk-test-key"),
        patch("middleware.g05_cache.G05Cache.store_response", new_callable=AsyncMock),
        patch("middleware.g18_observability._emit_trace", new_callable=AsyncMock),
        patch("cache.redis_pool.init_pool"),
        patch("cache.redis_pool.close_pool", new_callable=AsyncMock),
        patch("cache.redis_pool.get_redis", return_value=c._make_mock_redis()),
        patch("middleware.g00_rate_limit._get_redis", return_value=c._make_mock_redis()),
        patch("middleware.g17_loop_control._get_redis", return_value=c._make_mock_redis()),
    ]


async def _send(lite_cfg, body, acompletion):
    import contextlib
    import importlib

    from httpx import ASGITransport, AsyncClient
    # G00 runs its token bucket as a Redis script, which the mock Redis cannot execute (it
    # would answer 429). Limits are not what this test is about; the profile keeps them on
    # (test_profile_effective_config).
    lite_cfg = copy.deepcopy(lite_cfg)
    lite_cfg["rate_limit"]["enabled"] = False
    with contextlib.ExitStack() as stack:
        for p in _patches(lite_cfg, acompletion):
            stack.enter_context(p)
        import main
        importlib.reload(main)
        async with AsyncClient(transport=ASGITransport(app=main.app),
                               base_url="http://test") as client:
            resp = await client.post("/v1/chat/completions", json=body,
                                     headers={"Authorization": "Bearer test-key"})
            text = resp.text
        return resp, text


def _assert_forwarded_unchanged(kwargs, model):
    assert kwargs["model"].split("/")[-1] == model
    assert kwargs["messages"] == _MESSAGES
    for key, value in _CLIENT_PARAMS.items():
        assert kwargs.get(key) == value, key
    sent = set(kwargs) - {"model", "messages", "stream"} - set(_CLIENT_PARAMS)
    assert sent <= _ALLOWED_ADDED, sorted(sent - _ALLOWED_ADDED)
    if "stream_options" in kwargs:
        assert kwargs.get("stream") is True
        assert kwargs["stream_options"] == {"include_usage": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("model", _MODELS)
async def test_lite_pass_through_is_byte_faithful(monkeypatch, model):
    lite = _lite_config(monkeypatch)
    acompletion = AsyncMock(return_value=_response_mock(model))
    body = {"model": model, "messages": copy.deepcopy(_MESSAGES), **copy.deepcopy(_CLIENT_PARAMS)}
    resp, _ = await _send(lite, body, acompletion)
    assert resp.status_code == 200, resp.text
    assert acompletion.await_count == 1             # one provider call, no judge/cascade
    _assert_forwarded_unchanged(acompletion.call_args.kwargs, model)
    assert resp.headers["x-tokenlean-routed-model"].split("/")[-1] == model
    assert float(resp.headers["x-tokenlean-cost-usd"]) > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("model", _MODELS)
async def test_lite_stream_pass_through_is_byte_faithful(monkeypatch, model):
    lite = _lite_config(monkeypatch)
    chunks = [
        {"id": "c1", "object": "chat.completion.chunk", "model": model,
         "choices": [{"index": 0, "delta": {"role": "assistant", "content": "--- a/"}}]},
        {"id": "c1", "object": "chat.completion.chunk", "model": model,
         "choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": "stop"}]},
        {"id": "c1", "object": "chat.completion.chunk", "model": model, "choices": [],
         "usage": {"prompt_tokens": 900, "completion_tokens": 2, "total_tokens": 902}},
    ]
    acompletion = _stream_mock(chunks)
    body = {"model": model, "messages": copy.deepcopy(_MESSAGES), "stream": True,
            **copy.deepcopy(_CLIENT_PARAMS)}
    resp, text = await _send(lite, body, acompletion)
    assert resp.status_code == 200
    _assert_forwarded_unchanged(acompletion.kwargs, model)
    frames = [f for f in text.split("\n\n") if f.strip()]
    relayed = [json.loads(f[len("data: "):]) for f in frames if f.startswith("data: {")]
    assert relayed == chunks                         # the provider's chunks, unchanged
    assert frames[-1] == "data: [DONE]"
    assert frames[-2].startswith(": x-tokenlean-cost ")
