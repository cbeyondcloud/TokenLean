"""python -m readonly_role on a real Postgres: the login reads exactly the tables named.

Run as the owner of the proxy's tables (here, as in the LITE local stack, the server's
superuser), the job creates a login that reads usage_events and nothing else, writes nothing,
creates nothing, defaults to read-only transactions, has a statement timeout and a connection
limit; a second run with another table list re-aligns it. An existing role it must not take
over (a superuser, a role with CREATEDB/CREATEROLE/REPLICATION/BYPASSRLS, one that owns
something, or one with rights outside the schema) is refused BEFORE anything changes: its
password still works afterwards. It waits for a database and tables created after it starts
(Langfuse's, at its first boot), and --revoke drops a database's grants (Langfuse off).

Needs a real Postgres: set TEST_PG_DSN to a THROWAWAY server (the test creates the proxy's
tables, roles, a schema and a database, and drops them afterwards).
"""
import asyncio
import os
import secrets
import subprocess
import sys
import time
from urllib.parse import urlsplit

import pytest

asyncpg = pytest.importorskip("asyncpg")
DSN = os.environ.get("TEST_PG_DSN")
pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not DSN, reason="set TEST_PG_DSN to a throwaway Postgres")]

_PROXY = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy"))
ROLE = "lite_test_reader"
OTHER = "lite_test_existing"
LATE_DB = "lite_test_late"
PASSWORD = secrets.token_hex(16)
OTHER_PASSWORD = secrets.token_hex(16)


def _dbname():
    return urlsplit(DSN).path.lstrip("/") or "postgres"


