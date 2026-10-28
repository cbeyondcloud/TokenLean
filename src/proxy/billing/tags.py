"""Usage tags (ST-2): allow-listed request headers recorded on ``usage_events.tags``.

A caller can label each request for cost attribution (which run, step or agent made the
call) by sending headers on an operator-controlled allow-list. Each allow-listed header
present on the request becomes one tag; every other header is ignored. The tags are
stored as a JSONB object on the request's usage row, and nothing else reads them: they
never select a team, a limit, a route or a config. They are what the caller asserts, so
``usage_events.team`` (the authenticated team) and the tags' own ``team`` may differ.

Config (``billing.metering.tags``, global only: a tenant cannot widen what is recorded):

* ``enabled`` (default true);
* ``headers`` — header name -> tag key. Default :data:`DEFAULT_TAG_HEADERS`, the identity
  headers an agent framework sends. A value given here replaces the default entirely;
  ``{}`` records nothing. Header names match in any case; tag keys are
  ``[a-z][a-z0-9_]{0,63}``; an invalid entry is skipped with a warning;
* ``max_value_chars`` (default 256, at most 1024) — longer values are cut to it.

A value is trimmed and must be printable ASCII; one that looks like a credential (a
provider, registry or proxy key, a bearer or basic token, a JWT, credentials in a URL, a
webhook or signed-URL secret, a ``password=...`` pair, a private-key header) is dropped and
counted, never stored. Bare hex or base64 strings are kept: commit SHAs and run ids look the
same, and they are what the tags are for. When a tag key arrives more than once (a header
sent twice, or two allow-listed headers mapped to one key), the first value in the request
decides (stored, or dropped as a credential); later ones are ignored and counted. A section
that is present but unreadable stores nothing (fail closed), with a warning.
"""
import logging
import re
from typing import Any, Dict, Mapping, Optional

from prometheus_client import Counter

logger = logging.getLogger(__name__)

#: The default allow-list: the identity headers an agent framework stamps on every model
#: call, mapped to the tag keys its cost views join on.
DEFAULT_TAG_HEADERS: Dict[str, str] = {
    "X-Agentic-Team": "team",
    "X-Agentic-User": "user",
    "X-Agentic-Repo": "repo",
    "X-Agentic-Pipeline": "pipeline",
    "X-Agentic-Run-Id": "run_id",
    "X-Agentic-Step": "step",
    "X-Agentic-Agent": "agent",
    "X-Agentic-Parent-Agent": "parent_agent",
    "X-Agentic-Tier": "tier",
    "X-Agentic-Policy-Version": "policy_version",
}
DEFAULT_MAX_VALUE_CHARS = 256
_MAX_VALUE_CHARS_CEILING = 1024
_MAX_HEADERS = 64

_HEADER_NAME = re.compile(r"[A-Za-z0-9-]{1,128}")
_TAG_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}")
_PRINTABLE = re.compile(r"[\x20-\x7e]+")

