"""Use the actual pinned upstream loop; scripted fixtures are not model scores."""

import base64
import importlib.util
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shlex
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("mini_task", ROOT / "benchmarks/clients/mini_task.py")
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


class MiniTaskTests(unittest.TestCase):
    def test_invalid_request_controls_stop_before_credentials_or_sdk_access(self):
        for option, value in (("--request-interval", "nan"), ("--request-interval", "61"),
                              ("--request-timeout", "0"), ("--request-timeout", "inf")):
            with self.subTest(option=option, value=value), patch.object(sys, "argv", [
                "mini-task", "--run", "--case", "py-pagination", option, value]), \
                    patch.object(driver, "environment_api_key") as key, \
                    patch.object(driver, "run_case") as run, patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit):
                    driver.main()
                key.assert_not_called()
                run.assert_not_called()

    def test_real_sdk_http_transport_records_only_numeric_usage_and_routes_model(self):
        for generation in ({}, {"max_tokens": 2048, "thinking": True}):
            with self.subTest(generation=generation):
                self.check_real_sdk_http(generation)

    def check_real_sdk_http(self, generation):
        case = driver.bench.suite.cases()["py-interval"]
        commands = ["echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT", self.repair(case), "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"]
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers['Content-Length']))
                data = json.loads(raw)
                number = len(requests)
                requests.append({'model': data.get('model'), 'bash_tool': data['tools'][0]['function']['name'] == 'bash',
                                 'auth_ok': self.headers.get('Authorization') == 'Bearer fixture-private-marker',
                                 'max_tokens': data.get('max_tokens'), 'chat_template_kwargs': data.get('chat_template_kwargs'),
                                 'request_bytes': len(raw), 'identity': self.headers.get('Accept-Encoding') == 'identity'})
                body = json.dumps({'id': f'fixture-{number}', 'object': 'chat.completion', 'created': 1, 'model': 'fixture',
                                   'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None,
                                       'tool_calls': [{'id': f'call_{number}', 'type': 'function', 'function': {'name': 'bash',
                                                        'arguments': json.dumps({'command': commands[number]})}}]}}],
                                   'usage': {'prompt_tokens': 7, 'completion_tokens': 5, 'total_tokens': 12}}).encode()
                requests[-1]['response_bytes'] = len(body)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            report = driver.run_case(case, {'model': 'fixture', 'base_url': f'http://127.0.0.1:{server.server_port}/v1',
                                           'api_key': 'fixture-private-marker', **generation}, confirm=lambda _: True)
        finally:
            server.shutdown(); server.server_close(); thread.join(5)
        self.assertTrue(report['acceptance_passed'], report)
        self.assertEqual(len(requests), 3)
        self.assertTrue(all(row['model'] == 'fixture' and row['bash_tool'] and row['auth_ok'] for row in requests))
        self.assertEqual(report['model_queries'], 3)
        self.assertEqual(len(report['requests']), 3)
        wire = report['request_measurements']
        self.assertEqual([row['usage']['total_tokens'] for row in wire['recent']], [12, 12, 12])
        self.assertEqual([row['request_bytes'] for row in wire['recent']], [row['request_bytes'] for row in requests])
        self.assertEqual([row['response_bytes'] for row in wire['recent']], [row['response_bytes'] for row in requests])
        self.assertTrue(all(row['identity'] for row in requests))
        self.assertTrue(all(row['http_attempts'] == 1 for row in report['requests']))
        self.assertEqual(report['max_context_bytes'], 131072)
        self.assertFalse(report['automatic_retries'])
        self.assertNotIn('fixture-private-marker', json.dumps(report))
        self.assertNotIn('arguments', json.dumps(report))
        self.assertEqual(report['http_attempts'], 3)
        expected = ({'max_tokens': 2048, 'chat_template_kwargs': {'enable_thinking': True}} if generation else {})
        self.assertEqual(report['generation_options'], expected)
        self.assertTrue(all(row['max_tokens'] == generation.get('max_tokens') for row in requests))
        self.assertTrue(all(row['chat_template_kwargs'] == expected.get('chat_template_kwargs') for row in requests))

    def test_actual_sdk_budget_stops_before_http_or_approval(self):
        case = dict(driver.bench.suite.cases()['py-interval'])
        case['task'] += '\n' + '中文' * 4096
        calls = []
        from mini_transport import MeasuredTransport
        import httpx
        original = MeasuredTransport.__init__
        def init(transport, config):
            original(transport, config, inner=httpx.MockTransport(lambda request: calls.append(True)))
        with patch.object(MeasuredTransport, '__init__', init):
            report = driver.run_case(case, {'model': 'fixture', 'api_key': 'fixture-private-marker', 'max_context_bytes': 4096},
                                     confirm=lambda _: calls.append('approval'))
        self.assertEqual(report['outcome'], 'request_budget_exceeded', report)
        self.assertEqual(report['model_queries'], 1)
        self.assertEqual(report['http_attempts'], 0)
        self.assertEqual(report['request_measurements']['not_attempted'], 1)
        self.assertGreater(report['request_measurements']['recent'][0]['request_bytes'], 4096)
        self.assertFalse(calls)

    def test_actual_sdk_does_not_retry_http_failure_or_read_remote_error_body(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                requests.append(True)
                self.send_response(503)
                self.send_header('Content-Length', '1000000')
                self.end_headers()
                # Never send the promised body. The runner must close at the
                # status instead of waiting for SDK parsing or a retry.
                self.close_connection = True
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        original_retry = os.environ.get('MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT')
        try:
            report = driver.run_case(driver.bench.suite.cases()['py-interval'],
                                     {'model': 'fixture', 'base_url': f'http://127.0.0.1:{server.server_port}/v1',
                                      'api_key': 'fixture-private-marker'}, confirm=lambda _: self.fail('unexpected action'))
        finally:
            server.shutdown(); server.server_close(); thread.join(5)
        self.assertEqual(report['outcome'], 'http_error', report)
        self.assertEqual(report['model_queries'], 1)
        self.assertEqual(report['http_attempts'], 1)
        self.assertEqual(len(requests), 1)
        self.assertEqual(report['request_measurements']['recent'][0]['http_status'], 503)
        self.assertEqual(report['request_measurements']['response_bytes'], 0)
        self.assertEqual(os.environ.get('MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT'), original_retry)

    def model(self, commands, inspect=None):
        def factory(defaults):
            from minisweagent.models.test_models import DeterministicToolcallModel, make_toolcall_output
            outputs = []
            for number, command in enumerate(commands):
                identifier = f"call_{number}"
                outputs.append(make_toolcall_output(None, [{"id": identifier, "type": "function", "function": {
                    "name": "bash", "arguments": json.dumps({"command": command})}}], [{"command": command, "tool_call_id": identifier}]))
            class Model(DeterministicToolcallModel):
                def query(self, messages, **kwargs):
                    if inspect:
                        inspect(messages)
                    return super().query(messages, **kwargs)
            return Model(outputs=outputs, cost_per_call=0, observation_template=defaults["model"]["observation_template"])
        return factory

    def repair(self, case):
        payload = base64.b64encode(json.dumps(case["reference"]).encode()).decode()
        body = "import base64,json; from pathlib import Path; data=json.loads(base64.b64decode('" + payload + "')); "
        body += "[Path(name).write_text(value, encoding='utf-8') for name,value in data.items()]"
        return shlex.join([sys.executable.replace("\\", "/"), "-I", "-B", "-c", body])

    def test_upstream_loop_receives_failed_acceptance_then_repairs_and_passes(self):
        case = driver.bench.suite.cases()["py-interval"]
        observed = []
        commands = ["echo " + driver.ApprovedEnvironment.__module__]  # Ordinary upstream bash action.
        commands += ["echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT", self.repair(case), "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"]
        with patch.dict(os.environ, {"UNRELATED_API_KEY": "fixture-private-marker"}):
            report = driver.run_case(case, {"model": "fixture", "api_key": "fixture-private-marker"},
                                     model_factory=self.model(commands, lambda messages: observed.append(json.dumps(messages))), confirm=lambda _: True)
            self.assertEqual(os.environ["UNRELATED_API_KEY"], "fixture-private-marker")
        self.assertTrue(report["acceptance_passed"], report)
        self.assertEqual(report["model_queries"], 4)
        self.assertEqual(report["approvals"]["requests"], 6)
        self.assertTrue(any("Fixed acceptance failed" in item for item in observed))
        self.assertNotIn("fixture-private-marker", json.dumps(report) + "".join(observed))
        self.assertEqual(report["model_mode"], "scripted_protocol_fixture")
        self.assertIsNone(report["max_context_bytes"])

    def test_repository_sdk_clock_does_not_charge_approval_wait_as_agent_failure(self):
        import minisweagent.agents.default as sdk_agent
        case = driver.bench.suite.cases()["py-interval"]
        adapter = SimpleNamespace(files=driver.bench.suite.fixture_files(case), bundle=driver.bench.bundle,
                                  metadata={"protocol_fixture": True})
        clock = [0]
        def approve(_):
            clock[0] += 2000  # Simulated operator wait; no sleeping or model score.
            return True
        with patch.object(sdk_agent, "time", SimpleNamespace(time=lambda: clock[0])):
            report = driver.run_case(case, {"model": "fixture", "max_steps": 3}, case_adapter=adapter,
                                     model_factory=self.model([self.repair(case), "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"]),
                                     confirm=approve)
        self.assertTrue(report["acceptance_passed"], report)
        self.assertEqual(report["sdk_wall_time_limit_seconds"], 0)
        self.assertEqual(report["model_queries"], 2)
        clock[0] = 0
        with patch.object(sdk_agent, "time", SimpleNamespace(time=lambda: clock[0])):
            ordinary = driver.run_case(case, {"model": "fixture", "max_steps": 3},
                                       model_factory=self.model([self.repair(case), "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"]),
                                       confirm=approve)
        self.assertEqual(ordinary["sdk_wall_time_limit_seconds"], 600)
        self.assertEqual(ordinary["outcome"], "time_exceeded")

    def test_repository_sdk_step_limit_and_cancellation_remain_enforced(self):
        case = driver.bench.suite.cases()["py-interval"]
        adapter = SimpleNamespace(files=driver.bench.suite.fixture_files(case), bundle=driver.bench.bundle,
                                  metadata={"protocol_fixture": True})
        report = driver.run_case(case, {"model": "fixture", "max_steps": 1}, case_adapter=adapter,
                                 model_factory=self.model(["echo first", "echo second"]), confirm=lambda _: True)
        self.assertEqual(report["outcome"], "limits_exceeded")
        self.assertEqual(report["model_queries"], 1)
        self.assertEqual(report["approvals"]["requests"], 1)
        def cancel(_):
            raise KeyboardInterrupt
        cancelled = driver.run_case(case, {"model": "fixture"}, case_adapter=adapter,
                                    model_factory=self.model(["echo first"]), confirm=cancel)
        self.assertEqual(cancelled["outcome"], "cancelled")
        self.assertFalse(cancelled["acceptance_passed"])

    def test_refused_fixed_verification_never_counts_as_success(self):
        case = driver.bench.suite.cases()["py-interval"]
        answers = iter((True, False))
        report = driver.run_case(case, {"model": "fixture"}, model_factory=self.model(["echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"]),
                                 confirm=lambda _: next(answers))
        self.assertFalse(report["acceptance_passed"])
        self.assertEqual(report["outcome"], "acceptance_rejected")

    def test_javascript_task_uses_the_same_node_verifier(self):
        case = driver.bench.suite.cases()['js-search']
        report = driver.run_case(case, {'model': 'fixture'}, model_factory=self.model([
            self.repair(case), 'echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT']), confirm=lambda _: True)
        self.assertTrue(report['acceptance_passed'], report)
        self.assertEqual(report['model_queries'], 2)
        self.assertEqual(report['approvals']['requests'], 3)

    def test_user_cancellation_stops_actual_sdk_loop(self):
        def cancelled(_):
            raise KeyboardInterrupt
        case = driver.bench.suite.cases()["py-interval"]
        report = driver.run_case(case, {"model": "fixture"}, model_factory=self.model(["echo first", "echo forbidden"]), confirm=cancelled)
        self.assertEqual(report["outcome"], "cancelled")
        self.assertEqual(report["model_queries"], 1)
        self.assertFalse(report["acceptance_passed"])

    def test_changed_external_verifier_invalidates_run_before_action(self):
        def change_verifier(description):
            cwd_line = next(line for line in description.splitlines() if line.startswith('cwd: '))
            workspace = Path(json.loads(cwd_line[5:]))
            (workspace.parent / 'verification/benchmarks/coding/cases.json').write_text('{}', encoding='utf-8')
            return True
        case = driver.bench.suite.cases()['py-interval']
        report = driver.run_case(case, {'model': 'fixture'}, model_factory=self.model(['echo forbidden > candidate.txt']),
                                 confirm=change_verifier)
        self.assertEqual(report['outcome'], 'verifier_modified')
        self.assertFalse(report['verifier_intact'])
        self.assertFalse(report['acceptance_passed'])

    def test_history_case_is_rejected_before_model_factory(self):
        called = []
        with self.assertRaises(ValueError):
            driver.run_case(driver.bench.suite.cases()["py-long-history"], {"model": "fixture"}, model_factory=lambda _: called.append(True))
        self.assertFalse(called)

    def test_cli_existing_report_stops_before_sdk_or_key_access(self):
        with driver.bench.disposable() as root:
            report = root / "existing.json"
            report.write_text("preserve", encoding="utf-8")
            with patch.object(sys, "argv", ["mini_task", "--run", "--case", "py-interval", "--report", str(report)]), \
                    patch.object(driver, "run_case") as run, patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit):
                    driver.main()
                run.assert_not_called()
            self.assertEqual(report.read_text('utf-8'), 'preserve')

    def test_list_export_is_numeric_metadata_only_and_does_not_launch_a_model(self):
        with driver.bench.disposable() as root:
            report = root / 'list.json'
            with patch.object(sys, 'argv', ['mini_task', '--list', '--report', str(report)]), \
                    patch.object(driver, 'run_case') as run, patch('sys.stdout', io.StringIO()):
                self.assertEqual(driver.main(), 0)
                run.assert_not_called()
            rows = json.loads(report.read_text('utf-8'))
            self.assertEqual(len(rows), 7)
            self.assertFalse(any(row['id'] == 'py-long-history' for row in rows))


if __name__ == "__main__":
    unittest.main()
