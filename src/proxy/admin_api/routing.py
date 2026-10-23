"""/admin/routing (ST-10): routing rules and team limits, set over HTTP, versioned, hot-applied.

The admin-managed **fragment** is a config overlay: deep-merged over the loaded config (file,
params files and CONFIG_OVERLAYS) with the CONFIG_OVERLAYS rules (``config_loader.deep_merge``:
mappings merge key by key, a list or scalar replaces). It may hold only what routing and team
limits are made of, at the paths the proxy already reads them from:

    groups:     {G6_routing: <routing>}                  # every tenant
    rate_limit: {per_team: {<team>: {requests_per_minute, requests_per_hour}}}
    teams:      {<team>: <team block>}                   # that team name in every tenant
    tenants:    {<tenant>: {groups: {G6_routing: <routing>},
                            teams:  {<team>: <team block>}}}

    <team block> = {monthly_spend_cap_usd, config: {groups: {G6_routing: <routing>},
                                                    proxy: {default_model, fallback_request_model}}}
    <routing>    = the G6_routing keys in ROUTING_KEYS (tiers, tiers_by_provider, rules, ...)

Anything else is refused, naming its path: above all the protected groups a team overlay can
never change either (``tenancy.teams.TEAM_PROTECTED_GROUPS``: G00, G17, G29-G32), under any
spelling, at any depth, and any other section (``rate_limit`` beyond ``per_team``,
``providers``, ``billing``, ...). URL-valued G06 settings (judge, RouteLLM, Jev) are not
admin-managed: they decide where prompt text is sent. Strings are taken literally (no
``${VAR}`` expansion), so a fragment cannot read the proxy's environment.

Versions live in Postgres (``routing_overlay_versions``), the store the proxy already uses
for shared state such as tenant settings and, with ``key_store.backend: postgres``, keys.
Every apply and rollback is a new row (who, when, comment); nothing is ever overwritten. A
write takes a transaction-scoped advisory lock and may name the version it expects to replace
(``expected_version``, 409 otherwise), so concurrent admins on different replicas cannot
silently overwrite each other. The writing replica applies the new version at once; every
other replica polls the latest version number every ``admin_api.routing.poll_seconds``
(default 5) and applies a change when it sees one. Applying swaps the process config object
(``config_loader.set_runtime_overlay``): a request already in flight keeps the config it
started with; no restart. With no version stored (or an empty fragment), ``get_config()``
returns the loaded config object itself: behaviour and config are exactly as before.
"""
import asyncio
import copy
import hashlib
import json
import logging
import math
import re
from typing import Any, Dict, List, Optional

from tenancy.teams import TEAM_PROTECTED_GROUPS, valid_team_name

logger = logging.getLogger(__name__)

ROUTING_GROUP = "G6_routing"
SECTIONS = ("groups", "rate_limit", "teams", "tenants")
TEAM_KEYS = ("monthly_spend_cap_usd", "config")
TEAM_CONFIG_SECTIONS = ("groups", "proxy")
PROXY_KEYS = ("default_model", "fallback_request_model")
RATE_KEYS = ("requests_per_minute", "requests_per_hour")
STRATEGIES = ("priority", "cascade", "weighted", "round_robin", "least_latency", "canary")
TIERS = ("simple", "medium", "complex")
RULE_KEYS = ("id", "description", "enabled", "priority", "match", "action")
MATCH_KEYS = ("keywords", "patterns", "min_prompt_tokens", "max_prompt_tokens", "models",
              "has_tools", "params", "user_ids")
ACTION_KEYS = ("tier", "model", "allow_escalation", "overrides")
MAX_FRAGMENT_BYTES = 256 * 1024
MAX_LIST = 100
MAX_COMMENT = 500

_GROUP_NUMBER = re.compile(r"[Gg]0*(\d+)(?:_|$)")
_PROTECTED_NUMBERS = frozenset(int(g[1:]) for g in TEAM_PROTECTED_GROUPS)
_TENANT_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")
_RULE_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
_PARAM_KEY = re.compile(r"x_[a-z0-9_]{1,64}")


