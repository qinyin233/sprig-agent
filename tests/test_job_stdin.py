"""Freshly approved input to owned background pipes; real commands, no models."""
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent.config import Config
from sprig_agent.tools import Tools
from sprig_agent.agent import Agent
from tests.test_jobs import argv
from tests import test_tui as ui
from tests.test_mcp_stdio import objects, wire
from tests.test_native_protocols import backend
from sprig_agent.provider import Provider
from sprig_agent.session import validate_history
from textual.widgets import TextArea
from tests.test_generation_options import envelope

PROGRAM = ("import sys; from pathlib import Path; Path('ready').write_text('ok'); print('READY',flush=True); "
           "line=sys.stdin.buffer.readline(); "
           "Path('first').write_bytes(line) if line else None; print('FIRST',flush=True); "
           "rest=sys.stdin.buffer.read(); Path('rest').write_bytes(rest); print('FINISHED',flush=True)")


class JobStdinTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temporary).resolve()
        self.prompts = []
        self.cancel = threading.Event()

    def tools(self, **options):
        config = Config(self.root, "fixture", **({"background_enabled": True, "background_timeout": 30,
            "api_key": "stdin-known-credential", "max_output": 1800} | options))
        return self.enterContext(closing(Tools(config, lambda prompt: self.prompts.append(prompt) or True, self.cancel)))

    def wait_for(self, predicate):
        until = time.monotonic() + 4
        while not predicate() and time.monotonic() < until:
            time.sleep(.02)
        self.assertTrue(predicate(), "fixture did not reach its declared state")

    def start(self, tools, code=PROGRAM):
        row = tools.run(argv(code), background=True, stdin="pipe")
        self.wait_for(lambda: (self.root / "ready").exists())
        self.assertEqual(row["stdin"], "open")
        return row["job_id"]

    def done(self, tools, identity):
        row = tools.job("wait", identity, seconds=4)
        self.assertNotEqual(row["state"], "running", row)
        return row

    def test_exact_utf8_crlf_chunks_and_eof_require_separate_approvals(self):
        tools = self.tools()
        identity = self.start(tools)
        first, rest = "你好\r\n", "尾部\nlast"
        for text in (first, rest):
            result = tools.job("input", identity, text=text)
            self.assertTrue(result["approved"])
            self.assertTrue(result["input_complete"])
            self.assertEqual(result["written_bytes"], len(text.encode()))
            self.assertNotIn(text, json.dumps(result, ensure_ascii=False))
        self.assertFalse((self.root / "rest").exists())
        result = tools.job("eof", identity)
        self.assertTrue(result["approved"])
        self.assertEqual(result["stdin"], "closed")
        finished = self.done(tools, identity)
        self.assertEqual(finished["returncode"], 0)
        self.assertEqual((self.root / "first").read_bytes(), first.encode())
        self.assertEqual((self.root / "rest").read_bytes(), rest.encode())
        self.assertEqual(len(self.prompts), 4)
        self.assertIn(json.dumps(first, ensure_ascii=False), self.prompts[1])

    def test_default_closed_foreground_and_disabled_modes_do_not_enable_input(self):
        tools = self.tools()
        row = tools.run(argv("import sys; assert sys.stdin.read()==''"), background=True)
        self.done(tools, row["job_id"])
        before = len(self.prompts)
        self.assertIn("error", tools.execute("job", {"action": "input", "job_id": row["job_id"], "text": "x"}))
        for values in ({"background": False, "stdin": "pipe"}, {"background": True, "stdin": True}):
            with patch("sprig_agent.jobs.Process") as process:
                self.assertIn("error", tools.execute("run", {"argv": argv("raise AssertionError"), **values}))
                process.assert_not_called()
        self.assertEqual(len(self.prompts), before)
        plain = self.tools(background_enabled=False)
        schema = next(row for row in Agent(plain.config, None, plain, lambda _: None).schemas if row["function"]["name"] == "run")
        self.assertNotIn("stdin", schema["function"]["parameters"]["properties"])

    def test_rejection_truthy_approval_and_invalid_input_never_write(self):
        tools = self.tools()
        identity = self.start(tools)
        before = len(self.prompts)
        for text in (None, True, "", "\x00", "\ud800", "汉" * 4096, tools.config.api_key):
            with self.subTest(kind=type(text).__name__):
                result = tools.execute("job", {"action": "input", "job_id": identity, "text": text})
                self.assertIn("error", result)
                self.assertNotIn(tools.config.api_key, json.dumps(result))
        self.assertEqual(len(self.prompts), before)
        for answer in (False, {"approved": True}):
            tools.approve = lambda _, answer=answer: answer
            for action, arguments in (("input", {"text": "bad\n"}), ("eof", {})):
                self.assertIs(tools.job(action, identity, **arguments)["approved"], False)
        self.assertFalse((self.root / "first").exists())
        self.assertEqual(tools.job("status", identity)["stdin"], "open")

    def test_reset_and_policy_change_during_approval_expire_input(self):
        for mode in ("reset", "readonly"):
            with self.subTest(mode=mode):
                (self.root / "ready").unlink(missing_ok=True)
                tools = self.tools()
                identity = self.start(tools)
                def approve(_):
                    if mode == "reset":
                        tools.jobs.reset()
                    else:
                        tools.config = replace(tools.config, read_only=True)
                    return True
                tools.approve = approve
                result = tools.execute("job", {"action": "input", "job_id": identity, "text": "bad\n"})
                self.assertIn("error", result)
                self.assertFalse((self.root / "first").exists())
                tools.jobs.reset()

    def test_reentrant_input_expires_the_older_approved_proposal(self):
        tools = self.tools()
        identity = self.start(tools)
        def approve(_):
            tools.approve = lambda _: True
            self.assertTrue(tools.job("input", identity, text="INNER\n")["input_complete"])
            return True
        tools.approve = approve
        result = tools.execute("job", {"action": "input", "job_id": identity, "text": "OUTER\n"})
        self.assertIn("error", result)
        tools.job("eof", identity)
        self.done(tools, identity)
        self.assertEqual((self.root / "first").read_bytes(), b"INNER\n")
        self.assertEqual((self.root / "rest").read_bytes(), b"")

    def test_argument_snapshot_and_read_versions_are_preserved_across_approval(self):
        tools = self.tools()
        identity = self.start(tools)
        (self.root / "tracked").write_text("old\n")
        observed = tools.read_file("tracked")
        arguments = {"action": "input", "job_id": identity, "text": "SAFE\n"}
        def approve(_):
            arguments["text"] = "MUTATED\n"
            return True
        tools.approve = approve
        self.assertTrue(tools.execute("job", arguments)["input_complete"])
        result = tools.execute("edit_lines", {"path": "tracked", "read_id": observed["read_id"],
            "edits": [{"start_line": 1, "end_line": 1, "new": "wrong\n"}]})
        self.assertIn("error", result)
        tools.job("eof", identity)
        self.done(tools, identity)
        self.assertEqual((self.root / "first").read_bytes(), b"SAFE\n")
        self.assertEqual((self.root / "tracked").read_text(), "old\n")

    def test_partial_write_stops_job_and_cannot_be_replayed(self):
        tools = self.tools()
        identity = self.start(tools)
        process = tools.jobs.records[identity].process
        original, sent, attempts = process.write, [], []
        def partial(raw, **kwargs):
            if not attempts:
                attempts.append(True)
                return 0  # A nonblocking lock miss is not delivered input.
            remaining = 2 - sum(sent)
            if remaining:
                count = original(raw[:remaining], **kwargs)
                sent.append(count)
                return count
            return 0
        with patch.object(process, "write", partial), patch("sprig_agent.jobs.INPUT_TIMEOUT", .1):
            result = tools.job("input", identity, text="PARTIAL\n")
        self.assertEqual(result["written_bytes"], 2)
        self.assertFalse(result["input_complete"])
        self.assertEqual(result["outcome"], "uncertain")
        self.assertEqual(self.done(tools, identity)["state"], "failed")
        before = len(self.prompts)
        self.assertIn("error", tools.execute("job", {"action": "input", "job_id": identity, "text": "PARTIAL\n"}))
        self.assertEqual(len(self.prompts), before)

    def test_cancel_during_write_stops_the_owned_job(self):
        tools = self.tools()
        identity = self.start(tools)
        process = tools.jobs.records[identity].process
        def cancel(_, **kwargs):
            self.cancel.set()
            return 0
        with patch.object(process, "write", cancel), self.assertRaises(KeyboardInterrupt):
            tools.job("input", identity, text="CANCELLED\n")
        self.cancel.clear()
        self.assertEqual(self.done(tools, identity)["state"], "cancelled")
        self.assertFalse((self.root / "first").exists())

    def test_total_input_budget_is_enforced_before_approval(self):
        tools = self.tools()
        code = "import sys; from pathlib import Path; Path('ready').write_text('ok'); Path('bulk').write_bytes(sys.stdin.buffer.read())"
        identity = self.start(tools, code)
        for index in range(16):
            process = tools.jobs.records[identity].process
            write = process.write
            with patch.object(process, "write", lambda raw, **kwargs: write(raw[:1024] if index == 15 else raw, **kwargs)):
                result = tools.job("input", identity, text="x" * 4096)
            self.assertEqual(result["written_bytes"], 4096)
        before = len(self.prompts)
        self.assertIn("error", tools.execute("job", {"action": "input", "job_id": identity, "text": "x"}))
        self.assertEqual(len(self.prompts), before)
        tools.job("eof", identity)
        self.done(tools, identity)
        self.assertEqual((self.root / "bulk").read_bytes(), b"x" * 65536)

    def test_delivery_failure_after_natural_exit_is_not_reported_as_success(self):
        tools = self.tools()
        identity = self.start(tools, "import sys; from pathlib import Path; Path('ready').write_text('ok'); Path('partial').write_bytes(sys.stdin.buffer.read(2))")
        row = tools.jobs.records[identity]
        write = row.process.write
        def exit_before_return(raw, **kwargs):
            count = write(raw[:2], **kwargs)
            self.wait_for(row.done.is_set)
            return count
        with patch.object(row.process, "write", exit_before_return):
            result = tools.job("input", identity, text="WHOLE\n")
        self.assertFalse(result["input_complete"])
        self.assertEqual(result["written_bytes"], 2)
        self.assertEqual(tools.job("status", identity)["state"], "failed")
        self.assertEqual((self.root / "partial").read_bytes(), b"WH")

    def test_input_failure_preserves_output_limit_and_user_cancellation(self):
        for reason in ("output_limited", "cancelled", "timed_out"):
            with self.subTest(reason=reason):
                (self.root / "ready").unlink(missing_ok=True)
                tools = self.tools(command_output_limit=1024, **({"background_timeout": 2} if reason == "timed_out" else {}))
                code = ("import sys,time; from pathlib import Path; Path('ready').write_text('ok'); "
                        "sys.stdin.buffer.read(2); print('x'*(2*1024*1024),flush=True); time.sleep(30)")
                identity = self.start(tools, code if reason == "output_limited" else PROGRAM)
                row = tools.jobs.records[identity]
                write = row.process.write
                def interrupted(raw, **kwargs):
                    count = write(raw[:2], **kwargs)
                    if reason == "cancelled":
                        row.cancel.set()
                    self.wait_for(row.done.is_set)
                    return count
                with patch.object(row.process, "write", interrupted):
                    result = tools.job("input", identity, text="INCOMPLETE\n")
                self.assertFalse(result["input_complete"])
                status = tools.job("status", identity)
                self.assertEqual(status["state"], reason)
                self.assertTrue(status[reason])
                self.assertEqual(status["stdin"], "failed")

    def test_original_job_deadline_can_expire_while_input_is_awaiting_approval(self):
        tools = self.tools(background_timeout=2)
        identity = self.start(tools)
        row = tools.jobs.records[identity]
        def approve(_):
            self.assertTrue(row.done.wait(4))
            return True
        tools.approve = approve
        with patch.object(row.process, "write", wraps=row.process.write) as write:
            result = tools.execute("job", {"action": "input", "job_id": identity, "text": "TOO_LATE\n"})
        self.assertIn("error", result)
        write.assert_not_called()
        self.assertEqual(row.state, "timed_out")
        self.assertFalse((self.root / "first").exists())

    def test_process_lock_cannot_extend_the_input_deadline(self):
        for action in ("input", "eof"):
            with self.subTest(action=action):
                (self.root / "ready").unlink(missing_ok=True)
                tools = self.tools()
                identity = self.start(tools)
                process = tools.jobs.records[identity].process
                held, release = threading.Event(), threading.Event()
                def hold():
                    with process._lock:
                        held.set()
                        release.wait(2)
                thread = threading.Thread(target=hold, daemon=True)
                thread.start()
                self.assertTrue(held.wait(2))
                try:
                    started = time.monotonic()
                    with patch("sprig_agent.jobs.INPUT_TIMEOUT", .1):
                        result = tools.job(action, identity, **({"text": "WAIT\n"} if action == "input" else {}))
                    elapsed = time.monotonic() - started
                finally:
                    release.set()
                    thread.join(3)
                self.assertLess(elapsed, 1)
                self.assertFalse(result["input_complete"])
                self.assertEqual(self.done(tools, identity)["state"], "failed")

    def test_project_rule_change_during_input_approval_blocks_the_write(self):
        tools = self.tools()
        identity = self.start(tools)
        tools.instructions.mark_served(tools.instructions.context())
        def approve(_):
            (self.root / "AGENTS.md").write_text("New project requirements\n")
            return True
        tools.approve = approve
        with tools.instructions.model_action():
            result = tools.execute("job", {"action": "input", "job_id": identity, "text": "STALE\n"})
        self.assertIn("error", result)
        self.assertFalse((self.root / "first").exists())

    @unittest.skipUnless(sys.platform != "win32", "Windows pins the live working directory")
    def test_working_directory_replacement_during_approval_blocks_input(self):
        directory = self.root / "cwd"
        directory.mkdir()
        tools = self.tools()
        row = tools.run(argv(PROGRAM), cwd="cwd", background=True, stdin="pipe")
        self.wait_for(lambda: (directory / "ready").exists())
        def approve(_):
            directory.rename(self.root / "old-cwd")
            directory.mkdir()
            return True
        tools.approve = approve
        result = tools.execute("job", {"action": "input", "job_id": row["job_id"], "text": "STALE\n"})
        self.assertIn("error", result)
        self.assertFalse((self.root / "old-cwd/first").exists())
        self.assertFalse((directory / "first").exists())

    def test_local_json_input_preserves_literal_paths_and_rejects_extra_arguments(self):
        from sprig_agent.jobs import local_arguments
        text = "C:\\目录\\name\r\n@file $HOME"
        name, arguments = local_arguments("input job-example " + json.dumps(text, ensure_ascii=False))
        self.assertEqual(name, "job")
        self.assertEqual(arguments, {"action": "input", "job_id": "job-example", "text": text})
        self.assertEqual(local_arguments("eof job-example"), ("job", {"action": "eof", "job_id": "job-example"}))
        for value in ("input job-example", "input job-example {}", "input job-example 2", 'input job-example "a" extra',
                      "eof job-example extra", 'input job-example "' + "x" * 25000 + '"'):
            with self.subTest(value=value[:50]), self.assertRaises(ValueError):
                local_arguments(value)

    def test_three_protocols_json_and_sse_input_eof_and_independent_acceptance(self):
        first, rest = "输入🙂\r\n", "remaining"
        for protocol in ("chat_completions", "messages", "responses"):
            for streamed in (False, True):
                root = self.root / (protocol + ("-sse" if streamed else "-json"))
                root.mkdir()
                captured, approvals = [], []
                def encode(message):
                    if protocol == "chat_completions" and not streamed:
                        return envelope(message, "tool_calls" if message.get("tool_calls") else "stop")
                    return wire(protocol, message)
                def action(identity, operation, **arguments):
                    def reply(request):
                        row = next(row for row in objects(request) if row.get("job_id") and row.get("state") in {"running", "exited"})
                        return encode(ui.call(identity, "job", {"action": operation, "job_id": row["job_id"], **arguments}))
                    return reply
                replies = [encode(ui.call("start", "run", {"argv": argv(PROGRAM), "background": True, "stdin": "pipe"})),
                    action("first", "input", text=first), action("rest", "input", text=rest), action("eof", "eof"),
                    action("wait", "wait", seconds=3), action("output", "output"), encode(ui.response("Done"))]
                check = "from pathlib import Path; assert Path('first').read_bytes()==" + repr(first.encode()) + "; assert Path('rest').read_bytes()==" + repr(rest.encode()) + "; print('STDIN_ACCEPTED')"
                with self.subTest(protocol=protocol, stream=streamed), backend(protocol, replies, captured, stream=streamed) as url:
                    config = Config(root, "fixture", base_url=url, wire_api=protocol, stream=streamed, proxy="direct",
                        background_enabled=True, background_timeout=30, checks=((sys.executable, "-c", check),))
                    with closing(Tools(config, lambda prompt: approvals.append(prompt) or True)) as tools:
                        history, completed = Agent(config, Provider(config), tools, lambda _: None).run("Run the reader; send the two chunks and EOF, then inspect and verify.")
                    self.assertTrue(completed)
                    self.assertEqual(len(captured), 7)
                    self.assertEqual(len(approvals), 5)
                    self.assertIn("FINISHED", json.dumps(history))
                    self.assertIn("STDIN_ACCEPTED", json.dumps(history))
                    validate_history(history)


