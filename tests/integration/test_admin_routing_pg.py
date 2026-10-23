"""Real Postgres: the routing overlay store behind /admin/routing (ST-10).

* versions are appended under an advisory lock: concurrent writers on separate pools (two
  replicas) never get the same version number, and a stale ``expected_version`` writes
  nothing;
* history keeps every version with who/when; a rollback is a new row;
* another replica's poller (``sync_once``) applies the latest version, and a row edited by
  hand to touch a protected group is refused there;
* infra/migrations/routing_overlay.sql is the app's DDL (idempotent over it); the down script
  removes the table.

TEST_PG_DSN must point at a THROWAWAY database: the tests drop routing_overlay_versions.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import pytest  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_PG_DSN"),
                                reason="set TEST_PG_DSN to a throwaway Postgres")

_MIGRATIONS = Path(__file__).resolve().parents[2] / "infra" / "migrations"
DSN = os.environ.get("TEST_PG_DSN")
V1 = {"groups": {"G6_routing": {"tiers": {"simple": ["gpt-4o"]}}}}


async def _pool():
    import asyncpg
    return await asyncpg.create_pool(DSN, min_size=1, max_size=4)


async def _reset(pool):
    from admin_api import routing
    async with pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS routing_overlay_versions")
    await routing.ensure_schema(pool)


@pytest.fixture
def clean_overlay(monkeypatch):
    import config_loader
    from admin_api import routing
    base = {"groups": {"G6_routing": {"enabled": True}}}
    monkeypatch.setattr(config_loader, "_config", base)
    monkeypatch.setattr(config_loader, "_base_config", base)
    monkeypatch.setattr(config_loader, "_runtime_overlay", {})
    routing._applied.update(version=0, sha256=None)
    yield
    config_loader.set_runtime_overlay({})
    routing._applied.update(version=0, sha256=None)


def test_concurrent_writers_get_distinct_versions_and_a_stale_one_writes_nothing():
    from admin_api import routing

    async def exercise():
        a, b = await _pool(), await _pool()
        try:
            await _reset(a)
            rows = await asyncio.gather(*[
                routing.append(pool, {"teams": {f"t{i}": {"monthly_spend_cap_usd": i}}},
                               action="applied", actor=f"admin-key:ops:key={i:016x}")
                for i, pool in enumerate([a, b] * 5)])
            conflict = None
            try:
                await routing.append(a, V1, action="applied", actor="x", expected_version=3)
            except routing.VersionConflict as exc:
                conflict = exc.current
            hist = await routing.history(b, 50)
            return sorted(r["version"] for r in rows), conflict, hist
        finally:
            await a.close()
            await b.close()

    versions, conflict, hist = asyncio.run(exercise())
    assert versions == list(range(1, 11))
    assert conflict == 10
    assert [h["version"] for h in hist] == list(range(10, 0, -1))
    assert all(h["actor"].startswith("admin-key:ops") and h["created_at"] for h in hist)


def test_a_rollback_is_a_new_row_and_another_replica_applies_the_latest(clean_overlay):
    import config_loader
    from admin_api import routing

    async def exercise():
        pool = await _pool()
        try:
            await _reset(pool)
            one = await routing.append(pool, V1, action="applied", actor="admin-key:ops",
                                       comment="first")
            await routing.append(pool, {"teams": {"payments": {"monthly_spend_cap_usd": 5}}},
                                 action="applied", actor="admin-key:ops")
            back = await routing.append(pool, one["fragment"], action="rolled_back",
                                        actor="admin-key:ops", source_version=1)
            synced = await routing.sync_once(pool)       # a replica that has applied nothing
            current = await routing.current(pool)
            # Edited by hand to reach a protected group: refused by the replica's re-check.
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO routing_overlay_versions (version, fragment, action, actor, "
                    "fragment_sha256) VALUES (4, $1::jsonb, 'applied', 'psql', 'x')",
                    json.dumps({"groups": {"G30_guardrails": {"mode": "allow"}}}))
            refused = await routing.sync_once(pool)
            return back, synced, current, refused
        finally:
            await pool.close()

    back, synced, current, refused = asyncio.run(exercise())
    assert back["version"] == 3 and back["source_version"] == 1
    assert back["fragment"] == V1 and current["action"] == "rolled_back"
    assert synced is True
    assert config_loader.get_config()["groups"]["G6_routing"]["tiers"]["simple"] == ["gpt-4o"]
    assert refused is False and routing.applied_version() == 3     # version 3 is kept
    groups = config_loader.get_config()["groups"]
    assert "G30_guardrails" not in groups and groups["G6_routing"]["tiers"]["simple"] == [
        "gpt-4o"]


def test_the_migration_matches_the_app_ddl_and_the_down_script_removes_it():
    from admin_api import routing

    async def exercise():
        import asyncpg
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute("DROP TABLE IF EXISTS routing_overlay_versions")
            await conn.execute((_MIGRATIONS / "routing_overlay.sql").read_text(encoding="utf-8"))
            await conn.execute(routing.ROUTING_OVERLAY_DDL)
            await conn.execute(routing.ROUTING_OVERLAY_DDL)
            cols = [tuple(r) for r in await conn.fetch(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = 'routing_overlay_versions' ORDER BY 1")]
            await conn.execute((_MIGRATIONS / "down" / "routing_overlay.sql")
                               .read_text(encoding="utf-8"))
            left = await conn.fetchval("SELECT to_regclass('routing_overlay_versions') IS NULL")
            return cols, left
        finally:
            await conn.close()

    cols, gone = asyncio.run(exercise())
    assert [c[0] for c in cols] == sorted(["version", "fragment", "action", "actor",
                                           "created_at", "comment", "source_version",
                                           "fragment_sha256"])
    assert gone is True


async def test_the_endpoint_end_to_end_on_postgres(clean_overlay, monkeypatch):
    import hashlib
    from httpx import ASGITransport, AsyncClient
    import config_loader
    import main
    from auth import api_key_manager as akm

    class _Audit:
        rows = []

        async def log_config_change(self, **row):
            self.rows.append(row)
            return True

    pool = await _pool()
    try:
        await _reset(pool)
        akm.replace_cache({hashlib.sha256(b"k-root").hexdigest(): {"tenant_id": "ops",
                                                                    "admin": True}})
        monkeypatch.setattr(main, "_db_pool", pool)
        monkeypatch.setattr(main, "_audit_logger", _Audit())
        headers = {"Authorization": "Bearer k-root"}
        async with AsyncClient(transport=ASGITransport(app=main.app),
                               base_url="http://test") as client:
            r = await client.put("/admin/routing", headers=headers,
                                 json={"fragment": V1, "expected_version": 0})
            assert r.status_code == 200 and r.json()["version"] == 1, r.text
            r = await client.put("/admin/routing", headers=headers, json={
                "fragment": {"groups": {"G17_loop": {"enabled": False}}}})
            assert r.status_code == 422
            r = await client.post("/admin/routing/rollback", headers=headers,
                                  json={"version": 0, "expected_version": 1})
            assert r.status_code == 200 and r.json()["version"] == 2
            hist = (await client.get("/admin/routing/history", headers=headers)).json()
        assert [v["version"] for v in hist["versions"]] == [2, 1]
        assert hist["versions"][0]["action"] == "rolled_back"
        assert config_loader.get_config() is config_loader.get_base_config()
    finally:
        await pool.close()
