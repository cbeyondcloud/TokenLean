"""The admin-managed routing and team-limits fragment is validated before anything is applied
(ST-10). Every problem is reported with its path; the protected groups (G00, G17, G29-G32)
and every section outside routing and team limits are refused at any depth."""
import pytest

from admin_api import routing as r

BASE = {
    "providers": [
        {"name": "openai", "models": ["gpt-4o-mini", "gpt-4o"], "model_prefixes": ["gpt-"]},
        {"name": "anthropic", "models": [], "model_prefixes": ["claude-"]},
    ],
    "groups": {"G6_routing": {"enabled": True, "tiers": {"simple": ["gpt-4o-mini"]}}},
    "rate_limit": {"enabled": True, "per_team": {}},
}

GOOD = {
    "groups": {"G6_routing": {
        "tiers": {"simple": ["gpt-4o-mini"], "complex": ["claude-sonnet-4-5"]},
        "tiers_by_provider": {"anthropic": {"simple": ["claude-haiku-4-5"]}},
        "strategy": "weighted", "strategy_weights": {"gpt-4o-mini": 3, "gpt-4o": 1},
        "canary_pct": 10, "on_unreachable_tier": "error",
        "rules": [{"id": "confidential-to-strong", "priority": 100,
                   "match": {"params": {"x_agentic_classification": ["confidential"]},
                             "patterns": [r"\bMSA-\d+\b"]},
                   "action": {"tier": "complex", "overrides": {"strategy": "priority"}}}],
    }},
    "rate_limit": {"per_team": {"payments": {"requests_per_minute": 20}}},
    "teams": {"payments": {"monthly_spend_cap_usd": 50}},
    "tenants": {"ACME-PRD-01": {
        "groups": {"G6_routing": {"tiers": {"medium": ["gpt-4o"]}}},
        "teams": {"search": {"monthly_spend_cap_usd": 0, "config": {
            "groups": {"G6_routing": {"tiers": {"simple": ["claude-haiku-4-5"]}}},
            "proxy": {"default_model": "gpt-4o-mini"}}}},
    }},
}


def _errors(fragment, base=BASE):
    with pytest.raises(r.FragmentError) as exc:
        r.validate_fragment(fragment, base)
    return {e["path"]: e["message"] for e in exc.value.errors}


def test_a_full_routing_and_limits_fragment_is_valid():
    r.validate_fragment(GOOD, BASE)
    r.validate_fragment({}, BASE)                       # empty: nothing set


@pytest.mark.parametrize("path", [
    ("groups", "G0_rate_limit"), ("groups", "G00_rate_limit"), ("groups", "G17_loop"),
    ("groups", "G29_pii_redaction"), ("groups", "g30_guardrails"), ("groups", "G31_context_trust"),
    ("groups", "G032_tool_eligibility"),
])
def test_a_protected_group_is_refused_at_the_top_level(path):
    errs = _errors({path[0]: {path[1]: {"enabled": False}}})
    assert "protected group" in errs[".".join(path)]


@pytest.mark.parametrize("fragment,path", [
    ({"tenants": {"ACME": {"groups": {"G30_guardrails": {"mode": "allow"}}}}},
     "tenants.ACME.groups.G30_guardrails"),
    ({"teams": {"payments": {"config": {"groups": {"G17_loop": {"enabled": False}}}}}},
     "teams.payments.config.groups.G17_loop"),
    ({"tenants": {"ACME": {"teams": {"x": {"config": {"groups": {"G29_pii": {}}}}}}}},
     "tenants.ACME.teams.x.config.groups.G29_pii"),
])
def test_a_protected_group_is_refused_at_any_depth(fragment, path):
    assert "protected group" in _errors(fragment)[path]


@pytest.mark.parametrize("fragment,path", [
    ({"providers": [{"name": "evil"}]}, "fragment.providers"),
    ({"billing": {"metering": {}}}, "fragment.billing"),
    ({"rate_limit": {"enabled": False}}, "rate_limit.enabled"),
    ({"rate_limit": {"spend_cap": {"enabled": False}}}, "rate_limit.spend_cap"),
    ({"groups": {"G5_cache": {"enabled": False}}}, "groups.G5_cache"),
    ({"groups": {"G6_routing": {"judge_model": "gpt-4o"}}}, "groups.G6_routing.judge_model"),
    ({"groups": {"G6_routing": {"jev": {"url": "https://x.test"}}}}, "groups.G6_routing.jev"),
    ({"teams": {"payments": {"config": {"rate_limit": {}}}}}, "teams.payments.config.rate_limit"),
    ({"teams": {"payments": {"config": {"proxy": {"port": 1}}}}},
     "teams.payments.config.proxy.port"),
    ({"tenants": {"ACME": {"rate_limit": {}}}}, "tenants.ACME.rate_limit"),
])
def test_anything_outside_routing_and_team_limits_is_refused(fragment, path):
    assert path in _errors(fragment)


