"""Probe boundaries use disposable commands, never remote models or API keys."""

import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("client_probes", ROOT / "scripts" / "benchmark-clients.py")
probes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probes)
child_spec = importlib.util.spec_from_file_location("probe_child", probes.CHILD)
child = importlib.util.module_from_spec(child_spec)
child_spec.loader.exec_module(child)


class ClientProbeTests(unittest.TestCase):
    def fake(self, root, body):
        source = root / "fake.py"
        source.write_text(body, encoding="utf-8")
        return [sys.executable, "-I", "-B", str(source)]

    def test_child_environment_is_private_and_contains_no_inherited_credentials(self):
        with probes.task_bench.disposable() as root:
            original = {"SPRIG_API_KEY": "secret", "OPENAI_API_KEY": "secret", "GITHUB_TOKEN": "secret",
                        "PASSWORD": "secret", "MY_SECRET": "secret", "PATH": "safe", "HOME": "original",
                        "AWS_ACCESS_KEY_ID": "secret", "GOOGLE_APPLICATION_CREDENTIALS": "secret",
                        "OPENCODE_CONFIG_CONTENT": "secret", "MSWEA_GLOBAL_CONFIG_DIR": "original"}
            environment = child.environment(root, original)
            self.assertEqual(original["HOME"], "original")
            self.assertFalse((set(original) - {"PATH", "HOME", "MSWEA_GLOBAL_CONFIG_DIR"}) & set(environment))
            for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "PI_CODING_AGENT_DIR", "MSWEA_GLOBAL_CONFIG_DIR"):
                self.assertTrue(Path(environment[name]).is_relative_to(root))
                self.assertTrue(Path(environment[name]).is_dir())
            self.assertEqual(environment["OPENCODE_DISABLE_MODELS_FETCH"], "true")
            self.assertEqual(environment["LITELLM_LOCAL_MODEL_COST_MAP"], "True")

    def test_distribution_metadata_probe_does_not_pretend_to_launch_cli(self):
        with probes.task_bench.disposable() as root:
            cli = self.fake(root, "import sys\nassert sys.argv[-1] == '--help'\nprint('--model')")
            version = [sys.executable, "-I", "-c", "print('1.2.3')"]
            report = probes.probe(cli, "1.2.3", ("--model",), 1, version_argv=version)
            self.assertTrue(report["success"], report)
            self.assertEqual(report["invocations"][0]["option"], "distribution-version")

    def test_mini_help_adapter_rejects_tasks_before_importing_client(self):
        result = subprocess.run([sys.executable, "-I", "-B", str(probes.CLIENT_ROOT / "mini_help.py"), "--task", "forbidden"],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"accepts only --help", result.stderr)
        self.assertNotIn(b"ModuleNotFoundError", result.stderr)

    def test_runtime_metadata_requires_an_exact_safe_version(self):
        with probes.task_bench.disposable() as root:
            cli = self.fake(root, "import sys\nprint('1.2.3' if sys.argv[-1] == '--version' else '--model')")
            report = probes.probe(cli, "1.2.3", ("--model",), 1, runtime_name="python")
            self.assertTrue(report["success"], report)
            self.assertEqual(report["runtime"]["version"], '.'.join(str(part) for part in sys.version_info[:3]))
            report = probes.probe(cli, "1.2.3", ("--model",), 1, runtime_name="node")
            self.assertFalse(report["success"])
            self.assertIsNone(report["runtime"]["version"])

    def test_real_child_checks_version_flags_private_home_and_filtered_process_environment(self):
        with probes.task_bench.disposable() as root:
            argv = self.fake(root, """import os, pathlib, sys
assert not os.environ.get('SPRIG_API_KEY')
assert not os.environ.get('OPENAI_API_KEY')
assert pathlib.Path.home() == pathlib.Path.cwd() / 'home'
print('1.2.3' if '--version' in sys.argv else '--model --offline')
""")
            with patch.dict(os.environ, {"SPRIG_API_KEY": "fake-private-marker", "OPENAI_API_KEY": "fake-private-marker"}):
                report = probes.probe(argv, "1.2.3", ("--model", "--offline"), 2)
            self.assertTrue(report["success"], report)
            self.assertEqual([row["runs"] for row in report["invocations"]], [2, 1])
            self.assertNotIn("fake-private-marker", json.dumps(report))

    def test_zero_exit_missing_version_wrong_version_and_missing_flags_fail(self):
        for body in ("", "print('9.9.9')", "print('1.2.3')"):
            with self.subTest(body=body), probes.task_bench.disposable() as root:
                report = probes.probe(self.fake(root, body), "1.2.3", ("--model",), 1)
                self.assertFalse(report["success"])

    def test_failed_output_is_bounded_and_never_republished(self):
        with probes.task_bench.disposable() as root:
            report = probes.probe(self.fake(root, "print('private-marker' * 100000)"), "1.2.3", (), 1)
            self.assertFalse(report["success"])
            self.assertNotIn("private-marker", json.dumps(report))
            self.assertTrue(all(size <= 65536 for row in report["invocations"] for size in row["output_bytes"]))

    def test_existing_report_is_rejected_before_client_launch(self):
        with probes.task_bench.disposable() as root:
            path = root / "existing.json"
            path.write_text("preserve", encoding="utf-8")
            with patch.object(sys, "argv", ["probes", "--report", str(path)]), \
                    patch.object(probes, "run") as run, patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit):
                    probes.main()
                run.assert_not_called()
            self.assertEqual(path.read_text(encoding="utf-8"), "preserve")

    def test_failed_client_and_phase_are_reported_without_raw_exception(self):
        marker = 'private-path-and-credential-marker'
        with patch.object(sys, 'argv', ['probes', '--client', 'sprig', '--repeat', '1']), \
                patch.object(probes, 'invocation', side_effect=PermissionError(marker)), \
                patch('sys.stderr', new_callable=io.StringIO) as stderr:
            self.assertEqual(probes.main(), 1)
        output = stderr.getvalue()
        self.assertIn('client=sprig', output)
        self.assertIn('stage=installation', output)
        self.assertIn('error_type=PermissionError', output)
        self.assertNotIn(marker, output)
        self.assertNotIn(marker, str(probes.ProbeFailure('sprig', 'metadata', PermissionError(marker))))


if __name__ == "__main__":
    unittest.main()
