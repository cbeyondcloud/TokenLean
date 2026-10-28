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
    "G21_cache_alignment",    # provider prompt-cache key only (its editing knobs are off)
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
    # The profile alone: a checkout's own gitignored config/params/*.yaml (a commercial
    # tiers.yaml with a rate_card) is not part of what the profile sets.
    monkeypatch.setattr(config_loader, "merge_params_dir", lambda base, params_dir: base)
    return copy.deepcopy(config_loader.load_config(required=True))


def test_profile_is_a_mapping_overlay():
    with open(_PROFILE, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    assert isinstance(data, dict)
    assert set(data) <= {"groups", "observability", "rate_limit", "billing", "key_store"}


def test_profile_effective_config(monkeypatch):
    cfg = _lite_config(monkeypatch)
    groups = cfg["groups"]
    for key in MUST_BE_OFF:
        assert groups[key]["enabled"] is False, key
    g5 = groups["G5_cache"]
    assert g5["enabled"] is True
    assert g5["step_cache_enabled"] is False       # X-Step-Name would replay answers
    assert g5["l2_enabled"] is False               # near-match answers
    assert groups["G6_routing"]["on_unconfigured_model"] == "reject"
    assert groups["G12_reasoning"]["reasoning_headroom"]["enabled"] is False
    g21 = groups["G21_cache_alignment"]
    assert g21["enabled"] is True
    assert g21["stabilise"]["enabled"] is False
    assert g21["providers"]["openai"]["auto"] is False
    assert g21["providers"]["openai"]["prompt_cache_key"] is True
    assert g21["providers"]["anthropic"]["marker"] is False
    assert groups["G18_observability"]["enabled"] is True
    assert groups["G18_observability"]["langfuse_enabled"] is False   # optional, own overlay
    for key in ("G29_pii_redaction", "G30_guardrails", "G31_context_trust",
                "G32_tool_eligibility"):
        assert groups[key]["enabled"] is True
        assert groups[key]["mode"] == "flag", key   # records, never edits or blocks
    assert cfg["rate_limit"]["enabled"] is True     # spend caps and limits stay
    assert cfg["observability"]["stream_cost_event"] is True


def test_profile_turns_the_team_level_on(monkeypatch):
    """LITE runs teams under one tenant (ST-1, ST-2, ST-9): spend caps are enforced (teams'
    own caps included; the tenant has none unless one is set), the X-Agentic-* usage tags are
    recorded, and proxy keys live in Postgres so /admin/keys works on every replica."""
    from auth.key_store_config import key_store_settings
    from billing.tags import DEFAULT_TAG_HEADERS, extract_tags
    cfg = _lite_config(monkeypatch)
    spend = cfg["rate_limit"]["spend_cap"]
    assert spend["enabled"] is True
    assert "monthly_spend_cap_usd" not in (cfg.get("limits") or {})   # no tenant cap assumed
    assert not (cfg.get("billing") or {}).get("rate_card")            # nor a tier default
    assert cfg["billing"]["metering"]["tags"]["enabled"] is True
    sent = {name: f"v-{key}" for name, key in DEFAULT_TAG_HEADERS.items()}
    assert extract_tags({**sent, "X-Not-Listed": "x"}, cfg) == {
        key: f"v-{key}" for key in DEFAULT_TAG_HEADERS.values()}
    assert key_store_settings(cfg)["backend"] == "postgres"
    assert "teams" not in cfg              # teams are the deployment's, via /admin/routing


def test_only_approved_groups_enabled(monkeypatch):
    groups = _lite_config(monkeypatch)["groups"]
    enabled = {k for k, v in groups.items() if isinstance(v, dict) and v.get("enabled")}
    assert enabled <= APPROVED_ON, sorted(enabled - APPROVED_ON)


def test_profile_names_no_provider():
    """No provider is assumed. Provider names appear only under G21's per-provider block,
    and only to switch OFF the knobs that would edit a request (OpenAI's system-message
    reordering, Anthropic's cache_control markers)."""
    with open(_TEMPLATE, encoding="utf-8") as fh:
        names = {p["name"] for p in yaml.safe_load(fh)["providers"]}
    with open(_PROFILE, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    g21_providers = data["groups"]["G21_cache_alignment"].pop("providers")
    assert g21_providers == {"openai": {"auto": False}, "anthropic": {"marker": False}}
    text = yaml.safe_dump(data).lower()
    assert not [n for n in names if n.lower() in text]


# ── Pass-through: the provider receives the request the client sent ─────────────────────
#
# The fixture gives each optimisation something to act on: a long system prompt with
# redundant whitespace, ~30 turns of verbose history, a turn the user repeats verbatim, a
# system message in the middle of the conversation, a tool call answered by a large JSON
# body, 40 tool definitions and an e-mail address (G29 runs in `flag` mode).


def _tool(name, description, props):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props, "required": sorted(props)}}}


