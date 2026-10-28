"""Real disposable stdio processes; no external programs, models or credentials."""
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
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.mcp_config import McpServer, parse_servers
from sprig_agent.mcp_http import McpError
from sprig_agent.policy import command_environment
from sprig_agent.provider import Provider
from sprig_agent.session import SessionStore, validate_history
from sprig_agent.settings import Settings
from sprig_agent.tools import Tools
from tests import test_tui as ui
from tests.test_generation_options import envelope
from tests.test_native_protocols import backend, native
from tests.test_provider_recovery import stream

FIXTURE = Path(__file__).parent / "fixtures/mcp_stdio_server.py"


def running(pid):
    if os.name == "nt":
        import ctypes as c
        import ctypes.wintypes as w
        kernel = c.WinDLL("kernel32", use_last_error=True)
        open_process = kernel.OpenProcess
        open_process.argtypes, open_process.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
        handle = open_process(0x100000, False, pid)
        if not handle:
            return False
        wait = kernel.WaitForSingleObject
        wait.argtypes, wait.restype = [w.HANDLE, w.DWORD], w.DWORD
        try:
            return wait(handle, 0) != 0
        finally:
            close = kernel.CloseHandle
            close.argtypes = [w.HANDLE]
            close(handle)
    try:
        os.kill(pid, 0)
        stat = Path('/proc') / str(pid) / 'stat'
        return not stat.exists() or stat.read_text().split()[2] != 'Z'
    except (ProcessLookupError, FileNotFoundError):
        return False


class ProcessProbeTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX /proc disappearance; Windows uses a process handle")
    def test_proc_disappearing_after_exists_is_exited_without_hiding_other_errors(self):
        with patch(__name__ + ".os.kill"), patch(__name__ + ".Path") as path:
            status = path.return_value.__truediv__.return_value.__truediv__.return_value
            status.exists.return_value = True
            status.read_text.side_effect = FileNotFoundError
            self.assertFalse(running(12345))
            status.read_text.side_effect = PermissionError
            with self.assertRaises(PermissionError):
                running(12345)


def rows(state):
    path = state / "rpc.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def wire(protocol, message):
    if protocol == "chat_completions":
        delta = dict(message)
        if "tool_calls" in delta:
            delta["tool_calls"] = [{"index": i, **row} for i, row in enumerate(delta["tool_calls"])]
        return stream(delta, "tool_calls" if "tool_calls" in delta else "stop")
    calls = [(row["id"], row["function"]["name"], json.loads(row["function"]["arguments"])) for row in message.get("tool_calls", [])]
    return native(protocol, message.get("content"), calls, reasoning=False)


