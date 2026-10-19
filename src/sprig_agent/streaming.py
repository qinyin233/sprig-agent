"""Bounded SSE decoding and Chat Completions delta assembly."""

import json
import re

from .config import redact
from .provider import MAX_RESPONSE, MAX_TOOL_CALLS, ProviderError, completion_failure, validate_message


def events(response, *, completion_marker="[DONE]"):
    """Read incrementally, including CR/LF, comments and multiline data fields."""
    buffer, data, total, first = b"", [], 0, True
    while True:
        chunk = response.read1(min(8192, MAX_RESPONSE - total + 1))
        if not chunk:
            raise ProviderError("provider stream ended before " + completion_marker + "; no tools were executed", code="incomplete_stream")
        total += len(chunk)
        if total > MAX_RESPONSE:
            raise ProviderError("provider response exceeds 2 MiB", code="response_limit")
        buffer += chunk
        while True:
            boundary = re.search(b"[\r\n]", buffer)
            if boundary is None or (buffer[boundary.start():] == b"\r"):
                break
            end = boundary.start()
            width = 2 if buffer[end:end + 2] == b"\r\n" else 1
            line, buffer = buffer[:end], buffer[end + width:]
            if first:
                line = line.removeprefix(b"\xef\xbb\xbf")
                first = False
            if not line:
                if data:
                    yield b"\n".join(data).decode("utf-8")
                    data = []
            else:
                field, separator, value = line.partition(b":")
                if field == b"data":
                    data.append(value.removeprefix(b" ") if separator else b"")


class _SecretOutput:
    """Withhold a possible key prefix so split deltas cannot leak the key."""

    def __init__(self, emit, secret):
        self.emit, self.secret, self.pending = emit, secret, ""

    def write(self, text):
        if not self.emit:
            return
        if not self.secret:
            if text:
                self.emit(text)
            return
        self.pending += text
        output = []
        while self.secret in self.pending:
            before, _, self.pending = self.pending.partition(self.secret)
            output.extend((before, "[REDACTED]"))
        hold = 0
        for length in range(1, min(len(self.secret), len(self.pending) + 1)):
            if self.pending.endswith(self.secret[:length]):
                hold = length
        if len(self.pending) > hold:
            output.append(self.pending[:-hold] if hold else self.pending)
            self.pending = self.pending[-hold:] if hold else ""
        if output:
            self.emit("".join(output))

    def finish(self):
        if self.emit and self.pending:
            self.emit(redact(self.pending, self.secret))
        self.pending = ""


class TextOutput:
    """Compose bounded prefix filters for all known credential values."""

    def __init__(self, emit, secret):
        values = secret if isinstance(secret, (list, tuple)) else (secret,)
        self.filters = []
        callback = emit
        for value in reversed(tuple(dict.fromkeys(value for value in values if value))):
            stage = _SecretOutput(callback, value)
            self.filters.insert(0, stage)
            callback = stage.write
        self.emit = callback

    def write(self, text):
        if self.emit:
            self.emit(text)

    def finish(self):
        for stage in self.filters:
            stage.finish()


