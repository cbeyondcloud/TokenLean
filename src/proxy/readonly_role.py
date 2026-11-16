"""``python -m readonly_role``: a SELECT-only login for dashboards, then exit.

A dashboard tool (Grafana) only reads. Logging it in as the owner of the tables gives every
dashboard query, and anyone who can edit or craft one, the owner's rights: write and drop the
tables, and on a stack whose owner is a superuser (the LITE local Postgres) run programs on
the database server. This creates or re-aligns a second LOGIN role, run as the owner, that:

* is LOGIN only: no SUPERUSER, CREATEDB, CREATEROLE, REPLICATION or BYPASSRLS, a member of
  no role, with ``default_transaction_read_only`` on, a ``statement_timeout`` and a
  connection limit;
* in each database named by ``--grant``, has CONNECT, USAGE on the schema, and SELECT on
  exactly the tables named: every other privilege it holds on that schema's tables,
  sequences and functions, CREATE on the schema and TEMP on the database are revoked. Run
  again with another table list, it follows the list. ``--revoke DATABASE`` takes back
  everything it holds in a database it should no longer read (skipped when that database
  does not exist).

"SELECT-only" means it cannot change a table. It is not a guard against resource abuse: a
query can still be slow (the statement timeout bounds it), and PUBLIC's defaults still apply
to it (TEMP on every database; EXECUTE on functions, so a session that switches the read-only
default off could write a large object unless ``--lock-large-objects`` revokes those from
PUBLIC in that database).

Before it changes anything it checks an existing role of that name, and refuses one it must
not take over: a superuser, a role with CREATEDB, CREATEROLE, REPLICATION or BYPASSRLS, one
that owns any object, or one with rights outside the target schema (CREATE on the database,
tables in other schemas, any grant option). A mistyped name that is a real admin is left as
it is, password included. A role it created itself passes.

It is idempotent, so a stack runs it at every start (the LITE local stack's
``grafana-db-reader`` service, the LITE chart's migration hook when dashboards.reader is on).
A database or table may not exist yet when it starts (the proxy creates usage_events at its
own startup, Langfuse its database and tables at its first boot): it waits up to
``--wait-seconds`` for them, then fails loudly. A wrong owner password fails at once.

PostgreSQL 14 and earlier give PUBLIC CREATE on schema ``public``. That is PUBLIC's right,
not the role's: it is reported, not refused (``REVOKE CREATE ON SCHEMA public FROM PUBLIC``
removes it). On PostgreSQL 16 and later, a CREATEROLE owner may only alter roles it
administers (the ones it created), so a role made by someone else is refused by the server.

Connection: ``DATABASE_URL`` (the owner; its password may come from ``PGPASSWORD``). Each
database named connects to the same server. The role's password comes from
``READONLY_ROLE_PASSWORD``, never the command line, and reaches the server only as a SCRAM
verifier, so a statement log never records it.

Exit codes: 0 the role reads exactly the tables named; 1 refused, a step failed, or a check
did not hold; 2 a setting is missing or invalid.
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

DEFAULT_STATEMENT_TIMEOUT = "60s"
# Grafana's two LITE datasources keep at most 10 + 5 connections open.
DEFAULT_CONNECTION_LIMIT = 20

# A database name as a managed service allows one (quoted wherever it is used).
_DATABASE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$-]{0,62}$")
_TIMEOUT_RE = re.compile(r"^[0-9]{1,7}(ms|s|min)?$")

_WRITE_PRIVILEGES = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")

# Large-object functions that create, change or delete one (TokenLean uses none).
LARGE_OBJECT_WRITES = (
    "lo_creat(integer)", "lo_create(oid)", "lo_from_bytea(oid, bytea)", "lo_import(text)",
    "lo_import(text, oid)", "lo_put(oid, bigint, bytea)", "lowrite(integer, bytea)",
    "lo_truncate(integer, integer)", "lo_truncate64(integer, bigint)", "lo_unlink(oid)")

_ROLE_SQL = """
SELECT r.oid, r.rolsuper, r.rolcreatedb, r.rolcreaterole, r.rolreplication, r.rolbypassrls,
       r.rolcanlogin,
       EXISTS (SELECT 1 FROM pg_auth_members m WHERE m.member = r.oid) AS member_of_a_role,
       COALESCE((SELECT bool_or(u.c = 'default_transaction_read_only=on')
                 FROM unnest(r.rolconfig) AS u(c)), false) AS read_only_default
