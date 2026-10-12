"""
G06 · Jev complexity classifier (``groups.G6_routing.classifier: jev``)

TypeSafe's Jev is a decision model: it answers typed questions with probabilities, not text.
G06 asks it one ``score`` question per request: how hard is this, on the operator's own
ordered levels (``jev.levels``, lowest first, 2-10 of them), each naming the G06 tier whose
models answer it. The chosen level is the lowest one whose probability, plus that of every
level below it, reaches ``jev.enough_probability``: 0.5 is Jev's middle estimate, and a higher
bar only ever routes up.

The key is the TENANT's own, through the own-key seam (``providers.key_resolver``): stored in
the portal under ``jev.key_provider``, or a self-hosted operator's ``LLM_KEY_<NAME>``. A
lookup, found or missing, is reused for ``jev.key_cache_s``.

Whatever stops Jev deciding (no key, no text, a bad config, the breaker open, a timeout, an
HTTP error, an answer that fails the check) comes back as a ``JevDecision`` whose ``fallback``
is one of ``FALLBACK_REASONS``, and G06 asks its default classifier instead. Each fallback is
counted and warned about once per tenant and reason per ``jev.warn_every_s`` (a bad config
once per distinct config), so a keyless tenant never floods the log.

Every string (endpoint, model, key name, instructions, level text) comes from config.
"""
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import httpx
from prometheus_client import Counter

from middleware import record_provider_call

logger = logging.getLogger(__name__)

# The one question G06 asks; Jev answers under the same id.
QUESTION = "complexity"
FALLBACK_REASONS = ("no_key", "no_text", "bad_config", "breaker_open", "timeout",
                    "http_error", "bad_answer")

FALLBACKS = Counter(
    "token_opt_g06_jev_fallbacks_total",
    "G06 requests the Jev classifier handed to the default classifier, by reason: no_key "
    "(the tenant has no key under jev.key_provider), no_text, bad_config, breaker_open, "
    "timeout, http_error, bad_answer. Alert on a sustained rise in anything but no_key and "
    "no_text.",
    ["tenant_id", "reason"],
)

# Jev's own limit for a score question.
_MAX_LEVELS = 10
# The probabilities are a distribution: a sum further than this from 1 is not one.
_SUM_TOLERANCE = 0.02
# Running sums of float probabilities land a hair under an exact bar (0.7 + 0.1 + 0.1 is
# 0.8999999999999999); this keeps "exactly on the bar" counting as reaching it.
_FLOAT_EPSILON = 1e-9
_KEY_PROVIDER_RE = re.compile(r"[a-z0-9_]{2,40}")
# The caller's instructions to its model, not the request: never sent to Jev.
_INSTRUCTION_ROLES = frozenset({"system", "developer"})
# knob: (default, matching the template; minimum; whether the minimum itself is refused)
_NUMBERS = {
    "timeout_ms": (1000, 0, True),
    "max_state_chars": (4000, 0, True),
    "key_cache_s": (60, 0, False),
    "warn_every_s": (3600, 0, False),
}
_DEFAULT_ENOUGH = 0.5

_clock = time.monotonic
# (tenant, key_provider) -> (when looked up, key or None)
_KEY_CACHE: Dict[Tuple[str, str], Tuple[float, Optional[str]]] = {}
# (tenant, reason) -> when last warned
_WARNED: Dict[Tuple[str, str], float] = {}
# config signature -> its problem, or None
_CONFIGS_CHECKED: Dict[str, Optional[str]] = {}


@dataclass
class JevDecision:
    """Jev's answer (``tier`` and ``level``), or why there is none (``fallback``)."""

    tier: Optional[str] = None
    level: Optional[str] = None
    fallback: Optional[str] = None
    detail: str = ""


class _JevCallError(Exception):
    """A failed call, carrying the status the breaker classifies it by."""

    def __init__(self, status_code: int):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


