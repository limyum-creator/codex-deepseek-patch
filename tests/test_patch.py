import copy
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch as mock

import patch


def write_asar(path, entries, data):
    tree = {"files": entries}
    raw = json.dumps(tree, separators=(",", ":")).encode()
    payload = struct.pack("<I", len(raw)) + raw + b"\0" * (-(4 + len(raw)) % 4)
    header = struct.pack("<I", len(payload)) + payload
    path.write_bytes(struct.pack("<II", 4, len(header)) + header + data)


def file_entry(data, offset=0):
    return {"offset": str(offset), "size": len(data), "integrity": {
        "algorithm": "SHA256", "hash": patch.sha(data), "blockSize": 3,
        "blocks": [patch.sha(data[i:i + 3]) for i in range(0, len(data), 3)]}}


class ArchiveTests(unittest.TestCase):
    def test_append_preserves_shared_ranges_links_unpacked_and_unknown_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "old.asar", Path(tmp) / "new.asar"
            shared = b"abcdef"
            members = {"target.js": file_entry(shared), "alias.js": file_entry(shared),
                       "native.node": {"size": 42, "unpacked": True, "custom": {"x": 1}},
                       "link.js": {"link": "alias.js"}}
            members["target.js"]["extra"] = "retain me"
            write_asar(a, members, shared)
            patch.repack(a, b, {"target.js": b"edited"})
            old, ob, _ = patch.archive_header(a)
            new, nb, _ = patch.archive_header(b)
            for name in ("alias.js", "native.node", "link.js"):
                self.assertEqual(old["files"][name], new["files"][name])
            self.assertEqual(new["files"]["target.js"]["extra"], "retain me")
            self.assertEqual(b.read_bytes()[nb:nb + len(shared)], a.read_bytes()[ob:])
            self.assertEqual(patch.read_member(b, new["files"]["target.js"], nb), b"edited")
            patch.check_member(b"edited", new["files"]["target.js"])

    def test_corrupt_input_refused_before_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a", Path(tmp) / "b"
            write_asar(a, {"target": file_entry(b"good")}, b"evil")
            with self.assertRaises(patch.PatchError):
                patch.repack(a, b, {"target": b"new"})
            self.assertFalse(b.exists())

    def test_truncated_and_oversize_headers_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "file"
            for data in (b"short", struct.pack("<4I", 4, 2**30, 2**30 - 4, 1)):
                p.write_bytes(data)
                with self.assertRaises(patch.PatchError):
                    patch.archive_header(p)

    def test_destination_cannot_replace_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a"
            write_asar(p, {"target": file_entry(b"old")}, b"old")
            old = p.read_bytes()
            with self.assertRaises(patch.PatchError):
                patch.repack(p, p, {"target": b"new"})
            self.assertEqual(p.read_bytes(), old)


