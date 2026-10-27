"""Acceptance integrity and input separation without downloaded fixtures or models."""

from contextlib import ExitStack, closing, redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("requests_task_entry_tests", ROOT / "scripts/benchmark-requests-task.py")
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)
spec = importlib.util.spec_from_file_location("requests_oracle_tests", ROOT / "benchmarks/repositories/requests/oracle.py")
oracle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oracle)


def archive(files, *, link=None):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as writer:
        for name, body in files.items():
            info = tarfile.TarInfo("requests-fixture/" + name)
            info.size = len(body)
            writer.addfile(info, io.BytesIO(body))
        if link:
            info = tarfile.TarInfo("requests-fixture/" + link)
            info.type, info.linkname = tarfile.SYMTYPE, "../outside"
            writer.addfile(info)
    return output.getvalue()


class RequestsBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.handles = self.enterContext(ExitStack())
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.parent = Path(temporary).resolve()
        self.fixture = self.parent / "fixture"
        if os.name == "nt":
            from sprig_agent.windows import private_directory
            private_directory(self.fixture)
        else:
            self.fixture.mkdir(mode=0o700)
        self.files = {"base": {"src/requests/__init__.py": b"VALUE='BASE'\n", "LICENSE": b"fixture license\n"},
                      "reference": {"src/requests/__init__.py": b"REFERENCE_IMPLEMENTATION_ONLY\n", "LICENSE": b"fixture license\n",
                                    "tests/test_requests.py": b"def test_json_decode_errors_are_serializable_deserializable():\n    pass\n"}}
        self.archives = {phase: archive(files) for phase, files in self.files.items()}
        pins = {phase: ("a" * 40, hashlib.sha256(self.archives[phase]).hexdigest(), entry.bench.suite.digest(files))
                for phase, files in self.files.items()}
        self.enterContext(patch.object(entry.task_module, "PINS", pins))
        from sprig_agent.filesystem import Directory
        with Directory(self.fixture) as directory:
            for phase, raw in self.archives.items():
                directory.write_new(phase + ".tar.gz", raw, private=True)

    def test_archive_bytes_and_expanded_digest_must_both_match(self):
        self.assertEqual(entry.task_module.load(self.fixture, "base"), self.files["base"])
        with self.assertRaises(ValueError):
            entry.task_module.unpack(self.archives["base"] + b"changed", "base")
        commit, digest, _ = entry.task_module.PINS["base"]
        with patch.dict(entry.task_module.PINS, base=(commit, digest, "0" * 64)), self.assertRaises(ValueError):
            entry.task_module.unpack(self.archives["base"], "base")

    def test_traversal_links_and_size_caps_are_rejected_before_extraction(self):
        for files, link in (({"../escape": b"data"}, None), ({}, "src/requests/link")):
            raw = archive(files, link=link)
            with self.subTest(link=link), patch.dict(entry.task_module.PINS,
                    base=("a" * 40, hashlib.sha256(raw).hexdigest(), entry.bench.suite.digest(files))):
                with self.assertRaises(ValueError):
                    entry.task_module.unpack(raw, "base")
        with patch.object(entry.task_module, "MAX_ARCHIVE", 1), self.assertRaises(ValueError):
            entry.task_module.unpack(self.archives["base"], "base")
        with patch.object(entry.task_module, "MAX_FILE", 1), self.assertRaises(ValueError):
            entry.task_module.unpack(self.archives["base"], "base")
        self.assertFalse((self.parent / "escape").exists())

    def test_adapter_separates_reference_implementation_and_detects_verifier_changes(self):
        adapter = entry.task_module.RequestsTask(self.fixture)
        self.assertEqual(dict(adapter.files), self.files["base"])
        self.assertFalse(any(b"REFERENCE_IMPLEMENTATION_ONLY" in body for body in adapter.oracle.values()))
        target = self.parent / "prepared"
        result = entry.prepare(target, adapter)
        self.assertEqual((target / "workspace/src/requests/__init__.py").read_bytes(), b"VALUE='BASE'\n")
        self.assertEqual(result["repository_task"]["snapshot_files"], 2)
        second = self.parent / "second"
        second.mkdir()
        _, _, intact = adapter.bundle(second, adapter.case)
        self.assertTrue(intact())
        (second / "verification/empty").mkdir()
        self.assertFalse(intact())
        (second / "verification/empty").rmdir()
        self.assertTrue(intact())
        (second / "verification/oracle.py").write_bytes(b"changed\n")
        self.assertFalse(intact())

    def test_prepared_tasks_and_fetches_never_overwrite_existing_directories(self):
        with self.assertRaises(ValueError):
            entry.new_directory(self.fixture)
        with self.assertRaises(ValueError):
            entry.task_module.fetch(self.fixture)
        with self.assertRaises(ValueError):
            entry.new_directory(ROOT / "should-not-create-requests-fixture")

    def test_active_runtime_source_change_or_alias_invalidates_acceptance(self):
        adapter = entry.task_module.RequestsTask(self.fixture)
        target = self.parent / "active-source"
        target.mkdir()
        _, _, intact = adapter.bundle(target, adapter.case)
        runtime = ROOT / "src/sprig_agent/agent.py"
        original = runtime.read_bytes()
        real_open, real_resolve = Path.open, Path.resolve
        def changed_open(path, *args, **kwargs):
            if path == runtime:
                return io.BytesIO(bytes([original[0] ^ 1]) + original[1:])
            return real_open(path, *args, **kwargs)
        # Simulate an equal-sized runtime write without modifying shared source.
        with patch.object(Path, "open", changed_open):
            self.assertFalse(intact())
        def redirected(path, *args, **kwargs):
            return runtime.with_name("replacement.py") if path == runtime else real_resolve(path, *args, **kwargs)
        with patch.object(Path, "resolve", redirected):
            self.assertFalse(intact())
        self.assertTrue(intact())

    def test_fetch_uses_fixed_public_urls_and_private_files(self):
        target = self.parent / "downloaded"
        with patch.object(entry.task_module.urllib.request, "urlopen", side_effect=lambda url, **kw: io.BytesIO(self.archives["base"])) as opened:
            # Both synthetic pins use the same SHA; use equal bytes too.
            with patch.dict(entry.task_module.PINS, reference=entry.task_module.PINS["base"]):
                entry.task_module.fetch(target)
        self.assertEqual(opened.call_count, 2)
        for call in opened.call_args_list:
            self.assertEqual(call.args[0], "https://codeload.github.com/psf/requests/tar.gz/" + "a" * 40)
        self.assertEqual(entry.task_module.load(target, "base"), self.files["base"])

    def good_worker(self, backend="stdlib", passed=True):
        failed = [] if passed else sorted(oracle.IDS[:1])
        data = {"schema": 1, "backend": backend, "collected": 29, "checks_passed": 29 - len(failed),
                "failed": failed, "skipped": 0, "passed": passed}
        return {"returncode": 0 if passed else 1, "output": json.dumps(data)}

    def test_worker_success_requires_exact_typed_complete_results(self):
        self.assertTrue(oracle.validate_worker(self.good_worker(), "stdlib")["passed"])
        values = [dict(self.good_worker(), output=""), dict(self.good_worker(), output="candidate text\n{}"),
                  dict(self.good_worker(), output='{"schema":1,"schema":1}'),
                  dict(self.good_worker(), returncode=True), dict(self.good_worker(), output_limited=True)]
        for field, value in (("collected", True), ("checks_passed", 28), ("skipped", 1),
                             ("failed", ["unknown private text"]), ("backend", "other"), ("passed", "yes")):
            item = self.good_worker()
            data = json.loads(item["output"])
            data[field] = value
            item["output"] = json.dumps(data)
            values.append(item)
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                oracle.validate_worker(value, "stdlib")

    def test_calibration_rejects_a_different_baseline_failure_pattern(self):
        adapter = entry.task_module.RequestsTask(self.fixture)
        baseline = {"schema": 1, "valid": True, "passed": False, "collected": 58, "checks_passed": 56,
                    "skipped": 0, "backends": [json.loads(self.good_worker(name, False)["output"]) for name in ("stdlib", "simplejson")]}
        reference = {"schema": 1, "valid": True, "passed": True, "collected": 58, "checks_passed": 58,
                     "skipped": 0, "backends": [json.loads(self.good_worker(name)["output"]) for name in ("stdlib", "simplejson")]}
        with patch.object(entry, "Tools") as tools:
            tools.return_value.run.side_effect = [dict(returncode=1, output=json.dumps(baseline)), dict(returncode=0, output=json.dumps(reference))]
            with self.assertRaises(ValueError):
                entry.self_test(adapter, self.fixture)

    def test_invalid_generation_controls_stop_before_loading_fixtures_or_credentials(self):
        for option, value in (("--max-tokens", "-1"), ("--request-timeout", "nan"), ("--extra-body", '{"stream":true}')):
            with self.subTest(option=option), patch.object(sys, "argv", ["requests-task", "--run", "--fixture", str(self.fixture), option, value]), \
                    patch.object(entry.sys, "version_info", (3, 12)), patch.object(entry.task_module, "RequestsTask") as adapter, \
                    patch.object(entry, "environment_api_key") as key, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(entry.main(), 1)
                adapter.assert_not_called()
                key.assert_not_called()

    def test_mini_comparison_rejects_invalid_environment_lock_before_sdk_access(self):
        mini = entry.load_mini()
        invalid = self.fixture / "private-config.toml"
        invalid.write_text("fixture data", encoding="utf-8")
        with patch.object(mini, "private_process") as private, self.assertRaises(ValueError):
            mini.run_case({}, {"model": "fixture"}, environment_lock=invalid)
        private.assert_not_called()

    def worker_probe(self, code, *, inner_timeout=3, limit=65536):
        probe = self.parent / "probe.py"
        probe.write_text("import json,os,sys\nfrom pathlib import Path\nsys.path.insert(0," + repr(str(ROOT / "benchmarks/repositories")) + ")\n"
                         "from supervised_worker import run_worker\n"
                         "result=run_worker([sys.executable,'-I','-B','-c'," + repr(code) + "],env=dict(os.environ),timeout="
                         + repr(inner_timeout) + ",max_output=" + repr(limit) + ")\nprint(json.dumps(result))\n", encoding="utf-8")
        return [sys.executable, "-I", "-B", str(probe)]

    def grandchild_code(self):
        ready = self.parent / "descendant.ready"
        child = "from pathlib import Path; import os,time; p=Path(" + repr(str(ready)) + "); t=p.with_suffix('.tmp'); t.write_text(str(os.getpid())); t.replace(p); time.sleep(15)"
        return ready, "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); time.sleep(15)"

    def assert_descendant_stopped(self, ready):
        from tests.test_mcp_stdio import running
        self.assertTrue(ready.exists(), "fixture descendant never started")
        pid = int(ready.read_text())
        deadline = time.monotonic() + 3
        while running(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(running(pid), "fixed worker descendant survived the approved command")

    def test_fixed_worker_capture_rejects_overflow_and_invalid_utf8(self):
        from sprig_agent.config import Config
        from sprig_agent.tools import Tools
        for code, flag in (("import os; os.write(1,b'x'*10000)", "output_limited"),
                           ("import os; os.write(1,b'\\xff')", "error")):
            with self.subTest(flag=flag), closing(Tools(Config(self.parent, "fixture", timeout=6), lambda _: True)) as tools:
                result = tools.run(self.worker_probe(code, limit=256))
                self.assertEqual(result["returncode"], 0, result)
                captured = json.loads(result["output"])
                self.assertTrue(captured[flag], captured)
                self.assertLessEqual(len(captured["output"].encode()), 256)

    def test_fixed_worker_inner_timeout_reaps_ordinary_descendants(self):
        from sprig_agent.config import Config
        from sprig_agent.tools import Tools
        ready, code = self.grandchild_code()
        with closing(Tools(Config(self.parent, "fixture", timeout=6), lambda _: True)) as tools:
            result = tools.run(self.worker_probe(code, inner_timeout=1))
        self.assertTrue(json.loads(result["output"])["timed_out"], result)
        self.assert_descendant_stopped(ready)

    def test_fixed_worker_outer_timeout_reaps_ordinary_descendants(self):
        from sprig_agent.config import Config
        from sprig_agent.tools import Tools
        ready, code = self.grandchild_code()
        with closing(Tools(Config(self.parent, "fixture", timeout=2), lambda _: True)) as tools:
            result = tools.run(self.worker_probe(code, inner_timeout=10))
        self.assertTrue(result["timed_out"], result)
        self.assert_descendant_stopped(ready)

    def test_fixed_worker_outer_cancellation_reaps_ordinary_descendants(self):
        from sprig_agent.config import Config
        from sprig_agent.tools import Tools
        ready, code = self.grandchild_code()
        stop = threading.Event()
        def cancel_when_started():
            deadline = time.monotonic() + 4
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            stop.set()
        timer = threading.Thread(target=cancel_when_started, daemon=True)
        timer.start()
        with closing(Tools(Config(self.parent, "fixture", timeout=6), lambda _: True, stop)) as tools:
            with self.assertRaises(KeyboardInterrupt):
                tools.run(self.worker_probe(code, inner_timeout=10))
        timer.join(5)
        self.assert_descendant_stopped(ready)