_TOOLS = [
    _tool("read_file", "Read a file from the repository.", {"path": {"type": "string"}}),
    _tool("write_file", "Write a file in the repository.",
          {"path": {"type": "string"}, "content": {"type": "string"}}),
] + [
    _tool(f"repo_tool_{i}", f"Repository helper {i}: inspects the working tree. " * 12,
          {"target": {"type": "string", "description": "The path or symbol to act on. " * 8}})
    for i in range(38)
]


def _build_messages():
    filler = "Here is the context you asked for, with the details repeated.    \n\n   "
    msgs = [{"role": "system",
             "content": "You are a careful coding agent.   \n\n  Follow the repo rules.   " * 60}]
    for i in range(14):
        msgs.append({"role": "user", "content": f"Look at module {i}. " + filler * 30})
        msgs.append({"role": "assistant",
                     "content": f"Module {i} reviewed. " + "I checked it line by line. " * 25})
        if i == 6:
            msgs.append({"role": "system", "content": "Reminder: never edit files under /vendor."})
    ask = "Fix the failing test in tests/test_math.py. Questions go to dev.lead@example.com."
    msgs.append({"role": "user", "content": ask})
    msgs.append({"role": "user", "content": ask})             # repeated verbatim
    msgs.append({"role": "user", "content": "Start by reading that test file."})
    msgs.append({"role": "assistant", "content": None, "tool_calls": [{
        "id": "call_1", "type": "function",
        "function": {"name": "read_file", "arguments": "{\"path\": \"tests/test_math.py\"}"}}]})
    msgs.append({"role": "tool", "tool_call_id": "call_1", "content": json.dumps(
        [{"id": i, "name": f"case_{i}", "value": None, "tags": [], "notes": ""}
         for i in range(200)], indent=4)})
    msgs.append({"role": "user", "content": "Propose the patch as a unified diff."})
    return msgs


_MESSAGES = _build_messages()

_CLIENT_PARAMS = {"tools": _TOOLS, "tool_choice": "auto", "temperature": 0.2,
                  "top_p": 0.9, "max_tokens": 512, "seed": 7}
# A reasoning model takes its output budget as max_completion_tokens.
_REASONING_PARAMS = {k: v for k, v in _CLIENT_PARAMS.items() if k != "max_tokens"}
_REASONING_PARAMS["max_completion_tokens"] = 512

# Keys the proxy itself adds to the provider call. Transport (credentials, endpoint,
# timeouts/retries) and provider prompt caching only; nothing that changes what the model
# is asked or how it answers.
#   api_key / timeout / max_retries  the provider credential and the resilience call limits
#   prompt_cache_key                 G21's tenant-scoped provider prompt-cache key (where the
#                                    provider supports one)
#   stream_options                   streams only: the usage chunk the proxy prices from
_ALLOWED_ADDED = {"api_key", "timeout", "max_retries", "prompt_cache_key", "stream_options"}

_MODELS = ["gpt-4o-mini", "claude-haiku-4-5", "gemini-2.5-flash"]
_REASONING_MODEL = "o4-mini"
_REPLY = "--- a/x"


def _params_for(model):
    return _REASONING_PARAMS if model == _REASONING_MODEL else _CLIENT_PARAMS


