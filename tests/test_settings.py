"""Real private files and loopback HTTP; configuration never needs a paid API."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent import cli
from sprig_agent.filesystem import Directory
from sprig_agent.policy import command_environment
from sprig_agent.provider import Provider, NoRedirect
from sprig_agent.session import SessionError, SessionStore
from sprig_agent.settings import Settings, TEMPLATE, initialize_config, load_settings, public_config
from sprig_agent.tools import Tools
from tests.test_generation_options import envelope, local_backend


class SettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.path = self.parent / "config.toml"
        self.secret = "fixture-key-never-a-real-credential"
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        home = patch("pathlib.Path.home", return_value=self.parent)
        home.start()
        self.addCleanup(home.stop)

    def write(self, text, path=None):
        target = path or self.path
        with Directory(target.parent) as directory:
            directory.write_new(target.name, text.encode("utf-8"), private=True)
        return target

    def resolve(self, settings, **overrides):
        return settings.resolve({"workdir": str(self.root), **overrides}, os.environ)

    def invoke(self, arguments):
        out, error = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(error):
            status = cli.main(["--workdir", str(self.root), *arguments])
        self.assertNotIn(self.secret, out.getvalue() + error.getvalue())
        return status, out.getvalue(), error.getvalue()

    def test_initialize_confirms_exact_template_and_never_overwrites(self):
        path = self.parent / "new" / "config.toml"
        confirm = Mock(return_value=False)
        self.assertFalse(initialize_config(path, confirm))
        self.assertIn(str(path), confirm.call_args.args[0])
        self.assertIn(TEMPLATE, confirm.call_args.args[0])
        self.assertFalse(path.parent.exists())
        self.assertTrue(initialize_config(path, lambda _: True))
        with Directory(path.parent) as directory:
            directory.check_private(path.name)
        self.assertEqual(load_settings(path).data["provider"], "agnes")
        with self.assertRaisesRegex(ValueError, "already exists"):
            initialize_config(path, lambda _: self.fail("must not ask to overwrite"))

    def test_cli_init_rejects_noninteractive_and_creation_is_local_only(self):
        with patch.object(cli, "Provider") as provider, patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(self.invoke(["--config", str(self.path), "--init-config"])[0], 2)
            self.assertFalse(self.path.exists())
        with patch.object(cli, "Provider") as provider, patch.object(cli, "approve", return_value=True):
            self.assertEqual(self.invoke(["--config", str(self.path), "--init-config"])[0], 0)
            provider.assert_not_called()

    def test_precedence_boolean_overrides_and_provider_selection(self):
        self.write(TEMPLATE + '\n[providers.other]\nmodel = "other-model"\nbase_url = "https://other.example/v1"\napi_key_env = "OTHER_KEY"\n')
        settings = load_settings(self.path)
        with patch.dict(os.environ, {"AGNES_API_KEY": self.secret}):
            config, details = self.resolve(settings)
            self.assertEqual((config.provider_name, config.model, config.api_key), ("agnes", "agnes-3.0-flash", self.secret))
            self.assertEqual((config.timeout, config.max_tokens, config.stream), (60, 2048, True))
            self.assertNotIn(self.secret, repr(config) + repr(settings) + json.dumps(public_config(config, details)))
        with patch.dict(os.environ, {"SPRIG_PROVIDER": "other", "OTHER_KEY": self.secret, "SPRIG_MODEL": "env-model"}):
            config, _ = self.resolve(settings, model="flag-model", stream=False, thinking=True)
            self.assertEqual((config.provider_name, config.model, config.api_key), ("other", "flag-model", self.secret))
            self.assertFalse(config.stream)
            self.assertTrue(config.thinking)
        with patch.dict(os.environ, {"SPRIG_API_KEY": self.secret, "OPENAI_API_KEY": "lower", "AGNES_API_KEY": "lower-agnes"}):
            self.assertEqual(self.resolve(settings)[0].api_key, self.secret)

    def test_configured_credentials_never_follow_overridden_endpoint(self):
        self.write('provider="x"\n[providers.x]\nmodel="test"\nbase_url="https://trusted.example/v1"\napi_key="' + self.secret + '"\n')
        settings = load_settings(self.path)
        for url in ("https://other.example/v1", "https://trusted.example/other"):
            with self.assertRaisesRegex(ValueError, "overridden base URL"):
                self.resolve(settings, base_url=url)
        with patch.dict(os.environ, {"SPRIG_API_KEY": "explicit-key"}):
            self.assertEqual(self.resolve(settings, base_url="https://other.example/v1")[0].api_key, "explicit-key")
        self.assertEqual(self.resolve(settings)[0].api_key, self.secret)

    def test_missing_named_environment_variable_is_an_error_without_fallback(self):
        self.write(TEMPLATE)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "unrelated-provider-key"}):
            with self.assertRaisesRegex(ValueError, "unset or empty"):
                self.resolve(load_settings(self.path))
        with self.assertRaisesRegex(ValueError, "not defined"):
            self.resolve(load_settings(self.path), provider="missing")

    def test_agnes_alias_cannot_be_rebound_by_a_config_env_reference(self):
        self.write('provider="x"\n[providers.x]\nmodel="test"\nbase_url="https://other.example/v1"\napi_key_env="AGNES_API_KEY"\n')
        with patch.dict(os.environ, {"AGNES_API_KEY": self.secret}):
            with self.assertRaisesRegex(ValueError, "Agnes HTTPS origin"):
                self.resolve(load_settings(self.path))

    def test_invalid_files_are_bounded_and_diagnostics_never_echo_values(self):
        cases = [
            'api_key="' + self.secret + '"',  # unknown top-level setting
            '[providers.x]\napi_key="' + self.secret,  # malformed TOML
            '[providers.x]\napi_key=true',
            '[providers.x]\napi_key="x"\napi_key_env="X"',
            '[providers.x]\nbase_url="http://remote.example/v1"',
            '[providers.x]\napi_key_env="invalid-name"',
            'provider="missing"',
            'interface=[]',
            '[agent]\nmax_steps=true',
            '[agent]\nmax_tokens=0',
            '[agent]\nchecks=["python"]',
            '[agent]\nchecks=[[true]]',
            '[agent]\nread_only=true\nchecks=[["python"]]',
            '[agent]\nauto_approve=true',
            '#' + 'x' * 65536,
        ]
        for index, text in enumerate(cases):
            with self.subTest(index=index):
                path = self.write(text, self.parent / f"invalid-{index}.toml")
                with self.assertRaises(ValueError) as error:
                    load_settings(path)
                self.assertNotIn(self.secret, str(error.exception))
                with patch.object(cli, "Provider") as provider:
                    self.assertEqual(self.invoke(["--config", str(path), "--doctor"])[0], 1)
                    provider.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits")
    def test_insecure_permissions_and_symlink_ancestors_are_rejected(self):
        self.write(TEMPLATE)
        self.path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "private"):
            load_settings(self.path)
        self.path.chmod(0o600)
        link = self.parent / "alias"
        link.symlink_to(self.parent, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            load_settings(link / self.path.name)

    @unittest.skipUnless(os.name == "nt", "native Windows ACLs and junctions")
    def test_windows_public_acl_and_junction_ancestors_are_rejected(self):
        self.write(TEMPLATE)
        link = self.parent / "alias"
        subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(self.parent)], check=True, capture_output=True)
        try:
            with self.assertRaisesRegex(ValueError, "symlinks"):
                load_settings(link / self.path.name)
        finally:
            link.rmdir()
        subprocess.run(["icacls.exe", str(self.path), "/grant", "*S-1-1-0:(R)"], check=True, capture_output=True)
        with self.assertRaisesRegex(ValueError, "private"):
            load_settings(self.path)

    def test_missing_default_and_explicit_path_and_no_repo_discovery(self):
        self.write(TEMPLATE, self.root / "config.toml")
        absent = self.parent / "absent.toml"
        with patch("sprig_agent.settings.default_config_path", return_value=absent), patch.object(cli, "Provider") as provider:
            code, body, _ = self.invoke(["--model", "fixture", "--show-config"])
            self.assertEqual(code, 0)
            self.assertIsNone(json.loads(body)["config_file"])
            self.assertEqual(self.invoke(["--config", str(absent), "--show-config"])[0], 1)
            provider.assert_not_called()

    def test_no_config_overrides_file_and_environment_path(self):
        self.write("this isn't valid TOML")
        with patch.dict(os.environ, {"SPRIG_CONFIG": str(self.path)}), patch.object(cli, "Provider") as provider:
            self.assertEqual(self.invoke(["--show-config", "--model", "fixture"])[0], 1)
            code, body, _ = self.invoke(["--no-config", "--show-config", "--model", "fixture"])
            self.assertEqual(code, 0)
            self.assertIsNone(json.loads(body)["config_file"])
            provider.assert_not_called()

    def test_doctor_show_config_and_session_management_never_start_provider(self):
        self.write(TEMPLATE)
        with patch.dict(os.environ, {"AGNES_API_KEY": self.secret}), patch.object(cli, "Provider") as provider:
            for option in ("--show-config", "--doctor"):
                status, out, _ = self.invoke(["--config", str(self.path), option])
                self.assertEqual(status, 0)
                report = json.loads(out)
                if option == "--doctor":
                    self.assertFalse(report["network_contacted"])
            provider.assert_not_called()
        with patch.dict(os.environ, {"SPRIG_CONFIG": "missing-file"}), patch.object(cli, "Provider") as provider:
            self.assertEqual(self.invoke(["--list-sessions", "--session-dir", str(self.parent / "sessions")])[0], 0)
            provider.assert_not_called()

    def test_active_config_hidden_from_every_file_tool_and_custom_env_removed(self):
        path = self.write('provider="x"\n[providers.x]\nmodel="test"\napi_key="arbitrary-secret-not-matched-by-sk-pattern"\n', self.root / "private.toml")
        config, _ = self.resolve(load_settings(path))
        approve = Mock(return_value=True)
        tools = Tools(config, approve)
        self.addCleanup(tools.close)
        for name, args in (("read_file", {"path": "private.toml"}), ("search", {"path": "private.toml", "query": "secret"}),
                           ("edit_file", {"path": "private.toml", "old": "test", "new": "bad"})):
            self.assertIn("error", tools.execute(name, args))
        self.assertNotIn("private.toml", json.dumps(tools.execute("project_map", {})))
        self.assertNotIn("private.toml", json.dumps(tools.execute("list_files", {})))
        approve.assert_not_called()
        with patch.dict(os.environ, {"AGNES_API_KEY": "other-key", "CUSTOM_TOKEN": "unused-provider-key", "NORMAL": "value"}):
            env = command_environment(replace(config, inherit_env=True, credential_env_vars=("CUSTOM_TOKEN",)))
            self.assertNotIn("CUSTOM_TOKEN", env)
            self.assertNotIn("AGNES_API_KEY", env)
            self.assertEqual(env["NORMAL"], "value")

    def test_actual_loopback_request_uses_selected_key_and_generation_controls(self):
        requests, authorizations = [], []
        with local_backend(envelope({"role": "assistant", "content": "Done"}), requests, authorizations) as url:
            self.write('provider="local"\n[providers.local]\nmodel="fixture"\nbase_url="' + url + '"\napi_key_env="LOCAL_KEY"\n[agent]\nmax_tokens=1234\nthinking=true\n')
            with patch.dict(os.environ, {"LOCAL_KEY": self.secret}):
                config, _ = self.resolve(load_settings(self.path))
            provider = Provider(config)
            provider.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            self.assertEqual(provider.complete([{"role": "user", "content": "test"}], [])["content"], "Done")
        self.assertEqual(requests[0][0]["model"], "fixture")
        self.assertEqual(requests[0][0]["max_tokens"], 1234)
        self.assertEqual(requests[0][0]["chat_template_kwargs"], {"enable_thinking": True})
        self.assertEqual(authorizations, ["Bearer " + self.secret])
        # Key values never enter the request JSON or a resumable binding.
        self.assertNotIn(self.secret, json.dumps(requests))
        self.assertNotIn(self.secret, json.dumps(config.binding()))

    def test_configured_checks_remain_bound_when_session_is_resumed(self):
        self.write('provider="x"\n[providers.x]\nmodel="test"\n[agent]\nchecks=[["python", "-V"]]\n')
        config, _ = self.resolve(load_settings(self.path))
        self.assertEqual(config.checks, (("python", "-V"),))
        with self.assertRaisesRegex(ValueError, "read-only"):
            self.resolve(load_settings(self.path), read_only=True)
        store = SessionStore(self.parent / "sessions", config)
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}]
        try:
            store.save("configured", history)
        finally:
            store.close()
        other = SessionStore(self.parent / "sessions", replace(config, checks=()))
        try:
            with self.assertRaisesRegex(SessionError, "history will not be sent"):
                other.load("configured")
        finally:
            other.close()

    def test_tui_receives_effective_config_and_flags_override_boolean_defaults(self):
        self.write(TEMPLATE)
        with patch.dict(os.environ, {"AGNES_API_KEY": self.secret}), patch("sys.stdin.isatty", return_value=True), \
                patch("sys.stdout.isatty", return_value=True), patch("sprig_agent.tui.run_tui", return_value=0) as tui:
            self.assertEqual(cli.main(["--workdir", str(self.root), "--config", str(self.path), "--thinking", "--no-project-instructions"]), 0)
            config = tui.call_args.args[0]
            self.assertEqual(config.model, "agnes-3.0-flash")
            self.assertTrue(config.stream)
            self.assertTrue(config.thinking)
            self.assertFalse(config.project_instructions)
        with patch.dict(os.environ, {"AGNES_API_KEY": self.secret}):
            code, body, _ = self.invoke(["--config", str(self.path), "--cli", "--no-stream", "--no-thinking", "--show-config"])
            self.assertEqual(code, 0)
            self.assertFalse(json.loads(body)["agent"]["stream"])
            self.assertFalse(json.loads(body)["agent"]["thinking"])


if __name__ == "__main__":
    unittest.main()
