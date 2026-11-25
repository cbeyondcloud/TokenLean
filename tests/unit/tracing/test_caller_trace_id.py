"""The caller's trace id without span export.

A caller that sends a W3C ``traceparent`` names its trace. With spans exported, TokenLean's
request span joins that trace (test_traceparent.py). Without an exporter there is no span,
but the id is still worth keeping: ``caller_trace_id`` reads it from the header, and
``request_trace_id`` (what usage rows and Langfuse traces record) falls back to it. The
``X-Trace-ID`` response header is unchanged: it names an exported trace only.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))
from types import SimpleNamespace

import pytest
from starlette.datastructures import Headers

from tracing import otel

_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
_SPAN = "00f067aa0ba902b7"
_TRACEPARENT = f"00-{_TRACE}-{_SPAN}-01"


@pytest.fixture
def export_off():
    saved = (otel._otel_available, otel._tracer, otel._provider)
    otel._otel_available, otel._tracer, otel._provider = False, None, None
    yield
    otel._otel_available, otel._tracer, otel._provider = saved


@pytest.fixture
def export_on():
    pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    saved = (otel._otel_available, otel._tracer, otel._provider)
    assert otel.configure({"tracing": {"enabled": True}}, exporter=InMemorySpanExporter()) is True
    yield
    otel._otel_available, otel._tracer, otel._provider = saved


class TestCallerTraceId:
    def test_the_id_is_kept_with_export_off(self, export_off):
        assert otel.caller_trace_id({"traceparent": _TRACEPARENT}) == _TRACE

    def test_the_header_name_matches_in_any_case(self, export_off):
        assert otel.caller_trace_id({"TraceParent": _TRACEPARENT}) == _TRACE
        assert otel.caller_trace_id(Headers({"traceparent": _TRACEPARENT})) == _TRACE

    def test_surrounding_whitespace_is_ignored(self, export_off):
        assert otel.caller_trace_id({"traceparent": f"  {_TRACEPARENT} "}) == _TRACE

    def test_tracestate_does_not_matter(self, export_off):
        headers = {"traceparent": _TRACEPARENT, "tracestate": "vendor=1"}
        assert otel.caller_trace_id(headers) == _TRACE

    @pytest.mark.parametrize("value", [
        "",
        "garbage",
        _TRACE,                                              # the id alone
        f"00-{_TRACE}-{_SPAN}",                              # no flags
        f"00-{_TRACE}-{_SPAN}-01-extra",                     # version 00 has four fields
        f"ff-{_TRACE}-{_SPAN}-01",                           # the forbidden version
        f"00-{_TRACE[:-1]}-{_SPAN}-01",                      # short trace id
        f"00-{_TRACE}-{_SPAN[:-1]}-01",                      # short span id
        f"00-{_TRACE.upper()}-{_SPAN}-01",                   # upper case is not W3C
        f"00-{'g' * 32}-{_SPAN}-01",                         # not hex
        f"00-{'0' * 32}-{_SPAN}-01",                         # all-zero trace id
        f"00-{_TRACE}-{'0' * 16}-01",                        # all-zero span id
        f"00-{_TRACE}-{_SPAN}-zz",                           # flags not hex
        f"00-{_TRACE}-{_SPAN}-01" + " " * 500,               # longer than 512 characters
        f"01-{_TRACE}-{_SPAN}-01-" + "x" * 500,              # a later version, but too long
    ])
    def test_a_malformed_header_gives_none(self, export_off, value):
        assert otel.caller_trace_id({"traceparent": value}) == ""

    @pytest.mark.parametrize("value", [
        f"01-{_TRACE}-{_SPAN}-01",                           # a later version: read as 00 is
        f"01-{_TRACE}-{_SPAN}-01-what-a-later-version-adds",
        f"fe-{_TRACE}-{_SPAN}-00",
        f"\t {_TRACEPARENT}\t ",
    ])
    def test_a_later_version_is_read_as_the_propagator_reads_it(self, export_off, value):
        # W3C: a parser reads the fields it knows from a higher version. The id recorded
        # with export off must be the one an exported span would carry.
        assert otel.caller_trace_id({"traceparent": value}) == _TRACE

    @pytest.mark.parametrize("value", [
        _TRACEPARENT,                                        # v00 valid
        f"00-{_TRACE}-{_SPAN}-00",                           # not sampled is still a trace
        f"01-{_TRACE}-{_SPAN}-01",                           # v01 valid
        f"01-{_TRACE}-{_SPAN}-01-extra",                     # v01 with an extra field
        f"01-{_TRACE}-{_SPAN}-01extra",                      # v01, not a field
        f"ff-{_TRACE}-{_SPAN}-01",                           # vff
        f"00-{_TRACE}-{_SPAN}-01-extra",                     # v00 with an extra field
        f"00-{_TRACE}-{_SPAN}-01-",                          # v00 with an empty extra field
        f"00-{'0' * 32}-{_SPAN}-01",                         # all-zero trace id
        f"00-{_TRACE}-{'0' * 16}-01",                        # all-zero span id
        f"00-{_TRACE.upper()}-{_SPAN}-01",                   # upper case
        f"00-{_TRACE}-{_SPAN.upper()}-01",
        f"00-{_TRACE}-{_SPAN}-01" + " " * 500,               # over-long
        f"01-{_TRACE}-{_SPAN}-01-" + "x" * 456,              # exactly 512: allowed
        f"01-{_TRACE}-{_SPAN}-01-" + "x" * 457,              # 513: over-long
        f"  {_TRACEPARENT}  ",                               # surrounding spaces
        f"\t{_TRACEPARENT}\t",                               # surrounding tabs
        f" \t {_TRACEPARENT}",
        f"00-{_TRACE}-{_SPAN}-01 x",                         # trailing text
        f"x{_TRACEPARENT}",
        f"00-{_TRACE}-{_SPAN}",
        "",
        "garbage",
    ])
    def test_it_agrees_with_the_opentelemetry_propagator(self, export_off, value):
        """Export on and export off must record the same id: whatever the installed W3C
        propagator accepts gives its trace id here, and whatever it rejects gives ""."""
        tracecontext = pytest.importorskip("opentelemetry.trace.propagation.tracecontext")
        from opentelemetry import trace as otel_trace
        carrier = {"traceparent": value}
        context = tracecontext.TraceContextTextMapPropagator().extract(carrier)
        span_context = otel_trace.get_current_span(context).get_span_context()
        expected = format(span_context.trace_id, "032x") if span_context.is_valid else ""
        assert otel.caller_trace_id(carrier) == expected

    def test_the_length_limit_is_512_characters(self, export_off):
        at_limit = f"01-{_TRACE}-{_SPAN}-01-" + "x" * 456
        assert len(at_limit) == 512
        assert otel.caller_trace_id({"traceparent": at_limit}) == _TRACE
        assert otel.caller_trace_id({"traceparent": at_limit + "x"}) == ""

    def test_a_repeated_header_gives_none(self, export_off):
        other = f"00-{'a' * 32}-{_SPAN}-01"
        headers = Headers(raw=[(b"traceparent", _TRACEPARENT.encode()),
                               (b"traceparent", other.encode())])
        assert otel.caller_trace_id(headers) == ""

    @pytest.mark.parametrize("headers", [None, {}, "traceparent", 7, {"x-other": "1"},
                                         {"traceparent": 7}])
    def test_no_usable_headers_give_none(self, export_off, headers):
        assert otel.caller_trace_id(headers) == ""

    def test_export_on_reads_the_same_id(self, export_on):
        assert otel.caller_trace_id({"traceparent": _TRACEPARENT}) == _TRACE


class TestRequestTraceId:
    def test_with_export_off_it_is_the_callers(self, export_off):
        ctx = SimpleNamespace(otel_span=None, otel_request_span=otel._NoOpSpan(),
                              caller_trace_id=_TRACE)
        assert otel.request_trace_id(ctx) == _TRACE

    def test_nothing_known_gives_none(self, export_off):
        assert otel.request_trace_id(SimpleNamespace()) == ""
        assert otel.request_trace_id(None) == ""
        ctx = SimpleNamespace(otel_span=None, otel_request_span=None, caller_trace_id=None)
        assert otel.request_trace_id(ctx) == ""

    def test_an_exported_span_wins(self, export_on):
        ctx = SimpleNamespace(request_id="r1", tenant_id="acme", model="m", otel_span=None,
                              caller_trace_id="a" * 32)
        ctx.otel_request_span = otel.start_request_span(ctx, {})     # a new root trace
        exported = otel.get_trace_id(ctx.otel_request_span)
        assert len(exported) == 32 and exported != "a" * 32
        assert otel.request_trace_id(ctx) == exported
        ctx.otel_span = otel.start_span("proxy-pipeline", ctx)
        assert otel.request_trace_id(ctx) == otel.get_trace_id(ctx.otel_span) == exported
        otel.end_span(ctx.otel_span)
        otel.end_span(ctx.otel_request_span)

    def test_with_export_on_the_callers_trace_is_the_exported_one(self, export_on):
        headers = {"traceparent": _TRACEPARENT}
        ctx = SimpleNamespace(request_id="r1", tenant_id="acme", model="m", otel_span=None,
                              caller_trace_id=otel.caller_trace_id(headers))
        ctx.otel_request_span = otel.start_request_span(ctx, headers)
        assert otel.request_trace_id(ctx) == _TRACE == ctx.caller_trace_id
        otel.end_span(ctx.otel_request_span)


# ── The request handler records it, and X-Trace-ID stays an exported trace's ──────────

class _Pipeline:
    """Stands in for OptimisationPipeline: keeps the context and answers from the cache."""

    def __init__(self):
        from middleware.g32_tool_eligibility import G32ToolEligibility
        self.ctx = None
        self.g32 = G32ToolEligibility()

    async def process_request(self, ctx, request_headers=None):
        self.ctx = ctx
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
        return ctx

    async def process_response(self, ctx, response):
        return ctx, response


async def _fake_auth(request, *_protocol):
    return "acme", "tok-x", {"tenant_id": "acme", "tier": "enterprise"}


def _post(monkeypatch, pipeline, headers, path="/v1/chat/completions", **body):
    from fastapi.testclient import TestClient
    import main
    monkeypatch.setattr(main, "_authenticate", _fake_auth)
    monkeypatch.setattr(main, "get_config", lambda: {"groups": {}, "providers": []})
    monkeypatch.setattr(main, "_pipeline", pipeline)
    monkeypatch.setattr(main, "_usage_meter", None)
    payload = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}], **body}
    return TestClient(main.app).post(path, json=payload,
                                     headers=[("Authorization", "Bearer tok-x"), *headers])


@pytest.mark.parametrize("path,body", [("/v1/chat/completions", {}),
                                       ("/v1/messages", {"max_tokens": 16})],
                         ids=["openai", "anthropic"])
def test_the_handler_keeps_the_callers_id_with_export_off(monkeypatch, export_off, path, body):
    pipeline = _Pipeline()
    resp = _post(monkeypatch, pipeline, [("traceparent", _TRACEPARENT)], path=path, **body)
    assert resp.status_code == 200, resp.text
    assert pipeline.ctx.caller_trace_id == _TRACE
    assert otel.request_trace_id(pipeline.ctx) == _TRACE
    # No exported trace, so no X-Trace-ID: the header names a trace a backend holds.
    assert "x-trace-id" not in resp.headers


def test_the_handler_records_none_without_a_traceparent(monkeypatch, export_off):
    pipeline = _Pipeline()
    resp = _post(monkeypatch, pipeline, [])
    assert resp.status_code == 200, resp.text
    assert pipeline.ctx.caller_trace_id == ""
    assert otel.request_trace_id(pipeline.ctx) == ""


def test_the_handler_drops_a_repeated_traceparent(monkeypatch, export_off):
    pipeline = _Pipeline()
    other = f"00-{'a' * 32}-{_SPAN}-01"
    resp = _post(monkeypatch, pipeline, [("traceparent", _TRACEPARENT), ("traceparent", other)])
    assert resp.status_code == 200, resp.text
    assert pipeline.ctx.caller_trace_id == ""
