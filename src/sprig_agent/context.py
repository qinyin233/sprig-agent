"""Request byte budgets and explicit, non-destructive history elision."""
import json
from collections import deque
from .config import redact
from .json_budget import JSON_OPTIONS, encoded_size
from .planning import plan_result, validate_plan
from .revisions import valid_read_id


def request_payload(config, messages, tools):
    payload = {"model": config.model, "messages": redact(messages, config.secrets), "tools": tools, "tool_choice": "auto"}
    payload.update(config.generation_options())
    if config.stream:
        payload["stream"] = True
        if config.request_usage:
            payload["stream_options"] = {"include_usage": True}
    return payload


def request_body(config, messages, tools, *, max_bytes=None):
    try:
        payload = request_payload(config, messages, tools)
        if max_bytes is not None:
            encoded_size(payload, max_bytes=max_bytes)
        return json.dumps(payload, **JSON_OPTIONS).encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("request contains invalid Unicode; no request sent") from None


def request_size(config, messages, tools, *, check_cancelled=None):
    try:
        return encoded_size(request_payload(config, messages, tools), check_cancelled=check_cancelled)
    except UnicodeEncodeError:
        raise ValueError("request contains invalid Unicode; no request sent") from None


def _result(content):
    try:
        value = json.loads(content)
    except (ValueError, TypeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _successful(result):
    return (result is not None and "error" not in result and result.get("approved") is not False
            and not any(result.get(flag) for flag in (
                "timed_out", "output_limited", "skipped", "cancelled", "interrupted", "outcome"))
            and result.get("returncode", 0) == 0)


def _fixed_check(config, function, content):
    """Only complete approved observations of a configured default-cwd check."""
    if not config.checks or function.get("name") != "run":
        return None
    arguments = _result(function.get("arguments"))
    if (arguments is None or set(arguments) != {"argv"} or not isinstance(arguments["argv"], list)
            or not all(isinstance(arg, str) for arg in arguments["argv"])):
        return None
    argv = tuple(arguments["argv"])
    if argv not in config.checks:
        return None
    result = _result(content)
    if (result is None or "error" in result or result.get("approved") is not True
            or type(result.get("returncode")) is not int or result["returncode"] < 0
            or not isinstance(result.get("output"), str) or result.get("context_elided")
            or any(result.get(flag, False) is not False for flag in (
                "timed_out", "output_limited", "skipped", "cancelled", "interrupted", "outcome"))):
        return None
    return argv


def _edit_path(arguments, *, by_line=False):
    value = _result(arguments)
    if value is None or not isinstance(value.get("path"), str) or not value["path"]:
        return None
    if by_line:
        if set(value) != {"path", "read_id", "edits"} or not valid_read_id(value["read_id"]):
            return None
        from .line_edits import validate_edits
        try:
            validate_edits(value["edits"], 512 * 1024, lambda: None)
        except (ValueError, TypeError):
            return None
    elif set(value) == {"path", "old", "new"}:
        if not all(isinstance(value[key], str) for key in ("old", "new")):
            return None
    elif set(value) == {"path", "edits"}:
        edits = value["edits"]
        if (not isinstance(edits, list) or not 1 <= len(edits) <= 32
                or any(not isinstance(edit, dict) or set(edit) != {"old", "new"}
                       or not all(isinstance(edit[key], str) for key in ("old", "new")) for edit in edits)):
            return None
    elif set(value) == {"path", "sha256", "line_edits"}:
        from .line_edits import validate_edits
        digest = value["sha256"]
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            return None
        try:
            validate_edits(value["line_edits"], 512 * 1024, lambda: None)
        except (ValueError, TypeError):
            return None
    else:
        return None
    return value["path"]


def _complete_read(result, arguments):
    """A complete observed window, never a prefix or an unvalidated byte page."""
    digest = result.get("sha256")
    if (not isinstance(result.get("content"), str) or not result["content"]
            or result.get("context_elided") or not valid_read_id(result.get("read_id"))
            or not isinstance(digest, str) or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest) or "cursor" in arguments
            or any(result.get(flag, False) is not False for flag in (
                "line_truncated", "timed_out", "output_limited", "skipped", "cancelled", "interrupted", "outcome"))
            or ("approved" in result and result["approved"] is not True)
            or ("returncode" in result and (type(result["returncode"]) is not int or result["returncode"] != 0))):
        return False
    if "symbol" in arguments:
        return result.get("symbol_complete") is True
    if "offset" not in result:
        return result.get("truncated") is False
    # Older saved reads predate range_complete. Verify their exact requested
    # range from the original call and the returned physical-line metadata.
    offset, end, total = (result.get(name) for name in ("offset", "end_line", "total_lines"))
    limit = arguments.get("limit", 2000)
    return (all(type(value) is int for value in (offset, end, total, limit))
            and type(arguments.get("offset", 1)) is int
            and offset == arguments.get("offset", 1) and offset >= 1 and total >= end >= offset
            and 1 <= limit <= 10000 and result.get("line_truncated") is False
            and result.get("range_complete", True) is True
            and end == min(total, offset + limit - 1))


