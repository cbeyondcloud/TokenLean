"""Teams under a tenant (ST-1): a team's settings, its spend cap and its config overlay.

A request's team is ``ctx.team`` (``tenancy.resolver.resolve_team``): the team its proxy key
is bound to, else a gateway key's ``X-Team``, else ``"default"``. ``"default"`` means "no
team": it never has settings, a cap or an overlay, so a deployment that binds no key to a
team and configures no ``teams:`` block behaves exactly as before.

Where a team's block comes from, most specific last (it wins):

1. ``teams.<team>`` in the effective config: the platform file, or the tenant's own
   ``tenant_configs`` overrides, which the pipeline has already merged in;
2. ``tenants.<tenant>.teams.<team>``: the operator's per-tenant block in the config file.

Team keys are matched by their string form, since YAML reads an unquoted ``4421`` as an
integer; a key YAML read as a boolean (``no``, ``on``) cannot be told apart from its
synonyms and never matches. Every non-string team key is named in a WARNING, once.

A block may carry:

* ``monthly_spend_cap_usd`` — the team's own monthly USD ceiling, checked by G00 against the
  team's spend counter while ``rate_limit.spend_cap.enabled`` is on. The tenant's cap still
  applies to the team's requests: a team cannot spend past its tenant. Absent or 0: no team
  cap. Negative, NaN, infinite or not a number: :class:`InvalidTeamCap` (G00 then refuses
  the team's requests rather than treat a broken cap as no cap).
* ``config`` — an overlay deep-merged over the tenant's config for the team's requests,
  with the CONFIG_OVERLAYS rules (``config_loader.deep_merge``: mappings merge key by key,
  a null changes nothing). It is applied BEFORE the operator's ``tenants.<id>.groups``
  overlay, so an operator pin always wins. Limits on what it may change:

  - only the sections in :data:`TEAM_OVERLAY_SECTIONS` (how a request is served). Who
    pays, how much, what is recorded and which providers exist stay the tenant's;
  - not the groups numbered in :data:`TEAM_PROTECTED_GROUPS`: rate limiting (G00), loop
    and budget control (G17) and trust & safety (G29-G32);
  - never a mapping replaced by a scalar or a list (``G5_cache: false``), and every group
    block must be a mapping: groups read their block as one.

  Anything dropped is named in a WARNING (never its value).

Every reader type-checks each level: a mis-indented key in operator YAML degrades to "no
team settings", never an error in the request path.
"""
import logging
import math
import re
from typing import Any, Dict, Optional, Tuple

from config_loader import deep_merge
from tenancy.resolver import DEFAULT_TEAM

logger = logging.getLogger(__name__)

#: Top-level config sections a team overlay may change.
TEAM_OVERLAY_SECTIONS = ("groups", "proxy")

#: Groups a team overlay may never change, by number (any spelling: ``G29_pii_redaction``,
#: ``G29_pii``, ``g30_guardrails``): rate limiting, loop/budget control, trust & safety.
TEAM_PROTECTED_GROUPS = ("G00", "G17", "G29", "G30", "G31", "G32")
_GROUP_NUMBER = re.compile(r"[Gg]0*(\d+)(?:_|$)")
_PROTECTED_NUMBERS = frozenset(int(g[1:]) for g in TEAM_PROTECTED_GROUPS)

# A team name bound to a key at issuance: short, and safe as a Redis key segment, a metric
# label and a log field without escaping. "default" is reserved for "no team".
_TEAM_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")

_warned: set = set()


class InvalidTeamCap(ValueError):
    """A team's ``monthly_spend_cap_usd`` that is set but is not a usable amount."""


def _warn_once(message: str, *args: Any) -> None:
    key = message % args
    if key not in _warned:
        _warned.add(key)
        logger.warning(message, *args)


def valid_team_name(name: Any) -> bool:
    """Whether ``name`` may be bound to a proxy key: 1-64 of ``A-Z a-z 0-9 . _ -``, and
    not the reserved ``"default"``."""
    return isinstance(name, str) and name != DEFAULT_TEAM and bool(_TEAM_NAME.fullmatch(name))


def _mapping(node: Any, *path: str) -> Dict[str, Any]:
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else {}


def _team_block(teams: Dict[Any, Any], team: str, where: str) -> Dict[str, Any]:
    """``teams[team]``, matching a non-string YAML key by its string form (a boolean key
    never matches: ``on`` and ``yes`` both read as True). Names non-string keys once."""
    odd = [k for k in teams if not isinstance(k, str)]
    if odd:
        _warn_once("%s has team key(s) %s that YAML did not read as text: quote team names "
                   "(an unquoted no/on/yes/off is a boolean and never matches a team)",
                   where, sorted(repr(k) for k in odd))
    block = teams.get(team)
    if block is None:
        block = next((v for k, v in teams.items()
                      if not isinstance(k, (str, bool)) and str(k) == team), None)
    return block if isinstance(block, dict) else {}


