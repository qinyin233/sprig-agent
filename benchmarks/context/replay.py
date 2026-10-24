"""Fixed offline edit replay through HTTP, native tools and private sessions."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading

EDIT_COUNT, TEXT_LENGTH = 6, 20000


def run(parent, *, compact=True):
    """Use only a new, caller-owned fixture parent; no model or arbitrary task."""
    from sprig_agent.agent import Agent
    from sprig_agent.config import Config
    from sprig_agent.context import prepare_context
    from sprig_agent.provider import Provider, ProviderError
    from sprig_agent.session import SessionStore, validate_history
    from sprig_agent.tools import Tools

    root = parent / "workspace"
    root.mkdir()
    before, after = "x" * TEXT_LENGTH, "y" * TEXT_LENGTH
    for index in range(EDIT_COUNT):
        (root / f"file-{index}.txt").write_bytes(before.encode())
    request_sizes, elided_counts, wire_errors = [], [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            if self.path != "/v1/chat/completions" or not 0 < length <= 131072:
                self.send_error(400)
                return
            body = self.rfile.read(length)
            payload = json.loads(body)
            history = payload["messages"]
            if validate_history(history) != history:
                wire_errors.append(True)
            request_sizes.append(len(body))
            elided_counts.append(sum(
                json.loads(call["function"]["arguments"]).get("context_elided") is True
                for message in history for call in message.get("tool_calls", [])))
            index = len(request_sizes) - 1
            message = {"role": "assistant", "content": "fixture finished"}
            if index < EDIT_COUNT:
                message = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": f"edit-{index}", "type": "function", "function": {
                        "name": "edit_file", "arguments": json.dumps({
                            "path": f"file-{index}.txt", "old": before, "new": after})}}]}
            response = json.dumps({"choices": [{"message": message,
                "finish_reason": "tool_calls" if index < EDIT_COUNT else "stop"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    tools = store = None
    try:
        check = (sys.executable, "-I", "-c",
                 "from pathlib import Path; assert all(Path(f'file-{i}.txt').read_bytes() == b'y'*20000 for i in range(6))")
        config = Config(root, "offline-http-fixture", base_url=f"http://127.0.0.1:{server.server_port}/v1",
                        max_output=65536, command_output_limit=65536, max_steps=10, checks=(check,), compact_context=compact)
        approved_edits, approved_checks = [], []
        expected = {f'WRITE "file-{index}.txt"\n' + Tools._diff(f"file-{index}.txt", before, after)
                    for index in range(EDIT_COUNT)}
        expected_command = ("RUN (NOT SANDBOXED; may access outside files/network)\n"
                            f"cwd: {json.dumps(str(config.workdir))}\nargv: {json.dumps(list(check))}\n"
                            f"timeout: {config.timeout}s; output cap: {config.max_output} bytes")
        def approve(description):
            if description in expected:
                approved_edits.append(True)
                return True
            if description == expected_command:
                approved_checks.append(True)
                return True
            raise AssertionError("replay attempted an operation outside the fixed fixture")
        tools = Tools(config, approve)
        store = SessionStore(parent / "sessions", config)
        provider = Provider(config)
        agent = Agent(config, provider, tools, lambda _: None,
                      lambda messages: store.save("replay", messages))
        complete, budget_blocked = False, False
        try:
            _, complete = agent.run("Update the six fixed fixture files without changing their paths. Every operation still needs approval.")
        except ProviderError as exc:
            if not str(exc).startswith("context byte budget exceeded"):
                raise
            budget_blocked = True
        saved = store.load("replay")
        blocked_bytes = (prepare_context(config, saved, agent.schemas, agent.compact_context)[1]["after_bytes"]
                         if budget_blocked else None)
        edits = [call for message in saved for call in message.get("tool_calls", [])
                 if call["function"]["name"] == "edit_file"]
        full_arguments = all(json.loads(call["function"]["arguments"]) == {
            "path": f"file-{index}.txt", "old": before, "new": after} for index, call in enumerate(edits))
        changed = sum((root / f"file-{index}.txt").read_bytes() == after.encode() for index in range(EDIT_COUNT))
        checks = [json.loads(message["content"]) for message in saved
                  if message.get("role") == "tool" and message["tool_call_id"].startswith("sprig-check-")]
        checks_passed = (len(checks) == 1 and checks[0].get("approved") is True
                         and checks[0].get("returncode") == 0 and not checks[0].get("timed_out")
                         and not checks[0].get("output_limited") and "error" not in checks[0])
        return {"compact_enabled": compact, "complete": complete, "budget_blocked": budget_blocked,
                "http_requests": len(request_sizes), "request_bytes": request_sizes,
                "blocked_next_request_bytes": blocked_bytes,
                "elided_calls_in_requests": elided_counts, "wire_history_valid": not wire_errors,
                "edit_approvals": len(approved_edits), "check_approvals": len(approved_checks),
                "changed_files": changed, "checks_passed": checks_passed,
                "saved_edits": len(edits), "saved_arguments_full": full_arguments,
                "saved_history_bytes": len(json.dumps(saved).encode()),
                "transport_requests": provider.measurements.report()["attempts"]}
    finally:
        if tools is not None:
            tools.close()
        if store is not None:
            store.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
