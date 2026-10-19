"""Bounded verbose commands retain final diagnostics without weakening kill limits."""

from contextlib import closing
import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent.agent import Agent
from sprig_agent.config import Config, terminal_text
from sprig_agent.output import SampledCommandOutput
from sprig_agent.provider import Provider
from sprig_agent.session import SessionError, SessionStore, validate_history
from sprig_agent.settings import load_settings, public_config
from sprig_agent.tools import Tools
from tests.test_edit_checks import DONE, edit, response
from tests.test_planning import replies
from tests import test_settings as settings_fixtures


class SampledOutputTests(unittest.TestCase):
    def sample(self, raw, *, limit=256, secrets=(), width=31):
        emitted = []
        capture = SampledCommandOutput(emitted.append, secrets, limit)
        for offset in range(0, len(raw), width):
            capture.write(raw[offset:offset + width])
        result = capture.finish()
        self.assertEqual("".join(emitted), terminal_text(result["output"]))
        self.assertLessEqual(len(result["output"].encode("utf-8")), limit)
        self.assertEqual(result["output_bytes"], len(raw))
        return result

    def test_small_empty_and_exact_limit_outputs_are_complete_without_notice(self):
        for raw in (b"", b"hello\r\n", b"x" * 256, "中文🙂\n".encode() * 20):
            with self.subTest(length=len(raw)):
                result = self.sample(raw, width=1)
                self.assertEqual(result["output"], raw.decode())
                self.assertFalse(result["output_truncated"])
                self.assertEqual(result["output_omitted_bytes"], 0)

    def test_long_output_keeps_first_and_final_failure_and_reports_omission(self):
        raw = b"BEGIN\n" + b"progress\n" * 10000 + b"FINAL_FAILURE\n"
        result = self.sample(raw)
        self.assertTrue(result["output_truncated"])
        self.assertTrue(result["output"].startswith("BEGIN\n"))
        self.assertTrue(result["output"].endswith("FINAL_FAILURE\n"))
        self.assertIn("output omitted", result["output"])
        kept = len(result["output"].encode()) - len(SampledCommandOutput.marker.encode())
        self.assertEqual(result["output_omitted_bytes"], len(raw) - kept)

    def test_unicode_boundaries_are_valid_and_independent_of_pipe_fragmentation(self):
        raw = ("开始🙂\r\n" + "中🙂\n" * 1000 + "最终失败🙂\n").encode()
        results = [self.sample(raw, limit=limit, width=width) for limit in (256, 257, 1024) for width in (1, 3, 65536)]
        for index in range(0, len(results), 3):
            self.assertEqual(results[index], results[index + 1])
            self.assertEqual(results[index], results[index + 2])
            self.assertNotIn("\ufffd", results[index]["output"])
            self.assertTrue(results[index]["output"].endswith("最终失败🙂\n"))

    def test_redaction_precedes_selection_even_when_long_key_crosses_both_windows(self):
        key = "fixture-private-" + "credential" * 200
        raw = ("start:" + key + ":end").encode()
        result = self.sample(raw, secrets=(key,), width=7)
        self.assertEqual(result["output"], "start:[REDACTED]:end")
        self.assertFalse(result["output_truncated"])

    def test_secrets_in_tail_split_chunks_and_trailing_prefix_are_masked(self):
        key = "fixture-private-command-key"
        raw = ("start\n" + "progress\n" * 1000 + key + "\n" + key[:18]).encode()
        result = self.sample(raw, secrets=(key, "progress"), width=1)
        self.assertNotIn(key, result["output"])
        self.assertNotIn(key[:18], result["output"])
        self.assertTrue(result["output"].endswith("[REDACTED]\n[REDACTED]"))

    def test_invalid_utf8_and_terminal_controls_do_not_break_display_budget(self):
        raw = b"header\x1b[31m\n" + b"\xff\x80" * 1000 + "结尾\u202e\n".encode()
        result = self.sample(raw, width=3)
        self.assertIn("\u202e", result["output"])
        self.assertIn("\x1b", result["output"])

    def test_prefix_is_visible_before_command_finishes(self):
        shown = []
        capture = SampledCommandOutput(shown.append, (), 256)
        capture.write(b"early progress\n")
        self.assertEqual("".join(shown), "early progress\n")
        capture.write(b"x" * 10000 + b"late diagnostic")
        capture.finish()
        self.assertTrue("".join(shown).endswith("late diagnostic"))

    def test_generated_notice_is_redacted_when_it_coincides_with_a_known_secret(self):
        secret = "showing final output"
        result = self.sample(b"x" * 1000, secrets=(secret,))
        self.assertNotIn(secret, result["output"])
        self.assertIn("[REDACTED]", result["output"])


class CommandCaptureTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.parent = Path(temp.name)
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.config = Config(self.root, "offline", max_output=512, command_output_limit=262144, timeout=5)
        self.prompts, self.output = [], []

    def tools(self, config=None, approve=None, **kwargs):
        tools = Tools(config or self.config, approve or (lambda prompt: self.prompts.append(prompt) or True),
                      emit_output=self.output.append, **kwargs)
        self.addCleanup(tools.close)
        return tools

    def run_command(self, script, config=None, **kwargs):
        return self.tools(config).execute("run", {"argv": [sys.executable, "-I", "-B", "-c", script], **kwargs})

    def test_verbose_failed_and_successful_commands_finish_and_keep_final_diagnostic(self):
        for exit_code in (0, 1):
            self.output.clear()
            result = self.run_command("import sys; print('BEGIN'); print('x'*50000); print('FINAL_DIAGNOSTIC'); sys.exit(" + str(exit_code) + ")")
            self.assertEqual(result["returncode"], exit_code)
            self.assertFalse(result["output_limited"])
            self.assertFalse(result["timed_out"])
            self.assertTrue(result["output_truncated"])
            self.assertTrue(result["output"].endswith("FINAL_DIAGNOSTIC\n" if os.name != "nt" else "FINAL_DIAGNOSTIC\r\n"))
            self.assertLessEqual(len(result["output"].encode()), 512)
            self.assertEqual("".join(self.output), terminal_text(result["output"]))
            self.assertIn("output cap: 262144 bytes", self.prompts[-1])
            self.assertIn("displayed output cap: 512 bytes", self.prompts[-1])

    def test_default_still_kills_at_max_output_and_has_no_new_result_fields(self):
        result = self.run_command("print('x'*50000)", replace(self.config, command_output_limit=None))
        self.assertTrue(result["output_limited"])
        self.assertEqual(len(result["output"].encode()), 512)
        self.assertNotIn("output_truncated", result)
        self.assertNotIn("displayed output cap", self.prompts[0])

    def test_explicit_total_limit_still_kills_flood_and_can_be_lower_than_display_limit(self):
        for cap in (256, 4096):
            self.output.clear()
            started = time.monotonic()
            result = self.run_command("import sys,time; sys.stdout.write('x'*1000000); sys.stdout.flush(); time.sleep(30)",
                                      replace(self.config, command_output_limit=cap))
            self.assertTrue(result["output_limited"])
            self.assertLess(time.monotonic() - started, 4)
            self.assertLessEqual(len(result["output"].encode()), min(cap, 512))
            if cap > 512:
                self.assertEqual(result["output_bytes"], cap)
                self.assertTrue(result["output_truncated"])

    def test_exact_raw_limit_completes_but_one_more_byte_marks_execution_limited(self):
        for count in (4096, 4097):
            result = self.run_command("import sys; sys.stdout.buffer.write(b'x'*" + str(count) + ")",
                                      replace(self.config, command_output_limit=4096))
            self.assertEqual(result["output_bytes"], 4096)
            self.assertIs(result["output_limited"], count > 4096)
            self.assertTrue(result["output_truncated"])
            if count == 4096:
                self.assertEqual(result["returncode"], 0)

    def test_real_utf8_pipe_keeps_valid_unicode_and_last_diagnostic(self):
        raw = ("首🙂\n" + "中🙂\n" * 2000 + "尾部诊断🙂\n").encode()
        (self.root / "utf8-output.txt").write_bytes(raw)
        result = self.run_command("import sys; from pathlib import Path; sys.stdout.buffer.write(Path('utf8-output.txt').read_bytes())")
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["output_bytes"], len(raw))
        self.assertTrue(result["output"].endswith("尾部诊断🙂\n"))
        self.assertNotIn("\ufffd", result["output"])

    def test_timeout_and_descendant_cleanup_still_apply_with_sampling(self):
        child = "import time; from pathlib import Path; time.sleep(0.8); Path('unexpected-child-write').write_text('bad')"
        script = ("import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); "
                  "print('x'*10000,flush=True); time.sleep(30)")
        result = self.run_command(script, timeout=0.2)
        self.assertTrue(result["timed_out"])
        self.assertFalse(result["output_limited"])
        self.assertTrue(result["output_truncated"])
        time.sleep(0.9)
        self.assertFalse((self.root / "unexpected-child-write").exists())

    def test_readonly_disabled_tool_and_refusal_do_not_start_command(self):
        script = "from pathlib import Path; Path('unexpected-write').write_text('bad')"
        for config in (replace(self.config, read_only=True), replace(self.config, disabled_tools=("run",))):
            result = self.tools(config).execute("run", {"argv": [sys.executable, "-c", script]})
            self.assertIn("error", result)
        self.assertFalse(self.prompts)
        result = self.tools(approve=lambda prompt: False).execute("run", {"argv": [sys.executable, "-c", script]})
        self.assertIs(result["approved"], False)
        self.assertFalse((self.root / "unexpected-write").exists())

    def test_cancel_after_early_output_kills_process_before_delayed_write(self):
        event = threading.Event()
        def emit(text):
            self.output.append(text)
            event.set()
        tools = self.tools(cancel_event=event)
        tools.emit_output = emit
        script = "import time; from pathlib import Path; print('EARLY',flush=True); time.sleep(0.8); Path('unexpected-write').write_text('bad')"
        with self.assertRaises(KeyboardInterrupt):
            tools.execute("run", {"argv": [sys.executable, "-I", "-B", "-c", script]})
        time.sleep(0.9)
        self.assertFalse((self.root / "unexpected-write").exists())

    def test_known_file_credentials_never_leak_in_sample_stream_or_result(self):
        key = "fixture-private-command-credential"
        (self.root / "input.txt").write_text("x" * 1000 + key + "y" * 1000 + key, encoding="utf-8")
        config = replace(self.config, api_key=key)
        result = self.run_command("from pathlib import Path; print(Path('input.txt').read_text())", config)
        self.assertNotIn(key, json.dumps(result) + "".join(self.output))
        self.assertIn("[REDACTED]", result["output"])
        self.assertEqual("".join(self.output), terminal_text(result["output"]))

    def test_loopback_verbose_baseline_repair_stage_and_independent_final_checks(self):
        (self.root / "target.txt").write_bytes(b"broken\n")
        checker = self.parent / "fixed-check.py"
        checker.write_text("from pathlib import Path\nprint('progress'*10000)\n"
                           "assert Path('target.txt').read_text()=='fixed\\n', 'ACTUAL_FAILURE_AT_END'\nprint('ALL_PASS')\n", encoding="utf-8")
        captured, saved = [], []
        def repair(payload):
            checks = [json.loads(item["content"]) for item in payload["messages"] if item["role"] == "tool"]
            self.assertEqual(len(checks), 1)
            self.assertEqual(checks[0]["returncode"], 1)
            self.assertFalse(checks[0]["output_limited"])
            self.assertTrue(checks[0]["output_truncated"])
            self.assertIn("ACTUAL_FAILURE_AT_END", checks[0]["output"])
            return response(edit("repair", "broken", "fixed", "target.txt"))
        with replies([repair, DONE], captured) as url:
            config = replace(self.config, base_url=url, proxy="direct", max_steps=2, max_output=1024,
                             max_context_bytes=16384, check_before_task=True, check_after_edit=True,
                             checks=((sys.executable, "-I", "-B", str(checker)),))
            tools = self.tools(config)
            history, complete = Agent(config, Provider(config), tools, lambda message: None,
                                      lambda value: saved.append(copy.deepcopy(value))).run("Repair the behavior")
        self.assertTrue(complete)
        self.assertEqual(validate_history(history), history)
        self.assertEqual(len(captured), 2)
        self.assertTrue(all(size <= 16384 for _, size in captured))
        self.assertEqual(len(self.prompts), 4)  # Baseline, edit, stage, independent final.
        results = [json.loads(item["content"]) for item in history if item["role"] == "tool"]
        checks = [item for item in results if "returncode" in item]
        self.assertEqual([item["returncode"] for item in checks], [1, 0, 0])
        self.assertTrue(all(item["output_truncated"] and not item["output_limited"] for item in checks))
        self.assertTrue(history[-1]["tool_call_id"].startswith("sprig-check-"))


