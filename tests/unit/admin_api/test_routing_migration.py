"""infra/migrations/routing_overlay.sql states the app's own DDL for routing_overlay_versions,
the migration job runs it, and the down script removes it (ST-10)."""
from pathlib import Path

from admin_api import routing

_MIGRATIONS = Path(__file__).resolve().parents[3] / "infra" / "migrations"


def _statements(sql: str) -> str:
    return "\n".join(line for line in sql.splitlines()
                     if line.strip() and not line.lstrip().startswith("--")).strip()


def test_the_migration_is_the_app_ddl():
    migration = (_MIGRATIONS / "routing_overlay.sql").read_text(encoding="utf-8")
    assert _statements(migration) == _statements(routing.ROUTING_OVERLAY_DDL)


def test_the_migration_job_runs_it_and_the_image_carries_it():
    assert "run_sql routing_overlay.sql" in (_MIGRATIONS / "run.sh").read_text(encoding="utf-8")
    copy = [line for line in (_MIGRATIONS / "Dockerfile.migrate").read_text(encoding="utf-8")
            .splitlines() if line.startswith("COPY") and ".sql" in line]
    assert any("routing_overlay.sql" in line for line in copy)


def test_the_down_script_drops_only_the_versions_table():
    down = _statements((_MIGRATIONS / "down" / "routing_overlay.sql").read_text(encoding="utf-8"))
    assert down == "DROP TABLE IF EXISTS routing_overlay_versions;"
