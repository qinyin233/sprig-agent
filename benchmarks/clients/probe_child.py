"""Fixed diagnostic child: private home/config paths and no inherited API keys."""

import os
from pathlib import Path
import subprocess
import sys


def environment(root, original):
    # Preserve OS/loader settings, rather than guessing every provider's key or
    # configuration variable. Metadata commands need no model credentials.
    allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
               "LANG", "LC_ALL", "LC_CTYPE", "NUMBER_OF_PROCESSORS"}
    result = {name: value for name, value in original.items() if name.upper() in allowed}
    locations = {
        "HOME": "home", "USERPROFILE": "home", "APPDATA": "appdata", "LOCALAPPDATA": "localappdata",
        "XDG_CONFIG_HOME": "config", "XDG_DATA_HOME": "data", "XDG_CACHE_HOME": "cache", "XDG_STATE_HOME": "state",
        "PI_CODING_AGENT_DIR": "pi", "PI_SESSION_DIR": "pi-sessions",
        "MSWEA_GLOBAL_CONFIG_DIR": "mini",
    }
    for name, directory in locations.items():
        target = root / directory
        target.mkdir(parents=True, exist_ok=True)
        result[name] = str(target)
    result.update({"OPENCODE_DISABLE_AUTOUPDATE": "true", "OPENCODE_DISABLE_MODELS_FETCH": "true",
                   "OPENCODE_DISABLE_PROJECT_CONFIG": "true", "DO_NOT_TRACK": "1",
                   "AIDER_ANALYTICS": "false", "AIDER_CHECK_UPDATE": "false", "LITELLM_LOCAL_MODEL_COST_MAP": "True",
                   "MSWEA_SILENT_STARTUP": "1", "NO_COLOR": "1", "TERM": "dumb"})
    return result


if __name__ == "__main__":
    root = Path(sys.argv[1]).resolve(strict=True)
    argv = sys.argv[2:]
    child_env = environment(root, os.environ)
    if os.name == "nt":
        # Windows CRT execve loses Python's non-inheritable output handles.
        # Popen duplicates the explicit standard handles; the child inherits
        # the wrapper's existing Job Object and is cleaned up with it.
        process = subprocess.Popen(argv, env=child_env, stdin=subprocess.DEVNULL,
                                   stdout=sys.stdout, stderr=sys.stderr, creationflags=subprocess.CREATE_NO_WINDOW)
        raise SystemExit(process.wait())
    os.execve(argv[0], argv, child_env)