class SourceTests(unittest.TestCase):
    def test_fresh_parser_uses_each_package_edition_instead_of_a_copied_constant(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for name in ('patch.py','windows_support.py'):
                (root/name).write_bytes((patch.ROOT/name).read_bytes())
            for edition in ('ds','proxy'):
                with self.subTest(edition=edition):
                    (root/'edition.json').write_text(json.dumps({'edition':edition,'version':'fixture-version'}))
                    raw=subprocess.check_output([sys.executable,'-c',
                        'import patch; print(patch.EDITION, patch.VERSION)'],cwd=root,text=True,**patch.background_options())
                    self.assertEqual(raw.strip(),edition+' fixture-version')

    def fixture(self):
        return "\n".join(old for old, new in patch.EDITS)

    def test_exact_once_and_already_patched(self):
        result = patch.patch_source(self.fixture())
        self.assertEqual(result.count(patch.MARKER), 1)
        for old, new in patch.EDITS:
            self.assertIn(new, result)
        with self.assertRaises(patch.PatchError):
            patch.patch_source(result)

    def test_missing_and_duplicate_anchors_refused(self):
        for source in ("unrelated", self.fixture() + "\n" + patch.EDITS[0][0]):
            with self.assertRaises(patch.PatchError):
                patch.patch_source(source)


class IntegrityTests(unittest.TestCase):
    def test_updates_digest_without_disabling_validation(self):
        old = {"Resources/app.asar": {"algorithm": "SHA256", "hash": "a" * 64}}
        new = {"Resources/app.asar": {"algorithm": "SHA256", "hash": "b" * 64}}
        blob = b"prefix" + patch.SENTINEL + b"\x01\x01" + patch.dictionary_digest(old) + b"suffix"
        result = patch.patch_framework_bytes(blob, old, new)
        self.assertEqual(result[:40], blob[:40])
        self.assertTrue(result.endswith(b"suffix"))
        self.assertIn(patch.dictionary_digest(new), result)
        with self.assertRaises(patch.PatchError):
            patch.patch_framework_bytes(blob, new, old)
        with self.assertRaises(patch.PatchError):
            patch.patch_framework_bytes(blob + blob, old, new)

    def test_dictionary_order_is_stable(self):
        values = {"b": {"algorithm": "SHA256", "hash": "2"}, "a": {"algorithm": "SHA256", "hash": "1"}}
        self.assertEqual(patch.dictionary_digest(values), patch.dictionary_digest(dict(reversed(list(values.items())))))


class ConfigTests(unittest.TestCase):
    def test_windowless_installer_uses_console_credential_helper(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "pythonw.exe"
            console = Path(temp) / "python.exe"
            console.touch()
            with mock.object(patch.sys, "platform", "win32"), mock.object(patch.sys, "executable", str(executable)):
                parsed = patch.tomllib.loads(patch.provider_config("user", "service"))
            self.assertEqual(parsed["model_providers"][patch.PROVIDER]["auth"]["command"], str(console))

    def test_preserves_values_comments_and_existing_providers(self):
        source = '# keep this\nmodel="gpt-example"\n[model_providers.other]\nname="Other"\n[desktop]\nsize=16\n'
        result = patch.prepared_config(source, Path("/tmp/catalog.json"), 'an "account"', "service")
        self.assertIn(source, result)
        parsed = patch.tomllib.loads(result)
        self.assertEqual(parsed["model"], "gpt-example")
        self.assertEqual(parsed["model_providers"]["other"], {"name": "Other"})
        auth = parsed["model_providers"][patch.PROVIDER]["auth"]
        if patch.sys.platform == "win32":
            executable = Path(patch.sys.executable)
            expected = executable.with_name("python.exe") if executable.name.lower() == "pythonw.exe" else executable
            self.assertEqual(auth["command"], str(expected))
            self.assertEqual(auth["args"], [str(patch.ROOT / "windows_support.py"), "credential-get", "--target", "service"])
        else:
            self.assertEqual(auth["args"][2], 'an "account"')
        self.assertNotIn("api_key", result)

    def test_conflicting_config_rejected(self):
        for source in ('model_catalog_json="x"', '[model_providers.deepseek-desktop]\nname="DS"', 'model_provider="other"'):
            with self.assertRaises(patch.PatchError):
                patch.prepared_config(source, Path("/tmp/cat"), "user", "service")

    def test_catalog_is_flash_only_and_does_not_mutate_input(self):
        original = {"models": [{"slug": "gpt-6.1-sol", "model_messages": {"instructions_template": "local fixture"}},
                                {"slug": "gpt-other", "extra": [1, 2]}, {"slug": "deepseek-v4-pro"}]}
        snapshot = copy.deepcopy(original)
        result = patch.make_catalog(original)
        self.assertEqual(original, snapshot)
        self.assertEqual(result["models"][:2], original["models"][:2])
        self.assertEqual(result["models"][-1]["slug"], "deepseek-flash")
        self.assertEqual(result["models"][-1]["model_messages"], original["models"][0]["model_messages"])
        self.assertFalse(result["models"][-1]["use_responses_lite"])
        self.assertEqual(result["models"][-1]["service_tiers"], [])


class TransactionTests(unittest.TestCase):
    @staticmethod
    def exchange(a, b):
        temp = a.parent / "swap.tmp"
        os.rename(a, temp)
        os.rename(b, a)
        os.rename(temp, b)

    def scenario(self, exists=True, fail=False):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); app = root / "live"; staged = root / "stage"; config = root / "config.toml"
            app.mkdir(); staged.mkdir()
            (app / "id").write_text("original"); (staged / "id").write_text("patched")
            before = "original config" if exists else ""
            if exists:
                config.write_text(before)
            def commit():
                self.assertEqual((app / "id").read_text(), "patched")
                self.assertEqual(config.read_text(), "new config")
                if fail:
                    raise OSError("simulated verification failure")
            with mock.object(patch, "exchange", self.exchange):
                if fail:
                    with self.assertRaises(OSError):
                        patch.install_transaction(app, staged, config, before, "new config", commit)
                    self.assertEqual((app / "id").read_text(), "original")
                    self.assertEqual(config.exists(), exists)
                    if exists:
                        self.assertEqual(config.read_text(), before)
                else:
                    patch.install_transaction(app, staged, config, before, "new config", commit)
                    self.assertEqual((staged / "id").read_text(), "original")

    def test_install_succeeds(self):
        self.scenario()

    def test_verification_failure_restores_app_and_config(self):
        self.scenario(fail=True)

    def test_failure_restores_absent_config(self):
        self.scenario(exists=False, fail=True)

    def test_concurrent_config_change_prevents_swap(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"; config.write_text("changed")
            with mock.object(patch, "exchange") as exchange:
                with self.assertRaises(patch.PatchError):
                    patch.install_transaction(Path(tmp) / "a", Path(tmp) / "b", config, "old", "new", lambda: None)
                exchange.assert_not_called()



class MacQuitProcessTests(unittest.TestCase):
    def test_only_known_orphan_helpers_are_ignored(self):
        from unittest import mock
        from pathlib import Path
        import patch as subject
        root=Path('/Applications/Codex.app')
        reporter=str(root)+'/Contents/Frameworks/Codex Framework.framework/Versions/1/Helpers/browser_crashpad_handler'
        keyboard=str(root)+'/Contents/Resources/native/bare-modifier-monitor'
        with mock.patch.object(subject.sys,'platform','darwin'), mock.patch.object(subject.subprocess,'check_output',return_value='123 1 '+reporter+'\n124 1 '+keyboard+'\n'):
            subject.require_stopped(root)
        for command in ('2 '+reporter, '2 '+keyboard, '1 '+str(root)+'/Contents/MacOS/Codex',
                        '1 '+str(root)+'/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'):
            with self.subTest(command=command), mock.patch.object(subject.sys,'platform','darwin'), mock.patch.object(subject.subprocess,'check_output',return_value='123 '+command+'\n'):
                with self.assertRaises(subject.AppRunningError):
                    subject.require_stopped(root)

if __name__ == "__main__":
    unittest.main()
