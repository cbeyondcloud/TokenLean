"""G00 per-team monthly spend cap (ST-1).

A team's cap (``teams.<team>.monthly_spend_cap_usd``) is checked against the team's own
counter (``<tenant prefix>team:<team>:spend:<YYYYMM>``) under the same switch, grace and
exemptions as the tenant's cap (``rate_limit.spend_cap``). Over it, the request is refused
with ``team_spend_cap_exceeded`` (a 429 in main). The tenant's cap still applies to the
team's requests, the default team has no cap, and nothing changes for a deployment that
configures none. Every served cost is added to the team's counter as well as the tenant's.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import time
from unittest.mock import patch

import pytest

import events
from middleware.g00_rate_limit import G00RateLimit, RateLimitExceeded, add_to_spend


class _Ctx:
    def __init__(self, team="payments", tenant_id="ACME-PRD-01", config=None):
        self.tenant_id = tenant_id
        self.pricing_tier = "free"
        self.redis_prefix = f"t:{tenant_id}:"
        self.request_id = "req-1"
        self.team = team
        self.config = config if config is not None else _cfg()


class _Redis:
    """GET answers from ``values`` (by key); records SET NX dedups and float increments."""

    def __init__(self, values=None, fail=False, set_result=True):
        self.values = dict(values or {})
        self.fail = fail
        self.set_result = set_result
        self.sets, self.incrs, self.expires = [], [], []

    async def get(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        return self.values.get(key)

    async def set(self, key, val, nx=False, ex=None):
        self.sets.append(key)
        return self.set_result

    async def incrbyfloat(self, key, amount):
        self.values[key] = float(self.values.get(key) or 0) + amount
        self.incrs.append((key, amount))
        return self.values[key]

    async def expire(self, key, ttl):
        self.expires.append((key, ttl))


def _cfg(team_cap=10, tenant_cap=None, enabled=True, grace=0, warn=0, exempt=None):
    conf = {
        "rate_limit": {"spend_cap": {"enabled": enabled, "grace_pct": grace, "warn_pct": warn,
                                     "exempt_tenants": exempt or ["admin", "default"]}},
        "billing": {"rate_card": {"free": {}}},
        "teams": {"payments": {"monthly_spend_cap_usd": team_cap}},
    }
    if tenant_cap is not None:
        conf["limits"] = {"monthly_spend_cap_usd": tenant_cap}
    return conf


def _team_key(team="payments", tenant="ACME-PRD-01"):
    return G00RateLimit.team_spend_key(f"t:{tenant}:", team)


@pytest.fixture
def captured(monkeypatch):
    calls = []
    monkeypatch.setattr(events, "schedule_event", lambda t, e, p: calls.append((t, e, p)))
    return calls


async def _run(ctx, redis):
    with patch("middleware.g00_rate_limit._get_redis", return_value=redis):
        await G00RateLimit().process_request(ctx)


class TestTheKey:
    def test_monthly_and_tenant_scoped(self):
        month = time.strftime("%Y%m", time.gmtime())
        key = G00RateLimit.team_spend_key("t:acme:", "payments")
        assert key == f"t:acme:team:payments:spend:{month}"

    def test_an_odd_team_name_cannot_add_a_key_segment(self):
        key = G00RateLimit.team_spend_key("t:acme:", "evil:spend:x")
        assert key.count(":") == G00RateLimit.team_spend_key("t:acme:", "ok").count(":")


class TestTheGate:
    async def test_under_the_cap_is_admitted(self, captured):
        await _run(_Ctx(), _Redis({_team_key(): "9.99"}))
        assert captured == []

    async def test_at_the_cap_is_refused(self, captured):
        with pytest.raises(RateLimitExceeded) as ei:
            await _run(_Ctx(), _Redis({_team_key(): "10"}))
        assert ei.value.limit_type == "team_spend_cap_exceeded"
        assert "team=payments" in ei.value.scope and "tenant=ACME-PRD-01" in ei.value.scope
        assert [e for _, e, _ in captured] == [events.TEAM_SPEND_CAP_REACHED]
        tenant, _, payload = captured[0]
        assert tenant == "ACME-PRD-01"
        assert payload == {"team": "payments", "cap_usd": 10, "spent_usd": 10, "tier": "free"}

    async def test_grace_is_the_tenant_cap_s(self, captured):
        await _run(_Ctx(config=_cfg(grace=10)), _Redis({_team_key(): "10.5"}))
        with pytest.raises(RateLimitExceeded):
            await _run(_Ctx(config=_cfg(grace=10)), _Redis({_team_key(): "11"}))

    async def test_another_team_s_spend_does_not_count(self, captured):
        await _run(_Ctx(), _Redis({_team_key("search"): "999"}))

    async def test_the_tenant_cap_still_binds_a_team(self, captured):
        ctx = _Ctx(config=_cfg(team_cap=100, tenant_cap=5))
        redis = _Redis({G00RateLimit.spend_key("t:ACME-PRD-01:"): "5", _team_key(): "1"})
        with pytest.raises(RateLimitExceeded) as ei:
            await _run(ctx, redis)
        assert ei.value.limit_type == "spend_cap_exceeded"

    @pytest.mark.parametrize("case", ["switch off", "default team", "no team cap",
                                      "exempt tenant", "redis down"])
    async def test_admitted_when(self, captured, case):
        ctx, redis = _Ctx(), _Redis({_team_key(): "50"})
        if case == "switch off":
            ctx.config = _cfg(enabled=False)
        elif case == "default team":
            ctx.team = "default"
            redis = _Redis({_team_key("default"): "50"})
        elif case == "no team cap":
            ctx.config = _cfg(team_cap=None)
        elif case == "exempt tenant":
            ctx.config = _cfg(exempt=["ACME-PRD-01"])
        else:
            redis = _Redis(fail=True)            # fail open, as the tenant cap does
        await _run(ctx, redis)
        assert captured == []

    async def test_the_operator_s_tenant_block_sets_the_cap(self, captured):
        cfg = _cfg(team_cap=None)
        cfg["tenants"] = {"ACME-PRD-01": {"teams": {"payments": {"monthly_spend_cap_usd": 2}}}}
        with pytest.raises(RateLimitExceeded):
            await _run(_Ctx(config=cfg), _Redis({_team_key(): "2"}))


class TestTheWarning:
    async def test_one_shot_at_warn_pct(self, captured):
        redis = _Redis({_team_key(): "8.5"})
        await _run(_Ctx(config=_cfg(warn=80)), redis)
        assert [e for _, e, _ in captured] == [events.TEAM_BUDGET_THRESHOLD]
        assert captured[0][2]["team"] == "payments"
        month = time.strftime("%Y%m", time.gmtime())
        assert redis.sets == [f"t:ACME-PRD-01:team:payments:budgetwarn:{month}"]

    async def test_deduped(self, captured):
        await _run(_Ctx(config=_cfg(warn=80)), _Redis({_team_key(): "9"}, set_result=None))
        assert captured == []


class TestTheCounter:
    async def test_a_team_s_cost_is_added_to_both_counters(self):
        redis = _Redis()
        with patch("middleware.g00_rate_limit._get_redis", return_value=redis):
            await add_to_spend("t:acme:", 0.25, team="payments")
        assert redis.incrs == [(G00RateLimit.spend_key("t:acme:"), 0.25),
                               (G00RateLimit.team_spend_key("t:acme:", "payments"), 0.25)]
        assert {k for k, _ in redis.expires} == {k for k, _ in redis.incrs}   # first accrual

    @pytest.mark.parametrize("team", [None, "", "default"])
    async def test_no_team_counter_without_a_team(self, team):
        redis = _Redis()
        with patch("middleware.g00_rate_limit._get_redis", return_value=redis):
            await add_to_spend("t:acme:", 0.25, team=team)
        assert redis.incrs == [(G00RateLimit.spend_key("t:acme:"), 0.25)]

    async def test_never_raises(self):
        with patch("middleware.g00_rate_limit._get_redis", return_value=_Redis(fail=True)):
            class _Boom(_Redis):
                async def incrbyfloat(self, key, amount):
                    raise ConnectionError("down")
            with patch("middleware.g00_rate_limit._get_redis", return_value=_Boom()):
                await add_to_spend("t:acme:", 0.25, team="payments")


class TestAnInvalidCap:
    """A cap that is set but unreadable (negative, NaN, infinite, not a number) refuses the
    team's requests: a broken cap is never read as "no cap" (review)."""

    @pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), "lots"])
    async def test_refused_without_reading_the_counter(self, captured, value):
        ctx = _Ctx(config=_cfg(team_cap=value))
        with pytest.raises(RateLimitExceeded) as ei:
            await _run(ctx, _Redis(fail=True))          # not even Redis is needed
        assert ei.value.limit_type == "team_spend_cap_exceeded"
        assert "cap_invalid" in ei.value.scope

    async def test_only_while_the_switch_is_on(self, captured):
        await _run(_Ctx(config=_cfg(team_cap=-1, enabled=False)), _Redis())
