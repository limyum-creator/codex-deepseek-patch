import copy
import io
import json
import os
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import Mock, patch as mock

import patch
from scripts import test_boot, test_offline


class FakeRpc:
    def __init__(self, models=None, thread=None):
        self.methods = []
        self.requests = []
        self.results = [ {}, {"data": models if models is not None else [
            {"model": "gpt-6.1-sol"}, {"model": patch.MODEL}]},
            thread if thread is not None else {"model": patch.MODEL, "modelProvider": patch.PROVIDER,
                                               "thread": {"id": "ephemeral-offline-test"}}]

    def send(self, method, parameters=None, notification=False):
        self.methods.append(method)
        self.requests.append((method, parameters, notification))
        return len(self.requests)

    def receive(self, identifier):
        return self.results.pop(0)


class OfflineTests(unittest.TestCase):
    def test_probe_registers_one_model_and_starts_only_ephemeral_local_thread(self):
        rpc = FakeRpc()
        result = test_offline.check_session(rpc, Path("/tmp/offline-work"))
        self.assertEqual(result["inference_turns"], 0)
        self.assertEqual(result["deepseek_models"], 1)
        self.assertEqual(rpc.methods, ["initialize", "initialized", "model/list", "thread/start"])
        thread = rpc.requests[-1][1]
        self.assertTrue(thread["ephemeral"])
        self.assertEqual(thread["approvalPolicy"], "never")
        self.assertEqual(thread["sandbox"], "read-only")
        self.assertEqual(thread["modelProvider"], patch.PROVIDER)
        self.assertEqual(thread["model"], patch.MODEL)

    def test_missing_duplicate_or_other_deepseek_model_is_rejected(self):
        for models in ([], [{"model": patch.MODEL}, {"model": patch.MODEL}],
                       [{"model": "deepseek-other"}], [{"model": patch.MODEL}, {"model": "deepseek-other"}]):
            rpc = FakeRpc(models=models)
            with self.assertRaises(patch.PatchError):
                test_offline.check_session(rpc, Path("/tmp/work"))
            self.assertNotIn("thread/start", rpc.methods)

    def test_catalog_pagination_checks_all_pages(self):
        rpc = FakeRpc()
        rpc.results = [{}, {"data": [{"model": "gpt-6.1-sol"}], "nextCursor": "page2"},
                       {"data": [{"model": patch.MODEL}]},
                       {"model": patch.MODEL, "modelProvider": patch.PROVIDER, "thread": {"id": "test"}}]
        result = test_offline.check_session(rpc, Path("/tmp/work"))
        self.assertEqual(result["catalog_models"], 2)
        self.assertEqual(rpc.requests[3][1]["cursor"], "page2")

    def test_repeated_catalog_pagination_cursor_is_rejected(self):
        rpc = FakeRpc()
        rpc.results = [{}, {"data": [], "nextCursor": "same"}, {"data": [], "nextCursor": "same"}]
        with self.assertRaises(patch.PatchError):
            test_offline.check_session(rpc, Path("/tmp/work"))

    def test_wrong_thread_model_provider_or_missing_thread_is_rejected(self):
        for thread in ({"model": "gpt-6.1-sol", "modelProvider": patch.PROVIDER, "thread": {"id": "x"}},
                       {"model": patch.MODEL, "modelProvider": "openai", "thread": {"id": "x"}},
                       {"model": patch.MODEL, "modelProvider": patch.PROVIDER, "thread": {}}):
            with self.assertRaises(patch.PatchError):
                test_offline.check_session(FakeRpc(thread=thread), Path("/tmp/work"))

    def test_disposable_environment_excludes_real_keys_auth_and_user_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            with mock.dict(os.environ, {"OPENAI_API_KEY": "real-openai-value", "DEEPSEEK_API_KEY": "real-deepseek-value",
                                        "CODEX_API_KEY": "real-codex-value", "HOME": "/real/home", "PATH": "/safe/bin"}, clear=True):
                environment = test_offline.isolated_environment(base)
            for name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "CODEX_API_KEY"):
                self.assertNotIn(name, environment)
            self.assertEqual(environment["CODEX_HOME"], str(base / "home"))
            self.assertEqual(environment["HOME"], str(base / "user"))
            self.assertEqual(environment["USERPROFILE"], str(base / "user"))
            self.assertEqual(environment["PATH"], "/safe/bin")
            self.assertTrue((base / "home").is_dir())
            self.assertNotIn("real-", json.dumps(environment))

    def test_isolated_cli_and_gui_disable_background_plugin_downloads(self):
        with mock.object(patch, "cli_path", return_value=Path("/probe/codex")):
            command = test_offline.probe_command(Path("/probe/app"))
        self.assertEqual(command[1:5], ["--disable", "plugins", "--disable", "remote_plugin"])
        self.assertEqual(command[-3:], ["app-server", "--listen", "stdio://"])
        config = patch.tomllib.loads(test_offline.offline_config(Path("/probe/catalog.json")))
        self.assertFalse(config["features"]["plugins"])
        self.assertFalse(config["features"]["remote_plugin"])

    def test_offline_provider_uses_loopback_env_dummy_and_no_credential_command(self):
        config = patch.tomllib.loads(test_offline.offline_config(Path("/tmp/catalog.json")))
        provider = config["model_providers"][patch.PROVIDER]
        self.assertEqual(provider["base_url"], "http://127.0.0.1:9/")
        self.assertEqual(provider["env_key"], test_offline.DUMMY_KEY_VARIABLE)
        self.assertFalse(provider["requires_openai_auth"])
        self.assertNotIn("auth", provider)
        self.assertNotIn("api_key", provider)
        self.assertEqual(config["model"], patch.MODEL)

    def rpc_client(self, messages=()):
        process = Mock(stdin=io.StringIO(), stdout=io.StringIO("".join(json.dumps(value) + "\n" for value in messages)))
        client = test_offline.RpcClient(process)
        client.reader.join(timeout=1)
        return process, client

    def test_rpc_allowlist_rejects_every_inference_tool_or_arbitrary_request(self):
        process, client = self.rpc_client()
        for method in ("turn/start", "turn/steer", "command/exec", "thread/resume", "models", "unknown"):
            with self.assertRaises(patch.PatchError):
                client.send(method, {})
        self.assertEqual(process.stdin.getvalue(), "")

    def test_rpc_uses_json_lines_and_notification_has_no_request_id(self):
        process, client = self.rpc_client([{"id": 1, "result": {"ok": True}}])
        identifier = client.send("initialize", {"clientInfo": {"name": "probe"}})
        self.assertEqual(client.receive(identifier), {"ok": True})
        client.send("initialized", notification=True)
        values = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
        self.assertEqual(values[0]["id"], 1)
        self.assertNotIn("id", values[1])

    def test_rpc_rejects_error_and_early_eof(self):
        for messages in ([], [{"id": 1, "error": {"message": "rejected"}}]):
            process, client = self.rpc_client(messages)
            with self.assertRaises(patch.PatchError):
                client.receive(1)

    def test_rpc_ignores_notifications_before_response(self):
        process, client = self.rpc_client([{"method": "notification"}, {"id": 2, "result": "okay"}])
        self.assertEqual(client.receive(2), "okay")

    def test_windows_cleanup_targets_only_owned_pid_tree(self):
        process = Mock(pid=12345)
        process.poll.return_value = None
        with mock.object(test_offline.sys, "platform", "win32"), mock.object(test_offline.subprocess, "run") as runner:
            test_offline.stop_probe(process)
        self.assertEqual(runner.call_args.args[0], ["taskkill.exe", "/PID", "12345", "/T", "/F"])
        process.kill.assert_not_called()

    def test_windows_cleanup_does_not_kill_naturally_exited_probe(self):
        process = Mock(pid=12345)
        process.poll.return_value = 0
        with mock.object(test_offline.sys, "platform", "win32"), mock.object(test_offline.subprocess, "run") as runner:
            test_offline.stop_probe(process)
        runner.assert_not_called()

    def test_offline_failure_is_recorded_and_probe_is_cleaned(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            manifest = {"status": "prepared", "patched_asar": "hash"}
            process = Mock(stdin=None, stdout=None)
            writes = []
            with mock.object(patch, "load_build", return_value=(directory, manifest)), \
                 mock.object(patch, "verify_app"), mock.object(patch, "cli_path", return_value=directory / "cli"), \
                 mock.object(patch, "save_json", side_effect=lambda path, value: writes.append((path.name, copy.deepcopy(value)))), \
                 mock.object(test_offline.subprocess, "Popen", return_value=process), \
                 mock.object(test_offline, "RpcClient"), \
                 mock.object(test_offline, "check_session", side_effect=RuntimeError("routing failed")), \
                 mock.object(test_offline, "stop_probe") as cleanup:
                with self.assertRaises(RuntimeError):
                    test_offline.main(directory)
            cleanup.assert_called_once_with(process)
            self.assertEqual(writes[-1][1]["offline_probe"], "failed")
            evidence = [value for name, value in writes if name == "offline-result.json"][0]
            self.assertEqual(evidence["inference_turns"], 0)
            self.assertEqual(evidence["status"], "failed")


class BootTests(unittest.TestCase):
    def test_mock_keychain_is_mac_probe_only_and_verified_on_its_own_process(self):
        for platform in ['darwin', 'win32', 'linux']:
            with mock.object(test_boot.sys, 'platform', platform):
                args = test_boot.boot_command('/stage/app', '/temporary/browser')
                self.assertEqual('--use-mock-keychain' in args, platform == 'darwin')
                self.assertIn('--user-data-dir=/temporary/browser', args)
        rows = [{'pid': 100, 'ppid': 1, 'command': '/stage/app --user-data-dir=/temporary/browser'},
                {'pid': 200, 'ppid': 1, 'command': '/other/app --use-mock-keychain'}]
        self.assertFalse(test_boot.inspect_children(rows, 100, Path('/temporary/browser'), Path('/stage/cli'))['mock_keychain'])
        rows[0]['command'] += ' --use-mock-keychain'
        self.assertTrue(test_boot.inspect_children(rows, 100, Path('/temporary/browser'), Path('/stage/cli'))['mock_keychain'])

    def test_owned_tree_excludes_another_running_app_and_grandchildren_are_included(self):
        rows = [{"pid": 100, "ppid": 1, "command": "root"},
                {"pid": 101, "ppid": 100, "command": "helper"},
                {"pid": 102, "ppid": 101, "command": "renderer"},
                {"pid": 200, "ppid": 1, "command": "real-user-app"}]
        self.assertEqual({row["pid"] for row in test_boot.owned_processes(rows, 100)}, {100, 101, 102})

    def test_complete_isolated_tree_requires_renderer_and_exact_bundled_cli(self):
        browser, cli = Path("/temp/browser"), Path("/stage/resources/codex.exe")
        rows = [{"pid": 100, "ppid": 1, "command": '/stage/ChatGPT --user-data-dir="' + str(browser) + '"'},
                {"pid": 101, "ppid": 100, "command": '/stage/Helper --type=renderer'},
                {"pid": 102, "ppid": 100, "command": '"' + str(cli) + '" -c x=true app-server --listen stdio://'},
                {"pid": 999, "ppid": 1, "command": '/production/resources/codex.exe app-server'}]
        result = test_boot.inspect_children(rows, 100, browser, cli)
        self.assertTrue(result["isolated_profile"])
        self.assertTrue(result["renderer"])
        self.assertTrue(result["backend"])
        self.assertEqual(result["owned_processes"], 3)
        wrong = test_boot.inspect_children(rows, 100, Path("/temp/browser"), Path("/production/resources/codex.exe"))
        self.assertFalse(wrong["backend"])

    def test_other_app_renderer_and_backend_cannot_satisfy_probe(self):
        rows = [{"pid": 100, "ppid": 1, "command": '/stage/ChatGPT --user-data-dir=/temp/browser'},
                {"pid": 201, "ppid": 200, "command": '/stage/Helper --type=renderer'},
                {"pid": 202, "ppid": 200, "command": '/stage/resources/codex.exe app-server'}]
        result = test_boot.inspect_children(rows, 100, Path("/temp/browser"), Path("/stage/resources/codex.exe"))
        self.assertFalse(result["renderer"])
        self.assertFalse(result["backend"])

    def test_windows_process_table_parses_singleton_and_queries_parent_command_fields(self):
        value = json.dumps({"ProcessId": 1, "ParentProcessId": 0, "CommandLine": "one"})
        with mock.object(test_boot.sys, "platform", "win32"), \
             mock.object(test_boot.subprocess, "check_output", return_value=value) as runner:
            result = test_boot.process_table()
        self.assertEqual(result, [{"pid": 1, "ppid": 0, "command": "one"}])
        script = runner.call_args.args[0][-1]
        self.assertIn("Get-CimInstance Win32_Process", script)
        self.assertIn("ParentProcessId", script)
        self.assertIn("CommandLine", script)

    def test_mac_process_table_preserves_spaces_in_commands(self):
        with mock.object(test_boot.sys, "platform", "darwin"), \
             mock.object(test_boot.subprocess, "check_output", return_value='  22   1 /My App/Helper --type=renderer\n'):
            result = test_boot.process_table()
        self.assertEqual(result[0]["command"], "/My App/Helper --type=renderer")


if __name__ == "__main__":
    unittest.main()
