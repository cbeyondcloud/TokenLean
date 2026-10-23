"""main wires the Postgres key store when ``key_store.backend: postgres`` (ST-9).

The key store is one more database-backed part: wired with billing, the audit log and the
tenant settings, retried with them, reported by /health while it is not wired. With the
default ``blob`` nothing about the database wiring changes.
"""
import asyncio

import pytest

import main
from auth import api_key_manager as akm
from auth import key_store_config as ksc


class _Refresher:
    def __init__(self):
        self.runs = 0

    async def run(self):
        self.runs += 1
        await asyncio.Event().wait()


@pytest.fixture
def wiring(monkeypatch):
    import audit.log
    import billing.models
    import cache.pg_pool
    import retention

    pool = object()
    calls = {"wire": [], "fail": 0}
    refresher = _Refresher()

    async def get_pg_pool(dsn):
        return pool

    async def ok(*_a, **_k):
        return None

    async def wire_postgres(p, loop, settings):
        calls["wire"].append((p, settings["backend"]))
        if len(calls["wire"]) <= calls["fail"]:
            raise RuntimeError("proxy_keys schema step failed")
        return refresher

    monkeypatch.setattr(cache.pg_pool, "get_pg_pool", get_pg_pool)
    monkeypatch.setattr(billing.models, "ensure_usage_events_schema", ok)
    monkeypatch.setattr(audit.log, "ensure_audit_schema", ok)
    monkeypatch.setattr(retention, "run_retention_loop", ok)
    monkeypatch.setattr(ksc, "wire_postgres", wire_postgres)
    for name in ("_db_pool", "_usage_meter", "_audit_logger", "_db_retry_task",
                 "_retention_task", "_key_store_refresher"):
        monkeypatch.setattr(main, name, None)
    monkeypatch.setattr(main, "_db_expected", True)
    monkeypatch.setattr(main._pipeline._tenant_config_loader, "_db_pool", None)
    yield calls, pool, refresher
    akm.reset_key_store_backend()


async def test_the_blob_default_wires_no_key_store(wiring, monkeypatch):
    calls, _pool, _r = wiring
    monkeypatch.setattr(main, "_key_store", ksc.key_store_settings({}))
    await main._wire_database("postgresql://fake/db")
    assert calls["wire"] == [] and main._db_not_wired() == []


async def test_postgres_is_wired_with_the_database_and_refreshed(wiring, monkeypatch):
    calls, pool, refresher = wiring
    monkeypatch.setattr(main, "_key_store",
                        ksc.key_store_settings({"key_store": {"backend": "postgres"}}))
    await main._wire_database("postgresql://fake/db")
    await asyncio.sleep(0)
    assert calls["wire"] == [(pool, "postgres")]
    assert main._db_not_wired() == []
    assert refresher.runs == 1
    await main._wire_database("postgresql://fake/db")            # wired once only
    assert len(calls["wire"]) == 1


async def test_a_failed_key_store_wiring_is_reported_and_retried(wiring, monkeypatch):
    calls, _pool, _r = wiring
    calls["fail"] = 1
    monkeypatch.setattr(main, "_key_store",
                        ksc.key_store_settings({"key_store": {"backend": "postgres"}}))
    with pytest.raises(RuntimeError, match="key_store"):
        await main._wire_database("postgresql://fake/db")
    assert main._db_not_wired() == ["key_store"]
    await main._wire_database("postgresql://fake/db")
    assert main._db_not_wired() == []


def test_postgres_without_a_database_url_refuses_to_start(monkeypatch):
    monkeypatch.setattr(main, "_key_store", None)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="DATABASE_URL"):
        main._select_key_store({"key_store": {"backend": "postgres"}})
    assert akm.key_store_backend_installed() is False
    main._select_key_store({})                                   # the blob needs none


def test_startup_selects_the_key_store_from_the_config(monkeypatch):
    monkeypatch.setattr(main, "_key_store", None)
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")
    main._select_key_store({"key_store": {"backend": "postgres", "max_stale_seconds": 60}})
    assert main._key_store["backend"] == "postgres"
    assert akm.key_store_backend_installed() and akm.key_store_stale()
    akm.reset_key_store_backend()
    with pytest.raises(ValueError):
        main._select_key_store({"key_store": {"backend": "postgress"}})
