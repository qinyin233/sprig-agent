"""Offline startup diagnostics; no model connection or command execution."""

from contextlib import closing
from importlib import metadata
import os
import platform

from .filesystem import Directory
from .policy import FilePolicy
from .settings import public_config
from .tools import Tools


def diagnose(config, details):
    checks = []
    try:
        with Directory(config.workdir) as root:
            FilePolicy(root, (config.config_path, *config.config_paths), config.excluded_paths).reload()
        checks.append({"check": "workspace and .sprigignore", "ok": True})
    except (OSError, ValueError, UnicodeError):
        checks.append({"check": "workspace and .sprigignore", "ok": False,
                       "detail": "cannot safely inspect workspace or privacy rules"})
    try:
        with closing(Tools(config, lambda _: False)) as tools:
            records = tools.instructions.context()
        checks.append({"check": "development guidance", "ok": True, "files": len(records)})
    except (OSError, ValueError):
        checks.append({"check": "development guidance", "ok": False,
                       "detail": "cannot safely load configured guidance; check paths, privacy, UTF-8 and size limits"})
    for package in ("textual", "tree-sitter", "google-re2"):
        try:
            version = metadata.version(package)
            checks.append({"check": package, "ok": True, "version": version})
        except metadata.PackageNotFoundError:
            checks.append({"check": package, "ok": False, "detail": "run uv sync --locked"})
    if config.reference_files:
        from .references import FileReferences
        try:
            with closing(Tools(config, lambda _: False)) as tools:
                _, observed = FileReferences(config, tools).prepare()
            checks.append({"check": "file references", "ok": True, "files": len(observed),
                           "metadata_only": sum(not row["content_attached"] for row in observed)})
        except (OSError, ValueError):
            checks.append({"check": "file references", "ok": False,
                           "detail": "cannot safely prepare selected references; check paths, privacy, read tool and file limits"})
    checks.append({"check": "platform", "ok": os.name in {"nt", "posix"}})
    if config.base_url.startswith("https://api.openai.com/") and not config.api_key:
        checks.append({"check": "provider credential", "ok": False, "detail": "API key is not set"})
    return {"ok": all(check["ok"] for check in checks), "network_contacted": False,
            "provider_compatibility": "not tested; offline diagnostics do not validate a real model",
            "python": platform.python_version(), "platform": platform.system(),
            "config": public_config(config, details), "checks": checks}
