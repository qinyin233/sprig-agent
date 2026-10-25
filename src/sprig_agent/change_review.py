"""Read-only retained edit comparisons; bounded, cancellable, never an authorization."""
from bisect import bisect_left
import copy
import hashlib
import json
import re

from .changes import MAX_JOURNAL, unpack
from .config import redact, terminal_text
from .json_budget import encoded_size

ID = re.compile(r"[a-f0-9]{32}")
DIGEST = re.compile(r"[a-f0-9]{64}")
MAX_VIEW = 8 * 1024 * 1024


def command(argument):
    if not isinstance(argument, str) or len(argument) > 256 or len(argument.encode('utf-8')) > 512:
        raise ValueError('change review arguments exceed the local command limit')
    parts = argument.split()
    if parts == ['review']:
        return None, 0, None
    if len(parts) not in (2, 4) or parts[0] != 'show' or not (parts[1] == 'latest' or ID.fullmatch(parts[1])):
        raise ValueError('Use /changes review or /changes show ID|latest [OFFSET SNAPSHOT]')
    offset, snapshot = 0, None
    if len(parts) == 4:
        if (len(parts[2]) > 8 or not parts[2].isascii() or not parts[2].isdecimal()
                or int(parts[2]) > MAX_VIEW or not DIGEST.fullmatch(parts[3])):
            raise ValueError('review pagination requires a character offset and exact snapshot from the previous page')
        offset, snapshot = int(parts[2]), parts[3]
    return parts[1], offset, snapshot


def _digest(value, check):
    digest = hashlib.sha256()
    for part in json.JSONEncoder(sort_keys=True, ensure_ascii=False).iterencode(value):
        check()
        digest.update(part.encode('utf-8'))
    return digest.hexdigest()


def _guard(tools):
    tools.root.ensure_open()
    tools._check_cancelled()
    tools._require_tool('read_file')
    if tools.history.store is not None:
        tools.history.store.handle.ensure_open()
    config, source, version = tools.config, tools.history.state, tools._conversation_version
    if tools.history.workdir != str(config.workdir):
        raise ValueError('change history belongs to a different workspace')
    encoded_size(source, max_bytes=MAX_JOURNAL, check_cancelled=tools._check_cancelled)
    state = tools.history.validate(copy.deepcopy(source))
    source_hash = _digest(state, tools._check_cancelled)
    secrets = frozenset(config.secrets)
    tools.policy.reload()
    patterns = tools.policy.patterns
    def fresh():
        tools.root.ensure_open()
        tools._check_cancelled()
        if tools.history.store is not None:
            tools.history.store.handle.ensure_open()
        tools.policy.reload()
        if (tools.config is not config or tools._conversation_version != version or tools.policy.patterns != patterns
                or frozenset(config.secrets) != secrets or tools.history.state is not source
                or _digest(source, tools._check_cancelled) != source_hash):
            raise ValueError('change review source or privacy policy changed; start again')
    return state, fresh


def _safe(tools, text):
    return terminal_text(redact(text, tools.config.secrets))


def _visible(tools, record):
    tools._parts(record['path'])
    if not ID.fullmatch(record['id']):
        raise ValueError('retained change identifier is invalid')
    identifiers = (record['path'], record['id'], record.get('batch', {}).get('id', ''))
    if any(secret in value for secret in tools.config.secrets for value in identifiers):
        raise ValueError('record path is protected by credential policy')


def catalog(tools):
    from .tools import ToolError
    state, fresh = _guard(tools)
    rows, hidden = [], 0
    for index, record in enumerate(state['records']):
        tools._check_cancelled()
        try:
            _visible(tools, record)
        except (ValueError, ToolError):
            hidden += 1
            continue
        path = _safe(tools, record['path'])
        rows.append({'id': record['id'], 'path': path[:80], 'path_truncated': len(path) > 80,
                     'applied': None if state['pending'] and state['pending']['path'] == record['path'] else index < state['cursor'],
                     'before_bytes': None if record['before'] is None else len(unpack(record['before'])),
                     'after_bytes': None if record['after'] is None else len(unpack(record['after']))}
                    | ({'batch': copy.deepcopy(record['batch'])} if 'batch' in record else {}))
    result = {'records': rows, 'hidden_records': hidden, 'pending': state['pending'] is not None,
              'historical': True, 'not_acceptance': True, 'network_contacted': False}
    fresh()
    if len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > min(tools.config.max_output, 16384):
        raise ValueError('change review catalog exceeds output budget; increase max_output explicitly')
    return result


