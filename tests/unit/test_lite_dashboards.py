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
import yaml

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


# ── Provisioning: deploy/lite's optional Grafana (`up --grafana`) loads exactly the set ──


_LITE = _REPO / "deploy" / "lite"
_GRAFANA = _LITE / "grafana"
_PROV = _GRAFANA / "provisioning"
_LITE_DIR_IN_GRAFANA = "/etc/grafana/dashboards/lite/"
_LANGFUSE_DIR_IN_GRAFANA = "/etc/grafana/dashboards/langfuse/"


def _compose_service(name: str) -> dict:
    text = (_LITE / "local" / "docker-compose.lite.yml").read_text(encoding="utf-8")
    return yaml.safe_load(text)["services"][name]


def _mounts(service: str) -> list:
    """(source, target) of each bind mount, with the trailing :ro dropped. A source may hold
    a colon (`${VAR:-default}`); a target never does."""
    out = []
    for vol in _compose_service(service).get("volumes", []):
        for mode in (":ro", ":rw"):
            vol = vol[:-len(mode)] if vol.endswith(mode) else vol
        source, _, target = vol.rpartition(":")
        if source.startswith("."):
            out.append((source, target))
    return out


def _yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _datasource_uids(dashboard: dict) -> set:
    uids = {_uid(v) for v in variables(dashboard) if _uid(v)}
    for panel in panels(dashboard):
        uids |= {_uid(panel)} | {_uid(t) for t in panel.get("targets", []) or []}
    return {u for u in uids if u and not u.startswith("$") and u != "-- Grafana --"}


def test_grafana_mounts_exactly_the_lite_set():
    mounts = _mounts("grafana")
    lite = {t[len(_LITE_DIR_IN_GRAFANA):]: s for s, t in mounts
            if t.startswith(_LITE_DIR_IN_GRAFANA)}
    langfuse = {t[len(_LANGFUSE_DIR_IN_GRAFANA):]: s for s, t in mounts
                if t.startswith(_LANGFUSE_DIR_IN_GRAFANA)}
    assert sorted(lite) == sorted(LITE_SET)
    assert sorted(langfuse) == sorted(LANGFUSE_ONLY)
    for name, source in {**lite, **langfuse}.items():
        assert source == f"../../../dashboard/dashboards/{name}"   # the shipped files, as is
    assert not [t for _, t in mounts if any(n in t for n in NOT_IN_LITE)]


def test_dashboard_providers_load_the_lite_folder_and_per_call_only_with_langfuse():
    lite = _yaml(_PROV / "dashboards" / "lite.yaml")["providers"]
    assert [p["options"]["path"] for p in lite] == [_LITE_DIR_IN_GRAFANA.rstrip("/")]
    on = _yaml(_PROV / "dashboards" / "langfuse-on.yaml")["providers"]
    assert [p["options"]["path"] for p in on] == [_LANGFUSE_DIR_IN_GRAFANA.rstrip("/")]
    assert not _yaml(_PROV / "dashboards" / "langfuse-off.yaml").get("providers")
    names = [p["name"] for p in lite + on]
    assert len(set(names)) == len(names)
    for p in lite + on:
        assert p["type"] == "file" and p.get("allowUiUpdates") is False   # the repo is the source


def test_the_langfuse_files_are_chosen_by_one_variable():
    mounts = dict((t, s) for s, t in _mounts("grafana"))
    for kind in ("datasources", "dashboards"):
        source = mounts[f"/etc/grafana/provisioning/{kind}/langfuse.yaml"]
        assert source == f"../grafana/provisioning/{kind}/langfuse-${{LITE_GRAFANA_LANGFUSE:-off}}.yaml"
        for value in ("on", "off"):
            assert (_PROV / kind / f"langfuse-{value}.yaml").is_file()
    for script in ("up.sh", "up.ps1"):
        assert "LITE_GRAFANA_LANGFUSE" in (_LITE / "local" / script).read_text(encoding="utf-8")


def test_every_datasource_the_set_reads_is_provisioned():
    lite = {d["uid"] for d in _yaml(_PROV / "datasources" / "lite.yaml")["datasources"]}
    on = {d["uid"] for d in _yaml(_PROV / "datasources" / "langfuse-on.yaml")["datasources"]}
    assert not _yaml(_PROV / "datasources" / "langfuse-off.yaml").get("datasources")
    for fname in LITE_SET:
        assert _datasource_uids(load(fname)) <= lite, fname
    for fname in LANGFUSE_ONLY:
        assert _datasource_uids(load(fname)) <= lite | on, fname
    assert "langfuse-postgres" in on and "langfuse-postgres" not in lite


def test_datasources_take_their_credentials_from_the_environment():
    for name in ("lite.yaml", "langfuse-on.yaml"):
        for ds in _yaml(_PROV / "datasources" / name)["datasources"]:
            password = (ds.get("secureJsonData") or {}).get("password")
            if ds["type"] == "postgres":
                assert password and password.startswith("${"), (name, ds["uid"])
                assert ds["url"].startswith("${") and ds["user"].startswith("${")
            assert ds.get("editable") is False


def test_prometheus_scrapes_the_proxy_with_the_scrape_token_file():
    prom = _yaml(_GRAFANA / "prometheus.yml")
    jobs = prom["scrape_configs"]
    assert len(jobs) == 1
    job = jobs[0]
    assert job["static_configs"][0]["targets"] == ["proxy:8080"]
    auth = job["authorization"]
    assert auth.get("type", "Bearer") == "Bearer" and "credentials" not in auth
    target = dict((t, s) for s, t in _mounts("prometheus"))
    secrets_dir = next(t for t in target if target[t] == "./.state/prometheus")
    assert auth["credentials_file"].startswith(secrets_dir + "/")
