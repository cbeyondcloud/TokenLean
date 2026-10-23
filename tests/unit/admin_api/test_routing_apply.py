"""Applying a stored routing overlay version on a replica (ST-10).

``apply_row`` swaps the runtime overlay in (``config_loader.set_runtime_overlay``) after
checking the fragment's shape again: a row that reaches a protected group (edited by hand in
the database) is refused and the replica keeps the version it has. ``sync_once`` applies the
store's latest version when it is not the applied one; ``run_poller`` does that every
``admin_api.routing.poll_seconds`` once the database is wired.
"""
import asyncio

import pytest

import config_loader
from admin_api import routing

BASE = {"groups": {"G6_routing": {"enabled": True, "tiers": {"simple": ["a"]}}}}
ROW1 = {"version": 1, "fragment": {"groups": {"G6_routing": {"tiers": {"simple": ["b"]}}}},
        "action": "applied", "fragment_sha256": "s1"}
BAD = {"version": 2, "fragment": {"groups": {"G29_pii_redaction": {"enabled": False}}},
       "action": "applied", "fragment_sha256": "s2"}


@pytest.fixture(autouse=True)
def state(monkeypatch):
    base = {"groups": {"G6_routing": {"enabled": True, "tiers": {"simple": ["a"]}}}}
    monkeypatch.setattr(config_loader, "_config", base)
    monkeypatch.setattr(config_loader, "_base_config", base)
    monkeypatch.setattr(config_loader, "_runtime_overlay", {})
    monkeypatch.setattr(routing, "_applied", {"version": 0, "sha256": None})
    monkeypatch.setattr(routing, "_sync_state", {"failing": False, "refused": None})
    yield base
    config_loader.set_runtime_overlay({})


def _tiers():
    return config_loader.get_config()["groups"]["G6_routing"]["tiers"]


def test_a_row_is_applied_and_none_clears_it(state):
    assert routing.apply_row(ROW1) is True
    assert _tiers() == {"simple": ["b"]} and routing.applied_version() == 1
    assert routing.apply_row(None) is True
    assert config_loader.get_config() is state and routing.applied_version() == 0


def test_a_row_reaching_a_protected_group_is_refused_and_the_old_one_kept():
    routing.apply_row(ROW1)
    assert routing.apply_row(BAD) is False
    assert routing.applied_version() == 1 and _tiers() == {"simple": ["b"]}
    assert "G29_pii_redaction" not in config_loader.get_config()["groups"]


async def test_sync_once_applies_only_a_new_version(monkeypatch):
    fetched = []

    async def current_version(pool):
        return 1

    async def get_version(pool, version):
        fetched.append(version)
        return dict(ROW1)

    monkeypatch.setattr(routing, "current_version", current_version)
    monkeypatch.setattr(routing, "get_version", get_version)
    assert await routing.sync_once(object()) is True
    assert await routing.sync_once(object()) is True
    assert fetched == [1] and _tiers() == {"simple": ["b"]}


async def test_the_poller_waits_for_the_database_then_follows_the_store(monkeypatch):
    pool = {"value": None}
    versions = {"latest": 0}
    calls = []

    async def ensure_schema(p):
        calls.append("schema")

    async def current_version(p):
        calls.append("version")
        return versions["latest"]

    async def get_version(p, v):
        return {**ROW1, "version": v}

    monkeypatch.setattr(routing, "ensure_schema", ensure_schema)
    monkeypatch.setattr(routing, "current_version", current_version)
    monkeypatch.setattr(routing, "get_version", get_version)
    monkeypatch.setattr(routing, "poll_seconds", lambda cfg: 0.01)
    task = asyncio.create_task(routing.run_poller(lambda: pool["value"], lambda: {}))
    await asyncio.sleep(0.05)
    assert calls == []                                  # no database yet: nothing read
    pool["value"] = object()
    await asyncio.sleep(0.05)
    assert calls[0] == "schema" and calls.count("schema") == 1
    versions["latest"] = 4
    await asyncio.sleep(0.05)
    assert routing.applied_version() == 4
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_poll_seconds_reads_the_config_with_a_floor():
    assert routing.poll_seconds({}) == 5.0
    assert routing.poll_seconds({"admin_api": {"routing": {"poll_seconds": 2}}}) == 2.0
    assert routing.poll_seconds({"admin_api": {"routing": {"poll_seconds": 0.1}}}) == 1.0
    assert routing.poll_seconds({"admin_api": {"routing": {"poll_seconds": "x"}}}) == 5.0


async def test_sync_never_steps_back_over_a_newer_local_apply(monkeypatch):
    async def current_version(pool):
        return 1

    async def get_version(pool, version):
        routing.apply_row({**ROW1, "version": 2,              # a PUT landed meanwhile
                           "fragment": {"groups": {"G6_routing": {"tiers": {"simple": ["c"]}}}}})
        return dict(ROW1)

    monkeypatch.setattr(routing, "current_version", current_version)
    monkeypatch.setattr(routing, "get_version", get_version)
    await routing.sync_once(object())
    assert routing.applied_version() == 2 and _tiers() == {"simple": ["c"]}


async def test_a_refused_version_is_logged_once(monkeypatch, caplog):
    async def current_version(pool):
        return 2

    async def get_version(pool, version):
        return dict(BAD)

    monkeypatch.setattr(routing, "current_version", current_version)
    monkeypatch.setattr(routing, "get_version", get_version)
    for _ in range(3):
        assert await routing.sync_once(object()) is False
    assert sum("refused" in r.getMessage() for r in caplog.records) == 1
    assert routing.sync_failing() is True


async def test_a_failed_startup_sync_shows_on_health(monkeypatch):
    import main

    class _Broken:
        def acquire(self):
            raise OSError("database unreachable")

    monkeypatch.setattr(main, "_db_pool", _Broken())
    monkeypatch.setattr(main, "_db_expected", True)
    monkeypatch.setattr(main, "_usage_meter", object())
    monkeypatch.setattr(main, "_audit_logger", object())
    monkeypatch.setattr(main, "_ROUTING_STARTUP_SYNC_S", 1.0)
    monkeypatch.setattr(main, "_service_task", lambda coro, what: coro.close())
    monkeypatch.setattr(routing, "_sync_state", {"failing": False, "refused": None})
    await main._start_routing_overlay()
    assert "routing_overlay" in main._db_not_wired()
    routing._sync_ok()
    assert "routing_overlay" not in main._db_not_wired()
