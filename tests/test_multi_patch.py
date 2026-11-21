"""Multi-file patches through existing revision, approval and provider boundaries."""

from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest

from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.context import prepare_context, request_size
from sprig_agent.provider import Provider
from sprig_agent.tools import PATCH_FILES_SCHEMA, Tools
from sprig_agent.tui import ApprovalScreen, TaskInput
from tests import test_tui as ui
from tests.test_glob_protocols import answer, values
from tests.test_native_protocols import backend


FILES = {"registry.py": "REGISTRY", "dispatch.py": "COMMAND", "exports.py": "PUBLIC"}


def manifest(identities):
    return [{"path": path, "read_id": identities[path], "patch": f'@@\n-{name} = "old"\n+{name} = "new"\n'}
            for path, name in FILES.items()]


def prepare(root):
    for index, (path, name) in enumerate(FILES.items()):
        ending = b"\r\n" if index == 1 else b"\n"
        (root / path).write_bytes(f'{name} = "old"'.encode() + ending)


def tool_result(history, identity):
    return next(json.loads(item["content"]) for item in history if item.get("tool_call_id") == identity)


def offered(payload):
    return {tool.get("function", tool).get("name") for tool in payload.get("tools", ())}


class MultiPatchTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        prepare(self.root)
        self.config = Config(self.root, "fixture", api_key="fixture-patch-key")

    def source(self):
        return [(self.root / path).read_bytes() for path in FILES]

    def live_manifest(self, tools):
        return manifest({path: tools.read_file(path)["read_id"] for path in FILES})

    def test_invalid_last_patch_or_stale_id_never_opens_first_approval(self):
        approvals = []
        source = self.source()
        with closing(Tools(self.config, lambda prompt: approvals.append(prompt) or True)) as tools:
            files = self.live_manifest(tools)
            malformed = [*files[:-1], {**files[-1], "patch": '@@\n-PUBLIC = "wrong"\n+PUBLIC = "new"\n'}]
            result = tools.execute("patch_files", {"files": malformed})
            self.assertEqual((result["error"], result["stopped_file"], result["changed_files"]),
                             ("Patch preflight failed", 3, 0))
            expired = [*files[:-1], {**files[-1], "read_id": files[0]["read_id"]}]
            self.assertEqual(tools.execute("patch_files", {"files": expired})["stopped_file"], 3)
            alias = [files[0], {**files[0], "path": "./registry.py"}]
            self.assertIn("repeats", tools.execute("patch_files", {"files": alias})["error"])
            self.assertIsNone(tools._edit_batch)
            self.assertEqual(approvals, [])
            self.assertEqual(self.source(), source)

    def test_refusal_keeps_completed_prefix_and_reports_bounded_detail(self):
        approvals = []
        def decide(prompt):
            approvals.append(prompt)
            return len(approvals) == 1

        with closing(Tools(replace(self.config, max_output=256), decide)) as tools:
            result = tools.execute("patch_files", {"files": self.live_manifest(tools)})
            self.assertEqual((result["changed_files"], result["stopped_file"]), (1, 2))
            self.assertTrue(result["partial"])
            self.assertFalse(result["approved"])
            self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), 256)
            self.assertIsNone(tools._edit_batch)
            retained = tools.history.state["records"]
            self.assertEqual([row["path"] for row in retained], ["registry.py"])
            self.assertEqual(retained[0]["batch"]["id"], result["batch_id"])
        self.assertEqual(len(approvals), 2)
        self.assertEqual(self.source(), [b'REGISTRY = "new"\n', b'COMMAND = "old"\r\n', b'PUBLIC = "old"\n'])

    def test_external_change_during_approval_stops_before_any_patch_write(self):
        def decide(_):
            (self.root / "exports.py").write_bytes(b'PUBLIC = "external"\n')
            return True

        with closing(Tools(self.config, decide)) as tools:
            result = tools.execute("patch_files", {"files": self.live_manifest(tools)})
            self.assertEqual((result["changed_files"], result["stopped_file"]), (0, 1))
            self.assertIn("changed after edit batch preflight", result["detail"])
            self.assertEqual(tools.review_changes()["records"], [])
        self.assertEqual(self.source(), [b'REGISTRY = "old"\n', b'COMMAND = "old"\r\n', b'PUBLIC = "external"\n'])

    def test_tool_is_optional_and_never_bypasses_budget_or_write_settings(self):
        for disabled in ("read_file", "edit_file", "edit_lines", "patch_files"):
            config = replace(self.config, disabled_tools=(disabled,))
            with closing(Tools(config, lambda _: self.fail("unexpected approval"))) as tools:
                self.assertIn("disabled", tools.execute("patch_files", {"files": []})["error"])
        with closing(Tools(replace(self.config, read_only=True), lambda _: self.fail("unexpected approval"))) as tools:
            self.assertFalse(tools.execute("patch_files", {"files": []})["approved"])
        with closing(Tools(self.config, lambda _: self.fail("unexpected approval"))) as tools:
            agent = Agent(self.config, None, tools, lambda _: None)
            call = lambda path: {"id": path, "type": "function", "function": {
                "name": "read_file", "arguments": json.dumps({"path": path})}}
            history = [{"role": "system", "content": "fixture"}, {"role": "user", "content": "coordinate patch"},
                       {"role": "assistant", "content": None, "tool_calls": [call(path) for path in FILES]}]
            history.extend({"role": "tool", "tool_call_id": path, "content": json.dumps(tools.read_file(path))} for path in FILES)
            base_size = request_size(self.config, history, agent.schemas)
            report = {"budget_bytes": base_size, "after_bytes": base_size, "before_bytes": base_size}
            self.assertIs(agent._request_schemas(history, report), agent.schemas)
            report["budget_bytes"] = self.config.max_context_bytes
            self.assertEqual(agent._request_schemas(history, report)[-1], PATCH_FILES_SCHEMA)
            self.assertGreater(report["after_bytes"], base_size)

    def test_three_protocols_prepare_one_patch_with_independent_stage_and_final_checks(self):
        for protocol in ("chat_completions", "messages", "responses"):
            for stream in (False, True):
                with self.subTest(protocol=protocol, stream=stream):
                    prepare(self.root)
                    self.run_protocol(protocol, stream)

    def run_protocol(self, protocol, stream, *, steer=False):
        captured, approvals, pending, dispatched = [], [], [], []
        reads = [("read" + str(index), "read_file", {"path": path}) for index, path in enumerate(FILES)]
        patch_arguments = {}

        def patch(payload):
            self.assertIn("patch_files", offered(payload))
            identities = {item["path"]: item["read_id"] for item in values(payload)
                          if isinstance(item.get("read_id"), str) and item.get("path") in FILES}
            patch_arguments.update(files=manifest(identities))
            return answer(protocol, stream, calls=[("group", "patch_files", patch_arguments)])

        def done(payload):
            if steer:
                self.assertIn("Keep only the first file", json.dumps(payload, ensure_ascii=False))
            return answer(protocol, stream, text="Done")

        def approve(prompt):
            approvals.append(prompt)
            if steer and prompt.startswith('WRITE "registry.py"'):
                pending.append("Keep only the first file")
            return True

        def receive():
            queued = list(pending)
            pending.clear()
            return queued

        class ObservedTools(Tools):
            def execute(self, name, arguments):
                dispatched.append(name)
                return super().execute(name, arguments)

        checks = ((sys.executable, "-I", "-B", "-c",
                   "from pathlib import Path; assert all('new' in Path(p).read_text() for p in " + repr(list(FILES)) + ")"),)
        responses = [answer(protocol, stream, calls=reads), patch, done]
        with backend(protocol, responses, captured, stream=stream) as url:
            config = replace(self.config, base_url=url, proxy="direct", wire_api=protocol, stream=stream,
                             checks=() if steer else checks, check_after_edit=not steer, max_steps=3,
                             max_context_bytes=16384)
            with closing(ObservedTools(config, approve)) as tools:
                history, complete = Agent(config, Provider(config), tools, lambda _: None, receive_input=receive).run(
                    "Coordinate changes across three files")
                self.assertIsNone(tools._patch_superseded)
        self.assertTrue(complete)
        self.assertEqual(dispatched.count("patch_files"), 1)
        self.assertEqual(len(captured), 3)
        self.assertNotIn("patch_files", offered(captured[0]["body"]))
        self.assertTrue(all(row["bytes"] <= 16384 for row in captured))
        result = tool_result(history, "group")
        if steer:
            self.assertEqual((result["changed_files"], result["stopped_file"]), (1, 2))
            self.assertIn("user guidance", result["error"])
            self.assertEqual(len(approvals), 1)
            self.assertEqual(self.source(), [b'REGISTRY = "new"\n', b'COMMAND = "old"\r\n', b'PUBLIC = "old"\n'])
            return
        self.assertEqual(result["changed_files"], 3)
        self.assertEqual(len(approvals), 5)
        self.assertEqual([item["sprig_check"] for item in history if "sprig_check" in item], ["edit-check", "acceptance"])
        self.assertEqual(self.source(), [b'REGISTRY = "new"\n', b'COMMAND = "new"\r\n', b'PUBLIC = "new"\n'])
        if protocol == "chat_completions":
            tail = [{"role": "user", "content": f"Later task {index}"} for index in range(8)]
            compacted, _ = prepare_context(config, [*history, *tail], [], compact=True, force=True)
            old = next(call for row in compacted for call in row.get("tool_calls", []) if call["id"] == "group")
            self.assertEqual(json.loads(old["function"]["arguments"])["completed_files"], 3)

    def test_new_guidance_between_patch_files_settles_only_completed_prefix(self):
        self.run_protocol("chat_completions", True, steer=True)