def edits(before, after, check=lambda: None):
    """Unique-line anchors plus an ordered subsequence; ambiguous gaps use exact replacements.

    Every line is scanned a bounded number of times. This deliberately avoids
    quadratic LCS matching on large/repetitive input; output need not be minimal.
    """
    def unique(lines):
        positions = {}
        for index, line in enumerate(lines):
            if index % 256 == 0: check()
            positions[line] = None if line in positions else index
        return positions
    left, right = unique(before), unique(after)
    pairs = sorted((index, right[line]) for line, index in left.items()
                   if index is not None and right.get(line) is not None)
    tails, tail_indexes, previous = [], [], []
    for index, (_, position) in enumerate(pairs):
        if index % 256 == 0: check()
        place = bisect_left(tails, position)
        previous.append(tail_indexes[place - 1] if place else -1)
        if place == len(tails):
            tails.append(position)
            tail_indexes.append(index)
        else:
            tails[place], tail_indexes[place] = position, index
    anchors = []
    index = tail_indexes[-1] if tail_indexes else -1
    while index >= 0:
        if len(anchors) % 256 == 0: check()
        anchors.append(pairs[index])
        index = previous[index]
    anchors.reverse()
    changes, a, b = [], 0, 0
    for end_a, end_b in [*anchors, (len(before), len(after))]:
        check()
        while a < end_a and b < end_b and before[a] == after[b]:
            if a % 256 == 0: check()
            a, b = a + 1, b + 1
        stop_a, stop_b = end_a, end_b
        while stop_a > a and stop_b > b and before[stop_a - 1] == after[stop_b - 1]:
            if stop_a % 256 == 0: check()
            stop_a, stop_b = stop_a - 1, stop_b - 1
        if a != stop_a or b != stop_b:
            changes.append((a, stop_a, b, stop_b))
        a, b = end_a + 1, end_b + 1
    check()
    return changes


