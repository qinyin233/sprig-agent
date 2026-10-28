"""Bounded filename discovery through pinned directories; never open file bodies."""
import time

from .config import terminal_text
from .filesystem import is_link
from .json_budget import encoded_size

MAX_ENTRIES = 5000
MAX_SECONDS = 1.0
MAX_DEPTH = 32


def discover(tools, parts, matcher, hidden, limit, skip_directory):
    result = {"files": [], "entries_scanned": 0, "truncated": False,
              "timed_out": False, "skipped_entries": 0}
    cap = min(tools.config.max_output, 16384)
    deadline = time.monotonic() + MAX_SECONDS
    stopped = False

    def budget():
        nonlocal stopped
        tools.root.ensure_open()
        tools._check_cancelled()
        if time.monotonic() >= deadline:
            result["timed_out"] = result["truncated"] = stopped = True
        if result["entries_scanned"] >= MAX_ENTRIES:
            result["truncated"] = stopped = True
        return not stopped

    def visible(path):
        if ("\\" in path or terminal_text(path) != path or len(path.encode("utf-8")) > 4096
                or any(secret in path for secret in tools.config.credential_secrets)):
            return False
        try:
            tools.policy.check_text(path)
        except ValueError:
            return False
        return True

    def walk(directory, relative, depth):
        nonlocal stopped
        if depth > MAX_DEPTH:
            result["truncated"] = True
            return
        if not budget():
            return
        with directory.scandir() as entries:
            for entry in entries:
                if not budget():
                    return
                result["entries_scanned"] += 1
                local = (*relative, entry.name)
                full = (*parts, *local)
                path = "/".join(full)
                try:
                    if ((not hidden and entry.name.startswith(".")) or tools.policy.excluded(full)
                            or not visible(path) or is_link(entry)):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if not skip_directory(entry.name) and matcher.could_descend(local):
                            with directory.child(entry.name) as child:
                                walk(child, local, depth + 1)
                    elif entry.is_file(follow_symlinks=False) and matcher.matches(local):
                        candidate = {**result, "files": [*result["files"], path],
                                     "entries_scanned": MAX_ENTRIES, "skipped_entries": MAX_ENTRIES}
                        if len(result["files"]) >= limit or encoded_size(candidate) > cap:
                            result["truncated"] = stopped = True
                            return
                        result["files"].append(path)
                except (OSError, UnicodeError, ValueError):
                    result["skipped_entries"] += 1
                    result["truncated"] = True
                if stopped:
                    return

    with tools._directory(parts) as directory:
        walk(directory, (), 0)
    tools._check_cancelled()
    tools.root.ensure_open()
    if time.monotonic() >= deadline:
        result["timed_out"] = result["truncated"] = True
    # Names describe an observation, not a current file version. Later reads
    # independently reopen no-follow paths; discovery cannot authorize them.
    tools.policy.reload()
    retained = []
    for path in result["files"]:
        tools._check_cancelled()
        if not tools.policy.excluded(tuple(path.split("/"))) and visible(path):
            retained.append(path)
    tools._check_cancelled()
    result["files"] = sorted(retained)
    if time.monotonic() >= deadline:
        result["timed_out"] = result["truncated"] = True
    return result
