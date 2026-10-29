"""Bounded three-protocol HTTP client; opt-in retries only for explicit HTTP failures."""

import json
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from http.client import HTTPException
import math
import random
import re
import ssl
import time
import urllib.error
import urllib.request

from .config import redact
from .json_budget import JsonBudgetExceeded
from .metrics import FAILURE_CODES, MeasuredResponse, Measurements, RequestMeasurement
from .transport import HTTPHandler, HTTPSHandler, IOBudget, RequestDeadline

MAX_RESPONSE = 2 * 1024 * 1024
MAX_TOOL_CALLS = 16
MAX_RETRY_AFTER = 24 * 60 * 60
RETRY_STATUSES = {429, 500, 502, 503, 504}


class ProviderError(Exception):
    def __init__(self, message, *, status=None, retry_after=None, code="invalid_response", retryable=False):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.code = code if isinstance(code, str) and code in FAILURE_CODES else "invalid_response"
        self.retryable = retryable


def completion_failure(reason):
    if reason == "length":
        return ProviderError("provider stopped before a complete response: output token limit reached; no tools were executed. "
                             "Use smaller edit proposals or explicitly raise --max-tokens; no automatic retry", code="output_limit")
    if reason == "content_filter":
        return ProviderError("provider stopped before a complete response: content filtered; no tools were executed", code="content_filter")
    return ProviderError("provider stopped before a complete response; no tools were executed", code="unsupported_finish_reason")


