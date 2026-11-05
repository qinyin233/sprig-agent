"""Approved Git observations using disposable index and reference metadata."""

from contextlib import ExitStack
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import stat
import tempfile

from .config import redact
from .policy import KEY_PATTERN
from .worktrees import COMMIT, _PinnedDirectory, _identity, _safe_path


MAX_INDEX = 16 * 1024 * 1024
MAX_REF = 4096
MAX_PACKED_REFS = 4 * 1024 * 1024
STATUS = re.compile(r"(?:[ MADTUC]{2}|\?\?)\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
PATCH_RECORD = re.compile(r":(000000|100644|100755) (000000|100644|100755) [0-9a-f]+ [0-9a-f]+ [ADM]\Z")


def diff_arguments(argument):
    if argument.startswith("{"):
        parsed = json.loads(argument)
        if not isinstance(parsed, dict) or "path" not in parsed:
            raise ValueError("Git diff page arguments require a path")
        return parsed
    staged = argument.startswith("--staged ")
    return {"path": argument.removeprefix("--staged ") if staged else argument, "staged": staged}


def checkout_available(workspace):
    for root in (workspace, *workspace.parents):
        try:
            info = (root / ".git").lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return False
        return not getattr(info, "st_file_attributes", 0) & 0x400 and (
            stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
    return False


def _read(directory, name, limit):
    try:
        raw = directory.read(name, limit)
    except FileNotFoundError:
        return None
    if len(raw) > limit:
        raise ValueError("Git observation metadata exceeds its byte limit")
    return raw


def _read_ref(common, head):
    if head is None:
        raise ValueError("Git observation could not find HEAD")
    try:
        value = head.decode("utf-8").rstrip("\r\n")
    except UnicodeDecodeError:
        raise ValueError("Git HEAD is not supported UTF-8") from None
    if COMMIT.fullmatch(value):
        return value
    ref = value.removeprefix("ref: ")
    if not ref.startswith("refs/heads/") or value == ref:
        raise ValueError("Git observation requires a detached commit or a local branch")
    parts = ref.split("/")
    if (".." in ref or "@{" in ref or any(char in r"~^:?*[\\" or ord(char) < 33 or ord(char) == 127 for char in ref)
            or any(not part or part.startswith(".") or part.endswith((".", ".lock")) for part in parts)):
        raise ValueError("Git HEAD has an invalid branch reference")
    directory = common.directory
    with ExitStack() as opened:
        try:
            for part in parts[:-1]:
                directory = opened.enter_context(directory.child(part))
            loose = _read(directory, parts[-1], MAX_REF)
        except FileNotFoundError:
            loose = None
    if loose is not None:
        candidate = loose.decode("ascii", errors="replace").rstrip("\r\n")
        if not COMMIT.fullmatch(candidate):
            raise ValueError("Git branch reference is not a supported commit")
        return candidate
    packed = _read(common.directory, "packed-refs", MAX_PACKED_REFS) or b""
    encoded = ref.encode("utf-8")
    for line in packed.splitlines():
        oid, _, name = line.partition(b" ")
        if name == encoded:
            candidate = oid.decode("ascii", errors="replace")
            if not COMMIT.fullmatch(candidate):
                raise ValueError("Git packed branch reference is not a supported commit")
            return candidate
    return None  # Unborn branch.


def _repo(workspace, stack):
    for root in (workspace, *workspace.parents):
        marker = root / ".git"
        try:
            mode = marker.lstat().st_mode
        except FileNotFoundError:
            continue
        marker_identity = _identity(marker)
        if stat.S_ISDIR(mode):
            common = index = _PinnedDirectory(marker, stack, metadata=True)
            binding = None
        elif stat.S_ISREG(mode):
            checkout = _PinnedDirectory(root, stack, metadata=True)
            binding = _read(checkout.directory, ".git", MAX_REF)
            if binding is None or not binding.startswith(b"gitdir: "):
                raise ValueError("Git observation could not read the linked checkout registration")
            location = binding[8:].decode("utf-8").rstrip("\r\n")
            if os.name != "nt" and PureWindowsPath(location).drive:
                raise ValueError("Git linked worktree uses a Windows path; open it with Windows Sprig")
            link = _safe_path(location, root)
            if link.parent.name != "worktrees":
                raise ValueError("Git observation requires a registered linked worktree")
            index = _PinnedDirectory(link, stack, metadata=True)
            common = _PinnedDirectory(link.parent.parent, stack, metadata=True)
            backlink = _read(index.directory, "gitdir", MAX_REF)
            if (_read(index.directory, "commondir", MAX_REF) not in (b"../..\n", b"../..\r\n")
                    or backlink is None or _safe_path(backlink.decode("utf-8").rstrip("\r\n"), link) != marker):
                raise ValueError("Git linked worktree does not point back to this checkout")
        else:
            raise ValueError("Git observation requires ordinary repository metadata")
        objects = _PinnedDirectory(common.path / "objects", stack, metadata=True)

        def check():
            if _identity(marker) != marker_identity:
                raise ValueError("Git checkout registration changed during observation")
            if binding is not None and _read(checkout.directory, ".git", MAX_REF) != binding:
                raise ValueError("Git checkout registration changed during observation")
            for pinned in (common, index, objects):
                pinned.check()
            if (common.path / "reftable").exists():
                raise ValueError("Git observation does not support the reftable reference backend")
            if any(os.path.lexists(objects.path / "info" / name) for name in ("alternates", "http-alternates")):
                raise ValueError("Git observation refuses alternate object directories")

        check()
        return root, common, index, objects.path, check
    raise ValueError("no Git checkout contains this workspace")


def _prepare_shadow(path, common, index, commit, check_cancelled):
    raw_index = _read(index.directory, "index", MAX_INDEX)
    (path / "refs" / "heads").mkdir(parents=True)
    (path / "objects").mkdir()
    head = commit or "ref: refs/heads/sprig-unborn"
    (path / "HEAD").write_bytes((head + "\n").encode("ascii"))
    config = "[core]\n\tfilemode = " + ("false" if os.name == "nt" else "true") + "\n"
    if commit and len(commit) == 64:
        config += "\trepositoryformatversion = 1\n[extensions]\n\tobjectformat = sha256\n"
    (path / "config").write_text(config, encoding="ascii")
    try:
        with common.directory.child("info") as info:
            exclude = _read(info, "exclude", 32768)
    except FileNotFoundError:
        exclude = None
    if exclude is not None:
        (path / "info").mkdir()
        (path / "info" / "exclude").write_bytes(exclude)
    if raw_index is not None:
        (path / "index").write_bytes(raw_index)
    total = len(raw_index or b"")
    # Split-index bases are addressed by their hash inside the copied index.
    count = 0
    with os.scandir(index.path) as entries:
        for entry in entries:
            check_cancelled()
            if not re.fullmatch(r"sharedindex\.[0-9a-f]{40}(?:[0-9a-f]{24})?", entry.name):
                continue
            count += 1
            if count > 32:
                raise ValueError("Git observation supports at most 32 split index bases")
            shared = _read(index.directory, entry.name, MAX_INDEX - total)
            if shared is None:
                raise ValueError("Git split index changed during observation")
            total += len(shared)
            (path / entry.name).write_bytes(shared)


def _rows(output, prefix, policy, secrets):
    if output and not output.endswith("\0"):
        raise ValueError("Git returned an incomplete status")
    rows = []
    for entry in output[:-1].split("\0") if output else ():
        xy, separator, name = entry[:2], entry[2:3], entry[3:]
        if not STATUS.fullmatch(xy) or separator != " ":
            raise ValueError("Git returned an unsupported status record")
        path = PurePosixPath(name)
        parts = path.parts
        if (path.is_absolute() or not parts or ".." in parts or "\\" in name
                or parts[:len(prefix)] != prefix or len(parts) == len(prefix)):
            raise ValueError("Git returned a path outside the requested workspace")
        visible = parts[len(prefix):]
        name = "/".join(visible)
        if (policy.excluded(visible, ignore_case=True) or KEY_PATTERN.search(name)
                or "[REDACTED]" in name or "\ufffd" in name or redact(name, secrets) != name):
            continue
        rows.append({"path": name, "index": xy[0], "worktree": xy[1]})
    return sorted(rows, key=lambda row: row["path"])


def _working_file(tools, path, *, content=False):
    from .tools import MAX_FILE

    try:
        with tools._parent(path) as (directory, name):
            info = directory.stat(name)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or getattr(info, "st_file_attributes", 0) & 0x400):
                raise ValueError("Git diff requires a single ordinary file without links")
            if not content:
                return info, None
            with directory.open_read(name) as stream:
                opened = os.fstat(stream.fileno())
                # Windows path stat and descriptor fstat expose different ctime.
                if (not os.path.samestat(info, opened) or opened.st_size != info.st_size
                        or opened.st_mtime_ns != info.st_mtime_ns or opened.st_nlink != 1):
                    raise ValueError("Git diff file changed while opening")
                data = stream.read(MAX_FILE + 1)
                if tools._revision(os.fstat(stream.fileno())) != tools._revision(opened):
                    raise ValueError("Git diff file changed while reading")
            if tools._revision(directory.stat(name)) != tools._revision(info):
                raise ValueError("Git diff file changed while reading")
            if len(data) > MAX_FILE:
                raise ValueError("Git diff working copy exceeds 512 KiB")
            text = tools._decode(data)
            if redact(text, tools.config.secrets) != text:
                raise ValueError("Git diff working copy contains protected content")
            return info, data
    except FileNotFoundError:
        return None, None


def _observe(tools, scope, *, staged=None):
    from .tools import Tools

    config = tools.config
    diff = staged is not None
    operation = "diff" if diff else "status"
    original = tools._revision(_working_file(tools, scope)[0]) if diff else None
    executable = shutil.which("git")
    if executable is None:
        raise ValueError("Git executable not found on the process PATH")
    executable = Path(executable).resolve()
    with ExitStack() as stack:
        root, common, index, objects, check = _repo(config.workdir, stack)
        if executable.is_relative_to(root):
            raise ValueError("Git observation refuses an executable inside the repository")
        working = _PinnedDirectory(config.workdir, stack)
        scratch = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="sprig-git-observe-"))).resolve()
        if scratch.is_relative_to(root):
            raise ValueError("Git observation requires a temporary directory outside the repository")
        private = scratch / "private"
        if os.name == "nt":
            from .windows import private_directory
            private_directory(private)
        else:
            private.mkdir(mode=0o700)
        shadow = private / "snapshot.git"
        worktree = private / "tree" if diff else root
        prefix = tuple(config.workdir.relative_to(root).parts)
        requested = "/".join((*prefix, scope)) if diff else scope
        state = None

        def snapshot():
            try:
                index_path = index.path / "index"
                index_identity = _identity(index_path)
                info = index_path.lstat()
                stamp = (index_identity, info.st_mtime_ns, info.st_size)
            except FileNotFoundError:
                stamp = None
            return _read_ref(common, _read(index.directory, "HEAD", MAX_REF)), stamp

        def confirm(description):
            nonlocal state
            proposal = ("GIT " + operation.upper() + " · fixed read-only host command with a temporary Git index\n"
                        + "Repository: " + json.dumps(str(root)) + "\n" + description)
            if not tools.request_approval(redact(proposal, config.secrets)):
                return False
            working.check()
            check()
            state = snapshot()
            _prepare_shadow(shadow, common, index, state[0], tools._check_cancelled)
            if diff:
                info, raw = _working_file(tools, scope, content=not staged)
                if tools._revision(info) != original:
                    raise ValueError("Git diff file changed during approval")
                worktree.mkdir()
                if raw is not None:
                    target = worktree / requested
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(raw)
                    target.chmod(0o700 if info.st_mode & stat.S_IXUSR else 0o600)
            check()
            return True

        cap = min(1_000_000, config.command_output_limit or config.max_output)
        env = (("GIT_CONFIG_NOSYSTEM", "1"), ("GIT_CONFIG_GLOBAL", str(private / "global-config")),
               ("GIT_ATTR_NOSYSTEM", "1"), ("GIT_TERMINAL_PROMPT", "0"),
               ("GIT_OPTIONAL_LOCKS", "0"), ("GIT_ALLOW_PROTOCOL", ""),
               ("GIT_NO_LAZY_FETCH", "1"), ("GIT_OBJECT_DIRECTORY", str(objects)))
        isolated = replace(config, command_sandbox="off", sandbox_read_dirs=(), inherit_env=False,
                           env_allowlist=(), command_env=env, max_output=cap, command_output_limit=cap,
                           command_log_dir=None)
        runner = Tools(isolated, confirm, tools.cancel_event)
        runner.instructions = tools.instructions
        stack.callback(runner.close)
        argv = [str(executable), "--no-pager", "--literal-pathspecs", "--git-dir", str(shadow),
                "--work-tree", str(worktree), "-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull,
                "-c", "core.attributesFile=" + os.devnull, "-c", "core.excludesFile=" + os.devnull]
        if diff:
            argv.extend(["-C", str(worktree), "diff", "--raw", "-z", "--patch", "--no-color", "--no-relative",
                         "--no-ext-diff", "--no-textconv", "--no-renames", "--ignore-submodules=all", "--unified=3"])
            if staged:
                argv.append("--cached")
        else:
            argv.extend(["status", "--porcelain=v1", "-z", "--no-renames", "--untracked-files=all", "--ignore-submodules=all"])
        argv.extend(["--", requested])
        outcome = runner.execute("run", {"argv": argv})
        if outcome.get("approved") is False:
            return {"approved": False}, None, prefix
        if outcome.get("returncode") != 0 or outcome.get("timed_out") or outcome.get("output_limited") or "error" in outcome:
            suggestion = "" if diff else ". Try a narrower path."
            return {"approved": outcome.get("approved", False), "error": f"Git {operation} did not complete within its safety limits{suggestion}",
                    "timed_out": bool(outcome.get("timed_out")), "output_limited": bool(outcome.get("output_limited"))}, None, prefix
        check()
        working.check()
        if state != snapshot():
            raise ValueError("Git HEAD or index changed during observation; request a fresh result")
        if diff and tools._revision(_working_file(tools, scope)[0]) != original:
            raise ValueError("Git diff file changed during observation; request a fresh diff")
        tools.policy.reload()
        return outcome, state[0], prefix


