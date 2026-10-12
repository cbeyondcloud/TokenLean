"""G06's Jev classifier (`classifier: jev`): TypeSafe's Jev scores a request on the operator's
ordered complexity levels, each naming the G06 tier whose models answer it.

What is pinned here:
- the question Jev is asked: one `score` question, the levels lowest first, and only the
  request's own text (never the system prompt), newest part kept;
- the level rule: the lowest level whose probability, with every level below it, reaches
  `enough_probability`;
- that everything after the tier (strategy, guards, overrides, rules) is unchanged;
- every fallback goes to the DEFAULT classifier (cascade), says why in `routing_mode`, counts,
  and warns once per tenant and reason, never on every request;
- the tenant's own key, cached; the shared breaker sees outages but not one tenant's 401/429;
- the call's cost is recorded even when the answer is refused;
- the shipped template, pricing row and self-hosted key plumbing.
"""
import json
import logging
import re
from pathlib import Path

import httpx
import pytest
import yaml

from middleware import apply_operator_overlay, g06_jev
from middleware.g06_routing import G06Routing, _TIER_ORDER
from providers import key_resolver, resilience
from providers.key_resolver import ProviderKeyError

REPO = Path(__file__).resolve().parents[3]

LEVELS = [
    {"name": "low", "tier": "simple", "describe": "A quick fact or a short rewrite"},
    {"name": "medium", "tier": "medium", "describe": "Explaining or comparing, with some reasoning"},
    {"name": "high", "tier": "complex", "describe": "Multi-step analysis or writing code"},
    {"name": "complex", "tier": "complex", "describe": "Deep design or research"},
]
TIERS = {"simple": ["gpt-4o-mini"], "medium": ["gpt-4o"], "complex": ["gpt-4o"]}
SIMPLE = "What is the capital of France?"
COMPLEX = "Design an algorithm and analyse its time complexity in detail."
# ~100 words with no keyword either way: the keyword rules call it "medium" and are unsure.
UNSURE = (
    "Our nightly export job moved about forty thousand customer records into the reporting "
    "warehouse and then paused for roughly nine minutes before finishing the remaining "
    "batches without any operator action taken at all. The dashboard showed the queue depth "
    "climbing steadily and then draining back down to zero on its own while the on call "
    "engineer was still reading the very first alert page. Walk me through what the most "
    "likely sequence of events was here and whether the pause should be treated as expected "
    "backpressure behaviour or a genuine incident worth escalating to the wider platform "
    "team for a deeper follow up review tomorrow."
)


def _jev(**over):
    cfg = {"url": "https://jev.test/v1/systemone", "model": "jev-1.13.0",
           "key_provider": "typesafe", "timeout_ms": 1000, "max_state_chars": 4000,
           "enough_probability": 0.5, "key_cache_s": 60, "warn_every_s": 3600,
           "instructions": "How hard is this request?",
           "levels": [dict(level) for level in LEVELS]}
    cfg.update(over)
    return cfg


class Keys:
    """The own-key seam, as the commercial resolver would answer it: (tenant, provider) → key."""

    def __init__(self):
        self.keys = {("default", "typesafe"): "ts-key-default"}
        self.denied = set()
        self.calls = []

    async def __call__(self, provider, tenant_id, ctx):
        self.calls.append((tenant_id, provider))
        if (tenant_id, provider) in self.denied:
            raise ProviderKeyError(provider, tenant_id)
        return self.keys.get((tenant_id, provider))


