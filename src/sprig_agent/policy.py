"""File privacy and command environment policy, enforced outside the model."""
import fnmatch
import os
import re
from .config import validate_exclusions

SENSITIVE = {".git", ".sprig", ".env", ".ssh", ".aws", ".azure", ".kube", ".gnupg", ".netrc", ".npmrc", ".pypirc",
             "id_rsa", "id_ed25519", "id_ecdsa", "credentials", "credentials.json", "secrets.json"}
KEY_PATTERN = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{24,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
ENVIRONMENT = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR",
               "LANG", "LANGUAGE", "TZ", "TERM", "COLORTERM", "PYTHONUTF8", "PYTHONIOENCODING", "VIRTUAL_ENV"}


def command_environment(config, *, minimal=False):
    allowed = {name.upper() if os.name == "nt" else name for name in (() if minimal else config.env_allowlist)}
    protected = {"SPRIG_API_KEY", "OPENAI_API_KEY", "AGNES_API_KEY", *(name.upper() for name in config.credential_env_names)}
    values = {key: value for key, value in os.environ.items()
            if (config.inherit_env and not minimal or key.upper() in ENVIRONMENT or key.upper().startswith("LC_")
                or (key.upper() if os.name == "nt" else key) in allowed)
            and key.upper() not in protected
            and not any(secret in value for secret in config.credential_secrets)}
    if not minimal:
        if any(name.upper() in protected or any(secret in value for secret in config.credential_secrets)
               for name, value in config.command_env):
            raise ValueError("configured command environment now contains a protected credential; restart with corrected configuration")
        values.update(config.command_env)
    return values


class FilePolicy:
    def __init__(self, directory, config_path=None, patterns=()):
        self.directory = directory
        self.patterns = []
        self.config_patterns = validate_exclusions(patterns)
        self.config_parts = []
        paths = config_path if isinstance(config_path, (tuple, list)) else (config_path,)
        for path in paths:
            if path is None:
                continue
            from pathlib import Path
            try:
                parts = Path(path).resolve().relative_to(directory.path.resolve()).parts
                if os.name == "nt":
                    parts = tuple(p.lower() for p in parts)
                self.config_parts.append(parts)
            except ValueError:
                pass

    def reload(self):
        try:
            raw = self.directory.read(".sprigignore", 32768)
        except FileNotFoundError:
            self.patterns = self.config_patterns
            return
        if len(raw) > 32768:
            raise ValueError(".sprigignore exceeds 32 KiB")
        patterns = [line.strip() for line in raw.decode("utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]
        self.patterns = validate_exclusions((*self.config_patterns, *patterns))

    def excluded(self, parts, *, ignore_case=False):
        ignore_case = ignore_case or os.name == "nt"
        if ignore_case:
            parts = tuple(part.lower() for part in parts)
        if any(tuple(parts) == (tuple(part.lower() for part in path) if ignore_case else path)
               for path in self.config_parts):
            return True
        if any(part in SENSITIVE or part.startswith(".env.") or part.endswith((".pem", ".p12", ".pfx", ".key")) for part in parts):
            return True
        for pattern in self.patterns:
            if ignore_case:
                pattern = pattern.lower()
            directory = pattern.endswith("/")
            pattern = pattern.rstrip("/")
            for end in range(1, len(parts) + 1):
                name = "/".join(parts[:end]) if "/" in pattern else parts[end - 1]
                if fnmatch.fnmatchcase(name, pattern):
                    return True
                if directory and name.startswith(pattern + "/"):
                    return True
        return False

    @staticmethod
    def check_text(text):
        if KEY_PATTERN.search(text):
            raise ValueError("file appears to contain credentials; upload through file tools is blocked")
