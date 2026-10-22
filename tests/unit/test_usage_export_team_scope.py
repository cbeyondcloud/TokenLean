"""/admin/usage-export and teams (owner decision 82).

A team-bound key exports ONLY its own team's rows of its own tenant, whatever it puts in
the body or the query string. A key with no team binding keeps the tenant view exactly as
before (and an admin key its cross-tenant view).
"""
import contextlib
import copy
import hashlib
import importlib
import json
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from tests import conftest as c

_KEYS = {
    hashlib.sha256(b"k-payments").hexdigest():
        {"tenant_id": "ACME-PRD-01", "tier": "enterprise", "team": "payments"},
    hashlib.sha256(b"k-tenant").hexdigest(): {"tenant_id": "ACME-PRD-01", "tier": "enterprise"},
    hashlib.sha256(b"k-admin").hexdigest():
        {"tenant_id": "ops", "tier": "enterprise", "admin": True},
    hashlib.sha256(b"k-admin-team").hexdigest():
        {"tenant_id": "ops", "tier": "enterprise", "admin": True, "team": "sre"},
}


class _Conn:
    def __init__(self):
        self.calls = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return []


class _Pool:
    def __init__(self):
        self.conn = _Conn()

    def acquire(self):
        conn = self.conn

        class _Acq:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return _Acq()


async def _export(key, body=None, query=""):
    pool = _Pool()

    async def _get_pool(_dsn):
        return pool

    patches = [
        patch("auth.api_key_manager._fetch_secret", return_value=json.dumps(_KEYS)),
        patch("auth.api_key_manager._KEY_CACHE", copy.deepcopy(_KEYS)),
        patch("auth.api_key_manager._CACHE_LOADED_AT", 9999999999.0),
        patch("config_loader.load_config"),
        patch("config_loader.start_hot_reload"),
        patch("config_loader.get_config", return_value=c._integration_config()),
        patch("cache.redis_pool.init_pool"),
        patch("cache.redis_pool.close_pool", new_callable=AsyncMock),
        patch("cache.redis_pool.get_redis", return_value=c._make_mock_redis()),
        patch("cache.pg_pool.get_pg_pool", _get_pool),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        stack.enter_context(patch.dict("os.environ", {"DATABASE_URL": "postgresql://fake/db"}))
        import main
        importlib.reload(main)
        async with AsyncClient(transport=ASGITransport(app=main.app),
                               base_url="http://test") as client:
            resp = await client.post(f"/admin/usage-export{query}", json=body or {},
                                     headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 200, resp.text
    [(sql, args)] = pool.conn.calls
    return sql, args


@pytest.mark.asyncio
async def test_a_team_key_reads_only_its_own_team():
    sql, args = await _export("k-payments")
    assert "tenant_id = $3" in sql and "team = $4" in sql
    assert args[2:] == ("ACME-PRD-01", "payments")


@pytest.mark.asyncio
async def test_a_team_key_cannot_widen_it_by_body_or_query():
    body = {"tenant_id": "OTHER-PRD-01", "team": "search"}
    sql, args = await _export("k-payments", body, query="?team=search&tenant_id=OTHER-PRD-01")
    assert args[2:] == ("ACME-PRD-01", "payments")
    assert "search" not in args and "OTHER-PRD-01" not in args


@pytest.mark.asyncio
async def test_an_unbound_key_keeps_the_tenant_view():
    sql, args = await _export("k-tenant", {"team": "payments"})
    assert "team =" not in sql
    assert args[2:] == ("ACME-PRD-01",)


@pytest.mark.asyncio
async def test_an_admin_key_keeps_its_view():
    sql, args = await _export("k-admin", {"tenant_id": "ACME-PRD-01"})
    assert "team =" not in sql and args[2:] == ("ACME-PRD-01",)
    sql, args = await _export("k-admin")
    assert "tenant_id =" not in sql and len(args) == 2


@pytest.mark.asyncio
async def test_a_team_bound_admin_key_is_scoped_like_any_team_key():
    sql, args = await _export("k-admin-team", {"tenant_id": "ACME-PRD-01"})
    assert args[2:] == ("ops", "sre")
