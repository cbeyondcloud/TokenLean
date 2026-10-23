"""Unit tests for the G13 provider-native batch lane (P2): dispatcher, native
flush grouping/fallback, and the background poller. Redis and provider adapters
are mocked — no network, no live batch."""
import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from middleware import g13_batch


@pytest.fixture(autouse=True)
def _clear_native_memo():
    """The native-unsupported memo is process-global — isolate it between tests."""
    g13_batch._NATIVE_BATCH_UNSUPPORTED.clear()
    yield
    g13_batch._NATIVE_BATCH_UNSUPPORTED.clear()


def _items(n=2, provider_model="gpt-4o-mini"):
    return [
        {
            "request_id": f"r{i}",
            "model": provider_model,
            "messages": [{"role": "user", "content": f"Q{i}"}],
            "params": {},
        }
        for i in range(n)
    ]


def _fake_adapter(name="openai", native=True, job_id="batch-1"):
    a = MagicMock()
    a.name = name
    a.supports_native_batch.return_value = native
    a.submit_batch = AsyncMock(return_value=job_id)
    return a


# ---------------------------------------------------------------------------
# Dispatcher: provider_native flag routes to native vs loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestFlushDispatcher:
    async def test_disabled_uses_loop(self):
        cfg = {"groups": {"G13_batch": {"provider_native": False}}}
        with patch.object(g13_batch, "_flush_batch_native", new=AsyncMock()) as native, \
             patch.object(g13_batch, "_flush_batch_loop", new=AsyncMock()) as loop:
            await g13_batch._flush_batch("t", _items(), cfg)
        loop.assert_awaited_once()
        native.assert_not_awaited()

    async def test_enabled_uses_native(self):
        cfg = {"groups": {"G13_batch": {"provider_native": True}}}
        with patch.object(g13_batch, "_flush_batch_native", new=AsyncMock()) as native, \
             patch.object(g13_batch, "_flush_batch_loop", new=AsyncMock()) as loop:
            await g13_batch._flush_batch("t", _items(), cfg)
        native.assert_awaited_once()
        loop.assert_not_awaited()


# ---------------------------------------------------------------------------
# Native flush: grouping, submission, fallback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestFlushNative:
    async def test_submits_native_and_records_job(self):
        adapter = _fake_adapter()
        cfg = {"providers": [], "groups": {"G13_batch": {}}}
        with patch("providers.get_adapter", return_value=adapter), \
             patch("auth.api_key_manager.get_llm_provider_key", return_value="sk-test"), \
             patch.object(g13_batch, "_record_batch_job", new=AsyncMock()) as rec, \
             patch.object(g13_batch, "_flush_batch_loop", new=AsyncMock()) as loop:
            await g13_batch._flush_batch_native("t", _items(2), cfg)

        adapter.submit_batch.assert_awaited_once()
        submitted_items = adapter.submit_batch.await_args.args[0]
        assert [it["request_id"] for it in submitted_items] == ["r0", "r1"]
        # _record_batch_job now receives the full items (not just ids) so it can carry
        # each item's baseline_tokens through to the poller for header attribution.
        rec.assert_awaited_once()
        job_id_arg, provider_arg, items_arg, tenant_arg = rec.await_args.args
        assert job_id_arg == "batch-1" and provider_arg == "openai" and tenant_arg == "default"
        assert [it["request_id"] for it in items_arg] == ["r0", "r1"]
        loop.assert_not_awaited()

    async def test_falls_back_when_provider_not_native(self):
        adapter = _fake_adapter(native=False)
        cfg = {"providers": [], "groups": {"G13_batch": {}}}
        with patch("providers.get_adapter", return_value=adapter), \
             patch("auth.api_key_manager.get_llm_provider_key", return_value="sk-test"), \
             patch.object(g13_batch, "_flush_batch_loop", new=AsyncMock()) as loop:
            await g13_batch._flush_batch_native("t", _items(2), cfg)
        adapter.submit_batch.assert_not_awaited()
        loop.assert_awaited_once()

    async def test_falls_back_when_key_missing(self):
        adapter = _fake_adapter()
        cfg = {"providers": [], "groups": {"G13_batch": {}}}
        with patch("providers.get_adapter", return_value=adapter), \
             patch("auth.api_key_manager.get_llm_provider_key", return_value=None), \
             patch.object(g13_batch, "_flush_batch_loop", new=AsyncMock()) as loop:
            await g13_batch._flush_batch_native("t", _items(1), cfg)
        adapter.submit_batch.assert_not_awaited()
        loop.assert_awaited_once()

    async def test_falls_back_when_submit_raises(self):
        adapter = _fake_adapter()
        adapter.submit_batch = AsyncMock(side_effect=RuntimeError("boom"))
        cfg = {"providers": [], "groups": {"G13_batch": {}}}
        with patch("providers.get_adapter", return_value=adapter), \
             patch("auth.api_key_manager.get_llm_provider_key", return_value="sk-test"), \
             patch.object(g13_batch, "_flush_batch_loop", new=AsyncMock()) as loop:
            await g13_batch._flush_batch_native("t", _items(1), cfg)
        loop.assert_awaited_once()
        assert ("openai", "default") in g13_batch._NATIVE_BATCH_UNSUPPORTED  # memoised, per tenant

    async def test_memo_skips_native_after_failure(self):
        adapter = _fake_adapter()
        adapter.submit_batch = AsyncMock(side_effect=RuntimeError("boom"))
        cfg = {"providers": [], "groups": {"G13_batch": {}}}
        with patch("providers.get_adapter", return_value=adapter), \
             patch("auth.api_key_manager.get_llm_provider_key", return_value="sk-test"), \
             patch.object(g13_batch, "_flush_batch_loop", new=AsyncMock()) as loop:
            await g13_batch._flush_batch_native("t", _items(1), cfg)  # 1st: fails → memo
            await g13_batch._flush_batch_native("t", _items(1), cfg)  # 2nd: skips native
        assert adapter.submit_batch.await_count == 1   # native attempted only once
        assert loop.await_count == 2                   # loop used both times


