"""Real disposable LSP processes, protocol fixtures and actual CLI/TUI paths."""
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
from sprig_agent.lsp import local_arguments, location_range, position
from sprig_agent.lsp_client import Client, LspError, MAX_FRAME, decode, encode
from sprig_agent.lsp_config import LspServer, parse_servers
from sprig_agent.provider import Provider
from sprig_agent.session import validate_history
from sprig_agent.settings import Settings, public_config
from sprig_agent.tools import Tools
from tests import test_tui as ui
from tests.test_mcp_stdio import objects, rows, running, wire
from tests.test_native_protocols import backend

FIXTURE = Path(__file__).parent / "fixtures/lsp_server.py"
SOURCE = "🎍symbol()\n"


class LspTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "目录 space"
        self.root.mkdir()
        self.state = self.parent / "state"
        self.state.mkdir()
        (self.root / "main.cpp").write_text(SOURCE, encoding="utf-8")
        self.approvals, self.cancel = [], threading.Event()

    def server(self, **values):
        return LspServer("cpp", (sys.executable, "-u", "-B", str(FIXTURE), str(self.state)), ((".cpp", "cpp"),), **values)

    def tools(self, *, server=None, approve=None, **values):
        config = Config(self.root, "fixture", api_key="model-fixture-secret", lsp_servers=(server or self.server(),), **values)
        tools = Tools(config, approve or (lambda text: self.approvals.append(text) or True), self.cancel)
        self.addCleanup(tools.close)
        return tools

    def mode(self, value):
        (self.state / "mode").write_text(value)

    def query(self, tools, action="definition", **values):
        args = {"action": action, "server": "cpp", "path": "main.cpp"}
        if action in {"definition", "references", "implementation", "hover"}:
            args.update(line=1, column=2)
        args.update(values)
        return tools.execute("lsp", args)

    def unpack(self, result):
        self.assertNotIn("error", result)
        self.assertTrue(result["complete"], result)
        return json.loads(result["content"])

    def test_startup_cwd_chain_is_bound_to_the_query_approval(self):
        parent = self.root / "configured"
        cwd = parent / "working"
        cwd.mkdir(parents=True)
        moved = self.root / "original"

        def approve(_):
            if os.name == "nt":
                with self.assertRaises(OSError):
                    parent.rename(moved)
            else:
                parent.rename(moved)
                parent.mkdir()
                (moved / cwd.name).rename(cwd)
            return True

        tools = self.tools(server=self.server(cwd="configured/working"), approve=approve)
        result = self.query(tools)
        if os.name == "nt":
            self.assertNotIn("error", result)
            with self.assertRaises(OSError):
                parent.rename(moved)
            tools.approve = lambda _: True
            self.assertTrue(tools.execute("lsp", {"action": "disconnect", "server": "cpp"})["disconnected"])
            parent.rename(moved)
        else:
            self.assertIn("working directory changed", result["error"])
            self.assertEqual(rows(self.state), [])

    @unittest.skipUnless(os.name == "posix", "POSIX permits directory replacement while a process holds cwd")
    def test_existing_lsp_server_rejects_query_when_cwd_changes_during_approval(self):
        cwd = self.root / "configured"
        cwd.mkdir()
        tools = self.tools(server=self.server(cwd=cwd.name))
        self.assertNotIn("error", self.query(tools))
        previous = rows(self.state)

        def approve(_):
            cwd.rename(self.root / "original")
            cwd.mkdir()
            return True

        tools.approve = approve
        result = self.query(tools, "hover")
        self.assertIn("working directory changed", result["error"])
        self.assertEqual(rows(self.state), previous)
        self.assertNotIn("cpp", tools.lsp_manager.clients)

    def test_config_resolution_privacy_profiles_and_minimal_environment(self):
        data = {"provider": "p", "providers": {"p": {"model": "fixture", "api_key_env": "OPENAI_API_KEY"}},
                "lsp": {"servers": {"cpp": {"command": list(self.server().command), "languages": {".cpp": "cpp"},
                                             "env_from": {"LSP_FIXTURE_TOKEN": "OWN_LSP_SECRET"}}}},
                "execution": {"inherit_env": True, "env": {"COMMAND_OVERRIDE": "override"}}}
        env = {"OPENAI_API_KEY": "model-fixture-secret", "OWN_LSP_SECRET": "own-lsp-fixture-token"}
        config, metadata = Settings(self.parent / "config.toml", data).resolve({"workdir": self.root}, env)
        tools = Tools(config, lambda text: self.approvals.append(text) or True)
        self.addCleanup(tools.close)
        with patch.dict(os.environ, {**env, "UNRELATED_FIXTURE_KEY": "unrelated-secret"}):
            result = self.query(tools, "hover")
        self.assertIn("[REDACTED]", self.unpack(result)["content"])
        self.assertEqual(rows(self.state)[0]["own"], env["OWN_LSP_SECRET"])
        self.assertFalse(any(rows(self.state)[0][key] for key in ("model_key_present", "unrelated_present", "command_override_present")))
        for value in (config.binding(), public_config(config, metadata), self.approvals, repr(config.lsp_servers[0])):
            self.assertNotIn(env["OWN_LSP_SECRET"], str(value))
        self.assertIn(env["OWN_LSP_SECRET"], config.credential_secrets)
        changed = config.lsp_servers[0].resolve({"OWN_LSP_SECRET": "rotated-lsp-fixture-token"})
        self.assertEqual(changed.public(), config.lsp_servers[0].public())
        self.assertNotEqual(replace(config, lsp_servers=()).binding(), config.binding())
        data["profiles"] = {"review": {"lsp": {"servers": {"cpp": {"command": ["other-server"], "languages": {".cpp": "cpp"}}}}}}
        selected, _ = Settings(self.parent / "config.toml", data).resolve({"workdir": self.root, "profile": "review"}, env)
        self.assertEqual(selected.lsp_servers[0].command, ("other-server",))
        project = self.root / "project.toml"
        project.write_text('[lsp.servers.cpp]\ncommand=["server"]\nlanguages={".cpp"="cpp"}\n')
        with self.assertRaises(ValueError):
            Settings(self.parent / "config.toml", data).with_project(project)

    def test_invalid_config_is_rejected_before_startup(self):
        valid = {"command": ["server"], "languages": {".cpp": "cpp"}}
        for value in ({**valid, "command": "server"}, {**valid, "languages": {}}, {**valid, "cwd": "../elsewhere"},
                      {**valid, "languages": {"cpp": "cpp"}}, {**valid, "languages": {".CPP": "cpp", ".cpp": "cpp"}},
                      {**valid, "timeout": float("nan")}, {**valid, "install": True}, {**valid, "env": {"TOKEN": "x"}, "env_from": {"TOKEN": "X"}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_servers({"cpp": value})
        with self.assertRaises(ValueError):
            parse_servers({str(i): valid for i in range(9)})

    def test_fresh_approval_each_query_utf16_results_versions_and_no_read_id(self):
        tools = self.tools()
        for action in ("definition", "references", "implementation", "hover", "symbols", "diagnostics"):
            value = self.unpack(self.query(tools, action))
            self.assertTrue(value["not_authorization"] and value["not_acceptance"])
            self.assertNotIn("read_id", value)
            self.assertEqual(value["source"]["version"], 1)
            if "results" in value:
                self.assertEqual(value["results"][0]["start_column"], 2)
        self.assertEqual(len(self.approvals), 6)
        self.assertIn("NOT an OS sandbox", self.approvals[0])
        self.assertEqual(sum("started" in row for row in rows(self.state)), 1)
        sent = next(row for row in rows(self.state) if row.get("method") == "textDocument/definition")
        self.assertEqual(sent["params"]["position"], {"line": 0, "character": 2})
        old = tools.read_file("main.cpp")["read_id"]
        (self.root / "main.cpp").write_text(SOURCE + "// updated\n", encoding="utf-8")
        self.assertEqual(self.unpack(self.query(tools))["source"]["version"], 2)
        self.assertIsNone(tools.read_versions.resolve(old, ("main.cpp",)))
        changes = [row for row in rows(self.state) if row.get("method") == "textDocument/didChange"]
        self.assertEqual(len(changes), 1)
        self.assertIn("range", changes[0]["params"]["contentChanges"][0])
        self.assertTrue(tools.execute("lsp", {"action": "disconnect", "server": "cpp"})["approved"])
        self.assertEqual(len(self.approvals), 8)
        self.assertFalse(tools.lsp_manager.clients)

    def test_decline_readonly_disabled_missing_environment_and_closed_root_never_spawn(self):
        for values, approve in (({}, lambda _: False), ({"read_only": True}, self.fail),
                                ({"disabled_tools": ("lsp",)}, self.fail), ({"disabled_tools": ("read_file",)}, self.fail),
                                ({"max_output": 512}, self.fail)):
            tools = self.tools(approve=approve, **values)
            self.assertFalse(self.query(tools).get("approved", False))
        tools = self.tools(server=self.server(env_from=(("TOKEN", "MISSING_LSP_FIXTURE"),)).resolve({}), approve=self.fail)
        self.assertIn("error", self.query(tools))
        tools = self.tools(approve=self.fail)
        tools.root.close()
        self.assertIn("error", self.query(tools))
        self.assertEqual(rows(self.state), [])

    def test_paths_credentials_and_oversize_blocked_before_approval(self):
        tools = self.tools(approve=self.fail)
        (self.root / "secret.cpp").write_text("model-fixture-secret")
        (self.root / "large.cpp").write_bytes(b"x" * (512 * 1024 + 1))
        (self.root / ".env.cpp").write_text(SOURCE, encoding="utf-8")
        for path in ("../outside.cpp", "secret.cpp", "large.cpp", ".env.cpp"):
            self.assertIn("error", self.query(tools, path=path))
        self.assertEqual(rows(self.state), [])

    def test_source_changed_during_approval_is_not_sent(self):
        def approve(_):
            (self.root / "main.cpp").write_text(SOURCE + "// changed", encoding="utf-8")
            return True
        tools = self.tools(approve=approve)
        self.assertIn("source changed", self.query(tools)["error"])
        self.assertEqual(rows(self.state), [])

    def test_source_changed_during_query_discards_result_and_closes_process(self):
        self.mode("pause")
        tools, result = self.tools(), []
        worker = threading.Thread(target=lambda: result.append(self.query(tools)))
        worker.start()
        try:
            until = time.monotonic() + 5
            while not any(row.get("method") == "textDocument/definition" for row in rows(self.state)):
                self.assertLess(time.monotonic(), until)
                time.sleep(.01)
            (self.root / "main.cpp").write_text(SOURCE + "// changed", encoding="utf-8")
        finally:
            (self.state / "release").touch()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertIn("stale results discarded", result[0]["error"])
        self.assertFalse(tools.lsp_manager.clients)
        self.assertTrue(all(not running(row["started"]) for row in rows(self.state) if "started" in row))

    def test_server_edits_commands_registrations_denied_configuration_empty(self):
        self.mode("requests")
        tools = self.tools()
        self.unpack(self.query(tools))
        # Ensure replies have reached the peer by completing a second exchange.
        self.unpack(self.query(tools))
        replies = [row for row in rows(self.state) if str(row.get("id", "")).startswith("server-")]
        for index in range(3):
            self.assertEqual(next(row for row in replies if row["id"] == "server-" + str(index))["error"]["code"], -32601)
        self.assertEqual(next(row for row in replies if row["id"] == "server-3")["result"], [None])
        self.assertEqual(len(self.approvals), 2)
        self.assertEqual((self.root / "main.cpp").read_text(encoding="utf-8"), SOURCE)
        self.assertGreaterEqual(tools.lsp_manager.status()["servers"][0]["connection"]["denied_requests"], 3)

    def test_private_external_and_nonfile_targets_are_omitted(self):
        private = self.root / "private"
        private.mkdir()
        (private / "secret.cpp").write_text(SOURCE, encoding="utf-8")
        outside = self.parent / "outside.cpp"
        outside.write_text(SOURCE, encoding="utf-8")
        targets = [(private / "secret.cpp").as_uri(), outside.as_uri(), "https://example.invalid/a.cpp", "file://remote/a.cpp"]
        (self.state / "targets.json").write_text(json.dumps(targets))
        self.mode("private_targets")
        value = self.unpack(self.query(self.tools(excluded_paths=("private/",))))
        self.assertEqual(value["omitted"], 4)
        self.assertEqual(len(value["results"]), 1)
        self.assertNotIn("secret.cpp", json.dumps(value))

    def test_paging_unicode_and_changed_privacy_expire_handles_without_rpc(self):
        self.mode("large_hover")
        tools = self.tools(max_output=1024)
        first = self.query(tools, "hover")
        self.assertNotIn("error", first)
        before = len(rows(self.state))
        text, page = "", first
        while True:
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False).encode()), 1024)
            text += page["content"]
            if page["complete"]:
                break
            page = tools.execute("lsp", {"action": "result", "result_id": first["result_id"], "offset": page["next_offset"]})
        self.assertEqual(json.loads(text)["content"], "中文🎍" * 5000)
        self.assertEqual(len(rows(self.state)), before)
        (self.root / "main.cpp").write_text("model-fixture-secret")
        self.assertIn("error", tools.execute("lsp", {"action": "result", "result_id": first["result_id"]}))
        self.assertNotIn(first["result_id"], tools.lsp_manager.results)

    def test_push_stale_and_versionless_reports_and_pull(self):
        for mode in ("stale_diagnostics", "versionless", "pull"):
            self.mode(mode)
            tools = self.tools()
            value = self.unpack(self.query(tools, "diagnostics"))
            self.assertTrue(value["available"])
            self.assertEqual(value["version_confirmed"], mode != "versionless")
            self.assertEqual(value["mode"], "pull" if mode == "pull" else "push")
            self.assertNotIn("STALE_REPORT", json.dumps(value))
            if mode == "stale_diagnostics":
                self.assertEqual(tools.lsp_manager.status()["servers"][0]["connection"]["discarded_diagnostics"], 1)
            tools.close()

    def test_absent_diagnostics_cannot_become_clean_check(self):
        tools = self.tools()
        manager = tools.lsp_manager
        primary = tools._lsp_document("main.cpp")
        value, _ = manager._normalize("diagnostics", None, primary)
        self.assertFalse(value["available"] or value["version_confirmed"])
        self.assertIn("not a clean check", value["reason"])

    def test_full_sync_and_four_open_document_limit(self):
        self.mode("full_sync")
        tools = self.tools()
        self.unpack(self.query(tools))
        (self.root / "main.cpp").write_text(SOURCE + "changed", encoding="utf-8")
        self.unpack(self.query(tools))
        change = next(row for row in rows(self.state) if row.get("method") == "textDocument/didChange")
        self.assertNotIn("range", change["params"]["contentChanges"][0])
        for index in range(5):
            path = "other" + str(index) + ".cpp"
            (self.root / path).write_text(SOURCE, encoding="utf-8")
            self.unpack(self.query(tools, path=path))
        self.assertEqual(len(tools.lsp_manager.clients["cpp"].documents), 4)
        self.assertTrue(any(row.get("method") == "textDocument/didClose" for row in rows(self.state)))

    def test_bad_protocol_or_unsupported_capability_closes_without_retry(self):
        for mode in ("utf8", "no_sync", "no_capability", "bad_header", "duplicate_json", "mismatch", "error"):
            self.mode(mode)
            tools = self.tools()
            before = sum("started" in row for row in rows(self.state))
            value = self.query(tools)
            self.assertIn("error", value, mode)
            self.assertNotIn("PRIVATE_SERVER_ERROR", str(value))
            self.assertFalse(tools.lsp_manager.clients)
            self.assertEqual(sum("started" in row for row in rows(self.state)), before + 1)

    def test_fragmented_frames_and_total_deadline_despite_activity(self):
        self.mode("fragment")
        tools = self.tools()
        self.assertEqual(self.unpack(self.query(tools, "hover"))["content"], "中文🎍")
        tools.close()
        self.mode("drip")
        tools = self.tools(server=self.server(timeout=1, deadline=.4))
        start = time.monotonic()
        self.assertIn("limit exceeded", self.query(tools)["error"])
        self.assertLess(time.monotonic() - start, 3)
        self.assertFalse(tools.lsp_manager.clients)

    def test_cancellation_owned_child_cleanup_and_reset_no_replay(self):
        self.mode("child")
        tools = self.tools()
        result = self.query(tools)
        self.unpack(result)
        pids = [row[key] for row in rows(self.state) for key in ("started", "child") if key in row]
        Agent(tools.config, Mock(), tools, lambda _: None).reset_runtime()
        self.assertTrue(all(not running(pid) for pid in pids))
        self.assertIn("unavailable", tools.execute("lsp", {"action": "result", "result_id": result["result_id"]})["error"])
        self.mode("normal")
        self.unpack(self.query(tools))
        self.mode("waiting")
        timer = threading.Timer(.1, self.cancel.set)
        timer.start()
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.query(tools)
        finally:
            timer.cancel()
            self.cancel.clear()
        self.assertFalse(tools.lsp_manager.clients)

    def test_schema_visibility_no_start_on_status_and_local_argument_positions(self):
        tools = self.tools(approve=self.fail)
        self.assertIn("servers", self.unpack(tools.execute("lsp", {"action": "status"})))
        self.assertEqual(rows(self.state), [])
        schemas = Agent(tools.config, Mock(), tools, lambda _: None).schemas
        self.assertIn("lsp", [schema["function"]["name"] for schema in schemas])
        readonly = self.tools(read_only=True, approve=self.fail)
        self.assertNotIn("lsp", [schema["function"]["name"] for schema in Agent(readonly.config, Mock(), readonly, lambda _: None).schemas])
        self.assertEqual(local_arguments('definition cpp "path space.cpp" 2 3')["path"], "path space.cpp")
        with self.assertRaises(ValueError):
            local_arguments("definition cpp main.cpp 1")
        self.assertEqual(position("a\r🎍b\r\nc", 2, 2), {"line": 1, "character": 2})
        for point in ((0, 1), (True, 1), (1, 99)):
            with self.assertRaises(LspError):
                position(SOURCE, *point)
        with self.assertRaises(LspError):
            location_range(SOURCE, {"start": {"line": 0, "character": 1}, "end": {"line": 0, "character": 2}})

    def test_frame_and_json_limits_reject_duplicates_partial_and_oversize(self):
        for raw in (b'{"jsonrpc":"2.0","id":1,"id":2}', b'{"jsonrpc":"2.0","result":NaN}', b'[]', b'\xff'):
            with self.assertRaises(LspError):
                decode(raw)
        with self.assertRaises(LspError):
            encode({"value": "x" * MAX_FRAME})
        client = Client.__new__(Client)
        client.expected = None
        client.buffer = bytearray(encode({"jsonrpc": "2.0", "result": "中文🎍"}))
        last = client.buffer.pop()
        self.assertIsNone(client._frame())
        client.buffer.append(last)
        self.assertEqual(client._frame()["result"], "中文🎍")
        for raw in (b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}", b"X" * 8193,
                    b"Content-Length: 2\r\nContent-Type: text/plain\r\n\r\n{}"):
            client.buffer, client.expected = bytearray(raw), None
            with self.assertRaises(LspError):
                client._frame()

    def test_three_model_protocols_dispatch_semantics_with_no_automatic_edits(self):
        for protocol in ("chat_completions", "messages", "responses"):
            captured, approvals = [], []
            replies = [wire(protocol, ui.call("semantic", "lsp", {"action": "definition", "server": "cpp", "path": "main.cpp", "line": 1, "column": 2})),
                       wire(protocol, ui.response("Observed definition"))]
            with self.subTest(protocol=protocol), backend(protocol, replies, captured, stream=protocol != "chat_completions") as url:
                config = Config(self.root, "fixture", base_url=url, wire_api=protocol, proxy="direct", stream=True, lsp_servers=(self.server(),))
                tools = Tools(config, lambda text: approvals.append(text) or True)
                self.addCleanup(tools.close)
                history, complete = Agent(config, Provider(config), tools, lambda _: None).run("Find the definition")
                self.assertTrue(complete)
                self.assertEqual(len(approvals), 1)
                self.assertEqual(len(captured), 2)
                self.assertTrue(any(row.get("source", {}).get("version") == 1 for row in objects(captured[-1])))
                self.assertEqual((self.root / "main.cpp").read_text(encoding="utf-8"), SOURCE)
                validate_history(history)
                tools.close()


