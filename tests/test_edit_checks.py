"""Fixed checks after edit batches return evidence without completing the task."""

import copy
from contextlib import closing
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.provider import Provider
from sprig_agent.session import SessionError, SessionStore, validate_history
from sprig_agent.tools import Tools
from tests.test_planning import replies


def edit(identity, old, new, path="value.txt"):
    return {"id": identity, "type": "function", "function": {"name": "edit_file", "arguments": json.dumps({
        "path": path, "old": old, "new": new})}}


def response(*calls):
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


DONE = {"role": "assistant", "content": "Done"}
PASS = {"approved": True, "returncode": 0}
EDITED = {"approved": True, "changed": True}


class EditChecksTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.check = (sys.executable, "-I", "-B", "-c", "print('CHECK_OBSERVED')")
        self.base = Config(self.root, "fixture", checks=(self.check,), max_steps=3)
        self.saved, self.emitted = [], []

    def agent(self, config, provider, tools, **kwargs):
        return Agent(config, provider, tools, self.emitted.append,
                     lambda history: self.saved.append(copy.deepcopy(history)), **kwargs)

    def test_actual_http_failure_after_edit_guides_repair_before_fresh_final_acceptance(self):
        (self.root / "value.txt").write_text("initial", encoding="utf-8")
        verifier = self.parent / "fixed_check.py"
        verifier.write_text("import sys\nfrom pathlib import Path\n"
                            "assert Path(sys.argv[1], 'value.txt').read_text() == 'good', 'FIXED_FAILURE'\n", encoding="utf-8")
        original = hashlib.sha256(verifier.read_bytes()).hexdigest()
        captured, prompts = [], []
        def repair(payload):
            checks = [m for m in payload["messages"] if m.get("tool_call_id", "").startswith("sprig-edit-check-")]
            result = json.loads(checks[-1]["content"])
            self.assertNotEqual(result["returncode"], 0)
            self.assertIn("FIXED_FAILURE", result["output"])
            self.assertIn("after file edits", payload["messages"][0]["content"])
            return response(edit("repair", "still bad", "good"))
        def finish(payload):
            checks = [m for m in payload["messages"] if m.get("tool_call_id", "").startswith("sprig-edit-check-")]
            self.assertEqual(json.loads(checks[-1]["content"])["returncode"], 0)
            return DONE
        def approve(description):
            self.assertTrue(self.saved[-1][-1].get("tool_calls"), "checkpoint intent before side effects")
            prompts.append(description)
            return True
        with replies([response(edit("first", "initial", "still bad")), repair, finish], captured) as url:
            config = replace(self.base, base_url=url, proxy="direct", check_after_edit=True,
                             checks=((sys.executable, "-I", "-B", str(verifier), str(self.root)),))
            with closing(Tools(config, approve)) as tools:
                agent = self.agent(config, Provider(config), tools)
                history, completed = agent.run("Repair the value using the fixed external check")
        self.assertTrue(completed)
        self.assertEqual(agent.edit_check_status, "passed")
        self.assertEqual(len(captured), 3)
        self.assertEqual(len(prompts), 5)  # Two writes, two edit checks, final acceptance.
        self.assertTrue(history[-1]["tool_call_id"].startswith("sprig-check-"))
        self.assertEqual(validate_history(history), history)
        self.assertEqual(hashlib.sha256(verifier.read_bytes()).hexdigest(), original)
        self.assertEqual((self.root / "value.txt").read_text(), "good")

    def test_multiple_edits_finish_their_tool_results_before_one_check_batch(self):
        config = replace(self.base, check_after_edit=True)
        provider, tools = Mock(), Mock()
        provider.complete.side_effect = [response(edit("a", "old", "new"), edit("b", "old", "new", "other.txt")), DONE]
        tools.execute.side_effect = [EDITED, EDITED, PASS, PASS]
        agent = self.agent(config, provider, tools)
        history, complete = agent.run("Edit both files")
        self.assertTrue(complete)
        self.assertEqual([call.args[0] for call in tools.execute.call_args_list], ["edit_file", "edit_file", "run", "run"])
        checkpoint = next(s for s in self.saved if s[-1].get("tool_calls", [{}])[0].get("id", "").startswith("sprig-edit-check-"))
        self.assertEqual([m["tool_call_id"] for m in checkpoint if m.get("role") == "tool"], ["a", "b"])
        self.assertEqual(validate_history(history), history)

    def test_last_request_runs_check_but_does_not_extend_budget_or_claim_completion(self):
        config = replace(self.base, check_after_edit=True, max_steps=1)
        provider, tools = Mock(), Mock()
        provider.complete.return_value = response(edit("first", "old", "new"))
        tools.execute.side_effect = [EDITED, {"approved": True, "returncode": 1, "output": "REPAIR_REQUIRED"}]
        agent = self.agent(config, provider, tools)
        history, complete = agent.run("Repair")
        self.assertFalse(complete)
        self.assertEqual(provider.complete.call_count, 1)
        self.assertEqual(agent.edit_check_status, "failed")
        self.assertIn("REPAIR_REQUIRED", str(history))
        self.assertEqual(validate_history(history), history)

    def test_rejected_or_unreliable_edit_checks_stop_without_model_bypass(self):
        config = replace(self.base, check_after_edit=True)
        for result in ({"approved": False}, {"error": "missing command"},
                       {"approved": True, "returncode": False},
                       {**PASS, "skipped": True}, {**PASS, "cancelled": True}, {**PASS, "interrupted": True},
                       {**PASS, "outcome": "uncertain"}):
            with self.subTest(result=result):
                provider, tools = Mock(), Mock()
                provider.complete.return_value = response(edit("a", "old", "new"))
                tools.execute.side_effect = [EDITED, result]
                agent = self.agent(config, provider, tools)
                history, complete = agent.run("Task")
                self.assertFalse(complete)
                self.assertEqual(provider.complete.call_count, 1)
                self.assertIn(agent.edit_check_status, {"blocked", "incomplete"})
                self.assertEqual(validate_history(history), history)

    def test_runtime_timeout_output_limit_and_signal_failures_return_to_repair_loop(self):
        config = replace(self.base, check_after_edit=True)
        for fault in ({"approved": True, "returncode": -11}, {**PASS, "timed_out": True}, {**PASS, "output_limited": True}):
            with self.subTest(fault=fault):
                provider, tools = Mock(), Mock()
                def respond(messages, schemas):
                    if provider.complete.call_count == 1:
                        return response(edit("first", "old", "bad"))
                    if provider.complete.call_count == 2:
                        observed = [json.loads(m["content"]) for m in messages
                                    if m.get("tool_call_id", "").startswith("sprig-edit-check-")]
                        self.assertEqual(observed, [fault])
                        return response(edit("repair", "bad", "good"))
                    return DONE
                provider.complete.side_effect = respond
                tools.execute.side_effect = [EDITED, fault, EDITED, PASS, PASS]
                agent = self.agent(config, provider, tools)
                history, complete = agent.run("Repair the failing behavior")
                self.assertTrue(complete)
                self.assertEqual(provider.complete.call_count, 3)
                self.assertEqual(validate_history(history), history)

    def test_final_acceptance_also_rejects_false_skipped_and_uncertain_success(self):
        for result in ({"approved": True, "returncode": False}, {**PASS, "skipped": True}, {**PASS, "cancelled": True},
                       {**PASS, "interrupted": True}, {**PASS, "outcome": "uncertain"}):
            with self.subTest(result=result):
                provider, tools = Mock(), Mock()
                provider.complete.return_value = DONE
                tools.execute.return_value = result
                _, complete = self.agent(self.base, provider, tools).run("Task")
                self.assertFalse(complete)
                self.assertEqual(provider.complete.call_count, 1)

    def test_rejected_noop_and_command_actions_do_not_trigger_file_edit_checks(self):
        config = replace(self.base, check_after_edit=True)
        for name, result in (("edit_file", {"approved": False}), ("edit_file", {"approved": True, "changed": False}), ("run", PASS)):
            provider, tools = Mock(), Mock()
            call = edit("a", "old", "new")
            if name == "run":
                call["function"] = {"name": name, "arguments": json.dumps({"argv": ["fixture"]})}
            provider.complete.side_effect = [response(call), DONE]
            tools.execute.side_effect = [result, PASS]
            agent = self.agent(config, provider, tools)
            _, complete = agent.run("Task")
            self.assertTrue(complete)
            self.assertEqual(agent.edit_check_status, "not_run")
            self.assertEqual(tools.execute.call_count, 2)

    def test_new_guidance_between_edits_prevents_checks_for_superseded_batch(self):
        config = replace(self.base, check_after_edit=True)
        pending, provider, tools = [], Mock(), Mock()
        def execute(name, arguments):
            if name == "edit_file":
                pending.append("Review the changed scope first")
                return EDITED
            return {"approved": False}
        tools.execute.side_effect = execute
        def respond(messages, schemas):
            if provider.complete.call_count == 1:
                return response(edit("a", "old", "new"), edit("b", "old", "new", "other.txt"))
            self.assertEqual([call.args[0] for call in tools.execute.call_args_list], ["edit_file"])
            self.assertEqual(messages[-1]["content"], "Review the changed scope first")
            return DONE
        provider.complete.side_effect = respond
        agent = self.agent(config, provider, tools, receive_input=lambda: [pending.pop()] if pending else [])
        history, completed = agent.run("Task")
        self.assertFalse(completed)
        self.assertEqual(agent.edit_check_status, "not_run")
        self.assertEqual(validate_history(history), history)

    def test_check_intent_checkpoint_failure_never_executes_and_restore_never_replays(self):
        config = replace(self.base, check_after_edit=True)
        provider, tools = Mock(), Mock()
        provider.complete.return_value = response(edit("a", "old", "new"))
        tools.execute.return_value = EDITED
        agent = self.agent(config, provider, tools)
        def checkpoint(history):
            self.saved.append(copy.deepcopy(history))
            if history[-1].get("tool_calls", [{}])[0].get("id", "").startswith("sprig-edit-check-"):
                raise OSError("disk full")
        agent.checkpoint = checkpoint
        with self.assertRaises(OSError):
            agent.run("Task")
        tools.execute.assert_called_once()
        self.assertEqual(agent.edit_check_status, "error")
        restored = validate_history(self.saved[-1])
        self.assertIn("NOT replayed", restored[-1]["content"])

    def test_opt_in_validation_and_binding_preserve_legacy_sessions(self):
        self.assertNotIn("check_after_edit", self.base.binding())
        config = replace(self.base, check_after_edit=True)
        for values in ({"check_after_edit": "yes"}, {"checks": ()}, {"read_only": True}, {"disabled_tools": ("run",)}):
            with self.assertRaises(ValueError):
                replace(config, **values)
        with closing(SessionStore(self.parent / "sessions", config)) as store:
            store.save("bound", [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}])
        with closing(SessionStore(self.parent / "sessions", self.base)) as store:
            with self.assertRaises(SessionError):
                store.load("bound")

    def test_default_off_retains_existing_finish_only_acceptance(self):
        provider, tools = Mock(), Mock()
        provider.complete.side_effect = [response(edit("a", "old", "new")), DONE]
        tools.execute.side_effect = [EDITED, PASS]
        agent = self.agent(self.base, provider, tools)
        history, complete = agent.run("Task")
        self.assertTrue(complete)
        self.assertEqual(agent.edit_check_status, "not_run")
        self.assertEqual(tools.execute.call_count, 2)
        self.assertFalse(any(m.get("tool_call_id", "").startswith("sprig-edit-check-") for m in history))

    def test_cancelled_edit_check_stops_and_retained_intent_is_never_replayed(self):
        config = replace(self.base, check_after_edit=True)
        provider, tools = Mock(), Mock()
        provider.complete.return_value = response(edit("a", "old", "new"))
        tools.execute.side_effect = [EDITED, KeyboardInterrupt()]
        agent = self.agent(config, provider, tools)
        with self.assertRaises(KeyboardInterrupt):
            agent.run("Task")
        self.assertEqual(agent.edit_check_status, "cancelled")
        self.assertEqual(provider.complete.call_count, 1)
        self.assertIn("NOT replayed", validate_history(self.saved[-1])[-1]["content"])

    def test_real_line_edit_triggers_check_and_commands_expire_prior_read_ids(self):
        (self.root / "value.txt").write_bytes(b"before\n")
        captured, read_ids, prompts = [], [], []
        def change(payload):
            result = json.loads(payload["messages"][-1]["content"])
            read_ids.append(result["read_id"])
            return response({"id": "line-edit", "type": "function", "function": {"name": "edit_lines",
                "arguments": json.dumps({"path": "value.txt", "read_id": read_ids[-1],
                    "edits": [{"start_line": 1, "end_line": 1, "new": "after\n"}]})}})
        def finish(payload):
            self.assertIsNone(tools.read_versions.resolve(read_ids[-1], ("value.txt",)))
            return DONE
        initial = response({"id": "read", "type": "function", "function": {"name": "read_file",
            "arguments": json.dumps({"path": "value.txt", "offset": 1, "limit": 1})}})
        with replies([initial, change, finish], captured) as url:
            config = replace(self.base, base_url=url, proxy="direct", check_after_edit=True,
                checks=((sys.executable, "-I", "-B", "-c", "from pathlib import Path; assert Path('value.txt').read_bytes() == b'after\\n'"),))
            with closing(Tools(config, lambda description: prompts.append(description) or True)) as tools:
                history, complete = self.agent(config, Provider(config), tools).run("Repair the line")
        self.assertTrue(complete)
        self.assertEqual((self.root / "value.txt").read_bytes(), b"after\n")
        self.assertEqual(len(prompts), 3)
        self.assertEqual(validate_history(history), history)

    def test_edit_check_reuses_only_unchanged_and_still_visible_read_ids(self):
        for name in ("value.txt", "next.txt", "modified.txt", "hidden.txt", "deleted.txt"):
            (self.root / name).write_bytes(b"before\n")
        captured, prompts, read_ids = [], [], {}
        names = ("next.txt", "modified.txt", "hidden.txt", "deleted.txt")
        initial = response(*({"id": "read-" + name, "type": "function", "function": {"name": "read_file",
                             "arguments": json.dumps({"path": name})}} for name in names))

        def first_edit(payload):
            for message in payload["messages"]:
                if message.get("role") == "tool":
                    result = json.loads(message["content"])
                    read_ids[result["path"]] = result["read_id"]
            return response(edit("first", "before", "after"))

        def next_edit(payload):
            for name in names:
                self.assertIsNotNone(read_ids[name])
                version = tools.read_versions.resolve(read_ids[name], (name,))
                self.assertEqual(version is not None, name == "next.txt")
            return response({"id": "second", "type": "function", "function": {"name": "edit_lines",
                "arguments": json.dumps({"path": "next.txt", "read_id": read_ids["next.txt"],
                                         "edits": [{"start_line": 1, "end_line": 1, "lines": ["fixed"]}]})}})

        script = ("from pathlib import Path\n"
                  "Path('modified.txt').write_bytes(b'changed\\n')\n"
                  "Path('deleted.txt').unlink(missing_ok=True)\n"
                  "Path('.sprigignore').write_bytes(b'hidden.txt\\n')\n"
                  "assert Path('next.txt').read_bytes() == b'fixed\\n'\n")
        with replies([initial, first_edit, next_edit, DONE], captured) as url:
            config = replace(self.base, base_url=url, proxy="direct", check_after_edit=True, max_steps=4,
                             checks=((sys.executable, "-I", "-B", "-c", script),))
            with closing(Tools(config, lambda description: prompts.append(description) or True)) as tools:
                history, complete = self.agent(config, Provider(config), tools).run("Edit the two files")
        self.assertTrue(complete)
        self.assertEqual(len(captured), 4)
        self.assertEqual(len(prompts), 5)  # Two writes, two edit checks, and final acceptance.
        self.assertEqual((self.root / "next.txt").read_bytes(), b"fixed\n")
        self.assertEqual(validate_history(history), history)

    def test_timed_out_check_does_not_restore_an_unchanged_read_id(self):
        (self.root / "value.txt").write_bytes(b"before")
        (self.root / "next.txt").write_bytes(b"before")
        initial = response({"id": "read", "type": "function", "function": {"name": "read_file",
                            "arguments": '{"path":"next.txt"}'}})
        config = replace(self.base, check_after_edit=True, max_steps=2, timeout=0.3,
                         checks=((sys.executable, "-I", "-B", "-c", "import time; time.sleep(10)"),))
        provider = Mock()
        provider.complete.side_effect = [initial, response(edit("first", "before", "after"))]
        with closing(Tools(config, lambda _: True)) as tools:
            agent = self.agent(config, provider, tools)
            history, complete = agent.run("Edit the value")
            result = next(json.loads(message["content"]) for message in history if message.get("tool_call_id") == "read")
            self.assertIsNone(tools.read_versions.resolve(result["read_id"], ("next.txt",)))
        self.assertFalse(complete)
        self.assertEqual(agent.edit_check_status, "failed")
        self.assertTrue(json.loads(history[-2]["content"])["timed_out"])


if __name__ == "__main__":
    unittest.main()
