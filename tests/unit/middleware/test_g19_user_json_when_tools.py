"""G19 leaves a JSON payload the USER pasted alone when the request offers tools (#133).

On DS4 (2026-10-15, arms confirmed live, temperature 0) G19 rewrote the user's order array into
`{"_schema_": [...], "_rows_": [[...]]}`. With a `get_order_details` tool on offer the model then
called the tool in 9 of 20 requests instead of answering from the array (0 of 20 unrewritten): a
second turn carrying a ~10k-token tool result, 64,058 tokens sent against 9,900.

The rule is deliberately narrow. A tool RESULT is data the model already fetched, and logs, code
and prose were never implicated, so those are still compressed - G19's agentic savings stay.
"""
import json

import pytest
import yaml

from middleware.g19_headroom import G19Headroom

_ORDERS = json.dumps([
    {"order_id": f"ORD-{1000 + i}", "customer": "Acme Corp", "amount": 50.0 + i,
     "status": "pending" if i % 2 == 0 else "shipped", "region": "us-east"}
    for i in range(8)
], indent=2)

_TOOL = {"type": "function", "function": {"name": "get_order_details", "parameters": {}}}

_LOGS = "\n".join(["2026-10-15T10:00:00Z INFO worker started"] * 40)


def _cfg(**over):
    block = {
        "enabled": True, "request_side_enabled": True, "response_side_enabled": True,
        "min_length_to_compress": 50,
        "compression_strategies": {
            "json": {"remove_empty": True, "dedupe_keys": True},
            "logs": {"dedupe_lines": True, "truncate_long_lines": 200,
                     "always_keep_severities": ["ERROR", "FATAL", "CRITICAL", "PANIC"]},
        },
    }
    block.update(over)
    return {"groups": {"G19_headroom": block}}


async def _run(make_ctx, messages, params=None, **over):
    ctx = make_ctx(messages, params=params or {}, config=_cfg(**over))
    before = [m["content"] for m in ctx.messages]
    ctx = await G19Headroom().process_request(ctx)
    return ctx, before


@pytest.mark.asyncio
class TestUserJsonWithTools:
    _USER = [{"role": "system", "content": "You process order batches."},
             {"role": "user", "content": _ORDERS}]

    async def test_without_tools_the_array_is_still_rewritten(self, make_ctx):
        ctx, before = await _run(make_ctx, self._USER)
        assert ctx.messages[1]["content"] != before[1]
        assert "_schema_" in ctx.messages[1]["content"]

    async def test_a_request_offering_tools_keeps_the_users_array(self, make_ctx):
        ctx, before = await _run(make_ctx, self._USER, params={"tools": [_TOOL]})
        assert ctx.messages[1]["content"] == before[1]
        assert not any(s.group == "G19" for s in ctx.savings.step_savings)

    async def test_the_legacy_functions_field_counts_as_tools(self, make_ctx):
        ctx, before = await _run(make_ctx, self._USER, params={"functions": [{"name": "f"}]})
        assert ctx.messages[1]["content"] == before[1]

    async def test_an_empty_tools_list_offers_nothing(self, make_ctx):
        ctx, before = await _run(make_ctx, self._USER, params={"tools": []})
        assert ctx.messages[1]["content"] != before[1]

    async def test_the_knob_off_restores_the_rewrite(self, make_ctx):
        ctx, before = await _run(make_ctx, self._USER, params={"tools": [_TOOL]},
                                 skip_user_json_when_tools=False)
        assert ctx.messages[1]["content"] != before[1]

    async def test_a_tool_result_is_still_compressed_with_tools_on_offer(self, make_ctx):
        """The agentic path: data the model already fetched. This is where G19 earns its keep
        on tool-heavy conversations, so the rule must not reach it."""
        msgs = [
            {"role": "user", "content": "look up the batch"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "get_order_details", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": _ORDERS},
        ]
        ctx, before = await _run(make_ctx, msgs, params={"tools": [_TOOL]})
        assert ctx.messages[2]["content"] != before[2]
        assert "_schema_" in ctx.messages[2]["content"]

    async def test_logs_in_a_user_turn_are_still_compressed_with_tools(self, make_ctx):
        """Only the shape measured to change answer-vs-fetch is spared."""
        msgs = [{"role": "user", "content": _LOGS}]
        ctx, before = await _run(make_ctx, msgs, params={"tools": [_TOOL]})
        assert len(ctx.messages[0]["content"]) < len(before[0])

    async def test_the_spared_message_does_not_stop_its_neighbours(self, make_ctx):
        msgs = [{"role": "user", "content": _ORDERS}, {"role": "user", "content": _LOGS}]
        ctx, before = await _run(make_ctx, msgs, params={"tools": [_TOOL]})
        assert ctx.messages[0]["content"] == before[0]
        assert len(ctx.messages[1]["content"]) < len(before[1])


def test_the_template_ships_it_on():
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    cfg = yaml.safe_load((root / "config" / "config.yaml.template").read_text(encoding="utf-8"))
    assert cfg["groups"]["G19_headroom"]["skip_user_json_when_tools"] is True