def _is_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def config_problem(jev_cfg: Any, llm_provider_names: Sequence[str],
                   tier_order: Mapping[str, int]) -> Optional[str]:
    """What is wrong with a ``jev`` block, or None when Jev can be asked with it."""
    if not isinstance(jev_cfg, dict):
        return "groups.G6_routing.jev is missing"
    for knob in ("url", "model", "key_provider", "instructions"):
        value = jev_cfg.get(knob)
        if not isinstance(value, str) or not value.strip():
            return f"jev.{knob} is missing"
    key_provider = jev_cfg["key_provider"]
    if not _KEY_PROVIDER_RE.fullmatch(key_provider):
        return (f"jev.key_provider {key_provider!r} must be 2-40 lowercase letters, digits or "
                "underscores: it is the name tenants store their key under")
    if key_provider in set(llm_provider_names):
        return (f"jev.key_provider {key_provider!r} is a configured LLM provider's name, so "
                "tenants' keys for that provider would be sent to Jev")
    enough = jev_cfg.get("enough_probability", _DEFAULT_ENOUGH)
    if not _is_number(enough) or not 0 < enough <= 1:
        return f"jev.enough_probability must be above 0 and at most 1, not {enough!r}"
    for knob, (default, minimum, exclusive) in _NUMBERS.items():
        value = jev_cfg.get(knob, default)
        if not _is_number(value) or value < minimum or (exclusive and value == minimum):
            return (f"jev.{knob} must be a number {'above' if exclusive else 'at least'} "
                    f"{minimum}, not {value!r}")
    levels = jev_cfg.get("levels")
    if not isinstance(levels, list) or not 2 <= len(levels) <= _MAX_LEVELS:
        return f"jev.levels must list 2-{_MAX_LEVELS} levels, lowest first"
    names: set = set()
    previous = -1
    for level in levels:
        if not isinstance(level, dict):
            return "each of jev.levels must be a mapping of name, tier and describe"
        name, tier, describe = level.get("name"), level.get("tier"), level.get("describe")
        if not isinstance(name, str) or not name.strip():
            return "every jev level needs a name"
        if name in names:
            return f"jev level name {name!r} is used twice"
        names.add(name)
        if not isinstance(tier, str) or tier not in tier_order:
            return f"jev level {name!r} names tier {tier!r}, not one of {', '.join(tier_order)}"
        if tier_order[tier] < previous:
            return f"jev level {name!r} steps down to tier {tier!r}: list the levels lowest first"
        previous = tier_order[tier]
        if not isinstance(describe, str) or not describe.strip():
            return f"jev level {name!r} needs a describe: the text Jev rates the request against"
    return None


def _checked(jev_cfg: Any, llm_provider_names: Sequence[str],
             tier_order: Mapping[str, int]) -> Optional[str]:
    """``config_problem``, worked out and warned about once per distinct config."""
    try:
        signature = json.dumps([jev_cfg, sorted(str(n) for n in llm_provider_names)],
                               sort_keys=True, default=str)
    except (TypeError, ValueError):
        signature = repr(jev_cfg)
    if signature not in _CONFIGS_CHECKED:
        problem = config_problem(jev_cfg, llm_provider_names, tier_order)
        _CONFIGS_CHECKED[signature] = problem
        if problem:
            logger.warning("G06 Jev: %s. Requests use the default classifier until it is fixed.",
                           problem)
    return _CONFIGS_CHECKED[signature]


def request_text(messages: Any, max_chars: int) -> str:
    """The request as Jev should judge it: every turn's text except the caller's instructions
    (system and developer messages), text parts of multi-part content included, the newest
    ``max_chars`` kept so the latest question always survives."""
    parts: List[str] = []
    for message in messages or []:
        if not isinstance(message, dict) or message.get("role") in _INSTRUCTION_ROLES:
            continue
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(part["text"] for part in content
                         if isinstance(part, dict) and part.get("type") == "text"
                         and isinstance(part.get("text"), str))
    text = "\n".join(part for part in parts if part.strip())
    return text[-max_chars:] if len(text) > max_chars else text


def pick_level(probabilities: Sequence[float], enough: float) -> int:
    """The lowest level whose probability, plus every level below it, reaches ``enough``,
    after normalising the probabilities. The top level when none does."""
    total = sum(probabilities)
    if total <= 0:
        return len(probabilities) - 1
    running = 0.0
    for index, probability in enumerate(probabilities):
        running += probability / total
        if running >= enough - _FLOAT_EPSILON:
            return index
    return len(probabilities) - 1


