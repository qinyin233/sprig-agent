"""Stateless Messages / Responses adapters for the canonical, approved tool loop.

Only client function tools and text are accepted. Native reasoning is opaque
history, never tool input or terminal output. All transport limits are shared.
"""

import copy
import json

from .json_budget import encoded_size, JsonBudgetExceeded
from .provider import MAX_RESPONSE, MAX_TOOL_CALLS, ProviderError, completion_failure, validate_message

PATHS = {"chat_completions": "/chat/completions", "messages": "/messages", "responses": "/responses"}
MAX_ITEMS = 256


def _require(condition):
    if not condition:
        raise ProviderError("invalid native protocol response; no tools were executed")


def _text(value):
    _require(isinstance(value, str))
    return value


def _object_arguments(value):
    _require(isinstance(value, dict))
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _call(identity, name, arguments):
    return {"id": _text(identity), "type": "function", "function": {
        "name": _text(name), "arguments": _text(arguments)}}


def _blocks(protocol, items, *, partial=False):
    """Allowlist native items and derive the same canonical assistant intent."""
    _require(isinstance(items, list) and len(items) <= MAX_ITEMS)
    texts, calls, clean = [], [], []
    for item in items:
        _require(isinstance(item, dict))
        kind = item.get("type")
        if protocol == "messages":
            if kind == "text":
                block = {"type": kind, "text": _text(item.get("text"))}
                texts.append(block["text"])
            elif kind == "tool_use":
                arguments = _object_arguments(item.get("input"))
                calls.append(_call(item.get("id"), item.get("name"), arguments))
                block = {"type": kind, "id": item["id"], "name": item["name"], "input": copy.deepcopy(item["input"])}
            elif kind == "thinking":
                block = {"type": kind, "thinking": _text(item.get("thinking")), "signature": _text(item.get("signature"))}
                _require(bool(block["signature"]))
            elif kind == "redacted_thinking":
                block = {"type": kind, "data": _text(item.get("data"))}
            else:
                raise ProviderError("unsupported Messages content block; no tools were executed")
        else:
            _require(item.get("status") in (None, "completed"))
            if kind == "message":
                _require(item.get("role") == "assistant" and isinstance(item.get("content"), list))
                content = []
                for part in item["content"]:
                    _require(isinstance(part, dict))
                    if part.get("type") == "output_text":
                        text = _text(part.get("text"))
                        content.append({"type": "output_text", "text": text, "annotations": []})
                    elif part.get("type") == "refusal":
                        text = _text(part.get("refusal"))
                        content.append({"type": "refusal", "refusal": text})
                    else:
                        raise ProviderError("unsupported Responses message content; no tools were executed")
                    texts.append(text)
                # ResponseOutputMessage input requires a status alongside its
                # output_text/refusal content; EasyInputMessage is a different union.
                block = {"type": kind, "role": "assistant", "content": content, "status": "completed"}
                if item.get("phase") is not None:
                    _require(item["phase"] in ("commentary", "final_answer"))
                    block["phase"] = item["phase"]
            elif kind == "function_call":
                calls.append(_call(item.get("call_id"), item.get("name"), item.get("arguments")))
                block = {"type": kind, "call_id": item["call_id"], "name": item["name"], "arguments": item["arguments"]}
            elif kind == "reasoning":
                summary = item.get("summary", [])
                _require(isinstance(summary, list) and len(summary) <= MAX_ITEMS)
                block = {"type": kind, "summary": []}
                for part in summary:
                    _require(isinstance(part, dict) and part.get("type") == "summary_text")
                    block["summary"].append({"type": "summary_text", "text": _text(part.get("text"))})
                if item.get("encrypted_content") is not None:
                    block["encrypted_content"] = _text(item["encrypted_content"])
                if item.get("content") is not None:
                    _require(isinstance(item["content"], list) and len(item["content"]) <= MAX_ITEMS)
                    block["content"] = []
                    for part in item["content"]:
                        _require(isinstance(part, dict) and part.get("type") == "reasoning_text")
                        block["content"].append({"type": "reasoning_text", "text": _text(part.get("text"))})
            else:
                raise ProviderError("unsupported Responses output item; no tools were executed")
            if "id" in item:
                block["id"] = _text(item["id"])
            if item.get("status") is not None:
                block["status"] = item["status"]
        clean.append(block)
    _require(len(calls) <= MAX_TOOL_CALLS)
    result = {"role": "assistant", "content": "".join(texts) or None}
    if calls:
        result["tool_calls"] = calls
    return (result if partial else validate_message(result)), clean


