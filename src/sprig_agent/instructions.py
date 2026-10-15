"""Bounded workspace guidance; never an authorization source."""

import hashlib
import json
from contextlib import contextmanager

MAX_RULE_FILE = 16384
MAX_RULE_CONTEXT = 65536
MAX_RULE_SCOPES = 128


class InstructionError(ValueError):
    pass


class ProjectInstructions:
    def __init__(self, read, check_cancelled, enabled=True, files=()):
        self.read, self.check_cancelled = read, check_cancelled
        self.discover, self.files = enabled, files
        self.enabled = enabled or bool(files)
        self.scopes = {()}
        self.served = None
        self.model_action_active = False

    def register(self, directory):
        if not self.discover:
            return
        scopes = {directory[:end] for end in range(len(directory) + 1)}
        if len(self.scopes | scopes) > MAX_RULE_SCOPES:
            raise InstructionError("project instruction discovery exceeds 128 directories; narrow the workspace or explicitly use --no-project-instructions")
        self.scopes.update(scopes)

    def _load(self, scopes):
        records = []
        candidates = {path: (path.rpartition("/")[0] or ".") if path.rpartition("/")[2] == "AGENTS.md" else "."
                      for path in self.files}
        if self.discover:
            for directory in sorted(scopes, key=lambda parts: (len(parts), parts)):
                candidates["/".join((*directory, "AGENTS.md"))] = "/".join(directory) or "."
        for path, scope in candidates.items():
            self.check_cancelled()
            content = self.read(path, MAX_RULE_FILE)
            if content is None and path in self.files:
                raise InstructionError("configured instruction file is missing or excluded: " + path)
            if content is not None:
                records.append({"path": path, "scope": scope, "content": content})
                if len(json.dumps(records, ensure_ascii=False).encode("utf-8")) > MAX_RULE_CONTEXT:
                    raise InstructionError("project instruction context exceeds 64 KiB; shorten guidance files, reduce configured files or disable AGENTS.md discovery")
        self.check_cancelled()
        return records

    def context(self):
        return self._load(self.scopes) if self.enabled else []

    @staticmethod
    def _fingerprints(records):
        return {record["path"]: hashlib.sha256(record["content"].encode("utf-8")).hexdigest()
                for record in records}

    def mark_served(self, records):
        if self.enabled:
            self.served = self._fingerprints(records)

    @contextmanager
    def model_action(self):
        previous = self.model_action_active
        self.model_action_active = True
        try:
            yield
        finally:
            self.model_action_active = previous

    def prepare_action(self, directory):
        if not self.enabled:
            return None
        self.register(directory)
        # Standalone, explicitly approved local tool operations have no model
        # request to compare against. Agent records its snapshot before HTTP.
        if not self.model_action_active:
            return None
        if self.served is None:
            raise InstructionError("model instruction context is unavailable; no action performed")
        scopes = {directory[:end] for end in range(len(directory) + 1)}
        current = self._fingerprints(self._load(scopes))
        paths = {*self.files, *("/".join((*scope, "AGENTS.md")) for scope in scopes if self.discover)}
        if current != {path: value for path, value in self.served.items() if path in paths}:
            raise InstructionError("project instructions are new or changed; no action performed. Review refreshed development guidance in the next model request, then propose the action again")
        return current

    def recheck_action(self, directory, expected):
        if not self.enabled or expected is None:
            return
        scopes = {directory[:end] for end in range(len(directory) + 1)}
        if self._fingerprints(self._load(scopes)) != expected:
            raise InstructionError("project instructions changed during approval; no action performed. Review current guidance and request fresh approval")
