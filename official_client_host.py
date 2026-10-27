"""Native lifecycle and scoped runtime UI compatibility; no app-bundle edits.

The native hooks call ``ensure SETTINGS`` synchronously and produce no context.
SETTINGS contains an argv array, a private loopback health URL and its identity.
"""
from __future__ import annotations

import argparse
import base64
import queue
import re
import threading
import copy
import json
import os
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


class HostError(ValueError):
    pass


class ComposerAlreadyRunningError(HostError):
    pass


def _require(condition, message):
    if not condition:
        raise HostError(message)


def windowless_python(executable, platform=None):
    """Require a real pythonw sibling on Windows; never fall back to a console."""
    executable = Path(executable).resolve()
    if (platform or sys.platform) == "win32":
        if executable.name.lower() == "python.exe":
            executable = executable.with_name("pythonw.exe")
        _require(executable.name.lower() == "pythonw.exe" and executable.is_file(),
                 "A pythonw.exe runtime is required for native Windows hooks")
    _require(executable.is_file(), "Python runtime is missing")
    return executable


def managed_hook_entries(settings_path, python=None, script=None, platform=None):
    """Build owned user hooks. Native trust review still applies to these entries."""
    platform = platform or sys.platform
    executable = windowless_python(python or sys.executable, platform)
    args = [str(executable), str(Path(script or __file__).resolve()), "ensure",
            str(Path(settings_path).resolve())]
    command = subprocess.list2cmdline(args) if platform == "win32" else shlex.join(args)
    handler = {"type": "command", "command": command, "timeout": 20}
    return {
        "SessionStart": {"matcher": "^(startup|resume|clear|compact|fork)$",
                         "hooks": [copy.deepcopy(handler)]},
        "UserPromptSubmit": {"hooks": [copy.deepcopy(handler)]},
    }


def merge_managed_hooks(existing, entries):
    """Pure merge. Return exact added entries for a later ownership-safe undo."""
    result = copy.deepcopy(existing or {})
    _require(isinstance(result, dict), "Hooks file must be a JSON object")
    hooks = result.setdefault("hooks", {})
    _require(isinstance(hooks, dict), "Existing hooks field must be an object")
    added = {}
    for event, entry in entries.items():
        values = hooks.setdefault(event, [])
        _require(isinstance(values, list), "Existing hook event must be an array: " + event)
        matches = values.count(entry)
        _require(matches <= 1, "Duplicate managed hook entries require review: " + event)
        if matches == 0:
            values.append(copy.deepcopy(entry))
            added[event] = copy.deepcopy(entry)
    return result, {"format": 1, "added": added}


def remove_managed_hooks(existing, ownership):
    """Remove only unchanged entries recorded as added by this installation."""
    _require(ownership.get("format") == 1, "Unknown managed-hook ownership format")
    result = copy.deepcopy(existing)
    hooks = result.get("hooks", {})
    for event, entry in ownership.get("added", {}).items():
        values = hooks.get(event, [])
        _require(isinstance(values, list) and values.count(entry) == 1,
                 "Managed hook changed or disappeared; review before undo: " + event)
        values.remove(entry)
        if not values:
            hooks.pop(event, None)
    if not hooks:
        result.pop("hooks", None)
    return result


def _settings(value):
    result = copy.deepcopy(value)
    command = result.get("adapter_command")
    _require(isinstance(command, list) and len(command) >= 2 and
             all(isinstance(part, str) and part and "\0" not in part for part in command),
             "Adapter command must be a nonempty argv array")
    _require(Path(command[0]).is_absolute(), "Adapter runtime must use an absolute path")
    url = urllib.parse.urlsplit(result.get("health_url", ""))
    _require(url.scheme == "http" and url.hostname == "127.0.0.1" and url.port and
             not url.username and not url.password and not url.query and not url.fragment,
             "Adapter health endpoint must be numeric loopback HTTP")
    _require(isinstance(result.get("health_identity"), str) and result["health_identity"],
             "Adapter health identity is required")
    return result


def health_status(settings, timeout=0.4):
    """Return bounded, credential-free diagnostics for the exact local identity."""
    try:
        request = urllib.request.Request(settings["health_url"], method="GET")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                return {'ready': False, 'reason': 'http_status'}
            value = json.loads(response.read(8193))
        ready = (value.get("ok") is True and value.get("identity") == settings["health_identity"]
                 and (settings.get("edition") is None or value.get("edition") == settings["edition"]))
        return {'ready': ready, 'reason': 'ready' if ready else 'identity_mismatch'}
    except TimeoutError:
        return {'ready': False, 'reason': 'timeout'}
    except urllib.error.URLError as error:
        return {'ready': False, 'reason': 'timeout' if isinstance(error.reason, TimeoutError) else 'unreachable'}
    except Exception:
        return {'ready': False, 'reason': 'invalid_response'}