FROM pg_roles r WHERE r.rolname = $1
"""

_ATTRIBUTES = (("rolsuper", "is a superuser"), ("rolcreatedb", "can create databases"),
               ("rolcreaterole", "can create roles"), ("rolreplication", "can replicate"),
               ("rolbypassrls", "bypasses row security"))

# Tables (and views) the role may read outside the target schema, system schemas aside.
_OTHER_SCHEMA_TABLES_SQL = """
SELECT n.nspname || '.' || c.relname
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f') AND n.nspname <> $2
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg\\_toast%' AND n.nspname NOT LIKE 'pg\\_temp%'
  AND has_table_privilege($1, c.oid, 'SELECT')
ORDER BY 1 LIMIT 5
"""

# The role's own grants (never PUBLIC's) that carry a grant option, in this database.
_GRANT_OPTIONS_SQL = """
SELECT 'table ' || c.oid::regclass::text FROM pg_class c, aclexplode(c.relacl) a
  WHERE a.grantee = $1 AND a.is_grantable
UNION ALL SELECT 'schema ' || n.nspname FROM pg_namespace n, aclexplode(n.nspacl) a
  WHERE a.grantee = $1 AND a.is_grantable
UNION ALL SELECT 'database ' || d.datname FROM pg_database d, aclexplode(d.datacl) a
  WHERE a.grantee = $1 AND a.is_grantable
UNION ALL SELECT 'function ' || p.oid::regprocedure::text FROM pg_proc p, aclexplode(p.proacl) a
  WHERE a.grantee = $1 AND a.is_grantable
