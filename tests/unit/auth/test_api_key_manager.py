"""Unit tests for auth/api_key_manager.py."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import hashlib
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import HTTPException


_KEY_ALICE = "proxy-key-abc123"
_KEY_BOB = "proxy-key-xyz789"

# Secret stores SHA256(key) → user_id
_VALID_KEYS_JSON = json.dumps({
    hashlib.sha256(_KEY_ALICE.encode()).hexdigest(): "alice",
    hashlib.sha256(_KEY_BOB.encode()).hexdigest(): "bob",
})


def _mock_secret_client(secret_data: str):
    """Build a mock Google SecretManagerServiceClient that returns secret_data."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.payload.data = secret_data.encode()
    mock_client.access_secret_version.return_value = mock_response
    return mock_client


class TestValidateProxyKey:
    def _reset_cache(self):
        import auth.api_key_manager as mgr
        mgr._KEY_CACHE = {}
        mgr._CACHE_LOADED_AT = 0.0

    def test_valid_key_returns_is_valid_and_user_id(self):
        """Legacy key format ({hash: user_id}) returns (True, user_id, None) —
        the third element is reserved for new-format tenant metadata."""
        import auth.api_key_manager as mgr
        self._reset_cache()
        with patch("auth.api_key_manager._fetch_secret", return_value=_VALID_KEYS_JSON):
            is_valid, user_id, metadata = mgr.validate_proxy_key(_KEY_ALICE)
        assert is_valid is True
        assert user_id == "alice"
        assert metadata is None

    def test_invalid_key_returns_false(self):
        import auth.api_key_manager as mgr
        self._reset_cache()
        with patch("auth.api_key_manager._fetch_secret", return_value=_VALID_KEYS_JSON):
            is_valid, user_id, metadata = mgr.validate_proxy_key("bad-key-000")
        assert is_valid is False
        assert user_id is None
        assert metadata is None

    def test_malformed_json_returns_false(self):
        import auth.api_key_manager as mgr
        self._reset_cache()
        with patch("auth.api_key_manager._fetch_secret", return_value="not-valid-json{{{}"):
            is_valid, user_id, metadata = mgr.validate_proxy_key(_KEY_ALICE)
        assert is_valid is False

    def test_new_format_key_returns_tenant_id_and_metadata(self):
        """New key format ({hash: {tenant_id, tier}}) returns (True, tenant_id, metadata_dict)."""
        import auth.api_key_manager as mgr
        self._reset_cache()
        new_format_keys = json.dumps({
            hashlib.sha256(_KEY_ALICE.encode()).hexdigest(): {"tenant_id": "acme", "tier": "enterprise"},
        })
        with patch("auth.api_key_manager._fetch_secret", return_value=new_format_keys):
            is_valid, tenant_id, metadata = mgr.validate_proxy_key(_KEY_ALICE)
        assert is_valid is True
        assert tenant_id == "acme"
        assert metadata == {"tenant_id": "acme", "tier": "enterprise"}


class TestIsSuspended:
    def test_suspended_true(self):
        from auth.api_key_manager import is_suspended
        assert is_suspended({"tenant_id": "acme", "suspended": True}) is True

    def test_suspended_false(self):
        from auth.api_key_manager import is_suspended
        assert is_suspended({"tenant_id": "acme", "suspended": False}) is False

    def test_suspended_absent_defaults_false(self):
        from auth.api_key_manager import is_suspended
        assert is_suspended({"tenant_id": "acme", "tier": "enterprise"}) is False

    def test_legacy_string_key_never_suspended(self):
        from auth.api_key_manager import is_suspended
        assert is_suspended(None) is False


class TestGetLLMProviderKey:
    def test_returns_env_var_override(self):
        with patch.dict(os.environ, {"LLM_KEY_OPENAI": "sk-local-test-key"}):
            from auth.api_key_manager import get_llm_provider_key
            result = get_llm_provider_key("openai")
        assert result == "sk-local-test-key"

    def test_falls_back_to_secret_manager(self):
        with patch("auth.api_key_manager._access_llm_key_secret", return_value="sk-secret-key"):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("LLM_KEY_OPENAI", None)
                from auth.api_key_manager import get_llm_provider_key
                result = get_llm_provider_key("openai")
        assert result == "sk-secret-key"


