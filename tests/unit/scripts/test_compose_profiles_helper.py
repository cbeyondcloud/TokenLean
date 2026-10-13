"""scripts/local/compose-profiles.sh chooses the optional compose profiles the local deploys start.

Langfuse is the `tracing` profile, and the OSS template ships Langfuse tracing off, so a default
stack runs none of it. The helper adds `tracing` when the operator's config turns it on
(G18_observability.langfuse_enabled, in config.yaml, a params file or one tenant's overlay), or
with --with-tracing; and `observability` with --with-grafana. It merges into COMPOSE_PROFILES
without dropping what the caller or .env set. It runs here in bash against a scratch repo.
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
LOCAL = _ROOT / "scripts" / "local"
HELPER = LOCAL / "compose-profiles.sh"
_ON = "groups:\n  G18_observability:\n    enabled: true\n    langfuse_enabled: true\n"
_OFF = "groups:\n  G18_observability:\n    enabled: true\n    langfuse_enabled: false\n"


def _bash():
    """Git's bash on Windows (a PATH `bash` may be the WSL launcher), else PATH bash."""
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            candidate = Path(git).resolve().parents[1] / "bin" / "bash.exe"
            if candidate.exists():
                return str(candidate)
    return shutil.which("bash")


pytestmark = pytest.mark.skipif(_bash() is None, reason="needs bash")


def _posix(path) -> str:
    """A path bash can use: Git's bash wants /c/... for a Windows drive."""
    p = Path(path).resolve().as_posix()
    if os.name == "nt" and len(p) > 1 and p[1] == ":":
        p = "/" + p[0].lower() + p[2:]
    return p


def _shims(tmp_path, body=None) -> Path:
    """`python3` and `python` first on PATH: the interpreter running these tests (so the answer
    does not depend on the machine's Pythons, or Windows' Store stub), or `body` instead."""
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir(exist_ok=True)
    text = body or f'#!/usr/bin/env bash\nexec "{_posix(sys.executable)}" "$@"\n'
    for name in ("python3", "python"):
        (shim_dir / name).write_text(text, encoding="utf-8", newline="\n")
        (shim_dir / name).chmod(0o755)
    return shim_dir


def _run(tmp_path, lines, *, config=None, params=None, env=None, shim_body=None):
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True, exist_ok=True)
    if config is not None:
        (repo / "config" / "config.yaml").write_text(config, encoding="utf-8", newline="\n")
    for name, text in (params or {}).items():
        (repo / "config" / "params").mkdir(exist_ok=True)
        (repo / "config" / "params" / name).write_text(text, encoding="utf-8", newline="\n")
    script = "\n".join([
        "set -euo pipefail",
        f'export PATH="{_posix(_shims(tmp_path, shim_body))}:$PATH"',
        f'ROOT="{_posix(repo)}"',
        f'source "{_posix(HELPER)}"',
        *lines,
    ])
    clean = {k: v for k, v in os.environ.items() if k not in (
        "COMPOSE_PROFILES", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "PYTHONPATH", "LF_ON")}
    clean.update(env or {})
    r = subprocess.run([_bash(), "-c", script], capture_output=True, encoding="utf-8",
                       timeout=60, env=clean, cwd=str(repo), check=False)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout + r.stderr


def _wanted(tmp_path, **kw):
    test = 'if compose_tracing_wanted "$ROOT"; then echo ANSWER=on; else echo ANSWER=off; fi'
    out = _run(tmp_path, [test], **kw)
    return re.search(r"ANSWER=(on|off)", out).group(1) == "on", out


def _profiles_after(tmp_path, lines, **kw):
    out = _run(tmp_path, [*lines, 'echo "PROFILES=[${COMPOSE_PROFILES-<unset>}]"'], **kw)
    return re.search(r"PROFILES=\[(.*)\]", out).group(1), out


# ── Whether the config turns Langfuse tracing on ─────────────────────────────

def test_a_config_that_turns_tracing_on_wants_langfuse(tmp_path):
    assert _wanted(tmp_path, config=_ON)[0]