def activity_status(settings, *, lease=None, release=False, timeout=1.5):
    """Read/drain only the exact owned adapter; old protocols remain unknown."""
    try:
        import math
        settings=_settings(settings)
        url=settings['health_url'];data=None
        if lease is not None:
            _require(isinstance(lease,str) and __import__('re').fullmatch('[a-f0-9]{32}',lease),'Invalid update lease')
            url=url.removesuffix('/health')+'/maintenance'
            data=json.dumps({'action':'release' if release else 'drain','lease':lease}).encode()
        request=urllib.request.Request(url,data=data,headers={'Content-Type':'application/json'})
        with urllib.request.build_opener(urllib.request.ProxyHandler({}),_NoRedirect()).open(request,timeout=timeout) as response:
            value=json.loads(response.read(8193))
        if value.get('identity')!=settings['health_identity'] or value.get('edition')!=settings.get('edition'):
            return {'known':False,'reason':'identity_mismatch'}
        a=value.get('activity',{})
        if value.get('update_control')!=1:return {'known':False,'reason':'older_adapter'}
        _require(all(type(a.get(k)) is int and a[k]>=0 for k in ('requests','tools','unknown')) and
                 isinstance(a.get('idle_for'),(float,int)) and math.isfinite(a['idle_for']) and a['idle_for']>=0,
                 'Invalid activity response')
        return {'known':True,**a,'acquired':value.get('acquired') is True,
                'idle':not any(a[k] for k in ('requests','tools','unknown')) and a['idle_for']>=10}
    except Exception:
        return {'known':False,'reason':'unavailable'}


def request_diagnostics(settings, timeout=1):
    """Read recent metadata from the exact owned service, without persisting it."""
    try:
        url=settings['health_url']
        if not url.endswith('/health'):return []
        request=urllib.request.Request(url[:-len('health')]+'diagnostics')
        with urllib.request.build_opener(urllib.request.ProxyHandler({}),_NoRedirect()).open(request,timeout=timeout) as response:
            raw=response.read(65537)
        if len(raw)>65536:return []
        value=json.loads(raw)
        if value.get('identity')!=settings['health_identity'] or value.get('edition')!=settings.get('edition'):return []
        rows=value.get('requests',[])
        if not isinstance(rows,list):return []
        return [{k:r[k] for k in ('id','model','target','service','protocol','phase','elapsed_ms','first_byte_ms','status','error_kind','error_source') if k in r}
                for r in rows[:64] if isinstance(r,dict)]
    except Exception:return []


def healthy(settings, timeout=0.4):
    """Check the installation identity, never accept an unrelated local listener."""
    return health_status(settings, timeout)['ready']


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise HostError("Adapter health redirect refused")


