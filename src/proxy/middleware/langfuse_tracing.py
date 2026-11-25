"""
Langfuse tracing helpers for the G0-G28 middleware pipeline.

Provides clean abstractions so individual middleware can emit spans
without importing the Langfuse SDK directly.
"""
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from config_loader import get_config

logger = logging.getLogger(__name__)

_LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "http://localhost:3000")
_LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
_LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")

_client = None
_disabled = False               # permanent off: keys absent or SDK incompatible (restart to change)
_last_init_attempt = 0.0        # monotonic ts of the last *failed* init, for retry throttling
_INIT_RETRY_COOLDOWN = float(os.getenv("LANGFUSE_INIT_RETRY_COOLDOWN", "30"))


def get_client() -> Optional[Any]:
    """Return a cached Langfuse client, or None if unavailable.

    A *transient* init failure (e.g. Langfuse not yet reachable during a
    startup/recreate race) must NOT permanently disable tracing — that would
    silently starve every Langfuse-backed dashboard for the whole process
    lifetime. Such failures are retried on a cooldown. Only a genuinely
    permanent condition (keys absent / incompatible SDK) latches off.
    """
    global _client, _disabled, _last_init_attempt
    if _client is not None:
        return _client
    if _disabled:
        return None
    if not _LANGFUSE_PUBLIC_KEY or not _LANGFUSE_SECRET_KEY:
        _disabled = True  # config-level, won't change without a restart
        logger.warning("Langfuse keys not configured — tracing disabled for this process")
        return None
    # Throttle re-init so a truly-down Langfuse doesn't get hammered per request.
    now = time.monotonic()
    if _last_init_attempt and (now - _last_init_attempt) < _INIT_RETRY_COOLDOWN:
        return None
    _last_init_attempt = now
    try:
        from langfuse import Langfuse
        client = Langfuse(
            host=_LANGFUSE_HOST,
            public_key=_LANGFUSE_PUBLIC_KEY,
            secret_key=_LANGFUSE_SECRET_KEY,
        )
        # Verify SDK is compatible — langfuse>=2.x uses .trace(), v3 uses different API
        if not hasattr(client, "trace"):
            logger.warning("Langfuse SDK does not support .trace() — tracing disabled")
            _disabled = True  # SDK incompatibility won't change at runtime
            return None
        _client = client
        logger.info("Langfuse client initialised (host=%s)", _LANGFUSE_HOST)
        return _client
    except Exception as exc:
        logger.warning(
            "Langfuse client init failed (will retry in ~%.0fs): %s",
            _INIT_RETRY_COOLDOWN, exc,
        )
        return None


# What a G29/G31 action leaves in the text: only `mask` replaces the PII it found.
_PII_LEFT_IN_TEXT = frozenset({"flag", "block"})


def _should_capture_content(ctx: "RequestContext") -> bool:
    """Whether a trace may carry the request's raw prompt and response (I1).

    All tenants share one Langfuse project, so raw ``input``/``output`` would be
    visible to anyone with project access. Off unless ``capture_trace_content`` is true
    under G18_observability (a tenant's override wins); token counts and savings metadata
    are recorded either way. Even when on, a request whose PII G29 or G31 found and left in
    the text (``flag``, or a ``block``) is traced without its content.
    """
    if (getattr(ctx, "pii_action", None) in _PII_LEFT_IN_TEXT
            or getattr(ctx, "context_trust_pii_action", None) in _PII_LEFT_IN_TEXT):
        return False
    try:
        base = ctx.config.get("groups", {}).get("G18_observability", {})
        tenant_cfg = (
            ctx.config.get("tenants", {})
            .get(getattr(ctx, "tenant_id", "default"), {})
            .get("groups", {})
            .get("G18_observability", {})
        )
        return bool(tenant_cfg.get("capture_trace_content", base.get("capture_trace_content", False)))
    except Exception:
        return False


# The tag every trace of this proxy carries; usage-tag tags come after it.
_BASE_TAGS = ("token-optimisation",)
# Langfuse's own limit on a tag and on a session id: it drops a longer one, so a value taken
# from a usage tag (up to billing.metering.tags.max_value_chars) is cut to it.
_MAX_TAG_CHARS = 200


def _g18_setting(ctx: "RequestContext", key: str, default: Any) -> Any:
    """A G18_observability setting as this request sees it: the tenant's own value under
    ``tenants.<id>.groups.G18_observability`` when it sets one, else the base value (read the
    same way as ``capture_trace_content``)."""
    base = ctx.config.get("groups", {}).get("G18_observability", {})
    tenant_cfg = (
        ctx.config.get("tenants", {})
        .get(getattr(ctx, "tenant_id", "default"), {})
        .get("groups", {})
        .get("G18_observability", {})
    )
    return tenant_cfg.get(key, base.get(key, default))


