"""/admin/routing (ST-10): routing rules and team limits over HTTP — validated, versioned,
hot-applied, audited.

The store is an in-memory stand-in here (tests/integration/test_admin_routing_pg.py runs the
real Postgres one). An invalid fragment is refused with every problem's path and nothing is
stored or applied; a valid one becomes the next version and the running config at once; a
rollback stores an earlier version's fragment as a new version; a stale expected_version is a
409. Only admin keys without a team binding may read or change it.
"""
import copy
import hashlib
import json

import pytest
from httpx import ASGITransport, AsyncClient

import config_loader
import main
from admin_api import routing
from auth import api_key_manager as akm

ROOT, SRE, PLAIN = "k-root", "k-sre", "k-plain"
_KEYS = {
    hashlib.sha256(ROOT.encode()).hexdigest(): {"tenant_id": "ops", "admin": True},
    hashlib.sha256(SRE.encode()).hexdigest(): {"tenant_id": "acme", "admin": True,
                                               "team": "payments"},
    hashlib.sha256(PLAIN.encode()).hexdigest(): {"tenant_id": "acme"},
}
BASE = {
    "providers": [{"name": "openai", "models": ["gpt-4o-mini", "gpt-4o"],
                   "model_prefixes": ["gpt-"]}],
    "groups": {"G6_routing": {"enabled": True, "tiers": {"simple": ["gpt-4o-mini"],
                                                         "medium": ["gpt-4o"]}},
               "G30_guardrails": {"enabled": True, "mode": "block"}},
    "rate_limit": {"enabled": True, "per_team": {}},
}
V1 = {"groups": {"G6_routing": {"tiers": {"simple": ["gpt-4o"]}}},
      "teams": {"payments": {"monthly_spend_cap_usd": 50}}}
V2 = {"tenants": {"ACME-PRD-01": {"groups": {"G6_routing": {"strategy": "round_robin"}}}},
      "rate_limit": {"per_team": {"payments": {"requests_per_minute": 20}}}}


class _Store:
    """In-memory stand-in for the Postgres store functions of admin_api.routing."""

    def __init__(self):
        self.rows = []

    async def current_version(self, pool):
        return self.rows[-1]["version"] if self.rows else 0

    async def current(self, pool):
        return copy.deepcopy(self.rows[-1]) if self.rows else None

    async def get_version(self, pool, version):
        return next((copy.deepcopy(r) for r in self.rows if r["version"] == version), None)

    async def history(self, pool, limit, before=None):
        rows = [r for r in reversed(self.rows) if before is None or r["version"] < before]
        return copy.deepcopy(rows[:limit])

    async def append(self, pool, fragment, *, action, actor, comment="", expected_version=None,
                     source_version=None):
        latest = await self.current_version(pool)
        if expected_version is not None and expected_version != latest:
            raise routing.VersionConflict(latest)
        row = {"version": latest + 1, "fragment": copy.deepcopy(fragment), "action": action,
               "actor": actor, "created_at": f"2026-10-23T00:00:0{latest + 1}+00:00",
               "comment": comment, "source_version": source_version,
               "fragment_sha256": routing.fragment_sha256(fragment)}
        self.rows.append(row)
        return copy.deepcopy(row)


class _Audit:
    def __init__(self):
        self.rows = []

    async def log_config_change(self, **row):
        self.rows.append(row)
        return True


@pytest.fixture
def env(monkeypatch):
    store, audit = _Store(), _Audit()
    for name in ("current_version", "current", "get_version", "history", "append"):
        monkeypatch.setattr(routing, name, getattr(store, name))
    akm.replace_cache(dict(_KEYS))
    base = copy.deepcopy(BASE)
    monkeypatch.setattr(config_loader, "_config", base)
    monkeypatch.setattr(config_loader, "_base_config", base)
    monkeypatch.setattr(config_loader, "_runtime_overlay", {})
    monkeypatch.setattr(main, "_db_pool", object())
    monkeypatch.setattr(main, "_audit_logger", audit)
    monkeypatch.setattr(routing, "_applied", {"version": 0, "sha256": None})
    monkeypatch.setattr(routing, "_sync_state", {"failing": False, "refused": None})
    yield store, audit, base
    config_loader.set_runtime_overlay({})


