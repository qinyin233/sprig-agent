"""Real owned command trees, bounded output and independent acceptance; no models."""
from contextlib import closing
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.provider import Provider
from sprig_agent.session import SessionStore, validate_history
from sprig_agent.settings import Settings, _validate, public_config
from sprig_agent.tools import Tools
from tests import test_tui as ui
from tests.test_mcp_stdio import running, wire, objects
from tests.test_native_protocols import backend


def argv(code):
    return [sys.executable, "-u", "-c", code]


class JobTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "目录 space"
        self.root.mkdir()
        self.approvals = []
        self.cancel = threading.Event()

    def tools(self, approve=None, **options):
        config = Config(self.root, "fixture", **({"background_enabled": True, "max_output": 1800, "background_timeout": 8} | options))
        tools = Tools(config, approve or (lambda text: self.approvals.append(text) or True), self.cancel)
        self.addCleanup(tools.close)
        return tools

    def done(self, tools, identity, seconds=4):
        result = tools.job("wait", identity, seconds=seconds)
        self.assertNotEqual(result["state"], "running", result)
        return result

    def test_start_returns_while_command_runs_and_output_is_observation_not_acceptance(self):
        tools = self.tools()
        start = time.monotonic()
        result = tools.run(argv("import time,sys; print(sys.stdin.read() == ''); time.sleep(.6); print('EXIT_MARKER')"), background=True)
        self.assertLess(time.monotonic() - start, .5)
        self.assertTrue(result["started"])
        self.assertFalse(result["task_completed"])
        self.assertNotIn("returncode", result)
        end = self.done(tools, result["job_id"])
        self.assertEqual(end["returncode"], 0)
        self.assertEqual(end["state"], "exited")
        output = tools.job("output", result["job_id"])
        self.assertIn("True", output["content"])
        self.assertIn("EXIT_MARKER", output["content"])
        self.assertTrue(output["not_authorization"])
        self.assertEqual(len(self.approvals), 1)

    def test_default_readonly_disabled_and_rejected_starts_never_spawn(self):
        for options in ({"background_enabled": False}, {"read_only": True}, {"disabled_tools": ("run",)}, {"disabled_tools": ("job",)}):
            tools = self.tools(approve=self.fail, **options)
            with patch("sprig_agent.jobs.Process") as process:
                result = tools.execute("run", {"argv": argv("raise AssertionError('must not run')"), "background": True})
                self.assertTrue("error" in result or result.get("approved") is False)
                process.assert_not_called()
                schemas = Agent(tools.config, Mock(), tools, lambda _: None).schemas
                self.assertNotIn("job", [row["function"]["name"] for row in schemas])
        tools = self.tools(approve=lambda _: False)
        with patch("sprig_agent.jobs.Process") as process:
            self.assertFalse(tools.run(argv("print('wrong')"), background=True)["approved"])
            process.assert_not_called()

    def test_argument_snapshot_and_foreground_timeout_are_not_relaxed(self):
        original = argv("print('APPROVED_ARGV')")
        def mutate(_):
            original[-1] = "print('MUTATED_ARGV')"
            return True
        tools = self.tools(approve=mutate, timeout=.5)
        row = tools.run(original, background=True)
        self.done(tools, row["job_id"])
        output = tools.job("output", row["job_id"])["content"]
        self.assertIn("APPROVED_ARGV", output)
        self.assertNotIn("MUTATED_ARGV", output)
        original = argv("print('APPROVED_FOREGROUND')")
        result = tools.run(original)
        self.assertIn("APPROVED_FOREGROUND", result["output"])
        self.assertNotIn("MUTATED_ARGV", result["output"])
        self.assertIn("error", tools.execute("run", {"argv": argv("print('x')"), "timeout": 1}))
        self.assertIn("error", tools.execute("run", {"argv": argv("print('x')"), "background": "true"}))

    def test_cwd_privacy_credentials_and_postapproval_cancellation_block_start(self):
        tools = self.tools(api_key="job-private-fixture-secret", excluded_paths=("private/",))
        (self.root / "private").mkdir()
        for arguments in ({"cwd": "../outside"}, {"cwd": "private"}, {"argv": argv("print('job-private-fixture-secret')")}):
            with self.subTest(arguments=arguments), patch("sprig_agent.jobs.Process") as process:
                result = tools.execute("run", {"argv": argv("print('wrong')"), "background": True, **arguments})
                self.assertIn("error", result)
                process.assert_not_called()
        tools = self.tools(approve=lambda _: self.cancel.set() or True)
        with patch("sprig_agent.jobs.Process") as process, self.assertRaises(KeyboardInterrupt):
            tools.run(argv("print('wrong')"), background=True)
        process.assert_not_called()
        self.cancel.clear()
        tools = self.tools(approve=lambda _: tools.root.close() or True)
        with patch("sprig_agent.jobs.Process") as process:
            self.assertIn("error", tools.execute("run", {"argv": argv("print('wrong')"), "background": True}))
            process.assert_not_called()

    def test_served_instruction_changes_during_approval_prevent_launch(self):
        path = self.root / "AGENTS.md"
        path.write_text("original served instructions", encoding="utf-8")
        def change(_):
            path.write_text("new instructions", encoding="utf-8")
            return True
        tools = self.tools(approve=change)
        tools.instructions.mark_served(tools.instructions.context())
        with tools.instructions.model_action(), patch("sprig_agent.jobs.Process") as process:
            self.assertIn("error", tools.execute("run", {"argv": argv("print('wrong')"), "background": True}))
            process.assert_not_called()

    def test_timeout_output_limit_failed_exit_and_fresh_cancel_are_distinct(self):
        tools = self.tools(background_timeout=.15)
        row = tools.run(argv("import time; time.sleep(10)"), background=True)
        self.assertTrue(self.done(tools, row["job_id"])["timed_out"])
        tools = self.tools(max_output=1200)
        row = tools.run(argv("print('x'*(2*1024*1024))"), background=True)
        self.assertTrue(self.done(tools, row["job_id"])["output_limited"])
        row = tools.run(argv("raise SystemExit(7)"), background=True)
        self.assertEqual(self.done(tools, row["job_id"])["returncode"], 7)
        row = tools.run(argv("import time; time.sleep(10)"), background=True)
        before = len(self.approvals)
        result = tools.job("cancel", row["job_id"])
        self.assertTrue(result["approved"])
        self.assertTrue(result["cancelled"])
        self.assertEqual(len(self.approvals), before + 1)
        tools.approve = lambda _: False
        self.assertFalse(tools.job("cancel", result["job_id"])["approved"])

    def test_four_active_capacity_eight_records_and_bounded_list_pages(self):
        tools = self.tools(max_output=1024)
        active = [tools.run(argv("import time; time.sleep(10)"), background=True)["job_id"] for _ in range(4)]
        before = len(self.approvals)
        self.assertIn("error", tools.execute("run", {"argv": argv("print('wrong')"), "background": True}))
        self.assertEqual(len(self.approvals), before)
        page = tools.job("list")
        self.assertLessEqual(len(json.dumps(page, ensure_ascii=False).encode()), 1024)
        self.assertIsNotNone(page["next_offset"])
        tools.jobs.reset()
        first = None
        for _ in range(9):
            row = tools.run(argv("print('finished')"), background=True)
            first = first or row["job_id"]
            self.done(tools, row["job_id"])
        self.assertEqual(len(tools.jobs.records), 8)
        self.assertIn("error", tools.execute("job", {"action": "status", "job_id": first}))

    def test_bounded_unicode_pages_redaction_live_prefix_and_credential_invalidation(self):
        secret = "known-background-private-fixture-token"
        script = self.root / "output.py"
        script.write_text("import sys,time\nfor value in " + repr(secret) + ":\n sys.stdout.write(value); sys.stdout.flush(); time.sleep(.001)\nprint('中文🎍'*9000)\n", encoding="utf-8")
        tools = self.tools(api_key=secret, command_output_limit=300000, max_output=1200)
        row = tools.run([sys.executable, "-X", "utf8", "-u", "output.py"], background=True)
        first = tools.job("output", row["job_id"])
        self.assertNotIn(secret[:8], first["content"])
        self.done(tools, row["job_id"])
        current = tools.job("output", row["job_id"])
        body, end, token = [], current["end"], current["sha256"]
        while True:
            self.assertLessEqual(len(json.dumps(current, ensure_ascii=False).encode()), 1200)
            body.append(current["content"])
            if current["next_offset"] is None:
                break
            current = tools.job("output", row["job_id"], offset=current["next_offset"], end=end)
            self.assertEqual(current["sha256"], token)
        text = "".join(body)
        self.assertIn("中文🎍", text)
        self.assertNotIn(secret, text)
        self.assertIn("[REDACTED]", text)
        tools.config._runtime_credentials.remember("中文🎍")
        self.assertIn("error", tools.execute("job", {"action": "output", "job_id": row["job_id"]}))
        self.assertEqual(tools.jobs.records[row["job_id"]].output.data, b"")

    def test_retained_prefix_is_one_mib_and_is_not_the_process_output_limit(self):
        tools = self.tools(command_output_limit=3 * 1024 * 1024)
        row = tools.run(argv("import sys; sys.stdout.write('a'*(2*1024*1024))"), background=True)
        result = self.done(tools, row["job_id"])
        self.assertEqual(result["returncode"], 0)
        self.assertTrue(result["retention_limited"])
        self.assertFalse(result["output_limited"])
        self.assertEqual(result["retained_bytes"], 1024 * 1024)

    def test_independent_stdout_stderr_keep_split_unicode_valid(self):
        tools = self.tools()
        code = "import os,time; from pathlib import Path; raw='中文🎍'.encode('utf-8'); os.write(1,raw[:2])\nwhile not Path('split-next').exists(): time.sleep(.01)\nos.write(2,b'DIAGNOSTIC\\n')\nwhile not Path('split-last').exists(): time.sleep(.01)\nos.write(1,raw[2:])"
        row = tools.run(argv(code), background=True)
        for amount, name in ((2, "split-next"), (13, "split-last")):
            until = time.monotonic() + 2
            while tools.jobs.records[row["job_id"]].bytes < amount and time.monotonic() < until:
                time.sleep(.01)
            self.assertGreaterEqual(tools.jobs.records[row["job_id"]].bytes, amount)
            (self.root / name).write_text("continue", encoding="utf-8")
        self.done(tools, row["job_id"])
        output = tools.job("output", row["job_id"])["content"]
        self.assertIn("中文🎍", output)
        self.assertIn("DIAGNOSTIC", output)

    def test_owned_descendants_stop_on_cancel_reset_and_after_leader_exit(self):
        child = "import time; time.sleep(20)"
        for mode in ("cancel", "reset", "leader-exit"):
            path = self.root / (mode + ".pid")
            # Existence is the reader's ready signal: publish only after close,
            # rather than exposing an empty file between create and write.
            code = ("import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c'," + repr(child) +
                    "]); ready=Path(" + repr(path.name + '.ready') + "); ready.write_text(str(p.pid)); ready.replace(" + repr(path.name) + "); " +
                    ("pass" if mode == "leader-exit" else "time.sleep(20)"))
            tools = self.tools()
            row = tools.run(argv(code), background=True)
            until = time.monotonic() + 3
            while not path.exists() and time.monotonic() < until:
                time.sleep(.02)
            pid = int(path.read_text())
            if mode == "cancel":
                tools.job("cancel", row["job_id"])
            elif mode == "reset":
                tools.jobs.reset()
            else:
                self.done(tools, row["job_id"])
            self.assertFalse(running(pid), mode)

    def test_background_environment_keeps_existing_command_policy(self):
        script = self.root / "environment.py"
        script.write_text("import os,json\nvalue={name:os.environ.get(name) for name in ['OPENAI_API_KEY','FIXTURE_VISIBLE','FIXTURE_OVERRIDE','UNLISTED_PRIVATE']}\nvalue['override_matched']=os.environ.get('FIXTURE_OVERRIDE')=='ordinary'\nprint(json.dumps(value))\n", encoding="utf-8")
        tools = self.tools(env_allowlist=("FIXTURE_VISIBLE",), command_env=(("FIXTURE_OVERRIDE", "ordinary"),))
        with patch.dict(os.environ, {"OPENAI_API_KEY": "hidden-background-fixture", "FIXTURE_VISIBLE": "allowed", "UNLISTED_PRIVATE": "hidden"}):
            row = tools.run([sys.executable, "-u", "environment.py"], background=True)
        self.done(tools, row["job_id"])
        content = tools.job("output", row["job_id"])["content"]
        self.assertIn('"FIXTURE_VISIBLE": "allowed"', content)
        self.assertIn('"FIXTURE_OVERRIDE": "[REDACTED]"', content)
        self.assertIn('"override_matched": true', content)
        self.assertNotIn("hidden-background-fixture", content)
        self.assertIn('"UNLISTED_PRIVATE": null', content)

    def test_active_jobs_block_completion_baseline_and_fixed_acceptance(self):
        tools = self.tools(checks=((sys.executable, "-c", "print('CHECK')"),))
        row = tools.run(argv("import time; time.sleep(10)"), background=True)
        provider = ui.ScriptedProvider([ui.response("Done")])
        agent = Agent(tools.config, provider, tools, lambda _: None)
        history, complete = agent.run("Do the task")
        self.assertFalse(complete)
        self.assertEqual(agent._stop_reason, "background")
        self.assertEqual(len(self.approvals), 1)
        self.assertFalse(any(message.get("sprig_check") for message in history))
        tools.job("cancel", row["job_id"])
        config = replace(tools.config, check_before_task=True)
        tools = self.tools(checks=config.checks, check_before_task=True)
        tools.run(argv("import time; time.sleep(10)"), background=True)
        provider = Mock()
        history, complete = Agent(tools.config, provider, tools, lambda _: None).run("task")
        self.assertFalse(complete)
        provider.complete.assert_not_called()

    def test_completed_jobs_clear_old_read_ids_and_restore_never_replays(self):
        tools = self.tools()
        (self.root / "value.txt").write_text("original", encoding="utf-8")
        read_id = tools.read_file("value.txt", line_numbers=True)["read_id"]
        row = tools.run(argv("print('completed')"), background=True)
        self.done(tools, row["job_id"])
        self.assertIn("error", tools.execute("edit_lines", {"path": "value.txt", "read_id": read_id, "edits": [{"start_line": 1, "end_line": 1, "new": "wrong"}]}))
        history = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}, ui.response(json.dumps(row))]
        with closing(SessionStore(self.parent / "sessions", tools.config)) as store:
            store.save_new("private", history)
            restored = store.load("private")
        tools.jobs.reset()
        with patch("sprig_agent.jobs.Process") as process:
            Agent(tools.config, Mock(), tools, lambda _: None).restore_plan(restored)
            self.assertIn("error", tools.execute("job", {"action": "status", "job_id": row["job_id"]}))
            process.assert_not_called()
        validate_history(restored)

    def test_new_background_effects_during_check_cannot_be_reported_as_acceptance(self):
        tools = self.tools(checks=((sys.executable, "-c", "print('CHECK')"),))
        started = False
        def approve(description):
            nonlocal started
            if not started:
                started = True
                row = tools.run(argv("print('concurrent background change')"), background=True)
                self.done(tools, row["job_id"])
            return True
        tools.approve = approve
        messages = []
        provider = ui.ScriptedProvider([ui.response("Done")])
        agent = Agent(tools.config, provider, tools, messages.append)
        history, complete = agent.run("task")
        self.assertFalse(complete)
        self.assertEqual(agent._stop_reason, "background")
        self.assertFalse(any("All user checks passed" in text for text in messages))
        validate_history(history)

    def test_confirmed_conversation_branch_stops_jobs_without_replay(self):
        from sprig_agent.conversation_fork import ConversationFork
        tools = self.tools()
        row = tools.run(argv("import time; time.sleep(10)"), background=True)
        original = tools.jobs.records[row["job_id"]]
        agent = Agent(tools.config, Mock(), tools, lambda _: None)
        history = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}, ui.response("historical")]
        candidate = ConversationFork(agent, history, "current")
        self.assertTrue(candidate.preflight(lambda text: "后台命令停止" in text, self.parent / "sessions"))
        selected, new_history, store, name = candidate.commit(self.parent / "sessions")
        self.assertTrue(original.done.is_set())
        self.assertEqual(original.state, "cancelled")
        self.assertFalse(tools.jobs.records)
        self.assertIn("NOT rolled back", json.dumps(new_history))

    def test_reset_fences_concurrent_start_and_close_is_permanent(self):
        tools = self.tools()
        row = tools.run(argv("import time; time.sleep(10)"), background=True)
        record = tools.jobs.records[row["job_id"]]
        closing, release, errors = threading.Event(), threading.Event(), []
        original = record.process.close
        def paused_close():
            closing.set()
            release.wait(3)
            original()
        def reset():
            try:
                tools.jobs.reset()
            except BaseException as error:
                errors.append(error)
        with patch.object(record.process, "close", side_effect=paused_close):
            worker = threading.Thread(target=reset)
            worker.start()
            try:
                self.assertTrue(closing.wait(2))
                before = len(self.approvals)
                with patch("sprig_agent.jobs.Process") as process:
                    result = tools.execute("run", {"argv": argv("print('must not start during reset')"), "background": True})
                    self.assertIn("error", result)
                    process.assert_not_called()
                self.assertEqual(len(self.approvals), before)
            finally:
                release.set()
                worker.join(5)
        self.assertEqual(errors, [])
        self.assertFalse(tools.jobs.records)
        tools.jobs.close()
        with patch("sprig_agent.jobs.Process") as process:
            self.assertIn("error", tools.execute("run", {"argv": argv("print('closed')"), "background": True}))
            process.assert_not_called()

    def test_trusted_configuration_profile_cli_and_binding(self):
        data = {"provider": "p", "providers": {"p": {"model": "fixture"}}, "execution": {"background": False},
                "profiles": {"long": {"execution": {"background": True, "background_timeout": 123}}}}
        _validate(data, self.root)
        config, metadata = Settings(data=data).resolve({"workdir": str(self.root), "profile": "long"}, {})
        self.assertTrue(config.background_enabled)
        self.assertEqual(config.background_timeout, 123)
        self.assertEqual(config.binding()["background"]["timeout"], 123)
        self.assertTrue(public_config(config, metadata)["background"]["enabled"])
        disabled, _ = Settings(data=data).resolve({"workdir": str(self.root), "profile": "long", "background_enabled": False}, {})
        self.assertFalse(disabled.background_enabled)
        with self.assertRaises(ValueError):
            _validate({"execution": {"background": True}}, self.root, project=True)
        for value in (0, 3601, True, float("inf")):
            with self.assertRaises(ValueError):
                Config(self.root, "fixture", background_timeout=value)

    def test_three_model_protocols_background_edit_wait_output_and_independent_check(self):
        for protocol in ("chat_completions", "messages", "responses"):
            root = self.root / protocol
            root.mkdir()
            captured, approvals = [], []
            code = "import time; from pathlib import Path; stop=time.monotonic()+5\nwhile not Path('release').exists() and time.monotonic()<stop: time.sleep(.02)\nassert Path('release').read_text()=='go'; Path('result').write_text('BACKGROUND_DONE'); print('JOB_FINISHED')"
            def job_action(action):
                def reply(request):
                    row = next(row for row in objects(request) if row.get("job_id") and row.get("state") in {"running", "exited"})
                    return wire(protocol, ui.call(action, "job", {"action": action, "job_id": row["job_id"], **({"seconds": 3} if action == "wait" else {})}))
                return reply
            replies = [wire(protocol, ui.call("start", "run", {"argv": argv(code), "background": True})),
                       wire(protocol, ui.call("release", "edit_file", {"path": "release", "old": "", "new": "go"})),
                       job_action("wait"), job_action("output"), wire(protocol, ui.response("Done"))]
            with self.subTest(protocol=protocol), backend(protocol, replies, captured, stream=protocol != "chat_completions") as url:
                config = Config(root, "fixture", base_url=url, wire_api=protocol, proxy="direct", stream=True, background_enabled=True, background_timeout=6,
                    checks=((sys.executable, "-c", "from pathlib import Path; assert Path('result').read_text()=='BACKGROUND_DONE'; print('FIXED_ACCEPTED')"),))
                with closing(Tools(config, lambda prompt: approvals.append(prompt) or True)) as tools:
                    history, complete = Agent(config, Provider(config), tools, lambda _: None).run("Run the command while preparing release, then inspect and verify")
                self.assertTrue(complete)
                self.assertEqual(len(captured), 5)
                self.assertEqual(len(approvals), 3)
                self.assertIn("JOB_FINISHED", json.dumps(history))
                self.assertIn("FIXED_ACCEPTED", json.dumps(history))
                validate_history(history)


