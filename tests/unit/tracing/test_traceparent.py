"""W3C trace context on every model endpoint (ST-5, M2-33).

A caller that sends ``traceparent`` (and ``tracestate``) gets TokenLean's request span as a
CHILD of its own span: the same trace id, the caller's span as parent. The request span
covers the whole request (pipeline, provider call, accounting) and carries the OpenTelemetry
GenAI semantic-convention attributes; the pipeline span and every stage span sit under it, so
X-Trace-ID names the caller's trace. A malformed header is ignored (a new root trace), never
an error. With tracing off nothing changes. ``traceparent`` is forwarded to providers only
when ``tracing.forward_traceparent`` is on (default off).
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))
from types import SimpleNamespace

import pytest

pytest.importorskip("opentelemetry.sdk.trace")

from fastapi.testclient import TestClient  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402
from opentelemetry.trace import SpanKind  # noqa: E402

import main  # noqa: E402
from middleware.g32_tool_eligibility import G32ToolEligibility  # noqa: E402
from tracing import otel  # noqa: E402

_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
_SPAN = "00f067aa0ba902b7"
_TRACEPARENT = f"00-{_TRACE}-{_SPAN}-01"
_CFG = {"groups": {}, "providers": []}

# The GenAI semantic-convention attribute names this proxy sets (pinned: a backend's
# dashboards key on them, so a rename is a breaking change).
_GEN_AI = ("gen_ai.operation.name", "gen_ai.provider.name", "gen_ai.system",
           "gen_ai.request.model", "gen_ai.response.model", "gen_ai.usage.input_tokens",
           "gen_ai.usage.output_tokens")


class _Pipeline:
    """Stands in for OptimisationPipeline: opens the pipeline span as it does, then answers
    from the cache, so no provider is involved."""

    def __init__(self):
        self.g32 = G32ToolEligibility()

    async def process_request(self, ctx, request_headers=None):
        ctx.otel_span = otel.start_span("proxy-pipeline", ctx)
        ctx.cache_hit = ctx.savings.cache_hit = True
        ctx.cache_level = ctx.savings.cache_level = "L1"
        ctx.cache_response = {
            "id": "chatcmpl-cached", "object": "chat.completion", "created": 1700000000,
            "model": "gpt-4o-mini",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "Paris"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
        }
        otel.end_span(ctx.otel_span)
        return ctx

    async def process_response(self, ctx, response):
        return ctx, response


async def _fake_auth(request, *_protocol):
    return "acme", "tok-x", {"tenant_id": "acme", "tier": "enterprise"}


@pytest.fixture
def memory():
    """Spans exported to memory; tracing state restored afterwards."""
    saved = (otel._otel_available, otel._tracer, otel._provider, otel._propagate_header,
             otel._forward_traceparent)
    exporter = InMemorySpanExporter()

    def configure(**block):
        assert otel.configure({"tracing": {"enabled": True, **block}}, exporter=exporter)
        return exporter
    yield configure
    (otel._otel_available, otel._tracer, otel._provider, otel._propagate_header,
     otel._forward_traceparent) = saved


_BODIES = {
    "/v1/chat/completions": {"model": "gpt-4o-mini",
                             "messages": [{"role": "user", "content": "hi"}]},
    "/v1/messages": {"model": "gpt-4o-mini", "max_tokens": 16,
                     "messages": [{"role": "user", "content": "hi"}]},
    "/v1/responses": {"model": "gpt-4o-mini", "input": "hi"},
    "/v1beta/models/gpt-4o-mini:generateContent": {
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
}


def _post(monkeypatch, path="/v1/chat/completions", headers=None):
    monkeypatch.setattr(main, "_authenticate", _fake_auth)
    monkeypatch.setattr(main, "get_config", lambda: _CFG)
    monkeypatch.setattr(main, "_pipeline", _Pipeline())
    monkeypatch.setattr(main, "_usage_meter", None)
    return TestClient(main.app).post(path, json=_BODIES[path], headers={
        "Authorization": "Bearer tok-x", **(headers or {})})


def _spans(exporter):
    spans = exporter.get_finished_spans()
    requests = [s for s in spans if s.kind == SpanKind.SERVER]
    assert len(requests) == 1, [s.name for s in spans]
    return requests[0], {s.name: s for s in spans}


@pytest.mark.parametrize("path", list(_BODIES))
def test_the_request_span_is_a_child_of_the_callers_span(monkeypatch, memory, path):
    exporter = memory()
    resp = _post(monkeypatch, path, {"traceparent": _TRACEPARENT})
    assert resp.status_code == 200, resp.text
    request, by_name = _spans(exporter)
    assert format(request.context.trace_id, "032x") == _TRACE
    assert request.parent is not None and request.parent.is_remote
    assert format(request.parent.span_id, "016x") == _SPAN
    # the pipeline span (and so every stage span) sits under the request span
    pipeline = by_name["proxy-pipeline"]
    assert pipeline.parent.span_id == request.context.span_id
    assert pipeline.context.trace_id == request.context.trace_id
    # X-Trace-ID keeps working, and now names the caller's trace
    assert resp.headers["x-trace-id"] == _TRACE


def test_a_caller_that_did_not_sample_is_honoured(monkeypatch, memory):
    # W3C sampled flag 00: the caller is not recording this trace, so neither is the proxy
    # (parent-based sampling); the response still names the caller's trace.
    exporter = memory()
    resp = _post(monkeypatch, headers={"traceparent": f"00-{_TRACE}-{_SPAN}-00"})
    assert resp.status_code == 200
    assert exporter.get_finished_spans() == ()
    assert resp.headers["x-trace-id"] == _TRACE


def test_tracestate_travels_with_the_trace(monkeypatch, memory):
    exporter = memory()
    _post(monkeypatch, headers={"traceparent": _TRACEPARENT, "tracestate": "acme=t61rcWkgMzE"})
    request, _ = _spans(exporter)
    assert request.context.trace_state.get("acme") == "t61rcWkgMzE"


@pytest.mark.parametrize("traceparent", [
    "garbage",
    f"00-{'0' * 32}-{_SPAN}-01",          # an all-zero trace id is invalid
    f"00-{_TRACE}-{'0' * 16}-01",         # so is an all-zero span id
    f"ff-{_TRACE}-{_SPAN}-01",            # version ff is forbidden
    f"00-{_TRACE[:-2]}-{_SPAN}-01",       # short trace id
    f"00-{_TRACE.upper()}-{_SPAN}-01",    # upper-case hex is not W3C
    "",
])
def test_a_malformed_traceparent_starts_a_new_trace_and_is_never_an_error(monkeypatch, memory,
                                                                       traceparent):
    exporter = memory()
    resp = _post(monkeypatch, headers={"traceparent": traceparent})
    assert resp.status_code == 200
    request, _ = _spans(exporter)
    assert request.parent is None
    assert format(request.context.trace_id, "032x") != _TRACE
    assert resp.headers["x-trace-id"] == format(request.context.trace_id, "032x")


def test_without_a_traceparent_the_request_span_is_a_root(monkeypatch, memory):
    exporter = memory()
    resp = _post(monkeypatch)
    request, _ = _spans(exporter)
    assert request.parent is None
    assert resp.headers["x-trace-id"] == format(request.context.trace_id, "032x")


def test_with_tracing_off_the_header_changes_nothing(monkeypatch, memory):
    exporter = memory()
    otel.configure({"tracing": {"enabled": False}})
    plain = _post(monkeypatch)
    traced = _post(monkeypatch, headers={"traceparent": _TRACEPARENT})
    assert exporter.get_finished_spans() == ()
    assert (plain.status_code, plain.json()["choices"]) == (traced.status_code,
                                                            traced.json()["choices"])
    assert "x-trace-id" not in traced.headers


# ── GenAI semantic-convention attributes ───────────────────────────────────────

def _ctx(**kw):
    base = dict(request_id="r1", tenant_id="acme", model="gpt-4o", routed_model="gpt-4o-mini",
                ingress_protocol="responses", otel_span=None, otel_request_span=None,
                otel_request_span_ended=False,
                provider_adapter=SimpleNamespace(name="openai"),
                provider_calls=[{"model": "gpt-4o-mini", "prompt_tokens": 1200,
                                 "completion_tokens": 40},
                                {"model": "gpt-4o-mini", "prompt_tokens": 30,
                                 "completion_tokens": 2}])
    base.update(kw)
    return SimpleNamespace(**base)


def test_the_request_span_carries_the_gen_ai_attributes(memory):
    exporter = memory()
    ctx = _ctx()
    ctx.otel_request_span = otel.start_request_span(ctx, {"traceparent": _TRACEPARENT})
    otel.finish_request_span(ctx, "200", {"model": "gpt-4o-mini-2024-07-18"})
    (span,) = exporter.get_finished_spans()
    attrs = dict(span.attributes)
    assert {k: attrs[k] for k in _GEN_AI} == {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "openai",
        "gen_ai.system": "openai",
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.response.model": "gpt-4o-mini-2024-07-18",
        "gen_ai.usage.input_tokens": 1230,       # every provider call the request paid for
        "gen_ai.usage.output_tokens": 42,
    }
    assert span.name == "chat gpt-4o"
    assert span.kind == SpanKind.SERVER
    assert attrs["http.response.status_code"] == 200
    assert attrs["proxy.ingress_protocol"] == "responses"
    assert attrs["proxy.tenant_id"] == "acme"


def test_a_request_with_no_provider_call_reports_no_usage(memory):
    exporter = memory()
    ctx = _ctx(provider_calls=[])
    ctx.otel_request_span = otel.start_request_span(ctx, {})
    otel.finish_request_span(ctx, "200", None)
    (span,) = exporter.get_finished_spans()
    assert "gen_ai.usage.input_tokens" not in span.attributes
    assert span.attributes["gen_ai.response.model"] == "gpt-4o-mini"   # the routed model


def test_a_server_error_marks_the_span_and_it_ends_once(memory):
    from opentelemetry.trace import StatusCode
    exporter = memory()
    ctx = _ctx()
    ctx.otel_request_span = otel.start_request_span(ctx, {})
    otel.finish_request_span(ctx, "502", None)
    otel.finish_request_span(ctx, "200", None)        # a second exit path: ignored
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["http.response.status_code"] == 502


def test_switched_off_the_request_span_is_a_no_op(memory):
    memory()
    otel.configure({"tracing": {"enabled": False}})
    ctx = _ctx()
    span = otel.start_request_span(ctx, {"traceparent": _TRACEPARENT})
    assert isinstance(span, otel._NoOpSpan)
    ctx.otel_request_span = span
    otel.finish_request_span(ctx, "200", None)       # no error


def test_a_long_model_name_is_capped_on_the_span(memory):
    exporter = memory()
    ctx = _ctx(model="m" * 5000)
    ctx.otel_request_span = otel.start_request_span(ctx, {})
    otel.finish_request_span(ctx, "400", None)
    (span,) = exporter.get_finished_spans()
    assert len(span.attributes["gen_ai.request.model"]) <= 128
    assert len(span.name) <= 133


class _FailingPipeline(_Pipeline):
    async def process_request(self, ctx, request_headers=None):
        ctx.otel_span = otel.start_span("proxy-pipeline", ctx)
        raise RuntimeError("a stage blew up")


def test_a_request_whose_pipeline_raises_still_ends_its_span(monkeypatch, memory):
    exporter = memory()
    monkeypatch.setattr(main, "_authenticate", _fake_auth)
    monkeypatch.setattr(main, "get_config", lambda: _CFG)
    monkeypatch.setattr(main, "_pipeline", _FailingPipeline())
    monkeypatch.setattr(main, "_usage_meter", None)
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = client.post("/v1/chat/completions", json=_BODIES["/v1/chat/completions"],
                       headers={"Authorization": "Bearer tok-x", "traceparent": _TRACEPARENT})
    assert resp.status_code == 500
    request, _ = _spans(exporter)
    assert request.attributes["http.response.status_code"] == 500
    assert format(request.parent.span_id, "016x") == _SPAN


def test_finishing_a_context_without_a_request_span_is_harmless():
    otel.finish_request_span(None, "500", None)
    otel.finish_request_span(SimpleNamespace(), "200", None)


# ── forwarding traceparent upstream (opt-in) ───────────────────────────────────

def _outgoing(make_ctx, ctx_span):
    from providers import get_adapter_by_name, outgoing_params_for
    ctx = make_ctx()
    ctx.otel_request_span = ctx_span
    return outgoing_params_for(ctx, get_adapter_by_name("openai"), "gpt-4o", ctx.config)


def test_traceparent_is_not_forwarded_by_default(memory, make_ctx):
    memory()
    ctx = _ctx()
    span = otel.start_request_span(ctx, {"traceparent": _TRACEPARENT})
    assert "extra_headers" not in _outgoing(make_ctx, span)
    assert otel.outgoing_trace_headers(SimpleNamespace(otel_request_span=span)) == {}


def test_forwarding_on_sends_the_request_spans_context(memory, make_ctx):
    memory(forward_traceparent=True)
    ctx = _ctx()
    span = otel.start_request_span(ctx, {"traceparent": _TRACEPARENT,
                                         "tracestate": "acme=v1"})
    headers = _outgoing(make_ctx, span)["extra_headers"]
    version, trace_id, parent_id, flags = headers["traceparent"].split("-")
    assert (version, trace_id, flags) == ("00", _TRACE, "01")
    assert parent_id == format(span.get_span_context().span_id, "016x") != _SPAN
    assert headers["tracestate"] == "acme=v1"


def test_forwarding_keeps_the_adapters_own_extra_headers(memory, make_ctx):
    from providers import get_adapter_by_name, outgoing_params_for
    memory(forward_traceparent=True)
    ctx = make_ctx(model="claude-sonnet-4-5")
    ctx.otel_request_span = otel.start_request_span(ctx, {"traceparent": _TRACEPARENT})
    adapter = get_adapter_by_name("anthropic")
    real = adapter.apply_context_management

    def with_beta(params, cfg):
        out = dict(real(params, cfg))
        out["extra_headers"] = {"anthropic-beta": "context-management-2025-06-27"}
        return out
    adapter.apply_context_management = with_beta
    try:
        out = outgoing_params_for(ctx, adapter, "claude-sonnet-4-5", ctx.config)
    finally:
        del adapter.apply_context_management
    assert out["extra_headers"]["anthropic-beta"] == "context-management-2025-06-27"
    assert out["extra_headers"]["traceparent"].split("-")[1] == _TRACE


@pytest.mark.parametrize("value", ["true", 1, None])
def test_only_a_real_true_switches_forwarding_on(memory, value):
    memory(forward_traceparent=value)
    assert otel._forward_traceparent is False
