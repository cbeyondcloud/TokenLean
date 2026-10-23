"""The runtime overlay (ST-10): the admin-managed routing fragment, merged over the loaded
config with the CONFIG_OVERLAYS rules, swapped in without a restart.

With no runtime overlay, get_config() returns the loaded config object itself: exactly as
before. Setting one swaps in a NEW object (a request already holding the old one keeps it);
a hot reload of the file keeps it applied; clearing it restores the loaded object. Its
strings are taken literally (no ${VAR} expansion).
"""
import copy

import pytest
import yaml

import config_loader
from config_loader import deep_merge


@pytest.fixture
def loaded(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    data = {"proxy": {"port": 4000},
            "groups": {"G6_routing": {"enabled": True, "tiers": {"simple": ["a"],
                                                                   "medium": ["b"]}}},
            "rate_limit": {"enabled": True}}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv("CONFIG_LOCAL_PATH", str(path))
    monkeypatch.delenv("CONFIG_GCS_BUCKET", raising=False)
    monkeypatch.delenv("CONFIG_OVERLAYS", raising=False)
    monkeypatch.setattr(config_loader, "_sleep", lambda s: None)
    monkeypatch.setattr(config_loader, "_config", {})
    monkeypatch.setattr(config_loader, "_base_config", None)
    monkeypatch.setattr(config_loader, "_runtime_overlay", {})
    config_loader.load_config(required=True)
    yield path, data
    config_loader.set_runtime_overlay({})


def test_without_an_overlay_the_loaded_object_is_served(loaded):
    _path, data = loaded
    cfg = config_loader.get_config()
    assert cfg == data
    assert config_loader.get_config() is cfg
    config_loader.set_runtime_overlay({})
    assert config_loader.get_config() is cfg            # an empty overlay changes nothing


def test_an_overlay_is_merged_and_swapped_in_without_touching_the_old_object(loaded):
    _path, data = loaded
    in_flight = config_loader.get_config()
    snapshot = copy.deepcopy(in_flight)
    fragment = {"groups": {"G6_routing": {"tiers": {"simple": ["c"]}}},
                "teams": {"payments": {"monthly_spend_cap_usd": 5}}}
    config_loader.set_runtime_overlay(fragment)
    cfg = config_loader.get_config()
    assert cfg == deep_merge(data, fragment)
    assert cfg["groups"]["G6_routing"]["tiers"] == {"simple": ["c"], "medium": ["b"]}
    assert in_flight == snapshot                        # a request in flight is unaffected
    config_loader.set_runtime_overlay({})
    assert config_loader.get_config() is in_flight      # cleared: the loaded object again


def test_a_hot_reload_keeps_the_overlay(loaded):
    path, data = loaded
    config_loader.set_runtime_overlay({"teams": {"payments": {"monthly_spend_cap_usd": 5}}})
    data["proxy"]["port"] = 4001
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    cfg = config_loader.load_config()                   # a hot reload (not required)
    assert cfg["proxy"]["port"] == 4001
    assert cfg["teams"] == {"payments": {"monthly_spend_cap_usd": 5}}
    # The section the overlay adds is not "dropped" by a reload of a file without it.
    assert config_loader.CONFIG_RELOAD_FAILURES._value.get() is not None


def test_a_reload_is_judged_against_the_file_not_the_overlay(loaded, monkeypatch):
    path, data = loaded
    config_loader.set_runtime_overlay({"tenants": {"ACME": {"groups": {}}}})
    failures = []
    monkeypatch.setattr(config_loader.CONFIG_RELOAD_FAILURES, "inc",
                        lambda *a: failures.append(1))
    config_loader.load_config()
    assert failures == []                               # 'tenants' is the overlay's, not lost
    del data["rate_limit"]
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    config_loader.load_config()
    assert failures == [1]                              # a section the FILE lost: refused
    assert config_loader.get_config()["rate_limit"] == {"enabled": True}


def test_overlay_strings_are_taken_literally(loaded, monkeypatch):
    monkeypatch.setenv("SOME_SECRET", "s3cr3t")
    config_loader.set_runtime_overlay(
        {"groups": {"G6_routing": {"tiers": {"simple": ["${SOME_SECRET}"]}}}})
    assert config_loader.get_config()["groups"]["G6_routing"]["tiers"]["simple"] == [
        "${SOME_SECRET}"]
    config_loader.load_config()
    assert config_loader.get_config()["groups"]["G6_routing"]["tiers"]["simple"] == [
        "${SOME_SECRET}"]
