"""Apply a known Click fix through actual file tools; no model performance claim."""

import argparse
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
import difflib
import gzip
import hashlib
import importlib.util
import inspect
import io
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tarfile
import time
import urllib.request

REPO = Path(__file__).resolve().parents[1]
PINS = {
    'base': ('19fd4d6e18bc9fce451f92f422696b11169faa57',
             '3293779a84ee2b354aed96c1abb3e7b4ca38fe4715df8382e780faf5754ddc95',
             '7d610833c8a6564f980f747e4601e7f4821872702c4a59f32ff9c5d653300d08'),
    'reference': ('831c8f0948af519e45b90801d7430ff25451f972',
                  '36019ac4f17022da3b9a5294446e6a13ab84d58dd18eb03059de658338dc7a16',
                  '62d0162a63fa5322307e29d1710ec7624bd3ca20572329fddbdae1fe628f628d'),
}
MAX_ARCHIVE, MAX_EXPANDED, MAX_FILE = 4 * 1024 * 1024, 16 * 1024 * 1024, 1024 * 1024
FAILED_BEFORE = {
    "tests/test_commands.py::test_suggest_possible_commands[pause-Did you mean 'push'?]",
    "tests/test_commands.py::test_suggest_possible_commands[decline-(Did you mean one of: 'declare', 'refine'?)]",
    'tests/test_options.py::test_unknown_options[--foo]',
    'tests/test_options.py::test_unknown_options[-f]',
    "tests/test_options.py::test_suggest_possible_options[--cat-Did you mean '--count'?]",
    "tests/test_options.py::test_suggest_possible_options[--bounds-(Did you mean one of: '--bound', '--count'?)]",
    "tests/test_options.py::test_suggest_possible_options[--bount-(Did you mean one of: '--bound', '--count'?)]",
}
spec = importlib.util.spec_from_file_location('edit_task_bench', REPO / 'scripts/benchmark-tasks.py')
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


class Discard(io.TextIOBase):
    def write(self, value):
        return len(value)
    def flush(self):
        pass


def unpack(raw, phase):
    commit, archive_hash, snapshot_hash = PINS[phase]
    if len(raw) > MAX_ARCHIVE or hashlib.sha256(raw).hexdigest() != archive_hash:
        raise ValueError('upstream archive identity mismatch')
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
        expanded = compressed.read(MAX_EXPANDED + 1)
    if len(expanded) > MAX_EXPANDED:
        raise ValueError('expanded archive limit exceeded')
    files = {}
    with tarfile.open(fileobj=io.BytesIO(expanded), mode='r:') as archive:
        for visited, member in enumerate(archive, 1):
            parts = member.name.rstrip('/').split('/')
            if (visited > 2048 or not (member.isdir() or member.isfile()) or not parts
                    or parts[0] != 'click-' + commit
                    or any(part in ('', '.', '..') or '\\' in part or ':' in part for part in parts)
                    or not 0 <= member.size <= MAX_FILE):
                raise ValueError('unsupported upstream archive entry')
            relative = '/'.join(parts[1:])
            if member.isfile() and (relative.startswith(('src/', 'tests/'))
                                    or relative in ('LICENSE.txt', 'pyproject.toml', 'README.md')):
                with archive.extractfile(member) as reader:
                    body = reader.read(member.size + 1)
                if len(body) != member.size or relative in files:
                    raise ValueError('upstream archive entry changed or repeated')
                files[relative] = body
    if bench.suite.digest(files) != snapshot_hash:
        raise ValueError('upstream snapshot identity mismatch')
    return files


def fetch(destination):
    from sprig_agent.provider import NoRedirect
    # Read and verify both archives before creating the requested new directory.
    if destination.exists() or destination.is_symlink() or not destination.parent.is_dir():
        raise ValueError('fixture destination must be new, with an existing parent')
    snapshots = {}
    opener = urllib.request.build_opener(NoRedirect())
    for phase, (commit, _, _) in PINS.items():
        with opener.open('https://codeload.github.com/pallets/click/tar.gz/' + commit, timeout=30) as response:
            snapshots[phase] = unpack(response.read(MAX_ARCHIVE + 1), phase)
    destination.mkdir(mode=0o700)
    for phase, files in snapshots.items():
        bench.suite.seed(destination / phase, files)
    return {'scope': 'pinned upstream source/test snapshots; no commands or model run',
            'snapshots': {phase: {'commit': PINS[phase][0], 'sha256': bench.suite.digest(files),
                                  'files': len(files), 'bytes': sum(map(len, files.values()))}
                          for phase, files in snapshots.items()}}