class MultiPatchTuiTests(unittest.IsolatedAsyncioTestCase):
    setUp = ui.TuiTests.setUp
    asyncSetUp = ui.TuiTests.asyncSetUp
    app = ui.TuiTests.app
    until = ui.TuiTests.until
    click = ui.TuiTests.click

    async def test_real_tui_dispatch_opens_each_approval_and_keeps_rejected_suffix_untouched(self):
        prepare(self.root)
        protocol, captured = "responses", []
        reads = [(f"read-{index}", "read_file", {"path": path}) for index, path in enumerate(FILES)]

        def patch(payload):
            identities = {item["path"]: item["read_id"] for item in values(payload)
                          if isinstance(item.get("read_id"), str) and item.get("path") in FILES}
            return answer(protocol, True, calls=[("group", "patch_files", {"files": manifest(identities)})])

        scripted = [answer(protocol, True, calls=reads), patch, answer(protocol, True, text="Reviewed")]
        with backend(protocol, scripted, captured, stream=True) as url:
            self.config = replace(self.config, base_url=url, proxy="direct", wire_api=protocol, stream=True)
            app = self.app(Provider(self.config))
            async with app.run_test(size=(80, 24)) as pilot:
                editor = app.query_one("#task", TaskInput)
                editor.load_text("Coordinate the three fixture files")
                editor.focus()
                await pilot.press("enter")
                await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen) and "registry.py" in app.screen.description)
                await self.click(pilot, "#approve")
                await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen) and "dispatch.py" in app.screen.description)
                await pilot.press("escape")
                await self.until(pilot, lambda: not app.busy)
                result = tool_result(app.messages, "group")
                self.assertEqual((result["changed_files"], result["stopped_file"]), (1, 2))
                self.assertFalse(result["approved"])
        self.assertEqual((self.root / "registry.py").read_bytes(), b'REGISTRY = "new"\n')
        self.assertEqual((self.root / "dispatch.py").read_bytes(), b'COMMAND = "old"\r\n')
        self.assertEqual((self.root / "exports.py").read_bytes(), b'PUBLIC = "old"\n')
