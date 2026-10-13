"""Minimal Chat Completions client; optional SSE and no automatic retries."""

import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
import math
import re
import ssl
import urllib.error
import urllib.request

from .config import redact
from .metrics import MeasuredResponse, Measurements, RequestMeasurement

MAX_RESPONSE = 2 * 1024 * 1024
MAX_TOOL_CALLS = 16
MAX_RETRY_AFTER = 24 * 60 * 60


class ProviderError(Exception):
    def __init__(self, message, *, status=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def retry_after_seconds(value, *, now=None):
    """Parse a bounded server hint, never schedule or automatically retry a POST."""
    if (not isinstance(value, str) or len(value) > 128
            or any(ord(char) < 32 or ord(char) > 126 for char in value)):
        return None
    value = value.strip()
    if re.fullmatch(r"[0-9]{1,5}", value):
        seconds = int(value)
    else:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None or date.utcoffset().total_seconds() != 0:
                return None
            seconds = max(0, math.ceil((date - (now or datetime.now(timezone.utc))).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return None
    return seconds if seconds <= MAX_RETRY_AFTER else None


def http_failure(status, headers, secret=""):
    """Expose fixed diagnostics and numeric delay only; never server error text."""
    hints = {
        400: "request rejected; check model and Chat Completions tool compatibility",
        401: "authentication failed; check the configured API key",
        403: "access denied; check account permissions and model access",
        404: "endpoint or model not found; check --base-url and --model",
        405: "Chat Completions POST not accepted; check --base-url",
        408: "service request timed out; check provider status before retrying manually",
        413: "request too large for the service; review /context or reduce --max-context-bytes",
        422: "request rejected; check model and Chat Completions tool compatibility",
        429: "rate limit or quota reached; check service limits",
        504: "service request timed out; check provider status before retrying manually",
    }
    hint = hints.get(status, "service failed; check provider status before retrying manually"
                     if 500 <= status <= 599 else "check endpoint, model, or account settings")
    delay = None
    if status in {429, 503} and headers is not None:
        # Multiple hints are ambiguous; HTTPMessage preserves repeated fields.
        values = headers.get_all("Retry-After", []) if hasattr(headers, "get_all") else [headers.get("Retry-After")]
        if len(values) == 1:
            delay = retry_after_seconds(values[0])
    message = f"provider HTTP {status}; {hint}; no automatic retry"
    if delay is not None:
        message += f"; server suggests waiting {delay} seconds before a manual retry"
    return ProviderError(redact(message, secret), status=status, retry_after=delay)


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
    # Compatible APIs may explicitly return null for an absent optional array.
    if raw_calls is None:
        raw_calls = []
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
        self.measurements = Measurements()

    def complete(self, messages, tools, on_text=None):
        measurement = RequestMeasurement(self.config.stream)
        try:
            result = self._complete(messages, tools, on_text, measurement)
            measurement.outcome = "success"
            return result
        except KeyboardInterrupt:
            measurement.outcome = "cancelled"
            raise
        finally:
            self.measurements.add(measurement.finish())

    def _complete(self, messages, tools, on_text, measurement):
        config = self.config
        from .context import request_body
        body = request_body(config, messages, tools)
        if len(body) > config.max_context_bytes:
            raise ProviderError("context byte budget exceeded; request was not sent")
        headers = {"Content-Type": "application/json", "User-Agent": "sprig-agent/0.1.0"}
        if config.stream:
            headers["Accept"] = "text/event-stream"
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        request = urllib.request.Request(config.base_url + "/chat/completions", body, headers)
        measurement.begin(len(body))
        try:
            with self.opener.open(request, timeout=config.timeout) as response:
                status = getattr(response, "status", None)
                if type(status) is int and 100 <= status <= 599:
                    measurement.status = status
                reader = MeasuredResponse(response, measurement)
                if config.stream:
                    from .streaming import complete_stream
                    return complete_stream(reader, on_text, config.api_key,
                                           on_usage=measurement.observe_usage, on_delta=measurement.text_received)
                raw = reader.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ProviderError("provider response exceeds 2 MiB")
            data = json.loads(raw)
            if isinstance(data, dict):
                measurement.observe_usage(data.get("usage"))
            if (not isinstance(data, dict) or not isinstance(data.get("choices"), list)
                    or not data["choices"] or not isinstance(data["choices"][0], dict)):
                raise ProviderError("invalid Chat Completions choices")
            choice = data["choices"][0]
            if choice.get("finish_reason") not in {"stop", "tool_calls"}:
                raise ProviderError("provider stopped before a complete response")
            return redact(validate_message(choice["message"]), config.api_key)
        except urllib.error.HTTPError as exc:
            if type(exc.code) is int and 100 <= exc.code <= 599:
                measurement.status = exc.code
            # Never echo response bodies: gateways can include request credentials.
            try:
                failure = http_failure(exc.code, exc.headers, config.api_key)
            finally:
                exc.close()
            raise failure from None
        except (urllib.error.URLError, TimeoutError, OSError, HTTPException) as exc:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            if isinstance(reason, ssl.SSLCertVerificationError):
                hint = "TLS certificate verification failed; check the trusted endpoint and local certificates"
            elif isinstance(reason, TimeoutError):
                hint = "socket timed out; check connectivity or --timeout"
            else:
                hint = "connection or HTTP response failed; check endpoint, network, or proxy"
            raise ProviderError(redact(f"provider {hint}; no automatic retry", config.api_key)) from None
        except (KeyError, IndexError, TypeError, ValueError, RecursionError):
            raise ProviderError("invalid Chat Completions response") from None
