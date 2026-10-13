"""
G06's RouteLLM classifier: which router, at which threshold, reaches the sidecar.

The default router is `bert`: its checkpoint is Apache-2.0 on an MIT base model, while `mf`'s
was published with no licence and `causal_llm`'s is a Meta Llama 3 derivative. `mf` and
`sw_ranking` embed the prompt with OpenAI inside the sidecar, so they run only when an OpenAI
key is configured; without one G06 logs the switch and uses `bert` at bert's own threshold,
because a threshold calibrated for one router means nothing to another. Each default threshold
is RouteLLM's calibration for ~50% strong-model calls on its published Chatbot Arena scores
(`routellm.calibrate_threshold --strong-model-pct 0.5`; it gives mf the README's 0.11593).
"""
import logging

import httpx
import pytest

import middleware.g06_routing as g06

MESSAGES = [{"role": "user", "content": "hi"}]
BASE = {"url": "http://routellm-svc:8081", "weak_model": "weak-m", "strong_model": "strong-m"}


class _Resp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"routed_model": "weak-m", "confidence": 0.9}


@pytest.fixture
def sent(monkeypatch):
    """The JSON bodies G06 posts to the sidecar."""
    bodies = []

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, json=None, headers=None):
            bodies.append(json)
            return _Resp()

    monkeypatch.setattr(g06.httpx, "AsyncClient", _Client)
    monkeypatch.setattr("ml_models.cloud_run_auth_headers", lambda url: {})
    return bodies


def _openai_key(monkeypatch, present):
    monkeypatch.setattr(g06, "_openai_key_available", lambda: present)


async def _route(cfg):
    return await g06._classify_routellm(MESSAGES, {}, {"routellm": {**BASE, **cfg}})


@pytest.mark.parametrize("key", [True, False])   # with a key, an mf default would show
async def test_an_unset_router_is_bert_at_its_calibrated_threshold(sent, monkeypatch, key):
    _openai_key(monkeypatch, key)
    assert await _route({}) == "simple"
    assert (sent[0]["router"], sent[0]["threshold"]) == ("bert", 0.4066)


@pytest.mark.parametrize("router, threshold", [
    ("bert", 0.4066), ("mf", 0.11593), ("sw_ranking", 0.21647), ("causal_llm", 0.0962)])
async def test_a_named_router_without_a_threshold_gets_its_own_calibration(
        sent, monkeypatch, router, threshold):
    _openai_key(monkeypatch, True)
    await _route({"router": router})
    assert (sent[0]["router"], sent[0]["threshold"]) == (router, threshold)


async def test_an_operator_threshold_is_sent_as_configured(sent, monkeypatch):
    _openai_key(monkeypatch, True)
    await _route({"router": "mf", "threshold": 0.2})
    assert (sent[0]["router"], sent[0]["threshold"]) == ("mf", 0.2)


@pytest.mark.parametrize("router", ["mf", "sw_ranking"])
async def test_an_embedding_router_without_an_openai_key_falls_back_to_bert_and_logs_it(
        sent, monkeypatch, caplog, router):
    _openai_key(monkeypatch, False)
    with caplog.at_level(logging.WARNING, logger=g06.logger.name):
        await _route({"router": router, "threshold": 0.2})
    # bert at bert's calibration: the configured 0.2 was tuned for the other router.
    assert (sent[0]["router"], sent[0]["threshold"]) == ("bert", 0.4066)
    assert [r for r in caplog.records
            if r.levelno == logging.WARNING and router in r.getMessage() and "bert" in r.getMessage()]


@pytest.mark.parametrize("router", ["bert", "causal_llm"])
async def test_a_local_router_needs_no_key_and_is_used_as_configured(sent, monkeypatch, router):
    _openai_key(monkeypatch, False)
    await _route({"router": router})
    assert sent[0]["router"] == router


# `_token_opt` documents routellm_router_used and routellm_threshold; nothing set them.
@pytest.mark.parametrize("key, router, threshold", [(True, "mf", 0.2), (False, "bert", 0.4066)])
async def test_a_decision_records_the_router_and_threshold_it_was_made_with(
        sent, monkeypatch, make_ctx, key, router, threshold):
    """Without an OpenAI key mf runs as bert at bert's threshold, and the fields say so."""
    _openai_key(monkeypatch, key)
    ctx = make_ctx(MESSAGES)
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
        monkeypatch, make_ctx, cfg, raises, answers, reason):
    """The keyword rules decide, and routing_mode will say routellm+fallback:<reason>."""
    _openai_key(monkeypatch, True)
    _sidecar(monkeypatch, raises=raises, answers=answers)
    ctx = make_ctx(MESSAGES)
    tier = await g06._classify_routellm(MESSAGES, {}, {"routellm": {**BASE, **cfg}}, ctx=ctx)
    assert tier == g06._classify_heuristic(MESSAGES, {})[0]
    assert ctx.classifier_fallback == reason
    assert (ctx.savings.routellm_router_used, ctx.savings.routellm_threshold) == (None, None)
