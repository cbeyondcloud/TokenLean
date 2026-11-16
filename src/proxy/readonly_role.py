"""``python -m readonly_role``: a SELECT-only login for dashboards, then exit.

A dashboard tool (Grafana) only reads. Logging it in as the owner of the tables gives every
dashboard query, and anyone who can edit or craft one, the owner's rights: write and drop the
tables, and on a stack whose owner is a superuser (the LITE local Postgres) run programs on
the database server. This creates or re-aligns a second LOGIN role, run as the owner, that:

* is LOGIN only: no SUPERUSER, CREATEDB, CREATEROLE, REPLICATION or BYPASSRLS, a member of
  no role, and ``default_transaction_read_only`` on;
* in each database named, has CONNECT, USAGE on the schema, and SELECT on exactly the tables
  named: every other privilege it holds on that schema's tables, sequences and functions,
  and CREATE on the schema, is revoked. Run again with another table list, it follows the
  list.

It is idempotent, so a stack runs it at every start (the LITE local stack's
``grafana-db-reader`` service, the LITE chart's migration hook when dashboards.reader is on).
A table may not exist yet when it starts (the proxy creates usage_events at its own startup,
Langfuse its tables at its first boot): it waits up to ``--wait-seconds`` for the database and
the tables, then fails loudly.

Connection: ``DATABASE_URL`` (the owner; its password may come from ``PGPASSWORD``). Each
``--grant DATABASE:TABLE[,TABLE...]`` connects to DATABASE on the same server. The role's
password comes from ``READONLY_ROLE_PASSWORD``, never the command line, and reaches the server
only as a SCRAM verifier, so a statement log never records it.

Exit codes: 0 the role reads exactly the tables named; 1 a step failed or a check did not hold;
2 a setting is missing or invalid.
"""
import argparse
import asyncio
import logging
import os
import re
import sys
import time
from typing import Dict, List, Sequence, Tuple

from audit.enforcement import _ROLE_RE, _ident, _literal, _scram_verifier

logger = logging.getLogger(__name__)

Grant = Tuple[str, Tuple[str, ...]]

# What a read-only login must never be or hold; each one is reported by check().
_ROLE_CHECK_SQL = """
SELECT r.rolsuper, r.rolcreatedb, r.rolcreaterole, r.rolreplication, r.rolbypassrls,
       r.rolcanlogin,
       EXISTS (SELECT 1 FROM pg_auth_members m WHERE m.member = r.oid) AS member_of_a_role,
       COALESCE((SELECT bool_or(u.c = 'default_transaction_read_only=on')
                 FROM unnest(r.rolconfig) AS u(c)), false) AS read_only_default
FROM pg_roles r WHERE r.rolname = $1
"""

# A database name as a managed service allows one (quoted wherever it is used).
_DATABASE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$-]{0,62}$")

_WRITE_PRIVILEGES = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


def parse_grant(text: str) -> Grant:
    """``DATABASE:TABLE[,TABLE...]`` -> (database, tables)."""
    database, sep, tables = text.partition(":")
    database = database.strip()
    names = tuple(t.strip() for t in tables.split(",") if t.strip())
    if not sep or not database or not names:
        raise ValueError(f"--grant needs DATABASE:TABLE[,TABLE...], got {text!r}")
    if not _DATABASE_RE.match(database):
        raise ValueError(f"not a database name: {database!r}")
    for name in names:
        if not _ROLE_RE.match(name):
            raise ValueError(f"not a plain lower-case table name: {name!r}")
    return database, names


def _check_role(role: str, password: str) -> None:
    if not _ROLE_RE.match(role):
        raise ValueError(f"invalid role name: {role!r}")
    if not password or not password.isascii() or not password.isprintable():
        raise ValueError("READONLY_ROLE_PASSWORD must be set, in printable ASCII")


