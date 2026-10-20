"""deploy/lite: the local LITE stack's files hold their contract.

What a broken file here costs is a laptop that does not come up (or comes up with the full
optimisation set on), across Windows, macOS and Linux, Docker or Podman. These checks run
without a container engine; the stack itself is exercised by deploy/lite/local/smoke.*.
"""
import os
import re
import subprocess

import pytest
import yaml

import config_loader

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_LITE = os.path.join(_REPO, "deploy", "lite")
_LOCAL = os.path.join(_LITE, "local")
_COMPOSE = os.path.join(_LOCAL, "docker-compose.lite.yml")


def _git(*args):
    """Run git in the repo, or skip: the OSS-tree gate and source tarballs run these tests
    without git or outside a work tree."""
    import shutil
    if not shutil.which("git"):
        pytest.skip("git not available")
    inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=_REPO,
                            capture_output=True, text=True)
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        pytest.skip("not a git work tree")
    return subprocess.run(["git", *args], cwd=_REPO, capture_output=True, text=True)


def _compose():
    with open(_COMPOSE, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _read(path, mode="r"):
    with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as fh:
        return fh.read()


def test_deploy_profile_is_a_byte_copy_of_the_shipped_profile():
    assert _read(os.path.join(_LITE, "config", "agentic.yaml"), "rb") == \
        _read(os.path.join(_REPO, "config", "profiles", "agentic.yaml"), "rb")


def test_every_image_is_pinned_to_an_exact_release():
    for name, svc in _compose()["services"].items():
        image = svc.get("image", "")
        assert image, name
        if image.startswith("${TOKENLEAN_IMAGE"):
            continue
        repo, _, tag = image.rpartition(":")
        assert repo and tag and tag != "latest", f"{name}: {image}"
        # an exact release, not a floating major/minor such as `7.2-alpine` or `pg15`
        assert re.match(r"^v?\d+\.\d+\.\d+", tag), f"{name}: {image}"


def test_proxy_is_built_from_the_oss_dockerfile_unless_an_image_is_given():
    proxy = _compose()["services"]["proxy"]
    assert proxy["image"].startswith("${TOKENLEAN_IMAGE:-")
    assert proxy["build"]["context"].replace("\\", "/").endswith("src/proxy")
    assert proxy["build"].get("dockerfile", "Dockerfile") == "Dockerfile"


def test_proxy_depends_on_postgres_and_redis_only():
    proxy = _compose()["services"]["proxy"]
    assert set(proxy["depends_on"]) == {"postgres", "redis"}
    assert proxy["environment"]["CONFIG_OVERLAYS"].startswith("${LITE_CONFIG_OVERLAYS:-")


def test_optional_services_sit_in_their_profiles():
    services = _compose()["services"]
    assert services["qdrant"]["profiles"] == ["qdrant"]
    assert services["langfuse"]["profiles"] == ["langfuse"]
    assert services["mock-llm"]["profiles"] == ["mock"]
    for core in ("proxy", "postgres", "redis"):
        assert "profiles" not in services[core], core


def test_published_ports_are_bound_to_the_configured_address():
    published = {}
    for name, svc in _compose()["services"].items():
        for port in svc.get("ports", []):
            assert str(port).startswith("${LITE_BIND:-127.0.0.1}:"), f"{name}: {port}"
            published[name] = port
    assert set(published) == {"proxy", "langfuse"}   # the databases are never published


def test_no_fixed_container_names():
    """A fixed container_name collides with a second stack (the full TokenLean compose
    runs token-opt-*); the compose project name keeps the LITE stack apart."""
    compose = _compose()
    assert compose["name"] == "tokenlean-lite"
    assert not [n for n, s in compose["services"].items() if "container_name" in s]


@pytest.mark.parametrize("script", ["up", "down", "smoke"])
def test_each_script_ships_for_bash_and_powershell(script):
    sh = _read(os.path.join(_LOCAL, f"{script}.sh"), "rb")
    ps1 = _read(os.path.join(_LOCAL, f"{script}.ps1"), "rb")
    assert sh.startswith(b"#!/usr/bin/env bash")
    assert b"\r\n" not in sh                       # bash on macOS/Linux rejects CRLF
    ps1.decode("ascii")                              # Windows PowerShell 5.1 reads ANSI
    assert b"_lib.sh" in sh and b"_lib.ps1" in ps1   # the shared helpers below


@pytest.mark.parametrize("script", ["up.sh", "down.sh", "smoke.sh"])
def test_bash_scripts_are_committed_executable(script):
    """A checkout on macOS/Linux must be able to run ./up.sh as is (mode 100755 in git;
    files written on Windows otherwise land as 100644)."""
    rel = f"deploy/lite/local/{script}"
    out = _git("ls-files", "-s", rel).stdout
    if not out:
        pytest.skip(f"{rel} is not committed yet")
    assert out.split()[0] == "100755", out


def test_shared_helpers_drive_any_engine_through_compose():
    sh = _read(os.path.join(_LOCAL, "_lib.sh"), "rb")
    ps1 = _read(os.path.join(_LOCAL, "_lib.ps1"), "rb")
    assert b"\r\n" not in sh
    ps1.decode("ascii")
    assert b'"${CONTAINER_ENGINE:-docker}"' in sh    # docker or podman, never hard-wired
    assert b"$env:CONTAINER_ENGINE" in ps1
    for body in (sh, ps1):
        assert b" compose " in body                  # `<engine> compose`, not docker-compose
        assert b"docker-compose " not in body


def test_env_template_is_tracked_and_the_real_env_is_ignored():
    def ignored(rel):
        return _git("check-ignore", "-q", rel).returncode == 0

    assert not ignored("deploy/lite/local/.env.lite.template")
    assert ignored("deploy/lite/local/.env.lite")
    assert ignored("deploy/lite/local/.state/local-keys.json")


def test_env_template_generated_values_are_empty():
    lines = _read(os.path.join(_LOCAL, ".env.lite.template")).splitlines()
    values = dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))
    for key in ("TOKENLEAN_API_KEY", "TOKENLEAN_ADMIN_KEY", "DB_PASSWORD", "REDIS_PASSWORD",
                "QDRANT_API_KEY", "METRICS_SCRAPE_TOKEN", "LANGFUSE_PUBLIC_KEY",
                "LANGFUSE_SECRET_KEY", "LANGFUSE_NEXTAUTH_SECRET", "LANGFUSE_SALT",
                "LANGFUSE_INIT_USER_PASSWORD"):
        assert values.get(key) == "", key
    assert not [k for k, v in values.items() if k.startswith("LLM_KEY_") and v]


