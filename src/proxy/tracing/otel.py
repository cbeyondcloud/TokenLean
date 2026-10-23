"""
OpenTelemetry tracing for the Token Optimisation proxy pipeline.

Every middleware stage in pipeline.py wraps its call with start_span / end_span
so the full G0→G32 execution graph is visible in Jaeger.  The Langfuse layer
(langfuse_tracing.py) handles LLM-specific spans; this layer covers the proxy
pipeline graph.

Exports via OTLP gRPC to Jaeger (or any OTLP collector), set up once at startup by
``configure`` from the ``tracing`` config block: ``enabled``, ``otlp_endpoint``,
``sample_rate`` and ``service_name``. The OTEL_EXPORTER_OTLP_ENDPOINT environment variable
wins for the endpoint, and with no ``enabled`` set it is what turns export on. Until then,
and whenever it is off, every span is a no-op.

A request is one trace. Its request span (``start_request_span``, ``ctx.otel_request_span``)
covers the whole request, carries the OpenTelemetry GenAI semantic-convention attributes, and
is a child of the caller's span when the request carries a valid W3C ``traceparent`` (and
``tracestate``); a malformed one is ignored and the request starts a new trace. The pipeline
span (``ctx.otel_span``) sits under it, each stage span under that, and
``TraceIdHeaderMiddleware`` names the trace on the response as X-Trace-ID
(``propagate_trace_id_header``, on by default). ``forward_traceparent`` (off by default) sends
the request span's context to the provider on every call.

OSS stack: opentelemetry-sdk (Apache-2), opentelemetry-exporter-otlp (Apache-2).
Gracefully no-ops when opentelemetry is not installed so unit tests run without
the SDK dependency.
"""
import logging
import os
from typing import Any, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

# ── Optional OTel import (graceful no-op if SDK not installed) ────────────────
try:
    from opentelemetry import trace
    from opentelemetry.trace import SpanKind, StatusCode
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
    _sdk_available = True
except Exception:  # ImportError
    trace = None
    _sdk_available = False

_DEFAULT_ENDPOINT = "http://jaeger:4317"
_otel_available = False          # True once configure() has set up an exporter
_tracer = None
_provider = None
_propagate_header = False        # tracing.propagate_trace_id_header, while spans are exported
_forward_traceparent = False     # tracing.forward_traceparent, while spans are exported
TRACE_ID_HEADER = b"x-trace-id"
# W3C trace context (https://www.w3.org/TR/trace-context/): the headers read from a request
# and, with forward_traceparent on, written on the provider call.
_TRACE_CONTEXT_HEADERS = ("traceparent", "tracestate")

# OpenTelemetry GenAI semantic-convention attributes set on the request span. A backend's
# dashboards key on these names, so they are pinned by a test. gen_ai.provider.name is the
# current name; gen_ai.system is the older one, still read by many backends, set alongside.
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_PROVIDER_NAME = "gen_ai.provider.name"
GEN_AI_SYSTEM = "gen_ai.system"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
# Every model route is a chat completion inside the proxy, whatever the client's protocol.
GEN_AI_OPERATION = "chat"
# The client chooses the model string: capped on a span so no request can export an
# arbitrarily long name.
_MAX_MODEL_CHARS = 128


def _settings(config: Any, env: Mapping[str, str] = os.environ) -> Dict[str, Any]:
    """What ``configure`` sets up, from the ``tracing`` block and the environment.

    Spans are exported when ``tracing.enabled`` says so, or, with ``enabled`` unset, when
    OTEL_EXPORTER_OTLP_ENDPOINT names a collector (docker-compose sets it). A deploy with
    neither exports nothing: it used to send every span to jaeger:4317, which is not there,
    and retry. The variable also wins over ``otlp_endpoint``, as the template documents. TLS
    is used unless the endpoint is plain ``http://``."""
    block = config.get("tracing") if isinstance(config, dict) else None
    block = block if isinstance(block, dict) else {}
    env_endpoint = env.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    enabled = block.get("enabled")
    endpoint = env_endpoint or str(block.get("otlp_endpoint") or _DEFAULT_ENDPOINT)
    try:
        rate = min(1.0, max(0.0, float(block.get("sample_rate", 1.0))))
    except (TypeError, ValueError):
        rate = 1.0
    return {"enabled": bool(env_endpoint) if enabled is None else bool(enabled),
            "endpoint": endpoint, "insecure": endpoint.startswith("http://"),
            "sample_rate": rate,
            "service_name": str(block.get("service_name") or "token-opt-proxy"),
            "propagate_trace_id_header": bool(block.get("propagate_trace_id_header", True)),
            # Strictly True: an unexpanded "false" string must not send trace ids upstream.
            "forward_traceparent": block.get("forward_traceparent") is True}


