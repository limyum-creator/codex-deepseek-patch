import argparse
import datetime as dt
import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import Mock, patch as mock
import xml.etree.ElementTree as ET

import monitor


class FakeBackend:
    stable_seconds = 10

    def __init__(self):
        self.current = "v1"
        self.running = False
        self.compatible = True
        self.builds = []
        self.installs = []
        self.adoption = None
        self.inspect_count = 0
        self.change_during_build = None
        self.change_during_running_check = None
        self.fail_build = None
        self.fail_install = None
        self.installed_fingerprint = "patched-v1"

    def snapshot(self):
        return {"fingerprint": self.current, "version": self.current}

    def adopt(self):
        return self.adoption

    def inspect(self, snapshot):
        self.inspect_count += 1
        return {"compatible": self.compatible, "reason": "Unknown routing anchor"}

    def build(self, snapshot, output):
        self.builds.append(snapshot["fingerprint"])
        if self.fail_build:
            raise self.fail_build
        output.mkdir()
        if self.change_during_build:
            self.current = self.change_during_build
        return {"directory": str(output)}

    def is_running(self):
        if self.change_during_running_check:
            self.current = self.change_during_running_check
        return self.running

    def install(self, build, snapshot):
        self.installs.append(snapshot["fingerprint"])
        if self.fail_install:
            raise self.fail_install
        self.current = self.installed_fingerprint
        return {"installed_fingerprint": self.current}