class FragmentError(ValueError):
    """An invalid fragment: ``errors`` is ``[{"path", "message"}]``, every problem found."""

    def __init__(self, errors: List[Dict[str, str]]):
        super().__init__("; ".join(f"{e['path']}: {e['message']}" for e in errors))
        self.errors = errors


class VersionConflict(Exception):
    """``expected_version`` is not the current version."""

    def __init__(self, current: int):
        super().__init__(f"the current version is {current}")
        self.current = current


# ── Validation ──────────────────────────────────────────────────────────────

def _models_offered(config: Any):
    """A predicate: does a configured provider offer this model (listed in
    providers[].models, or matching a model_prefixes entry)? None: nothing to check against."""
    providers = config.get("providers") if isinstance(config, dict) else None
    if not isinstance(providers, list) or not providers:
        return None
    names, prefixes = set(), []
    for p in providers:
        if not isinstance(p, dict):
            continue
        names.update(m for m in (p.get("models") or []) if isinstance(m, str))
        prefixes.extend(x for x in (p.get("model_prefixes") or []) if isinstance(x, str) and x)
    return lambda m: m in names or any(m.startswith(x) for x in prefixes)


def _provider_names(config: Any) -> Optional[set]:
    providers = config.get("providers") if isinstance(config, dict) else None
    if not isinstance(providers, list) or not providers:
        return None
    return {p.get("name") for p in providers if isinstance(p, dict) and p.get("name")}