class LspTuiTests(unittest.IsolatedAsyncioTestCase):
    setUp = ui.TuiTests.setUp
    asyncSetUp = ui.TuiTests.asyncSetUp
    until = ui.TuiTests.until
    click = ui.TuiTests.click

    async def test_local_refusal_guidance_approval_and_cancel_own_the_server(self):
        state = self.parent / "state"
        state.mkdir()
        (self.root / "main.cpp").write_text(SOURCE, encoding="utf-8")
        server = LspServer("cpp", (sys.executable, "-u", "-B", str(FIXTURE), str(state)), ((".cpp", "cpp"),))
        self.config = replace(self.config, lsp_servers=(server,))
        provider = ui.ScriptedProvider([ui.response("New guidance")])
        app = ui.TuiTests.app(self, provider)
        async with app.run_test(size=(100, 32)) as pilot:
            app.slash_command("/lsp definition cpp main.cpp 1 2")
            await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
            await self.click(pilot, "#reject")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(rows(state), [])
            app.slash_command("/lsp definition cpp main.cpp 1 2")
            await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
            app.queue_guidance("inspect locally")
            await self.until(pilot, lambda: not app.busy and len(provider.calls) == 1)
            self.assertEqual(rows(state), [])
            app.slash_command("/lsp definition cpp main.cpp 1 2")
            await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(app.task_status, 2)
            (state / "mode").write_text("waiting")
            app.slash_command("/lsp hover cpp main.cpp 1 2")
            await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: any(row.get("method") == "textDocument/hover" for row in rows(state)))
            await pilot.press("ctrl+c")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(app.last_operation_status, 130)
        app.close()
        self.assertTrue(all(not running(row["started"]) for row in rows(state) if "started" in row))