class MonitorTests(unittest.TestCase):
    def test_single_start_crosses_stability_wait_and_installs_once(self):
        from update_progress import Progress
        progress=Progress(self.directory);identity=progress.begin('ds','1.0','candidate')
        monitor.atomic_json(self.directory/'progress.request.json',{'id':identity,'action':'start'})
        def advance(seconds):self.now+=seconds
        result=monitor.run_requested(self.monitor,sleep=advance)
        self.assertEqual(result['phase'],'installed')
        self.assertEqual(len(self.backend.builds),1);self.assertEqual(len(self.backend.installs),1)
        self.assertEqual(self.now,1010)

    def test_passive_check_returns_immediately_without_an_install_click(self):
        sleeper=Mock()
        self.assertEqual(monitor.run_requested(self.monitor,sleep=sleeper)['phase'],'stabilizing')
        sleeper.assert_not_called();self.assertEqual(self.backend.installs,[])

    def test_busy_owner_preserves_and_consumes_coalesced_settings_request_once(self):
        self.backend.settings={}
        monitor.request_update(self.directory,sync_now=True)
        latest=monitor.request_update(self.directory,retry=True,code_now=True)
        with monitor.state_lock(self.directory) as locked:
            self.assertTrue(locked)
            self.assertEqual(self.poll()['phase'],'busy')
            self.assertTrue((self.directory/'monitor.request.json').exists())
        self.poll()
        self.assertTrue(self.backend.settings['force_sync'])
        self.assertTrue(self.backend.settings['force_code'])
        self.assertEqual(self.monitor.state['last_request']['id'],latest['id'])
        self.assertTrue(self.monitor.state['last_request']['retry'])
        self.assertFalse((self.directory/'monitor.request.json').exists())
        self.assertIsNone(monitor.take_update_request(self.directory))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.backend = FakeBackend()
        self.notices = []
        self.now = 1000
        self.monitor = self.new_monitor()

    def new_monitor(self):
        return monitor.Monitor(self.directory, self.backend,
                               lambda kind, message: self.notices.append((kind, message)),
                               lambda: self.now)

    def poll(self, seconds=0, retry=False):
        self.now += seconds
        return self.monitor.run_once(retry=retry)

    def prepare(self):
        self.poll()
        return self.poll(10)

    def test_requires_separate_stable_observations(self):
        self.assertEqual(self.poll()["phase"], "stabilizing")
        self.assertEqual(self.poll(9)["phase"], "stabilizing")
        self.assertEqual(self.backend.builds, [])
        self.assertEqual(self.poll(1)["phase"], "installed")
        self.assertEqual(self.backend.installs, ["v1"])
        self.assertEqual([kind for kind, _ in self.notices], ["installed"])

    def test_reopen_during_install_keeps_prepared_update_without_false_failure(self):
        from patch import AppRunningError
        self.backend.fail_install=AppRunningError('Reopened')
        result=self.prepare()
        self.assertEqual(result['phase'],'waiting_for_exit')
        self.assertIn('pending',result)
        self.assertEqual(self.notices,[])
        self.backend.fail_install=None
        self.assertEqual(self.poll()['phase'],'installed')

    def test_new_fingerprint_restarts_stability_window(self):
        self.poll()
        self.backend.current = "v2"
        self.poll(20)
        self.assertEqual(self.backend.builds, [])
        self.poll(10)
        self.assertEqual(self.backend.builds, ["v2"])

    def test_unchanged_installed_app_is_quiet(self):
        self.prepare()
        before = (self.directory / "events.jsonl").read_text()
        for _ in range(3):
            self.poll(300)
        self.assertEqual(self.backend.builds, ["v1"])
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(before, (self.directory / "events.jsonl").read_text())

    def test_adopts_existing_install_without_rebuilding_or_notification(self):
        self.backend.adoption = {"fingerprint": "v1", "build": {"directory": "existing"}}
        self.assertEqual(self.poll()["phase"], "installed")
        self.assertEqual(self.backend.builds, [])
        self.assertEqual(self.notices, [])

    def test_build_waits_for_normal_exit_and_survives_restart(self):
        self.backend.running = True
        self.assertEqual(self.prepare()["phase"], "waiting_for_exit")
        self.assertEqual(self.backend.installs, [])
        self.assertEqual(self.notices, [])
        self.monitor = self.new_monitor()
        self.poll(300)
        self.assertEqual(self.backend.builds, ["v1"])
        self.backend.running = False
        self.assertEqual(self.poll(300)["phase"], "installed")
        self.assertEqual(self.backend.installs, ["v1"])

    def test_source_fingerprint_can_be_installed_identity_for_managed_windows_copy(self):
        self.backend.installed_fingerprint = "v1"
        self.prepare()
        self.poll(300)
        self.assertEqual(self.backend.builds, ["v1"])
        self.assertEqual(self.backend.installs, ["v1"])

    def test_incompatibility_notifies_once_per_fingerprint_including_return_to_old(self):
        self.backend.compatible = False
        self.assertEqual(self.prepare()["phase"], "incompatible")
        for _ in range(3):
            self.poll(300)
        self.assertEqual(self.backend.inspect_count, 1)
        self.assertEqual(len(self.notices), 1)
        self.backend.current = "v2"
        self.poll(300)
        self.poll(300)
        self.assertEqual(len(self.notices), 2)
        self.backend.current = "v1"
        self.poll(300)
        self.assertEqual(len(self.notices), 2)
        self.assertEqual(self.backend.builds, [])

    def test_build_failure_is_durable_and_explicit_retry_is_possible(self):
        self.backend.fail_build = OSError("disk full")
        self.assertEqual(self.prepare()["phase"], "failed")
        self.monitor = self.new_monitor()
        self.poll(300)
        self.assertEqual(self.backend.builds, ["v1"])
        self.assertEqual(len(self.notices), 1)
        self.backend.fail_build = None
        self.assertEqual(self.poll(300, retry=True)["phase"], "installed")
        self.assertEqual(self.backend.builds, ["v1", "v1"])

    def test_install_failure_preserves_build_and_does_not_repeat(self):
        self.backend.fail_install = OSError("transaction rolled back")
        self.assertEqual(self.prepare()["phase"], "failed")
        self.poll(300)
        self.assertEqual(self.backend.installs, ["v1"])
        self.assertTrue(Path(self.monitor.state["build_attempt"]).is_dir())
        self.assertEqual(len(self.notices), 1)

    def test_update_during_build_never_installs_stale_build(self):
        self.backend.change_during_build = "v2"
        self.assertEqual(self.prepare()["phase"], "stabilizing")
        self.assertEqual(self.backend.installs, [])
        self.assertEqual(self.monitor.state["candidate"]["fingerprint"], "v2")
        self.assertEqual(self.notices, [])

    def test_update_while_waiting_discards_pending_build(self):
        self.backend.running = True
        self.prepare()
        self.backend.current = "v2"
        self.assertEqual(self.poll(300)["phase"], "stabilizing")
        self.assertNotIn("pending", self.monitor.state)
        self.assertEqual(self.backend.installs, [])

    def test_update_immediately_before_install_is_detected(self):
        self.backend.change_during_running_check = "v2"
        self.assertEqual(self.prepare()["phase"], "stabilizing")
        self.assertEqual(self.backend.installs, [])

    def test_interrupted_build_is_actionable_and_not_repeated(self):
        self.backend.fail_build = KeyboardInterrupt()
        self.poll()
        with self.assertRaises(KeyboardInterrupt):
            self.poll(10)
        self.monitor = self.new_monitor()
        self.assertEqual(self.poll(300)["phase"], "failed")
        self.assertEqual(self.backend.builds, ["v1"])
        self.assertIn("interrupted", self.notices[0][1])

    def test_recovers_committed_install_after_monitor_process_interruption(self):
        self.backend.running = True
        self.prepare()
        self.monitor.state["phase"] = "installing"
        self.monitor.save()
        self.backend.current = "patched-v1"
        self.backend.adoption = {"fingerprint": "patched-v1"}
        self.monitor = self.new_monitor()
        self.assertEqual(self.poll(300)["phase"], "installed")
        self.assertEqual(self.backend.installs, [])

    def test_transient_missing_target_read_retries_and_notice_is_deduplicated(self):
        with mock.object(self.backend, "snapshot", side_effect=FileNotFoundError("Codex unavailable")):
            self.assertEqual(self.poll()["phase"], "unavailable")
            self.poll(300)
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(self.poll(300)["phase"], "stabilizing")

    def test_native_notification_failure_does_not_undo_success_or_repeat(self):
        self.monitor.notifier = Mock(side_effect=OSError("desktop unavailable"))
        self.prepare()
        self.poll(300)
        self.monitor.notifier.assert_called_once()
        self.assertEqual(self.monitor.state["phase"], "installed")
        self.assertIn("desktop unavailable", self.monitor.state["notification_error"])

    def test_concurrent_invocation_does_no_work(self):
        with monitor.state_lock(self.directory):
            self.assertEqual(self.poll(), {"phase": "busy"})
        self.assertEqual(self.backend.builds, [])

    def test_sensitive_error_text_is_redacted_in_status_and_log(self):
        self.backend.fail_build = RuntimeError("api_key=secret123 Bearer top-secret sk-abcdefghijk")
        self.prepare()
        combined = (self.directory / "state.json").read_text() + (self.directory / "events.jsonl").read_text()
        for secret in ("secret123", "top-secret", "sk-abcdefghijk"):
            self.assertNotIn(secret, combined)
        self.assertIn("[redacted]", combined)

    def test_successful_install_moves_managed_baseline_in_state_and_settings(self):
        monitor.atomic_json(self.directory / "settings.json", {"baseline": "old", "app": "Codex.app"})
        original = self.backend.install
        def installed(build, snapshot):
            return {**original(build, snapshot), "baseline": build["directory"]}
        with mock.object(self.backend, "install", side_effect=installed):
            self.prepare()
        settings = monitor.load_json(self.directory / "settings.json")
        self.assertEqual(settings["baseline"], self.monitor.state["installed"]["baseline"])
        self.assertEqual(settings["app"], "Codex.app")

    def test_failed_install_does_not_move_managed_baseline(self):
        monitor.atomic_json(self.directory / "settings.json", {"baseline": "old"})
        self.backend.fail_install = OSError("rolled back")
        self.prepare()
        self.assertEqual(monitor.load_json(self.directory / "settings.json")["baseline"], "old")

    def test_adoption_repairs_baseline_after_interrupted_state_write(self):
        monitor.atomic_json(self.directory / "settings.json", {"baseline": "old"})
        self.backend.adoption = {"fingerprint": "v1", "baseline": "adopted-build"}
        self.poll()
        self.assertEqual(monitor.load_json(self.directory / "settings.json")["baseline"], "adopted-build")
        self.assertEqual(self.notices, [])

    def test_cli_supplies_pending_install_for_committed_transaction_recovery(self):
        monitor.atomic_json(self.directory / "settings.json", {"baseline": "old-baseline"})
        monitor.atomic_json(self.directory / "state.json", {"format": 1, "phase": "installing",
                            "installed": {"baseline": "old-baseline"},
                            "pending": {"build": {"directory": "new-committed-build"}}})
        instance = Mock()
        instance.run_once.return_value = {"phase": "installed"}
        with mock.object(monitor, "CoreBackend") as backend, \
             mock.object(monitor, "Monitor", return_value=instance), mock("builtins.print"):
            result = monitor.main(["--state-dir", str(self.directory), "run-once"])
        self.assertEqual(result, 0)
        self.assertEqual(backend.call_args.args[0]["recovery_build"], "new-committed-build")
        self.assertEqual(backend.call_args.args[0]["baseline"], "old-baseline")
        self.assertNotIn("recovery_build", monitor.load_json(self.directory / "settings.json"))

    def test_unknown_state_format_is_rejected(self):
        monitor.atomic_json(self.directory / "state.json", {"format": 999})
        with self.assertRaises(ValueError):
            self.new_monitor()


