import ast
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path, PureWindowsPath
import struct
import tempfile
import unittest
from unittest import mock

import windows_support as win


class BackgroundProcessTests(unittest.TestCase):
    def test_all_preflight_test_children_explicitly_disable_windows_consoles(self):
        # A pythonw parent does not suppress a console allocated by its child.
        # Public updates execute these tests, so their real subprocess calls
        # follow the same no-window contract as the production worker.
        root = Path(__file__).resolve().parent
        missing = []
        for path in root.glob('test_*.py'):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for call in ast.walk(tree):
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                    continue
                owner = ast.unparse(call.func.value)
                if owner not in ('subprocess', 'win.subprocess') or call.func.attr not in ('run','Popen','check_output','check_call'):
                    continue
                keywords = [ast.unparse(value.value) for value in call.keywords if value.arg is None]
                explicit = any(value.arg == 'creationflags' for value in call.keywords)
                if not explicit and not any('background_options(' in value or 'launch_options(' in value for value in keywords):
                    missing.append(f'{path.name}:{call.lineno}')
        self.assertEqual(missing, [], 'Update preflight can flash child consoles: ' + ', '.join(missing))

    @unittest.skipUnless(os.name == 'nt', 'Real Windows scoped backend exit')
    def test_explicit_exit_closes_owned_headless_backend_and_keeps_other_processes(self):
        import subprocess
        import time
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);app=root/'isolated application directory';outside=root/'other'
            (app/'resources').mkdir(parents=True);outside.mkdir()
            (app/'resources/app.asar').write_bytes(b'isolated fixture')
            source=Path(os.environ['SystemRoot'])/'System32/ping.exe'
            import ctypes
            from ctypes import wintypes
            kernel=ctypes.WinDLL('kernel32');kernel.GetShortPathNameW.argtypes=[wintypes.LPCWSTR,wintypes.LPWSTR,wintypes.DWORD]
            paths=[app/'backend.exe',app/'resources/codex-windows-sandbox-service.exe',outside/'backend.exe']
            children=[]
            try:
                for path in paths:
                    shutil.copy2(source,path)
                    short=ctypes.create_unicode_buffer(32768)
                    length=kernel.GetShortPathNameW(str(path),short,len(short))
                    executable=short.value if 0<length<len(short) else str(path)
                    children.append(subprocess.Popen([executable,'-n','60','127.0.0.1'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**win.background_options()))
                time.sleep(.2)
                self.assertTrue(all(p.poll() is None for p in children))
                win.close_app_handles([app])
                self.assertIsNotNone(children[0].poll())
                self.assertIsNone(children[1].poll());self.assertIsNone(children[2].poll())
            finally:
                for child in children:
                    if child.poll() is None:child.kill()
                    child.wait(timeout=5)

    @unittest.skipUnless(os.name == 'nt', 'Real Windows console and redirected handles')
    def test_console_program_retains_utf8_pipes_without_allocating_a_console(self):
        executable = str(Path(sys.executable).with_name('python.exe'))
        code = "import ctypes,json,sys; text=sys.stdin.buffer.read().decode('utf-8'); sys.stdout.buffer.write(json.dumps({'console':int(ctypes.windll.kernel32.GetConsoleWindow()),'text':text},ensure_ascii=False).encode('utf-8')); sys.stderr.buffer.write(b'diagnostic')"
        result = win.subprocess.run([executable, '-c', code], input='中文输入'.encode('utf-8'),
            capture_output=True, timeout=20, **win.background_options())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'console': 0, 'text': '中文输入'})
        self.assertEqual(result.stderr, b'diagnostic')

    @unittest.skipUnless(os.name == 'nt', 'Real Windows desktop isolation')
    def test_gui_probe_desktop_is_private_and_removed_after_process_exit(self):
        import ctypes
        from ctypes import wintypes
        executable = str(Path(sys.executable).with_name('python.exe'))
        code = "import ctypes,json; from ctypes import wintypes; u=ctypes.WinDLL('user32'); u.GetThreadDesktop.restype=wintypes.HANDLE; u.GetUserObjectInformationW.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD,ctypes.c_void_p]; h=u.GetThreadDesktop(ctypes.windll.kernel32.GetCurrentThreadId()); b=ctypes.create_unicode_buffer(256); assert u.GetUserObjectInformationW(h,2,b,ctypes.sizeof(b),None); print(json.dumps({'desktop':b.value}))"
        with win.isolated_desktop_options() as desktop:
            name = desktop['desktop']
            with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryFile() as log:
                process=win.desktop_process([executable,'-c',code],**desktop,env=dict(os.environ),cwd=temporary,stdout=log)
                try:self.assertEqual(process.wait(timeout=20),0)
                finally:
                    if process.poll() is None:process.kill();process.wait(timeout=5)
                log.seek(0)
                self.assertEqual(json.loads(log.read())['desktop'],name)
        user = ctypes.WinDLL('user32')
        user.OpenDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        user.OpenDesktopW.restype = wintypes.HANDLE
        handle = user.OpenDesktopW(name, 0, False, 1)
        if handle:
            user.CloseDesktop.argtypes = [wintypes.HANDLE]
            user.CloseDesktop(handle)
        self.assertFalse(handle, 'The completed test must release its private desktop')