class _Checker:
    """Walks a fragment, collecting every problem with its path. ``base`` (the loaded config)
    lets models and provider names be checked against what is configured; without it (the
    re-check when another replica applies a stored version) only the shape is checked."""

    def __init__(self, base: Any):
        self.errors: List[Dict[str, str]] = []
        self.offered = _models_offered(base) if base is not None else None
        self.providers = _provider_names(base) if base is not None else None

    def err(self, path: str, message: str) -> None:
        self.errors.append({"path": path, "message": message})

    def mapping(self, value: Any, path: str) -> bool:
        if not isinstance(value, dict):
            self.err(path, "must be a mapping")
            return False
        for key, item in value.items():
            if item is None:
                self.err(f"{path}.{key}", "null is not allowed: leave the key out instead")
        return True

    def keys(self, value: Dict, allowed, path: str) -> None:
        for key in value:
            if key not in allowed:
                self.err(f"{path}.{key}", f"not admin-managed here; allowed: {', '.join(allowed)}")

    def number(self, value: Any, path: str, lo=None, hi=None, integer=False) -> None:
        ok = (not isinstance(value, bool) and isinstance(value, (int, float))
              and (not integer or isinstance(value, int)) and math.isfinite(value))
        if not ok or (lo is not None and value < lo) or (hi is not None and value > hi):
            kind = "an integer" if integer else "a number"
            bounds = "" if lo is None else (f" from {lo}" + (f" to {hi}" if hi is not None else
                                                             " or more"))
            self.err(path, f"must be {kind}{bounds}")

    def boolean(self, value: Any, path: str) -> None:
        if not isinstance(value, bool):
            self.err(path, "must be true or false")

    def choice(self, value: Any, options, path: str) -> None:
        if value not in options:
            self.err(path, f"must be one of {', '.join(options)}")

    def string_list(self, value: Any, path: str, non_empty=True) -> bool:
        if (not isinstance(value, list) or (non_empty and not value) or len(value) > MAX_LIST
                or not all(isinstance(v, str) and v.strip() for v in value)):
            self.err(path, f"must be a list of 1-{MAX_LIST} non-empty strings")
            return False
        return True

    def model(self, value: Any, path: str) -> None:
        if not isinstance(value, str) or not value.strip():
            self.err(path, "must be a model name")
        elif self.offered is not None and not self.offered(value):
            self.err(path, f"model {value!r} is not offered by any configured provider")

    # ── sections ──

    def fragment(self, fragment: Any) -> None:
        if not self.mapping(fragment, "fragment"):
            return
        self.keys(fragment, SECTIONS, "fragment")
        if "groups" in fragment:
            self.groups(fragment["groups"], "groups")
        if "rate_limit" in fragment:
            self.rate_limit(fragment["rate_limit"], "rate_limit")
        if "teams" in fragment:
            self.teams(fragment["teams"], "teams")
        if "tenants" in fragment:
            self.tenants(fragment["tenants"], "tenants")

    def groups(self, groups: Any, path: str) -> None:
        if not self.mapping(groups, path):
            return
        for key, block in groups.items():
            match = _GROUP_NUMBER.match(str(key))
            if match and int(match.group(1)) in _PROTECTED_NUMBERS:
                self.err(f"{path}.{key}", "a protected group (rate limiting G00, loop control "
                                          "G17, trust & safety G29-G32) is never admin-managed")
            elif key != ROUTING_GROUP:
                self.err(f"{path}.{key}", f"only {ROUTING_GROUP} is admin-managed")
            elif block is not None:
                self.routing(block, f"{path}.{key}")

    def rate_limit(self, section: Any, path: str) -> None:
        if not self.mapping(section, path):
            return
        self.keys(section, ("per_team",), path)
        per_team = section.get("per_team")
        if per_team is None or not self.mapping(per_team, f"{path}.per_team"):
            return
        for team, limits in per_team.items():
            where = f"{path}.per_team.{team}"
            if not valid_team_name(team):
                self.err(where, "team names are 1-64 of A-Z a-z 0-9 . _ - and not 'default'")
            if limits is None or not self.mapping(limits, where):
                continue
            self.keys(limits, RATE_KEYS, where)
            if not any(k in limits for k in RATE_KEYS):
                self.err(where, f"set at least one of {', '.join(RATE_KEYS)}")
            for key in RATE_KEYS:
                if key in limits:
                    self.number(limits[key], f"{where}.{key}", lo=1, integer=True)

    def teams(self, teams: Any, path: str) -> None:
        if not self.mapping(teams, path):
            return
        for team, block in teams.items():
            where = f"{path}.{team}"
            if not valid_team_name(team):
                self.err(where, "team names are 1-64 of A-Z a-z 0-9 . _ - and not 'default'")
            if block is None or not self.mapping(block, where):
                continue
            self.keys(block, TEAM_KEYS, where)
            if "monthly_spend_cap_usd" in block:
                self.number(block["monthly_spend_cap_usd"], f"{where}.monthly_spend_cap_usd",
                            lo=0)
            config = block.get("config")
            if config is not None and self.mapping(config, f"{where}.config"):
                self.keys(config, TEAM_CONFIG_SECTIONS, f"{where}.config")
                if config.get("groups") is not None:
                    self.groups(config["groups"], f"{where}.config.groups")
                proxy = config.get("proxy")
                if proxy is not None and self.mapping(proxy, f"{where}.config.proxy"):
                    self.keys(proxy, PROXY_KEYS, f"{where}.config.proxy")
                    for key in PROXY_KEYS:
                        if key in proxy:
                            self.model(proxy[key], f"{where}.config.proxy.{key}")

    def tenants(self, tenants: Any, path: str) -> None:
        if not self.mapping(tenants, path):
            return
        for tenant, block in tenants.items():
            where = f"{path}.{tenant}"
            if not isinstance(tenant, str) or not _TENANT_ID.fullmatch(tenant):
                self.err(where, "tenant ids are 1-128 of A-Z a-z 0-9 . _ -")
            if block is None or not self.mapping(block, where):
                continue
            self.keys(block, ("groups", "teams"), where)
            if block.get("groups") is not None:
                self.groups(block["groups"], f"{where}.groups")
            if block.get("teams") is not None:
                self.teams(block["teams"], f"{where}.teams")

    # ── G6_routing ──

    def tiers(self, tiers: Any, path: str) -> None:
        if not self.mapping(tiers, path):
            return
        self.keys(tiers, TIERS, path)
        for tier in TIERS:
            if tier in tiers and self.string_list(tiers[tier], f"{path}.{tier}"):
                for i, m in enumerate(tiers[tier]):
                    self.model(m, f"{path}.{tier}[{i}]")

    def routing(self, block: Any, path: str) -> None:
        if not self.mapping(block, path):
            return
        self.keys(block, tuple(ROUTING_KEYS), path)
        for key, value in block.items():
            check = ROUTING_KEYS.get(key)
            if check is not None and value is not None:
                check(self, value, f"{path}.{key}")

    def tiers_by_provider(self, value: Any, path: str) -> None:
        if not self.mapping(value, path):
            return
        for provider, tiers in value.items():
            if self.providers is not None and provider not in self.providers:
                self.err(f"{path}.{provider}", "not a configured provider name")
            if tiers is not None:
                self.tiers(tiers, f"{path}.{provider}")

    def weights(self, value: Any, path: str) -> None:
        if not self.mapping(value, path):
            return
        for model, weight in value.items():
            self.model(model, f"{path}.{model}")
            self.number(weight, f"{path}.{model}", lo=0)

    def rules(self, rules: Any, path: str) -> None:
        if not isinstance(rules, list) or len(rules) > MAX_LIST:
            self.err(path, f"must be a list of at most {MAX_LIST} rules")
            return
        seen = set()
        for i, rule in enumerate(rules):
            where = f"{path}[{i}]"
            if not self.mapping(rule, where):
                continue
            self.keys(rule, RULE_KEYS, where)
            rid = rule.get("id")
            if not isinstance(rid, str) or not _RULE_ID.fullmatch(rid):
                self.err(f"{where}.id", "required: 1-64 of A-Z a-z 0-9 . _ -")
            elif rid in seen:
                self.err(f"{where}.id", f"duplicate rule id {rid!r}")
            else:
                seen.add(rid)
            if "description" in rule and (not isinstance(rule["description"], str)
                                          or len(rule["description"]) > MAX_COMMENT):
                self.err(f"{where}.description", f"a string of at most {MAX_COMMENT} characters")
            if "enabled" in rule:
                self.boolean(rule["enabled"], f"{where}.enabled")
            if "priority" in rule:
                self.number(rule["priority"], f"{where}.priority", integer=True)
            self.match(rule.get("match"), f"{where}.match")
            self.action(rule.get("action"), f"{where}.action")

    def match(self, match: Any, path: str) -> None:
        if not isinstance(match, dict) or not match:
            self.err(path, "required: a mapping with at least one matcher (a rule with none "
                           "would match every request)")
            return
        self.mapping(match, path)
        self.keys(match, MATCH_KEYS, path)
        for key in ("keywords", "models", "user_ids"):
            if key in match:
                self.string_list(match[key], f"{path}.{key}")
        if "patterns" in match and self.string_list(match["patterns"], f"{path}.patterns"):
            from middleware.g06_rules import MAX_PATTERN_LEN, has_nested_quantifier
            for i, pattern in enumerate(match["patterns"]):
                where = f"{path}.patterns[{i}]"
                if len(pattern) > MAX_PATTERN_LEN:
                    self.err(where, f"at most {MAX_PATTERN_LEN} characters")
                    continue
                try:
                    re.compile(pattern)
                except re.error as exc:
                    self.err(where, f"not a valid regular expression: {exc}")
                    continue
                if has_nested_quantifier(pattern):
                    self.err(where, "a nested repeat such as (a+)+ can take exponential time")
        for key in ("min_prompt_tokens", "max_prompt_tokens"):
            if key in match:
                self.number(match[key], f"{path}.{key}", lo=0, integer=True)
        if "has_tools" in match:
            self.boolean(match["has_tools"], f"{path}.has_tools")
        params = match.get("params")
        if params is not None and self.mapping(params, f"{path}.params"):
            for key, wanted in params.items():
                where = f"{path}.params.{key}"
                if not isinstance(key, str) or not _PARAM_KEY.fullmatch(key):
                    self.err(where, "an x_ parameter name (an X-* header, lower case, - as _)")
                if isinstance(wanted, list):
                    self.string_list(wanted, where)
                elif not isinstance(wanted, str) or not wanted:
                    self.err(where, "a string or a list of strings")

    def action(self, action: Any, path: str) -> None:
        if not isinstance(action, dict) or not any(k in action for k in ("tier", "model",
                                                                          "overrides")):
            self.err(path, "required: a mapping with tier, model or overrides")
            return
        self.mapping(action, path)
        self.keys(action, ACTION_KEYS, path)
        if "tier" in action:
            self.choice(action["tier"], TIERS, f"{path}.tier")
        if "model" in action:
            self.model(action["model"], f"{path}.model")
        if "allow_escalation" in action:
            self.boolean(action["allow_escalation"], f"{path}.allow_escalation")
        overrides = action.get("overrides")
        if overrides is not None and self.mapping(overrides, f"{path}.overrides"):
            from middleware.g06_rules import RULE_OVERRIDE_KEYS
            allowed = tuple(sorted(RULE_OVERRIDE_KEYS))
            self.keys(overrides, allowed, f"{path}.overrides")
            for key, value in overrides.items():
                check = ROUTING_KEYS.get(key)
                if key in RULE_OVERRIDE_KEYS and check is not None:
                    check(self, value, f"{path}.overrides.{key}")


