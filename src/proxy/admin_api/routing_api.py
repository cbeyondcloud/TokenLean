"""HTTP handlers for /admin/routing (ST-10); the fragment, store and apply logic is
``admin_api.routing``.

    GET  /admin/routing                    the current version (version 0: none) and the
                                           version this replica runs
    PUT  /admin/routing                    {fragment, expected_version?, comment?}: validate,
                                           store as the next version, apply
    GET  /admin/routing/history            ?limit=50&before=<version>: newest first
    GET  /admin/routing/versions/{version} one stored version
    POST /admin/routing/rollback           {version, expected_version?, comment?}: store that
                                           version's fragment (version 0: none) as the next
                                           version, re-validated against today's config

Admin keys with no team binding only: the overlay is one document for every tenant, so a
team-bound admin key gets 403 (it could otherwise read or change other tenants' routing).
Every change writes an audit row (``routing_overlay.applied`` / ``.rolled_back``) naming the
version, the previous one, the fragment's sha256 and what it reaches, never its values.
"""
import logging
from typing import Any, Dict, Optional

from fastapi import HTTPException

from admin_api import routing
from admin_api.common import AdminScope, audit, ensure_auditable, field_error

logger = logging.getLogger(__name__)

PUT_FIELDS = ("fragment", "expected_version", "comment")
ROLLBACK_FIELDS = ("version", "expected_version", "comment")
MAX_HISTORY = 200
MAX_VERSION = 2 ** 62  # well inside Postgres BIGINT


def _require_unbound(scope: AdminScope) -> None:
    if scope.team_bound:
        raise HTTPException(status_code=403, detail=(
            "Routing overlays apply to every tenant: a team-bound admin key cannot read or "
            "change them."))


def _require_pool(pool) -> None:
    if pool is None:
        raise HTTPException(status_code=503, detail=(
            "Routing overlays are stored in Postgres: DATABASE_URL is unset or the database "
            "has not been reached yet."))


def _store_failed(exc: Exception) -> HTTPException:
    logger.warning("/admin/routing: store unavailable: %s", exc)
    return HTTPException(status_code=503, detail="The routing overlay store is unavailable.")


