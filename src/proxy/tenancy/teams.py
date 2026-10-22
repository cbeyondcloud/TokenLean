"""Teams under a tenant (ST-1): a team's settings, its spend cap and its config overlay.

A request's team is ``ctx.team`` (``tenancy.resolver.resolve_team``): the team its proxy key
is bound to, else a gateway key's ``X-Team``, else ``"default"``. ``"default"`` means "no
team": it never has settings, a cap or an overlay, so a deployment that binds no key to a
team and configures no ``teams:`` block behaves exactly as before.

Where a team's block comes from, most specific last (it wins):

1. ``teams.<team>`` in the effective config: the platform file, or the tenant's own
   ``tenant_configs`` overrides, which the pipeline has already merged in;
2. ``tenants.<tenant>.teams.<team>``: the operator's per-tenant block in the config file.

A block may carry:

* ``monthly_spend_cap_usd`` — the team's own monthly USD ceiling, checked by G00 against the
  team's spend counter while ``rate_limit.spend_cap.enabled`` is on. The tenant's cap still
  applies to the team's requests: a team cannot spend past its tenant.
* ``config`` — an overlay deep-merged over the tenant's config for the team's requests,
  with the CONFIG_OVERLAYS rules (``config_loader.deep_merge``: mappings merge key by key,
  anything else replaces, a null changes nothing). Only the sections in
  :data:`TEAM_OVERLAY_SECTIONS` may change — how a request is served. Who pays, how much,
  what is recorded and which providers exist stay the tenant's, so a team overlay can
  never lift the tenant's cap, switch off a gate or a metering column, or add a provider.

Every reader type-checks each level: a mis-indented key in operator YAML degrades to "no
team settings", never an error in the request path.
"""
import logging
import re
from typing import Any, Dict, Optional, Tuple

from config_loader import deep_merge
from tenancy.resolver import DEFAULT_TEAM

logger = logging.getLogger(__name__)

#: Top-level config sections a team overlay may change.
TEAM_OVERLAY_SECTIONS = ("groups", "proxy")

# A team name bound to a key at issuance: short, and safe as a Redis key segment, a metric
# label and a log field without escaping. "default" is reserved for "no team".
_TEAM_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")


def valid_team_name(name: Any) -> bool:
    """Whether ``name`` may be bound to a proxy key: 1-64 of ``A-Z a-z 0-9 . _ -``, and
    not the reserved ``"default"``."""
    return isinstance(name, str) and name != DEFAULT_TEAM and bool(_TEAM_NAME.fullmatch(name))


def _mapping(node: Any, *path: str) -> Dict[str, Any]:
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else {}


def team_settings(config: Any, tenant_id: str, team: str) -> Dict[str, Any]:
    """The team's block: ``teams.<team>`` with ``tenants.<tenant>.teams.<team>`` merged over
    it. ``{}`` for the default team, an unknown team, or malformed config. A new dict when
    both exist; otherwise the block itself, which callers must not modify."""
    if not team or team == DEFAULT_TEAM or not isinstance(config, dict):
        return {}
    base = _mapping(config, "teams", team)
    operator = _mapping(config, "tenants", tenant_id, "teams", team)
    if base and operator:
        return deep_merge(base, operator)
    return operator or base


def team_monthly_spend_cap(config: Any, tenant_id: str, team: str) -> Optional[float]:
    """The team's ``monthly_spend_cap_usd`` as a positive float, or ``None`` (no cap) when it
    is absent, zero, negative or not a number."""
    raw = team_settings(config, tenant_id, team).get("monthly_spend_cap_usd")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        cap = float(raw)
    except (TypeError, ValueError):
        return None
    return cap if cap > 0 else None


def team_overlay(config: Any, tenant_id: str, team: str) -> Dict[str, Any]:
    """The part of the team's ``config`` overlay that may apply: the mapping sections named
    in :data:`TEAM_OVERLAY_SECTIONS`. Anything else is dropped with a WARNING naming the
    sections (never their values)."""
    raw = team_settings(config, tenant_id, team).get("config")
    if not isinstance(raw, dict):
        return {}
    allowed = {k: v for k, v in raw.items()
               if k in TEAM_OVERLAY_SECTIONS and isinstance(v, dict) and v}
    dropped = sorted(str(k) for k in raw if k not in allowed and raw[k] is not None)
    if dropped:
        logger.warning("team %r of tenant %r: config overlay section(s) %s ignored; a team "
                       "overlay may change only %s", team, tenant_id, dropped,
                       list(TEAM_OVERLAY_SECTIONS))
    return allowed


def apply_team_overlay(config: Dict[str, Any], tenant_id: str,
                       team: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """``(config, overlay)``: the config with the team's overlay merged in, and the overlay
    that was applied. With no overlay, the SAME config object and ``{}`` (no copy, no cost);
    otherwise a new dict, as ``config`` may be the process-wide ``get_config()`` dict."""
    overlay = team_overlay(config, tenant_id, team)
    if not overlay:
        return config, {}
    return deep_merge(config, overlay), overlay
