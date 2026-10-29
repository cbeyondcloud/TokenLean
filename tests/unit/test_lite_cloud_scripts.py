"""deploy/lite/cloud: install / upgrade / uninstall / smoke, for bash and PowerShell (ST-8 cloud).

The scripts drive helm and kubectl; here they run against stubs that record their arguments,
so no cluster is needed. What a broken script costs is a release installed with the wrong
values, a secret on a command line, or a smoke that checks something else than the local one.
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys

import pytest
import yaml

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_LITE = os.path.join(_REPO, "deploy", "lite")
_CLOUD = os.path.join(_LITE, "cloud")
_LOCAL = os.path.join(_LITE, "local")
_SCRIPTS = ("install", "upgrade", "uninstall", "smoke")


def _read(path, mode="r"):
    with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as fh:
        return fh.read()


def _bash():
    if sys.platform == "win32":
        git_bash = r"C:\Program Files\Git\bin\bash.exe"
        if os.path.exists(git_bash):
            return git_bash
        found = shutil.which("bash")
        return found if found and "system32" not in found.lower() else None
    return shutil.which("bash")


def _powershell():
    return shutil.which("pwsh") or shutil.which("powershell")


def _git(*args):
    if not shutil.which("git"):
        pytest.skip("git not available")
    inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=_REPO,
                            capture_output=True, text=True)
    if inside.returncode != 0:
        pytest.skip("not a git work tree")
    return subprocess.run(["git", *args], cwd=_REPO, capture_output=True, text=True)


# ── Files ───────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("script", _SCRIPTS)
def test_each_script_ships_for_bash_and_powershell(script):
    sh = _read(os.path.join(_CLOUD, f"{script}.sh"), "rb")
    ps1 = _read(os.path.join(_CLOUD, f"{script}.ps1"), "rb")
    assert sh.startswith(b"#!/usr/bin/env bash")
    assert b"\r\n" not in sh                         # bash on macOS/Linux rejects CRLF
    ps1.decode("ascii")                                # Windows PowerShell 5.1 reads ANSI
    assert b"_lib.sh" in sh and b"_lib.ps1" in ps1


def test_shared_helpers_are_lf_and_ascii():
    assert b"\r\n" not in _read(os.path.join(_CLOUD, "_lib.sh"), "rb")
    _read(os.path.join(_CLOUD, "_lib.ps1"), "rb").decode("ascii")


@pytest.mark.parametrize("script", [f"{s}.sh" for s in _SCRIPTS])
def test_bash_scripts_are_committed_executable(script):
    rel = f"deploy/lite/cloud/{script}"
    out = _git("ls-files", "-s", rel).stdout
    if not out:
        pytest.skip(f"{rel} is not committed yet")
    assert out.split()[0] == "100755", out


def test_the_kind_e2e_script_parses_is_lf_and_executable():
    path = os.path.join(_CLOUD, "kind", "e2e.sh")
    assert b"\r\n" not in _read(path, "rb")
    bash = _bash()
    if bash:
        out = subprocess.run([bash, "-n", "./e2e.sh"], cwd=os.path.dirname(path),
                             capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
    staged = _git("ls-files", "-s", "deploy/lite/cloud/kind/e2e.sh").stdout
    if staged:
        assert staged.split()[0] == "100755", staged


def test_the_kind_e2e_pins_every_image_by_digest():
    text = _read(os.path.join(_CLOUD, "kind", "e2e.sh"))
    for name in ("NODE_IMAGE", "DOCKER_CLI_IMAGE", "K8S_TOOLS_IMAGE"):
        value = re.search(rf"^{name}=(\S+)$", text, re.M).group(1)
        assert re.search(r":v?\d+\.\d+\.\d+[^@]*@sha256:[0-9a-f]{64}$", value), (name, value)
    assert re.search(r"^KIND_SHA256=[0-9a-f]{64}$", text, re.M)
    assert "sha256sum -c" in text


def test_the_state_directory_is_ignored():
    if _git("check-ignore", "-q", "deploy/lite/cloud/.state/x.env").returncode != 0:
        pytest.fail("deploy/lite/cloud/.state/ (generated keys) must be git-ignored")


@pytest.mark.parametrize("script", [f"{s}.sh" for s in _SCRIPTS] + ["_lib.sh"])
def test_bash_scripts_parse(script):
    bash = _bash()
    if not bash:
        pytest.skip("bash not available")
    out = subprocess.run([bash, "-n", f"./{script}"], cwd=_CLOUD, capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr


@pytest.mark.parametrize("script", [f"{s}.ps1" for s in _SCRIPTS] + ["_lib.ps1"])
def test_powershell_scripts_parse(script):
    ps = _powershell()
    if not ps:
        pytest.skip("PowerShell not available")
    path = os.path.join(_CLOUD, script)
    command = ("$e = $null; $t = $null; "
               f"$null = [System.Management.Automation.Language.Parser]::ParseFile('{path}', "
               "[ref]$t, [ref]$e); if ($e.Count) { $e | ForEach-Object { $_.ToString() }; "
               "exit 1 }")
    out = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", command],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stdout + out.stderr


def test_create_secrets_uses_the_names_dev_yaml_gives():
    dev = yaml.safe_load(_read(os.path.join(_CLOUD, "values", "dev.yaml")))
    chart = yaml.safe_load(_read(os.path.join(_CLOUD, "helm", "tokenlean-lite", "values.yaml")))
    names = {dev["redis"]["existingSecret"], chart["proxyKeys"]["existingSecret"],
             chart["metrics"]["existingSecret"], chart["providerKeys"]["existingSecret"]}
    for script in ("_lib.sh", "_lib.ps1"):
        text = _read(os.path.join(_CLOUD, script))
        for name in names:
            assert name in text, (script, name)


@pytest.mark.parametrize("script", ["_lib.sh", "_lib.ps1"])
def test_secret_values_never_reach_a_command_line(script):
    """Arguments are visible to every user of the machine: Secrets are created from files."""
    text = _read(os.path.join(_CLOUD, script))
    assert "--from-literal" not in text
    assert "--from-file" in text


@pytest.mark.parametrize("script", ["smoke.sh", "smoke.ps1"])
def test_smoke_runs_the_local_checks_against_the_cluster(script):
    text = _read(os.path.join(_CLOUD, script))
    assert "local" in text and ("smoke.sh" in text or "smoke.ps1" in text)
    assert "LITE_BASE_URL" in text and "LITE_ENV_FILE" in text
    assert "port-forward" in text


@pytest.mark.parametrize("script", ["smoke.sh", "smoke.ps1"])
def test_the_shared_smoke_waits_out_the_key_store_refresh(script):
    """A key issued or revoked on one worker (the image runs two) or replica reaches the
    others within key_store.refresh_seconds, and a routing overlay within poll_seconds (5 s
    each): the checks retry for 12 s instead of reading the first answer (seen on kind)."""
    text = _read(os.path.join(_LOCAL, script))
    assert text.count("key_store.refresh_seconds") >= 2, script
    assert "poll_seconds" in text
    loops = re.findall(r"for _ in 1 2 3 4 5 6 7 8 9 10 11 12;|\$try -lt 12|\$i -le 12|"
                       r"for i in 1 2 3 4 5 6 7 8 9 10 11 12;", text)
    assert len(loops) == 3, (script, loops)


# ── The scripts run against stub helm / kubectl ────────────────────────────────────────

_STUB_SH = r"""#!/usr/bin/env bash
# A stand-in for helm and kubectl: records each call, answers the few the scripts read.
tool=$(basename "$0")
printf '%s %s\n' "$tool" "$*" >>"$STUB_LOG"
case "$tool $*" in
  *"get secret"*) exit "${STUB_SECRET_EXISTS:-1}" ;;
  *"get namespace"*) exit 0 ;;
  *"status "*) exit "${STUB_RELEASE_EXISTS:-0}" ;;
  *"get deployment"*"-o name"*) echo "deployment.apps/tokenlean-lite" ;;
  *"create secret"*)
    for arg in "$@"; do
      case "$arg" in --from-file=*) f=${arg#*=}; f=${f#*=}; printf 'FILE %s\n' "$f" >>"$STUB_LOG"
        cat "$f" >>"$STUB_LOG"; printf '\n' >>"$STUB_LOG" ;; esac
    done ;;
esac
exit 0
"""


@pytest.fixture
def stubs(tmp_path):
    if not _bash():
        pytest.skip("bash not available")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("helm", "kubectl"):
        path = bin_dir / tool
        path.write_bytes(_STUB_SH.encode())
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")
    state = tmp_path / "state"
    env = dict(os.environ, HELM=(bin_dir / "helm").as_posix(),
               KUBECTL=(bin_dir / "kubectl").as_posix(), STUB_LOG=log.as_posix(),
               TOKENLEAN_LITE_STATE_DIR=state.as_posix())
    for key in ("TOKENLEAN_IMAGE", "TOKENLEAN_LITE_NAMESPACE", "TOKENLEAN_LITE_RELEASE",
                "TOKENLEAN_API_KEY", "TOKENLEAN_ADMIN_KEY", "SMOKE_MODEL"):
        env.pop(key, None)
    return env, log, state


def _run(script, args, env, ok=True):
    out = subprocess.run([_bash(), f"./{script}", *args], cwd=_CLOUD, env=env,
                         capture_output=True, text=True, timeout=120)
    if ok:
        assert out.returncode == 0, out.stdout + out.stderr
    return out


def _calls(log, tool):
    return [line.split(" ", 1)[1] for line in log.read_text(encoding="utf-8").splitlines()
            if line.startswith(tool + " ")]


@pytest.mark.parametrize("script", [f"{s}.sh" for s in _SCRIPTS])
def test_bash_help_prints_only_the_header(script, stubs):
    env, _log, _state = stubs
    out = _run(script, ["--help"], env)
    assert out.stdout.strip()
    assert not [line for line in out.stdout.splitlines() if line.startswith("#")]
    assert "set -euo" not in out.stdout


def test_install_runs_helm_upgrade_install_with_the_env_values_and_waits(stubs):
    env, log, _state = stubs
    _run("install.sh", ["staging", "--image", "registry.example.internal/tl-proxy:1.2.3"], env)
    helm = _calls(log, "helm")
    deploy = [c for c in helm if c.startswith("upgrade ")][0]
    assert deploy.startswith("upgrade --install tokenlean-lite ")
    assert "helm/tokenlean-lite" in deploy
    assert "--namespace tokenlean-lite --create-namespace" in deploy
    assert re.search(r"-f \S*values/staging\.yaml", deploy)
    assert "--set image.repository=registry.example.internal/tl-proxy" in deploy
    assert "--set image.tag=1.2.3" in deploy
    assert "--wait --timeout 10m" in deploy
    kubectl = _calls(log, "kubectl")
    assert any(c.startswith("rollout status deployment.apps/tokenlean-lite") or
               "rollout status deployment.apps/tokenlean-lite" in c for c in kubectl), kubectl


def test_install_passes_namespace_release_context_and_extra_values(stubs, tmp_path):
    env, log, _state = stubs
    mine = tmp_path / "mine.yaml"
    mine.write_text("replicas: 3\n", encoding="utf-8")
    _run("install.sh", ["prod", "--image", "reg.example.internal/p:2.0.0", "--namespace",
                        "llm-gw", "--release", "gw", "--context", "prod-eu", "--values",
                        mine.as_posix(), "--timeout", "15m"], env)
    deploy = [c for c in _calls(log, "helm") if c.startswith("upgrade ")][0]
    assert deploy.startswith("upgrade --install gw ")
    assert "--namespace llm-gw" in deploy and "--kube-context prod-eu" in deploy
    assert deploy.index("values/prod.yaml") < deploy.index("mine.yaml")   # yours win
    assert "--timeout 15m" in deploy
    assert all("--context prod-eu" in c for c in _calls(log, "kubectl"))


def test_install_takes_the_image_from_the_environment(stubs):
    env, log, _state = stubs
    _run("install.sh", ["dev"], dict(env, TOKENLEAN_IMAGE="reg.example.internal:5000/p:3.1.0"))
    deploy = [c for c in _calls(log, "helm") if c.startswith("upgrade ")][0]
    assert "--set image.repository=reg.example.internal:5000/p --set image.tag=3.1.0" in deploy


@pytest.mark.parametrize("args, message", [
    (["dev"], "--image"),
    (["dev", "--image", "reg/p:latest"], "exact release"),
    (["dev", "--image", "reg/p"], "exact release"),
    (["qa", "--image", "reg/p:1.0.0"], "dev, staging, prod or managed"),
    (["staging", "--image", "reg/p:1.0.0", "--mock"], "dev only"),
    (["prod", "--image", "reg/p:1.0.0", "--create-secrets"], "dev only"),
    (["dev", "--image", "reg/p:1.0.0", "--namespace", "Bad_NS"], "namespace"),
    (["dev", "--image", "reg/p:1.0.0", "--bogus"], "unknown option"),
])
def test_install_refuses_unsafe_or_unknown_input(stubs, args, message):
    env, log, _state = stubs
    out = _run("install.sh", args, env, ok=False)
    assert out.returncode != 0
    assert message in out.stderr, out.stderr
    assert not [c for c in _calls(log, "helm") if c.startswith("upgrade")]


def test_install_mock_turns_the_mock_provider_on_and_remembers_it(stubs):
    env, log, state = stubs
    _run("install.sh", ["dev", "--image", "reg/p:1.0.0", "--mock"], env)
    deploy = [c for c in _calls(log, "helm") if c.startswith("upgrade ")][0]
    assert "--set mockProvider.enabled=true" in deploy
    text = (state / "tokenlean-lite.tokenlean-lite.env").read_text(encoding="utf-8")
    assert "LITE_MODE=mock" in text


def test_install_create_secrets_makes_the_dev_secrets_from_files(stubs):
    env, log, state = stubs
    _run("install.sh", ["dev", "--image", "reg/p:1.0.0", "--create-secrets"],
         dict(env, LITE_TENANT_ID="LITE-K8S-07"))
    kubectl = _calls(log, "kubectl")
    created = [c for c in kubectl if "create secret generic" in c]
    names = {re.search(r"create secret generic (\S+)", c).group(1) for c in created}
    assert names == {"tokenlean-lite-bootstrap-keys", "tokenlean-lite-redis",
                     "tokenlean-lite-metrics", "tokenlean-lite-provider-keys"}
    assert not [c for c in created if "--from-literal" in c]
    # Relative file names: Git Bash rewrites no path after `--from-file=key=`, so an absolute
    # /tmp path would not reach a Windows kubectl.exe.
    assert not [c for c in created if re.search(r"--from-file=[^= ]+=/", c)], created
    # the bootstrap key file holds hashes only; the raw keys are in the owner-only state file
    keys = dict(line.split("=", 1) for line in
                (state / "tokenlean-lite.tokenlean-lite.env").read_text().splitlines() if line)
    assert keys["LITE_TENANT_ID"] == "LITE-K8S-07"
    log_text = log.read_text(encoding="utf-8")
    assert keys["TOKENLEAN_API_KEY"] not in log_text
    assert keys["TOKENLEAN_ADMIN_KEY"] not in log_text
    import hashlib
    blob = log_text[log_text.index("{"):log_text.index("}\n}") + 3]
    bootstrap = json.loads(blob)
    api = hashlib.sha256(keys["TOKENLEAN_API_KEY"].encode()).hexdigest()
    admin = hashlib.sha256(keys["TOKENLEAN_ADMIN_KEY"].encode()).hexdigest()
    assert bootstrap[api] == {"tenant_id": "LITE-K8S-07", "tier": "free"}
    assert bootstrap[admin] == {"tenant_id": "LITE-K8S-07", "tier": "free", "admin": True}
    # secrets are created before helm installs anything that reads them
    order = log.read_text(encoding="utf-8").splitlines()
    first_secret = next(i for i, line in enumerate(order) if "create secret" in line)
    deploy = next(i for i, line in enumerate(order) if line.startswith("helm upgrade"))
    assert first_secret < deploy
    if sys.platform != "win32":
        assert stat.S_IMODE((state / "tokenlean-lite.tokenlean-lite.env").stat().st_mode) == 0o600


def test_install_create_secrets_never_replaces_an_existing_secret(stubs):
    env, log, _state = stubs
    _run("install.sh", ["dev", "--image", "reg/p:1.0.0", "--create-secrets"],
         dict(env, STUB_SECRET_EXISTS="0"))
    assert not [c for c in _calls(log, "kubectl") if "create secret" in c]


def test_upgrade_needs_an_installed_release(stubs):
    env, log, _state = stubs
    out = _run("upgrade.sh", ["prod", "--image", "reg/p:1.0.1"], dict(env, STUB_RELEASE_EXISTS="1"),
               ok=False)
    assert out.returncode != 0 and "install.sh" in out.stderr
    assert not [c for c in _calls(log, "helm") if c.startswith("upgrade")]


def test_upgrade_upgrades_in_place_and_waits(stubs):
    env, log, _state = stubs
    _run("upgrade.sh", ["prod", "--image", "reg/p:1.0.1"], env)
    deploy = [c for c in _calls(log, "helm") if c.startswith("upgrade ")][0]
    assert deploy.startswith("upgrade tokenlean-lite ")
    assert "--install" not in deploy and "--create-namespace" not in deploy
    assert "--wait" in deploy and "values/prod.yaml" in deploy


def test_uninstall_removes_the_release_and_keeps_secrets_unless_purged(stubs):
    env, log, _state = stubs
    _run("uninstall.sh", [], env)
    assert any(c.startswith("uninstall tokenlean-lite --namespace tokenlean-lite")
               for c in _calls(log, "helm"))
    assert not [c for c in _calls(log, "kubectl") if c.startswith("delete")
                or " delete " in c]
    _run("uninstall.sh", ["--purge"], env)
    deletes = [c for c in _calls(log, "kubectl") if "delete" in c]
    assert any("secret" in c and "app.kubernetes.io/created-by=tokenlean-lite-install" in c
               for c in deletes), deletes
    assert any("pvc" in c and "app.kubernetes.io/instance=tokenlean-lite" in c
               for c in deletes), deletes


def test_smoke_needs_keys(stubs):
    env, _log, _state = stubs
    out = _run("smoke.sh", ["--url", "http://127.0.0.1:9"], env, ok=False)
    assert out.returncode != 0 and "TOKENLEAN_API_KEY" in out.stderr


def test_smoke_needs_a_model_when_not_installed_with_mock(stubs):
    env, _log, _state = stubs
    out = _run("smoke.sh", ["--url", "http://127.0.0.1:9"],
               dict(env, TOKENLEAN_API_KEY="k1", TOKENLEAN_ADMIN_KEY="k2"), ok=False)
    assert out.returncode != 0 and "SMOKE_MODEL" in out.stderr


# ── The same, in PowerShell ────────────────────────────────────────────────────────────

_STUB_PS1 = r"""
$tool = [IO.Path]::GetFileNameWithoutExtension($MyInvocation.MyCommand.Path)
Add-Content -Path $env:STUB_LOG -Value ("$tool " + ($args -join ' '))
$all = $args -join ' '
if ($all -like '*get secret*') { exit 1 }
if ($all -like '*status *') { exit 0 }
if ($all -like '*get deployment*-o name*') { Write-Output 'deployment.apps/tokenlean-lite' }
exit 0
"""


def test_powershell_install_runs_the_same_helm_command(tmp_path):
    ps = _powershell()
    if not ps:
        pytest.skip("PowerShell not available")
    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")
    for tool in ("helm", "kubectl"):
        (tmp_path / f"{tool}.ps1").write_text(_STUB_PS1, encoding="ascii")
    env = dict(os.environ, HELM=str(tmp_path / "helm.ps1"), KUBECTL=str(tmp_path / "kubectl.ps1"),
               STUB_LOG=str(log), TOKENLEAN_LITE_STATE_DIR=str(tmp_path / "state"))
    out = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                          "-File", os.path.join(_CLOUD, "install.ps1"), "staging", "-Image",
                          "registry.example.internal/tl-proxy:1.2.3", "-Context", "stage-1"],
                         env=env, capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stdout + out.stderr
    helm = _calls(log, "helm")
    deploy = [c for c in helm if c.startswith("upgrade ")][0]
    assert deploy.startswith("upgrade --install tokenlean-lite ")
    assert "--namespace tokenlean-lite --create-namespace" in deploy
    assert re.search(r"-f \S*values[\\/]staging\.yaml", deploy)
    assert "--set image.repository=registry.example.internal/tl-proxy" in deploy
    assert "--set image.tag=1.2.3" in deploy
    assert "--wait --timeout 10m" in deploy and "--kube-context stage-1" in deploy
    assert any("rollout status deployment.apps/tokenlean-lite" in c
               for c in _calls(log, "kubectl"))


def test_powershell_install_refuses_latest(tmp_path):
    ps = _powershell()
    if not ps:
        pytest.skip("PowerShell not available")
    out = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                          "-File", os.path.join(_CLOUD, "install.ps1"), "dev", "-Image",
                          "reg/p:latest"], env=dict(os.environ, HELM="helm-not-called"),
                         capture_output=True, text=True, timeout=120)
    assert out.returncode != 0
    assert "exact release" in out.stdout + out.stderr


# ── The local helpers take the cloud smoke's target (backward compatible) ───────────────

def _bash_out(script, env_extra):
    bash = _bash()
    if not bash:
        pytest.skip("bash not available")
    env = {k: v for k, v in os.environ.items() if k not in ("LITE_BASE_URL", "LITE_ENV_FILE")}
    out = subprocess.run([bash, "-c", script], cwd=_LOCAL, env=dict(env, **env_extra),
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_local_bash_helpers_default_to_the_local_stack():
    out = _bash_out('. ./_lib.sh; echo "url=$(lite_base_url) env=${LITE_ENV_FILE##*/}"', {})
    assert "url=http://127.0.0.1:4100 env=.env.lite" in out


def test_local_bash_helpers_take_a_base_url_and_env_file_from_the_environment():
    out = _bash_out('. ./_lib.sh; echo "url=$(lite_base_url) env=$LITE_ENV_FILE"',
                    {"LITE_BASE_URL": "https://gw.example.internal", "LITE_ENV_FILE": "/x/e.env"})
    assert "url=https://gw.example.internal env=/x/e.env" in out


def test_local_powershell_helpers_take_a_base_url_and_env_file_from_the_environment():
    ps = _powershell()
    if not ps:
        pytest.skip("PowerShell not available")
    script = (f". '{os.path.join(_LOCAL, '_lib.ps1')}'; "
              "Write-Output ('url=' + (Get-LiteBaseUrl) + ' env=' + $LiteEnvFile)")
    out = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", script],
                         env=dict(os.environ, LITE_BASE_URL="https://gw.example.internal/",
                                  LITE_ENV_FILE="C:/x/e.env"),
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "url=https://gw.example.internal env=C:/x/e.env" in out.stdout
    env = {k: v for k, v in os.environ.items() if k not in ("LITE_BASE_URL", "LITE_ENV_FILE")}
    out = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", script], env=env,
                         capture_output=True, text=True, timeout=120)
    assert "url=http://127.0.0.1:4100" in out.stdout and ".env.lite" in out.stdout
