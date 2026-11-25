"""Regression tests for nested Langfuse spans across middleware."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import copy
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def mock_trace_ctx(make_ctx):
    """Return a RequestContext with a mocked langfuse_trace already attached."""
    ctx = make_ctx()
    mock_trace = MagicMock()
    ctx.langfuse_trace = mock_trace
    return ctx, mock_trace


def _span_kwargs(mock_trace):
    """Extract kwargs dict from the first .span() call on a mock trace."""
    # method_calls entries are call objects: (name, args, kwargs)
    span_calls = [c for c in mock_trace.method_calls if c[0] == "span"]
    assert span_calls, "No span() call found on mock_trace"
    return span_calls[0][2]  # index 2 = kwargs dict


@pytest.mark.asyncio
class TestG01CompressionSpan:
    async def test_span_emitted_when_compression_occurs(self, make_ctx):
        # Provide a long system message (>100 chars) to trigger G01 compression
        long_system = "You are a helpful AI assistant. " * 10  # ~320 chars
        ctx = make_ctx(messages=[
            {"role": "system", "content": long_system},
            {"role": "user", "content": "Hello"},
        ])
        mock_trace = MagicMock()
        ctx.langfuse_trace = mock_trace
        # System-prompt compression is opt-in (off by default for safety); enable it
        # so the system message is actually compressed and the span is emitted.
        ctx.config["groups"]["G1_compression"]["compress_system_prompt"] = True

        from middleware.g01_compression import G01Compression

        # Return something shorter to trigger the "len(compressed) < len(content)" guard.
        # _call_llmlingua is awaited, so the patch must be async.
        compressed_system = "You are a helpful AI assistant."
        with patch("middleware.g01_compression._call_llmlingua",
                   new=AsyncMock(return_value=compressed_system)):
            await G01Compression().process_request(ctx)

        kwargs = _span_kwargs(mock_trace)
        assert kwargs["name"] == "G01-compression"
        assert "tokens_before" in kwargs["input"]
        assert "tokens_after" in kwargs["output"]

    async def test_no_span_when_disabled(self, make_ctx):
        ctx = make_ctx()
        mock_trace = MagicMock()
        ctx.langfuse_trace = mock_trace
        ctx.config["groups"]["G1_compression"]["enabled"] = False

        from middleware.g01_compression import G01Compression
        await G01Compression().process_request(ctx)
        mock_trace.span.assert_not_called()


@pytest.mark.asyncio
class TestG05CacheSpan:
    async def test_span_emitted_on_l1_hit(self, mock_trace_ctx):
        ctx, mock_trace = mock_trace_ctx
        from middleware.g05_cache import G05Cache

        with patch("middleware.g05_cache._get_redis") as mock_get_redis:
            mock_redis = MagicMock()
            mock_redis.get = AsyncMock(return_value='{"choices": [], "usage": {}}')
            mock_get_redis.return_value = mock_redis
            await G05Cache().process_request(ctx)

        kwargs = _span_kwargs(mock_trace)
        assert kwargs["name"] == "G05-cache"
        assert kwargs["metadata"]["level"] == "L1"


@pytest.mark.asyncio
class TestG06RoutingSpan:
    async def test_span_emitted_with_routing_details(self, mock_trace_ctx):
        ctx, mock_trace = mock_trace_ctx
        ctx.config["groups"]["G6_routing"]["tiers"] = {
            "simple": ["gpt-4o-mini"],
            "medium": ["gpt-4o"],
            "complex": ["gpt-4o"],
        }
        from middleware.g06_routing import G06Routing
        await G06Routing().process_request(ctx)

        kwargs = _span_kwargs(mock_trace)
        assert kwargs["name"] == "G06-routing"
        assert "routing_mode" in kwargs["metadata"]


@pytest.mark.asyncio
class TestG07RetrievalSpan:
    async def test_span_emitted_when_chunks_found(self, mock_trace_ctx):
        ctx, mock_trace = mock_trace_ctx
        ctx.params["rag_query"] = "test query"  # required to bypass early return
        from middleware.g07_retrieval import G07Retrieval

        with patch("middleware.g07_retrieval._hybrid_search", return_value=[{"text": "chunk1", "score": 0.9}]):
            with patch("middleware.g07_retrieval._rerank", return_value=[{"text": "chunk1", "score": 0.95}]):
                await G07Retrieval().process_request(ctx)

        kwargs = _span_kwargs(mock_trace)
        assert kwargs["name"] == "G07-retrieval"
        assert kwargs["output"]["chunks_retrieved"] == 1


@pytest.mark.asyncio
class TestG10MemorySpan:
    async def test_span_emitted_on_sliding_window(self, mock_trace_ctx):
        ctx, mock_trace = mock_trace_ctx
        # window=2, so need >4 non-system turns to trigger sliding window
        ctx.messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "Q2"},
            {"role": "assistant", "content": "A2"},
            {"role": "user", "content": "Q3"},
        ]
        from middleware.g10_memory import G10Memory

        with patch("middleware.g10_memory._summarise", return_value="summary"):
            await G10Memory().process_request(ctx)

        span_calls = [c for c in mock_trace.method_calls if c[0] == "span"]
        assert len(span_calls) >= 1
        names = {c[2]["name"] for c in span_calls}  # kwargs are at index 2
        assert "G10-memory" in names


@pytest.mark.asyncio
class TestG11OutputFormatSpan:
    async def test_span_emitted_when_max_tokens_set(self, mock_trace_ctx):
        ctx, mock_trace = mock_trace_ctx
        # G11 no longer invents a cap without completion-size evidence; give it the
        # configured static fallback so the max_tokens change (and its span) fires.
        ctx.config["groups"]["G11_output"]["fallback_max_tokens"] = 512
        from middleware.g11_output_format import G11OutputFormat
        await G11OutputFormat().process_request(ctx)

        span_calls = [c for c in mock_trace.method_calls if c[0] == "span"]
        # G11 emits a span whenever it changes params
        assert len(span_calls) == 1
        kwargs = span_calls[0][2]  # kwargs at index 2
        assert kwargs["name"] == "G11-output-format"
        assert "notes" in kwargs["metadata"]


@pytest.mark.asyncio
class TestG12ReasoningBudgetSpan:
    async def test_span_emitted_for_o1_model(self, mock_trace_ctx):
        ctx, mock_trace = mock_trace_ctx
        ctx.routed_model = "o1-preview"
        ctx.model = "o1-preview"
        from middleware.g12_reasoning_budget import G12ReasoningBudget
        await G12ReasoningBudget().process_request(ctx)

        kwargs = _span_kwargs(mock_trace)
        assert kwargs["name"] == "G12-reasoning-budget"
        assert kwargs["metadata"]["provider"] in ("openai", "anthropic", "gemini")


@pytest.mark.asyncio
class TestStartTraceIdentityMetadata:
    """Grafana's User/Tenant dropdowns query metadata->>'user_id' and rely on
    the tenant_id label — both must be present on the trace from creation."""

    async def test_start_trace_includes_user_id_in_metadata(self, make_ctx):
        ctx = make_ctx()
        ctx.user_id = "acme-pitch"
        ctx.config["groups"]["G18_observability"] = {"enabled": True, "langfuse_enabled": True}

        mock_client = MagicMock()
        mock_trace = MagicMock()
        mock_client.trace.return_value = mock_trace

        from middleware import langfuse_tracing
        with patch.object(langfuse_tracing, "get_client", return_value=mock_client):
            langfuse_tracing.start_trace(ctx)

        _, kwargs = mock_client.trace.call_args
        assert kwargs["metadata"]["user_id"] == "acme-pitch"

    async def test_start_trace_includes_tenant_id_in_metadata(self, make_ctx):
        ctx = make_ctx()
        ctx.tenant_id = "nova-med"
        ctx.config["groups"]["G18_observability"] = {"enabled": True, "langfuse_enabled": True}

        mock_client = MagicMock()
        mock_trace = MagicMock()
        mock_client.trace.return_value = mock_trace

        from middleware import langfuse_tracing
        with patch.object(langfuse_tracing, "get_client", return_value=mock_client):
            langfuse_tracing.start_trace(ctx)

        _, kwargs = mock_client.trace.call_args
        assert kwargs["metadata"]["tenant_id"] == "nova-med"

    async def test_finish_trace_calls_trace_update_with_tenant(self, mock_trace_ctx):
        ctx, mock_trace = mock_trace_ctx
        ctx.tenant_id = "nova-med"
        ctx.user_id = "nova-med-pitch"

        from middleware import langfuse_tracing
        with patch.object(langfuse_tracing, "get_client", return_value=None):
            langfuse_tracing.finish_trace(ctx, response=None)

        update_calls = [c for c in mock_trace.method_calls if c[0] == "update"]
        assert update_calls, "trace.update() was not called"
        _, _, kwargs = update_calls[0]
        assert kwargs["metadata"]["tenant_id"] == "nova-med"
        assert kwargs["metadata"]["user_id"] == "nova-med-pitch"


_RUN_TAGS = {"run_id": "run-42", "step": "review", "agent": "code-review",
             "pipeline": "feature-delivery", "team": "payments"}
_CALLER_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
# What a trace carried before usage tags reached it: dashboards read these keys.
_BASE_START_KEYS = {"model_requested", "baseline_tokens", "user_id", "tenant_id"}


@pytest.mark.asyncio
class TestUsageTagsOnTheTrace:
    """A request's usage tags (billing.tags: the allow-listed headers a caller sends to say
    which run, step or agent made the call) and its trace id reach the Langfuse trace, so a
    trace can be found by run and joined to usage_events and to the caller's own trace.
    `langfuse_usage_tags` (default on) adds them to the metadata; `langfuse_tag_keys` and
    `langfuse_session_tag` (default none) choose what becomes a Langfuse tag and session."""

    def _ctx(self, make_ctx, tags=_RUN_TAGS, params=None, **g18):
        ctx = make_ctx(params=params)
        ctx.config = copy.deepcopy(ctx.config)
        ctx.config["groups"]["G18_observability"] = {
            "enabled": True, "langfuse_enabled": True, **g18}
        ctx.usage_tags = dict(tags) if isinstance(tags, dict) else tags
        # The operator's global config (config_loader.get_config): what the request's config
        # was before any tenant or team override was merged into ctx.config.
        ctx.operator_config = copy.deepcopy(ctx.config)
        return ctx

    def _start(self, ctx):
        from middleware import langfuse_tracing
        client = MagicMock()
        client.trace.return_value = MagicMock()
        with patch.object(langfuse_tracing, "get_client", return_value=client), \
                patch.object(langfuse_tracing, "get_config", return_value=ctx.operator_config):
            langfuse_tracing.start_trace(ctx)
        client.trace.assert_called_once()
        return client.trace.call_args.kwargs

    def _finish(self, ctx):
        from middleware import langfuse_tracing
        trace = MagicMock()
        ctx.langfuse_trace = trace
        with patch.object(langfuse_tracing, "get_config", return_value=ctx.operator_config):
            langfuse_tracing.finish_trace(ctx, response=None)
        trace.update.assert_called_once()
        return trace.update.call_args.kwargs["metadata"]

    @staticmethod
    def _tenant_overrides(ctx, tenant_id, *, g18=None, billing=None):
        """Merge a tenant's own (portal-written) overrides into the request's config, as the
        pipeline does: over the top-level keys and under ``tenants.<id>`` alike."""
        ctx.tenant_id = tenant_id
        if g18:
            ctx.config["groups"]["G18_observability"].update(g18)
            ctx.config.setdefault("tenants", {})[tenant_id] = {
                "groups": {"G18_observability": dict(g18)}}
        if billing:
            ctx.config["billing"] = copy.deepcopy(billing)

    # ── metadata ──────────────────────────────────────────────────────────────────
    async def test_the_tags_are_in_the_metadata_by_default(self, make_ctx):
        ctx = self._ctx(make_ctx)
        kwargs = self._start(ctx)
        assert kwargs["metadata"]["usage_tags"] == _RUN_TAGS
        assert kwargs["metadata"]["usage_tags"] is not ctx.usage_tags      # a copy

    async def test_the_callers_trace_id_is_in_the_metadata(self, make_ctx):
        ctx = self._ctx(make_ctx)
        ctx.caller_trace_id = _CALLER_TRACE        # no exported span: the traceparent's id
        assert self._start(ctx)["metadata"]["otel_trace_id"] == _CALLER_TRACE

    async def test_the_trace_id_is_recorded_without_any_tag(self, make_ctx):
        ctx = self._ctx(make_ctx, tags=None)
        ctx.caller_trace_id = _CALLER_TRACE
        metadata = self._start(ctx)["metadata"]
        assert metadata["otel_trace_id"] == _CALLER_TRACE
        assert "usage_tags" not in metadata

    async def test_no_trace_id_leaves_the_key_out(self, make_ctx):
        assert "otel_trace_id" not in self._start(self._ctx(make_ctx))["metadata"]

    async def test_the_existing_keys_are_unchanged(self, make_ctx):
        ctx = self._ctx(make_ctx, langfuse_tag_keys=["run_id"], langfuse_session_tag="run_id")
        ctx.user_id, ctx.tenant_id = "acme-user", "acme"
        ctx.caller_trace_id = _CALLER_TRACE
        kwargs = self._start(ctx)
        assert set(kwargs["metadata"]) == _BASE_START_KEYS | {"usage_tags", "otel_trace_id"}
        assert kwargs["metadata"]["model_requested"] == ctx.model
        assert kwargs["metadata"]["baseline_tokens"] == ctx.savings.baseline_tokens
        assert kwargs["metadata"]["user_id"] == kwargs["user_id"] == "acme-user"
        assert kwargs["metadata"]["tenant_id"] == "acme"
        assert kwargs["id"] == ctx.request_id and kwargs["name"] == "llm-proxy-call"

    @pytest.mark.parametrize("tags", [None, {}, "run-42", ["run_id"]])
    async def test_a_request_without_tags_is_traced_as_before(self, make_ctx, tags):
        ctx = self._ctx(make_ctx, tags=tags, langfuse_tag_keys=["run_id", "step"],
                        langfuse_session_tag="run_id")
        kwargs = self._start(ctx)
        assert set(kwargs["metadata"]) == _BASE_START_KEYS
        assert kwargs["tags"] == ["token-optimisation"]
        assert kwargs["session_id"] is None

    async def test_switched_off_nothing_is_added(self, make_ctx):
        ctx = self._ctx(make_ctx, langfuse_usage_tags=False,
                        langfuse_tag_keys=["run_id", "step"], langfuse_session_tag="run_id")
        ctx.caller_trace_id = _CALLER_TRACE
        kwargs = self._start(ctx)
        assert set(kwargs["metadata"]) == _BASE_START_KEYS
        assert kwargs["tags"] == ["token-optimisation"]
        assert kwargs["session_id"] is None
        assert "usage_tags" not in self._finish(ctx)
        assert "otel_trace_id" not in self._finish(ctx)

    async def test_a_tenant_can_switch_it_off_for_its_own_traces(self, make_ctx):
        ctx = self._ctx(make_ctx, langfuse_tag_keys=["run_id"])
        ctx.tenant_id = "acme"
        ctx.config["tenants"] = {"acme": {"groups": {"G18_observability": {
            "langfuse_usage_tags": False}}}}
        kwargs = self._start(ctx)
        assert "usage_tags" not in kwargs["metadata"]
        assert kwargs["tags"] == ["token-optimisation"]
        ctx.tenant_id = "other"
        assert self._start(ctx)["metadata"]["usage_tags"] == _RUN_TAGS

    # ── Langfuse tags ─────────────────────────────────────────────────────────────
    async def test_no_tag_keys_configured_means_the_one_tag(self, make_ctx):
        assert self._start(self._ctx(make_ctx))["tags"] == ["token-optimisation"]

    async def test_the_configured_keys_become_tags_in_their_order(self, make_ctx):
        ctx = self._ctx(make_ctx, langfuse_tag_keys=["run_id", "step", "agent", "pipeline"])
        assert self._start(ctx)["tags"] == [
            "token-optimisation", "run_id:run-42", "step:review", "agent:code-review",
            "pipeline:feature-delivery"]

    async def test_a_key_the_request_did_not_send_is_skipped(self, make_ctx):
        ctx = self._ctx(make_ctx, tags={"step": "review"},
                        langfuse_tag_keys=["run_id", "step", "step", 7, None])
        assert self._start(ctx)["tags"] == ["token-optimisation", "step:review"]

    @pytest.mark.parametrize("keys", ["run_id", {"run_id": True}, 7, None])
    async def test_an_unreadable_key_list_adds_no_tag(self, make_ctx, keys):
        ctx = self._ctx(make_ctx, langfuse_tag_keys=keys)
        kwargs = self._start(ctx)
        assert kwargs["tags"] == ["token-optimisation"]
        assert kwargs["metadata"]["usage_tags"] == _RUN_TAGS

    async def test_a_long_value_is_cut_to_a_tags_length(self, make_ctx):
        from middleware import langfuse_tracing
        ctx = self._ctx(make_ctx, tags={"run_id": "r" * 600}, langfuse_tag_keys=["run_id"])
        tag = self._start(ctx)["tags"][1]
        assert tag.startswith("run_id:rrr")
        assert len(tag) == langfuse_tracing._MAX_TAG_CHARS

    # ── session ───────────────────────────────────────────────────────────────────
    async def test_the_session_falls_back_to_the_named_tag_under_its_tenant(self, make_ctx):
        # All tenants share one Langfuse project: a caller-chosen run id alone would let one
        # tenant put its traces into another tenant's session (as G10 scopes its session).
        ctx = self._ctx(make_ctx, langfuse_session_tag="run_id")
        ctx.tenant_id = "acme"
        assert self._start(ctx)["session_id"] == "acme::run-42"

    async def test_two_tenants_with_one_run_id_get_two_sessions(self, make_ctx):
        first = self._ctx(make_ctx, langfuse_session_tag="run_id")
        second = self._ctx(make_ctx, langfuse_session_tag="run_id")
        first.tenant_id, second.tenant_id = "acme", "globex"
        sessions = {self._start(first)["session_id"], self._start(second)["session_id"]}
        assert sessions == {"acme::run-42", "globex::run-42"}

    async def test_unscoped_the_session_is_the_tag_value_itself(self, make_ctx):
        ctx = self._ctx(make_ctx, langfuse_session_tag="run_id",
                        langfuse_session_tenant_scope=False)
        ctx.tenant_id = "acme"
        assert self._start(ctx)["session_id"] == "run-42"

    async def test_a_tenant_cannot_switch_the_scope_off(self, make_ctx):
        # Operator-only: off, a tenant could write into another tenant's session.
        ctx = self._ctx(make_ctx, langfuse_session_tag="run_id")
        self._tenant_overrides(ctx, "acme", g18={"langfuse_session_tenant_scope": False})
        assert self._start(ctx)["session_id"] == "acme::run-42"

    async def test_a_tenant_cannot_switch_the_scope_on_either(self, make_ctx):
        # The operator's value decides in both directions (a single-organisation gateway).
        ctx = self._ctx(make_ctx, langfuse_session_tag="run_id",
                        langfuse_session_tenant_scope=False)
        self._tenant_overrides(ctx, "acme", g18={"langfuse_session_tenant_scope": True})
        assert self._start(ctx)["session_id"] == "run-42"

    async def test_a_tenants_own_session_tag_is_still_scoped_to_it(self, make_ctx):
        # langfuse_session_tag stays the tenant's to choose: whichever tag it names, the
        # session id carries its tenant id, so it cannot name another tenant's session.
        ctx = self._ctx(make_ctx, langfuse_session_tag="run_id")
        self._tenant_overrides(ctx, "acme", g18={"langfuse_session_tag": "pipeline"})
        assert self._start(ctx)["session_id"] == "acme::feature-delivery"

    @pytest.mark.parametrize("operator_config", [None, {}, {"groups": None},
                                                 {"groups": {"G18_observability": "x"}}])
    async def test_an_unreadable_operator_config_means_scoped(self, make_ctx, operator_config):
        ctx = self._ctx(make_ctx, langfuse_session_tag="run_id",
                        langfuse_session_tenant_scope=False)
        ctx.tenant_id = "acme"
        ctx.operator_config = operator_config
        assert self._start(ctx)["session_id"] == "acme::run-42"

    async def test_no_tenant_means_no_prefix(self, make_ctx):
        ctx = self._ctx(make_ctx, langfuse_session_tag="run_id")
        ctx.tenant_id = ""
        assert self._start(ctx)["session_id"] == "run-42"

    async def test_the_session_length_cut_includes_the_tenant_prefix(self, make_ctx):
        from middleware import langfuse_tracing
        ctx = self._ctx(make_ctx, tags={"run_id": "r" * 600}, langfuse_session_tag="run_id")
        ctx.tenant_id = "acme"
        session_id = self._start(ctx)["session_id"]
        assert session_id.startswith("acme::rrr")
        assert len(session_id) == langfuse_tracing._MAX_TAG_CHARS

    async def test_a_named_workflow_or_session_is_never_prefixed(self, make_ctx):
        ctx = self._ctx(make_ctx, params={"x_session_id": "wf-1"},
                        langfuse_session_tag="run_id")
        ctx.tenant_id = "acme"
        assert self._start(ctx)["session_id"] == "wf-1"       # as before this setting

    # ── tenant-only tag keys ──────────────────────────────────────────────────────
    async def test_a_tenant_only_key_is_left_out_of_the_trace(self, make_ctx):
        # billing.metering.tags.tenant_only_keys (default: user) are not for every reader of
        # the usage export; the shared Langfuse project gets none of them.
        tags = {**_RUN_TAGS, "user": "dev@example.test"}
        ctx = self._ctx(make_ctx, tags=tags, langfuse_tag_keys=["user", "run_id"],
                        langfuse_session_tag="user")
        kwargs = self._start(ctx)
        assert kwargs["metadata"]["usage_tags"] == _RUN_TAGS
        assert kwargs["tags"] == ["token-optimisation", "run_id:run-42"]
        assert kwargs["session_id"] is None
        assert self._finish(ctx)["usage_tags"] == _RUN_TAGS
        assert ctx.usage_tags == tags                      # the usage row keeps it

    async def test_the_configured_tenant_only_keys_decide(self, make_ctx):
        tags = {**_RUN_TAGS, "user": "dev@example.test"}
        ctx = self._ctx(make_ctx, tags=tags, langfuse_tag_keys=["user", "team"],
                        langfuse_session_tag="team", langfuse_session_tenant_scope=False)
        ctx.operator_config["billing"] = {"metering": {"tags": {"tenant_only_keys": ["team"]}}}
        kwargs = self._start(ctx)
        assert "team" not in kwargs["metadata"]["usage_tags"]
        assert kwargs["metadata"]["usage_tags"]["user"] == "dev@example.test"
        assert kwargs["tags"] == ["token-optimisation", "user:dev@example.test"]
        assert kwargs["session_id"] is None

    async def test_a_tenant_cannot_un_hide_a_tenant_only_key(self, make_ctx):
        # Operator-only, as the usage export reads it: a tenant's own
        # billing.metering.tags.tenant_only_keys changes nothing.
        tags = {**_RUN_TAGS, "user": "dev@example.test"}
        ctx = self._ctx(make_ctx, tags=tags, langfuse_tag_keys=["user", "run_id"],
                        langfuse_session_tag="user")
        self._tenant_overrides(
            ctx, "acme", billing={"metering": {"tags": {"tenant_only_keys": []}}})
        kwargs = self._start(ctx)
        assert kwargs["metadata"]["usage_tags"] == _RUN_TAGS
        assert kwargs["tags"] == ["token-optimisation", "run_id:run-42"]
        assert kwargs["session_id"] is None
        assert self._finish(ctx)["usage_tags"] == _RUN_TAGS

    async def test_a_tenant_cannot_hide_more_keys_either(self, make_ctx):
        ctx = self._ctx(make_ctx, langfuse_tag_keys=["run_id"])
        self._tenant_overrides(
            ctx, "acme", billing={"metering": {"tags": {"tenant_only_keys": ["run_id"]}}})
        kwargs = self._start(ctx)
        assert kwargs["metadata"]["usage_tags"] == _RUN_TAGS
        assert kwargs["tags"] == ["token-optimisation", "run_id:run-42"]

    async def test_an_unreadable_operator_config_hides_the_default_keys(self, make_ctx):
        tags = {**_RUN_TAGS, "user": "dev@example.test"}
        ctx = self._ctx(make_ctx, tags=tags)
        ctx.operator_config = None
        assert self._start(ctx)["metadata"]["usage_tags"] == _RUN_TAGS

    async def test_only_tenant_only_tags_means_no_usage_tags_key(self, make_ctx):
        ctx = self._ctx(make_ctx, tags={"user": "dev@example.test"},
                        langfuse_tag_keys=["user"], langfuse_session_tag="user")
        kwargs = self._start(ctx)
        assert set(kwargs["metadata"]) == _BASE_START_KEYS
        assert kwargs["tags"] == ["token-optimisation"]
        assert kwargs["session_id"] is None

    async def test_no_session_tag_configured_means_no_fallback(self, make_ctx):
        assert self._start(self._ctx(make_ctx))["session_id"] is None
        assert self._start(self._ctx(make_ctx, langfuse_session_tag=""))["session_id"] is None
        assert self._start(self._ctx(make_ctx, langfuse_session_tag=7))["session_id"] is None

    async def test_a_session_tag_the_request_did_not_send_gives_none(self, make_ctx):
        ctx = self._ctx(make_ctx, tags={"step": "review"}, langfuse_session_tag="run_id")
        assert self._start(ctx)["session_id"] is None

    @pytest.mark.parametrize("param", ["workflow_id", "x_workflow_id", "session_id",
                                       "x_session_id"])
    async def test_a_workflow_or_session_the_request_names_wins(self, make_ctx, param):
        ctx = self._ctx(make_ctx, params={param: "wf-1"}, langfuse_session_tag="run_id")
        assert self._start(ctx)["session_id"] == "wf-1"

    # ── finish_trace ──────────────────────────────────────────────────────────────
    async def test_finish_trace_keeps_them(self, make_ctx):
        ctx = self._ctx(make_ctx)
        ctx.tenant_id, ctx.user_id = "acme", "acme-user"
        ctx.caller_trace_id = _CALLER_TRACE
        metadata = self._finish(ctx)
        assert metadata["usage_tags"] == _RUN_TAGS
        assert metadata["otel_trace_id"] == _CALLER_TRACE
        assert metadata["tenant_id"] == "acme" and metadata["user_id"] == "acme-user"

    async def test_finish_trace_without_tags_is_as_before(self, make_ctx):
        ctx = self._ctx(make_ctx, tags=None)
        ctx.tenant_id, ctx.user_id = "acme", "acme-user"
        expected = {**ctx.savings.to_langfuse_metadata(),
                    "user_id": "acme-user", "tenant_id": "acme"}
        assert self._finish(ctx) == expected


class TestUsageTagSettingsShipped:
    """The shipped template states the three settings with the defaults the code assumes:
    tags and trace id in the metadata, no extra Langfuse tag, no session fallback."""

    def test_the_template_ships_the_code_defaults(self):
        import yaml
        template = os.path.join(os.path.dirname(__file__), "..", "..", "..", "config",
                                "config.yaml.template")
        with open(template, encoding="utf-8") as fh:
            g18 = yaml.safe_load(fh)["groups"]["G18_observability"]
        assert g18["langfuse_usage_tags"] is True
        assert g18["langfuse_tag_keys"] == []
        assert g18["langfuse_session_tag"] == ""
        assert g18["langfuse_session_tenant_scope"] is True
        assert g18["langfuse_enabled"] is False          # tracing itself stays off in OSS

    def test_the_settings_are_documented(self):
        doc = os.path.join(os.path.dirname(__file__), "..", "..", "..", "docs",
                           "config-reference.md")
        with open(doc, encoding="utf-8") as fh:
            text = fh.read()
        for key in ("langfuse_usage_tags", "langfuse_tag_keys", "langfuse_session_tag",
                    "langfuse_session_tenant_scope"):
            assert f"| `{key}` |" in text, key


@pytest.mark.asyncio
class TestLangfuseEnabledGate:
    """`langfuse_enabled` gates trace emission. OSS default OFF (absent → off);
    the commercial deploy sets it true. Keys are still required to actually emit."""

    def _cfg(self, ctx, **g18):
        ctx.config["groups"]["G18_observability"] = g18
        return ctx

    async def test_no_trace_when_langfuse_disabled(self, make_ctx):
        ctx = self._cfg(make_ctx(), enabled=True)  # langfuse_enabled absent → OFF
        mock_client = MagicMock()
        from middleware import langfuse_tracing
        with patch.object(langfuse_tracing, "get_client", return_value=mock_client):
            trace = langfuse_tracing.start_trace(ctx)
        assert trace is None
        mock_client.trace.assert_not_called()

    async def test_trace_created_when_enabled(self, make_ctx):
        ctx = self._cfg(make_ctx(), enabled=True, langfuse_enabled=True)
        mock_client = MagicMock()
        mock_client.trace.return_value = MagicMock()
        from middleware import langfuse_tracing
        with patch.object(langfuse_tracing, "get_client", return_value=mock_client):
            trace = langfuse_tracing.start_trace(ctx)
        assert trace is not None
        mock_client.trace.assert_called_once()

    async def test_no_trace_when_flag_true_but_no_keys(self, make_ctx):
        ctx = self._cfg(make_ctx(), enabled=True, langfuse_enabled=True)
        from middleware import langfuse_tracing
        with patch.object(langfuse_tracing, "get_client", return_value=None):  # keys absent
            trace = langfuse_tracing.start_trace(ctx)
        assert trace is None
