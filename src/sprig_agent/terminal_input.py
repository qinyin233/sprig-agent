"""Task-scoped terminal corrections; approval still returns literal booleans."""

from contextlib import contextmanager
from contextvars import ContextVar

from .config import redact
from .input_inbox import InputInbox, check_input_growth, input_state, new_input
from .session import SessionError


MAX_CORRECTION_BYTES = 4096
_feedback = ContextVar("sprig_terminal_feedback", default=None)


@contextmanager
def feedback_scope(receiver):
    token = _feedback.set(receiver)
    try:
        yield
    finally:
        _feedback.reset(token)


def current_feedback():
    return _feedback.get()


def correction_text(text):
    if not isinstance(text, str):
        raise ValueError("correction must be text")
    text = text.strip()
    if (not text or "\n" in text or "\r" in text or len(text) > MAX_CORRECTION_BYTES
            or len(text.encode("utf-8")) > MAX_CORRECTION_BYTES):
        raise ValueError("correction must be one nonempty UTF-8 line of at most 4096 bytes")
    return text


class TerminalInputs:
    def __init__(self, emit):
        self.emit = emit
        self.inbox = InputInbox()
        self.retry = None
        self.agent = self.store = self.name = self.history = None

    def bind(self, agent, store, name, history):
        self.agent, self.store, self.name, self.history = agent, store, name, history

    @property
    def ready(self):
        return self.retry is None and not self.inbox.blocked and not self.inbox.claimed()

    def state(self):
        try:
            state = self.store.pending_inputs(self.name) if self.store and self.name else self.inbox.snapshot()
        except FileNotFoundError:
            state = self.inbox.snapshot()
        if self.retry and not any(row["id"] == self.retry.id for row in state["pending"]):
            state = input_state({"version": 1, "pending": [*state["pending"], self.retry.row()]})
        return state

    def accept(self, text, *, delivery="steer", history=None):
        if self.inbox.blocked:
            raise SessionError("input save is uncertain; restore the session or explicitly discard pending inputs")
        text = redact(text, self.agent.config.secrets)
        if self.retry and (self.retry.text != text or self.retry.delivery != delivery):
            raise ValueError("retry the same input and delivery mode, or explicitly discard pending inputs")
        entry = self.retry or new_input(text, delivery=delivery)
        state = self.state()
        if not any(row["id"] == entry.id for row in state["pending"]):
            check_input_growth(state, {"version": 1, "pending": [*state["pending"], entry.row()]}, self.agent.config.max_context_bytes)
        self.retry = entry
        if self.store and self.name:
            history = self.history if history is None else history
            history = history if history is not None else [{"role": "system", "content": self.agent._prompt()}]
            self.store.enqueue_input(self.name, entry, history=history, **self.agent.reference_save_options())
        self.inbox.append(entry)
        self.retry = None
        return entry

    def submit(self, text):
        self.accept(correction_text(text))
        receipt = "saved" if self.store and self.name else "in process memory only"
        self.emit("[feedback] Current action rejected; correction received (" + receipt
                  + "). Delivery waits for a safe boundary; the model request budget is unchanged.")

    def receive(self):
        if not self.ready:
            raise SessionError("input receipt is uncertain; resolve /pending apply or /pending discard before continuing")
        count = 1 if self.agent.config.steering_mode == "one-at-a-time" else None
        return self.inbox.claim(count, delivery="steer")

    def checkpoint(self, history):
        self.history = history
        if self.store and self.name:
            self.store.save(self.name, history, **self.agent.reference_save_options())
        self.inbox.acknowledge(history)

    def revise(self, expected, candidate):
        if not self.ready or self.inbox.claimed():
            raise ValueError("pending inputs are being delivered or their receipt is uncertain; apply or discard first")
        if self.store and self.name:
            try:
                self.store.revise_inputs(self.name, expected, candidate)
            except BaseException:
                self.inbox.blocked = True
                raise
            self.inbox = InputInbox(candidate)
        else:
            self.inbox.revise(expected, candidate)

    def discard(self):
        count = len(self.state()["pending"])
        result = {"discarded_inputs": count, "network_contacted": False}
        if self.store and self.name:
            try:
                result = self.store.discard_inputs(self.name)
            except FileNotFoundError:
                result = {"discarded_inputs": 0, "network_contacted": False}
            except BaseException:
                self.inbox.blocked = True
                raise
        self.inbox, self.retry = InputInbox(), None
        return result
