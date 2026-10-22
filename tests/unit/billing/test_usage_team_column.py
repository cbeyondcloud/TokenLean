"""usage_events.team (ST-1): the trusted team of each request.

Written from ``ctx.team`` (the key's team binding, else a gateway's X-Team, else
"default"). The migration is additive and reversible: a NOT NULL column with a DEFAULT,
so rows written before it read '' (not recorded) and code that does not know the column
keeps inserting; the down script drops it.
"""
import re
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from billing.metering import UsageMeter
from billing.models import USAGE_EVENTS_DDL, UsageEvent
from middleware import RequestContext
from savings.models import SavingsRecord

_ROOT = Path(__file__).resolve().parents[3]
_UP = _ROOT / "infra" / "migrations" / "billing.sql"
_DOWN = _ROOT / "infra" / "migrations" / "down" / "usage_events_team_tags.sql"
_COLUMN = re.compile(r"ADD COLUMN IF NOT EXISTS team TEXT NOT NULL DEFAULT ''")


def _ctx(team=None):
    savings = SavingsRecord(request_id="r1", user_id="u1", timestamp=datetime.now(timezone.utc),
                            model_requested="gpt-4o", routed_model="gpt-4o", baseline_tokens=10)
    ctx = RequestContext(request_id="r1", user_id="u1", original_messages=[], messages=[],
                         model="gpt-4o", routed_model="gpt-4o", params={}, config={"groups": {}},
                         savings=savings, tenant_id="acme")
    if team is not None:
        ctx.team = team
    return ctx


def _insert(conn):
    sql, *params = conn.execute.call_args[0]
    cols = [c.strip() for c in re.search(r"INSERT INTO usage_events\s*\((.*?)\)\s*VALUES",
                                         sql, re.S).group(1).split(",") if c.strip()]
    return dict(zip(cols, params))


def test_the_event_carries_the_team():
    assert UsageEvent(tenant_id="t", request_id="r", timestamp=datetime.now(timezone.utc),
                      baseline_tokens=0, optimised_tokens=0, tokens_saved=0, cost_saved_usd=0,
                      groups_applied=[], pricing_tier="free").team == ""
    assert UsageMeter()._build_event(_ctx("payments"), {}).team == "payments"
    assert UsageMeter()._build_event(_ctx(), {}).team == "default"


def test_a_long_team_is_capped():
    assert len(UsageMeter()._build_event(_ctx("x" * 1000), {}).team) == 256


@pytest.mark.asyncio
async def test_the_row_stores_it():
    conn = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    await UsageMeter(db_pool=pool).record(_ctx("payments"), {})
    assert _insert(conn)["team"] == "payments"


def test_the_app_ddl_and_the_migration_add_it_additively():
    assert re.search(r"\n\s+team\s+TEXT\s+NOT NULL DEFAULT ''", USAGE_EVENTS_DDL)
    assert _COLUMN.search(USAGE_EVENTS_DDL)
    assert _COLUMN.search(_UP.read_text(encoding="utf-8"))


def test_the_down_script_drops_it():
    down = _DOWN.read_text(encoding="utf-8")
    assert "ALTER TABLE usage_events DROP COLUMN IF EXISTS team;" in down
