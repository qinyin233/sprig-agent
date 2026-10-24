"""Reference shell guard works without installing the reference SDK."""

import importlib.util
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from sprig_agent.config import Config

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("mini_environment", ROOT / "benchmarks/clients/mini_environment.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Submitted(Exception):
    def __init__(self, message):
        self.message = message


class MiniEnvironmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.descriptions = []

    def environment(self, confirm, **options):
        config = Config(self.root, "fixture", **options)
        env = module.ApprovedEnvironment(config, [sys.executable, "-I", "-c", "print('fixed check')"],
                                         lambda: True, confirm, Submitted)
        self.addCleanup(env.close)
        return env

    def command(self, body):
        return shlex.join([sys.executable.replace("\\", "/"), "-I", "-B", "-c", body])

    def test_rejection_and_read_only_prevent_shell_writes(self):
        for readonly in (False, True):
            with self.subTest(readonly=readonly):
                env = self.environment(lambda text: self.descriptions.append(text) or False, read_only=readonly)
                result = env.execute({"command": "echo forbidden > candidate.txt"})
                self.assertFalse((self.root / "candidate.txt").exists())
                self.assertEqual(result["exception_info"], "read-only mode blocks command execution" if readonly else "Action rejected")
                self.assertEqual(env.approvals["requests"], 0 if readonly else 1)

    def test_each_command_requires_fresh_approval_and_cwd_is_restricted(self):
        env = self.environment(lambda text: self.descriptions.append(text) or True)
        for command in ("pwd", "echo hello"):
            self.assertEqual(env.execute({"command": command})["returncode"], 0)
        self.assertEqual(len(self.descriptions), 2)
        self.assertIn("echo hello", self.descriptions[-1])
        env.execute({"command": "pwd"}, cwd=str(self.root.parent))
        self.assertEqual(len(self.descriptions), 2)

    def test_only_literal_true_can_authorize_a_shell_side_effect(self):
        class Truthy:
            def __bool__(self):
                raise AssertionError("approval must not coerce callback values")

        for index, value in enumerate((1, "yes", [True], Truthy(), False, None)):
            with self.subTest(kind=type(value).__name__):
                env = self.environment(lambda _: value)
                filename = "candidate-" + str(index) + ".txt"
                result = env.execute({"command": "echo forbidden > " + filename})
                self.assertFalse((self.root / filename).exists())
                self.assertEqual(result["exception_info"], "Action rejected")
                self.assertEqual(env.approvals["accepted"], 0)
                self.assertEqual(env.approvals["rejected"], 1)

    def test_credentials_are_not_in_templates_or_approved_command_environment(self):
        env = self.environment(lambda _: True, api_key="fixture-private-marker")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fixture-private-marker", "OTHER_SECRET": "fixture-private-marker"}):
            result = env.execute({"command": self.command("import os; print(os.getenv('OPENAI_API_KEY'), os.getenv('OTHER_SECRET'))")})
            self.assertEqual(result["output"].strip(), "None None")
            self.assertNotIn("fixture-private-marker", json.dumps(env.get_template_vars()))
            self.assertNotIn("fixture-private-marker", json.dumps(env.serialize()))

    def test_output_limit_stops_flood_and_marks_it_for_the_reference_model(self):
        env = self.environment(lambda _: True, max_output=256, command_output_limit=256)
        result = env.execute({"command": self.command("print('x' * 1000000)")})
        self.assertLessEqual(len(result["output"].encode()), 256)
        self.assertEqual(result["exception_info"], "Command output limit exceeded")
        self.assertTrue(env.records[-1]["output_limited"])

    def test_bash_timeout_terminates_descendants_on_the_actual_platform(self):
        env = self.environment(lambda _: True, timeout=3)
        child = "from pathlib import Path; import time; Path('ready').write_text('yes'); time.sleep(3); Path('escaped').write_text('bad')"
        parent = "import subprocess,time; subprocess.Popen(" + repr([sys.executable, '-I', '-B', '-c', child]) + "); time.sleep(10)"
        result = env.execute({"command": self.command(parent)}, timeout=1)
        self.assertEqual(result['exception_info'], 'Command timed out')
        self.assertTrue((self.root / 'ready').exists(), 'fixture descendant did not start before timeout')
        time.sleep(2.5)
        self.assertFalse((self.root / 'escaped').exists())
        self.assertTrue(env.records[-1]['timed_out'])

    def test_command_relay_preserves_quotes_backslashes_and_unicode(self):
        env = self.environment(lambda text: self.descriptions.append(text) or True)
        values = ["E:\\example\\Unicode", "a\"b'c", "名🙂"]
        body = "import json; print(json.dumps(" + repr(values) + ", ensure_ascii=True))"
        command = self.command(body)
        result = env.execute({"command": command})
        self.assertEqual(result['returncode'], 0, result)
        self.assertEqual(json.loads(result['output']), values)
        argv_line = next(line for line in self.descriptions[0].splitlines() if line.startswith('argv: '))
        self.assertEqual(json.loads(argv_line[6:])[-1], command)

    def test_acceptance_rejection_stops_before_claiming_completion(self):
        answers = iter((True, False))
        env = self.environment(lambda _: next(answers))
        with self.assertRaises(Submitted) as caught:
            env.execute({"command": "echo " + module.MARKER})
        self.assertEqual(caught.exception.message["extra"]["exit_status"], "AcceptanceRejected")
        self.assertFalse(env.acceptance_passed)
        self.assertEqual(env.approvals["requests"], 2)

    def test_verifier_change_during_approval_blocks_effect(self):
        valid = True
        def changed(_):
            nonlocal valid
            valid = False
            return True
        env = self.environment(changed)
        env.intact = lambda: valid
        with self.assertRaises(ValueError):
            env.execute({"command": "echo forbidden > candidate.txt"})
        self.assertFalse((self.root / "candidate.txt").exists())


if __name__ == "__main__":
    unittest.main()
