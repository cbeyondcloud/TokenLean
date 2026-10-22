"""A proxy key can be bound to a team (ST-1).

The binding is set at issuance (``create_key(team=...)``), like the ``gateway`` trust flag,
and never through ``extra``: the team selects a spend cap and a config overlay, so it must
not be settable by whatever feeds ``extra``. ``key_team`` reads it back from a validated
key's metadata; legacy string-format keys have none.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

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
    return store


def test_a_key_is_issued_bound_to_a_team(temp_store):
    raw, _h, meta = akm.create_key("acme", team="payments")
    assert meta["team"] == "payments"
    ok, tid, m = akm.validate_proxy_key(raw)
    assert ok and tid == "acme"
    assert akm.key_team(m) == "payments"


def test_a_key_without_a_team_has_none(temp_store):
    raw, _h, meta = akm.create_key("acme")
    assert "team" not in meta
    assert akm.key_team(akm.validate_proxy_key(raw)[2]) is None


@pytest.mark.parametrize("bad", ["", "  ", "has space", "a:b", "x" * 65, "default", 3])
def test_an_invalid_team_is_refused(temp_store, bad):
    with pytest.raises(ValueError):
        akm.create_key("acme", team=bad)
    assert temp_store.read_text() == "{}"          # nothing written


def test_the_team_cannot_be_set_through_extra(temp_store):
    _raw, _h, meta = akm.create_key("acme", extra={"team": "payments", "owner_domain": "a.test"})
    assert "team" not in meta
    assert meta["owner_domain"] == "a.test"


def test_rotation_keeps_the_team(temp_store):
    akm.create_key("acme", team="payments")
    _raw, _h, meta, revoked = akm.rotate_tenant_keys("acme")
    assert revoked == 1 and meta["team"] == "payments"


def test_key_team_reads_only_a_usable_string():
    assert akm.key_team({"team": "payments"}) == "payments"
    assert akm.key_team({"team": "  payments "}) == "payments"
    for meta in (None, "legacy-user", {}, {"team": ""}, {"team": "  "}, {"team": 5},
                 {"team": ["a"]}, {"team": None}):
        assert akm.key_team(meta) is None, meta