def prepare_context(config, messages, tools, compact=False, *, force=False, check_cancelled=None):
    """Keep the saved transcript intact; never discard user instructions or refusals."""
    selected = redact(messages, config.secrets)
    before = size = request_size(config, selected, tools, check_cancelled=check_cancelled)
    changes, retained_reads = [], []
    retained_bytes = 0
    def wire_size(value):
        return encoded_size(redact(value, config.secrets), check_cancelled=check_cancelled)
    def replace_string(container, field, value, index, identity, fields):
        old_bytes, new_bytes = wire_size(container[field]), wire_size(value)
        if old_bytes - new_bytes < 128:
            return
        container[field] = value
        changes.append({"message": index, "tool_call_id": identity, "fields": fields,
                        "before_bytes": old_bytes, "after_bytes": new_bytes})
    triggered = compact and (force or before >= config.max_context_bytes * config.context_trigger_ratio)
    if triggered:
        cutoff = max(0, len(selected) - config.context_keep_recent_messages)
        calls, results, ambiguous = {}, {}, set()
        # Correlate each historical intent with one completed result. Duplicate
        # IDs and a result in the protected tail must never permit elision.
        for index, message in enumerate(selected):
            if message.get("role") == "assistant":
                for call in message.get("tool_calls", []):
                    identity = call.get("id")
                    if identity in calls:
                        ambiguous.add(identity)
                    calls[identity] = (index, call)
            elif message.get("role") == "tool":
                identity = message.get("tool_call_id")
                if identity in results:
                    ambiguous.add(identity)
                results[identity] = (index, message.get("content"))
        # Inspect small read arguments, not the potentially large protected
        # result bodies. A newer read attempt on a path suppresses older picks,
        # including a failed or incomplete attempt; this is a recency policy,
        # not a claim that different windows are duplicates.
        latest_reads = {}
        if config.context_keep_read_bytes:
            for identity, (index, call) in calls.items():
                function = call.get("function", {})
                if function.get("name") == "read_file":
                    arguments = _result(function.get("arguments"))
                    if arguments is not None and isinstance(arguments.get("path"), str):
                        path = arguments["path"]
                        if index > latest_reads.get(path, (-1, None))[0]:
                            latest_reads[path] = (index, identity)
        read_candidates = deque(maxlen=32)
        plan_results = {index for identity, (index, content) in results.items()
                        if identity not in ambiguous and calls.get(identity, (0, {}))[1].get("function", {}).get("name") == "update_plan"
                        and plan_result(_result(content)) is not None}
        latest_plan = max(plan_results, default=-1)
        check_results, latest_checks = {}, {}
        for identity, (index, content) in results.items():
            if identity in ambiguous or identity not in calls or calls[identity][0] >= index:
                continue
            check = _fixed_check(config, calls[identity][1].get("function", {}), content)
            if check is not None:
                check_results[index] = check
                latest_checks[check] = (index, identity)
        superseded_checks = {index: latest_checks[check][1] for index, check in check_results.items()
                             if index != latest_checks[check][0]}
        for identity, (index, call) in calls.items():
            result_index, content = results.get(identity, (len(selected), None))
            function = call.get("function", {})
            if identity in ambiguous or not index < result_index < cutoff:
                continue
            result = _result(content)
            if result_index in superseded_checks:
                marker = json.dumps({"context_elided": True,
                    "note": "Past fixed-check argv omitted; not runnable."}, separators=(",", ":"))
                replace_string(function, "arguments", marker, index, identity, ["arguments"])
                continue
            if function.get("name") == "update_plan" and result_index in plan_results and result_index != latest_plan:
                arguments = _result(function.get("arguments"))
                try:
                    if arguments is None or validate_plan(**arguments) != result:
                        continue
                except (TypeError, ValueError):
                    continue
                marker = json.dumps({"context_elided": True, "note": "Older completed plan update omitted from this request. This is not runnable tool input. The latest successful plan result is retained; saved history is unchanged."})
                replace_string(function, "arguments", marker, index, identity, ["arguments"])
                continue
            if function.get("name") not in {"edit_file", "edit_lines"}:
                continue
            if (not _successful(result) or result.get("approved") is not True
                    or result.get("changed") is not True):
                continue
            path = _edit_path(function.get("arguments"), by_line=function["name"] == "edit_lines")
            if path is None or result.get("path") != path:
                continue
            marker = json.dumps({"path": path, "context_elided": True,
                "note": "Completed approved historical edit: replacement text omitted from this request. This is not runnable tool input. Inspect current files before further edits; the saved transcript retains the original arguments."}, ensure_ascii=False)
            replace_string(function, "arguments", marker, index, identity, ["arguments"])
        for index, message in enumerate(selected[:cutoff]):
            if message.get("role") != "tool":
                continue
            if message.get("tool_call_id") in ambiguous:
                continue
            result = _result(message.get("content"))
            if index in superseded_checks:
                reduced = {key: value for key, value in result.items() if key != "output"}
                reduced.update(context_elided=True, check_ref=superseded_checks[index],
                               note="Past check output omitted; newer approved result retained.")
                content = json.dumps(reduced, ensure_ascii=False, separators=(",", ":"))
                replace_string(message, "content", content, index, message["tool_call_id"], ["output"])
                continue
            if not _successful(result):
                continue
            fields = [name for name in ("content", "output", "files", "entries", "matches", "read_guidance", "search_guidance") if name in result]
            note = "Older successful output elided; reread files if needed. Full result remains in saved history."
            identity = message["tool_call_id"]
            read = calls.get(identity, (len(selected), {}))
            read_arguments = _result(read[1].get("function", {}).get("arguments"))
            if ("content" in fields and identity not in ambiguous and read[0] < index
                    and read[1].get("function", {}).get("name") == "read_file"
                    and read_arguments is not None and isinstance(result.get("path"), str)
                    and read_arguments.get("path") == result["path"]):
                # A discarded read body is not current edit evidence. Keep its
                # coverage flags; obtain provenance/pagination again by rereading.
                fields.extend(name for name in ("sha256", "read_id", "line_numbers", "offset", "end_line", "total_lines",
                                                "next_offset", "path", "symbol", "symbol_start_line", "symbol_end_line") if name in result)
                note = "Past read omitted; reread source. Full history saved."
                if (config.context_keep_read_bytes and latest_reads.get(result["path"], (None, None))[1] == identity
                        and _complete_read(result, read_arguments)):
                    read_candidates.append((index, identity, message["content"]))
            if index in plan_results and index != latest_plan:
                fields.extend(("plan", "explanation"))
            if not fields:
                continue
            reduced = {key: value for key, value in result.items() if key not in fields}
            reduced.update(context_elided=True, note=note)
            content = json.dumps(reduced, ensure_ascii=False, separators=(",", ":"))
            replace_string(message, "content", content, index, message["tool_call_id"], fields)
        # Count the actual serialized request, including redaction of new markers.
        if changes:
            size = request_size(config, selected, tools, check_cancelled=check_cancelled)
        # Restore a few complete snapshots only when both independent byte caps
        # permit it. Keep their original transcript position, redact before
        # sizing, and remove read_id: historical text is not a fresh observation.
        for index, identity, original in reversed(read_candidates):
            if check_cancelled:
                check_cancelled()
            message = selected[index]
            if message["content"] == original:
                continue  # Too small to elide; already retained by existing policy.
            snapshot = _result(original)
            available = min(config.context_keep_read_bytes - retained_bytes,
                            config.max_context_bytes - size + wire_size(message["content"]))
            # The body alone is a lower bound on the doubly encoded result.
            # Do not allocate a giant JSON string merely to reject its size.
            if len(snapshot["content"]) > available or wire_size(snapshot["content"]) > available:
                continue
            snapshot.pop("read_id", None)
            snapshot.update(context_retained=True,
                note="Historical read snapshot; may be stale. Reread before editing. No current read_id or approval.")
            if wire_size(snapshot) > available:
                continue  # Include all metadata before allocating the inner JSON.
            content = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
            new_bytes, old_bytes = wire_size(content), wire_size(message["content"])
            if (retained_bytes + new_bytes > config.context_keep_read_bytes
                    or size - old_bytes + new_bytes > config.max_context_bytes):
                continue
            message["content"] = content
            size += new_bytes - old_bytes
            retained_bytes += new_bytes
            retained_reads.append({"message": index, "tool_call_id": identity, "bytes": new_bytes})
            change = next(change for change in changes if change["message"] == index and change["fields"] != ["arguments"])
            change.update(fields=["read_id"], after_bytes=new_bytes, retained_read=True)
        if retained_reads:
            size = request_size(config, selected, tools, check_cancelled=check_cancelled)
    return selected, {"before_bytes": before, "after_bytes": size, "budget_bytes": config.max_context_bytes,
                      "within_budget": size <= config.max_context_bytes, "changes": changes,
                      "compaction_triggered": triggered, "trigger_ratio": config.context_trigger_ratio,
                      "keep_recent_messages": config.context_keep_recent_messages,
                      "keep_read_bytes": config.context_keep_read_bytes, "retained_read_bytes": retained_bytes,
                      "retained_reads": retained_reads}
