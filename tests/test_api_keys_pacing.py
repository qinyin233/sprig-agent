"""Origin-bound environment credentials and opt-in, non-retrying pacing."""

import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sprig_agent.config import environment_api_key

spec = importlib.util.spec_from_file_location("benchmark_pacing", ROOT / "benchmarks/clients/request_pacing.py")
pacing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pacing)


class KeySelectionTests(unittest.TestCase):
    def test_explicit_key_wins_and_agnes_key_precedes_generic_key_at_its_origin(self):
        values = {"SPRIG_API_KEY": "explicit", "AGNES_API_KEY": "agnes", "OPENAI_API_KEY": "generic"}
        self.assertEqual(environment_api_key("https://apihub.agnes-ai.com/v1", values), "explicit")
        del values["SPRIG_API_KEY"]
        for base in ("https://apihub.agnes-ai.com/v1", "https://APIHUB.AGNES-AI.COM:443/v1/"):
            self.assertEqual(environment_api_key(base, values), "agnes")
        self.assertEqual(environment_api_key("https://other.example/v1", values), "generic")

    def test_service_alias_never_reaches_another_origin_or_loopback(self):
        values = {"AGNES_API_KEY": "agnes"}
        for base in ("https://other.example/v1", "http://127.0.0.1/v1", "http://apihub.agnes-ai.com/v1",
                     "https://apihub.agnes-ai.com:8443/v1", "https://apihub.agnes-ai.com.evil.example/v1",
                     "https://apihub.agnes-ai.com@evil.example/v1", "https://[malformed"):
            with self.subTest(base=base):
                self.assertEqual(environment_api_key(base, values), "")
        self.assertEqual(environment_api_key("https://apihub.agnes-ai.com/v1", {}), "")


class PacingTests(unittest.TestCase):
    def clock(self):
        self.now, self.delays = 0.0, []
        def sleep(delay):
            self.delays.append(delay)
            self.now += delay
        return lambda: self.now, sleep

    def test_first_start_is_immediate_and_later_starts_obey_interval(self):
        clock, sleep = self.clock()
        pacer = pacing.RequestPacer(20, clock=clock, sleep=sleep)
        pacer.wait()
        self.assertFalse(self.delays)
        self.now = 7
        pacer.wait()
        self.assertEqual(self.delays, [13])
        self.now = 45
        pacer.wait()
        self.assertEqual(self.delays, [13])
        self.assertEqual(pacer.report()["wait_ms"], 13000)
        self.assertEqual(pacer.report()["backend_starts"], 3)

    def test_default_uses_no_clock_or_sleep_and_invalid_values_are_rejected(self):
        fail = Mock(side_effect=AssertionError("default pacing must be idle"))
        pacer = pacing.RequestPacer(clock=fail, sleep=fail)
        pacer.wait()
        fail.assert_not_called()
        for value in (True, -1, 61, float("nan"), float("inf"), "20", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                pacing.RequestPacer(value)

    def test_cancellation_while_waiting_does_not_start_another_backend_call(self):
        clock, _ = self.clock()
        def interrupted(delay):
            self.now += 2
            raise KeyboardInterrupt
        pacer = pacing.RequestPacer(20, clock=clock, sleep=interrupted)
        pacer.wait()
        with self.assertRaises(KeyboardInterrupt):
            pacer.wait()
        self.assertEqual(pacer.starts, 1)
        self.assertEqual(pacer.report()["wait_ms"], 2000)

    def test_proxy_preserves_callbacks_measurements_errors_and_never_retries(self):
        provider = Mock()
        marker = object()
        provider.measurements = marker
        provider.complete.side_effect = RuntimeError("fixture failure")
        pacer = pacing.RequestPacer()
        proxy = pacing.PacedProvider(provider, pacer)
        callback = Mock()
        with self.assertRaises(RuntimeError):
            proxy.complete([], [], on_text=callback)
        provider.complete.assert_called_once_with([], [], on_text=callback)
        self.assertIs(proxy.measurements, marker)
        self.assertEqual(pacer.starts, 1)
        self.assertFalse(pacer.report()["automatic_retries"])


if __name__ == "__main__":
    unittest.main()
