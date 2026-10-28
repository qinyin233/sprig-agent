"""Exercise the HTTP boundary with the actual pinned HTTPX implementation."""

import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

import httpx

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'benchmarks/clients'))
from mini_transport import MeasuredTransport, TransportPolicyError
from sprig_agent.provider import MAX_RESPONSE


class Body(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks, self.reads, self.closed = chunks, 0, False

    def __iter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    def close(self):
        self.closed = True


class MiniTransportTests(unittest.TestCase):
    def test_network_failures_have_fixed_numeric_reasons_without_retry(self):
        for kind in (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout,
                     httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError):
            calls = []
            def failed(request):
                calls.append(True)
                raise kind('fixture-private-marker', request=request)
            reason = 'timeout' if issubclass(kind, httpx.TimeoutException) else 'connection_error'
            with self.subTest(kind=kind.__name__):
                transport = MeasuredTransport(SimpleNamespace(base_url='https://example.invalid/v1', max_context_bytes=4096),
                                              inner=httpx.MockTransport(failed))
                self.addCleanup(transport.close)
                with self.assertRaises(TransportPolicyError) as caught:
                    self.send(transport)
                self.assertEqual(caught.exception.reason, reason)
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertEqual(calls, [True])
                report = transport.report()
                self.assertEqual(report['attempts'], 1)
                self.assertEqual(report['outcomes']['error'], 1)
                self.assertEqual(report['failures'], {reason: 1})
                self.assertEqual(report['recent'][0]['policy_failure'], reason)
                self.assertIsNone(report['recent'][0]['usage'])
                self.assertNotIn('fixture-private-marker', str(caught.exception) + json.dumps(report))

    def test_partial_body_timeout_closes_stream_and_keeps_usage_unknown(self):
        body = b'{"text":"fixture-private-marker",'
        class FailedBody(Body):
            def __iter__(self):
                yield body
                raise httpx.ReadTimeout('fixture-private-marker')
        stream = FailedBody([])
        transport = MeasuredTransport(SimpleNamespace(base_url='https://example.invalid/v1', max_context_bytes=4096),
            inner=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)))
        self.addCleanup(transport.close)
        with self.assertRaises(TransportPolicyError) as caught:
            self.send(transport)
        self.assertEqual(caught.exception.reason, 'timeout')
        self.assertTrue(stream.closed)
        report = transport.report()
        self.assertEqual(report['response_bytes'], len(body))
        self.assertEqual(report['recent'][0]['http_status'], 200)
        self.assertIsNone(report['recent'][0]['usage'])
        self.assertEqual(report['failures'], {'timeout': 1})
        self.assertNotIn('fixture-private-marker', json.dumps(report))

    def transport(self, body=b'{}', *, status=200, headers=None, cap=4096):
        stream = Body([body] if isinstance(body, bytes) else body)
        calls = []
        def respond(request):
            calls.append(len(request.content))
            return httpx.Response(status, headers=headers, stream=stream)
        transport = MeasuredTransport(SimpleNamespace(base_url='https://example.invalid/v1', max_context_bytes=cap),
                                      inner=httpx.MockTransport(respond))
        self.addCleanup(transport.close)
        return transport, stream, calls

    def send(self, transport, body=b'{}', **kwargs):
        return transport.handle_request(httpx.Request('POST', str(transport.url), content=body, **kwargs))

    def test_exact_serialized_byte_boundary_includes_multibyte_text(self):
        body = json.dumps({'task': '中文🙂'}, ensure_ascii=False).encode('utf-8')
        transport, stream, calls = self.transport(cap=len(body))
        response = self.send(transport, body)
        self.assertEqual(response.content, b'{}')
        self.assertTrue(stream.closed)
        with self.assertRaises(TransportPolicyError) as caught:
            self.send(transport, body + b' ')
        self.assertEqual(caught.exception.reason, 'request_budget_exceeded')
        self.assertEqual(calls, [len(body)])
        report = transport.report()
        self.assertEqual(report['attempts'], 1)
        self.assertEqual(report['not_attempted'], 1)
        self.assertEqual(report['recent'][1]['request_bytes'], len(body) + 1)

    def test_unread_request_stream_is_rejected_without_consuming_it(self):
        transport, _, calls = self.transport()
        consumed = []
        def stream():
            consumed.append(True)
            yield b'unknown'
        request = httpx.Request('POST', str(transport.url), content=stream())
        with self.assertRaises(TransportPolicyError) as caught:
            transport.handle_request(request)
        self.assertEqual(caught.exception.reason, 'request_body_rejected')
        self.assertFalse(consumed)
        self.assertFalse(calls)
        self.assertEqual(transport.report()['not_attempted'], 1)

    def test_other_endpoint_is_rejected_before_network(self):
        transport, _, calls = self.transport()
        with self.assertRaises(TransportPolicyError):
            transport.handle_request(httpx.Request('POST', 'https://other.invalid/v1/chat/completions', content=b'{}'))
        self.assertFalse(calls)
        self.assertEqual(transport.report()['attempts'], 0)

    def test_redirect_is_never_followed_and_body_is_never_read(self):
        transport, stream, calls = self.transport(b'fixture-private-marker', status=307,
                                                 headers={'Location': 'https://other.invalid/secret'})
        with self.assertRaises(TransportPolicyError) as caught:
            self.send(transport)
        self.assertEqual(caught.exception.reason, 'redirect_rejected')
        self.assertEqual(len(calls), 1)
        self.assertEqual(stream.reads, 0)
        self.assertTrue(stream.closed)
        self.assertEqual(transport.report()['response_bytes'], 0)
        self.assertNotIn('other.invalid', json.dumps(transport.report()))
        self.assertNotIn('fixture-private-marker', json.dumps(transport.report()))

    def test_compressed_bodies_are_rejected_before_decompression(self):
        transport, stream, _ = self.transport(b'fixture-private-marker', headers={'Content-Encoding': 'gzip'})
        with self.assertRaises(TransportPolicyError) as caught:
            self.send(transport)
        self.assertEqual(caught.exception.reason, 'content_encoding_rejected')
        self.assertEqual(stream.reads, 0)
        self.assertTrue(stream.closed)

    def test_error_body_is_not_read_or_recorded(self):
        transport, stream, _ = self.transport(b'fixture-private-marker', status=500)
        with self.assertRaises(TransportPolicyError) as caught:
            self.send(transport)
        self.assertEqual(caught.exception.reason, 'http_error')
        self.assertEqual(stream.reads, 0)
        self.assertTrue(stream.closed)
        self.assertNotIn('fixture-private-marker', json.dumps(transport.report()))

    def test_response_boundary_and_overflow_close_the_stream(self):
        # A legal JSON body at exactly 2 MiB must be accepted, not truncated.
        body = b'{"text":"' + b'x' * (MAX_RESPONSE - 11) + b'"}'
        self.assertEqual(len(body), MAX_RESPONSE)
        transport, stream, _ = self.transport(body)
        self.assertEqual(self.send(transport).content, body)
        self.assertTrue(stream.closed)
        transport, stream, _ = self.transport([body, b' ', b'never-consumed'])
        with self.assertRaises(TransportPolicyError) as caught:
            self.send(transport)
        self.assertEqual(caught.exception.reason, 'response_budget_exceeded')
        self.assertEqual(stream.reads, 2)
        self.assertTrue(stream.closed)
        self.assertEqual(transport.report()['response_bytes'], MAX_RESPONSE + 1)

    def test_raw_usage_does_not_inherit_sdk_synthetic_fields(self):
        cases = [({}, None), ({'usage': None}, None),
                 ({'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}},
                  {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}),
                 ({'usage': {'prompt_tokens': 2}}, {'prompt_tokens': 2, 'completion_tokens': None, 'total_tokens': None}),
                 ({'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 7}}, None),
                 ({'usage': {'total_tokens': True}}, None)]
        for payload, expected in cases:
            with self.subTest(payload=payload):
                payload['secret'] = 'fixture-private-marker'
                transport, _, _ = self.transport(json.dumps(payload).encode())
                self.send(transport)
                report = transport.report()
                self.assertEqual(report['recent'][0]['usage'], expected)
                self.assertNotIn('fixture-private-marker', json.dumps(report))

    def test_cancelled_network_call_is_counted_once_and_not_retried(self):
        calls = []
        def cancelled(request):
            calls.append(True)
            raise KeyboardInterrupt
        transport = MeasuredTransport(SimpleNamespace(base_url='https://example.invalid/v1', max_context_bytes=4096),
                                      inner=httpx.MockTransport(cancelled))
        self.addCleanup(transport.close)
        with self.assertRaises(KeyboardInterrupt):
            self.send(transport)
        self.assertEqual(len(calls), 1)
        report = transport.report()
        self.assertEqual(report['attempts'], 1)
        self.assertEqual(report['outcomes']['cancelled'], 1)


if __name__ == '__main__':
    unittest.main()
