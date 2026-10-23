"""Usage tags end to end (ST-2): request headers -> ctx.usage_tags -> the usage row.

* The pipeline reads the tags at entry from the GLOBAL config, before any tenant or team
  overlay is merged: a tenant cannot widen (or switch off) what is recorded.
* Through /v1/chat/completions: the allow-listed headers are on the usage row; a header
  not on the list is not.
"""
import asyncio
import contextlib
import copy
import importlib
import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

from billing.metering import UsageMeter  # noqa: E402
from middleware import RequestContext  # noqa: E402
from savings.models import SavingsRecord  # noqa: E402
from tests import conftest as c  # noqa: E402

_AGENTIC = {
    "X-Agentic-Run-Id": "run-42", "X-Agentic-Step": "implement",
    "X-Agentic-Agent": "builder@1.4.0", "X-Agentic-Team": "payments",
}


def _ctx(config):
    savings = SavingsRecord(request_id="r1", user_id="u1", timestamp=datetime.now(timezone.utc),
                            model_requested="gpt-4o", routed_model="gpt-4o", baseline_tokens=5)
    return RequestContext(request_id="r1", user_id="u1",
                          original_messages=[{"role": "user", "content": "hi"}],
                          messages=[{"role": "user", "content": "hi"}], model="gpt-4o",
                          routed_model="gpt-4o", savings=savings, config=config,
                          params={"_auth_tenant_id": "acme", "_auth_principal": "acme"})


def _at_g00(ctx, headers, tenant_overrides=None):
    seen = []

    async def _g00(cx):
        seen.append(cx)
        cx.bypassed = True
        return cx

    async def _load(self, cx):
        if tenant_overrides:
            from config_loader import deep_merge
            cx.config = deep_merge(cx.config, tenant_overrides)

    with patch("tenancy.config.TenantConfigLoader.load", _load):
        from middleware.pipeline import OptimisationPipeline
        pipeline = OptimisationPipeline()
        with patch.object(pipeline.g00, "process_request", side_effect=_g00), \
             patch("middleware.pipeline.langfuse_tracing.start_trace"):
            asyncio.run(pipeline.process_request(ctx, request_headers=headers))
    return seen[0]


class TestThePipeline:
    def test_tags_are_on_the_context_before_g00(self):
        seen = _at_g00(_ctx({"groups": {}}), {**_AGENTIC, "X-Other": "x"})
        assert seen.usage_tags == {"run_id": "run-42", "step": "implement",
                                   "agent": "builder@1.4.0", "team": "payments"}

    def test_a_tag_never_selects_the_team(self):
        seen = _at_g00(_ctx({"groups": {}}), _AGENTIC)
        assert seen.team == "default"          # X-Agentic-Team is attribution only

    def test_no_allow_listed_header_means_none(self):
        assert _at_g00(_ctx({"groups": {}}), {"X-Other": "x"}).usage_tags is None

    def test_a_tenant_s_overrides_cannot_change_what_is_recorded(self):
        off = {"billing": {"metering": {"tags": {"enabled": False}}}}
        seen = _at_g00(_ctx({"groups": {}}), _AGENTIC, tenant_overrides=off)
        assert seen.usage_tags["run_id"] == "run-42"
        widen = {"billing": {"metering": {"tags": {"headers": {"X-Other": "other"}}}}}
        seen = _at_g00(_ctx({"groups": {}}), {**_AGENTIC, "X-Other": "x"},
                       tenant_overrides=widen)
        assert "other" not in seen.usage_tags

    def test_the_operator_switch_turns_it_off(self):
        cfg = {"groups": {}, "billing": {"metering": {"tags": {"enabled": False}}}}
        assert _at_g00(_ctx(cfg), _AGENTIC).usage_tags is None


@pytest.mark.asyncio
async def test_the_usage_row_stores_allow_listed_headers_only():
    meter = MagicMock()
    meter.record = AsyncMock()
    patches = [
        patch("auth.api_key_manager._fetch_secret", return_value=c._VALID_KEYS_JSON),
        patch("auth.api_key_manager._KEY_CACHE", c._setup_auth_keys()),
        patch("auth.api_key_manager._CACHE_LOADED_AT", 9999999999.0),
        patch("config_loader.load_config"),
        patch("config_loader.start_hot_reload"),
        patch("config_loader.get_config", return_value=_http_config()),
        patch("litellm.acompletion", AsyncMock(return_value=c._LLM_RESPONSE_MOCK)),
        patch("auth.api_key_manager._access_llm_key_secret", return_value="sk-test-key"),
        patch("middleware.g05_cache.G05Cache.store_response", new_callable=AsyncMock),
        patch("middleware.g18_observability._emit_trace", new_callable=AsyncMock),
        patch("cache.redis_pool.init_pool"),
        patch("cache.redis_pool.close_pool", new_callable=AsyncMock),
        patch("cache.redis_pool.get_redis", return_value=c._make_mock_redis()),
        patch("middleware.g00_rate_limit._get_redis", return_value=c._make_mock_redis()),
        patch("middleware.g17_loop_control._get_redis", return_value=c._make_mock_redis()),
    ]
    headers = {"Authorization": "Bearer test-key", **_AGENTIC,
               "X-Agentic-Unlisted": "nope", "X-Cost-Center": "cc-1",
               "X-Agentic-Repo": "https://deploy:hunter2secret@git.example/acme/shop"}
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        import main
        importlib.reload(main)
        stack.enter_context(patch.object(main, "_usage_meter", meter))
        async with AsyncClient(transport=ASGITransport(app=main.app),
                               base_url="http://test") as client:
            resp = await client.post("/v1/chat/completions", headers=headers, json={
                "model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]})
        await main._drain_after_response()
    assert resp.status_code == 200
    ctx = meter.record.await_args.args[0]
    row = UsageMeter()._build_event(ctx, {})
    assert row.tags == {"run_id": "run-42", "step": "implement", "agent": "builder@1.4.0",
                        "team": "payments"}          # no unlisted header, no credential URL


def _http_config():
    cfg = copy.deepcopy(c._integration_config())
    cfg["groups"].setdefault("G7_retrieval", {})["enabled"] = False
    return cfg
