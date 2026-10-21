"""Bounded process-memory credentials; excluded from public settings and bindings."""
import threading


class RuntimeCredentials:
    LIMIT = 1024

    def __init__(self):
        self._values = set()
        self._lock = threading.Lock()

    def snapshot(self):
        with self._lock:
            return tuple(self._values)

    def available(self):
        with self._lock:
            return len(self._values) < self.LIMIT

    def remember(self, value):
        if not value:
            return
        with self._lock:
            if value not in self._values and len(self._values) >= self.LIMIT:
                raise ValueError("runtime credential capacity reached; restart with a new configuration")
            self._values.add(value)
