"""Real repository/oracle and loopback protocols; scripted replies are not model scores."""

import base64
from contextlib import contextmanager
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import shlex
import sys
import threading
import unittest

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("requests_integration_entry", ROOT / "scripts/benchmark-requests-task.py")
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)
from tests.test_native_protocols import native, stream_events, sse
from tests.test_generation_options import envelope, stream_body

OLD = "        InvalidJSONError.__init__(self, *self.args, **kwargs)\n"
NEW = OLD + "\n    def __reduce__(self):\n        return CompatJSONDecodeError.__reduce__(self)\n"


@contextmanager
def backend(protocol, calls, *, streamed=False):
    captured = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            captured.append({"path": self.path, "bytes": len(raw)})
            value = calls[len(captured) - 1]
            if protocol == "chat_completions":
                message = {"role": "assistant", "content": value if isinstance(value, str) else None}
                reason = "stop" if isinstance(value, str) else "tool_calls"
                if not isinstance(value, str):
                    identity, name, arguments = value
                    message["tool_calls"] = [{"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]
                if streamed:
                    delta = {key: item for key, item in message.items() if key != "role"}
                    for index, call in enumerate(delta.get("tool_calls", [])):
                        call["index"] = index
                    body = stream_body(delta, reason)
                else:
                    body = envelope(message, reason)
            else:
                data = native(protocol, value if isinstance(value, str) else None, () if isinstance(value, str) else [value])
                body = sse(stream_events(protocol, data)) if streamed else json.dumps(data).encode()
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
        yield f"http://127.0.0.1:{server.server_port}/v1", captured
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


class RequestsTaskIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sys.version_info[:2] != (3, 12):
            raise RuntimeError("use the locked Requests Python 3.12 environment")
        cls.fixture = Path(os.environ["SPRIG_REQUESTS_FIXTURE"])
        cls.adapter = entry.task_module.RequestsTask(cls.fixture)

    def options(self, url, protocol="chat_completions", **extra):
        return {"model": "fixture", "base_url": url, "api_key": "fixture-private-marker", "wire_api": protocol,
                "timeout": 60, "max_steps": 4, **extra}

    def repair_calls(self):
        path = "src/requests/exceptions.py"
        return [("read1", "read_file", {"path": path}), ("edit1", "edit_file", {"path": path, "old": OLD, "new": NEW}), "Done"]

    def test_comparison_environment_contains_the_complete_sprig_runtime(self):
        from importlib.metadata import requires, version
        from packaging.requirements import Requirement
        from sprig_agent.config import Config
        from sprig_agent.tools import Tools
        for value in requires("sprig-agent"):
            requirement = Requirement(value)
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                self.assertIn(version(requirement.name), requirement.specifier)
        for name in ("re2", "PIL", "yaml", "jsonschema", "textual", "tree_sitter", "tree_sitter_c",
                     "tree_sitter_cpp", "tree_sitter_go", "tree_sitter_java", "tree_sitter_javascript",
                     "tree_sitter_rust", "tree_sitter_typescript"):
            importlib.import_module(name)
        with entry.bench.disposable() as parent:
            root = parent / "workspace"
            entry.bench.suite.seed(root, self.adapter.files)
            with closing(Tools(Config(root, "fixture"), lambda _: self.fail("read-only map asked approval"))) as tools:
                result = tools.project_map(path="src/requests/exceptions.py")
            self.assertNotIn("error", result)
            self.assertIn("JSONDecodeError", json.dumps(result))

    def test_three_protocols_json_and_sse_repair_and_run_the_real_external_acceptance(self):
        for protocol in ("chat_completions", "messages", "responses"):
            for streamed in (False, True):
                with self.subTest(protocol=protocol, streamed=streamed), backend(protocol, self.repair_calls(), streamed=streamed) as (url, captured):
                    report = entry.bench.run_case(self.adapter.case, self.options(url, protocol, stream=streamed),
                                                   case_adapter=self.adapter, confirm=lambda _: True)
                self.assertTrue(report["acceptance_passed"], report)
                self.assertTrue(report["verifier_intact"])
                self.assertEqual(len(captured), 3)
                self.assertTrue(all(row["path"] == "/v1/" + ("chat/completions" if protocol == "chat_completions" else protocol) for row in captured))
                self.assertNotIn("fixture-private-marker", json.dumps(report))

    def test_done_without_repair_cannot_pass_the_real_acceptance(self):
        with backend("chat_completions", ["Done"]) as (url, captured):
            report = entry.bench.run_case(self.adapter.case, self.options(url, max_steps=1),
                                           case_adapter=self.adapter, confirm=lambda _: True)
        self.assertFalse(report["acceptance_passed"], report)
        self.assertTrue(report["verifier_intact"])
        self.assertEqual(len(captured), 1)

    def test_refused_actions_never_claim_acceptance(self):
        with backend("chat_completions", self.repair_calls()) as (url, _):
            report = entry.bench.run_case(self.adapter.case, self.options(url, max_steps=3),
                                           case_adapter=self.adapter, confirm=lambda _: False)
        self.assertFalse(report["acceptance_passed"], report)
        self.assertEqual(report["approvals"]["accepted"], 0)
        self.assertTrue(report["verifier_intact"])

    def test_pinned_mini_native_sdk_can_repair_the_same_task_with_its_own_loop(self):
        # Known upstream repair only as a mechanical integration fixture.
        script = "from pathlib import Path; p=Path('src/requests/exceptions.py'); s=p.read_text(); assert s.count(" + repr(OLD) + ")==1; p.write_text(s.replace(" + repr(OLD) + "," + repr(NEW) + "))"
        encoded = base64.b64encode(script.encode()).decode()
        patch_command = shlex.quote(sys.executable) + " -c " + shlex.quote("import base64; exec(base64.b64decode('" + encoded + "'))")
        commands = ["echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT", patch_command, "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"]
        calls = [("bash" + str(index), "bash", {"command": command}) for index, command in enumerate(commands)]
        with backend("chat_completions", calls) as (url, captured):
            report = entry.load_mini().run_case(self.adapter.case, self.options(url), case_adapter=self.adapter, confirm=lambda _: True,
                                               environment_lock=ROOT / "benchmarks/repositories/requests/uv.lock")
        self.assertTrue(report["acceptance_passed"], report)
        self.assertTrue(report["verifier_intact"])
        self.assertEqual(len(captured), 3)
        self.assertNotIn("fixture-private-marker", json.dumps(report))
        self.assertEqual(report["client_lock_sha256"], self.adapter.metadata["comparison_lock_sha256"])
        self.assertEqual(report["client_dependency_versions"]["mini-swe-agent"], "2.4.6")
