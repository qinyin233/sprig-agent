"""Bounded local filename discovery; no source bodies, model calls or actions."""

import bisect
import time

from .config import terminal_text
from .filesystem import is_link
from .policy import FilePolicy
from .tools import skip_directory


from .local_commands import COMMANDS


def file_candidates(config, root, query, cancelled, *, max_entries=5000, seconds=0.25):
    """Caller owns root lifetime. Only relative names pass the shared privacy policy."""
    result = {"files": [], "entries_scanned": 0, "limited": False, "cancelled": False}
    if not config.tool_enabled("list_files") or not config.tool_enabled("read_file"):
        return {**result, "error": "文件选择需要启用 list_files 和 read_file"}
    if not isinstance(query, str) or len(query) > 256 or any(ord(c) < 32 for c in query):
        return {**result, "error": "文件查询最多 256 字符，不接受控制字符"}
    if cancelled.is_set():
        return {**result, "cancelled": True}
    query = query.strip().replace("\\", "/").casefold()
    deadline = time.monotonic() + seconds
    policy = FilePolicy(root, (config.config_path, *config.config_paths), config.excluded_paths)
    policy.reload()
    ranked = []

    def stopped():
        root.ensure_open()
        if cancelled.is_set():
            result["cancelled"] = True
            return True
        if result["entries_scanned"] >= max_entries or time.monotonic() >= deadline:
            result["limited"] = True
            return True
        return False

    def walk(directory, prefix=(), depth=0):
        if depth > 32:
            result["limited"] = True
            return
        with directory.scandir() as entries:
            for entry in entries:
                if stopped():
                    return
                result["entries_scanned"] += 1
                parts = (*prefix, entry.name)
                path = "/".join(parts)
                try:
                    if (policy.excluded(parts) or "\\" in path or terminal_text(path) != path or len(path.encode("utf-8")) > 4096
                            or any(secret in path for secret in config.credential_secrets) or is_link(entry)):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if not skip_directory(entry.name):
                            with directory.child(entry.name) as child:
                                walk(child, parts, depth + 1)
                    elif entry.is_file(follow_symlinks=False):
                        folded, basename = path.casefold(), entry.name.casefold()
                        if not query or basename.startswith(query):
                            rank = 0
                        elif query in basename:
                            rank = 1
                        elif query in folded:
                            rank = 2
                        else:
                            letters = iter(folded)
                            if not all(any(char == letter for letter in letters) for char in query):
                                continue
                            rank = 3
                        bisect.insort(ranked, (rank, len(path), folded, path))
                        if len(ranked) > 20:
                            ranked.pop()
                            result["limited"] = True
                except (OSError, UnicodeError):
                    result["limited"] = True
    walk(root)
    result["files"] = [item[-1] for item in ranked]
    if result["cancelled"]:
        result["files"] = []
    return result