def git_status(tools, path, limit):
    config = tools.config
    if config.read_only or not config.tool_enabled("run"):
        return {"approved": False, "error": "Git status requires enabled, approved command execution"}
    if type(limit) is not int or not 1 <= limit <= 512:
        raise ValueError("Git status limit must be from 1 to 512")
    scope = "/".join(tools._parts(path)) or "."
    outcome, head, prefix = _observe(tools, scope)
    if "output" not in outcome:
        return outcome
    rows = _rows(outcome["output"], prefix, tools.policy, config.secrets)
    payload = {"approved": True, "scope": scope, "head": head, "entries": [],
               "total": len(rows), "truncated": True}
    used = len(json.dumps(payload, ensure_ascii=True))
    if used + 1 > config.max_output:
        return {"approved": True, "error": "Git status scope is too long for the display limit"}
    page = payload["entries"]
    for row in rows[:limit]:
        used += len(json.dumps(row, ensure_ascii=True)) + 2
        if used + 1 > config.max_output:
            break
        page.append(row)
    payload["truncated"] = len(page) < len(rows)
    return payload


def _patch(output, expected):
    if not output:
        return ""
    sections = output.split("\0", 3)
    if len(sections) != 4 or not PATCH_RECORD.fullmatch(sections[0]) or sections[1] != expected or sections[2]:
        raise ValueError("Git diff did not describe exactly one ordinary file")
    body = sections[3]
    if (not body.startswith("diff --git ") or not body.endswith("\n")
            or any(ord(char) < 32 and char not in "\n\r\t" for char in body)):
        raise ValueError("Git diff requires one complete text patch")
    in_hunk = False
    for line in body.split("\n")[1:-1]:
        if line.startswith("@@ "):
            in_hunk = True
        elif in_hunk:
            if not line.startswith((" ", "+", "-")) and line != r"\ No newline at end of file":
                raise ValueError("Git diff contains output outside the text patch")
        elif not line.startswith(("index ", "new file mode ", "deleted file mode ", "old mode ", "new mode ", "--- ", "+++ ")):
            raise ValueError("Git diff requires one complete text patch")
    if KEY_PATTERN.search(body) or "[REDACTED]" in body or "\ufffd" in body:
        raise ValueError("Git diff contains protected or undecodable text")
    return body