# ---------------------------------------------------------------------------
# #105: each tenant's own key (the owner's rule: every model key is the tenant's)
# ---------------------------------------------------------------------------

@pytest.fixture
def tenant_keys():
    """A fake tenant-key resolver: `table[(provider, tenant)]`, `denied` refuses (strict BYOK),
    `asked` records lookups."""
    import types

    from providers.key_resolver import (
        ProviderKeyError,
        reset_provider_key_resolver,
        set_provider_key_resolver,
    )
    state = types.SimpleNamespace(table={}, denied=set(), asked=[])

    async def resolver(provider, tenant_id, ctx=None):
        state.asked.append((provider, tenant_id))
        if (provider, tenant_id) in state.denied:
            raise ProviderKeyError(provider, tenant_id)
        return state.table.get((provider, tenant_id))

    set_provider_key_resolver(resolver)
    yield state
    reset_provider_key_resolver()


def _tenant_items(tenant, n=1):
    return [dict(item, request_id=f"{tenant}-{item['request_id']}", tenant_id=tenant)
            for item in _items(n)]


@pytest.mark.asyncio
class TestEachTenantsOwnKey:
    """The native lane submitted, polled and fetched every tenant's batch with the PLATFORM
    key, billing tenants' requests to the operator's account."""

    async def test_each_tenant_submits_its_own_batch_with_its_own_key(self, tenant_keys):
        tenant_keys.table.update({("openai", "A"): "sk-A", ("openai", "B"): "sk-B"})
        adapter = _fake_adapter()
        cfg = {"providers": [], "groups": {"G13_batch": {}}}
        with patch("providers.get_adapter", return_value=adapter), \
             patch("auth.api_key_manager.get_llm_provider_key",
                   side_effect=AssertionError("the platform key was used")), \
             patch.object(g13_batch, "_record_batch_job", new=AsyncMock()) as rec, \
             patch.object(g13_batch, "_flush_batch_loop", new=AsyncMock()) as loop:
            await g13_batch._flush_batch_native("t", _tenant_items("A", 2) + _tenant_items("B"), cfg)
        submitted = sorted((call.args[1], [it["request_id"] for it in call.args[0]])
                           for call in adapter.submit_batch.await_args_list)
        assert submitted == [("sk-A", ["A-r0", "A-r1"]), ("sk-B", ["B-r0"])]
        assert sorted(call.args[3] for call in rec.await_args_list) == ["A", "B"]
        loop.assert_not_awaited()

    @pytest.mark.parametrize("deny", [False, True], ids=["no-key", "strict-byok-refuses"])
    async def test_a_tenant_without_its_own_key_takes_the_loop_lane(self, tenant_keys, deny):
        tenant_keys.table[("openai", "A")] = "sk-A"
        if deny:
            tenant_keys.denied.add(("openai", "B"))
        adapter = _fake_adapter()
        cfg = {"providers": [], "groups": {"G13_batch": {}}}
        with patch("providers.get_adapter", return_value=adapter), \
             patch.object(g13_batch, "_record_batch_job", new=AsyncMock()), \
             patch.object(g13_batch, "_flush_batch_loop", new=AsyncMock()) as loop:
            await g13_batch._flush_batch_native("t", _tenant_items("A") + _tenant_items("B"), cfg)
        assert [call.args[1] for call in adapter.submit_batch.await_args_list] == ["sk-A"]
        assert [[it["request_id"] for it in call.args[1]] for call in loop.await_args_list] == [["B-r0"]]

    async def test_a_failed_submit_turns_the_native_lane_off_for_that_tenant_only(self, tenant_keys):
        tenant_keys.table.update({("openai", "A"): "sk-A", ("openai", "B"): "sk-B"})
        adapter = _fake_adapter()
        adapter.submit_batch = AsyncMock(side_effect=lambda items, key, cfg: (
            (_ for _ in ()).throw(RuntimeError("A's key cannot batch")) if key == "sk-A" else "job-B"))
        cfg = {"providers": [], "groups": {"G13_batch": {}}}
        with patch("providers.get_adapter", return_value=adapter), \
             patch.object(g13_batch, "_record_batch_job", new=AsyncMock()), \
             patch.object(g13_batch, "_flush_batch_loop", new=AsyncMock()):
            await g13_batch._flush_batch_native("t", _tenant_items("A") + _tenant_items("B"), cfg)
            await g13_batch._flush_batch_native("t", _tenant_items("A") + _tenant_items("B"), cfg)
        keys_tried = [call.args[1] for call in adapter.submit_batch.await_args_list]
        assert keys_tried.count("sk-A") == 1 and keys_tried.count("sk-B") == 2

    async def test_the_job_record_carries_its_tenant(self):
        redis = MagicMock()
        redis.hset = AsyncMock()
        with patch.object(g13_batch, "_get_redis", return_value=redis):
            await g13_batch._record_batch_job("job-1", "openai", _tenant_items("A"), "A")
        assert json.loads(redis.hset.await_args.args[2])["tenant_id"] == "A"

    def _redis(self, jobs):
        r = MagicMock()
        r.hgetall = AsyncMock(return_value=jobs)
        r.hdel = AsyncMock()
        return r

    async def test_a_job_is_polled_and_fetched_with_its_tenants_key(self, tenant_keys):
        tenant_keys.table[("openai", "A")] = "sk-A"
        jobs = {"job-A": json.dumps({"provider": "openai", "tenant_id": "A", "request_ids": ["A-r0"]})}
        adapter = MagicMock()
        adapter.poll_batch = AsyncMock(return_value="completed")
        adapter.fetch_batch_results = AsyncMock(return_value=[{"request_id": "A-r0", "response": {}}])
        with patch.object(g13_batch, "_get_redis", return_value=self._redis(jobs)), \
             patch("providers.get_adapter_by_name", return_value=adapter), \
             patch("auth.api_key_manager.get_llm_provider_key",
                   side_effect=AssertionError("the platform key was used")), \
             patch.object(g13_batch, "_store_batch_result", new=AsyncMock()):
            assert await g13_batch.poll_batch_jobs({}) == 1
        assert adapter.poll_batch.await_args.args == ("job-A", "sk-A")
        assert adapter.fetch_batch_results.await_args.args == ("job-A", "sk-A")

    async def test_a_job_whose_tenant_lost_its_key_waits_and_never_takes_the_platform_key(
            self, tenant_keys):
        tenant_keys.denied.add(("openai", "A"))
        jobs = {"job-A": json.dumps({"provider": "openai", "tenant_id": "A", "request_ids": ["A-r0"]})}
        redis = self._redis(jobs)
        adapter = MagicMock()
        adapter.poll_batch = AsyncMock(return_value="completed")
        with patch.object(g13_batch, "_get_redis", return_value=redis), \
             patch("providers.get_adapter_by_name", return_value=adapter), \
             patch("auth.api_key_manager.get_llm_provider_key",
                   side_effect=AssertionError("the platform key was used")):
            assert await g13_batch.poll_batch_jobs({}) == 0
        adapter.poll_batch.assert_not_awaited()
        redis.hdel.assert_not_awaited()            # kept for the next pass

    async def test_a_job_recorded_before_jobs_carried_a_tenant_is_read_with_the_platform_key(
            self, tenant_keys):
        """Only a key of the account that created a batch can read it, and such a job was
        created with the platform key."""
        jobs = {"job-old": json.dumps({"provider": "openai", "request_ids": ["r0"]})}
        adapter = MagicMock()
        adapter.poll_batch = AsyncMock(return_value="pending")
        with patch.object(g13_batch, "_get_redis", return_value=self._redis(jobs)), \
             patch("providers.get_adapter_by_name", return_value=adapter), \
             patch("auth.api_key_manager.get_llm_provider_key", return_value="sk-platform"):
            await g13_batch.poll_batch_jobs({})
        assert adapter.poll_batch.await_args.args == ("job-old", "sk-platform")
        assert tenant_keys.asked == []