def complete_stream(response, on_text, secret, *, on_usage=None, on_delta=None, on_finish=None, preserve_reasoning=False):
    output = TextOutput(on_text, secret)
    content, refusal, calls, finished = [], [], {}, None
    reasoning, reasoning_seen, reasoning_text = [], False, False
    for event in events(response):
        if event == "[DONE]":
            if finished is None:
                raise ProviderError("provider stopped before a complete response", code="incomplete_stream")
            if sorted(calls) != list(range(len(calls))):
                raise ProviderError("non-contiguous streamed tool call indices")
            message = {"role": "assistant", "content": "".join(content) or None}
            if calls:
                message["tool_calls"] = [calls[index] for index in sorted(calls)]
            if refusal:
                message["refusal"] = "".join(refusal)
            if preserve_reasoning and reasoning_seen:
                message["reasoning_content"] = "".join(reasoning) if reasoning_text else None
            result = redact(validate_message(message, preserve_reasoning=preserve_reasoning), secret)
            if not content and not calls and refusal:
                output.write("".join(refusal))
            output.finish()
            return result
        chunk = json.loads(event)
        if not isinstance(chunk, dict) or "error" in chunk:
            raise ProviderError("invalid or failed provider stream")
        choices = chunk.get("choices")
        if not isinstance(choices, list) or len(choices) > 1:
            raise ProviderError("invalid streamed choices")
        if not choices:  # Optional final usage chunk.
            if on_usage:
                on_usage(chunk.get("usage"))
            continue
        choice = choices[0]
        if not isinstance(choice, dict) or type(choice.get("index")) is not int or choice["index"] != 0:
            raise ProviderError("invalid streamed choice index")
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            raise ProviderError("invalid delta or data after stream completion")
        if finished is not None:
            # Some compatible providers keep index=0 in the final usage event.
            if not delta and choice.get("finish_reason") is None and isinstance(chunk.get("usage"), dict):
                if on_usage:
                    on_usage(chunk["usage"])
                continue
            raise ProviderError("invalid delta or data after stream completion")
        if delta.get("role") not in {None, "assistant"} or delta.get("function_call") is not None:
            raise ProviderError("only assistant text and function tools are supported")
        if preserve_reasoning and "reasoning_content" in delta:
            reasoning_seen = True
            text = delta["reasoning_content"]
            if text is not None:
                if not isinstance(text, str):
                    raise ProviderError("invalid streamed reasoning_content; expected text or null")
                reasoning_text = True
                reasoning.append(text)
        for field, target in (("content", content), ("refusal", refusal)):
            text = delta.get(field)
            if text is not None:
                if not isinstance(text, str):
                    raise ProviderError("only text assistant content is supported")
                target.append(text)
                if text and on_delta:
                    on_delta()
                if field == "content":
                    output.write(text)
        fragments = delta.get("tool_calls", [])
        if fragments is None:
            fragments = []
        if not isinstance(fragments, list) or len(fragments) > MAX_TOOL_CALLS:
            raise ProviderError("invalid or too many streamed tool calls")
        indices = set()
        for fragment in fragments:
            if not isinstance(fragment, dict):
                raise ProviderError("invalid streamed tool call")
            index = fragment.get("index")
            if type(index) is not int or not 0 <= index < MAX_TOOL_CALLS:
                raise ProviderError("invalid or duplicate streamed tool call index")
            if index in indices:
                # Gateways can coalesce an intent and its argument continuation
                # into one delta array. Only argument-only continuations may
                # repeat an index; a second identity/name remains ambiguous.
                continuation = fragment.get("function")
                if ("id" in fragment or not isinstance(continuation, dict)
                        or set(continuation) != {"arguments"}):
                    raise ProviderError("invalid or duplicate streamed tool call index")
            indices.add(index)
            call = calls.setdefault(index, {"function": {"name": "", "arguments": ""}})
            for field in ("id", "type"):
                value = fragment.get(field)
                if value is not None:
                    if not isinstance(value, str) or not value or (field in call and call[field] != value):
                        raise ProviderError("inconsistent streamed tool call metadata")
                    call[field] = value
            function = fragment.get("function", {})
            if not isinstance(function, dict):
                raise ProviderError("invalid streamed function")
            for field in ("name", "arguments"):
                value = function.get(field)
                if value is not None:
                    if not isinstance(value, str):
                        raise ProviderError("invalid streamed function fragment")
                    call["function"][field] += value
        reason = choice.get("finish_reason")
        if on_usage:
            on_usage(chunk.get("usage"))
        if reason is not None:
            if on_finish:
                on_finish(reason)
            if reason not in {"stop", "tool_calls"}:
                raise completion_failure(reason)
            if (reason == "tool_calls") != bool(calls):
                raise ProviderError("stream finish reason does not match tool calls")
            finished = reason
