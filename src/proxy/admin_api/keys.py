"""/admin/keys (ST-9): create, list, revoke and rotate proxy keys.

The engine is ``auth.api_key_manager`` (the same writes ``issue-key.sh`` and the management
console make), over whichever key store ``key_store.backend`` selects. A key is named by its
``key_id`` (the first 16 hex characters of its sha256). The raw key appears in a response
exactly once, on create and on rotate; the store keeps only its hash, and a listing shows
neither. A key may be bound to a team and to a user of its tenant; the binding is the
request's team and user whenever the key is used.

Scope (``admin_api.common``): an unbound admin key manages every tenant's keys; a team-bound
admin key only its own team's keys of its own tenant. For such a key a listing is that team's
whatever the query says; a create naming another tenant or team, or asking for a trust flag
(``admin``, ``gateway``), is refused (403); revoking or rotating another team's key is "not
found" (404), as is any id that names no key. Every change writes one audit row
(``proxy_key.created`` / ``.revoked`` / ``.rotated``) with ids only, never a key or a hash.
"""
import asyncio
import json
import logging
import re
from typing import Any, Dict, Optional

from fastapi import HTTPException

from admin_api.common import (
    AdminError, AdminScope, audit, ensure_auditable, field_error,
)
from auth import api_key_manager as km
from tenancy.teams import valid_team_name

logger = logging.getLogger(__name__)

TIERS = ("free", "enterprise")
CREATE_FIELDS = ("tenant_id", "team", "user", "tier", "label", "admin", "gateway")
_TENANT_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")
_MAX_LABEL = 128

NOT_FOUND = "No proxy key with that id in your scope."
ONCE = "Store this key now: it is shown only once and cannot be retrieved again."


def _store_unavailable(exc: Exception) -> HTTPException:
    logger.warning("/admin/keys: key store unavailable: %s", exc)
    return HTTPException(status_code=503, detail="The proxy key store is unavailable.")


async def _engine(fn, *args, **kwargs):
    """Run a (blocking) engine call on a worker thread; a store failure is a 503."""
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except km.AmbiguousKeyId as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except km.KeyScopeError as exc:
        raise HTTPException(status_code=403, detail=(
            "A team-bound admin key cannot bind a user who holds a key of the tenant outside "
            "its team.")) from exc
    except json.JSONDecodeError as exc:  # a corrupt store, not a bad request
        raise _store_unavailable(exc) from exc
    except ValueError as exc:
        raise field_error("body", str(exc)) from exc
    except Exception as exc:  # the store did not answer (file, Secret Manager, Postgres)
        raise _store_unavailable(exc) from exc


def _engine_scope(scope: AdminScope) -> Dict[str, Optional[str]]:
    if scope.team_bound:
        return {"tenant_id": scope.caller_tenant, "team": scope.team}
    return {"tenant_id": None, "team": None}


def _check_create(body: Dict[str, Any], scope: AdminScope) -> Dict[str, Any]:
    """The create_key arguments from a request body, or a 422 naming the first bad field, or
    a 403 when a team-bound admin reaches outside its team."""
    unknown = sorted(k for k in body if k not in CREATE_FIELDS)
    if unknown:
        raise field_error(unknown[0], f"unknown field; allowed: {', '.join(CREATE_FIELDS)}")
    tenant = body.get("tenant_id")
    team = body.get("team")
    if tenant is not None and (not isinstance(tenant, str) or not _TENANT_ID.fullmatch(tenant)):
        raise field_error("tenant_id", "1-128 characters of A-Z a-z 0-9 . _ -")
    if team is not None and not valid_team_name(team):
        raise field_error("team", "1-64 characters of A-Z a-z 0-9 . _ - and not 'default'")
    user = body.get("user")
    if user is not None and not km.valid_user_name(user):
        raise field_error("user", "1-128 characters of A-Z a-z 0-9 . _ @ + -")
    tier = body.get("tier", "free")
    if tier not in TIERS:
        raise field_error("tier", f"one of {', '.join(TIERS)}")
    label = body.get("label")
    if label is not None and (not isinstance(label, str) or not label.isascii()
                              or not label.isprintable() or not 0 < len(label) <= _MAX_LABEL):
        raise field_error("label", f"1-{_MAX_LABEL} printable ASCII characters")
    for flag in ("admin", "gateway"):
        if flag in body and not isinstance(body[flag], bool):
            raise field_error(flag, "true or false")
    if scope.team_bound:
        if tenant not in (None, scope.caller_tenant) or team not in (None, scope.team):
            raise HTTPException(status_code=403, detail=(
                "A team-bound admin key manages only its own team's keys."))
        if body.get("admin") or body.get("gateway"):
            raise HTTPException(status_code=403, detail=(
                "A team-bound admin key cannot issue admin or gateway keys."))
        tenant, team = scope.caller_tenant, scope.team
    elif tenant is None:
        raise field_error("tenant_id", "required: the tenant the key belongs to")
    return {"tenant_id": tenant, "tier": tier, "admin": bool(body.get("admin")),
            "gateway": bool(body.get("gateway")), "team": team, "user": user,
            "extra": {"label": label} if label is not None else None,
            "user_team_scope": scope.team if scope.team_bound and user is not None else None}


async def create(body: Dict[str, Any], scope: AdminScope, audit_logger, config) -> Dict:
    args = _check_create(body, scope)
    ensure_auditable(audit_logger, config)
    tenant = args.pop("tenant_id")
    tier = args.pop("tier")
    admin = args.pop("admin")
    raw, key_hash, metadata = await _engine(km.create_key, tenant, tier, admin, **args)
    view = km.key_view(key_hash, metadata)
    await audit(audit_logger, tenant_id=view["tenant_id"], actor=scope.actor,
                action="proxy_key.created", details=view)
    return {"key": raw, **view, "notice": ONCE}


async def listing(query: Dict[str, str], scope: AdminScope) -> Dict:
    """Every key in the caller's scope matching ``tenant_id`` / ``team`` / ``user``. A
    team-bound admin's listing is its own team's, whatever the query says."""
    if scope.team_bound:
        tenant, team = scope.caller_tenant, scope.team
    else:
        tenant, team = query.get("tenant_id") or None, query.get("team") or None
    keys = await _engine(km.list_keys, tenant_id=tenant, team=team,
                         user=query.get("user") or None)
    return {"keys": keys, "count": len(keys)}


async def revoke(wanted: str, scope: AdminScope, audit_logger, config) -> Dict:
    if not km.valid_key_id(wanted):
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    ensure_auditable(audit_logger, config)
    view = await _engine(km.revoke_key, wanted, **_engine_scope(scope))
    if view is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    await audit(audit_logger, tenant_id=view["tenant_id"], actor=scope.actor,
                action="proxy_key.revoked", details=view)
    return {"revoked": True, **view}


async def rotate(wanted: str, scope: AdminScope, audit_logger, config) -> Dict:
    if not km.valid_key_id(wanted):
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    ensure_auditable(audit_logger, config)
    result = await _engine(km.rotate_key, wanted, **_engine_scope(scope))
    if result is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    raw, key_hash, metadata, old = result
    view = km.key_view(key_hash, metadata)
    await audit(audit_logger, tenant_id=view["tenant_id"], actor=scope.actor,
                action="proxy_key.rotated",
                details={**view, "replaced_key_id": old["key_id"]})
    return {"key": raw, **view, "replaced_key_id": old["key_id"], "notice": ONCE}


__all__ = ["create", "listing", "revoke", "rotate", "AdminError"]