# ---------------------------------------------------------------------------
# Poller: status handling + result storage
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestPollBatchJobs:
    def _redis(self, jobs):
        r = MagicMock()
        r.hgetall = AsyncMock(return_value=jobs)
        r.hdel = AsyncMock()
        return r

    async def test_pending_job_is_left_in_place(self):
        jobs = {"batch-1": json.dumps({"provider": "openai", "request_ids": ["r0"]})}
        redis = self._redis(jobs)
        adapter = MagicMock()
        adapter.poll_batch = AsyncMock(return_value="pending")
        with patch.object(g13_batch, "_get_redis", return_value=redis), \
             patch("providers.get_adapter_by_name", return_value=adapter), \
             patch("auth.api_key_manager.get_llm_provider_key", return_value="sk-test"), \
             patch.object(g13_batch, "_store_batch_result", new=AsyncMock()) as store:
            finished = await g13_batch.poll_batch_jobs({})
        assert finished == 0
        redis.hdel.assert_not_awaited()
        store.assert_not_awaited()

    async def test_completed_job_stores_results_and_removes_job(self):
        jobs = {"batch-1": json.dumps({"provider": "openai", "request_ids": ["r0", "r1"]})}
        redis = self._redis(jobs)
        adapter = MagicMock()
        adapter.poll_batch = AsyncMock(return_value="completed")
        adapter.fetch_batch_results = AsyncMock(return_value=[
            {"request_id": "r0", "response": {"id": "c0"}},
            {"request_id": "r1", "error": "bad"},
        ])
        with patch.object(g13_batch, "_get_redis", return_value=redis), \
             patch("providers.get_adapter_by_name", return_value=adapter), \
             patch("auth.api_key_manager.get_llm_provider_key", return_value="sk-test"), \
             patch.object(g13_batch, "_store_batch_result", new=AsyncMock()) as store:
            finished = await g13_batch.poll_batch_jobs({})

        assert finished == 1
        redis.hdel.assert_awaited_once()
        stored = {c.args[0]: c.args[1] for c in store.await_args_list}
        assert stored["r0"]["status"] == "completed"
        assert stored["r0"]["response"]["_batch_request_id"] == "r0"
        assert stored["r1"]["status"] == "failed"

    async def test_completed_missing_result_marked_failed(self):
        jobs = {"batch-1": json.dumps({"provider": "openai", "request_ids": ["r0", "r1"]})}
        redis = self._redis(jobs)
        adapter = MagicMock()
        adapter.poll_batch = AsyncMock(return_value="completed")
        adapter.fetch_batch_results = AsyncMock(return_value=[
            {"request_id": "r0", "response": {"id": "c0"}},
        ])
        with patch.object(g13_batch, "_get_redis", return_value=redis), \
             patch("providers.get_adapter_by_name", return_value=adapter), \
             patch("auth.api_key_manager.get_llm_provider_key", return_value="sk-test"), \
             patch.object(g13_batch, "_store_batch_result", new=AsyncMock()) as store:
            await g13_batch.poll_batch_jobs({})
        stored = {c.args[0]: c.args[1] for c in store.await_args_list}
        assert stored["r1"]["status"] == "failed"

    async def test_failed_job_marks_all_failed_and_removes(self):
        jobs = {"batch-1": json.dumps({"provider": "openai", "request_ids": ["r0", "r1"]})}
        redis = self._redis(jobs)
        adapter = MagicMock()
        adapter.poll_batch = AsyncMock(return_value="failed")
        with patch.object(g13_batch, "_get_redis", return_value=redis), \
             patch("providers.get_adapter_by_name", return_value=adapter), \
             patch("auth.api_key_manager.get_llm_provider_key", return_value="sk-test"), \
             patch.object(g13_batch, "_store_batch_result", new=AsyncMock()) as store:
            finished = await g13_batch.poll_batch_jobs({})
        assert finished == 1
        assert all(c.args[1]["status"] == "failed" for c in store.await_args_list)
        redis.hdel.assert_awaited_once()

    async def test_no_jobs_is_noop(self):
        redis = self._redis({})
        with patch.object(g13_batch, "_get_redis", return_value=redis):
            assert await g13_batch.poll_batch_jobs({}) == 0