def _normalised(text: Any) -> str:
    return " ".join(str(text).split()).casefold()


def answer_probabilities(body: Any, criteria: Sequence[str]) -> Tuple[Optional[List[float]], str]:
    """Jev's probability for each level, lowest first, from its reply; or None and what was
    wrong with the reply. Only an answer that is unmistakably a score over exactly the levels
    sent is used: anything else would route on a misreading."""
    answers = body.get("answers") if isinstance(body, dict) else None
    answer = answers.get(QUESTION) if isinstance(answers, dict) else None
    if not isinstance(answer, dict):
        return None, f"no answer to {QUESTION!r}"
    if answer.get("type") != "score":
        return None, f"a {answer.get('type')!r} answer, not a score"
    expected = [str(i) for i in range(len(criteria))]
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict) or set(probabilities) != set(expected):
        count = len(probabilities) if isinstance(probabilities, dict) else 0
        return None, f"probabilities for {count} levels, not {len(criteria)}"
    values: List[float] = []
    for index in expected:
        value = probabilities[index]
        if not _is_number(value) or not 0 <= value <= 1:
            return None, f"level {index}'s probability is {value!r}"
        values.append(float(value))
    total = sum(values)
    if abs(total - 1) > _SUM_TOLERANCE:
        return None, f"the probabilities sum to {total:.3f}, not 1"
    legend = answer.get("legend")
    if legend is not None and (
            not isinstance(legend, dict) or set(legend) != set(expected)
            or any(_normalised(legend[i]) != _normalised(criteria[int(i)]) for i in expected)):
        return None, "the legend does not match the levels sent"
    return values, ""


def _fallback(ctx: Any, reason: str, detail: str, warn_every: Optional[float]) -> JevDecision:
    """Count a fallback and warn about it once per tenant and reason per ``warn_every``
    seconds. ``warn_every`` None: already warned about elsewhere (a bad config)."""
    tenant = getattr(ctx, "tenant_id", None) or "default"
    FALLBACKS.labels(tenant_id=tenant, reason=reason).inc()
    if warn_every is not None:
        now = _clock()
        last = _WARNED.get((tenant, reason))
        if last is None or now - last >= warn_every:
            _WARNED[(tenant, reason)] = now
            logger.log(logging.INFO if reason == "no_text" else logging.WARNING,
                       "[%s] G06 Jev: %s. Tenant %s uses the default classifier (%s).",
                       getattr(ctx, "request_id", "?"), detail, tenant, reason)
    return JevDecision(fallback=reason, detail=detail)


async def _tenant_key(ctx: Any, provider: str, cache_s: float) -> Tuple[Optional[str], str]:
    """The tenant's key for ``provider`` through the own-key seam, and why there is none.
    A lookup is reused for ``cache_s`` whether it found a key or not: on GCP a missing key
    otherwise costs a blocking secret read and an error line on every request."""
    from providers.resilience import describe_error
    tenant = getattr(ctx, "tenant_id", None) or "default"
    now = _clock()
    cached = _KEY_CACHE.get((tenant, provider))
    if cached is not None and now - cached[0] < cache_s:
        return cached[1], ""
    why = ""
    try:
        from providers.key_resolver import ProviderKeyError, resolve_provider_key
        try:
            key = await resolve_provider_key(provider, tenant, ctx)
        except ProviderKeyError as exc:  # strict own-key denial, or a key that won't decrypt
            key, why = None, type(exc).__name__
    except Exception as exc:  # noqa: BLE001 — a lookup failure is "no key", never a 500
        key, why = None, describe_error(exc)
    _KEY_CACHE[(tenant, provider)] = (now, key or None)
    return key or None, why


def _record_usage(ctx: Any, label: str, body: Any) -> None:
    """Record the call as a side call so G18 prices it (Jev reports input/output tokens)."""
    usage = body.get("usage") if isinstance(body, dict) else None
    if isinstance(usage, dict):
        record_provider_call(ctx, label, {"usage": {
            "prompt_tokens": usage.get("input_tokens") or 0,
            "completion_tokens": usage.get("output_tokens") or 0}}, side=True)


