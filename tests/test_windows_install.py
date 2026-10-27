import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

import windows_install as installer


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "official"
        self.source.mkdir()
        (self.source / "source.txt").write_text("official")
        self.build = self.base / "build"
        self.build.mkdir()
        self.stage = self.build / "patched.app"
        self.stage.mkdir()
        (self.stage / "ChatGPT.exe").write_bytes(b"new-app")
        self.target = self.base / "managed/app"
        self.target.mkdir(parents=True)
        (self.target / "ChatGPT.exe").write_bytes(b"previous-app")
        self.config = self.base / "home/config.toml"
        self.config.parent.mkdir()
        self.config.write_text("original\n")
        (self.build / "config-before.toml").write_text("original\n")
        self.manifest = {"status": "prepared", "target": str(self.source), "deployment_target": str(self.target),
                         "codex_home": str(self.config.parent), "patched_asar": "fake"}
        installer._json(self.build / "manifest.json", self.manifest)
        self.original_manifest = (self.build / "manifest.json").read_bytes()
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {"APPDATA": str(self.base / "roaming")}).start()
        self.stopped = mock.patch.object(installer.win, "require_stopped").start()
        mock.patch.object(installer.win, "verify_integrity", return_value={}).start()
        self.clone = mock.patch.object(installer.win, "clone_app", side_effect=shutil.copytree).start()
        self.shortcut_creator = mock.patch.object(installer, "_make_shortcut", side_effect=lambda path, target: path.write_bytes(b"new-link")).start()
        self.shortcut = installer._shortcut_path()
        self.shortcut.parent.mkdir(parents=True)
        self.shortcut.write_bytes(b"previous-link")

    def commit(self):
        self.assertEqual((self.target / "ChatGPT.exe").read_bytes(), b"new-app")
        self.assertEqual(self.config.read_text(), "updated\n")
        self.manifest.update(status="installed", config_existed=True,
                             installed_config_hash=hashlib.sha256(b"updated\n").hexdigest())
        installer._json(self.build / "manifest.json", self.manifest)

    def install(self, commit=None):
        installer.install(self.manifest, self.build, self.config, "original\n", "updated\n", commit or self.commit)

    def assert_original(self):
        self.assertEqual((self.target / "ChatGPT.exe").read_bytes(), b"previous-app")
        self.assertEqual(self.config.read_text(), "original\n")
        self.assertEqual(self.shortcut.read_bytes(), b"previous-link")
        self.assertEqual((self.build / "manifest.json").read_bytes(), self.original_manifest)
        self.assertEqual((self.source / "source.txt").read_text(), "official")

    def test_installs_and_restores_previous_copy_shortcut_and_config(self):
        self.install()
        self.assertEqual(self.manifest["status"], "installed")
        self.assertEqual(self.shortcut.read_bytes(), b"new-link")
        self.assertEqual((self.build / "windows-previous.app/ChatGPT.exe").read_bytes(), b"previous-app")
        self.assertEqual(self.stopped.call_count, 4)
        installer.restore(self.manifest, self.build)
        self.assertEqual(self.manifest["status"], "restored")
        self.assertEqual((self.target / "ChatGPT.exe").read_bytes(), b"previous-app")
        self.assertEqual(self.config.read_text(), "original\n")
        self.assertEqual(self.shortcut.read_bytes(), b"previous-link")
        self.assertEqual((self.source / "source.txt").read_text(), "official")

    def test_commit_failure_restores_all_changed_state(self):
        def fail():
            self.commit()
            raise RuntimeError("manifest verification failed")
        with self.assertRaisesRegex(RuntimeError, "verification failed"):
            self.install(fail)
        self.assert_original()
        journal = json.loads((self.build / "windows-install-journal.json").read_text())
        self.assertEqual(journal["phase"], "rolled-back")
        self.assertEqual(self.manifest["status"], "prepared")

    def test_rollback_failure_is_journaled_and_other_state_is_restored(self):
        original_replace = os.replace
        def fail_previous_restore(source, destination):
            if Path(source).resolve() == (self.build / "windows-previous.app").resolve() and Path(destination).resolve() == self.target.resolve():
                raise OSError("previous app locked")
            return original_replace(source, destination)
        def fail_commit():
            self.commit()
            raise RuntimeError("commit failed")
        with mock.patch.object(installer.os, "replace", side_effect=fail_previous_restore):
            with self.assertRaisesRegex(installer.WindowsInstallError, "recovery needs attention"):
                self.install(fail_commit)
        self.assertEqual(self.config.read_text(), "original\n")
        self.assertEqual(self.shortcut.read_bytes(), b"previous-link")
        self.assertTrue((self.build / "windows-previous.app").is_dir())
        journal = json.loads((self.build / "windows-install-journal.json").read_text())
        self.assertEqual(journal["phase"], "rollback-failed")
        self.assertIn("previous app locked", journal["rollback_errors"][0])

    def test_shortcut_failure_before_mutation_leaves_original(self):
        self.shortcut_creator.side_effect = RuntimeError("COM failed")
        with self.assertRaisesRegex(RuntimeError, "COM failed"):
            self.install()
        self.assert_original()

    def test_copy_failure_leaves_original(self):
        self.clone.side_effect = OSError("disk full")
        with self.assertRaisesRegex(OSError, "disk full"):
            self.install()
        self.assert_original()

    def test_concurrent_config_edit_is_preserved(self):
        def copy_and_edit(source, destination):
            shutil.copytree(source, destination)
            self.config.write_text("external-edit\n")
        self.clone.side_effect = copy_and_edit
        with self.assertRaisesRegex(installer.WindowsInstallError, "while copying"):
            self.install()
        self.assertEqual(self.config.read_text(), "external-edit\n")
        self.assertEqual((self.target / "ChatGPT.exe").read_bytes(), b"previous-app")

    def test_initial_install_and_restore_without_previous_managed_copy(self):
        shutil.rmtree(self.target)
        self.shortcut.unlink()
        self.install()
        self.assertIsNone(self.manifest["windows_install"]["previous_app"])
        installer.restore(self.manifest, self.build)
        self.assertFalse(self.target.exists())
        self.assertFalse(self.shortcut.exists())
        self.assertEqual(self.config.read_text(), "original\n")

    def test_failed_build_can_be_retried_without_overwriting_shortcut_backup(self):
        self.shortcut_creator.side_effect = RuntimeError("COM failed")
        with self.assertRaises(RuntimeError):
            self.install()
        self.shortcut_creator.side_effect = lambda path, target: path.write_bytes(b"new-link")
        self.install()
        self.assertEqual((self.build / "windows-shortcut-before.lnk").read_bytes(), b"previous-link")

    def test_restore_refuses_modified_shortcut_backup(self):
        self.install()
        (self.build / "windows-shortcut-before.lnk").write_bytes(b"changed-backup")
        with self.assertRaisesRegex(installer.WindowsInstallError, "backup changed"):
            installer.restore(self.manifest, self.build)
        self.assertEqual((self.target / "ChatGPT.exe").read_bytes(), b"new-app")

    def test_restore_refuses_later_config_edits(self):
        self.install()
        self.config.write_text("user-edit\n")
        with self.assertRaisesRegex(installer.WindowsInstallError, "changed after"):
            installer.restore(self.manifest, self.build)
        self.assertEqual(self.config.read_text(), "user-edit\n")
        self.assertEqual((self.target / "ChatGPT.exe").read_bytes(), b"new-app")

    def test_source_overlap_refused(self):
        self.manifest["deployment_target"] = str(self.source)
        with self.assertRaisesRegex(installer.WindowsInstallError, "overlaps"):
            self.install()
        self.assertEqual(self.clone.call_count, 0)

    def test_restore_failure_rolls_back_installed_state(self):
        self.install()
        original_json = installer._json
        def fail_manifest(path, data):
            if path.name == "manifest.json" and data.get("status") == "restored":
                raise OSError("manifest write failed")
            return original_json(path, data)
        with mock.patch.object(installer, "_json", side_effect=fail_manifest):
            with self.assertRaisesRegex(OSError, "manifest write failed"):
                installer.restore(self.manifest, self.build)
        self.assertEqual(self.config.read_text(), "updated\n")
        self.assertEqual(self.shortcut.read_bytes(), b"new-link")
        self.assertEqual((self.target / "ChatGPT.exe").read_bytes(), b"new-app")
        self.assertEqual(self.manifest["status"], "installed")


if __name__ == "__main__":
    unittest.main()
