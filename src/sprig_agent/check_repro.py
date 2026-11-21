"""Local cards for app-owned fixed check observations, indexed by result message."""

from datetime import datetime, timedelta, timezone
import json
import re
import sys
import time

from .config import redact, terminal_text
from .json_budget import JSON_OPTIONS, JsonBudgetExceeded, encoded_size


OBSERVATION_KEY = "sprig_check_observation"
FAILURES = {"failed", "timed_out", "output_limited", "incomplete"}
USAGE = "Use /repro [OFFSET], show MESSAGE, run MESSAGE, report MESSAGE, or export MESSAGE PATH"


def command(argument):
    parts = argument.split(None, 2)
    if not parts:
        return {"offset": 0}
    if len(parts) == 1 and re.fullmatch(r"[0-9]{1,4}", parts[0]):
        return {"offset": int(parts[0])}
    if len(parts) >= 2 and re.fullmatch(r"[1-9][0-9]{0,3}", parts[1]):
        action, position = parts[0], int(parts[1])
        if len(parts) == 2 and action in {"show", "run", "report"}:
            return {"action": action, "position": position}
        if len(parts) == 3 and action == "export" and parts[2].strip():
            return {"action": action, "position": position, "path": parts[2].strip()}
    raise ValueError(USAGE)


def check_observation(before, after):
    return {"before": None if before is None else [int.from_bytes(row[2], "big") for row in before],
            "after": None if after is None else [int.from_bytes(row[2], "big") for row in after],
            "platform": sys.platform, "python": list(sys.version_info[:3]), "time": int(time.time())}


def validate_observation(value):
    if (isinstance(value, dict) and set(value) == {"before", "after", "platform", "python", "time"}
            and isinstance(value["platform"], str) and re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value["platform"])
            and isinstance(value["python"], list) and len(value["python"]) == 3
            and all(type(part) is int and 0 <= part <= 999 for part in value["python"])
            and type(value["time"]) is int and 0 <= value["time"] <= 253402239999):
        sizes = set()
        for hashes in (value["before"], value["after"]):
            if hashes is None:
                continue
            if (not isinstance(hashes, list) or not 1 <= len(hashes) <= 16
                    or any(type(digest) is not int or not 0 <= digest < 2 ** 256 for digest in hashes)):
                break
            sizes.add(len(hashes))
        else:
            if len(sizes) <= 1:
                return value
    raise ValueError("invalid fixed check observation metadata")


def _object(value):
    try:
        result = json.loads(value)
    except (ValueError, TypeError, RecursionError):
        return {}
    return result if isinstance(result, dict) else {}


def _status(result):
    if result.get("approved") is False:
        return "rejected"
    for flag in ("interrupted", "cancelled", "skipped", "timed_out", "output_limited"):
        if result.get(flag) is True:
            return flag
    if (result.get("approved") is not True or "error" in result or result.get("outcome")
            or type(result.get("returncode")) is not int or result["returncode"] < 0):
        return "incomplete"
    return "passed" if result["returncode"] == 0 else "failed"


def observation(messages, position):
    from .session import application_check
    if not 2 <= position <= len(messages):
        return None
    intent, observed = messages[position - 2:position]
    phase = application_check(intent)
    if phase is None or observed.get("role") != "tool":
        return None
    call = intent["tool_calls"][0]
    if call["id"] != observed.get("tool_call_id"):
        return None
    args = _object(call["function"]["arguments"])
    argv = args.get("argv")
    if set(args) != {"argv"} or not isinstance(argv, list) or not argv or any(not isinstance(arg, str) for arg in argv):
        return None
    result = _object(observed.get("content"))
    return {"message": position, "phase": phase, "argv": argv, "outcome": _status(result),
            "result": result, "samples": observed.get(OBSERVATION_KEY)}


def failure(messages, position):
    row = observation(messages or [], position)
    if row is None or row["outcome"] not in FAILURES:
        raise ValueError("select a failed or incomplete application fixed check result from /repro")
    return row


def retry_argv(config, messages, position):
    argv = tuple(failure(messages, position)["argv"])
    if argv not in config.checks:
        raise ValueError("this exact argv is not a current configured fixed check")
    return argv


def _summary(row, *, runtime=False):
    result = {key: row[key] for key in ("message", "phase", "outcome")}
    status = row["result"].get("returncode")
    if type(status) is int:
        result["exit_code"] = status
    if runtime and row["samples"]:
        data = row["samples"]
        result["sprig_runtime"] = {"platform": data["platform"], "python": ".".join(map(str, data["python"]))}
        result["recorded_at_utc"] = (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=data["time"])).isoformat()
    return result


def catalog(config, messages, offset, *, limit=12):
    messages = messages or []
    rows = []
    for position in range(len(messages), 1, -1):
        row = observation(messages, position)
        if row is not None and row["outcome"] in FAILURES:
            program = row["argv"][0].replace("\\", "/").rsplit("/", 1)[-1]
            rows.append({**_summary(row), "program": redact(program[:40], config.secrets),
                         "rerun_available": tuple(row["argv"]) in config.checks})
    page = {"failures": [], "offset": offset, "total": len(rows), "next_offset": None}
    budget = min(config.max_output, 8192)
    for row in rows[offset:offset + limit]:
        page["failures"].append(row)
        page["next_offset"] = offset + len(page["failures"])
        if len(json.dumps(page, **JSON_OPTIONS).encode("utf-8")) > budget:
            page["failures"].pop()
            break
    end = offset + len(page["failures"])
    if end < len(rows) and end == offset:
        raise ValueError("fixed check list exceeds the display budget")
    page["next_offset"] = end if end < len(rows) else None
    return page


