"""OpenAI Responses API (``POST /v1/responses``) ↔ the internal chat shape.

A client that speaks only the Responses API (an agent CLI, the OpenAI SDK's
``client.responses``) reaches every configured provider through the same pipeline as the
other routes: the request is translated to chat messages and function tools on the way in,
and the chat completion, or its stream, is re-serialised as a Response object, or as
Responses SSE events, on the way out. Stateless: no response is stored, so
``previous_response_id`` and ``GET /v1/responses/{id}`` are refused; a client resends the
whole history each turn.

Request mapping:

* ``instructions`` → a system message; ``message`` items (role ``developer`` → system,
  ``user``, ``assistant``, ``system``) with ``input_text``/``output_text``/``refusal``/
  ``input_image`` parts → chat messages (all-text parts join into one string);
* ``function_call`` → the assistant's ``tool_calls`` (``call_id`` kept as the id, parallel
  calls in one assistant message); ``function_call_output`` → a ``tool`` message;
* ``custom_tool_call`` / ``_output`` → the same, the free-form ``input`` wrapped as the
  one string argument ``{"input": ...}``;
* ``reasoning`` items → dropped (their encrypted content is readable only by the model
  that wrote it);
* ``function`` tools → function tools; ``custom`` tools → a function with one string
  parameter ``input`` whose description carries the tool's grammar, the name remembered so
  the reply comes back as a ``custom_tool_call`` item, streamed or not;
* ``text.format`` → ``response_format``; ``text.verbosity`` → ``verbosity``;
  ``reasoning.effort`` → ``reasoning_effort``; ``max_output_tokens`` → ``max_tokens``;
  ``tool_choice`` and ``parallel_tool_calls`` (only with tools), ``temperature``, ``top_p``,
  ``prompt_cache_key`` and the other chat parameters of the same name → as they are.

Anything it cannot honour is refused with a 400 naming the field (``UnsupportedRequestField``);
``store``, ``include``, ``metadata`` and ``client_metadata`` are accepted and never sent
upstream; any other field is dropped and its name logged, unless the operator admits it
(``ingress.extra_allowed_params``).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

from protocols.base import (
    INTERNAL_PARAM_PREFIX, IngressProtocol, StreamTranslator, UnsupportedRequestField,
    _log_dropped, safe_json_dumps,
)

logger = logging.getLogger(__name__)

# Body fields carried as the chat parameter of the same name. (``parallel_tool_calls`` and
# ``tool_choice`` are carried only with tools: a chat provider rejects them alone.)
_DIRECT_PARAMS = ("temperature", "top_p", "prompt_cache_key", "prompt_cache_retention",
                  "safety_identifier", "user", "service_tier")
# Body fields the translation reads (not copied as params).
_TRANSLATED = frozenset({"model", "input", "instructions", "tools", "tool_choice", "text",
                         "reasoning", "max_output_tokens", "stream", "parallel_tool_calls"})
# Accepted and never forwarded: stateless (``store``), output options that only a native
# Responses upstream could honour (``include``, ``stream_options``), the caller's own
# labels (``metadata``, echoed back, and ``client_metadata``, which carries session and
# installation ids), and a limit on built-in tools this route never runs.
_NOT_FORWARDED = frozenset({"store", "include", "metadata", "client_metadata",
                            "stream_options", "max_tool_calls"})


def _is_set(value: Any) -> bool:
    return value is not None and value is not False and value != 0


# Fields refused when set to anything but their default: each needs state or a feature this
# stateless translation does not have. (field → why, as the 400 says it)
_REFUSED_WHEN_SET = {
    "previous_response_id": "this proxy is stateless and stores no responses; resend the "
                            "whole conversation in input",
    "conversation": "this proxy is stateless and stores no conversations; resend the whole "
                    "conversation in input",
    "background": "background responses need stored responses, which this proxy does not keep",
    "prompt": "stored prompt templates are not supported; send instructions and input",
    "top_logprobs": "log probabilities are not returned by this proxy",
}
_KNOWN = _TRANSLATED | frozenset(_DIRECT_PARAMS) | _NOT_FORWARDED | frozenset(
    _REFUSED_WHEN_SET) | {"truncation"}

# Request headers a Responses client sends that identify its user, machine, session or
# thread. They are never needed to serve the request, so the pipeline never sees them:
# nothing downstream can log, match on or forward them.
_PRIVATE_HEADERS = frozenset({"originator", "user-agent", "session-id", "session_id",
                              "thread-id", "thread_id", "conversation-id", "conversation_id",
                              "x-client-request-id", "chatgpt-account-id",
                              "openai-organization", "openai-project"})
_PRIVATE_HEADER_PREFIXES = ("x-codex-", "x-openai-", "x-stainless-")
DEFAULT_KEEPALIVE_SECONDS = 15.0
# An operator's own entries (``ingress.private_headers``) ending in this are prefixes.
_PREFIX_MARK = "*"

# What a tool output's image becomes: a chat provider takes no image in a tool message, and
# refusing it would make the whole session unservable (every later turn resends it).
IMAGE_OMITTED = "[image omitted: images in tool output are not supported by this gateway]"

_ROLES = {"user": "user", "assistant": "assistant", "system": "system", "developer": "system"}
_TEXT_PARTS = ("input_text", "output_text", "text")
_CUSTOM_INPUT = "input"   # the one parameter of a custom tool's function form
_ERROR_TYPE = {400: "invalid_request_error", 401: "authentication_error",
               403: "permission_error", 404: "invalid_request_error",
               413: "invalid_request_error", 422: "invalid_request_error",
               429: "rate_limit_error"}


def _refuse(message: str) -> UnsupportedRequestField:
    return UnsupportedRequestField(message)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# ── request ────────────────────────────────────────────────────────────────────

def _content(content: Any, where: str) -> Any:
    """Responses message content → chat content: one string when every part is text (the
    parts joined by a blank line), else a list of chat parts."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise _refuse(f"{where}.content must be a string or an array of content parts")
    parts: List[Dict[str, Any]] = []
    has_image = False
    for j, part in enumerate(content):
        pw = f"{where}.content[{j}]"
        if not isinstance(part, dict):
            raise _refuse(f"{pw} must be an object")
        ptype = part.get("type")
        if ptype in _TEXT_PARTS:
            parts.append({"type": "text", "text": str(part.get("text") or "")})
        elif ptype == "refusal":
            parts.append({"type": "text", "text": str(part.get("refusal") or "")})
        elif ptype == "input_image":
            url = part.get("image_url")
            if not isinstance(url, str) or not url:
                raise _refuse(f"{pw}: only an input_image with an image_url is supported "
                              "(a file_id needs the provider's file store)")
            image = {"url": url}
            if part.get("detail") in ("low", "high", "auto"):
                image["detail"] = part["detail"]
            parts.append({"type": "image_url", "image_url": image})
            has_image = True
        else:
            raise _refuse(f"{pw}.type {str(ptype)[:40]!r} is not supported by this proxy")
    if has_image:
        return parts
    return "\n\n".join(p["text"] for p in parts)


