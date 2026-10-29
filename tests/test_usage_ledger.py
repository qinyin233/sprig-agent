"""Session usage survives real transport, failures and process loss without replay."""
from contextlib import closing
from contextlib import redirect_stdout, redirect_stderr
import copy
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sprig_agent.agent import Agent
from sprig_agent import cli
from sprig_agent.config import Config
from sprig_agent.conversation_fork import ConversationFork
from sprig_agent.input_inbox import new_input
from sprig_agent.metrics import MAX_RECORDS
from sprig_agent.provider import Provider, ProviderError
from sprig_agent.session import SessionError, SessionStore
from sprig_agent.tools import Tools
from sprig_agent.usage_ledger import UsageLedger, UsagePersistenceError, usage_state
from tests.test_native_protocols import backend, native, stream_events
from tests.test_streaming import chunk, sse
from tests import test_tui as tui
from textual.widgets import TextArea
from sprig_agent.tui import ApprovalScreen

USAGE = {'prompt_tokens': 9, 'completion_tokens': 3, 'total_tokens': 12}


def measured(**changes):
    return {'attempted': True, 'streamed': False, 'outcome': 'success', 'http_status': 200,
            'request_bytes': 100, 'response_bytes': 200, 'elapsed_ms': 10.0, 'first_text_ms': None,
            'usage': USAGE.copy(), 'finish_reason': 'stop', 'error_code': None, **changes}


def settle(ledger, **changes):
    value = measured(**changes)
    if value['attempted']:
        ledger.begin({'streamed': value['streamed'], 'request_bytes': value['request_bytes']})
    ledger.finish(value)


def reply(protocol, streamed=False, *, failure=False):
    if protocol == 'chat_completions':
        reason = 'length' if failure else 'stop'
        if streamed:
            return sse(chunk({'role': 'assistant', 'content': 'fixture answer'}, reason), {'choices': [], 'usage': USAGE})
        return json.dumps({'choices': [{'finish_reason': reason, 'message': {'role': 'assistant', 'content': 'fixture answer'}}],
                           'usage': USAGE}).encode()
    value = native(protocol, 'fixture answer', reasoning=False)
    if failure:
        if protocol == 'messages':
            value['stop_reason'] = 'max_tokens'
        else:
            value['status'] = 'incomplete'
            value['incomplete_details'] = {'reason': 'max_output_tokens'}
            if streamed:
                events = stream_events(protocol, value)
                events[-1]['type'] = 'response.incomplete'
                return sse(*events)
    return value


class UsageLedgerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.root = self.parent/'workspace'
        self.root.mkdir()
        self.config = Config(self.root, 'fixture', api_key='fixture-private-credential', proxy='direct')
        self.directory = self.parent/'sessions'
        self.history = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'fixture task'}]

    def saved_agent(self, config=None, provider=None, *, name='task', failure=None):
        config = config or self.config
        store = SessionStore(self.directory, config)
        self.addCleanup(store.close)
        tools = Tools(config, self.fail)
        self.addCleanup(tools.close)
        def checkpoint(history):
            state = agent.usage_ledger.snapshot()
            if failure is not None:
                failure(state)
            store.save(name, history, **agent.reference_save_options())
        provider = provider or Provider(config)
        agent = Agent(config, provider, tools, lambda _: None, checkpoint)
        return agent, provider, store

    def test_aggregate_roundtrip_unknown_fields_and_bounded_recent(self):
        ledger = UsageLedger()
        for _ in range(MAX_RECORDS + 3):
            settle(ledger)
        settle(ledger, usage=None)
        state = ledger.snapshot()
        self.assertEqual(len(state['recent']), MAX_RECORDS)
        restored = UsageLedger(state)
        self.assertEqual(restored.snapshot(), state)
        report = restored.report()
        self.assertEqual(report['attempts'], MAX_RECORDS + 4)
        self.assertEqual(report['recent_dropped'], 4)
        self.assertIsNone(report['usage']['total_tokens']['complete_sum'])
        self.assertEqual(report['usage']['total_tokens']['unknown_requests'], 1)
        state['counts']['attempts'] = 0
        self.assertNotEqual(state, restored.snapshot())

    def test_pending_restore_is_unknown_once_and_never_a_success(self):
        ledger = UsageLedger()
        ledger.begin({'streamed': True, 'request_bytes': 123})
        restored = UsageLedger(ledger.snapshot())
        self.assertEqual(restored.report()['uncertain_requests'], 1)
        self.assertEqual(restored.report()['attempts'], 0)
        self.assertIsNone(restored.report()['usage']['total_tokens']['complete_sum'])
        self.assertFalse(restored.report()['pending_request'])
        twice = UsageLedger(restored.snapshot())
        self.assertEqual(twice.report()['uncertain_requests'], 1)
        settle(twice)
        self.assertEqual(twice.report()['attempts'], 1)
        self.assertEqual(twice.report()['uncertain_requests'], 1)

    def test_branches_keep_ancestry_separate_without_recounting_it(self):
        root = UsageLedger()
        settle(root)
        branch = root.fork()
        self.assertTrue(branch.source_matches(root))
        self.assertEqual(branch.report()['attempts'], 0)
        inherited = branch.report()['inherited_snapshot']
        self.assertEqual(inherited['usage']['attempts'], 1)
        self.assertFalse(inherited['new_consumption'])
        offline = Agent(self.config, None, Mock(), lambda _: None)
        offline.restore_usage(root.snapshot())
        self.assertFalse(offline.usage_report()['available'])
        self.assertEqual(offline.usage_report()['session_usage']['attempts'], 1)
        again = branch.fork()
        self.assertEqual(again.report()['inherited_snapshot']['usage']['attempts'], 1)
        settle(branch)
        twice = branch.fork()
        self.assertEqual(twice.report()['inherited_snapshot']['usage']['attempts'], 2)
        settle(root)
        self.assertFalse(branch.source_matches(root))

    def test_invalid_or_unbounded_saved_state_is_rejected(self):
        ledger = UsageLedger(); settle(ledger)
        original = ledger.snapshot()
        def altered(path, value):
            result = copy.deepcopy(original)
            target = result
            for key in path[:-1]: target = target[key]
            target[path[-1]] = value
            return result
        cases = [altered(['version'], True), altered(['id'], 'private-header'), altered(['sequence'], True),
                 altered(['sequence'], 2**53), altered(['counts', 'elapsed_ms'], float('nan')),
                 altered(['counts', 'elapsed_ms'], 10**1000),
                 altered(['counts', 'outcomes', 'success'], 2), altered(['counts', 'tokens', 'total_tokens'], -1),
                 altered(['recent'], original['recent'] * (MAX_RECORDS+1)),
                 altered(['recent', 0, 'record', 'usage'], {'total_tokens': 12, 'prompt': 'secret'}),
                 altered(['recent', 0, 'record', 'error_code'], ['secret']),
                 {**original, 'headers': {'Authorization': 'secret'}}]
        for state in cases:
            with self.subTest(state_keys=list(state)), self.assertRaises((ValueError, TypeError)):
                usage_state(state)

    def test_session_saves_and_input_edits_preserve_latest_usage(self):
        with closing(SessionStore(self.directory, self.config)) as store:
            ledger = UsageLedger(); ledger.begin({'streamed': False, 'request_bytes': 100})
            stale = ledger.snapshot()
            store.save('task', self.history, usage=stale)
            ledger.finish(measured()); current = ledger.snapshot()
            store.save('task', self.history, usage=current)
            entry = new_input('guidance')
            store.enqueue_input('task', entry, history=self.history, usage=stale)
            store.discard_inputs('task')
            store.load('task')
            self.assertEqual(store.loaded_usage, current)
            before = (self.directory/'task.json').read_bytes()
            with self.assertRaisesRegex(SessionError, 'stale'):
                store.save('task', self.history, usage=stale)
            self.assertEqual((self.directory/'task.json').read_bytes(), before)
            store.save('task', self.history)
            store.load('task')
            self.assertEqual(store.loaded_usage, current)

    def test_bad_or_credential_conflicting_usage_never_overwrites_session(self):
        with closing(SessionStore(self.directory, self.config)) as store:
            store.save('task', self.history)
            before = (self.directory/'task.json').read_bytes()
            with self.assertRaises(SessionError):
                store.save('task', self.history, usage={'secret': 'arbitrary text'})
            self.assertEqual((self.directory/'task.json').read_bytes(), before)
            saved = json.loads(before)
            saved['request_usage'] = {'secret': 'arbitrary text'}
            (self.directory/'task.json').write_text(json.dumps(saved), encoding='utf-8')
            with self.assertRaisesRegex(SessionError, 'request usage'):
                store.load('task')
        config = replace(self.config, api_key='a'*32)
        with closing(SessionStore(self.parent/'private', config)) as store:
            state = UsageLedger().snapshot(); state['id'] = config.api_key
            with self.assertRaisesRegex(SessionError, 'credential'):
                store.save('task', self.history, usage=state)
            self.assertFalse((self.parent/'private/task.json').exists())

    def test_legacy_session_has_coverage_gap_without_model_contact(self):
        agent, provider, store = self.saved_agent()
        store.save('task', self.history)
        history = store.load('task')
        agent.restore_usage(store.loaded_usage, history)
        with patch.object(provider, 'complete') as complete:
            report = agent.usage_report()
        complete.assert_not_called()
        self.assertTrue(report['session_usage']['coverage_gap'])
        self.assertEqual(report['session_usage']['attempts'], 0)

    def test_offline_usage_needs_no_original_model_and_never_outputs_history(self):
        with closing(SessionStore(self.directory, self.config)) as store:
            ledger = UsageLedger(); settle(ledger)
            store.save('stats', self.history, usage=ledger.snapshot())
        before = (self.directory/'stats.json').read_bytes()
        output = io.StringIO()
        with patch.object(cli, 'Provider') as provider, redirect_stdout(output):
            self.assertEqual(cli.main(['--no-config', '--session-dir', str(self.directory),
                                       '--workdir', str(self.root), '--session-usage', 'stats']), 0)
        provider.assert_not_called()
        result = json.loads(output.getvalue())
        self.assertEqual(result['attempts'], 1)
        self.assertFalse(result['network_contacted'])
        self.assertNotIn('fixture task', output.getvalue())
        self.assertNotIn(self.config.api_key, output.getvalue())
        self.assertEqual((self.directory/'stats.json').read_bytes(), before)

    def test_offline_usage_honors_busy_lease_and_invalid_names(self):
        with closing(SessionStore(self.directory, self.config)) as store:
            store.save('active', self.history, usage=UsageLedger().snapshot())
            with closing(SessionStore(self.directory, self.config)) as other:
                with self.assertRaisesRegex(SessionError, 'busy'):
                    other.usage('active')
                with self.assertRaises(SessionError):
                    other.usage('../active')

    def test_six_real_http_transports_persist_usage_without_sending_it(self):
        for protocol in ('chat_completions', 'messages', 'responses'):
            for streamed in (False, True):
                with self.subTest(protocol=protocol, stream=streamed):
                    captured = []
                    with backend(protocol, [reply(protocol, streamed)], captured, stream=streamed) as url:
                        config = replace(self.config, base_url=url, wire_api=protocol, stream=streamed, request_usage=streamed)
                        name = protocol + str(int(streamed))
                        agent, provider, store = self.saved_agent(config, name=name)
                        history, complete = agent.run('PRIVATE_TASK')
                        self.assertTrue(complete)
                        state = store.load(name); usage = store.loaded_usage
                        self.assertEqual(usage['counts']['attempts'], 1)
                        self.assertEqual(usage['counts']['tokens']['prompt_tokens'], 9)
                        self.assertEqual(usage['counts']['tokens']['completion_tokens'], 3)
                        self.assertEqual(usage['counts']['tokens']['total_tokens'], 0 if protocol == 'messages' else 12)
                        if protocol == 'messages':
                            self.assertIsNone(agent.usage_ledger.report()['usage']['total_tokens']['complete_sum'])
                        self.assertIsNone(usage['pending'])
                        numeric = json.dumps(usage)
                        for text in ('PRIVATE_TASK', 'fixture answer', config.api_key, url, 'reasoning_content'):
                            self.assertNotIn(text, numeric)
                        self.assertNotIn('request_usage', json.dumps(captured[0]['body']))
                        self.assertNotIn('ledger_id', json.dumps(captured[0]['body']))
                        resumed = Agent(config, Provider(config), agent.tools, lambda _: None)
                        resumed.restore_usage(usage, state)
                        with patch.object(resumed.provider, 'complete') as request:
                            report = resumed.usage_report()
                        request.assert_not_called()
                        self.assertEqual(report['attempts'], 0)
                        self.assertEqual(report['session_usage']['attempts'], 1)
                        self.assertFalse(report['session_usage']['coverage_gap'])
                        store.close()

    def test_six_output_limit_transports_preserve_failure_and_do_not_retry(self):
        for protocol in ('chat_completions', 'messages', 'responses'):
            for streamed in (False, True):
                with self.subTest(protocol=protocol, stream=streamed):
                    captured = []
                    with backend(protocol, [reply(protocol, streamed, failure=True)], captured, stream=streamed) as url:
                        config = replace(self.config, base_url=url, wire_api=protocol, stream=streamed, request_usage=streamed)
                        name = protocol + '-failure-' + str(int(streamed))
                        agent, provider, store = self.saved_agent(config, name=name)
                        with self.assertRaises(ProviderError):
                            agent.run('fixture')
                        store.load(name)
                        self.assertEqual(store.loaded_usage['counts']['outcomes']['error'], 1)
                        self.assertEqual(store.loaded_usage['counts']['failures'], {'output_limit': 1})
                        self.assertEqual(len(captured), 1)
                        self.assertIsNone(store.loaded_usage['pending'])
                        self.assertFalse(agent.usage_ledger.blocked)
                        store.close()

    def test_start_save_failure_blocks_http_and_later_continuation(self):
        def fail(state):
            if state['pending'] is not None: raise OSError('fixture start save failure')
        agent, provider, store = self.saved_agent(failure=fail)
        provider.opener = Mock()
        with self.assertRaises(UsagePersistenceError):
            agent.run('fixture')
        provider.opener.open.assert_not_called()
        self.assertTrue(agent.usage_report()['session_usage']['persistence_blocked'])
        with self.assertRaises(UsagePersistenceError):
            agent.run('continue')
        provider.opener.open.assert_not_called()

    def test_finish_save_failure_keeps_durable_start_and_never_executes_tool(self):
        def fail(state):
            if state['counts']['attempts']: raise OSError('fixture finish save failure')
        agent, provider, store = self.saved_agent(failure=fail)
        provider.opener = Mock()
        tool = {'id': 'must-not-run', 'type': 'function', 'function': {'name': 'run', 'arguments': '{"argv":["must-not-run"]}'}}
        provider.opener.open.return_value = io.BytesIO(json.dumps({'choices': [{'finish_reason': 'tool_calls', 'message': {
            'role': 'assistant', 'content': '', 'tool_calls': [tool]}}], 'usage': USAGE}).encode())
        with patch.object(agent.tools, 'execute') as execute, self.assertRaises(UsagePersistenceError):
            agent.run('fixture')
        execute.assert_not_called()
        self.assertEqual(provider.measurements.report()['outcomes']['success'], 1)
        history = store.load('task')
        self.assertIsNotNone(store.loaded_usage['pending'])
        self.assertFalse(any(row.get('tool_calls') for row in history))
        agent.restore_usage(store.loaded_usage, history)
        self.assertEqual(agent.usage_report()['session_usage']['uncertain_requests'], 1)
        self.assertFalse(agent.usage_ledger.blocked)

    def test_timeout_with_finish_save_failure_retains_original_failure_code(self):
        def fail(state):
            if state['counts']['attempts']: raise OSError('fixture save failure')
        agent, provider, store = self.saved_agent(failure=fail)
        provider.opener = Mock(); provider.opener.open.side_effect = TimeoutError()
        with self.assertRaisesRegex(UsagePersistenceError, 'Provider failure: timeout'):
            agent.run('fixture')
        self.assertEqual(provider.measurements.report()['failures'], {'timeout': 1})
        self.assertEqual(agent.usage_ledger.snapshot()['counts']['failures'], {'timeout': 1})

    def test_retries_are_individual_settlements_and_missing_usage_stays_unknown(self):
        config = replace(self.config, retry_attempts=1)
        agent, provider, store = self.saved_agent(config)
        provider.opener = Mock()
        successful = io.BytesIO(reply('chat_completions')); successful.status = 200
        provider.opener.open.side_effect = [urllib.error.HTTPError('http://localhost', 503, 'fixture', {}, io.BytesIO()), successful]
        with patch('sprig_agent.provider.random.uniform', return_value=0):
            agent.run('fixture')
        store.load('task')
        ledger = UsageLedger(store.loaded_usage)
        report = ledger.report()
        self.assertEqual(report['attempts'], 2)
        self.assertEqual(report['outcomes'], {'success': 1, 'error': 1, 'cancelled': 0})
        self.assertIsNone(report['usage']['total_tokens']['complete_sum'])
        self.assertEqual([row['record']['http_status'] for row in report['recent']], [503, 200])

    def test_ledger_change_invalidates_reviewed_branch(self):
        agent, provider, store = self.saved_agent()
        settle(agent.usage_ledger)
        proposal = ConversationFork(agent, self.history, 'current')
        self.assertTrue(proposal.preflight(lambda _: True, self.parent/'branches'))
        settle(agent.usage_ledger)
        with self.assertRaisesRegex(ValueError, 'stale'):
            proposal.commit(self.parent/'branches')

    def test_model_switch_preserves_source_usage_without_new_consumption(self):
        from sprig_agent.model_selection import ModelSwitch
        from tests.test_model_selection import configured
        config, selection, _, _ = configured(self.root)
        with closing(Tools(config, self.fail)) as tools:
            original = Agent(config, Provider(config), tools, lambda _: None)
            settle(original.usage_ledger)
            candidate = ModelSwitch(selection, original, self.history, 'b/main', saving=True)
            directory = self.parent/'models'
            self.assertTrue(candidate.preflight(lambda _: True, directory))
            agent, history, store, name = candidate.commit(directory)
            with closing(store):
                store.load(name)
                self.assertEqual(store.loaded_usage['counts']['attempts'], 0)
                self.assertEqual(store.loaded_usage['inherited']['counts']['attempts'], 1)
                self.assertEqual(agent.usage_report()['session_usage']['attempts'], 0)
                self.assertEqual(original.usage_report()['session_usage']['attempts'], 1)
                self.assertEqual(agent.provider.measurements.report()['attempts'], 0)
                self.assertNotIn('request_usage', json.dumps(history))

    def test_cancelled_transport_is_settled_without_retry(self):
        agent, provider, store = self.saved_agent()
        provider.opener = Mock(); provider.opener.open.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            agent.run('fixture')
        store.load('task')
        self.assertEqual(store.loaded_usage['counts']['outcomes']['cancelled'], 1)
        self.assertIsNone(store.loaded_usage['pending'])
        self.assertFalse(agent.usage_ledger.blocked)
        provider.opener.open.assert_called_once()

    def test_sync_failure_after_replacement_restores_observed_disk_state(self):
        agent, provider, store = self.saved_agent()
        provider.opener = Mock(); provider.opener.open.return_value = io.BytesIO(reply('chat_completions'))
        real_sync = store.handle.sync
        def sync():
            if agent.usage_ledger.snapshot()['counts']['attempts']:
                raise OSError('fixture durability uncertainty')
            real_sync()
        with patch.object(store.handle, 'sync', side_effect=sync), self.assertRaises(UsagePersistenceError):
            agent.run('fixture')
        self.assertTrue(agent.usage_ledger.blocked)
        history = store.load('task')
        agent.restore_usage(store.loaded_usage, history)
        self.assertEqual(agent.usage_report()['session_usage']['attempts'], 1)
        self.assertEqual(agent.usage_report()['session_usage']['uncertain_requests'], 0)
        self.assertFalse(agent.usage_ledger.blocked)

    def test_cli_resume_metrics_separate_current_process_and_saved_session(self):
        config = replace(self.config, base_url='http://localhost/v1')
        args = ['--no-config', '--cli', '--model', config.model, '--base-url', config.base_url,
                '--workdir', str(self.root), '--session-dir', str(self.directory), '--metrics']
        reports = []
        with patch.dict(os.environ, {'SPRIG_API_KEY': config.api_key}):
            for extra in (['fixture task', '--save-session', 'stats'], ['--resume', 'stats']):
                provider = Provider(config); provider.opener = Mock()
                provider.opener.open.return_value = io.BytesIO(reply('chat_completions'))
                output, errors = io.StringIO(), io.StringIO()
                with patch.object(cli, 'Provider', return_value=provider), redirect_stdout(output), redirect_stderr(errors):
                    self.assertEqual(cli.main([*args, *extra]), 0)
                line = next(row for row in errors.getvalue().splitlines() if row.startswith('[usage] '))
                reports.append(json.loads(line[len('[usage] '):]))
                self.assertEqual(provider.opener.open.call_count, 1)
        self.assertEqual([report['attempts'] for report in reports], [1, 1])
        self.assertEqual([report['session_usage']['attempts'] for report in reports], [1, 2])
        self.assertEqual(reports[0]['session_usage']['ledger_id'], reports[1]['session_usage']['ledger_id'])

    def test_killed_process_restores_an_unknown_request_without_replay(self):
        received, release = threading.Event(), threading.Event()
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                requests.append(self.path); received.set()
                release.wait(8)
                body = reply('chat_completions')
                try:
                    self.send_response(200); self.send_header('Content-Length', str(len(body)))
                    self.end_headers(); self.wfile.write(body)
                except OSError:
                    pass
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        source = str(Path(__file__).resolve().parents[1]/'src')
        url = f'http://127.0.0.1:{server.server_port}/v1'
        script = """import sys,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.provider import Provider
from sprig_agent.session import SessionStore
from sprig_agent.tools import Tools
c=Config(Path(sys.argv[2]),'fixture',base_url=sys.argv[4],proxy='direct')
s=SessionStore(Path(sys.argv[3]),c)
t=Tools(c,lambda _:False)
a=Agent(c,Provider(c),t,lambda _:None,lambda h:s.save('killed',h,**a.reference_save_options()))
a.run('fixture')
"""
        child = subprocess.Popen([sys.executable, '-I', '-B', '-c', script, source, str(self.root), str(self.directory), url],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertTrue(received.wait(6), 'child did not reach the local HTTP fixture')
            child.kill(); child.communicate(timeout=5)
            config = replace(self.config, api_key='', base_url=url)
            with closing(SessionStore(self.directory, config)) as store:
                history = store.load('killed')
                self.assertIsNotNone(store.loaded_usage['pending'])
                with closing(Tools(config, self.fail)) as tools:
                    provider = Provider(config)
                    agent = Agent(config, provider, tools, lambda _: None)
                    agent.restore_usage(store.loaded_usage, history)
                    with patch.object(provider, 'complete') as complete:
                        report = agent.usage_report()['session_usage']
                    complete.assert_not_called()
                    self.assertEqual(report['uncertain_requests'], 1)
                    self.assertEqual(report['attempts'], 0)
            self.assertEqual(len(requests), 1)
        finally:
            if child.poll() is None: child.kill()
            child.communicate(timeout=5)
            release.set(); server.shutdown(); server.server_close(); thread.join(3)


class UsageLedgerTuiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = tui.TuiTests.asyncSetUp
    setUp = tui.TuiTests.setUp
    app = tui.TuiTests.app
    until = tui.TuiTests.until
    click = tui.TuiTests.click
    send_text = tui.TuiTests.send_text

    async def test_enter_save_resume_and_usage_are_local_with_draft_preserved(self):
        provider = Provider(self.config); provider.opener = Mock()
        provider.opener.open.return_value = io.BytesIO(reply('chat_completions', True))
        app = self.app(provider, save_session='stats')
        async with app.run_test(size=(80, 24)) as pilot:
            await self.send_text(pilot, 'fixture task')
            await self.until(pilot, lambda: not app.busy and app.messages is not None)
            self.assertEqual(app.agent.usage_report()['session_usage']['attempts'], 1)
        app.close()
        forbidden = Provider(self.config); forbidden.opener = Mock()
        restored = self.app(forbidden, resume='stats')
        async with restored.run_test(size=(80, 24)) as pilot:
            editor = restored.query_one('#task', TextArea)
            editor.load_text('UNSENT_DRAFT')
            before = copy.deepcopy(restored.messages)
            await self.click(pilot, '#usage')
            self.assertEqual(editor.text, 'UNSENT_DRAFT')
            self.assertEqual(restored.messages, before)
            self.assertEqual(restored.agent.usage_report()['attempts'], 0)
            self.assertEqual(restored.agent.usage_report()['session_usage']['attempts'], 1)
            self.assertIn('"session_usage"', restored.transcript)
            forbidden.opener.open.assert_not_called()

    async def test_new_conversation_starts_own_usage_and_keeps_saved_stats(self):
        provider = Provider(self.config); provider.opener = Mock()
        provider.opener.open.side_effect = [io.BytesIO(reply('chat_completions', True)), io.BytesIO(reply('chat_completions', True))]
        app = self.app(provider, save_session='source')
        async with app.run_test(size=(80, 24)) as pilot:
            await self.send_text(pilot, 'first task')
            await self.until(pilot, lambda: not app.busy and app.messages is not None)
            original = (self.parent/'sessions/source.json').read_bytes()
            app.new_conversation()
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await self.click(pilot, '#approve')
            await self.until(pilot, lambda: app.messages is None)
            self.assertEqual(app.agent.usage_report()['session_usage']['attempts'], 0)
            await self.send_text(pilot, 'second task')
            await self.until(pilot, lambda: not app.busy and app.messages is not None)
            self.assertEqual(app.agent.usage_report()['attempts'], 2)
            self.assertEqual(app.agent.usage_report()['session_usage']['attempts'], 1)
            self.assertEqual((self.parent/'sessions/source.json').read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
