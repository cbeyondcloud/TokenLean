"""A real Grafana provisioned from deploy/lite/grafana loads the LITE set, and its Postgres
datasource answers a team-filtered panel query from seeded usage_events rows.

Run against a throwaway Grafana started with the mounts and environment the LITE compose's
`grafana` service uses (deploy/lite/local/docker-compose.lite.yml), whose app-postgres
datasource reaches the database TEST_PG_DSN names:

    TEST_GRAFANA_URL=http://127.0.0.1:3000  TEST_GRAFANA_PASSWORD=<admin password>
    TEST_PG_DSN=postgresql://...            TEST_GRAFANA_LANGFUSE=on|off (default off)

Grafana interpolates template variables in the browser, so the test renders the panel the
way tests/integration/test_lite_dashboards_pg.py does and sends the SQL through
/api/ds/query: what is proven here is that the provisioned set and datasource work.
The test replaces usage_events in the database's public schema.
"""
import asyncio
import base64
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))
sys.path.insert(0, os.path.dirname(__file__))

import pytest  # noqa: E402

pytestmark = pytest.mark.skipif(
    not (os.environ.get("TEST_GRAFANA_URL") and os.environ.get("TEST_GRAFANA_PASSWORD")
         and os.environ.get("TEST_PG_DSN")),
    reason="set TEST_GRAFANA_URL, TEST_GRAFANA_PASSWORD and TEST_PG_DSN (a throwaway Grafana "
           "provisioned from deploy/lite/grafana, and its database)")

_DASHBOARDS = Path(__file__).resolve().parents[2] / "dashboard" / "dashboards"
LITE_SET = ("live.json", "requests.json", "billing.json", "latency-breakup.json", "sla.json",
            "trust-safety.json", "trends.json", "quarterly.json")


def _api(path, body=None):
    url = os.environ["TEST_GRAFANA_URL"].rstrip("/") + path
    auth = base64.b64encode(f"admin:{os.environ['TEST_GRAFANA_PASSWORD']}".encode()).decode()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Basic {auth}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _uid(fname):
    return json.loads((_DASHBOARDS / fname).read_text(encoding="utf-8"))["uid"]


def test_grafana_loads_exactly_the_lite_set():
    expected = {_uid(f) for f in LITE_SET}
    if os.environ.get("TEST_GRAFANA_LANGFUSE", "off") == "on":
        expected.add(_uid("per-call.json"))
    found = _api("/api/search?type=dash-db&limit=5000")
    assert {d["uid"] for d in found} == expected
    assert {d.get("folderTitle") for d in found} == {"TokenLean LITE"}
    assert _uid("tenant-overview.json") not in {d["uid"] for d in found}


def test_the_provisioned_dashboards_carry_the_team_picker():
    for fname in ("billing.json", "live.json", "trends.json"):
        dash = _api(f"/api/dashboards/uid/{_uid(fname)}")["dashboard"]
        names = [v["name"] for v in dash["templating"]["list"]]
        assert "team" in names, fname


async def _seed():
    import asyncpg
    from billing.models import USAGE_EVENTS_DDL
    from test_lite_dashboards_pg import _seed_rows
    rows = _seed_rows()
    cols = list(rows[0])
    insert = (f"INSERT INTO usage_events ({', '.join(cols)}) VALUES "
              f"({', '.join(f'${i + 1}' for i in range(len(cols)))})")
    conn = await asyncpg.connect(os.environ["TEST_PG_DSN"])
    try:
        await conn.execute("DROP TABLE IF EXISTS public.usage_events")
        await conn.execute("SET search_path TO public")
        await conn.execute(USAGE_EVENTS_DDL)
        for r in rows:
            await conn.execute(insert, *[r[c] for c in cols])
    finally:
        await conn.close()
    return rows


def test_the_datasource_answers_a_team_filtered_panel():
    from test_lite_dashboards_pg import TENANTS, _queries, render
    rows = asyncio.run(_seed())
    sql = next(s for f, title, _, s, _ in _queries()
               if f == "billing.json" and title.startswith("Input Tokens"))
    expected = {}
    for team in ("payments", "search", "default"):
        expected[team] = sum(r["baseline_tokens"] for r in rows if r["tenant_id"] == TENANTS[0]
                             and (r["team"] or "default") == team and r["billable"])
        body = {"from": "now-7d", "to": "now", "queries": [{
            "refId": "A", "datasource": {"type": "postgres", "uid": "app-postgres"},
            "rawSql": render(sql, {"tenant": TENANTS[0], "team": team}), "format": "table"}]}
        frame = _api("/api/ds/query", body)["results"]["A"]["frames"][0]
        assert frame["data"]["values"] == [[expected[team]]], team
    assert len(set(expected.values())) == 3