def _job(*args, role=ROLE, wait="20", background=False, dsn=None):
    env = dict(os.environ, DATABASE_URL=dsn or DSN, PYTHONPATH=_PROXY,
               READONLY_ROLE_PASSWORD=PASSWORD)
    env.pop("PROMETHEUS_MULTIPROC_DIR", None)
    cmd = [sys.executable, "-m", "readonly_role", "--role", role, "--wait-seconds", wait, *args]
    if background:
        return subprocess.Popen(cmd, cwd=_PROXY, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
    return subprocess.run(cmd, cwd=_PROXY, env=env, capture_output=True, text=True, timeout=120)


def _sql(*statements, database=None):
    async def go():
        conn = await asyncpg.connect(DSN, **({"database": database} if database else {}))
        try:
            out = None
            for s in statements:
                out = await conn.fetch(s) if s.lstrip().upper().startswith("SELECT") \
                    else await conn.execute(s)
            return out
        finally:
            await conn.close()
    return asyncio.run(go())


def _as(role, password, sql, database=None):
    """The rows of ``sql`` run as ``role``, or the error's class name. Statements before the
    last ("a; b") run first, in the same session."""
    async def go():
        conn = await asyncpg.connect(DSN, user=role, password=password,
                                     **({"database": database} if database else {}))
        try:
            *before, last = sql.split("; ")
            for statement in before:
                await conn.execute(statement)
            return await conn.fetch(last)
        except asyncpg.PostgresError as exc:
            return type(exc).__name__
        finally:
            await conn.close()
    return asyncio.run(go())


def _drop_role(name):
    if _sql(f"SELECT 1 FROM pg_roles WHERE rolname = '{name}'"):
        for db in (_dbname(), LATE_DB):
            if _sql(f"SELECT 1 FROM pg_database WHERE datname = '{db}'"):
                _sql(f"DROP OWNED BY {name}", database=db)
        _sql(f"DROP ROLE {name}")


def _reset():
    _sql(f"DROP DATABASE IF EXISTS {LATE_DB}", "DROP SCHEMA IF EXISTS lite_other CASCADE",
         "REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    for name in (ROLE, OTHER, "lite_test_group"):
        _drop_role(name)


@pytest.fixture(scope="module")
def database():
    """The proxy's tables (python -m schema_setup), and no reader role yet."""
    env = dict(os.environ, DATABASE_URL=DSN, PYTHONPATH=_PROXY)
    env.pop("PROMETHEUS_MULTIPROC_DIR", None)
    done = subprocess.run([sys.executable, "-m", "schema_setup", "--attempts", "3"],
                          cwd=_PROXY, env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    _reset()
    yield
    _reset()


@pytest.fixture
def existing(database):
    """An existing LOGIN role named OTHER (with OTHER_PASSWORD), dropped afterwards."""
    _drop_role(OTHER)
    _sql(f"CREATE ROLE {OTHER} LOGIN PASSWORD '{OTHER_PASSWORD}'")
    yield OTHER
    _sql("DROP SCHEMA IF EXISTS lite_other CASCADE")
    _drop_role(OTHER)


def _denied(role, password, sql, database=None):
    got = _as(role, password, sql, database)
    return isinstance(got, str)


def test_the_login_reads_usage_events_and_nothing_else(database):
    done = _job("--grant", f"{_dbname()}:usage_events")
    assert done.returncode == 0, done.stderr
    assert "reads" in done.stdout and PASSWORD not in done.stdout + done.stderr
    assert _as(ROLE, PASSWORD, "SELECT count(*) FROM usage_events")[0][0] >= 0
    assert _as(ROLE, PASSWORD, "SHOW default_transaction_read_only")[0][0] == "on"
    assert _as(ROLE, PASSWORD, "SHOW statement_timeout")[0][0] == "1min"
    assert _sql(f"SELECT rolconnlimit FROM pg_roles WHERE rolname = '{ROLE}'")[0][0] == 20
    for sql in ("SELECT count(*) FROM proxy_keys", "SELECT count(*) FROM audit_events",
                "INSERT INTO usage_events (tenant_id) VALUES ('x')", "DELETE FROM usage_events",
                "DROP TABLE usage_events", "CREATE TABLE lite_reader_made (n int)",
                "COPY (SELECT 1) TO PROGRAM 'true'",
                # with the read-only default switched off, the grants still refuse
                "SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE; "
                "INSERT INTO usage_events (tenant_id) VALUES ('x')"):
        assert _denied(ROLE, PASSWORD, sql), sql


def test_large_object_writes_are_refused_where_asked(database):
    """--lock-large-objects: the large-object write functions are revoked from PUBLIC in that
    database (TokenLean uses none), so a session that switches the read-only default off still
    cannot write one."""
    done = _job("--grant", f"{_dbname()}:usage_events", "--lock-large-objects", _dbname())
    assert done.returncode == 0, done.stderr
    got = _as(ROLE, PASSWORD, "SET default_transaction_read_only = off; "
                              "SELECT lo_from_bytea(0, 'x'::bytea)")
    assert got == "InsufficientPrivilegeError", got
    _sql("GRANT EXECUTE ON FUNCTION lo_from_bytea(oid, bytea), lo_creat(integer), "
         "lo_create(oid), lo_put(oid, bigint, bytea), lowrite(integer, bytea), "
         "lo_truncate(integer, integer), lo_truncate64(integer, bigint), lo_unlink(oid), "
         "lo_import(text), lo_import(text, oid) TO PUBLIC")


def test_a_second_run_follows_the_table_list_and_strips_extra_rights(database):
    _sql(f"GRANT INSERT, DELETE ON usage_events TO {ROLE}",
         f"GRANT SELECT ON proxy_keys TO {ROLE}", f"GRANT CREATE ON SCHEMA public TO {ROLE}",
         f"ALTER ROLE {ROLE} RESET default_transaction_read_only",
         "CREATE ROLE lite_test_group NOLOGIN", f"GRANT lite_test_group TO {ROLE}")
    try:
        done = _job("--grant", f"{_dbname()}:usage_events,routing_overlay_versions")
        assert done.returncode == 0, done.stderr
        assert _as(ROLE, PASSWORD, "SELECT count(*) FROM routing_overlay_versions")[0][0] >= 0
        assert _denied(ROLE, PASSWORD, "SELECT count(*) FROM proxy_keys")
        assert _as(ROLE, PASSWORD, "SHOW default_transaction_read_only")[0][0] == "on"
        assert not _sql(f"SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
                        f"WHERE r.rolname = '{ROLE}'")
        assert _job("--grant", f"{_dbname()}:usage_events").returncode == 0
        assert _denied(ROLE, PASSWORD, "SELECT count(*) FROM routing_overlay_versions")
    finally:
        _sql("DROP ROLE IF EXISTS lite_test_group")


# ── An existing role it must not take over is refused before any change ──────


@pytest.mark.parametrize("setup, reason", [
    ("ALTER ROLE {r} SUPERUSER", "superuser"),
    ("ALTER ROLE {r} CREATEDB", "create databases"),
    ("ALTER ROLE {r} CREATEROLE", "create roles"),
    ("ALTER ROLE {r} REPLICATION", "replicate"),
    ("ALTER ROLE {r} BYPASSRLS", "row security"),
    ("CREATE SCHEMA lite_other AUTHORIZATION {r}", "owns"),
    ("GRANT CREATE ON DATABASE {db} TO {r}", "create schemas"),
    ("CREATE SCHEMA lite_other; CREATE TABLE lite_other.t (n int); "
     "GRANT USAGE ON SCHEMA lite_other TO {r}; GRANT SELECT ON lite_other.t TO {r}",
     "outside schema public"),
    ("GRANT SELECT ON usage_events TO {r} WITH GRANT OPTION", "grant option"),
])
def test_an_existing_role_with_more_rights_is_refused_and_left_unchanged(existing, setup,
                                                                       reason):
    for statement in setup.format(r=existing, db=_dbname()).split("; "):
        _sql(statement)
    before = _sql(f"SELECT rolsuper, rolcreatedb, rolcreaterole, rolconfig, rolconnlimit "
                  f"FROM pg_roles WHERE rolname = '{existing}'")
    done = _job("--grant", f"{_dbname()}:usage_events", role=existing)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "refused" in done.stderr and reason in done.stderr, done.stderr
    assert "nothing was changed" in done.stderr
    # its own password still works, and nothing about it changed
    assert [tuple(r) for r in _as(existing, OTHER_PASSWORD, "SELECT 1")] == [(1,)]
    after = _sql(f"SELECT rolsuper, rolcreatedb, rolcreaterole, rolconfig, rolconnlimit "
                 f"FROM pg_roles WHERE rolname = '{existing}'")
    assert before == after


def test_an_existing_plain_login_role_is_taken_over(existing):
    """A role with nothing beyond LOGIN (one made by hand, say) passes the check."""
    done = _job("--grant", f"{_dbname()}:usage_events", role=existing)
    assert done.returncode == 0, done.stderr
    assert _as(existing, PASSWORD, "SELECT count(*) FROM usage_events")[0][0] >= 0


def test_public_create_on_schema_public_is_not_the_role_s_own(database):
    """PostgreSQL 14 and earlier give PUBLIC CREATE on schema public: that is reported, not
    counted as the role's own right, so a PG14 server's install or upgrade still succeeds."""
    _sql("GRANT CREATE ON SCHEMA public TO PUBLIC")
    try:
        done = _job("--grant", f"{_dbname()}:usage_events")
        assert done.returncode == 0, done.stderr
        assert "PUBLIC" in done.stderr                    # said, as a warning
    finally:
        _sql("REVOKE CREATE ON SCHEMA public FROM PUBLIC")


# ── Waiting, revoking, failing ──────────────────────────────────────────────────


def test_it_waits_for_a_database_and_tables_created_after_it_starts(database):
    job = _job("--grant", f"{_dbname()}:usage_events", "--grant",
               f"{LATE_DB}:observations,scores", wait="60", background=True)
    time.sleep(3)
    _sql(f"CREATE DATABASE {LATE_DB}")
    time.sleep(3)
    _sql("CREATE TABLE observations (id text)", "CREATE TABLE scores (id text)",
         "CREATE TABLE users (id text, password text)", database=LATE_DB)
    _out, err = job.communicate(timeout=90)
    assert job.returncode == 0, err
    assert _as(ROLE, PASSWORD, "SELECT count(*) FROM observations", LATE_DB)[0][0] == 0
    assert _denied(ROLE, PASSWORD, "SELECT count(*) FROM users", LATE_DB)


def test_revoke_drops_a_database_s_grants_and_skips_a_missing_one(database):
    """Langfuse off: --revoke langfuse takes back what an earlier --langfuse run granted."""
    if not _sql(f"SELECT 1 FROM pg_database WHERE datname = '{LATE_DB}'"):
        _sql(f"CREATE DATABASE {LATE_DB}")
        _sql("CREATE TABLE observations (id text)", database=LATE_DB)
    assert _job("--grant", f"{_dbname()}:usage_events", "--grant",
                f"{LATE_DB}:observations").returncode == 0
    assert isinstance(_as(ROLE, PASSWORD, "SELECT count(*) FROM observations", LATE_DB), list)
    done = _job("--grant", f"{_dbname()}:usage_events", "--revoke", LATE_DB)
    assert done.returncode == 0, done.stderr
    assert _denied(ROLE, PASSWORD, "SELECT count(*) FROM observations", LATE_DB)
    skipped = _job("--grant", f"{_dbname()}:usage_events", "--revoke", "lite_no_such_db",
                   wait="2")
    assert skipped.returncode == 0, skipped.stderr


def test_a_table_that_never_appears_fails_the_job(database):
    done = _job("--grant", f"{_dbname()}:usage_events,no_such_table", wait="3")
    assert done.returncode == 1
    assert "no_such_table" in done.stderr


def test_the_owner_itself_is_refused(database):
    owner = _sql("SELECT current_user::text")[0][0]
    done = _job("--grant", f"{_dbname()}:usage_events", role=owner, wait="5")
    assert done.returncode == 1 and "owner itself" in done.stderr


def test_a_wrong_owner_password_fails_at_once_without_retrying(database):
    parts = urlsplit(DSN)
    host = parts.netloc.rsplit("@", 1)[-1]
    wrong = parts._replace(netloc=f"{parts.username}:not-the-password@{host}").geturl()
    start = time.monotonic()
    done = _job("--grant", f"{_dbname()}:usage_events", wait="60", dsn=wrong)
    assert done.returncode == 1
    assert "authentication" in done.stderr.lower() and time.monotonic() - start < 30
    assert "not-the-password" not in done.stderr