LIMIT 5
"""

# CREATE on a schema granted to the role itself, or to PUBLIC.
_SCHEMA_CREATE_SQL = """
SELECT a.grantee FROM pg_namespace n, aclexplode(n.nspacl) a
WHERE n.nspname = $1 AND a.privilege_type = 'CREATE' AND a.grantee IN ($2, 0)
"""


class Refused(Exception):
    """An existing role the job must not take over; nothing was changed."""


def parse_grant(text: str) -> Grant:
    """``DATABASE:TABLE[,TABLE...]`` -> (database, tables)."""
    database, sep, tables = text.partition(":")
    database = database.strip()
    names = tuple(t.strip() for t in tables.split(",") if t.strip())
    if not sep or not database or not names:
        raise ValueError(f"--grant needs DATABASE:TABLE[,TABLE...], got {text!r}")
    _check_database(database)
    for name in names:
        if not _ROLE_RE.match(name):
            raise ValueError(f"not a plain lower-case table name: {name!r}")
    return database, names


def _check_database(name: str) -> str:
    if not _DATABASE_RE.match(name):
        raise ValueError(f"not a database name: {name!r}")
    return name


def _check_role(role: str, password: str) -> None:
    if not _ROLE_RE.match(role):
        raise ValueError(f"invalid role name: {role!r}")
    if not password or not password.isascii() or not password.isprintable():
        raise ValueError("READONLY_ROLE_PASSWORD must be set, in printable ASCII")


async def precheck(conn, role: str, schema: str = "public") -> List[str]:
    """Why an EXISTING *role* must not be taken over, seen from the connected database:
    [] when it may (or does not exist yet). Changes nothing."""
    row = await conn.fetchrow(_ROLE_SQL, role)
    if row is None:
        return []
    oid = row["oid"]
    problems = [text for flag, text in _ATTRIBUTES if row[flag]]
    owned = await conn.fetchval(
        "SELECT count(*) FROM pg_shdepend WHERE refclassid = 'pg_authid'::regclass "
        "AND refobjid = $1 AND deptype = 'o'", oid)
    owned += await conn.fetchval("SELECT count(*) FROM pg_class WHERE relowner = $1", oid)
    if owned:
        problems.append(f"owns {owned} object(s)")
    if await conn.fetchval("SELECT has_database_privilege($1, current_database(), 'CREATE')",
                           role):
        problems.append("may create schemas in the database")
    others = [r[0] for r in await conn.fetch(_OTHER_SCHEMA_TABLES_SQL, role, schema)]
    if others:
        problems.append(f"reads tables outside schema {schema}: {', '.join(others)}")
    options = [r[0] for r in await conn.fetch(_GRANT_OPTIONS_SQL, oid)]
    if options:
        problems.append(f"holds a grant option on {', '.join(options)}")
    return [f"role {role} {p}" for p in problems]


async def _create_role(conn, who: str, role: str) -> None:
    """CREATE ROLE, tolerating a concurrent run that created it first."""
    import asyncpg
    try:
        async with conn.transaction():
            await conn.execute(f"CREATE ROLE {who} LOGIN")
    except (asyncpg.DuplicateObjectError, asyncpg.UniqueViolationError):
        logger.info("role %s was created by a concurrent run; aligning it", role)


async def apply(conn, role: str, password: str, tables: Sequence[str],
                schema: str = "public", statement_timeout: str = DEFAULT_STATEMENT_TIMEOUT,
                connection_limit: int = DEFAULT_CONNECTION_LIMIT,
                lock_large_objects: bool = False) -> None:
    """As the owner, connected to the database the *tables* are in: create or re-align *role*
    so that it reads exactly *tables* of *schema* there and nothing else. Refuses (Refused,
    nothing changed) an existing role ``precheck`` objects to."""
    _check_role(role, password)
    if not _TIMEOUT_RE.match(statement_timeout):
        raise ValueError(f"not a statement timeout: {statement_timeout!r}")
    if role == await conn.fetchval("SELECT current_user::text"):
        raise Refused(f"{role} is the connected owner itself: name another role")
    who, where = _ident(role), _ident(schema)
    async with conn.transaction():
        problems = await precheck(conn, role, schema)
        if problems:
            raise Refused("; ".join(problems))
        if not await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role):
            await _create_role(conn, who, role)
        # Only the attributes a non-superuser owner may set (a managed service's is one).
        await conn.execute(f"ALTER ROLE {who} WITH LOGIN NOCREATEDB NOCREATEROLE "
                           f"CONNECTION LIMIT {int(connection_limit)} "
                           f"PASSWORD {_literal(_scram_verifier(password))}")
        await conn.execute(f"ALTER ROLE {who} SET default_transaction_read_only = on")
        await conn.execute(f"ALTER ROLE {who} SET statement_timeout = "
                           f"{_literal(statement_timeout)}")
        for member_of in await conn.fetch(
                "SELECT g.rolname FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid "
                "JOIN pg_roles u ON u.oid = m.member WHERE u.rolname = $1", role):
            await conn.execute(f"REVOKE {_ident(member_of['rolname'])} FROM {who}")
        database = _ident(await conn.fetchval("SELECT current_database()"))
        await conn.execute(f"GRANT CONNECT ON DATABASE {database} TO {who}")
        await conn.execute(f"REVOKE TEMPORARY ON DATABASE {database} FROM {who}")
        await conn.execute(f"REVOKE CREATE ON SCHEMA {where} FROM {who}")
        await conn.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {where} FROM {who}")
        await conn.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {where} FROM {who}")
        await conn.execute(f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA {where} FROM {who}")
        await conn.execute(f"GRANT USAGE ON SCHEMA {where} TO {who}")
        for table in tables:
            await conn.execute(f"GRANT SELECT ON {where}.{_ident(table)} TO {who}")
    if lock_large_objects:
        await _lock_large_objects(conn)


async def _lock_large_objects(conn) -> None:
    """REVOKE the large-object write functions FROM PUBLIC in this database. Needs the
    functions' owner (a superuser); otherwise it is reported and left as it is."""
    import asyncpg
    for function in LARGE_OBJECT_WRITES:
        try:
            async with conn.transaction():
                await conn.execute(f"REVOKE EXECUTE ON FUNCTION pg_catalog.{function} "
                                   "FROM PUBLIC")
        except asyncpg.PostgresError as exc:
            logger.warning("could not revoke %s from PUBLIC: %s", function, exc)


