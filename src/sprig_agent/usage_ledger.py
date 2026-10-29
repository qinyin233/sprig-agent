"""Bounded numeric session usage. A start marker is uncertainty, never a receipt."""
import copy
import hashlib
import json
import math
import re
import threading
import uuid

from .metrics import FAILURE_CODES, FINISH_REASONS, MAX_RECORDS, MAX_TOKENS, TOKEN_FIELDS, Measurements

LIMIT = 2**53 - 1
COUNT_KEYS = {'attempts', 'not_attempted', 'outcomes', 'failures', 'request_bytes', 'response_bytes',
              'elapsed_ms', 'tokens', 'known'}
RECORD_KEYS = {'attempted', 'streamed', 'outcome', 'http_status', 'request_bytes', 'response_bytes',
               'elapsed_ms', 'first_text_ms', 'usage', 'finish_reason', 'error_code'}
STATE_KEYS = {'version', 'id', 'revision', 'sequence', 'coverage_gap', 'counts', 'uncertain',
              'pending', 'recent', 'inherited'}


class UsagePersistenceError(RuntimeError):
    """No next request or tool may follow an uncertain usage checkpoint."""


def integer(value, maximum=LIMIT):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError('invalid numeric usage counter')
    return value


def number(value):
    if type(value) not in (int, float) or not 0 <= value <= LIMIT or not math.isfinite(value):
        raise ValueError('invalid numeric usage duration')
    return value


def shape(value, keys):
    if not isinstance(value, dict) or len(value) != len(keys) or set(value) != keys:
        raise ValueError('invalid numeric usage fields')


def identity(value, size):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{' + str(size) + '}', value) is None:
        raise ValueError('invalid local usage identity')


def counts(value):
    shape(value, COUNT_KEYS)
    for key in ('attempts', 'not_attempted', 'request_bytes', 'response_bytes'):
        integer(value[key])
    number(value['elapsed_ms'])
    shape(value['outcomes'], {'success', 'error', 'cancelled'})
    for item in value['outcomes'].values():
        integer(item)
    if sum(value['outcomes'].values()) != value['attempts']:
        raise ValueError('inconsistent usage outcomes')
    if not isinstance(value['failures'], dict) or len(value['failures']) > len(FAILURE_CODES):
        raise ValueError('invalid usage failures')
    if not set(value['failures']) <= FAILURE_CODES:
        raise ValueError('invalid usage failure code')
    for item in value['failures'].values():
        integer(item)
    if sum(value['failures'].values()) > value['attempts'] + value['not_attempted']:
        raise ValueError('inconsistent usage failures')
    for key in ('tokens', 'known'):
        shape(value[key], set(TOKEN_FIELDS))
        for item in value[key].values():
            integer(item)
    for key in TOKEN_FIELDS:
        if value['known'][key] > value['attempts'] or value['tokens'][key] > value['known'][key] * MAX_TOKENS:
            raise ValueError('inconsistent reported token usage')


def record(value):
    shape(value, RECORD_KEYS)
    for key in ('attempted', 'streamed'):
        if type(value[key]) is not bool:
            raise ValueError('invalid usage flag')
    if value['outcome'] not in ('success', 'error', 'cancelled'):
        raise ValueError('invalid usage outcome')
    if value['http_status'] is not None and (type(value['http_status']) is not int or not 100 <= value['http_status'] <= 599):
        raise ValueError('invalid usage HTTP status')
    for key in ('request_bytes', 'response_bytes'):
        integer(value[key])
    for key in ('elapsed_ms', 'first_text_ms'):
        if value[key] is not None:
            number(value[key])
    if value['first_text_ms'] is not None and (value['elapsed_ms'] is None or value['first_text_ms'] > value['elapsed_ms']):
        raise ValueError('invalid usage first-text duration')
    if value['usage'] is not None:
        shape(value['usage'], set(TOKEN_FIELDS))
        from .metrics import token_usage
        if token_usage(value['usage']) != value['usage']:
            raise ValueError('invalid reported usage')
    if value['finish_reason'] is not None and value['finish_reason'] not in (*FINISH_REASONS, 'other'):
        raise ValueError('invalid usage finish reason')
    if value['error_code'] is not None and (not isinstance(value['error_code'], str) or value['error_code'] not in FAILURE_CODES):
        raise ValueError('invalid usage failure code')
    if value['attempted'] != (value['elapsed_ms'] is not None):
        raise ValueError('inconsistent usage attempt')
    if not value['attempted'] and (value['http_status'] is not None or value['request_bytes'] or value['response_bytes']
                                   or value['usage'] is not None or value['first_text_ms'] is not None):
        raise ValueError('unattempted request contains delivery measurements')


