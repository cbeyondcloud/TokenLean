"""python -m schema_setup on a real Postgres: every table created, and a second run is a no-op.

Needs a real Postgres: set TEST_PG_DSN to a THROWAWAY database (the test drops the proxy's
tables first). Skipped otherwise.
"""
import os
import subprocess
import sys

import pytest

asyncpg = pytest.importorskip("asyncpg")
DSN = os.environ.get("TEST_PG_DSN")
pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not DSN, reason="set TEST_PG_DSN to a throwaway Postgres")]

_PROXY = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy"))


async def _drop_tables():
    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute("DROP TABLE IF EXISTS usage_events, audit_events, proxy_keys, "
                           "revoked_proxy_keys, routing_overlay_versions CASCADE")
    finally:
        await conn.close()


async def _tables():
    conn = await asyncpg.connect(DSN)
    try:
        rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        return {r["tablename"] for r in rows}
    finally:
        await conn.close()


def _run_job():
    env = dict(os.environ, DATABASE_URL=DSN, PYTHONPATH=_PROXY)
    env.pop("PROMETHEUS_MULTIPROC_DIR", None)
    return subprocess.run([sys.executable, "-m", "schema_setup", "--attempts", "3",
                           "--delay", "1"], cwd=_PROXY, env=env, capture_output=True, text=True,
                          timeout=120)


async def test_the_job_creates_every_table_and_is_idempotent():
    import schema_setup
    await _drop_tables()
    first = _run_job()
    assert first.returncode == 0, first.stdout + first.stderr
    assert "schema ready" in first.stdout
    assert set(schema_setup.TABLES) <= await _tables()
    second = _run_job()
    assert second.returncode == 0, second.stdout + second.stderr