def _tool_output(output: Any, where: str) -> str:
    """A tool call's output as the tool message's text."""
    if isinstance(output, str):
        return output
    if output is None:
        return ""
    if isinstance(output, list):
        texts, images = [], 0
        for j, part in enumerate(output):
            ptype = part.get("type") if isinstance(part, dict) else None
            if ptype in _TEXT_PARTS:
                texts.append(str(part.get("text") or ""))
            elif ptype == "input_image":
                texts.append(IMAGE_OMITTED)
                images += 1
            else:
                raise _refuse(f"{where}[{j}]: only text and image parts are supported in a "
                              "tool output")
        if images:
            logger.info("Responses ingress: %d image part(s) in a tool output replaced with a "
                        "placeholder", images)
        return "\n\n".join(texts)
    return safe_json_dumps(output)


def _tool_call(item: Dict[str, Any], itype: str, where: str) -> Dict[str, Any]:
    call_id, name = item.get("call_id"), item.get("name")
    if not isinstance(call_id, str) or not call_id:
        raise _refuse(f"{where}.call_id must be a non-empty string")
    if not isinstance(name, str) or not name:
        raise _refuse(f"{where}.name must be a non-empty string")
    if itype == "function_call":
        args = item.get("arguments")
        args = args if isinstance(args, str) else safe_json_dumps(args)
    else:
        raw = item.get("input")
        if not isinstance(raw, str):
            raise _refuse(f"{where}.input must be a string")
        args = json.dumps({_CUSTOM_INPUT: raw})
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}


