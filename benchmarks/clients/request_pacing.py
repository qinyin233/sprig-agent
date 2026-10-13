"""Opt-in benchmark backend pacing; never retries a request."""

import math
import time
import hashlib
from pathlib import Path


class RequestPacer:
    def __init__(self, interval=0, *, clock=None, sleep=None):
        if (type(interval) not in (int, float) or not math.isfinite(interval)
                or not 0 <= interval <= 60):
            raise ValueError("request interval must be finite from 0 to 60 seconds")
        self.interval = float(interval)
        self.clock, self.sleep = clock or time.monotonic, sleep or time.sleep
        self.previous = None
        self.starts, self.wait_seconds = 0, 0.0

    def wait(self):
        if self.interval:
            now = self.clock()
            delay = 0 if self.previous is None else max(0, self.previous + self.interval - now)
            if delay:
                started = self.clock()
                try:
                    self.sleep(delay)
                finally:
                    self.wait_seconds += max(0, self.clock() - started)
            self.previous = self.clock()
        self.starts += 1

    def report(self):
        return {"interval_seconds": self.interval, "backend_starts": self.starts,
                "wait_ms": round(self.wait_seconds * 1000, 3),
                "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "scope": "backend invocation starts; HTTP may be blocked by validation", "automatic_retries": False}


class PacedProvider:
    def __init__(self, provider, pacer):
        self.provider, self.pacer = provider, pacer

    @property
    def measurements(self):
        return getattr(self.provider, "measurements", None)

    def complete(self, *args, **kwargs):
        self.pacer.wait()
        return self.provider.complete(*args, **kwargs)