async def revoke(conn, role: str, schema: str = "public") -> None:
    """Take back everything *role* holds in the connected database's *schema*."""
    if not await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role):
        return
    who, where = _ident(role), _ident(schema)
    database = _ident(await conn.fetchval("SELECT current_database()"))
    async with conn.transaction():
        await conn.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {where} FROM {who}")
        await conn.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {where} FROM {who}")
        await conn.execute(f"REVOKE ALL ON SCHEMA {where} FROM {who}")
        await conn.execute(f"REVOKE ALL ON DATABASE {database} FROM {who}")


async def check(conn, role: str, tables: Sequence[str], schema: str = "public") -> List[str]:
    """What is wrong with *role* in the connected database: [] when it reads exactly *tables*
    of *schema*, may write none of its tables, create nothing there or in the database, and
    reads no table in another schema. PUBLIC's own rights are reported, not counted."""
    row = await conn.fetchrow(_ROLE_SQL, role)
    if row is None:
        return [f"role {role} does not exist"]
    problems = [text for flag, text in _ATTRIBUTES if row[flag]]
    if row["member_of_a_role"]:
        problems.append("is a member of another role")
    if not row["rolcanlogin"]:
        problems.append("cannot log in")
    if not row["read_only_default"]:
        problems.append("does not default to read-only transactions")
    if await conn.fetchval("SELECT has_database_privilege($1, current_database(), 'CREATE')",
                           role):
        problems.append("may create schemas in the database")
    grantees = {r[0] for r in await conn.fetch(_SCHEMA_CREATE_SQL, schema, row["oid"])}
    if row["oid"] in grantees:
        problems.append(f"may create objects in schema {schema}")
    if 0 in grantees:
        logger.warning("PUBLIC may create objects in schema %s (the default before "
                       "PostgreSQL 15); REVOKE CREATE ON SCHEMA %s FROM PUBLIC removes it",
                       schema, schema)
    others = [r[0] for r in await conn.fetch(_OTHER_SCHEMA_TABLES_SQL, role, schema)]
    if others:
        problems.append(f"reads tables outside schema {schema}: {', '.join(others)}")
    for name in await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = $1",
                                 schema):
        table = f"{_ident(schema)}.{_ident(name['tablename'])}"
        wanted = name["tablename"] in tables
        if await conn.fetchval("SELECT has_table_privilege($1, $2, 'SELECT')", role,
                               table) != wanted:
            problems.append(f"{'cannot' if wanted else 'may'} read {schema}.{name['tablename']}")
        for privilege in _WRITE_PRIVILEGES:
            if await conn.fetchval("SELECT has_table_privilege($1, $2, $3)", role, table,
                                   privilege):
                problems.append(f"holds {privilege} on {schema}.{name['tablename']}")
    if tables and await conn.fetchval(
            "SELECT has_database_privilege($1, current_database(), 'TEMPORARY')", role):
        logger.info("role %s may create temporary tables through PUBLIC's default TEMP "
                    "right on this database", role)
    return [f"role {role} {p}" for p in problems]


def _is_auth_failure(exc) -> bool:
    import asyncpg
    return isinstance(exc, (asyncpg.InvalidPasswordError,
                            asyncpg.InvalidAuthorizationSpecificationError))


