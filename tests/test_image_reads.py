"""Model image inspection: fresh approval, native wire data and run-only lifetime."""
import base64
import copy
from contextlib import closing
from dataclasses import replace
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.image_inputs import IMAGE_KEY
from sprig_agent.image_reads import ImageReadResult
from sprig_agent.provider import Provider, ProviderError
from sprig_agent.session import SessionStore
from sprig_agent.tools import Tools
from tests.test_image_inputs import raster, native_images
from tests.test_generation_options import envelope
from tests.test_mcp_stdio import wire
from tests.test_native_protocols import backend
from tests import test_tui as ui


def reply(protocol, message, streaming):
    return envelope(message) if protocol=="chat_completions" and not streaming else wire(protocol,message)


class ImageReadTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.parent=Path(directory.name).resolve()
        self.root=self.parent/"workspace"
        self.root.mkdir()
        self.path=self.root/"screen.png"
        self.path.write_bytes(raster(metadata=True))
        self.config=Config(self.root,"fixture",max_output=1024,max_context_bytes=1048576)
        self.prompts,self.saved=[],[]
        self.provider=Mock()
        self.provider.complete.side_effect=AssertionError("no model query expected")

    def tools(self,config=None,approve=None,**options):
        tools=Tools(config or self.config,approve or (lambda text:self.prompts.append(text) or True),**options)
        self.addCleanup(tools.close)
        return tools

    def agent(self,config=None,provider=None,tools=None,**options):
        config=config or self.config
        return Agent(config,provider or self.provider,tools or self.tools(config),lambda _:None,
                     checkpoint=lambda rows:self.saved.append(copy.deepcopy(rows)),**options)

    def test_read_requires_fresh_send_approval_and_keeps_pixels_out_of_tool_json(self):
        tools=self.tools()
        for _ in range(2):
            result=tools.execute("read_file",{"path":"screen.png","image":True})
            self.assertIsInstance(result,ImageReadResult)
            self.assertTrue(result["approved"])
            self.assertTrue(result["image_prepared"])
            self.assertNotIn("iVBOR",json.dumps(result))
            self.assertNotIn("iVBOR",repr(result))
            self.assertNotIn("read_id",result)
            self.assertFalse(result["network_contacted"])
            with Image.open(io.BytesIO(base64.b64decode(result.frame["image"]["data"]))) as image:
                self.assertEqual(image.getpixel((0,0)),(12,34,56))
                self.assertEqual(image.info,{})
        self.assertEqual(len(self.prompts),2)
        self.assertTrue(all("READ AND SEND IMAGE" in text and "not saved" in text for text in self.prompts))

    def test_real_six_protocol_stream_paths_inspect_pixels_edit_and_independently_verify(self):
        (self.root/"answer.txt").write_text("old",encoding="utf-8")
        check=(sys.executable,"-I","-B","-c","from pathlib import Path;assert Path('answer.txt').read_text()=='(12, 34, 56)'")
        generate=(sys.executable,"-I","-B","-c",
                  "import struct,zlib;from pathlib import Path\n"
                  "def c(k,d): return struct.pack('>I',len(d))+k+d+struct.pack('>I',zlib.crc32(k+d))\n"
                  "pixels=(b'\\0'+bytes((12,34,56))*12)*8\n"
                  "Path('generated.png').write_bytes(b'\\x89PNG\\r\\n\\x1a\\n'+c(b'IHDR',struct.pack('>IIBBBBB',12,8,8,2,0,0,0))+c(b'IDAT',zlib.compress(pixels))+c(b'IEND',b''))")
        for protocol in ("chat_completions","messages","responses"):
            for streaming in (False,True):
                with self.subTest(protocol=protocol,streaming=streaming):
                    (self.root/"answer.txt").write_text("old",encoding="utf-8")
                    (self.root/"generated.png").unlink(missing_ok=True)
                    captured=[]
                    start=len(self.prompts)
                    def edit(payload):
                        pixels=native_images(payload)
                        self.assertEqual(len(pixels),1)
                        with Image.open(io.BytesIO(pixels[0])) as image: color=image.getpixel((0,0))
                        return reply(protocol,ui.call("edit","edit_file",{"path":"answer.txt","old":"old","new":str(color)}),streaming)
                    responses=[reply(protocol,ui.call("make","run",{"argv":list(generate)}),streaming),
                               reply(protocol,ui.call("view","read_file",{"path":"generated.png","image":True}),streaming),
                               edit,reply(protocol,ui.response(),streaming)]
                    with backend(protocol,responses,captured,stream=streaming and protocol!="chat_completions") as url:
                        config=replace(self.config,wire_api=protocol,stream=streaming,base_url=url,proxy="direct",checks=(check,))
                        agent=self.agent(config,Provider(config))
                        history,complete=agent.run("Read the screenshot and repair answer.txt")
                    self.assertTrue(complete)
                    self.assertEqual(len(captured),4)
                    self.assertEqual(native_images(captured[0]["body"]),[])
                    self.assertEqual(native_images(captured[1]["body"]),[])
                    self.assertTrue(all(len(native_images(row["body"]))==1 for row in captured[2:]))
                    self.assertTrue(all(row["bytes"]<=config.max_context_bytes for row in captured))
                    self.assertEqual(len(self.prompts)-start,4)  # Generate, image send, edit, independent fixed check.
                    self.assertNotIn("iVBOR",json.dumps(history))
                    self.assertFalse(any(IMAGE_KEY in row for row in history))
                    self.assertEqual(agent._tool_images,[])

    def test_refusal_or_non_boolean_approval_never_prepares_pixels(self):
        for decision in (False,1,"yes",[True]):
            with self.subTest(decision=type(decision).__name__):
                result=self.tools(approve=lambda _:decision).execute("read_file",{"path":"screen.png","image":True})
                self.assertFalse(result["approved"])
                self.assertFalse(result["image_prepared"])
                self.assertNotIsInstance(result,ImageReadResult)

    def test_approval_time_changes_privacy_rules_and_configuration_refuse_payloads(self):
        for change in ("file","privacy","instructions","configuration","closed"):
            with self.subTest(change=change):
                self.path.write_bytes(raster())
                for name in (".sprigignore","AGENTS.md"):
                    (self.root/name).unlink(missing_ok=True)
                tools=self.tools()
                def approve(_):
                    if change=="file": self.path.write_bytes(raster(size=(13,8)))
                    elif change=="privacy": (self.root/".sprigignore").write_text("screen.png\n",encoding="utf-8")
                    elif change=="instructions": (self.root/"AGENTS.md").write_text("Updated instructions",encoding="utf-8")
                    elif change=="configuration": tools.rebind_model(replace(self.config,model="different"))
                    else: tools.close()
                    return True
                tools.approve=approve
                tools.instructions.mark_served(tools.instructions.context())
                with tools.instructions.model_action():
                    result=tools.execute("read_file",{"path":"screen.png","image":True})
                self.assertIn("error",result)
                self.assertNotIsInstance(result,ImageReadResult)
        for name in (".sprigignore","AGENTS.md"): (self.root/name).unlink(missing_ok=True)

    def test_disabled_closed_excluded_and_invalid_modes_stop_before_decoder_or_approval(self):
        (self.root/".sprigignore").write_text("screen.png\n",encoding="utf-8")
        for mode in ("disabled","closed","excluded"):
            config=replace(self.config,disabled_tools=("read_file",)) if mode=="disabled" else self.config
            tools=self.tools(config)
            if mode=="closed": tools.close()
            with patch("PIL.Image.open",side_effect=AssertionError("decoder after denial")):
                result=tools.execute("read_file",{"path":"screen.png","image":True})
            self.assertIn("error",result)
        (self.root/".sprigignore").unlink()
        tools=self.tools()
        for extra in ({"image":1},{"offset":2},{"limit":1},{"line_numbers":False},{"cursor":"start"},{"symbol":"Name"}):
            self.assertIn("error",tools.execute("read_file",{"path":"screen.png","image":True,**extra}))
        self.assertEqual(self.prompts,[])

    def test_read_only_allows_explicit_image_approval_and_public_metadata_is_bounded(self):
        path=self.root/("图"*70+".png")
        path.write_bytes(raster())
        config=replace(self.config,read_only=True,max_output=256)
        result=self.tools(config).execute("read_file",{"path":path.name,"image":True})
        self.assertTrue(result["approved"])
        self.assertTrue(result["metadata_omitted"])
        self.assertLessEqual(len(json.dumps(result,ensure_ascii=False).encode()),256)
        self.assertEqual(len(self.prompts),1)

    def test_cancellation_during_approval_drops_prepared_pixels(self):
        cancelled=threading.Event()
        tools=self.tools(cancel_event=cancelled,approve=lambda _:cancelled.set() or True)
        with self.assertRaises(KeyboardInterrupt): tools.execute("read_file",{"path":"screen.png","image":True})

    def test_restoring_saved_metadata_does_not_restore_or_replay_pixel_payloads(self):
        provider=ui.ScriptedProvider([ui.call("view","read_file",{"path":"screen.png","image":True}),ui.response()])
        agent=self.agent(provider=provider)
        history,complete=agent.run("Inspect screenshot")
        self.assertTrue(complete)
        with closing(SessionStore(self.parent/"sessions",self.config)) as store:
            store.save("images",history)
            self.path.unlink()
            restored=store.load("images")
        self.assertEqual(restored,history)
        self.assertNotIn("iVBOR",json.dumps(restored))
        next_provider=ui.ScriptedProvider([ui.response()])
        next_agent=self.agent(provider=next_provider)
        next_agent.run(None,restored)
        self.assertFalse(any(IMAGE_KEY in row for row in next_provider.calls[0]))
        self.assertEqual(next_agent._tool_images,[])
        self.assertEqual(len(self.prompts),1)

    def test_new_guidance_invalidates_prior_pixel_send_consent(self):
        queued=[]
        provider=ui.ScriptedProvider([ui.call("view","read_file",{"path":"screen.png","image":True}),ui.response()])
        tools=self.tools(approve=lambda text:self.prompts.append(text) or queued.append("NEW_GUIDANCE") or True)
        def receive():
            result=queued[:]
            queued.clear()
            return result
        agent=self.agent(provider=provider,tools=tools,receive_input=receive)
        history,complete=agent.run("Inspect screenshot")
        self.assertTrue(complete)
        self.assertFalse(any(IMAGE_KEY in row for row in provider.calls[-1]))
        self.assertIn("NEW_GUIDANCE",json.dumps(history))
        self.assertEqual(agent._tool_images,[])

    def test_request_budget_stops_after_approved_read_before_another_http(self):
        self.path.write_bytes(raster(size=(200,200),noise=True))
        captured=[]
        first=envelope(ui.call("view","read_file",{"path":"screen.png","image":True}))
        with backend("chat_completions",[first],captured) as url:
            config=replace(self.config,max_context_bytes=65536,base_url=url,proxy="direct")
            agent=self.agent(config,Provider(config))
            with self.assertRaisesRegex(ProviderError,"context byte budget"):
                agent.run("Inspect screenshot")
        self.assertEqual(len(captured),1)
        self.assertEqual(len(self.prompts),1)
        self.assertEqual(agent._tool_images,[])
        self.assertNotIn("iVBOR",json.dumps(self.saved))

    def test_text_read_path_and_schema_mode_are_explicit(self):
        (self.root/"source.txt").write_text("TEXT",encoding="utf-8")
        tools=self.tools()
        self.assertEqual(tools.execute("read_file",{"path":"source.txt"})["content"],"TEXT")
        self.assertIn("error",tools.execute("read_file",{"path":"source.txt","image":True}))
        self.assertIn("error",tools.execute("read_file",{"path":"screen.png"}))
        schema=next(row for row in self.agent(tools=tools).schemas if row["function"]["name"]=="read_file")
        self.assertEqual(schema["function"]["parameters"]["properties"]["image"],{"type":"boolean"})
        self.assertEqual(self.prompts,[])

    def test_same_canonical_path_replaces_cached_snapshot_and_reset_clears_pixels(self):
        tools=self.tools()
        tools.instructions.mark_served(tools.instructions.context())
        agent=self.agent(tools=tools)
        for path in ("./screen.png","screen.png"):
            result=agent._execute_model_tool("read_file",{"path":path,"image":True})
            self.assertTrue(result["image_prepared"])
        self.assertEqual(len(agent._tool_images),1)
        self.assertEqual(agent._tool_images[0]["path"],"screen.png")
        for number in range(16):
            name=f"view-{number}.png"
            (self.root/name).write_bytes(raster())
            result=agent._execute_model_tool("read_file",{"path":name,"image":True})
        self.assertIn("error",result)
        self.assertFalse(result["image_prepared"])
        self.assertEqual(len(agent._tool_images),16)
        agent.reset_runtime()
        self.assertEqual(agent._tool_images,[])

    def test_normalized_model_image_cache_budget_refuses_addition_without_dropping_old_frames(self):
        source=raster("JPEG",size=(400,400),noise=True)
        tools=self.tools()
        tools.instructions.mark_served(tools.instructions.context())
        agent=self.agent(tools=tools)
        for number in range(9):
            path=self.root/f"view-{number}.jpg"
            path.write_bytes(source)
            result=agent._execute_model_tool("read_file",{"path":path.name,"image":True})
        self.assertIn("error",result)
        self.assertFalse(result["image_prepared"])
        self.assertEqual(len(agent._tool_images),8)