def _operator_g18_setting(key: str, default: Any) -> Any:
    """A G18_observability setting from the operator's global config (``get_config``, what
    the pipeline reads usage tags with), never from ``ctx.config``: that one has the
    tenant's own overrides merged in, and a control that keeps tenants apart must not be the
    tenant's to change. Unreadable: the default."""
    config = get_config()
    groups = config.get("groups") if isinstance(config, dict) else None
    g18 = groups.get("G18_observability") if isinstance(groups, dict) else None
    return g18.get(key, default) if isinstance(g18, dict) else default


def _usage_tag_fields(ctx: "RequestContext") -> Tuple[Dict[str, Any], List[str], Optional[str]]:
    """What a request's usage tags and trace id add to its Langfuse trace: the metadata keys,
    the Langfuse tags after the base one, and a session id to use when the request names none.

    With ``langfuse_usage_tags`` on (the default): ``otel_trace_id`` (the exported trace's id,
    else the caller's ``traceparent`` id) when one is known, and ``usage_tags`` (a copy of
    ``ctx.usage_tags``: allow-listed headers, credential-shaped values already dropped by
    ``billing.tags``) when the request carried any. The tenant-only tag keys
    (``billing.metering.tags.tenant_only_keys``, default ``user``) are left out first: they
    reach neither the metadata nor a tag nor the session, whatever the settings below list.
    ``langfuse_tag_keys`` lists the tag keys that also become a ``<key>:<value>`` Langfuse
    tag, in that order; ``langfuse_session_tag`` names the one whose value is the session.
    That session id is ``<tenant>::<value>`` while ``langfuse_session_tenant_scope`` is on
    (the default): every tenant shares one Langfuse project, and the value is the caller's
    to choose, so unscoped one tenant could add traces to another's session. The scope and
    the tenant-only keys are the operator's (the global config): a tenant's override of
    either is ignored, while the other three settings shape only its own traces. A request
    without usage tags adds neither tags nor a session, so its trace is what it was. Never
    raises: nothing is added instead."""
    try:
        if not _g18_setting(ctx, "langfuse_usage_tags", True):
            return {}, [], None
        metadata: Dict[str, Any] = {}
        from tracing.otel import request_trace_id
        trace_id = request_trace_id(ctx)
        if trace_id:
            metadata["otel_trace_id"] = trace_id
        usage_tags = getattr(ctx, "usage_tags", None)
        if not isinstance(usage_tags, dict) or not usage_tags:
            return metadata, [], None
        from billing.tags import tenant_only_keys, without_tenant_only
        # The operator's list, as the usage export reads it: not the tenant's to shorten.
        usage_tags = without_tenant_only(usage_tags, tenant_only_keys(get_config()))
        if not usage_tags:
            return metadata, [], None
        metadata["usage_tags"] = dict(usage_tags)
        tags: List[str] = []
        keys = _g18_setting(ctx, "langfuse_tag_keys", [])
        for key in keys if isinstance(keys, (list, tuple)) else ():
            if isinstance(key, str) and key in usage_tags:
                tag = f"{key}:{usage_tags[key]}"[:_MAX_TAG_CHARS]
                if tag not in tags:
                    tags.append(tag)
        session_id = None
        session_key = _g18_setting(ctx, "langfuse_session_tag", "")
        if isinstance(session_key, str) and session_key:
            value = usage_tags.get(session_key)
            if isinstance(value, str) and value:
                tenant_id = getattr(ctx, "tenant_id", "")
                if (_operator_g18_setting("langfuse_session_tenant_scope", True)
                        and isinstance(tenant_id, str) and tenant_id):
                    value = f"{tenant_id}::{value}"
                session_id = value[:_MAX_TAG_CHARS]
        return metadata, tags, session_id
    except Exception as exc:
        logger.debug("Langfuse usage tags skipped: %s", exc)
        return {}, [], None