async def _connect(dsn: str, database: str, deadline: float, missing_ok: bool = False):
    """A connection to *database*, retried until *deadline* while the server or the database
    is not there yet (Langfuse creates its own database at its first boot). An
    authentication failure is never retried. *missing_ok*: None when the database does not
    exist, at once."""
    import asyncpg
    while True:
        try:
            return await asyncpg.connect(dsn, database=database, timeout=10)
        except (OSError, asyncio.TimeoutError, asyncpg.PostgresError) as exc:
            if _is_auth_failure(exc):
                raise
            if missing_ok and isinstance(exc, asyncpg.InvalidCatalogNameError):
                return None
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
              wait_seconds: float, revokes: Sequence[str] = (),
              statement_timeout: str = DEFAULT_STATEMENT_TIMEOUT,
              connection_limit: int = DEFAULT_CONNECTION_LIMIT,
              lock_large_objects: Sequence[str] = ()) -> Dict[str, List[str]]:
    """Check every database first (Refused: nothing changed anywhere), then apply and check
    every grant and revoke; ``{database: problems}``."""
    deadline = time.monotonic() + wait_seconds
    conns = {}
    try:
        for database, _tables in grants:
            conns[database] = await _connect(dsn, database, deadline)
        for database in revokes:
            conn = await _connect(dsn, database, deadline, missing_ok=True)
            if conn is None:
                logger.info("database %s does not exist: nothing to revoke", database)
            else:
                conns[database] = conn
        if role == await conns[grants[0][0]].fetchval("SELECT current_user::text"):
            raise Refused(f"{role} is the connected owner itself: name another role")
        problems = [p for conn in conns.values() for p in await precheck(conn, role, schema)]
        if problems:
            raise Refused("; ".join(dict.fromkeys(problems)))
        out = {}
        for database, tables in grants:
            conn = conns[database]
            await _wait_for_tables(conn, tables, schema, deadline)
            await apply(conn, role, password, tables, schema, statement_timeout,
                        connection_limit, database in lock_large_objects)
            out[database] = await check(conn, role, tables, schema)
        for database in revokes:
            if database in conns:
                await revoke(conns[database], role, schema)
                out[database] = await check(conns[database], role, (), schema)
        return out
    finally:
        for conn in conns.values():
            await conn.close()


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
    parser.add_argument("--revoke", action="append", default=[], metavar="DATABASE",
                        help="a database the role should no longer read (skipped when it "
                             "does not exist); repeatable")
    parser.add_argument("--lock-large-objects", action="append", default=[],
                        metavar="DATABASE",
                        help="a granted database where the large-object write functions are "
                             "revoked from PUBLIC (needs their owner, a superuser)")
    parser.add_argument("--schema", default="public")
    parser.add_argument("--statement-timeout", default=DEFAULT_STATEMENT_TIMEOUT,
                        help=f"the role's statement_timeout ({DEFAULT_STATEMENT_TIMEOUT})")
    parser.add_argument("--connection-limit", type=int, default=DEFAULT_CONNECTION_LIMIT,
                        help=f"the role's connection limit ({DEFAULT_CONNECTION_LIMIT})")
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
        if not _TIMEOUT_RE.match(args.statement_timeout):
            raise ValueError(f"not a statement timeout (e.g. 60s): {args.statement_timeout!r}")
        if not 1 <= args.connection_limit <= 10000:
            raise ValueError("--connection-limit must be 1 to 10000")
        grants = [parse_grant(g) for g in args.grant]
        if not grants:
            raise ValueError("name at least one --grant DATABASE:TABLE[,TABLE...]")
        revokes = [_check_database(d) for d in args.revoke]
        granted = {d for d, _ in grants}
        if granted & set(revokes):
            raise ValueError("a database cannot be both granted and revoked")
        for database in args.lock_large_objects:
            if database not in granted:
                raise ValueError(f"--lock-large-objects {database}: not a granted database")
    except ValueError as exc:
        print(f"readonly_role: {exc}", file=sys.stderr)
        return 2
    try:
        result = asyncio.run(run(dsn, args.role, password, grants, args.schema,
                                 args.wait_seconds, revokes, args.statement_timeout,
                                 args.connection_limit, args.lock_large_objects))
    except Refused as exc:
        print(f"readonly_role: refused, nothing was changed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        if _is_auth_failure(exc):
            print("readonly_role: the owner's login was refused (authentication failed): "
                  "check DATABASE_URL / PGPASSWORD", file=sys.stderr)
        else:
            print(f"readonly_role: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    problems = [p for found in result.values() for p in found]
    for problem in problems:
        print(f"readonly_role: {problem}", file=sys.stderr)
    if problems:
        return 1
    for database, tables in grants:
        print(f"readonly_role: {args.role} reads {database}: {', '.join(tables)} (only)")
    for database in revokes:
        if database in result:
            print(f"readonly_role: {args.role} reads nothing in {database}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
