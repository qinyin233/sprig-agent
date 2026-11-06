"""Retained logs are bounded historical data, not file capabilities or checks."""

from contextlib import closing
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent.agent import Agent
from sprig_agent import cli
from sprig_agent.command_logs import CommandLogs, LogCapture, LogError, MAX_STORE, RETENTION, FILE, TEMP, NAME
from sprig_agent.config import Config
from sprig_agent.context import prepare_context, request_size
from sprig_agent.filesystem import Directory
from sprig_agent.provider import Provider
from sprig_agent.session import SessionStore, SessionError
from sprig_agent.settings import Settings, public_config
from sprig_agent.tools import Tools
from sprig_agent.tui import ApprovalScreen, TaskInput
from tests import test_tui as fixture
from tests.test_glob_protocols import answer, values
from tests.test_native_protocols import backend


STATUS = {"returncode": 0, "timed_out": False, "output_limited": False}


class CommandLogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.config = Config(self.root, "fixture", command_log_dir=self.parent / "logs", max_output=512,
                             command_output_limit=65536)

    def tools(self, config=None, approve=lambda _: True, **options):
        tools = Tools(config or self.config, approve, **options)
        self.addCleanup(tools.close)
        return tools

    def save(self, logs, text="log text", **status):
        return logs.save(logs.preview(), text, False, {**STATUS, **status})

    def test_disabled_default_and_reject_readonly_cancel_do_not_create_storage(self):
        default = replace(self.config, command_log_dir=None)
        tools = self.tools(default)
        result = tools.execute("run", {"argv": [sys.executable, "-c", "print('ok')"]})
        self.assertNotIn("log", result)
        self.assertNotIn("read_log", [r["function"]["name"] for r in Agent(default, None, tools, lambda _: None).schemas])
        for config, approval in ((self.config, False), (replace(self.config, read_only=True), True)):
            with self.subTest(readonly=config.read_only):
                tools = self.tools(config, lambda _: approval)
                self.assertFalse(tools.execute("run", {"argv": [sys.executable, "-c", "print('NO')"]})["approved"])
        cancelled = threading.Event()
        cancelled.set()
        tools = self.tools(cancel_event=cancelled)
        with self.assertRaises(KeyboardInterrupt):
            tools.run([sys.executable, "-c", "print('NO')"])
        self.assertFalse(self.config.command_log_dir.exists())

    def test_real_command_log_recovers_omitted_middle_and_exact_json_pages(self):
        approvals = []
        tools = self.tools(approve=lambda description: approvals.append(description) or True)
        result = tools.execute("run", {"argv": [sys.executable, "-c", "import sys;sys.stdout.write('A'*2048+'MIDDLE_FAILURE_MARKER'+'B'*2048)"]})
        self.assertNotIn("MIDDLE_FAILURE_MARKER", result["output"])
        self.assertTrue(result["log"]["saved"])
        self.assertEqual(len(approvals), 1)
        self.assertIn("RETAIN", approvals[0])
        log_id = result["log"]["log_id"]
        text, offset = "", 0
        while True:
            page = tools.execute("read_log", {"log_id": log_id, "offset": offset})
            self.assertNotIn("error", page)
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False).encode()), 512)
            self.assertTrue(page["historical"])
            text += page["text"]
            if page["complete"]:
                break
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        self.assertIn("MIDDLE_FAILURE_MARKER", text)
        self.assertEqual(len(approvals), 1)
        self.assertEqual(result["returncode"], 0)

    def test_default_memory_log_pages_redacted_failure_and_ends_with_the_conversation(self):
        key = "fixture-private-memory-value"
        (self.root / "fixture.txt").write_text("A" * 2048 + key + "MIDDLE_FAILURE_MARKER" + "B" * 2048,
                                              encoding="utf-8")
        config = replace(self.config, command_log_dir=None, api_key=key)
        approvals = []
        tools = self.tools(config, lambda text: approvals.append(text) or True)
        argv = [sys.executable, "-I", "-B", "-c", "from pathlib import Path; import sys; "
                "sys.stdout.write(Path('fixture.txt').read_text()); sys.exit(7)"]
        result = tools.execute("run", {"argv": argv})
        self.assertEqual(result["returncode"], 7)
        self.assertNotIn("MIDDLE_FAILURE_MARKER", result["output"])
        self.assertEqual(result["log"]["storage"], "memory")
        self.assertFalse(result["log"]["saved"])
        self.assertIn("conversation's memory", approvals[0])
        identity = result["log"]["log_id"]
        self.assertEqual(tools.log_control()["logs"][0]["log_id"], identity)
        page = tools.log_control("read", log_id=identity, offset=2000)
        self.assertLessEqual(len(json.dumps(page, ensure_ascii=False).encode()), config.max_output)
        self.assertIn("[REDACTED]MIDDLE_FAILURE_MARKER", page["text"])
        self.assertNotIn(key, json.dumps(page))
        self.assertEqual(page["returncode"], 7)
        match = tools.execute("read_log", {"log_id": identity, "query": "MIDDLE_FAILURE_MARKER"})
        self.assertEqual(match["offset"], match["match_offset"])
        self.assertTrue(match["text"].startswith("MIDDLE_FAILURE_MARKER"))
        missing = tools.execute("read_log", {"log_id": identity, "offset": match["offset"] + 1,
                                             "query": "MIDDLE_FAILURE_MARKER"})
        self.assertIsNone(missing["match_offset"])
        self.assertEqual(missing["text"], "")
        self.assertEqual(len(approvals), 1)
        self.assertIsNone(tools.logs.state())
        self.assertFalse(self.config.command_log_dir.exists())
        Agent(config, None, tools, lambda _: None).reset_runtime()
        self.assertIn("error", tools.execute("read_log", {"log_id": identity}))
        self.assertEqual(tools.log_control()["logs"], [])

    def test_memory_log_replacement_and_deletion_respect_the_approved_generation(self):
        config = replace(self.config, command_log_dir=None)
        tools = self.tools(config)
        completed = {**STATUS, "output": "newer private command result"}
        generation, _ = tools.logs.memory_preview()
        first = tools.logs.retain_memory(generation, completed)["log_id"]
        self.assertIn("reason", tools.logs.retain_memory(generation, completed))
        self.assertEqual(tools.log_control()["logs"][0]["log_id"], first)

        def approve_stale_clear(_):
            generation, _ = tools.logs.memory_preview()
            tools.logs.retain_memory(generation, completed)
            return True

        with self.assertRaisesRegex(LogError, "changed during approval"):
            tools.logs.clear(approve_stale_clear)
        current = tools.log_control()["logs"][0]["log_id"]
        self.assertNotEqual(first, current)
        self.assertIn("error", tools.execute("read_log", {"log_id": first}))
        self.assertEqual(tools.logs.clear(lambda _: False), {"approved": False, "deleted": 0})
        self.assertEqual(tools.logs.clear(lambda _: True), {"approved": True, "deleted": 1})

    def test_memory_cap_is_independent_of_a_successful_command_output_cap(self):
        config = replace(self.config, command_log_dir=None, command_output_limit=2 * 1024 * 1024)
        tools = self.tools(config)
        result = tools.execute("run", {"argv": [sys.executable, "-I", "-B", "-c",
                                                "import sys;sys.stdout.buffer.write(b'A' * 1100000 + b'TAIL')"]})
        self.assertEqual(result["returncode"], 0)
        self.assertFalse(result["output_limited"])
        self.assertTrue(result["output"].endswith("TAIL"))
        self.assertEqual(result["log"]["bytes"], 1024 * 1024)
        self.assertTrue(result["log"]["capture_limited"])
        tail = tools.read_log(result["log"]["log_id"], query="TAIL")
        self.assertIsNone(tail["match_offset"])
        self.assertTrue(tail["capture_limited"])

    def test_memory_tool_schema_respects_existing_request_budget(self):
        config = replace(self.config, command_log_dir=None)
        tools = self.tools(config)
        generation, _ = tools.logs.memory_preview()
        tools.logs.retain_memory(generation, {**STATUS, "output": "fixture"})
        messages = [{"role": "system", "content": "fixture"}, {"role": "user", "content": "task"}]
        agent = Agent(config, None, tools, lambda _: None)
        before = request_size(config, messages, agent.schemas)
        for mode in ("hard limit", "compaction", "disabled", "available"):
            with self.subTest(mode=mode):
                constrained = (replace(config, max_context_bytes=before) if mode == "hard limit" else
                               replace(config, compact_context=True, max_context_bytes=before + 1024,
                                       context_trigger_ratio=(before + 10) / (before + 1024)) if mode == "compaction" else
                               replace(config, disabled_tools=("read_log",)) if mode == "disabled" else config)
                agent = Agent(constrained, None, tools, lambda _: None)
                projected, report = prepare_context(constrained, messages, agent.schemas, constrained.compact_context)
                schemas = agent._request_schemas(projected, report)
                names = [schema["function"]["name"] for schema in schemas]
                self.assertEqual("read_log" in names, mode == "available")
                self.assertEqual(report["after_bytes"], request_size(constrained, projected, schemas))

    def test_unicode_controls_redaction_across_blocks_and_rotated_keys_before_paging(self):
        key = "fixture-secret-abcdefgh"
        capture = LogCapture((key,))
        body = ("中🙂\x1b[31m" + key + "\n" + key[:12]).encode()
        for byte in body:
            capture.write(bytes([byte]))
        text, limited = capture.finish()
        self.assertNotIn(key, text)
        self.assertNotIn(key[:12], text)
        logs = self.tools().logs
        result = self.save(logs, text)
        self.assertFalse(limited)
        small = replace(self.config, max_output=256, secret_values=("rotated-secret-123456",))
        reader = CommandLogs(small)
        reader.restore(logs.state())
        saved = self.save(logs, "prefix " + "rotated-secret-123456" + " 中文🙂\x00\n" * 30)
        combined, offset = "", 0
        while True:
            page = reader.read(saved["log_id"], offset)
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False).encode()), 256)
            combined += page["text"]
            if page["complete"]:
                break
            offset = page["next_offset"]
        self.assertNotIn("rotated-secret", combined)
        self.assertIn("[REDACTED]", combined)
        self.assertNotIn(key, (self.config.command_log_dir / FILE).read_text(encoding="utf-8"))
        expanding_config = replace(self.config, command_log_dir=self.parent / "expanded")
        expanding_logs = CommandLogs(expanding_config)
        expanding = self.save(expanding_logs, "ZZ" * 4)
        bounded_reader = CommandLogs(replace(expanding_config, secret_values=("ZZ",)))
        bounded_reader.restore(expanding_logs.state())
        with patch("sprig_agent.command_logs.MAX_LOG", 17):
            page = bounded_reader.read(expanding["log_id"])
            self.assertTrue(page["capture_limited"])
            self.assertNotIn("ZZ", page["text"])
            self.assertLessEqual(len(page["text"].encode()), 17)

    def test_capture_cap_and_command_failures_preserve_real_status(self):
        with patch("sprig_agent.command_logs.MAX_LOG", 17):
            capture = LogCapture(())
            capture.write(("中🙂" * 20).encode())
            text, limited = capture.finish()
            self.assertTrue(limited)
            self.assertLessEqual(len(text.encode()), 17)
        tools = self.tools()
        for script, options, field in (("print('failed');raise SystemExit(7)", {}, "returncode"),
                                       ("import time;print('started',flush=True);time.sleep(2)", {"timeout": 0.15}, "timed_out"),
                                       ("print('x'*100000)", {}, "output_limited")):
            with self.subTest(field=field):
                result = tools.execute("run", {"argv": [sys.executable, "-c", script], **options})
                self.assertTrue(result["log"]["saved"])
                page = tools.read_log(result["log"]["log_id"])
                self.assertEqual(page[field], result[field])
                self.assertTrue(page["not_current_evidence"])

    def test_session_save_resume_new_owner_and_different_workspace_are_isolated(self):
        tools = self.tools()
        saved = self.save(tools.logs, "ONLY_THIS_CONVERSATION")
        agent = Agent(self.config, None, tools, lambda _: None)
        messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}]
        with closing(SessionStore(self.parent / "sessions", self.config)) as store:
            store.save("original", messages, **agent.reference_save_options())
            self.assertNotIn("ONLY_THIS_CONVERSATION", (store.directory / "original.json").read_text(encoding="utf-8"))
            self.assertEqual(store.load("original"), messages)
            restored = self.tools()
            self.assertIn("error", restored.execute("read_log", {"log_id": saved["log_id"]}))
            restored.logs.restore(store.loaded_logs)
            self.assertIn("ONLY_THIS_CONVERSATION", restored.read_log(saved["log_id"])["text"])
            restored.logs.restore()
            self.assertIn("error", restored.execute("read_log", {"log_id": saved["log_id"]}))
            other_root = self.parent / "other"
            other_root.mkdir()
            other = self.tools(replace(self.config, workdir=other_root))
            other.logs.restore(tools.logs.state())
            self.assertIn("error", other.execute("read_log", {"log_id": saved["log_id"]}))

    def test_expiry_eviction_global_quota_and_crash_pending_file_are_bounded(self):
        logs = self.tools().logs
        with patch("sprig_agent.command_logs.MAX_RECORDS", 2):
            first = self.save(logs, "first")
            second = self.save(logs, "second")
            third = self.save(logs, "third")
            with self.assertRaises(LogError):
                logs.read(first["log_id"])
            self.assertEqual(len(logs.list()["logs"]), 2)
        with patch("sprig_agent.command_logs.time.time", return_value=third["expires_at"] + 1):
            with self.assertRaises(LogError):
                logs.read(third["log_id"])
            self.save(logs, "fresh")
            self.assertEqual(len(logs.list()["logs"]), 1)
        with closing(SessionStore(self.config.command_log_dir, self.config)) as store:
            store.handle.write_new(TEMP, b"interrupted pending bytes", private=True)
        self.save(logs, "after crash")
        self.assertFalse((self.config.command_log_dir / TEMP).exists())
        with patch("sprig_agent.command_logs.MAX_STORE", 2000):
            # Keep the previous state small enough for the tightened test quota.
            for _ in range(5):
                self.save(logs, "x" * 700)
                self.assertLessEqual((self.config.command_log_dir / FILE).stat().st_size, 2000)
            self.assertLessEqual(len(logs.list()["logs"]), 2)

    def test_oversized_record_does_not_evict_existing_logs(self):
        logs = self.tools().logs
        before = self.save(logs, "retained")
        original = (self.config.command_log_dir / FILE).read_bytes()
        with patch("sprig_agent.command_logs.MAX_STORE", len(original) + 100):
            with self.assertRaises(LogError):
                self.save(logs, "x" * 1000)
        self.assertEqual((self.config.command_log_dir / FILE).read_bytes(), original)
        self.assertEqual(logs.read(before["log_id"])["text"], "retained")

    def test_concurrent_store_change_lock_and_save_failure_do_not_change_command_result(self):
        tools = self.tools()
        preview = tools.logs.preview()
        self.save(tools.logs, "newer")
        before = (self.config.command_log_dir / FILE).read_bytes()
        with self.assertRaises(LogError):
            tools.logs.save(preview, "stale", False, STATUS)
        self.assertEqual((self.config.command_log_dir / FILE).read_bytes(), before)
        with closing(SessionStore(self.config.command_log_dir, self.config)) as store:
            store.acquire(NAME)
            with self.assertRaises(LogError):
                self.save(tools.logs, "busy")
        with patch.object(tools.logs, "save", side_effect=OSError("fixture storage failure")):
            result = tools.execute("run", {"argv": [sys.executable, "-c", "print('ok')"]})
        self.assertEqual(result["returncode"], 0)
        self.assertFalse(result["log"]["saved"])
        self.assertNotIn("error", result)

    def test_clear_requires_fresh_approval_preserves_other_conversation_and_rechecks_snapshot(self):
        first, second = self.tools().logs, self.tools().logs
        owned = self.save(first, "owned")
        other = self.save(second, "other")
        self.assertFalse(first.clear(lambda _: False)["approved"])
        self.assertEqual(first.read(owned["log_id"])["text"], "owned")
        def change_during_approval(_):
            self.save(second, "concurrent")
            return True
        with self.assertRaises(LogError):
            first.clear(change_during_approval)
        self.assertEqual(first.clear(lambda _: True)["deleted"], 1)
        self.assertEqual(second.read(other["log_id"])["text"], "other")
        reader = CommandLogs(replace(self.config, read_only=True))
        reader.restore(second.state())
        self.assertFalse(reader.clear(lambda _: self.fail("read-only must not ask"))["approved"])

    def test_invalid_ids_state_tamper_closed_disabled_and_no_create_read(self):
        tools = self.tools()
        self.assertEqual(tools.logs.list()["logs"], [])
        self.assertFalse(self.config.command_log_dir.exists())
        for log_id, offset in (("../outside", 0), ("a" * 32, True), ("a" * 32, -1), (123, 0)):
            self.assertIn("error", tools.execute("read_log", {"log_id": log_id, "offset": offset}))
        self.assertFalse(self.config.command_log_dir.exists())
        saved = self.save(tools.logs)
        disabled = self.tools(replace(self.config, disabled_tools=("read_log",)))
        disabled.logs.restore(tools.logs.state())
        self.assertIn("error", disabled.execute("read_log", {"log_id": saved["log_id"]}))
        self.assertEqual(disabled.log_control("read", log_id=saved["log_id"])["text"], "log text")
        path = self.config.command_log_dir / FILE
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["records"][0]["text"] = "tampered"
        with closing(SessionStore(self.config.command_log_dir, self.config)) as store:
            store.handle.unlink(FILE)
            store.handle.write_new(FILE, json.dumps(raw).encode(), private=True)
        self.assertIn("error", tools.execute("read_log", {"log_id": saved["log_id"]}))
        tools.close()
        with self.assertRaises(LogError):
            tools.logs.list()

    def test_storage_inside_workspace_unrelated_files_and_private_permissions_are_rejected(self):
        tools = self.tools(replace(self.config, command_log_dir=self.root / "unsafe"))
        result = tools.execute("run", {"argv": [sys.executable, "-c", "print('ok')"]})
        self.assertEqual(result["returncode"], 0)
        self.assertFalse(result["log"]["saved"])
        self.assertFalse((self.root / "unsafe").exists())
        logs = self.tools().logs
        self.save(logs)
        with closing(SessionStore(self.config.command_log_dir, self.config)) as store:
            store.handle.write_new("unrelated", b"preserve me", private=True)
        with self.assertRaises(LogError):
            logs.preview()
        self.assertEqual((self.config.command_log_dir / "unrelated").read_bytes(), b"preserve me")

    def test_config_profile_cli_binding_and_session_owner_validation(self):
        settings = Settings(data={"providers": {"test": {"model": "fixture"}}, "provider": "test",
                                  "storage": {"command_log_dir": str(self.config.command_log_dir)}})
        config, meta = settings.resolve({"workdir": str(self.root)}, {})
        self.assertEqual(config.command_log_dir, self.config.command_log_dir)
        self.assertEqual(public_config(config, meta)["storage"]["command_log_dir"], str(self.config.command_log_dir))
        self.assertEqual(config.binding()["command_log_dir"], str(self.config.command_log_dir))
        override, _ = settings.resolve({"workdir": str(self.root), "command_log_dir": self.parent / "override"}, {})
        self.assertNotEqual(config.command_log_dir, override.command_log_dir)
        with closing(SessionStore(self.parent / "sessions", config)) as store:
            for state in ({"version": True, "owner": "a" * 32}, {"version": 1, "owner": "../bad"}, {"version": 1, "owner": "a" * 32, "path": "other"}):
                with self.assertRaises(SessionError):
                    store.save("bad", [], logs=state)
        with self.assertRaises(FileNotFoundError):
            SessionStore(self.parent / "not-created", self.config, create=False)
        self.assertFalse((self.parent / "not-created").exists())
        with self.assertRaises(ValueError):
            replace(self.config, api_key="fixture-key-12345", command_log_dir=self.parent / "fixture-key-12345")

    def test_log_text_never_revives_a_file_read_capability(self):
        (self.root / "value.txt").write_text("old\n", encoding="utf-8")
        approvals = []
        tools = self.tools(approve=lambda text: approvals.append(text) or True)
        read = tools.execute("read_file", {"path": "value.txt", "offset": 1, "limit": 1})
        result = tools.execute("run", {"argv": [sys.executable, "-c", "print('" + read["read_id"] + "')"]})
        self.assertIn(read["read_id"], tools.read_log(result["log"]["log_id"])["text"])
        edit = tools.execute("edit_lines", {"path": "value.txt", "read_id": read["read_id"],
                                           "edits": [{"start_line": 1, "end_line": 1, "new": "unsafe\n"}]})
        self.assertIn("error", edit)
        self.assertEqual(len(approvals), 1)
        self.assertEqual((self.root / "value.txt").read_text(), "old\n")

    def test_native_private_files_and_unsafe_acl_or_mode_are_rejected(self):
        logs = self.tools().logs
        self.save(logs)
        path = self.config.command_log_dir / FILE
        with Directory(self.config.command_log_dir) as directory:
            directory.check_private()
            directory.check_private(FILE)
        before = path.read_bytes()
        if os.name == "nt":
            subprocess.run(["icacls.exe", str(path), "/grant", "*S-1-1-0:(R)"], check=True, capture_output=True)
        else:
            path.chmod(0o644)
        with self.assertRaises((OSError, ValueError)):
            logs.preview()
        self.assertEqual(path.read_bytes(), before)

    def test_hardlinked_payload_and_oversized_pending_cannot_be_used_or_removed(self):
        logs = self.tools().logs
        self.save(logs)
        path = self.config.command_log_dir / FILE
        outside = self.parent / "linked-payload"
        os.link(path, outside)
        before = outside.read_bytes()
        with self.assertRaises(LogError):
            logs.preview()
        self.assertEqual(outside.read_bytes(), before)
        outside.unlink()
        with closing(SessionStore(self.config.command_log_dir, self.config)) as store:
            store.handle.write_new(TEMP, b"x" * 1000, private=True)
        with patch("sprig_agent.command_logs.MAX_STORE", 800), self.assertRaises(LogError):
            logs.preview()
        self.assertEqual((self.config.command_log_dir / TEMP).stat().st_size, 1000)

    def test_crash_after_command_preserves_owner_for_readback_without_replay(self):
        tools = self.tools()
        agent = Agent(self.config, None, tools, lambda _: None)
        messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"},
                    fixture.call("pending-run", "run", {"argv": ["never-replay"]})]
        with closing(SessionStore(self.parent / "sessions", self.config)) as store:
            store.save("interrupted", messages, **agent.reference_save_options())
            saved = self.save(tools.logs, "completed command before crash", returncode=7)
            loaded = store.load("interrupted")
            self.assertIn("NOT replayed", loaded[-1]["content"])
            restored = self.tools()
            restored.logs.restore(store.loaded_logs)
            page = restored.read_log(saved["log_id"])
            self.assertEqual(page["returncode"], 7)
            self.assertIn("before crash", page["text"])

    def test_cli_local_controls_read_and_clear_without_model_or_history_mutation(self):
        tools = self.tools()
        saved = self.save(tools.logs, "CLI_LOG_DETAIL")
        provider = fixture.ScriptedProvider([])
        agent = Agent(self.config, provider, tools, lambda _: None)
        messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}]
        before = json.dumps(messages)
        output = []
        with patch("builtins.input", side_effect=["/logs", "/log " + saved["log_id"], "/logs clear", "/exit"]):
            cli.chat(agent, None, messages, output.append, autostart=False)
        self.assertIn("CLI_LOG_DETAIL", "\n".join(output))
        self.assertEqual(provider.calls, [])
        self.assertEqual(json.dumps(messages), before)
        self.assertEqual(tools.logs.list()["logs"], [])

    def test_cancel_during_command_does_not_create_partial_artifact_or_replay(self):
        cancelled = threading.Event()
        tools = self.tools(cancel_event=cancelled)
        script = "from pathlib import Path;import time;print('partial',flush=True);Path('started').write_text('yes');time.sleep(20)"
        outcome = []
        def run():
            try:
                tools.run([sys.executable, "-c", script])
            except BaseException as error:
                outcome.append(error)
        worker = threading.Thread(target=run)
        worker.start()
        try:
            import time
            end = time.monotonic() + 5
            while not (self.root / "started").exists() and time.monotonic() < end:
                time.sleep(0.01)
            self.assertTrue((self.root / "started").exists())
            cancelled.set()
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertIsInstance(outcome[0], KeyboardInterrupt)
            self.assertFalse(self.config.command_log_dir.exists())
        finally:
            cancelled.set()
            worker.join(3)

    def test_request_compaction_elides_only_successful_old_log_text(self):
        config = replace(self.config, max_context_bytes=4096, compact_context=True)
        for code in (0, 7):
            call = {"id": "log-read", "type": "function", "function": {"name": "read_log", "arguments": '{"log_id":"' + "a" * 32 + '"}'}}
            original = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"},
                        {"role": "assistant", "content": None, "tool_calls": [call]},
                        {"role": "tool", "tool_call_id": "log-read", "content": json.dumps({"text": "ORIGINAL" * 1000, **STATUS, "returncode": code})},
                        *[{"role": "assistant", "content": "recent"} for _ in range(6)]]
            projected, report = prepare_context(config, original, [], compact=True, force=True)
            result = json.loads(projected[3]["content"])
            self.assertEqual(result["returncode"], code)
            self.assertEqual("text" in result, code != 0)
            self.assertIn("ORIGINAL", original[3]["content"])

    def test_actual_protocol_loop_recovers_middle_without_more_command_approvals(self):
        script = "import sys;sys.stdout.write('A'*2048+'MIDDLE_FAILURE_MARKER'+'B'*2048)"
        cases = ((protocol, persistent, streamed) for protocol in ("chat_completions", "messages", "responses")
                 for persistent, streamed in ((True, False), (False, False), (False, True)))
        for protocol, persistent, streamed in cases:
            with self.subTest(protocol=protocol, persistent=persistent, stream=streamed):
                captured, approvals = [], []

                def start_request(data):
                    self.assertEqual(any(row.get("name") == "read_log" for row in values(data)), persistent)
                    return answer(protocol, streamed, calls=[("run", "run", {"argv": [sys.executable, "-c", script]})])

                def read_request(data):
                    result = next(row for row in values(data) if isinstance(row.get("log"), dict) and row["log"].get("log_id"))
                    self.assertTrue(any(row.get("name") == "read_log" for row in values(data)))
                    self.assertEqual(result["log"]["saved"], persistent)
                    self.assertNotIn("MIDDLE_FAILURE_MARKER", result["output"])
                    return answer(protocol, streamed, calls=[("read-log", "read_log", {
                        "log_id": result["log"]["log_id"], "query": "MIDDLE_FAILURE_MARKER"})])

                def finish_request(data):
                    page = next(row for row in values(data) if row.get("historical") is True and "text" in row)
                    self.assertTrue(page["text"].startswith("MIDDLE_FAILURE_MARKER"))
                    self.assertEqual(page["match_offset"], 2048)
                    return answer(protocol, streamed, text="examined")

                with backend(protocol, [start_request, read_request, finish_request], captured, stream=streamed) as url:
                    config = replace(self.config, base_url=url, wire_api=protocol, proxy="direct", max_steps=3,
                                     command_log_dir=self.parent / ("logs-" + protocol) if persistent else None,
                                     max_context_bytes=16000, stream=streamed)
                    tools = self.tools(config, lambda message: approvals.append(message) or True)
                    messages, done = Agent(config, Provider(config), tools, lambda _: None).run("inspect the command failure detail")
                    self.assertTrue(done)
                    self.assertEqual(len(captured), 3)
                    self.assertEqual(len(approvals), 1)
                    self.assertEqual(messages[-1]["content"], "examined")


class CommandLogTuiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.TuiTests.asyncSetUp
    setUp = fixture.TuiTests.setUp
    app = fixture.TuiTests.app
    until = fixture.TuiTests.until
    click = fixture.TuiTests.click

    async def test_actual_tui_local_read_fresh_clear_approval_and_restored_ownership(self):
        self.config = replace(self.config, command_log_dir=self.parent / "logs", command_output_limit=65536, max_output=512)
        provider = fixture.ScriptedProvider([fixture.call("run", "run", {"argv": [sys.executable, "-c", "print('LOCAL_LOG_DETAIL'*100)"]}), fixture.response("done")])
        app = self.app(provider, task="run the command", save_session="logs")
        async with app.run_test(size=(110, 36)) as pilot:
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy)
            log_id = app.tools.logs.list()["logs"][0]["log_id"]
            before = json.dumps(app.messages)
            editor = app.query_one("#task", TaskInput)
            editor.load_text("UNSENT_DRAFT")
            app.slash_command("/log " + log_id)
            await self.until(pilot, lambda: not app.busy)
            self.assertIn("LOCAL_LOG_DETAIL", app.transcript)
            self.assertEqual(json.dumps(app.messages), before)
            self.assertEqual(editor.text, "UNSENT_DRAFT")
            self.assertEqual(len(provider.calls), 2)
            app.new_conversation()
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: app.messages is None)
            self.assertEqual(app.tools.logs.list()["logs"], [])
            self.assertIn("error", app.tools.execute("read_log", {"log_id": log_id}))
        app.close()
        restored_provider = fixture.ScriptedProvider([])
        restored = self.app(restored_provider, resume="logs")
        async with restored.run_test(size=(110, 36)) as pilot:
            self.assertEqual(restored.tools.read_log(log_id)["offset"], 0)
            restored.slash_command("/logs clear")
            await self.until(pilot, lambda: isinstance(restored.screen, ApprovalScreen))
            await self.click(pilot, "#reject")
            await self.until(pilot, lambda: not restored.busy)
            self.assertEqual(len(restored.tools.logs.list()["logs"]), 1)
            restored.slash_command("/logs clear")
            await self.until(pilot, lambda: isinstance(restored.screen, ApprovalScreen))
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not restored.busy)
            self.assertEqual(restored.tools.logs.list()["logs"], [])
            self.assertEqual(restored_provider.calls, [])


if __name__ == "__main__":
    unittest.main()