@pytest.mark.asyncio
class TestBaselineTokensAttribution:
    """Regression: /v1/batch/results needs baseline_tokens to build x-tokenlean-* headers
    on completion, but it has no RequestContext at poll time — baseline_tokens has to be
    threaded through from _accumulate → the flush lane → _store_batch_result."""

    async def test_record_batch_job_carries_baseline_tokens_map(self):
        redis = MagicMock()
        redis.hset = AsyncMock()
        items = [
            {"request_id": "r0", "baseline_tokens": 120},
            {"request_id": "r1", "baseline_tokens": 340},
        ]
        with patch.object(g13_batch, "_get_redis", return_value=redis):
            await g13_batch._record_batch_job("job-1", "openai", items)
        stored = json.loads(redis.hset.await_args.args[2])
        assert stored["request_ids"] == ["r0", "r1"]
        assert stored["baseline_tokens"] == {"r0": 120, "r1": 340}

    async def test_poll_completed_native_job_stores_baseline_tokens(self):
        jobs = {"batch-1": json.dumps({
            "provider": "openai", "request_ids": ["r0"],
            "baseline_tokens": {"r0": 250},
        })}
        redis = MagicMock()
        redis.hgetall = AsyncMock(return_value=jobs)
        redis.hdel = AsyncMock()
        adapter = MagicMock()
        adapter.poll_batch = AsyncMock(return_value="completed")
        adapter.fetch_batch_results = AsyncMock(return_value=[
            {"request_id": "r0", "response": {"id": "c0", "usage": {"prompt_tokens": 100}}},
        ])
        with patch.object(g13_batch, "_get_redis", return_value=redis), \
             patch("providers.get_adapter_by_name", return_value=adapter), \
             patch("auth.api_key_manager.get_llm_provider_key", return_value="sk-test"), \
             patch.object(g13_batch, "_store_batch_result", new=AsyncMock()) as store:
            await g13_batch.poll_batch_jobs({})
        stored = {c.args[0]: c.args[1] for c in store.await_args_list}
        assert stored["r0"]["baseline_tokens"] == 250

    async def test_poll_missing_baseline_tokens_map_defaults_to_zero(self):
        """Backward compatible: a job recorded before this fix (no baseline_tokens key)
        must not crash the poller."""
        jobs = {"batch-1": json.dumps({"provider": "openai", "request_ids": ["r0"]})}
        redis = MagicMock()
        redis.hgetall = AsyncMock(return_value=jobs)
        redis.hdel = AsyncMock()
        adapter = MagicMock()
        adapter.poll_batch = AsyncMock(return_value="completed")
        adapter.fetch_batch_results = AsyncMock(return_value=[
            {"request_id": "r0", "response": {"id": "c0"}},
        ])
        with patch.object(g13_batch, "_get_redis", return_value=redis), \
             patch("providers.get_adapter_by_name", return_value=adapter), \
             patch("auth.api_key_manager.get_llm_provider_key", return_value="sk-test"), \
             patch.object(g13_batch, "_store_batch_result", new=AsyncMock()) as store:
            await g13_batch.poll_batch_jobs({})
        stored = {c.args[0]: c.args[1] for c in store.await_args_list}
        assert stored["r0"]["baseline_tokens"] == 0


