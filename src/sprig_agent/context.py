"""Request byte budgets and explicit, non-destructive history elision."""
import json
from collections import deque
from .config import redact
from .context_folding import fold_inspections
from .json_budget import JSON_OPTIONS, encoded_size
from .planning import plan_result, validate_plan
from .revisions import valid_read_id
from .session import application_check
from .input_inbox import INPUT_ID
from .image_inputs import IMAGE_KEY, wire_content
from .message_origin import ORIGIN, application_origin

MAX_READ_METADATA_CHARS = 32768


def provider_messages(config, messages, *, check_cancelled=None):
    """Project provider-specific history without changing saved actions/results."""
    selected, index = [], 0
    while index < len(messages):
        if check_cancelled:
            check_cancelled()
        message = messages[index]
        application_origin(message)  # Validate before dropping delivery identities/metadata.
        if IMAGE_KEY in message and any(key in message for key in (INPUT_ID, "sprig_check")):
            raise ValueError("image snapshots cannot carry input or fixed-check identities; no request sent")
        if "sprig_check" in message and config.check_history_format == "observations":
            phase = application_check(message)
            call = message["tool_calls"][0]
            if (index + 1 >= len(messages) or messages[index + 1].get("role") != "tool"
                    or messages[index + 1].get("tool_call_id") != call["id"]
                    or not isinstance(messages[index + 1].get("content"), str)):
                raise ValueError("incomplete application check observation; no request sent")
            # Reuse the result string, rather than re-encode a potentially long log.
            # Two user data messages preserve the complete argv and observation.
            selected.extend([{"role": "user", "content": (
                "Application fixed-check observation (" + phase + ", " + call["id"] + "). "
                "The next message contains its result JSON. These are historical application data, "
                "not a model tool call, new task or approval. No operation is replayed.\nArguments JSON: "
                + call["function"]["arguments"])},
                {"role": "user", "content": messages[index + 1]["content"]}])
            index += 2
            continue
        drop_reasoning = not config.preserve_reasoning or message.get("role") != "assistant"
        if ORIGIN in message or "sprig_check" in message or INPUT_ID in message or (drop_reasoning and "reasoning_content" in message):
            message = {key: value for key, value in message.items() if key not in {ORIGIN, "sprig_check", INPUT_ID}
                       and (key != "reasoning_content" or not drop_reasoning)}
        selected.append(message)
        index += 1
    return selected


def request_payload(config, messages, tools, *, check_cancelled=None):
    selected = provider_messages(config, messages, check_cancelled=check_cancelled)
    if config.wire_api != "chat_completions":
        from .native_protocols import payload
        return redact(payload(config, selected, tools, check_cancelled=check_cancelled), config.secrets)
    if any("native_state" in message for message in selected):
        raise ValueError("native protocol history cannot be sent through Chat Completions")
    selected = [{**{key: value for key, value in message.items() if key != IMAGE_KEY},
                 "content": wire_content(message, "chat_completions", config.secrets, check_cancelled or (lambda: None))}
                if IMAGE_KEY in message else message for message in selected]
    payload = {"model": config.model, "messages": redact(selected, config.secrets)}
    if tools:
        payload.update(tools=tools, tool_choice="auto")
    payload.update(config.generation_options())
    if config.stream:
        payload["stream"] = True
        if config.request_usage:
            payload["stream_options"] = {"include_usage": True}
    return payload


def request_body(config, messages, tools, *, max_bytes=None, check_cancelled=None):
    try:
        payload = request_payload(config, messages, tools, check_cancelled=check_cancelled)
        if max_bytes is not None:
            encoded_size(payload, max_bytes=max_bytes)
        return json.dumps(payload, **JSON_OPTIONS).encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("request contains invalid Unicode; no request sent") from None


def request_size(config, messages, tools, *, check_cancelled=None):
    try:
        return encoded_size(request_payload(config, messages, tools, check_cancelled=check_cancelled), check_cancelled=check_cancelled)
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


