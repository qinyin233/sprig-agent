"""Explicit HTTP retry policy, real transports and unchanged action authorization."""
from contextlib import closing
from dataclasses import replace
from email.message import Message
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import urllib.error

from jsonschema import Draft202012Validator
from sprig_agent import cli
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.configuration_schema import configuration_schema
from sprig_agent.model_selection import ModelSelection
from sprig_agent.provider import Provider, ProviderError, http_failure
from sprig_agent.session import SessionError, SessionStore
from sprig_agent.settings import load_settings, public_config
from sprig_agent.tools import Tools
from tests import test_settings as settings_fixture
from tests.test_provider_recovery import HTTPFixture, stream
from tests.test_transport import envelope, peer, reply
from tests.test_summary import history


def transient(status=503, headers=None):
    return urllib.error.HTTPError('https://example.test', status, 'private reason', headers or {}, Mock())


class ProviderRetryTests(HTTPFixture, unittest.TestCase):
    def test_six_protocol_chains_retry_identical_bytes_then_edit_and_independently_verify(self):
        for protocol in ('chat_completions', 'messages', 'responses'):
            for streaming in (False, True):
                with self.subTest(protocol=protocol, streaming=streaming):
                    self.requests.clear()
                    (self.root / 'a.txt').write_bytes(b'old')
                    rows = [(503, [], b'private gateway body'),
                            (200, [], envelope(protocol, '', [('read', 'read_file', {'path': 'a.txt'})], streaming)),
                            (429, [('Retry-After', '0')], b'private quota body'),
                            (200, [], envelope(protocol, '', [('edit', 'edit_file', {'path': 'a.txt', 'old': 'old', 'new': 'new'})], streaming)),
                            (200, [], envelope(protocol, streamed=streaming))]
                    config = replace(self.start_server(rows, streaming=streaming), wire_api=protocol, retry_attempts=1,
                                     checks=((sys.executable, '-I', '-B', '-c', "from pathlib import Path;assert Path('a.txt').read_text()=='new'"),),
                                     check_after_edit=True, max_steps=3)
                    approvals, output, waits = [], [], []
                    with closing(Tools(config, lambda text: approvals.append(text) or True)) as tools:
                        provider = Provider(config)
                        with patch.object(provider, '_wait_retry', side_effect=lambda *args: waits.append(args)):
                            messages, complete = Agent(config, provider, tools, output.append).run('Implement and verify')
                    self.assertTrue(complete)
                    self.assertEqual((len(self.requests), len(approvals), len(waits)), (5, 3, 2))
                    self.assertEqual(self.requests[0], self.requests[1])
                    self.assertEqual(self.requests[2], self.requests[3])
                    self.assertEqual((self.root / 'a.txt').read_bytes(), b'new')
                    self.assertEqual(provider.measurements.report()['outcomes'], {'success': 3, 'error': 2, 'cancelled': 0})
                    self.assertEqual(sum('[provider retry]' in text for text in output), 2)
                    self.assertNotIn('gateway body', json.dumps(messages))
                    self.assertNotIn('provider retry', json.dumps(messages))
        self.assertEqual(self.server_errors, [])

    def test_default_zero_and_non_transient_http_never_resend_or_read_error_bodies(self):
        for retries, status in [(0, 503), *[(5, code) for code in (400, 401, 403, 404, 408, 409, 413, 422, 501, 505)]]:
            with self.subTest(retries=retries, status=status):
                provider = Provider(Config(self.root, 'fixture', retry_attempts=retries))
                error = transient(status)
                provider.opener = Mock(open=Mock(side_effect=error))
                with self.assertRaises(ProviderError), patch.object(provider, '_wait_retry', side_effect=self.fail):
                    provider.complete([], [])
                provider.opener.open.assert_called_once()
                error.fp.read.assert_not_called()
                error.fp.close.assert_called_once()

    def test_retry_limit_exponential_gaps_and_unknown_usage_are_preserved(self):
        provider = Provider(Config(self.root, 'fixture', retry_attempts=5))
        provider.opener = Mock(open=Mock(side_effect=lambda *a, **kw: (_ for _ in ()).throw(transient())))
        waits, notices = [], []
        with patch('sprig_agent.provider.random.uniform', side_effect=lambda low, high: low), \
                patch.object(provider, '_wait_retry', side_effect=lambda *args: waits.append(args)):
            with self.assertRaisesRegex(ProviderError, 'additional HTTP attempts: 5'):
                provider.complete([], [], on_retry=notices.append)
        self.assertEqual(provider.opener.open.call_count, 6)
        self.assertEqual([row[0] for row in waits], [1, 2, 4, 8, 10])
        self.assertEqual(len({row[1] for row in waits}), 1)
        self.assertEqual([row['attempt'] for row in notices], [2, 3, 4, 5, 6])
        report = provider.measurements.report()
        self.assertEqual((report['attempts'], report['outcomes']['error']), (6, 6))
        self.assertEqual(report['usage']['total_tokens']['unknown_requests'], 6)
        self.assertIsNone(report['usage']['total_tokens']['complete_sum'])

    def test_each_eligible_status_recovers_without_hiding_failed_usage_or_reading_body(self):
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                provider = Provider(Config(self.root, 'fixture', retry_attempts=1))
                error = transient(status)
                payload = {'choices': [{'message': {'role': 'assistant', 'content': 'done'}, 'finish_reason': 'stop'}],
                           'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}}
                provider.opener = Mock(open=Mock(side_effect=[error, io.BytesIO(json.dumps(payload).encode())]))
                with patch.object(provider, '_wait_retry'):
                    self.assertEqual(provider.complete([], [])['content'], 'done')
                error.fp.read.assert_not_called()
                error.fp.close.assert_called_once()
                report = provider.measurements.report()
                self.assertEqual(report['outcomes'], {'success': 1, 'error': 1, 'cancelled': 0})
                self.assertEqual(report['usage']['total_tokens'], {'reported_sum': 5, 'known_requests': 1,
                                                                  'unknown_requests': 1, 'complete_sum': None})

    def test_retry_after_is_a_minimum_and_invalid_or_ambiguous_hints_refuse_resend(self):
        for raw in ('bad-private-text', '99999', '-1', '1.5', '86401', '1, 2', '',
                    'Sun, 25 Oct 2026 08:30:00 GMT trailing'):
            error = http_failure(503, {'Retry-After': raw})
            self.assertFalse(error.retryable)
            if raw: self.assertNotIn(raw, str(error))
        headers = Message()
        headers['Retry-After'] = '1'
        headers['Retry-After'] = '2'
        self.assertFalse(http_failure(429, headers).retryable)
        for hints in ({'Retry-After': '30'}, headers, {'Retry-After': 'bad'}, {'Retry-After': '86401'}):
            provider = Provider(Config(self.root, 'fixture', retry_attempts=2, request_deadline=3))
            provider.opener = Mock(open=Mock(side_effect=transient(503, hints)))
            with self.assertRaises(ProviderError), patch.object(provider, '_wait_retry', side_effect=self.fail):
                provider.complete([], [])
            provider.opener.open.assert_called_once()
        rows = [(503, [('Retry-After', '2')], b'private'), (200, [], envelope('chat_completions'))]
        config = replace(self.start_server(rows, streaming=False), retry_attempts=1)
        provider = Provider(config)
        with patch.object(provider, '_wait_retry') as wait:
            provider.complete([], [])
        self.assertEqual(wait.call_args.args[0], 2)

    def test_actual_backoff_cancellation_sends_no_second_request(self):
        config = replace(self.start_server([(503, [], b'private')], streaming=False), retry_attempts=5)
        provider, cancel = Provider(config), threading.Event()
        timers = []
        def checking():
            if cancel.is_set(): raise KeyboardInterrupt
        def notice(_):
            timer = threading.Timer(0.05, cancel.set)
            timers.append(timer)
            timer.start()
        started = time.monotonic()
        with self.assertRaises(KeyboardInterrupt):
            provider.complete([], [], check_cancelled=checking, on_retry=notice)
        for timer in timers: timer.join(2)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(len(self.requests), 1)
        report = provider.measurements.report()
        self.assertEqual((report['attempts'], report['not_attempted']), (1, 1))
        self.assertEqual(report['recent'][-1]['outcome'], 'cancelled')

    def test_all_attempts_and_wait_share_one_real_deadline(self):
        count = []
        def script(handler, stopped):
            count.append(True)
            if len(count) == 1:
                handler.send_response(503)
                handler.send_header('Content-Length', '0')
                handler.end_headers()
            else:
                stopped.wait(2)
                reply(handler, envelope('chat_completions'))
        with peer(script) as (url, captured, _, _, _):
            provider = Provider(Config(self.root, 'fixture', base_url=url, proxy='direct', retry_attempts=5,
                                       request_deadline=1.4, request_timeout=3))
            started = time.monotonic()
            with patch('sprig_agent.provider.random.uniform', return_value=1), self.assertRaises(ProviderError) as caught:
                provider.complete([], [])
            self.assertEqual(caught.exception.code, 'request_deadline')
            self.assertEqual(len(captured), 2)
            self.assertGreaterEqual(time.monotonic() - started, 1.2)
            self.assertLess(time.monotonic() - started, 2.5)

    def test_retry_reuses_serialized_body_even_if_callers_mutate_inputs_during_wait(self):
        config = replace(self.start_server([(503, [], b''), (200, [], envelope('chat_completions'))], streaming=False), retry_attempts=1)
        provider = Provider(config)
        messages, tools = [{'role': 'user', 'content': 'original task'}], []
        def notice(_):
            messages[0]['content'] = 'different task'
            tools.append({'injected': True})
        with patch.object(provider, '_wait_retry'):
            provider.complete(messages, tools, on_retry=notice)
        self.assertEqual(self.requests[0], self.requests[1])
        self.assertEqual(self.requests[1]['messages'][0]['content'], 'original task')

    def test_successful_status_malformed_json_output_limit_and_incomplete_sse_never_retry(self):
        for protocol in ('chat_completions', 'messages', 'responses'):
            for streaming in (False, True):
                body = envelope(protocol, streamed=streaming)
                body = body[:-20] if streaming else b'{broken'
                config = replace(self.start_server([(200, [], body)], streaming=streaming), wire_api=protocol, retry_attempts=5)
                before = len(self.requests)
                with self.assertRaises(ProviderError), patch.object(Provider, '_wait_retry', side_effect=self.fail):
                    Provider(config).complete([], [])
                self.assertEqual(len(self.requests) - before, 1)
        config = replace(self.start_server([(200, [], stream({'content': 'partial'}, 'length'))]), retry_attempts=5)
        provider = Provider(config)
        with self.assertRaises(ProviderError) as caught:
            provider.complete([], [])
        self.assertEqual(caught.exception.code, 'output_limit')

    def test_configuration_change_during_backoff_refuses_new_destination(self):
        config = replace(self.start_server([(503, [], b'private')], streaming=False), retry_attempts=1)
        provider = Provider(config)
        def notice(_):
            provider.config = replace(config, base_url='https://different.example/v1')
        with self.assertRaises(ProviderError) as caught:
            provider.complete([], [], on_retry=notice)
        self.assertEqual(caught.exception.code, 'invalid_request')
        self.assertEqual(len(self.requests), 1)
        provider = Provider(config)
        provider.opener = Mock()
        actual = provider._request_body
        def prepare(*args):
            provider.config = replace(config, base_url='https://different.example/v1')
            return actual(*args)
        with patch.object(provider, '_request_body', side_effect=prepare), self.assertRaises(ProviderError):
            provider.complete([], [])
        provider.opener.open.assert_not_called()

    def test_network_and_tls_errors_and_request_budget_never_retry(self):
        import ssl
        for error in (TimeoutError('private'), OSError('private'), ssl.SSLCertVerificationError('private')):
            provider = Provider(Config(self.root, 'fixture', retry_attempts=5))
            provider.opener = Mock(open=Mock(side_effect=error))
            with self.assertRaises(ProviderError), patch.object(provider, '_wait_retry', side_effect=self.fail):
                provider.complete([], [])
            provider.opener.open.assert_called_once()
        with self.assertRaises(ProviderError), patch.object(provider.opener, 'open') as opened:
            provider.complete([{'role': 'user', 'content': 'x' * 200000}], [])
        opened.assert_not_called()

    def test_summary_and_metadata_keep_their_single_request_contract(self):
        config = replace(self.start_server([(503, [], b'private')], streaming=False), retry_attempts=5, max_context_bytes=16000)
        with closing(Tools(config, lambda _: True)) as tools:
            agent = Agent(config, Provider(config), tools, lambda _: None)
            with self.assertRaises(ProviderError), patch.object(Provider, '_wait_retry', side_effect=self.fail):
                agent.summarize(history())
        self.assertEqual(len(self.requests), 1)
        provider = Provider(config)
        provider.opener = Mock(open=Mock(side_effect=transient()))
        with self.assertRaises(ProviderError), patch.object(provider, '_wait_retry', side_effect=self.fail):
            provider.list_models()
        provider.opener.open.assert_called_once()


class RetrySettingsTests(unittest.TestCase):
    setUp = settings_fixture.SettingsTests.setUp
    write = settings_fixture.SettingsTests.write
    invoke = settings_fixture.SettingsTests.invoke

    def test_private_provider_cli_sources_switch_binding_and_schema(self):
        self.write('provider="x"\n[providers.x]\nmodel="fixture"\nretry_attempts=2\n[providers.y]\nmodel="other"\nretry_attempts=1\n')
        settings = load_settings(self.path)
        config, metadata = settings.resolve({'workdir': self.root}, {})
        self.assertEqual((config.retry_attempts, dict(config.setting_sources)['retry_attempts']), (2, 'provider'))
        self.assertEqual(public_config(config, metadata)['transport']['retry_attempts'], 2)
        override, _ = settings.resolve({'workdir': self.root, 'retry_attempts': 0}, {})
        self.assertEqual((override.retry_attempts, dict(override.setting_sources)['retry_attempts']), (0, 'CLI'))
        selected = ModelSelection(settings, {}, {}, config).resolve(config, 'y')
        self.assertEqual(selected.retry_attempts, 1)
        self.assertEqual(config.binding()['retry_attempts'], 2)
        self.assertNotIn('retry_attempts', replace(config, retry_attempts=0).binding())
        schema = configuration_schema()
        self.assertEqual(schema, json.loads((Path(__file__).resolve().parents[1] / 'config.schema.json').read_text('utf-8')))
        for value in (True, False, -1, 6, '1', 1.2, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(config, retry_attempts=value)
            with self.assertRaises(Exception):
                Draft202012Validator(schema).validate({'providers': {'x': {'retry_attempts': value}}})
        with closing(SessionStore(self.parent / 'sessions', config)) as store:
            store.save('saved', [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'task'}])
        with closing(SessionStore(self.parent / 'sessions', replace(config, retry_attempts=1))) as store:
            with self.assertRaises(SessionError): store.load('saved')

    def test_cli_flag_runs_real_retry_and_invalid_configuration_stays_offline(self):
        seen = []
        def script(handler, stopped):
            seen.append(True)
            if len(seen) == 1:
                handler.send_response(503)
                handler.send_header('Content-Length', '0')
                handler.end_headers()
            else:
                reply(handler, envelope('chat_completions'))
        with peer(script) as (url, captured, _, _, _), patch.object(Provider, '_wait_retry'):
            status, output, error = self.invoke(['--no-config', '--base-url', url, '--proxy', 'direct', '--model', 'fixture',
                                                 '--retry-attempts', '1', 'Explain'])
            self.assertEqual(status, 0, error)
            self.assertEqual(len(captured), 2)
            self.assertIn('[provider retry]', output)
        self.write('provider="x"\n[providers.x]\nmodel="fixture"\nretry_attempts=6\n')
        with patch.object(cli, 'Provider') as factory:
            self.assertEqual(self.invoke(['--config', str(self.path), 'task'])[0], 1)
        factory.assert_not_called()
