"""Real Postgres: the key store behind /admin/keys (ST-9).

* ``key_store.backend: postgres`` wiring (auth/key_store_config.wire_postgres): the tables
  are created, the blob's keys are copied in once, and keys issued afterwards (with a team
  and a user binding) are rows of proxy_keys;
* one-key revoke and rotate on one replica reach another replica through its refresher
  within the refresh interval, and a revoked hash stays revoked;
* infra/migrations/proxy_keys.sql builds the same tables as the app's DDL (which stays
  idempotent over it), and the down script removes them.

TEST_PG_DSN must point at a THROWAWAY database: the tests drop and recreate the key tables.
"""
import asyncio
import hashlib
import json
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import pytest  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_PG_DSN"),
                                reason="set TEST_PG_DSN to a throwaway Postgres")

_MIGRATIONS = Path(__file__).resolve().parents[2] / "infra" / "migrations"
_REFRESH = 5  # CacheRefresher's floor


@pytest.fixture
def instances():
    from tests.integration.pg_key_instances import KeyStoreInstance, reset_tables
    asyncio.run(reset_tables())
    made = []

    def make(name, refresh_interval=None):
        inst = KeyStoreInstance(name, refresh_interval=refresh_interval)
        made.append(inst)
        return inst

    yield make
    for inst in made:
        inst.close()


def test_one_key_revoke_and_rotate_reach_another_replica(instances):
    from tests.integration.pg_key_instances import eventually
    a = instances("a")
    b = instances("b", refresh_interval=_REFRESH)
    raw, key_hash, meta = a.km.create_key("ACME-PRD-01", team="payments",
                                          user="alice@acme.test")
    assert eventually(lambda: b.validate(raw)[0], timeout=_REFRESH + 3)
    ok, tenant, seen = b.validate(raw)
    assert tenant == "ACME-PRD-01"
    assert b.km.key_team(seen) == "payments" and b.km.key_user(seen) == "alice@acme.test"

    new_raw, new_hash, new_meta, old = a.km.rotate_key(key_hash[:16])
    assert old["key_id"] == key_hash[:16] and new_meta["user"] == "alice@acme.test"
    assert a.validate(raw)[0] is False and a.validate(new_raw)[0] is True   # at once here
    assert eventually(lambda: not b.validate(raw)[0] and b.validate(new_raw)[0],
                      timeout=_REFRESH + 3)

    view = a.km.revoke_key(new_hash[:16], tenant_id="ACME-PRD-01", team="payments")
    assert view["key_id"] == new_hash[:16]
    assert eventually(lambda: not b.validate(new_raw)[0], timeout=_REFRESH + 3)

    async def revoked():
        from tests.integration.pg_key_instances import DSN
        import asyncpg
        conn = await asyncpg.connect(DSN)
        try:
            return {r["key_hash"] for r in await conn.fetch(
                "SELECT key_hash FROM revoked_proxy_keys")}
        finally:
            await conn.close()
    assert {key_hash, new_hash} <= asyncio.run(revoked())
    # A stale full-store write from another replica cannot bring the revoked key back.
    b.km._persist_store({new_hash: new_meta})
    assert new_hash not in a.rows()


def test_a_scoped_revoke_of_another_team_s_key_writes_nothing(instances):
    a = instances("a")
    raw, key_hash, _ = a.km.create_key("ACME-PRD-01", team="search")
    assert a.km.revoke_key(key_hash[:16], tenant_id="ACME-PRD-01", team="payments") is None
    assert a.km.rotate_key(key_hash[:16], tenant_id="OTHER") is None
    assert set(a.rows()) == {key_hash} and a.validate(raw)[0] is True


class _Loop:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(30)

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


def test_wiring_postgres_by_config_imports_the_blob_once_and_binds_users(tmp_path,
                                                                        monkeypatch):
    import asyncpg
    from auth import api_key_manager as km
    from auth import key_store_config as ksc
    from tests.integration.pg_key_instances import DSN, reset_tables

    asyncio.run(reset_tables())
    blob = tmp_path / "local-keys.json"
    blob.write_text(json.dumps({hashlib.sha256(b"k-from-blob").hexdigest():
                                {"tenant_id": "ACME-PRD-01", "tier": "free"}}),
                    encoding="utf-8")
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setattr(km, "_LOCAL_PROXY_KEYS_FILE", str(blob))
    settings = ksc.key_store_settings({"key_store": {"backend": "postgres"}})
    ksc.install_pending(settings)
    side = _Loop()

    async def make_pool():  # built ON the side loop, which the pool binds to
        return await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    pool = side.run(make_pool())
    try:
        async def validate(raw):
            return km.validate_proxy_key(raw)
        assert side.run(validate("k-from-blob"))[0] is False          # not wired yet
        refresher = side.run(ksc.wire_postgres(pool, side.loop, settings))
        assert refresher is not None and km.key_store_stale() is False
        assert side.run(validate("k-from-blob"))[0] is True           # imported once
        raw, key_hash, _ = km.create_key("ACME-PRD-01", team="payments",
                                         user="bob@acme.test")       # a worker thread
        assert side.run(validate(raw))[0] is True

        async def row():
            async with pool.acquire() as conn:
                return await conn.fetchrow(
                    "SELECT tenant_id, extra->>'team' AS team, extra->>'user' AS usr "
                    "FROM proxy_keys WHERE key_hash = $1", key_hash)
        r = side.run(row())
        assert (r["tenant_id"], r["team"], r["usr"]) == ("ACME-PRD-01", "payments",
                                                          "bob@acme.test")
        km.revoke_key(hashlib.sha256(b"k-from-blob").hexdigest()[:16])
        side.run(ksc.wire_postgres(pool, side.loop, settings))         # a restart
        assert side.run(validate("k-from-blob"))[0] is False          # never re-imported
    finally:
        km.reset_key_store_backend()
        side.run(pool.close())
        side.close()


def test_the_migration_matches_the_app_ddl_and_the_down_script_removes_it():
    import asyncpg
    from auth import pg_key_store
    from tests.integration.pg_key_instances import DSN

    async def exercise():
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute("DROP TABLE IF EXISTS proxy_keys, revoked_proxy_keys")
            await conn.execute((_MIGRATIONS / "proxy_keys.sql").read_text(encoding="utf-8"))
            await conn.execute(pg_key_store.PROXY_KEYS_DDL)            # idempotent over it
            await conn.execute(pg_key_store.PROXY_KEYS_DDL)
            cols = await conn.fetch(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_name IN ('proxy_keys', 'revoked_proxy_keys') ORDER BY 1, 2")
            await conn.execute((_MIGRATIONS / "down" / "proxy_keys.sql")
                               .read_text(encoding="utf-8"))
            left = await conn.fetchval(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_name IN ('proxy_keys', 'revoked_proxy_keys')")
            await conn.execute("DROP TABLE IF EXISTS proxy_keys, revoked_proxy_keys")
            await conn.execute(pg_key_store.PROXY_KEYS_DDL)
            app_cols = await conn.fetch(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_name IN ('proxy_keys', 'revoked_proxy_keys') ORDER BY 1, 2")
            return [tuple(c) for c in cols], [tuple(c) for c in app_cols], left
        finally:
            await conn.close()

    cols, app_cols, left = asyncio.run(exercise())
    assert cols == app_cols and len(cols) == 10
    assert left == 0
