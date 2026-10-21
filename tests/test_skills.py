"""Actual configured skill files, native path rejection, HTTP and terminal flows."""

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from textual.widgets import TextArea
from sprig_agent import cli
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.filesystem import Directory
from sprig_agent.provider import Provider, ProviderError
from sprig_agent.session import SessionError, SessionStore, validate_history
from sprig_agent.settings import _validate, load_settings, public_config
from sprig_agent.skills import SkillError, local_arguments, manifest
from sprig_agent.tools import Tools
from tests import test_tui as ui
from tests.test_generation_options import envelope
from tests.test_native_protocols import backend, native, sse, stream_events
from tests.test_provider_recovery import stream
from tests import test_settings as settings_fixture


def make_skill(root, name="review-code", body="WORKFLOW_BODY_ONLY_ON_DEMAND", description="Review Python code", extra=""):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "SKILL.md"
    path.write_text(f"---\nname: {name}\ndescription: {description}\n{extra}---\n{body}", encoding="utf-8")
    return folder


class SkillTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sprig-skills-")
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.library = self.parent / "技能 library"
        self.skill = make_skill(self.library)
        self.config = Config(self.root, "fixture", skill_dirs=(self.library,), max_steps=4)

    def tools(self, config=None, approve=None, cancel=None):
        tools = Tools(config or self.config, approve or (lambda _: self.fail("unexpected operation approval")), cancel)
        self.addCleanup(tools.close)
        return tools

    def test_safe_yaml_folded_description_optional_fields_and_unicode(self):
        value = manifest("---\nname: review-code\ndescription: >\n  Review code\n  carefully\ncompatibility: Python 3\nlicense: MIT\nallowed-tools: Bash(*)\nmetadata:\n  version: '1'\n---\n中文", "review-code")
        self.assertEqual(value["description"], "Review code carefully\n")
        self.assertEqual(value["metadata"], {"version": "1"})
        self.assertEqual(manifest("\ufeff---\nname: 中文技能\ndescription: 中文\n---\n", "中文技能")["name"], "中文技能")

    def test_invalid_yaml_and_duplicate_alias_tag_nested_metadata_are_rejected(self):
        fronts = ["name: OTHER\ndescription: text", "name: review--code\ndescription: text",
                  "name: review-code\nname: review-code\ndescription: text", "name: review-code\ndescription: 5",
                  "name: review-code\ndescription: &a text\nlicense: *a", "name: review-code\ndescription: !!python/object:object {}",
                  "name: review-code\ndescription: text\nmetadata: {v: [nested]}", "name: review-code\ndescription: text\nunknown: value",
                  "name: wrong-folder\ndescription: text", "name: review-code\ndescription: " + "x" * 1025,
                  "name: review-code\ndescription: " + "[" * 20 + "x" + "]" * 20]
        for front in fronts:
            with self.subTest(front=front[:80]), self.assertRaises(ValueError):
                manifest("---\n" + front + "\n---\nbody", "review-code")
        for text in ("no frontmatter", "---\nname: review-code", "---\n" + "#" * 17000 + "\n---\n"):
            with self.assertRaises(ValueError):
                manifest(text, "review-code")

    def test_catalog_only_metadata_and_no_script_execution_or_workspace_read_authority(self):
        marker = self.root / "MUST_NOT_EXECUTE"
        (self.skill / "script.py").write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").touch()", encoding="utf-8")
        tools = self.tools()
        catalog = tools.skills.catalog()
        self.assertEqual(len(catalog["skills"]), 1)
        self.assertNotIn("WORKFLOW_BODY", json.dumps(catalog))
        result = tools.skill("review-code")
        self.assertIn("WORKFLOW_BODY", result["content"])
        self.assertTrue(result["complete"] and result["not_authorization"] and result["guidance"])
        self.assertNotIn("read_id", result)
        self.assertFalse(marker.exists())
        self.assertIn("error", tools.execute("read_file", {"path": str(self.skill / "SKILL.md")}))

    def test_bounded_unicode_json_paging_reassembles_and_rejects_changed_resource(self):
        text = "中文🙂 \"\\\n" * 500
        (self.skill / "reference file.md").write_bytes(text.encode("utf-8"))
        tools = self.tools(replace(self.config, max_output=1200))
        args, chunks = {"name": "review-code", "path": "reference file.md"}, []
        while True:
            page = tools.skill(**args)
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False).encode()), 1200)
            chunks.append(page["content"])
            if page["complete"]:
                break
            args.update(offset=page["next_offset"], page_sha256=page["page_sha256"])
        self.assertEqual("".join(chunks).encode("utf-8"), text.encode("utf-8"))
        for bad in ({"offset": 1}, {"offset": 1, "page_sha256": page["page_sha256"]}, {"offset": True},
                    {"page_sha256": "0" * 64}, {"offset": -1}, {"offset": 262145}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                tools.skill("review-code", "reference file.md", **bad)
        (self.skill / "reference file.md").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed"):
            tools.skill("review-code", "reference file.md", 3, page["page_sha256"])

    def test_known_secret_redacted_whole_before_paging_and_generic_credentials_blocked(self):
        secret = "fixture-private-value-123456789"
        tools = self.tools(replace(self.config, secret_values=(secret,), max_output=1200))
        (self.skill / "notes.md").write_text("a" * 600 + secret + "b" * 600, encoding="utf-8")
        result = tools.skill("review-code", "notes.md")
        chunks = [result["content"]]
        while not result["complete"]:
            result = tools.skill("review-code", "notes.md", result["next_offset"], result["page_sha256"])
            chunks.append(result["content"])
        self.assertEqual("".join(chunks), "a" * 600 + "[REDACTED]" + "b" * 600)
        for name, data in (("token.md", "sk-" + "a" * 32), ("binary.dat", "x\0y")):
            (self.skill / name).write_text(data, encoding="utf-8")
            with self.assertRaises(ValueError):
                tools.skill("review-code", name)

    def test_duplicate_names_unavailable_without_overriding_and_invalid_skills_reported(self):
        second = self.parent / "second"
        make_skill(second, body="other")
        bad = self.library / "invalid"
        bad.mkdir()
        (bad / "SKILL.md").write_text("broken", encoding="utf-8")
        tools = self.tools(replace(self.config, skill_dirs=(self.library, second)))
        catalog = tools.skills.catalog()
        self.assertEqual(catalog["skills"], [])
        self.assertEqual(len(catalog["issues"]), 2)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            tools.skill("review-code")

    def test_scan_and_catalog_limits_never_expose_partial_registry(self):
        tools = self.tools()
        for limit in (patch("sprig_agent.skills.MAX_ENTRIES", 0), patch("sprig_agent.skills.MAX_CATALOG", 100)):
            with limit:
                result = tools.skills.catalog()
                self.assertEqual(result["skills"], [])
                self.assertTrue(result["limited"])

    def test_fresh_privacy_path_traversal_config_file_and_protected_components(self):
        tools = self.tools()
        for path in ("../outside", "/etc/passwd", "C:/private", "a\\b", ".env", ".git/config", ".", "x\0y"):
            with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                tools.skill("review-code", path)
        (self.skill / "private.md").write_text("private", encoding="utf-8")
        (self.library / ".sprigignore").write_text("*.md\n", encoding="utf-8")
        self.assertEqual(tools.skills.catalog()["skills"], [])
        (self.library / ".sprigignore").write_text("review-code/private.md\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "excluded"):
            tools.skill("review-code", "private.md")
        config = replace(self.config, config_path=self.skill / "private.md")
        with self.assertRaisesRegex(ValueError, "excluded"):
            self.tools(config).skill("review-code", "private.md")

    def test_workspace_exclusions_apply_to_explicit_skill_root_and_fresh_ignore(self):
        local = self.root / "workflows"
        make_skill(local)
        tools = self.tools(replace(self.config, skill_dirs=(local,)))
        self.assertEqual(len(tools.skills.catalog()["skills"]), 1)
        (self.root / ".sprigignore").write_text("workflows/\n", encoding="utf-8")
        self.assertEqual(tools.skills.catalog()["skills"], [])
        tools = self.tools(replace(self.config, skill_dirs=(local,), excluded_paths=("workflows/",)))
        self.assertEqual(tools.skills.catalog()["skills"], [])

    def test_hardlinked_file_cannot_transfer_external_data(self):
        source = self.parent / "private.md"
        source.write_text("OUTSIDE_PRIVATE", encoding="utf-8")
        os.link(source, self.skill / "linked.md")
        with self.assertRaisesRegex(ValueError, "single-link"):
            self.tools().skill("review-code", "linked.md")

    @unittest.skipIf(os.name == "nt", "POSIX symbolic link fixture")
    def test_posix_symlink_roots_skills_and_resources_never_followed(self):
        tools = self.tools()
        (self.library / "aliased").symlink_to(self.skill, target_is_directory=True)
        (self.skill / "alias.md").symlink_to(self.skill / "SKILL.md")
        self.assertEqual(len(tools.skills.catalog()["skills"]), 1)
        with self.assertRaises((ValueError, OSError)):
            tools.skill("review-code", "alias.md")
        alias = self.parent / "alias-root"
        alias.symlink_to(self.library, target_is_directory=True)
        self.assertEqual(self.tools(replace(self.config, skill_dirs=(alias,))).skills.catalog()["skills"], [])

    @unittest.skipUnless(os.name == "nt", "native Windows junction fixture")
    def test_windows_junction_skill_resource_and_root_rejected(self):
        link = self.library / "aliased"
        nested = self.skill / "escape"
        root_alias = self.parent / "alias-root"
        tools = self.tools()
        for path, target in ((link, self.skill), (nested, self.skill), (root_alias, self.library)):
            subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(path), str(target)], check=True, capture_output=True)
        try:
            self.assertEqual(len(tools.skills.catalog()["skills"]), 1)
            with self.assertRaises((ValueError, OSError)):
                tools.skill("review-code", "escape/SKILL.md")
            self.assertEqual(self.tools(replace(self.config, skill_dirs=(root_alias,))).skills.catalog()["skills"], [])
        finally:
            for path in (link, nested, root_alias):
                self.assertTrue(path.absolute().is_relative_to(self.parent))
                path.rmdir()  # Only the controlled junction, never its target.

    def test_closed_cancelled_disabled_and_oversized_resources(self):
        tools = self.tools(replace(self.config, disabled_tools=("skill",)))
        self.assertIn("error", tools.execute("skill", {"name": "review-code"}))
        self.assertNotIn("skill", [s["function"]["name"] for s in Agent(tools.config, None, tools, lambda _: None).schemas])
        tools.close()
        with self.assertRaises(OSError):
            tools.skills.catalog()
        event = threading.Event()
        cancelled = self.tools(cancel=event)
        event.set()
        with self.assertRaises(KeyboardInterrupt):
            cancelled.skills.catalog()
        (self.skill / "large.md").write_bytes(b"a" * 262145)
        with self.assertRaisesRegex(ValueError, "byte limit"):
            self.tools().skill("review-code", "large.md")
        self.assertTrue(self.tools(replace(self.config, skill_dirs=(self.parent / "missing",))).skills.catalog()["issues"])

    def test_read_only_and_allowed_tools_cannot_authorize_commands(self):
        make_skill(self.library, body="Run a script now", extra="allowed-tools: Bash(*)\n")
        for read_only in (False, True):
            approvals = Mock(return_value=False)
            config = replace(self.config, read_only=read_only)
            tools = self.tools(config, approvals)
            provider = ui.ScriptedProvider([ui.call("skill", "skill", {"name": "review-code"}),
                ui.call("run", "run", {"argv": [sys.executable, "-c", "raise SystemExit('NO')"]}), ui.response()])
            messages, complete = Agent(config, provider, tools, lambda _: None).run("use skill")
            self.assertTrue(complete)
            result = json.loads(next(row["content"] for row in messages if row.get("tool_call_id") == "run"))
            self.assertFalse(result.get("approved", False))
            self.assertEqual(approvals.call_count, 0 if read_only else 1)

    def test_stale_manifest_after_model_request_rejected_then_fresh_catalog_served(self):
        tools = self.tools()
        outer = self
        class Changing(ui.ScriptedProvider):
            def complete(self, messages, schemas, on_text=None):
                if not self.calls:
                    make_skill(outer.library, body="NEW_BODY", description="New description")
                return super().complete(messages, schemas, on_text)
        provider = Changing([ui.call("s", "skill", {"name": "review-code"}), ui.response()])
        messages, _ = Agent(self.config, provider, tools, lambda _: None).run("review")
        self.assertIn("changed since", json.loads(messages[3]["content"])["error"])
        self.assertIn("New description", provider.calls[1][0]["content"])
        self.assertNotIn("NEW_BODY", json.dumps(messages))

    def test_manifest_changed_during_resource_load_is_not_delivered(self):
        tools = self.tools()
        (self.skill / "reference.md").write_text("reference", encoding="utf-8")
        original = tools.skills.read
        def read(root, policy, parts, limit):
            result = original(root, policy, parts, limit)
            if parts[-1] == "reference.md":
                make_skill(self.library, body="changed during resource read")
            return result
        with patch.object(tools.skills, "read", side_effect=read), self.assertRaisesRegex(ValueError, "changed while"):
            tools.skill("review-code", "reference.md")

    def test_real_model_request_contains_only_metadata_then_loaded_skill_and_resource(self):
        (self.skill / "reference.md").write_text("RESOURCE_ON_DEMAND", encoding="utf-8")
        def wire(protocol, message):
            calls = [(row["id"], row["function"]["name"], json.loads(row["function"]["arguments"])) for row in message.get("tool_calls", [])]
            return envelope(message) if protocol == "chat_completions" else native(protocol, message.get("content"), calls, reasoning=False)
        for protocol in ("chat_completions", "messages", "responses"):
            for streaming in (False, True):
                captured = []
                messages = [ui.call("s", "skill", {"name": "review-code"}),
                            ui.call("r", "skill", {"name": "review-code", "path": "reference.md"}), ui.response()]
                if streaming and protocol == "chat_completions":
                    responses = []
                    for message in messages:
                        delta = dict(message)
                        if "tool_calls" in delta:
                            delta["tool_calls"] = [{"index": 0, **delta["tool_calls"][0]}]
                        responses.append(stream(delta, "tool_calls" if "tool_calls" in delta else "stop"))
                else:
                    responses = [wire(protocol, message) for message in messages]
                with self.subTest(protocol=protocol, streaming=streaming), backend(protocol, responses, captured, stream=streaming and protocol != "chat_completions") as url:
                    config = replace(self.config, base_url=url, proxy="direct", stream=streaming, wire_api=protocol)
                    tools = self.tools(config)
                    rows, complete = Agent(config, Provider(config), tools, lambda _: None).run("use skill")
                self.assertTrue(complete)
                self.assertEqual(len(captured), 3)
                first, second, third = [json.dumps(value) for value in captured]
                self.assertIn("Review Python code", first)
                self.assertNotIn("WORKFLOW_BODY", first)
                self.assertIn("WORKFLOW_BODY", second)
                self.assertNotIn("RESOURCE_ON_DEMAND", second)
                self.assertIn("RESOURCE_ON_DEMAND", third)
                validate_history(rows)

    def test_private_session_binding_and_resume_do_not_load_or_run_old_skill(self):
        tools = self.tools()
        provider = ui.ScriptedProvider([ui.call("s", "skill", {"name": "review-code"}), ui.response()])
        rows, _ = Agent(self.config, provider, tools, lambda _: None).run("review")
        store = SessionStore(self.parent / "sessions", self.config)
        self.addCleanup(store.close)
        store.save("skill", rows)
        self.assertEqual(store.load("skill"), rows)
        different = SessionStore(self.parent / "sessions", replace(self.config, skill_dirs=()))
        self.addCleanup(different.close)
        with self.assertRaises(SessionError):
            different.load("skill")

    def test_incomplete_protocol_stream_never_reads_skill_body(self):
        for protocol in ("chat_completions", "messages", "responses"):
            args = {"name": "review-code"}
            if protocol == "chat_completions":
                message = ui.call("s", "skill", args)
                body = stream({**message, "tool_calls": [{"index": 0, **message["tool_calls"][0]}]}, "tool_calls", done=False)
            else:
                body = sse(stream_events(protocol, native(protocol, None, [("s", "skill", args)], reasoning=False))[:-1])
            captured = []
            with self.subTest(protocol=protocol), backend(protocol, [body], captured) as url:
                config = replace(self.config, base_url=url, proxy="direct", stream=True, wire_api=protocol)
                tools = self.tools(config)
                with patch.object(tools.skills, "load", wraps=tools.skills.load) as loaded, self.assertRaises(ProviderError):
                    Agent(config, Provider(config), tools, lambda _: None).run("use skill")
                loaded.assert_not_called()
                self.assertEqual(len(captured), 1)

    def test_cli_local_preview_has_no_model_request_and_quotes_resource_paths(self):
        tools = self.tools()
        (self.skill / "a b.md").write_text("local resource", encoding="utf-8")
        provider = ui.ScriptedProvider([])
        agent = Agent(self.config, provider, tools, lambda _: None)
        output = []
        with patch("builtins.input", side_effect=["/skills", '/skill review-code "a b.md"', "/exit"]):
            cli.chat(agent, None, None, output.append, autostart=False)
        self.assertEqual(provider.calls, [])
        self.assertIn("local resource", "".join(output))
        self.assertEqual(local_arguments('review-code "a b.md"')["path"], "a b.md")