class JobStdinTuiTests(unittest.IsolatedAsyncioTestCase):
    setUp = ui.TuiTests.setUp
    asyncSetUp = ui.TuiTests.asyncSetUp
    until = ui.TuiTests.until
    click = ui.TuiTests.click

    async def command(self, app, pilot, text):
        app.query_one("#task", TextArea).load_text(text)
        app.query_one("#task", TextArea).focus()
        await pilot.press("enter")

    async def approve(self, app, pilot):
        await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
        await self.click(pilot, "#approve")
        await self.until(pilot, lambda: not app.busy)

    async def test_enter_starts_and_feeds_owned_pipe_without_model_requests(self):
        self.config = replace(self.config, background_enabled=True, background_timeout=30)
        provider = ui.ScriptedProvider([])
        app = ui.TuiTests.app(self, provider)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.command(app, pilot, "/job start " + json.dumps({"argv": argv(PROGRAM), "stdin": "pipe"}))
            await self.approve(app, pilot)
            identity = next(iter(app.tools.jobs.records))
            for text in ("中文\r\n", "tail"):
                await self.command(app, pilot, "/job input " + identity + " " + json.dumps(text, ensure_ascii=False))
                await self.approve(app, pilot)
            await self.command(app, pilot, "/job eof " + identity)
            await self.approve(app, pilot)
            await self.until(pilot, lambda: not app.tools.jobs.unsettled())
            self.assertEqual((self.root / "first").read_bytes(), "中文\r\n".encode())
            self.assertEqual((self.root / "rest").read_bytes(), b"tail")
            self.assertEqual(provider.calls, [])
        app.close()

    async def test_new_guidance_in_input_approval_does_not_write_old_text(self):
        self.config = replace(self.config, background_enabled=True, background_timeout=30)
        provider = ui.ScriptedProvider([ui.response("先保持等待")])
        app = ui.TuiTests.app(self, provider)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.command(app, pilot, "/job start " + json.dumps({"argv": argv(PROGRAM), "stdin": "pipe"}))
            await self.approve(app, pilot)
            identity = next(iter(app.tools.jobs.records))
            await self.command(app, pilot, "/job input " + identity + ' "OLD\\n"')
            await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
            app.screen.query_one("#approval-task", TextArea).load_text("先保留这个程序，调整要求")
            app.screen.query_one("#approval-task", TextArea).focus()
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy)
            self.assertFalse((self.root / "first").exists())
            self.assertEqual(app.tools.jobs.records[identity].stdin_bytes, 0)
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(provider.calls[0][-1]["content"], "先保留这个程序，调整要求")
        app.close()


