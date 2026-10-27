"""Windows app discovery, PE resource integrity and native credential helpers.

App-file mutations target an explicit caller-selected copy. Store files remain
read-only; an explicit updater action may stop verified app processes. No third party modules, fuse disabling or plaintext credential files.
"""
from __future__ import annotations

import ctypes
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import uuid

FUSE_SENTINEL = b"dL7pKGdnNz796PbbjQWNKmHXBZaB9tsX"
DICTIONARY_SENTINEL = b"AGbevlPCksUGKNL8TSn7wGmJEuJsXb2A"


class WindowsSupportError(ValueError):
    pass


def background_options():
    """Hide owned Windows utilities without changing their redirected pipes."""
    if os.name != "nt":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startup}


def native_ui_options():
    """Suppress a console without hiding the application's first GUI window."""
    return {"creationflags": 0x08000000} if os.name == 'nt' else {}


def close_app_handles(targets):
    """Explicit updater exit: retain verified process handles across graceful quit.

    Never kill by executable name, follow unrelated descendants, cross sessions,
    or include the Store sandbox service. A relaunched app has new handles and
    is left for the installer's final running-process check.
    """
    import ntpath
    import time
    from ctypes import wintypes as w
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];kernel.OpenProcess.restype=w.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes=[w.HANDLE,w.DWORD,w.LPWSTR,ctypes.POINTER(w.DWORD)]
    kernel.WaitForSingleObject.argtypes=[w.HANDLE,w.DWORD];kernel.WaitForSingleObject.restype=w.DWORD
    kernel.TerminateProcess.argtypes=[w.HANDLE,w.UINT];kernel.TerminateProcess.restype=w.BOOL
    kernel.CloseHandle.argtypes=[w.HANDLE]
    kernel.ProcessIdToSessionId.argtypes=[w.DWORD,ctypes.POINTER(w.DWORD)]
    current=w.DWORD()
    if not kernel.ProcessIdToSessionId(os.getpid(),ctypes.byref(current)):
        raise WindowsSupportError('Unable to verify the updater session')
    roots=[ntpath.normcase(str(_app_root(p))).rstrip('\\/')+'\\' for p in targets]
    handles={}
    try:
        # Enumerate without spawning PowerShell/WMI during the quit gap.
        kernel.K32EnumProcesses.argtypes=[ctypes.POINTER(w.DWORD),w.DWORD,ctypes.POINTER(w.DWORD)]
        capacity=1024
        while True:
            ids=(w.DWORD*capacity)();used=w.DWORD()
            if not kernel.K32EnumProcesses(ids,ctypes.sizeof(ids),ctypes.byref(used)):
                raise WindowsSupportError('Unable to inspect Codex processes')
            if used.value<ctypes.sizeof(ids):break
            capacity*=2
        for pid in ids[:used.value//ctypes.sizeof(w.DWORD)]:
            if not pid or pid==os.getpid():continue
            handle=kernel.OpenProcess(0x1000|0x100000|0x0001,False,pid)
            if not handle:continue
            path=ctypes.create_unicode_buffer(32768);size=w.DWORD(len(path));session=w.DWORD()
            queried=kernel.QueryFullProcessImageNameW(handle,0,path,ctypes.byref(size))
            # Windows may return the 8.3 spelling used at launch. Compare the
            # resolved path on both sides (also follows junctions consistently).
            executable=ntpath.normcase(str(Path(path.value).resolve())) if queried else ''
            valid=(queried and kernel.ProcessIdToSessionId(pid,ctypes.byref(session)) and session.value==current.value
                   and any(executable.startswith(root) for root in roots)
                   and ntpath.basename(executable).casefold()!='codex-windows-sandbox-service.exe')
            if valid:handles[pid]=handle
            else:kernel.CloseHandle(handle)
        user=ctypes.WinDLL('user32',use_last_error=True)
        callback=ctypes.WINFUNCTYPE(w.BOOL,w.HWND,w.LPARAM)
        user.EnumWindows.argtypes=[callback,w.LPARAM]
        user.GetWindowThreadProcessId.argtypes=[w.HWND,ctypes.POINTER(w.DWORD)]
        user.PostMessageW.argtypes=[w.HWND,w.UINT,w.WPARAM,w.LPARAM]
        @callback
        def close_window(hwnd, _):
            pid=w.DWORD();user.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
            if pid.value in handles and kernel.WaitForSingleObject(handles[pid.value],0)==258:
                user.PostMessageW(hwnd,0x0010,0,0)  # WM_CLOSE, asynchronous normal quit
            return True
        user.EnumWindows(close_window,0)
        until=time.monotonic()+3
        while time.monotonic()<until and any(kernel.WaitForSingleObject(h,0)==258 for h in handles.values()):
            time.sleep(.1)
        for handle in handles.values():
            if kernel.WaitForSingleObject(handle,0)==258 and not kernel.TerminateProcess(handle,0):
                raise WindowsSupportError('The verified Codex backend could not exit')
        for handle in handles.values():
            if kernel.WaitForSingleObject(handle,5000)!=0:
                raise WindowsSupportError('Codex backend exit has not completed')
    finally:
        for handle in handles.values():kernel.CloseHandle(handle)


def grant_current_user(directory):
    """Keep inherited UI access tied to the token user, including elevated writes.

    Preserve the directory's other ACEs. Native APIs avoid a PowerShell startup
    or module lookup on the first GUI launch.
    """
    from ctypes import wintypes as w
    adv=ctypes.WinDLL('advapi32',use_last_error=True)
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    pointer=ctypes.c_void_p
    class Trustee(ctypes.Structure):
        _fields_=[('multiple',pointer),('operation',w.DWORD),('form',w.DWORD),
                  ('kind',w.DWORD),('name',pointer)]
    class Access(ctypes.Structure):
        _fields_=[('permissions',w.DWORD),('mode',w.DWORD),
                  ('inheritance',w.DWORD),('trustee',Trustee)]
    kernel.GetCurrentProcess.restype=w.HANDLE
    kernel.CloseHandle.argtypes=[w.HANDLE]
    kernel.LocalFree.argtypes=[pointer];kernel.LocalFree.restype=pointer
    adv.OpenProcessToken.argtypes=[w.HANDLE,w.DWORD,ctypes.POINTER(w.HANDLE)]
    adv.GetTokenInformation.argtypes=[w.HANDLE,ctypes.c_int,pointer,w.DWORD,ctypes.POINTER(w.DWORD)]
    adv.ConvertSidToStringSidW.argtypes=[pointer,ctypes.POINTER(pointer)]
    adv.GetNamedSecurityInfoW.argtypes=[w.LPWSTR,ctypes.c_int,w.DWORD,
        ctypes.POINTER(pointer),ctypes.POINTER(pointer),ctypes.POINTER(pointer),
        ctypes.POINTER(pointer),ctypes.POINTER(pointer)]
    adv.GetNamedSecurityInfoW.restype=w.DWORD
    adv.SetEntriesInAclW.argtypes=[w.ULONG,ctypes.POINTER(Access),pointer,ctypes.POINTER(pointer)]
    adv.SetEntriesInAclW.restype=w.DWORD
    adv.SetNamedSecurityInfoW.argtypes=[w.LPWSTR,ctypes.c_int,w.DWORD,pointer,pointer,pointer,pointer]
    adv.SetNamedSecurityInfoW.restype=w.DWORD
    token=w.HANDLE();descriptor=pointer();updated=pointer();sid_text=pointer()
    if not adv.OpenProcessToken(kernel.GetCurrentProcess(),0x0008,ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        length=w.DWORD()
        adv.GetTokenInformation(token,1,None,0,ctypes.byref(length))
        if not length.value:raise ctypes.WinError(ctypes.get_last_error())
        data=ctypes.create_string_buffer(length.value)
        if not adv.GetTokenInformation(token,1,data,length,ctypes.byref(length)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid=ctypes.cast(data,ctypes.POINTER(pointer)).contents
        if not adv.ConvertSidToStringSidW(sid,ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        value=ctypes.wstring_at(sid_text)
        old=pointer();path=str(Path(directory).resolve())
        code=adv.GetNamedSecurityInfoW(path,1,4,None,None,ctypes.byref(old),None,ctypes.byref(descriptor))
        if code:raise ctypes.WinError(code)
        entry=Access(0x1F01FF,2,3,Trustee(None,0,0,1,sid))
        code=adv.SetEntriesInAclW(1,ctypes.byref(entry),old,ctypes.byref(updated))
        if code:raise ctypes.WinError(code)
        code=adv.SetNamedSecurityInfoW(path,1,4,None,None,updated,None)
        if code:raise ctypes.WinError(code)
        return value
    finally:
        for allocated in (sid_text,updated,descriptor):
            if allocated:kernel.LocalFree(allocated)
        kernel.CloseHandle(token)


def native_ui_command(script, parameters):
    """Host bundled WinForms code as a command, without changing execution policy.

    Parameters travel as encoded data rather than shell fragments. This is the
    same built-in PowerShell command host used by the other native utilities;
    system language/application controls remain in force.
    """
    if any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9]*', key) or not isinstance(value,(str,bool))
           for key,value in parameters.items()):
        raise WindowsSupportError('Invalid native UI parameters')
    value=base64.b64encode(json.dumps({'script':str(Path(script).resolve()),'parameters':parameters}).encode()).decode()
    code=r'''$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';[Console]::OutputEncoding=[Text.UTF8Encoding]::new();
$v=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__VALUE__'))|ConvertFrom-Json;
$p=@{};foreach($entry in $v.parameters.PSObject.Properties){$p[$entry.Name]=$entry.Value};
$ui=[ScriptBlock]::Create([IO.File]::ReadAllText($v.script,[Text.Encoding]::UTF8));& $ui @p'''.replace('__VALUE__',value)
    return ['powershell.exe','-NoLogo','-NoProfile','-NonInteractive','-STA',
            '-EncodedCommand',base64.b64encode(code.encode('utf-16le')).decode()]


@contextmanager
def isolated_desktop_options():
    """Boot a Windows test GUI on a private desktop, never the user's desktop."""
    if os.name != "nt":
        yield {}
        return
    from ctypes import wintypes
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.CreateDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
                                   wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    user.CreateDesktopW.restype = wintypes.HANDLE
    user.CloseDesktop.argtypes = [wintypes.HANDLE]
    user.CloseDesktop.restype = wintypes.BOOL
    name = 'CodexPatchProbe-' + uuid.uuid4().hex
    desktop = user.CreateDesktopW(name, None, None, 0, 0x10000000, None)
    if not desktop:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield {'desktop': name}
    finally:
        user.CloseDesktop(desktop)


def desktop_process(args, desktop, *, env, cwd, stdout):
    """Create an owned test process with native lpDesktop (not exposed by Popen)."""
    from ctypes import wintypes as w
    import msvcrt
    class Startup(ctypes.Structure):
        _fields_ = [('cb',w.DWORD),('reserved',w.LPWSTR),('desktop',w.LPWSTR),('title',w.LPWSTR),
                    *[(n,w.DWORD) for n in ('x','y','width','height','chars_x','chars_y','fill','flags')],
                    ('show',w.WORD),('reserved_size',w.WORD),('reserved_ptr',ctypes.c_void_p),
                    ('stdin',w.HANDLE),('stdout',w.HANDLE),('stderr',w.HANDLE)]
    class ProcessInfo(ctypes.Structure):
        _fields_ = [('process',w.HANDLE),('thread',w.HANDLE),('pid',w.DWORD),('tid',w.DWORD)]
    kernel = ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateProcessW.argtypes = [w.LPCWSTR,w.LPWSTR,ctypes.c_void_p,ctypes.c_void_p,w.BOOL,
        w.DWORD,ctypes.c_void_p,w.LPCWSTR,ctypes.POINTER(Startup),ctypes.POINTER(ProcessInfo)]
    kernel.CreateProcessW.restype = w.BOOL
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.WaitForSingleObject.argtypes = [w.HANDLE,w.DWORD]
    kernel.WaitForSingleObject.restype = w.DWORD
    kernel.GetExitCodeProcess.argtypes = [w.HANDLE,ctypes.POINTER(w.DWORD)]
    kernel.TerminateProcess.argtypes = [w.HANDLE,w.UINT]
    info = ProcessInfo()
    with open(os.devnull,'rb') as null:
        handles = [msvcrt.get_osfhandle(null.fileno()),msvcrt.get_osfhandle(stdout.fileno())]
        inherited = [os.get_handle_inheritable(h) for h in handles]
        try:
            for h in handles:os.set_handle_inheritable(h,True)
            startup = Startup();startup.cb=ctypes.sizeof(startup);startup.desktop=desktop
            # The private desktop supplies isolation. Do not hide the first
            # GUI window: the probe must exercise the production show path.
            startup.flags=0x100
            startup.stdin=handles[0];startup.stdout=startup.stderr=handles[1]
            command=ctypes.create_unicode_buffer(subprocess.list2cmdline([str(a) for a in args]))
            environment=ctypes.create_unicode_buffer('\0'.join(k+'='+v for k,v in sorted(env.items()))+'\0\0')
            # CREATE_UNICODE_ENVIRONMENT | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
            if not kernel.CreateProcessW(str(args[0]),command,None,None,True,0x08000600,
                                         environment,str(cwd),ctypes.byref(startup),ctypes.byref(info)):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            for h,old in zip(handles,inherited):os.set_handle_inheritable(h,old)
    kernel.CloseHandle(info.thread)
    class OwnedProcess:
        pid=info.pid
        returncode=None
        def poll(self):
            if self.returncode is None and kernel.WaitForSingleObject(info.process,0)==0:
                code=w.DWORD()
                if not kernel.GetExitCodeProcess(info.process,ctypes.byref(code)):raise ctypes.WinError(ctypes.get_last_error())
                self.returncode=code.value;kernel.CloseHandle(info.process)
            return self.returncode
        def wait(self,timeout=None):
            if self.poll() is None:
                result=kernel.WaitForSingleObject(info.process,0xFFFFFFFF if timeout is None else max(0,int(timeout*1000)))
                if result==258:raise subprocess.TimeoutExpired(args,timeout)
                if result!=0:raise ctypes.WinError(ctypes.get_last_error())
            return self.poll()
        def kill(self):
            if self.poll() is None and not kernel.TerminateProcess(info.process,1):raise ctypes.WinError(ctypes.get_last_error())
    return OwnedProcess()


def _require(ok, message):
    if not ok:
        raise WindowsSupportError(message)


def _powershell(script):
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            check=True, capture_output=True, text=True, encoding="utf-8", **background_options())
    return result.stdout.strip()


def discover_app():
    """Find the current registered Codex package, without changing its ACLs."""
    _require(sys.platform == "win32", "Automatic Windows discovery requires Windows")
    script = ("[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
              "@(Get-AppxPackage -Name OpenAI.Codex | Select-Object -ExpandProperty InstallLocation) | ConvertTo-Json -Compress")
    raw = _powershell(script)
    locations = json.loads(raw) if raw else []
    if isinstance(locations, str):
        locations = [locations]
    candidates = [Path(p) / "app" for p in locations if (Path(p) / "app/resources/app.asar").is_file()]
    if not candidates:
        for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("PROGRAMFILES")):
            if base:
                for name in ("Programs/Codex", "Codex", "OpenAI/Codex"):
                    candidate = Path(base) / name
                    if (candidate / "resources/app.asar").is_file():
                        candidates.append(candidate)
    candidates = list(dict.fromkeys(p.resolve() for p in candidates))
    _require(len(candidates) == 1, "Cannot identify one Codex installation; pass --app explicitly")
    return candidates[0]


