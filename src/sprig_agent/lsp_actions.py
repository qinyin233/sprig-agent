"""Conversation-local LSP code-action proposals, never commands or authorization."""
from collections import OrderedDict
import json
import uuid
import weakref

from .config import redact
from .lsp_client import Budget, LspError, encode
from .lsp_edits import prepare as prepare_edits

MAX_ACTIONS = 32
MAX_CACHE_BYTES = 8 * 1024 * 1024
FIELDS = {'title', 'kind', 'diagnostics', 'isPreferred', 'disabled', 'edit', 'command', 'data'}


def literal(value, *, resolved_from=None):
    if not isinstance(value, dict) or set(value) - FIELDS or 'command' in value:
        raise LspError('code actions require plain edits; commands and unknown fields are unsupported')
    title = value.get('title')
    if (not isinstance(title, str) or not title.strip() or len(title) > 256 or len(title.encode('utf-8')) > 1024
            or any(ord(char) < 32 or ord(char) == 127 for char in title)):
        raise LspError('invalid code action title')
    kind = value.get('kind')
    if kind is not None and (not isinstance(kind, str) or len(kind) > 128 or len(kind.encode('utf-8')) > 512
                             or any(ord(char) < 32 or ord(char) == 127 for char in kind)):
        raise LspError('invalid code action kind')
    if 'isPreferred' in value and type(value['isPreferred']) is not bool:
        raise LspError('invalid code action preferred flag')
    if 'disabled' in value:
        raise LspError('disabled code actions cannot be applied')
    if 'diagnostics' in value and not isinstance(value['diagnostics'], list):
        raise LspError('invalid code action diagnostics')
    if 'edit' in value and not isinstance(value['edit'], dict):
        raise LspError('code action edit must be a WorkspaceEdit object')
    encode(value)
    if resolved_from is not None:
        def canonical(item):
            return json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if any(key != 'edit' and (key not in value or canonical(value[key]) != canonical(item))
               for key, item in resolved_from.items()):
            raise LspError('resolved code action changed existing attributes')
    return {'title': title, 'kind': kind, 'preferred': value.get('isPreferred', False)}


