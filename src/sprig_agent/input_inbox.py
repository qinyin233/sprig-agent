"""Bounded user inputs and local delivery identities, never action authority."""
from dataclasses import dataclass
import re
import threading
import uuid
from .json_budget import JsonBudgetExceeded, encoded_size

MAX_INPUTS = 32
MAX_INPUT_BYTES = 2 * 1024 * 1024
INPUT_ID = "sprig_input_id"


@dataclass(frozen=True)
class UserInput:
    id: str
    text: str

    def __post_init__(self):
        if not isinstance(self.id, str) or not re.fullmatch(r"i[0-9a-f]{32}", self.id):
            raise ValueError("invalid user input identity")
        if (not isinstance(self.text, str) or not self.text.strip() or len(self.text) > MAX_INPUT_BYTES
                or len(self.text.encode("utf-8")) > MAX_INPUT_BYTES):
            raise ValueError("user input must be nonempty UTF-8 text of at most 2 MiB")

    def row(self):
        return {"id": self.id, "text": self.text}


def new_input(text):
    return UserInput("i" + uuid.uuid4().hex, text)


def input_state(value=None):
    if value is None:
        return {"version": 1, "pending": []}
    if (not isinstance(value, dict) or set(value) != {"version", "pending"}
            or type(value["version"]) is not int or value["version"] != 1
            or not isinstance(value["pending"], list) or len(value["pending"]) > MAX_INPUTS):
        raise ValueError("invalid input inbox state")
    rows, seen, size = [], set(), 0
    for row in value["pending"]:
        if not isinstance(row, dict) or set(row) != {"id", "text"}:
            raise ValueError("invalid queued user input")
        entry = UserInput(row["id"], row["text"])
        if entry.id in seen:
            raise ValueError("duplicate queued user input identity")
        seen.add(entry.id)
        size += len(entry.text.encode("utf-8"))
        if size > MAX_INPUT_BYTES:
            raise ValueError("queued user input exceeds 2 MiB")
        rows.append(entry.row())
    return {"version": 1, "pending": rows}


def delivered_inputs(messages):
    result = {}
    for message in messages if isinstance(messages, list) else ():
        if not isinstance(message, dict) or INPUT_ID not in message:
            continue
        if message.get("role") != "user":
            raise ValueError("only user messages can identify delivered user input")
        entry = UserInput(message[INPUT_ID], message.get("content"))
        if entry.id in result:
            raise ValueError("duplicate delivered user input identity")
        result[entry.id] = entry.text
    return result


def remaining_inputs(state, messages):
    state, delivered = input_state(state), delivered_inputs(messages)
    pending = []
    for row in state["pending"]:
        if row["id"] in delivered:
            if row["text"] != delivered[row["id"]]:
                raise ValueError("user input identity conflicts with delivered text")
        else:
            pending.append(row)
    return {"version": 1, "pending": pending}


class InputInbox:
    def __init__(self, state=None):
        self._lock = threading.RLock()
        self._state = input_state(state)

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
            return True

    def batch(self, count=None):
        with self._lock:
            rows = self._state["pending"] if count is None else self._state["pending"][:count]
            return [UserInput(**row) for row in rows]

    def acknowledge(self, messages):
        with self._lock:
            self._state = remaining_inputs(self._state, messages)


def input_preview(state, budget=4096):
    """Bounded local preview; inbox identities carry no action authority."""
    rows = input_state(state)["pending"]
    result = {"pending_inputs": len(rows), "items": [], "limited": bool(rows), "network_contacted": False}
    for row in rows:
        text = row["text"].encode("utf-8")[:256].decode("utf-8", errors="ignore")
        while text:
            candidate = {"preview": text, "truncated": text != row["text"]}
            try:
                encoded_size({**result, "items": [*result["items"], candidate]}, max_bytes=budget, indent=2)
            except JsonBudgetExceeded:
                text = text[:len(text) // 2]
            else:
                result["items"].append(candidate)
                break
        if not text or candidate["truncated"]:
            break
    result["limited"] = len(result["items"]) < len(rows) or any(row["truncated"] for row in result["items"])
    return result
