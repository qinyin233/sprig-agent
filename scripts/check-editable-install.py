"""Exercise the full editable package in private Unicode-path uv environments.

No model requests. A second Python may consume the first Python's PEP 660 wheel,
so the Windows 3.13 -> 3.11 locale failure is tested without changing user venvs.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MODULES = ("sprig_agent", "sprig_agent.cli", "sprig_agent.agent", "sprig_agent.provider",
           "sprig_agent.native_protocols", "sprig_agent.tui", "sprig_agent.pending_ui", "sprig_agent.session")
PROBE = "_sprig_install_probe"
SIBLING = "_sprig_unrelated_probe"


def child_environment():
    names = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "HOME", "USERPROFILE",
             "LOCALAPPDATA", "APPDATA", "LANG", "LC_ALL", "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR",
             "UV_PYTHON_BIN_DIR", "UV_LINK_MODE", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    selected = {name: value for name, value in os.environ.items() if name.upper() in names}
    selected.update(PYTHONUTF8="0", PYTHONNOUSERSITE="1", UV_NO_PROGRESS="1")
    return selected


def command(argv, *, env, cwd, allow_failure=False):
    result = subprocess.run(argv, env=env, cwd=cwd, capture_output=True, timeout=240)
    if result.returncode and not allow_failure:
        # Build-tool errors can include URLs or private paths. Keep diagnostics
        # categorical; never dump an inherited environment or credential output.
        raise RuntimeError("editable check command failed: " + Path(argv[0]).name + " (exit " + str(result.returncode) + ")")
    return result


def interpreter(environment):
    return environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def python_json(python, code, project, env, *arguments):
    result = command([str(python), "-I", "-X", "utf8=0", "-B", "-c", code, str(project), *arguments],
                     env=env, cwd=project, allow_failure=True)
    if result.returncode:
        return {"startup_or_import_failed": True,
                "gbk_startup_failure": b"UnicodeDecodeError" in result.stderr and b"gbk" in result.stderr}
    return json.loads(result.stdout)


def check(args):
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is required")
    base_env = child_environment()
    options = ["--offline"] if args.offline else []
    package = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    if package["project"]["name"] != "sprig-agent":
        raise RuntimeError("run from a Sprig source distribution or checkout")
    with tempfile.TemporaryDirectory(prefix="sprig-editable-") as temporary:
        private_root = Path(temporary).resolve()
        project = private_root / "随性 editable path"
        project.mkdir()
        for name in ("pyproject.toml", "uv.lock", "README.md", "LICENSE"):
            shutil.copy2(ROOT / name, project / name)
        source = project / "src/sprig_agent"
        source.mkdir(parents=True)
        files = sorted((ROOT / "src/sprig_agent").rglob("*.py"))
        for original in files:
            relative = original.relative_to(ROOT / "src/sprig_agent")
            destination = source / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, destination)
        environments = []
        for name, version in (("producer", args.producer_python), ("consumer", args.consumer_python or args.producer_python)):
            environment = project / name
            if not environment.resolve().is_relative_to(private_root):
                raise RuntimeError("environment escaped its private fixture")
            env = {**base_env, "UV_PROJECT_ENVIRONMENT": str(environment)}
            command([uv, "--no-config", "sync", *options, "--locked", "--no-install-project", "--python", version,
                     "--project", str(project)], env=env, cwd=project)
            environments.append(environment)
        producer = interpreter(environments[0])
        command([uv, "--no-config", "pip", "install", *options, "--python", str(producer),
                 *package["build-system"]["requires"]], env=base_env, cwd=project)
        wheels = project / "wheels"
        wheels.mkdir()
        command([str(producer), "-I", "-B", "-c",
                 "import setuptools.build_meta; setuptools.build_meta.build_editable('wheels')"], env=base_env, cwd=project)
        candidates = list(wheels.glob("*.whl"))
        if len(candidates) != 1:
            raise RuntimeError("build did not produce one editable wheel")
        wheel = candidates[0]
        with zipfile.ZipFile(wheel) as archive:
            hooks = [name for name in archive.namelist() if name.endswith(".pth")]
            ascii_hooks = bool(hooks) and all(archive.read(name).isascii() for name in hooks)
            metadata = next(name for name in archive.namelist() if name.endswith(".dist-info/WHEEL"))
            generator = next(line[11:] for line in archive.read(metadata).decode().splitlines() if line.startswith("Generator: "))
        for environment in environments:
            command([uv, "--no-config", "pip", "install", *options, "--python", str(interpreter(environment)),
                     "--no-deps", str(wheel)], env=base_env, cwd=project)
        # Added after the editable build: both new source and later edits must be
        # visible. An unrelated top-level source file must not be exposed.
        (source / (PROBE + ".py")).write_text("VALUE = 'first'\n", "utf-8")
        (source.parent / (SIBLING + ".py")).write_text("raise RuntimeError('unrelated source exposed')\n", "utf-8")
        inspect_code = """
