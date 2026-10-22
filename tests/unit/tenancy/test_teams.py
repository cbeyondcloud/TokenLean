"""Teams under a tenant (ST-1): where a team's settings come from, and what its config
overlay may change.

A team's block is ``teams.<team>`` in the effective config (the platform file, or the
tenant's own ``tenant_configs`` overrides), with the operator's ``tenants.<id>.teams.<team>``
merged over it. The overlay under ``config`` is deep-merged over the tenant's config for
that team's requests, with the CONFIG_OVERLAYS rules: mappings merge, anything else
replaces, a null changes nothing. It may change how requests are served (``groups``,
``proxy``) and nothing else: who pays, how much, and what is recorded stay the tenant's.
"""
import copy

from tenancy.teams import (
    TEAM_OVERLAY_SECTIONS,
    apply_team_overlay,
    team_monthly_spend_cap,
    team_settings,
    valid_team_name,
)


def _cfg():
    return {
        "groups": {"G5_cache": {"enabled": True, "ttl": 60}, "G6_routing": {"enabled": False}},
        "proxy": {"default_model": "model-a", "port": 4000},
        "limits": {"monthly_spend_cap_usd": 100},
        "teams": {
            "payments": {
                "monthly_spend_cap_usd": 25,
                "config": {"groups": {"G5_cache": {"ttl": 5}},
                           "proxy": {"default_model": "model-b"}},
            },
        },
    }


class TestTeamNames:
    def test_slug_names_are_valid(self):
        for name in ("payments", "team-alpha", "Team_1", "a.b", "x" * 64):
            assert valid_team_name(name), name

    def test_other_names_are_not(self):
        for name in ("", " ", "x" * 65, "has space", "colon:seg", "slash/x", "naïve", None, 7,
                     "default"):
            assert not valid_team_name(name), name


class TestTeamSettings:
    def test_reads_the_teams_block(self):
        assert team_settings(_cfg(), "acme", "payments")["monthly_spend_cap_usd"] == 25

    def test_unknown_team_has_none(self):
        assert team_settings(_cfg(), "acme", "search") == {}

    def test_the_default_team_never_has_settings(self):
        cfg = _cfg()
        cfg["teams"]["default"] = {"monthly_spend_cap_usd": 1}
        assert team_settings(cfg, "acme", "default") == {}
        assert team_settings(cfg, "acme", "") == {}

    def test_operator_tenant_block_wins(self):
        cfg = _cfg()
        cfg["tenants"] = {"acme": {"teams": {"payments": {"monthly_spend_cap_usd": 7}}}}
        got = team_settings(cfg, "acme", "payments")
        assert got["monthly_spend_cap_usd"] == 7
        assert got["config"]["proxy"]["default_model"] == "model-b"   # merged, not replaced
        assert team_settings(cfg, "other", "payments")["monthly_spend_cap_usd"] == 25

    def test_operator_block_alone_is_enough(self):
        cfg = {"tenants": {"acme": {"teams": {"ops": {"monthly_spend_cap_usd": 3}}}}}
        assert team_settings(cfg, "acme", "ops") == {"monthly_spend_cap_usd": 3}

    def test_malformed_blocks_read_as_none(self):
        for bad in ({"teams": ["payments"]}, {"teams": {"payments": "x"}},
                    {"tenants": {"acme": "x"}}, {"tenants": {"acme": {"teams": 3}}}, None, []):
            assert team_settings(bad, "acme", "payments") == {}


class TestSpendCap:
    def test_positive_number(self):
        assert team_monthly_spend_cap(_cfg(), "acme", "payments") == 25.0

    def test_absent_zero_negative_or_garbage_means_no_cap(self):
        for value in (None, 0, -5, "lots", True, [], {}):
            cfg = {"teams": {"payments": {"monthly_spend_cap_usd": value}}}
            assert team_monthly_spend_cap(cfg, "acme", "payments") is None, value
        assert team_monthly_spend_cap(_cfg(), "acme", "search") is None

    def test_numeric_string_is_read(self):
        cfg = {"teams": {"payments": {"monthly_spend_cap_usd": "12.5"}}}
        assert team_monthly_spend_cap(cfg, "acme", "payments") == 12.5


class TestOverlay:
    def test_merged_over_the_tenant_config(self):
        cfg = _cfg()
        merged, overlay = apply_team_overlay(cfg, "acme", "payments")
        assert merged["groups"]["G5_cache"] == {"enabled": True, "ttl": 5}   # sibling kept
        assert merged["proxy"] == {"default_model": "model-b", "port": 4000}
        assert overlay == {"groups": {"G5_cache": {"ttl": 5}},
                           "proxy": {"default_model": "model-b"}}

    def test_inputs_are_not_modified(self):
        cfg = _cfg()
        before = copy.deepcopy(cfg)
        apply_team_overlay(cfg, "acme", "payments")
        assert cfg == before

    def test_no_overlay_returns_the_same_object(self):
        cfg = _cfg()
        for team in ("search", "default", ""):
            merged, overlay = apply_team_overlay(cfg, "acme", team)
            assert merged is cfg and overlay == {}
        del cfg["teams"]["payments"]["config"]
        merged, overlay = apply_team_overlay(cfg, "acme", "payments")
        assert merged is cfg and overlay == {}

    def test_only_serving_sections_may_change(self):
        assert set(TEAM_OVERLAY_SECTIONS) == {"groups", "proxy"}
        cfg = _cfg()
        cfg["teams"]["payments"]["config"].update({
            "limits": {"monthly_spend_cap_usd": 10_000},       # the tenant's cap
            "rate_limit": {"spend_cap": {"enabled": False}},   # the gates
            "billing": {"metering": {"tags": {"enabled": False}}},
            "teams": {"payments": {"monthly_spend_cap_usd": 0}},
            "tenants": {"acme": {}},
            "observability": {"echo_sent_prompt": True},
            "providers": [{"name": "elsewhere", "base_url": "https://example.invalid"}],
        })
        merged, overlay = apply_team_overlay(cfg, "acme", "payments")
        assert set(overlay) == {"groups", "proxy"}
        assert merged["limits"] == {"monthly_spend_cap_usd": 100}
        assert "rate_limit" not in merged and "billing" not in merged
        assert "observability" not in merged and "providers" not in merged
        assert merged["teams"] == cfg["teams"]

    def test_null_changes_nothing_and_lists_replace(self):
        cfg = _cfg()
        cfg["groups"]["G6_routing"]["rules"] = [{"id": "a"}]
        cfg["teams"]["payments"]["config"] = {"groups": {"G5_cache": None,
                                                         "G6_routing": {"rules": [{"id": "b"}]}}}
        merged, _ = apply_team_overlay(cfg, "acme", "payments")
        assert merged["groups"]["G5_cache"] == {"enabled": True, "ttl": 60}
        assert merged["groups"]["G6_routing"]["rules"] == [{"id": "b"}]

    def test_a_malformed_overlay_is_ignored(self):
        cfg = _cfg()
        cfg["teams"]["payments"]["config"] = ["groups"]
        merged, overlay = apply_team_overlay(cfg, "acme", "payments")
        assert merged is cfg and overlay == {}
        cfg["teams"]["payments"]["config"] = {"groups": "off"}
        merged, overlay = apply_team_overlay(cfg, "acme", "payments")
        assert merged is cfg and overlay == {}