#: The G6_routing keys a fragment may set, each with its check.
ROUTING_KEYS = {
    "enabled": lambda c, v, p: c.boolean(v, p),
    "tiers": lambda c, v, p: c.tiers(v, p),
    "tiers_by_provider": lambda c, v, p: c.tiers_by_provider(v, p),
    "rules": lambda c, v, p: c.rules(v, p),
    "strategy": lambda c, v, p: c.choice(v, STRATEGIES, p),
    "strategy_weights": lambda c, v, p: c.weights(v, p),
    "canary_pct": lambda c, v, p: c.number(v, p, lo=0, hi=100),
    "on_unreachable_tier": lambda c, v, p: c.choice(v, ("fallback", "error"), p),
    "on_unconfigured_model": lambda c, v, p: c.choice(v, ("substitute", "reject"), p),
    "allow_escalation_above_requested": lambda c, v, p: c.boolean(v, p),
    "max_escalation_cost_usd": lambda c, v, p: c.number(v, p, lo=0),
    "cascade_confidence_threshold": lambda c, v, p: c.number(v, p, lo=0, hi=1),
    "expected_output_tokens_estimate": lambda c, v, p: c.number(v, p, lo=0, integer=True),
}


def validate_fragment(fragment: Any, base: Any = None) -> None:
    """Raise :class:`FragmentError` listing every problem, or return. ``base`` (the loaded
    config) also checks models and provider names against what is configured."""
    try:
        size = len(json.dumps(fragment, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        raise FragmentError([{"path": "fragment", "message": "must be JSON"}])
    if size > MAX_FRAGMENT_BYTES:
        raise FragmentError([{"path": "fragment",
                              "message": f"{size} bytes; at most {MAX_FRAGMENT_BYTES}"}])
    checker = _Checker(base)
    checker.fragment(fragment)
    if checker.errors:
        raise FragmentError(checker.errors)


def fragment_sha256(fragment: Dict[str, Any]) -> str:
    blob = json.dumps(fragment, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def touched(fragment: Dict[str, Any]) -> Dict[str, Any]:
    """What a fragment reaches, for the audit row (paths, never values)."""
    tenants = fragment.get("tenants") if isinstance(fragment.get("tenants"), dict) else {}
    teams = set((fragment.get("teams") or {}) if isinstance(fragment.get("teams"), dict) else ())
    per_team = ((fragment.get("rate_limit") or {}).get("per_team") or {})
    teams.update(per_team if isinstance(per_team, dict) else ())
    for block in tenants.values():
        if isinstance(block, dict) and isinstance(block.get("teams"), dict):
            teams.update(block["teams"])
    return {"sections": sorted(fragment), "tenants": sorted(tenants), "teams": sorted(teams)}


# ── Store (Postgres) ────────────────────────────────────────────────────────

ROUTING_OVERLAY_DDL = """
CREATE TABLE IF NOT EXISTS routing_overlay_versions (
    version         BIGINT      PRIMARY KEY,
    fragment        JSONB       NOT NULL,
    action          TEXT        NOT NULL,
    actor           TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    comment         TEXT        NOT NULL DEFAULT '',
    source_version  BIGINT,
    fragment_sha256 TEXT        NOT NULL
);
"""

# Transaction-scoped advisory lock held by every overlay write. "TLROUT".
ROUTING_LOCK_ID = 0x544C524F5554
_COLUMNS = ("version, fragment, action, actor, created_at, comment, source_version, "
            "fragment_sha256")


async def ensure_schema(pool) -> None:
    from cache.pg_pool import may_run_ddl
    if not await may_run_ddl(pool, "routing_overlay_versions"):
        return  # a restricted runtime role: the schema job keeps the table current
    async with pool.acquire() as conn:
        await conn.execute(ROUTING_OVERLAY_DDL)


def _row(record) -> Dict[str, Any]:
    if record is None:
        return None
    row = dict(record)
    if isinstance(row.get("fragment"), str):
        row["fragment"] = json.loads(row["fragment"])
    created = row.get("created_at")
    if created is not None and hasattr(created, "isoformat"):
        row["created_at"] = created.isoformat()
    return row


async def current_version(pool) -> int:
    async with pool.acquire() as conn:
        return int(await conn.fetchval(
            "SELECT COALESCE(MAX(version), 0) FROM routing_overlay_versions") or 0)


async def get_version(pool, version: int) -> Optional[Dict[str, Any]]:
    async with pool.acquire() as conn:
        return _row(await conn.fetchrow(
            f"SELECT {_COLUMNS} FROM routing_overlay_versions WHERE version = $1", version))


async def current(pool) -> Optional[Dict[str, Any]]:
    async with pool.acquire() as conn:
        return _row(await conn.fetchrow(
            f"SELECT {_COLUMNS} FROM routing_overlay_versions ORDER BY version DESC LIMIT 1"))


async def history(pool, limit: int, before: Optional[int] = None) -> List[Dict[str, Any]]:
    async with pool.acquire() as conn:
        if before is None:
            rows = await conn.fetch(f"SELECT {_COLUMNS} FROM routing_overlay_versions "
                                    "ORDER BY version DESC LIMIT $1", limit)
        else:
            rows = await conn.fetch(f"SELECT {_COLUMNS} FROM routing_overlay_versions "
                                    "WHERE version < $2 ORDER BY version DESC LIMIT $1",
                                    limit, before)
    return [_row(r) for r in rows]


async def append(pool, fragment: Dict[str, Any], *, action: str, actor: str, comment: str = "",
                 expected_version: Optional[int] = None,
                 source_version: Optional[int] = None) -> Dict[str, Any]:
    """Store ``fragment`` as the next version, atomically, and return its row. Raises
    :class:`VersionConflict` (writing nothing) when ``expected_version`` is not current."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", ROUTING_LOCK_ID)
            latest = int(await conn.fetchval(
                "SELECT COALESCE(MAX(version), 0) FROM routing_overlay_versions") or 0)
            if expected_version is not None and expected_version != latest:
                raise VersionConflict(latest)
            record = await conn.fetchrow(
                "INSERT INTO routing_overlay_versions (version, fragment, action, actor, "
                "comment, source_version, fragment_sha256) "
                "VALUES ($1, $2::jsonb, $3, $4, $5, $6, $7) "
                f"RETURNING {_COLUMNS}",
                latest + 1, json.dumps(fragment), action, actor, comment or "", source_version,
                fragment_sha256(fragment))
    return _row(record)


# ── Applying a version to this replica ──────────────────────────────────────

_applied: Dict[str, Any] = {"version": 0, "sha256": None}
# failing: the last attempt to read or apply the latest version failed (main's /health then
# lists routing_overlay). refused: a stored version this replica refused (logged once).
_sync_state: Dict[str, Any] = {"failing": False, "refused": None}


def applied_version() -> int:
    """The version this replica runs with (0: none)."""
    return int(_applied["version"])


def sync_failing() -> bool:
    """Whether this replica could not read or apply the store's latest version last time."""
    return bool(_sync_state["failing"])


def _sync_ok() -> None:
    _sync_state["failing"] = False


def _sync_failed() -> None:
    _sync_state["failing"] = True


def apply_row(row: Optional[Dict[str, Any]]) -> bool:
    """Make ``row`` (a stored version, or None for none) this replica's overlay. The shape
    is checked again: a row that would touch anything a fragment may not (edited by hand in
    the database) is refused, keeping the overlay this replica has. Returns whether the
    overlay now is ``row``'s."""
    from config_loader import set_runtime_overlay
    if row is None:
        set_runtime_overlay({})
        _applied.update(version=0, sha256=None)
        return True
    fragment = row.get("fragment")
    try:
        validate_fragment(fragment)
    except FragmentError as exc:
        if _sync_state["refused"] != row.get("version"):
            _sync_state["refused"] = row.get("version")
            logger.error("routing overlay version %s refused, keeping version %s: %s",
                         row.get("version"), applied_version(), exc)
        return False
    set_runtime_overlay(copy.deepcopy(fragment))
    _applied.update(version=int(row["version"]), sha256=row.get("fragment_sha256"))
    logger.info("routing overlay version %s applied (%s)", row["version"], row.get("action"))
    return True


def poll_seconds(config: Any) -> float:
    section = config.get("admin_api") if isinstance(config, dict) else None
    routing = section.get("routing") if isinstance(section, dict) else None
    value = routing.get("poll_seconds", 5) if isinstance(routing, dict) else 5
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return 5.0
    return max(1.0, float(value))


async def sync_once(pool) -> bool:
    """Apply the store's latest version here if it is not the one applied. Returns whether
    this replica now runs the latest version."""
    latest = await current_version(pool)
    if latest == applied_version():
        _sync_ok()
        return True
    if latest and latest == _sync_state["refused"]:
        _sync_failed()
        return False                                  # refused already: no new log line
    row = await get_version(pool, latest) if latest else None
    if row is not None and row["version"] < applied_version():
        return True       # a newer version was applied here meanwhile (this replica's PUT)
    applied = apply_row(row)
    if applied:
        _sync_ok()
    else:
        _sync_failed()
    return applied


async def run_poller(get_pool, get_config) -> None:
    """Background task: once the database is wired (``get_pool()`` not None), create the
    table if this role may, apply the latest version, then check for a newer one every
    ``admin_api.routing.poll_seconds``. A failed check is logged and retried."""
    schema_ready = False
    wake = asyncio.Event()  # never set: waits are timeouts, not asyncio.sleep
    while True:
        pool = get_pool()
        if pool is not None:
            try:
                if not schema_ready:
                    await ensure_schema(pool)
                    schema_ready = True
                await sync_once(pool)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _sync_failed()
                logger.warning("routing overlay: could not read the latest version (this "
                               "replica keeps version %s): %s", applied_version(), exc)
        try:
            await asyncio.wait_for(wake.wait(), poll_seconds(get_config()))
        except asyncio.TimeoutError:
            pass
