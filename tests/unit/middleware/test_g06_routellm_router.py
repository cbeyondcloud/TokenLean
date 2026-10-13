"""
G06's RouteLLM classifier: which router, at which threshold, reaches the sidecar.

The default router is `bert`: its checkpoint is Apache-2.0 on an MIT base model, while `mf`'s
was published with no licence and `causal_llm`'s is a Meta Llama 3 derivative. `mf` and
`sw_ranking` embed the prompt with OpenAI inside the sidecar, with the TENANT's own OpenAI key
(the owner's rule: every model key is the tenant's), sent per request; without one G06 logs the
switch and uses `bert` at bert's own threshold, because a threshold calibrated for one router
means nothing to another. Each default threshold is RouteLLM's calibration for ~50%
strong-model calls on its published Chatbot Arena scores (`routellm.calibrate_threshold
--strong-model-pct 0.5`; it gives mf the README's 0.11593).
"""
import logging

import httpx
import pytest

import middleware.g06_routing as g06

MESSAGES = [{"role": "user", "content": "hi"}]
BASE = {"url": "http://routellm-svc:8081", "weak_model": "weak-m", "strong_model": "strong-m"}
TENANT_KEY = "sk-tenant-own-key-123"


class _Resp:
    def __init__(self, body=None):
        self._body = body or {"routed_model": "weak-m"}

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class _Posts(list):
    """The JSON bodies G06 posts to the sidecar; `.headers` holds each post's headers."""

    def __init__(self):
        super().__init__()
        self.headers = []
        self.answer = None


@pytest.fixture
def sent(monkeypatch):
    posts = _Posts()

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, json=None, headers=None):
            posts.append(json)
            posts.headers.append(dict(headers or {}))
            return _Resp(posts.answer)

    monkeypatch.setattr(g06.httpx, "AsyncClient", _Client)
    monkeypatch.setattr("ml_models.cloud_run_auth_headers", lambda url: {})
    return posts


@pytest.fixture
def keys():
    """A fake tenant-key resolver: `table[(provider, tenant)]` is the tenant's own key,
    `denied` holds pairs strict BYOK refuses, `asked` records every lookup."""
    import types

    from providers.key_resolver import (
        ProviderKeyError,
        reset_provider_key_resolver,
        set_provider_key_resolver,
    )
    state = types.SimpleNamespace(table={}, denied=set(), asked=[])

    async def resolver(provider, tenant_id, ctx=None):
        state.asked.append((provider, tenant_id))
        if (provider, tenant_id) in state.denied:
            raise ProviderKeyError(provider, tenant_id)
        return state.table.get((provider, tenant_id))

    set_provider_key_resolver(resolver)
    yield state
    reset_provider_key_resolver()


@pytest.fixture(autouse=True)
def _fresh_warnings():
    g06._ROUTELLM_NO_KEY_WARNED.clear()


def _openai_key(keys, present, tenant="default"):
    keys.table.pop(("openai", tenant), None)
    if present:
        keys.table[("openai", tenant)] = TENANT_KEY


async def _route(cfg):
    return await g06._classify_routellm(MESSAGES, {}, {"routellm": {**BASE, **cfg}})


@pytest.mark.parametrize("key", [True, False])   # with a key, an mf default would show
async def test_an_unset_router_is_bert_at_its_calibrated_threshold(sent, keys, key):
    _openai_key(keys, key)
    assert await _route({}) == "simple"
    assert (sent[0]["router"], sent[0]["threshold"]) == ("bert", 0.4066)


@pytest.mark.parametrize("router, threshold", [
    ("bert", 0.4066), ("mf", 0.11593), ("sw_ranking", 0.21647), ("causal_llm", 0.0962)])
async def test_a_named_router_without_a_threshold_gets_its_own_calibration(
        sent, keys, router, threshold):
    _openai_key(keys, True)
    await _route({"router": router})
    assert (sent[0]["router"], sent[0]["threshold"]) == (router, threshold)