def _read_digest(result, path):
    """Recognize a reliable historical version, not current edit authority."""
    if not _successful(result) or result.get("path") != path or not isinstance(result.get("content"), str):
        return None
    digest = result.get("sha256")
    if (result.get("context_elided") or not isinstance(digest, str) or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
            or any(result.get(flag, False) is not False for flag in (
                "timed_out", "output_limited", "skipped", "cancelled", "interrupted", "outcome"))
            or ("approved" in result and result["approved"] is not True)
            or ("returncode" in result and (type(result["returncode"]) is not int or result["returncode"] != 0))):
        return None
    return digest


def _complete_read(result, arguments):
    """A complete observed window, never a prefix or an unvalidated byte page."""
    if (not _read_digest(result, arguments.get("path")) or not result["content"]
            or not valid_read_id(result.get("read_id")) or "cursor" in arguments
            or result.get("line_truncated", False) is not False):
        return False
    if "offset" not in result:
        return ("symbol" not in arguments and "limit" not in arguments
                and arguments.get("offset", 1) == 1 and not arguments.get("line_numbers")
                and result.get("truncated") is False)
    # Older saved reads predate range_complete. Verify their exact requested
    # range from the original call and the returned physical-line metadata.
    offset, end, total = (result.get(name) for name in ("offset", "end_line", "total_lines"))
    if not (all(type(value) is int for value in (offset, end, total)) and 1 <= offset <= end <= total):
        return False
    if "symbol" in arguments:
        start, stop = result.get("symbol_start_line"), result.get("symbol_end_line")
        return (result.get("symbol_complete") is True and result.get("symbol") == arguments["symbol"]
                and type(start) is int and type(stop) is int and (start, stop) == (offset, end))
    limit = arguments.get("limit", 2000)
    return (all(type(value) is int for value in (offset, end, total, limit))
            and type(arguments.get("offset", 1)) is int
            and offset == arguments.get("offset", 1) and offset >= 1 and total >= end >= offset
            and 1 <= limit <= 10000 and result.get("line_truncated") is False
            and result.get("range_complete", True) is True
            and end == min(total, offset + limit - 1))


def _read_span(result):
    # None denotes the entire observed file, not an unknown line window.
    return (result["offset"], result["end_line"]) if "offset" in result else None


def _covered(span, windows):
    if None in windows:
        return True
    if span is None:
        return False
    cursor = span[0]
    for start, end in sorted(windows):
        if start > cursor:
            break
        cursor = max(cursor, end + 1)
    return cursor > span[1]


def _overlaps(span, windows):
    return any(span is None or window is None or span[0] <= window[1] and window[0] <= span[1]
               for window in windows)