def display_catalog(page):
    if not page["failures"]:
        return "这一页没有已记录的固定检查失败。"
    labels = {"failed": "失败", "timed_out": "超时", "output_limited": "输出超限", "incomplete": "未结算"}
    lines = ["固定检查失败 · /repro show 消息号"]
    for row in page["failures"]:
        label = labels[row["outcome"]]
        if "exit_code" in row:
            label += f" · 退出码 {row['exit_code']}"
        if not row["rerun_available"]:
            label += " · 当前配置不可复跑"
        lines.append(f"消息 {row['message']} · {label}")
    if page["next_offset"] is not None:
        lines.append(f"下一页 /repro {page['next_offset']}")
    return "\n".join(lines)


def _file_changes(config, before, after):
    if (before is None or after is None or len(before) != len(config.check_files)
            or len(after) != len(config.check_files)):
        return None
    return [redact(path, config.secrets) for path, left, right in zip(config.check_files, before, after) if left != right]


def card(config, messages, position):
    source = failure(messages, position)
    latest = None
    for index in range(len(messages), position, -1):
        row = observation(messages, index)
        if row is not None and row["argv"] == source["argv"]:
            latest = row
            break
    original = source["samples"] or {}
    recent = (latest["samples"] or {}) if latest else {}
    result = {"argv": redact(source["argv"], config.secrets), "cwd": ".", "source": _summary(source, runtime=True),
              "latest_same_check": None if latest is None else _summary(latest, runtime=True),
              "rerun_available": tuple(source["argv"]) in config.checks,
              "tracked_files_changed_between_runs": _file_changes(config, original.get("before"), recent.get("before")),
              "tracked_files_changed_during_source": _file_changes(config, original.get("before"), original.get("after")),
              "tracked_files_changed_during_latest": _file_changes(config, recent.get("before"), recent.get("after"))}
    budget = min(config.max_output, 16384)
    for label, row in (("source", source), ("latest_same_check", latest)):
        if row is None:
            continue
        data = row["result"]
        text = data.get("output") or data.get("error")
        if not isinstance(text, str) or not text:
            continue
        excerpt = terminal_text(redact(text, config.secrets)[-2048:])
        result[label]["output_excerpt"] = excerpt
        result[label]["excerpt_truncated"] = (len(text) > 2048 or data.get("output_truncated") is True
                                                or data.get("output_limited") is True)
    for entry in (result.get("latest_same_check"), result.get("source")):
        if len(json.dumps(result, **JSON_OPTIONS).encode("utf-8")) <= budget:
            return result
        if entry and entry.pop("output_excerpt", None) is not None:
            entry["excerpt_truncated"] = True
    try:
        encoded_size(result, max_bytes=budget)
    except JsonBudgetExceeded:
        raise ValueError(f"fixed check card exceeds the display budget; use /history {position}") from None
    return result


def _code_block(text, language="text"):
    runs = [max(map(len, re.findall(pattern, text)), default=0) for pattern in (r"`+", r"~+")]
    mark, length = ("`", runs[0]) if runs[0] <= runs[1] else ("~", runs[1])
    fence = mark * max(3, length + 1)
    return fence + language + "\n" + text + "\n" + fence


def markdown(report):
    lines = ["# Fixed check reproduction", "", "The command ran from the workspace root with merged stdout/stderr.",
             "Each exit code describes a single check.", "", "## Command argv", "",
             _code_block(json.dumps(report["argv"], ensure_ascii=False), "json")]
    for title, row in (("Original failure", report["source"]), ("Latest later observation", report["latest_same_check"])):
        lines += ["", "## " + title, ""]
        if row is None:
            lines.append("No later observation of this command.")
            continue
        lines.append(f"Result message {row['message']} · {row['phase']} · {row['outcome']}")
        if "exit_code" in row:
            lines.append(f"Exit code {row['exit_code']}")
        if "sprig_runtime" in row:
            runtime = row["sprig_runtime"]
            lines.append(f"Recorded {row['recorded_at_utc']} · Sprig host {runtime['platform']}, Python {runtime['python']}")
        if row.get("output_excerpt"):
            lines += ["", _code_block(row["output_excerpt"])]
        if row.get("excerpt_truncated"):
            lines.append("Output excerpt is incomplete.")
    lines += ["", "## Tracked file contents", ""]
    for title, field in (("During original check", "tracked_files_changed_during_source"),
                         ("Between checks", "tracked_files_changed_between_runs"),
                         ("During latest check", "tracked_files_changed_during_latest")):
        observed = report[field]
        if observed:
            lines += ["", title + " · changed files", "", _code_block(json.dumps(observed, ensure_ascii=False), "json")]
        else:
            lines += ["", title + (" · unavailable" if observed is None else " · unchanged")]
    return "\n".join(lines) + "\n"


def export(agent, messages, position, path):
    from .session import MAX_HISTORY_MESSAGES
    if len(messages or []) >= MAX_HISTORY_MESSAGES:
        raise ValueError("session history is full; preview with /repro report instead")
    text = markdown(card(agent.config, messages, position))
    result = agent.tools.execute("edit_file", {"path": path, "old": "", "new": text})
    return {**result, "action": "export check reproduction report", "path": result.get("path", path)}