def git_diff(tools, path, staged=False, offset=0, snapshot=None):
    config = tools.config
    if config.read_only or not config.tool_enabled("run"):
        return {"approved": False, "error": "Git diff requires enabled, approved command execution"}
    if (type(staged) is not bool or type(offset) is not int or not 0 <= offset <= 1_000_000
            or snapshot is not None and (not isinstance(snapshot, str) or not DIGEST.fullmatch(snapshot))
            or offset and snapshot is None):
        raise ValueError("Git diff continuation requires its snapshot and character offset")
    parts = tools._parts(path)
    scope = "/".join(parts)
    if (not scope or any(ord(char) < 32 or ord(char) == 127 for char in scope)
            or tools.policy.excluded(parts, ignore_case=True) or KEY_PATTERN.search(scope)):
        raise ValueError("Git diff requires one visible workspace file")
    outcome, head, prefix = _observe(tools, scope, staged=staged)
    if "output" not in outcome:
        return outcome
    if tools.policy.excluded(parts, ignore_case=True):
        raise ValueError("Git diff path is now excluded")
    body = _patch(outcome["output"], "/".join((*prefix, scope)))
    digest = hashlib.sha256(json.dumps([head, scope, staged, body], ensure_ascii=False).encode("utf-8")).hexdigest()
    if snapshot is not None and snapshot != digest:
        raise ValueError("Git diff changed; reopen at offset 0")
    if offset > len(body):
        raise ValueError("Git diff offset exceeds the patch")

    def page(length):
        end = offset + length
        return {"approved": True, "path": scope, "staged": staged, "snapshot": digest, "offset": offset,
                "text": body[offset:end], "next_offset": end if end < len(body) else None}

    def size(result):
        return len(json.dumps(result, ensure_ascii=False).encode("utf-8"))

    low, high = 0, min(8192, len(body) - offset)
    budget = min(config.max_output, 16384)
    while low < high:
        mid = (low + high + 1) // 2
        if size(page(mid)) <= budget:
            low = mid
        else:
            high = mid - 1
    result = page(low)
    if size(result) > budget or not low and result["next_offset"] is not None:
        raise ValueError("Git diff page exceeds its output budget")
    return result
