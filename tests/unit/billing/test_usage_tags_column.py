"""usage_events.tags (ST-2): the request's usage tags as a JSONB object.

Nullable with no default: NULL means the request carried no allow-listed header (and every
row written before the column). Additive and reversible like the team column.
"""
import json
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
_COLUMN = re.compile(r"ADD COLUMN IF NOT EXISTS tags JSONB;")


def _ctx(tags=None):
    savings = SavingsRecord(request_id="r1", user_id="u1", timestamp=datetime.now(timezone.utc),
                            model_requested="gpt-4o", routed_model="gpt-4o", baseline_tokens=10)
    ctx = RequestContext(request_id="r1", user_id="u1", original_messages=[], messages=[],
                         model="gpt-4o", routed_model="gpt-4o", params={}, config={"groups": {}},
                         savings=savings, tenant_id="acme")
    ctx.usage_tags = tags
    return ctx


async def _stored(ctx):
    conn = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    await UsageMeter(db_pool=pool).record(ctx, {})
    sql, *params = conn.execute.call_args[0]
    cols = [c.strip() for c in re.search(r"INSERT INTO usage_events\s*\((.*?)\)\s*VALUES",
                                         sql, re.S).group(1).split(",") if c.strip()]
    placeholders = re.search(r"VALUES\s*\((.*?)\)", sql, re.S).group(1).split(",")
    i = cols.index("tags")
    return params[i], placeholders[i].strip()


def test_the_context_starts_with_no_tags():
    savings = SavingsRecord(request_id="r", user_id="u", timestamp=datetime.now(timezone.utc),
                            model_requested="m", routed_model="m", baseline_tokens=0)
    ctx = RequestContext(request_id="r", user_id="u", original_messages=[], messages=[],
                         model="m", routed_model="m", params={}, config={}, savings=savings)
    assert ctx.usage_tags is None


def test_the_event_carries_the_tags():
    assert UsageEvent(tenant_id="t", request_id="r", timestamp=datetime.now(timezone.utc),
                      baseline_tokens=0, optimised_tokens=0, tokens_saved=0, cost_saved_usd=0,
                      groups_applied=[], pricing_tier="free").tags is None
    assert UsageMeter()._build_event(_ctx({"run_id": "r1"}), {}).tags == {"run_id": "r1"}
    assert UsageMeter()._build_event(_ctx(), {}).tags is None


@pytest.mark.asyncio
async def test_the_row_stores_them_as_jsonb():
    value, placeholder = await _stored(_ctx({"run_id": "r1", "step": "plan"}))
    assert json.loads(value) == {"run_id": "r1", "step": "plan"}
    assert placeholder.endswith("::jsonb")


@pytest.mark.asyncio
async def test_no_tags_store_null():
    value, _ = await _stored(_ctx())
    assert value is None


def test_the_app_ddl_and_the_migration_add_it_additively():
    assert re.search(r"\n\s+tags\s+JSONB,?\n", USAGE_EVENTS_DDL)
    assert _COLUMN.search(USAGE_EVENTS_DDL)
    assert _COLUMN.search(_UP.read_text(encoding="utf-8"))


def test_the_down_script_drops_it():
    assert "ALTER TABLE usage_events DROP COLUMN IF EXISTS tags;" in _DOWN.read_text(
        encoding="utf-8")