def usage_state(value):
    if value is None:
        return None
    shape(value, STATE_KEYS)
    if type(value['version']) is not int or value['version'] != 1 or type(value['coverage_gap']) is not bool:
        raise ValueError('unsupported usage state')
    identity(value['id'], 32)
    integer(value['revision']); integer(value['sequence']); integer(value['uncertain'])
    counts(value['counts'])
    settled = value['counts']['attempts'] + value['counts']['not_attempted']
    pending = value['pending']
    if pending is not None:
        shape(pending, {'sequence', 'streamed', 'request_bytes'})
        if type(pending['streamed']) is not bool or integer(pending['sequence']) != value['sequence']:
            raise ValueError('invalid pending usage')
        integer(pending['request_bytes'])
    if settled + value['uncertain'] + int(pending is not None) != value['sequence']:
        raise ValueError('inconsistent usage sequence')
    if value['revision'] < value['sequence']:
        raise ValueError('invalid usage revision')
    recent = value['recent']
    if not isinstance(recent, list) or len(recent) > MAX_RECORDS:
        raise ValueError('usage recent limit exceeded')
    previous = 0
    for row in recent:
        shape(row, {'sequence', 'record'})
        sequence = integer(row['sequence'])
        if not previous < sequence <= value['sequence'] or pending is not None and sequence == value['sequence']:
            raise ValueError('invalid recent usage order')
        previous = sequence
        record(row['record'])
    if len(recent) > settled:
        raise ValueError('inconsistent recent usage count')
    inherited = value['inherited']
    if inherited is not None:
        shape(inherited, {'source_id', 'snapshot_sha256', 'counts', 'uncertain', 'coverage_gap'})
        identity(inherited['source_id'], 32); identity(inherited['snapshot_sha256'], 64)
        if inherited['source_id'] == value['id'] or type(inherited['coverage_gap']) is not bool:
            raise ValueError('invalid inherited usage')
        counts(inherited['counts']); integer(inherited['uncertain'])
    return copy.deepcopy(value)


def measurement_counts(measurements):
    return {key: copy.deepcopy(getattr(measurements, key)) for key in COUNT_KEYS}


def measurement_report(value, *, incomplete=False):
    measurements = Measurements()
    for key, item in value.items():
        setattr(measurements, key, copy.deepcopy(item))
    result = measurements.report()
    result.pop('recent'); result.pop('recent_limit'); result.pop('recent_dropped')
    result['scope'] = 'settled session requests only; copied ancestry is separate; not a bill'
    if incomplete:
        for field in result['usage'].values():
            field['complete_sum'] = None
    return result