import importlib, importlib.util, json, pathlib, sys
source = pathlib.Path(sys.argv[1]) / 'src/sprig_agent'
modules = json.loads(sys.argv[2])
loaded = [importlib.import_module(name) for name in modules]
probe = importlib.import_module('sprig_agent._sprig_install_probe')
print(json.dumps({'python': '.'.join(map(str, sys.version_info[:3])), 'utf8_mode': sys.flags.utf8_mode,
                  'source_imports': all(pathlib.Path(module.__file__).resolve().is_relative_to(source.resolve()) for module in loaded),
                  'sibling_hidden': importlib.util.find_spec('_sprig_unrelated_probe') is None, 'value': probe.VALUE}))
"""
        rows = []
        for environment in environments:
            python = interpreter(environment)
            first = python_json(python, inspect_code, project, base_env, json.dumps(MODULES))
            entrypoint = environment / ("Scripts/sprig.exe" if os.name == "nt" else "bin/sprig")
            help_result = command([str(entrypoint), "--no-config", "--help"], env=base_env, cwd=project, allow_failure=True)
            row = {"initial": first, "console_help": help_result.returncode == 0 and b"--model" in help_result.stdout}
            if not row["console_help"]:
                # Keep launcher/import failures diagnosable on native runners
                # without exposing arbitrary subprocess output or private paths.
                row["console_failure"] = {
                    "exit_code": help_result.returncode,
                    "stdout_bytes": len(help_result.stdout), "stderr_bytes": len(help_result.stderr),
                    "categories": [name for name in ("UnicodeDecodeError", "UnicodeEncodeError", "ModuleNotFoundError",
                                                      "ImportError", "OSError", "ValueError", "Fatal Python error")
                                   if name.encode("ascii") in help_result.stderr]}
            rows.append(row)
        (source / (PROBE + ".py")).write_text("VALUE = 'second revision'\n", "utf-8")
        for row, environment in zip(rows, environments):
            second = python_json(interpreter(environment), inspect_code, project, base_env, json.dumps(MODULES))
            row["source_update_visible"] = second.get("value") == "second revision" and second.get("source_imports") is True
        unchanged = all((source / path.relative_to(ROOT / "src/sprig_agent")).read_bytes() == path.read_bytes() for path in files)
        passed = ascii_hooks and unchanged and all(
            row["initial"].get("source_imports") is True and row["initial"].get("sibling_hidden") is True
            and row["initial"].get("value") == "first" and row["initial"].get("utf8_mode") == 0
            and row["source_update_visible"] and row["console_help"] for row in rows)
        return {"passed": passed, "platform": sys.platform, "editable_pth_ascii": ascii_hooks,
                "source_files": len(files), "source_files_unchanged": unchanged, "generator": generator,
                "producer": rows[0], "consumer": rows[1], "model_requests": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--producer-python", default=sys.executable)
    parser.add_argument("--consumer-python", help="optional different Python version or executable already available to uv")
    parser.add_argument("--offline", action="store_true", help="require prepopulated runtime and build caches; disable downloads")
    args = parser.parse_args()
    try:
        result = check(args)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        # Error messages here are fixed categories, not subprocess stdout/stderr.
        print(json.dumps({"passed": False, "stage_error": type(error).__name__,
                          "detail": str(error) if type(error) is RuntimeError else None, "model_requests": 0}))
        return 1
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
