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


class TestRotationPerBinding:
    """Rotation issues one new key per binding (the unbound keys, and each team), each with
    its own team and metadata, and revokes every old key in the same store write (review
    I1). ``rotate_tenant_keys`` keeps its 4-tuple for a tenant with one binding and refuses,
    changing nothing, for one with several (it could return only one of the new keys)."""

    @staticmethod
    def _tenant():
        import time
        raws = {}
        raws[None], _, _ = akm.create_key("acme", extra={"owner_domain": "acme.test"})
        time.sleep(0.01)
        raws["search"], _, _ = akm.create_key("acme", team="search", gateway=True)
        time.sleep(0.01)
        raws["payments"], _, _ = akm.create_key("acme", team="payments")
        time.sleep(0.01)
        raws["payments-2"], _, _ = akm.create_key("acme", team="payments")
        return raws

    def test_one_new_key_per_binding_each_keeping_its_own(self, temp_store):
        old = self._tenant()
        issued, revoked = akm.rotate_tenant_keys_per_binding("acme")
        assert revoked == 4
        by_team = {meta.get("team"): (raw, meta) for raw, _h, meta in issued}
        assert list(by_team) == [None, "payments", "search"]    # unbound first, then sorted
        assert by_team[None][1]["owner_domain"] == "acme.test"  # its own metadata
        assert by_team["search"][1]["gateway"] is True
        assert "gateway" not in by_team["payments"][1]
        for raw, meta in by_team.values():
            ok, tid, m = akm.validate_proxy_key(raw)
            assert ok and tid == "acme" and akm.key_team(m) == meta.get("team")
        for raw in old.values():
            assert akm.validate_proxy_key(raw)[0] is False

    def test_the_old_api_refuses_several_bindings_and_changes_nothing(self, temp_store):
        old = self._tenant()
        before = temp_store.read_text()
        with pytest.raises(ValueError, match="rotate_tenant_keys_per_binding"):
            akm.rotate_tenant_keys("acme")
        assert temp_store.read_text() == before
        assert all(akm.validate_proxy_key(raw)[0] for raw in old.values())

    def test_the_old_api_is_unchanged_for_one_binding(self, temp_store):
        akm.create_key("solo", team="payments")
        akm.create_key("solo", team="payments")
        raw, _h, meta, revoked = akm.rotate_tenant_keys("solo")
        assert revoked == 2 and meta["team"] == "payments"
        akm.create_key("plain")
        raw, _h, meta, revoked = akm.rotate_tenant_keys("plain")
        assert revoked == 1 and "team" not in meta
        assert akm.validate_proxy_key(raw)[0]

    def test_suspension_and_ip_allowlist_carry_to_every_new_key(self, temp_store):
        self._tenant()
        akm.set_ip_allowlist("acme", ["10.0.0.0/8"])
        akm.set_suspended("acme", True)
        issued, _ = akm.rotate_tenant_keys_per_binding("acme")
        assert all(m.get("suspended") is True and m.get("ip_allowlist") == ["10.0.0.0/8"]
                   for _r, _h, m in issued)

    def test_no_keys(self, temp_store):
        with pytest.raises(ValueError):
            akm.rotate_tenant_keys_per_binding("nobody")