def _response_mock(model):
    mock = MagicMock()
    mock.model_dump.return_value = {
        "id": "chatcmpl-lite", "object": "chat.completion", "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": _REPLY},
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


def _forwarded_changes(kwargs, model, messages=None, params=None):
    """How the provider call differs from the client's request ([] = forwarded as sent):
    the model, the messages, each client parameter, and any key the proxy added beyond
    transport and provider prompt caching."""
    messages = _MESSAGES if messages is None else messages
    params = _CLIENT_PARAMS if params is None else params
    if not kwargs:
        return ["no single provider call"]
    changes = []
    if kwargs["model"].split("/")[-1] != model:
        changes.append(f"model -> {kwargs['model']}")
    if kwargs["messages"] != messages:
        changes.append("messages")
    changes += [f"param {k}" for k, v in params.items() if kwargs.get(k) != v]
    added = set(kwargs) - {"model", "messages", "stream"} - set(params) - _ALLOWED_ADDED
    changes += [f"added {k}" for k in sorted(added)]
    if "stream_options" in kwargs and (kwargs.get("stream") is not True
                                       or kwargs["stream_options"] != {"include_usage": True}):
        changes.append("stream_options")
    return changes


def _assert_forwarded_unchanged(kwargs, model, messages=None, params=None):
    assert _forwarded_changes(kwargs, model, messages, params) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("model", _MODELS + [_REASONING_MODEL])
async def test_lite_pass_through_is_byte_faithful(monkeypatch, model):
    lite = _lite_config(monkeypatch)
    params = _params_for(model)
    acompletion = AsyncMock(return_value=_response_mock(model))
    body = {"model": model, "messages": copy.deepcopy(_MESSAGES), **copy.deepcopy(params)}
    resp, _ = await _send(lite, body, acompletion)
    assert resp.status_code == 200, resp.text
    assert acompletion.await_count == 1             # one provider call, no judge/cascade
    _assert_forwarded_unchanged(acompletion.call_args.kwargs, model, params=params)
    assert resp.json()["choices"][0]["message"]["content"] == _REPLY   # answer unchanged
    assert resp.headers["x-tokenlean-routed-model"].split("/")[-1] == model
    assert "x-tokenlean-model-substituted" not in resp.headers
    assert "x-tokenlean-output-budget-raised" not in resp.headers
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


# ── Mutation coverage: would the pass-through test notice an optimisation coming back? ──
#
# Every group the profile turns off is EITHER switched back on below and caught by the body
# comparison, OR listed in GATED_OFF with the reason a unit test cannot make it act on this
# request. Gated groups are pinned off by test_profile_effective_config (MUST_BE_OFF) and
# test_only_approved_groups_enabled: for them the guard is the config, not the body.

_SIMPLE = [{"role": "user", "content": "What is 2+2?"}]

# group -> (model, messages); messages None = the rich fixture
DETECTED_WHEN_ON = {
    "G6_routing": ("gpt-4o", _SIMPLE),               # cascade sends a simple ask down a tier
    "G10_memory": ("gpt-4o-mini", None),             # trims / summarises history
    "G12_reasoning": ("gpt-4o-mini", None),          # injects a budget prompt
    "G16_agent_arch": ("gpt-4o-mini", None),         # prunes tool definitions
    "G19_headroom": ("gpt-4o-mini", None),           # compacts the JSON tool result
    "g22_deduplication": ("gpt-4o-mini", None),      # drops the repeated turn
    "G25_adaptive_reasoning": (_REASONING_MODEL, None),   # sets reasoning_effort
    "G28_ccr": ("gpt-4o-mini", None),                # advertises its own tools
}