class RunningProcessTests(unittest.TestCase):
    def check(self, names, read_only_source):
        app = PureWindowsPath("C:/Codex/app")
        rows = [{"ProcessId": i + 1, "ExecutablePath": str(app / name)} for i, name in enumerate(names)]
        with mock.patch.object(win, "_app_root", return_value=app), mock.patch.object(win, "_powershell", return_value=json.dumps(rows)):
            win.require_stopped(app, read_only_source=read_only_source)

    def test_shared_service_can_run_only_in_read_only_source(self):
        self.check(["resources/codex-windows-sandbox-service.exe"], True)
        with self.assertRaises(win.WindowsSupportError):
            self.check(["resources/codex-windows-sandbox-service.exe"], False)

    def test_gui_and_other_backends_still_block_installation(self):
        for name in ("ChatGPT.exe", "resources/codex.exe", "other/codex-windows-sandbox-service.exe"):
            with self.subTest(name=name), self.assertRaises(win.WindowsSupportError):
                self.check(["resources/codex-windows-sandbox-service.exe", name], True)


def pe_fixture(payload, fuse=b"010011001"):
    """Minimal PE32+ with a named resource type/name and one language leaf."""
    blob = bytearray(2048)
    blob[:2] = b"MZ"
    struct.pack_into("<I", blob, 0x3c, 0x80)
    blob[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", blob, 0x86, 1)
    struct.pack_into("<H", blob, 0x94, 240)
    struct.pack_into("<H", blob, 0x98, 0x20b)
    struct.pack_into("<II", blob, 0x98 + 112 + 16, 0x1000, 0x500)
    section = 0x98 + 240
    blob[section:section + 8] = b".rsrc\0\0\0"
    struct.pack_into("<IIII", blob, section + 8, 0x500, 0x1000, 0x500, 0x200)
    base = 0x200
    for relative, named, numbered, key, value in (
        (0, 1, 0, 0x80000080, 0x80000020),
        (0x20, 1, 0, 0x800000a0, 0x80000040),
        (0x40, 0, 1, 1033, 0x60),
    ):
        struct.pack_into("<HH", blob, base + relative + 12, named, numbered)
        struct.pack_into("<II", blob, base + relative + 16, key, value)
    for relative, name in ((0x80, "Integrity"), (0xa0, "ElectronAsar")):
        encoded = name.encode("utf-16le")
        struct.pack_into("<H", blob, base + relative, len(name))
        blob[base + relative + 2:base + relative + 2 + len(encoded)] = encoded
    struct.pack_into("<IIII", blob, base + 0x60, 0x1100, len(payload), 0, 0)
    blob[base + 0x100:base + 0x100 + len(payload)] = payload
    wire = win.FUSE_SENTINEL + bytes((1, len(fuse))) + fuse
    blob[0x700:0x700 + len(wire)] = wire
    return bytes(blob)


def write_asar(path):
    package = json.dumps({"name": "openai-codex-electron", "productName": "Codex", "version": "26.1", "codexBuildNumber": "42"}).encode()
    raw = json.dumps({"files": {"package.json": {"offset": "0", "size": len(package), "integrity": {"algorithm": "SHA256", "hash": hashlib.sha256(package).hexdigest()}}}}, separators=(",", ":")).encode()
    payload = struct.pack("<I", len(raw)) + raw
    payload += b"\0" * (-len(payload) % 4)
    header = struct.pack("<I", len(payload)) + payload
    path.parent.mkdir(parents=True)
    path.write_bytes(struct.pack("<II", 4, len(header)) + header + package)
    return hashlib.sha256(raw).hexdigest()


class ResourceTests(unittest.TestCase):
    def test_extracts_named_resource_exact_range(self):
        payload = b'[{"file":"resources/app.asar","alg":"sha256","value":"' + b"a" * 64 + b'"}]'
        blob = pe_fixture(payload)
        leaves = win.pe_resources(blob)
        self.assertEqual(len(leaves), 1)
        self.assertEqual(leaves[0]["path"], ("Integrity", "ElectronAsar", 1033))
        self.assertEqual(leaves[0]["data"], payload)
        self.assertEqual(win._fuses(blob), "010011001")

    def test_rejects_truncated_resources(self):
        for blob in (b"", b"MZ", pe_fixture(b"[]")[:800]):
            with self.assertRaises(win.WindowsSupportError):
                win.pe_resources(blob)

    def test_rejects_cycle(self):
        blob = bytearray(pe_fixture(b"[]"))
        struct.pack_into("<I", blob, 0x200 + 20, 0x80000000)
        with self.assertRaisesRegex(win.WindowsSupportError, "Cyclic"):
            win.pe_resources(blob)

    def test_rejects_ambiguous_integrity_entry(self):
        item = {"file": "resources/app.asar", "alg": "SHA256", "value": "a" * 64}
        with self.assertRaises(win.WindowsSupportError):
            win._integrity_entry([item, item])

    def test_update_changes_only_existing_hash_and_preserves_fuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp)
            digest = write_asar(app / "resources/app.asar")
            (app / "resources/codex.exe").write_bytes(b"cli")
            metadata = [{"file": "resources\\app.asar", "alg": "SHA256", "value": "a" * 64}]
            original = pe_fixture(json.dumps(metadata).encode())
            exe = app / "ChatGPT.exe"
            exe.write_bytes(original)
            result = win.update_integrity(app, digest)
            expected = original.replace(b"a" * 64, digest.encode())
            self.assertEqual(exe.read_bytes(), expected)
            self.assertEqual(result["header_hash"], digest)
            self.assertEqual(result["fuses"], "010011001")
            self.assertEqual(win.verify_integrity(app)["header_hash"], digest)
            with self.assertRaisesRegex(win.WindowsSupportError, "archive changed"):
                win.verify_integrity(app, "b" * 64)

    def test_no_mutation_when_header_or_schema_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp)
            digest = write_asar(app / "resources/app.asar")
            (app / "resources/codex.exe").write_bytes(b"cli")
            original = pe_fixture(b"[]")
            exe = app / "ChatGPT.exe"
            exe.write_bytes(original)
            with self.assertRaises(win.WindowsSupportError):
                win.update_integrity(app, digest)
            self.assertEqual(exe.read_bytes(), original)

    def test_store_source_is_read_only(self):
        with mock.patch.object(win, "_app_root", return_value=Path("/WindowsApps/Codex/app")):
            with self.assertRaisesRegex(win.WindowsSupportError, "protected Store"):
                win.update_integrity(Path("ignored"), "a" * 64)

    def test_rejects_disabled_integrity_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp)
            digest = write_asar(app / "resources/app.asar")
            (app / "resources/codex.exe").write_bytes(b"cli")
            original = pe_fixture(json.dumps([{"file": "resources/app.asar", "alg": "SHA256", "value": "a" * 64}]).encode(), b"010001001")
            exe = app / "ChatGPT.exe"
            exe.write_bytes(original)
            with self.assertRaisesRegex(win.WindowsSupportError, "remain enabled"):
                win.update_integrity(app, digest)
            self.assertEqual(exe.read_bytes(), original)

    def test_build_number_uses_shipped_codex_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp)
            write_asar(app / "resources/app.asar")
            (app / "resources/codex.exe").write_bytes(b"cli")
            (app / "ChatGPT.exe").write_bytes(b"exe")
            self.assertEqual(win.inspect_app(app)["build"], "42")

    @unittest.skipUnless(sys.platform == "win32", "native Windows long-path regression")
    def test_clone_preserves_files_beyond_legacy_max_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source, target = base / "source", base / "target"
            (source / "resources").mkdir(parents=True)
            (source / "resources/app.asar").write_bytes(b"fixture")
            relative = Path(*(["nested-component-0123456789"] * 12)) / "preserved.txt"
            source_file = "\\\\?\\" + str((source / relative).resolve())
            os.makedirs(os.path.dirname(source_file))
            with open(source_file, "wb") as handle:
                handle.write(b"deep package file")
            try:
                win.clone_app(source, target)
                with open("\\\\?\\" + str((target / relative).resolve()), "rb") as handle:
                    self.assertEqual(handle.read(), b"deep package file")
            finally:
                for path in (source, target):
                    if path.exists():
                        shutil.rmtree("\\\\?\\" + str(path.resolve()))

    def test_credentials_reject_non_windows(self):
        with mock.patch.object(win.sys, "platform", "darwin"):
            with self.assertRaisesRegex(win.WindowsSupportError, "requires Windows"):
                win.credential_set("test", "user", "secret")


if __name__ == "__main__":
    unittest.main()