def start_trace(ctx: "RequestContext") -> Optional[Any]:
    """Create a Langfuse trace at the beginning of a request and store it on ctx."""
    cfg = ctx.config.get("groups", {}).get("G18_observability", {})
    # Two gates: G18 observability on, AND Langfuse tracing explicitly enabled.
    # `langfuse_enabled` defaults False so OSS ships tracing OFF; the commercial
    # deploy flips it true. This gates only Langfuse trace emission — Prometheus
    # and savings metrics are unaffected, and keys are still required (get_client).
    if not cfg.get("enabled", False) or not cfg.get("langfuse_enabled", False):
        return None

    client = get_client()
    if not client:
        return None

    try:
        session_id = (
            ctx.params.get("workflow_id")
            or ctx.params.get("x_workflow_id")
            or ctx.params.get("session_id")
            or ctx.params.get("x_session_id")
        )
        # The caller's usage tags and trace id (langfuse_usage_tags): found by run, joined
        # to usage_events. A workflow or session id the request names still decides.
        tag_metadata, usage_tag_tags, tag_session_id = _usage_tag_fields(ctx)
        session_id = session_id or tag_session_id
        trace = client.trace(
            id=ctx.request_id,
            name="llm-proxy-call",
            user_id=ctx.user_id,
            metadata={
                "model_requested": ctx.model,
                "baseline_tokens": ctx.savings.baseline_tokens,
                "user_id": ctx.user_id,
                "tenant_id": ctx.tenant_id,
                **tag_metadata,
            },
            tags=[*_BASE_TAGS, *usage_tag_tags],
            session_id=session_id,
        )
        ctx.langfuse_trace = trace
        return trace
    except Exception as exc:
        logger.warning("Langfuse trace creation failed: %s", exc)
        return None


def _measurements_only(payload: Any) -> Optional[Dict[str, Any]]:
    """A span payload's numbers and flags (token counts, ratios, hit/miss), without any
    text, list or nested object that could carry the request's content."""
    if not isinstance(payload, dict):
        return None
    return {k: v for k, v in payload.items()
            if v is None or isinstance(v, (bool, int, float))}


def add_span(
    ctx: "RequestContext",
    name: str,
    span_input: Any = None,
    output: Any = None,
    metadata: Optional[Dict[str, Any]] = None,
    usage: Optional[Dict[str, Any]] = None,
) -> Optional[Any]:
    """Add a child span to the current trace."""
    trace = getattr(ctx, "langfuse_trace", None)
    if not trace:
        return None
    # I1: without content capture a span keeps its measurements, never its text.
    if not _should_capture_content(ctx):
        span_input = _measurements_only(span_input)
        output = _measurements_only(output)
    try:
        return trace.span(
            name=name,
            input=span_input,
            output=output,
            metadata=metadata or {},
            usage=usage,
        )
    except Exception as exc:
        logger.warning("Langfuse span creation failed (%s): %s", name, exc)
        return None


def add_score(
    ctx: "RequestContext",
    name: str,
    value: float,
    comment: Optional[str] = None,
) -> None:
    """Add a score to the current trace."""
    trace = getattr(ctx, "langfuse_trace", None)
    if not trace:
        return
    try:
        trace.score(name=name, value=value, comment=comment)
    except Exception as exc:
        logger.warning("Langfuse score creation failed (%s): %s", name, exc)