def comparison(path, before, after, check=lambda: None):
    def physical_lines(text):
        parts = (text or '').split('\n')
        return [part + '\n' for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
    a, b = physical_lines(before), physical_lines(after)
    changes = edits(a, b, check)
    if not changes and before is not None and after is not None:
        return '(retained contents are identical)\n'
    output = ['--- ' + (json.dumps('a/' + path, ensure_ascii=False) if before is not None else '/dev/null') + '\n',
              '+++ ' + (json.dumps('b/' + path, ensure_ascii=False) if after is not None else '/dev/null') + '\n']
    if not changes:
        return ''.join([*output, '(empty file created)\n' if before is None else '(empty file deleted)\n'])
    groups = []
    for change in changes:
        if groups and change[0] - groups[-1][-1][1] <= 6:
            groups[-1].append(change)
        else:
            groups.append([change])
    def span(start, end):
        return str(start if start == end else start + 1) + ',' + str(end - start)
    def lines(prefix, values):
        for index, line in enumerate(values):
            if index % 256 == 0: check()
            output.append(prefix + line)
            if not line.endswith('\n'):
                output.append('\n\\ No newline at end of file\n')
    for group in groups:
        check()
        first, last = group[0], group[-1]
        prefix = min(3, first[0], first[2])
        suffix = min(3, len(a) - last[1], len(b) - last[3])
        start_a, start_b, end_a, end_b = first[0] - prefix, first[2] - prefix, last[1] + suffix, last[3] + suffix
        output.append('@@ -' + span(start_a, end_a) + ' +' + span(start_b, end_b) + ' @@\n')
        cursor = start_a
        for lo_a, hi_a, lo_b, hi_b in group:
            lines(' ', a[cursor:lo_a])
            lines('-', a[lo_a:hi_a])
            lines('+', b[lo_b:hi_b])
            cursor = hi_a
        lines(' ', a[cursor:end_a])
    check()
    return ''.join(output)


def page(tools, identity, offset=0, snapshot=None):
    if (not isinstance(identity, str) or not (identity == 'latest' or ID.fullmatch(identity))
            or type(offset) is not int or not 0 <= offset <= MAX_VIEW
            or snapshot is not None and (not isinstance(snapshot, str) or not DIGEST.fullmatch(snapshot))
            or offset and snapshot is None):
        raise ValueError('choose an exact retained change ID; continuation requires its snapshot and character offset')
    state, fresh = _guard(tools)
    if identity == 'latest' and state['records']:
        identity = state['records'][-1]['id']
    entry = next(((index, row) for index, row in enumerate(state['records']) if row['id'] == identity), None)
    if entry is None:
        raise ValueError('retained change not found; list /changes or reopen review')
    index, record = entry
    _visible(tools, record)
    raw_before, raw_after = unpack(record['before']), unpack(record['after'])
    # Redact whole snapshots before adding diff prefixes or splitting pages.
    before = None if raw_before is None else _safe(tools, tools._decode(raw_before))
    after = None if raw_after is None else _safe(tools, tools._decode(raw_after))
    def observe_current():
        try:
            with tools._parent(record['path']) as (directory, name):
                raw, info = tools._snapshot(directory, name)
                if raw is not None: tools._decode(raw)
                return raw, tools._revision(info)
        except FileNotFoundError:
            return 'missing_parent', None
    observation = observe_current()
    current = 'unavailable'
    if observation[0] != 'missing_parent':
        raw, revision = observation
        applied = [row for row in state['records'][:state['cursor']] if row['path'] == record['path']]
        expected = unpack(applied[-1]['after']) if applied else unpack(next(row['before'] for row in state['records'] if row['path'] == record['path']))
        anchor = state['anchors'][record['path']]
        current = ('matches_recorded_state' if raw == expected and revision == (tuple(anchor) if anchor is not None else None)
                   else 'changed_since_recording')
    if state['pending'] is not None: current = 'uncertain_pending_change'
    path = _safe(tools, record['path'])
    body = _safe(tools, comparison(path, before, after, tools._check_cancelled))
    if len(body) > MAX_VIEW:
        raise ValueError('safe change comparison exceeds review limit')
    metadata = {'id': identity, 'path': path, 'applied': None if state['pending'] and state['pending']['path'] == record['path'] else index < state['cursor'],
                'before_bytes': None if raw_before is None else len(raw_before), 'after_bytes': None if raw_after is None else len(raw_after),
                'current': current, 'historical': True, 'not_acceptance': True, 'network_contacted': False,
                'offset_unit': 'characters', 'display_escaped': True}
    if 'batch' in record:
        metadata['batch'] = copy.deepcopy(record['batch'])
    digest = hashlib.sha256(json.dumps(metadata, sort_keys=True, ensure_ascii=False).encode())
    for start in range(0, len(body), 4096):
        tools._check_cancelled()
        digest.update(body[start:start + 4096].encode('utf-8'))
    token = digest.hexdigest()
    if snapshot is not None and snapshot != token:
        raise ValueError('review snapshot changed; reopen at offset 0')
    if offset > len(body): raise ValueError('review offset exceeds the comparison')
    budget = min(tools.config.max_output, 16384)
    def fragment(length):
        complete = offset + length == len(body)
        return {**metadata, 'snapshot': token, 'offset': offset, 'text': body[offset:offset + length],
                'complete': complete, 'next_offset': None if complete else offset + length}
    low, high = 0, min(len(body) - offset, 8192)
    while low < high:
        middle = (low + high + 1) // 2
        if len(json.dumps(fragment(middle), ensure_ascii=False).encode('utf-8')) <= budget: low = middle
        else: high = middle - 1
    result = fragment(low)
    if observe_current() != observation:
        raise ValueError('workspace file changed while preparing review; refresh the comparison')
    fresh()
    if len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > budget or not low and not result['complete']:
        raise ValueError('review page metadata exceeds output budget')
    return result
