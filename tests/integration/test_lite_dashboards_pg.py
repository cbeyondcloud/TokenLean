"""Real Postgres: every usage_events panel of the LITE dashboard set, with its team filter.

Each panel query is rendered the way Grafana renders it (template variables at their
defaults, `$__timeFilter` as a time range) and run against seeded usage_events rows of two
tenants and several teams, including a row written before the team column existed ('') and a
non-billable error row. The team filter must be exactly "only this team's rows":

    query(team = X) on every row  ==  query(team = All) on the rows of team X alone

for X in payments, search and default ('' rows count as default), for each tenant. The two
sides run on two schemas of the same database (search_path), so the query text is the same.
The team variable's own query lists the tenant's teams, with '' folded into default.

TEST_PG_DSN must point at a THROWAWAY server: the test creates and drops two schemas.
"""
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import pytest  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_PG_DSN"),
                                reason="set TEST_PG_DSN to a throwaway Postgres")

_DASHBOARDS = Path(__file__).resolve().parents[2] / "dashboard" / "dashboards"
WITH_TEAM = ("live.json", "requests.json", "billing.json", "sla.json", "trends.json",
             "quarterly.json", "per-call.json")
TENANTS = ("LITE-LOC-01", "OTHER-01")
_NOW = datetime.now(timezone.utc)
_FROM = (_NOW - timedelta(days=2)).isoformat()
_TO = (_NOW + timedelta(minutes=5)).isoformat()


def _panels(dashboard):
    for panel in dashboard.get("panels", []):
        yield panel
        yield from panel.get("panels", []) or []


def _queries():
    """(dashboard, panel title, refId, sql, variable defaults) per usage_events target."""
    out = []
    for fname in WITH_TEAM:
        d = json.loads((_DASHBOARDS / fname).read_text(encoding="utf-8"))
        defaults = {}
        for var in d.get("templating", {}).get("list", []):
            current = (var.get("current") or {}).get("value", "")
            if isinstance(current, list):
                current = current[0] if current else ""
            if current == "$__all":
                current = var.get("allValue", ".*")
            defaults[var["name"]] = str(current)
        for panel in _panels(d):
            for target in panel.get("targets", []) or []:
                sql = target.get("rawSql") or ""
                if "usage_events" in sql:
                    out.append((fname, panel.get("title"), target.get("refId"), sql, defaults))
    return out


def render(sql: str, values: dict) -> str:
    """Grafana's interpolation for these dashboards: ${var} / $var as the raw value (they are
    single-value variables, quoted by the query itself) and $__timeFilter(col)."""
    sql = re.sub(r"\$__timeFilter\((\w+)\)",
                 lambda m: f"{m.group(1)} BETWEEN '{_FROM}' AND '{_TO}'", sql)
    for name in sorted(values, key=len, reverse=True):
        sql = sql.replace("${" + name + "}", values[name])
        sql = re.sub(r"\$" + re.escape(name) + r"\b", lambda _m, v=values[name]: v, sql)
    leftover = re.findall(r"\$\{?\w+", sql)
    assert not leftover, f"unrendered {leftover} in: {sql}"
    return sql


def _seed_rows():
    """(tenant, team, request_id, ...) rows: several teams, a legacy '' row and an error row."""
    rows = []
    plan = {TENANTS[0]: {"payments": 3, "search": 2, "default": 2, "": 1},
            TENANTS[1]: {"payments": 2, "default": 1}}
    n = 0
    for tenant, teams in plan.items():
        for team, count in teams.items():
            for i in range(count + 1):              # + one non-billable error row
                n += 1
                billable = i < count
                rows.append({
                    "tenant_id": tenant, "team": team,
                    "request_id": f"req-{tenant}-{team or 'legacy'}-{i}",
                    "timestamp": _NOW - timedelta(minutes=7 * n),
                    "baseline_tokens": 1000 + 37 * n, "optimised_tokens": 900 + 31 * n,
                    "proxy_optimised_tokens": 900 + 31 * n, "tokens_saved": 100 + 6 * n,
                    "cost_saved_usd": Decimal("0.0001") * n, "cost_actual_usd": Decimal("0.001") * n,
                    "provider_prompt_tokens": 950 + n, "response_tokens": 40 + n,
                    "user_id": f"user-{n % 3}", "model": "gpt-4o-mini" if n % 2 else "claude-haiku-4-5",
                    "routed_model": "gpt-4o-mini" if n % 2 else "claude-haiku-4-5",
                    "cache_hit": n % 4 == 0, "cache_level": "L1" if n % 4 == 0 else "",
                    "complexity_tier": "simple", "bypassed": False,
                    "group_savings": json.dumps({"G05": 10 + n}), "status_code": 200 if billable else 500,
                    "billable": billable, "total_duration_ms": 300 + 11 * n, "llm_duration_ms": 250 + 9 * n,
                    "cache_read_tokens": 100 + n, "cache_write_tokens": n, "pricing_tier": "free",
                    "protocol": "openai", "groups_applied": ["G05", "G18"],
                })
    return rows