def configure(config: Any, exporter: Any = None) -> bool:
    """Set up pipeline tracing from the proxy config (once, at startup; a change applies at
    the next start). ``exporter`` replaces the OTLP exporter (tests). Returns whether spans
    are now exported."""
    global _otel_available, _tracer, _provider, _propagate_header, _forward_traceparent
    s = _settings(config)
    _forward_traceparent = False
    if not (_sdk_available and s["enabled"]):
        _otel_available, _tracer, _provider = False, None, None
        logger.info("OTel pipeline tracing is off%s", "" if _sdk_available else
                    " (opentelemetry is not installed)")
        return False
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
        if exporter is None:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            processor = BatchSpanProcessor(
                OTLPSpanExporter(endpoint=s["endpoint"], insecure=s["insecure"]))
        else:
            processor = SimpleSpanProcessor(exporter)
        provider = TracerProvider(
            resource=Resource.create({"service.name": s["service_name"]}),
            sampler=ParentBased(TraceIdRatioBased(s["sample_rate"])))
        provider.add_span_processor(processor)
        _provider = provider
        _tracer = provider.get_tracer("token-opt-proxy",
                                      schema_url="https://opentelemetry.io/schemas/1.11.0")
        _otel_available = True
        _propagate_header = s["propagate_trace_id_header"]
        _forward_traceparent = s["forward_traceparent"]
        logger.info("OTel pipeline tracing → %s (sample rate %s)", s["endpoint"],
                    s["sample_rate"])
        return True
    except Exception as exc:
        _otel_available, _tracer, _provider = False, None, None
        logger.warning("OTel pipeline tracing could not start, so no spans are exported: %s",
                       exc)
        return False


class _NoOpSpan:
    """Returned when OTel is not available so callers need no None checks."""
    trace_id: int = 0

    def set_attribute(self, *_a: Any, **_kw: Any) -> None:  # noqa: D401
        pass

    def set_status(self, *_a: Any, **_kw: Any) -> None:
        pass

    def record_exception(self, *_a: Any, **_kw: Any) -> None:
        pass

    def end(self) -> None:
        pass

    def __enter__(self) -> "_NoOpSpan":
        return self

    def __exit__(self, *_: Any) -> None:
        pass


def start_span(name: str, ctx: Any) -> Any:
    """Start an OTel span for a pipeline stage, as a child of the request's pipeline span
    (``ctx.otel_span``) when it has one.

    Args:
        name: Human-readable stage name, e.g. ``"G01-compression"``.
        ctx:  ``RequestContext`` — span attributes ``request_id`` and
              ``tenant_id`` are read from it.

    Returns:
        The OTel span (or a no-op span if OTel is unavailable).
    """
    if not _otel_available or _tracer is None:
        return _NoOpSpan()

    try:
        # Under the pipeline span; the pipeline span itself goes under the request span.
        parent = getattr(ctx, "otel_span", None)
        if parent is None or isinstance(parent, _NoOpSpan):
            parent = getattr(ctx, "otel_request_span", None)
        context = (trace.set_span_in_context(parent)
                   if parent is not None and not isinstance(parent, _NoOpSpan) else None)
        span = _tracer.start_span(name, context=context)
        span.set_attribute("proxy.request_id", getattr(ctx, "request_id", ""))
        span.set_attribute("proxy.tenant_id", getattr(ctx, "tenant_id", "default"))
        span.set_attribute("proxy.model", getattr(ctx, "model", ""))
        return span
    except Exception as exc:
        logger.debug("OTel start_span failed: %s", exc)
        return _NoOpSpan()


