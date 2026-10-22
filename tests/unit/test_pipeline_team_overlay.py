"""The pipeline merges a team's config overlay at entry, before G00 (ST-1).

The overlay is merged after the tenant's and the operator's overlays, so it wins for the
team's requests; it is recorded as the tenant's own choice (``tenant_config_overrides``),
so the groups that cap a tenant's setting (G30/G31 thresholds) or defer to it (G06 tier
picks, G12 effort) treat a team's setting the same way. A request without a team, or a
team without an overlay, runs on the very same config object as before.
"""
import asyncio
import sys, os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

from middleware import RequestContext
from savings.models import SavingsRecord


def _config():
    return {
        "groups": {"G30_guardrails": {"enabled": True, "threshold": 0.5}},
        "teams": {"payments": {"config": {"groups": {"G30_guardrails": {"threshold": 0.9}}}}},
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
    assert seen.config["groups"]["G30_guardrails"] == {"enabled": True, "threshold": 0.9}
    assert seen.tenant_config_overrides == {"groups": {"G30_guardrails": {"threshold": 0.9}}}


def test_the_global_config_is_not_modified():
    cfg = _config()
    _seen_at_g00(_ctx(cfg, team="payments"))
    assert cfg["groups"]["G30_guardrails"]["threshold"] == 0.5


def test_no_team_runs_on_the_same_config_object():
    cfg = _config()
    seen = _seen_at_g00(_ctx(cfg))
    assert seen.team == "default"
    assert seen.config is cfg
    assert seen.tenant_config_overrides is None


def test_a_team_without_an_overlay_runs_on_the_same_config_object():
    cfg = _config()
    seen = _seen_at_g00(_ctx(cfg, team="search"))
    assert seen.team == "search" and seen.config is cfg