class FakeJev:
    """TypeSafe's /v1/systemone: echoes the criteria it was sent back as the legend."""

    def __init__(self):
        self.requests = []
        self.probabilities = [0.7, 0.2, 0.05, 0.05]
        self.status = 200
        self.body = None            # replaces the whole JSON body when set
        self.raw = None             # replaces the body with non-JSON bytes when set
        self.legend = None          # replaces the echoed legend when set
        self.answer_type = "score"
        self.error = None           # an httpx exception class to raise instead of answering

    def handle(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error("jev unreachable", request=request)
        if self.raw is not None:
            return httpx.Response(self.status, content=self.raw)
        if self.body is not None:
            return httpx.Response(self.status, json=self.body)
        sent = json.loads(request.content)["questions"][g06_jev.QUESTION]["criteria"]
        answer = {
            "type": self.answer_type,
            "score": sum(i * p for i, p in enumerate(self.probabilities)),
            "legend": self.legend or {str(i): text for i, text in enumerate(sent)},
            "probabilities": {str(i): p for i, p in enumerate(self.probabilities)},
            "confidence": 0.4,
        }
        return httpx.Response(self.status, json={
            "model": "jev-1.13.0", "answers": {g06_jev.QUESTION: answer},
            "usage": {"input_tokens": 312, "output_tokens": 48}})

    def sent(self, i=-1):
        return json.loads(self.requests[i].content)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """Fresh module state per test, a fresh breaker store, and reachable tier models."""
    monkeypatch.setattr(g06_jev, "_KEY_CACHE", {})
    monkeypatch.setattr(g06_jev, "_WARNED", {})
    monkeypatch.setattr(g06_jev, "_CONFIGS_CHECKED", {})
    monkeypatch.setattr(resilience, "_STORE", resilience.ResilienceStore())
    providers = [{"name": "openai", "model_prefixes": ["gpt"]}]
    monkeypatch.setattr("middleware.g06_routing.get_providers", lambda: providers)
    monkeypatch.setattr("auth.api_key_manager.get_llm_provider_key", lambda provider: "sk-test")


@pytest.fixture
def keys():
    k = Keys()
    key_resolver.set_provider_key_resolver(k)
    yield k
    key_resolver.reset_provider_key_resolver()


@pytest.fixture
def jev(monkeypatch):
    fake = FakeJev()
    real = httpx.AsyncClient
    monkeypatch.setattr(g06_jev.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(fake.handle), **kw))
    return fake


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(g06_jev, "_clock", lambda: now[0])
    return now


def _config(minimal_config, jev_cfg=None, **g6):
    minimal_config["groups"]["G6_routing"] = {
        "enabled": True, "classifier": "jev", "tiers": dict(TIERS),
        "jev": jev_cfg if jev_cfg is not None else _jev(), **g6}
    return minimal_config


async def _route(make_ctx, config, text=SIMPLE, model="gpt-4o", messages=None, params=None,
                 tenant="default"):
    ctx = make_ctx(messages or [{"role": "user", "content": text}], model=model,
                   params=params, config=config)
    ctx.tenant_id = tenant
    return await G06Routing().process_request(ctx)


def _fallbacks(reason, tenant="default"):
    return g06_jev.FALLBACKS.labels(tenant_id=tenant, reason=reason)._value.get()


def _warnings(caplog):
    return [r for r in caplog.records
            if r.name == g06_jev.logger.name and r.levelno == logging.WARNING]


