"""Bounded process-memory credentials; excluded from public settings and bindings."""
import threading
import re


class RuntimeCredentials:
    LIMIT = 1024

    def __init__(self):
        self._values = set()
        self._environment_names = set()
        self._lock = threading.Lock()

    def snapshot(self):
        with self._lock:
            return tuple(self._values)

    def available(self):
        with self._lock:
            return len(self._values) < self.LIMIT

    def environment_names(self):
        with self._lock:
            return tuple(self._environment_names)

    def remember_many(self, values, names=()):
        values, names = set(filter(None, values)), set(names)
        if (any(not isinstance(value, str) or len(value) > 65536 for value in values)
                or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name) for name in names)):
            raise ValueError("invalid runtime credential protection; values withheld")
        with self._lock:
            if len(self._values | values) > self.LIMIT or len(self._environment_names | names) > self.LIMIT:
                raise ValueError("runtime credential capacity reached; restart with a new configuration")
            self._values.update(values)
            self._environment_names.update(names)

    def remember(self, value):
        if not value:
            return
        with self._lock:
            if value not in self._values and len(self._values) >= self.LIMIT:
                raise ValueError("runtime credential capacity reached; restart with a new configuration")
            self._values.add(value)