def load(root, phase):
    directory = root / phase
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('fixture directory must be a regular directory')
    files, total = {}, 0
    for visited, path in enumerate(directory.rglob('*'), 1):
        if visited > 2048 or path.is_symlink():
            raise ValueError('unsupported fixture inventory')
        if path.is_file():
            size = path.stat().st_size
            if size > MAX_FILE or total + size > 2 * MAX_FILE:
                raise ValueError('fixture inventory too large')
            with path.open('rb') as reader:
                body = reader.read(size + 1)
            if len(body) != size:
                raise ValueError('fixture changed during read')
            files[path.relative_to(directory).as_posix()] = body
            total += size
    if bench.suite.digest(files) != PINS[phase][2]:
        raise ValueError('fixture snapshot differs from the upstream pin')
    return files


def replacements(base, reference):
    result = {}
    for name, body in sorted(base.items()):
        if not name.startswith('src/') or body == reference.get(name):
            continue
        before, after = body.decode('utf-8'), reference[name].decode('utf-8')
        left, right = before.splitlines(keepends=True), after.splitlines(keepends=True)
        groups = difflib.SequenceMatcher(None, left, right, autojunk=False).get_grouped_opcodes(3)
        edits = [{'old': ''.join(left[group[0][1]:group[-1][2]]),
                  'new': ''.join(right[group[0][3]:group[-1][4]])} for group in groups]
        if any(not edit['old'] or before.count(edit['old']) != 1 for edit in edits):
            raise ValueError('reference chunks are not unique in original source')
        result[name] = edits
    return result


def line_replacements(base, reference):
    result = {}
    for name, body in sorted(base.items()):
        if not name.startswith('src/') or body == reference.get(name):
            continue
        left = list(io.StringIO(body.decode('utf-8'), newline=''))
        right = list(io.StringIO(reference[name].decode('utf-8'), newline=''))
        changes = [{'start_line': start + 1, 'end_line': end, 'new': ''.join(right[first:last])}
                   for kind, start, end, first, last in difflib.SequenceMatcher(None, left, right, autojunk=False).get_opcodes()
                   if kind != 'equal']
        result[name] = changes
    return result


def verify(candidate, fixture):
    # Match upstream's normal module import mode. Importlib mode changes class
    # reprs and incorrectly fails six unrelated option tests in this revision.
    sys.path.insert(0, str(candidate / 'src'))
    class Counts:
        def __init__(self):
            self.collected = self.passed = self.skipped = 0
            self.failed = []
        def pytest_collection_finish(self, session):
            self.collected = len(session.items)
        def pytest_runtest_logreport(self, report):
            if report.failed:
                self.failed.append(report.nodeid)
            elif report.skipped:
                self.skipped += 1
            elif report.when == 'call' and report.passed:
                self.passed += 1
    count = Counts()
    with redirect_stdout(Discard()), redirect_stderr(Discard()):
        import click
        import pytest
        if Path(click.__file__).resolve() != (candidate / 'src/click/__init__.py').resolve():
            raise ValueError('acceptance imported the wrong Click source')
        code = pytest.main(['-c', str(fixture / 'reference/pyproject.toml'), '-p', 'no:cacheprovider',
                            '--import-mode=prepend', '-q', str(fixture / 'reference/tests/test_commands.py'),
                            str(fixture / 'reference/tests/test_options.py')], plugins=[count])
    return {'collected': count.collected, 'passed': count.passed, 'skipped': count.skipped,
            'failed': count.failed, 'returncode': int(code)}


def source_digest(source):
    return bench.suite.digest({path.relative_to(source).as_posix(): path.read_bytes()
                               for path in source.rglob('*.py')})