class CommandOutputConfigurationTests(unittest.TestCase):
    setUp = settings_fixtures.SettingsTests.setUp
    write = settings_fixtures.SettingsTests.write
    resolve = settings_fixtures.SettingsTests.resolve
    invoke = settings_fixtures.SettingsTests.invoke

    def test_user_profile_and_cli_precedence_and_public_sources(self):
        self.write('provider="x"\n[providers.x]\nmodel="fixture"\n[execution]\nmax_output=512\noutput_limit=4096\n'
                   '[profiles.verbose.execution]\noutput_limit=8192\n')
        settings = load_settings(self.path)
        config, details = self.resolve(settings)
        self.assertEqual(config.command_output_limit, 4096)
        self.assertEqual(dict(config.setting_sources)["command_output_limit"], "user")
        config, details = self.resolve(settings, profile="verbose")
        self.assertEqual(config.command_output_limit, 8192)
        self.assertEqual(dict(config.setting_sources)["command_output_limit"], "profile")
        config, details = self.resolve(settings, profile="verbose", command_output_limit=16384)
        self.assertEqual(config.command_output_limit, 16384)
        self.assertEqual(public_config(config, details)["execution"]["output_limit"], 16384)
        self.assertEqual(dict(config.setting_sources)["command_output_limit"], "CLI")
        status, output, _ = self.invoke(["--config", str(self.path), "--command-output-limit", "32768", "--show-config"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output)["execution"]["output_limit"], 32768)

    def test_default_matches_existing_output_cap_and_schema_tracks_runtime(self):
        self.write('[agent]\nmax_output=1024\n')
        config, details = self.resolve(load_settings(self.path), model="fixture")
        self.assertIsNone(config.command_output_limit)
        self.assertEqual(public_config(config, details)["execution"]["output_limit"], 1024)
        from sprig_agent.configuration_schema import configuration_schema
        schema = configuration_schema()
        self.assertEqual(schema["properties"]["execution"]["properties"]["output_limit"]["maximum"], 8388608)
        self.assertEqual(json.loads((Path(__file__).resolve().parents[1] / "config.schema.json").read_text(encoding="utf-8")), schema)

    def test_invalid_limits_fail_even_when_cli_would_override(self):
        for value in (True, False, 255, 8388609, 1.5, "4096"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    Config(self.root, "fixture", command_output_limit=value)
        self.write('[execution]\noutput_limit=255\n')
        with self.assertRaises(ValueError):
            self.resolve(load_settings(self.path), model="fixture", command_output_limit=4096)

    def test_project_cannot_expand_execution_budget(self):
        project = self.write('[execution]\noutput_limit=8388608\n', self.root / "project.toml")
        with self.assertRaisesRegex(ValueError, "execution environment"):
            load_settings(project, required=True, project=True)

    def test_session_binding_rejects_changed_explicit_command_limit(self):
        config = Config(self.root, "fixture", command_output_limit=4096)
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}]
        directory = self.parent / "sessions"
        with closing(SessionStore(directory, config)) as store:
            store.save("session", history)
            self.assertEqual(store.load("session"), history)
        with closing(SessionStore(directory, replace(config, command_output_limit=8192))) as store:
            with self.assertRaises(SessionError):
                store.load("session")
        with closing(SessionStore(directory, replace(config, command_output_limit=None))) as store:
            with self.assertRaises(SessionError):
                store.load("session")


if __name__ == "__main__":
    unittest.main()
