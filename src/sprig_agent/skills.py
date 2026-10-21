"""Explicit, bounded Agent Skills discovery and read-only resource loading."""

from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import time

import yaml

from .config import redact, terminal_text
from .filesystem import is_link
from .policy import FilePolicy

MAX_MANIFEST = 65536
MAX_RESOURCE = 262144
MAX_CATALOG = 32768
MAX_SKILLS = 64
MAX_ENTRIES = 4096
SKILL_SCHEMA = {"type": "function", "function": {
    "name": "skill", "description": "Load a configured skill's SKILL.md or UTF-8 resource by name and skill-relative path. Follow next_offset for complete text. No execution or approval; scripts require a separate fresh run proposal.",
    "parameters": {"type": "object", "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 64},
        "path": {"type": "string", "maxLength": 1024},
        "offset": {"type": "integer", "minimum": 0, "maximum": MAX_RESOURCE},
        "page_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$", "description": "Required for offset>0; use page_sha256 returned by the first page. Rejects changed resources or redaction."}},
        "required": ["name"], "additionalProperties": False}}}


class SkillError(ValueError):
    pass


class ManifestLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise SkillError("manifest keys must be unique strings")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def valid_name(name):
    return (isinstance(name, str) and 1 <= len(name) <= 64 and name == name.lower()
            and not name.startswith("-") and not name.endswith("-") and "--" not in name
            and all(c.isalnum() or c == "-" for c in name))


def local_arguments(argument):
    parts = shlex.split(argument)
    if not 1 <= len(parts) <= 4:
        raise SkillError("Usage: /skill NAME [PATH [OFFSET PAGE_SHA256]]; quote resource paths containing spaces")
    return {"name": parts[0], "path": parts[1] if len(parts) > 1 else "SKILL.md",
            "offset": int(parts[2]) if len(parts) > 2 else 0, "page_sha256": parts[3] if len(parts) > 3 else None}