class CodeActions:
    def __init__(self, host):
        self.host = host
        self.entries = OrderedDict()

    def clear(self, server=None):
        for identity, entry in list(self.entries.items()):
            if server is None or entry['server'].name == server:
                self.entries.pop(identity, None)

    def _safe(self, value):
        if redact(value, self.host.config.secrets) != value:
            raise LspError('code action contains configured credentials; proposal rejected')

    def catalog(self, raw, selected, client, prepared, check):
        if raw is None:
            raw = []
        if not isinstance(raw, list) or len(raw) > 64:
            raise LspError('code action response must contain at most 64 entries')
        rows, candidates, omitted, cost, observed = [], [], 0, 0, {}
        synchronized = {uri: {key: document[key] for key in ('path', 'sha256', 'version')}
                        for uri, document in client.documents.items()}
        provider = client.capabilities.get('codeActionProvider')
        can_resolve = isinstance(provider, dict) and provider.get('resolveProvider') is True
        editable = self.host.config.tool_enabled('edit_file') and callable(self.host.apply_code_action)
        for action in raw:
            check()
            try:
                metadata = literal(action)
                self._safe(action)
                lazy = 'edit' not in action
                if lazy and not can_resolve:
                    raise LspError('code action has no text edit or supported resolver')
                documents = {row['path']: row for row in prepared.values()}
                plans = [] if lazy else prepare_edits(action['edit'], self.host.path_from_uri, self.host.document, synchronized, check)
                documents.update((plan['document']['path'], plan['document']) for plan in plans)
                stored = json.loads(json.dumps(action, ensure_ascii=False)) if lazy else {'edit': action['edit']}
                size = len(encode(stored)) + len(encode(synchronized)) + sum(row['bytes'] for row in documents.values()) + 2048
                if size + cost > MAX_CACHE_BYTES or len(rows) >= MAX_ACTIONS:
                    raise LspError('code action proposal cache limit reached')
                row = {**metadata, 'requires_resolve': lazy, 'files': None if lazy else len(plans),
                       'can_apply': editable, 'not_authorization': True}
                candidate = None
                if editable:
                    identity = 'lsp-action-' + uuid.uuid4().hex
                    entry = {'server': selected, 'client': weakref.ref(client) if lazy else None,
                             'documents': documents, 'synchronized': synchronized,
                             'raw': stored, 'lazy': lazy, 'metadata': metadata, 'cost': size}
                    candidate = (identity, entry)
                    row['action_id'] = identity
                for path, document in documents.items():
                    if path in observed and observed[path]['sha256'] != document['sha256']:
                        raise LspError('code action documents changed while building the catalog')
                cost += size
                for path, document in documents.items():
                    observed[path] = document
                if candidate is not None:
                    candidates.append(candidate)
                rows.append(row)
            except (ValueError, OSError):
                check()
                omitted += 1
        for document in observed.values():
            check()
            if self.host.document(document['path'])['sha256'] != document['sha256']:
                raise LspError('code action documents changed while building the catalog')
        cache_cost = sum(entry['cost'] for _, entry in candidates)
        with self.host.lock:
            check()
            while self.entries and (len(self.entries) + len(candidates) > MAX_ACTIONS
                    or sum(entry['cost'] for entry in self.entries.values()) + cache_cost > MAX_CACHE_BYTES):
                self.entries.popitem(last=False)
            self.entries.update(candidates)
        return {'actions': rows, 'omitted': omitted, 'cache_limit_actions': MAX_ACTIONS,
                'cache_limit_bytes': MAX_CACHE_BYTES, 'complete_project_index': False}, tuple(observed)

    def apply(self, identity, check):
        host = self.host
        if not host.config.tool_enabled('edit_file') or not callable(host.apply_code_action):
            raise LspError('applying code actions requires enabled file editing')
        with host.lock:
            check()
            if not isinstance(identity, str) or identity not in self.entries:
                raise LspError('code action unavailable in this conversation; query again, no operation was replayed')
            entry = self.entries[identity]
        selected = entry['server']
        if not any(row is selected and row.enabled for row in host.config.lsp_servers):
            raise LspError('code action server configuration changed')
        directory, _, recheck = host.prepare(selected.process_config())
        def current():
            check()
            recheck()
            self._safe(entry['raw'])
            for document in entry['documents'].values():
                check()
                if host.document(document['path'])['sha256'] != document['sha256']:
                    raise LspError('code action source or target changed; query again')
            if entry['lazy']:
                client = host.clients.get(selected.name)
                if client is None or client is not entry['client']() or not client.status()['running']:
                    raise LspError('code action resolver connection expired; no automatic restart')
                for uri, document in entry['synchronized'].items():
                    now = client.documents.get(uri)
                    if now is None or (now['sha256'], now['version']) != (document['sha256'], document['version']):
                        raise LspError('code action resolver document version changed; query again')
        current()
        description = ('APPLY LSP code action via ' + selected.name + '\n'
            + json.dumps(entry['metadata'], ensure_ascii=False)
            + '\nConfigured command: ' + json.dumps(list(selected.command))
            + '\ncwd: ' + json.dumps(str(directory))
            + '\nEnvironment names: ' + json.dumps([name for name, _ in (*selected.env, *selected.env_from)])
            + f'\nResolver idle {selected.timeout:g}s; total {selected.deadline:g}s.'
            + '\nSource snapshots: ' + json.dumps([{'path': row['path'], 'sha256': row['sha256']} for row in entry['documents'].values()], ensure_ascii=False)
            + ('\nResolve edit using the same live server connection; no restart or retry.' if entry['lazy'] else '\nUse the cached plain-text edit; no server RPC.')
            + '\nStop the owned server before file reviews. Each file requires its own full-diff approval. '
              'Commands, resource operations and annotations are denied; no automatic replay or acceptance.')
        if host.approve(redact(description, host.config.secrets)) is not True:
            return {'action': 'apply_code_action', 'approved': False, 'changed': False, 'server_contacted': False}
        current()
        with host.lock:
            check()
            if self.entries.get(identity) is not entry:
                raise LspError('code action expired during approval')
            self.clear(selected.name)  # One selection consumes all old proposals from this server.
        client = host.clients.get(selected.name)
        try:
            if entry['lazy']:
                budget = Budget(selected, check)
                resolved = client.exchange('codeAction/resolve', entry['raw'], budget)
                literal(resolved, resolved_from=entry['raw'])
                self._safe(resolved)
                if 'edit' not in resolved:
                    raise LspError('code action resolver did not return a plain text edit')
                current()
                raw = resolved['edit']
                bound = budget.check
            else:
                raw = entry['raw']['edit']
                bound = check
            plans = prepare_edits(raw, host.path_from_uri, host.document, entry['synchronized'], bound)
            current(); bound()
        finally:
            with host.lock:
                # Even resolver failure closes the selected owned process; cached
                # results are not executable and partial intentions never retry.
                owned = host.clients.get(selected.name)
                if owned is not None:
                    owned.close()
                    host.clients.pop(selected.name, None)
        check()
        targets = {plan['document']['path'] for plan in plans}
        def application_guard():
            check()
            recheck()
            # The shared edit batch checks remaining write targets. Non-target
            # context must remain fixed even while humans review file diffs.
            for document in entry['documents'].values():
                if document['path'] not in targets and host.document(document['path'])['sha256'] != document['sha256']:
                    raise LspError('code action context changed during file review')
        return host.apply_code_action(plans, application_guard)