async def test_an_operator_threshold_is_sent_as_configured(sent, keys):
    _openai_key(keys, True)
    await _route({"router": "mf", "threshold": 0.2})
    assert (sent[0]["router"], sent[0]["threshold"]) == ("mf", 0.2)


@pytest.mark.parametrize("router", ["mf", "sw_ranking"])
async def test_an_embedding_router_without_an_openai_key_falls_back_to_bert_and_logs_it(
        sent, keys, caplog, router):
    _openai_key(keys, False)
    with caplog.at_level(logging.WARNING, logger=g06.logger.name):
        await _route({"router": router, "threshold": 0.2})
    # bert at bert's calibration: the configured 0.2 was tuned for the other router.
    assert (sent[0]["router"], sent[0]["threshold"]) == ("bert", 0.4066)
    assert [r for r in caplog.records
            if r.levelno == logging.WARNING and router in r.getMessage() and "bert" in r.getMessage()]


@pytest.mark.parametrize("router", ["bert", "causal_llm"])
async def test_a_local_router_needs_no_key_and_is_used_as_configured(sent, keys, router):
    _openai_key(keys, False)
    await _route({"router": router})
    assert sent[0]["router"] == router
    assert keys.asked == [], "a local router has no embedding, so no key is looked up"
    assert "X-Embedding-Key" not in sent.headers[0]


# `_token_opt` documents routellm_router_used and routellm_threshold; nothing set them.
@pytest.mark.parametrize("key, router, threshold", [(True, "mf", 0.2), (False, "bert", 0.4066)])
async def test_a_decision_records_the_router_and_threshold_it_was_made_with(
        sent, keys, make_ctx, key, router, threshold):
    """Without an OpenAI key mf runs as bert at bert's threshold, and the fields say so."""
    ctx = make_ctx(MESSAGES)
    _openai_key(keys, key, tenant=ctx.tenant_id)
    tier = await g06._classify_routellm(
        MESSAGES, {}, {"routellm": {**BASE, "router": "mf", "threshold": 0.2}}, ctx=ctx)
    assert tier == "simple"
    assert (ctx.savings.routellm_router_used, ctx.savings.routellm_threshold) == (router, threshold)
    assert ctx.classifier_fallback is None


class _Answer:
    def __init__(self, model):
        self._model = model

    def raise_for_status(self):
        pass

    def json(self):
        return {"routed_model": self._model, "reason": "below_threshold"}


def _sidecar(monkeypatch, *, raises=None, answers="weak-m"):
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, json=None, headers=None):
            if raises is not None:
                raise raises
            return _Answer(answers)

    monkeypatch.setattr(g06.httpx, "AsyncClient", _Client)
    monkeypatch.setattr("ml_models.cloud_run_auth_headers", lambda url: {})


@pytest.mark.parametrize("cfg, raises, answers, reason", [
    ({"url": ""}, None, "weak-m", "no_config"),
    ({"weak_model": ""}, None, "weak-m", "no_config"),
    ({}, httpx.ReadTimeout("slow"), "weak-m", "timeout"),
    ({}, httpx.ConnectError("refused"), "weak-m", "http_error"),
    ({}, None, "a-model-nobody-named", "bad_answer"),
    ({}, RuntimeError("boom"), "weak-m", "error"),
])
async def test_a_fallback_says_why_and_records_no_decision(
        monkeypatch, keys, make_ctx, cfg, raises, answers, reason):
    """The keyword rules decide, and routing_mode will say routellm+fallback:<reason>."""
    _sidecar(monkeypatch, raises=raises, answers=answers)
    ctx = make_ctx(MESSAGES)
    _openai_key(keys, True, tenant=ctx.tenant_id)
    tier = await g06._classify_routellm(MESSAGES, {}, {"routellm": {**BASE, **cfg}}, ctx=ctx)
    assert tier == g06._classify_heuristic(MESSAGES, {})[0]
    assert ctx.classifier_fallback == reason
    assert (ctx.savings.routellm_router_used, ctx.savings.routellm_threshold) == (None, None)


# ── #99: the tenant's own OpenAI key, sent per request · #108: the embedding is priced ──

