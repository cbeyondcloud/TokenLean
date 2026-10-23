"""/admin/keys (ST-9): create, list, revoke and rotate proxy keys over HTTP.

Admin keys only, as the other /admin endpoints. An admin key bound to a team manages only
its own team's keys of its own tenant (owner decision 82's scoping, applied to keys): its
listing is that team's whatever the query says, and a write naming another tenant or team,
or asking for a trust flag, is refused with nothing written. The raw key is in the response
exactly once (create, rotate); the store holds only its hash, and a listing shows neither.
Every change is an audit_events row; with no audit log wired, changes are refused.
"""
import hashlib
import json
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

import main
from auth import api_key_manager as akm

ROOT, SRE, PLAIN, SRE_FREE = "k-root", "k-sre", "k-plain", "k-sre-free"
_SEED = {
    hashlib.sha256(SRE_FREE.encode()).hexdigest(): {"tenant_id": "acme", "tier": "free",
                                                    "admin": True, "team": "search"},
    hashlib.sha256(ROOT.encode()).hexdigest(): {"tenant_id": "ops", "tier": "enterprise",
                                                "admin": True},
    hashlib.sha256(SRE.encode()).hexdigest(): {"tenant_id": "acme", "tier": "enterprise",
                                               "admin": True, "team": "payments"},
    hashlib.sha256(PLAIN.encode()).hexdigest(): {"tenant_id": "acme", "tier": "free"},
}


class _Audit:
    def __init__(self, ok=True):
        self.rows, self.ok = [], ok

    async def log_config_change(self, **row):
        self.rows.append(row)
        return self.ok


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = tmp_path / "local-keys.json"
    store.write_text(json.dumps(_SEED), encoding="utf-8")
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setattr(akm, "_LOCAL_PROXY_KEYS_FILE", str(store))
    akm.reset_key_store_backend()
    audit = _Audit()
    monkeypatch.setattr(main, "_audit_logger", audit)
    cfg = {}
    monkeypatch.setattr(main, "get_config", lambda: cfg)
    yield store, audit, cfg
    akm.reset_key_store_backend()


async def _call(method, path, key=ROOT, body=None):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with AsyncClient(transport=ASGITransport(app=main.app),
                           base_url="http://test") as client:
        return await client.request(method, path, headers=headers,
                                    content=None if body is None else json.dumps(body))


def _stored(store):
    return json.loads(store.read_text(encoding="utf-8"))


async def _create(body, key=ROOT):
    return await _call("POST", "/admin/keys", key, body)


# ── auth ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("method,path", [
    ("POST", "/admin/keys"), ("GET", "/admin/keys"),
    ("DELETE", "/admin/keys/0123456789abcdef"), ("POST", "/admin/keys/0123456789abcdef/rotate"),
])
async def test_only_admin_keys_may_call_it(env, method, path):
    store, audit, _ = env
    assert (await _call(method, path, PLAIN, {"tenant_id": "acme"})).status_code == 403
    assert (await _call(method, path, None, {"tenant_id": "acme"})).status_code == 401
    assert _stored(store) == _SEED and audit.rows == []


# ── create ──────────────────────────────────────────────────────────────────

async def test_create_shows_the_raw_key_once_and_stores_only_its_hash(env):
    store, audit, _ = env
    r = await _create({"tenant_id": "acme", "team": "payments", "user": "alice@acme.test",
                       "tier": "enterprise", "label": "laptop"})
    assert r.status_code == 201, r.text
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    raw = body["key"]
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    assert body["key_id"] == key_hash[:16]
    assert (body["tenant_id"], body["team"], body["user"], body["tier"], body["label"]) == (
        "acme", "payments", "alice@acme.test", "enterprise", "laptop")
    assert raw not in store.read_text(encoding="utf-8")
    assert _stored(store)[key_hash]["user"] == "alice@acme.test"
    ok, tenant, meta = akm.validate_proxy_key(raw)
    assert ok and tenant == "acme"
    assert akm.key_team(meta) == "payments" and akm.key_user(meta) == "alice@acme.test"
    [row] = audit.rows
    assert row["action"] == "proxy_key.created" and row["tenant_id"] == "acme"
    assert row["actor"].startswith("admin-key:ops:key=")
    assert raw not in json.dumps(row) and key_hash not in json.dumps(row)
    assert row["details"]["key_id"] == key_hash[:16]


@pytest.mark.parametrize("body,field", [
    ({}, "tenant_id"),
    ({"tenant_id": "acme", "team": "has space"}, "team"),
    ({"tenant_id": "acme", "team": "default"}, "team"),
    ({"tenant_id": "acme", "user": "a b"}, "user"),
    ({"tenant_id": "acme", "tier": "gold"}, "tier"),
    ({"tenant_id": "acme", "label": "x" * 129}, "label"),
    ({"tenant_id": "acme", "admin": "yes"}, "admin"),
    ({"tenant_id": "acme", "colour": "blue"}, "colour"),
    ({"tenant_id": "bad tenant"}, "tenant_id"),
])
async def test_an_invalid_create_is_refused_naming_the_field(env, body, field):
    store, audit, _ = env
    r = await _create(body)
    assert r.status_code == 422, r.text
    assert field in json.dumps(r.json()["detail"])
    assert _stored(store) == _SEED and audit.rows == []