def manifest(text, folder):
    """Parse only bounded safe YAML; aliases, tags and duplicate fields cannot execute."""
    lines = text.removeprefix("\ufeff").splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise SkillError("SKILL.md requires YAML frontmatter")
    end = next((i for i in range(1, len(lines)) if lines[i].rstrip("\r\n") == "---"), None)
    if end is None:
        raise SkillError("SKILL.md frontmatter is not closed")
    front = "".join(lines[1:end])
    if len(front.encode("utf-8")) > 16384:
        raise SkillError("skill frontmatter exceeds 16 KiB")
    depth = count = 0
    try:
        for event in yaml.parse(front, Loader=yaml.SafeLoader):
            count += 1
            if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                depth += 1
            if isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                depth -= 1
            if count > 512 or depth > 16 or isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
                raise SkillError("skill YAML structure exceeds limits or uses anchors/aliases")
        value = yaml.load(front, Loader=ManifestLoader)
    except yaml.YAMLError:
        raise SkillError("invalid safe YAML in skill frontmatter") from None
    if (not isinstance(value, dict) or set(value) - {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
            or not valid_name(value.get("name")) or value["name"] != folder):
        raise SkillError("skill name must match its directory; unknown fields belong in metadata")
    for field, limit in (("description", 1024), ("license", 1024), ("compatibility", 500), ("allowed-tools", 2048)):
        if field not in value and field != "description":
            continue
        text = value.get(field)
        if (not isinstance(text, str) or not text.strip() or len(text) > limit
                or any(ord(c) < 32 and c not in "\n\t" for c in text)):
            raise SkillError("invalid skill metadata text")
    metadata = value.get("metadata", {})
    if (not isinstance(metadata, dict) or len(metadata) > 32
            or any(not isinstance(k, str) or not isinstance(v, str) or len(k) > 128 or len(v) > 1024 for k, v in metadata.items())):
        raise SkillError("skill metadata must be a bounded map of strings")
    return value


class Skills:
    def __init__(self, config, ensure_open, check_cancelled, workspace_policy):
        self.config, self.ensure_open, self.check_cancelled = config, ensure_open, check_cancelled
        self.workspace_policy = workspace_policy
        self.served = {}

    def check(self):
        self.ensure_open()
        self.check_cancelled()

    @contextmanager
    def root(self, path):
        # Local import avoids settings/config initialization cycles.
        from .settings import config_parent
        self.check()
        if FilePolicy(self.workspace_policy.directory).excluded(path.parts):
            raise SkillError("skill root contains a protected path component")
        with config_parent(path / "SKILL.md") as (directory, _):
            policy = FilePolicy(directory, (self.config.config_path, *self.config.config_paths), self.config.excluded_paths)
            policy.reload()
            self.workspace_policy.reload()
            yield directory, policy

    def allowed(self, root, policy, parts):
        if policy.excluded(parts):
            return False
        try:
            relative = (root.path.joinpath(*parts)).relative_to(self.config.workdir)
        except ValueError:
            return True
        return not self.workspace_policy.excluded(relative.parts)

    def read(self, root, policy, parts, limit):
        self.check()
        policy.reload()
        self.workspace_policy.reload()
        if not self.allowed(root, policy, parts):
            raise SkillError("skill path is excluded by file privacy policy")
        with ExitStack() as stack:
            directory = root
            for part in parts[:-1]:
                directory = stack.enter_context(directory.child(part))
            with directory.open_read(parts[-1]) as handle:
                before = os.fstat(handle.fileno())
                if before.st_nlink != 1 or before.st_size > limit:
                    raise SkillError("skill files must be single-link regular files within the byte limit")
                raw = handle.read(limit + 1)
                after = os.fstat(handle.fileno())
            if (len(raw) > limit or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise SkillError("skill file changed while reading or exceeds the byte limit")
        self.check()
        if b"\0" in raw:
            raise SkillError("skill files must be UTF-8 text")
        text = raw.decode("utf-8")
        FilePolicy.check_text(text)
        return redact(text, self.config.secrets), hashlib.sha256(raw).hexdigest()

    def catalog(self):
        self.check()
        records, index, issues, ambiguous = [], {}, [], set()
        scanned = 0
        deadline = time.monotonic() + 2
        limited = False
        for number, path in enumerate(self.config.skill_dirs):
            try:
                with self.root(path) as (root, policy):
                    with root.scandir() as entries:
                        for entry in entries:
                            self.check()
                            scanned += 1
                            if scanned > MAX_ENTRIES or time.monotonic() > deadline:
                                limited = True
                                break
                            if (not valid_name(entry.name) or terminal_text(entry.name) != entry.name
                                    or not self.allowed(root, policy, (entry.name,)) or is_link(entry)
                                    or not entry.is_dir(follow_symlinks=False)):
                                continue
                            try:
                                text, digest = self.read(root, policy, (entry.name, "SKILL.md"), MAX_MANIFEST)
                                value = manifest(text, entry.name)
                                name = value["name"]
                                if name in index or name in ambiguous:
                                    index.pop(name, None)
                                    ambiguous.add(name)
                                    issues.append({"root": number, "name": name, "error": "duplicate skill name; no version is exposed"})
                                    continue
                                index[name] = (path, digest, value["description"])
                                if len(index) > MAX_SKILLS:
                                    limited = True
                                    break
                            except FileNotFoundError:
                                continue
                            except (OSError, ValueError, RecursionError):
                                issues.append({"root": number, "name": entry.name, "error": "cannot safely read valid bounded SKILL.md; check YAML, UTF-8, naming and privacy"})
                            if len(issues) > MAX_SKILLS:
                                limited = True
                                break
            except (OSError, ValueError):
                issues.append({"root": number, "error": "cannot safely scan configured skill root; check existence, links and privacy"})
            if limited:
                break
        for name, (path, digest, description) in sorted(index.items()):
            records.append({"name": name, "description": description, "base_directory": str(path / name), "sha256": digest})
        self.check()
        result = redact({"skills": records, "issues": issues, "limited": limited, "not_authorization": True}, self.config.secrets)
        if limited or len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_CATALOG:
            return {"skills": [], "issues": [{"error": "skill catalog incomplete or over 32 KiB; narrow configured roots; no skills exposed"}],
                    "limited": True, "not_authorization": True}
        return result

    def mark_served(self, catalog):
        self.served = {row["name"]: (row["base_directory"], row["sha256"]) for row in catalog["skills"]}

    def load(self, name, path="SKILL.md", offset=0, *, page_sha256=None, model_action=False):
        self.check()
        if not valid_name(name) or not isinstance(path, str) or not path or len(path) > 1024:
            raise SkillError("choose a configured skill name and a bounded skill-relative path")
        parts = PurePosixPath(path).parts
        if (not parts or PurePosixPath(path).is_absolute() or ".." in parts or "\\" in path or ":" in path
                or any(ord(c) < 32 for c in path) or type(offset) is not int or not 0 <= offset <= MAX_RESOURCE):
            raise SkillError("invalid skill-relative resource path or redacted UTF-8 byte offset")
        path = "/".join(parts)
        if ((page_sha256 is not None and (not isinstance(page_sha256, str) or not re.fullmatch("[0-9a-f]{64}", page_sha256)))
                or offset and page_sha256 is None):
            raise SkillError("continuation needs the page_sha256 from the same redacted resource")
        catalog = self.catalog()
        record = next((row for row in catalog["skills"] if row["name"] == name), None)
        if record is None:
            raise SkillError("skill unavailable or ambiguous; inspect /skills and configured roots")
        expected = (record["base_directory"], record["sha256"])
        if model_action and self.served.get(name) != expected:
            raise SkillError("skill changed since the model request; review refreshed catalog and propose again")
        base = Path(record["base_directory"])
        with self.root(base.parent) as (root, policy):
            _, current = self.read(root, policy, (name, "SKILL.md"), MAX_MANIFEST)
            if current != record["sha256"]:
                raise SkillError("skill manifest changed during loading; propose again")
            text, digest = self.read(root, policy, (name, *parts), MAX_MANIFEST if path == "SKILL.md" else MAX_RESOURCE)
            _, final_manifest = self.read(root, policy, (name, "SKILL.md"), MAX_MANIFEST)
            if final_manifest != current:
                raise SkillError("skill manifest changed while loading its resource; propose again")
        data = text.encode("utf-8")
        page_digest = hashlib.sha256(data).hexdigest()
        if page_sha256 is not None and page_sha256 != page_digest:
            raise SkillError("skill resource changed; restart at offset 0 and read its new pages")
        if offset > len(data):
            raise SkillError("skill offset exceeds the redacted resource")
        try:
            remaining = data[offset:].decode("utf-8")
        except UnicodeError:
            raise SkillError("skill offset must be a redacted UTF-8 character boundary") from None
        result = {"name": name, "path": "/".join(parts), "base_directory": str(base), "sha256": digest,
                  "manifest_sha256": current, "page_sha256": page_digest, "offset": offset, "total_bytes": len(data), "content": "",
                  "next_offset": None, "complete": False, "guidance": path == "SKILL.md", "not_authorization": True}
        budget = min(self.config.max_output, 16384)
        lo, hi = 0, len(remaining)
        while lo < hi:
            self.check()
            size = (lo + hi + 1) // 2
            chunk = remaining[:size]
            end = offset + len(chunk.encode("utf-8"))
            result.update(content=chunk, next_offset=end if end < len(data) else None, complete=end == len(data))
            if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= budget:
                lo = size
            else:
                hi = size - 1
        chunk = remaining[:lo]
        end = offset + len(chunk.encode("utf-8"))
        result.update(content=chunk, next_offset=end if end < len(data) else None, complete=end == len(data))
        if (remaining and not chunk) or len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > budget:
            raise SkillError("skill output metadata exceeds the output budget; increase max_output")
        self.check()
        return result
