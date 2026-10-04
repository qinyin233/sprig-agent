"""Relay an approved command without Windows CRT/MSYS quote rewriting."""

import os
import subprocess
import sys

if len(sys.argv) != 3:
    raise SystemExit("Expected bash executable and an approved command.")

child_env = dict(os.environ)
child_env["SPRIG_BENCH_COMMAND"] = sys.argv[2]
# The model command crosses the Python argv boundary, then the environment.
# Only this fixed short expression crosses the Windows-to-MSYS argv boundary.
result = subprocess.run([sys.argv[1], "--noprofile", "--norc", "-c", 'eval -- "$SPRIG_BENCH_COMMAND"'],
                        env=child_env, stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=sys.stderr,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
raise SystemExit(result.returncode)
