"""``python -m schema_setup``: create or update the proxy's Postgres tables, then exit.

The proxy applies these steps itself at startup, part by part, and keeps serving while one
fails. Run once per deploy instead (the LITE Helm chart's migration hook does), they happen
before the new replicas start, so replicas never race to create a table, and they can run as
the tables' owner while the proxy connects as a restricted role (audit/enforcement.py). As a
deploy step it fails loudly: a step that fails or a table that is still missing afterwards
exits non-zero, and the deploy stops.

Connection: ``DATABASE_URL``; its password may come from ``PGPASSWORD`` instead, which the
Postgres driver reads. The database may still be starting: the connection is retried.

Exit codes: 0 every table exists; 1 a step failed or a table is missing; 2 no DATABASE_URL.
"""
import argparse
import asyncio
import logging
import os
import sys
from typing import Awaitable, Callable, List, Tuple

import asyncpg

logger = logging.getLogger(__name__)

# The tables the steps below create; each must exist when they have run.
TABLES = ("usage_events", "audit_events", "proxy_keys", "revoked_proxy_keys",
          "routing_overlay_versions")

Step = Tuple[str, Callable[[object], Awaitable[None]]]


def steps() -> List[Step]:
    """The proxy's own schema steps, in the order its startup applies them."""
    from admin_api.routing import ensure_schema as ensure_routing_schema
    from audit.log import ensure_audit_schema
    from auth.pg_key_store import ensure_proxy_keys_schema
    from billing.models import ensure_usage_events_schema
    return [("usage_events", ensure_usage_events_schema),
            ("audit_events", ensure_audit_schema),
            ("proxy_keys", ensure_proxy_keys_schema),
            ("routing_overlay", ensure_routing_schema)]


async def missing_tables(pool) -> List[str]:
    async with pool.acquire() as conn:
        return [t for t in TABLES if not await conn.fetchval("SELECT to_regclass($1)::text", t)]


async def apply(pool, todo: List[Step]) -> List[str]:
    """Run every step (one failing does not stop the others); return the problems found."""
    problems = []
    for name, step in todo:
        try:
            await step(pool)
            logger.info("schema step %s applied", name)
        except Exception as exc:
            problems.append(f"{name}: {exc}")
    problems += [f"table {t} is missing" for t in await missing_tables(pool)]
    return problems


async def connect(dsn: str, attempts: int, delay: float):
    """A small pool, retried while the database is not reachable yet."""
    for attempt in range(1, attempts + 1):
        try:
            return await asyncpg.create_pool(dsn, min_size=1, max_size=2)
        except (OSError, asyncpg.PostgresError) as exc:
            if attempt == attempts:
                raise
            logger.warning("database not reachable (attempt %d/%d): %s", attempt, attempts, exc)
            await asyncio.sleep(delay)


async def _run(dsn: str, attempts: int, delay: float) -> List[str]:
    pool = await connect(dsn, attempts, delay)
    try:
        return await apply(pool, steps())
    finally:
        await pool.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m schema_setup",
        description="Create or update the proxy's Postgres tables (DATABASE_URL), then exit.")
    parser.add_argument("--attempts", type=int, default=30,
                        help="connection attempts while the database starts (default 30)")
    parser.add_argument("--delay", type=float, default=2.0,
                        help="seconds between connection attempts (default 2)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                        format="%(levelname)s %(name)s: %(message)s")
    dsn = os.getenv("DATABASE_URL", "")
    if not dsn:
        print("DATABASE_URL must be set (its password may come from PGPASSWORD)",
              file=sys.stderr)
        return 2
    problems = asyncio.run(_run(dsn, args.attempts, args.delay))
    if problems:
        for problem in problems:
            print(f"schema_setup: {problem}", file=sys.stderr)
        return 1
    print("schema_setup: schema ready (" + ", ".join(TABLES) + ")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