def ensure_adapter(value, timeout=12, poll_interval=0.1):
    """Start on demand; concurrent starters accept the matching winner's health.

    The adapter owns its listener/bind race. A second child may exit because the
    first child won the port; readiness is always checked before handling exit.
    """
    settings = _settings(value)
    if healthy(settings):
        return {"status": "ready", "started": False}
    options = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {"start_new_session": True}
    child = subprocess.Popen(settings["adapter_command"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             cwd=settings.get("working_directory") or str(Path(settings["adapter_command"][0]).parent),
                             **options)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if healthy(settings):
            return {"status": "ready", "started": True}
        # A winning concurrent starter can still be initializing after this
        # child exits. Wait through the same bounded readiness deadline.
        time.sleep(min(poll_interval, max(0, deadline - time.monotonic())))
    raise HostError("The local model adapter did not become ready before the startup deadline")


def lifecycle_mcp_config(settings_path, python=None, script=None):
    """A native stdio service with no tools and no model-visible output."""
    executable = Path(python or sys.executable).resolve()
    _require(executable.is_file(), "A stdio-capable Python runtime is required")
    return {"command": str(executable),
            "args": [str(Path(script or __file__).resolve()), "mcp-ensure", str(Path(settings_path).resolve())],
            "enabled": True, "startup_timeout_sec": 20}


def adapter_launchd_plist(value, label="com.limyum.codex-model-adapter"):
    """Plan a per-user relay service; the official app launches independently."""
    settings = _settings(value)
    _require(label and all(c.isalnum() or c in ".-" for c in label), "Invalid service label")
    return {"Label": label, "ProgramArguments": settings["adapter_command"],
            "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 5,
            # This listener is on the critical path of foreground model turns.
            # Background I/O throttling can starve a cold start during updates.
            "ProcessType": "Standard", "WorkingDirectory": settings.get("working_directory") or
            str(Path(settings["adapter_command"][0]).parent)}


def adapter_windows_task_xml(value, user_sid):
    """Plan original-icon-compatible startup without a replacement shortcut."""
    import re
    settings = _settings(value)
    _require(re.fullmatch(r"S-\d+(?:-\d+)+", user_sid or ""), "Invalid Windows user SID")
    command = list(settings["adapter_command"])
    _require(Path(command[0]).name.lower() == "pythonw.exe", "The adapter task requires pythonw.exe")
    ns = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    ET.register_namespace("", ns)
    def node(parent, name, text=None, **attributes):
        value = ET.SubElement(parent, "{" + ns + "}" + name, attributes)
        if text is not None:
            value.text = str(text)
        return value
    task = ET.Element("{" + ns + "}Task", {"version": "1.2"})
    triggers = node(task, "Triggers")
    logon = node(triggers, "LogonTrigger")
    node(logon, "Enabled", "true")
    node(logon, "UserId", user_sid)
    principals = node(task, "Principals")
    principal = node(principals, "Principal", id="Author")
    node(principal, "UserId", user_sid)
    node(principal, "LogonType", "InteractiveToken")
    node(principal, "RunLevel", "LeastPrivilege")
    options = node(task, "Settings")
    node(options, "MultipleInstancesPolicy", "IgnoreNew")
    node(options, "DisallowStartIfOnBatteries", "false")
    node(options, "StopIfGoingOnBatteries", "false")
    node(options, "StartWhenAvailable", "true")
    node(options, "Enabled", "true")
    node(options, "Hidden", "true")
    node(options, "ExecutionTimeLimit", "PT0S")
    restart = node(options, "RestartOnFailure")
    node(restart, "Interval", "PT1M")
    node(restart, "Count", "3")
    actions = node(task, "Actions", Context="Author")
    execute = node(actions, "Exec")
    node(execute, "Command", command[0])
    node(execute, "Arguments", subprocess.list2cmdline(command[1:]))
    node(execute, "WorkingDirectory", settings.get("working_directory") or str(Path(command[0]).parent))
    return ET.tostring(task, encoding="utf-16", xml_declaration=True).decode("utf-16")


def mcp_lifecycle(settings, input_stream=None, output_stream=None):
    """Initialization waits for health; the service exposes an empty tool list."""
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    for line in input_stream:
        if len(line) > 1_048_576:
            raise HostError("Oversized lifecycle protocol message")
        message = json.loads(line)
        method, identifier = message.get("method"), message.get("id")
        if identifier is None:
            continue
        if method == "initialize":
            try:
                ensure_adapter(settings)
                result = {"protocolVersion": message.get("params", {}).get("protocolVersion", "2024-11-05"),
                          "capabilities": {"tools": {}},
                          "serverInfo": {"name": "codex-model-adapter-lifecycle", "version": "1"}}
                response = {"jsonrpc": "2.0", "id": identifier, "result": result}
            except Exception:
                response = {"jsonrpc": "2.0", "id": identifier,
                            "error": {"code": -32603, "message": "Local model adapter startup failed"}}
        elif method == "tools/list":
            response = {"jsonrpc": "2.0", "id": identifier, "result": {"tools": []}}
        elif method == "ping":
            response = {"jsonrpc": "2.0", "id": identifier, "result": {}}
        else:
            response = {"jsonrpc": "2.0", "id": identifier,
                        "error": {"code": -32601, "message": "Method not found"}}
        output_stream.write(json.dumps(response) + "\n")
        output_stream.flush()


def mac_host_identity(app):
    """Read and validate the untouched official bundle and its bundled CLI."""
    app = Path(app).resolve()
    with (app / "Contents/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    _require(info.get("CFBundleIdentifier") == "com.openai.codex", "Unexpected Mac app identity")
    executable = app / "Contents/MacOS" / info["CFBundleExecutable"]
    cli = app / "Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
    node = app / "Contents/Resources/cua_node/bin/node"
    identities = {}
    for item in (app, cli, node):
        subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(item)],
                       check=True, capture_output=True, timeout=60)
        result = subprocess.run(["/usr/bin/codesign", "-d", "--verbose=4", str(item)],
                                check=True, capture_output=True, text=True, timeout=20)
        _require("TeamIdentifier=2DC432GLL2" in result.stderr,
                 "The selected app/CLI is not signed by OpenAI")
        identities[str(item)] = next((line.removeprefix("Identifier=") for line in result.stderr.splitlines()
                                      if line.startswith("Identifier=")), None)
    return {"app": str(app), "executable": str(executable), "cli": str(cli), "node": str(node),
            "code_identifiers": identities,
            "bundle_id": info["CFBundleIdentifier"], "team_id": "2DC432GLL2",
            "version": info.get("CFBundleShortVersionString"), "build": info.get("CFBundleVersion")}


def isolated_mac_plan(app, directory, verify=True):
    """Plan an isolated official-host run; execution is explicitly separate."""
    identity = mac_host_identity(app) if verify else {"executable": str(Path(app) / "Contents/MacOS/ChatGPT")}
    directory = Path(directory).resolve()
    _require(len(os.fsencode(directory / "home/ipc/ipc.sock")) <= 103,
             "The isolated directory is too long for a Mac Unix socket; choose a short temporary path")
    environment = {key: value for key, value in os.environ.items()
                   if key in ("PATH", "LANG", "LC_ALL", "LC_CTYPE")}
    environment.update(HOME=str(directory / "user"), CODEX_HOME=str(directory / "home"),
                       CODEX_ELECTRON_USER_DATA_PATH=str(directory / "browser"),
                       TMPDIR=str(directory / "temp"), NO_PROXY="*", no_proxy="*")
    return {"argv": [identity["executable"], "--user-data-dir=" + str(directory / "browser"),
                     "--no-first-run", "--use-mock-keychain"],
            "environment": environment, "cwd": str(directory), "identity": identity,
            "directories": [str(directory / part) for part in ("user", "home", "browser", "temp")]}


# Model-scoped renderer compatibility. Original application bytes remain signed.
class CompatibilityError(ValueError):
    pass


IDENT = r"[A-Za-z_$][\w$]*"
MARKER = "/* codex-independent-model-quota-v1 */"


def _one(pattern, text, label):
    found = list(re.finditer(pattern, text))
    if len(found) != 1:
        raise CompatibilityError("Unsupported native composer: " + label)
    return found[0]