# Credential shapes. High-confidence provider formats (the same families the repo's leak
# scan looks for), plus the generic shapes a mistaken header value takes. The generic ones
# match a credential-shaped BODY, not a keyword: "Basic analysis", "token: refresh" and
# "ssh://git@host/repo" are attribution values, not credentials.
_SEP = r"(?:\s+|%20|\+)"               # "Bearer x", URL-encoded "Bearer%20x" or "Bearer+x"
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bGOCSPX-[0-9A-Za-z_-]{28}\b"),
    re.compile(r"\b(?:gh[pousr]_[0-9A-Za-z]{36,}|github_pat_[0-9A-Za-z_]{40,})\b"),
    re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}\b"),
    re.compile(r"\b(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{20,}\b"),
    re.compile(r"\bhf_[0-9A-Za-z]{30,}\b"),                     # Hugging Face
    re.compile(r"\bnpm_[0-9A-Za-z]{36,}\b"),                    # npm
    re.compile(r"\bpypi-AgE[0-9A-Za-z_-]{50,}"),                # PyPI upload token
    # Webhook URLs whose path is the secret, and a signed URL's signature (Azure SAS sig=).
    re.compile(r"(?i)\bhooks\.slack\.com/(?:services|workflows|triggers)/[0-9A-Za-z/_-]{20,}"),
    re.compile(r"(?i)(?:^|[?&\s])sig=[0-9A-Za-z%+/_=-]{16,}"),
    re.compile(r"\bSG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}\b"),
    # Provider / proxy keys in any case: sk-…, rk-…, tok-… with a long body that has a digit.
    re.compile(r"(?i)\b(?:sk|rk|tok)-(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{20,}"),
    # A JWT: two base64url JSON segments and a signature.
    re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]+"),
    # A bearer token pasted into a header: a token-shaped body (12+ chars with a digit).
    re.compile(r"(?i)\bbearer" + _SEP + r"(?=[A-Za-z0-9._~+/=-]*\d)[A-Za-z0-9._~+/=-]{12,}"),
    # A password, under a password-named key: any value, since passwords can be words.
    re.compile(r"(?i)\b(?:password|passwd|pwd)\s*[=:]\s*\S{4,}"),
    # Another secret-named key whose value is credential-shaped (a digit, or 20+ chars).
    re.compile(r"(?i)\b(?:secret|token|api[_-]?key|access[_-]?key|client[_-]?secret|"
               r"private[_-]?key)\s*[=:]\s*(?:(?=\S*\d)\S{6,}|\S{20,})"),
)
_BASIC = re.compile(r"(?i)\bbasic" + _SEP + r"([A-Za-z0-9+/]{2,}={0,2})")
_URL_USERINFO = re.compile(r"://([^/\s@]+)@")
_TOKENISH = re.compile(r"(?=[A-Za-z0-9._~-]*\d)(?=[A-Za-z0-9._~-]*[A-Za-z])[A-Za-z0-9._~-]{20,}")


def _basic_credentials(value: str) -> bool:
    """``Basic <base64 of user:password>``: the body must decode to text with a colon."""
    import base64
    import binascii
    for match in _BASIC.finditer(value):
        body = match.group(1)
        try:
            decoded = base64.b64decode(body + "=" * (-len(body) % 4), validate=True)
        except (binascii.Error, ValueError):
            continue
        if b":" in decoded and decoded.isascii():
            return True
    return False


def _url_credentials(value: str) -> bool:
    """Credentials before a URL's host: ``user:password@`` or a token-shaped user. A plain
    user name (``ssh://git@github.com``, ``https://user@host``) is not a credential."""
    return any(":" in m.group(1) or _TOKENISH.fullmatch(m.group(1))
               for m in _URL_USERINFO.finditer(value))

TAGS_DROPPED = Counter(
    "token_opt_usage_tags_dropped_total",
    "Allow-listed usage-tag header values not stored, by reason",
    ["reason"],   # secret | invalid | duplicate
)

_warned: set = set()


def looks_like_secret(value: str) -> bool:
    """Whether ``value`` has the shape of a credential (see the module docstring)."""
    return (any(rx.search(value) for rx in _SECRET_PATTERNS)
            or _basic_credentials(value) or _url_credentials(value))


#: Tag keys only tenant-level keys may read back (owner decision 84): a team-bound key's
#: usage export has them removed. Default: the user identity.
DEFAULT_TENANT_ONLY_KEYS = ("user",)


def tenant_only_keys(config: Any) -> frozenset:
    """``billing.metering.tags.tenant_only_keys`` (a list of tag keys; default
    :data:`DEFAULT_TENANT_ONLY_KEYS`). Unreadable: the default, so never fewer keys hidden."""
    billing = config.get("billing") if isinstance(config, dict) else None
    metering = billing.get("metering") if isinstance(billing, dict) else None
    section = metering.get("tags") if isinstance(metering, dict) else None
    raw = section.get("tenant_only_keys") if isinstance(section, dict) else None
    if not isinstance(raw, (list, tuple)):
        return frozenset(DEFAULT_TENANT_ONLY_KEYS)
    return frozenset(k for k in raw if isinstance(k, str))


