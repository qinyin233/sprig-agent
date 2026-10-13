"""Exercise the private probe through actual local HTTP, not a live model."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("result_probe", Path(__file__).resolve().parents[1] / "scripts/probe-tool-results.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ToolResultProbeTests(unittest.TestCase):
    def test_random_marker_is_only_in_observation_and_report_remains_private(self):
        observed = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                last = data["messages"][-1]
                if last["role"] == "user":
                    text = json.loads(last["content"].split("\n", 1)[1])["content"]
                elif last["content"].startswith("{"):
                    text = json.loads(last["content"])["content"]
                else:
                    text = last["content"]
                marker = text.strip().split("=", 1)[1]
                observed.append({"marker": marker, "task": data["messages"][1]["content"], "role": last["role"],
                                 "auth": self.headers.get("Authorization") == "Bearer private-fixture-credential"})
                body = json.dumps({"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": marker}}],
                                   "usage": {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            report = probe.probe({"model": "fixture", "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                                  "api_key": "private-fixture-credential"}, direct=True, interval=0)
        finally:
            server.shutdown(); server.server_close(); thread.join(5)
        self.assertEqual([row["role"] for row in observed], ["tool", "tool", "user"])
        self.assertEqual(len({row["marker"] for row in observed}), 3)
        rendered = json.dumps(report)
        for row in observed:
            self.assertTrue(row["auth"])
            self.assertNotIn(row["marker"], row["task"])
            self.assertNotIn(row["marker"], rendered)
        self.assertNotIn("private-fixture-credential", rendered)
        self.assertTrue(all(row["exact_match"] and row["returned_tool_calls"] == 0 for row in report["results"]))
        self.assertTrue(all(row["requests"]["attempts"] == 1 for row in report["results"]))
        self.assertEqual(report["request_pacing"]["backend_starts"], 3)

    def test_existing_report_is_rejected_before_key_access_or_model_call(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text("preserve", encoding="utf-8")
            with patch.object(sys, "argv", ["probe", "--run", "--report", str(path)]), \
                    patch.object(probe, "environment_api_key") as key, patch.object(probe, "probe") as run, \
                    patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit):
                    probe.main()
                key.assert_not_called()
                run.assert_not_called()
            self.assertEqual(path.read_text(encoding="utf-8"), "preserve")


if __name__ == "__main__":
    unittest.main()