def objects(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from objects(item)
    elif isinstance(value, list):
        for item in value:
            yield from objects(item)
    elif isinstance(value, str):
        try:
            yield from objects(json.loads(value))
        except ValueError:
            pass


class McpStdioTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.state = self.parent / "state"
        self.state.mkdir()
        self.cancel = threading.Event()
        self.approvals = []

    def server(self, **values):
        return McpServer("local", transport="stdio", protocol_version="2026-07-28",
                         command=(sys.executable, "-u", "-B", str(FIXTURE), str(self.state)), **values)

    def tools(self, *, server=None, approve=None, **values):
        config = Config(self.root, "fixture", api_key="model-fixture-secret", mcp_servers=(server or self.server(),), **values)
        tools = Tools(config, approve or (lambda text: self.approvals.append(text) or True), self.cancel)
        self.addCleanup(tools.close)
        return tools

    def action(self, tools, action, **values):
        return tools.execute("mcp", {"action": action, "server": "local", **values})

    def mode(self, name):
        (self.state / "mode").write_text(name, encoding="utf-8")

    def test_configuration_auth_snapshot_public_binding_and_minimal_environment(self):
        data = {"provider": "p", "providers": {"p": {"model": "fixture", "api_key_env": "OPENAI_API_KEY"}},
                "mcp": {"servers": {"local": {"transport": "stdio", "command": list(self.server().command),
                                               "env_from": {"MCP_FIXTURE_TOKEN": "OWN_MCP_SECRET"}}}},
                "execution": {"inherit_env": True, "env": {"COMMAND_OVERRIDE": "ordinary-override"}}}
        environment = {"OPENAI_API_KEY": "model-fixture-secret", "OWN_MCP_SECRET": "own-server-fixture-token"}
        config, _ = Settings(self.parent / "config.toml", data).resolve({"workdir": self.root}, environment)
        with patch.dict(os.environ, {**environment, "UNRELATED_FIXTURE_KEY": "other-fixture-secret"}):
            tools = Tools(config, lambda _: True)
            self.addCleanup(tools.close)
            self.assertTrue(self.action(tools, "connect")["approved"])
        started = rows(self.state)[0]
        self.assertEqual(started["own"], environment["OWN_MCP_SECRET"])
        self.assertFalse(started["model_key_present"] or started["unrelated_present"] or started["command_override_present"])
        self.assertNotIn(environment["OWN_MCP_SECRET"], json.dumps(config.binding()))
        self.assertNotIn(environment["OWN_MCP_SECRET"], repr(config.mcp_servers[0]))
        self.assertIn(environment["OWN_MCP_SECRET"], config.credential_secrets)
        changed_key = config.mcp_servers[0].resolve({"OWN_MCP_SECRET": "rotated-server-fixture-token"})
        self.assertEqual(config.mcp_servers[0].public(), changed_key.public())
        self.assertNotEqual(self.server(env=(("NODE_ENV", "production"),)).public()["environment_sha256"],
                            self.server(env=(("NODE_ENV", "development"),)).public()["environment_sha256"])
        with patch.dict(os.environ, environment):
            self.assertNotIn("OWN_MCP_SECRET", command_environment(config))
        for values in ({"transport": "stdio", "command": []}, {"transport": "stdio", "url": "http://127.0.0.1"},
                       {"transport": "stdio", "command": ["python"], "cwd": "../outside"},
                       {"transport": "stdio", "command": ["python"], "headers": {"x": "y"}},
                       {"url": "https://example.invalid", "command": ["python"]}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                parse_servers({"local": values})

    def test_modern_and_legacy_full_rpc_cycle_requires_each_approval(self):
        for version in ("2026-07-28", "2025-11-25", "2025-06-18"):
            with self.subTest(version=version):
                entry = replace(self.server(), protocol_version=version)
                tools = self.tools(server=entry)
                self.assertTrue(self.action(tools, "connect").get("approved"))
                self.assertTrue(self.action(tools, "tools").get("approved"))
                token = tools.mcp_manager.tools["local"]["echo"][0]
                result = self.action(tools, "call", name="echo", catalog_id=token, arguments={"text": "中文正文"})
                self.assertIn("中文正文", result["content"])
                for action in ("resources", "templates", "prompts"):
                    self.assertTrue(self.action(tools, action).get("approved"))
                self.assertTrue(self.action(tools, "read_resource", uri="fixture://text").get("approved"))
                self.assertTrue(self.action(tools, "get_prompt", name="fixture").get("approved"))
                before = len(rows(self.state))
                tools.mcp_manager.control("result", result_id=result["result_id"])
                self.assertEqual(len(rows(self.state)), before)
                self.assertTrue(self.action(tools, "disconnect").get("approved"))
                self.assertFalse(tools.mcp_manager.clients)
                tools.close()
        self.assertEqual(len(self.approvals), 27)
        self.assertIn("NOT SANDBOXED", self.approvals[0])

    def test_decline_readonly_disabled_missing_environment_and_closed_root_never_spawn(self):
        for options, approve in (({}, lambda _: False), ({"read_only": True}, self.fail), ({"disabled_tools": ("mcp",)}, self.fail)):
            tools = self.tools(approve=approve, **options)
            self.assertFalse(self.action(tools, "connect").get("approved", False))
        missing = replace(self.server(), env_from=(("TOKEN", "MISSING_FIXTURE_VAR"),)).resolve({})
        tools = self.tools(server=missing, approve=self.fail)
        self.assertIn("error", self.action(tools, "connect"))
        tools = self.tools(approve=self.fail)
        tools.root.close()
        self.assertIn("error", self.action(tools, "connect"))
        self.assertEqual(rows(self.state), [])

    def test_cwd_and_instruction_change_during_approval_prevents_launch(self):
        path = self.root / "AGENTS.md"
        path.write_text("Original guidance", encoding="utf-8")
        def approve(_):
            path.write_text("Updated guidance", encoding="utf-8")
            return True
        tools = self.tools(approve=approve)
        tools.instructions.mark_served(tools.instructions.context())
        with tools.instructions.model_action():
            self.assertIn("error", self.action(tools, "connect"))
        self.assertEqual(rows(self.state), [])
        excluded = self.tools(server=self.server(cwd="private"), excluded_paths=("private",), approve=self.fail)
        (self.root / "private").mkdir()
        self.assertIn("error", self.action(excluded, "connect"))

    def test_errors_partial_frames_and_floods_close_without_retry_or_model_output(self):
        for fault in ("bad_id", "invalid", "partial", "request", "flood", "stderr_flood", "duplicate"):
            self.mode(fault)
            # Include a real interpreter startup in the handshake allowance.
            # Backpressure's tight RPC budget is tested after readiness below.
            tools = self.tools(server=self.server(timeout=3, deadline=4))
            result = self.action(tools, "connect")
            self.assertIn("error", result, fault)
            self.assertFalse(tools.mcp_manager.clients["local"].status()["process_running"])
            self.assertNotIn("PRIVATE_STDERR_FIXTURE", json.dumps(result))
            attempts = [r for r in rows(self.state) if r.get("method") in ("initialize", "server/discover")]
            self.assertEqual(len(attempts), ("bad_id", "invalid", "partial", "request", "flood", "stderr_flood", "duplicate").index(fault) + 1)
            tools.close()

    def test_backpressure_timeout_and_cancel_kill_owned_descendants(self):
        self.mode("block_input")
        tools = self.tools(server=self.server(timeout=5, deadline=6))
        self.assertTrue(self.action(tools, "connect").get("approved"))
        client = tools.mcp_manager.clients["local"]
        self.assertTrue(client.ready)  # Approval alone does not prove connection.
        client.server = replace(client.server, timeout=.3, deadline=.5)
        started = time.monotonic()
        with self.assertRaises(McpError):
            client.rpc("tools/list", {"padding": "x" * 60000})
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(client.status()["process_running"])
        self.mode("child")
        tools = self.tools(server=self.server(timeout=5, deadline=6))
        self.assertTrue(self.action(tools, "connect").get("approved"))
        pids = [entry[key] for entry in rows(self.state) for key in ("started", "child") if key in entry]
        self.mode("waiting")
        timer = threading.Timer(.1, self.cancel.set)
        timer.start()
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.action(tools, "tools")
        finally:
            timer.join()
            self.cancel.clear()
        self.assertTrue(all(not running(pid) for pid in pids))

    def test_stderr_allowance_is_enforced_without_retaining_or_returning_raw_logs(self):
        self.mode("stderr_flood")
        tools = self.tools(server=self.server(timeout=5, deadline=6))
        result = self.action(tools, "connect")
        self.assertIn("stderr exceeded", result["error"])
        client = tools.mcp_manager.clients["local"]
        self.assertGreater(client.stderr_bytes, 8 * 1024 * 1024)
        self.assertNotIn("PRIVATE_STDERR_FIXTURE", json.dumps(result))
        self.assertFalse(client.status()["process_running"])

    def test_total_deadline_includes_draining_a_pending_notification(self):
        tools = self.tools(server=self.server(timeout=.4, deadline=.4))
        self.assertTrue(self.action(tools, "connect").get("approved"))
        def approve(_):
            self.mode("delayed")
            until = time.monotonic() + 2
            while not (self.state / "partial-notification").exists() and time.monotonic() < until:
                time.sleep(.01)
            self.assertTrue((self.state / "partial-notification").exists())
            return True
        tools.approve = approve
        result = self.action(tools, "tools")
        self.assertIn("error", result)
        self.assertFalse(tools.mcp_manager.clients["local"].status()["process_running"])
        self.assertEqual(sum(r.get("method") == "tools/list" for r in rows(self.state)), 1)

    def test_observed_async_catalog_change_during_approval_prevents_call(self):
        tools = self.tools()
        self.action(tools, "connect")
        self.action(tools, "tools")
        token = tools.mcp_manager.tools["local"]["echo"][0]
        def approve(_):
            self.mode("changed")
            until = time.monotonic() + 2
            while not (self.state / "notified").exists() and time.monotonic() < until:
                time.sleep(.01)
            self.assertTrue((self.state / "notified").exists())
            return True
        tools.approve = approve
        self.assertIn("error", self.action(tools, "call", name="echo", catalog_id=token, arguments={"text": "no call"}))
        self.assertFalse(any(r.get("method") == "tools/call" for r in rows(self.state)))

    def test_exited_leader_status_does_not_lose_ownership_of_live_descendants(self):
        self.mode("parent_exit")
        tools = self.tools()
        self.assertTrue(self.action(tools, "connect").get("approved"))
        child = next(row["child"] for row in rows(self.state) if "child" in row)
        self.assertTrue(running(child))
        client = tools.mcp_manager.clients["local"]
        until = time.monotonic() + 2
        while client.status()["process_running"] and time.monotonic() < until:
            time.sleep(.01)
        self.assertFalse(client.status()["process_running"])
        tools.close()
        self.assertFalse(running(child))

    def test_new_conversation_and_session_resume_do_not_start_or_replay_process(self):
        tools = self.tools()
        self.action(tools, "connect")
        pids = [r["started"] for r in rows(self.state) if "started" in r]
        agent = Agent(tools.config, None, tools, lambda _: None)
        history = [{"role": "system", "content": "s"}, {"role": "user", "content": "old task"}, ui.response("old answer")]
        with closing(SessionStore(self.parent / "sessions", tools.config)) as store:
            store.save_new("source", history)
            agent.reset_runtime()
            self.assertTrue(all(not running(pid) for pid in pids))
            before = len(rows(self.state))
            self.assertEqual(store.load("source"), history)
            self.assertEqual(len(rows(self.state)), before)
            self.assertFalse(tools.mcp_manager.clients)

    def test_fragmented_unicode_and_old_read_ids_are_invalidated(self):
        self.mode("fragment")
        tools = self.tools()
        old = tools.read_versions.remember(("x.txt",), "old")
        self.action(tools, "connect")
        self.assertIsNone(tools.read_versions.resolve(old, ("x.txt",)))
        self.action(tools, "tools")
        token = tools.mcp_manager.tools["local"]["echo"][0]
        result = self.action(tools, "call", name="echo", catalog_id=token, arguments={"text": "中文🎍"})
        self.assertIn("中文🎍", result["content"])

    def test_workspace_relative_unicode_directory_and_missing_executable(self):
        directory = self.root / "目录 space"
        directory.mkdir()
        tools = self.tools(server=self.server(cwd="目录 space"))
        self.assertTrue(self.action(tools, "connect").get("approved"))
        self.assertEqual(Path(rows(self.state)[0]["cwd"]), directory)
        tools.close()
        missing = replace(self.server(), command=(str(self.root / "not-an-executable"),))
        tools = self.tools(server=missing)
        self.assertIn("error", self.action(tools, "connect"))
        self.assertEqual(len([r for r in rows(self.state) if "started" in r]), 1)
        if os.name == "nt":
            batch = self.root / "must-not-run.cmd"
            batch.write_text("@exit /b 0")
            tools = self.tools(server=replace(self.server(), command=(str(batch),)))
            self.assertIn("error", self.action(tools, "connect"))

    def test_nonsecret_flags_and_path_values_do_not_become_authentication_tokens(self):
        server = self.server(env=(("DEBUG", "1"), ("PYTHONPATH", str(self.parent)), ("NODE_ENV", "production")))
        tools = self.tools(server=server)
        self.assertTrue(self.action(tools, "connect").get("approved"))
        self.assertTrue(self.action(tools, "tools").get("approved"))
        token = tools.mcp_manager.tools["local"]["echo"][0]
        self.assertIn("production1", self.action(tools, "call", name="echo", catalog_id=token, arguments={"text": "production1"})["content"])
        self.assertNotIn("1", tools.config.credential_secrets)

    def test_quoted_multiline_credential_is_blocked_before_rpc_or_new_approval(self):
        secret = 'fixture-private-line\n"quoted-fixture-private-line"'
        server = replace(self.server(), env_from=(("MCP_FIXTURE_TOKEN", "OWN_PRIVATE_FIXTURE"),)).resolve({"OWN_PRIVATE_FIXTURE": secret})
        tools = self.tools(server=server)
        self.assertTrue(self.action(tools, "connect").get("approved"))
        self.assertTrue(self.action(tools, "tools").get("approved"))
        token = tools.mcp_manager.tools["local"]["echo"][0]
        count = len(self.approvals)
        result = self.action(tools, "call", name="echo", catalog_id=token, arguments={"text": secret})
        self.assertIn("credentials", result["error"])
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(len(self.approvals), count)
        self.assertFalse(any(r.get("method") == "tools/call" for r in rows(self.state)))

    def test_three_model_streaming_protocols_connect_discover_write_and_independent_check(self):
        for protocol in ("chat_completions", "messages", "responses"):
            subroot = self.root / protocol
            subroot.mkdir()
            captured, approvals = [], []
            def proposed(request):
                token = next(row["catalog_id"] for row in objects(request) if row.get("name") == "write_marker" and "catalog_id" in row)
                return wire(protocol, ui.call("invoke", "mcp", {"action": "call", "server": "local", "name": "write_marker", "catalog_id": token, "arguments": {"text": "WRITE_APPROVED"}}))
            replies = [wire(protocol, ui.call("connect", "mcp", {"action": "connect", "server": "local"})),
                       wire(protocol, ui.call("catalog", "mcp", {"action": "tools", "server": "local"})), proposed, wire(protocol, ui.response())]
            with self.subTest(protocol=protocol), backend(protocol, replies, captured, stream=protocol != "chat_completions") as url:
                config = Config(subroot, "fixture", base_url=url, wire_api=protocol, proxy="direct", stream=True, mcp_servers=(self.server(),),
                                checks=((sys.executable, "-c", "from pathlib import Path; assert Path('MCP_MARKER').read_text() == 'CREATED'; print('CHECK_ACCEPTED')"),))
                tools = Tools(config, lambda text: approvals.append(text) or True)
                self.addCleanup(tools.close)
                history, complete = Agent(config, Provider(config), tools, lambda _: None).run("Create the fixture marker through MCP and verify it")
                self.assertTrue(complete)
                self.assertEqual(len(approvals), 4)
                self.assertEqual(len(captured), 4)
                self.assertEqual((subroot / "MCP_MARKER").read_text(), "CREATED")
                self.assertIn("CHECK_ACCEPTED", json.dumps(history))
                validate_history(history)
                tools.close()


class McpStdioTuiTests(unittest.IsolatedAsyncioTestCase):
    setUp = ui.TuiTests.setUp
    asyncSetUp = ui.TuiTests.asyncSetUp
    until = ui.TuiTests.until
    click = ui.TuiTests.click

    async def test_refusal_guidance_and_close_never_start_or_leave_an_unowned_server(self):
        state = self.parent / "state"
        state.mkdir()
        server = McpServer("local", transport="stdio", command=(sys.executable, "-u", str(FIXTURE), str(state)))
        self.config = replace(self.config, mcp_servers=(server,))
        provider = ui.ScriptedProvider([ui.response("New guidance")])
        app = ui.TuiTests.app(self, provider)
        async with app.run_test(size=(100, 32)) as pilot:
            app.slash_command("/mcp connect local")
            await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
            await self.click(pilot, "#reject")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(rows(state), [])
            app.slash_command("/mcp connect local")
            await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
            app.queue_guidance("inspect locally")
            await self.until(pilot, lambda: not app.busy and len(provider.calls) == 1)
            self.assertEqual(rows(state), [])
            app.slash_command("/mcp connect local")
            await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(app.task_status, 2)
            pids = [r["started"] for r in rows(state) if "started" in r]
        app.close()
        self.assertTrue(all(not running(pid) for pid in pids))

    async def test_cancel_and_unmount_stop_a_waiting_server_without_replay(self):
        for mode in ("cancel", "unmount"):
            state = self.parent / mode
            state.mkdir()
            server = McpServer("local", transport="stdio", command=(sys.executable, "-u", str(FIXTURE), str(state)))
            self.config = replace(self.config, mcp_servers=(server,))
            app = ui.TuiTests.app(self, ui.ScriptedProvider([]))
            async with app.run_test(size=(100, 32)) as pilot:
                app.slash_command("/mcp connect local")
                await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
                await self.click(pilot, "#approve")
                await self.until(pilot, lambda: not app.busy)
                (state / "mode").write_text("waiting")
                app.slash_command("/mcp tools local")
                await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
                await self.click(pilot, "#approve")
                await self.until(pilot, lambda: any(r.get("method") == "tools/list" for r in rows(state)))
                if mode == "cancel":
                    await pilot.press("ctrl+c")
                    await self.until(pilot, lambda: not app.busy)
                    self.assertEqual(app.last_operation_status, 130)
                pids = [r["started"] for r in rows(state) if "started" in r]
            app.close()
            self.assertTrue(all(not running(pid) for pid in pids))
            self.assertEqual(sum(r.get("method") == "tools/list" for r in rows(state)), 1)


class McpStdioChatTests(unittest.TestCase):
    def test_actual_cli_local_commands_start_once_and_exit_without_model_calls(self):
        from tests import test_settings as fixture
        case = fixture.SettingsTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        state = case.parent / "state"
        state.mkdir()
        command = [sys.executable, "-u", str(FIXTURE), str(state)]
        case.write('provider="p"\n[providers.p]\nmodel="fixture"\napi_key="model-cli-fixture-secret"\n[mcp.servers.local]\ntransport="stdio"\ncommand=' + json.dumps(command) + '\n')
        inputs = iter(["/mcp connect local", "yes", "/mcp tools local", "yes", "/exit"])
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=lambda *_: next(inputs)), patch.object(Provider, "complete", side_effect=AssertionError("local commands cannot call a model")):
            status, output, error = case.invoke(["--config", str(case.path), "--chat"])
        self.assertEqual(status, 2, error)
        self.assertNotIn("model-cli-fixture-secret", output + error)
        pids = [r["started"] for r in rows(state) if "started" in r]
        self.assertEqual(len(pids), 1)
        self.assertFalse(running(pids[0]))
