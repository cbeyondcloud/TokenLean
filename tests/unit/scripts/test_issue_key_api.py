"""issue-key.sh --api: issue, list, revoke and rotate keys through /admin/keys (ST-9).

With ``--api URL`` (or TOKENLEAN_ADMIN_URL) the script calls the proxy's admin API instead
of writing the Secret Manager blob, so it works whatever key store the proxy uses
(``key_store.backend: blob | postgres``) and every change is audited by the proxy. The admin
key comes from TOKENLEAN_ADMIN_KEY only, and reaches curl through a header FILE, never the
command line (where other users of the host could read it). gcloud is never called.

A FAKE `curl` first on PATH records its arguments, the header file it was given and the
body, and answers from FAKE_STATUS / FAKE_BODY.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.scripts.test_issue_key import SCRIPT, _bash

ADMIN_KEY = "k-test-admin-not-a-real-key"

FAKE_CURL = r'''#!/usr/bin/env bash
: > "$FAKE_LOG"
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  printf '%s\n' "${args[$i]}" >> "$FAKE_LOG"
  if [[ "${args[$i]}" == "-H" && "${args[$((i + 1))]}" == @* ]]; then
    cat "${args[$((i + 1))]#@}" >> "$FAKE_HEADERS"
  fi
  if [[ "${args[$i]}" == "--data-binary" ]]; then
    printf '%s' "${args[$((i + 1))]}" > "$FAKE_SENT"
  fi
done
if [[ -n "${FAKE_CURL_EXIT:-}" ]]; then exit "$FAKE_CURL_EXIT"; fi
printf '%s\n%s' "$FAKE_BODY" "$FAKE_STATUS"
'''

pytestmark = pytest.mark.skipif(
    _bash() is None or shutil.which("python3") is None, reason="needs bash and python3")


@pytest.fixture
def sandbox(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / "issue-key.sh")
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    (fakebin / "curl").write_text(FAKE_CURL, encoding="utf-8", newline="\n")
    (fakebin / "gcloud").write_text("#!/usr/bin/env bash\necho called > \"$GCLOUD_LOG\"\nexit 3\n",
                                    encoding="utf-8", newline="\n")
    os.chmod(fakebin / "curl", 0o700)
    os.chmod(fakebin / "gcloud", 0o700)
    return tmp_path


def _run(root, *args, status=200, body=None, admin_key=ADMIN_KEY, url_env=None,
         curl_exit=None):
    files = {n: root / f"{n}.txt" for n in ("log", "headers", "sent", "gcloud")}
    for f in files.values():
        f.unlink(missing_ok=True)
    env = {**os.environ,
           "PATH": str(root / "fakebin") + os.pathsep + os.environ.get("PATH", ""),
           "FAKE_LOG": files["log"].as_posix(), "FAKE_HEADERS": files["headers"].as_posix(),
           "FAKE_SENT": files["sent"].as_posix(), "GCLOUD_LOG": files["gcloud"].as_posix(),
           "FAKE_STATUS": str(status), "FAKE_BODY": json.dumps(body or {}),
           "TMPDIR": (root / "tmp").as_posix()}
    (root / "tmp").mkdir(exist_ok=True)
    if curl_exit is not None:
        env["FAKE_CURL_EXIT"] = str(curl_exit)
    env.pop("TOKENLEAN_ADMIN_URL", None)
    env.pop("TOKENLEAN_ADMIN_KEY", None)
    if admin_key is not None:
        env["TOKENLEAN_ADMIN_KEY"] = admin_key
    if url_env:
        env["TOKENLEAN_ADMIN_URL"] = url_env
    r = subprocess.run([_bash(), "scripts/issue-key.sh", *args], cwd=root, env=env,
                       capture_output=True, encoding="utf-8", errors="replace")
    read = {n: (f.read_text(encoding="utf-8") if f.exists() else None) for n, f in files.items()}
    return r, read


_CREATED = {"key": "tok-fake-created", "key_id": "0123456789abcdef", "tenant_id": "acme",
            "team": "payments", "user": "alice@acme.test", "tier": "free"}


def test_issue_posts_the_binding_and_prints_the_key_once(sandbox):
    r, seen = _run(sandbox, "issue", "--api", "https://proxy.example.test", "--tenant", "acme",
                   "--team", "payments", "--bind-user", "alice@acme.test", "--label", "ci",
                   status=201, body=_CREATED)
    assert r.returncode == 0, r.stdout + r.stderr
    argv = seen["log"].splitlines()
    assert "POST" in argv and "https://proxy.example.test/admin/keys" in argv
    assert json.loads(seen["sent"]) == {"tenant_id": "acme", "tier": "free", "team": "payments",
                                        "user": "alice@acme.test", "label": "ci"}
    assert (r.stdout + r.stderr).count("tok-fake-created") == 2   # the key line + usage line
    assert "0123456789abcdef" in r.stdout
    assert seen["gcloud"] is None                                  # no Secret Manager at all


def test_the_admin_key_travels_in_a_header_file_never_on_the_command_line(sandbox):
    r, seen = _run(sandbox, "list", "--api", "https://proxy.example.test",
                   body={"keys": [], "count": 0})
    assert r.returncode == 0, r.stdout + r.stderr
    assert ADMIN_KEY not in seen["log"]
    assert f"Authorization: Bearer {ADMIN_KEY}" in seen["headers"]
    assert ADMIN_KEY not in r.stdout + r.stderr


def test_without_an_admin_key_nothing_is_sent(sandbox):
    r, seen = _run(sandbox, "list", "--api", "https://proxy.example.test", admin_key=None)
    assert r.returncode != 0 and "TOKENLEAN_ADMIN_KEY" in r.stdout + r.stderr
    assert seen["log"] is None


def test_list_passes_the_filters_and_prints_ids_not_keys(sandbox):
    keys = [{"key_id": "0123456789abcdef", "tenant_id": "acme", "team": "payments",
             "user": "alice@acme.test", "tier": "free", "admin": False, "gateway": False,
             "suspended": False, "label": "ci", "created_at": "2026-10-23T00:00:00+00:00"}]
    r, seen = _run(sandbox, "list", "--tenant", "acme", "--team", "payments",
                   url_env="https://proxy.example.test/", body={"keys": keys, "count": 1})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "https://proxy.example.test/admin/keys?tenant_id=acme&team=payments" in seen["log"]
    assert "0123456789abcdef" in r.stdout and "alice@acme.test" in r.stdout


def test_revoke_and_rotate_name_one_key(sandbox):
    r, seen = _run(sandbox, "revoke", "--api", "https://p.example.test", "--key-id",
                   "0123456789abcdef", body={"revoked": True, "key_id": "0123456789abcdef"})
    assert r.returncode == 0, r.stdout + r.stderr
    argv = seen["log"].splitlines()
    assert "DELETE" in argv and "https://p.example.test/admin/keys/0123456789abcdef" in argv
    r, seen = _run(sandbox, "rotate", "--api", "https://p.example.test", "--key-id",
                   "0123456789abcdef",
                   body={**_CREATED, "key": "tok-fake-rotated", "key_id": "fedcba9876543210",
                         "replaced_key_id": "0123456789abcdef"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "https://p.example.test/admin/keys/0123456789abcdef/rotate" in seen["log"]
    assert "tok-fake-rotated" in r.stdout and "fedcba9876543210" in r.stdout


def test_an_api_refusal_is_reported_and_fails(sandbox):
    r, _seen = _run(sandbox, "revoke", "--api", "https://p.example.test", "--key-id",
                    "0123456789abcdef", status=404,
                    body={"detail": "No proxy key with that id in your scope."})
    assert r.returncode != 0
    assert "404" in r.stdout + r.stderr and "No proxy key" in r.stdout + r.stderr


@pytest.mark.parametrize("args,needs", [
    (("revoke", "--tenant", "acme"), "--key-id"),
    (("rotate",), "--key-id"),
    (("issue",), "--tenant"),
    (("issue", "--tenant", "acme", "--bind-user", "a b"), "--bind-user"),
])
def test_bad_api_arguments_are_refused_before_any_call(sandbox, args, needs):
    r, seen = _run(sandbox, *args, "--api", "https://p.example.test")
    assert r.returncode != 0 and needs in r.stdout + r.stderr
    assert seen["log"] is None


def test_rotate_without_the_api_says_so(sandbox):
    r, seen = _run(sandbox, "rotate", "--key-id", "0123456789abcdef", "--project", "fake-p")
    assert r.returncode != 0 and "--api" in r.stdout + r.stderr
    assert seen["gcloud"] is None



# ── independent review m9 ──

@pytest.mark.parametrize("curl_exit", [None, 7])
def test_the_header_file_is_removed_whatever_happens(sandbox, curl_exit):
    r, _seen = _run(sandbox, "list", "--api", "https://p.example.test",
                    body={"keys": [], "count": 0}, curl_exit=curl_exit)
    assert (r.returncode == 0) == (curl_exit is None)
    assert list((sandbox / "tmp").iterdir()) == []


def test_plain_http_to_another_host_is_refused_without_insecure(sandbox):
    r, seen = _run(sandbox, "list", "--api", "http://proxy.example.test")
    assert r.returncode != 0 and "--insecure" in r.stdout + r.stderr
    assert seen["log"] is None
    r, seen = _run(sandbox, "list", "--api", "http://proxy.example.test", "--insecure",
                   body={"keys": [], "count": 0})
    assert r.returncode == 0 and "WARN" in r.stdout + r.stderr


@pytest.mark.parametrize("url", ["http://127.0.0.1:4000", "http://localhost:4000",
                                 "http://[::1]:4000"])
def test_plain_http_to_this_machine_is_allowed(sandbox, url):
    r, _seen = _run(sandbox, "list", "--api", url, body={"keys": [], "count": 0})
    assert r.returncode == 0, r.stdout + r.stderr