def end_span(span: Any, *, error: Optional[Exception] = None) -> None:
    """End an OTel span, optionally recording an error.

    Args:
        span:  Span returned by ``start_span``.
        error: If provided, records the exception and sets ERROR status.
    """
    if span is None:
        return
    try:
        if error is not None:
            span.record_exception(error)
            if _otel_available:
                span.set_status(StatusCode.ERROR, str(error))
        elif _otel_available:
            span.set_status(StatusCode.OK)
        span.end()
    except Exception as exc:
        logger.debug("OTel end_span failed: %s", exc)


def _trace_context_carrier(headers: Any) -> Dict[str, str]:
    """The W3C trace-context headers of a request, by lower-case name. Read from every
    header line (Starlette's ``Headers.items()`` keeps repeats): more than one
    ``traceparent`` is malformed (W3C) and is dropped, so the request starts a new trace on
    every route alike; several ``tracestate`` lines combine, as the spec says."""
    if not hasattr(headers, "items"):
        return {}
    parents: List[str] = []
    states: List[str] = []
    for key, value in headers.items():
        name = str(key).lower()
        if not isinstance(value, str):
            continue
        if name == "traceparent":
            parents.append(value)
        elif name == "tracestate":
            states.append(value)
    if len(parents) != 1:
        return {}
    carrier = {"traceparent": parents[0]}
    if states:
        carrier["tracestate"] = ",".join(states)
    return carrier


def parent_context(headers: Any) -> Any:
    """The caller's span, from the request's ``traceparent`` (and ``tracestate``), as the
    OTel context a span starts under; None when there is none or it is malformed (the W3C
    propagator rejects a bad version, length, case or an all-zero id), so the request starts
    a new trace instead. Never raises."""
    if not _otel_available:
        return None
    carrier = _trace_context_carrier(headers)
    if not carrier.get("traceparent"):
        return None
    try:
        context = TraceContextTextMapPropagator().extract(carrier)
        if not trace.get_current_span(context).get_span_context().is_valid:
            return None
        return context
    except Exception as exc:
        logger.debug("traceparent ignored: %s", exc)
        return None


def start_request_span(ctx: Any, headers: Any) -> Any:
    """Start the request span: the whole request, as a child of the caller's span when
    ``headers`` carry a valid ``traceparent``, else a new trace's root. A server span (the
    proxy is answering a caller), named ``chat <model>`` as the GenAI conventions name an
    inference span. ``finish_request_span`` ends it. A no-op span when tracing is off."""
    if not _otel_available or _tracer is None:
        return _NoOpSpan()
    try:
        model = str(getattr(ctx, "model", "") or "")[:_MAX_MODEL_CHARS]
        span = _tracer.start_span(f"{GEN_AI_OPERATION} {model}".strip(),
                                  context=parent_context(headers), kind=SpanKind.SERVER)
        span.set_attribute(GEN_AI_OPERATION_NAME, GEN_AI_OPERATION)
        span.set_attribute(GEN_AI_REQUEST_MODEL, model)
        span.set_attribute("proxy.request_id", str(getattr(ctx, "request_id", "") or ""))
        span.set_attribute("proxy.ingress_protocol",
                           str(getattr(ctx, "ingress_protocol", "") or ""))
        return span
    except Exception as exc:
        logger.debug("OTel start_request_span failed: %s", exc)
        return _NoOpSpan()