def validate_state(state, message, protocol=None):
    """Validate saved metadata and prohibit a second, conflicting tool intent."""
    _require(isinstance(state, dict) and set(state) == {"protocol", "output"})
    kind = state["protocol"]
    _require(kind in ("messages", "responses") and (protocol is None or kind == protocol))
    try:
        encoded_size(state, max_bytes=MAX_RESPONSE)
        canonical, items = _blocks(kind, state["output"])
    except JsonBudgetExceeded:
        raise ProviderError("native state exceeds response limit", code="response_limit") from None
    _require(canonical == {key: message[key] for key in ("role", "content", "tool_calls") if key in message})
    return {"protocol": kind, "output": items}


def parse_response(protocol, data):
    _require(isinstance(data, dict))
    if protocol == "messages":
        _require(data.get("type") == "message" and data.get("role") == "assistant")
        reason = data.get("stop_reason")
        if reason not in ("end_turn", "tool_use", "stop_sequence"):
            raise completion_failure("length" if reason == "max_tokens" else "content_filter" if reason == "refusal" else reason)
        result, items = _blocks(protocol, data.get("content"))
        _require((reason == "tool_use") == bool(result.get("tool_calls")))
    else:
        if data.get("status") != "completed" or data.get("error") is not None or data.get("incomplete_details") is not None:
            details = data.get("incomplete_details")
            reason = details.get("reason") if isinstance(details, dict) else None
            raise completion_failure("length" if reason == "max_output_tokens" else reason)
        result, items = _blocks(protocol, data.get("output"))
    result["native_state"] = {"protocol": protocol, "output": items}
    return result


def payload(config, messages, tools, *, check_cancelled=None):
    """Build the exact wire payload used both for counting and sending."""
    protocol = config.wire_api
    from .image_inputs import wire_content
    definitions, history, system = [], [], []
    for tool in tools:
        function = tool["function"]
        if protocol == "messages":
            definitions.append({"name": function["name"], "description": function.get("description", ""),
                                "input_schema": function["parameters"]})
        else:
            definitions.append({"type": "function", "name": function["name"], "description": function.get("description", ""),
                                "parameters": function["parameters"], "strict": False})
    for message in messages:
        role = message["role"]
        content = wire_content(message, protocol, config.secrets, check_cancelled or (lambda: None))
        if protocol == "messages" and role == "system":
            system.append({"type": "text", "text": message["content"]})
            continue
        state = message.get("native_state")
        if state is not None:
            state = validate_state(state, message, protocol)
        if protocol == "messages":
            if role == "tool":
                blocks, role = [{"type": "tool_result", "tool_use_id": message["tool_call_id"], "content": message["content"]}], "user"
            elif state is not None:
                blocks = state["output"]
            else:
                blocks = (content if isinstance(content, list) else [{"type": "text", "text": content}]) if content else []
                for call in message.get("tool_calls", []):
                    arguments = json.loads(call["function"]["arguments"])
                    _require(isinstance(arguments, dict))
                    blocks.append({"type": "tool_use", "id": call["id"], "name": call["function"]["name"], "input": arguments})
            _require(role in ("user", "assistant") and bool(blocks))
            if history and history[-1]["role"] == role:
                history[-1]["content"].extend(blocks)
            else:
                history.append({"role": role, "content": list(blocks)})
        elif role == "tool":
            history.append({"type": "function_call_output", "call_id": message["tool_call_id"], "output": message["content"]})
        elif state is not None:
            history.extend(state["output"])
        else:
            if message.get("content"):
                history.append({"role": role, "content": content})
            for call in message.get("tool_calls", []):
                history.append({"type": "function_call", "call_id": call["id"], **call["function"]})
    body = {"model": config.model}
    if definitions:
        body.update(tools=definitions, tool_choice={"type": "auto"} if protocol == "messages" else "auto")
    if protocol == "messages":
        body.update(messages=history, system=system)
    else:
        body.update(input=history, store=False, include=["reasoning.encrypted_content"])
    body.update(config.generation_options())
    if config.stream:
        body["stream"] = True
    return body


