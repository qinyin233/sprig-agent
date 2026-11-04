"""CLI help remains usable with Unicode launch paths and legacy output encodings."""
from contextlib import redirect_stdout
from importlib.machinery import ModuleSpec
import io
import sys
import unittest
from unittest.mock import patch

from sprig_agent import cli


class CliHelpTests(unittest.TestCase):
    def test_unicode_launch_paths_do_not_enter_legacy_encoded_help(self):
        # Python 3.14 includes a zip entry point's path in its default program
        # name. A console-script wheel in a Chinese directory reproduces this.
        for argv0 in ("随性 editable path/producer/Scripts/sprig.exe", "随性.py"):
            with self.subTest(argv0=argv0), io.BytesIO() as raw:
                with io.TextIOWrapper(raw, encoding="cp1252", errors="strict") as output:
                    with patch.object(sys, "argv", [argv0]), \
                            patch.object(sys.modules["__main__"], "__spec__", ModuleSpec("__main__", None)), \
                            patch.object(cli, "Provider") as provider, redirect_stdout(output):
                        with self.assertRaises(SystemExit) as caught:
                            cli.main(["--no-config", "--help"])
                    self.assertEqual(caught.exception.code, 0)
                    provider.assert_not_called()
                    output.flush()
                    help_text = raw.getvalue()
                    self.assertIn(b"usage: sprig", help_text)
                    self.assertIn(b"--model", help_text)
                    self.assertNotIn(b"producer", help_text)