class SkillSettingsTests(unittest.TestCase):
    setUp = settings_fixture.SettingsTests.setUp
    write = settings_fixture.SettingsTests.write
    resolve = settings_fixture.SettingsTests.resolve
    invoke = settings_fixture.SettingsTests.invoke

    def test_offline_cli_catalog_needs_neither_model_credential_nor_execution(self):
        library = self.parent / "library"
        make_skill(library)
        with patch.object(cli, "Provider") as provider, patch.object(cli, "approve", side_effect=AssertionError("must not execute")):
            code, output, _ = self.invoke(["--no-config", "--list-skills", "--skill-dir", str(library)])
        self.assertEqual(code, 0)
        result = json.loads(output)
        self.assertFalse(result["network_contacted"])
        self.assertEqual(result["skills"][0]["name"], "review-code")
        self.assertNotIn("WORKFLOW_BODY", output)
        provider.assert_not_called()

    def test_root_precedence_public_sources_binding_and_project_rejection(self):
        self.write('provider="x"\n[providers.x]\nmodel="fixture"\n[skills]\ndirs=["user-skills"]\n[profiles.review.skills]\ndirs=["profile-skills"]\n')
        settings = load_settings(self.path)
        config, _ = settings.resolve({"workdir": str(self.root), "profile": "review"}, environ={})
        self.assertEqual(config.skill_dirs, (self.parent / "profile-skills",))
        self.assertEqual(dict(config.setting_sources)["skill_dirs"], "profile")
        config, details = settings.resolve({"workdir": str(self.root), "skill_dirs": ["cli-skills"]}, environ={})
        self.assertEqual(config.skill_dirs, (self.root / "cli-skills",))
        self.assertEqual(public_config(config, details)["skills"]["dirs"], [str(self.root / "cli-skills")])
        self.assertEqual(config.binding()["skill_dirs"], [str(self.root / "cli-skills")])
        for bad in ("dirs='one'", "dirs=[false]", "dirs=['']", "dirs=[" + ",".join('"x"' for _ in range(9)) + "]"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                import tomllib
                _validate(tomllib.loads('[skills]\n' + bad), self.parent)
        with self.assertRaises(ValueError):
            _validate({"skills": {"dirs": ["elsewhere"]}}, self.parent, project=True)


class SkillTuiTests(unittest.IsolatedAsyncioTestCase):
    setUp = ui.TuiTests.setUp
    asyncSetUp = ui.TuiTests.asyncSetUp
    app = ui.TuiTests.app
    until = ui.TuiTests.until
    click = ui.TuiTests.click

    async def test_local_preview_preserves_main_draft_then_skill_task_needs_fresh_approval(self):
        library = self.parent / "library"
        make_skill(library)
        self.config = replace(self.config, skill_dirs=(library,))
        provider = ui.ScriptedProvider([ui.call("s", "skill", {"name": "review-code"}),
            ui.call("e", "edit_file", {"path": "chosen.txt", "old": "", "new": "reviewed"}), ui.response()])
        app = self.app(provider, save_session="skill")
        async with app.run_test(size=(110, 36)) as pilot:
            draft = app.query_one("#task", TextArea)
            draft.load_text("UNSENT_DRAFT")
            app.slash_command("/skills")
            await self.until(pilot, lambda: not app.busy)
            self.assertIn("Review Python code", app.transcript)
            self.assertEqual(draft.text, "UNSENT_DRAFT")
            app.slash_command("/skill review-code")
            await self.until(pilot, lambda: not app.busy)
            self.assertIn("WORKFLOW_BODY", app.transcript)
            self.assertEqual(provider.calls, [])
            app.start("use review-code")
            await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
            self.assertFalse((self.root / "chosen.txt").exists())
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual((self.root / "chosen.txt").read_text(), "reviewed")
            self.assertEqual(draft.text, "UNSENT_DRAFT")
            self.assertEqual(app.store.load("skill"), app.messages)


if __name__ == "__main__":
    unittest.main()