def _view(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if row is None:
        return {"version": 0, "fragment": {}, "action": None, "applied_by": None,
                "applied_at": None, "comment": "", "source_version": None,
                "fragment_sha256": None}
    return {"version": row["version"], "fragment": row["fragment"], "action": row["action"],
            "applied_by": row["actor"], "applied_at": row["created_at"],
            "comment": row.get("comment") or "", "source_version": row.get("source_version"),
            "fragment_sha256": row["fragment_sha256"]}


def _optional_version(body: Dict[str, Any], field: str) -> Optional[int]:
    value = body.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_VERSION:
        raise field_error(field, f"a version number from 0 to {MAX_VERSION}")
    return value


def _comment(body: Dict[str, Any]) -> str:
    value = body.get("comment", "")
    if not isinstance(value, str) or len(value) > routing.MAX_COMMENT or not value.isprintable():
        raise field_error("comment", f"a printable string of at most {routing.MAX_COMMENT} "
                                     "characters")
    return value


def _unknown(body: Dict[str, Any], allowed) -> None:
    unknown = sorted(k for k in body if k not in allowed)
    if unknown:
        raise field_error(unknown[0], f"unknown field; allowed: {', '.join(allowed)}")


def _invalid(exc: routing.FragmentError) -> HTTPException:
    return HTTPException(status_code=422, detail={"error": "invalid_fragment",
                                                  "errors": exc.errors})


def _base_config() -> Dict[str, Any]:
    from config_loader import get_base_config
    return get_base_config()


async def get_current(pool, scope: AdminScope) -> Dict[str, Any]:
    _require_unbound(scope)
    _require_pool(pool)
    try:
        row = await routing.current(pool)
    except Exception as exc:
        raise _store_failed(exc) from exc
    return {**_view(row), "replica_version": routing.applied_version()}


async def _store(pool, fragment, *, action, scope, comment, expected, source, audit_logger):
    try:
        row = await routing.append(pool, fragment, action=action, actor=scope.actor,
                                   comment=comment, expected_version=expected,
                                   source_version=source)
    except routing.VersionConflict as exc:
        raise HTTPException(status_code=409, detail={
            "error": "version_conflict", "current_version": exc.current,
            "message": "Another change was stored first: read it, then retry with "
                       "expected_version set to the current version."}) from exc
    except Exception as exc:
        raise _store_failed(exc) from exc
    routing.apply_row(row)  # this replica at once; the others at their next poll
    await audit(audit_logger, tenant_id=scope.caller_tenant, actor=scope.actor,
                action=f"routing_overlay.{action}",
                details={"version": row["version"], "previous_version": row["version"] - 1,
                         "source_version": source, "fragment_sha256": row["fragment_sha256"],
                         "comment": comment, **routing.touched(fragment)})
    return {**_view(row), "replica_version": routing.applied_version()}


async def put(body: Dict[str, Any], pool, scope: AdminScope, audit_logger,
              config) -> Dict[str, Any]:
    _require_unbound(scope)
    _unknown(body, PUT_FIELDS)
    if "fragment" not in body:
        raise field_error("fragment", "required: the routing and team-limits overlay")
    expected = _optional_version(body, "expected_version")
    comment = _comment(body)
    try:
        routing.validate_fragment(body["fragment"], _base_config())
    except routing.FragmentError as exc:
        raise _invalid(exc) from exc
    _require_pool(pool)
    ensure_auditable(audit_logger, config)
    return await _store(pool, body["fragment"], action="applied", scope=scope, comment=comment,
                        expected=expected, source=None, audit_logger=audit_logger)


async def rollback(body: Dict[str, Any], pool, scope: AdminScope, audit_logger,
                   config) -> Dict[str, Any]:
    _require_unbound(scope)
    _unknown(body, ROLLBACK_FIELDS)
    target = _optional_version(body, "version")
    if target is None:
        raise field_error("version", "required: the version to restore (0: no overlay)")
    expected = _optional_version(body, "expected_version")
    comment = _comment(body)
    _require_pool(pool)
    if target == 0:
        fragment = {}
    else:
        try:
            row = await routing.get_version(pool, target)
        except Exception as exc:
            raise _store_failed(exc) from exc
        if row is None:
            raise HTTPException(status_code=404, detail=f"No routing overlay version {target}.")
        fragment = row["fragment"]
    try:
        routing.validate_fragment(fragment, _base_config())
    except routing.FragmentError as exc:
        raise _invalid(exc) from exc
    ensure_auditable(audit_logger, config)
    return await _store(pool, fragment, action="rolled_back", scope=scope, comment=comment,
                        expected=expected, source=target, audit_logger=audit_logger)


async def get_history(query: Dict[str, str], pool, scope: AdminScope) -> Dict[str, Any]:
    _require_unbound(scope)
    try:
        limit = int(query.get("limit", 50))
        before = int(query["before"]) if query.get("before") else None
    except ValueError:
        raise field_error("limit", "limit and before are whole numbers") from None
    if before is not None and not 0 <= before <= MAX_VERSION:
        raise field_error("before", f"a version number from 0 to {MAX_VERSION}")
    if not 1 <= limit <= MAX_HISTORY:
        raise field_error("limit", f"1-{MAX_HISTORY}")
    _require_pool(pool)
    try:
        rows = await routing.history(pool, limit, before)
    except Exception as exc:
        raise _store_failed(exc) from exc
    versions = [_view(r) for r in rows]
    more = len(rows) == limit and rows[-1]["version"] > 1
    return {"versions": versions, "next_before": rows[-1]["version"] if more else None}


async def get_one(version: int, pool, scope: AdminScope) -> Dict[str, Any]:
    _require_unbound(scope)
    _require_pool(pool)
    try:
        row = await routing.get_version(pool, version)
    except Exception as exc:
        raise _store_failed(exc) from exc
    if row is None:
        raise HTTPException(status_code=404, detail=f"No routing overlay version {version}.")
    return _view(row)
