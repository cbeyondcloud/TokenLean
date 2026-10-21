"""`scripts/compile-requirements.sh --upgrade-package NAME` moves one package, through the sanctioned
compile (2026-10-21).

Without arguments pip-compile keeps every existing pin it can, so re-running the script never
delivered a security fix in a transitive package; the only way to get one was Dependabot's
regenerator, which re-pins torch and the CUDA runtime (PR #33 then, #47/#48 for aiohttp 3.14.4).
The flag is forwarded to every pip-compile in the container, and anything that is not a package name
is refused before Docker is reached, because the value is expanded inside the container's bash -c.

Docker is replaced by a stub on PATH that records its arguments, so these tests run anywhere bash does.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = _ROOT / "scripts" / "compile-requirements.sh"


def _bash():
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            candidate = Path(git).resolve().parents[1] / "bin" / "bash.exe"
            if candidate.exists():
                return str(candidate)
    return shutil.which("bash")


pytestmark = pytest.mark.skipif(_bash() is None, reason="needs bash")


def _run(tmp_path, *args):
    """Run the script with a recording `docker` stub first on PATH."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    record = tmp_path / "docker-args.txt"
    stub = stub_dir / "docker"
    stub.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$DOCKER_RECORD"\n', encoding="utf-8")
    stub.chmod(0o755)
    env = dict(os.environ, DOCKER_RECORD=record.as_posix())
    path_sep = ";" if os.name == "nt" else ":"
    # Git Bash maps a Windows PATH entry itself; a POSIX-style entry works on both.
    stub_posix = stub_dir.as_posix()
    if os.name == "nt" and len(stub_posix) > 1 and stub_posix[1] == ":":
        stub_posix = "/" + stub_posix[0].lower() + stub_posix[2:]
    env["PATH"] = stub_posix + ":" + env.get("PATH", "") if os.name != "nt" else env.get("PATH", "")
    cmd = [_bash(), "-c", f'export PATH="{stub_posix}:$PATH"; exec bash "$0" "$@"', SCRIPT.as_posix(), *args]
    proc = subprocess.run(cmd, cwd=_ROOT, env=env, capture_output=True, text=True, timeout=60)
    docker_args = record.read_text(encoding="utf-8").splitlines() if record.exists() else None
    return proc, docker_args


def _env_value(docker_args, name):
    for i, arg in enumerate(docker_args):
        if arg == "-e" and docker_args[i + 1].startswith(name + "="):
            return docker_args[i + 1][len(name) + 1:]
    return None


def test_with_no_arguments_nothing_is_upgraded(tmp_path):
    proc, docker_args = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert _env_value(docker_args, "UPGRADE_ARGS") == ""


def test_each_named_package_reaches_the_container(tmp_path):
    proc, docker_args = _run(tmp_path, "--upgrade-package", "aiohttp", "--upgrade-package", "yarl")
    assert proc.returncode == 0, proc.stderr
    assert _env_value(docker_args, "UPGRADE_ARGS").split() == [
        "--upgrade-package", "aiohttp", "--upgrade-package", "yarl"]


def test_every_pip_compile_in_the_container_receives_it(tmp_path):
    """All three pip-compile calls (proxy, tests, the four sidecar/pipeline images) take the flag:
    a security fix must land in every lockfile that holds the package."""
    _run(tmp_path, "--upgrade-package", "aiohttp")
    # The stub writes one argument per line, so the multi-line `bash -c` script spans many.
    container_script = (tmp_path / "docker-args.txt").read_text(encoding="utf-8")
    assert container_script.count("pip-compile --no-strip-extras $UPGRADE_ARGS") == 3
    assert container_script.count("pip-compile") == container_script.count("$UPGRADE_ARGS")


@pytest.mark.parametrize("bad", ["aiohttp;rm -rf /", "$(id)", "-rf", "a b", ""])
def test_anything_but_a_package_name_is_refused_before_docker(tmp_path, bad):
    proc, docker_args = _run(tmp_path, "--upgrade-package", bad)
    assert proc.returncode == 2
    assert docker_args is None, "docker must not be reached"


def test_a_missing_name_is_refused(tmp_path):
    proc, docker_args = _run(tmp_path, "--upgrade-package")
    assert proc.returncode == 2 and "needs a package name" in proc.stderr
    assert docker_args is None


def test_an_unknown_argument_is_refused(tmp_path):
    proc, docker_args = _run(tmp_path, "--upgrade")
    assert proc.returncode == 2 and "unknown argument" in proc.stderr
    assert docker_args is None


def test_the_cuda_exclusions_survive_the_change():
    """The flag must not displace what the script exists for."""
    src = SCRIPT.read_text(encoding="utf-8")
    assert src.count("--unsafe-package torch --unsafe-package triton --unsafe-package uvloop") == 2
    assert src.count("sweep requirements.txt") == 2