def test_the_shipped_template_does_not(tmp_path):
    template = (_ROOT / "config" / "config.yaml.template").read_text(encoding="utf-8")
    assert not _wanted(tmp_path, config=template)[0]


@pytest.mark.parametrize("config", [None, "groups: {}\n", _OFF], ids=["no file", "no key", "off"])
def test_without_the_setting_on_it_does_not_and_says_nothing(tmp_path, config):
    wanted, out = _wanted(tmp_path, config=config)
    assert not wanted and "--with-tracing" not in out


def test_a_base_enabled_false_does_not_hide_it(tmp_path):
    # G18's `enabled` is a toggle a tenant can flip in the portal; only langfuse_enabled decides.
    config = "groups:\n  G18_observability:\n    enabled: false\n    langfuse_enabled: true\n"
    assert _wanted(tmp_path, config=config)[0]


def test_one_tenant_turning_it_on_is_enough(tmp_path):
    config = _OFF + ("tenants:\n  ACME-PRD-01:\n    groups:\n      G18_observability:\n"
                     "        langfuse_enabled: true\n")
    assert _wanted(tmp_path, config=config)[0]


def test_params_files_merge_in_name_order_as_the_proxy_merges_them(tmp_path):
    base = _OFF + "params_dir: config/params\n"
    assert _wanted(tmp_path / "on", config=base, params={"a.yaml": _ON})[0]
    assert not _wanted(tmp_path / "later_off", config=base,
                       params={"a.yaml": _ON, "b.yaml": _OFF})[0]
    assert not _wanted(tmp_path / "template", config=base, params={"a.yaml.template": _ON})[0]


def test_a_placeholder_is_expanded_from_the_environment_as_the_proxy_does(tmp_path):
    config = 'groups:\n  G18_observability:\n    langfuse_enabled: "${LF_ON:-}"\n'
    assert _wanted(tmp_path / "set", config=config, env={"LF_ON": "1"})[0]
    assert not _wanted(tmp_path / "unset", config=config)[0]


@pytest.mark.parametrize("case", ["no interpreter", "no PyYAML", "bad YAML"])
def test_when_it_cannot_tell_it_starts_no_langfuse_and_names_with_tracing(tmp_path, case):
    kw = {"config": _ON}
    if case == "no interpreter":
        kw["shim_body"] = "#!/usr/bin/env bash\nexit 9\n"
    elif case == "no PyYAML":
        fake = tmp_path / "fake_modules"
        fake.mkdir()
        (fake / "yaml.py").write_text('raise ImportError("PyYAML is not installed")\n',
                                      encoding="utf-8")
        kw["env"] = {"PYTHONPATH": str(fake)}
    else:
        kw["config"] = "groups: [this is not\n"
    wanted, out = _wanted(tmp_path, **kw)
    assert not wanted and "--with-tracing" in out


# ── What the local deploys start ──────────────────────────────────────────────

def test_with_tracing_starts_langfuse_without_reading_the_config(tmp_path):
    profiles, out = _profiles_after(tmp_path, ['compose_profiles_apply "$ROOT" false true'],
                                    config="groups: [this is not\n",
                                    env={"LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk"})
    assert profiles == "tracing" and "--with-tracing" not in out


def test_the_config_alone_starts_langfuse(tmp_path):
    assert _profiles_after(tmp_path, ['compose_profiles_apply "$ROOT" false false'],
                           config=_ON)[0] == "tracing"


def test_with_grafana_adds_observability_and_a_default_stack_adds_nothing(tmp_path):
    assert _profiles_after(tmp_path / "g", ['compose_profiles_apply "$ROOT" true false'],
                           config=_OFF)[0] == "observability"
    assert _profiles_after(tmp_path / "none", ['compose_profiles_apply "$ROOT" false false'],
                           config=_OFF)[0] == "<unset>"


def test_langfuse_without_its_keys_is_called_out(tmp_path):
    out = _run(tmp_path, ['compose_profiles_apply "$ROOT" false true'])
    assert "LANGFUSE_PUBLIC_KEY" in out


