"""Reusable tasks use real configuration and protocol paths without external APIs."""
from contextlib import closing, redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import jsonschema
from sprig_agent import cli
from sprig_agent.agent import Agent
from sprig_agent.config import Config, validate_model_change
from sprig_agent.configuration_schema import configuration_schema
from sprig_agent.filesystem import Directory
from sprig_agent.local_commands import COMMANDS
from sprig_agent.prompt_templates import PromptTemplate, expand_prompt, parse_templates, prompt_catalog
from sprig_agent.session import SessionStore
from sprig_agent.settings import load_settings, public_config
from sprig_agent.tools import Tools
from tests.test_generation_options import envelope
from tests.test_mcp_stdio import wire
from tests.test_native_protocols import backend
from tests import test_tui as ui


class PromptTemplateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.config = Config(self.root, "fixture")
        home = patch("pathlib.Path.home", return_value=self.parent)
        home.start()
        self.addCleanup(home.stop)

    def with_prompt(self, template, **options):
        return replace(self.config, prompt_templates=(PromptTemplate("review", template),), **options)

    def test_full_arguments_unicode_multiline_and_whitespace_are_preserved(self):
        config = self.with_prompt("Start\n$ARGUMENTS\nEnd")
        arguments = '  中文 C:\\目录\\file.py\r\n\tkeep trailing  '
        self.assertEqual(expand_prompt(config, "/review " + arguments), "Start\n" + arguments + "\nEnd")
        self.assertEqual(expand_prompt(config, "/review"), "Start\n\nEnd")

    def test_numbered_quoted_arguments_keep_native_windows_backslashes(self):
        config = self.with_prompt("$2 then $1; $$1; $$ARGUMENTS")
        self.assertEqual(expand_prompt(config, '/review "C:\\目录 space\\file.py" second'),
                         "second then C:\\目录 space\\file.py; $1; $ARGUMENTS")
        for arguments in ("one", "one two three", '"unterminated'):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                expand_prompt(config, "/review " + arguments)
        self.assertEqual(expand_prompt(self.with_prompt("$1\n$ARGUMENTS"), "/review first second"), "first\nfirst second")

    def test_expansion_is_single_pass_plain_text_without_shell_file_environment_or_dispatch(self):
        config = self.with_prompt('/exit\n!`touch forbidden` @private.env ${API_KEY} $$HOME $ARGUMENTS')
        with patch("subprocess.run", side_effect=AssertionError("no process")), patch.object(Path, "read_text", side_effect=AssertionError("no file")):
            body = expand_prompt(config, "/review $1 $ARGUMENTS /undo")
        self.assertEqual(body, '/exit\n!`touch forbidden` @private.env ${API_KEY} $HOME $1 $ARGUMENTS /undo')
        self.assertFalse((self.root / "forbidden").exists())
        self.assertEqual(expand_prompt(self.with_prompt("Fixed"), "/review suffix  "), "Fixed\n\nsuffix  ")
        self.assertIsNone(expand_prompt(config, "//review anything"))
        self.assertIsNone(expand_prompt(config, "/help"))

    def test_strict_names_fields_controls_placeholders_and_library_budgets(self):
        for name in (*COMMANDS, "Review", "../bad", "a" * 33, "中文", ""):
            with self.subTest(name=name), self.assertRaises(ValueError):
                PromptTemplate(name, "body")
        for template in ("", "  ", "\x1b[31m", "\u202esecret", "$2", "$10", "中" * 5462):
            with self.subTest(template_size=len(template)), self.assertRaises(ValueError):
                PromptTemplate("review", template)
        for table in ([], {"review": {"template": "safe", "model": "other"}}, {"review": {"description": "missing"}}):
            with self.assertRaises(ValueError):
                parse_templates(table)
        for templates in ((PromptTemplate("review", "a"),) * 2,
                          tuple(PromptTemplate("a" + str(i), "a") for i in range(33)),
                          tuple(PromptTemplate("a" + str(i), "x" * 16384) for i in range(4))):
            with self.assertRaises(ValueError):
                replace(self.config, prompt_templates=templates)

    def test_expanded_bytes_obey_original_task_budget_without_truncating(self):
        config = self.with_prompt("中" * 1000 + "$ARGUMENTS", max_context_bytes=4096)
        with self.assertRaises(ValueError):
            expand_prompt(config, "/review " + "文" * 400)
        self.assertEqual(expand_prompt(config, "/review " + "a" * 1000), "中" * 1000 + "a" * 1000)

    def test_repeated_large_arguments_refuse_before_materializing_an_oversized_body(self):
        import tracemalloc
        config = self.with_prompt("$ARGUMENTS" * 32, max_context_bytes=1048576)
        for size in (400000, 16 * config.max_context_bytes):
            with self.subTest(input_bytes=size):
                invocation = "/review " + "x" * size
                tracemalloc.start()
                try:
                    with self.assertRaises(ValueError):
                        expand_prompt(config, invocation)
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                self.assertLess(peak, 8 * config.max_context_bytes)

    def test_catalog_is_bounded_redacted_and_never_contains_template_bodies(self):
        secret = "fixture-hidden-credential"
        config = replace(self.config, api_key=secret, max_output=256,
                         prompt_templates=tuple(PromptTemplate("task" + str(i), "PRIVATE_BODY", "中" * 50 + secret[:8]) for i in range(4)))
        seen, offset = [], 0
        while True:
            result = prompt_catalog(config, offset)
            encoded = json.dumps(result, ensure_ascii=False).encode()
            self.assertLessEqual(len(encoded), 256)
            self.assertNotIn("PRIVATE_BODY", encoded.decode())
            self.assertNotIn(secret[:8], encoded.decode())
            seen.extend(row["command"] for row in result["prompts"])
            if result["next_offset"] is None:
                break
            self.assertGreater(result["next_offset"], offset)
            offset = result["next_offset"]
        self.assertEqual(seen, ["/task" + str(i) for i in range(4)])
        for offset in (-1, True, 5):
            with self.assertRaises(ValueError):
                prompt_catalog(config, offset)

    def test_private_user_profile_resolution_sources_and_project_refusal(self):
        path = self.parent / "config.toml"
        body = '''[prompts.review]
template = "USER_BODY $ARGUMENTS"
description = "user"
[prompts.explain]
template = "EXPLAIN"
[profiles.work.prompts.review]
template = "PROFILE_BODY $ARGUMENTS"
'''
        with Directory(self.parent) as directory:
            directory.write_new(path.name, body.encode(), private=True)
        settings = load_settings(path)
        config, details = settings.resolve({"workdir": str(self.root), "model": "fixture", "profile": "work"}, {})
        self.assertEqual(expand_prompt(config, "/review target"), "PROFILE_BODY target")
        self.assertEqual(expand_prompt(config, "/explain"), "EXPLAIN")
        self.assertEqual(dict(config.setting_sources)["prompts.review"], "profile")
        self.assertEqual(dict(config.setting_sources)["prompts.explain"], "user")
        self.assertNotIn("PROFILE_BODY", json.dumps(public_config(config, details)))
        project = self.root / "project.toml"
        with Directory(self.root) as directory:
            directory.write_new(project.name, b'[prompts.evil]\ntemplate = "redirect"\n', private=True)
        with self.assertRaisesRegex(ValueError, "project configuration"):
            settings.with_project(project)

    def test_schema_matches_generated_definition_and_rejects_builtin_shadowing(self):
        schema = configuration_schema()
        self.assertEqual(json.loads((Path(__file__).resolve().parents[1] / "config.schema.json").read_text("utf-8")), schema)
        jsonschema.validate({"prompts": {"review": {"template": "Review $ARGUMENTS"}}}, schema)
        for body in ({"help": {"template": "wrong"}}, {"review": {"template": "safe", "command": ["bad"]}}):
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate({"prompts": body}, schema)

    def test_library_never_enters_requests_or_session_binding_until_invoked(self):
        config = replace(self.config, prompt_templates=(PromptTemplate("review", "USED $ARGUMENTS"), PromptTemplate("unused", "UNUSED_PRIVATE")))
        self.assertEqual(config.binding(), self.config.binding())
        provider = ui.ScriptedProvider([ui.response()])
        with closing(Tools(config, lambda _: self.fail("no action approval needed"))) as tools:
            agent = Agent(config, provider, tools, lambda _: None)
            history, complete = agent.run(expand_prompt(config, "/review target"))
        self.assertTrue(complete)
        self.assertNotIn("UNUSED_PRIVATE", json.dumps(provider.calls))
        with closing(SessionStore(self.parent / "sessions", config)) as store:
            store.save("prompt", history)
            self.assertEqual(store.load("prompt"), history)
        selected = replace(config, prompt_templates=(PromptTemplate("review", "changed"),))
        object.__setattr__(selected, "_runtime_credentials", config._runtime_credentials)
        with self.assertRaises(ValueError):
            validate_model_change(config, selected)

    def test_six_real_cli_protocol_paths_expand_before_requests_and_keep_approval_boundary(self):
        path = self.parent / "provider.toml"
        with Directory(self.parent) as directory:
            directory.write_new(path.name, b'[prompts.review]\ntemplate = "TASK_START $ARGUMENTS TASK_END"\n[prompts.unused]\ntemplate = "UNUSED_PRIVATE"\n', private=True)
        for protocol in ("chat_completions", "messages", "responses"):
            for stream in (False, True):
                with self.subTest(protocol=protocol, stream=stream):
                    captured, approvals = [], []
                    message = ui.call("write", "edit_file", {"path": "answer.txt", "old": "old", "new": "new"})
                    reply = envelope(message) if protocol == "chat_completions" and not stream else wire(protocol, message)
                    (self.root / "answer.txt").write_text("old", encoding="utf-8")
                    with backend(protocol, [reply], captured, stream=stream and protocol != "chat_completions") as url:
                        out = io.StringIO()
                        with patch.dict(os.environ, {}, clear=True), patch.object(cli, "approve", side_effect=lambda value: approvals.append(value) or False), redirect_stdout(out), redirect_stderr(out):
                            status = cli.main(["--config", str(path), "--workdir", str(self.root), "--model", "fixture", "--base-url", url,
                                               "--wire-api", protocol, "--proxy", "direct", "--max-steps", "1", "--stream" if stream else "--no-stream", "/review Unicode中文"])
                    self.assertEqual(status, 2)
                    self.assertEqual(len(captured), 1)
                    payload = json.dumps(captured[0]["body"], ensure_ascii=False)
                    self.assertIn("TASK_START Unicode中文 TASK_END", payload)
                    self.assertNotIn("UNUSED_PRIVATE", payload)
                    self.assertEqual(len(approvals), 1)
                    self.assertEqual((self.root / "answer.txt").read_text(), "old")

    def test_chat_dispatch_catalog_arguments_and_expansion_prefix_never_reinvoke_local_commands(self):
        config = self.with_prompt("/exit\n$ARGUMENTS")
        provider = ui.ScriptedProvider([ui.response()])
        output = []
        with closing(Tools(config, lambda _: self.fail("no action"))) as tools:
            agent = Agent(config, provider, tools, lambda _: None)
            with patch("builtins.input", side_effect=["/prompts", "/review  keep trailing  ", "/exit"]):
                self.assertEqual(cli.chat(agent, None, None, output.append, autostart=False), 0)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(provider.calls[0][-1]["content"], "/exit\n keep trailing  ")
        self.assertIn('"/review"', "\n".join(output))

    def test_offline_cli_catalog_needs_no_model_credentials_and_keeps_bodies_private(self):
        path = self.parent / "config.toml"
        with Directory(self.parent) as directory:
            directory.write_new(path.name, b'[prompts.review]\ntemplate = "PRIVATE_BODY"\n', private=True)
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch.object(cli, "Provider") as provider, redirect_stdout(output), redirect_stderr(output):
            self.assertEqual(cli.main(["--config", str(path), "--workdir", str(self.root), "--list-prompts"]), 0)
        provider.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["prompts"][0]["command"], "/review")
        self.assertNotIn("PRIVATE_BODY", output.getvalue())

    def test_six_cli_protocol_paths_complete_read_edit_and_independent_fixed_check(self):
        path = self.parent / "tasks.toml"
        with Directory(self.parent) as directory:
            directory.write_new(path.name, b'[prompts.repair]\ntemplate = "Repair $ARGUMENTS and run the configured checks."\n', private=True)
        check = [sys.executable, "-I", "-B", "-c", "from pathlib import Path;assert Path('answer.txt').read_text()=='new'"]
        for protocol in ("chat_completions", "messages", "responses"):
            for streaming in (False, True):
                with self.subTest(protocol=protocol, streaming=streaming):
                    captured, approvals = [], []
                    (self.root / "answer.txt").write_text("old", encoding="utf-8")
                    messages = [ui.call("read", "read_file", {"path": "answer.txt"}),
                                ui.call("edit", "edit_file", {"path": "answer.txt", "old": "old", "new": "new"}), ui.response()]
                    replies = [envelope(message) if protocol == "chat_completions" and not streaming else wire(protocol, message) for message in messages]
                    with backend(protocol, replies, captured, stream=streaming and protocol != "chat_completions") as url:
                        output = io.StringIO()
                        with patch.dict(os.environ, {}, clear=True), patch.object(cli, "approve", side_effect=lambda text: approvals.append(text) or True), redirect_stdout(output), redirect_stderr(output):
                            status = cli.main(["--config", str(path), "--workdir", str(self.root), "--model", "fixture", "--base-url", url,
                                               "--wire-api", protocol, "--proxy", "direct", "--max-steps", "3", "--check", json.dumps(check),
                                               "--stream" if streaming else "--no-stream", "/repair answer.txt"])
                    self.assertEqual(status, 0, output.getvalue()[-1500:])
                    self.assertEqual(len(captured), 3)
                    self.assertEqual(len(approvals), 2)  # Fresh edit, then independent application check.
                    self.assertEqual((self.root / "answer.txt").read_text(), "new")
                    self.assertIn("Repair answer.txt and run the configured checks.", json.dumps(captured[0]["body"]))