def finish_trace(ctx: "RequestContext", response: Optional[Dict[str, Any]] = None) -> None:
    """Add the final LLM generation and flush the trace."""
    trace = getattr(ctx, "langfuse_trace", None)
    if not trace:
        return

    try:
        usage: Optional[Dict[str, Any]] = None
        output_text = ""
        if response:
            usage_data = response.get("usage", {})
            prompt_tokens = usage_data.get("prompt_tokens", ctx.savings.final_tokens_sent)
            output_tokens = usage_data.get("completion_tokens", 0)
            total_tokens = usage_data.get("total_tokens", prompt_tokens + output_tokens)
            usage = {
                "input": prompt_tokens,
                "output": output_tokens,
                "total": total_tokens,
                "unit": "TOKENS",
            }
            choices = response.get("choices", [{}])
            if choices:
                output_text = choices[0].get("message", {}).get("content", "")

        metadata = ctx.savings.to_langfuse_metadata()

        # I1: omit raw prompt/response for tenants that opt out of content capture
        # (shared Langfuse project → no per-tenant RBAC on stored content).
        if _should_capture_content(ctx):
            gen_input = ctx.messages          # G29 already masked these in mask mode
            gen_output = output_text
            # G29 mask mode un-masks the response for the CLIENT (coherence), but the
            # persisted trace must not carry the restored PII — re-apply the placeholders
            # to the traced output only (the returned response object is untouched). F3.
            vault = getattr(ctx, "pii_vault", None)
            if vault:
                from guardrails.pii import remask_with_vault
                gen_output = remask_with_vault(gen_output, vault)
        else:
            gen_input = gen_output = "[redacted: capture_trace_content=false]"

        trace.generation(
            name="llm-completion",
            model=ctx.routed_model,
            input=gen_input,
            output=gen_output,
            usage=usage,
            metadata=metadata,
        )

        # Write full savings metadata to the trace so Grafana SQL queries on
        # traces.metadata can find total_abs_saving, cost_saving_usd,
        # total_pct_saving, and step_savings. Also re-affirms identity fields
        # in case start_trace ran before tenant resolution.
        # Spread savings metadata first, then re-affirm live identity fields last
        # so ctx.user_id / ctx.tenant_id always win over any stale snapshot in
        # metadata (and in case start_trace ran before tenant resolution).
        trace_meta = {
            **metadata,
            "user_id": ctx.user_id,
            "tenant_id": ctx.tenant_id,
            # The usage tags and trace id start_trace recorded, re-affirmed with the identity
            # fields so the final metadata carries them (langfuse_usage_tags).
            **_usage_tag_fields(ctx)[0],
        }
        # Record the RAG collection (set from the X-Rag-Collection header) so
        # dashboards can classify RAG / Doc-Q&A requests. Without this the
        # pitch "Medium — Doc Q&A" panel can never match a request.
        rag_collection = ctx.params.get("x_rag_collection") or ctx.params.get("rag_collection")
        if rag_collection:
            trace_meta["rag_collection"] = rag_collection
            trace_meta["x_rag_collection"] = rag_collection
        # Workload complexity tier (simple/medium/complex) — set from the
        # X-Complexity-Tier header (auto-mapped to params.x_complexity_tier).
        # Lets the pitch dashboard classify Simple/Medium/Complex requests by an
        # intrinsic workload label instead of a hardcoded model name (the
        # "complex" model is per-run/per-tenant config). Mirrors rag_collection.
        complexity_tier = ctx.params.get("x_complexity_tier") or ctx.params.get("complexity_tier")
        if complexity_tier:
            trace_meta["complexity_tier"] = complexity_tier
        trace.update(metadata=trace_meta)

        _add_scores(ctx, trace)

        # Do NOT flush synchronously here. `trace.generation()`/`.update()` only
        # enqueue events in-memory; the persistent Langfuse client (module-level
        # singleton, created once) runs its own background consumer thread that
        # flushes on an interval and on process exit (atexit). A per-request
        # client.flush() forces a blocking HTTP round-trip to Langfuse on the
        # response critical path — when Langfuse is slow/backed up that surfaced
        # as multi-second G18-observability stage latency (the SLA "Proxy only"
        # tail). Delivery is unchanged; only the timing moves off-path.
    except Exception as exc:
        logger.warning("Langfuse finish_trace failed: %s", exc)


def _add_scores(ctx: "RequestContext", trace: Any) -> None:
    """Attach evaluation scores to the trace."""
    # Token savings percentage
    try:
        trace.score(name="savings_pct", value=float(ctx.savings.total_pct_saving))
    except Exception as exc:
        logger.debug("Langfuse score savings_pct failed: %s", exc)

    # Cache hit rate (binary per request)
    try:
        trace.score(name="cache_hit_rate", value=1.0 if ctx.savings.cache_hit else 0.0)
    except Exception as exc:
        logger.debug("Langfuse score cache_hit_rate failed: %s", exc)

    # Turn efficiency (higher turn count = lower score)
    workflow_id = ctx.params.get("workflow_id") or ctx.params.get("x_workflow_id")
    if workflow_id:
        try:
            turn_count = ctx.params.get("_token_budget", {}).get("workflow_turn", 0)
            if turn_count <= 3:
                efficiency = 1.0
            elif turn_count <= 6:
                efficiency = 0.5
            else:
                efficiency = 0.0
            trace.score(
                name="turn_efficiency",
                value=efficiency,
                comment=f"turn_count={turn_count}",
            )
        except Exception as exc:
            logger.debug("Langfuse score turn_efficiency failed: %s", exc)

    # Compression ratio (when G1 is active)
    g1_step = next((s for s in ctx.savings.step_savings if s.group == "G01"), None)
    if g1_step and g1_step.tokens_before > 0:
        try:
            ratio = 1.0 - (g1_step.tokens_after / g1_step.tokens_before)
            trace.score(name="compression_ratio", value=round(ratio, 2))
        except Exception as exc:
            logger.debug("Langfuse score compression_ratio failed: %s", exc)