def _app_root(app):
    app = Path(app).resolve()
    if (app / "app/resources/app.asar").is_file():
        app /= "app"
    _require((app / "resources/app.asar").is_file(), "Windows app.asar is missing")
    return app


def is_store_app(app):
    return any(part.casefold() == "windowsapps" for part in Path(app).parts)


def _hash_file(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _asar(path):
    with Path(path).open("rb") as handle:
        prefix = handle.read(16)
        _require(len(prefix) == 16, "Truncated ASAR prefix")
        magic, size, payload, length = struct.unpack("<4I", prefix)
        _require(magic == 4 and 8 <= size <= 64 * 1024 * 1024 and payload == size - 4 and length <= size - 8,
                 "Invalid ASAR header")
        raw = handle.read(length)
        _require(len(raw) == length and 8 + size <= Path(path).stat().st_size, "Truncated ASAR header")
    return json.loads(raw), 8 + size, hashlib.sha256(raw).hexdigest()


def _package(archive):
    header, base, _ = _asar(archive)
    item = header.get("files", {}).get("package.json", {})
    _require("offset" in item and not item.get("unpacked"), "Packed package metadata is missing")
    offset, size = int(item["offset"]), item["size"]
    _require(offset >= 0 and 0 <= size <= 4 * 1024 * 1024 and base + offset + size <= archive.stat().st_size,
             "Invalid package metadata range")
    with archive.open("rb") as handle:
        handle.seek(base + offset)
        data = handle.read(size)
    integrity = item.get("integrity", {})
    _require(integrity.get("algorithm") == "SHA256" and
             hashlib.sha256(data).hexdigest() == integrity.get("hash"),
             "Package metadata member integrity mismatch")
    return json.loads(data)


def inspect_app(app):
    app = _app_root(app)
    archive = app / "resources/app.asar"
    package = _package(archive)
    cli = app / "resources/codex.exe"
    exe = app / "ChatGPT.exe"
    _require(exe.is_file() and cli.is_file(), "Unsupported Windows runtime layout; expected ChatGPT.exe and bundled codex.exe")
    _require(package.get("name") == "openai-codex-electron" and package.get("productName") == "Codex",
             "Wrong application package identity")
    version = str(package.get("version", ""))
    build = str(package.get("codexBuildNumber", package.get("buildNumber", package.get("build", ""))))
    manifest = app.parent / "AppxManifest.xml"
    store_version = None
    if manifest.is_file():
        import xml.etree.ElementTree as ET
        root = ET.fromstring(manifest.read_bytes())
        identity = next((item for item in root if item.tag.rsplit("}", 1)[-1] == "Identity"), None)
        if identity is not None:
            _require(identity.get("Name") == "OpenAI.Codex", "Wrong MSIX package identity")
            store_version = identity.get("Version")
    return {"app": app, "asar": archive, "exe": exe, "cli": cli,
            "version": version, "build": build, "store_version": store_version,
            "runtime": "owl" if (app / "owl-shell-runtime.json").is_file() else "electron"}


def pe_resources(blob):
    """Return bounded PE resource leaves with exact payload byte ranges."""
    def unpack(fmt, offset):
        size = struct.calcsize(fmt)
        _require(0 <= offset <= len(blob) - size, "Truncated PE structure")
        return struct.unpack_from(fmt, blob, offset)
    _require(blob[:2] == b"MZ", "Not a PE executable")
    pe, = unpack("<I", 0x3c)
    _require(blob[pe:pe + 4] == b"PE\0\0", "Invalid PE signature")
    sections, = unpack("<H", pe + 6)
    optional_size, = unpack("<H", pe + 20)
    optional = pe + 24
    magic, = unpack("<H", optional)
    _require(magic in (0x10b, 0x20b), "Unsupported PE optional header")
    directories = optional + (112 if magic == 0x20b else 96)
    _require(directories + 24 <= optional + optional_size, "PE has no resource directory")
    resource_rva, resource_size = unpack("<II", directories + 16)
    table = optional + optional_size
    mappings = []
    for i in range(sections):
        vsize, va, raw_size, raw = unpack("<IIII", table + i * 40 + 8)
        mappings.append((va, raw_size, raw))
    def rva_offset(rva, size):
        matches = [raw + rva - va for va, raw_size, raw in mappings
                   if va <= rva and 0 <= size <= raw_size and rva - va <= raw_size - size]
        _require(len(matches) == 1 and matches[0] <= len(blob) - size, "Ambiguous/out-of-range PE resource RVA")
        return matches[0]
    if not resource_rva or not resource_size:
        return []
    base = rva_offset(resource_rva, resource_size)
    def bounded(relative, size):
        _require(0 <= relative <= resource_size - size, "Resource tree points outside its directory")
        return base + relative
    def name(value):
        if not value & 0x80000000:
            return value
        relative = value & 0x7fffffff
        count, = unpack("<H", bounded(relative, 2))
        start = bounded(relative + 2, count * 2)
        return blob[start:start + count * 2].decode("utf-16le")
    leaves, visited = [], set()
    count = 0
    def walk(relative, path):
        nonlocal count
        _require(relative not in visited and len(path) <= 2, "Cyclic or overdeep PE resource tree")
        visited.add(relative)
        directory = bounded(relative, 16)
        named, numbered = unpack("<HH", directory + 12)
        _require(named + numbered <= 65536, "Excessive PE resource entries")
        bounded(relative + 16, 8 * (named + numbered))
        for i in range(named + numbered):
            count += 1
            _require(count <= 65536, "Excessive PE resource tree")
            key, value = unpack("<II", directory + 16 + 8 * i)
            parts = (*path, name(key))
            if value & 0x80000000:
                walk(value & 0x7fffffff, parts)
            else:
                _require(len(parts) == 3, "Unexpected PE resource path depth")
                entry = bounded(value, 16)
                rva, size, codepage, reserved = unpack("<IIII", entry)
                offset = rva_offset(rva, size)
                _require(not any(max(offset, x['offset']) < min(offset + size, x['offset'] + x['size']) for x in leaves),
                         "Overlapping PE resource payloads")
                leaves.append({"path": parts, "offset": offset, "size": size,
                               "data": blob[offset:offset + size], "codepage": codepage})
    walk(0, ())
    return leaves


def _integrity_resource(blob):
    candidates = []
    for leaf in pe_resources(blob):
        if any(isinstance(p, str) and p.upper() in ("INTEGRITY", "ELECTRONASAR") for p in leaf["path"]):
            try:
                raw = leaf["data"].rstrip(b"\0")
                metadata = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            candidates.append((leaf, metadata))
    _require(len(candidates) == 1, "Missing or ambiguous executable ASAR integrity resource")
    return candidates[0]


def _integrity_entry(metadata):
    _require(isinstance(metadata, list), "Unsupported Windows ASAR integrity schema")
    entries = [entry for entry in metadata if isinstance(entry, dict) and
               str(entry.get("file", "")).replace("\\", "/").casefold() == "resources/app.asar"]
    _require(len(entries) == 1, "Missing or ambiguous app.asar integrity entry")
    entry = entries[0]
    _require(str(entry.get("alg")).upper() == "SHA256" and re.fullmatch("[0-9a-fA-F]{64}", str(entry.get("value", ""))),
             "Unsupported Windows ASAR integrity algorithm/hash")
    return entry


def _fuses(blob):
    if FUSE_SENTINEL not in blob:
        return None
    _require(blob.count(FUSE_SENTINEL) == 1, "Ambiguous Electron fuse wire")
    start = blob.index(FUSE_SENTINEL) + len(FUSE_SENTINEL)
    _require(start + 2 <= len(blob), "Truncated Electron fuse wire")
    version, count = blob[start:start + 2]
    _require(version == 1 and start + 2 + count <= len(blob), "Unsupported Electron fuse wire")
    states = blob[start + 2:start + 2 + count]
    _require(all(value in b"01r" for value in states), "Invalid Electron fuse states")
    return states.decode()


def _runtime_fuses(info):
    carrier = info["app"] / "chrome.dll"
    if not carrier.is_file():
        carrier = info["exe"]
    blob = carrier.read_bytes()
    _require(DICTIONARY_SENTINEL not in blob, "Unsupported Windows integrity dictionary digest policy")
    states = _fuses(blob)
    _require(states is not None and len(states) > 5 and states[4:6] == "11",
             "Embedded ASAR integrity and ASAR-only loading must remain enabled")
    return carrier, states


def verify_integrity(app, expected_asar=None):
    info = inspect_app(app)
    if expected_asar:
        _require(_hash_file(info["asar"]) == expected_asar, "App archive changed")
    header_hash = _asar(info["asar"])[2]
    blob = info["exe"].read_bytes()
    leaf, metadata = _integrity_resource(blob)
    entry = _integrity_entry(metadata)
    _require(entry["value"].lower() == header_hash, "Executable ASAR header hash mismatch")
    carrier, states = _runtime_fuses(info)
    return {"header_hash": header_hash, "resource_path": list(leaf["path"]),
            "fuses": states, "fuse_carrier": str(carrier.name)}


def update_integrity(app, new_header_hash):
    """Update only the existing fixed-width resource hash on an unpackaged copy."""
    app = _app_root(app)
    _require(not is_store_app(app), "Refusing to modify a protected Store package; build a separate copy")
    _require(re.fullmatch("[0-9a-f]{64}", new_header_hash) is not None, "Invalid new ASAR header hash")
    info = inspect_app(app)
    _require(_asar(info["asar"])[2] == new_header_hash, "New ASAR header hash does not match the archive")
    _runtime_fuses(info)
    blob = info["exe"].read_bytes()
    leaf, metadata = _integrity_resource(blob)
    entry = _integrity_entry(metadata)
    old = entry["value"].encode("ascii")
    payload = leaf["data"]
    _require(payload.count(old) == 1, "Original integrity hash is ambiguous")
    updated = payload.replace(old, new_header_hash.encode("ascii"), 1)
    result = blob[:leaf["offset"]] + updated + blob[leaf["offset"] + leaf["size"]:]
    _require(len(result) == len(blob) and _fuses(result) == _fuses(blob), "Executable structure/fuses changed")
    with info["exe"].open("r+b") as handle:
        handle.seek(leaf["offset"])
        handle.write(updated)
        handle.flush()
        os.fsync(handle.fileno())
    return verify_integrity(app)


def clone_app(source, target):
    source, target = _app_root(source), Path(target)
    _require(not target.exists(), "Refusing to overwrite " + str(target))
    _require(not target.resolve().is_relative_to(source), "App copy cannot be inside its source")
    def extended(path):
        value = str(path.resolve())
        if sys.platform != "win32" or value.startswith("\\\\?\\"):
            return value
        return "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
    # Deep bundled pnpm paths exceed MAX_PATH even on fresh Windows installs.
    shutil.copytree(extended(source), extended(target), symlinks=True)


def require_stopped(app, *, read_only_source=False):
    app = str(_app_root(app)).rstrip("\\/").casefold() + "\\"
    raw = _powershell("[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
                      "@(Get-CimInstance Win32_Process | Select-Object ProcessId,ExecutablePath) | ConvertTo-Json -Compress")
    processes = json.loads(raw) if raw else []
    if isinstance(processes, dict):
        processes = [processes]
    shared_service = app + "resources\\codex-windows-sandbox-service.exe"
    # The official source is never renamed or modified. Its registered sandbox
    # service can outlive the GUI; keep it running for other Codex clients.
    # A managed deployment being replaced must still have no active binaries.
    active = [str(p.get("ExecutablePath") or "").casefold() for p in processes]
    _require(not any(path.startswith(app) and not (read_only_source and path == shared_service)
                     for path in active),
             "Quit the target app normally before installation. No process was stopped.")


def verify_publisher(app):
    """Require Windows to validate the original publisher, without trusting path names."""
    info = inspect_app(app)
    paths = [info["exe"]]
    paths.extend(p for p in (info["app"] / "chrome.dll", info["app"] / "Codex.exe")
                 if p.is_file() and p not in paths)
    results = []
    for path in paths:
        literal = str(path).replace("'", "''")
        raw = _powershell("[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
                          f"$s=Get-AuthenticodeSignature -LiteralPath '{literal}'; "
                          "@{status=$s.Status.ToString();subject=$s.SignerCertificate.Subject;"
                          "name=$s.SignerCertificate.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName,$false)} | ConvertTo-Json -Compress")
        result = json.loads(raw)
        _require(result.get("status") == "Valid" and result.get("name") == "OpenAI OpCo, LLC",
                 "Original runtime does not have a trusted OpenAI publisher signature: " + path.name)
        results.append({"file": path.name, **result})
    return results


def _credential_api():
    _require(sys.platform == "win32", "Credential Manager requires Windows")
    from ctypes import wintypes
    class Credential(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                    ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]
    api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    api.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(Credential))]
    api.CredReadW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    return api, Credential