class JobTuiTests(unittest.IsolatedAsyncioTestCase):
    setUp = ui.TuiTests.setUp
    asyncSetUp = ui.TuiTests.asyncSetUp
    until = ui.TuiTests.until
    click = ui.TuiTests.click

    async def test_local_start_idle_cancel_and_unmount_do_not_call_model_or_leave_processes(self):
        self.config = replace(self.config, background_enabled=True)
        for mode in ("cancel", "unmount"):
            provider = ui.ScriptedProvider([])
            app = ui.TuiTests.app(self, provider)
            async with app.run_test(size=(80, 24)) as pilot:
                app.slash_command('/job start ' + json.dumps({"argv": argv("import time; time.sleep(10)")}, ensure_ascii=False))
                await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
                self.assertFalse(app.tools.jobs.records)
                await self.click(pilot, "#approve")
                await self.until(pilot, lambda: not app.busy)
                self.assertTrue(app.tools.jobs.unsettled())
                self.assertEqual(provider.calls, [])
                if mode == "cancel":
                    await pilot.press("ctrl+c")
                    await self.until(pilot, lambda: not app.tools.jobs.unsettled())
                    await self.until(pilot, lambda: "后台任务状态" in app.transcript)
                    self.assertIn("后台任务状态", app.transcript)
            app.close()
            self.assertFalse(app.tools.jobs.records)
            self.assertEqual(provider.calls, [])

    async def test_approval_refusal_and_new_guidance_never_launch_old_command(self):
        self.config = replace(self.config, background_enabled=True)
        provider = ui.ScriptedProvider([ui.response("new request")])
        app = ui.TuiTests.app(self, provider)
        async with app.run_test(size=(100, 32)) as pilot:
            for mode in ("reject", "steer"):
                app.slash_command('/job start ' + json.dumps({"argv": argv("raise AssertionError('must not launch')")}))
                await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
                if mode == "reject":
                    await self.click(pilot, "#reject")
                else:
                    app.queue_guidance("inspect locally instead")
                await self.until(pilot, lambda: not app.busy)
                self.assertFalse(app.tools.jobs.records)
            self.assertEqual(len(provider.calls), 1)


class JobChatTests(unittest.TestCase):
    def test_actual_chat_starts_and_closes_owned_task_without_model_request(self):
        from tests import test_settings as fixture
        case = fixture.SettingsTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.write('provider="p"\n[providers.p]\nmodel="fixture"\napi_key="cli-job-private-fixture"\n')
        code = "import os,time; from pathlib import Path; Path('cli.pid').write_text(str(os.getpid())); time.sleep(10)"
        values = iter(['/job start ' + json.dumps({"argv": argv(code)}), "yes", "/jobs", "/exit"])
        def response(*_):
            value = next(values)
            if value == "/jobs":
                until = time.monotonic() + 3
                while not (case.root / "cli.pid").exists() and time.monotonic() < until:
                    time.sleep(.02)
            return value
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=response), patch.object(Provider, "complete", side_effect=AssertionError("local control cannot call a model")):
            status, output, error = case.invoke(["--config", str(case.path), "--chat", "--background"])
        self.assertEqual(status, 2, error)
        self.assertIn('"started": true', output)
        self.assertNotIn("cli-job-private-fixture", output + error)
        self.assertFalse(running(int((case.root / "cli.pid").read_text())))


if __name__ == "__main__":
    unittest.main()