def usage(protocol, value):
    if not isinstance(value, dict):
        return value
    result = {"prompt_tokens": value.get("input_tokens"), "completion_tokens": value.get("output_tokens"),
              "total_tokens": value.get("total_tokens")}
    if protocol == "messages" and result["prompt_tokens"] is not None:
        counts = [result["prompt_tokens"], value.get("cache_read_input_tokens", 0), value.get("cache_creation_input_tokens", 0)]
        result["prompt_tokens"] = sum(counts) if all(type(n) is int and n >= 0 for n in counts) else -1
    return result


def complete_stream(protocol, response, on_text, secrets, measurement):
    from .streaming import events, TextOutput
    from .config import redact
    output = TextOutput(on_text, secrets)
    def emit(text):
        _text(text)
        if text:
            measurement.text_received()
            output.write(text)
    if protocol == "messages":
        result = _messages_stream(events(response, completion_marker="message_stop"), emit, measurement)
    else:
        result = _responses_stream(events(response, completion_marker="response.completed"), emit, measurement)
    output.finish()
    return redact(result, secrets)


def _messages_stream(events, emit, measurement):
    message, active, fragments, stopped, counts = None, None, [], False, {}
    for raw in events:
        event = json.loads(raw)
        _require(isinstance(event, dict))
        kind = event.get("type")
        if kind == "ping":
            continue
        if kind == "error":
            raise ProviderError("Messages stream failed; no tools were executed")
        if kind == "message_start":
            _require(message is None)
            message = event.get("message")
            _require(isinstance(message, dict) and message.get("type") == "message"
                     and message.get("role") == "assistant" and message.get("content") == []
                     and message.get("stop_reason") is None)
            message = copy.deepcopy(message)
            counts = dict(message.get("usage") or {})
        else:
            _require(message is not None)
            if kind == "content_block_start":
                _require(not stopped and active is None and type(event.get("index")) is int
                         and event["index"] == len(message["content"]) and event["index"] < MAX_ITEMS)
                block = event.get("content_block")
                _require(isinstance(block, dict) and block.get("type") in ("text", "tool_use", "thinking", "redacted_thinking"))
                message["content"].append(copy.deepcopy(block))
                active, fragments = event["index"], []
                if block["type"] == "text":
                    emit(block.get("text"))
            elif kind in ("content_block_delta", "content_block_stop"):
                _require(not stopped and active is not None and type(event.get("index")) is int and event["index"] == active)
                block = message["content"][active]
                if kind == "content_block_stop":
                    if fragments:
                        _require(block.get("input") == {})
                        block["input"] = json.loads("".join(fragments))
                    active = None
                    continue
                delta = event.get("delta")
                _require(isinstance(delta, dict))
                pair = (block["type"], delta.get("type"))
                if pair == ("tool_use", "input_json_delta"):
                    fragments.append(_text(delta.get("partial_json")))
                elif pair in (("text", "text_delta"), ("thinking", "thinking_delta"), ("thinking", "signature_delta")):
                    field = {"text_delta": "text", "thinking_delta": "thinking", "signature_delta": "signature"}[delta["type"]]
                    text = _text(delta.get(field))
                    block[field] = _text(block.get(field, "")) + text
                    if field == "text":
                        emit(text)
                else:
                    raise ProviderError("unsupported Messages delta; no tools were executed")
            elif kind == "message_delta":
                _require(active is None)
                delta = event.get("delta")
                _require(isinstance(delta, dict) and set(delta) <= {"stop_reason", "stop_sequence"})
                if delta.get("stop_reason") is not None:
                    _require(not stopped)
                    message["stop_reason"] = delta["stop_reason"]
                    stopped = True
                new_counts = event.get("usage")
                if new_counts is not None:
                    _require(isinstance(new_counts, dict))
                    counts.update(new_counts)
            elif kind == "message_stop":
                _require(stopped and active is None)
                measurement.observe_usage(usage("messages", counts))
                result = parse_response("messages", message)
                measurement.observe_finish("tool_calls" if result.get("tool_calls") else "stop")
                return result
            else:
                raise ProviderError("unsupported Messages stream event; no tools were executed")
    raise ProviderError("Messages stream ended before message_stop", code="incomplete_stream")


