"""A team-bound proxy key, end to end through /v1/chat/completions (ST-1).

* Over its team's monthly cap the key gets a 429 (``team_spend_cap_exceeded``) before any
  provider call; a key of the same tenant with no team is still served.
* The team's config overlay applies to the team's requests only (here: the default model a
  request without ``model`` gets).
* A served answer's cost is added to the team's counter as well as the tenant's.
"""
import contextlib
import copy
import hashlib
import importlib
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from tests import conftest as c

_TEAM_KEY = "team-key-payments"
_PLAIN_KEY = "team-key-none"
_TENANT = "ACME-PRD-01"
_KEYS = {
    hashlib.sha256(_TEAM_KEY.encode()).hexdigest():
        {"tenant_id": _TENANT, "tier": "enterprise", "team": "payments"},
    hashlib.sha256(_PLAIN_KEY.encode()).hexdigest():
        {"tenant_id": _TENANT, "tier": "enterprise"},
}
_MONTH = time.strftime("%Y%m", time.gmtime())
_TEAM_COUNTER = f"t:{_TENANT}:team:payments:spend:{_MONTH}"


def _config():
    cfg = c._integration_config()
    cfg["groups"].setdefault("G7_retrieval", {})["enabled"] = False   # no vector store here
    cfg["groups"].setdefault("G6_routing", {})["enabled"] = False     # the model as resolved
    cfg.setdefault("proxy", {})["default_model"] = "gpt-4o-mini"
    cfg["rate_limit"]["spend_cap"] = {"enabled": True, "grace_pct": 0, "warn_pct": 0,
                                      "exempt_tenants": ["admin", "default"]}
    cfg["teams"] = {"payments": {"monthly_spend_cap_usd": 10,
                                 "config": {"proxy": {"default_model": "gpt-4o"}}}}
    return cfg


def _redis(team_spent):
    redis = c._make_mock_redis()
    redis.get = AsyncMock(side_effect=lambda key: team_spent if key == _TEAM_COUNTER else None)
    return redis


async def _post(body, key, team_spent=None, acompletion=None):
    acompletion = acompletion or AsyncMock(return_value=c._LLM_RESPONSE_MOCK)
    redis = _redis(team_spent)
    add_to_spend = AsyncMock()
    patches = [
        patch("auth.api_key_manager._fetch_secret", return_value=json.dumps(_KEYS)),
        patch("auth.api_key_manager._KEY_CACHE", copy.deepcopy(_KEYS)),
        patch("auth.api_key_manager._CACHE_LOADED_AT", 9999999999.0),
        patch("config_loader.load_config"),
        patch("config_loader.start_hot_reload"),
        patch("config_loader.get_config", return_value=_config()),
        patch("litellm.acompletion", acompletion),
        patch("auth.api_key_manager._access_llm_key_secret", return_value="sk-test-key"),
        patch("middleware.g05_cache.G05Cache.store_response", new_callable=AsyncMock),
        patch("middleware.g18_observability._emit_trace", new_callable=AsyncMock),
        patch("cache.redis_pool.init_pool"),
        patch("cache.redis_pool.close_pool", new_callable=AsyncMock),
        patch("cache.redis_pool.get_redis", return_value=redis),
        patch("middleware.g00_rate_limit._get_redis", return_value=redis),
        patch("middleware.g17_loop_control._get_redis", return_value=redis),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        import main
        importlib.reload(main)
        stack.enter_context(patch.object(main, "add_to_spend", add_to_spend))
        async with AsyncClient(transport=ASGITransport(app=main.app),
                               base_url="http://test") as client:
            resp = await client.post("/v1/chat/completions", json=body,
                                     headers={"Authorization": f"Bearer {key}"})
        await main._drain_after_response()
    return resp, acompletion, add_to_spend


_BODY = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}


@pytest.mark.asyncio
async def test_a_team_over_its_cap_gets_a_429():
    resp, acompletion, _ = await _post(_BODY, _TEAM_KEY, team_spent="10")
    assert resp.status_code == 429
    err = resp.json()["error"]
    assert err["code"] == "team_spend_cap_exceeded"
    assert err["type"] == "rate_limit_exceeded"
    assert "team=payments" in err["message"]
    assert resp.headers["retry-after"] == "86400"
    acompletion.assert_not_called()


@pytest.mark.asyncio
async def test_the_same_tenant_without_a_team_is_served():
    resp, acompletion, _ = await _post(_BODY, _PLAIN_KEY, team_spent="10")
    assert resp.status_code == 200
    acompletion.assert_called_once()


@pytest.mark.asyncio
async def test_a_team_under_its_cap_is_served_and_its_cost_counted():
    resp, _, add_to_spend = await _post(_BODY, _TEAM_KEY, team_spent="9")
    assert resp.status_code == 200
    add_to_spend.assert_awaited_once()
    args, kwargs = add_to_spend.await_args
    assert args[0] == f"t:{_TENANT}:" and kwargs["team"] == "payments"


@pytest.mark.asyncio
async def test_the_team_overlay_applies_to_the_team_only():
    body = {"messages": [{"role": "user", "content": "hi"}]}       # no model: the default
    _, team_call, _ = await _post(body, _TEAM_KEY)
    _, plain_call, _ = await _post(body, _PLAIN_KEY)
    assert team_call.await_args.kwargs["model"].endswith("gpt-4o")
    assert plain_call.await_args.kwargs["model"].endswith("gpt-4o-mini")
