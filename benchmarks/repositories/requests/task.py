"""Pinned Requests task; reference implementation never enters live inputs."""

import ast
import hashlib
import importlib.util
from importlib.metadata import PackageNotFoundError, version
import io
import json
from pathlib import Path, PurePosixPath
import stat
import sys
import tarfile
from types import MappingProxyType
import urllib.request

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
spec = importlib.util.spec_from_file_location("requests_common_repository", REPO / "benchmarks/repositories/click/task.py")
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)
bench = common.fixture.bench
PINS = {
    "base": ("96b22fa18c00831656ee4b286bf1c9062459b00a", "b9a1f599c259a4e29edeefb0a0690a31c89a4d038212bd44ea9b9d8321908ca5", "1e92afeb2bca849b3dfa91a1d604c43fc2d960a2b2736db18a1edb062b2de0a2"),
    "reference": ("3ff3ff21dd45957c9e143cd500291959bb15f690", "7df06fd63752aeaf6e0bed7d2f31d97383bebec3cf19694dbe26d92f9153fdc5", "8cf248a17d3c52edb0a25bc79f050cda83891a3fd3aa106089e122c6b16e707b"),
}
MAX_ARCHIVE, MAX_FILE, MAX_EXPANDED = 8 * 1024 * 1024, 4 * 1024 * 1024, 64 * 1024 * 1024
TASK = """Fix Requests JSON decoding exception serialization. An invalid JSON response
processed in a process pool must reach its caller as requests.exceptions.JSONDecodeError,
without breaking the pool or preventing later valid work. A pickle round trip must
preserve the exception type, message, document, position and formatting. Preserve
existing exception inheritance and normal request/JSON behavior with both the standard
json backend and simplejson installed. Diagnose and change library code. Do not change
the external acceptance, dependency environment, or skip checks. Use the configured
fixed acceptance command to verify completion. No network server is needed to reproduce.
"""


def selected(name):
    return name.startswith(("src/requests/", "tests/")) or name in {
        "README.md", "LICENSE", "NOTICE", "HISTORY.md", "pyproject.toml", "setup.py", "setup.cfg", "requirements-dev.txt"}


def unpack(data, phase):
    if phase not in PINS or len(data) > MAX_ARCHIVE or hashlib.sha256(data).hexdigest() != PINS[phase][1]:
        raise ValueError("Requests source archive differs from the fixed pin")
    files, total = {}, 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for count, member in enumerate(archive, 1):
            parts = PurePosixPath(member.name).parts
            if count > 10000 or not parts or PurePosixPath(member.name).is_absolute() or ".." in parts:
                raise ValueError("unsafe or oversized source archive")
            name = "/".join(parts[1:])
            if not selected(name):
                continue
            if member.isdir():
                continue
            if not member.isfile() or name in files or not 0 <= member.size <= MAX_FILE:
                raise ValueError("unsupported source archive member")
            body = archive.extractfile(member).read(member.size + 1)
            total += len(body)
            if len(body) != member.size or total > MAX_EXPANDED:
                raise ValueError("source archive exceeds its expanded limit")
            files[name] = body
    if bench.suite.digest(files) != PINS[phase][2]:
        raise ValueError("Requests source snapshot differs from the fixed digest")
    return files


def load(root, phase):
    from sprig_agent.filesystem import Directory
    with Directory(root) as directory:
        directory.check_private()
        return unpack(directory.read(phase + ".tar.gz", MAX_ARCHIVE, private=True), phase)


def fetch(root):
    from sprig_agent.filesystem import Directory
    root = Path(root).absolute()
    if root.exists() or root.is_symlink() or not root.parent.is_dir() or root.resolve().is_relative_to(REPO):
        raise ValueError("source fixture must be a new external directory with an existing parent")
    if sys.platform == "win32":
        from sprig_agent.windows import private_directory
        private_directory(root)
    else:
        root.mkdir(mode=0o700)
    with Directory(root) as directory:
        directory.check_private()
        for phase, (commit, _, _) in PINS.items():
            with urllib.request.urlopen("https://codeload.github.com/psf/requests/tar.gz/" + commit, timeout=60) as response:
                data = response.read(MAX_ARCHIVE + 1)
            unpack(data, phase)
            directory.write_new(phase + ".tar.gz", data, private=True)
    return {"scope": "downloaded fixed public source; no candidate command or model request", "source_pins": PINS}