def measure(source, fixture, repeat, *, line_edits=False):
    # benchmark-tasks imports current classes during module setup; isolate
    # actual revision modules before measuring each trusted source snapshot.
    for name in list(sys.modules):
        if name == 'sprig_agent' or name.startswith('sprig_agent.'):
            del sys.modules[name]
    sys.path.insert(0, str(source))
    from sprig_agent.config import Config
    from sprig_agent.tools import Tools
    base, reference = load(fixture, 'base'), load(fixture, 'reference')
    edits = line_replacements(base, reference) if line_edits else replacements(base, reference)
    expected = {**base, **{name: reference[name] for name in edits}}
    rows = []
    applied_acceptance = None
    for run_index in range(repeat):
        with bench.disposable() as parent:
            workspace = parent / 'workspace'
            bench.suite.seed(workspace, base)
            approved = []
            tools = Tools(Config(workspace, 'offline', max_output=1000000, command_output_limit=1000000), lambda prompt: approved.append(True) or True)
            multiple = 'edits' in inspect.signature(tools.edit_file).parameters
            calls, elapsed, payload = 0, 0.0, 0
            read_calls, read_ms, read_bytes = 0, 0.0, 0
            try:
                for name, chunks in edits.items():
                    if line_edits:
                        start = time.perf_counter()
                        observed = tools.execute('read_file', {'path': name, 'offset': 1, 'limit': 10000})
                        read_ms += (time.perf_counter() - start) * 1000
                        read_calls += 1
                        read_bytes += len(json.dumps(observed, ensure_ascii=False).encode())
                        if observed.get('error') or observed.get('truncated') or not observed.get('sha256'):
                            raise ValueError('revision read was incomplete')
                        arguments = [{'path': name, 'sha256': observed['sha256'], 'line_edits': chunks}]
                    else:
                        arguments = ([{'path': name, 'edits': chunks}] if multiple else
                                     [{'path': name, **chunk} for chunk in reversed(chunks)])
                    for argument in arguments:
                        start = time.perf_counter()
                        result = tools.execute('edit_file', argument)
                        elapsed += (time.perf_counter() - start) * 1000
                        calls += 1
                        payload += len(json.dumps(argument, ensure_ascii=False, separators=(',', ':')).encode())
                        if not result.get('approved') or not result.get('changed') or result.get('error'):
                            raise ValueError('known reference edit was not applied')
                actual = {name: (workspace / name).read_bytes() for name in base}
                if actual != expected:
                    raise ValueError('file tools did not reproduce the exact reference source')
            finally:
                tools.close()
            rows.append({'edit_calls': calls, 'approvals': len(approved), 'argument_bytes': payload,
                         'revision_read_calls': read_calls, 'revision_read_ms': round(read_ms, 3),
                         'revision_read_result_bytes': read_bytes,
                         'tool_ms': round(elapsed, 3), 'result_sha256': bench.suite.digest(actual)})
            if run_index == repeat - 1:
                applied_acceptance = verify(workspace, fixture)
                if applied_acceptance != {'collected': 642, 'passed': 642, 'skipped': 0, 'failed': [], 'returncode': 0}:
                    raise ValueError('actual file-tool result failed upstream acceptance')
    return {'scope': 'known patch on disposable inputs; no model, no user waiting time, no coding success score',
            'multi_edit': multiple, 'line_edits': line_edits, 'source_sha256': source_digest(source), 'runs': rows,
            'median_tool_ms': round(statistics.median(row['tool_ms'] for row in rows), 3),
            'max_tool_ms': max(row['tool_ms'] for row in rows), 'files_changed': len(edits),
            'hunks': sum(map(len, edits.values())), 'reference_bytes_exact': True,
            'applied_acceptance': applied_acceptance}


