"""Minimal, non-streaming Chat Completions client; no automatic retries."""

import json
from http.client import HTTPException
import urllib.error
import urllib.request

from .config import redact

MAX_RESPONSE = 2 * 1024 * 1024
MAX_TOOL_CALLS = 16


class ProviderError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProviderError("HTTP redirects are disabled; use the final trusted base URL")


def validate_message(message):
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ProviderError("invalid assistant message")
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise ProviderError("only text assistant content is supported")
    raw_calls = message.get("tool_calls", [])
    if not isinstance(raw_calls, list) or len(raw_calls) > MAX_TOOL_CALLS:
        raise ProviderError("invalid or too many tool calls")
    calls, ids = [], set()
    for call in raw_calls:
        if not isinstance(call, dict):
            raise ProviderError("invalid tool call")
        function = call.get("function")
        call_id = call.get("id")
        if (call.get("type") != "function" or not isinstance(call_id, str)
                or not call_id or call_id in ids or not isinstance(function, dict)
                or not isinstance(function.get("name"), str)
                or not isinstance(function.get("arguments"), str)):
            raise ProviderError("invalid or duplicate function call")
        ids.add(call_id)
        calls.append({"id": call_id, "type": "function", "function": {
            "name": function["name"], "arguments": function["arguments"]}})
    if not calls and not content:
        refusal = message.get("refusal")
        if not isinstance(refusal, str) or not refusal:
            raise ProviderError("provider returned neither text nor tool calls")
        content = refusal
    result = {"role": "assistant", "content": content}
    if calls:
        result["tool_calls"] = calls
    return result


class Provider:
    def __init__(self, config):
        self.config = config
        self.opener = urllib.request.build_opener(NoRedirect())

    def complete(self, messages, tools):
        config = self.config
        body = json.dumps({"model": config.model, "messages": redact(messages, config.api_key),
                           "tools": tools, "tool_choice": "auto"}).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": "sprig-agent/0.1.0"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        request = urllib.request.Request(config.base_url + "/chat/completions", body, headers)
        try:
            with self.opener.open(request, timeout=config.timeout) as response:
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ProviderError("provider response exceeds 2 MiB")
            data = json.loads(raw)
            if (not isinstance(data, dict) or not isinstance(data.get("choices"), list)
                    or not data["choices"] or not isinstance(data["choices"][0], dict)):
                raise ProviderError("invalid Chat Completions choices")
            choice = data["choices"][0]
            if choice.get("finish_reason") not in {"stop", "tool_calls"}:
                raise ProviderError("provider stopped before a complete response")
            return redact(validate_message(choice["message"]), config.api_key)
        except urllib.error.HTTPError as exc:
            # Never echo response bodies: gateways can include request credentials.
            raise ProviderError(f"provider HTTP {exc.code}; check endpoint, model, or credentials") from None
        except (urllib.error.URLError, TimeoutError, OSError, HTTPException):
            raise ProviderError("provider request failed or timed out; no automatic retry") from None
        except (KeyError, IndexError, TypeError, ValueError, RecursionError):
            raise ProviderError("invalid Chat Completions response") from None
