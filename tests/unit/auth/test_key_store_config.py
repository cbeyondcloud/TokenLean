"""Choosing the key store by config (ST-9): ``key_store.backend: blob | postgres``.

``blob`` (the default, and what an absent section means) is the local file / Secret Manager
store, unchanged. ``postgres`` validates keys against the ``proxy_keys`` table: until the
database is wired, NO key validates (the blob is not consulted: a key revoked in Postgres
may still be in it), then every replica re-reads the table each ``refresh_seconds`` and
refuses every key once it has not managed to for ``max_stale_seconds``.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import hashlib
import json

import pytest

from auth import api_key_manager as akm
from auth import key_store_config as ksc


@pytest.fixture(autouse=True)
def _clean():
    akm.reset_key_store_backend()
    yield
    akm.reset_key_store_backend()


def test_no_section_means_the_blob_store():
    assert ksc.key_store_settings({}) == {"backend": "blob", "refresh_seconds": 5,
                                          "max_stale_seconds": 300}
    assert ksc.key_store_settings({"key_store": None})["backend"] == "blob"


def test_postgres_settings_are_read_and_bounded():
    s = ksc.key_store_settings({"key_store": {"backend": "Postgres", "refresh_seconds": 2,
                                              "max_stale_seconds": 120}})
    assert s == {"backend": "postgres", "refresh_seconds": 5, "max_stale_seconds": 120}


@pytest.mark.parametrize("bad", [
    {"backend": "postgress"}, {"backend": 3}, {"refresh_seconds": "soon"},
    {"max_stale_seconds": -1}, {"refresh_seconds": True}, "postgres",
])
def test_an_unusable_setting_is_refused_not_guessed(bad):
    with pytest.raises(ValueError):
        ksc.key_store_settings({"key_store": bad})


def test_a_stale_bound_below_the_refresh_interval_is_refused():
    with pytest.raises(ValueError):
        ksc.key_store_settings({"key_store": {"backend": "postgres", "refresh_seconds": 30,
                                              "max_stale_seconds": 20}})


def test_while_postgres_is_pending_no_key_validates_and_no_write_happens(tmp_path,
                                                                        monkeypatch):
    blob = tmp_path / "local-keys.json"
    blob.write_text(json.dumps({hashlib.sha256(b"k-blob").hexdigest(): {"tenant_id": "acme"}}),
                    encoding="utf-8")
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setattr(akm, "_LOCAL_PROXY_KEYS_FILE", str(blob))
    ksc.install_pending({"backend": "postgres", "refresh_seconds": 5,
                         "max_stale_seconds": 300})
    assert akm.key_store_backend_installed() is True
    assert akm.key_store_stale() is True              # main answers 503 for every key
    assert akm.validate_proxy_key("k-blob")[0] is False   # the blob is not consulted
    with pytest.raises(RuntimeError):
        akm.create_key("acme")
    assert akm._load_blob_store() == json.loads(blob.read_text(encoding="utf-8"))


def test_the_blob_default_installs_nothing():
    ksc.install_pending(ksc.key_store_settings({}))
    assert akm.key_store_backend_installed() is False



def test_a_stale_bound_under_twice_the_refresh_is_refused():
    with pytest.raises(ValueError):
        ksc.key_store_settings({"key_store": {"backend": "postgres", "refresh_seconds": 30,
                                              "max_stale_seconds": 50}})
    s = ksc.key_store_settings({"key_store": {"backend": "postgres", "refresh_seconds": 30,
                                              "max_stale_seconds": 60}})
    assert s["max_stale_seconds"] == 60
    assert ksc.key_store_settings({"key_store": {"backend": "postgres",
                                                 "max_stale_seconds": 0}})["max_stale_seconds"] == 0
