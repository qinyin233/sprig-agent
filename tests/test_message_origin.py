"""Preserve actual requests through observations, summaries, forks and retries."""
import copy
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.context import request_body
from sprig_agent.conversation_fork import ConversationFork, turn_catalog
from sprig_agent.input_inbox import INPUT_ID, new_input
from sprig_agent.message_origin import ORIGIN, application_message, user_request
from sprig_agent.provider import Provider, ProviderError
from sprig_agent.session import SessionError, SessionStore, validate_history
from sprig_agent.summary import FIELDS, binding_digest, fingerprint, project
from sprig_agent.tools import Tools
from tests.test_generation_options import envelope
from tests.test_native_protocols import backend
from tests.test_mcp_stdio import wire
from tests.test_tui import response


def history():
    return [{"role":"system","content":"system"},{"role":"user","content":"EXACT_USER_REQUIREMENT 中文"},
            {"role":"assistant","content":"working"},application_message("Application acceptance checks failed; repair."),
            *[{"role":"assistant","content":"historical observation"} for _ in range(9)]]


def checkpoint(rows):
    return {"version":1,"end":7,"sha256":fingerprint(rows,7),"binding":binding_digest({}),
            "summary":{"objective":"short checkpoint",**{key:[] for key in FIELDS}}}


class MessageOriginTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.parent=Path(directory.name).resolve()
        self.root=self.parent/"workspace"
        self.root.mkdir()
        self.config=Config(self.root,"fixture",max_context_bytes=65536)
        self.prompts,self.saved=[],[]
        self.provider=Mock()
        self.provider.complete.side_effect=AssertionError("no model request expected")

    def agent(self,config=None,provider=None,**options):
        config=config or self.config
        tools=Tools(config,lambda text:self.prompts.append(text) or True)
        self.addCleanup(tools.close)
        return Agent(config,provider or self.provider,tools,lambda _:None,
                     checkpoint=lambda rows:self.saved.append(copy.deepcopy(rows)),**options)

    def test_summary_keeps_actual_request_instead_of_the_last_application_notice(self):
        rows=history()
        selected=project(rows,checkpoint(rows))
        self.assertTrue(any(row.get("content")==rows[1]["content"] for row in selected))
        self.assertEqual(rows,history())
        self.assertTrue(all(not user_request(row) for row in selected if row.get("content","").startswith("Application summary")))

    def test_timeline_and_forks_group_observations_with_the_actual_human_turn(self):
        rows=history()
        rows.extend([{"role":"user","content":"SECOND_REQUEST"},response()])
        catalog=turn_catalog(rows)
        self.assertEqual([(row["message"],row["through_message"]) for row in catalog],[(14,15),(2,13)])
        proposal=ConversationFork(self.agent(),rows,"2")
        self.assertTrue(proposal.preflight(lambda _:True,self.parent/"sessions"))
        _,forked,store,_=proposal.commit(self.parent/"sessions")
        self.assertIsNone(store)
        self.assertTrue(any(row.get("content")==rows[3]["content"] for row in forked))
        self.assertFalse(any(row.get("content")=="SECOND_REQUEST" for row in forked))
        self.assertEqual([row["message"] for row in turn_catalog(forked)],[2])
        self.provider.complete.assert_not_called()

    def test_literal_notice_looking_human_text_and_legacy_unmarked_history_remain_requests(self):
        rows=[{"role":"system","content":"system"},{"role":"user","content":"Application baseline checks failed"}]
        self.assertEqual(validate_history(rows),rows)
        self.assertTrue(user_request(rows[1]))
        self.assertEqual(len(turn_catalog(rows)),1)
        self.assertEqual(turn_catalog(rows)[0]["preview"],rows[1]["content"])

    def test_actual_baseline_failure_is_observation_not_a_new_task(self):
        check=(sys.executable,"-I","-B","-c","raise SystemExit(1)")
        captured=[]
        with backend("chat_completions",[envelope(response())],captured) as url:
            config=replace(self.config,checks=(check,),check_before_task=True,base_url=url,proxy="direct",max_steps=1)
            rows,complete=self.agent(config,Provider(config)).run("FIX_THE_TASK")
        self.assertFalse(complete)  # Final fixed check still fails; no invented acceptance.
        self.assertEqual([row["preview"] for row in turn_catalog(rows)],["FIX_THE_TASK"])
        notices=[row for row in rows if (row.get("content") or "").startswith("Application ")]
        self.assertTrue(notices)
        self.assertTrue(all(row.get(ORIGIN)=="application" for row in notices))
        self.assertEqual(len(captured),1)

    def test_application_origin_survives_private_save_and_restore(self):
        rows=history()
        with closing(SessionStore(self.parent/"sessions",self.config)) as store:
            store.save("origin",rows)
            restored=store.load("origin")
        self.assertEqual(restored,rows)
        self.assertEqual(len(turn_catalog(restored)),1)
        self.assertTrue(any(row.get("content")==rows[1]["content"] for row in project(restored,checkpoint(restored))))

    def test_malformed_origin_or_delivery_identity_is_rejected_before_any_wire_request(self):
        entry=new_input("EXACT")
        valid=application_message("observation")
        variants=[{**valid,ORIGIN:value} for value in (True,None,{},"user","unknown")]
        variants += [{**valid,"role":role} for role in ("system","assistant","tool")]
        variants += [{**valid,INPUT_ID:entry.id},{**valid,"tool_calls":[]},{**valid,"native_state":{}}]
        for number,row in enumerate(variants):
            with self.subTest(number=number),self.assertRaises(SessionError):
                validate_history([{"role":"system","content":"system"},row])
            for protocol in ("chat_completions","messages","responses"):
                with self.subTest(number=number,protocol=protocol),self.assertRaises(ProviderError) as caught:
                    Provider(replace(self.config,wire_api=protocol,base_url="http://127.0.0.1:1",proxy="direct")).complete([row],[])
                self.assertEqual(caught.exception.code,"invalid_request")

    def test_internal_origin_metadata_is_removed_from_actual_three_protocol_requests(self):
        rows=history()[:4]
        for protocol in ("chat_completions","messages","responses"):
            captured=[]
            reply=envelope(response()) if protocol=="chat_completions" else wire(protocol,response())
            with backend(protocol,[reply],captured) as url:
                config=replace(self.config,wire_api=protocol,base_url=url,proxy="direct")
                Provider(config).complete(rows,[])
            self.assertEqual(len(captured),1)
            self.assertNotIn(ORIGIN,json.dumps(captured[0]["body"]))
            self.assertIn("Application acceptance checks failed",json.dumps(captured[0]["body"]))
            plain=[{key:value for key,value in row.items() if key!=ORIGIN} for row in rows]
            self.assertEqual(request_body(config,rows,[]),request_body(config,plain,[]))

    def test_attachment_and_local_verification_do_not_create_human_task_boundaries(self):
        (self.root/"source.txt").write_text("SOURCE",encoding="utf-8")
        agent=self.agent()
        rows,_=agent.attach_files(["source.txt"])
        self.assertEqual(turn_catalog(rows),[])
        with self.assertRaisesRegex(ValueError,"user turn"):
            ConversationFork(agent,rows,"current")
        config=replace(self.config,checks=((sys.executable,"-I","-B","-c","pass"),))
        rows,result=self.agent(config).verify(history()[:3])
        self.assertEqual(result["verification"],"passed")
        self.assertEqual(len(turn_catalog(rows)),1)
        self.provider.complete.assert_not_called()

    def test_storage_rejects_malformed_or_redaction_conflicting_origin_without_overwrite(self):
        config=replace(self.config,api_key="application")
        original=[{"role":"system","content":"system"},{"role":"user","content":"ORIGINAL"}]
        with closing(SessionStore(self.parent/"sessions",config)) as store:
            store.save("origin",original)
            path=self.parent/"sessions"/"origin.json"
            before=path.read_bytes()
            for row in (application_message("observation"),{"role":"user","content":"x",ORIGIN:True}):
                with self.assertRaises(SessionError): store.save("origin",[*original,row])
                self.assertEqual(path.read_bytes(),before)
            document=json.loads(before)
            document["messages"].append({"role":"user","content":"x",ORIGIN:"unknown"})
            path.write_text(json.dumps(document),encoding="utf-8")
            with self.assertRaises(SessionError): store.load("origin")

    def test_output_limit_background_and_workspace_observations_never_become_user_turns(self):
        from sprig_agent.provider import ProviderError
        provider=Mock()
        provider.complete.side_effect=ProviderError("fixture output limit",code="output_limit")
        agent=self.agent(provider=provider)
        rows=[]
        with self.assertRaises(ProviderError): agent.run("REAL_REQUEST",rows)
        rows=self.saved[-1]
        self.assertEqual(len(turn_catalog(rows)),1)
        self.assertTrue(rows[-1].get(ORIGIN)=="application")
        with patch.object(agent.tools.jobs,"enabled",return_value=True),patch.object(agent.tools.jobs,"observations",return_value=[{"job_id":"fixture-job","state":"completed"}]):
            self.assertTrue(agent._job_notices(rows))
        agent.record_workspace_change(rows,{"action":"undo","path":"source.txt","changed":True})
        self.assertEqual(len(turn_catalog(rows)),1)
        self.assertTrue(all(row.get(ORIGIN)=="application" for row in rows[-3:]))