def without_tenant_only(tags: Any, hidden: frozenset) -> Any:
    """``tags`` without the ``hidden`` keys; ``None`` when nothing is left."""
    if not isinstance(tags, dict):
        return tags
    kept = {k: v for k, v in tags.items() if k not in hidden}
    return kept or None


def _warn_once(message: str, *args: Any) -> None:
    key = message % args
    if key not in _warned:
        _warned.add(key)
        logger.warning(message, *args)


def _settings(config: Any) -> Optional[tuple]:
    """``(header->key map with lower-cased names, max chars)``, or ``None`` to record
    nothing (switched off, or unreadable)."""
    section = None
    billing = config.get("billing") if isinstance(config, dict) else None
    metering = billing.get("metering") if isinstance(billing, dict) else None
    if isinstance(metering, dict) and "tags" in metering and metering["tags"] is not None:
        section = metering["tags"]
        if not isinstance(section, dict):
            _warn_once("billing.metering.tags is not a mapping: no usage tags are recorded")
            return None
    section = section or {}
    if section.get("enabled", True) is False:
        return None
    headers = section.get("headers", DEFAULT_TAG_HEADERS)
    if headers is None:
        headers = DEFAULT_TAG_HEADERS
    if not isinstance(headers, dict):
        _warn_once("billing.metering.tags.headers is not a mapping of header name to tag "
                   "key: no usage tags are recorded")
        return None
    allowed: Dict[str, str] = {}
    for name, key in list(headers.items())[:_MAX_HEADERS]:
        if (not isinstance(name, str) or not _HEADER_NAME.fullmatch(name)
                or not isinstance(key, str) or not _TAG_KEY.fullmatch(key)):
            _warn_once("billing.metering.tags.headers: entry %r -> %r skipped (header names "
                       "are letters, digits and '-'; tag keys are [a-z][a-z0-9_]*)",
                       name, key)
            continue
        allowed[name.lower()] = key
    raw_cap = section.get("max_value_chars", DEFAULT_MAX_VALUE_CHARS)
    try:
        cap = int(raw_cap) if not isinstance(raw_cap, bool) else DEFAULT_MAX_VALUE_CHARS
    except (TypeError, ValueError):
        cap = DEFAULT_MAX_VALUE_CHARS
    cap = max(1, min(cap, _MAX_VALUE_CHARS_CEILING))
    return allowed, cap


def extract_tags(headers: Mapping[str, str], config: Any) -> Optional[Dict[str, str]]:
    """The request's usage tags, or ``None`` when there are none.

    ``headers`` are the request headers (any case); ``config`` is the GLOBAL config.
    """
    settings = _settings(config)
    if not settings:
        return None
    allowed, cap = settings
    if not allowed or not headers:
        return None
    tags: Dict[str, str] = {}
    seen: set = set()
    for name, raw in headers.items():
        key = allowed.get(str(name).lower())
        if key is None or not isinstance(raw, str):
            continue
        value = raw.strip()
        if not value:
            continue
        # The first value for a key decides, stored or dropped: a later header can neither
        # override a stored value nor stand in for one dropped as a credential.
        if key in seen:
            TAGS_DROPPED.labels(reason="duplicate").inc()
            continue
        seen.add(key)
        if not _PRINTABLE.fullmatch(value):
            TAGS_DROPPED.labels(reason="invalid").inc()
            continue
        # Scanned WHOLE, before the cut: a credential straddling the cap must not leave its
        # prefix behind in the stored value.
        if looks_like_secret(value):
            TAGS_DROPPED.labels(reason="secret").inc()
            # Per request, so DEBUG: the counter is how an operator sees it. Never the value.
            logger.debug("usage tag %r dropped: its value looks like a credential", key)
            continue
        tags[key] = value[:cap]
    return tags or None
