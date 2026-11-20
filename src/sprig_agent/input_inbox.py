"""Bounded user inputs and local delivery identities, never action authority."""
from dataclasses import dataclass
import hashlib
import json
import re
import threading
import uuid
from .json_budget import JsonBudgetExceeded, encoded_size
from .config import terminal_text
from .requirements import REQUIREMENT_KEY, change as requirement_change

MAX_INPUTS = 32
MAX_INPUT_BYTES = 2 * 1024 * 1024
INPUT_ID = "sprig_input_id"


def _output_size(value, budget):
    encoded_size(value, max_bytes=budget, indent=2)
    size = len(terminal_text(json.dumps(value, ensure_ascii=False, indent=2)).encode("utf-8"))
    if size > budget:
        raise JsonBudgetExceeded
    return size


class InputChanged(ValueError):
    """A local review snapshot is stale; no mutation was attempted."""


def input_revision(state):
    return hashlib.sha256(json.dumps(input_state(state), ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def change_inputs(state, action, identity, *, text=None, position=None, delivery=None):
    state = input_state(state)
    index = next((index for index, row in enumerate(state["pending"]) if row["id"] == identity), None)
    if index is None:
        raise InputChanged("input is no longer pending; refresh before reviewing another change")
    result = {"action": action, "input_id": identity, "network_contacted": False, "task_executed": False}
    if action == "edit":
        replacement = new_input(text, delivery=state["pending"][index].get("delivery", "steer"),
                                requirement=state["pending"][index].get("requirement", False))
        state["pending"][index] = replacement.row()
        result["replacement_id"] = replacement.id
    elif action == "delivery":
        replacement = new_input(state["pending"][index]["text"], delivery=delivery,
                                requirement=state["pending"][index].get("requirement", False))
        state["pending"][index] = replacement.row()
        result.update(replacement_id=replacement.id, delivery=delivery)
    elif action == "drop":
        state["pending"].pop(index)
    elif action == "move":
        if type(position) is not int or not 1 <= position <= len(state["pending"]):
            raise ValueError("position must identify a pending input, starting at 1")
        state["pending"].insert(position - 1, state["pending"].pop(index))
        result["position"] = position
    else:
        raise ValueError("unknown pending input mutation")
    return input_state(state), result


def pending_command(argument):
    """Shared local command grammar; edit text is one JSON string, never a shell."""
    if len(argument) > MAX_INPUT_BYTES * 6 + 256:
        raise ValueError("pending command exceeds the encoded text limit")
    parts = argument.split(None, 2)
    if not parts:
        return {"action": "preview"}
    if parts[0] in {"apply", "discard", "manage"} and len(parts) == 1:
        return {"action": parts[0]}
    if len(parts) >= 2 and re.fullmatch(r"i[0-9a-f]{32}", parts[1]):
        request = {"action": parts[0], "identity": parts[1]}
        if parts[0] == "show" and len(parts) in (2, 3):
            request["offset"] = int(parts[2]) if len(parts) == 3 else 0
            return request
        if parts[0] == "drop" and len(parts) == 2:
            return request
        if parts[0] == "move" and len(parts) == 3:
            request["position"] = int(parts[2])
            return request
        if parts[0] == "delivery" and len(parts) == 3 and parts[2] in {"steer", "queue"}:
            request["delivery"] = parts[2]
            return request
        if parts[0] == "edit" and len(parts) == 3:
            try:
                request["text"] = json.loads(parts[2])
                UserInput(parts[1], request["text"])
            except (ValueError, TypeError, RecursionError):
                raise ValueError("edit expects a nonempty UTF-8 JSON string of at most 2 MiB") from None
            return request
    raise ValueError('Use /pending [apply|discard|manage], show ID [CHAR_OFFSET], edit ID "JSON text", drop ID, move ID POSITION or delivery ID steer|queue')


def queue_command(argument):
    """Explicit local dispatch; JSON add preserves whitespace and reserved words."""
    if len(argument) > MAX_INPUT_BYTES * 6 + 256:
        raise ValueError("queue command exceeds the encoded input limit")
    command = argument.strip()
    if not command:
        return {"action": "preview"}
    if command in {"run", "pause"}:
        return {"action": command}
    try:
        text = json.loads(command[4:]) if command.startswith("add ") else argument
    except (ValueError, TypeError, RecursionError):
        raise ValueError("queue add requires one UTF-8 JSON string") from None
    UserInput("i" + "0" * 32, text, "queue")
    return {"action": "add", "text": text}


def check_input_growth(expected, candidate, limit):
    size = sum(len(row["text"].encode("utf-8")) for row in input_state(candidate)["pending"])
    previous = sum(len(row["text"].encode("utf-8")) for row in input_state(expected)["pending"])
    if size > limit and size > previous:
        raise ValueError("pending input change exceeds the context byte limit; original inputs retained")


def input_page(state, identity, offset=0, budget=4096):
    row = next((row for row in input_state(state)["pending"] if row["id"] == identity), None)
    if row is None:
        raise InputChanged("input is no longer pending; refresh the list")
    text = row["text"]
    if type(offset) is not int or not 0 <= offset <= len(text):
        raise ValueError("pending input character offset is out of range")
    def page(length):
        end = offset + length
        return {**({"delivery": "queue"} if row.get("delivery") == "queue" else {}), "input_id": identity, "offset": offset, "text": text[offset:end],
                **({"requirement": True} if row.get("requirement") else {}),
                "next_offset": end if end < len(text) else None, "complete": end == len(text),
                "characters": len(text), "network_contacted": False}
    low, high = 0, min(len(text) - offset, 8192)
    while low < high:
        middle = (low + high + 1) // 2
        try:
            _output_size(page(middle), budget)
        except JsonBudgetExceeded:
            high = middle - 1
        else:
            low = middle
    result = page(low)
    _output_size(result, budget)
    if not low and not result["complete"]:
        raise ValueError("pending input page metadata exceeds output budget")
    return result


def input_change_prompt(expected, candidate, result):
    """Small review with fingerprints for the full, explicitly edited local text."""
    identity = result["input_id"]
    before = input_page(expected, identity, budget=768)
    after = (input_page(candidate, result["replacement_id"], budget=768)
             if "replacement_id" in result else None)
    return ("Change local pending user input; no model request or workspace action.\n"
            + json.dumps({**result, "before": before, "after": after,
                          "before_revision": input_revision(expected), "after_revision": input_revision(candidate)},
                         ensure_ascii=False)
            + "\nPreviews may be partial. Only this reviewed local queue change is approved; tool actions still need fresh approval.")


@dataclass(frozen=True)
class UserInput:
    id: str
    text: str
    delivery: str = "steer"
    requirement: bool = False

    def __post_init__(self):
        if not isinstance(self.delivery, str) or self.delivery not in {"steer", "queue"}:
            raise ValueError("user input delivery must be steer or queue")
        if not isinstance(self.id, str) or not re.fullmatch(r"i[0-9a-f]{32}", self.id):
            raise ValueError("invalid user input identity")
        if (not isinstance(self.text, str) or not self.text.strip() or len(self.text) > MAX_INPUT_BYTES
                or len(self.text.encode("utf-8")) > MAX_INPUT_BYTES):
            raise ValueError("user input must be nonempty UTF-8 text of at most 2 MiB")
        if type(self.requirement) is not bool or (self.requirement and self.delivery != "steer"):
            raise ValueError("a tracked requirement change must be delivered as steering")
        if self.requirement:
            requirement_change(self.text)

    def row(self):
        row = {"id": self.id, "text": self.text}
        if self.delivery == "queue":
            row["delivery"] = "queue"
        if self.requirement:
            row["requirement"] = True
        return row


def new_input(text, *, delivery="steer", requirement=False):
    return UserInput("i" + uuid.uuid4().hex, text, delivery, requirement)


def input_state(value=None):
    if value is None:
        return {"version": 1, "pending": []}
    if (not isinstance(value, dict) or set(value) != {"version", "pending"}
            or type(value["version"]) is not int or value["version"] != 1
            or not isinstance(value["pending"], list) or len(value["pending"]) > MAX_INPUTS):
        raise ValueError("invalid input inbox state")
    rows, seen, size = [], set(), 0
    for row in value["pending"]:
        if not isinstance(row, dict) or not {"id", "text"} <= row.keys() <= {"id", "text", "delivery", "requirement"}:
            raise ValueError("invalid queued user input")
        entry = UserInput(**row)
        if entry.id in seen:
            raise ValueError("duplicate queued user input identity")
        seen.add(entry.id)
        size += len(entry.text.encode("utf-8"))
        if size > MAX_INPUT_BYTES:
            raise ValueError("queued user input exceeds 2 MiB")
        rows.append(entry.row())
    return {"version": 1, "pending": rows}


def delivered_inputs(messages, *, typed=False):
    result = {}
    for message in messages if isinstance(messages, list) else ():
        if not isinstance(message, dict) or INPUT_ID not in message:
            continue
        if message.get("role") != "user":
            raise ValueError("only user messages can identify delivered user input")
        entry = UserInput(message[INPUT_ID], message.get("content"), requirement=message.get(REQUIREMENT_KEY, False))
        if entry.id in result:
            raise ValueError("duplicate delivered user input identity")
        result[entry.id] = (entry.text, entry.requirement) if typed else entry.text
    return result


def remaining_inputs(state, messages):
    state, delivered = input_state(state), delivered_inputs(messages, typed=True)
    pending = []
    for row in state["pending"]:
        if row["id"] in delivered:
            if (row["text"], row.get("requirement", False)) != delivered[row["id"]]:
                raise ValueError("user input identity conflicts with delivered text")
        else:
            pending.append(row)
    return {"version": 1, "pending": pending}


class InputInbox:
    def __init__(self, state=None):
        self._lock = threading.RLock()
        self._state = input_state(state)
        self._claimed = set()
        self.blocked = False
        self._version = 0

    def snapshot(self):
        with self._lock:
            return input_state(self._state)

    def append(self, entry):
        if not isinstance(entry, UserInput):
            raise ValueError("structured user input is required")
        with self._lock:
            previous = next((r for r in self._state["pending"] if r["id"] == entry.id), None)
            if previous is not None:
                if previous != entry.row():
                    raise ValueError("user input identity was reused with different text")
                return False
            self._state = input_state({"version": 1, "pending": [*self._state["pending"], entry.row()]})
            self._version += 1
            return True

    def batch(self, count=None, *, delivery=None):
        with self._lock:
            rows = [row for row in self._state["pending"] if delivery is None or row.get("delivery", "steer") == delivery]
            rows = rows if count is None else rows[:count]
            return [UserInput(**row) for row in rows]

    def claim(self, count=None, *, delivery=None):
        with self._lock:
            if self.blocked:
                raise RuntimeError("pending input save is uncertain; restore this session before continuing")
            entries = self.batch(count, delivery=delivery)
            previous = len(self._claimed)
            self._claimed.update(entry.id for entry in entries)
            if len(self._claimed) != previous:
                self._version += 1
            return entries

    def release(self, entries):
        """Release a delivery that failed before entering owned history."""
        with self._lock:
            previous = len(self._claimed)
            self._claimed.difference_update(entry.id for entry in entries)
            if len(self._claimed) != previous:
                self._version += 1

    def version(self):
        with self._lock:
            return self._version

    def claimed(self):
        with self._lock:
            return frozenset(self._claimed)

    def revise(self, expected, candidate, persist=lambda: None):
        """Reserve delivery across durable mutation; never publish an uncertain write."""
        candidate = input_state(candidate)
        with self._lock:
            if self.blocked:
                raise RuntimeError("pending input save is uncertain; restore this session before editing")
            if self._claimed or input_revision(self._state) != input_revision(expected):
                raise InputChanged("pending inputs changed or are being delivered; refresh before editing")
            try:
                persist()
            except InputChanged:
                raise
            except BaseException:
                self.blocked = True
                raise
            self._state = candidate
            self._version += 1

    def acknowledge(self, messages):
        with self._lock:
            candidate = remaining_inputs(self._state, messages)
            if candidate != self._state:
                self._version += 1
            self._state = candidate
            self._claimed.intersection_update(row["id"] for row in self._state["pending"])


def input_preview(state, budget=4096):
    """Bounded local preview; inbox identities carry no action authority."""
    rows = input_state(state)["pending"]
    result = {"pending_inputs": len(rows), "items": [], "limited": False, "network_contacted": False}
    for index, row in enumerate(rows, 1):
        text = row["text"].encode("utf-8")[:256].decode("utf-8", errors="ignore")
        while text:
            candidate = {"id": row["id"], "position": index, "preview": text, "truncated": text != row["text"]}
            if row.get("delivery") == "queue":
                candidate["delivery"] = "queue"
            if row.get("requirement"):
                candidate["requirement"] = True
            try:
                _output_size({**result, "items": [*result["items"], candidate]}, budget)
            except JsonBudgetExceeded:
                text = text[:len(text) // 2]
            else:
                result["items"].append(candidate)
                break
        if not text:
            break
    result["limited"] = len(result["items"]) < len(rows) or any(row["truncated"] for row in result["items"])
    _output_size(result, budget)
    return result
