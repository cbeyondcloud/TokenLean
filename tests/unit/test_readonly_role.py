"""python -m readonly_role: a SELECT-only login for dashboards (Grafana), run as the owner.

The database side runs in tests/integration/test_readonly_role_pg.py; these pin what is
checked before any connection and how the LITE stack and chart invoke it.
"""
import os

import pytest
import yaml

import readonly_role

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_LITE = os.path.join(_REPO, "deploy", "lite")


def test_a_grant_names_a_database_and_its_tables():
    assert readonly_role.parse_grant("tokenlean:usage_events") == ("tokenlean", ("usage_events",))
    assert readonly_role.parse_grant("langfuse: observations, scores") == (
        "langfuse", ("observations", "scores"))
    assert readonly_role.parse_grant("tokenlean-prod:usage_events")[0] == "tokenlean-prod"


@pytest.mark.parametrize("bad", ["tokenlean", "tokenlean:", ":usage_events", "a:b;DROP",
                                 'db:"x"', "d b:t", "db:Usage_Events"])
def test_a_malformed_grant_is_refused(bad):
    with pytest.raises(ValueError):
        readonly_role.parse_grant(bad)


@pytest.mark.parametrize("env, args", [
    ({"READONLY_ROLE_PASSWORD": "pw"}, ["--role", "r", "--grant", "d:t"]),          # no DSN
    ({"DATABASE_URL": "postgresql://o@h/d"}, ["--role", "r", "--grant", "d:t"]),     # no password
    ({"DATABASE_URL": "postgresql://o@h/d", "READONLY_ROLE_PASSWORD": "pw"},
     ["--role", "r"]),                                                               # no grant
    ({"DATABASE_URL": "postgresql://o@h/d", "READONLY_ROLE_PASSWORD": "pw"},
     ["--role", "Bad-Role", "--grant", "d:t"]),
    ({"DATABASE_URL": "postgresql://o@h/d", "READONLY_ROLE_PASSWORD": "pw"},
     ["--role", "r", "--grant", "d:t", "--schema", "x;y"]),
    ({"DATABASE_URL": "postgresql://o@h/d", "READONLY_ROLE_PASSWORD": "pw"},
     ["--role", "r", "--grant", "d:t", "--statement-timeout", "1s; DROP"]),
    ({"DATABASE_URL": "postgresql://o@h/d", "READONLY_ROLE_PASSWORD": "pw"},
     ["--role", "r", "--grant", "d:t", "--connection-limit", "0"]),
    ({"DATABASE_URL": "postgresql://o@h/d", "READONLY_ROLE_PASSWORD": "pw"},
     ["--role", "r", "--grant", "d:t", "--revoke", "d"]),                        # both
    ({"DATABASE_URL": "postgresql://o@h/d", "READONLY_ROLE_PASSWORD": "pw"},
     ["--role", "r", "--grant", "d:t", "--lock-large-objects", "other"]),        # not granted
    ({"DATABASE_URL": "postgresql://o@h/d", "READONLY_ROLE_PASSWORD": "pw"},
     ["--role", "r", "--grant", "d:t", "--revoke", "x y"]),
])
def test_missing_or_invalid_settings_exit_2_before_connecting(monkeypatch, capsys, env, args):
    for key in ("DATABASE_URL", "READONLY_ROLE_PASSWORD", "READONLY_ROLE"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(readonly_role, "run", lambda *a, **k: pytest.fail("connected"))
    assert readonly_role.main(args) == 2
    assert "readonly_role:" in capsys.readouterr().err


def test_the_role_name_may_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://o@h/d")
    monkeypatch.setenv("READONLY_ROLE_PASSWORD", "pw")
    monkeypatch.setenv("READONLY_ROLE", "tokenlean_grafana")
    seen = {}

    async def fake_run(dsn, role, password, grants, schema, wait, revokes, timeout, limit,
                       lock):
        seen.update(role=role, grants=grants, schema=schema, revokes=revokes,
                    timeout=timeout, limit=limit, lock=lock)
        return {"d": []}
    monkeypatch.setattr(readonly_role, "run", fake_run)
    assert readonly_role.main(["--grant", "d:t1,t2"]) == 0
    assert seen == {"role": "tokenlean_grafana", "grants": [("d", ("t1", "t2"))],
                    "schema": "public", "revokes": [], "timeout": "60s", "limit": 20,
                    "lock": []}


def test_a_refusal_says_nothing_was_changed(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://o@h/d")
    monkeypatch.setenv("READONLY_ROLE_PASSWORD", "pw")

    async def fake_run(*a, **k):
        raise readonly_role.Refused("role r is a superuser")
    monkeypatch.setattr(readonly_role, "run", fake_run)
    assert readonly_role.main(["--role", "r", "--grant", "d:t"]) == 1
    assert "refused, nothing was changed: role r is a superuser" in capsys.readouterr().err


def test_a_check_that_does_not_hold_exits_1(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://o@h/d")
    monkeypatch.setenv("READONLY_ROLE_PASSWORD", "pw")

    async def fake_run(*a, **k):
        return {"d": ["role r may read public.proxy_keys"]}
    monkeypatch.setattr(readonly_role, "run", fake_run)
    assert readonly_role.main(["--role", "r", "--grant", "d:t"]) == 1
    assert "may read public.proxy_keys" in capsys.readouterr().err


def test_the_password_is_never_a_command_line_argument():
    with open(readonly_role.__file__, encoding="utf-8") as fh:
        source = fh.read()
    assert "--password" not in source and "READONLY_ROLE_PASSWORD" in source


# ── How the LITE local stack uses it ───────────────────────────────────────────


def _compose():
    with open(os.path.join(_LITE, "local", "docker-compose.lite.yml"), encoding="utf-8") as fh:
        return yaml.safe_load(fh)["services"]


def test_grafana_logs_in_as_the_read_only_role_never_the_owner():
    services = _compose()
    reader, grafana = services["grafana-db-reader"], services["grafana"]
    env = grafana["environment"]
    assert env["LITE_DB_USER"] == "${LITE_GRAFANA_DB_USER:-tokenlean_grafana}"
    assert env["LITE_DB_PASSWORD"].startswith("${GRAFANA_DB_PASSWORD:?")
    assert not any("${DB_PASSWORD" in str(v) for v in env.values())   # no owner password
    assert reader["environment"]["READONLY_ROLE"] == env["LITE_DB_USER"]
    assert reader["environment"]["READONLY_ROLE_PASSWORD"].startswith("${GRAFANA_DB_PASSWORD:?")
    assert grafana["depends_on"]["grafana-db-reader"] == {
        "condition": "service_completed_successfully"}
    assert reader["profiles"] == ["grafana"] and reader["restart"] == "no"


def _reader_args(langfuse, tmp_path):
    """The arguments the grafana-db-reader service passes to python -m readonly_role, run
    through its own shell script with a stub python."""
    import shutil
    import subprocess
    import sys
    bash = (r"C:\Program Files\Git\bin\bash.exe" if sys.platform == "win32" else
            shutil.which("bash"))
    if not bash or not os.path.exists(bash):
        pytest.skip("bash not available")
    stub = tmp_path / "python"
    stub.write_bytes(b'#!/usr/bin/env bash\nprintf \'%s\\n\' "$@"\n')
    stub.chmod(0o755)
    command = _compose()["grafana-db-reader"]["command"]
    assert command[:2] == ["sh", "-c"]
    script = command[2].replace("$$", "$")              # compose's own escaping
    env = dict(os.environ, LITE_GRAFANA_LANGFUSE=langfuse,
               PATH=str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    out = subprocess.run([bash, "-c", script], env=env, capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout.split()


def test_the_reader_grants_what_the_datasources_read(tmp_path):
    grafana_env = _compose()["grafana"]["environment"]
    db, langfuse = grafana_env["LITE_DB_NAME"], grafana_env["LITE_LANGFUSE_DB_NAME"]
    off = _reader_args("off", tmp_path)
    assert off == ["-m", "readonly_role", "--grant", f"{db}:usage_events",
                   "--lock-large-objects", db, "--revoke", langfuse]
    # Per Call's Langfuse panels (observations, scores), only when Langfuse runs; with it
    # off, an earlier run's Langfuse grant is taken back.
    on = _reader_args("on", tmp_path)
    assert on == ["-m", "readonly_role", "--grant", f"{db}:usage_events",
                  "--lock-large-objects", db, "--grant", f"{langfuse}:observations,scores"]
    reader = _compose()["grafana-db-reader"]
    assert reader["environment"]["LITE_GRAFANA_LANGFUSE"] == "${LITE_GRAFANA_LANGFUSE:-off}"


def test_up_generates_the_reader_password_and_down_needs_none():
    for script in ("up.sh", "up.ps1", "down.sh", "down.ps1"):
        with open(os.path.join(_LITE, "local", script), encoding="utf-8") as fh:
            assert "GRAFANA_DB_PASSWORD" in fh.read(), script
    with open(os.path.join(_LITE, "local", ".env.lite.template"), encoding="utf-8") as fh:
        assert "\nGRAFANA_DB_PASSWORD=\n" in fh.read()
