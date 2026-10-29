"""python -m schema_setup: the proxy's schema steps, run once per deploy (the LITE chart's hook).

The proxy applies the same steps at startup. Run as a deploy step they must fail loudly: a
missing table or a failed step exits non-zero, so the deploy stops before new replicas start.
"""
import asyncio

import pytest

import schema_setup


class _FakeConn:
    def __init__(self, tables):
        self.tables = tables

    async def fetchval(self, sql, name):
        assert "to_regclass" in sql
        return name if name in self.tables else None


class _FakePool:
    def __init__(self, tables):
        self.conn = _FakeConn(tables)
        self.closed = False

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()

    async def close(self):
        self.closed = True


def _steps(calls, created, fail=None):
    def step(name, tables):
        async def run(pool):
            calls.append(name)
            if name == fail:
                raise RuntimeError(f"{name} broke")
            created.update(tables)
        return run

    return [(name, step(name, tables)) for name, tables in (
        ("usage_events", {"usage_events"}),
        ("audit_events", {"audit_events"}),
        ("proxy_keys", {"proxy_keys", "revoked_proxy_keys"}),
        ("routing_overlay", {"routing_overlay_versions"}))]


def test_every_table_the_proxy_needs_is_checked():
    assert set(schema_setup.TABLES) == {"usage_events", "audit_events", "proxy_keys",
                                        "revoked_proxy_keys", "routing_overlay_versions"}


def test_the_real_steps_are_the_proxys_own():
    names = [name for name, _ in schema_setup.steps()]
    assert names == ["usage_events", "audit_events", "proxy_keys", "routing_overlay"]
    from audit.log import ensure_audit_schema
    from billing.models import ensure_usage_events_schema
    funcs = dict(schema_setup.steps())
    assert funcs["usage_events"] is ensure_usage_events_schema
    assert funcs["audit_events"] is ensure_audit_schema


def test_apply_runs_every_step_in_order_and_reports_nothing_when_all_tables_exist():
    calls, created = [], set()
    pool = _FakePool(created)
    problems = asyncio.run(schema_setup.apply(pool, _steps(calls, created)))
    assert calls == ["usage_events", "audit_events", "proxy_keys", "routing_overlay"]
    assert problems == []


def test_a_failing_step_does_not_stop_the_others_and_is_reported():
    calls, created = [], set()
    pool = _FakePool(created)
    problems = asyncio.run(schema_setup.apply(pool, _steps(calls, created, fail="audit_events")))
    assert calls == ["usage_events", "audit_events", "proxy_keys", "routing_overlay"]
    assert problems == ["audit_events: audit_events broke", "table audit_events is missing"]


def test_a_step_that_swallows_its_error_is_caught_by_the_table_check():
    calls, created = [], set()
    steps = _steps(calls, created)
    steps[0] = ("usage_events", lambda pool: asyncio.sleep(0))   # "succeeds", creates nothing
    problems = asyncio.run(schema_setup.apply(_FakePool(created), steps))
    assert problems == ["table usage_events is missing"]


def test_main_needs_database_url(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert schema_setup.main([]) == 2
    assert "DATABASE_URL" in capsys.readouterr().err


@pytest.mark.parametrize("fail, code", [(None, 0), ("proxy_keys", 1)])
def test_main_exit_code_follows_the_result(monkeypatch, capsys, fail, code):
    calls, created = [], set()
    pool = _FakePool(created)

    async def connect(dsn, attempts, delay):
        assert dsn == "postgresql://tokenlean@db:5432/tokenlean?sslmode=disable"
        return pool

    monkeypatch.setenv("DATABASE_URL", "postgresql://tokenlean@db:5432/tokenlean?sslmode=disable")
    monkeypatch.setattr(schema_setup, "connect", connect)
    monkeypatch.setattr(schema_setup, "steps", lambda: _steps(calls, created, fail=fail))
    assert schema_setup.main([]) == code
    assert pool.closed
    out = capsys.readouterr()
    assert ("schema ready" in out.out) if code == 0 else ("proxy_keys" in out.err)


def test_connect_retries_until_the_database_answers(monkeypatch):
    attempts = []

    async def create_pool(dsn, min_size, max_size):
        attempts.append(dsn)
        if len(attempts) < 3:
            raise OSError("connection refused")
        return "pool"

    monkeypatch.setattr(schema_setup.asyncpg, "create_pool", create_pool)
    assert asyncio.run(schema_setup.connect("postgresql://x@db/t", attempts=5, delay=0)) == "pool"
    assert len(attempts) == 3


def test_connect_gives_up_after_its_attempts(monkeypatch):
    async def create_pool(dsn, min_size, max_size):
        raise OSError("connection refused")

    monkeypatch.setattr(schema_setup.asyncpg, "create_pool", create_pool)
    with pytest.raises(OSError):
        asyncio.run(schema_setup.connect("postgresql://x@db/t", attempts=2, delay=0))