def _effective(monkeypatch, *overlays):
    monkeypatch.chdir(_REPO)
    monkeypatch.setenv("CONFIG_LOCAL_PATH", os.path.join(_REPO, "config", "config.yaml.template"))
    monkeypatch.setenv("CONFIG_OVERLAYS", ",".join(overlays))
    monkeypatch.delenv("CONFIG_GCS_BUCKET", raising=False)
    monkeypatch.setattr(config_loader, "_config", {})
    return config_loader.load_config(required=True)


def test_mock_overlay_routes_everything_to_the_mock_provider(monkeypatch):
    cfg = _effective(monkeypatch, os.path.join(_LITE, "config", "agentic.yaml"),
                     os.path.join(_LOCAL, "mock", "mock-provider.yaml"))
    assert [p["name"] for p in cfg["providers"]] == ["litemock"]
    assert cfg["proxy"]["default_provider"] == "litemock"
    assert cfg["groups"]["G1_compression"]["enabled"] is False   # the LITE profile still applies


def test_langfuse_overlay_only_turns_tracing_on(monkeypatch):
    base = _effective(monkeypatch, os.path.join(_LITE, "config", "agentic.yaml"))
    import copy
    base = copy.deepcopy(base)
    traced = _effective(monkeypatch, os.path.join(_LITE, "config", "agentic.yaml"),
                        os.path.join(_LITE, "config", "langfuse.yaml"))
    assert traced["groups"]["G18_observability"]["langfuse_enabled"] is True
    traced["groups"]["G18_observability"]["langfuse_enabled"] = False
    assert traced == base


# ── The scripts' helpers, run for real with a stub engine (skipped without the shell) ──

def _bash():
    import shutil
    import sys
    if sys.platform == "win32":
        git_bash = r"C:\Program Files\Git\bin\bash.exe"
        if os.path.exists(git_bash):
            return git_bash
        found = shutil.which("bash")
        # System32\bash.exe is WSL: it would run in another filesystem namespace
        return found if found and "system32" not in found.lower() else None
    return shutil.which("bash")


def _powershell():
    import shutil
    return shutil.which("powershell") or shutil.which("pwsh")


