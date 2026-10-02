"""Real paged file reads, bounded output, privacy and cancellable scan."""

import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sprig_agent.config import Config
from sprig_agent.tools import MAX_READ, Tools


class ReadPageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cancel = threading.Event()
        self.tools = Tools(Config(self.root, "test", max_output=256, read_only=True),
                           lambda _: self.fail("file reads must not request write or command approval"), self.cancel)
        self.addCleanup(self.tools.close)

    def read(self, **arguments):
        return self.tools.execute("read_file", {"path": "code.py", **arguments})

    def test_pages_preserve_unicode_crlf_and_unterminated_last_line(self):
        (self.root / "code.py").write_bytes("第一行\r\nsecond\r\n🙂 third\r\nlast".encode())
        first = self.read(offset=1, limit=2)
        self.assertEqual(first["content"], "第一行\r\nsecond\r\n")
        self.assertEqual((first["offset"], first["end_line"], first["total_lines"], first["next_offset"]), (1, 2, 4, 3))
        self.assertTrue(first["truncated"])
        second = self.read(offset=first["next_offset"], limit=2)
        self.assertEqual(second["content"], "🙂 third\r\nlast")
        self.assertEqual(second["end_line"], 4)
        self.assertFalse(second["truncated"])
        self.assertFalse(second["line_truncated"])
        self.assertIsNone(second["next_offset"])

    def test_byte_cap_returns_whole_lines_with_a_progressing_next_offset(self):
        line = "x" * 159 + "\n"
        (self.root / "code.py").write_text(line * 3, newline="", encoding="utf-8")
        pages, offset = [], 1
        while offset is not None:
            result = self.read(offset=offset, limit=3)
            pages.append(result["content"])
            self.assertEqual(len(result["content"].encode()), 160)
            self.assertFalse(result["line_truncated"])
            next_offset = result["next_offset"]
            if next_offset is not None:
                self.assertGreater(next_offset, offset)
            offset = next_offset
        self.assertEqual("".join(pages), line * 3)

    def test_search_and_pages_share_physical_line_numbers_with_unicode_separators(self):
        (self.root / "code.py").write_text("prefix\u2028same physical line\r\nwanted\r\nlast", encoding="utf-8", newline="")
        match = self.tools.execute("search", {"query": "wanted"})["matches"][0]
        self.assertEqual((match["line"], match["text"]), (2, "wanted"))
        self.assertEqual(self.read(offset=match["line"], limit=1)["content"], "wanted\r\n")
        self.assertEqual(self.read(limit=1)["content"], "prefix\u2028same physical line\r\n")

    def test_single_oversized_line_is_explicitly_incomplete_without_fake_continuation(self):
        (self.root / "code.py").write_text("🙂" * 200 + "\nnext\n", encoding="utf-8", newline="")
        result = self.read(limit=1)
        self.assertEqual(result["content"], "🙂" * 64)
        self.assertTrue(result["line_truncated"])
        self.assertTrue(result["truncated"])
        self.assertIsNone(result["next_offset"])
        self.assertEqual(self.read(offset=2, limit=1)["content"], "next\n")

    def test_can_inspect_code_beyond_legacy_prefix_and_observes_external_changes(self):
        source = "# context padding\n" * 1000 + "def target():\n    return 'old'\n"
        (self.root / "code.py").write_text(source, encoding="utf-8", newline="")
        self.assertNotIn("def target", self.read()["content"])
        page = self.read(offset=1001, limit=2)
        self.assertEqual(page["content"], "def target():\n    return 'old'\n")
        self.assertFalse(page["truncated"])
        (self.root / "code.py").write_text(source.replace("'old'", "'new'"), encoding="utf-8", newline="")
        self.assertIn("'new'", self.read(offset=1001, limit=2)["content"])

    def test_binary_invalid_utf8_and_credentials_outside_page_block_all_output(self):
        for suffix in (b"\x00", b"\xff", ("sk-" + "a" * 32).encode()):
            with self.subTest(kind=suffix[:3]):
                (self.root / "code.py").write_bytes(b"safe first line\n" + suffix)
                result = self.read(limit=1)
                self.assertIn("error", result)
                self.assertNotIn("content", result)
                self.assertNotIn(suffix.decode(errors="replace"), json.dumps(result))

    def test_range_parameters_empty_files_and_out_of_range_requests(self):
        (self.root / "code.py").touch()
        empty = self.read(limit=2)
        self.assertEqual((empty["content"], empty["end_line"], empty["total_lines"]), ("", 0, 0))
        self.assertFalse(empty["truncated"])
        for arguments in ({"offset": True}, {"offset": 0}, {"offset": 1000001}, {"offset": 2},
                          {"offset": 1.0}, {"limit": True}, {"limit": 0}, {"limit": 10001}, {"limit": "1"}):
            with self.subTest(arguments=arguments):
                self.assertIn("error", self.read(**arguments))

    def test_scan_limit_is_distinct_from_output_budget(self):
        (self.root / "code.py").write_bytes(b"x" * (MAX_READ + 1))
        self.assertIn("8 MiB scan limit", self.read(limit=1)["error"])
        self.assertTrue(self.read()["truncated"])

    def test_cancel_during_scan_closes_file_and_remains_cancelled_until_cleared(self):
        file = self.root / "code.py"
        file.write_text("padding\n" * 20000, encoding="utf-8", newline="")
        check, checks = self.tools._check_cancelled, []

        def stop_after_chunk():
            checks.append(True)
            if len(checks) == 3:
                self.cancel.set()
            check()

        with patch.object(self.tools, "_check_cancelled", side_effect=stop_after_chunk):
            with self.assertRaises(KeyboardInterrupt):
                self.read(offset=10000, limit=2)
        with self.assertRaises(KeyboardInterrupt):
            self.read(limit=1)
        file.write_text("closed handle\n", encoding="utf-8", newline="")
        self.cancel.clear()
        self.assertEqual(self.read(limit=1)["content"], "closed handle\n")

    def test_range_cannot_bypass_paths_or_fresh_ignore_policy(self):
        (self.root / "code.py").write_text("code\n", encoding="utf-8", newline="")
        self.assertEqual(self.read(limit=1)["content"], "code\n")
        (self.root / ".sprigignore").write_text("code.py\n", encoding="utf-8")
        self.assertIn("error", self.read(limit=1))
        for path in ("../outside", ".env", ".git/config", "/absolute"):
            self.assertIn("error", self.tools.execute("read_file", {"path": path, "offset": 2, "limit": 1}))


if __name__ == "__main__":
    unittest.main()