async def _call(method, path, key=ROOT, body=None):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with AsyncClient(transport=ASGITransport(app=main.app),
                           base_url="http://test") as client:
        return await client.request(method, path, headers=headers,
                                    content=None if body is None else json.dumps(body))


async def _put(fragment, **extra):
    return await _call("PUT", "/admin/routing", body={"fragment": fragment, **extra})


@pytest.mark.parametrize("method,path", [
    ("GET", "/admin/routing"), ("PUT", "/admin/routing"), ("GET", "/admin/routing/history"),
    ("GET", "/admin/routing/versions/1"), ("POST", "/admin/routing/rollback"),
])
async def test_only_unbound_admin_keys_may_call_it(env, method, path):
    store, audit, _ = env
    body = {"fragment": V1, "version": 0}
    assert (await _call(method, path, None, body)).status_code == 401
    assert (await _call(method, path, PLAIN, body)).status_code == 403
    assert (await _call(method, path, SRE, body)).status_code == 403
    assert store.rows == [] and audit.rows == []


async def test_nothing_stored_means_version_0_and_the_loaded_config(env):
    _store, _audit, base = env
    r = await _call("GET", "/admin/routing")
    assert r.status_code == 200
    assert r.json()["version"] == 0 and r.json()["fragment"] == {}
    assert r.json()["replica_version"] == 0
    assert config_loader.get_config() is base


async def test_a_valid_fragment_is_stored_applied_at_once_and_audited(env):
    store, audit, base = env
    before = config_loader.get_config()
    r = await _put(V1, comment="tier map from the control plane")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 1 and body["action"] == "applied"
    assert body["applied_by"].startswith("admin-key:ops:key=") and body["applied_at"]
    assert body["replica_version"] == 1
    cfg = config_loader.get_config()
    assert cfg["groups"]["G6_routing"]["tiers"] == {"simple": ["gpt-4o"], "medium": ["gpt-4o"]}
    assert cfg["teams"]["payments"]["monthly_spend_cap_usd"] == 50
    assert cfg["groups"]["G30_guardrails"] == BASE["groups"]["G30_guardrails"]
    assert before is base and "teams" not in before        # the old object is untouched
    [row] = audit.rows
    assert row["action"] == "routing_overlay.applied" and row["tenant_id"] == "ops"
    assert row["details"]["version"] == 1 and row["details"]["previous_version"] == 0
    assert row["details"]["teams"] == ["payments"]
    assert row["details"]["fragment_sha256"] == routing.fragment_sha256(V1)
    assert "gpt-4o" not in json.dumps(row["details"])      # paths, never values


async def test_an_invalid_fragment_is_refused_precisely_and_nothing_is_applied(env):
    store, audit, base = env
    r = await _put({"groups": {"G30_guardrails": {"mode": "allow"},
                               "G6_routing": {"tiers": {"simple": ["llama-nope"]}}}})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "invalid_fragment"
    paths = {e["path"] for e in detail["errors"]}
    assert paths == {"groups.G30_guardrails", "groups.G6_routing.tiers.simple[0]"}
    assert store.rows == [] and audit.rows == []
    assert config_loader.get_config() is base


@pytest.mark.parametrize("body,field", [
    ({}, "fragment"), ({"fragment": {}, "colour": 1}, "colour"),
    ({"fragment": {}, "expected_version": -1}, "expected_version"),
    ({"fragment": {}, "comment": "x" * 501}, "comment"),
])
async def test_a_bad_request_body_is_refused(env, body, field):
    r = await _call("PUT", "/admin/routing", body=body)
    assert r.status_code == 422 and field in json.dumps(r.json()["detail"])


