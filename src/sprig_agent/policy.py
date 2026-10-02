"""File privacy and command environment policy, enforced outside the model."""
import fnmatch
import os
import re

SENSITIVE = {".git", ".env", ".ssh", ".aws", ".azure", ".kube", ".gnupg", ".netrc", ".npmrc", ".pypirc",
             "id_rsa", "id_ed25519", "id_ecdsa", "credentials", "credentials.json", "secrets.json"}
KEY_PATTERN = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{24,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
ENVIRONMENT = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR",
               "LANG", "LANGUAGE", "TZ", "TERM", "COLORTERM", "PYTHONUTF8", "PYTHONIOENCODING", "VIRTUAL_ENV"}


def command_environment(config):
    return {key: value for key, value in os.environ.items()
            if (config.inherit_env or key.upper() in ENVIRONMENT or key.upper().startswith("LC_"))
            and key.upper() not in {"SPRIG_API_KEY", "OPENAI_API_KEY"}
            and not (config.api_key and config.api_key in value)}


class FilePolicy:
    def __init__(self, directory):
        self.directory = directory
        self.patterns = []

    def reload(self):
        try:
            raw = self.directory.read(".sprigignore", 32768)
        except FileNotFoundError:
            self.patterns = []
            return
        if len(raw) > 32768:
            raise ValueError(".sprigignore exceeds 32 KiB")
        patterns = [line.strip() for line in raw.decode("utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]
        if len(patterns) > 256 or any(p.startswith(("!", "/")) or "\\" in p or ".." in p.split("/") or "\0" in p for p in patterns):
            raise ValueError(".sprigignore accepts at most 256 relative exclusion patterns; negation is not supported")
        self.patterns = patterns

    def excluded(self, parts):
        if os.name == "nt":
            parts = tuple(part.lower() for part in parts)
        if any(part in SENSITIVE or part.startswith(".env.") or part.endswith((".pem", ".p12", ".pfx", ".key")) for part in parts):
            return True
        for pattern in self.patterns:
            if os.name == "nt":
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