def worker(fixture, *, source=None, repeat=1, candidate=None, line_edits=False):
    from sprig_agent.config import Config
    from sprig_agent.tools import Tools
    with bench.disposable() as parent:
        (parent / 'profile').mkdir(mode=0o700)
        argv = [sys.executable, '-I', '-B', str(Path(__file__).resolve()), '--worker', '--fixture', str(fixture)]
        if line_edits:
            argv.append('--line-edits')
        if candidate:
            argv += ['--verify', str(candidate)]
        else:
            argv += ['--source', str(source), '--repeat', str(repeat)]
        # Only this fixed validation worker is launched, on pinned disposable
        # inputs. No model is present and no product auto-approval mode is added.
        tools = Tools(Config(parent, 'offline', timeout=120, max_output=65536, command_output_limit=65536), lambda _: True)
        try:
            relay = REPO / 'benchmarks/clients/probe_child.py'
            result = tools.run([sys.executable, '-I', '-B', str(relay), str(parent / 'profile'), *argv], timeout=120)
        finally:
            tools.close()
        if result.get('returncode') != 0 or result.get('timed_out') or result.get('output_limited'):
            raise ValueError('bounded benchmark worker failed')
        return json.loads(result['output'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--fetch', type=Path, help='download fixed public snapshots to a new directory; no code execution')
    mode.add_argument('--fixture', type=Path, help='existing pinned fixture snapshots; no network requests during measurement')
    parser.add_argument('--repeat', type=int, default=11)
    parser.add_argument('--compare-ref', help='trusted local Git revision')
    parser.add_argument('--line-edits', action='store_true', help='also measure revision-pinned line edits of the same known patch; no model')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--source', type=Path, default=REPO / 'src', help=argparse.SUPPRESS)
    parser.add_argument('--verify', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.repeat <= 50:
        parser.error('repeat must be between 1 and 50')
    if args.report and (args.report.exists() or args.report.is_symlink() or not args.report.parent.is_dir()):
        parser.error('report must be a new file with an existing parent')
    try:
        if args.worker:
            os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
            result = verify(args.verify, args.fixture) if args.verify else measure(args.source, args.fixture, args.repeat, line_edits=args.line_edits)
        elif args.fetch:
            result = fetch(args.fetch)
        else:
            from importlib.metadata import version
            base, reference = load(args.fixture, 'base'), load(args.fixture, 'reference')
            with bench.disposable() as parent:
                baseline, accepted = parent / 'baseline', parent / 'accepted'
                bench.suite.seed(baseline, base)
                expected = {**base, **{name: reference[name] for name in replacements(base, reference)}}
                bench.suite.seed(accepted, expected)
                initial, known = worker(args.fixture, candidate=baseline), worker(args.fixture, candidate=accepted)
                if (initial['collected'] != 642 or initial['passed'] != 635 or initial['skipped']
                        or initial['returncode'] != 1 or len(initial['failed']) != 7 or set(initial['failed']) != FAILED_BEFORE):
                    raise ValueError('baseline acceptance calibration differs from expected cases')
                if known != {'collected': 642, 'passed': 642, 'skipped': 0, 'failed': [], 'returncode': 0}:
                    raise ValueError('known reference failed upstream acceptance')
                result = {'scope': 'Click issue 3107 / accepted PR 3228; selected upstream tests; no live agent score',
                          'recorded_at_utc': datetime.now(timezone.utc).isoformat(),
                          'revision': subprocess.check_output(['git', '-C', str(REPO), 'rev-parse', 'HEAD']).decode().strip(),
                          'dirty': bool(subprocess.check_output(['git', '-C', str(REPO), 'status', '--porcelain'])),
                          'python': platform.python_version(), 'platform': platform.platform(),
                          'dependencies': {name: version(name) for name in ('pytest', 'textual', 'tree-sitter')},
                          'uv_lock_sha256': hashlib.sha256((REPO / 'benchmarks/repositories/click/uv.lock').read_bytes()).hexdigest(),
                          'driver_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                          'fixture_sha256': PINS['base'][2], 'reference_sha256': PINS['reference'][2],
                          'base_commit': PINS['base'][0], 'reference_commit': PINS['reference'][0],
                          'acceptance': {'base': initial, 'reference': known},
                          'current': worker(args.fixture, source=REPO / 'src', repeat=args.repeat)}
                if args.line_edits:
                    result['revision_line_edits'] = worker(args.fixture, source=REPO / 'src', repeat=args.repeat, line_edits=True)
                if args.compare_ref:
                    # Extract regular source files from this trusted revision.
                    sha = subprocess.check_output(['git', '-C', str(REPO), 'rev-parse', '--verify', '--end-of-options',
                                                   args.compare_ref + '^{commit}']).decode().strip()
                    raw = subprocess.check_output(['git', '-C', str(REPO), 'archive', '--format=tar', sha, 'src'])
                    source = parent / 'baseline-source'
                    source.mkdir()
                    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
                        for visited, member in enumerate(archive, 1):
                            parts = member.name.split('/')
                            if visited > 2048 or not member.isdir() and not member.isfile():
                                raise ValueError('unsupported source archive')
                            if not member.isfile():
                                continue
                            if parts[0] != 'src' or any(p in ('', '.', '..') or '\\' in p or ':' in p for p in parts):
                                raise ValueError('unsafe source archive path')
                            if member.size > MAX_FILE:
                                raise ValueError('source archive entry too large')
                            with archive.extractfile(member) as reader:
                                body = reader.read(member.size + 1)
                            target = source.joinpath(*parts)
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_bytes(body)
                    result['baseline'] = {'revision': sha, **worker(args.fixture, source=source / 'src', repeat=args.repeat)}
        rendered = json.dumps(result, ensure_ascii=True, indent=2) + '\n'
        print(rendered, end='')
        if args.report:
            with args.report.open('x', encoding='utf-8', newline='\n') as target:
                target.write(rendered)
        return 0
    except KeyboardInterrupt:
        print('Pinned repository benchmark cancelled.', file=sys.stderr)
        return 130
    except Exception:
        print('Pinned repository benchmark failed; raw exceptions and subprocess output are not published.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
