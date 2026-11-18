"""The gateway columns agentic-sdlc's control plane reads by name.

agentic's dashboards link TokenLean's ``usage_events`` and ``audit_events`` over postgres_fdw
and select these columns by name (agentic-sdlc
``control-plane/src/agentic_cp/dashboards/views.py`` ``USAGE_COLUMNS`` /
``GATEWAY_AUDIT_COLUMNS``). Renaming or dropping one here does not fail anything in this
repo; it silently turns a customer's dashboard panel into "No data". Change this list only
together with that file.
"""
import re
from pathlib import Path

from audit.log import AUDIT_EVENTS_DDL
from billing.models import USAGE_EVENTS_DDL

_METERING = Path(__file__).resolve().parents[3] / "src" / "proxy" / "billing" / "metering.py"

USAGE_COLUMNS_READ = (
    "request_id", "tenant_id", "timestamp", "provider", "model", "routed_model", "team",
    "tags", "provider_prompt_tokens", "response_tokens", "cache_read_tokens",
    "cache_write_tokens", "cost_actual_usd", "status_code", "total_duration_ms",
    # savings, read for the Org / Team / Executive ROI savings panels
    "baseline_tokens", "tokens_saved", "cost_baseline_usd", "cost_saved_usd",
)
AUDIT_COLUMNS_READ = ("request_id", "tenant_id", "timestamp", "action", "details")


def _declared(ddl: str, column: str) -> bool:
    return re.search(rf"(^|\(|,|ADD COLUMN IF NOT EXISTS)\s*{column}\s+[A-Z]", ddl, re.M) is not None


def test_every_usage_column_agentic_reads_is_declared():
    missing = [c for c in USAGE_COLUMNS_READ if not _declared(USAGE_EVENTS_DDL, c)]
    assert missing == []


def test_every_usage_column_agentic_reads_is_written():
    insert = re.search(r"INSERT INTO usage_events\s*\((.*?)\)", _METERING.read_text(), re.S)
    written = {c.strip() for c in insert.group(1).split(",")}
    assert [c for c in USAGE_COLUMNS_READ if c not in written] == []


def test_every_audit_column_agentic_reads_is_declared():
    missing = [c for c in AUDIT_COLUMNS_READ if not _declared(AUDIT_EVENTS_DDL, c)]
    assert missing == []
