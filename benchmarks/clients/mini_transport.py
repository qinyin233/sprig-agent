"""Bound the actual SDK HTTP bodies; export numbers, never headers or text."""

import json

import httpx

from sprig_agent.metrics import Measurements, RequestMeasurement
from sprig_agent.provider import MAX_RESPONSE


class TransportPolicyError(Exception):
    def __init__(self, reason):
        super().__init__("reference transport stopped by a benchmark limit")
        self.reason = reason


class MeasuredTransport(httpx.BaseTransport):
    def __init__(self, config, *, inner=None):
        self.inner = inner if inner is not None else httpx.HTTPTransport(retries=0, trust_env=False)
        self.url = httpx.URL(config.base_url + "/chat/completions")
        self.max_request_bytes = config.max_context_bytes
        self.measurements = Measurements()
        self.last_failure = None

    def stop(self, reason):
        self.last_failure = reason
        raise TransportPolicyError(reason) from None

    def handle_request(self, request):
        measurement = RequestMeasurement(False)
        response = None
        self.last_failure = None
        try:
            if request.method != "POST" or request.url != self.url:
                self.stop("endpoint_rejected")
            try:
                # The SDK serializes JSON before send. Reject unknown streams
                # rather than reading an unbounded body to estimate its size.
                measurement.request_bytes = len(request.content)
            except httpx.RequestNotRead:
                self.stop("request_body_rejected")
            if measurement.request_bytes > self.max_request_bytes:
                self.stop("request_budget_exceeded")
            measurement.begin(measurement.request_bytes)
            response = self.inner.handle_request(request)
            measurement.status = response.status_code
            if 300 <= response.status_code < 400:
                self.stop("redirect_rejected")
            if not 200 <= response.status_code < 300:
                # Do not give upstream exception handlers remote error bodies.
                self.stop("http_error")
            if response.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
                # Request identity and reject encodings before decompression;
                # a compressed bomb must not expand inside the SDK/HTTPX.
                self.stop("content_encoding_rejected")
            body = bytearray()
            for chunk in response.iter_raw():
                measurement.response_bytes += len(chunk)
                remaining = MAX_RESPONSE + 1 - len(body)
                body.extend(chunk[:remaining])
                if len(body) > MAX_RESPONSE:
                    self.stop("response_budget_exceeded")
            try:
                data = json.loads(body)
            except (ValueError, UnicodeError, RecursionError):
                data = None  # Protocol parsing remains the SDK's responsibility.
            if isinstance(data, dict):
                measurement.observe_usage(data.get("usage"))
            measurement.outcome = "success"
            # Body already consumed under the cap. Do not pass remote headers
            # (cookies/redirects/encoding) into the SDK or retain them in metrics.
            return httpx.Response(response.status_code, content=bytes(body),
                                  headers={"Content-Type": "application/json"})
        except KeyboardInterrupt:
            measurement.outcome = "cancelled"
            raise
        except httpx.TimeoutException:
            measurement.error_code = "timeout"
            self.stop("timeout")
        except httpx.TransportError:
            measurement.error_code = "connection_error"
            self.stop("connection_error")
        finally:
            try:
                if response is not None:
                    response.close()
            finally:
                record = measurement.finish()
                record["policy_failure"] = self.last_failure
                self.measurements.add(record)

    def report(self):
        report = self.measurements.report()
        report["scope"] = "mini HTTP transport; complete bounded body is transport success, SDK validation is separate"
        report["body_encoding"] = "identity; HTTP headers excluded"
        return report

    def close(self):
        self.inner.close()
