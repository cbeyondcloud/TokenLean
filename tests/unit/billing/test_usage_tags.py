"""Usage tags (ST-2): allow-listed request headers stored as ``usage_events.tags``.

Only headers on the allow-list (``billing.metering.tags.headers``: header name -> tag key;
by default the ten ``X-Agentic-*`` identity headers an agent framework sends) become tags.
Any other header is ignored. Values are trimmed, capped at ``max_value_chars``, must be
printable ASCII, and a value that looks like a credential is dropped, never stored. The
tags are what the CALLER asserts, for attribution: they never select a team, a limit or a
config (``usage_events.team`` is the authenticated team).
"""
import pytest

from billing import tags as usage_tags
from billing.tags import DEFAULT_TAG_HEADERS, extract_tags, looks_like_secret

FRAMEWORK = {
    "x-agentic-team": "payments",
    "x-agentic-user": "dev@acme.test",
    "x-agentic-repo": "github.com/acme/shop",
    "x-agentic-pipeline": "feature",
    "x-agentic-run-id": "3f2b9c3e-8a51-4c1e-9d7a-2b9e0c4d5f61",
    "x-agentic-step": "implement",
    "x-agentic-agent": "builder@1.4.0",
    "x-agentic-parent-agent": "planner@1.2.0",
    "x-agentic-tier": "standard",
    "x-agentic-policy-version": "9fceb02d0ae598e95dc970b74767f19372d61af8",
}
EXPECTED = {
    "team": "payments", "user": "dev@acme.test", "repo": "github.com/acme/shop",
    "pipeline": "feature", "run_id": "3f2b9c3e-8a51-4c1e-9d7a-2b9e0c4d5f61",
    "step": "implement", "agent": "builder@1.4.0", "parent_agent": "planner@1.2.0",
    "tier": "standard", "policy_version": "9fceb02d0ae598e95dc970b74767f19372d61af8",
}


def _cfg(**tags):
    return {"billing": {"metering": {"tags": tags}}} if tags else {}


class TestTheDefaultAllowList:
    def test_names_the_ten_identity_headers(self):
        assert {h.lower(): k for h, k in DEFAULT_TAG_HEADERS.items()} == {
            h: EXPECTED_KEY for h, EXPECTED_KEY in zip(FRAMEWORK, EXPECTED)}

    def test_the_shipped_template_states_the_same_default(self):
        from pathlib import Path

        import yaml
        template = Path(__file__).resolve().parents[3] / "config" / "params" / \
            "billing.yaml.template"
        shipped = yaml.safe_load(template.read_text(encoding="utf-8"))["billing"]["metering"]
        assert shipped["tags"] == {"enabled": True, "max_value_chars": 256,
                                   "headers": DEFAULT_TAG_HEADERS}

    def test_all_ten_are_stored(self):
        assert extract_tags(FRAMEWORK, {}) == EXPECTED

    def test_a_header_not_on_the_list_is_ignored(self):
        headers = {**FRAMEWORK, "x-agentic-secret-thing": "value", "x-step-name": "s",
                   "authorization": "Bearer abc", "x-team": "finance", "cookie": "a=b"}
        assert extract_tags(headers, {}) == EXPECTED

    def test_header_names_match_in_any_case(self):
        assert extract_tags({"X-Agentic-Run-Id": "r1", "X-AGENTIC-STEP": "s1"}, {}) == {
            "run_id": "r1", "step": "s1"}

    def test_no_allow_listed_header_means_no_tags(self):
        assert extract_tags({"content-type": "application/json"}, {}) is None
        assert extract_tags({}, {}) is None