GATED_OFF = {
    "G1_compression": "needs the LLMLingua sidecar, which LITE does not run (fails open)",
    "G2_template_registry": "read-only registry: warns on budgets, never edits a request",
    "G3_doc_pipeline": "the RAG fallback chain, used only from G07",
    "G4_bypass": "answers only from bypass rules; none ship and LITE has no rules table",
    "G7_retrieval": "needs a vector store and documents ingested for the tenant",
    "G8_tools": "prunes only tools listed in its registry; a caller's tools are not",
    "G9_context_schema": "acts only on configured schema fields",
    "G11_output": "acts on caller-requested output formats; its max_tokens loop ships off",
    "G13_batch": "defers only requests that opt in to batching",
    "G14_tool_output": "minimises results of server-executed tools only (G15/G28)",
    "G15_server_compute": "executes only tools G28 injected",
    "G17_loop": "keyed on X-Workflow-ID, which the caller chooses to send",
    "g20_prompt_optimizer": "needs learned templates stored in Redis",
    "G23_streaming_compression": "measurement only: records repetition, never edits output",
    "G26_context_budget": "fires only past 85% of the model's context window",
    "G27_multimodal": "reserved slot: no transform ships",
}


def _set(path, value):
    def mutate(cfg):
        node = cfg
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
    return mutate


# Knobs inside groups LITE keeps (or reads while off) that the profile sets so the request is
# untouched; switched back on, each is caught.
_KNOB_MUTATIONS = {
    "G21 openai.auto moves the mid-conversation system message": (
        "gpt-4o-mini",
        _set(["groups", "G21_cache_alignment", "providers", "openai", "auto"], True)),
    "G21 anthropic.marker adds cache_control": (
        "claude-haiku-4-5",
        _set(["groups", "G21_cache_alignment", "providers", "anthropic", "marker"], True)),
    "G12 reasoning_headroom raises the output budget": (
        _REASONING_MODEL,
        _set(["groups", "G12_reasoning", "reasoning_headroom", "enabled"], True)),
}


def test_every_off_group_is_either_detected_or_gated():
    assert not set(DETECTED_WHEN_ON) & set(GATED_OFF)
    assert set(DETECTED_WHEN_ON) | set(GATED_OFF) == set(MUST_BE_OFF)


async def _changes_with(monkeypatch, mutate, model, messages=None):
    lite = _lite_config(monkeypatch)
    mutate(lite)
    messages = _MESSAGES if messages is None else messages
    params = _params_for(model)
    acompletion = AsyncMock(return_value=_response_mock(model))
    body = {"model": model, "messages": copy.deepcopy(messages), **copy.deepcopy(params)}
    resp, _ = await _send(lite, body, acompletion)
    kwargs = acompletion.call_args.kwargs if acompletion.await_count == 1 else {}
    changes = _forwarded_changes(kwargs, model, messages, params)
    if resp.status_code != 200:
        changes.append(f"status {resp.status_code}")
    return changes


@pytest.mark.asyncio
@pytest.mark.parametrize("group", sorted(DETECTED_WHEN_ON))
async def test_switching_a_group_back_on_is_detected(monkeypatch, group):
    model, messages = DETECTED_WHEN_ON[group]

    def on(cfg):
        cfg["groups"][group]["enabled"] = True

    # control: the same request under the unmodified profile is forwarded as sent
    assert await _changes_with(monkeypatch, lambda cfg: None, model, messages) == []
    assert await _changes_with(monkeypatch, on, model, messages) != []


@pytest.mark.asyncio
@pytest.mark.parametrize("knob", sorted(_KNOB_MUTATIONS))
async def test_switching_a_knob_back_on_is_detected(monkeypatch, knob):
    model, mutate = _KNOB_MUTATIONS[knob]
    assert await _changes_with(monkeypatch, lambda cfg: None, model) == []
    assert await _changes_with(monkeypatch, mutate, model) != []


@pytest.mark.asyncio
async def test_lite_refuses_an_unconfigured_model(monkeypatch):
    """LITE never answers with a model the caller did not ask for: an unconfigured model is
    a 400 naming it, and no provider is called (the shipped default substitutes)."""
    lite = _lite_config(monkeypatch)
    assert lite["groups"]["G6_routing"]["on_unconfigured_model"] == "reject"
    acompletion = AsyncMock(return_value=_response_mock("gpt-4o-mini"))
    body = {"model": "my-custom-model-x", "messages": copy.deepcopy(_MESSAGES)}
    resp, _ = await _send(lite, body, acompletion)
    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert error["code"] == "model_not_configured"
    assert "my-custom-model-x" in error["message"]
    assert acompletion.await_count == 0
    assert "x-tokenlean-model-substituted" not in resp.headers


