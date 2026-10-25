"""History-bound request revision proposals. No model request or action replay."""
from .config import redact
from .conversation_fork import ConversationFork, turn_catalog
from .json_budget import encoded_size
from .message_origin import user_request
from .session import MAX_SESSION, validate_history
from .summary import fingerprint


def revision_command(argument, limit):
    import json
    if not isinstance(argument, str) or len(argument) > limit * 6 + 64 or len(argument.encode("utf-8")) > limit * 6 + 64:
        raise ValueError("revision command exceeds the input limit")
    parts = argument.split(None, 1)
    if not parts:
        return None, None
    target = parts[0]
    if len(target) > 4 or not target.isascii() or not target.isdecimal() or not 1 <= int(target) <= 5000:
        raise ValueError('Use /revise MESSAGE ["JSON replacement text"]')
    body = None
    if len(parts) == 2:
        try:
            body = json.loads(parts[1])
        except (ValueError, RecursionError):
            raise ValueError("replacement must be one JSON string; values withheld") from None
        if not isinstance(body, str) or not body.strip() or len(body) > limit or len(body.encode("utf-8")) > limit:
            raise ValueError("replacement must be nonempty text within the task UTF-8 byte limit")
    return str(int(target)), body


class RevisionSource:
    def __init__(self, original, messages, target):
        original.tools.root.ensure_open()
        original._check_cancelled()
        if original._running or original.tools.config is not original.config or not messages:
            raise ValueError("wait for an idle valid conversation before revising a request")
        encoded_size(messages, max_bytes=MAX_SESSION, check_cancelled=original._check_cancelled)
        clean = validate_history(messages)
        target, _ = revision_command(str(target), original.config.max_context_bytes)
        row = next((row for row in turn_catalog(clean) if row["message"] == int(target)), None)
        if row is None or not user_request(clean[int(target) - 1]):
            raise ValueError("choose an exact real user message number from /timeline")
        self.original, self.source, self.target = original, messages, target
        self.version = original.tools._conversation_version
        self.source_hash = fingerprint(messages, len(messages), original._check_cancelled)
        self.body = redact(clean[int(target) - 1]["content"], original.config.secrets)

    def fresh(self):
        original = self.original
        original.tools.root.ensure_open()
        original._check_cancelled()
        if (original._running or original.tools.config is not original.config or original.tools._conversation_version != self.version
                or fingerprint(self.source, len(self.source), original._check_cancelled) != self.source_hash):
            raise ValueError("historical request changed; keep your draft and select the source again")

    def prepare(self, text, **options):
        self.fresh()
        candidate = ConversationFork(self.original, self.source, self.target, before=True, request=text, **options)
        if candidate.source_hash != self.source_hash or candidate.version != self.version:
            raise ValueError("revision source changed; select again")
        return candidate
