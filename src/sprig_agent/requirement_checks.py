"""Application-owned receipts for checks the user selects for a requirement."""

from dataclasses import dataclass, field
import json
import re

from .json_budget import JsonBudgetExceeded, encoded_size
from .requirements import NAME, REQUIREMENT_KEY, change, state

CHECK_KEY = "sprig_requirement_check"
TERMINAL_STATES = {"passed", "failed", "blocked", "incomplete", "steered"}
UNRELIABLE_FLAGS = ("timed_out", "output_limited", "skipped", "interrupted", "cancelled")
USAGE = "Use /verify or /verify NAME CHECK [CHECK ...] with distinct configured check numbers (1–16)"


def valid_indices(indices):
    return (isinstance(indices, (tuple, list)) and 0 < len(indices) <= 16
            and all(type(index) is int and 1 <= index <= 16 for index in indices)
            and len(set(indices)) == len(indices))


def command(argument):
    if not argument.strip():
        return {}
    if len(argument) > 256:
        raise ValueError(USAGE)
    name, *raw = argument.split()
    if NAME.fullmatch(name) and all(re.fullmatch(r"[0-9]{1,2}", item) for item in raw):
        indices = tuple(map(int, raw))
        if valid_indices(indices):
            return {"requirement": name, "indices": indices}
    raise ValueError(USAGE)


@dataclass
class RequirementCheck:
    id: str
    source: int
    indices: tuple[int, ...]
    results: list[int] = field(default_factory=list)
    stamp: tuple | None = None

    def started(self):
        return {"id": self.id, "source": self.source, "checks": list(self.indices)}

    def finished(self, status, files):
        return {"id": self.id, "status": status, "results": self.results, "files": files}


def marker(value):
    """Validate private metadata before admitting it to saved history."""
    def position(number):
        return type(number) is int and 1 <= number <= 5000

    if isinstance(value, dict) and isinstance(value.get("id"), str) and re.fullmatch(r"[0-9a-f]{32}", value["id"]):
        if (set(value) == {"id", "source", "checks"} and position(value["source"])
                and isinstance(value["checks"], list) and valid_indices(value["checks"])):
            return value
        if set(value) == {"id", "status", "results", "files"}:
            results, files = value["results"], value["files"]
            if (isinstance(value["status"], str) and value["status"] in TERMINAL_STATES
                    and type(files) is int and 0 <= files <= 16
                    and isinstance(results, list) and len(results) <= 16 and all(map(position, results))
                    and all(left < right for left, right in zip(results, results[1:]))):
                return value
    raise ValueError("invalid tracked requirement check receipt")


def latest_attempt(messages, name):
    """Use original message positions so the displayed receipt can be audited."""
    attempt = None
    for position, row in enumerate(messages, 1):
        if CHECK_KEY not in row:
            continue
        entry = marker(row[CHECK_KEY])
        if "source" in entry:
            if entry["source"] >= position:
                raise ValueError("invalid tracked requirement check source")
            source = messages[entry["source"] - 1]
            if source.get(REQUIREMENT_KEY) is not True:
                raise ValueError("invalid tracked requirement check source")
            selected_name, text = change(source["content"])
            if text is None:
                raise ValueError("tracked requirement check refers to a withdrawal")
            if selected_name == name:
                attempt = {**entry, "start_message": position}
        elif attempt and attempt["id"] == entry["id"]:
            if "status" in attempt or len(entry["results"]) > len(attempt["checks"]):
                raise ValueError("invalid tracked requirement check results")
            if entry["status"] == "passed" and len(entry["results"]) != len(attempt["checks"]):
                raise ValueError("passed tracked requirement check has missing results")
            attempt.update(entry, end_message=position)
    return attempt


def report(messages, requirement, attempt, freshness, budget):
    result = {"name": requirement["name"], "current_revision": requirement["revision"], "checks": []}
    if attempt is None:
        result["verification"] = "not_run"
        return result
    status = attempt.get("status", "unfinished")
    result.update(verification=status, freshness=freshness, start_message=attempt["start_message"],
                  source_message=attempt["source"], files=attempt.get("files"),
                  checked_revision=state(messages[:attempt["source"]])[requirement["name"]]["revision"])
    for index, check in enumerate(attempt["checks"]):
        detail = {"number": check, "status": "unrecorded"}
        if index < len(attempt.get("results", ())):
            position = attempt["results"][index]
            if not attempt["start_message"] < position < attempt["end_message"]:
                raise ValueError("invalid tracked requirement check result location")
            call, message = messages[position - 2:position]
            calls = call.get("tool_calls", [])
            if (call.get("sprig_check") != "requirement" or len(calls) != 1
                    or message.get("role") != "tool" or calls[0]["id"] != message.get("tool_call_id")):
                raise ValueError("tracked requirement check result does not match its fixed command")
            outcome = json.loads(message["content"])
            if not isinstance(outcome, dict):
                raise ValueError("invalid tracked requirement check result")
            detail = {"number": check, "command_message": position - 1, "result_message": position,
                      "approved": outcome.get("approved") is True}
            if type(outcome.get("returncode")) is int:
                detail["returncode"] = outcome["returncode"]
            flags = {flag: True for flag in UNRELIABLE_FLAGS if outcome.get(flag) is True}
            detail.update(flags)
            if status == "passed" and (not detail["approved"] or detail.get("returncode") != 0
                                       or flags or "error" in outcome or outcome.get("outcome")):
                raise ValueError("passed tracked requirement check has unreliable results")
        result["checks"].append(detail)
    try:
        encoded_size(result, max_bytes=budget)
    except JsonBudgetExceeded:
        raise ValueError("check receipt exceeds the output budget; see /history " + str(attempt["start_message"])) from None
    return result