def transform(source, role, models):
    """Match the selected-model binding and preserve every other guard byte."""
    if MARKER in source:
        raise CompatibilityError("Composer asset already adapted")
    if not isinstance(models, list) or not models or any(
            not isinstance(m, str) or not m or '\0' in m for m in models):
        raise CompatibilityError("Explicit independent model IDs are required")
    ids = json.dumps(sorted(set(models)), ensure_ascii=True, separators=(',', ':'))
    if role == 'adapter':
        match = _one(r'(?P<quota>'+IDENT+r')=(?P<hook>'+IDENT+r')\('
                     r'(?P<host>'+IDENT+r')===`local`&&(?P<atom>'+IDENT+r')\)', source,
                     'local quota subscription')
        prefix = source[max(0, match.start()-1500):match.start()]
        selected = _one(r'(?P<mode>'+IDENT+r')\.settings\.model===`gpt-reserve`', prefix,
                        'selected model')
        model = selected['mode'] + '.settings.model'
        # Keep the subscription unconditional across model changes (React hooks).
        replacement = match[0] + '&&!' + ids + '.includes(' + model + ')'
    elif role == 'banner':
        match = _one(r'(?P<result>'+IDENT+r')=(?P<host>'+IDENT+r')===`local`&&'
                     r'(?P<global>'+IDENT+r')\|\|(?P<per_model>'+IDENT+r')\('
                     r'(?P<status>'+IDENT+r'),(?P<model>'+IDENT+r')\)', source,
                     'composer quota banner')
        # The existing memo dependencies already include the selected model.
        # Remote hosts are outside this installation's model route ownership.
        replacement = (match['result'] + '=(' + match['host'] + '!==`local`||!'
                       + ids + '.includes(' + match['model'] + '))&&('
                       + match[0].split('=', 1)[1] + ')')
    else:
        raise CompatibilityError("Unknown composer role")
    return source[:match.start()] + MARKER + replacement + source[match.end():]


def prepare(app, models):
    """Read integrity-checked originals; persist only hashes and member names."""
    import patch as p
    archive = p.app_layout(app)['asar']
    header, base, _ = p.archive_header(archive)
    assets = {}
    for member, entry in p.entries(header):
        role = ('adapter' if re.fullmatch(r'webview/assets/codex-composer-adapter-[\w-]+\.js', member)
                else 'banner' if re.fullmatch(r'webview/assets/composer-content-[\w-]+\.js', member)
                else None)
        if role is None or entry.get('unpacked'):
            continue
        raw = p.read_member(archive, entry, base)
        p.check_member(raw, entry)
        source = raw.decode('utf-8')
        # Some builds have a re-export shim with the same filename prefix.
        if (role == 'banner' and 'sendBlocked:' not in source
                or role == 'adapter' and 'submitDisabled:' not in source):
            continue
        if role in assets:
            raise CompatibilityError('Ambiguous native composer asset')
        adapted = transform(source, role, models).encode()
        assets[role] = {'member': member, 'source_sha256': p.sha(raw),
                        'adapted_sha256': p.sha(adapted)}
    if set(assets) != {'adapter', 'banner'}:
        raise CompatibilityError('Native composer assets were not identified')
    return {'format': 1, 'source_asar': p.file_sha(archive),
            'models': sorted(set(models)), 'assets': assets}


def assets(app, plan):
    """Revalidate against the staged application before any runtime interception."""
    import patch as p
    archive = p.app_layout(app)['asar']
    if plan.get('format') != 1 or p.file_sha(archive) != plan.get('source_asar'):
        raise CompatibilityError('Codex changed; rebuild composer compatibility first')
    header, base, _ = p.archive_header(archive)
    inventory = dict(p.entries(header))
    result = {}
    for role, record in plan['assets'].items():
        entry = inventory.get(record['member'])
        if entry is None:
            raise CompatibilityError('Native composer asset disappeared')
        raw = p.read_member(archive, entry, base)
        p.check_member(raw, entry)
        if p.sha(raw) != record['source_sha256']:
            raise CompatibilityError('Native composer asset changed')
        raw = transform(raw.decode('utf-8'), role, plan['models']).encode()
        if p.sha(raw) != record['adapted_sha256']:
            raise CompatibilityError('Composer adaptation changed after preparation')
        result['app://-/' + record['member'].removeprefix('webview/')] = raw
    return result


