"""Key-store engine for the /admin/keys API (ST-9).

A key is named by its ``key_id`` (the first 16 hex characters of its sha256), never by the
raw key, which the store does not hold. A key may be bound to a user as well as a team, at
issuance only. One key can be listed, revoked or rotated within a scope (tenant, team), and
the scope is checked inside the store write, against the current store.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import hashlib
import json
import time

import pytest

from auth import api_key_manager as akm


@pytest.fixture
def temp_store(tmp_path, monkeypatch):
    store = tmp_path / "local-keys.json"
    store.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setattr(akm, "_LOCAL_PROXY_KEYS_FILE", str(store))
    monkeypatch.setattr(akm, "_KEY_CACHE", {})
    monkeypatch.setattr(akm, "_CACHE_LOADED_AT", 0.0)
    akm.reset_key_store_backend()
    yield store
    akm.reset_key_store_backend()


def _store(path):
    return json.loads(path.read_text(encoding="utf-8"))


# ── user binding ────────────────────────────────────────────────────────────

def test_a_key_is_issued_bound_to_a_user(temp_store):
    raw, _h, meta = akm.create_key("acme", team="payments", user="alice@acme.test")
    assert meta["user"] == "alice@acme.test"
    ok, tid, m = akm.validate_proxy_key(raw)
    assert ok and tid == "acme"
    assert akm.key_user(m) == "alice@acme.test" and akm.key_team(m) == "payments"


def test_a_key_without_a_user_has_none(temp_store):
    raw, _h, meta = akm.create_key("acme")
    assert "user" not in meta
    assert akm.key_user(akm.validate_proxy_key(raw)[2]) is None
    assert akm.key_user(None) is None


@pytest.mark.parametrize("bad", ["", " ", "has space", "a\tb", "x" * 129, "é@acme.test", 7])
def test_an_invalid_user_is_refused(temp_store, bad):
    with pytest.raises(ValueError):
        akm.create_key("acme", user=bad)
    assert _store(temp_store) == {}


def test_the_user_cannot_be_set_through_extra(temp_store):
    _raw, _h, meta = akm.create_key("acme", extra={"user": "mallory", "label": "ci"})
    assert "user" not in meta and meta["label"] == "ci"


# ── key ids and listing ─────────────────────────────────────────────────────

def test_the_key_id_is_the_hash_prefix(temp_store):
    raw, key_hash, _m = akm.create_key("acme")
    assert akm.key_id(key_hash) == hashlib.sha256(raw.encode()).hexdigest()[:16]


def test_list_never_returns_a_raw_key_or_a_hash(temp_store):
    raw, key_hash, _m = akm.create_key("acme", team="payments", user="alice@acme.test",
                                       extra={"label": "laptop"})
    [view] = akm.list_keys()
    assert view == {
        "key_id": key_hash[:16], "tenant_id": "acme", "team": "payments",
        "user": "alice@acme.test", "tier": "free", "admin": False, "gateway": False,
        "suspended": False, "label": "laptop", "created_at": view["created_at"],
    }
    text = json.dumps(view)
    assert raw not in text and key_hash not in text


def test_list_filters_by_tenant_team_and_user(temp_store):
    akm.create_key("acme", team="payments", user="alice@acme.test")
    akm.create_key("acme", team="payments", user="bob@acme.test")
    akm.create_key("acme", team="search")
    akm.create_key("acme")
    akm.create_key("other", team="payments")
    assert len(akm.list_keys()) == 5
    assert len(akm.list_keys(tenant_id="acme")) == 4
    assert {v["user"] for v in akm.list_keys(tenant_id="acme", team="payments")} == {
        "alice@acme.test", "bob@acme.test"}
    assert [v["user"] for v in akm.list_keys(tenant_id="acme", user="bob@acme.test")] == [
        "bob@acme.test"]


def test_a_legacy_string_entry_lists_under_its_user_id(temp_store):
    temp_store.write_text(json.dumps({"ab" * 32: "old-user"}), encoding="utf-8")
    [view] = akm.list_keys()
    assert view["tenant_id"] == "old-user" and view["tier"] == "legacy"
    assert view["key_id"] == ("ab" * 32)[:16]


# ── revoke one key ──────────────────────────────────────────────────────────

def test_revoke_removes_only_that_key(temp_store):
    raw1, h1, _ = akm.create_key("acme", team="payments")
    raw2, _h2, _ = akm.create_key("acme", team="payments")
    view = akm.revoke_key(h1[:16])
    assert view["key_id"] == h1[:16] and view["team"] == "payments"
    assert akm.validate_proxy_key(raw1)[0] is False
    assert akm.validate_proxy_key(raw2)[0] is True
    assert len(_store(temp_store)) == 1


def test_revoke_outside_the_scope_finds_nothing_and_writes_nothing(temp_store):
    _raw, h, _ = akm.create_key("acme", team="payments")
    before = temp_store.read_text(encoding="utf-8")
    assert akm.revoke_key(h[:16], tenant_id="other") is None
    assert akm.revoke_key(h[:16], tenant_id="acme", team="search") is None
    assert akm.revoke_key("0" * 16) is None
    assert temp_store.read_text(encoding="utf-8") == before


def test_a_team_scope_never_reaches_an_unbound_key(temp_store):
    _raw, h, _ = akm.create_key("acme")
    assert akm.revoke_key(h[:16], tenant_id="acme", team="payments") is None


def test_an_ambiguous_key_id_is_refused(temp_store):
    a, b = "abcd" * 4 + "0" * 48, "abcd" * 4 + "1" * 48
    temp_store.write_text(json.dumps({a: {"tenant_id": "acme"}, b: {"tenant_id": "acme"}}),
                          encoding="utf-8")
    with pytest.raises(akm.AmbiguousKeyId):
        akm.revoke_key("abcd" * 4)
    assert len(_store(temp_store)) == 2
    assert akm.revoke_key(a)["tenant_id"] == "acme"        # the full hash is unambiguous


# ── rotate one key ──────────────────────────────────────────────────────────

def test_rotate_replaces_one_key_keeping_its_binding(temp_store):
    raw, h, _ = akm.create_key("acme", "enterprise", team="payments", user="alice@acme.test",
                               extra={"label": "laptop", "owner_domain": "acme.test"})
    other_raw, _oh, _ = akm.create_key("acme", team="payments", user="bob@acme.test")
    new_raw, new_hash, meta, old = akm.rotate_key(h[:16])
    assert new_raw != raw and old["key_id"] == h[:16]
    assert (meta["tenant_id"], meta["tier"], meta["team"], meta["user"]) == (
        "acme", "enterprise", "payments", "alice@acme.test")
    assert meta["label"] == "laptop" and meta["owner_domain"] == "acme.test"
    assert akm.validate_proxy_key(raw)[0] is False
    assert akm.validate_proxy_key(new_raw)[0] is True
    assert akm.validate_proxy_key(other_raw)[0] is True       # bob's key is untouched
    assert set(_store(temp_store)) == {new_hash, _oh}


def test_rotate_keeps_trust_flags_allowlist_and_suspension(temp_store):
    _raw, h, _ = akm.create_key("acme", admin=True, gateway=True)
    akm.set_ip_allowlist("acme", ["10.0.0.0/8"])
    akm.set_suspended("acme", True)
    _new, _nh, meta, _old = akm.rotate_key(h[:16])
    assert meta["admin"] is True and meta["gateway"] is True
    assert meta["ip_allowlist"] == ["10.0.0.0/8"] and meta["suspended"] is True


def test_rotate_outside_the_scope_finds_nothing(temp_store):
    _raw, h, _ = akm.create_key("acme", team="payments")
    before = temp_store.read_text(encoding="utf-8")
    assert akm.rotate_key(h[:16], tenant_id="acme", team="search") is None
    assert akm.rotate_key("f" * 16) is None
    assert temp_store.read_text(encoding="utf-8") == before


# ── a bounded staleness for an installed backend ────────────────────────────

def test_an_installed_backend_past_its_max_staleness_validates_nothing(temp_store):
    store = {hashlib.sha256(b"k-live").hexdigest(): {"tenant_id": "acme"}}
    akm.install_key_store_backend(lambda: None, lambda s: None, name="test",
                                  max_stale_seconds=60)
    akm.replace_cache(dict(store))
    assert akm.key_store_stale() is False
    assert akm.validate_proxy_key("k-live")[0] is True
    akm._CACHE_LOADED_AT = time.monotonic() - 61       # no refresh for over a minute
    assert akm.key_store_stale() is True
    assert akm.validate_proxy_key("k-live")[0] is False


def test_without_a_bound_or_a_backend_nothing_is_stale(temp_store):
    akm.create_key("acme")
    akm._CACHE_LOADED_AT = time.monotonic() - 10_000
    assert akm.key_store_stale() is False                 # blob backends: unchanged
    akm.install_key_store_backend(lambda: None, lambda s: None, name="test")
    assert akm.key_store_stale() is False                 # no bound set: unchanged


def test_a_one_key_change_is_announced_for_the_key_s_tenant(temp_store):
    backing, heard = {}, []

    def persist(store):
        backing.clear()
        backing.update(store)

    akm.install_key_store_backend(lambda: dict(backing), persist, name="test",
                                  on_change=heard.append)
    _raw, h1, _ = akm.create_key("acme", team="payments")
    _raw, h2, _ = akm.create_key("acme")
    heard.clear()
    akm.revoke_key(h1[:16])                         # no scope given: the key's own tenant
    akm.rotate_key(h2[:16], tenant_id="acme")
    akm.revoke_key("0" * 16)                         # nothing found: nothing announced
    assert heard == ["acme", "acme"]


# ── a user is part of a key's binding for tenant-wide rotation ──────────────

def test_rotation_per_binding_keeps_each_user_s_key(temp_store):
    akm.create_key("acme", team="payments", user="alice@acme.test")
    akm.create_key("acme", team="payments", user="bob@acme.test")
    akm.create_key("acme", team="payments")
    akm.create_key("acme")
    issued, revoked = akm.rotate_tenant_keys_per_binding("acme")
    assert revoked == 4
    assert [(m.get("team"), m.get("user")) for _r, _h, m in issued] == [
        (None, None), ("payments", None), ("payments", "alice@acme.test"),
        ("payments", "bob@acme.test")]


def test_single_rotation_refuses_a_tenant_with_two_users(temp_store):
    akm.create_key("acme", user="alice@acme.test")
    akm.create_key("acme", user="bob@acme.test")
    before = temp_store.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        akm.rotate_tenant_keys("acme")
    assert temp_store.read_text(encoding="utf-8") == before