class SchedulerTests(unittest.TestCase):
    def test_old_macos_background_jobs_restore_only_their_own_policy(self):
        with mock.object(monitor.sys, "platform", "darwin"), mock.object(monitor.os, "getpid", return_value=1234), mock.object(monitor.subprocess, "run") as run:
            monitor.prepare_background_work()
        run.assert_called_once_with(["/usr/sbin/taskpolicy", "-B", "-p", "1234"], check=True, capture_output=True, timeout=20)

    def test_windows_jobs_do_not_run_macos_policy_tools(self):
        with mock.object(monitor.sys, "platform", "win32"), mock.object(monitor.subprocess, "run") as run:
            monitor.prepare_background_work()
        run.assert_not_called()

    def test_launchd_runs_on_login_and_interval_and_uses_private_runtime(self):
        directory = Path("/tmp/Codex Patch Directory").resolve()
        value = monitor.launchd_plist(directory, 300, "/usr/local/bin/python3")
        self.assertEqual(value["StartInterval"], 300)
        self.assertTrue(value["RunAtLoad"])
        # Background I/O throttling can make a valid app signature time out.
        self.assertEqual(value["ProcessType"], "Standard")
        self.assertEqual(value["ProgramArguments"], [str(Path("/usr/local/bin/python3")), str(directory / "runtime/monitor.py"),
                                                    "--state-dir", str(directory), "run-once", "--quiet"])
        self.assertEqual(plistlib.loads(plistlib.dumps(value)), value)
        self.assertNotIn("KeepAlive", value)

    def test_windows_task_is_interactive_unprivileged_and_has_two_triggers(self):
        xml = monitor.windows_task_xml(Path("/tmp/Patch & Monitor"), 300, "C:\\Program Files\\Python\\pythonw.exe",
                                      dt.datetime(2026, 10, 18, 12, 0, 0), identity_sid="S-1-5-21-100-200-300-1001")
        root = ET.fromstring(xml)
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        self.assertIsNotNone(root.find("t:Triggers/t:LogonTrigger", ns))
        self.assertEqual(root.findtext("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", namespaces=ns), "PT300S")
        self.assertEqual(root.findtext("t:Principals/t:Principal/t:LogonType", namespaces=ns), "InteractiveToken")
        self.assertEqual(root.findtext("t:Principals/t:Principal/t:RunLevel", namespaces=ns), "LeastPrivilege")
        self.assertEqual(root.findtext("t:Settings/t:MultipleInstancesPolicy", namespaces=ns), "IgnoreNew")
        args = root.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns)
        self.assertIn('"' + str(Path('/tmp/Patch & Monitor').resolve() / 'runtime/monitor.py') + '"', args)
        self.assertNotIn("ExecutionPolicy", xml)

    def test_task_cwd_migration_preserves_disabled_task_and_triggers(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp).resolve()
            xml = monitor.windows_task_xml(directory, 300, identity_sid="S-1-5-21-100-200-300-1001")
            root = ET.fromstring(xml)
            self.assertEqual(root.findtext("{*}Actions/{*}Exec/{*}WorkingDirectory"), str(directory))
            root.find("{*}Actions/{*}Exec/{*}WorkingDirectory").text = str(directory / "runtime")
            ET.SubElement(root.find("{*}Settings"), "{http://schemas.microsoft.com/windows/2004/02/mit/task}Enabled").text = "false"
            before = ET.tostring(root, encoding="utf-16")
            result = Mock(returncode=0, stdout=before)
            with mock.object(monitor.sys, 'platform', 'win32'), mock.object(monitor.subprocess, 'run', return_value=result) as run:
                self.assertTrue(monitor.migrate_windows_working_directory(directory))
                after = ET.fromstring((directory / 'task-cwd-migration.xml').read_bytes())
                self.assertEqual(after.findtext('{*}Settings/{*}Enabled'), 'false')
                self.assertEqual(after.findtext('{*}Actions/{*}Exec/{*}WorkingDirectory'), str(directory))
                after.find('{*}Actions/{*}Exec/{*}WorkingDirectory').text = str(directory / 'runtime')
                self.assertEqual(ET.tostring(after), ET.tostring(root))
                self.assertEqual(next(directory.glob('task-before-cwd-*.xml')).read_bytes(), before)
                self.assertEqual(run.call_count, 2)
                result.stdout = (directory / 'task-cwd-migration.xml').read_bytes()
                self.assertFalse(monitor.migrate_windows_working_directory(directory))
                result.returncode = 1
                self.assertFalse(monitor.migrate_windows_working_directory(directory))
                self.assertEqual(run.call_count, 4)

    def test_windows_identity_uses_current_sid_even_with_workgroup_environment(self):
        expected = "S-1-5-21-100-200-300-1001"
        with mock.dict(monitor.os.environ, {"USERDOMAIN": "WorkGroup", "USERNAME": "pc"}), \
             mock.object(monitor.subprocess, "check_output", return_value=expected + "\n") as runner:
            xml = monitor.windows_task_xml(Path("/tmp/task"), 300)
        root = ET.fromstring(xml)
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        self.assertEqual(root.findtext("t:Triggers/t:LogonTrigger/t:UserId", namespaces=ns), expected)
        self.assertEqual(root.findtext("t:Principals/t:Principal/t:UserId", namespaces=ns), expected)
        self.assertNotIn("WorkGroup", xml)
        self.assertIn("WindowsIdentity]::GetCurrent().User.Value", runner.call_args.args[0][-1])

    def test_invalid_windows_identity_never_registers_a_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.object(monitor.sys, "platform", "win32"), \
                 mock.object(monitor.subprocess, "check_output", return_value="WorkGroup\\pc"), \
                 mock.object(monitor.subprocess, "run") as runner:
                with self.assertRaises(ValueError):
                    monitor.Scheduler(Path(temporary)).enable(300)
            runner.assert_not_called()
            self.assertFalse((Path(temporary) / "task.xml").exists())

    def test_windows_task_definition_has_matching_utf16_declaration(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with mock.object(monitor.sys, "platform", "win32"), mock.object(monitor.subprocess, "run"), \
                 mock.object(monitor, "windows_identity_sid", return_value="S-1-5-21-100-200-300-1001"):
                monitor.Scheduler(directory).enable(300)
            data = (directory / "task.xml").read_bytes()
            self.assertTrue(data.startswith((b"\xff\xfe", b"\xfe\xff")))
            self.assertIn("encoding='utf-16'", data.decode("utf-16"))
            ET.fromstring(data)

    def test_disabled_registered_windows_task_is_reported_and_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            value = Mock(returncode=0, stdout=b'<Task><Settings><Enabled>false</Enabled></Settings></Task>')
            with mock.object(monitor.sys, "platform", "win32"), mock.object(monitor.subprocess, "run", return_value=value) as runner:
                scheduler = monitor.Scheduler(directory)
                self.assertEqual(scheduler.status()["enabled"], False)
                self.assertEqual(scheduler.status()["registered"], True)
                scheduler.disable()
                self.assertIn("/Delete", runner.call_args.args[0])

    def test_windows_status_decodes_schtasks_bytes_before_declared_encoding(self):
        xml = '<?xml version="1.0" encoding="UTF-16"?><Task><Settings><Enabled>false</Enabled></Settings></Task>'
        for data in (xml.encode("utf-8"), xml.encode("utf-16"), xml.encode("utf-16-le"), xml.encode("utf-16-be")):
            value = Mock(returncode=0, stdout=data)
            with mock.object(monitor.sys, "platform", "win32"), \
                 mock.object(monitor.subprocess, "run", return_value=value):
                result = monitor.Scheduler(Path("/tmp/task")).status()
            self.assertTrue(result["registered"])
            self.assertFalse(result["enabled"])

    def test_staging_runtime_does_not_copy_repository_or_secrets(self):
        import patch
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with mock.object(patch, "SERVICE_RUNTIME_FILES", ("monitor.py", "patch.py", "route.js"), create=True):
                monitor.stage_runtime(directory)
            self.assertEqual({str(path.relative_to(directory / "runtime")) for path in (directory / "runtime").rglob("*")},
                             {"monitor.py", "patch.py", "route.js"})
            self.assertFalse((directory / "settings.json").exists())

    def test_invalid_runtime_entry_is_rejected(self):
        import patch
        with tempfile.TemporaryDirectory() as temporary:
            with mock.object(patch, "SERVICE_RUNTIME_FILES", ("../secret.txt",), create=True):
                with self.assertRaises(ValueError):
                    monitor.stage_runtime(Path(temporary))
            self.assertFalse((Path(temporary) / "runtime").exists())

    def enable_args(self, directory):
        return argparse.Namespace(state_dir=directory, interval=300, app=Path("/new/Codex.app"),
                                  codex_home=Path("/tmp/codex-home"), baseline=Path("/new/build"),
                                  keychain_account="user", keychain_service="service")

    def test_failed_scheduler_registration_restores_settings_runtime_and_state(self):
        import patch
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            settings = {"app": "/old/Codex.app", "baseline": "/old/build"}
            state = {"format": 1, "installed": {"fingerprint": "old"}}
            monitor.atomic_json(directory / "settings.json", settings)
            monitor.atomic_json(directory / "state.json", state)
            (directory / "runtime").mkdir()
            (directory / "runtime/old-marker").write_text("old")
            with mock.object(monitor.sys, "platform", "darwin"), \
                 mock.object(patch, "MonitorBackend", return_value=FakeBackend()), \
                 mock.object(monitor, "Scheduler") as scheduler, \
                 mock.object(monitor.shutil, "which", return_value="/usr/local/bin/node"), \
                 mock.object(patch, "SERVICE_RUNTIME_FILES", ("monitor.py", "patch.py", "route.js"), create=True):
                scheduler.return_value.enable.side_effect = OSError("registration failed")
                with self.assertRaises(OSError):
                    monitor.enable(self.enable_args(directory))
            self.assertEqual(monitor.load_json(directory / "settings.json"), settings)
            self.assertEqual(monitor.load_json(directory / "state.json"), state)
            self.assertEqual((directory / "runtime/old-marker").read_text(), "old")
            self.assertTrue(list(directory.glob("runtime-failed-*")))

    def test_explicit_identity_change_resets_adoption_and_old_baseline_override(self):
        import patch
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            monitor.atomic_json(directory / "settings.json", {"app": "/old/Codex.app", "baseline": "/old/build"})
            monitor.atomic_json(directory / "state.json", {"format": 1, "installed": {"baseline": "/old/build"}})
            with mock.object(monitor.sys, "platform", "darwin"), \
                 mock.object(patch, "MonitorBackend", return_value=FakeBackend()), \
                 mock.object(monitor, "Scheduler") as scheduler, \
                 mock.object(monitor.shutil, "which", return_value="/usr/local/bin/node"), \
                 mock.object(patch, "SERVICE_RUNTIME_FILES", ("monitor.py", "patch.py", "route.js"), create=True):
                scheduler.return_value.status.return_value = {"enabled": True}
                monitor.enable(self.enable_args(directory))
            self.assertNotIn("installed", monitor.load_json(directory / "state.json"))
            self.assertEqual(monitor.load_json(directory / "settings.json")["baseline"], str(Path("/new/build").resolve()))
            self.assertTrue(list(directory.glob("state-backup-*")))

    def test_launchd_receives_node_directory_from_saved_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            monitor.atomic_json(directory / "settings.json", {"node_path": "/custom/node/bin/node", "python_path": "/custom/python/bin/python3"})
            value = monitor.launchd_plist(directory, 300)
            prefix = str(Path("/custom/node/bin")) + ":" + str(Path("/custom/python/bin")) + ":"
            self.assertTrue(value["EnvironmentVariables"]["PATH"].startswith(prefix))
            self.assertEqual(value["ProgramArguments"][0], str(Path("/custom/python/bin/python3")))

    def test_short_interval_is_rejected_before_side_effects(self):
        import argparse
        with tempfile.TemporaryDirectory() as temporary:
            args = argparse.Namespace(state_dir=Path(temporary), interval=5)
            with self.assertRaises(ValueError):
                monitor.enable(args)
            self.assertFalse((Path(temporary) / "settings.json").exists())


if __name__ == "__main__":
    unittest.main()