class LspChatTests(unittest.TestCase):
    def test_actual_cli_approved_server_failure_invalidates_previous_success(self):
        from tests import test_settings as fixture
        case = fixture.SettingsTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        state = case.parent / "state"
        state.mkdir()
        (state / "mode").write_text("error")
        (case.root / "main.cpp").write_text(SOURCE, encoding="utf-8")
        command = [sys.executable, "-u", "-B", str(FIXTURE), str(state)]
        case.write('provider="p"\n[providers.p]\nmodel="fixture"\napi_key="model-cli-fixture-secret"\n[lsp.servers.cpp]\ncommand=' + json.dumps(command) + '\nlanguages={".cpp"="cpp"}\n')
        inputs = iter(["/lsp definition cpp main.cpp 1 2", "yes", "/exit"])
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=lambda *_: next(inputs)), patch.object(Provider, "complete", return_value=ui.response("Initial success")) as provider:
            status, output, error = case.invoke(["--config", str(case.path), "--chat", "Initial task"])
        self.assertEqual(provider.call_count, 1)
        self.assertIn("language server rejected", output)
        self.assertEqual(status, 2, error)
        self.assertTrue(all(not running(row["started"]) for row in rows(state) if "started" in row))

    def test_actual_cli_local_queries_do_not_call_model_and_exit_cleans_up(self):
        from tests import test_settings as fixture
        case = fixture.SettingsTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        state = case.parent / "state"
        state.mkdir()
        (case.root / "main.cpp").write_text(SOURCE, encoding="utf-8")
        command = [sys.executable, "-u", "-B", str(FIXTURE), str(state)]
        case.write('provider="p"\n[providers.p]\nmodel="fixture"\napi_key="model-cli-fixture-secret"\n[lsp.servers.cpp]\ncommand=' + json.dumps(command) + '\nlanguages={".cpp"="cpp"}\n')
        inputs = iter(["/lsp", "/lsp definition cpp main.cpp 1 2", "yes", "/lsp diagnostics cpp main.cpp", "yes", "/exit"])
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=lambda *_: next(inputs)), patch.object(Provider, "complete", side_effect=AssertionError("local commands cannot call a model")):
            status, output, error = case.invoke(["--config", str(case.path), "--chat"])
        self.assertEqual(status, 2, error)
        self.assertNotIn("model-cli-fixture-secret", output + error)
        pids = [row["started"] for row in rows(state) if "started" in row]
        self.assertEqual(len(pids), 1)
        self.assertFalse(running(pids[0]))