def _run_bash(script, env_extra=None):
    bash = _bash()
    if not bash:
        pytest.skip("bash not available")
    env = dict(os.environ, **(env_extra or {}))
    out = subprocess.run([bash, "-c", script], cwd=_LOCAL, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_bash_helpers_pin_the_project_and_ignore_a_foreign_compose_project(tmp_path):
    """The full stack's .env sets COMPOSE_PROJECT_NAME=token-opt. Exported in the shell, or
    written into .env.lite, it must never steer `down --purge` onto that stack."""
    env_file = tmp_path / "env.lite"
    env_file.write_text("COMPOSE_PROJECT_NAME=token-opt\nCOMPOSE_FILE=other.yml\n"
                        "COMPOSE_PROFILES=x\nLITE_PROXY_PORT=4199\n", encoding="utf-8")
    out = _run_bash(
        '. ./_lib.sh; LITE_ENV_FILE="$1"; LITE_ENGINE=echo; lite_load_env; '
        'echo "project=${COMPOSE_PROJECT_NAME-unset} file=${COMPOSE_FILE-unset} '
        'profiles=${COMPOSE_PROFILES-unset} port=$LITE_PROXY_PORT"; lite_compose down'
        .replace('"$1"', f'"{env_file.as_posix()}"'),
        {"COMPOSE_PROJECT_NAME": "token-opt", "COMPOSE_FILE": "x.yml"})
    assert "project=unset file=unset profiles=unset port=4199" in out
    assert "compose -p tokenlean-lite -f " in out


def test_bash_fill_handles_a_crlf_env_file(tmp_path):
    """.env.lite edited in a Windows editor has CRLF line ends: generated values must still
    be filled, and no value may keep a trailing CR."""
    env_file = tmp_path / "env.lite"
    env_file.write_bytes(b"LITE_TENANT_ID=T1\r\nDB_PASSWORD=\r\nREDIS_PASSWORD=keep\r\n")
    _run_bash(f'. ./_lib.sh; LITE_ENV_FILE="{env_file.as_posix()}"; lite_normalize_env_file; '
              'lite_fill DB_PASSWORD abc; lite_fill REDIS_PASSWORD xyz; lite_fill NEW_KEY n1')
    data = env_file.read_bytes()
    assert b"\r" not in data
    assert data.decode().splitlines() == ["LITE_TENANT_ID=T1", "DB_PASSWORD=abc",
                                          "REDIS_PASSWORD=keep", "NEW_KEY=n1"]


@pytest.mark.parametrize("script", ["up.sh", "down.sh", "smoke.sh"])
def test_bash_help_prints_only_the_header(script):
    out = _run_bash(f"./{script} --help" if script != "smoke.sh" else
                    ". ./_lib.sh; lite_usage smoke.sh")
    assert out.strip()
    assert "set -euo" not in out
    assert not [line for line in out.splitlines() if line.startswith("#")]


def test_up_sh_writes_secrets_owner_only():
    text = _read(os.path.join(_LOCAL, "up.sh"))
    first_write = text.index('cp "$LITE_DIR/.env.lite.template"')
    assert text.index("umask 077") < first_write


def test_smoke_sh_keeps_the_key_off_the_command_line():
    """Arguments are visible to every user of the machine (ps); headers go in a 0600 file."""
    text = _read(os.path.join(_LOCAL, "smoke.sh"))
    assert "$TOKENLEAN_API_KEY" not in "".join(
        line for line in text.splitlines() if line.lstrip().startswith("curl")
        or line.lstrip().startswith("-H"))
    assert "-H @" in text


def test_powershell_helpers_pin_the_project_and_ignore_a_foreign_compose_project(tmp_path):
    ps = _powershell()
    if not ps:
        pytest.skip("PowerShell not available")
    env_file = tmp_path / "env.lite"
    env_file.write_text("COMPOSE_PROJECT_NAME=token-opt\nLITE_PROXY_PORT=4199\n",
                        encoding="utf-8")
    out_file = tmp_path / "args.txt"
    script = (
        f". '{os.path.join(_LOCAL, '_lib.ps1')}'; "
        f"$LiteEnvFile = '{env_file}'; Import-LiteEnv; "
        f"function Invoke-FakeEngine {{ ($args -join ' ') | Set-Content -Path '{out_file}' }}; "
        "$LiteEngine = 'Invoke-FakeEngine'; $null = Invoke-LiteCompose @('down'); "
        "Write-Output ('project=' + [string]$env:COMPOSE_PROJECT_NAME + "
        "' port=' + $env:LITE_PROXY_PORT)"
    )
    env = dict(os.environ, COMPOSE_PROJECT_NAME="token-opt")
    out = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", script],
                         env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "project= port=4199" in out.stdout
    assert out_file.read_text().startswith("compose -p tokenlean-lite -f ")


def test_mock_and_optional_services_have_healthchecks():
    services = _compose()["services"]
    for name in ("mock-llm", "qdrant", "langfuse"):
        assert services[name].get("healthcheck", {}).get("test"), name
