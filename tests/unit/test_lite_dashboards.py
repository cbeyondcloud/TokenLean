"""The LITE dashboard set (ST-11): which of the OSS dashboards LITE provisions, and their
`team` filter.

LITE provisions eight of the OSS dashboards (Live Calls, Requests Explorer, Billing, Latency
Breakup, SLA, Trust & Safety, Trends, Quarterly) and Per Call only when Langfuse is on (its
trace panels read the `langfuse-postgres` datasource). Tenant Overview is not provisioned:
an org runs LITE as one tenant with teams under it.

Every dashboard in the set that reads usage_events has a `team` variable listing
usage_events.team, and every panel query that reads usage_events honours it (Latency Breakup
and Trust & Safety read only Prometheus, so they offer no team picker). A row written before the column existed has team
'' and a request without a team has 'default'; both are shown as `default`.
"""
import json
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
DASHBOARD_DIR = _REPO / "dashboard" / "dashboards"

LITE_SET = ("live.json", "requests.json", "billing.json", "latency-breakup.json", "sla.json",
            "trust-safety.json", "trends.json", "quarterly.json")
LANGFUSE_ONLY = ("per-call.json",)
NOT_IN_LITE = ("tenant-overview.json",)
ALL_LITE = LITE_SET + LANGFUSE_ONLY
# Only Prometheus panels: a team picker there would filter nothing, so they have none.
PROMETHEUS_ONLY = ("latency-breakup.json", "trust-safety.json")
WITH_TEAM = tuple(f for f in ALL_LITE if f not in PROMETHEUS_ONLY)

TENANT_FILTER = "('${tenant}' = '.*' OR tenant_id = '${tenant}')"
TEAM_FILTER = "('${team}' = '.*' OR COALESCE(NULLIF(team, ''), 'default') = '${team}')"
TEAM_VALUE = "COALESCE(NULLIF(team, ''), 'default')"


def load(fname: str) -> dict:
    return json.loads((DASHBOARD_DIR / fname).read_text(encoding="utf-8"))


def _uid(obj: dict) -> str:
    ds = obj.get("datasource")
    return ds.get("uid", "") if isinstance(ds, dict) else str(ds or "")


def panels(dashboard: dict) -> list:
    """Every panel, rows' nested panels included."""
    out = []
    for panel in dashboard.get("panels", []):
        out.append(panel)
        out += panel.get("panels", []) or []
    return out


def usage_events_queries(dashboard: dict) -> list:
    """(panel title, refId, sql) for every panel target that reads usage_events."""
    out = []
    for panel in panels(dashboard):
        for target in panel.get("targets", []) or []:
            sql = target.get("rawSql") or ""
            if "usage_events" in sql:
                out.append((panel.get("title"), target.get("refId"), sql))
    return out


def variables(dashboard: dict) -> list:
    return dashboard.get("templating", {}).get("list", [])


def test_the_set_names_existing_dashboards_and_leaves_tenant_overview_out():
    on_disk = {p.name for p in DASHBOARD_DIR.glob("*.json")}
    assert set(ALL_LITE) <= on_disk
    assert not set(NOT_IN_LITE) & set(ALL_LITE)
    assert on_disk - set(ALL_LITE) == set(NOT_IN_LITE)   # a new dashboard needs a decision


@pytest.mark.parametrize("fname", PROMETHEUS_ONLY)
def test_prometheus_only_dashboards_read_no_usage_events_and_offer_no_team(fname):
    assert usage_events_queries(load(fname)) == []
    assert "team" not in [v.get("name") for v in variables(load(fname))]


@pytest.mark.parametrize("fname", WITH_TEAM)
def test_each_dashboard_has_a_team_variable_over_usage_events(fname):
    names = [v.get("name") for v in variables(load(fname))]
    assert "team" in names, f"{fname}: no team variable"
    assert names.index("team") == names.index("tenant") + 1   # next to the tenant picker
    var = variables(load(fname))[names.index("team")]
    assert var["type"] == "query" and _uid(var) == "app-postgres"
    query = var["query"]
    assert "FROM usage_events" in query and TEAM_VALUE in query
    assert TENANT_FILTER in query                     # lists the chosen tenant's teams
    assert var["includeAll"] is True and var["allValue"] == ".*"
    assert var.get("multi") is False                  # the panels compare one value
    assert var["current"] == {"value": "$__all", "text": "All"}
    assert var.get("refresh") == 2                    # re-listed when the time range changes
    assert "usage_events" in var.get("description", "")


@pytest.mark.parametrize("fname", WITH_TEAM)
def test_every_usage_events_panel_honours_the_team(fname):
    queries = usage_events_queries(load(fname))
    assert queries, f"{fname}: no usage_events panel"
    for title, ref, sql in queries:
        assert TEAM_FILTER in sql, f"{fname}: {title!r} ({ref}) ignores the team"
        assert TENANT_FILTER in sql, f"{fname}: {title!r} ({ref}) ignores the tenant"


def test_the_full_stack_dashboards_keep_working_without_a_team_choice():
    """The same files are the full stack's dashboards: All (the default) filters nothing."""
    for fname in WITH_TEAM:
        for var in variables(load(fname)):
            if var.get("name") == "team":
                assert var["current"]["value"] == "$__all"