def _mac_process(command, input_fd, output_fd, env, cwd):
    """Launch the signed app as its own TCC responsible process, like open -a.

    Disclaim only for the official app child, never for its tools or a shell.
    Pipes remain inherited without moving privacy grants to the Python host.
    """
    import ctypes as ct
    import signal
    library=ct.CDLL(None)
    try:disclaim=library.responsibility_spawnattrs_setdisclaim
    except AttributeError:
        raise CompatibilityError('This macOS cannot preserve the official app permission identity') from None
    pointer=ct.POINTER(ct.c_void_p)
    signatures={
        'posix_spawnattr_init':[pointer], 'posix_spawnattr_destroy':[pointer],
        'posix_spawn_file_actions_init':[pointer], 'posix_spawn_file_actions_destroy':[pointer],
        'posix_spawnattr_setflags':[pointer,ct.c_short], 'posix_spawnattr_setpgroup':[pointer,ct.c_int],
        'posix_spawn_file_actions_adddup2':[pointer,ct.c_int,ct.c_int],
        'posix_spawn_file_actions_addclose':[pointer,ct.c_int],
        'posix_spawn_file_actions_addopen':[pointer,ct.c_int,ct.c_char_p,ct.c_int,ct.c_uint],
        'posix_spawn_file_actions_addchdir_np':[pointer,ct.c_char_p],
        'posix_spawn':[ct.POINTER(ct.c_int),ct.c_char_p,pointer,pointer,ct.POINTER(ct.c_char_p),ct.POINTER(ct.c_char_p)]}
    for name,args in signatures.items():
        getattr(library,name).argtypes=args;getattr(library,name).restype=ct.c_int
    disclaim.argtypes=[pointer,ct.c_int];disclaim.restype=ct.c_int
    def checked(result):
        if result:raise OSError(result,os.strerror(result))
    attributes=ct.c_void_p();actions=ct.c_void_p()
    checked(library.posix_spawnattr_init(ct.byref(attributes)))
    copies=[]
    try:
        checked(library.posix_spawn_file_actions_init(ct.byref(actions)))
        try:
            copies=[os.dup(input_fd),os.dup(output_fd)]
            for fd,mode in ((0,os.O_RDONLY),(1,os.O_WRONLY),(2,os.O_WRONLY)):
                checked(library.posix_spawn_file_actions_addopen(ct.byref(actions),fd,b'/dev/null',mode,0))
            for source,target in zip(copies,(3,4)):
                checked(library.posix_spawn_file_actions_adddup2(ct.byref(actions),source,target))
                checked(library.posix_spawn_file_actions_addclose(ct.byref(actions),source))
            if cwd:checked(library.posix_spawn_file_actions_addchdir_np(ct.byref(actions),os.fsencode(cwd)))
            checked(library.posix_spawnattr_setflags(ct.byref(attributes),0x4000|0x0002))
            checked(library.posix_spawnattr_setpgroup(ct.byref(attributes),0))
            checked(disclaim(ct.byref(attributes),1))
            args=[os.fsencode(v) for v in command]
            environment=[os.fsencode(k+'='+v) for k,v in (env if env is not None else os.environ).items()]
            argv=(ct.c_char_p*(len(args)+1))(*args,None)
            envp=(ct.c_char_p*(len(environment)+1))(*environment,None)
            child=ct.c_int()
            checked(library.posix_spawn(ct.byref(child),args[0],ct.byref(actions),ct.byref(attributes),argv,envp))
        finally:library.posix_spawn_file_actions_destroy(ct.byref(actions))
    finally:
        library.posix_spawnattr_destroy(ct.byref(attributes))
        for fd in copies:os.close(fd)
    class Process:
        pid=child.value
        returncode=None
        def poll(self):
            if self.returncode is None:
                pid,status=os.waitpid(self.pid,os.WNOHANG)
                if pid:self.returncode=os.waitstatus_to_exitcode(status)
            return self.returncode
        def terminate(self):os.kill(self.pid,signal.SIGTERM)
        def wait(self,timeout=None):
            deadline=time.monotonic()+timeout if timeout is not None else float('inf')
            while self.poll() is None:
                if time.monotonic()>=deadline:raise subprocess.TimeoutExpired(command,timeout)
                time.sleep(.02)
            return self.returncode
    return Process()


