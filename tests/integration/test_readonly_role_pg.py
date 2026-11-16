"""python -m readonly_role on a real Postgres: the login reads exactly the tables named.

Run as the owner of the proxy's tables (here, as in the LITE local stack, the server's
superuser), the job creates a login that reads usage_events and nothing else, writes nothing,
creates nothing, and defaults to read-only transactions; a second run with another table list
re-aligns it, and an existing role with extra rights is stripped of them. It waits for a
database and a table created after it starts (Langfuse's, at its first boot).

Needs a real Postgres: set TEST_PG_DSN to a THROWAWAY server (the test creates the proxy's
tables, a role and a database, and drops the role and the database afterwards).
"""
import asyncio
import os
import secrets
import subprocess
import sys
import time

import pytest

asyncpg = pytest.importorskip("asyncpg")
DSN = os.environ.get("TEST_PG_DSN")
pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not DSN, reason="set TEST_PG_DSN to a throwaway Postgres")]

_PROXY = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy"))
ROLE = "lite_test_reader"
LATE_DB = "lite_test_late"
PASSWORD = secrets.token_hex(16)


def _job(*args, wait="20", background=False):
    env = dict(os.environ, DATABASE_URL=DSN, PYTHONPATH=_PROXY, READONLY_ROLE_PASSWORD=PASSWORD)
    env.pop("PROMETHEUS_MULTIPROC_DIR", None)
    cmd = [sys.executable, "-m", "readonly_role", "--role", ROLE, "--wait-seconds", wait, *args]
    if background:
        return subprocess.Popen(cmd, cwd=_PROXY, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
    return subprocess.run(cmd, cwd=_PROXY, env=env, capture_output=True, text=True, timeout=120)


async def _owner(database=None):
    return await asyncpg.connect(DSN, **({"database": database} if database else {}))


async def _as_reader(database=None):
    return await asyncpg.connect(DSN, user=ROLE, password=PASSWORD,
                                 **({"database": database} if database else {}))


async def _reset():
    conn = await _owner()
    try:
        await conn.execute(f"DROP DATABASE IF EXISTS {LATE_DB}")
        if await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", ROLE):
            await conn.execute(f"DROP OWNED BY {ROLE}")
            await conn.execute(f"DROP ROLE {ROLE}")
    finally:
        await conn.close()


@pytest.fixture(scope="module")
def database():
    """The proxy's tables (python -m schema_setup), and no reader role yet."""
    env = dict(os.environ, DATABASE_URL=DSN, PYTHONPATH=_PROXY)
    env.pop("PROMETHEUS_MULTIPROC_DIR", None)
    done = subprocess.run([sys.executable, "-m", "schema_setup", "--attempts", "3"],
                          cwd=_PROXY, env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    asyncio.run(_reset())
    yield
    asyncio.run(_reset())


async def _denied(conn, sql):
    try:
        await conn.execute(sql)
    except asyncpg.PostgresError as exc:
        return type(exc).__name__
    return None


def test_the_login_reads_usage_events_and_nothing_else(database):
    done = _job("--grant", f"{_dbname()}:usage_events")
    assert done.returncode == 0, done.stderr
    assert "reads" in done.stdout and PASSWORD not in done.stdout + done.stderr

    async def probe():
        conn = await _as_reader()
        try:
            assert await conn.fetchval("SELECT count(*) FROM usage_events") >= 0
            assert await conn.fetchval("SHOW default_transaction_read_only") == "on"
            out = {}
            for name, sql in {
                "read proxy_keys": "SELECT count(*) FROM proxy_keys",
                "read audit_events": "SELECT count(*) FROM audit_events",
                "insert": "INSERT INTO usage_events (tenant_id) VALUES ('x')",
                "delete": "DELETE FROM usage_events",
                "drop": "DROP TABLE usage_events",
                "create": "CREATE TABLE lite_reader_made (n int)",
                "program": "COPY (SELECT 1) TO PROGRAM 'true'",
                # Even with the read-only default switched off, the grants still refuse.
                "insert read-write": "BEGIN READ WRITE; "
                                     "INSERT INTO usage_events (tenant_id) VALUES ('x'); COMMIT",
            }.items():
                out[name] = await _denied(conn, sql)
                if conn.is_in_transaction():
                    await conn.execute("ROLLBACK")
            return out
        finally:
            await conn.close()
    denied = asyncio.run(probe())
    assert all(denied.values()), {k: v for k, v in denied.items() if not v}


def test_a_second_run_follows_the_table_list_and_strips_extra_rights(database):
    async def widen():
        conn = await _owner()
        try:
            await conn.execute(f"GRANT INSERT, DELETE ON usage_events TO {ROLE}")
            await conn.execute(f"GRANT SELECT ON proxy_keys TO {ROLE}")
            await conn.execute(f"GRANT CREATE ON SCHEMA public TO {ROLE}")
            await conn.execute(f"ALTER ROLE {ROLE} CREATEDB")
            await conn.execute(f"ALTER ROLE {ROLE} RESET default_transaction_read_only")
            await conn.execute("CREATE ROLE lite_test_group NOLOGIN")
            await conn.execute(f"GRANT lite_test_group TO {ROLE}")
        finally:
            await conn.close()
    asyncio.run(widen())
    try:
        done = _job("--grant", f"{_dbname()}:usage_events,routing_overlay_versions")
        assert done.returncode == 0, done.stderr

        async def state():
            conn = await _owner()
            try:
                import readonly_role
                return await readonly_role.check(
                    conn, ROLE, ("usage_events", "routing_overlay_versions"))
            finally:
                await conn.close()
        assert asyncio.run(state()) == []
        assert _job("--grant", f"{_dbname()}:usage_events").returncode == 0

        async def reads_routing():
            conn = await _as_reader()
            try:
                return await _denied(conn, "SELECT count(*) FROM routing_overlay_versions")
            finally:
                await conn.close()
        assert asyncio.run(reads_routing())                 # dropped from the list: refused
    finally:
        async def drop_group():
            conn = await _owner()
            try:
                await conn.execute("DROP ROLE IF EXISTS lite_test_group")
            finally:
                await conn.close()
        asyncio.run(drop_group())


def test_it_waits_for_a_database_and_tables_created_after_it_starts(database):
    job = _job("--grant", f"{LATE_DB}:observations,scores", wait="60", background=True)

    async def create_late():
        conn = await _owner()
        try:
            await conn.execute(f"CREATE DATABASE {LATE_DB}")
        finally:
            await conn.close()
        await asyncio.sleep(3)
        conn = await _owner(LATE_DB)
        try:
            await conn.execute("CREATE TABLE observations (id text); CREATE TABLE scores (id text);"
                               "CREATE TABLE users (id text, password text)")
        finally:
            await conn.close()
    time.sleep(3)
    asyncio.run(create_late())
    out, err = job.communicate(timeout=90)
    assert job.returncode == 0, err

    async def probe():
        conn = await _as_reader(LATE_DB)
        try:
            await conn.fetchval("SELECT count(*) FROM observations")
            await conn.fetchval("SELECT count(*) FROM scores")
            return await _denied(conn, "SELECT count(*) FROM users")
        finally:
            await conn.close()
    assert asyncio.run(probe())


def test_a_table_that_never_appears_fails_the_job(database):
    done = _job("--grant", f"{_dbname()}:usage_events,no_such_table", wait="3")
    assert done.returncode == 1
    assert "no_such_table" in done.stderr


def test_the_owner_itself_is_refused(database):
    async def owner_name():
        conn = await _owner()
        try:
            return await conn.fetchval("SELECT current_user::text")
        finally:
            await conn.close()
    env = dict(os.environ, DATABASE_URL=DSN, PYTHONPATH=_PROXY, READONLY_ROLE_PASSWORD=PASSWORD)
    done = subprocess.run([sys.executable, "-m", "readonly_role", "--role", asyncio.run(owner_name()),
                           "--grant", f"{_dbname()}:usage_events", "--wait-seconds", "5"],
                          cwd=_PROXY, env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 1 and "owner itself" in done.stderr


def _dbname():
    from urllib.parse import urlsplit
    return urlsplit(DSN).path.lstrip("/") or "postgres"