@pytest.mark.asyncio
class TestTheQuestionJevIsAsked:
    async def test_one_score_question_with_the_levels_lowest_first(
            self, make_ctx, minimal_config, keys, jev):
        await _route(make_ctx, _config(minimal_config))
        request = jev.requests[0]
        assert str(request.url) == "https://jev.test/v1/systemone"
        assert request.headers["authorization"] == "Bearer ts-key-default"
        body = jev.sent()
        assert body["model"] == "jev-1.13.0"
        assert list(body["questions"]) == [g06_jev.QUESTION]
        question = body["questions"][g06_jev.QUESTION]
        assert question["type"] == "score"
        assert question["instructions"] == "How hard is this request?"
        assert len(question["criteria"]) == len(LEVELS)
        for criterion, level in zip(question["criteria"], LEVELS, strict=True):
            assert level["name"] in criterion and level["describe"] in criterion

    async def test_jev_never_sees_the_system_prompt(self, make_ctx, minimal_config, keys, jev):
        """The system prompt is the caller's fixed instructions: it says nothing about how
        hard THIS request is (the keyword rules leave it out for that reason), and it stays
        away from a third party."""
        await _route(make_ctx, _config(minimal_config), messages=[
            {"role": "system", "content": "SYSTEM-RULES-NOT-FOR-JEV"},
            {"role": "developer", "content": "DEVELOPER-RULES-NOT-FOR-JEV"},
            {"role": "user", "content": "the actual question"},
        ])
        state = jev.sent()["state"]
        assert "the actual question" in state
        assert "NOT-FOR-JEV" not in state

    async def test_text_parts_of_a_multipart_message_are_included(
            self, make_ctx, minimal_config, keys, jev):
        await _route(make_ctx, _config(minimal_config), messages=[{"role": "user", "content": [
            {"type": "text", "text": "what is in this picture"},
            {"type": "image_url", "image_url": {"url": "https://img.test/IMAGE-URL"}},
        ]}])
        assert len(jev.requests) == 1, "the text part is text Jev can judge"
        state = jev.sent()["state"]
        assert "what is in this picture" in state
        assert "IMAGE-URL" not in state

    async def test_only_the_newest_text_is_kept(self, make_ctx, minimal_config, keys, jev):
        await _route(make_ctx, _config(minimal_config, _jev(max_state_chars=30)), messages=[
            {"role": "user", "content": "OLDEST " * 20},
            {"role": "assistant", "content": "an answer"},
            {"role": "user", "content": "and the newest question?"},
        ])
        state = jev.sent()["state"]
        assert len(state) == 30
        assert state.endswith("and the newest question?")
        assert "OLDEST" not in state

    async def test_a_request_with_no_text_is_not_sent(self, make_ctx, minimal_config, keys, jev):
        out = await _route(make_ctx, _config(minimal_config), messages=[{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://img.test/a.png"}}]}])
        assert jev.requests == []
        assert out.savings.routing_mode == "jev+fallback:no_text"


class TestTheLevelRule:
    @pytest.mark.parametrize("probabilities, enough, level", [
        ([0.6, 0.2, 0.1, 0.1], 0.5, 0),
        ([0.3, 0.3, 0.2, 0.2], 0.5, 1),
        ([0.3, 0.3, 0.2, 0.2], 0.8, 2),        # a higher bar only ever routes up
        ([0.25, 0.25, 0.25, 0.25], 0.5, 1),    # exactly on the bar counts as reaching it
        ([0.7, 0.1, 0.1, 0.1], 0.9, 2),        # 0.7+0.1+0.1 is 0.8999999999999999 in floats
        ([0.49, 0.0, 0.49, 0.0], 0.5, 0),      # normalised first: 0.49 of 0.98 is half
        ([0.1, 0.1, 0.1, 0.7], 1.0, 3),
        ([0.0, 0.0, 0.0, 0.0], 0.5, 3),        # nothing to go on: the top level
    ])
    def test_the_lowest_level_that_is_enough(self, probabilities, enough, level):
        assert g06_jev.pick_level(probabilities, enough) == level


@pytest.mark.asyncio
class TestJevDecides:
    async def test_the_level_becomes_the_tier_and_is_disclosed(
            self, make_ctx, minimal_config, keys, jev):
        jev.probabilities = [0.05, 0.1, 0.6, 0.25]
        out = await _route(make_ctx, _config(minimal_config), text=COMPLEX)
        assert out.complexity_tier == "complex"
        assert out.savings.routing_mode == "jev"
        assert out.savings.routing_level == "high"
        assert out.savings.to_langfuse_metadata()["routing_level"] == "high"

    async def test_a_low_level_routes_to_the_simple_tier_model(
            self, make_ctx, minimal_config, keys, jev):
        out = await _route(make_ctx, _config(minimal_config), text=COMPLEX)
        assert out.routed_model == "gpt-4o-mini"
        assert "level=low" in out.routing_detail

    async def test_a_higher_bar_routes_up(self, make_ctx, minimal_config, keys, jev):
        jev.probabilities = [0.45, 0.35, 0.15, 0.05]
        low_bar = await _route(make_ctx, _config(minimal_config, _jev(enough_probability=0.5)))
        high_bar = await _route(make_ctx, _config(minimal_config, _jev(enough_probability=0.9)))
        assert (low_bar.savings.routing_level, high_bar.savings.routing_level) == ("medium", "high")

    async def test_the_cost_floor_still_applies(self, make_ctx, minimal_config, keys, jev,
                                                monkeypatch):
        """Jev can rate a cheap model's request "high"; G06 still never routes above the
        model the caller asked for."""
        monkeypatch.setattr("middleware.g06_routing.estimate_cost",
                            lambda i, o, model: (i + o) * (1.0 if "mini" in model else 30.0))
        jev.probabilities = [0.05, 0.1, 0.6, 0.25]
        out = await _route(make_ctx, _config(minimal_config), text=COMPLEX, model="gpt-4o-mini")
        assert out.routed_model == "gpt-4o-mini"
        assert out.savings.routing_mode == "jev+cost_floor"
        assert out.savings.routing_level == "high"


@pytest.mark.asyncio
class TestOverridesStillWin:
    async def test_x_complexity_skips_jev(self, make_ctx, minimal_config, keys, jev):
        out = await _route(make_ctx, _config(minimal_config), params={"x_complexity": "simple"})
        assert jev.requests == []
        assert out.savings.routing_mode == "user_override"

    async def test_a_tenant_rule_skips_jev(self, make_ctx, minimal_config, keys, jev):
        rules = [{"id": "r", "match": {"keywords": ["capital"]}, "action": {"tier": "simple"}}]
        out = await _route(make_ctx, _config(minimal_config, rules=rules))
        assert jev.requests == []
        assert out.savings.routing_mode == "rules:r"

    async def test_g06_disabled_skips_jev(self, make_ctx, minimal_config, keys, jev):
        await _route(make_ctx, _config(minimal_config, enabled=False))
        assert jev.requests == []


@pytest.mark.asyncio
class TestEveryFallbackUsesTheDefaultClassifier:
    """The default classifier (cascade, here without a judge: the keyword rules) rates COMPLEX
    as "complex". Jev, had it answered, would have said "low"."""

    async def _fallback(self, make_ctx, minimal_config, reason, jev_cfg=None):
        before = _fallbacks(reason)
        out = await _route(make_ctx, _config(minimal_config, jev_cfg), text=COMPLEX)
        assert out.savings.routing_mode == f"jev+fallback:{reason}"
        assert out.complexity_tier == "complex", "the default classifier's tier, not Jev's"
        assert out.savings.routing_level is None
        assert _fallbacks(reason) == before + 1
        return out

    async def test_no_key(self, make_ctx, minimal_config, keys, jev):
        keys.keys.clear()
        await self._fallback(make_ctx, minimal_config, "no_key")
        assert jev.requests == []

    async def test_a_strict_own_key_denial_is_no_key(self, make_ctx, minimal_config, keys, jev):
        keys.denied.add(("default", "typesafe"))
        await self._fallback(make_ctx, minimal_config, "no_key")
        assert jev.requests == []

    async def test_a_timeout(self, make_ctx, minimal_config, keys, jev):
        jev.error = httpx.ReadTimeout
        await self._fallback(make_ctx, minimal_config, "timeout")

    @pytest.mark.parametrize("status", [500, 503, 401, 429])
    async def test_an_http_error(self, make_ctx, minimal_config, keys, jev, status):
        jev.status, jev.body = status, {"error": {"message": "nope"}}
        await self._fallback(make_ctx, minimal_config, "http_error")

    async def test_an_unreachable_service(self, make_ctx, minimal_config, keys, jev):
        jev.error = httpx.ConnectError
        await self._fallback(make_ctx, minimal_config, "http_error")

    @pytest.mark.parametrize("change", [
        {"raw": b"<html>not json</html>"},
        {"answer_type": "choice"},
        {"probabilities": [0.5, 0.3, 0.2]},                 # one level short
        {"probabilities": [0.5, 0.3, 0.1, 0.05, 0.05]},     # one level too many
        {"probabilities": [0.3, 0.1, 0.05, 0.05]},          # sums to 0.5
        {"probabilities": [1.4, -0.2, -0.1, -0.1]},         # out of range, sums to 1
        {"legend": {"0": "x", "1": "y", "2": "z", "3": "w"}},
        {"body": {"model": "jev-1.13.0", "answers": {}}},
    ])
    async def test_an_answer_that_fails_the_check(self, make_ctx, minimal_config, keys, jev,
                                                  change):
        for attribute, value in change.items():
            setattr(jev, attribute, value)
        await self._fallback(make_ctx, minimal_config, "bad_answer")
        assert len(jev.requests) == 1

    @pytest.mark.parametrize("change", [
        {"levels": [LEVELS[0]]},
        {"levels": [{"name": f"l{i}", "tier": "simple", "describe": "x"} for i in range(11)]},
        {"levels": [LEVELS[3], LEVELS[0]]},                 # tiers step down
        {"levels": [LEVELS[0], {**LEVELS[1], "name": "low"}]},
        {"levels": [LEVELS[0], {**LEVELS[1], "tier": "huge"}]},
        {"levels": [LEVELS[0], {**LEVELS[1], "describe": " "}]},
        {"key_provider": "openai"},                         # a configured LLM provider's name
        {"key_provider": "Type-Safe"},
        {"enough_probability": 0},
        {"enough_probability": 1.5},
        {"url": ""},
        {"model": None},
        {"instructions": ""},
        {"timeout_ms": 0},
        {"max_state_chars": -1},
    ])
    async def test_a_bad_config(self, make_ctx, minimal_config, keys, jev, change):
        await self._fallback(make_ctx, minimal_config, "bad_config", _jev(**change))
        assert jev.requests == []

    async def test_no_jev_block_is_a_bad_config(self, make_ctx, minimal_config, keys, jev):
        config = _config(minimal_config)
        del config["groups"]["G6_routing"]["jev"]
        out = await _route(make_ctx, config, text=COMPLEX)
        assert out.savings.routing_mode == "jev+fallback:bad_config"

    async def test_the_route_names_the_fallback(self, make_ctx, minimal_config, keys, jev):
        keys.keys.clear()
        out = await _route(make_ctx, _config(minimal_config))   # SIMPLE: routed down
        assert out.routed_model == "gpt-4o-mini"
        assert "fell back" in out.routing_detail and "no_key" in out.routing_detail

    async def test_the_default_classifier_still_asks_its_judge(
            self, make_ctx, minimal_config, keys, jev, monkeypatch):
        """The default is the cascade, not just the keyword rules: on a request the rules are
        unsure about, it asks the configured judge."""
        keys.keys.clear()
        asked = []

        async def judge(messages, params, cfg, ctx=None):
            asked.append(cfg.get("judge_model"))
            return "complex"
        monkeypatch.setattr("middleware.g06_routing._classify_llm_judge", judge)
        out = await _route(make_ctx, _config(minimal_config, judge_model="the-judge"), text=UNSURE)
        assert asked == ["the-judge"]
        assert out.complexity_tier == "complex"
        assert out.savings.routing_mode == "jev+fallback:no_key"

    async def test_the_trace_span_shows_the_level_model_and_fallback(
            self, make_ctx, minimal_config, keys, jev, monkeypatch):
        spans = []
        monkeypatch.setattr("middleware.g06_routing.langfuse_tracing.add_span",
                            lambda ctx, **kw: spans.append(kw["metadata"]))
        await _route(make_ctx, _config(minimal_config))
        assert (spans[-1]["routing_level"], spans[-1]["jev_model"],
                spans[-1]["classifier_fallback"]) == ("low", "jev-1.13.0", None)
        keys.keys.clear()
        await _route(make_ctx, _config(minimal_config), tenant="acme")
        assert (spans[-1]["routing_level"], spans[-1]["classifier_fallback"]) == (None, "no_key")


@pytest.mark.asyncio
class TestTheBreaker:
    @staticmethod
    def _with_breaker(minimal_config):
        config = _config(minimal_config)
        config["resilience"] = {"enabled": True, "failure_threshold": 2, "cooldown_seconds": 30}
        return config

    async def test_an_outage_opens_it_and_requests_stop_waiting(
            self, make_ctx, minimal_config, keys, jev):
        config = self._with_breaker(minimal_config)
        jev.status, jev.body = 503, {"error": {"message": "down"}}
        await _route(make_ctx, config)
        await _route(make_ctx, config)
        out = await _route(make_ctx, config)
        assert len(jev.requests) == 2, "the third request must not wait on a service that is down"
        assert out.savings.routing_mode == "jev+fallback:breaker_open"

    @pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectError])
    async def test_timeouts_and_unreachable_count_as_an_outage(
            self, make_ctx, minimal_config, keys, jev, error):
        config = self._with_breaker(minimal_config)
        jev.error = error
        for _ in range(3):
            await _route(make_ctx, config)
        assert len(jev.requests) == 2

    @pytest.mark.parametrize("status", [401, 403, 429])
    async def test_one_tenants_key_trouble_never_opens_it(
            self, make_ctx, minimal_config, keys, jev, status):
        config = self._with_breaker(minimal_config)
        jev.status, jev.body = status, {"error": {"message": "refused"}}
        for _ in range(4):
            await _route(make_ctx, config)
        assert len(jev.requests) == 4


@pytest.mark.asyncio
class TestTheTenantsKey:
    async def test_each_tenant_uses_its_own_key(self, make_ctx, minimal_config, keys, jev):
        keys.keys[("acme", "typesafe")] = "ts-key-acme"
        await _route(make_ctx, _config(minimal_config), tenant="acme")
        assert jev.requests[0].headers["authorization"] == "Bearer ts-key-acme"

    async def test_lookups_are_reused_for_key_cache_s(self, make_ctx, minimal_config, keys, jev,
                                                      clock):
        config = _config(minimal_config)
        await _route(make_ctx, config)
        await _route(make_ctx, config)
        assert keys.calls.count(("default", "typesafe")) == 1
        clock[0] += 61
        await _route(make_ctx, config)
        assert keys.calls.count(("default", "typesafe")) == 2

    async def test_a_missing_key_is_reused_too(self, make_ctx, minimal_config, keys, jev, clock):
        """Without this a keyless tenant costs a key lookup (on GCP a blocking secret read
        and an ERROR line) on every request."""
        keys.keys.clear()
        config = _config(minimal_config)
        for _ in range(3):
            await _route(make_ctx, config)
        assert keys.calls.count(("default", "typesafe")) == 1


@pytest.mark.asyncio
class TestWarnings:
    async def test_a_keyless_tenant_is_warned_about_once_per_window(
            self, make_ctx, minimal_config, keys, jev, clock, caplog):
        keys.keys.clear()
        config = _config(minimal_config)
        with caplog.at_level(logging.WARNING, logger=g06_jev.logger.name):
            await _route(make_ctx, config)
            await _route(make_ctx, config)
            assert len(_warnings(caplog)) == 1
            await _route(make_ctx, config, tenant="acme")
            assert len(_warnings(caplog)) == 2, "another tenant gets its own warning"
            clock[0] += 3601
            await _route(make_ctx, config)
            assert len(_warnings(caplog)) == 3, "and the first is reminded after warn_every_s"
        assert "typesafe" in _warnings(caplog)[0].getMessage()

    async def test_a_bad_config_is_warned_about_once(self, make_ctx, minimal_config, keys, jev,
                                                     caplog):
        bad = _config(minimal_config, _jev(enough_probability=2))
        with caplog.at_level(logging.WARNING, logger=g06_jev.logger.name):
            await _route(make_ctx, bad)
            await _route(make_ctx, bad)
            await _route(make_ctx, bad, tenant="acme")
        assert len(_warnings(caplog)) == 1
        assert "enough_probability" in _warnings(caplog)[0].getMessage()

    async def test_the_key_never_reaches_the_log(self, make_ctx, minimal_config, keys, jev,
                                                 caplog):
        jev.status, jev.body = 401, {"error": {"message": "bad key"}}
        with caplog.at_level(logging.DEBUG):
            await _route(make_ctx, _config(minimal_config))
        assert "ts-key-default" not in caplog.text


@pytest.mark.asyncio
class TestTheCallsCost:
    async def test_the_call_is_recorded_as_a_side_call(self, make_ctx, minimal_config, keys, jev):
        out = await _route(make_ctx, _config(minimal_config))
        assert out.provider_calls == [{
            "model": "typesafe/jev-1.13.0", "prompt_tokens": 312, "completion_tokens": 48,
            "reasoning_tokens": 0, "cached_tokens": 0, "side": True}]

    async def test_a_refused_answer_was_still_billed(self, make_ctx, minimal_config, keys, jev):
        jev.legend = {"0": "x", "1": "y", "2": "z", "3": "w"}
        out = await _route(make_ctx, _config(minimal_config))
        assert out.savings.routing_mode == "jev+fallback:bad_answer"
        assert [c["model"] for c in out.provider_calls] == ["typesafe/jev-1.13.0"]


@pytest.mark.asyncio
class TestPerTenant:
    async def test_the_operator_can_switch_one_tenant_to_jev(
            self, make_ctx, minimal_config, keys, jev):
        config = _config(minimal_config, classifier="cascade")
        config["tenants"] = {"acme": {"groups": {"G6_routing": {
            "classifier": "jev", "jev": {"enough_probability": 0.9}}}}}
        keys.keys[("acme", "typesafe")] = "ts-key-acme"
        jev.probabilities = [0.45, 0.35, 0.15, 0.05]
        acme = await _route(make_ctx, apply_operator_overlay(config, "acme"), tenant="acme")
        other = await _route(make_ctx, apply_operator_overlay(config, "other"), tenant="other")
        assert len(jev.requests) == 1
        assert acme.savings.routing_mode == "jev"
        assert acme.savings.routing_level == "high", "the overlay's bar, the rest of the block kept"
        assert other.savings.routing_mode == "cascade"


class TestWhatShips:
    @pytest.fixture(scope="class")
    def template(self):
        return yaml.safe_load((REPO / "config" / "config.yaml.template").read_text(encoding="utf-8"))

    def test_the_template_ships_a_valid_jev_block_that_is_off(self, template):
        g6 = template["groups"]["G6_routing"]
        assert g6["classifier"] == "cascade"
        names = [p["name"] for p in template["providers"]]
        assert g06_jev.config_problem(g6["jev"], names, _TIER_ORDER) is None

    def test_the_template_prices_the_call(self, template, monkeypatch):
        from savings import calculator
        label = "typesafe/" + template["groups"]["G6_routing"]["jev"]["model"]
        matches = [k for k in template["pricing"] if k != "default" and k.lower() in label]
        assert matches == ["typesafe/jev"], "exactly one pricing row may match the label"
        monkeypatch.setattr("config_loader.get_pricing_table", lambda: template["pricing"])
        assert calculator.get_cost_per_1k(label) == (0.000042, 0.0)

    def test_a_self_hoster_can_supply_the_key(self):
        """No portal on a self-hosted proxy: the operator's LLM_KEY_TYPESAFE is the key, so
        the env template must list it and compose must hand it to the proxy (compose passes
        only the variables it names)."""
        env_template = (REPO / ".env.template").read_text(encoding="utf-8")
        assert re.search(r"^LLM_KEY_TYPESAFE=$", env_template, re.MULTILINE)
        compose = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
        environment = compose["services"]["proxy"]["environment"]
        assert environment["LLM_KEY_TYPESAFE"] == "${LLM_KEY_TYPESAFE:-}"