def _tokens(calls: Any, key: str) -> int:
    total = 0
    for call in calls:
        try:
            total += int(call.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return total


def finish_request_span(ctx: Any, status: Any, response: Any = None) -> None:
    """End the request span once, at the request's outcome (``main._record_outcome``), with
    what is known by then: the provider and model that served, the tokens of every provider
    call the request paid for (none on a cache hit or a refusal), the HTTP status and the
    tenant. A 5xx marks the span as an error. A second call (another exit path) does nothing;
    so does a context without a request span. Never raises."""
    span = getattr(ctx, "otel_request_span", None) if ctx is not None else None
    if (span is None or isinstance(span, _NoOpSpan)
            or getattr(ctx, "otel_request_span_ended", False)):
        return
    try:
        ctx.otel_request_span_ended = True
        model = str(getattr(ctx, "model", "") or "")[:_MAX_MODEL_CHARS]
        span.update_name(f"{GEN_AI_OPERATION} {model}".strip())
        span.set_attribute(GEN_AI_REQUEST_MODEL, model)
        adapter = getattr(ctx, "provider_adapter", None)
        provider = str(getattr(adapter, "name", "") or "")
        if provider:
            # The OpenTelemetry well-known name the adapter declares (aws.bedrock,
            # gcp.gemini...), else TokenLean's own; TokenLean's name stays in proxy.provider.
            semconv = str(getattr(adapter, "semconv_provider_name", "") or provider)
            span.set_attribute(GEN_AI_PROVIDER_NAME, semconv)
            span.set_attribute(GEN_AI_SYSTEM, semconv)
            span.set_attribute("proxy.provider", provider)
        reported = response.get("model") if isinstance(response, dict) else None
        span.set_attribute(GEN_AI_RESPONSE_MODEL,
                           str(reported or getattr(ctx, "routed_model", "") or model)
                           [:_MAX_MODEL_CHARS])
        calls = [c for c in (getattr(ctx, "provider_calls", None) or []) if isinstance(c, dict)]
        if calls:
            span.set_attribute(GEN_AI_USAGE_INPUT_TOKENS, _tokens(calls, "prompt_tokens"))
            span.set_attribute(GEN_AI_USAGE_OUTPUT_TOKENS, _tokens(calls, "completion_tokens"))
        span.set_attribute("proxy.tenant_id", str(getattr(ctx, "tenant_id", "") or ""))
        try:
            code = int(status)
        except (TypeError, ValueError):
            code = 0
        if code:
            span.set_attribute("http.response.status_code", code)
        if code >= 500:
            span.set_status(StatusCode.ERROR)
        span.end()
    except Exception as exc:
        logger.debug("OTel finish_request_span failed: %s", exc)


def outgoing_trace_headers(ctx: Any) -> Dict[str, str]:
    """The W3C trace-context headers for a provider call (the request span as the parent),
    when ``tracing.forward_traceparent`` is on and spans are exported; otherwise none, so no
    trace id goes to a provider by default."""
    if not (_otel_available and _forward_traceparent):
        return {}
    span = getattr(ctx, "otel_request_span", None)
    if span is None or isinstance(span, _NoOpSpan):
        return {}
    try:
        carrier: Dict[str, str] = {}
        TraceContextTextMapPropagator().inject(carrier, context=trace.set_span_in_context(span))
        return {k: v for k, v in carrier.items() if k in _TRACE_CONTEXT_HEADERS}
    except Exception as exc:
        logger.debug("OTel trace-context inject failed: %s", exc)
        return {}


def response_trace_id(ctx: Any) -> str:
    """The trace id a response names in X-Trace-ID: its request's trace (the pipeline span
    ``ctx.otel_span``, else the request span), while spans are exported and
    ``tracing.propagate_trace_id_header`` is on (the default). Empty otherwise, and the header
    is left out."""
    if not (_otel_available and _propagate_header):
        return ""
    return (get_trace_id(getattr(ctx, "otel_span", None))
            or get_trace_id(getattr(ctx, "otel_request_span", None)))


class TraceIdHeaderMiddleware:
    """ASGI middleware: put the request's trace id on its response as X-Trace-ID. The request
    handler leaves its RequestContext on ``request.state.pipeline_ctx``; the id is read when
    the response starts, by when the pipeline span exists, so every response to a pipeline
    request carries it: served, streamed or refused, on every protocol route."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        state = scope.setdefault("state", {})

        async def send_with_trace_id(message):
            if message.get("type") == "http.response.start":
                trace_id = response_trace_id(state.get("pipeline_ctx"))
                if trace_id:
                    headers = [(k, v) for k, v in message.get("headers") or []
                               if k.lower() != TRACE_ID_HEADER]
                    message = dict(message, headers=[*headers, (TRACE_ID_HEADER,
                                                                trace_id.encode("latin-1"))])
            await send(message)

        await self.app(scope, receive, send_with_trace_id)


def get_trace_id(span: Any) -> str:
    """Return the W3C trace-id hex string for a span (empty string if unavailable)."""
    if span is None or isinstance(span, _NoOpSpan):
        return ""
    try:
        ctx = span.get_span_context()
        tid = ctx.trace_id
        if tid == 0:
            return ""
        return format(tid, "032x")
    except Exception:
        return ""