@pytest.mark.parametrize("before, add, after", [
    ("portal", "tracing", "portal,tracing"),
    ("tracing", "tracing", "tracing"),
    (" observability , tracing", "observability", "observability,tracing"),
    ("*", "tracing", "*,tracing"),
    (None, "", "<unset>"),
], ids=["keeps-the-caller-s", "no-duplicate", "trims", "star-not-expanded", "never-empty"])
def test_profiles_are_merged_never_overwritten(tmp_path, before, add, after):
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "a-file-a-glob-would-match").write_text("x", encoding="utf-8")
    lines = ([f'export COMPOSE_PROFILES="{before}"'] if before is not None else [])
    lines.append(f"compose_profiles_add {add}")
    assert _profiles_after(tmp_path, lines)[0] == after


def test_star_counts_as_every_profile(tmp_path):
    out = _run(tmp_path, ['export COMPOSE_PROFILES="*"',
                          'compose_profiles_has tracing && echo HAS=yes || echo HAS=no'])
    assert "HAS=yes" in out


# ── The scripts use it ────────────────────────────────────────────────────────

def _text(name):
    return (LOCAL / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["deploy-local.sh", "start-local.sh"])
def test_the_local_deploys_choose_profiles_before_starting(name):
    text = _text(name)
    assert "--with-tracing)" in text and "export COMPOSE_PROFILES=" not in text
    source = text.index('compose-profiles.sh"')
    apply = text.index('compose_profiles_apply "$REPO_ROOT" "$WITH_GRAFANA" "$WITH_TRACING"')
    assert source < apply < text.index("$DC up -d")


@pytest.mark.parametrize("name", ["deploy-local.sh", "start-local.sh"])
def test_help_prints_the_whole_usage_before_touching_env(tmp_path, name):
    # In a scratch repo: run before the flags, the .env step would fill (write) a missing login.
    local = tmp_path / "scripts" / "local"
    local.mkdir(parents=True)
    for helper in (name, "fill-local-env.sh", "compose-profiles.sh"):
        if (LOCAL / helper).exists():
            shutil.copy(LOCAL / helper, local / helper)
    env_file = tmp_path / ".env"
    env_file.write_text("REDIS_PASSWORD=\n", encoding="utf-8", newline="\n")
    r = subprocess.run([_bash(), _posix(local / name), "--help"], capture_output=True,
                       encoding="utf-8", timeout=60, cwd=str(tmp_path), check=False)
    assert r.returncode == 0 and "--with-tracing" in r.stdout
    assert r.stdout.strip().splitlines()[-1].startswith("# ===")     # the whole usage block
    assert env_file.read_text(encoding="utf-8") == "REDIS_PASSWORD=\n"


def test_stop_local_stops_every_profile():
    text = _text("stop-local.sh")
    assert "$DC --profile '*' down" in text and not re.search(r"^\s*\$DC down", text, re.M)


def test_build_local_prints_langfuse_only_with_tracing_and_on_its_port():
    text = _text("build-local.sh")
    assert "3001" not in text and "compose_profiles_apply" in text
    assert re.search(r"compose_profiles_has tracing.*\n.*localhost:3100", text)


def test_precheck_needs_langfuse_s_port_only_when_tracing_is_on():
    text = _text("precheck-local-deployment.sh")
    ports = re.search(r"^REQUIRED_PORTS=\(([^)]*)\)", text, re.M).group(1).split()
    assert "3100" not in ports
    assert re.search(r'compose_tracing_wanted "\$REPO_ROOT".*\n.*REQUIRED_PORTS\+=\(3100\)', text)


def test_the_post_deploy_check_requires_langfuse_when_tracing_is_on():
    text = _text("post-deploy-check-local.sh")
    assert re.search(r'compose_profiles_has tracing \|\| compose_tracing_wanted "\$REPO_ROOT"', text)
    assert 'CHECKS+=("langfuse:true")' in text