def _package_process(command, handles, env, cwd):
    """Retain the package identity on this app, with normal child breakaway.

    The controller is invoked inside the registered package. Unlike
    PreventBreakaway, policy 0x05 applies identity to this child only and leaves
    its descendants at the documented default. No package registration changes.
    """
    import ctypes as ct
    from ctypes import wintypes as wt
    kernel = ct.WinDLL('kernel32', use_last_error=True)
    class Startup(ct.Structure):
        _fields_ = [('cb',wt.DWORD),('reserved',wt.LPWSTR),('desktop',wt.LPWSTR),('title',wt.LPWSTR),
                    ('x',wt.DWORD),('y',wt.DWORD),('xsize',wt.DWORD),('ysize',wt.DWORD),
                    ('xchars',wt.DWORD),('ychars',wt.DWORD),('fill',wt.DWORD),('flags',wt.DWORD),
                    ('show',wt.WORD),('reserved_size',wt.WORD),('reserved_ptr',ct.c_void_p),
                    ('stdin',wt.HANDLE),('stdout',wt.HANDLE),('stderr',wt.HANDLE)]
    class StartupEx(ct.Structure):
        _fields_ = [('startup',Startup),('attributes',ct.c_void_p)]
    class Info(ct.Structure):
        _fields_ = [('process',wt.HANDLE),('thread',wt.HANDLE),('pid',wt.DWORD),('tid',wt.DWORD)]
    kernel.InitializeProcThreadAttributeList.argtypes=[ct.c_void_p,wt.DWORD,wt.DWORD,ct.POINTER(ct.c_size_t)]
    kernel.UpdateProcThreadAttribute.argtypes=[ct.c_void_p,wt.DWORD,ct.c_size_t,ct.c_void_p,ct.c_size_t,ct.c_void_p,ct.c_void_p]
    kernel.DeleteProcThreadAttributeList.argtypes=[ct.c_void_p]
    kernel.CreateProcessW.argtypes=[wt.LPCWSTR,wt.LPWSTR,ct.c_void_p,ct.c_void_p,wt.BOOL,wt.DWORD,ct.c_void_p,wt.LPCWSTR,ct.c_void_p,ct.POINTER(Info)]
    kernel.CloseHandle.argtypes=[wt.HANDLE]
    size=ct.c_size_t()
    kernel.InitializeProcThreadAttributeList(None,2,0,ct.byref(size))
    attributes=ct.create_string_buffer(size.value)
    if not kernel.InitializeProcThreadAttributeList(attributes,2,0,ct.byref(size)):
        raise ct.WinError(ct.get_last_error())
    info=Info()
    try:
        inherited=(wt.HANDLE*len(handles))(*handles)
        policy=wt.DWORD(0x05)
        for attribute,value in ((0x20002,inherited),(0x20012,policy)):
            if not kernel.UpdateProcThreadAttribute(attributes,0,attribute,ct.byref(value),ct.sizeof(value),None,None):
                raise ct.WinError(ct.get_last_error())
        startup=StartupEx();startup.startup.cb=ct.sizeof(startup)
        startup.attributes=ct.cast(attributes,ct.c_void_p)
        block=ct.create_unicode_buffer('\0'.join(k+'='+v for k,v in sorted((env or os.environ).items(),key=lambda x:x[0].upper()))+'\0\0')
        line=ct.create_unicode_buffer(subprocess.list2cmdline(command))
        if not kernel.CreateProcessW(command[0],line,None,None,True,0x08000000|0x00080000|0x400,
                                     block,str(cwd) if cwd else None,ct.byref(startup),ct.byref(info)):
            raise ct.WinError(ct.get_last_error())
    finally:
        kernel.DeleteProcThreadAttributeList(attributes)
    kernel.CloseHandle(info.thread)
    class Process:
        pid=info.pid
        _handle=info.process
        def poll(self):
            code=wt.DWORD()
            kernel.GetExitCodeProcess.argtypes=[wt.HANDLE,ct.POINTER(wt.DWORD)]
            if not kernel.GetExitCodeProcess(self._handle,ct.byref(code)):
                raise ct.WinError(ct.get_last_error())
            return None if code.value==259 else code.value
        def terminate(self):
            kernel.TerminateProcess.argtypes=[wt.HANDLE,wt.UINT]
            if not kernel.TerminateProcess(self._handle,1):raise ct.WinError(ct.get_last_error())
        def wait(self,timeout=None):
            kernel.WaitForSingleObject.argtypes=[wt.HANDLE,wt.DWORD]
            if kernel.WaitForSingleObject(self._handle,0xffffffff if timeout is None else int(timeout*1000))==258:
                raise subprocess.TimeoutExpired(command,timeout)
            return self.poll()
        def __del__(self):
            kernel.CloseHandle(self._handle)
    return Process()


class Pipe:
    """A private inherited pipe; callers never attach to another running app."""
    def __init__(self, argv, *, env=None, cwd=None, package=False):
        read_child, write_parent = os.pipe()
        read_parent, write_child = os.pipe()
        options = {}
        if os.name == 'nt':
            import msvcrt
            handles = [msvcrt.get_osfhandle(fd) for fd in (read_child, write_child)]
            for handle in handles:
                os.set_handle_inheritable(handle, True)
            startup = subprocess.STARTUPINFO()
            startup.lpAttributeList = {'handle_list': handles}
            options.update(startupinfo=startup, creationflags=0x08000000)
            command = [*argv, '--remote-debugging-pipe',
                       '--remote-debugging-io-pipes=' + ','.join(map(str, handles))]
        elif sys.platform=='darwin':
            command=[*argv,'--remote-debugging-pipe']
        else:
            # A tiny exec shim maps only inherited pipe descriptors to 3/4.
            # It avoids preexec_fn in a potentially multithreaded host.
            shim = ('import os,sys; r=int(sys.argv[1]);w=int(sys.argv[2]);'
                    'r=os.dup(r);w=os.dup(w);os.dup2(r,3);os.dup2(w,4);'
                    'os.set_inheritable(3,True);os.set_inheritable(4,True);'
                    '[os.close(f) for f in set([int(sys.argv[1]),int(sys.argv[2]),r,w]) if f not in (3,4)];'
                    'os.execv(sys.argv[3],sys.argv[3:])')
            command = [sys.executable, '-c', shim, str(read_child), str(write_child),
                       *argv, '--remote-debugging-pipe']
            options.update(pass_fds=(read_child, write_child), start_new_session=True)
        try:
            if sys.platform=='darwin':
                self.process = _mac_process(command,read_child,write_child,env,cwd)
            elif package and os.name == 'nt':
                self.process = _package_process(command, handles, env, cwd)
            else:
                self.process = subprocess.Popen(command, env=env, cwd=cwd,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    **options)
        except BaseException:
            os.close(read_parent); os.close(write_parent)
            raise
        finally:
            os.close(read_child); os.close(write_child)
        self.reader, self.writer = read_parent, write_parent
        self.messages = queue.Queue()
        self.sequence = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        pending = bytearray()
        try:
            while chunk := os.read(self.reader, 65536):
                pending.extend(chunk)
                while 0 in pending:
                    raw, _, rest = pending.partition(b'\0')
                    pending = bytearray(rest)
                    try:self.messages.put(json.loads(raw))
                    except ValueError:self.messages.put({'method':'__composer_pipe_error'})
        except OSError:
            self.messages.put({'method':'__composer_pipe_error'})
            return
        self.messages.put(None)

    def send(self, method, params=None, session=None):
        self.sequence += 1
        message = {'id': self.sequence, 'method': method, 'params': params or {}}
        if session:
            message['sessionId'] = session
        raw = memoryview(json.dumps(message).encode() + b'\0')
        while raw:
            raw = raw[os.write(self.writer, raw):]
        return self.sequence

    def close(self):
        for fd in (self.reader, self.writer):
            try:
                os.close(fd)
            except OSError:
                pass


