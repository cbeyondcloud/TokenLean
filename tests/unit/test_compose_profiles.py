"""Langfuse runs only with the `tracing` compose profile: the OSS template ships tracing off.

The core stack runs unprofiled; Prometheus, Grafana and Jaeger are the `observability` profile
and Langfuse is `tracing`, which the local deploys add when the config turns Langfuse tracing on
(scripts/local/compose-profiles.sh). A service that is on must never REQUIRE a dependency that can
be off: Compose then refuses `up`, `down` and `restart` whenever that dependency's profile is off.
"""
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
SERVICES = yaml.safe_load((_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
CORE = ("redis", "postgres", "qdrant", "llmlingua", "routellm", "tika", "proxy")
OBSERVABILITY = ("prometheus", "grafana", "jaeger")


def _profiles(name: str) -> list:
    return SERVICES[name].get("profiles") or []


def test_langfuse_runs_only_with_the_tracing_profile():
    assert _profiles("langfuse") == ["tracing"]


def test_the_core_stack_runs_without_a_profile():
    assert {name: _profiles(name) for name in CORE} == {name: [] for name in CORE}


def test_prometheus_grafana_and_jaeger_stay_the_observability_profile():
    assert {n: _profiles(n) for n in OBSERVABILITY} == {n: ["observability"] for n in OBSERVABILITY}


def test_the_proxy_starts_without_langfuse():
    assert SERVICES["proxy"]["depends_on"]["langfuse"] == {
        "condition": "service_started", "required": False}


def test_no_service_requires_a_dependency_its_profiles_can_leave_off():
    for name, service in SERVICES.items():
        depends = service.get("depends_on") or {}
        if isinstance(depends, list):
            depends = dict.fromkeys(depends)
        mine = set(_profiles(name))
        for dependency, spec in depends.items():
            theirs = set(_profiles(dependency))
            # On whenever `name` is: unprofiled, or every profile that starts `name` starts it too.
            always_with_it = not theirs or (mine and mine <= theirs)
            if not always_with_it:
                assert (spec or {}).get("required") is False, (
                    f"{name} requires {dependency}, which its profiles {sorted(theirs)} can "
                    "leave off: Compose would then refuse every command")
