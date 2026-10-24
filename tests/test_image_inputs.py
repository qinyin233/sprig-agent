"""Actual raster bytes cross native protocols, private sessions and hard budgets."""
import base64
import copy
from contextlib import closing, redirect_stderr, redirect_stdout
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import random
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import zlib

from PIL import Image, PngImagePlugin

from sprig_agent import cli
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.context import prepare_context, request_body
from sprig_agent.image_inputs import IMAGE_KEY, encode_image, images
from sprig_agent.provider import Provider, ProviderError
from sprig_agent.session import SessionError, SessionStore, validate_history
from sprig_agent.summary import history_page, public_message
from sprig_agent.tools import Tools
from tests.test_attachment_snapshots import chat_stream
from tests.test_generation_options import envelope
from tests.test_mcp_stdio import wire
from tests.test_native_protocols import backend
from tests.test_tui import response, call


def raster(format="PNG", *, metadata=False, size=(12, 8), noise=False):
    output=io.BytesIO()
    raw=random.Random(1701).randbytes(size[0]*size[1]*3) if noise else bytes((12, 34, 56))*size[0]*size[1]
    with Image.frombytes("RGB",size,raw) as image:
        options={"quality":5} if format=="JPEG" else {}
        if metadata and format=="PNG":
            info=PngImagePlugin.PngInfo()
            info.add_text("Comment","PRIVATE_METADATA_DO_NOT_SEND")
            options["pnginfo"]=info
        if metadata and format=="JPEG":
            exif=Image.Exif()
            exif[274]=6
            exif[270]="PRIVATE_METADATA_DO_NOT_SEND"
            options["exif"]=exif
        image.save(output,format=format,**options)
    return output.getvalue()


def native_images(value):
    result=[]
    def walk(item):
        if isinstance(item,dict):
            if item.get("type")=="image":
                source=item["source"]
                assert source["type"]=="base64" and source["media_type"]=="image/png"
                result.append(base64.b64decode(source["data"],validate=True))
            elif item.get("type") in {"input_image","image_url"}:
                uri=item["image_url"]
                uri=uri["url"] if isinstance(uri,dict) else uri
                assert uri.startswith("data:image/png;base64,")
                result.append(base64.b64decode(uri.split(",",1)[1],validate=True))
            else:
                for child in item.values(): walk(child)
        elif isinstance(item,list):
            for child in item: walk(child)
    walk(value)
    return result


class ImageInputTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.parent=Path(directory.name).resolve()
        self.root=self.parent/"workspace"
        self.root.mkdir()
        self.config=Config(self.root,"fixture",max_output=256,max_context_bytes=1048576)
        self.provider=Mock()
        self.provider.complete.side_effect=AssertionError("no model request expected")
        self.saved,self.prompts=[],[]

    def agent(self,config=None,provider=None,**options):
        selected=config or self.config
        tools=Tools(selected,lambda text:self.prompts.append(text) or True)
        self.addCleanup(tools.close)
        return Agent(selected,provider or self.provider,tools,lambda _:None,
                     checkpoint=lambda history:self.saved.append(copy.deepcopy(history)),**options)

    def test_four_formats_are_sniffed_decoded_and_metadata_free_without_resizing(self):
        for format in ("PNG","JPEG","WEBP","GIF"):
            with self.subTest(format=format):
                raw=raster(format,metadata=True)
                name=format+"-unknown.bin"
                (self.root/name).write_bytes(raw)
                history,receipt=self.agent().attach_files([name])
                row=history[-1][IMAGE_KEY][0]
                data=base64.b64decode(row["data"])
                self.assertNotIn(b"PRIVATE_METADATA_DO_NOT_SEND",data)
                with Image.open(io.BytesIO(data)) as image:
                    self.assertEqual(image.format,"PNG")
                    self.assertEqual(image.size,(8,12) if format=="JPEG" else (12,8))
                    self.assertEqual(image.info,{})
                self.assertEqual(row["sha256"],hashlib.sha256(data).hexdigest())
                self.assertEqual(receipt["attached"][0]["bytes"],len(raw))
                self.assertNotIn(row["data"],history[-1]["content"])
        self.provider.complete.assert_not_called()
        self.assertEqual(self.prompts,[])

    def test_mixed_inputs_reach_all_protocols_json_and_sse_with_the_same_pixels(self):
        raw=raster(metadata=True)
        expected=base64.b64decode(encode_image(raw)["data"])
        (self.root/"截图.png").write_bytes(raw)
        (self.root/"source.txt").write_bytes("完整源码\r\n".encode())
        for mode in ("history","run"):
            for protocol in ("chat_completions","messages","responses"):
                for streaming in (False,True):
                    with self.subTest(mode=mode,protocol=protocol,streaming=streaming):
                        captured,deltas=[],[]
                        reply=(chat_stream() if streaming else envelope(response())) if protocol=="chat_completions" else wire(protocol,response())
                        with backend(protocol,[reply],captured,stream=streaming and protocol!="chat_completions") as url:
                            config=replace(self.config,wire_api=protocol,base_url=url,proxy="direct",stream=streaming)
                            agent=self.agent(config,Provider(config),emit_delta=deltas.append)
                            if mode=="history":
                                history,_=agent.attach_files(["source.txt","截图.png"])
                                history,complete=agent.run("Explain screenshot",history)
                            else:
                                history,complete=agent.run("Explain screenshot",context_files=["source.txt","截图.png"])
                        self.assertTrue(complete)
                        self.assertEqual(len(captured),1)
                        self.assertEqual(native_images(captured[0]["body"]),[expected])
                        serialized=json.dumps(captured[0]["body"],ensure_ascii=False)
                        self.assertNotIn(IMAGE_KEY,serialized)
                        self.assertIn("完整源码",serialized)
                        self.assertLessEqual(captured[0]["bytes"],config.max_context_bytes)
                        self.assertEqual(any(IMAGE_KEY in row for row in history),mode=="history")
                        self.assertEqual(agent._context_files,[])
                        self.assertEqual(bool(deltas),streaming)
        self.assertEqual(self.prompts,[])

    def test_private_snapshot_restores_after_source_deletion_without_rereading(self):
        path=self.root/"screen.png"
        path.write_bytes(raster())
        history,_=self.agent().attach_files([path.name])
        with closing(SessionStore(self.parent/"sessions",self.config)) as store:
            store.save("image",history)
            path.unlink()
            restored=store.load("image")
            self.assertEqual(restored,history)
            with patch.object(Tools,"_parts",side_effect=AssertionError("snapshot reread source")):
                for protocol in ("chat_completions","messages","responses"):
                    payload=json.loads(request_body(replace(self.config,wire_api=protocol),restored,[]))
                    self.assertEqual(len(native_images(payload)),1)
        self.provider.complete.assert_not_called()

    def test_malformed_or_foreign_image_records_fail_before_http(self):
        asset={"path":"screen.png",**encode_image(raster())}
        valid={"role":"user","content":"Explicit image",IMAGE_KEY:[asset]}
        variants=[]
        for key,value in (("data","https://untrusted.invalid/picture.png"),("sha256","0"*64),
                          ("width",True),("height",999),("media_type","image/jpeg"),("url","https://untrusted.invalid")):
            row=copy.deepcopy(valid)
            row[IMAGE_KEY][0][key]=value
            variants.append(row)
        for role in ("system","assistant","tool"):
            variants.append({**valid,"role":role})
        variants.append({**valid,"sprig_input_id":"0"*32})
        variants.append({**valid,IMAGE_KEY:[]})
        variants.append({**valid,IMAGE_KEY:[asset]*17})
        for number,row in enumerate(variants):
            with self.subTest(variant=number,role=row["role"]):
                with self.assertRaises(SessionError): validate_history([{"role":"system","content":"system"},row])
                for protocol in ("chat_completions","messages","responses"):
                    with self.assertRaises(ProviderError) as caught:
                        Provider(replace(self.config,wire_api=protocol,base_url="http://127.0.0.1:1",proxy="direct")).complete([row],[])
                    self.assertEqual(caught.exception.code,"invalid_request")

    def test_saved_metadata_trailing_bytes_and_broken_pixels_are_rejected(self):
        normalized=base64.b64decode(encode_image(raster())["data"])
        damaged=bytearray(normalized)
        start=damaged.index(b"IDAT")
        length=int.from_bytes(damaged[start-4:start],"big")
        damaged[start+4]=0  # Invalid zlib header, but valid PNG CRC.
        damaged[start+4+length:start+8+length]=zlib.crc32(damaged[start:start+4+length]).to_bytes(4,"big")
        for raw in (raster(metadata=True),normalized+b"TRAILING",bytes(damaged)):
            asset={"path":"screen.png","media_type":"image/png","width":12,"height":8,
                   "sha256":hashlib.sha256(raw).hexdigest(),"data":base64.b64encode(raw).decode()}
            with self.assertRaises(ValueError): images({"role":"user","content":"image",IMAGE_KEY:[asset]})

    def test_png_critical_chunk_tails_are_not_metadata_free_snapshots(self):
        original={"path":"screen.png",**encode_image(raster())}
        raw=base64.b64decode(original["data"])
        extra=b"INERT_UNEXPECTED_CHUNK_DATA"
        for kind in (b"IHDR",b"IDAT",b"IEND"):
            start=raw.index(kind)
            length=int.from_bytes(raw[start-4:start],"big")
            chunk=raw[start:start+4+length]+extra
            changed=raw[:start-4]+(length+len(extra)).to_bytes(4,"big")+chunk+zlib.crc32(chunk).to_bytes(4,"big")+raw[start+8+length:]
            asset={**original,"sha256":hashlib.sha256(changed).hexdigest(),"data":base64.b64encode(changed).decode()}
            message={"role":"user","content":"image",IMAGE_KEY:[asset]}
            with self.subTest(kind=kind),self.assertRaises(ValueError): images(message)
            for protocol in ("chat_completions","messages","responses"):
                with self.subTest(kind=kind,protocol=protocol),self.assertRaises(ProviderError) as caught:
                    Provider(replace(self.config,wire_api=protocol,base_url="http://127.0.0.1:1",proxy="direct")).complete([message],[])
                self.assertEqual(caught.exception.code,"invalid_request")

    def test_pixel_dimension_and_animation_limits_stop_before_attachment_commit(self):
        raw=raster()
        for width,height in ((4097,1),(2049,2049)):
            modified=bytearray(raw)
            modified[16:24]=struct.pack(">II",width,height)
            modified[29:33]=zlib.crc32(modified[12:29]).to_bytes(4,"big")
            with patch.object(Image.Image,"load",side_effect=AssertionError("oversized pixel decode")):
                with self.assertRaisesRegex(ValueError,"4096|pixels"): encode_image(bytes(modified))
        output=io.BytesIO()
        with Image.new("RGB",(4,4),"red") as first, Image.new("RGB",(4,4),"blue") as second:
            first.save(output,format="GIF",save_all=True,append_images=[second])
        path=self.root/"animated.gif"
        path.write_bytes(output.getvalue())
        with self.assertRaisesRegex(ValueError,"animated"): self.agent().attach_files([path.name])
        self.assertEqual(self.saved,[])

    def test_encoded_output_and_normalized_batch_have_independent_hard_limits(self):
        small=raster("JPEG",size=(64,64),noise=True)
        self.assertLess(len(small),1024)
        with patch("sprig_agent.image_inputs.MAX_IMAGE_BYTES",1024):
            with self.assertRaisesRegex(ValueError,"normalized"): encode_image(small)
        source=raster("JPEG",size=(400,400),noise=True)
        for index in range(9): (self.root/f"image-{index}.jpg").write_bytes(source)
        self.assertLess(len(source)*9,2097152)
        with self.assertRaisesRegex(ValueError,"normalized image batch"):
            self.agent().attach_files([f"image-{index}.jpg" for index in range(9)])
        self.assertEqual(self.saved,[])

    def test_alpha_and_palette_transparency_survive_normalization(self):
        for mode,color in (("RGBA",(20,40,60,0)),("P",0)):
            with self.subTest(mode=mode):
                output=io.BytesIO()
                with Image.new(mode,(2,2),color) as original:
                    if mode=="P": original.info["transparency"]=0
                    original.save(output,format="PNG")
                normalized=base64.b64decode(encode_image(output.getvalue())["data"])
                with Image.open(io.BytesIO(normalized)) as restored:
                    self.assertEqual(restored.mode,"RGBA")
                    self.assertEqual(restored.getpixel((0,0))[3],0)

    def test_restored_normalized_batch_limit_and_request_cancellation(self):
        asset={"path":"screen.png",**encode_image(raster("JPEG",size=(400,400),noise=True))}
        with self.assertRaisesRegex(ValueError,"batch budget"):
            images({"role":"user","content":"image",IMAGE_KEY:[asset]*9})
        message={"role":"user","content":"image",IMAGE_KEY:[asset]}
        interrupted=threading.Event()
        original=Image.Image.load
        def cancel(image,*args,**kwargs):
            result=original(image,*args,**kwargs)
            interrupted.set()
            return result
        def check():
            if interrupted.is_set(): raise KeyboardInterrupt
        with patch.object(Image.Image,"load",cancel),self.assertRaises(KeyboardInterrupt):
            Provider(replace(self.config,base_url="http://127.0.0.1:1",proxy="direct")).complete([message],[],check_cancelled=check)

    def test_image_privacy_and_disabled_reading_precede_decoder_access(self):
        (self.root/"private.png").write_bytes(raster())
        (self.root/".sprigignore").write_text("private.png\n",encoding="utf-8")
        for config in (self.config,replace(self.config,disabled_tools=("read_file",))):
            with patch("PIL.Image.open",side_effect=AssertionError("private image decode")):
                with self.assertRaises(ValueError): self.agent(config).attach_files(["private.png"])
        self.assertEqual(self.saved,[])

    def test_cancel_after_decode_keeps_batch_out_of_history_and_provider(self):
        (self.root/"screen.png").write_bytes(raster())
        cancelled=threading.Event()
        agent=self.agent(cancel_event=cancelled)
        transpose=__import__("PIL.ImageOps",fromlist=["exif_transpose"]).exif_transpose
        def cancel(image):
            result=transpose(image)
            cancelled.set()
            return result
        with patch("PIL.ImageOps.exif_transpose",side_effect=cancel),self.assertRaises(KeyboardInterrupt):
            agent.attach_files(["screen.png"])
        self.assertEqual(self.saved,[])
        self.provider.complete.assert_not_called()

    def test_base64_bytes_count_in_actual_budget_before_baseline_and_http(self):
        (self.root/"screen.png").write_bytes(raster(size=(200,200),noise=True))
        config=replace(self.config,max_context_bytes=65536,check_before_task=True,checks=(("not-executed",),))
        with self.assertRaisesRegex(ProviderError,"context budget"):
            self.agent(config).run("Explain",context_files=["screen.png"])
        self.assertEqual(self.prompts,[])
        self.assertEqual(self.saved,[])

    def test_redaction_collision_refuses_outbound_and_session_overwrite(self):
        raw=raster()
        (self.root/"screen.png").write_bytes(raw)
        row=encode_image(raw)
        secret=row["data"][45:61]
        history,_=self.agent().attach_files(["screen.png"])
        config=replace(self.config,api_key=secret)
        with self.assertRaises(ValueError): self.agent(config).attach_files(["screen.png"])
        with closing(SessionStore(self.parent/"sessions",config)) as store:
            store.save("image",[{"role":"system","content":"system"}])
            before=(self.parent/"sessions"/"image.json").read_bytes()
            with self.assertRaises(SessionError): store.save("image",history)
            self.assertEqual((self.parent/"sessions"/"image.json").read_bytes(),before)

    def test_wire_prefix_secret_collisions_fail_without_contacting_a_provider(self):
        message={"role":"user","content":"image",IMAGE_KEY:[{"path":"screen.png",**encode_image(raster())}]}
        for protocol,secret in (("chat_completions","data:image"),("responses","data:image"),("messages","base64")):
            with self.subTest(protocol=protocol),self.assertRaises(ProviderError) as caught:
                Provider(replace(self.config,wire_api=protocol,api_key=secret,base_url="http://127.0.0.1:1",proxy="direct")).complete([message],[])
            self.assertEqual(caught.exception.code,"invalid_request")

    def test_invalid_image_does_not_commit_any_part_of_a_mixed_text_image_batch(self):
        (self.root/"source.txt").write_text("VALID_SOURCE",encoding="utf-8")
        (self.root/"broken.png").write_bytes(b"\x89PNG\r\n\x1a\nBROKEN")
        history=[{"role":"system","content":"system"},{"role":"user","content":"original"}]
        before=copy.deepcopy(history)
        with self.assertRaisesRegex(ValueError,"image"):
            self.agent().attach_files(["source.txt","broken.png"],history)
        self.assertEqual(history,before)
        self.assertEqual(self.saved,[])
        self.provider.complete.assert_not_called()

    def test_summary_and_history_pages_include_metadata_without_base64_payloads(self):
        (self.root/"screen.png").write_bytes(raster())
        history,_=self.agent().attach_files(["screen.png"])
        encoded=history[-1][IMAGE_KEY][0]["data"]
        metadata=public_message(history[-1])
        self.assertTrue(metadata["image_pixels_omitted"])
        self.assertNotIn(encoded,json.dumps(metadata))
        page=history_page(history,2,budget=4096)
        self.assertNotIn(encoded,page["text"])
        self.assertIn("screen.png",page["text"])
        prepared,report=prepare_context(self.config,history,[],True,force=True)
        self.assertTrue(report["within_budget"])
        self.assertEqual(len(native_images(json.loads(request_body(self.config,prepared,[])))),1)

    def test_real_edit_uses_native_pixels_and_retains_run_only_context_across_steps(self):
        raw=raster()
        (self.root/"screen.png").write_bytes(raw)
        (self.root/"answer.txt").write_text("old",encoding="utf-8")
        captured=[]
        def edit(payload):
            with Image.open(io.BytesIO(native_images(payload)[0])) as image:
                color=image.getpixel((0,0))
            self.assertEqual(color,(12,34,56))
            return envelope(call("edit","edit_file",{"path":"answer.txt","old":"old","new":str(color)}))
        check=(sys.executable,"-I","-B","-c","from pathlib import Path;assert Path('answer.txt').read_text()=='(12, 34, 56)'")
        with backend("chat_completions",[edit,envelope(response())],captured) as url:
            config=replace(self.config,base_url=url,proxy="direct",checks=(check,))
            history,complete=self.agent(config,Provider(config)).run("Repair using screenshot",context_files=["screen.png"])
        self.assertTrue(complete)
        self.assertEqual(len(captured),2)
        self.assertTrue(all(len(native_images(row["body"]))==1 for row in captured))
        self.assertEqual(len(self.prompts),2)
        self.assertFalse(any(IMAGE_KEY in row for row in history))

    def test_cli_file_sends_native_image_without_exposing_base64_in_terminal(self):
        (self.root/"screen.png").write_bytes(raster())
        captured=[]
        environment={name:value for name,value in os.environ.items()
                     if not name.upper().startswith("SPRIG_") and name.upper() not in {"OPENAI_API_KEY","AGNES_API_KEY"}}
        output=io.StringIO()
        with backend("chat_completions",[envelope(response())],captured) as url, \
                patch.dict(os.environ,environment,clear=True),redirect_stdout(output),redirect_stderr(output):
            status=cli.main(["--no-config","--cli","Explain","--model","fixture","--base-url",url,"--proxy","direct",
                             "--workdir",str(self.root),"--file","screen.png","--max-context-bytes","1048576"])
        self.assertEqual(status,0)
        self.assertEqual(len(native_images(captured[0]["body"])),1)
        self.assertNotIn("iVBOR",output.getvalue())