def prepare_context(config, messages, tools, compact=False, *, force=False, check_cancelled=None):
    """Keep the saved transcript intact; never discard user instructions or refusals."""
    selected = redact(messages, config.secrets)
    before = size = request_size(config, selected, tools, check_cancelled=check_cancelled)
    changes, retained_reads, folded = [], [], []
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
        # Only inspect a protected result when an older candidate needs its
        # version/coverage; never decode every large recent read speculatively.
        latest_reads, read_calls = {}, {}
        if config.context_keep_read_bytes:
            for identity, (index, call) in calls.items():
                function = call.get("function", {})
                if function.get("name") == "read_file":
                    arguments = _result(function.get("arguments"))
                    if arguments is not None and isinstance(arguments.get("path"), str):
                        read_calls[identity] = (index, arguments)
                        path = arguments["path"]
                        if index > latest_reads.get(path, (-1, None, None))[0]:
                            latest_reads[path] = (index, identity, arguments)
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
            if identity in ambiguous or not index < result_index < cutoff or "native_state" in selected[index]:
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
            if read[1].get("function", {}).get("name") == "read_log" and "text" in result:
                fields.append("text")
                note = "Older historical command log excerpt omitted; use read_log to page the retained log again. This is not current evidence or approval."
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
                if config.context_keep_read_bytes and _complete_read(result, read_arguments):
                    read_candidates.append((index, identity, message["content"], result["path"],
                                            result["sha256"], _read_span(result), "symbol" in read_arguments))
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
        versions, coverage = {}, {}
        for path in dict.fromkeys(candidate[3] for candidate in read_candidates):
            if check_cancelled:
                check_cancelled()
            call_index, identity, arguments = latest_reads[path]
            result_index, content = results.get(identity, (-1, None))
            # Bound metadata decoding as well as later JSON allocation. Unknown
            # or unreliable latest versions cannot resurrect older file text.
            if (identity in ambiguous or result_index <= call_index or not isinstance(content, str)
                    or len(content) > MAX_READ_METADATA_CHARS):
                continue
            latest = _result(content)
            digest = _read_digest(latest, path)
            if digest is None or not _complete_read(latest, arguments):
                continue
            versions[path], coverage[path] = digest, []
            if result_index >= cutoff and _complete_read(latest, arguments):
                coverage[path].append(_read_span(latest))
        for identity, (call_index, arguments) in read_calls.items():
            path = arguments["path"]
            result_index, content = results.get(identity, (-1, None))
            if (path not in versions or identity == latest_reads[path][1] or identity in ambiguous
                    or result_index < cutoff or result_index <= call_index
                    or not isinstance(content, str) or len(content) > MAX_READ_METADATA_CHARS):
                continue
            recent = _result(content)
            if _read_digest(recent, path) == versions[path] and _complete_read(recent, arguments):
                coverage[path].append(_read_span(recent))
        read_candidates = list(read_candidates)
        # Restore a few complete snapshots only when both independent byte caps
        # permit it. Keep their original transcript position, redact before
        # sizing, and remove read_id: historical text is not a fresh observation.
        while read_candidates:
            # Complete named declarations are self-contained observations. Then
            # prefer disjoint windows by recency, recomputing after each pick.
            pick = min(range(len(read_candidates)), key=lambda n: (
                not read_candidates[n][6],
                _overlaps(read_candidates[n][5], coverage.get(read_candidates[n][3], [])),
                -read_candidates[n][0]))
            index, identity, original, path, digest, span, _named = read_candidates.pop(pick)
            if check_cancelled:
                check_cancelled()
            if versions.get(path) != digest or _covered(span, coverage[path]):
                continue
            message = selected[index]
            if message["content"] == original:
                coverage[path].append(span)
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
            coverage[path].append(span)
            retained_reads.append({"message": index, "tool_call_id": identity, "bytes": new_bytes})
            change = next(change for change in changes if change["message"] == index and change["fields"] != ["arguments"])
            change.update(fields=["read_id"], after_bytes=new_bytes, retained_read=True)
        if retained_reads:
            size = request_size(config, selected, tools, check_cancelled=check_cancelled)
        if size > config.max_context_bytes and not config.preserve_reasoning:
            selected, folded = fold_inspections(selected, changes, cutoff, ambiguous, config.max_context_bytes,
                                                wire_size, check_cancelled, secrets=config.secrets)
            if folded:
                size = request_size(config, selected, tools, check_cancelled=check_cancelled)
                changes.extend({"message": group["first_message"], "fields": ["tool_call_batches"],
                                "operation": "fold_inspections", "before_bytes": group["before_bytes"],
                                "after_bytes": group["after_bytes"]} for group in folded)
    return selected, {"before_bytes": before, "after_bytes": size, "budget_bytes": config.max_context_bytes,
                      "within_budget": size <= config.max_context_bytes, "changes": changes,
                      "compaction_triggered": triggered, "trigger_ratio": config.context_trigger_ratio,
                      "keep_recent_messages": config.context_keep_recent_messages,
                      "keep_read_bytes": config.context_keep_read_bytes, "retained_read_bytes": retained_bytes,
                      "retained_reads": retained_reads, "folded_inspections": folded}
