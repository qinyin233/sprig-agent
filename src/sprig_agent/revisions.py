"""Short references to validated reads; content hashes remain the write guard."""

from collections import OrderedDict
import re
import uuid

MAX_READ_VERSIONS = 128
READ_ID_PATTERN = r"^r[0-9a-f]{12}-[1-9][0-9]*$"


def valid_read_id(value):
    return isinstance(value, str) and len(value) <= 64 and re.fullmatch(READ_ID_PATTERN, value) is not None


class ReadVersions:
    def __init__(self):
        self._entries = OrderedDict()
        self._prefix = "r" + uuid.uuid4().hex[:12] + "-"
        self._sequence = 0

    def clear(self):
        # Do not reuse identifiers after a new task, command or session reset.
        self._entries.clear()

    def discard(self, parts):
        for identity, (path, _) in list(self._entries.items()):
            if path == tuple(parts):
                del self._entries[identity]

    def snapshot(self):
        return tuple(self._entries.items())

    def restore_verified(self, entries):
        if not self._entries:
            self._entries.update(entries)

    def remember(self, parts, digest, *, validate=None):
        path = tuple(parts)
        for identity, recorded in list(self._entries.items()):
            if recorded == (path, digest):
                if validate is not None:
                    validate(identity)
                self._entries.move_to_end(identity)
                return identity
        identity = self._prefix + str(self._sequence + 1)
        if validate is not None:
            validate(identity)
        self._sequence += 1
        self._entries[identity] = (path, digest)
        if len(self._entries) > MAX_READ_VERSIONS:
            self._entries.popitem(last=False)
        return identity

    def resolve(self, identity, parts):
        value = self._entries.get(identity)
        if value is None or value[0] != tuple(parts):
            return None
        self._entries.move_to_end(identity)
        return value[1]