def _norm(team: str) -> str:
    return team or "default"


def _value(v):
    if isinstance(v, Decimal):
        return str(v.normalize())
    if isinstance(v, float):
        return round(v, 6)
    return v


async def _fetch(conn, schema, sql):
    await conn.execute(f"SET search_path TO {schema}")
    records = await conn.fetch(sql)
    return sorted((tuple(_value(v) for v in r.values()) for r in records), key=repr)


async def _exercise():
    import asyncpg
    from billing.models import USAGE_EVENTS_DDL
    rows = _seed_rows()
    cols = list(rows[0])
    insert = (f"INSERT INTO usage_events ({', '.join(cols)}) VALUES "
              f"({', '.join(f'${i + 1}' for i in range(len(cols)))})")
    conn = await asyncpg.connect(os.environ["TEST_PG_DSN"])
    schemas = {"full": rows}
    for tenant in TENANTS:
        for team in ("payments", "search", "default"):
            schemas[f"only_{tenant}_{team}"] = [
                r for r in rows if r["tenant_id"] == tenant and _norm(r["team"]) == team]
    names = {key: re.sub(r"[^a-z0-9_]", "_", f"lite_dash_{key}".lower()) for key in schemas}
    results = {}
    try:
        for key, subset in schemas.items():
            schema = names[key]
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            await conn.execute(f"CREATE SCHEMA {schema}")
            await conn.execute(f"SET search_path TO {schema}")
            await conn.execute(USAGE_EVENTS_DDL)
            for r in subset:
                await conn.execute(insert, *[r[c] for c in cols])
        queries = _queries()
        team_var = None
        for fname, title, ref, sql, defaults in queries:
            for tenant in TENANTS:
                req = next(r["request_id"] for r in rows
                           if r["tenant_id"] == tenant and r["team"] == "payments" and r["billable"])
                for team in ("payments", "search", "default"):
                    filtered = render(sql, {**defaults, "tenant": tenant, "team": team,
                                            "request_id": req})
                    alone = render(sql, {**defaults, "tenant": tenant, "team": ".*",
                                         "request_id": req})
                    got = await _fetch(conn, names["full"], filtered)
                    want = await _fetch(conn, names[f"only_{tenant}_{team}"], alone)
                    results[(fname, title, ref, tenant, team)] = (got, want)
        d = json.loads((_DASHBOARDS / "billing.json").read_text(encoding="utf-8"))
        var = next(v for v in d["templating"]["list"] if v["name"] == "team")
        team_var = {t: await _fetch(conn, names["full"], render(var["query"], {"tenant": t}))
                    for t in TENANTS}
        total = await _fetch(conn, names["full"], render(
            next(s for f, ti, _, s, _ in queries if f == "billing.json" and ti.startswith("Input Tokens")),
            {"tenant": TENANTS[0], "team": "payments"}))
        return results, team_var, total, rows
    finally:
        for schema in names.values():
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()


@pytest.fixture(scope="module")
def outcome():
    return asyncio.run(_exercise())


def test_every_panel_shows_exactly_the_chosen_team(outcome):
    results, _, _, _ = outcome
    assert len({k[:3] for k in results}) == 47          # every usage_events target in the set
    wrong = [k for k, (got, want) in results.items() if got != want]
    assert not wrong, wrong[:5]


def test_the_filter_is_not_vacuous(outcome):
    """Some panel differs between teams, so the comparison above has something to compare."""
    results, _, total, rows = outcome
    by_team = {}
    for (fname, title, ref, tenant, team), (got, _) in results.items():
        by_team.setdefault((fname, title, ref, tenant), {})[team] = got
    assert any(len({repr(v) for v in teams.values()}) > 1 for teams in by_team.values())
    expected = sum(r["baseline_tokens"] for r in rows if r["tenant_id"] == TENANTS[0]
                   and r["team"] == "payments" and r["billable"])
    assert total == [(expected,)]


def test_the_team_picker_lists_the_tenant_s_teams_with_legacy_rows_as_default(outcome):
    _, team_var, _, _ = outcome
    assert team_var[TENANTS[0]] == [("default",), ("payments",), ("search",)]
    assert team_var[TENANTS[1]] == [("default",), ("payments",)]