class JobStdinCliTests(unittest.TestCase):
    def test_chat_local_input_eof_wait_and_output_without_model_or_credential_echo(self):
        from tests import test_settings as settings
        case = settings.SettingsTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        key = "cli-stdin-private-fixture"
        case.write('provider="p"\n[providers.p]\nmodel="fixture"\napi_key="' + key + '"\n')
        owned = []
        def create(*arguments, **options):
            tools = Tools(*arguments, **options)
            owned.append(tools)
            return tools
        def identity():
            return next(iter(owned[0].jobs.records))
        values = iter(["/job start " + json.dumps({"argv": argv(PROGRAM), "stdin": "pipe"}), "yes",
            lambda: "/job input " + identity() + " " + json.dumps(key),
            lambda: "/job input " + identity() + ' "CLI\\r\\n"', "yes",
            lambda: "/job input " + identity() + ' "tail"', "yes",
            lambda: "/job eof " + identity(), "yes", lambda: "/job wait " + identity() + " 3",
            lambda: "/job output " + identity(), "/exit"])
        def reply(*_):
            value = next(values)
            return value() if callable(value) else value
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=reply), \
                patch("sprig_agent.cli.Tools", side_effect=create), \
                patch.object(Provider, "complete", side_effect=AssertionError("local commands cannot request a model")):
            status, output, error = case.invoke(["--config", str(case.path), "--chat", "--background"])
        self.assertEqual(status, 2, error)
        self.assertEqual((case.root / "first").read_bytes(), b"CLI\r\n")
        self.assertEqual((case.root / "rest").read_bytes(), b"tail")
        self.assertIn('"input_complete": true', output)
        self.assertIn("FINISHED", output)
        self.assertNotIn(key, output + error)