async def test_a_body_that_is_not_an_object_is_refused(env):
    r = await _call("POST", "/admin/keys", ROOT, ["tenant_id", "acme"])
    assert r.status_code == 422


async def test_a_team_admin_creates_keys_for_its_own_team_only(env):
    store, audit, _ = env
    r = await _create({"user": "bob@acme.test"}, key=SRE)
    assert r.status_code == 201, r.text
    assert (r.json()["tenant_id"], r.json()["team"]) == ("acme", "payments")
    assert audit.rows[0]["actor"].startswith("admin-key:acme:team=payments:key=")
    before = _stored(store)
    for body in ({"tenant_id": "other"}, {"team": "search"}, {"admin": True},
                 {"gateway": True}):
        assert (await _create(body, key=SRE)).status_code == 403, body
    assert _stored(store) == before and len(audit.rows) == 1


# ── list ────────────────────────────────────────────────────────────────────

async def test_list_never_shows_a_raw_key_or_a_hash(env):
    store, _audit, _ = env
    raw = (await _create({"tenant_id": "acme", "team": "payments"})).json()["key"]
    r = await _call("GET", "/admin/keys")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == len(body["keys"]) == 5
    text = r.text
    assert raw not in text
    for key_hash in _stored(store):
        assert key_hash not in text


async def test_list_filters_and_a_team_admin_sees_only_its_team(env):
    await _create({"tenant_id": "acme", "team": "payments", "user": "alice@acme.test"})
    await _create({"tenant_id": "acme", "team": "search"})
    await _create({"tenant_id": "other", "team": "payments"})
    r = await _call("GET", "/admin/keys?tenant_id=acme&team=payments")
    assert {(k["tenant_id"], k["team"]) for k in r.json()["keys"]} == {("acme", "payments")}
    assert len(r.json()["keys"]) == 2                      # SRE's own key + alice's
    r = await _call("GET", "/admin/keys?user=alice@acme.test")
    assert [k["user"] for k in r.json()["keys"]] == ["alice@acme.test"]
    r = await _call("GET", "/admin/keys?tenant_id=other&team=search", key=SRE)
    assert r.status_code == 200
    assert {(k["tenant_id"], k["team"]) for k in r.json()["keys"]} == {("acme", "payments")}


# ── revoke ──────────────────────────────────────────────────────────────────

async def test_revoke_rejects_the_key_at_once_and_is_audited(env):
    _store, audit, _ = env
    created = (await _create({"tenant_id": "acme", "team": "payments"})).json()
    assert akm.validate_proxy_key(created["key"])[0] is True
    r = await _call("DELETE", f"/admin/keys/{created['key_id']}")
    assert r.status_code == 200, r.text
    assert r.json()["revoked"] is True and r.json()["key_id"] == created["key_id"]
    assert akm.validate_proxy_key(created["key"])[0] is False
    assert audit.rows[-1]["action"] == "proxy_key.revoked"
    assert audit.rows[-1]["tenant_id"] == "acme"
    details = audit.rows[-1]["details"]
    assert (details["key_id"], details["tenant_id"], details["team"]) == (
        created["key_id"], "acme", "payments")
    assert created["key"] not in json.dumps(audit.rows[-1])
    assert (await _call("DELETE", f"/admin/keys/{created['key_id']}")).status_code == 404


async def test_a_team_admin_cannot_revoke_another_team_s_key(env):
    _store, audit, _ = env
    other = (await _create({"tenant_id": "acme", "team": "search"})).json()
    rows = len(audit.rows)
    r = await _call("DELETE", f"/admin/keys/{other['key_id']}", key=SRE)
    assert r.status_code == 404
    assert akm.validate_proxy_key(other["key"])[0] is True
    assert len(audit.rows) == rows


@pytest.mark.parametrize("bad", ["0123", "XYZ0123456789abc", "0123456789abcdef" * 5])
async def test_a_malformed_key_id_is_not_found(env, bad):
    assert (await _call("DELETE", f"/admin/keys/{bad}")).status_code == 404


# ── rotate ──────────────────────────────────────────────────────────────────

async def test_rotate_issues_a_new_key_once_with_the_same_binding(env):
    store, audit, _ = env
    old = (await _create({"tenant_id": "acme", "team": "payments",
                          "user": "alice@acme.test"})).json()
    r = await _call("POST", f"/admin/keys/{old['key_id']}/rotate", key=SRE)
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    new = r.json()
    assert new["replaced_key_id"] == old["key_id"] and new["key"] != old["key"]
    assert (new["tenant_id"], new["team"], new["user"]) == ("acme", "payments",
                                                            "alice@acme.test")
    assert akm.validate_proxy_key(old["key"])[0] is False
    assert akm.validate_proxy_key(new["key"])[0] is True
    assert new["key"] not in store.read_text(encoding="utf-8")
    row = audit.rows[-1]
    assert row["action"] == "proxy_key.rotated"
    assert row["details"]["replaced_key_id"] == old["key_id"]
    assert new["key"] not in json.dumps(row)


