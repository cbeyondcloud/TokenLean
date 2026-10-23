"""OpenAI Responses API ingress (``POST /v1/responses``) <-> the internal chat shape (ST-4).

The adapter is a pure translator, like the Anthropic and Gemini ones: request items and tools
become chat messages and function tools on the way in; the chat completion (or its stream)
becomes a Response object (or Responses SSE events) on the way out. A tool the request
declared ``custom`` (Codex's apply_patch) travels as a one-string-parameter function and comes
back as a ``custom_tool_call`` item, streamed or not, because Codex accepts apply_patch only
in that shape (spike S3, run T).
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import json

import pytest

from protocols import RESPONSES, get_protocol
from protocols.base import UnsupportedRequestField
from protocols.responses_ingress import ResponsesProtocol

_GRAMMAR = 'start: begin_patch hunk+ end_patch\nbegin_patch: "*** Begin Patch" LF\n'
_PATCH = "*** Begin Patch\n*** Add File: hello.txt\n+hello\n*** End Patch\n"

_EXEC_TOOL = {"type": "function", "name": "exec_command", "description": "Run a command",
              "strict": False,
              "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}},
                             "required": ["cmd"]}}
_PATCH_TOOL = {"type": "custom", "name": "apply_patch", "description": "Edit files",
               "format": {"type": "grammar", "syntax": "lark", "definition": _GRAMMAR}}


def _bound(request_id="req-1"):
    return RESPONSES.for_request(request_id)


def _parse(body, **kw):
    proto = _bound()
    messages, model, params = proto.parse_request(body, {}, "", **kw)
    return proto, messages, model, params


def _events(frames):
    """(event name, data) for each SSE frame a translator produced."""
    out = []
    for frame in frames:
        lines = frame.strip().split("\n")
        assert lines[0].startswith("event: "), frame
        assert lines[1].startswith("data: "), frame
        name, data = lines[0][len("event: "):], json.loads(lines[1][len("data: "):])
        assert data["type"] == name
        out.append((name, data))
    return out


def _run_stream(proto, chunks, *, fail=None):
    t = proto.stream_translator()
    frames = list(t.start())
    for c in chunks:
        frames.extend(t.chunk(c))
    if fail is not None:
        frames.extend(t.error(fail))
    else:
        frames.extend(t.finish())
    return _events(frames)


# ── registration ────────────────────────────────────────────────────────────────

def test_registered_by_its_own_name():
    assert get_protocol("responses") is RESPONSES
    assert RESPONSES.name == "responses"


def test_for_request_gives_a_fresh_instance_each_time():
    a, b = RESPONSES.for_request("r1"), RESPONSES.for_request("r2")
    assert a is not b and a is not RESPONSES
    assert isinstance(a, ResponsesProtocol)


def test_the_other_adapters_bind_to_themselves():
    from protocols import ANTHROPIC, GEMINI
    assert ANTHROPIC.for_request("r") is ANTHROPIC
    assert GEMINI.for_request("r") is GEMINI


# ── request: messages ──────────────────────────────────────────────────────────

def test_instructions_and_message_items_become_chat_messages():
    _, messages, model, params = _parse({
        "model": "claude-sonnet-4-5", "instructions": "You are Codex.",
        "input": [
            {"type": "message", "role": "developer",
             "content": [{"type": "input_text", "text": "sandbox: off"}]},
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "<env/>"},
                         {"type": "input_text", "text": "fix the bug"}]},
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "Done.", "annotations": []}]},
            {"role": "user", "content": "thanks"},          # EasyInputMessage: no type
        ],
    })
    assert model == "claude-sonnet-4-5"
    assert messages == [
        {"role": "system", "content": "You are Codex."},
        {"role": "system", "content": "sandbox: off"},
        {"role": "user", "content": "<env/>\n\nfix the bug"},
        {"role": "assistant", "content": "Done."},
        {"role": "user", "content": "thanks"},
    ]


def test_a_string_input_is_one_user_message():
    _, messages, _, _ = _parse({"model": "m", "input": "hello"})
    assert messages == [{"role": "user", "content": "hello"}]


def test_an_image_part_keeps_the_content_as_parts():
    _, messages, _, _ = _parse({"model": "m", "input": [{"type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "what is this"},
                    {"type": "input_image", "image_url": "data:image/png;base64,AAAA",
                     "detail": "low"}]}]})
    assert messages == [{"role": "user", "content": [
        {"type": "text", "text": "what is this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA",
                                            "detail": "low"}}]}]


def test_function_calls_and_outputs_become_tool_calls_and_tool_messages():
    _, messages, _, _ = _parse({"model": "m", "input": [
        {"role": "user", "content": "list files"},
        {"type": "reasoning", "id": "rs_1", "summary": [],
         "encrypted_content": "gAAAAopaque"},
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "Looking."}]},
        {"type": "function_call", "id": "fc_1", "call_id": "call_a", "name": "exec_command",
         "arguments": '{"cmd":"ls"}'},
        {"type": "function_call", "id": "fc_2", "call_id": "call_b", "name": "exec_command",
         "arguments": '{"cmd":"pwd"}'},
        {"type": "function_call_output", "call_id": "call_a", "output": "a.txt"},
        {"type": "function_call_output", "call_id": "call_b",
         "output": [{"type": "input_text", "text": "/repo"}]},
    ]})
    assert messages == [
        {"role": "user", "content": "list files"},
        {"role": "assistant", "content": "Looking.", "tool_calls": [
            {"id": "call_a", "type": "function",
             "function": {"name": "exec_command", "arguments": '{"cmd":"ls"}'}},
            {"id": "call_b", "type": "function",
             "function": {"name": "exec_command", "arguments": '{"cmd":"pwd"}'}}]},
        {"role": "tool", "tool_call_id": "call_a", "content": "a.txt"},
        {"role": "tool", "tool_call_id": "call_b", "content": "/repo"},
    ]


def test_custom_tool_calls_round_trip_with_the_input_wrapped():
    _, messages, _, _ = _parse({"model": "m", "tools": [_PATCH_TOOL], "input": [
        {"role": "user", "content": "add hello.txt"},
        {"type": "custom_tool_call", "id": "ctc_1", "call_id": "call_p", "name": "apply_patch",
         "input": _PATCH},
        {"type": "custom_tool_call_output", "call_id": "call_p", "output": "Success."},
    ]})
    assert messages[1] == {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_p", "type": "function",
         "function": {"name": "apply_patch", "arguments": json.dumps({"input": _PATCH})}}]}
    assert messages[2] == {"role": "tool", "tool_call_id": "call_p", "content": "Success."}


def test_an_orphaned_tool_output_degrades_to_text():
    _, messages, _, _ = _parse({"model": "m", "input": [
        {"role": "user", "content": "hi"},
        {"type": "function_call_output", "call_id": "call_zzz", "output": "late"}]})
    assert messages[-1]["role"] == "user"
    assert "late" in messages[-1]["content"]
    assert not any(m.get("role") == "tool" for m in messages)


# ── request: tools and params ──────────────────────────────────────────────────

def test_function_and_custom_tools_become_function_tools():
    _, _, _, params = _parse({"model": "m", "input": "x", "tools": [_EXEC_TOOL, _PATCH_TOOL]})
    exec_fn, patch_fn = params["tools"]
    assert exec_fn == {"type": "function", "function": {
        "name": "exec_command", "description": "Run a command",
        "parameters": _EXEC_TOOL["parameters"], "strict": False}}
    assert patch_fn["type"] == "function"
    assert patch_fn["function"]["name"] == "apply_patch"
    schema = patch_fn["function"]["parameters"]
    assert schema["required"] == ["input"] and list(schema["properties"]) == ["input"]
    assert schema["properties"]["input"]["type"] == "string"
    # the model needs the grammar to write a valid patch
    assert _GRAMMAR in schema["properties"]["input"]["description"]
    assert "lark" in schema["properties"]["input"]["description"]


@pytest.mark.parametrize("tool,field", [
    ({"type": "namespace", "name": "multi_agent_v1", "tools": []}, "tools[1].type"),
    ({"type": "web_search"}, "tools[1].type"),
    ({"type": "file_search", "vector_store_ids": ["vs"]}, "tools[1].type"),
    ({"type": "function"}, "tools[1].name"),
])
def test_tools_it_cannot_carry_are_refused_naming_the_field(tool, field):
    with pytest.raises(UnsupportedRequestField) as exc:
        _parse({"model": "m", "input": "x", "tools": [_EXEC_TOOL, tool]})
    assert field in str(exc.value)


def test_a_custom_and_a_function_tool_with_one_name_are_refused():
    clash = dict(_PATCH_TOOL, name="exec_command")
    with pytest.raises(UnsupportedRequestField, match="tools"):
        _parse({"model": "m", "input": "x", "tools": [_EXEC_TOOL, clash]})


def test_text_format_and_reasoning_map_to_chat_params():
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
              "additionalProperties": False}
    _, _, _, params = _parse({
        "model": "m", "input": "x",
        "text": {"format": {"type": "json_schema", "name": "codex_output_schema",
                            "strict": True, "schema": schema}, "verbosity": "low"},
        "reasoning": {"effort": "high", "summary": "auto"},
        "max_output_tokens": 900, "temperature": 0.2, "top_p": 0.9, "stream": True,
        "tools": [_EXEC_TOOL], "tool_choice": "auto", "parallel_tool_calls": True,
        "prompt_cache_key": "thread-123",
    })
    assert params["response_format"] == {"type": "json_schema", "json_schema": {
        "name": "codex_output_schema", "schema": schema, "strict": True}}
    assert params["verbosity"] == "low"
    assert params["reasoning_effort"] == "high"
    assert params["max_tokens"] == 900
    assert (params["temperature"], params["top_p"], params["stream"]) == (0.2, 0.9, True)
    assert (params["tool_choice"], params["parallel_tool_calls"]) == ("auto", True)
    assert params["prompt_cache_key"] == "thread-123"


def test_json_object_and_text_formats():
    _, _, _, params = _parse({"model": "m", "input": "x",
                              "text": {"format": {"type": "json_object"}}})
    assert params["response_format"] == {"type": "json_object"}
    _, _, _, params = _parse({"model": "m", "input": "x", "text": {"format": {"type": "text"}}})
    assert "response_format" not in params


@pytest.mark.parametrize("choice,expected", [
    ("required", "required"),
    ("none", "none"),
    ({"type": "function", "name": "exec_command"},
     {"type": "function", "function": {"name": "exec_command"}}),
    ({"type": "custom", "name": "apply_patch"},
     {"type": "function", "function": {"name": "apply_patch"}}),
])
def test_tool_choice_maps(choice, expected):
    _, _, _, params = _parse({"model": "m", "input": "x", "tools": [_EXEC_TOOL, _PATCH_TOOL],
                              "tool_choice": choice})
    assert params["tool_choice"] == expected


@pytest.mark.parametrize("tools", [None, []])
def test_tool_settings_without_tools_are_not_sent(tools):
    # Codex sends tool_choice and parallel_tool_calls on every request, tool-less ones too;
    # OpenAI rejects either without tools.
    body = {"model": "m", "input": "x", "tool_choice": "auto", "parallel_tool_calls": True}
    if tools is not None:
        body["tools"] = tools
    _, _, _, params = _parse(body)
    assert not {"tools", "tool_choice", "parallel_tool_calls"} & set(params)


def test_a_non_string_role_is_refused_naming_the_field():
    with pytest.raises(UnsupportedRequestField, match=r"input\[0\]\.role"):
        _parse({"model": "m", "input": [{"role": ["user"], "content": "x"}]})


def test_a_message_with_no_content_parts_is_left_out():
    _, messages, _, _ = _parse({"model": "m", "input": [
        {"role": "user", "content": "hi"},
        {"type": "message", "role": "assistant", "content": []}]})
    assert messages == [{"role": "user", "content": "hi"}]


@pytest.mark.parametrize("value", ["abc", True, -1, 1.5])
def test_max_output_tokens_must_be_a_positive_integer(value):
    with pytest.raises(UnsupportedRequestField, match="max_output_tokens"):
        _parse({"model": "m", "input": "x", "max_output_tokens": value})


def test_a_hosted_tool_choice_is_refused():
    with pytest.raises(UnsupportedRequestField, match="tool_choice"):
        _parse({"model": "m", "input": "x", "tool_choice": {"type": "web_search"}})


def test_stateless_and_private_fields_never_become_params():
    _, _, _, params = _parse({
        "model": "m", "input": "x", "store": False,
        "include": ["reasoning.encrypted_content"],
        "client_metadata": {"session_id": "s-1", "installation_id": "inst-9"},
        "metadata": {"k": "v"}, "batch_topic": "bulk", "_auth_admin": True,
        "x_route_to": "gpt-4o", "stream_options": {"include_obfuscation": False},
    })
    for name in ("store", "include", "client_metadata", "metadata", "batch_topic",
                 "_auth_admin", "x_route_to", "stream_options", "input", "instructions"):
        assert name not in params, name


def test_an_operator_admitted_field_passes():
    _, _, _, params = _parse({"model": "m", "input": "x", "seed": 7, "mystery": 1},
                             extra_allowed=("seed",))
    assert params["seed"] == 7 and "mystery" not in params


@pytest.mark.parametrize("body,field", [
    ({"previous_response_id": "resp_1"}, "previous_response_id"),
    ({"conversation": "conv_1"}, "conversation"),
    ({"background": True}, "background"),
    ({"prompt": {"id": "pmpt_1"}}, "prompt"),
    ({"truncation": "auto"}, "truncation"),
    ({"top_logprobs": 3}, "top_logprobs"),
    ({"input": [{"type": "item_reference", "id": "msg_1"}]}, "input[0]"),
    ({"input": [{"type": "web_search_call", "id": "ws_1"}]}, "input[0].type"),
    ({"input": [{"role": "user", "content": [{"type": "input_file", "file_id": "f"}]}]},
     "input[0].content[0]"),
    ({"input": [{"role": "user", "content": [{"type": "input_image", "file_id": "f"}]}]},
     "input[0].content[0]"),
    ({"input": [{"type": "function_call", "call_id": "c", "name": "f", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "c",
                 "output": [{"type": "input_image", "image_url": "data:x"}]}]},
     "input[1].output"),
    ({"input": 7}, "input"),
    ({"input": []}, "input"),
    ({"input": [{"role": "wizard", "content": "x"}]}, "input[0].role"),
    ({"text": {"format": {"type": "xml"}}}, "text.format.type"),
])
def test_what_it_cannot_honour_is_refused_naming_the_field(body, field):
    req = {"model": "m", "input": "x", **body}
    with pytest.raises(UnsupportedRequestField) as exc:
        _parse(req)
    assert field in str(exc.value)


def test_null_stateful_fields_are_fine():
    _parse({"model": "m", "input": "x", "previous_response_id": None, "background": False,
            "truncation": "disabled", "conversation": None, "top_logprobs": 0})


# ── privacy: headers the pipeline never sees ───────────────────────────────────

def test_client_identifying_headers_are_kept_from_the_pipeline():
    headers = {"Authorization": "Bearer k", "originator": "codex_exec",
               "User-Agent": "codex_exec/0.160.1", "session-id": "s", "thread-id": "t",
               "x-client-request-id": "r", "x-codex-window-id": "w",
               "x-codex-turn-metadata": "{}", "x-codex-beta-features": "remote_compaction_v2",
               "X-Agentic-Run-Id": "run-1", "traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01",
               "X-Team": "payments"}
    headers.update({"conversation-id": "c", "conversation_id": "c", "chatgpt-account-id": "a",
                    "openai-organization": "o", "openai-project": "p",
                    "x-openai-subagent": "s", "x-stainless-os": "Linux"})
    kept = RESPONSES.pipeline_headers(headers)
    assert {k.lower() for k in kept} == {"authorization", "x-agentic-run-id", "traceparent",
                                         "x-team"}


def test_the_other_adapters_keep_every_header():
    from protocols import ANTHROPIC
    assert ANTHROPIC.pipeline_headers({"user-agent": "x"}) is None


# ── response: stream ───────────────────────────────────────────────────────────

_USAGE = {"prompt_tokens": 1200, "completion_tokens": 40, "total_tokens": 1240,
          "prompt_tokens_details": {"cached_tokens": 1000},
          "completion_tokens_details": {"reasoning_tokens": 12}}


def test_a_text_stream_emits_the_full_responses_event_sequence():
    proto, *_ = _parse({"model": "m", "input": "x", "stream": True})
    events = _run_stream(proto, [
        {"id": "c1", "model": "gpt-4o-mini-2024", "choices": [{"delta": {"content": "Hel"}}]},
        {"id": "c1", "choices": [{"delta": {"content": "lo"}}]},
        {"id": "c1", "choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"id": "c1", "choices": [], "usage": _USAGE},
    ])
    assert [e for e, _ in events] == [
        "response.created", "response.in_progress",
        "response.output_item.added", "response.content_part.added",
        "response.output_text.delta", "response.output_text.delta",
        "response.output_text.done", "response.content_part.done",
        "response.output_item.done", "response.completed",
    ]
    assert [d["sequence_number"] for _, d in events] == list(range(len(events)))
    created = events[0][1]["response"]
    assert created["status"] == "in_progress" and created["id"] == "resp_req1"
    item = events[-2][1]["item"]
    assert item["type"] == "message" and item["role"] == "assistant"
    assert item["content"] == [{"type": "output_text", "text": "Hello", "annotations": [],
                                "logprobs": []}]
    done = events[-1][1]["response"]
    assert done["id"] == "resp_req1" and done["status"] == "completed"
    assert done["model"] == "gpt-4o-mini-2024"
    assert done["output"] == [item]
    assert done["usage"] == {
        "input_tokens": 1200, "input_tokens_details": {"cached_tokens": 1000},
        "output_tokens": 40, "output_tokens_details": {"reasoning_tokens": 12},
        "total_tokens": 1240}
    assert done["store"] is False


def test_a_custom_tool_call_streams_back_as_a_custom_tool_call_item():
    proto, *_ = _parse({"model": "m", "input": "x", "tools": [_EXEC_TOOL, _PATCH_TOOL]})
    args = json.dumps({"input": _PATCH})
    events = _run_stream(proto, [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_p", "type": "function",
                                                "function": {"name": "apply_patch",
                                                             "arguments": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0,
                                                "function": {"arguments": args[:20]}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0,
                                                "function": {"arguments": args[20:]}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": _USAGE},
    ])
    names = [e for e, _ in events]
    assert names == ["response.created", "response.in_progress",
                     "response.output_item.added", "response.custom_tool_call_input.delta",
                     "response.custom_tool_call_input.done", "response.output_item.done",
                     "response.completed"]
    item = events[5][1]["item"]
    assert item == {"type": "custom_tool_call", "id": item["id"], "call_id": "call_p",
                    "name": "apply_patch", "input": _PATCH, "status": "completed"}
    assert events[4][1]["input"] == _PATCH
    assert events[-1][1]["response"]["output"] == [item]


def test_function_calls_stream_back_as_function_call_items_after_the_text():
    proto, *_ = _parse({"model": "m", "input": "x", "tools": [_EXEC_TOOL, _PATCH_TOOL]})
    events = _run_stream(proto, [
        {"choices": [{"delta": {"content": "Running."}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "function": {"name": "exec_command",
                                                      "arguments": '{"cmd":'}},
            {"index": 1, "id": "call_2", "function": {"name": "exec_command",
                                                      "arguments": '{"cmd":"pwd"}'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0,
                                                "function": {"arguments": '"ls"}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ])
    done = [d["item"] for e, d in events if e == "response.output_item.done"]
    assert [i["type"] for i in done] == ["message", "function_call", "function_call"]
    assert done[1] == {"type": "function_call", "id": done[1]["id"], "call_id": "call_1",
                       "name": "exec_command", "arguments": '{"cmd":"ls"}',
                       "status": "completed"}
    assert done[2]["call_id"] == "call_2" and done[2]["arguments"] == '{"cmd":"pwd"}'
    assert [d["output_index"] for e, d in events if e == "response.output_item.done"] == [0, 1, 2]
    assert "response.function_call_arguments.done" in [e for e, _ in events]
    assert events[-1][0] == "response.completed"
    assert len({i["id"] for i in done}) == 3


def test_a_truncated_answer_ends_incomplete():
    proto, *_ = _parse({"model": "m", "input": "x"})
    events = _run_stream(proto, [{"choices": [{"delta": {"content": "part"},
                                               "finish_reason": "length"}]}])
    assert events[-1][0] == "response.incomplete"
    resp = events[-1][1]["response"]
    assert resp["status"] == "incomplete"
    assert resp["incomplete_details"] == {"reason": "max_output_tokens"}


def test_a_content_filter_block_completes_so_the_client_does_not_retry_it():
    # A G29/G30/G31 block is a served refusal (finish_reason content_filter). Codex treats
    # response.incomplete as a retryable stream error; each retry would be a billed block.
    proto, *_ = _parse({"model": "m", "input": "x"})
    events = _run_stream(proto, [{"choices": [{"delta": {"content": "Blocked."},
                                               "finish_reason": "content_filter"}]}])
    assert events[-1][0] == "response.completed"
    assert events[-1][1]["response"]["incomplete_details"] is None
    resp = proto.serialise_response({"choices": [{"finish_reason": "content_filter",
                                                  "message": {"content": "Blocked."}}]})
    assert resp["status"] == "completed"


def test_parallel_calls_that_reuse_an_index_stay_apart():
    proto, *_ = _parse({"model": "m", "input": "x", "tools": [_EXEC_TOOL]})
    events = _run_stream(proto, [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {
            "name": "exec_command", "arguments": '{"cmd":"ls"}'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_2", "function": {
            "name": "exec_command", "arguments": '{"cmd":"pwd"}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ])
    done = [d["item"] for e, d in events if e == "response.output_item.done"]
    assert [(i["call_id"], i["arguments"]) for i in done] == [
        ("call_1", '{"cmd":"ls"}'), ("call_2", '{"cmd":"pwd"}')]


def test_an_upstream_failure_ends_the_stream_failed_never_silently():
    proto, *_ = _parse({"model": "m", "input": "x"})
    events = _run_stream(proto, [{"choices": [{"delta": {"content": "par"}}]}],
                         fail="LLM provider error (upstream call failed)")
    assert events[-1][0] == "response.failed"
    resp = events[-1][1]["response"]
    assert resp["status"] == "failed"
    assert resp["error"] == {"code": "server_error",
                             "message": "LLM provider error (upstream call failed)"}
    assert "response.completed" not in [e for e, _ in events]


def test_a_rate_limited_upstream_fails_with_the_rate_limit_code():
    proto, *_ = _parse({"model": "m", "input": "x"})
    events = _run_stream(proto, [], fail="LLM provider rate limit reached")
    assert events[-1][1]["response"]["error"]["code"] == "rate_limit_exceeded"


def test_reasoning_text_becomes_a_reasoning_item_first():
    proto, *_ = _parse({"model": "m", "input": "x"})
    events = _run_stream(proto, [
        {"choices": [{"delta": {"reasoning_content": "Think"}}]},
        {"choices": [{"delta": {"reasoning_content": "ing."}}]},
        {"choices": [{"delta": {"content": "Answer"}, "finish_reason": "stop"}]},
    ])
    done = [d["item"] for e, d in events if e == "response.output_item.done"]
    assert [i["type"] for i in done] == ["reasoning", "message"]
    assert done[0]["content"] == [{"type": "reasoning_text", "text": "Thinking."}]
    assert done[0]["summary"] == []
    assert [e for e, _ in events].count("response.reasoning_text.delta") == 2


def test_an_empty_stream_still_completes_with_no_output():
    proto, *_ = _parse({"model": "m", "input": "x"})
    events = _run_stream(proto, [])
    assert [e for e, _ in events] == ["response.created", "response.in_progress",
                                      "response.completed"]
    assert events[-1][1]["response"]["output"] == []


# ── response: whole ────────────────────────────────────────────────────────────

def test_a_whole_completion_becomes_a_response_object():
    proto, *_ = _parse({"model": "m", "input": "x", "tools": [_EXEC_TOOL, _PATCH_TOOL],
                        "metadata": {"k": "v"}})
    resp = proto.serialise_response({
        "id": "chatcmpl-1", "model": "claude-sonnet-4-5",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": "Patching.",
            "tool_calls": [
                {"id": "call_p", "type": "function",
                 "function": {"name": "apply_patch", "arguments": json.dumps({"input": _PATCH})}},
                {"id": "call_e", "type": "function",
                 "function": {"name": "exec_command", "arguments": '{"cmd":"ls"}'}}]}}],
        "usage": _USAGE,
    })
    assert resp["object"] == "response" and resp["id"] == "resp_req1"
    assert resp["status"] == "completed" and resp["model"] == "claude-sonnet-4-5"
    assert [i["type"] for i in resp["output"]] == ["message", "custom_tool_call", "function_call"]
    assert resp["output"][1]["input"] == _PATCH
    assert resp["output"][2]["arguments"] == '{"cmd":"ls"}'
    assert resp["usage"]["input_tokens_details"]["cached_tokens"] == 1000
    assert resp["metadata"] == {"k": "v"}
    assert resp["tools"] == [_EXEC_TOOL, _PATCH_TOOL]


def test_a_custom_call_whose_arguments_are_not_the_wrapper_is_passed_raw():
    proto, *_ = _parse({"model": "m", "input": "x", "tools": [_PATCH_TOOL]})
    resp = proto.serialise_response({"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None, "tool_calls": [
            {"id": "c", "type": "function",
             "function": {"name": "apply_patch", "arguments": "*** Begin Patch"}}]}}]})
    assert resp["output"] == [{"type": "custom_tool_call", "id": resp["output"][0]["id"],
                               "call_id": "c", "name": "apply_patch",
                               "input": "*** Begin Patch", "status": "completed"}]


def test_cache_write_tokens_are_reported_when_the_provider_gives_them():
    proto, *_ = _parse({"model": "m", "input": "x"})
    resp = proto.serialise_response({"choices": [{"finish_reason": "stop",
                                                  "message": {"content": "ok"}}],
                                     "usage": {"prompt_tokens": 10, "completion_tokens": 2,
                                               "cache_creation_input_tokens": 8,
                                               "cache_read_input_tokens": 0}})
    assert resp["usage"]["input_tokens_details"] == {"cached_tokens": 0,
                                                     "cache_write_tokens": 8}
    assert resp["usage"]["total_tokens"] == 12


def test_errors_use_the_openai_envelope():
    body, status = RESPONSES.serialise_error(429, "slow down", "rate_limit_exceeded")
    assert status == 429
    assert body == {"error": {"message": "slow down", "type": "rate_limit_error",
                              "code": "rate_limit_exceeded", "param": None}}
    body, _ = RESPONSES.serialise_error(400, "bad")
    assert body["error"]["type"] == "invalid_request_error"
    body, _ = RESPONSES.serialise_error(502, "upstream")
    assert body["error"]["type"] == "server_error"