async def classify(ctx: Any, jev_cfg: Any, tier_order: Mapping[str, int]) -> JevDecision:
    """Ask Jev how hard the request is. A decision with ``tier`` set is Jev's; one with
    ``fallback`` set says why the default classifier must decide instead."""
    from providers.resilience import (
        ResilienceConfig, describe_error, get_resilience_store, note_provider_outcome,
    )
    config = getattr(ctx, "config", None) or {}
    providers = config.get("providers") or []
    names = (list(providers) if isinstance(providers, dict) else
             [p.get("name") for p in providers if isinstance(p, dict) and p.get("name")])
    problem = _checked(jev_cfg, names, tier_order)
    if problem:
        return _fallback(ctx, "bad_config", problem, None)

    warn_every = float(jev_cfg.get("warn_every_s", _NUMBERS["warn_every_s"][0]))
    text = request_text(getattr(ctx, "messages", None),
                        int(jev_cfg.get("max_state_chars", _NUMBERS["max_state_chars"][0])))
    if not text.strip():
        return _fallback(ctx, "no_text", "the request has no text for Jev to judge", warn_every)

    provider = jev_cfg["key_provider"]
    key, why = await _tenant_key(ctx, provider,
                                 float(jev_cfg.get("key_cache_s", _NUMBERS["key_cache_s"][0])))
    if not key:
        return _fallback(ctx, "no_key", (
            f"no key stored under {provider!r}" + (f" ({why})" if why else "")
            + f"; tenants add it in the portal, a self-hosted proxy reads LLM_KEY_{provider.upper()}"
        ), warn_every)

    resilience = ResilienceConfig.resolve(config, provider)
    if resilience.enabled and not get_resilience_store().allow_provider(provider, resilience):
        return _fallback(ctx, "breaker_open",
                         "Jev has been failing, so it is skipped until its breaker closes",
                         warn_every)

    criteria = [f"{level['name']}: {level['describe']}" for level in jev_cfg["levels"]]
    payload = {"model": jev_cfg["model"], "state": text, "questions": {QUESTION: {
        "type": "score", "instructions": jev_cfg["instructions"], "criteria": criteria}}}
    timeout_ms = float(jev_cfg.get("timeout_ms", _NUMBERS["timeout_ms"][0]))
    outcome: Optional[BaseException] = None
    try:
        try:
            async with httpx.AsyncClient(timeout=timeout_ms / 1000.0) as client:
                response = await client.post(jev_cfg["url"], json=payload,
                                             headers={"Authorization": f"Bearer {key}"})
        except httpx.TimeoutException:
            outcome = _JevCallError(504)
            return _fallback(ctx, "timeout", f"Jev did not answer within {timeout_ms:g} ms",
                             warn_every)
        except Exception as exc:  # noqa: BLE001 — unreachable, or anything else: never a 500
            outcome = _JevCallError(503)
            return _fallback(ctx, "http_error", f"Jev could not be reached ({describe_error(exc)})",
                             warn_every)
        if not 200 <= response.status_code < 300:
            outcome = _JevCallError(response.status_code)
            return _fallback(ctx, "http_error", f"Jev answered HTTP {response.status_code}",
                             warn_every)
    finally:
        # Timeouts and 5xx count against the breaker; one tenant's 401/403/429 never do.
        note_provider_outcome(provider, outcome, config)

    try:
        body = response.json()
    except ValueError:
        return _fallback(ctx, "bad_answer", "Jev's reply is not JSON", warn_every)
    # Before the check: a reply the check refuses was still billed.
    _record_usage(ctx, f"{provider}/{jev_cfg['model']}", body)
    probabilities, why = answer_probabilities(body, criteria)
    if probabilities is None:
        return _fallback(ctx, "bad_answer", f"Jev's answer failed the check: {why}", warn_every)

    index = pick_level(probabilities,
                       float(jev_cfg.get("enough_probability", _DEFAULT_ENOUGH)))
    level = jev_cfg["levels"][index]
    logger.debug("[%s] G06 Jev: level=%s tier=%s tenant=%s", getattr(ctx, "request_id", "?"),
                 level["name"], level["tier"], getattr(ctx, "tenant_id", "default"))
    return JevDecision(tier=level["tier"], level=level["name"])