class _SpendCounter:
    """The spend counter's Redis: records what is added to it."""

    def __init__(self):
        self.added = []

    async def incrbyfloat(self, key, amount):
        self.added.append((key, amount))
        return amount

    async def expire(self, key, ttl):
        pass


@pytest.mark.asyncio
class TestNativeBatchCostReachesTheSpendCap:
    """The native lane's answers arrive in the poller. It adds each completed answer's cost,
    at the configured native-batch discount, to the spend counter of the tenant whose request
    it was."""

    async def test_the_job_keeps_whose_spend_each_request_is(self):
        redis = MagicMock()
        redis.hset = AsyncMock()
        items = [{"request_id": "r0", "model": "gpt-4o-mini", "spend_prefix": "t:acme:"},
                 {"request_id": "r1", "model": "gpt-4o-mini", "spend_prefix": ""},
                 {"request_id": "r2", "model": "gpt-4o-mini"}]
        with patch.object(g13_batch, "_get_redis", return_value=redis):
            await g13_batch._record_batch_job("job-1", "openai", items)
        stored = json.loads(redis.hset.await_args.args[2])
        assert stored["spend"] == {"r0": {"prefix": "t:acme:", "model": "gpt-4o-mini"}}

    @staticmethod
    async def _poll(monkeypatch, meta):
        from middleware import g00_rate_limit
        counter = _SpendCounter()
        monkeypatch.setattr(g00_rate_limit, "_get_redis", lambda: counter)
        redis = MagicMock()
        redis.hgetall = AsyncMock(return_value={"batch-1": json.dumps(meta)})
        redis.hdel = AsyncMock()
        adapter = MagicMock()
        adapter.poll_batch = AsyncMock(return_value="completed")
        adapter.fetch_batch_results = AsyncMock(return_value=[
            {"request_id": "r0", "response": {
                "id": "c0", "usage": {"prompt_tokens": 1200, "completion_tokens": 300}}},
            {"request_id": "r1", "error": "bad"},
        ])
        adapter.extract_usage.return_value = {}
        cfg = {"groups": {"G18_observability": {"enabled": True, "batch_discount_multiplier": 0.5}}}
        with patch.object(g13_batch, "_get_redis", return_value=redis), \
             patch("providers.get_adapter_by_name", return_value=adapter), \
             patch("auth.api_key_manager.get_llm_provider_key", return_value="sk-test"), \
             patch.object(g13_batch, "_store_batch_result", new=AsyncMock()):
            assert await g13_batch.poll_batch_jobs(cfg) == 1
        return counter.added

    async def test_a_completed_answer_s_discounted_cost_is_added(self, monkeypatch):
        from middleware.g00_rate_limit import G00RateLimit
        from savings.calculator import estimate_cost
        owed = {"prefix": "t:acme:", "model": "gpt-4o-mini"}
        added = await self._poll(monkeypatch, {"provider": "openai", "request_ids": ["r0", "r1"],
                                               "spend": {"r0": owed, "r1": owed}})
        # r1 failed, so only r0's cost is added.
        assert added == [(G00RateLimit.spend_key("t:acme:"),
                          pytest.approx(0.5 * estimate_cost(1200, 300, "gpt-4o-mini")))]

    async def test_a_job_that_names_no_counter_adds_nothing(self, monkeypatch):
        added = await self._poll(monkeypatch, {"provider": "openai", "request_ids": ["r0", "r1"]})
        assert added == []

    async def test_the_job_keeps_each_request_s_team(self):
        redis = MagicMock()
        redis.hset = AsyncMock()
        items = [{"request_id": "r0", "model": "m", "spend_prefix": "t:acme:",
                  "spend_team": "payments"},
                 {"request_id": "r1", "model": "m", "spend_prefix": "t:acme:"}]
        with patch.object(g13_batch, "_get_redis", return_value=redis):
            await g13_batch._record_batch_job("job-1", "openai", items)
        stored = json.loads(redis.hset.await_args.args[2])
        assert stored["spend"] == {"r0": {"prefix": "t:acme:", "model": "m", "team": "payments"},
                                   "r1": {"prefix": "t:acme:", "model": "m"}}

    async def test_a_team_s_answer_is_added_to_the_team_s_counter_too(self, monkeypatch):
        from middleware.g00_rate_limit import G00RateLimit
        from savings.calculator import estimate_cost
        owed = {"prefix": "t:acme:", "model": "gpt-4o-mini", "team": "payments"}
        added = await self._poll(monkeypatch, {"provider": "openai", "request_ids": ["r0", "r1"],
                                               "spend": {"r0": owed, "r1": owed}})
        cost = pytest.approx(0.5 * estimate_cost(1200, 300, "gpt-4o-mini"))
        assert added == [(G00RateLimit.spend_key("t:acme:"), cost),
                         (G00RateLimit.team_spend_key("t:acme:", "payments"), cost)]


@pytest.mark.asyncio
async def test_start_batch_poller_noop_when_native_disabled():
    # Returns immediately (no infinite loop) when provider_native is off.
    cfg = {"groups": {"G13_batch": {"enabled": True, "provider_native": False}}}
    await g13_batch.start_batch_poller(cfg)