class RequestsTask:
    def __init__(self, root):
        self.files = MappingProxyType(load(root, "base"))
        reference = load(root, "reference")
        source = reference["tests/test_requests.py"].decode("utf-8")
        tree = ast.parse(source)
        matches = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                   and node.name == "test_json_decode_errors_are_serializable_deserializable"]
        if len(matches) != 1:
            raise ValueError("fixed upstream regression is missing")
        regression = ast.get_source_segment(source, matches[0]).encode("utf-8") + b"\n"
        oracle = {"upstream-regression.py": regression, "LICENSE.requests": reference["LICENSE"],
                  "oracle.py": Path(__file__).with_name("oracle.py").read_bytes(),
                  "supervised_worker.py": Path(__file__).parents[1].joinpath("supervised_worker.py").read_bytes()}
        for path in (REPO / "src").rglob("*.py"):
            oracle["runner/" + path.relative_to(REPO / "src").as_posix()] = path.read_bytes()
        self.oracle = MappingProxyType(oracle)
        self.case = {"id": "requests-json-error-pickling", "language": "python", "category": "repository-bug", "task": TASK}
        self.metadata = {"protocol": 1, "repository": "psf/requests", "issue": 6628, "reference_pr": 6629,
            "base_commit": PINS["base"][0], "base_sha256": PINS["base"][2],
            "acceptance_commit": PINS["reference"][0], "acceptance_snapshot_sha256": PINS["reference"][2],
            "snapshot_files": len(self.files), "expected_checks": 58, "backend_checks": 29,
            "backends": ["stdlib", "simplejson"], "task_sha256": hashlib.sha256(TASK.encode()).hexdigest(),
            "adapter_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "comparison_lock_sha256": hashlib.sha256(Path(__file__).with_name("uv.lock").read_bytes()).hexdigest()}
        versions = {}
        for name in ("sprig-agent", "simplejson", "requests", "mini-swe-agent", "litellm", "openai", "pytest"):
            try:
                versions[name] = version(name)
            except PackageNotFoundError:
                versions[name] = None
        self.metadata["comparison_environment_versions"] = versions

    def bundle(self, parent, case):
        if case != self.case:
            raise ValueError("repository task differs from the fixed contract")
        directory = parent.resolve() / "verification"
        directory.mkdir(exist_ok=False)
        identity = directory.stat()
        bench.suite.seed(directory, self.oracle)
        protected = dict(self.oracle)
        directories = {path.parent.as_posix() for path in map(PurePosixPath, protected)}
        directories |= {str(parent) for name in protected for parent in PurePosixPath(name).parents}
        paths = [REPO / name for name in (
            "scripts/benchmark-requests-task.py", "scripts/benchmark-tasks.py", "scripts/benchmark-edit-repo.py",
            "benchmarks/repositories/requests/task.py", "benchmarks/repositories/requests/oracle.py",
            "benchmarks/repositories/requests/uv.lock", "benchmarks/repositories/requests/pyproject.toml",
            "benchmarks/repositories/click/task.py", "benchmarks/repositories/supervised_worker.py",
            "benchmarks/coding/suite.py", "benchmarks/clients/request_pacing.py", "benchmarks/clients/tool_diagnostics.py",
            "pyproject.toml", "uv.lock")]
        paths.extend((REPO / "src").rglob("*.py"))
        guards = {path: (path.resolve(), path.read_bytes()) for path in paths}
        def intact():
            try:
                current = directory.lstat()
                if (directory.resolve() != directory or not stat.S_ISDIR(current.st_mode)
                        or getattr(current, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                        or (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino)):
                    return False
                for path, (original, body) in guards.items():
                    if path.is_symlink() or path.resolve() != original or path.stat().st_size != len(body):
                        return False
                    with path.open("rb") as reader:
                        if reader.read(len(body) + 1) != body:
                            return False
                return common.snapshot_intact(directory, protected, directories)
            except (OSError, RuntimeError):
                return False
        return (sys.executable, "-I", "-B", str(directory / "oracle.py")), bench.suite.digest(protected), intact