@pytest.mark.parametrize("fragment,path", [
    ({"groups": {"G6_routing": {"tiers": {"simple": ["llama-imaginary"]}}}},
     "groups.G6_routing.tiers.simple[0]"),
    ({"groups": {"G6_routing": {"tiers": {"simple": []}}}}, "groups.G6_routing.tiers.simple"),
    ({"groups": {"G6_routing": {"tiers": {"huge": ["gpt-4o"]}}}}, "groups.G6_routing.tiers.huge"),
    ({"groups": {"G6_routing": {"tiers_by_provider": {"nope": {}}}}},
     "groups.G6_routing.tiers_by_provider.nope"),
    ({"groups": {"G6_routing": {"strategy": "random"}}}, "groups.G6_routing.strategy"),
    ({"groups": {"G6_routing": {"canary_pct": 101}}}, "groups.G6_routing.canary_pct"),
    ({"groups": {"G6_routing": {"enabled": "yes"}}}, "groups.G6_routing.enabled"),
    ({"groups": {"G6_routing": {"rules": [{"id": "x", "match": {},
                                           "action": {"tier": "simple"}}]}}},
     "groups.G6_routing.rules[0].match"),
    ({"groups": {"G6_routing": {"rules": [{"id": "x", "match": {"keywords": ["a"]},
                                           "action": {}}]}}},
     "groups.G6_routing.rules[0].action"),
    ({"groups": {"G6_routing": {"rules": [{"id": "x", "match": {"patterns": ["(a+)+$"]},
                                           "action": {"tier": "simple"}}]}}},
     "groups.G6_routing.rules[0].match.patterns[0]"),
    ({"groups": {"G6_routing": {"rules": [{"id": "x", "match": {"patterns": ["(unclosed"]},
                                           "action": {"tier": "simple"}}]}}},
     "groups.G6_routing.rules[0].match.patterns[0]"),
    ({"groups": {"G6_routing": {"rules": [
        {"id": "x", "match": {"keywords": ["a"]}, "action": {"tier": "simple"}},
        {"id": "x", "match": {"keywords": ["b"]}, "action": {"tier": "simple"}}]}}},
     "groups.G6_routing.rules[1].id"),
    ({"groups": {"G6_routing": {"rules": [{"id": "x", "match": {"keywords": ["a"]},
                                           "action": {"overrides": {"judge_model": "m"}}}]}}},
     "groups.G6_routing.rules[0].action.overrides.judge_model"),
    ({"groups": {"G6_routing": {"rules": [{"id": "x", "match": {"params": {"team": "a"}},
                                           "action": {"tier": "simple"}}]}}},
     "groups.G6_routing.rules[0].match.params.team"),
    ({"rate_limit": {"per_team": {"payments": {"requests_per_minute": 0}}}},
     "rate_limit.per_team.payments.requests_per_minute"),
    ({"rate_limit": {"per_team": {"payments": {}}}}, "rate_limit.per_team.payments"),
    ({"rate_limit": {"per_team": {"default": {"requests_per_minute": 5}}}},
     "rate_limit.per_team.default"),
    ({"teams": {"payments": {"monthly_spend_cap_usd": -1}}},
     "teams.payments.monthly_spend_cap_usd"),
    ({"teams": {"payments": {"monthly_spend_cap_usd": True}}},
     "teams.payments.monthly_spend_cap_usd"),
    ({"teams": {"has space": {"monthly_spend_cap_usd": 5}}}, "teams.has space"),
    ({"tenants": {"bad tenant": {}}}, "tenants.bad tenant"),
    ({"teams": {"payments": {"config": {"proxy": {"default_model": "nope-1"}}}}},
     "teams.payments.config.proxy.default_model"),
    ({"teams": {"payments": None}}, "teams.payments"),
    ({"groups": "G6_routing"}, "groups"),
])
def test_an_invalid_value_is_refused_with_its_path(fragment, path):
    assert path in _errors(fragment)


def test_every_problem_is_reported_at_once():
    errs = _errors({"groups": {"G30_guardrails": {}, "G6_routing": {"strategy": "x"}},
                    "billing": {}})
    assert {"groups.G30_guardrails", "groups.G6_routing.strategy", "fragment.billing"} <= set(errs)


def test_without_a_base_only_the_shape_is_checked():
    r.validate_fragment({"groups": {"G6_routing": {"tiers": {"simple": ["anything-1"]}}}})
    with pytest.raises(r.FragmentError):
        r.validate_fragment({"groups": {"G29_pii_redaction": {}}})


def test_a_fragment_over_the_size_cap_is_refused():
    big = {"groups": {"G6_routing": {"rules": [
        {"id": f"r{i}", "description": "x" * 400, "match": {"keywords": ["k" * 3000]},
         "action": {"tier": "simple"}} for i in range(100)]}}}
    assert "fragment" in _errors(big)


def test_touched_names_sections_tenants_and_teams_never_values():
    assert r.touched(GOOD) == {"sections": ["groups", "rate_limit", "teams", "tenants"],
                               "tenants": ["ACME-PRD-01"], "teams": ["payments", "search"]}
