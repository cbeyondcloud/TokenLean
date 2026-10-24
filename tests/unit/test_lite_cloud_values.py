"""deploy/lite/cloud: LITE against a managed Postgres and Redis (ST-6 final).

Two files describe a cloud LITE deployment before any chart exists: the proxy container's
environment (`tokenlean-lite.env.template`, usable as it is with any orchestrator's env file)
and the values a Helm chart will read (`values/managed.yaml`, consumed by the M3 chart). They
must agree with each other, with the local stack the smoke proves, and with the LITE profile.
"""
import os
import re

import pytest
import yaml

import config_loader

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_LITE = os.path.join(_REPO, "deploy", "lite")
_CLOUD = os.path.join(_LITE, "cloud")
_ENV = os.path.join(_CLOUD, "tokenlean-lite.env.template")
_VALUES = os.path.join(_CLOUD, "values", "managed.yaml")
_COMPOSE = os.path.join(_LITE, "local", "docker-compose.lite.yml")

# Local-stack only: the mock provider, the in-stack Qdrant/Langfuse wiring and the key file
# that up writes. Everything else the local proxy gets, the cloud proxy gets too.
LOCAL_ONLY = {"LLM_KEY_LITEMOCK", "QDRANT_URL", "QDRANT_API_KEY", "QDRANT_LOCAL_NOAUTH",
              "LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
              "OTEL_EXPORTER_OTLP_ENDPOINT", "CORS_ORIGINS"}
SECRETS = {"DATABASE_URL", "REDIS_PASSWORD", "METRICS_SCRAPE_TOKEN", "REDIS_CA_CERT"}


def _env():
    out = {}
    with open(_ENV, encoding="utf-8") as fh:
        for line in fh.read().splitlines():
            if line and not line.startswith("#"):
                key, _, value = line.partition("=")
                out[key] = value
    return out


def _values():
    with open(_VALUES, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _compose_proxy_env():
    with open(_COMPOSE, encoding="utf-8") as fh:
        return yaml.safe_load(fh)["services"]["proxy"]["environment"]


def test_the_env_template_covers_what_the_local_proxy_gets():
    missing = set(_compose_proxy_env()) - LOCAL_ONLY - set(_env())
    assert not missing, sorted(missing)


def test_no_value_is_filled_in_for_a_secret_or_a_provider_key():
    env = _env()
    for key in SECRETS | {k for k in env if k.startswith("LLM_KEY_")}:
        assert env.get(key, "") == "" or "${" in env[key], key


def test_the_managed_services_are_reached_over_tls():
    env = _env()
    assert env["REDIS_URL"].startswith("rediss://")
    values = _values()
    assert values["postgres"]["sslmode"] in ("require", "verify-ca", "verify-full")
    assert values["redis"]["tls"] is True
    # The DSN the chart builds carries the same sslmode (documented next to DATABASE_URL).
    with open(_ENV, encoding="utf-8") as fh:
        assert "sslmode=require" in fh.read()


def test_the_env_template_applies_the_lite_profile_and_postgres_keys(monkeypatch):
    env = _env()
    overlays = env["CONFIG_OVERLAYS"].split(",")
    assert overlays[0].endswith("/agentic.yaml")
    assert env["STORAGE_BACKEND"] == "local"
    assert env["METRICS_ALLOW_UNAUTHENTICATED"] == "false"
    # The profile, as the cloud proxy loads it, keeps proxy keys in Postgres.
    monkeypatch.chdir(_REPO)
    monkeypatch.setenv("CONFIG_LOCAL_PATH", os.path.join(_REPO, "config", "config.yaml.template"))
    monkeypatch.setenv("CONFIG_OVERLAYS", os.path.join(_LITE, "config", "agentic.yaml"))
    monkeypatch.delenv("CONFIG_GCS_BUCKET", raising=False)
    monkeypatch.setattr(config_loader, "_config", {})
    cfg = config_loader.load_config(required=True)
    assert cfg["key_store"]["backend"] == "postgres"
    assert _values()["proxyKeys"]["backend"] == "postgres"


def test_values_hold_secret_references_never_secret_values():
    values = _values()

    def walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                yield from walk(v, f"{path}.{k}" if path else k)
        else:
            yield path, node

    for path, value in walk(values):
        leaf = path.rsplit(".", 1)[-1].lower()
        if re.search(r"password|secret|token|apikey|api_key", leaf) and not leaf.endswith(
                ("secretname", "secretkey", "existingsecret")):
            assert value in (None, "", {}), f"{path} holds a value; use a secret reference"
    for name in ("postgres", "redis", "proxyKeys", "providerKeys", "metrics"):
        assert "existingSecret" in values[name], name


def test_values_name_the_profile_and_the_dashboards_the_local_stack_uses():
    values = _values()
    assert values["config"]["profile"] == "deploy/lite/config/agentic.yaml"
    assert values["dashboards"]["provisioning"] == "deploy/lite/grafana/provisioning"
    assert values["image"]["tag"] and values["image"]["tag"] != "latest"


@pytest.mark.parametrize("path", [_ENV, _VALUES])
def test_cloud_files_are_lf(path):
    with open(path, "rb") as fh:
        assert b"\r\n" not in fh.read()