def _responses_stream(events, emit, measurement):
    added, done, text, arguments = {}, {}, {}, {}
    for raw in events:
        event = json.loads(raw)
        _require(isinstance(event, dict))
        kind = event.get("type")
        if kind in ("error", "response.failed", "response.incomplete"):
            response = event.get("response")
            if isinstance(response, dict):
                measurement.observe_usage(usage("responses", response.get("usage")))
                parse_response("responses", response)  # Raises a bounded diagnostic.
            raise ProviderError("Responses stream failed; no tools were executed", code="incomplete_stream")
        if kind == "response.completed":
            response = event.get("response")
            if isinstance(response, dict):
                measurement.observe_usage(usage("responses", response.get("usage")))
            result = parse_response("responses", response)
            items = response["output"]
            _require(set(added) == set(done) and all(index < len(items) for index in done))
            for index, item in done.items():
                _require(_blocks("responses", [item], partial=True) == _blocks("responses", [items[index]], partial=True))
            for (index, part), fragments in text.items():
                _require(index < len(items) and items[index].get("type") == "message"
                         and part < len(items[index]["content"]))
                final = items[index]["content"][part]
                _require("".join(fragments) == final.get("text", final.get("refusal")))
            for index, fragments in arguments.items():
                _require(index < len(items) and items[index].get("type") == "function_call"
                         and "".join(fragments) == items[index].get("arguments"))
            measurement.observe_finish("tool_calls" if result.get("tool_calls") else "stop")
            return result
        if kind in ("response.created", "response.in_progress"):
            continue
        if not isinstance(kind, str):
            raise ProviderError("invalid Responses stream event")
        index = event.get("output_index")
        if kind.startswith(("response.output_item.", "response.output_text.", "response.refusal.", "response.function_call_arguments.")):
            _require(type(index) is int and 0 <= index < MAX_ITEMS)
        if kind == "response.output_item.added":
            _require(index not in added and index == len(added))
            item = event.get("item")
            _require(isinstance(item, dict) and item.get("type") in ("message", "function_call", "reasoning"))
            added[index] = item
        elif kind == "response.output_item.done":
            _require(index in added and index not in done)
            item = event.get("item")
            _require(isinstance(item, dict) and item.get("type") == added[index].get("type")
                     and item.get("id") == added[index].get("id"))
            done[index] = item
        elif kind in ("response.output_text.delta", "response.refusal.delta"):
            part = event.get("content_index")
            _require(index in added and index not in done and added[index].get("type") == "message"
                     and type(part) is int and 0 <= part < MAX_ITEMS and event.get("item_id") == added[index].get("id"))
            delta = _text(event.get("delta"))
            text.setdefault((index, part), []).append(delta)
            emit(delta)
        elif kind == "response.function_call_arguments.delta":
            _require(index in added and index not in done and added[index].get("type") == "function_call"
                     and event.get("item_id") == added[index].get("id"))
            arguments.setdefault(index, []).append(_text(event.get("delta")))
        elif kind.startswith(("response.content_part.", "response.output_text.done", "response.refusal.done",
                              "response.function_call_arguments.done", "response.reasoning_summary_", "response.reasoning_")):
            continue  # Final output items are authoritative for metadata, never partial reasoning.
        else:
            raise ProviderError("unsupported Responses stream event; no tools were executed")
    raise ProviderError("Responses stream ended before response.completed", code="incomplete_stream")
