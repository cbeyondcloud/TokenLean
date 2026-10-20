"""CONFIG_OVERLAYS: named YAML files deep-merged over the loaded config, in order.

A deployment profile (e.g. config/profiles/*.yaml) states only what it changes, at any depth,
over the shipped config. Unset, nothing changes. A missing overlay fails the load instead of
silently serving the config without it - a profile that exists to turn things OFF must never
quietly fall back to everything on.
"""
import pytest
import yaml

import config_loader
from config_loader import ConfigLoadError, deep_merge


def _write(path, data):
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return str(path)


@pytest.fixture
def base(tmp_path, monkeypatch):
    path = _write(tmp_path / "config.yaml", {
        "proxy": {"port": 4000, "default_model": "m"},
        "providers": [{"name": "a"}, {"name": "b"}],
        "groups": {"G5_cache": {"enabled": True, "l1_ttl_seconds": 3600,
                                "nested": {"x": 1, "y": 2}}},
    })
    monkeypatch.setenv("CONFIG_LOCAL_PATH", path)
    monkeypatch.delenv("CONFIG_GCS_BUCKET", raising=False)
    monkeypatch.delenv("CONFIG_OVERLAYS", raising=False)
    monkeypatch.setattr(config_loader, "_sleep", lambda s: None)
    monkeypatch.setattr(config_loader, "_config", {})
    return path


def test_no_overlays_env_leaves_config_unchanged(base):
    cfg = config_loader.load_config(required=True)
    assert cfg["groups"]["G5_cache"] == {"enabled": True, "l1_ttl_seconds": 3600,
                                         "nested": {"x": 1, "y": 2}}
    assert [p["name"] for p in cfg["providers"]] == ["a", "b"]


def test_overlay_deep_merges_maps_and_replaces_lists(base, tmp_path, monkeypatch):
    ov = _write(tmp_path / "ov.yaml", {
        "groups": {"G5_cache": {"enabled": False, "nested": {"y": 20}}},
        "providers": [{"name": "c"}],
    })
    monkeypatch.setenv("CONFIG_OVERLAYS", ov)
    cfg = config_loader.load_config(required=True)
    assert cfg["groups"]["G5_cache"] == {"enabled": False, "l1_ttl_seconds": 3600,
                                         "nested": {"x": 1, "y": 20}}
    assert cfg["providers"] == [{"name": "c"}]
    assert cfg["proxy"] == {"port": 4000, "default_model": "m"}


def test_overlays_apply_in_order(base, tmp_path, monkeypatch):
    one = _write(tmp_path / "one.yaml", {"proxy": {"port": 1, "extra": "one"}})
    two = _write(tmp_path / "two.yaml", {"proxy": {"port": 2}})
    monkeypatch.setenv("CONFIG_OVERLAYS", f" {one} , {two} ,")
    cfg = config_loader.load_config(required=True)
    assert cfg["proxy"] == {"port": 2, "default_model": "m", "extra": "one"}


def test_overlay_values_get_env_expansion(base, tmp_path, monkeypatch):
    ov = _write(tmp_path / "ov.yaml", {"proxy": {"default_model": "${LITE_TEST_MODEL:-dflt}"}})
    monkeypatch.setenv("CONFIG_OVERLAYS", ov)
    monkeypatch.delenv("LITE_TEST_MODEL", raising=False)
    assert config_loader.load_config(required=True)["proxy"]["default_model"] == "dflt"


def test_missing_overlay_fails_required_load(base, tmp_path, monkeypatch):
    missing = str(tmp_path / "nope.yaml")
    monkeypatch.setenv("CONFIG_OVERLAYS", missing)
    with pytest.raises(ConfigLoadError) as err:
        config_loader.load_config(required=True)
    assert "nope.yaml" in str(err.value)
    # the base file is fine: no "copy the template" hint pointing at the wrong file
    assert "copy config/config.yaml.template" not in str(err.value)


def test_missing_overlay_on_reload_keeps_last_good(base, tmp_path, monkeypatch):
    ov = _write(tmp_path / "ov.yaml", {"proxy": {"port": 9}})
    monkeypatch.setenv("CONFIG_OVERLAYS", ov)
    assert config_loader.load_config(required=True)["proxy"]["port"] == 9
    monkeypatch.setenv("CONFIG_OVERLAYS", str(tmp_path / "gone.yaml"))
    assert config_loader.load_config()["proxy"]["port"] == 9


def test_overlay_root_must_be_mapping(base, tmp_path, monkeypatch):
    bad = tmp_path / "bad.yaml"
    bad.write_text("- a\n- b\n", encoding="utf-8")
    monkeypatch.setenv("CONFIG_OVERLAYS", str(bad))
    with pytest.raises(ConfigLoadError):
        config_loader.load_config(required=True)


def test_empty_overlay_file_changes_nothing(base, tmp_path, monkeypatch):
    empty = tmp_path / "empty.yaml"
    empty.write_text("# nothing yet\n", encoding="utf-8")
    monkeypatch.setenv("CONFIG_OVERLAYS", str(empty))
    assert config_loader.load_config(required=True)["proxy"]["port"] == 4000


def test_deep_merge_does_not_mutate_inputs():
    base = {"a": {"b": 1}}
    over = {"a": {"c": 2}}
    merged = deep_merge(base, over)
    assert merged == {"a": {"b": 1, "c": 2}}
    assert base == {"a": {"b": 1}} and over == {"a": {"c": 2}}


@pytest.mark.parametrize("text", [
    "groups:\n",                              # an empty section
    "groups:\n  G5_cache:\n",                 # an empty group
    "groups:\n  G5_cache:\n    nested:\n",     # an empty nested map
    "proxy:\n  port: ~\n",                    # an explicit null scalar
])
def test_a_null_value_in_an_overlay_changes_nothing(base, tmp_path, monkeypatch, text):
    """A key written with nothing under it (a half-edited overlay) must never replace what
    the config has: `groups:` alone used to put groups=None live on a hot reload."""
    ov = tmp_path / "ov.yaml"
    ov.write_text(text, encoding="utf-8")
    monkeypatch.setenv("CONFIG_OVERLAYS", str(ov))
    cfg = config_loader.load_config(required=True)
    assert cfg["groups"]["G5_cache"] == {"enabled": True, "l1_ttl_seconds": 3600,
                                         "nested": {"x": 1, "y": 2}}
    assert cfg["proxy"]["port"] == 4000


def test_deep_merge_skips_none_at_any_depth():
    assert deep_merge({"a": {"b": 1}, "c": 2}, {"a": None, "c": None, "d": None}) == \
        {"a": {"b": 1}, "c": 2}


def test_a_bad_overlay_is_not_retried(base, tmp_path, monkeypatch):
    """Retrying rides out a storage blip; a missing or invalid overlay is the operator's
    mistake and will not fix itself, so it fails at once, without the 1s + 2s of sleeps."""
    sleeps = []
    monkeypatch.setattr(config_loader, "_sleep", sleeps.append)
    monkeypatch.setenv("CONFIG_OVERLAYS", str(tmp_path / "missing.yaml"))
    with pytest.raises(ConfigLoadError):
        config_loader.load_config(required=True)
    assert sleeps == []