def team_settings(config: Any, tenant_id: str, team: str) -> Dict[str, Any]:
    """The team's block: ``teams.<team>`` with ``tenants.<tenant>.teams.<team>`` merged over
    it. ``{}`` for the default team, an unknown team, or malformed config. A new dict when
    both exist; otherwise the block itself, which callers must not modify."""
    if not team or team == DEFAULT_TEAM or not isinstance(config, dict):
        return {}
    base = _team_block(_mapping(config, "teams"), team, "teams")
    operator = _team_block(_mapping(config, "tenants", tenant_id, "teams"), team,
                           f"tenants.{tenant_id}.teams")
    if base and operator:
        return deep_merge(base, operator)
    return operator or base


def team_monthly_spend_cap(config: Any, tenant_id: str, team: str) -> Optional[float]:
    """The team's ``monthly_spend_cap_usd`` as a positive float, ``None`` (no cap) when it is
    absent or zero. Raises :class:`InvalidTeamCap`, with a WARNING, when it is negative,
    NaN, infinite or not a number: a broken cap is not "no cap"."""
    raw = team_settings(config, tenant_id, team).get("monthly_spend_cap_usd")
    if raw is None:
        return None
    try:
        if isinstance(raw, bool):
            raise ValueError("a boolean")
        cap = float(raw)
    except (TypeError, ValueError):
        cap = math.nan
    if cap == 0:
        return None
    if not math.isfinite(cap) or cap < 0:
        _warn_once("team %r of tenant %r: monthly_spend_cap_usd is not a positive number; "
                   "the team's requests are refused until it is fixed", team, tenant_id)
        raise InvalidTeamCap(f"team {team!r}: monthly_spend_cap_usd is not a positive number")
    return cap


def _protected_group(key: Any) -> bool:
    match = _GROUP_NUMBER.match(str(key))
    return bool(match) and int(match.group(1)) in _PROTECTED_NUMBERS


def _keep_shapes(base: Any, overlay: Dict[str, Any], path: str,
                 dropped: list) -> Dict[str, Any]:
    """``overlay`` without any value that would replace a mapping in ``base`` with a
    non-mapping (recursively). Nulls are kept: they change nothing."""
    kept: Dict[str, Any] = {}
    for key, value in overlay.items():
        current = base.get(key) if isinstance(base, dict) else None
        if isinstance(current, dict) and value is not None and not isinstance(value, dict):
            dropped.append(f"{path}.{key}")
            continue
        if isinstance(value, dict):
            value = _keep_shapes(current, value, f"{path}.{key}", dropped)
        kept[key] = value
    return kept


def team_overlay(config: Any, tenant_id: str, team: str) -> Dict[str, Any]:
    """The part of the team's ``config`` overlay that may apply (see the module docstring).
    Anything else is dropped with a WARNING naming it (never its value)."""
    raw = team_settings(config, tenant_id, team).get("config")
    if not isinstance(raw, dict):
        return {}
    dropped = sorted(str(k) for k, v in raw.items()
                     if v is not None and (k not in TEAM_OVERLAY_SECTIONS or not isinstance(v, dict)))
    allowed: Dict[str, Any] = {}
    for section in TEAM_OVERLAY_SECTIONS:
        block = raw.get(section)
        if not isinstance(block, dict) or not block:
            continue
        if section == "groups":
            block = dict(block)
            for key in list(block):
                if _protected_group(key) or (block[key] is not None
                                             and not isinstance(block[key], dict)):
                    dropped.append(f"groups.{key}")
                    del block[key]
        base = config.get(section) if isinstance(config, dict) else None
        block = _keep_shapes(base, block, section, dropped)
        if any(v is not None for v in block.values()):
            allowed[section] = block
    if dropped:
        logger.warning("team %r of tenant %r: config overlay entries %s ignored; a team "
                       "overlay may change only %s, never the groups %s, and never replace "
                       "a mapping with a value", team, tenant_id, sorted(set(dropped)),
                       list(TEAM_OVERLAY_SECTIONS), list(TEAM_PROTECTED_GROUPS))
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


def overlay_tag(overlay: Dict[str, Any]) -> str:
    """A short, stable fingerprint of an applied overlay ("" for none): the G05 cache keys
    fold it in, so an answer produced under one team's overlay is never served to a request
    that ran under another (or none)."""
    if not overlay:
        return ""
    import hashlib
    import json
    blob = json.dumps(overlay, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