class Controller:
    """Intercept only the exact prepared assets. Never inspect chat payloads."""
    def __init__(self, pipe, replacements):
        self.pipe, self.replacements = pipe, replacements
        self.pending = {}
        self.fulfilled = {}
        self.sessions = set()
        self.frames = {}
        self.applied = set()
        self.ready = False
        self.attach_request = None

    def start(self):
        self.attach_request = self.pipe.send('Target.setAutoAttach', {'autoAttach': True,
            'waitForDebuggerOnStart': True, 'flatten': True,
            'filter': [{'type': 'page'}, {'exclude': True}]})

    def handle(self, message):
        method, data = message.get('method'), message.get('params', {})
        if method=='__composer_pipe_error':
            raise CompatibilityError('Native renderer pipe became unreadable')
        if self.attach_request is not None and message.get('id') == self.attach_request:
            if 'error' in message:
                raise CompatibilityError('Native renderer attachment is unavailable')
            self.ready = True
        elif method == 'Target.attachedToTarget':
            session = data['sessionId']
            self.sessions.add(session)
            self.frames[session] = set()
            self.pipe.send('Page.enable',session=session)
            identifier = self.pipe.send('Fetch.enable', {'patterns': [
                {'urlPattern': url, 'requestStage': 'Request'} for url in self.replacements]}, session)
            self.pending[identifier] = session
        elif message.get('id') in self.pending:
            session = self.pending.pop(message['id'])
            # Always resume the native target even when interception is unsupported.
            self.pipe.send('Runtime.runIfWaitingForDebugger', session=session)
            if 'error' in message:
                raise CompatibilityError('Native renderer interception is unavailable')
        elif method == 'Page.frameNavigated':
            session=message.get('sessionId');frame=data.get('frame',{})
            if session in self.frames and not frame.get('parentId'):
                self.frames[session].clear()
                url=urllib.parse.urlsplit(frame.get('url',''))
                if url.scheme=='app' and url.netloc=='-' and url.path in ('/index.html','/detached-window.html'):
                    self.frames[session].add(frame['id'])
        elif method == 'Target.detachedFromTarget':
            session=data.get('sessionId');self.sessions.discard(session);self.frames.pop(session,None)
            self.pending={key:value for key,value in self.pending.items() if value!=session}
            self.fulfilled={key:value for key,value in self.fulfilled.items() if value[0]!=session}
        elif method == 'Fetch.requestPaused':
            session, request = message.get('sessionId'), data.get('request', {})
            url = request.get('url')
            raw = self.replacements.get(url)
            if (data.get('frameId') in self.frames.get(session,set())
                    and request.get('method') == 'GET' and raw is not None):
                identifier = self.pipe.send('Fetch.fulfillRequest', {'requestId': data['requestId'],
                    'responseCode': 200, 'responseHeaders': [
                        {'name': 'Content-Type', 'value': 'text/javascript; charset=utf-8'},
                        {'name': 'Cache-Control', 'value': 'no-store'}],
                    'body': base64.b64encode(raw).decode()}, session)
                self.fulfilled[identifier] = (session,url)
            else:
                self.pipe.send('Fetch.continueRequest', {'requestId': data['requestId']}, session)
        elif message.get('id') in self.fulfilled:
            session,url = self.fulfilled.pop(message['id'])
            if 'error' in message:
                raise CompatibilityError('Native composer replacement was rejected')
            self.applied.add(url)

    def detach(self):
        for session in self.sessions:
            self.pipe.send('Fetch.disable', session=session)
            self.pipe.send('Runtime.runIfWaitingForDebugger', session=session)
        self.pipe.send('Target.setAutoAttach',{'autoAttach':False,
            'waitForDebuggerOnStart':False,'flatten':True})