async def apply(conn, role: str, password: str, tables: Sequence[str],
                schema: str = "public") -> None:
    """As the owner, connected to the database the *tables* are in: create or re-align *role*
    so that it reads exactly *tables* of *schema* there and nothing else."""
    _check_role(role, password)
    if role == await conn.fetchval("SELECT current_user::text"):
        raise ValueError(f"{role} is the connected owner itself: name another role")
    who, where = _ident(role), _ident(schema)
    async with conn.transaction():
        if not await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role):
            await conn.execute(f"CREATE ROLE {who} LOGIN")
        # Only the attributes a non-superuser owner may set (a managed service's is one).
        # SUPERUSER, REPLICATION and BYPASSRLS are off for a new role; check() reports an
        # existing role that has any of them.
        await conn.execute(f"ALTER ROLE {who} WITH LOGIN NOCREATEDB NOCREATEROLE "
                           f"PASSWORD {_literal(_scram_verifier(password))}")
        await conn.execute(f"ALTER ROLE {who} SET default_transaction_read_only = on")
        for member_of in await conn.fetch(
                "SELECT g.rolname FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid "
                "JOIN pg_roles u ON u.oid = m.member WHERE u.rolname = $1", role):
            await conn.execute(f"REVOKE {_ident(member_of['rolname'])} FROM {who}")
        database = await conn.fetchval("SELECT current_database()")
        await conn.execute(f"GRANT CONNECT ON DATABASE {_ident(database)} TO {who}")
        await conn.execute(f"REVOKE CREATE ON SCHEMA {where} FROM {who}")
        await conn.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {where} FROM {who}")
        await conn.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {where} FROM {who}")
        await conn.execute(f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA {where} FROM {who}")
        await conn.execute(f"GRANT USAGE ON SCHEMA {where} TO {who}")
        for table in tables:
            await conn.execute(f"GRANT SELECT ON {where}.{_ident(table)} TO {who}")


async def check(conn, role: str, tables: Sequence[str], schema: str = "public") -> List[str]:
    """What is wrong with *role* in the connected database: [] when it reads exactly *tables*
    of *schema*, may write none of its tables and create nothing there."""
    row = await conn.fetchrow(_ROLE_CHECK_SQL, role)
    if row is None:
        return [f"role {role} does not exist"]
    problems = [text for flag, text in (
        ("rolsuper", "is a superuser"), ("rolcreatedb", "can create databases"),
        ("rolcreaterole", "can create roles"), ("rolreplication", "can replicate"),
        ("rolbypassrls", "bypasses row security"),
        ("member_of_a_role", "is a member of another role")) if row[flag]]
    if not row["rolcanlogin"]:
        problems.append("cannot log in")
    if not row["read_only_default"]:
        problems.append("does not default to read-only transactions")
    if await conn.fetchval("SELECT has_schema_privilege($1, $2, 'CREATE')", role, schema):
        problems.append(f"may create objects in schema {schema}")
    for name in await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = $1", schema):
        table = f"{_ident(schema)}.{_ident(name['tablename'])}"
        wanted = name["tablename"] in tables
        if await conn.fetchval("SELECT has_table_privilege($1, $2, 'SELECT')", role,
                               table) != wanted:
            problems.append(f"{'cannot' if wanted else 'may'} read {schema}.{name['tablename']}")
        for privilege in _WRITE_PRIVILEGES:
            if await conn.fetchval("SELECT has_table_privilege($1, $2, $3)", role, table,
                                   privilege):
                problems.append(f"holds {privilege} on {schema}.{name['tablename']}")
    return [f"role {role} {p}" for p in problems]


async def _connect(dsn: str, database: str, deadline: float):
    """A connection to *database*, retried until *deadline* while the server or the
    database is not there yet (Langfuse creates its own database at its first boot)."""
    import asyncpg
    while True:
        try:
            return await asyncpg.connect(dsn, database=database)
        except (OSError, asyncpg.PostgresError) as exc:
            if time.monotonic() >= deadline:
                raise
            logger.info("database %s not reachable yet: %s", database, exc)
            await asyncio.sleep(2)


async def _wait_for_tables(conn, tables: Sequence[str], schema: str, deadline: float) -> None:
    while True:
        missing = [t for t in tables if not await conn.fetchval(
            "SELECT to_regclass($1)::text", f"{_ident(schema)}.{_ident(t)}")]
        if not missing:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f"tables still missing in {schema}: {', '.join(missing)}")
        logger.info("waiting for %s to be created", ", ".join(missing))
        await asyncio.sleep(2)


async def run(dsn: str, role: str, password: str, grants: Sequence[Grant], schema: str,
              wait_seconds: float) -> Dict[str, List[str]]:
    """Apply then check every grant; ``{database: problems}``."""
    deadline = time.monotonic() + wait_seconds
    out = {}
    for database, tables in grants:
        conn = await _connect(dsn, database, deadline)
        try:
            await _wait_for_tables(conn, tables, schema, deadline)
            await apply(conn, role, password, tables, schema)
            out[database] = await check(conn, role, tables, schema)
        finally:
            await conn.close()
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m readonly_role",
        description="Create or re-align a SELECT-only login role (run as the owner of the "
                    "tables: DATABASE_URL). Its password comes from READONLY_ROLE_PASSWORD.")
    parser.add_argument("--role", default=os.getenv("READONLY_ROLE", ""),
                        help="the role's name (default: READONLY_ROLE)")
    parser.add_argument("--grant", action="append", default=[], metavar="DATABASE:TABLES",
                        help="a database and the comma-separated tables the role may read "
                             "there; repeat for more databases")
    parser.add_argument("--schema", default="public")
    parser.add_argument("--wait-seconds", type=float, default=300.0,
                        help="how long to wait for the server, databases and tables (300)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                        format="%(levelname)s %(name)s: %(message)s")
    dsn, password = os.getenv("DATABASE_URL", ""), os.getenv("READONLY_ROLE_PASSWORD", "")
    try:
        if not dsn:
            raise ValueError("DATABASE_URL (the owner) must be set")
        _check_role(args.role, password)
        if not _ROLE_RE.match(args.schema):
            raise ValueError(f"not a plain lower-case schema name: {args.schema!r}")
        grants = [parse_grant(g) for g in args.grant]
        if not grants:
            raise ValueError("name at least one --grant DATABASE:TABLE[,TABLE...]")
    except ValueError as exc:
        print(f"readonly_role: {exc}", file=sys.stderr)
        return 2
    try:
        result = asyncio.run(run(dsn, args.role, password, grants, args.schema,
                                 args.wait_seconds))
    except Exception as exc:
        print(f"readonly_role: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    problems = [p for found in result.values() for p in found]
    for problem in problems:
        print(f"readonly_role: {problem}", file=sys.stderr)
    if problems:
        return 1
    for database, tables in grants:
        print(f"readonly_role: {args.role} reads {database}: {', '.join(tables)} (only)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
