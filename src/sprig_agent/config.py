"""Configuration and small shared safety helpers; no on-disk secrets."""

from dataclasses import dataclass, field
import math
from pathlib import Path
import unicodedata
from urllib.parse import urlsplit, urlunsplit


def redact(value, secret):
    """Replace only the configured key, not arbitrary or encoded secrets."""
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, list):
        return [redact(item, secret) for item in value]
    if isinstance(value, dict):
        return {redact(key, secret): redact(item, secret) for key, item in value.items()}
    return value


def terminal_text(text):
    """Make untrusted terminal controls visible, including bidi overrides."""
    return "".join(
        ch if ch == "\n" or (not unicodedata.category(ch).startswith("C"))
        else f"\\u{ord(ch):04x}" for ch in str(text)
    )


def endpoint(base_url):
    if not isinstance(base_url, str) or any(ord(ch) <= 32 for ch in base_url):
        raise ValueError("base URL contains whitespace or control characters")
    parsed = urlsplit(base_url)
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("base URL must not contain credentials, query, or fragment")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError("use HTTPS (HTTP is allowed only for localhost/loopback)")
    parsed.port  # Validate invalid/non-numeric ports before making a request.
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


@dataclass(frozen=True)
class Config:
    workdir: Path
    model: str
    base_url: str = "https://api.openai.com/v1"
    api_key: str = field(default="", repr=False)
    max_steps: int = 20
    timeout: float = 30
    max_output: int = 16000
    stream: bool = False
    checks: tuple = ()
    read_only: bool = False
    inherit_env: bool = False
    max_context_bytes: int = 131072
    compact_context: bool = False

    def __post_init__(self):
        root = Path(self.workdir).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("workdir must be a directory")
        object.__setattr__(self, "workdir", root)
        object.__setattr__(self, "base_url", endpoint(self.base_url))
        if not isinstance(self.model, str) or not self.model or len(self.model) > 200 or any(ord(c) < 32 for c in self.model):
            raise ValueError("a valid model is required (--model or SPRIG_MODEL)")
        if not isinstance(self.max_steps, int) or isinstance(self.max_steps, bool) or not 1 <= self.max_steps <= 100:
            raise ValueError("max-steps must be between 1 and 100")
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)) or not math.isfinite(self.timeout) or not 0 < self.timeout <= 120:
            raise ValueError("timeout must be greater than 0 and at most 120 seconds")
        if not isinstance(self.max_output, int) or isinstance(self.max_output, bool) or not 256 <= self.max_output <= 1000000:
            raise ValueError("max-output must be between 256 and 1000000 bytes")
        if not isinstance(self.api_key, str) or any(ord(c) < 32 for c in self.api_key):
            raise ValueError("API key contains control characters")
        if not isinstance(self.stream, bool):
            raise ValueError("stream must be a boolean")
        if not isinstance(self.read_only, bool) or not isinstance(self.inherit_env, bool):
            raise ValueError("read-only and inherit-env must be booleans")
        if self.read_only and self.checks:
            raise ValueError("read-only mode cannot execute acceptance commands")
        if (not isinstance(self.max_context_bytes, int) or isinstance(self.max_context_bytes, bool)
                or not 4096 <= self.max_context_bytes <= 2 * 1024 * 1024):
            raise ValueError("max-context-bytes must be between 4096 and 2097152")
        if not isinstance(self.compact_context, bool):
            raise ValueError("compact-context must be a boolean")
        if not isinstance(self.checks, tuple) or len(self.checks) > 16:
            raise ValueError("at most 16 acceptance checks are supported")
        for argv in self.checks:
            if (not isinstance(argv, tuple) or not argv or len(argv) > 256
                    or any(not isinstance(arg, str) or "\0" in arg for arg in argv) or not argv[0]
                    or sum(len(arg) for arg in argv) > 65536):
                raise ValueError("each acceptance check must be a nonempty argv array")
            if self.api_key and any(self.api_key in arg for arg in argv):
                raise ValueError("configured API key must not appear in acceptance arguments")

    def binding(self):
        binding = {"base_url": self.base_url, "model": self.model, "workdir": str(self.workdir)}
        if self.checks:
            binding["checks"] = [list(argv) for argv in self.checks]
        if self.read_only:
            binding["read_only"] = True
        if self.inherit_env:
            binding["inherit_env"] = True
        return binding