class NotFound(Exception):
    """Named as google.api_core's: the secret, or its latest version, does not exist."""


class ServiceUnavailable(Exception):
    """Named as google.api_core's: Secret Manager could not be asked."""


class TestPlatformKeyCache:
    """#107: on GCP every lookup that got past `LLM_KEY_<P>` built a Secret Manager client and
    made a blocking call, uncached, with an ERROR line per lookup when the secret had no version;
    on the managed service (no platform keys) that was every routed request."""

    @pytest.fixture
    def sm(self, monkeypatch):
        """A fake Secret Manager read: answers from `table`, raises from `errors`, counts calls;
        `clock` is the cache's clock."""
        import types

        from auth import api_key_manager as akm
        state = types.SimpleNamespace(table={}, errors={}, calls=[], now=1000.0)

        def access(secret_name):
            state.calls.append(secret_name)
            if secret_name in state.errors:
                raise state.errors[secret_name]
            return state.table[secret_name]

        monkeypatch.setattr(akm, "_access_llm_key_secret", access)
        monkeypatch.setattr(akm, "_now", lambda: state.now)
        monkeypatch.delenv("LLM_KEY_OPENAI", raising=False)
        return state

    def test_a_key_is_read_once_per_ttl(self, sm):
        from auth.api_key_manager import _LLM_KEY_CACHE_SECONDS, get_llm_provider_key
        sm.table["llm-key-openai"] = "sk-1"
        assert get_llm_provider_key("openai") == get_llm_provider_key("openai") == "sk-1"
        assert len(sm.calls) == 1
        sm.table["llm-key-openai"] = "sk-2"                    # rotated
        sm.now += _LLM_KEY_CACHE_SECONDS + 1
        assert get_llm_provider_key("openai") == "sk-2" and len(sm.calls) == 2

    def test_a_missing_secret_is_remembered_for_the_ttl_and_logged_once(self, sm, caplog):
        import logging

        from auth.api_key_manager import (
            _LLM_KEY_CACHE_SECONDS,
            _LLM_KEY_RETRY_SECONDS,
            get_llm_provider_key,
        )
        sm.errors["llm-key-openai"] = NotFound("no versions")
        with caplog.at_level(logging.ERROR, logger="auth.api_key_manager"):
            assert get_llm_provider_key("openai") is None
            sm.now += _LLM_KEY_RETRY_SECONDS + 1   # past an outage's retry, inside the TTL
            assert get_llm_provider_key("openai") is None
        assert len(sm.calls) == 1, "a secret that is not there is not an outage to retry"
        assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1
        sm.now += _LLM_KEY_CACHE_SECONDS
        get_llm_provider_key("openai")
        assert len(sm.calls) == 2

    def test_an_outage_keeps_the_last_key_and_is_retried_soon(self, sm):
        """A brief Secret Manager outage must not become a TTL of 'no key' on a proxy that
        has one: the last key is still served, and the read is retried within seconds."""
        from auth.api_key_manager import (
            _LLM_KEY_CACHE_SECONDS,
            _LLM_KEY_RETRY_SECONDS,
            get_llm_provider_key,
        )
        sm.table["llm-key-openai"] = "sk-1"
        get_llm_provider_key("openai")
        sm.now += _LLM_KEY_CACHE_SECONDS + 1
        sm.errors["llm-key-openai"] = ServiceUnavailable("unreachable")
        assert get_llm_provider_key("openai") == "sk-1"
        assert get_llm_provider_key("openai") == "sk-1" and len(sm.calls) == 2
        del sm.errors["llm-key-openai"]
        sm.table["llm-key-openai"] = "sk-2"
        sm.now += _LLM_KEY_RETRY_SECONDS + 1
        assert get_llm_provider_key("openai") == "sk-2" and len(sm.calls) == 3

    def test_an_outage_before_any_key_is_retried_soon_not_after_the_ttl(self, sm):
        from auth.api_key_manager import _LLM_KEY_RETRY_SECONDS, get_llm_provider_key
        sm.errors["llm-key-openai"] = ServiceUnavailable("unreachable")
        assert get_llm_provider_key("openai") is None
        del sm.errors["llm-key-openai"]
        sm.table["llm-key-openai"] = "sk-1"
        sm.now += _LLM_KEY_RETRY_SECONDS + 1
        assert get_llm_provider_key("openai") == "sk-1"

    @pytest.mark.parametrize("error", [NotFound, ServiceUnavailable], ids=["no key", "outage"])
    def test_a_key_put_in_the_secret_name_variable_never_reaches_the_log(
            self, sm, monkeypatch, caplog, error):
        """SECRET_<P>_API_KEY holds the NAME of the secret, and its own name invites the key
        itself: whatever it holds is never logged, not even inside the error's text. The log
        names the provider instead."""
        import logging

        from auth.api_key_manager import get_llm_provider_key
        monkeypatch.setenv("SECRET_OPENAI_API_KEY", "sk-put-here-by-mistake")
        sm.errors["sk-put-here-by-mistake"] = error(
            "projects/p/secrets/sk-put-here-by-mistake/versions/latest: failed")
        with caplog.at_level(logging.ERROR, logger="auth.api_key_manager"):
            assert get_llm_provider_key("openai") is None
        assert caplog.records, "the failure is still logged"
        assert "sk-put-here-by-mistake" not in caplog.text
        assert "openai" in caplog.text

    def test_the_env_key_is_read_on_every_lookup(self, sm, monkeypatch):
        from auth.api_key_manager import get_llm_provider_key
        sm.table["llm-key-openai"] = "sk-from-secret-manager"
        get_llm_provider_key("openai")
        monkeypatch.setenv("LLM_KEY_OPENAI", "sk-from-env")
        assert get_llm_provider_key("openai") == "sk-from-env"

    def test_one_client_serves_every_lookup(self, monkeypatch):
        """It was one new client per lookup; a client that cannot be built is not kept."""
        from auth.api_key_manager import get_llm_provider_key
        from google.cloud import secretmanager
        built = []

        class Client:
            def __init__(self, *args, **kwargs):
                built.append(self)

            def access_secret_version(self, request):
                return MagicMock(payload=MagicMock(data=b"sk-" + request["name"].encode()[-6:]))

        from auth.api_key_manager import _LLM_KEY_RETRY_SECONDS
        clock = [1000.0]
        monkeypatch.setattr("auth.api_key_manager._now", lambda: clock[0])
        monkeypatch.delenv("LLM_KEY_OPENAI", raising=False)
        monkeypatch.delenv("LLM_KEY_ANTHROPIC", raising=False)
        assert get_llm_provider_key("openai") is None          # the suite's stand-in refuses
        monkeypatch.setattr(secretmanager, "SecretManagerServiceClient", Client)
        clock[0] += _LLM_KEY_RETRY_SECONDS + 1                 # a refusal is retried soon
        assert get_llm_provider_key("openai") and get_llm_provider_key("anthropic")
        assert len(built) == 1

    async def test_async_lookups_fetch_a_miss_off_the_event_loop(self, sm):
        import threading

        from auth import api_key_manager as akm
        from providers.key_resolver import resolve_provider_key
        threads = []
        sm.table["llm-key-openai"] = "sk-1"
        real = akm._access_llm_key_secret

        def access(secret_name):
            threads.append(threading.get_ident())
            return real(secret_name)

        akm._access_llm_key_secret = access
        try:
            assert await resolve_provider_key("openai") == "sk-1"
            assert await akm.get_llm_provider_key_async("openai") == "sk-1"   # cached
        finally:
            akm._access_llm_key_secret = real
        assert threads and threading.get_ident() not in threads
        assert len(sm.calls) == 1


def test_tests_cannot_reach_the_real_secret_manager():
    """A test that reaches a Secret Manager call it did not patch used to call Google with the
    developer's own credentials. The suite's conftest swaps both client classes for a
    stand-in that refuses, and the key fetch then reports no secret, as it does without
    credentials."""
    from google.cloud import secretmanager
    for name in ("SecretManagerServiceClient", "SecretManagerServiceAsyncClient"):
        client_class = getattr(secretmanager, name)
        assert getattr(client_class, "is_test_stand_in", False), name
        with pytest.raises(RuntimeError, match="real Secret Manager"):
            client_class()
    from auth.api_key_manager import _fetch_secret
    assert _fetch_secret("llm-key-openai") is None
