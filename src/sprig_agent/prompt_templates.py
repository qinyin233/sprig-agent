"""Explicit user prompt templates; expansion never evaluates files or commands."""
from dataclasses import dataclass
from itertools import islice
import json
import re
import shlex
import unicodedata

from .local_commands import COMMANDS

NAME_PATTERN = r"[a-z][a-z0-9_-]{0,31}"
TOKEN = re.compile(r"\$\$|\$ARGUMENTS\b|\$[1-9][0-9]*")


def text_bytes(value, limit, *, multiline=False):
    if (not isinstance(value, str) or not value.strip()
            or any(unicodedata.category(char).startswith("C") and not (multiline and char in "\r\n\t") for char in value)):
        raise ValueError("prompt text must be nonempty and contain no terminal controls")
    if len(value.encode("utf-8")) > limit:
        raise ValueError("prompt text exceeds its UTF-8 byte limit")
    return len(value.encode("utf-8"))


@dataclass(frozen=True)
class PromptTemplate:
    name: str
    template: str
    description: str = ""

    def __post_init__(self):
        if not isinstance(self.name, str) or not re.fullmatch(NAME_PATTERN, self.name) or self.name in COMMANDS:
            raise ValueError("prompt name must be lowercase, at most 32 characters and distinct from built-in commands")
        text_bytes(self.template, 16384, multiline=True)
        if self.description:
            text_bytes(self.description, 160)
        elif self.description != "":
            raise ValueError("prompt description must be a string")
        positions = {int(match[1:]) for match in TOKEN.findall(self.template) if match[1:].isdigit()}
        if positions and (max(positions) > 9 or positions != set(range(1, max(positions) + 1))):
            raise ValueError("prompt positional placeholders must be contiguous $1 through at most $9")


def validate_templates(templates):
    if (not isinstance(templates, tuple) or len(templates) > 32
            or any(not isinstance(row, PromptTemplate) for row in templates)
            or len({row.name for row in templates}) != len(templates)):
        raise ValueError("prompts require at most 32 unique validated templates")
    if sum(len(row.template.encode("utf-8")) + len(row.description.encode("utf-8")) for row in templates) > 49152:
        raise ValueError("prompt library exceeds 48 KiB of UTF-8 text")
    return templates


def parse_templates(table):
    if not isinstance(table, dict) or len(table) > 32:
        raise ValueError("prompts must be a table of at most 32 definitions (values withheld)")
    result = []
    for name, entry in table.items():
        if not isinstance(entry, dict) or set(entry) - {"template", "description"} or "template" not in entry:
            raise ValueError("each prompt requires template and optional description (values withheld)")
        result.append(PromptTemplate(name, entry["template"], entry.get("description", "")))
    return validate_templates(tuple(result))


def expand_prompt(config, invocation):
    """Return None for ordinary/local input. Never recursively dispatch expansion."""
    if not isinstance(invocation, str) or not config.prompt_templates:
        return None
    if len(invocation) > config.max_context_bytes:
        raise ValueError("prompt invocation exceeds the task byte limit")
    match = re.fullmatch(r"\s*/(" + NAME_PATTERN + r")(?:[ \t\r\n]([\s\S]*))?", invocation)
    if match is None:
        return None
    prompt = next((row for row in config.prompt_templates if row.name == match[1]), None)
    if prompt is None:
        return None
    if len(invocation.encode("utf-8")) > config.max_context_bytes:
        raise ValueError("prompt invocation exceeds the task byte limit")
    arguments = match[2] or ""
    tokens = TOKEN.findall(prompt.template)
    positions = [int(token[1:]) for token in tokens if token[1:].isdigit()]
    parts = []
    if positions:
        lexer = shlex.shlex(arguments, posix=True)
        lexer.whitespace_split, lexer.commenters, lexer.escape = True, "", ""
        # Only referenced positions (and one excess-argument check) are needed.
        parts = list(islice(lexer, max(positions) + (0 if "$ARGUMENTS" in tokens else 1)))
        if len(parts) < max(positions) or ("$ARGUMENTS" not in tokens and len(parts) != max(positions)):
            raise ValueError("prompt needs exactly its numbered arguments, or $ARGUMENTS to retain extra text")
    def replace(match):
        token = match[0]
        return "$" if token == "$$" else arguments if token == "$ARGUMENTS" else parts[int(token[1:]) - 1]
    chunks, size, start = [], 0, 0
    def append(text):
        nonlocal size
        size += len(text.encode("utf-8"))
        if size > config.max_context_bytes:
            raise ValueError("expanded prompt exceeds the task UTF-8 byte limit")
        chunks.append(text)
    for token in TOKEN.finditer(prompt.template):
        append(prompt.template[start:token.start()])
        append(replace(token))
        start = token.end()
    append(prompt.template[start:])
    if not positions and "$ARGUMENTS" not in tokens and arguments:
        append("\n\n")
        append(arguments)
    body = "".join(chunks)
    text_bytes(body, config.max_context_bytes, multiline=True)
    return body


def prompt_catalog(config, offset=0):
    """Metadata only, with bounded pages. Bodies reach the model only on invocation."""
    from .config import redact
    if type(offset) is not int or not 0 <= offset <= len(config.prompt_templates):
        raise ValueError("Use /prompts [OFFSET] within this prompt library")
    rows = sorted(config.prompt_templates, key=lambda row: row.name)[offset:]
    result = {"prompts": [], "next_offset": None, "network_contacted": False}
    def fits(value):
        return len(json.dumps(value, ensure_ascii=False).encode("utf-8")) <= config.max_output
    for index, row in enumerate(rows, offset):
        entry = redact({"command": "/" + row.name, "description": row.description}, config.secrets)
        candidate = {**result, "prompts": [*result["prompts"], entry],
                     "next_offset": index + 1 if index + 1 < len(config.prompt_templates) else None}
        if not fits(candidate):
            if result["prompts"]:
                result["next_offset"] = index
                break
            candidate["prompts"] = [{"command": entry["command"], "description_omitted": True}]
        result = candidate
    return result
