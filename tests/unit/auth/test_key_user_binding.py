"""A key bound to a user (ST-9) resolves to that user at authentication.

The bound user is the request's user and its rate-limit principal; an ``X-User-ID`` override
cannot replace it (the binding wins, as a team binding wins over ``X-Team``). A key with no
user binding behaves exactly as before. An installed key store whose cache is past its
staleness bound answers 503, not "invalid key".
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import hashlib
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from auth import api_key_manager as akm
import main

_CFG = {"proxy": {"allow_user_id_header_override": True,
                  "allowed_user_id_headers": ["*@acme.test"]}}
_STORE = {
    hashlib.sha256(b"k-alice").hexdigest():
        {"tenant_id": "acme", "tier": "free", "team": "payments", "user": "alice@acme.test"},
    hashlib.sha256(b"k-tenant").hexdigest(): {"tenant_id": "acme", "tier": "free"},
}


def _request(key, **headers):
    return SimpleNamespace(headers={"Authorization": f"Bearer {key}", **headers},
                           query_params={}, state=SimpleNamespace(),
                           client=SimpleNamespace(host="203.0.113.9"))


@pytest.fixture(autouse=True)
def _keys():
    akm.replace_cache(dict(_STORE))
    with patch.object(main, "get_config", return_value=_CFG):
        yield
    akm.reset_key_store_backend()


async def test_a_user_bound_key_resolves_to_its_user():
    request = _request("k-alice")
    user_id, _key, meta = await main._authenticate(request)
    assert user_id == "alice@acme.test"
    assert request.state.key_principal == "alice@acme.test"
    assert akm.key_team(meta) == "payments"


async def test_x_user_id_cannot_replace_a_bound_user():
    user_id, _key, _meta = await main._authenticate(
        _request("k-alice", **{"X-User-ID": "bob@acme.test"}))
    assert user_id == "alice@acme.test"


async def test_an_unbound_key_is_unchanged():
    request = _request("k-tenant")
    assert (await main._authenticate(request))[0] == "acme"
    assert request.state.key_principal == "acme"
    overridden = await main._authenticate(_request("k-tenant", **{"X-User-ID": "bob@acme.test"}))
    assert overridden[0] == "bob@acme.test"


async def test_a_stale_key_store_answers_503():
    akm.install_key_store_backend(lambda: None, lambda s: None, name="test",
                                  max_stale_seconds=30)
    akm.replace_cache(dict(_STORE))
    akm._CACHE_LOADED_AT = time.monotonic() - 31
    with pytest.raises(HTTPException) as exc:
        await main._authenticate(_request("k-tenant"))
    assert exc.value.status_code == 503