class UsageLedger:
    def __init__(self, state=None, *, coverage_gap=False):
        self._lock = threading.RLock()
        self.blocked = False
        self.value = usage_state(state) if state is not None else {
            'version': 1, 'id': uuid.uuid4().hex, 'revision': 0, 'sequence': 0,
            'coverage_gap': coverage_gap, 'counts': measurement_counts(Measurements()),
            'uncertain': 0, 'pending': None, 'recent': [], 'inherited': None}
        self.value = usage_state(self.value)
        if self.value['pending'] is not None:
            # A durable begin does not prove that HTTP was sent or completed.
            self.value['uncertain'] += 1
            self.value['pending'] = None
            self.value['revision'] += 1
            usage_state(self.value)

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self.value)

    def _commit(self, candidate):
        self.value = usage_state(candidate)

    def begin(self, value):
        shape(value, {'streamed', 'request_bytes'})
        if type(value['streamed']) is not bool:
            raise ValueError('invalid usage start flag')
        integer(value['request_bytes'])
        with self._lock:
            if self.blocked or self.value['pending'] is not None:
                raise UsagePersistenceError('Usage receipt is uncertain; restore the saved session before another request.')
            candidate = self.snapshot()
            candidate['revision'] += 1
            candidate['sequence'] += 1
            candidate['pending'] = {'sequence': candidate['sequence'], **value}
            self._commit(candidate)

    def finish(self, value):
        record(value)
        with self._lock:
            candidate = self.snapshot()
            pending = candidate['pending']
            if pending is None:
                if value['attempted']:
                    raise ValueError('settled usage has no start')
                candidate['sequence'] += 1
            elif not value['attempted'] or (pending['streamed'], pending['request_bytes']) != (value['streamed'], value['request_bytes']):
                raise ValueError('usage finish differs from its start')
            measurements = Measurements()
            for key, item in candidate['counts'].items():
                setattr(measurements, key, copy.deepcopy(item))
            measurements.add(value)
            candidate['counts'] = measurement_counts(measurements)
            candidate['pending'] = None
            candidate['revision'] += 1
            candidate['recent'] = [*candidate['recent'], {'sequence': candidate['sequence'], 'record': copy.deepcopy(value)}][-MAX_RECORDS:]
            self._commit(candidate)

    def fork(self):
        source = self.snapshot()
        if self.blocked or source['pending'] is not None:
            raise UsagePersistenceError('Resolve uncertain usage before branching or switching models.')
        result = UsageLedger(coverage_gap=source['coverage_gap'])
        total = copy.deepcopy(source['counts'])
        prior = source['inherited']
        if prior is not None:
            for key in ('attempts', 'not_attempted', 'request_bytes', 'response_bytes', 'elapsed_ms'):
                total[key] += prior['counts'][key]
            for key in ('outcomes', 'failures', 'tokens', 'known'):
                for name, item in prior['counts'][key].items():
                    total[key][name] = total[key].get(name, 0) + item
        result.value['inherited'] = {'source_id': source['id'],
            'snapshot_sha256': hashlib.sha256(json.dumps(source, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
            'counts': total, 'uncertain': source['uncertain'] + (prior['uncertain'] if prior else 0),
            'coverage_gap': source['coverage_gap'] or bool(prior and prior['coverage_gap'])}
        result._commit(result.value)
        return result

    def source_matches(self, original):
        inherited = self.snapshot()['inherited']
        if inherited is None:
            return original is None
        if original is None or original.blocked:
            return False
        raw = json.dumps(original.snapshot(), sort_keys=True, separators=(',', ':')).encode()
        return inherited['snapshot_sha256'] == hashlib.sha256(raw).hexdigest()

    def report(self):
        state = self.snapshot()
        inherited = state['inherited']
        return {**measurement_report(state['counts'], incomplete=bool(state['coverage_gap'] or state['uncertain'] or state['pending'])), 'ledger_id': state['id'],
                'coverage_gap': state['coverage_gap'], 'uncertain_requests': state['uncertain'],
                'pending_request': state['pending'] is not None, 'persistence_blocked': self.blocked,
                'recent': copy.deepcopy(state['recent']), 'recent_limit': MAX_RECORDS,
                'recent_dropped': state['counts']['attempts'] + state['counts']['not_attempted'] - len(state['recent']),
                'inherited_snapshot': None if inherited is None else {
                    'source_id': inherited['source_id'], 'snapshot_sha256': inherited['snapshot_sha256'],
                    'usage': measurement_report(inherited['counts'], incomplete=bool(inherited['coverage_gap'] or inherited['uncertain'])), 'uncertain_requests': inherited['uncertain'],
                    'coverage_gap': inherited['coverage_gap'], 'new_consumption': False}}