async def test_a_team_admin_cannot_rotate_another_team_s_key(env):
    other = (await _create({"tenant_id": "acme", "team": "search"})).json()
    r = await _call("POST", f"/admin/keys/{other['key_id']}/rotate", key=SRE)
    assert r.status_code == 404
    assert akm.validate_proxy_key(other["key"])[0] is True


# ── audit and store failures ────────────────────────────────────────────────

async def test_with_no_audit_log_changes_are_refused(env, monkeypatch):
    store, _audit, cfg = env
    monkeypatch.setattr(main, "_audit_logger", None)
    r = await _create({"tenant_id": "acme"})
    assert r.status_code == 503 and "audit" in r.json()["detail"].lower()
    assert _stored(store) == _SEED
    assert (await _call("GET", "/admin/keys")).status_code == 200    # reads still work
    cfg["admin_api"] = {"require_audit": False}
    assert (await _create({"tenant_id": "acme"})).status_code == 201


async def test_an_unavailable_store_answers_503(env):
    with patch.object(akm, "_load_full_store", side_effect=RuntimeError("store down")):
        assert (await _create({"tenant_id": "acme"})).status_code == 503
        assert (await _call("GET", "/admin/keys")).status_code == 503


# ── review fixes ────────────────────────────────────────────────────────────

async def test_a_team_admin_cannot_bind_a_user_who_belongs_elsewhere(env):
    store, audit, _ = env
    await _create({"tenant_id": "acme", "team": "search", "user": "carol@acme.test"})
    await _create({"tenant_id": "acme", "user": "dave@acme.test"})          # tenant-level
    rows, before = len(audit.rows), _stored(store)
    for user in ("carol@acme.test", "dave@acme.test"):
        r = await _create({"user": user}, key=SRE)
        assert r.status_code == 403, (user, r.text)
    assert _stored(store) == before and len(audit.rows) == rows
    assert (await _create({"user": "erin@acme.test"}, key=SRE)).status_code == 201
    assert (await _create({"user": "erin@acme.test"}, key=SRE)).status_code == 201
    # The same user in another tenant is another person: not refused.
    await _create({"tenant_id": "other", "team": "x", "user": "frank@acme.test"})
    assert (await _create({"user": "frank@acme.test"}, key=SRE)).status_code == 201


async def test_a_corrupt_store_is_503_not_422(env):
    store, _audit, _ = env
    assert akm.validate_proxy_key(ROOT)[0] is True       # the caller's key is cached
    store.write_text("{not json", encoding="utf-8")
    assert (await _create({"tenant_id": "acme"})).status_code == 503


async def test_an_oversized_or_too_deep_body_is_refused(env):
    big = {"tenant_id": "acme", "label": "x" * (1024 * 1024 + 10)}
    assert (await _create(big)).status_code == 413
    deep = "[" * 100_000 + "]" * 100_000
    async with AsyncClient(transport=ASGITransport(app=main.app),
                           base_url="http://test") as client:
        r = await client.post("/admin/keys", content=deep,
                              headers={"Authorization": f"Bearer {ROOT}"})
    assert r.status_code == 422



# ── independent review fixes ────────────────────────────────────────────────

async def test_a_team_admin_cannot_raise_the_tier(env):
    store, audit, _ = env
    before = _stored(store)
    r = await _create({"tier": "enterprise"}, key=SRE_FREE)
    assert r.status_code == 403 and "tier" in r.text.lower()
    assert _stored(store) == before and audit.rows == []
    r = await _create({}, key=SRE_FREE)
    assert r.status_code == 201 and r.json()["tier"] == "free"
    r = await _create({}, key=SRE)                         # an enterprise team admin
    assert r.status_code == 201 and r.json()["tier"] == "enterprise"
    assert (await _create({"tier": "free"}, key=SRE)).status_code == 403
    assert (await _create({"tier": "enterprise"}, key=SRE)).status_code == 201


async def test_a_team_admin_cannot_bind_a_user_with_a_global_per_user_limit(env):
    store, audit, cfg = env
    cfg["rate_limit"] = {"per_user": {"vip@acme.test": {"requests_per_minute": 1000}}}
    before = _stored(store)
    r = await _create({"user": "vip@acme.test"}, key=SRE)
    assert r.status_code == 403
    assert "vip@acme.test" not in r.text
    assert _stored(store) == before and audit.rows == []
    r = await _create({"tenant_id": "acme", "team": "payments", "user": "vip@acme.test"})
    assert r.status_code == 201                            # an unbound admin may