def credential_set(target, username, secret):
    api, Credential = _credential_api()
    raw = secret.encode("utf-8")
    _require(0 < len(raw) <= 2560, "Credential has an invalid size")
    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    credential = Credential(Type=1, TargetName=target, UserName=username, CredentialBlobSize=len(raw),
                            CredentialBlob=buffer, Persist=2)
    try:
        if not api.CredWriteW(ctypes.byref(credential), 0):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        ctypes.memset(buffer, 0, len(raw))


def credential_get(target):
    api, Credential = _credential_api()
    pointer = ctypes.POINTER(Credential)()
    if not api.CredReadW(target, 1, 0, ctypes.byref(pointer)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(pointer.contents.CredentialBlob, pointer.contents.CredentialBlobSize).decode("utf-8")
    finally:
        api.CredFree(pointer)


def main():
    import argparse
    import getpass
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("credential-get", "credential-set", "inspect"))
    parser.add_argument("--target", default="Codex DeepSeek API Key")
    parser.add_argument("--username", default=getpass.getuser())
    parser.add_argument("--app", type=Path)
    args = parser.parse_args()
    if args.action == "credential-get":
        print(credential_get(args.target), end="")
    elif args.action == "credential-set":
        credential_set(args.target, args.username, getpass.getpass("DeepSeek API key: "))
        print("Credential saved in Windows Credential Manager.")
    else:
        info = inspect_app(args.app or discover_app())
        print(json.dumps({**info, "integrity": verify_integrity(info["app"])}, default=str, indent=2))


if __name__ == "__main__":
    main()