async def test_a_stale_expected_version_is_a_conflict(env):
    store, _audit, _ = env
    assert (await _put(V1, expected_version=0)).status_code == 200
    r = await _put(V2, expected_version=0)
    assert r.status_code == 409
    assert r.json()["detail"]["current_version"] == 1
    assert len(store.rows) == 1
    assert (await _put(V2, expected_version=1)).status_code == 200


async def test_history_and_versions_keep_who_and_when(env):
    await _put(V1, comment="first")
    await _put(V2, comment="second")
    r = await _call("GET", "/admin/routing/history?limit=1")
    assert r.status_code == 200
    [latest] = r.json()["versions"]
    assert latest["version"] == 2 and latest["comment"] == "second"
    assert r.json()["next_before"] == 2
    r = await _call("GET", "/admin/routing/history?before=2")
    assert [v["version"] for v in r.json()["versions"]] == [1]
    assert r.json()["next_before"] is None
    one = (await _call("GET", "/admin/routing/versions/1")).json()
    assert one["fragment"] == V1 and one["applied_by"].startswith("admin-key:ops")
    assert (await _call("GET", "/admin/routing/versions/9")).status_code == 404
    assert (await _call("GET", "/admin/routing/history?limit=0")).status_code == 422


async def test_rollback_stores_an_earlier_fragment_as_a_new_version(env):
    store, audit, base = env
    await _put(V1)
    await _put(V2)
    r = await _call("POST", "/admin/routing/rollback", body={"version": 1, "comment": "undo"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["version"], body["action"], body["source_version"]) == (3, "rolled_back", 1)
    assert body["fragment"] == V1
    cfg = config_loader.get_config()
    assert "tenants" not in cfg and cfg["teams"]["payments"]["monthly_spend_cap_usd"] == 50
    assert audit.rows[-1]["action"] == "routing_overlay.rolled_back"
    assert audit.rows[-1]["details"]["source_version"] == 1
    assert [row["version"] for row in store.rows] == [1, 2, 3]     # nothing overwritten


async def test_rollback_to_0_clears_the_overlay(env):
    _store, _audit, base = env
    await _put(V1)
    r = await _call("POST", "/admin/routing/rollback", body={"version": 0})
    assert r.status_code == 200 and r.json()["fragment"] == {}
    assert config_loader.get_config() is base


async def test_a_rollback_is_validated_against_today_s_config(env):
    store, _audit, base = env
    await _put(V1)
    base["providers"][0]["models"] = ["gpt-4o-mini"]
    base["providers"][0]["model_prefixes"] = []                  # gpt-4o is gone
    await _call("POST", "/admin/routing/rollback", body={"version": 0})
    r = await _call("POST", "/admin/routing/rollback", body={"version": 1})
    assert r.status_code == 422
    assert r.json()["detail"]["errors"][0]["path"] == "groups.G6_routing.tiers.simple[0]"
    assert len(store.rows) == 2
    assert (await _call("POST", "/admin/routing/rollback", body={"version": 7})).status_code == 404
    assert (await _call("POST", "/admin/routing/rollback", body={})).status_code == 422


async def test_without_the_database_or_the_audit_log_it_answers_503(env, monkeypatch):
    store, _audit, _ = env
    monkeypatch.setattr(main, "_audit_logger", None)
    r = await _put(V1)
    assert r.status_code == 503 and "audit" in r.json()["detail"].lower()
    monkeypatch.setattr(main, "_db_pool", None)
    assert (await _call("GET", "/admin/routing")).status_code == 503
    assert (await _put(V1)).status_code == 503
    assert store.rows == []


async def test_what_is_applied_is_what_the_request_path_reads(env):
    from middleware.g00_rate_limit import G00RateLimit
    from tenancy.teams import apply_team_overlay, team_monthly_spend_cap
    fragment = {
        "rate_limit": {"per_team": {"payments": {"requests_per_minute": 7}}},
        "tenants": {"ACME-PRD-01": {"teams": {"payments": {
            "monthly_spend_cap_usd": 25,
            "config": {"groups": {"G6_routing": {"tiers": {"simple": ["gpt-4o"]}}}}}}}},
    }
    assert (await _put(fragment)).status_code == 200
    cfg = config_loader.get_config()
    assert team_monthly_spend_cap(cfg, "ACME-PRD-01", "payments") == 25
    assert team_monthly_spend_cap(cfg, "OTHER-PRD-01", "payments") is None
    team_cfg, overlay = apply_team_overlay(cfg, "ACME-PRD-01", "payments")
    assert team_cfg["groups"]["G6_routing"]["tiers"]["simple"] == ["gpt-4o"]
    g00 = G00RateLimit()
    g00._load_config(cfg)
    limits = g00._get_limits_for_scope("u", "payments", "ACME-PRD-01", rl_cfg=cfg["rate_limit"])
    assert limits["requests_per_minute"] == 7


@pytest.mark.parametrize("path", ["/admin/routing/versions/%C2%B2",
                                  "/admin/routing/versions/99999999999999999999999"])
async def test_an_odd_version_in_the_path_is_not_found(env, path):
    assert (await _call("GET", path)).status_code == 404


async def test_out_of_range_numbers_are_refused(env):
    r = await _put(V1, expected_version=2 ** 63)
    assert r.status_code == 422
    r = await _call("GET", "/admin/routing/history?before=99999999999999999999")
    assert r.status_code == 422



async def test_a_huge_integer_over_http_is_422_and_nothing_is_stored(env):
    store, audit, _ = env
    async with AsyncClient(transport=ASGITransport(app=main.app),
                           base_url="http://test") as client:
        r = await client.put("/admin/routing", headers={"Authorization": f"Bearer {ROOT}"},
                             content='{"fragment": {"groups": {"G6_routing": {"canary_pct": 1'
                                     + "0" * 400 + '}}}}')
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["errors"][0]["path"] == "groups.G6_routing.canary_pct"
    assert store.rows == [] and audit.rows == []


async def test_a_nul_character_over_http_is_422(env):
    store, _audit, _ = env
    r = await _put({"groups": {"G6_routing": {"tiers": {"simple": ["gpt-4o\u0000"]}}}})
    assert r.status_code == 422
    r = await _put(V1, comment="bad\u0000comment")
    assert r.status_code == 422
    assert store.rows == []


class _Conn:
    """Records the SQL routing.append runs; MAX(version) answers ``latest``."""

    def __init__(self, latest):
        self.latest, self.sql = latest, []

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return _Tx()

    async def execute(self, sql, *args):
        self.sql.append(sql)

    async def fetchval(self, sql, *args):
        self.sql.append(sql)
        return self.latest

    async def fetchrow(self, sql, *args):
        self.sql.append(sql)
        return {"version": args[0], "fragment": args[1], "action": args[2], "actor": args[3],
                "created_at": None, "comment": args[4], "source_version": args[5],
                "fragment_sha256": args[6]}


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acq:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return _Acq()


async def test_append_locks_checks_the_expected_version_and_inserts_the_next():
    conn = _Conn(latest=4)
    with pytest.raises(routing.VersionConflict) as exc:
        await routing.append(_Pool(conn), V1, action="applied", actor="a", expected_version=3)
    assert exc.value.current == 4
    assert any("pg_advisory_xact_lock" in s for s in conn.sql)
    assert not any(s.startswith("INSERT") for s in conn.sql)        # nothing written
    conn = _Conn(latest=4)
    row = await routing.append(_Pool(conn), V1, action="applied", actor="a", expected_version=4)
    assert row["version"] == 5 and row["fragment"] == V1
    assert row["fragment_sha256"] == routing.fragment_sha256(V1)