class TestValues:
    def test_trimmed_and_empty_dropped(self):
        assert extract_tags({"x-agentic-step": "  plan  ", "x-agentic-run-id": "   "}, {}) == {
            "step": "plan"}

    def test_capped_at_256_by_default(self):
        got = extract_tags({"x-agentic-step": "s" * 1000}, {})
        assert got == {"step": "s" * 256}

    def test_the_cap_is_configurable_and_bounded(self):
        assert extract_tags({"x-agentic-step": "abcdef"}, _cfg(max_value_chars=3)) == {
            "step": "abc"}
        assert len(extract_tags({"x-agentic-step": "s" * 5000},
                                _cfg(max_value_chars=10**6))["step"]) == 1024
        assert extract_tags({"x-agentic-step": "abc"}, _cfg(max_value_chars="x")) == {
            "step": "abc"}

    @pytest.mark.parametrize("value", ["line\nbreak", "tab\there", "café", "nul\x00",
                                       "bell\x07"])
    def test_non_printable_ascii_is_dropped(self, value):
        assert extract_tags({"x-agentic-step": value}, {}) is None

    # Fake credentials, assembled at run time so no credential-shaped literal sits in the
    # source for the repository's own leak scan (scripts/ci/leak_scan.py) to flag.
    @pytest.mark.parametrize("value", [
        "sk-proj-Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4z",           # provider-style API key
        "sk-ant-api03-" + "A1b2C3d4" * 12,                     # long provider key
        "tok-3f9a1c0b8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a2b1c0d9e",  # proxy-key style
        "Bearer abcdef0123456789",
        "Basic dXNlcjpwYXNzd29yZA==",
        "https://deploy:" + "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz0123456789"
        + "@github.com/acme/shop",
        "https://deploy:hunter2@git.example/acme/shop",        # user:password in a URL
        "https://" + "Ab3dEf6hIj9kLm2nOp5qRs8t" + "@github.com/acme/shop",  # token as user
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
        "SK-" + "3f9a1c0b8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a2b1c0d9e",   # upper-case prefix
        "Bearer%20abcdef0123456789",                             # URL-encoded
        "bearer+abcdef0123456789",
        "AK" + "IA" + "IOSFODNN7EXAMPLE",
        "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz0123456789",
        "xo" + "xb-" + "1234567890-abcdefghij",
        "password=hunter2",
        "api_key: abc123",
        "-----BEGIN " + "PRIVATE KEY-----",
    ])
    def test_a_credential_is_never_stored(self, value):
        assert looks_like_secret(value)
        assert extract_tags({"x-agentic-repo": value, "x-agentic-step": "ok"}, {}) == {
            "step": "ok"}

    @pytest.mark.parametrize("value", [
        "github.com/acme/shop", "git@github.com:acme/shop.git", "https://github.com/acme/shop",
        "builder@1.4.0", "planner@1.2.0", "dev@acme.test", "task-runner", "risk-review",
        "3f2b9c3e-8a51-4c1e-9d7a-2b9e0c4d5f61", "9fceb02d0ae598e95dc970b74767f19372d61af8",
        "policy-2026-10-22", "standard", "sk-short", "desk-booking-service-v2",
        # Words that only look like a credential keyword: the body is what decides.
        "Basic analysis", "basic assistant", "bearer analysis", "token: refresh",
        "secret: rotation", "ssh://git@github.com/acme/payments",
        "https://user@github.com/acme/shop", "TASK-1234567890123456789012",
    ])
    def test_ordinary_values_are_kept(self, value):
        assert not looks_like_secret(value)
        assert extract_tags({"x-agentic-repo": value}, {}) == {"repo": value}

    @pytest.mark.parametrize("pad", [230, 240, 246, 250, 254])
    def test_a_credential_straddling_the_cap_is_dropped_not_cut(self, pad):
        # Scanned before the value is cut: otherwise a prefix of the key would be stored.
        key = "sk-proj-" + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ"
        assert extract_tags({"x-agentic-step": "x" * pad + " " + key}, {}) is None

    def test_a_dropped_value_is_counted_by_reason(self):
        before = usage_tags.TAGS_DROPPED.labels(reason="secret")._value.get()
        extract_tags({"x-agentic-repo": "password=hunter2"}, {})
        assert usage_tags.TAGS_DROPPED.labels(reason="secret")._value.get() == before + 1


class TestConfiguration:
    def test_switched_off(self):
        assert extract_tags(FRAMEWORK, _cfg(enabled=False)) is None

    def test_a_custom_allow_list_replaces_the_default(self):
        cfg = _cfg(headers={"X-Cost-Center": "cost_center", "X-Agentic-Step": "step"})
        assert extract_tags({**FRAMEWORK, "x-cost-center": "cc-42"}, cfg) == {
            "cost_center": "cc-42", "step": "implement"}

    def test_an_empty_allow_list_stores_nothing(self):
        assert extract_tags(FRAMEWORK, _cfg(headers={})) is None

    def test_invalid_entries_are_skipped(self):
        cfg = _cfg(headers={"X-Ok": "ok", "Bad Header": "bad", "X-Key": "Not A Key",
                            "X-Num": 3, "X-Upper": "Upper", "": "empty"})
        headers = {"x-ok": "1", "bad header": "2", "x-key": "3", "x-num": "4", "x-upper": "5"}
        assert extract_tags(headers, cfg) == {"ok": "1"}

    def test_no_section_means_the_default(self):
        for absent in ({"billing": "x"}, {"billing": {"metering": None}}, None, []):
            assert extract_tags(FRAMEWORK, absent) == EXPECTED

    def test_a_malformed_section_stores_nothing(self):
        # Fail closed: an operator who wrote something unreadable did not ask for the default.
        for bad in ({"billing": {"metering": {"tags": "on"}}},
                    {"billing": {"metering": {"tags": {"headers": ["X-Agentic-Step"]}}}}):
            assert extract_tags(FRAMEWORK, bad) is None
