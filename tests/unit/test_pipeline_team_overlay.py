"""The pipeline merges a team's config overlay at entry, before G00 (ST-1).

The overlay is merged after the tenant's own overrides and BEFORE the operator's
``tenants.<id>.groups`` overlay, so an operator pin always wins. It is recorded as the
tenant's own choice (``tenant_config_overrides``), so the groups that cap or defer to a
tenant's setting treat a team's setting the same way. It can never switch off trust &
safety or loop control. Its fingerprint is on the context (``team_overlay_tag``) for the
G05 cache keys. A request without a team, or a team without an overlay, runs on the very
same config object as before.
"""
import asyncio
import sys, os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

from middleware import RequestContext, resolve_group_config
from savings.models import SavingsRecord


def _config():
    return {
        "groups": {"G5_cache": {"enabled": True, "cache_scope": "tenant"},
                   "G30_guardrails": {"enabled": True, "mode": "block"},
                   "G29_pii_redaction": {"enabled": True, "mode": "mask"},
                   "G17_loop": {"enabled": True, "max_iterations": 10}},
        "teams": {"payments": {"config": {"groups": {
            "G5_cache": {"cache_scope": "tenant+model"}}}}},
    }


def _ctx(config, team=None):
    savings = SavingsRecord(request_id="r1", user_id="u1", timestamp=datetime.now(timezone.utc),
                            model_requested="gpt-4o", routed_model="gpt-4o", baseline_tokens=5)
    params = {"_auth_tenant_id": "acme", "_auth_tier": "free", "_auth_principal": "acme"}
    if team:
        params["_auth_team"] = team
    return RequestContext(request_id="r1", user_id="u1",
                          original_messages=[{"role": "user", "content": "hi"}],
                          messages=[{"role": "user", "content": "hi"}], model="gpt-4o",
                          routed_model="gpt-4o", params=params, config=config, savings=savings)


def _seen_at_g00(ctx):
    """Run the pipeline up to G00 and return the ctx G00 received."""
    seen = []

    async def _g00(c):
        seen.append(c)
        c.bypassed = True
        return c

    with patch("tenancy.config.TenantConfigLoader.load", new_callable=AsyncMock):
        from middleware.pipeline import OptimisationPipeline
        pipeline = OptimisationPipeline()
        with patch.object(pipeline.g00, "process_request", side_effect=_g00), \
             patch("middleware.pipeline.langfuse_tracing.start_trace"):
            asyncio.run(pipeline.process_request(ctx, request_headers={}))
    return seen[0]


def test_the_team_s_requests_get_its_overlay_as_a_tenant_choice():
    seen = _seen_at_g00(_ctx(_config(), team="payments"))
    assert seen.team == "payments"
    assert seen.config["groups"]["G5_cache"] == {"enabled": True, "cache_scope": "tenant+model"}
    assert seen.tenant_config_overrides == {"groups": {"G5_cache": {"cache_scope": "tenant+model"}}}
    assert len(seen.team_overlay_tag) == 16


def test_an_operator_pin_beats_the_team_overlay():
    cfg = _config()
    cfg["tenants"] = {"acme": {"groups": {"G5_cache": {"cache_scope": "tenant"}}}}
    seen = _seen_at_g00(_ctx(cfg, team="payments"))
    assert seen.config["groups"]["G5_cache"]["cache_scope"] == "tenant"
    assert resolve_group_config(seen, "G5_cache")["cache_scope"] == "tenant"


def test_trust_and_safety_and_loop_control_cannot_be_switched_off():
    cfg = _config()
    cfg["teams"]["payments"]["config"]["groups"].update({
        "G30_guardrails": {"enabled": False, "mode": "allow"},
        "G29_pii_redaction": {"enabled": False},
        "G17_loop": {"max_iterations": 100000},
        "G31_context_trust": {"enabled": False},
    })
    seen = _seen_at_g00(_ctx(cfg, team="payments"))
    groups = seen.config["groups"]
    assert groups["G30_guardrails"] == {"enabled": True, "mode": "block"}
    assert groups["G29_pii_redaction"] == {"enabled": True, "mode": "mask"}
    assert groups["G17_loop"]["max_iterations"] == 10
    assert "G31_context_trust" not in groups
    assert groups["G5_cache"]["cache_scope"] == "tenant+model"      # the rest still applies


def test_a_scalar_cannot_replace_a_group():
    cfg = _config()
    cfg["teams"]["payments"]["config"]["groups"] = {"G5_cache": False}
    seen = _seen_at_g00(_ctx(cfg, team="payments"))
    assert seen.config["groups"]["G5_cache"] == {"enabled": True, "cache_scope": "tenant"}
    assert seen.team_overlay_tag == ""


def test_the_global_config_is_not_modified():
    cfg = _config()
    _seen_at_g00(_ctx(cfg, team="payments"))
    assert cfg["groups"]["G5_cache"]["cache_scope"] == "tenant"


def test_no_team_runs_on_the_same_config_object():
    cfg = _config()
    seen = _seen_at_g00(_ctx(cfg))
    assert seen.team == "default"
    assert seen.config is cfg
    assert seen.tenant_config_overrides is None
    assert seen.team_overlay_tag == ""


def test_a_team_without_an_overlay_runs_on_the_same_config_object():
    cfg = _config()
    seen = _seen_at_g00(_ctx(cfg, team="search"))
    assert seen.team == "search" and seen.config is cfg and seen.team_overlay_tag == ""
