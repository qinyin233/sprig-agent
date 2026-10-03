"""Help-only adapter for mini's import-time prompt on non-console handles."""

import runpy
import sys

if sys.argv[1:] != ["--help"]:
    raise SystemExit("This adapter accepts only --help; it cannot run tasks.")

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import DummyInput
from prompt_toolkit.output import DummyOutput

# mini creates a PromptSession during import even for --help. On Windows that
# requires console handles. Typer still writes its own real help to stdout;
# only the unused prompt session is assigned inert IO.
with create_app_session(input=DummyInput(), output=DummyOutput()):
    runpy.run_module("minisweagent.run.mini", run_name="__main__")