def run_composer(directory, launch_id=None):
    """Own one app session; a compatibility error never terminates that app."""
    import patch as p
    from official_client_update import verify_official
    from monitor import state_lock
    directory=Path(directory).resolve()
    manifest=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
    _require(manifest.get('status')=='installed', 'Install the prepared adapter before launching Codex')
    plan=manifest.get('composer_compat')
    _require(isinstance(plan,dict), 'Rebuild the adapter with composer compatibility first')
    _require(p.file_sha(Path(__file__))==manifest.get('runtime_sha256',{}).get('official_client_host.py'),
             'The composer launcher differs from the validated build')
    profile=json.loads((directory/'adapter-profile.json').read_text(encoding='utf-8'))
    _require(p.file_sha(directory/'adapter-profile.json')==manifest['profile_sha256']
             and plan['models']==sorted(profile['routes']), 'Composer model ownership changed')
    app=Path(manifest['target']).resolve()
    verify_official(app)
    replacements=assets(app,plan)
    status_path=directory/'composer-session.json'
    with state_lock(directory/'composer-control') as acquired:
        _require(acquired, 'A composer launch is already in progress')
        _require(not p.active_app_processes(app,read_only_source=True),
                 'Quit Codex normally, then open it from the model settings window')
        ensure_adapter(json.loads((directory/'host-settings.json').read_text(encoding='utf-8')))
        environment={**os.environ,'CODEX_HOME':manifest['codex_home']}
        if sys.platform=='darwin':
            with (app/'Contents/Info.plist').open('rb') as stream:
                environment.update(plistlib.load(stream).get('LSEnvironment',{}))
        pipe=Pipe([str(p.app_layout(app)['exe'])],env=environment,cwd=app,package=sys.platform=='win32')
        controller=Controller(pipe,replacements)
        state={'format':1,'identity':profile['identity'],'pid':pipe.process.pid,'launch_id':launch_id,
               'status':'starting','applied':0,'source_asar':plan['source_asar']}
        persisted=None
        def record():
            nonlocal persisted
            raw=json.dumps(state)
            if raw==persisted:return
            persisted=raw
            try:p.atomic_text(status_path,raw)
            except OSError:pass  # Status persistence cannot terminate the app.
        record()
        failed=False
        try:
            controller.start()
            while pipe.process.poll() is None:
                try:message=pipe.messages.get(timeout=.5)
                except queue.Empty:continue
                if message is None:
                    if pipe.process.poll() is not None:break
                    failed=True
                    try:controller.detach()
                    except OSError:pass
                    state.update(status='compatibility_failed',error='composer_pipe_closed');record()
                    continue
                if failed:continue
                try:
                    controller.handle(message)
                    state.update(status='active' if controller.ready else 'starting',applied=len(controller.applied))
                    record()
                except Exception:
                    # Disconnecting a Chromium debugging pipe exits the browser.
                    # Disable only this interception and retain the pipe until the
                    # user closes Codex; do not let an adapter exception kill work.
                    failed=True
                    try:controller.detach()
                    except OSError:pass
                    state.update(status='compatibility_failed',error='composer_interception_failed');record()
        finally:
            # Normal app exit or pipe EOF only. No app termination operation.
            pipe.close()
            state['status']='stopped';record()
    return 0


def start_composer(directory, manifest):
    """Launch the verified original through a scoped, windowless controller."""
    import patch as p
    directory=Path(directory).resolve();app=Path(manifest['target']).resolve()
    existing=p.active_app_processes(app,read_only_source=True)
    state_path=directory/'composer-session.json'
    if existing:
        state=json.loads(state_path.read_text()) if state_path.is_file() else {}
        if state.get('status')=='active' and any(row.get('pid')==state.get('pid') for row in existing):
            return {'status':'running','pid':state['pid']}
        raise ComposerAlreadyRunningError('Codex is already open. Quit it normally, then launch from model settings to enable independent-model sending.')
    python=str(windowless_python(sys.executable))
    launch_id=__import__('uuid').uuid4().hex
    args=[str(Path(__file__).resolve()),'composer-run',str(directory),'--launch-id',launch_id]
    if sys.platform=='win32':
        from official_client_shortcut import official_entry, _powershell
        entry=official_entry(app)
        aumid=entry.get('app_user_model_id','')
        _require('!' in aumid, 'The registered Store application is required')
        family,app_id=aumid.split('!',1)
        value={'family':family,'app_id':app_id,'python':python,'args':subprocess.list2cmdline(args)}
        encoded=base64.b64encode(json.dumps(value).encode()).decode()
        script=r'''$ErrorActionPreference='Stop';[Console]::OutputEncoding=[Text.UTF8Encoding]::new();
$v=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__VALUES__'))|ConvertFrom-Json;
Invoke-CommandInDesktopPackage -PackageFamilyName $v.family -AppId $v.app_id -Command $v.python -Args $v.args;
@{ok=$true}|ConvertTo-Json -Compress'''
        _require(_powershell(script.replace('__VALUES__',encoded)).get('ok'), 'Could not launch the package controller')
    else:
        subprocess.Popen([python,*args],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,start_new_session=True,cwd=directory)
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        try:state=json.loads(state_path.read_text())
        except (OSError,ValueError):state={}
        if (state.get('launch_id')==launch_id and state.get('status')=='active'
                and state.get('source_asar')==manifest['composer_compat']['source_asar']):
            return state
        if state.get('launch_id')==launch_id and state.get('status')=='compatibility_failed':
            raise HostError('The native composer is incompatible; Codex has been left open')
        time.sleep(.1)
    raise HostError('Codex did not confirm composer compatibility before the startup deadline')



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["ensure", "mcp-ensure", "mac-plan", "composer-run"])
    parser.add_argument("settings", type=Path)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--launch-id")
    args = parser.parse_args()
    if args.action=='composer-run':
        return run_composer(args.settings,args.launch_id)
    if args.action == "mcp-ensure":
        mcp_lifecycle(json.loads(args.settings.read_text(encoding="utf-8")))
        return 0
    if args.action == "ensure":
        # Hook inputs can contain prompts: consume and discard, never retain.
        if sys.stdin is not None and not sys.stdin.isatty():
            sys.stdin.read(1_048_577)
        ensure_adapter(json.loads(args.settings.read_text(encoding="utf-8")))
        return 0
    _require(args.directory is not None, "An isolated directory is required")
    print(json.dumps(isolated_mac_plan(args.settings, args.directory), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (HostError, OSError, subprocess.SubprocessError, ValueError):
        # No private URL/token, prompt, path, or raw exception enters hook logs.
        if sys.stderr is not None:
            print("Local model adapter startup failed; inspect the adapter installation.", file=sys.stderr)
        raise SystemExit(1)
