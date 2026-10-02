"""Bounded numeric request measurements; never retain prompts or response text."""

from collections import deque
import copy
import time

MAX_RECORDS = 128
MAX_TOKENS = 10**9
TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")


def token_usage(value):
    if not isinstance(value, dict):
        return None
    result = {key: value.get(key) for key in TOKEN_FIELDS}
    if all(number is None for number in result.values()):
        return None
    if any(number is not None and (type(number) is not int or not 0 <= number <= MAX_TOKENS)
           for number in result.values()):
        return None
    if all(number is not None for number in result.values()):
        if result["prompt_tokens"] + result["completion_tokens"] != result["total_tokens"]:
            return None
    # Keep missing fields unknown. Do not infer a total or copy other metadata.
    return result


class RequestMeasurement:
    def __init__(self, streamed):
        self.streamed = streamed
        self.started = self.first_text = None
        self.request_bytes = self.response_bytes = 0
        self.usage = None
        self.usage_invalid = False
        self.status = None
        self.outcome = "error"

    def begin(self, request_bytes):
        self.request_bytes = request_bytes
        self.started = time.perf_counter()

    def text_received(self):
        if self.first_text is None:
            self.first_text = time.perf_counter()

    def observe_usage(self, value):
        if value is None:
            return  # Normal streaming chunks contain usage: null.
        normalized = token_usage(value)
        if normalized is None or (self.usage is not None and self.usage != normalized):
            self.usage_invalid = True
        elif not self.usage_invalid:
            self.usage = normalized

    def finish(self):
        elapsed = None if self.started is None else max(0, time.perf_counter() - self.started) * 1000
        first = None if self.first_text is None or self.started is None else max(0, self.first_text - self.started) * 1000
        return {"attempted": self.started is not None, "streamed": self.streamed, "outcome": self.outcome,
                "http_status": self.status, "request_bytes": self.request_bytes, "response_bytes": self.response_bytes,
                "elapsed_ms": round(elapsed, 3) if elapsed is not None else None,
                "first_text_ms": round(first, 3) if first is not None else None,
                "usage": None if self.usage_invalid else self.usage}


class MeasuredResponse:
    def __init__(self, response, measurement):
        self.response, self.measurement = response, measurement

    def read(self, size):
        data = self.response.read(size)
        self.measurement.response_bytes += len(data)
        return data

    def read1(self, size):
        data = self.response.read1(size)
        self.measurement.response_bytes += len(data)
        return data


class Measurements:
    def __init__(self):
        self.recent = deque(maxlen=MAX_RECORDS)
        self.attempts = self.not_attempted = 0
        self.outcomes = {key: 0 for key in ("success", "error", "cancelled")}
        self.request_bytes = self.response_bytes = 0
        self.elapsed_ms = 0
        self.tokens = {key: 0 for key in TOKEN_FIELDS}
        self.known = {key: 0 for key in TOKEN_FIELDS}

    def add(self, record):
        self.recent.append(copy.deepcopy(record))
        if not record["attempted"]:
            self.not_attempted += 1
            return
        self.attempts += 1
        self.outcomes[record["outcome"]] += 1
        self.request_bytes += record["request_bytes"]
        self.response_bytes += record["response_bytes"]
        self.elapsed_ms += record["elapsed_ms"]
        for key in TOKEN_FIELDS:
            number = (record["usage"] or {}).get(key)
            if number is not None:
                self.tokens[key] += number
                self.known[key] += 1

    def report(self):
        return {"available": True, "scope": "current provider instance; not persisted or sent to the model",
                "attempts": self.attempts, "not_attempted": self.not_attempted, "outcomes": dict(self.outcomes),
                "request_bytes": self.request_bytes, "response_bytes": self.response_bytes,
                "elapsed_ms": round(self.elapsed_ms, 3),
                "usage": {key: {"reported_sum": self.tokens[key], "known_requests": self.known[key],
                                "unknown_requests": self.attempts - self.known[key],
                                "complete_sum": self.tokens[key] if self.known[key] == self.attempts else None}
                          for key in TOKEN_FIELDS},
                "recent_limit": self.recent.maxlen, "recent_dropped": max(0, self.attempts + self.not_attempted - len(self.recent)),
                "recent": copy.deepcopy(list(self.recent))}
