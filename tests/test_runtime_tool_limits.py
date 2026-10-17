"""Model-visible command limits agree with enforcement and stay per-agent."""

import copy
from contextlib import closing
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.provider import Provider
from sprig_agent.tools import TOOL_SCHEMAS, Tools
from tests.test_planning import replies


def call(identity, arguments):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": identity, "type": "function", "function": {
        "name": "run", "arguments": json.dumps(arguments)}}]}


def run_schema(schemas):
    return next(schema["function"] for schema in schemas if schema["function"]["name"] == "run")


class RuntimeToolLimitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_schema_uses_command_limit_instead_of_http_or_global_maximum(self):
        original = copy.deepcopy(TOOL_SCHEMAS)
        agents = [Agent(Config(self.root, "fixture", timeout=seconds, request_timeout=90), Mock(), Mock(), lambda _: None)
                  for seconds in (5, 7.5, 60)]
        self.assertEqual([run_schema(agent.schemas)["parameters"]["properties"]["timeout"]["maximum"]
                          for agent in agents], [5, 7.5, 60])
        run_schema(agents[0].schemas)["parameters"]["properties"]["timeout"]["maximum"] = 1
        self.assertEqual(run_schema(agents[1].schemas)["parameters"]["properties"]["timeout"]["maximum"], 7.5)
        self.assertEqual(TOOL_SCHEMAS, original)
        readonly = Agent(Config(self.root, "fixture", read_only=True), Mock(), Mock(), lambda _: None)
        self.assertNotIn("run", {schema["function"]["name"] for schema in readonly.schemas})

    def test_rejected_timeout_reports_exact_limit_and_never_requests_approval(self):
        approve = Mock(side_effect=AssertionError("invalid timeout reached approval"))
        with closing(Tools(Config(self.root, "fixture", timeout=7.5, request_timeout=90), approve)) as tools:
            result = tools.execute("run", {"argv": [sys.executable, "-c", "pass"], "timeout": 8})
        self.assertIn("7.5", result["error"])
        self.assertIn("omit timeout", result["error"])
        approve.assert_not_called()

    def test_actual_http_uses_runtime_cap_and_rejected_proposal_can_omit_override(self):
        captured, approve = [], Mock(return_value=True)
        argv = [sys.executable, "-I", "-B", "-c", "print('bounded-command-ok')"]
        def first(payload):
            self.assertEqual(run_schema(payload["tools"])["parameters"]["properties"]["timeout"]["maximum"], 5)
            return call("invalid", {"argv": argv, "timeout": 6})
        def repaired(payload):
            rejected = json.loads(payload["messages"][-1]["content"])
            self.assertIn("at most 5 seconds", rejected["error"])
            approve.assert_not_called()
            return call("valid", {"argv": argv})
        with replies([first, repaired, {"role": "assistant", "content": "Done"}], captured) as url:
            config = Config(self.root, "fixture", base_url=url, proxy="direct", timeout=5, request_timeout=30, max_steps=3)
            with closing(Tools(config, approve)) as tools:
                history, completed = Agent(config, Provider(config), tools, lambda _: None).run("Run the bounded fixture command")
        self.assertTrue(completed)
        self.assertEqual(len(captured), 3)
        approve.assert_called_once()
        result = json.loads(history[-2]["content"])
        self.assertEqual(result["returncode"], 0)
        self.assertIn("bounded-command-ok", result["output"])


if __name__ == "__main__":
    unittest.main()