# ── Teams under LITE: a team-bound key, its spend cap, its usage tags ────────────────────

_TEAM = "payments"
_TEAM_KEY = "lite-team-key-payments"
_TEAM_TENANT = "LITE-LOC-01"


def _team_counter():
    import time
    return f"t:{_TEAM_TENANT}:team:{_TEAM}:spend:{time.strftime('%Y%m', time.gmtime())}"


def _with_team(lite, cap=5.0):
    cfg = copy.deepcopy(lite)
    cfg["teams"] = {_TEAM: {"monthly_spend_cap_usd": cap}}
    return cfg


async def _send_team(lite_cfg, body, acompletion, team_spent=None, headers=None):
    """_send with a team-bound key of the LITE tenant, the team's spend counter at
    ``team_spent`` and the request's usage tags captured."""
    import contextlib
    import hashlib
    import importlib

    from httpx import ASGITransport, AsyncClient

    from tests import conftest as c
    lite_cfg = copy.deepcopy(lite_cfg)
    lite_cfg["rate_limit"]["enabled"] = False     # the token bucket (see _send); caps stay on
    keys = c._setup_auth_keys()
    keys[hashlib.sha256(_TEAM_KEY.encode()).hexdigest()] = {
        "tenant_id": _TEAM_TENANT, "tier": "free", "team": _TEAM}
    redis = c._make_mock_redis()
    redis.get = AsyncMock(side_effect=lambda key: team_spent if key == _team_counter() else None)
    seen = {}
    with contextlib.ExitStack() as stack:
        for p in _patches(lite_cfg, acompletion):
            stack.enter_context(p)
        stack.enter_context(patch("auth.api_key_manager._KEY_CACHE", keys))
        stack.enter_context(patch("middleware.g00_rate_limit._get_redis", return_value=redis))
        import main
        importlib.reload(main)
        record = main._record_outcome

        def capture(ctx, *args, **kwargs):
            seen.update(team=ctx.team, tags=getattr(ctx, "usage_tags", None))
            return record(ctx, *args, **kwargs)
        stack.enter_context(patch.object(main, "_record_outcome", capture))
        async with AsyncClient(transport=ASGITransport(app=main.app),
                               base_url="http://test") as client:
            resp = await client.post("/v1/chat/completions", json=body, headers={
                "Authorization": f"Bearer {_TEAM_KEY}", **(headers or {})})
        return resp, seen


@pytest.mark.asyncio
async def test_lite_serves_a_team_key_unchanged_and_records_its_team_and_tags(monkeypatch):
    lite = _with_team(_lite_config(monkeypatch))
    acompletion = AsyncMock(return_value=_response_mock("gpt-4o-mini"))
    body = {"model": "gpt-4o-mini", "messages": copy.deepcopy(_MESSAGES),
            **copy.deepcopy(_CLIENT_PARAMS)}
    resp, seen = await _send_team(lite, body, acompletion, team_spent="1.0", headers={
        "X-Agentic-Run-Id": "run-42", "X-Agentic-Step": "plan", "X-Not-Listed": "x",
        "X-Team": "another-team"})                      # the key's binding wins
    assert resp.status_code == 200, resp.text
    _assert_forwarded_unchanged(acompletion.call_args.kwargs, "gpt-4o-mini")
    assert seen["team"] == _TEAM
    assert seen["tags"] == {"run_id": "run-42", "step": "plan"}


@pytest.mark.asyncio
async def test_lite_honours_a_team_spend_cap(monkeypatch):
    lite = _with_team(_lite_config(monkeypatch), cap=5.0)
    acompletion = AsyncMock(return_value=_response_mock("gpt-4o-mini"))
    body = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}
    resp, _ = await _send_team(lite, body, acompletion, team_spent="5.0")
    assert resp.status_code == 429, resp.text
    assert resp.json()["error"]["code"] == "team_spend_cap_exceeded"
    assert acompletion.await_count == 0                 # refused before any provider call
