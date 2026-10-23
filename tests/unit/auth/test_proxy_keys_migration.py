"""infra/migrations/proxy_keys.sql states the same tables as the app's startup DDL, and the
migration job runs it (ST-9). Either may run first: both are CREATE ... IF NOT EXISTS."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

from pathlib import Path

from auth import pg_key_store

_MIGRATIONS = Path(__file__).resolve().parents[3] / "infra" / "migrations"


def _statements(sql: str) -> str:
    return "\n".join(line for line in sql.splitlines()
                     if line.strip() and not line.lstrip().startswith("--")).strip()


def test_the_migration_is_the_app_ddl():
    migration = (_MIGRATIONS / "proxy_keys.sql").read_text(encoding="utf-8")
    assert _statements(migration) == _statements(pg_key_store.PROXY_KEYS_DDL)


def test_the_migration_job_runs_it_and_the_image_carries_it():
    assert "run_sql proxy_keys.sql" in (_MIGRATIONS / "run.sh").read_text(encoding="utf-8")
    copy = [line for line in (_MIGRATIONS / "Dockerfile.migrate").read_text(encoding="utf-8")
            .splitlines() if line.startswith("COPY") and ".sql" in line]
    assert any("proxy_keys.sql" in line for line in copy)


def test_the_down_script_exists_and_says_it_is_destructive():
    down = (_MIGRATIONS / "down" / "proxy_keys.sql").read_text(encoding="utf-8")
    assert "DESTRUCTIVE" in down and "DROP TABLE IF EXISTS proxy_keys" in down