def _items_to_messages(items: List[Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    call_ids = set()
    for i, item in enumerate(items):
        where = f"input[{i}]"
        if not isinstance(item, dict):
            raise _refuse(f"{where} must be an object")
        itype = item.get("type") or ("message" if "role" in item else None)
        if itype == "message":
            role = item.get("role")
            if not isinstance(role, str) or role not in _ROLES:
                raise _refuse(f"{where}.role must be user, assistant, system or developer")
            if item.get("content") == []:
                continue   # nothing said: a provider rejects an empty text block
            out.append({"role": _ROLES[role], "content": _content(item.get("content"), where)})
        elif itype == "reasoning":
            continue
        elif itype in ("function_call", "custom_tool_call"):
            call = _tool_call(item, itype, where)
            last = out[-1] if out else None
            # One assistant turn: its text and every call it made, in one chat message.
            if last is not None and last.get("role") == "assistant":
                last.setdefault("tool_calls", []).append(call)
            else:
                out.append({"role": "assistant", "content": None, "tool_calls": [call]})
            call_ids.add(call["id"])
        elif itype in ("function_call_output", "custom_tool_call_output"):
            call_id = item.get("call_id")
            text = _tool_output(item.get("output"), f"{where}.output")
            if isinstance(call_id, str) and call_id in call_ids:
                out.append({"role": "tool", "tool_call_id": call_id, "content": text})
            else:
                # An answer to no call this history made: a provider rejects a dangling
                # tool message, so it degrades to text, as on the other ingress routes.
                out.append({"role": "user",
                            "content": f"[tool output {str(call_id)[:64]}]: {text}"})
        elif itype == "item_reference":
            raise _refuse(f"{where}: item_reference needs stored responses, and this proxy is "
                          "stateless; send the item itself")
        else:
            raise _refuse(f"{where}.type {str(itype)[:40]!r} is not supported by this proxy")
    return out


def _messages(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    instructions = body.get("instructions")
    if instructions is not None:
        if not isinstance(instructions, str):
            raise _refuse("instructions must be a string")
        if instructions:
            out.append({"role": "system", "content": instructions})
    inp = body.get("input")
    if isinstance(inp, str):
        out.append({"role": "user", "content": inp})
    elif isinstance(inp, list) and inp:
        out.extend(_items_to_messages(inp))
    else:
        raise _refuse("input must be a string or a non-empty array of items")
    if not any(_has_content(m) for m in out if m.get("role") != "system"):
        # Sent on, it reached the provider as messages=[] or an empty user message and came
        # back as a 502 or a failed stream.
        raise _refuse("input contains no content")
    return out


def _has_content(message: Dict[str, Any]) -> bool:
    """Whether a non-system chat message gives the model something to answer: text that is
    not blank, any content part, a tool call or a tool result."""
    if message.get("tool_calls") or message.get("role") == "tool":
        return True
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    return bool(content)


def _custom_as_function(tool: Dict[str, Any], where: str) -> Dict[str, Any]:
    """A custom (free-form input) tool as a function the model calls with one string. Its
    grammar goes into the parameter's description: a chat upstream cannot constrain the
    output to it, but the model needs it to write valid input."""
    fmt = tool.get("format")
    desc = "The complete input for this tool, as one string."
    if isinstance(fmt, dict) and fmt.get("type") == "grammar":
        syntax = str(fmt.get("syntax") or "lark")
        desc += f" It must match this {syntax} grammar:\n{fmt.get('definition') or ''}"
    elif fmt is not None and not (isinstance(fmt, dict) and fmt.get("type") in (None, "text")):
        raise _refuse(f"{where}.format must be of type text or grammar")
    fn: Dict[str, Any] = {"name": tool["name"], "parameters": {
        "type": "object",
        "properties": {_CUSTOM_INPUT: {"type": "string", "description": desc}},
        "required": [_CUSTOM_INPUT], "additionalProperties": False}}
    if tool.get("description") is not None:
        fn["description"] = tool["description"]
    return {"type": "function", "function": fn}


def _tools(tools: Any) -> Tuple[List[Dict[str, Any]], FrozenSet[str]]:
    """Responses tools → chat function tools, and the names declared custom."""
    if tools is None:
        return [], frozenset()
    if not isinstance(tools, list):
        raise _refuse("tools must be an array")
    out: List[Dict[str, Any]] = []
    custom, names = set(), set()
    for i, tool in enumerate(tools):
        where = f"tools[{i}]"
        if not isinstance(tool, dict):
            raise _refuse(f"{where} must be an object")
        ttype = tool.get("type")
        if ttype not in ("function", "custom"):
            raise _refuse(f"{where}.type {str(ttype)[:40]!r} is not supported by this proxy "
                          "(function and custom tools only)")
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            raise _refuse(f"{where}.name must be a non-empty string")
        if name in names:
            raise _refuse(f"tools: the name {name[:64]!r} is declared twice")
        names.add(name)
        if ttype == "custom":
            out.append(_custom_as_function(tool, where))
            custom.add(name)
            continue
        fn: Dict[str, Any] = {"name": name}
        if tool.get("description") is not None:
            fn["description"] = tool["description"]
        fn["parameters"] = tool.get("parameters") or {"type": "object", "properties": {}}
        if tool.get("strict") is not None:
            fn["strict"] = tool["strict"]
        out.append({"type": "function", "function": fn})
    return out, frozenset(custom)


def _tool_choice(choice: Any) -> Any:
    if choice in ("auto", "none", "required"):
        return choice
    if (isinstance(choice, dict) and choice.get("type") in ("function", "custom")
            and isinstance(choice.get("name"), str) and choice["name"]):
        return {"type": "function", "function": {"name": choice["name"]}}
    raise _refuse("tool_choice must be auto, none, required, or a function or custom tool "
                  "by name")


def _text_params(text: Any, params: Dict[str, Any]) -> None:
    if text is None:
        return
    if not isinstance(text, dict):
        raise _refuse("text must be an object")
    fmt = text.get("format")
    if fmt is not None:
        if not isinstance(fmt, dict):
            raise _refuse("text.format must be an object")
        ftype = fmt.get("type")
        if ftype == "json_schema":
            schema = fmt.get("schema")
            if not isinstance(schema, dict):
                raise _refuse("text.format.schema must be an object")
            spec: Dict[str, Any] = {"name": fmt.get("name") or "response", "schema": schema}
            if fmt.get("strict") is not None:
                spec["strict"] = fmt["strict"]
            if fmt.get("description"):
                spec["description"] = fmt["description"]
            params["response_format"] = {"type": "json_schema", "json_schema": spec}
        elif ftype == "json_object":
            params["response_format"] = {"type": "json_object"}
        elif ftype not in (None, "text"):
            raise _refuse(f"text.format.type {str(ftype)[:40]!r} is not supported (text, "
                          "json_object or json_schema)")
    if text.get("verbosity") is not None:
        params["verbosity"] = text["verbosity"]


def _refuse_stateful(body: Dict[str, Any]) -> None:
    for field, why in _REFUSED_WHEN_SET.items():
        if _is_set(body.get(field)):
            raise _refuse(f"{field}: {why}")
    if body.get("truncation") not in (None, "disabled"):
        raise _refuse("truncation: only disabled is supported (the proxy's own context "
                      "budget, G26, is configured by the operator)")


# ── response ───────────────────────────────────────────────────────────────────

def responses_usage(usage: Any) -> Optional[Dict[str, Any]]:
    """Chat usage → Responses usage (None when the provider reported none)."""
    if not isinstance(usage, dict) or not usage:
        return None
    prompt, completion = _int(usage.get("prompt_tokens")), _int(usage.get("completion_tokens"))
    pd = usage.get("prompt_tokens_details") or {}
    cd = usage.get("completion_tokens_details") or {}
    pd = pd if isinstance(pd, dict) else {}
    cd = cd if isinstance(cd, dict) else {}
    cached = _int(pd.get("cached_tokens") or usage.get("cache_read_input_tokens"))
    written = _int(pd.get("cache_creation_tokens") or usage.get("cache_creation_input_tokens"))
    details: Dict[str, int] = {"cached_tokens": cached}
    if written:
        details["cache_write_tokens"] = written
    return {"input_tokens": prompt, "input_tokens_details": details,
            "output_tokens": completion,
            "output_tokens_details": {"reasoning_tokens": _int(cd.get("reasoning_tokens"))},
            "total_tokens": _int(usage.get("total_tokens")) or prompt + completion}


def _status(finish: Any) -> Tuple[str, Optional[Dict[str, str]]]:
    """A finish reason as the Response status. Only a truncated answer is ``incomplete``. A
    content-filter finish (the proxy's own G29/G30/G31 block, served as a refusal message,
    or a provider's filter) completes: a client such as Codex retries an incomplete response
    as a stream error, and every retry of a block would be served and billed again."""
    if finish == "length":
        return "incomplete", {"reason": "max_output_tokens"}
    return "completed", None


def _unwrap_custom_input(arguments: Any) -> str:
    """The free-form input of a custom tool call from its function arguments: the ``input``
    string the function form asked for, else the one string argument, else the raw text."""
    if not isinstance(arguments, str):
        return safe_json_dumps(arguments)
    try:
        parsed = json.loads(arguments)
    except ValueError:
        return arguments
    if isinstance(parsed, dict):
        if isinstance(parsed.get(_CUSTOM_INPUT), str):
            return parsed[_CUSTOM_INPUT]
        strings = [v for v in parsed.values() if isinstance(v, str)]
        if len(parsed) == 1 and strings:
            return strings[0]
    return arguments


def _message_item(item_id: str, text: str, status: str = "completed") -> Dict[str, Any]:
    return {"type": "message", "id": item_id, "status": status, "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": [],
                         "logprobs": []}]}


def _reasoning_item(item_id: str, text: str) -> Dict[str, Any]:
    return {"type": "reasoning", "id": item_id, "summary": [],
            "content": [{"type": "reasoning_text", "text": text}]}


class ResponsesProtocol(IngressProtocol):
    """The Responses adapter. ``for_request`` gives the instance that serves one request:
    ``parse_request`` records what the reply needs (the custom tool names, the fields a
    Response object echoes), so the routes never parse on the shared instance."""

    name = "responses"
    admits_extra_params = True

    def __init__(self, request_id: str = "") -> None:
        base = re.sub(r"[^A-Za-z0-9]", "", request_id or "") or uuid.uuid4().hex
        self._base = base
        self._created = int(time.time())
        self._custom: FrozenSet[str] = frozenset()
        self._echo: Dict[str, Any] = {}

    def for_request(self, request_id: str) -> "ResponsesProtocol":
        return ResponsesProtocol(request_id)

    def pipeline_headers(self, headers: Any, extra: Iterable[Any] = ()) -> Optional[Dict[str, str]]:
        """The headers without the private ones: the built-in list plus the operator's
        ``extra`` names (``ingress.private_headers``; one ending in ``*`` is a prefix)."""
        if not hasattr(headers, "items"):
            return {}
        names, prefixes = set(_PRIVATE_HEADERS), list(_PRIVATE_HEADER_PREFIXES)
        for entry in extra or ():
            if not isinstance(entry, str) or not entry.strip(_PREFIX_MARK).strip():
                continue
            entry = entry.strip().lower()
            if entry.endswith(_PREFIX_MARK):
                prefixes.append(entry.rstrip(_PREFIX_MARK))
            else:
                names.add(entry)
        prefix_tuple = tuple(prefixes)
        return {k: v for k, v in headers.items()
                if str(k).lower() not in names and not str(k).lower().startswith(prefix_tuple)}

    def keepalive_seconds(self, config: Any) -> float:
        """``ingress.responses.keepalive_seconds`` (default 15; 0 switches it off). A
        custom tool call's input is sent whole when the model finishes it, which for a large
        patch can take minutes; without these comments Codex's stream idle timeout (or a
        load balancer's) cuts the stream and the turn is retried and billed again."""
        section = config.get("ingress") if isinstance(config, dict) else None
        section = section.get("responses") if isinstance(section, dict) else None
        value = section.get("keepalive_seconds") if isinstance(section, dict) else None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            return DEFAULT_KEEPALIVE_SECONDS
        return float(value)

    def scope_params(self, params: Dict[str, Any], tenant_id: str, config: Any) -> None:
        """Replace the client's ``prompt_cache_key`` with an opaque digest scoped to the
        tenant. Codex sets the key to its thread id; the digest keeps equality (the same
        thread gets the same key, so provider caching still works) without sending the id.
        ``ingress.forward_prompt_cache_key: true`` (global config, only a literal true)
        sends the client's value as it is."""
        key = params.get("prompt_cache_key")
        if key is None:
            return
        section = config.get("ingress") if isinstance(config, dict) else None
        if isinstance(section, dict) and section.get("forward_prompt_cache_key") is True:
            return
        seed = f"responses|{tenant_id or ''}|{key}".encode("utf-8")
        params["prompt_cache_key"] = hashlib.sha256(seed).hexdigest()[:32]

    # ── request ──
    def parse_request(self, body, headers=None, path_model="", extra_allowed=()):
        if not isinstance(body, dict):
            raise _refuse("the request body must be a JSON object")
        _refuse_stateful(body)
        model = body.get("model") or path_model or ""
        if not isinstance(model, str):
            raise _refuse("model must be a string")
        tools, custom = _tools(body.get("tools"))
        messages = _messages(body)
        params: Dict[str, Any] = {}
        choice = (_tool_choice(body["tool_choice"]) if body.get("tool_choice") is not None
                  else None)
        if tools:
            params["tools"] = tools
            if choice is not None:
                params["tool_choice"] = choice
            if body.get("parallel_tool_calls") is not None:
                params["parallel_tool_calls"] = body["parallel_tool_calls"]
        _text_params(body.get("text"), params)
        reasoning = body.get("reasoning")
        if reasoning is not None:
            if not isinstance(reasoning, dict):
                raise _refuse("reasoning must be an object")
            if reasoning.get("effort") is not None:
                params["reasoning_effort"] = reasoning["effort"]
        limit = body.get("max_output_tokens")
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
                raise _refuse("max_output_tokens must be a positive integer")
            params["max_tokens"] = limit
        if body.get("stream") is not None:
            if not isinstance(body["stream"], bool):
                raise _refuse("stream must be true or false")
            params["stream"] = body["stream"]
        for name in _DIRECT_PARAMS:
            if body.get(name) is not None:
                params[name] = body[name]
        allowed = frozenset(n for n in extra_allowed if isinstance(n, str))
        dropped = []
        for key in body:
            if key in _KNOWN:
                continue
            if (isinstance(key, str) and key in allowed
                    and not key.startswith(INTERNAL_PARAM_PREFIX)):
                params[key] = body[key]
            else:
                dropped.append(str(key))
        _log_dropped(dropped, "Responses")
        self._custom = custom
        self._echo = {
            "model": model,
            "instructions": body.get("instructions"),
            "tools": body.get("tools") or [],
            "tool_choice": body.get("tool_choice") if body.get("tool_choice") is not None
            else "auto",
            "parallel_tool_calls": body.get("parallel_tool_calls")
            if body.get("parallel_tool_calls") is not None else True,
            "text": body.get("text") or {"format": {"type": "text"}},
            "reasoning": body.get("reasoning") or {"effort": None, "summary": None},
            "temperature": body.get("temperature"),
            "top_p": body.get("top_p"),
            "max_output_tokens": body.get("max_output_tokens"),
            "metadata": body.get("metadata") if isinstance(body.get("metadata"), dict) else {},
        }
        return messages, model, params

    # ── response ──
    @property
    def response_id(self) -> str:
        return f"resp_{self._base}"

    def item_id(self, prefix: str, n: int) -> str:
        return f"{prefix}_{self._base}_{n}"

    def is_custom(self, name: str) -> bool:
        return name in self._custom

    def call_item(self, item_id_n: int, call_id: str, name: str, arguments: str,
                  status: str = "completed") -> Dict[str, Any]:
        """A finished tool call as its Responses item: ``custom_tool_call`` for a tool the
        request declared custom, else ``function_call``."""
        if self.is_custom(name):
            return {"type": "custom_tool_call", "id": self.item_id("ctc", item_id_n),
                    "call_id": call_id, "name": name,
                    "input": _unwrap_custom_input(arguments), "status": status}
        return {"type": "function_call", "id": self.item_id("fc", item_id_n),
                "call_id": call_id, "name": name, "arguments": arguments or "{}",
                "status": status}

    def response(self, status: str, output: List[Dict[str, Any]], usage: Any, model: str = "",
                 *, error: Optional[Dict[str, Any]] = None,
                 incomplete: Optional[Dict[str, Any]] = None,
                 skeleton: bool = False) -> Dict[str, Any]:
        """A Response object (the same shape streamed and whole). A ``skeleton`` (the
        interim ``response.created`` / ``response.in_progress``) leaves out the echoed
        ``instructions`` and ``tools``, the large part; the terminal event carries them."""
        echo = dict(self._echo)
        requested = echo.pop("model", "")
        if skeleton:
            echo["instructions"], echo["tools"] = None, []
        return {"id": self.response_id, "object": "response", "created_at": self._created,
                "status": status, "error": error, "incomplete_details": incomplete,
                "model": model or requested, "output": output, "usage": usage,
                "store": False, "previous_response_id": None, "background": False,
                "truncation": "disabled", **echo}

    def serialise_response(self, openai_response):
        choice = (openai_response.get("choices") or [{}])[0] or {}
        msg = choice.get("message") or {}
        status, incomplete = _status(choice.get("finish_reason"))
        output: List[Dict[str, Any]] = []
        reasoning = msg.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            output.append(_reasoning_item(self.item_id("rs", len(output)), reasoning))
        text = msg.get("content")
        if isinstance(text, str) and text:
            output.append(_message_item(self.item_id("msg", len(output)), text,
                                        "incomplete" if status == "incomplete" else "completed"))
        for n, tc in enumerate(msg.get("tool_calls") or []):
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            call_id = tc.get("id") or self.item_id("call", n)
            output.append(self.call_item(len(output), call_id, fn.get("name") or "",
                                         fn.get("arguments") or ""))
        return self.response(status, output, responses_usage(openai_response.get("usage")),
                             openai_response.get("model") or "", incomplete=incomplete)

    def serialise_error(self, status, message, code=""):
        etype = _ERROR_TYPE.get(status, "server_error" if status >= 500
                                else "invalid_request_error")
        return {"error": {"message": message, "type": etype, "code": code or None,
                          "param": None}}, status

    def stream_translator(self):
        return _ResponsesStream(self)


class _ResponsesStream(StreamTranslator):
    """Chat stream chunks → Responses SSE events.

    Nothing is held back that can be sent: text and reasoning stream live, each in its own
    output item (one open at a time; a switch closes the open one with its ``*.done`` events
    and ``response.output_item.done``). A tool call is announced
    (``response.output_item.added``) as soon as its name arrives, and a function call's
    arguments stream live as ``response.function_call_arguments.delta``; a custom tool call's
    input is JSON-wrapped on the wire, so it is unwrapped and sent whole when the stream ends
    (the route's keepalive comments cover a long one). Each call is closed at the end, in the
    order the calls began. Every item ends with ``response.output_item.done``, and the stream
    with ``response.completed`` (``response.incomplete`` for a truncated answer,
    ``response.failed`` for an upstream error), which carries the Response id and usage: a
    client that needs them (Codex builds items only from ``output_item.done`` and ends a turn
    only on a terminal event) never sees a stream close without one.
    """

    def __init__(self, proto: ResponsesProtocol) -> None:
        self._p = proto
        self._seq = 0
        self._model = ""
        self._usage: Any = None
        self._finish: Any = None
        self._next_index = 0                          # output_index of the next item
        self._done: Dict[int, Dict[str, Any]] = {}    # finished items by output_index
        self._open: Optional[Dict[str, Any]] = None   # the live text/reasoning item
        self._calls: List[Dict[str, Any]] = []        # tool calls, in the order they began
        self._slots: Dict[int, Dict[str, Any]] = {}   # stream index -> its current call

    def _event(self, etype: str, **data: Any) -> str:
        payload = {"type": etype, "sequence_number": self._seq, **data}
        self._seq += 1
        return f"event: {etype}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"

    def _reserve_index(self) -> int:
        index, self._next_index = self._next_index, self._next_index + 1
        return index

    def _output(self) -> List[Dict[str, Any]]:
        return [self._done[i] for i in sorted(self._done)]

    def start(self) -> Iterable[str]:
        # A skeleton: the request's instructions and tools (about 20 KB for Codex) are echoed
        # once, on the terminal event, not on every interim one.
        resp = self._p.response("in_progress", [], None, skeleton=True)
        yield self._event("response.created", response=resp)
        yield self._event("response.in_progress", response=resp)

    def _open_item(self, kind: str) -> Iterable[str]:
        if self._open is not None and self._open["kind"] == kind:
            return
        yield from self._close_open()
        index = self._reserve_index()
        if kind == "reasoning":
            item_id = self._p.item_id("rs", index)
            self._open = {"kind": kind, "id": item_id, "index": index, "text": ""}
            yield self._event("response.output_item.added", output_index=index,
                              item={**_reasoning_item(item_id, ""), "content": []})
            return
        item_id = self._p.item_id("msg", index)
        self._open = {"kind": kind, "id": item_id, "index": index, "text": ""}
        yield self._event("response.output_item.added", output_index=index, item={
            "type": "message", "id": item_id, "status": "in_progress", "role": "assistant",
            "content": []})
        yield self._event("response.content_part.added", item_id=item_id, output_index=index,
                          content_index=0, part={"type": "output_text", "text": "",
                                                 "annotations": [], "logprobs": []})

    def _close_open(self, status: str = "completed") -> Iterable[str]:
        item, self._open = self._open, None
        if item is None:
            return
        ref = {"item_id": item["id"], "output_index": item["index"], "content_index": 0}
        if item["kind"] == "reasoning":
            final = _reasoning_item(item["id"], item["text"])
            yield self._event("response.reasoning_text.done", **ref, text=item["text"])
        else:
            final = _message_item(item["id"], item["text"], status)
            yield self._event("response.output_text.done", **ref, text=item["text"],
                              logprobs=[])
            yield self._event("response.content_part.done", **ref,
                              part=final["content"][0])
        self._done[item["index"]] = final
        yield self._event("response.output_item.done", output_index=item["index"], item=final)

    def _announce(self, slot: Dict[str, Any]) -> Iterable[str]:
        """Open a tool call's item once its name is known; a function call's arguments so
        far go out as its first delta."""
        yield from self._close_open()   # the text before a call is finished
        index = self._reserve_index()
        slot["index"] = index
        slot["call_id"] = slot["id"] or self._p.item_id("call", index)
        slot["custom"] = self._p.is_custom(slot["name"])
        if slot["custom"]:
            slot["item_id"] = self._p.item_id("ctc", index)
            yield self._event("response.output_item.added", output_index=index, item={
                "type": "custom_tool_call", "id": slot["item_id"],
                "call_id": slot["call_id"], "name": slot["name"], "input": "",
                "status": "in_progress"})
            return
        slot["item_id"] = self._p.item_id("fc", index)
        yield self._event("response.output_item.added", output_index=index, item={
            "type": "function_call", "id": slot["item_id"], "call_id": slot["call_id"],
            "name": slot["name"], "arguments": "", "status": "in_progress"})
        if slot["args"]:
            yield self._args_delta(slot, slot["args"])

    def _args_delta(self, slot: Dict[str, Any], piece: str) -> str:
        return self._event("response.function_call_arguments.delta", item_id=slot["item_id"],
                           output_index=slot["index"], delta=piece)

    def chunk(self, openai_chunk: Dict[str, Any]) -> Iterable[str]:
        self._model = openai_chunk.get("model") or self._model
        if openai_chunk.get("usage"):
            self._usage = openai_chunk["usage"]
        choice = (openai_chunk.get("choices") or [{}])[0] or {}
        if choice.get("finish_reason"):
            self._finish = choice["finish_reason"]
        delta = choice.get("delta") or {}
        reasoning = delta.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            yield from self._open_item("reasoning")
            self._open["text"] += reasoning
            yield self._event("response.reasoning_text.delta", item_id=self._open["id"],
                              output_index=self._open["index"], content_index=0,
                              delta=reasoning)
        text = delta.get("content")
        if isinstance(text, str) and text:
            yield from self._open_item("message")
            self._open["text"] += text
            yield self._event("response.output_text.delta", item_id=self._open["id"],
                              output_index=self._open["index"], content_index=0, delta=text,
                              logprobs=[])
        for tc in delta.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            idx = tc.get("index") or 0  # `or 0` so an explicit null cannot key the dict
            slot = self._slots.get(idx)
            # A new id on an index whose call already has one is a new call (a provider that
            # numbers parallel calls all 0): never merge two calls' arguments.
            if slot is None or (tc.get("id") and slot["id"] and tc["id"] != slot["id"]):
                slot = {"id": "", "name": "", "args": "", "index": None}
                self._slots[idx] = slot
                self._calls.append(slot)
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name") and not slot["name"]:
                slot["name"] = fn["name"]
            piece = fn.get("arguments") if isinstance(fn.get("arguments"), str) else ""
            slot["args"] += piece
            if slot["index"] is None:
                if slot["name"]:
                    yield from self._announce(slot)     # sends the arguments so far
            elif piece and not slot["custom"]:
                yield self._args_delta(slot, piece)

    def _close_calls(self) -> Iterable[str]:
        for slot in self._calls:
            if slot["index"] is None:   # never named: nothing a client could run
                continue
            index = slot["index"]
            final = self._p.call_item(index, slot["call_id"], slot["name"], slot["args"])
            ref = {"item_id": slot["item_id"], "output_index": index}
            if slot["custom"]:
                yield self._event("response.custom_tool_call_input.delta", **ref,
                                  delta=final["input"])
                yield self._event("response.custom_tool_call_input.done", **ref,
                                  input=final["input"])
            else:
                yield self._event("response.function_call_arguments.done", **ref,
                                  name=final["name"], arguments=final["arguments"])
            self._done[index] = final
            yield self._event("response.output_item.done", output_index=index, item=final)

    def error(self, message: str) -> Iterable[str]:
        text = message if isinstance(message, str) else "upstream error"
        code = "rate_limit_exceeded" if "rate limit" in text.lower() else "server_error"
        # The text or reasoning so far is closed (incomplete) so the client can show it; a
        # tool call is never closed half-streamed, so nothing can run truncated arguments.
        yield from self._close_open("incomplete")
        resp = self._p.response("failed", self._output(), responses_usage(self._usage),
                                self._model, error={"code": code, "message": text})
        yield self._event("response.failed", response=resp)

    def finish(self) -> Iterable[str]:
        status, incomplete = _status(self._finish)
        yield from self._close_open("incomplete" if status == "incomplete" else "completed")
        yield from self._close_calls()
        resp = self._p.response(status, self._output(), responses_usage(self._usage),
                                self._model, incomplete=incomplete)
        yield self._event("response.completed" if status == "completed"
                          else "response.incomplete", response=resp)
