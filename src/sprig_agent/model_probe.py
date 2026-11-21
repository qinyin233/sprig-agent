"""Approved protocol probes with fixed inputs and no agent/tool execution."""

from dataclasses import replace
import json

from .config import redact, terminal_text
from .native_protocols import PATHS
from .provider import Provider, ProviderError


CHECKS = (("json_text", False, False), ("stream_text", True, False),
          ("json_tool", False, True), ("stream_tool", True, True))
TOOL = {"type": "function", "function": {"name": "sprig_model_probe", "description": "Diagnostic echo only",
        "parameters": {"type": "object", "properties": {"value": {"type": "string"}},
                       "required": ["value"], "additionalProperties": False}}}
TOOL_INPUT = {"value": "sprig-probe"}
TOOL_PROMPT = 'Call sprig_model_probe once with value "sprig-probe".'
TEXT_PROMPT = "Reply with the short text Sprig probe OK."


def probe_model(config, approve, *, check_cancelled=None):
    tokens = min(config.max_tokens or 1024, 1024)
    deadline = min(config.request_deadline, 30)
    endpoint = redact(config.base_url + PATHS[config.wire_api], config.secrets)
    model = redact(config.model, config.secrets)
    checks = [{"check": name, "status": "not_run"} for name, _, _ in CHECKS]
    report = {"ok": False, "approved": False, "cancelled": False, "endpoint": endpoint, "model": model,
              "wire_api": config.wire_api, "max_requests": len(CHECKS), "attempts": 0,
              "requested_output_token_limit": tokens, "request_deadline_seconds": deadline, "checks": checks}
    notice = (f"PROBE MODEL {model} ({config.wire_api}) at {endpoint}\n"
              f"Send up to {len(CHECKS)} model requests, up to {tokens} requested output tokens and {deadline:g}s each. "
              "Requests may be billed.\n"
              "Use fixed test prompts and configured request settings, including extra_body. "
              "Workspace files and conversation history stay local. Returned tool calls are never executed.")
    if approve(terminal_text(notice)) is not True:
        return report
    report["approved"] = True
    for row, (_, stream, tool) in zip(checks, CHECKS):
        provider = Provider(replace(config, stream=stream, request_usage=stream and config.request_usage,
                                    retry_attempts=0, request_deadline=deadline, max_tokens=tokens))
        rows = [{"role": "user", "content": TOOL_PROMPT if tool else TEXT_PROMPT}]
        chunks = 0

        def count_text(text):
            nonlocal chunks
            if text:
                chunks += 1

        stop = False
        try:
            result = provider.complete(rows, [TOOL] if tool else [], count_text if stream else None,
                                       check_cancelled=check_cancelled)
            issue = _observed_issue(result, tool, stream, chunks)
            if issue is None:
                row["status"] = "passed"
            else:
                row.update(status="failed", reason=issue)
        except ProviderError as error:
            row.update(status="failed", reason=error.code)
            stop = error.status in {401, 403, 429} or error.code in {"tls_error", "redirect"}
        except KeyboardInterrupt:
            row["status"] = "cancelled"
            report["cancelled"] = stop = True
        if provider.measurements.recent:
            row["request"] = provider.measurements.recent[-1]
        if stream and not tool:
            row["text_chunks"] = chunks
        report["attempts"] += provider.measurements.attempts
        if stop:
            break
    report["ok"] = all(row["status"] == "passed" for row in checks)
    return report


def _observed_issue(result, tool, stream, chunks):
    calls = result.get("tool_calls", [])
    if tool:
        if not calls:
            return "tool_call_missing"
        if len(calls) == 1:
            function = calls[0]["function"]
            try:
                arguments = json.loads(function["arguments"])
            except (ValueError, RecursionError):
                return "tool_arguments_invalid"
            if function["name"] == TOOL["function"]["name"] and arguments == TOOL_INPUT:
                return None
        return "unexpected_tool_call"
    if calls or not (result.get("content") or "").strip():
        return "text_response_missing"
    if stream and not chunks:
        return "text_stream_missing"
    return None
