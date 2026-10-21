"""Bounded local filename discovery; no source bodies, model calls or actions."""

import bisect
import time

from .config import terminal_text
from .filesystem import is_link
from .policy import FilePolicy
from .tools import skip_directory


COMMANDS = {
    "help": "本地帮助", "keys": "快捷键", "status": "任务与运行状态", "plan": "模型报告的计划",
    "config": "生效配置", "models": "配置中的模型", "model": "明确确认后切换配置模型", "context": "上下文预算", "usage": "请求统计",
    "files": "选择快照或实时引用文件", "add": "附加文件快照", "ref": "添加实时文件引用",
    "refs": "查看或清空实时引用", "unref": "移除实时引用", "map": "项目地图",
    "continue": "继续对话", "new": "新建对话", "cancel": "取消任务", "exit": "退出",
    "git": "查看 Git 状态", "diff": "查看 Git diff", "compact": "压缩旧工具记录",
    "changes": "查看修改记录", "undo": "撤销文件编辑", "redo": "重做文件编辑",
    "summary": "预览摘要；create 会逐次确认一次额外模型请求", "history": "本地分页回查原对话",
    "timeline": "查看历史轮次；选择完整轮次建立分支", "fork": "明确确认后建立当前或历史轮次分支",
    "logs": "查看或逐次确认清理当前会话的命令日志", "log": "按日志句柄分页回查命令输出",
    "skills": "列出已配置技能；本地查看不请求模型", "skill": "本地预览技能或资料；不会执行脚本",
    "mcp": "查看外部服务状态；connect 和每次 RPC 单独批准",
    "discard-history": "清除修改备份", "sessions": "列出会话", "save": "保存会话",
    "restore": "恢复会话", "delete": "删除会话", "prune": "清理旧会话",
    "worktrees": "列出工作树", "worktree": "创建工作树",
}


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
