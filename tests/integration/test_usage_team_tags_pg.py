"""Real Postgres: the team-level (ST-1) and usage-tags (ST-2) columns on usage_events.

The table is built the way a deploy builds it (the in-VPC migration, then the app's
self-heal DDL twice: it must be idempotent). Then:

* a row written today stores its team (and tags, when the release has them);
* an insert that names none of the new columns, as an older release does, still works
  (additive migration: rolling the code back needs no schema change);
* the down script removes the columns, and the older insert still works afterwards.

TEST_PG_DSN must point at a THROWAWAY server: the test drops and recreates usage_events.
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import pytest  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_PG_DSN"),
                                reason="set TEST_PG_DSN to a throwaway Postgres")

_MIGRATIONS = Path(__file__).resolve().parents[2] / "infra" / "migrations"
_OLD_INSERT = ("INSERT INTO usage_events (tenant_id, request_id) VALUES ($1, $2)")


def _ctx(request_id, team):
    from middleware import RequestContext
    from savings.models import SavingsRecord
    savings = SavingsRecord(request_id=request_id, user_id="u1",
                            timestamp=datetime.now(timezone.utc), model_requested="gpt-4o",
                            routed_model="gpt-4o", baseline_tokens=100, final_tokens_sent=80)
    ctx = RequestContext(request_id=request_id, user_id="u1", original_messages=[],
                         messages=[], model="gpt-4o", routed_model="gpt-4o", params={},
                         config={"groups": {}}, savings=savings, tenant_id="ACME-PRD-01",
                         pricing_tier="free")
    ctx.team = team
    return ctx


async def _exercise():
    import asyncpg
    from billing.metering import UsageMeter
    from billing.models import USAGE_EVENTS_DDL
    pool = await asyncpg.create_pool(os.environ["TEST_PG_DSN"], min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS usage_events")
            await conn.execute((_MIGRATIONS / "billing.sql").read_text(encoding="utf-8"))
            await conn.execute(USAGE_EVENTS_DDL)
            await conn.execute(USAGE_EVENTS_DDL)
        await UsageMeter(db_pool=pool).record(_ctx("req-team", "payments"), {})
        await UsageMeter(db_pool=pool).record(_ctx("req-none", "default"), {})
        async with pool.acquire() as conn:
            await conn.execute(_OLD_INSERT, "ACME-PRD-01", "req-old-release")
            rows = {r["request_id"]: dict(r) for r in await conn.fetch(
                "SELECT request_id, team FROM usage_events")}
            await conn.execute((_MIGRATIONS / "down" / "usage_events_team_tags.sql")
                               .read_text(encoding="utf-8"))
            await conn.execute(_OLD_INSERT, "ACME-PRD-01", "req-after-down")
            cols = {r["column_name"] for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'usage_events'")}
            count = await conn.fetchval("SELECT count(*) FROM usage_events")
            await conn.execute("DROP TABLE usage_events")
        return rows, cols, count
    finally:
        await pool.close()


def test_the_columns_are_additive_and_reversible():
    rows, cols, count = asyncio.run(_exercise())
    assert rows["req-team"]["team"] == "payments"
    assert rows["req-none"]["team"] == "default"
    assert rows["req-old-release"]["team"] == ""          # an older release's row
    assert "team" not in cols                             # the down script removed it
    assert count == 4                                     # and nothing else was lost