@pytest.mark.parametrize("router", ["mf", "sw_ranking"])
async def test_an_embedding_router_sends_the_tenants_own_key(sent, keys, make_ctx, router):
    ctx = make_ctx(MESSAGES)
    _openai_key(keys, True, tenant=ctx.tenant_id)
    await g06._classify_routellm(MESSAGES, {}, {"routellm": {**BASE, "router": router}}, ctx=ctx)
    assert keys.asked == [("openai", ctx.tenant_id)]
    assert sent[0]["router"] == router
    assert sent.headers[0]["X-Embedding-Key"] == TENANT_KEY
    assert TENANT_KEY not in str(sent[0]), "the key goes in a header, never in the body"


@pytest.mark.parametrize("deny", [False, True], ids=["no-key", "strict-byok-refuses"])
async def test_a_tenant_without_its_own_key_gets_bert_and_one_warning(
        sent, keys, make_ctx, caplog, deny):
    """bert decides, no key is sent, and the tenant is warned once, not on every request."""
    ctx = make_ctx(MESSAGES)
    _openai_key(keys, False, tenant=ctx.tenant_id)
    if deny:
        keys.denied.add(("openai", ctx.tenant_id))
    cfg = {"routellm": {**BASE, "router": "mf"}}
    with caplog.at_level(logging.WARNING, logger=g06.logger.name):
        for _ in range(3):
            await g06._classify_routellm(MESSAGES, {}, cfg, ctx=ctx)
    assert [p["router"] for p in sent] == ["bert"] * 3
    assert all("X-Embedding-Key" not in h for h in sent.headers)
    warned = [r for r in caplog.records if r.levelno == logging.WARNING and "bert" in r.getMessage()]
    assert len(warned) == 1 and ctx.tenant_id in warned[0].getMessage()


async def test_the_key_never_reaches_a_log_line(sent, keys, make_ctx, caplog):
    ctx = make_ctx(MESSAGES)
    _openai_key(keys, True, tenant=ctx.tenant_id)
    with caplog.at_level(logging.DEBUG):
        await g06._classify_routellm(MESSAGES, {}, {"routellm": {**BASE, "router": "mf"}}, ctx=ctx)
    assert caplog.records and all(TENANT_KEY not in r.getMessage() for r in caplog.records)


async def test_the_embedding_is_booked_as_a_side_call(sent, keys, make_ctx):
    """#108: the sidecar's embedding call was never priced."""
    ctx = make_ctx(MESSAGES)
    _openai_key(keys, True, tenant=ctx.tenant_id)
    sent.answer = {"routed_model": "weak-m",
                   "embedding_usage": {"model": "text-embedding-3-small", "prompt_tokens": 12}}
    await g06._classify_routellm(MESSAGES, {}, {"routellm": {**BASE, "router": "mf"}}, ctx=ctx)
    assert ctx.provider_calls == [{"model": "openai/text-embedding-3-small", "prompt_tokens": 12,
                                   "completion_tokens": 0, "reasoning_tokens": 0,
                                   "cached_tokens": 0, "side": True}]


async def test_a_router_that_embeds_nothing_books_nothing(sent, keys, make_ctx):
    ctx = make_ctx(MESSAGES)
    await g06._classify_routellm(MESSAGES, {}, {"routellm": {**BASE, "router": "bert"}}, ctx=ctx)
    assert ctx.provider_calls == []


def test_the_template_prices_the_embedding(monkeypatch):
    import pathlib

    import yaml
    from savings import calculator
    template = yaml.safe_load((pathlib.Path(__file__).resolve().parents[3] / "config"
                               / "config.yaml.template").read_text(encoding="utf-8"))
    label = "openai/text-embedding-3-small"
    matches = [k for k in template["pricing"] if k != "default" and k.lower() in label]
    assert matches == ["text-embedding-3-small"], "exactly one pricing row may match the label"
    monkeypatch.setattr("config_loader.get_pricing_table", lambda: template["pricing"])
    assert calculator.get_cost_per_1k(label) == (0.00002, 0.0)   # $0.02 per 1M (OpenAI, 2026-10)