def retry_after_seconds(value, *, now=None):
    """Parse a bounded server hint without echoing untrusted header text."""
    if (not isinstance(value, str) or len(value) > 128
            or any(ord(char) < 32 or ord(char) > 126 for char in value)):
        return None
    value = value.strip()
    if re.fullmatch(r"[0-9]{1,5}", value):
        seconds = int(value)
    else:
        try:
            date = parsedate_to_datetime(value)
            if (date.tzinfo is None or date.utcoffset().total_seconds() != 0
                    or format_datetime(date, usegmt=True) != value):
                return None
            seconds = max(0, math.ceil((date - (now or datetime.now(timezone.utc))).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return None
    return seconds if seconds <= MAX_RETRY_AFTER else None


def http_failure(status, headers, secret=""):
    """Expose fixed diagnostics and numeric delay only; never server error text."""
    hints = {
        400: "request rejected; check model, wire_api and function tool compatibility",
        401: "authentication failed; check the configured API key",
        403: "access denied; check account permissions and model access",
        404: "endpoint or model not found; check --base-url and --model",
        405: "protocol POST not accepted; check --base-url and wire_api",
        408: "service request timed out; check provider status before retrying manually",
        413: "request too large for the service; review /context or reduce --max-context-bytes",
        422: "request rejected; check model, wire_api and function tool compatibility",
        429: "rate limit or quota reached; check service limits",
        504: "service request timed out; check provider status before retrying manually",
    }
    hint = hints.get(status, "service failed; check provider status before retrying manually"
                     if 500 <= status <= 599 else "check endpoint, model, or account settings")
    delay, retryable = None, status in RETRY_STATUSES
    if retryable and headers is not None:
        # Multiple hints are ambiguous; HTTPMessage preserves repeated fields.
        if hasattr(headers, 'get_all'):
            values = headers.get_all('Retry-After', [])
        else:
            value = headers.get('Retry-After')
            values = [] if value is None else [value]
        if len(values) == 1:
            delay = retry_after_seconds(values[0])
        if values and (len(values) != 1 or delay is None):
            retryable = False  # Invalid/ambiguous hints must not become an early retry.
    message = f"provider HTTP {status}; {hint}; no automatic retry"
    if delay is not None:
        message += f"; server suggests waiting {delay} seconds before a manual retry"
    return ProviderError(redact(message, secret), status=status, retry_after=delay, code="http_error", retryable=retryable)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if fp is not None:
            fp.close()
        raise ProviderError("HTTP redirects are disabled; use the final trusted base URL", code="redirect")


def validate_message(message, *, preserve_reasoning=False, wire_api="chat_completions"):
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
    if preserve_reasoning and "reasoning_content" in message:
        reasoning = message["reasoning_content"]
        if reasoning is not None and not isinstance(reasoning, str):
            raise ProviderError("invalid reasoning_content; expected text or null")
        if isinstance(reasoning, str):
            if len(reasoning) > MAX_RESPONSE:
                raise ProviderError("reasoning_content exceeds response limit", code="response_limit")
            try:
                if len(reasoning.encode("utf-8")) > MAX_RESPONSE:
                    raise ProviderError("reasoning_content exceeds response limit", code="response_limit")
            except UnicodeEncodeError:
                raise ProviderError("invalid Unicode in reasoning_content") from None
        result["reasoning_content"] = reasoning
    if wire_api != "chat_completions" and "native_state" in message:
        from .native_protocols import validate_state
        result["native_state"] = validate_state(message["native_state"], result, None if wire_api == "saved" else wire_api)
    return result


class Provider:
    def __init__(self, config):
        self.config = config
        handlers = [NoRedirect(), HTTPHandler(), HTTPSHandler()]
        if config.proxy == "direct":
            handlers.append(urllib.request.ProxyHandler({}))
        elif config.proxy != "system":
            handlers.append(urllib.request.ProxyHandler({"http": config.proxy, "https": config.proxy}))
        self.opener = urllib.request.build_opener(*handlers)
        self.measurements = Measurements()

    def _headers(self, *, stream=False):
        config = self.config
        headers = {"Content-Type": "application/json", "User-Agent": "sprig-agent/0.1.0", **dict(config.headers)}
        if config.wire_api == "messages" and not any(key.lower() == "anthropic-version" for key in headers):
            headers["anthropic-version"] = "2023-06-01"
        if stream:
            headers["Accept"] = "text/event-stream"
        if config.api_key:
            headers[config.api_key_header] = ("Bearer " if config.api_key_scheme == "bearer" else "") + config.api_key
        return headers

    def list_models(self, *, check_cancelled=None):
        """Explicit metadata request, separate from measured model completions."""
        request = urllib.request.Request(self.config.base_url + "/models", headers=self._headers())
        budget = request._sprig_io_budget = IOBudget(self.config.request_timeout or self.config.timeout,
                                                   self.config.request_deadline, check_cancelled)
        budget.check()
        try:
            with self.opener.open(request, timeout=self.config.request_timeout or self.config.timeout) as response:
                raw = response.read(MAX_RESPONSE + 1)
                budget.check()
            if len(raw) > MAX_RESPONSE:
                raise ValueError
            data = json.loads(raw)
            if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                raise ValueError
            names = set()
            for item in data["data"]:
                name = item.get("id") if isinstance(item, dict) else None
                if not isinstance(name, str) or not name or len(name) > 200 or any(ord(c) < 32 for c in name):
                    raise ValueError
                names.add(name)
            return redact({"models": sorted(names)[:256], "total": len(names), "truncated": len(names) > 256,
                           "network_contacted": True, "completion_requests": 0}, self.config.secrets)
        except urllib.error.HTTPError as error:
            try:
                failure = http_failure(error.code, error.headers, self.config.secrets)
            finally:
                error.close()
            raise failure from None
        except (urllib.error.URLError, TimeoutError, OSError, HTTPException) as error:
            reason = error.reason if isinstance(error, urllib.error.URLError) else error
            if isinstance(reason, RequestDeadline):
                raise ProviderError("model metadata request deadline exceeded; no automatic retry", code="request_deadline") from None
            raise ProviderError("model metadata connection failed; no automatic retry", code="connection_error") from None
        except (ValueError, TypeError, RecursionError):
            raise ProviderError("invalid or oversized model metadata response") from None

    def complete(self, messages, tools, on_text=None, *, check_cancelled=None, on_retry=None, on_measurement=None):
        config = self.config
        body, headers, deadline, pending = None, None, None, None
        sent = 0
        def fresh():
            if check_cancelled:
                check_cancelled()
            if self.config is not config:
                raise ProviderError('provider configuration changed; request stopped without resending', code='invalid_request')
        for attempt in range(config.retry_attempts + 1):
            measurement = RequestMeasurement(config.stream,
                (lambda value: on_measurement('started', value)) if on_measurement is not None else None)
            try:
                fresh()
                if pending is not None:
                    delay, status = pending
                    if on_retry is not None:
                        on_retry({'attempt': attempt + 1, 'max_attempts': config.retry_attempts + 1,
                                  'status': status, 'delay_seconds': delay})
                    self._wait_retry(delay, deadline, fresh)
                fresh()
                if body is None:
                    body = self._request_body(messages, tools, fresh)
                    fresh()
                    headers = self._headers(stream=config.stream)
                    deadline = time.monotonic() + config.request_deadline
                result = self._complete(body, headers, on_text, measurement, fresh, deadline)
                measurement.outcome = "success"
                return result
            except KeyboardInterrupt:
                measurement.outcome = "cancelled"
                raise
            except ProviderError as error:
                measurement.error_code = error.code
                if (error.retryable and error.code == 'http_error' and measurement.status == error.status
                        and attempt < config.retry_attempts):
                    base = min(2 ** attempt, 10)
                    delay = max(random.uniform(base, min(base * 1.25, 10)), error.retry_after or 0)
                    if delay < deadline - time.monotonic():
                        pending = delay, error.status
                        continue
                additional = max(0, sent + int(measurement.started is not None) - 1)
                if additional:
                    message = str(error).replace('; no automatic retry', '; no further automatic retry')
                    raise ProviderError(message + f'; additional HTTP attempts: {additional}', status=error.status,
                                        retry_after=error.retry_after, code=error.code) from None
                raise
            finally:
                sent += int(measurement.started is not None)
                record = measurement.finish()
                self.measurements.add(record)
                if on_measurement is not None:
                    on_measurement('finished', record)

    @staticmethod
    def _wait_retry(delay, deadline, check_cancelled):
        end = time.monotonic() + delay
        while True:
            if check_cancelled:
                check_cancelled()
            now = time.monotonic()
            if now >= deadline:
                raise ProviderError('request deadline exceeded during retry wait; no request resent', code='request_deadline')
            if now >= end:
                return
            time.sleep(min(0.1, end - now, deadline - now))

    def _request_body(self, messages, tools, check_cancelled):
        config = self.config
        from .context import request_body
        try:
            body = request_body(config, messages, tools, max_bytes=config.max_context_bytes, check_cancelled=check_cancelled)
        except JsonBudgetExceeded:
            raise ProviderError("context byte budget exceeded; request was not sent", code="request_budget") from None
        except (TypeError, ValueError, RecursionError):
            raise ProviderError("request contains invalid text or JSON; no request sent", code="invalid_request") from None
        if len(body) > config.max_context_bytes:
            raise ProviderError("context byte budget exceeded; request was not sent", code="request_budget")
        return body

    def _complete(self, body, headers, on_text, measurement, check_cancelled, deadline):
        config = self.config
        from .native_protocols import PATHS
        request = urllib.request.Request(config.base_url + PATHS[config.wire_api], body, headers)
        budget = request._sprig_io_budget = IOBudget(config.request_timeout or config.timeout,
                                                   config.request_deadline, check_cancelled)
        budget.deadline = deadline  # Each attempt has a fresh idle clock, never a fresh total deadline.
        try:
            budget.check()
            measurement.begin(len(body))
            budget.check()  # A durable start checkpoint may have consumed the remaining deadline.
            with self.opener.open(request, timeout=config.request_timeout or config.timeout) as response:
                status = getattr(response, "status", None)
                if type(status) is int and 100 <= status <= 599:
                    measurement.status = status
                reader = MeasuredResponse(response, measurement)
                if config.stream:
                    if config.wire_api != "chat_completions":
                        from .native_protocols import complete_stream as native_stream
                        result = native_stream(config.wire_api, reader, on_text, config.secrets, measurement)
                    else:
                        from .streaming import complete_stream
                        result = complete_stream(reader, on_text, config.secrets,
                                                 on_usage=measurement.observe_usage, on_delta=measurement.text_received,
                                                 on_finish=measurement.observe_finish, preserve_reasoning=config.preserve_reasoning)
                    budget.check()
                    return result
                raw = reader.read(MAX_RESPONSE + 1)
                budget.check()
            if len(raw) > MAX_RESPONSE:
                raise ProviderError("provider response exceeds 2 MiB", code="response_limit")
            data = json.loads(raw)
            if config.wire_api != "chat_completions":
                from .native_protocols import parse_response, usage
                if isinstance(data, dict):
                    measurement.observe_usage(usage(config.wire_api, data.get("usage")))
                result = parse_response(config.wire_api, data)
                measurement.observe_finish("tool_calls" if result.get("tool_calls") else "stop")
                return redact(result, config.secrets)
            if isinstance(data, dict):
                measurement.observe_usage(data.get("usage"))
            if (not isinstance(data, dict) or not isinstance(data.get("choices"), list)
                    or not data["choices"] or not isinstance(data["choices"][0], dict)):
                raise ProviderError("invalid Chat Completions choices")
            choice = data["choices"][0]
            measurement.observe_finish(choice.get("finish_reason"))
            if choice.get("finish_reason") not in {"stop", "tool_calls"}:
                raise completion_failure(choice.get("finish_reason"))
            return redact(validate_message(choice["message"], preserve_reasoning=config.preserve_reasoning), config.secrets)
        except urllib.error.HTTPError as exc:
            if type(exc.code) is int and 100 <= exc.code <= 599:
                measurement.status = exc.code
            # Never echo response bodies: gateways can include request credentials.
            try:
                failure = http_failure(exc.code, exc.headers, config.secrets)
            finally:
                exc.close()
            raise failure from None
        except (urllib.error.URLError, TimeoutError, OSError, HTTPException) as exc:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            if isinstance(reason, ssl.SSLCertVerificationError):
                hint = "TLS certificate verification failed; check the trusted endpoint and local certificates"
                code = "tls_error"
            elif isinstance(reason, RequestDeadline):
                hint = "request deadline exceeded; check provider latency or explicitly raise --request-deadline"
                code = "request_deadline"
            elif isinstance(reason, TimeoutError):
                hint = "socket timed out; check connectivity or --request-timeout (falls back to --timeout)"
                code = "timeout"
            else:
                hint = "connection or HTTP response failed; check endpoint, network, or proxy"
                code = "connection_error"
            raise ProviderError(redact(f"provider {hint}; no automatic retry", config.secrets), code=code) from None
        except (KeyError, IndexError, TypeError, ValueError, RecursionError):
            raise ProviderError("invalid provider protocol response") from None
