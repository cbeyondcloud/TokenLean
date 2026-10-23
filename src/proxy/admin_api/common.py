"""What the admin APIs share: who the caller is and what it may manage, the request body, and
the audit row every change writes.

Scope (owner decision 82's rule, the one /admin/usage-export applies): an admin key with no
team binding manages every tenant (the cross-tenant admin scope); an admin key bound to a
team manages only its own team of its own tenant.

Audit: every change is one ``audit_events`` row through ``AuditLogger.log_config_change``
(the portal's config-change rows use the same call). ``admin_api.require_audit`` (default
true) refuses changes while the audit log is not wired (no DATABASE_URL, or the database not
reached yet), so no change goes unrecorded.
"""
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from fastapi import HTTPException

logger = logging.getLogger(__name__)


MAX_BODY_BYTES = 1024 * 1024


class AdminError(HTTPException):
    """An HTTPException with a structured ``detail`` (field errors for a 422)."""


def field_error(field: str, message: str) -> AdminError:
    return AdminError(status_code=422, detail={"error": "invalid_request",
                                               "errors": [{"field": field,
                                                           "message": message}]})


@dataclass(frozen=True)
class AdminScope:
    """What an admin caller may manage. ``team`` set: only that team of ``tenant_id``."""
    caller_tenant: str
    team: Optional[str]
    actor: str

    @property
    def team_bound(self) -> bool:
        return self.team is not None


def admin_scope(tenant_metadata: Optional[dict], api_key: str) -> AdminScope:
    """The scope of a caller already checked to hold an admin key. ``actor`` names the
    caller in audit rows: ``admin-key:<tenant>[:team=<team>]:key=<key id>``."""
    from auth.api_key_manager import key_id, key_team
    meta = tenant_metadata if isinstance(tenant_metadata, dict) else {}
    tenant = str(meta.get("tenant_id") or "default")
    team = key_team(meta)
    caller_key = key_id(hashlib.sha256((api_key or "").encode("utf-8")).hexdigest())
    actor = f"admin-key:{tenant}" + (f":team={team}" if team else "") + f":key={caller_key}"
    return AdminScope(caller_tenant=tenant, team=team, actor=actor)


async def json_object(request) -> Dict[str, Any]:
    """The request body as a JSON object; an empty body is ``{}``. Anything else is a 422."""
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise AdminError(status_code=413, detail=f"request body over {MAX_BODY_BYTES} bytes")
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        raise AdminError(status_code=422, detail={"error": "invalid_request",
                                                  "errors": [{"field": "body",
                                                              "message": "not valid JSON"}]})
    if not isinstance(body, dict):
        raise field_error("body", "must be a JSON object")
    return body


def audit_required(config: Any) -> bool:
    section = config.get("admin_api") if isinstance(config, dict) else None
    value = section.get("require_audit", True) if isinstance(section, dict) else True
    return value is not False


def ensure_auditable(audit_logger, config: Any) -> None:
    """Refuse a change (503) that could not be audited, unless require_audit is off."""
    if audit_logger is None and audit_required(config):
        raise HTTPException(status_code=503, detail=(
            "The audit log is not wired (DATABASE_URL unset or the database not reached), so "
            "admin changes are refused. Set admin_api.require_audit: false to allow them "
            "unaudited."))


async def audit(audit_logger, *, tenant_id: str, actor: str, action: str,
                details: Dict[str, Any]) -> None:
    """Write one audit row for a change that has been made. A failed write cannot undo the
    change: it is logged at ERROR with the row (never a secret: callers pass ids only)."""
    if audit_logger is None:
        logger.warning("audit (not wired): %s tenant=%s actor=%s details=%s", action,
                       tenant_id, actor, json.dumps(details, sort_keys=True, default=str))
        return
    ok = await audit_logger.log_config_change(tenant_id=tenant_id, actor=actor, action=action,
                                              details=details)
    if ok is False:
        logger.error("AUDIT WRITE FAILED for a change already made: %s tenant=%s actor=%s "
                     "details=%s", action, tenant_id, actor,
                     json.dumps(details, sort_keys=True, default=str))
