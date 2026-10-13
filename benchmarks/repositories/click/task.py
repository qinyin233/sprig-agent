"""Identical pinned repository inputs and external acceptance for both clients."""

import hashlib
import importlib.util
import json
from pathlib import Path
import stat
import sys
from types import MappingProxyType

REPO = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("click_edit_fixture", REPO / "scripts/benchmark-edit-repo.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)

TASK = """Improve Click's diagnostics for misspelled group subcommands. Unknown commands
should offer similar available command names while preserving valid commands and
normal help behavior. Provide a public NoSuchCommand exception derived from UsageError.
Use consistent unknown-name diagnostics for commands and options: quote the unknown
name and end the main sentence with a period. A single close name adds
Did you mean 'name'?; multiple close names add (Did you mean one of: 'a', 'b'?).
Keep suggestion ordering deterministic and preserve behavior when no close names exist.
Implement and test the behavior in this repository. Existing files are the fixed
pre-fix snapshot. The application will run fixed external upstream checks and the
public exception contract after you finish; candidate test/config edits cannot change
which acceptance checks are selected. Do not retrieve an upstream solution or weaken
the acceptance checks. Every file write and command requires fresh user approval."""


def snapshot_intact(directory, protected, directories):
    try:
        found = set()
        for visited, path in enumerate(directory.rglob("*"), 1):
            status = path.lstat()
            if (visited > 5000 or stat.S_ISLNK(status.st_mode)
                    or getattr(status, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                    or not path.resolve().is_relative_to(directory)):
                return False
            relative = path.relative_to(directory).as_posix()
            if path.is_file():
                if relative not in protected or path.stat().st_size != len(protected[relative]):
                    return False
                with path.open("rb") as reader:
                    if reader.read(len(protected[relative]) + 1) != protected[relative]:
                        return False
                found.add(relative)
            elif relative not in directories:
                return False
        return found == set(protected)
    except (OSError, RuntimeError):
        return False


class ClickTask:
    def __init__(self, root):
        self.files = MappingProxyType(fixture.load(root, "base"))
        reference = fixture.load(root, "reference")
        # Retain tests/config only. The live adapter does not retain reference
        # implementation bytes or give them to the model or candidate workspace.
        self.oracle = {"upstream/" + name: body for name, body in reference.items()
                       if name.startswith("tests/") or name in {"pyproject.toml", "LICENSE.txt"}}
        self.oracle["oracle.py"] = Path(__file__).with_name("oracle.py").read_bytes()
        self.oracle["known-failures.json"] = json.dumps(sorted(fixture.FAILED_BEFORE)).encode()
        for path in (REPO / "src").rglob("*.py"):
            self.oracle["runner/" + path.relative_to(REPO / "src").as_posix()] = path.read_bytes()
        self.oracle = MappingProxyType(self.oracle)
        self.case = {"id": "click-command-suggestions", "language": "python",
                     "category": "repository-feature", "task": TASK}
        self.metadata = {"protocol": 1, "repository": "pallets/click", "snapshot_files": len(self.files),
                         "base_commit": fixture.PINS["base"][0], "base_sha256": fixture.PINS["base"][2],
                         "acceptance_commit": fixture.PINS["reference"][0],
                         "acceptance_snapshot_sha256": fixture.PINS["reference"][2],
                         "expected_upstream_checks": 642, "public_exception_contract": True,
                         "acceptance_python": "3.12", "acceptance_pytest": "8.4.2",
                         "entry_sha256": hashlib.sha256((REPO / "scripts/benchmark-repository-task.py").read_bytes()).hexdigest(),
                         "adapter_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                         "comparison_lock_sha256": hashlib.sha256((REPO / "benchmarks/repositories/click/uv.lock").read_bytes()).hexdigest(),
                         "task_sha256": hashlib.sha256(TASK.encode()).hexdigest()}

    def bundle(self, parent, case):
        if case != self.case:
            raise ValueError("repository task differs from the fixed contract")
        directory = parent.resolve() / "verification"
        directory.mkdir(exist_ok=False)
        identity = directory.stat()
        fixture.bench.suite.seed(directory, self.oracle)
        directories = {part.as_posix() for name in self.oracle for part in Path(name).parents if str(part) != "."}
        # Monitor the executed sources, not only unused copies of a driver.
        paths = [REPO / name for name in (
            "scripts/benchmark-repository-task.py", "scripts/benchmark-tasks.py", "scripts/benchmark-edit-repo.py",
            "benchmarks/repositories/click/task.py", "benchmarks/repositories/click/oracle.py",
            "benchmarks/repositories/click/uv.lock", "uv.lock")]
        paths.append(REPO / "benchmarks/clients/request_pacing.py")
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
                return snapshot_intact(directory, self.oracle, directories)
            except (OSError, RuntimeError):
                return False
        signature = fixture.bench.suite.digest(self.oracle)
        return (sys.executable, "-I", "-B", str(directory / "oracle.py")), signature, intact
