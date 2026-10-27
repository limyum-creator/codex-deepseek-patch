#!/usr/bin/env python3
"""Quiet, local update monitor for the Codex DeepSeek patch.

The backend owns compatibility checks, offline build verification and transactional
installation. This module owns durable orchestration, notifications and per-user
native scheduling. Explicit update-window requests can ask Codex to quit normally.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from windows_support import background_options

FORMAT = 1
DEFAULT_INTERVAL = 300
DEFAULT_STABLE_SECONDS = 10
ROOT = Path(__file__).resolve().parent


def timestamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def private_directory(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)
    return path


def atomic_json(path, value):
    path = Path(path)
    private_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def safe_error(error):
    text = str(error)
    text = re.sub(r"(?i)(?:sk-[\w-]{8,}|bearer\s+[^\s\"']+)", "[redacted]", text)
    text = re.sub(r"(?i)((?:api[_-]?key|token|password)\s*[=:]\s*)[^\s,;]+", r"\1[redacted]", text)
    return text[:1000]


def request_update(directory, *, sync_now=False, code_now=False, retry=False):
    """Coalesce explicit checks; a busy monitor must not discard a user request."""
    directory = Path(directory)
    with state_lock(directory / 'request-control') as locked:
        if not locked:
            raise ValueError('Another settings request is being saved; retry shortly')
        previous = load_json(directory / 'monitor.request.json', {})
        value = {'format': 1, 'id': uuid.uuid4().hex, 'requested_at': timestamp(),
                 'sync_now': bool(sync_now or previous.get('sync_now')),
                 'code_now': bool(code_now or previous.get('code_now')),
                 'retry': bool(retry or previous.get('retry'))}
        atomic_json(directory / 'monitor.request.json', value)
        return value


def take_update_request(directory):
    directory = Path(directory)
    with state_lock(directory / 'request-control') as locked:
        if not locked:
            return None
        path = directory / 'monitor.request.json'
        value = load_json(path)
        if value is None:
            return None
        if value.get('format') != 1 or not re.fullmatch(r'[a-f0-9]{32}', value.get('id', '')) or any(
                type(value.get(k)) is not bool for k in ('sync_now', 'retry')) or type(value.get('code_now',False)) is not bool:
            raise ValueError('Invalid queued update request')
        path.unlink()
        return value


def wake_monitor(directory):
    settings = load_json(Path(directory) / 'settings.json', {})
    command = service_command(directory, settings.get('python_path'))
    with (Path(directory) / 'request-worker.log').open('ab') as log:
        subprocess.Popen(command, cwd=directory, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         **(background_options() if os.name == 'nt' else {'start_new_session': True}))


@contextmanager
def state_lock(directory):
    """An OS lock releases automatically after crashes, including on Windows."""
    private_directory(directory)
    with (Path(directory) / "monitor.lock").open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def native_notify(kind, message):
    title = "Codex DeepSeek patch"
    if sys.platform == "darwin":
        script = 'on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run'
        subprocess.run(["/usr/bin/osascript", "-e", script, title, message],
                       check=True, capture_output=True, timeout=20)
    elif sys.platform == "win32":
        # Values enter PowerShell through encoded JSON, never shell interpolation.
        payload = base64.b64encode(json.dumps([title, message]).encode()).decode()
        script = ("Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
                  f"$v=ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}'))); "
                  "$n=New-Object System.Windows.Forms.NotifyIcon; "
                  "$n.Icon=[System.Drawing.SystemIcons]::Information; $n.Visible=$true; "
                  "try {$n.ShowBalloonTip(5000,$v[0],$v[1],[System.Windows.Forms.ToolTipIcon]::Info); "
                  "Start-Sleep -Seconds 6} finally {$n.Dispose()}")
        encoded = base64.b64encode(script.encode("utf-16-le")).decode()
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                       check=True, capture_output=True, timeout=20, **background_options())


class CoreBackend:
    def __new__(cls, settings):
        import patch
        if settings.get('edition'):
            from updater import UpdateBackend
            return UpdateBackend(settings)
        if settings.get('architecture')=='official-client-v1':
            from official_client_update import select_backend
            return select_backend(settings)
        return patch.MonitorBackend(settings)


class Monitor:
    def __init__(self, directory, backend, notifier=native_notify, clock=time.time):
        self.directory = private_directory(directory)
        self.backend, self.notifier, self.clock = backend, notifier, clock
        self.path = self.directory / "state.json"
        self.state = load_json(self.path, {"format": FORMAT, "phase": "idle", "terminal": {}, "notices": {}})
        if self.state.get("format") != FORMAT:
            raise ValueError("Unknown monitor state format")
        self.state.setdefault("terminal", {})
        self.state.setdefault("notices", {})

    def save(self):
        atomic_json(self.path, self.state)

    def persist_baseline(self, baseline):
        if not baseline:
            return
        # Write state first. The next invocation can recover settings from it if
        # the process exits between these two atomic writes.
        self.state["installed"]["baseline"] = baseline
        self.save()
        settings_path = self.directory / "settings.json"
        settings = load_json(settings_path)
        if settings is not None and settings.get("baseline") != baseline:
            settings["baseline"] = baseline
            atomic_json(settings_path, settings)

    def event(self, event_name, **fields):
        path = self.directory / "events.jsonl"
        if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
            os.replace(path, self.directory / "events.previous.jsonl")
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": timestamp(), "event": event_name, **fields}, ensure_ascii=False) + "\n")
        if os.name != "nt":
            path.chmod(0o600)

    def notify(self, kind, fingerprint, message):
        if kind == 'installed' and getattr(self.backend, 'quiet_model_sync', False) is True:
            return
        key = kind + ":" + fingerprint
        if key in self.state["notices"]:
            return
        # Persist before delivery: a process crash must not spam the next login.
        self.state["notices"][key] = timestamp()
        self.save()
        self.event("notification", kind=kind, fingerprint=fingerprint, message=message)
        try:
            if self.notifier is native_notify:
                from update_progress import Progress
                progress=Progress(self.directory)
                settings=load_json(self.directory/'settings.json',{})
                if progress.read().get('edition')==settings.get('edition') and settings.get('edition') in ('ds','proxy','unified'):
                    progress.present()
                    return
            self.notifier(kind, message)
        except Exception as error:
            self.state["notification_error"] = safe_error(error)
            self.event("notification_failed", kind=kind, reason=safe_error(error))
            self.save()

    def progress(self, phase):
        callback=getattr(self.backend,"progress_phase",None)
        if callback:
            try:callback(phase)
            except Exception:pass  # The optional view must never break recovery.

    def terminal(self, phase, fingerprint, reason):
        self.progress("failed")
        record = {"phase": phase, "reason": reason, "at": timestamp()}
        self.state.update(phase=phase, reason=reason)
        self.state["terminal"][fingerprint] = record
        self.state.pop("pending", None)
        self.save()
        self.event(phase, fingerprint=fingerprint, reason=reason)
        message = ("Codex was updated, but the patch is incompatible. " if phase == "incompatible" else
                   "The automatic Codex patch needs attention. ") + reason
        self.notify(phase, fingerprint, message)

    @staticmethod
    def fingerprint(snapshot):
        value = snapshot.get("fingerprint")
        if not isinstance(value, str) or not value:
            raise ValueError("Backend snapshot has no fingerprint")
        return value

    def observe_change(self, snapshot):
        fingerprint = self.fingerprint(snapshot)
        self.state.update(phase="stabilizing", candidate={"fingerprint": fingerprint,
                          "snapshot": snapshot, "first_seen": self.clock(), "observations": 1})
        self.state.pop("pending", None)
        self.state.pop("reason", None)
        self.save()
        self.event("candidate", fingerprint=fingerprint)
        self.progress("available")

    def run_once(self, retry=False):
        with state_lock(self.directory) as locked:
            if not locked:
                return {"phase": "busy"}
            # Another invocation may have changed state before we acquired the lock.
            self.state = load_json(self.path, self.state)
            request = take_update_request(self.directory)
            if request:
                retry = retry or request['retry']
                self.state['last_request'] = request
                settings = getattr(self.backend, 'settings', None)
                if isinstance(settings, dict) and request['sync_now']:
                    settings['force_sync'] = True
                if isinstance(settings, dict) and request.get('code_now'):
                    settings['force_code'] = True
                    settings['skip_model_sync'] = not request['sync_now']
            if retry:
                from update_progress import Progress
                Progress(self.directory).retry()
            self.state["last_checked_at"] = timestamp()
            fingerprint = "snapshot-unavailable"
            try:
                snapshot = self.backend.snapshot()
                fingerprint = self.fingerprint(snapshot)
                self.state["observed"] = snapshot
                for warning in snapshot.get('warnings',[]):
                    safe=safe_error(warning)
                    self.notify('update-check',hashlib.sha256(safe.encode()).hexdigest(),safe)
                if not self.state.get("adoption_checked") or self.state.get("phase") == "installing":
                    recovering_install = self.state.get("phase") == "installing"
                    adoption = self.backend.adopt()
                    self.state["adoption_checked"] = True
                    if adoption:
                        self.state["installed"] = adoption
                        self.state.update(phase="installed")
                        self.state.pop("pending", None)
                        self.event("adopted", fingerprint=adoption["fingerprint"])
                        self.persist_baseline(adoption.get("baseline"))
                        if recovering_install:
                            source = self.state.get("candidate", {}).get("fingerprint", fingerprint)
                            self.notify("installed", source, "The Codex update was checked and the DeepSeek patch was installed. Open Codex normally.")
                installed = self.state.get("installed", {})
                if installed.get("fingerprint") == fingerprint:
                    self.state.update(phase="installed")
                    self.state.pop("reason", None)
                    self.save()
                    return self.state
                if retry:
                    self.state["terminal"].pop(fingerprint, None)
                    self.state.pop("pending", None)
                    self.state["phase"] = "idle"
                    for kind in ("failed", "incompatible"):
                        self.state["notices"].pop(kind + ":" + fingerprint, None)
                previous = self.state["terminal"].get(fingerprint)
                if previous:
                    self.state.update(phase=previous["phase"], reason=previous["reason"])
                    self.save()
                    return self.state
                candidate = self.state.get("candidate", {})
                if candidate.get("fingerprint") != fingerprint:
                    self.observe_change(snapshot)
                    return self.state
                candidate["observations"] += 1
                stable_seconds = getattr(self.backend, "stable_seconds", DEFAULT_STABLE_SECONDS)
                if self.clock() - candidate["first_seen"] < stable_seconds:
                    self.save()
                    return self.state
                pending = self.state.get("pending")
                if not pending:
                    if self.state.get("phase") == "building":
                        self.terminal("failed", fingerprint, "A previous build was interrupted; run run-once --retry to retry.")
                        return self.state
                    inspection = self.backend.inspect(snapshot)
                    if not inspection.get("compatible"):
                        self.terminal("incompatible", fingerprint, safe_error(inspection.get("reason", "Compatibility checks failed.")))
                        return self.state
                    output = self.directory / "builds" / (timestamp().replace(":", "-") + "-" + uuid.uuid4().hex[:8])
                    private_directory(output.parent)
                    self.state.update(phase="building", build_attempt=str(output))
                    self.save()
                    self.event("build_started", fingerprint=fingerprint, directory=str(output))
                    build = self.backend.build(snapshot, output)
                    fresh = self.backend.snapshot()
                    if self.fingerprint(fresh) != fingerprint:
                        self.event("build_superseded", fingerprint=fingerprint, directory=str(output))
                        self.observe_change(fresh)
                        return self.state
                    pending = {"fingerprint": fingerprint, "snapshot": snapshot, "build": build}
                    self.state.update(phase="waiting_for_exit", pending=pending)
                    self.save()
                    self.event("build_prepared", fingerprint=fingerprint, directory=str(output))
                self.progress("waiting_for_exit")
                request_close=getattr(self.backend,"request_close",None)
                if request_close:request_close(pending["build"])
                allowed=getattr(self.backend,'install_allowed',lambda:True)()
                if not allowed:
                    self.state['phase']='waiting_for_exit';self.save();return self.state
                if self.backend.is_running():
                    self.state["phase"] = "waiting_for_exit"
                    self.save()
                    watch_exit=getattr(self.backend,"watch_exit",None)
                    if watch_exit:watch_exit(pending["build"])
                    return self.state
                fresh = self.backend.snapshot()
                if self.fingerprint(fresh) != fingerprint:
                    self.event("install_superseded", fingerprint=fingerprint)
                    self.observe_change(fresh)
                    return self.state
                self.progress("installing")
                self.state["phase"] = "installing"
                self.save()
                result = self.backend.install(pending["build"], snapshot)
                installed_fingerprint = result.get("installed_fingerprint")
                if not isinstance(installed_fingerprint, str) or not installed_fingerprint:
                    raise ValueError("Backend installation returned no installed fingerprint")
                self.state.update(phase="installed", installed={"fingerprint": installed_fingerprint,
                                  "source_fingerprint": fingerprint, "build": pending["build"],
                                  "at": timestamp(), "result": result})
                self.state.pop("pending", None)
                self.state.pop("reason", None)
                self.persist_baseline(result.get("baseline"))
                self.save()
                self.event("installed", fingerprint=fingerprint, installed_fingerprint=installed_fingerprint)
                self.progress("completed")
                self.notify("installed", fingerprint, "The Codex update was checked and the DeepSeek patch was installed. Open Codex normally.")
            except Exception as error:
                import patch as core
                if isinstance(error,core.AppRunningError) and self.state.get('pending'):
                    self.state.update(phase='waiting_for_exit')
                    self.state.pop('reason',None)
                    self.save()
                    self.event('reopened_before_install',fingerprint=fingerprint)
                    watch_exit=getattr(self.backend,'watch_exit',None)
                    if watch_exit:watch_exit(self.state['pending']['build'])
                    return self.state
                reason = safe_error(error)
                if fingerprint == "snapshot-unavailable":
                    # An updater may temporarily remove the app. Retry reads next tick.
                    self.state.update(phase="unavailable", reason=reason)
                    self.save()
                    error_key = hashlib.sha256(reason.encode()).hexdigest()
                    self.notify("unavailable", error_key, "The Codex update monitor needs attention. " + reason)
                else:
                    self.terminal("failed", fingerprint, reason)
            self.save()
            return self.state


def default_directory():
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "CodexDeepSeekPatch/monitor"
    return Path.home() / "Library/Application Support/CodexDeepSeekPatch/monitor"


def service_label(directory):
    digest = hashlib.sha256(str(Path(directory).resolve()).encode()).hexdigest()[:12]
    return "com.codex.deepseek-patch.monitor." + digest


def run_requested(instance, retry=False, sleep=time.sleep):
    """Continue a single explicit click after the candidate stability interval."""
    from update_progress import Progress
    result=instance.run_once(retry=retry)
    if result.get('phase')=='stabilizing' and Progress(instance.directory).requested():
        delay=max(0,getattr(instance.backend,'stable_seconds',DEFAULT_STABLE_SECONDS)-
                  (instance.clock()-result['candidate']['first_seen']))
        sleep(min(delay,DEFAULT_STABLE_SECONDS))
        result=instance.run_once()
    return result


def service_command(directory, python=None):
    executable = Path(python or sys.executable)
    if sys.platform == "win32" and executable.name.lower() == "python.exe":
        windowless = executable.with_name("pythonw.exe")
        if windowless.is_file():
            executable = windowless
    return [str(executable), str(Path(directory).resolve() / "runtime/monitor.py"),
            "--state-dir", str(Path(directory).resolve()), "run-once", "--quiet"]


def launchd_plist(directory, interval, python=None):
    settings = load_json(Path(directory) / "settings.json", {})
    executable = python or settings.get("python_path") or sys.executable
    bins = [str(Path(settings["node_path"]).parent)] if settings.get("node_path") else []
    bins += [str(Path(executable).parent), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return {"Label": service_label(directory), "ProgramArguments": service_command(directory, executable),
            "EnvironmentVariables": {"PATH": ":".join(dict.fromkeys(bins))},
            "RunAtLoad": True, "StartInterval": interval, "ProcessType": "Standard",
            "StandardOutPath": str(Path(directory) / "scheduler.stdout.log"),
            "StandardErrorPath": str(Path(directory) / "scheduler.stderr.log")}


def prepare_background_work():
    # Already-loaded older launchd definitions keep their Background class
    # after a runtime update. Restore normal policy for this process and its
    # children without stopping the job or changing other applications.
    if sys.platform == "darwin":
        subprocess.run(["/usr/sbin/taskpolicy", "-B", "-p", str(os.getpid())],
                       check=True, capture_output=True, timeout=20)


def windows_identity_sid():
    script = "$ErrorActionPreference='Stop'; [Security.Principal.WindowsIdentity]::GetCurrent().User.Value"
    value = subprocess.check_output(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                    text=True, encoding="utf-8", timeout=20, **background_options()).strip()
    if not re.fullmatch(r"S-\d+(?:-\d+)+", value):
        raise ValueError("Windows returned an invalid current-user security identifier")
    return value


def windows_task_xml(directory, interval, python=None, now=None, identity_sid=None):
    ns = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    ET.register_namespace("", ns)
    def child(parent, name, value=None, **attrs):
        node = ET.SubElement(parent, "{" + ns + "}" + name, attrs)
        if value is not None:
            node.text = str(value)
        return node
    task = ET.Element("{" + ns + "}Task", {"version": "1.2"})
    triggers = child(task, "Triggers")
    identity = identity_sid or windows_identity_sid()
    if not re.fullmatch(r"S-\d+(?:-\d+)+", identity):
        raise ValueError("Task Scheduler requires a valid current-user security identifier")
    logon = child(triggers, "LogonTrigger")
    child(logon, "Enabled", "true")
    child(logon, "UserId", identity)
    timer = child(triggers, "TimeTrigger")
    repetition = child(timer, "Repetition")
    child(repetition, "Interval", "PT" + str(interval) + "S")
    child(repetition, "StopAtDurationEnd", "false")
    child(timer, "StartBoundary", (now or dt.datetime.now()).replace(microsecond=0).isoformat())
    child(timer, "Enabled", "true")
    principals = child(task, "Principals")
    principal = child(principals, "Principal", id="Author")
    child(principal, "UserId", identity)
    child(principal, "LogonType", "InteractiveToken")
    child(principal, "RunLevel", "LeastPrivilege")
    settings = child(task, "Settings")
    for name, value in (("MultipleInstancesPolicy", "IgnoreNew"), ("DisallowStartIfOnBatteries", "false"),
                        ("StopIfGoingOnBatteries", "false"), ("StartWhenAvailable", "true"),
                        ("ExecutionTimeLimit", "PT1H")):
        child(settings, name, value)
    settings_value = load_json(Path(directory) / "settings.json", {})
    command = service_command(directory, python or settings_value.get("python_path"))
    actions = child(task, "Actions", Context="Author")
    execute = child(actions, "Exec")
    child(execute, "Command", command[0])
    child(execute, "Arguments", subprocess.list2cmdline(command[1:]))
    child(execute, "WorkingDirectory", str(Path(directory).resolve()))
    return ET.tostring(task, encoding="utf-16", xml_declaration=True).decode("utf-16")


def windows_xml_text(raw):
    # schtasks can emit OEM/UTF-8 bytes while declaring UTF-16 in the XML.
    # Decode the actual byte stream before asking ElementTree to read its text.
    if isinstance(raw, str):
        return raw
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    if raw.startswith(b"<\x00"):
        return raw.decode("utf-16-le")
    if raw.startswith(b"\x00<"):
        return raw.decode("utf-16-be")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("mbcs" if os.name == "nt" else "latin-1", errors="replace")


def migrate_windows_working_directory(directory):
    """Move an existing task outside the runtime that future updates replace."""
    if sys.platform != "win32":
        return False
    directory = Path(directory).resolve()
    label = service_label(directory)
    result = subprocess.run(["schtasks.exe", "/Query", "/TN", label, "/XML"], capture_output=True, **background_options())
    if result.returncode:
        return False  # An unregistered monitor stays unregistered.
    root = ET.fromstring(windows_xml_text(result.stdout))
    working = root.find("{*}Actions/{*}Exec/{*}WorkingDirectory")
    if working is None or not working.text or Path(working.text).resolve() != directory / "runtime":
        return False
    # Preserve triggers, enablement, identity, actions and all unrelated settings.
    backup = directory / ("task-before-cwd-" + uuid.uuid4().hex[:12] + ".xml")
    backup.write_bytes(result.stdout)
    working.text = str(directory)
    definition = directory / "task-cwd-migration.xml"
    definition.write_bytes(ET.tostring(root, encoding="utf-16", xml_declaration=True))
    subprocess.run(["schtasks.exe", "/Create", "/TN", label, "/XML", str(definition), "/F"],
                   check=True, capture_output=True, **background_options())
    return True


class Scheduler:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.label = service_label(directory)

    @property
    def plist(self):
        return Path.home() / "Library/LaunchAgents" / (self.label + ".plist")

    def status(self):
        if sys.platform == "darwin":
            result = subprocess.run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{self.label}"],
                                    capture_output=True, text=True)
            return {"scheduler": "launchd", "enabled": result.returncode == 0, "definition": str(self.plist)}
        if sys.platform == "win32":
            result = subprocess.run(["schtasks.exe", "/Query", "/TN", self.label, "/XML"], capture_output=True, **background_options())
            registered = result.returncode == 0
            enabled = registered
            if registered:
                try:
                    xml = ET.fromstring(windows_xml_text(result.stdout))
                    enabled = xml.findtext("{*}Settings/{*}Enabled", "true").lower() != "false"
                except (ET.ParseError, ValueError):
                    pass
            return {"scheduler": "task-scheduler", "enabled": enabled, "registered": registered, "name": self.label}
        raise ValueError("Native scheduling supports macOS and Windows only")

    def enable(self, interval):
        if sys.platform == "darwin":
            self.plist.parent.mkdir(parents=True, exist_ok=True)
            previous = self.plist.read_bytes() if self.plist.exists() else None
            loaded = self.status()["enabled"]
            if previous is not None:
                backup = self.directory / ("launchd-backup-" + uuid.uuid4().hex[:12] + ".plist")
                backup.write_bytes(previous)
                backup.chmod(0o600)
            if loaded:
                subprocess.run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{self.label}"], check=True, capture_output=True)
            try:
                self.plist.write_bytes(plistlib.dumps(launchd_plist(self.directory, interval)))
                self.plist.chmod(0o600)
                subprocess.run(["/bin/launchctl", "enable", f"gui/{os.getuid()}/{self.label}"], check=True, capture_output=True)
                subprocess.run(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(self.plist)], check=True, capture_output=True)
            except BaseException:
                if previous is not None:
                    self.plist.write_bytes(previous)
                    if loaded:
                        subprocess.run(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(self.plist)], check=True, capture_output=True)
                else:
                    self.plist.unlink(missing_ok=True)
                raise
        elif sys.platform == "win32":
            task = self.directory / "task.xml"
            task.write_text(windows_task_xml(self.directory, interval), encoding="utf-16")
            subprocess.run(["schtasks.exe", "/Create", "/TN", self.label, "/XML", str(task), "/F"],
                           check=True, capture_output=True, **background_options())
        else:
            raise ValueError("Native scheduling supports macOS and Windows only")

    def disable(self):
        if sys.platform == "darwin":
            subprocess.run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{self.label}"], capture_output=True)
            self.plist.unlink(missing_ok=True)
        elif sys.platform == "win32":
            if self.status()["registered"]:
                subprocess.run(["schtasks.exe", "/Delete", "/TN", self.label, "/F"], check=True, capture_output=True, **background_options())
        else:
            raise ValueError("Native scheduling supports macOS and Windows only")


def stage_runtime(directory):
    import patch
    names = getattr(patch, "SERVICE_RUNTIME_FILES", ("monitor.py", "patch.py", "route.js", "scripts/__init__.py", "scripts/test_boot.py"))
    directory = private_directory(directory)
    stage = Path(tempfile.mkdtemp(prefix="runtime-stage-", dir=directory))
    try:
        for name in names:
            source = (ROOT / name).resolve()
            if Path(name).is_absolute() or ".." in Path(name).parts or not source.is_relative_to(ROOT) or not source.is_file():
                raise ValueError("Invalid service runtime file: " + name)
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        runtime = directory / "runtime"
        backup = None
        if runtime.exists():
            backup = directory / ("runtime-backup-" + uuid.uuid4().hex[:12])
            os.replace(runtime, backup)
        try:
            os.replace(stage, runtime)
        except BaseException:
            if backup is not None:
                os.replace(backup, runtime)
            raise
        return backup
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def enable(args):
    directory = private_directory(args.state_dir.resolve())
    if args.interval < 60:
        raise ValueError("The scheduling interval must be at least 60 seconds")
    if sys.platform not in ("darwin", "win32"):
        raise ValueError("Native scheduling supports macOS and Windows only")
    settings_path = directory / "settings.json"
    existing = load_json(settings_path, {})
    import patch
    from updater import edition
    channel=edition()['edition']
    installed_edition=channel
    baseline=getattr(args,'baseline',None) or existing.get('baseline')
    if baseline:
        record=load_json(Path(baseline)/'manifest.json',{})
        installed_edition=record.get('edition','proxy' if record.get('proxy') else 'ds')
        if record and not patch.runtime_accepts(channel,installed_edition):raise ValueError('The baseline belongs to another edition; use its matching update channel')
    if not patch.runtime_accepts(channel,existing.get('edition',channel)):raise ValueError('This monitor directory is pinned to another installation')
    if baseline and existing.get('edition') and existing['edition']!=installed_edition:
        raise ValueError('The baseline and monitor belong to different installations')
    channel=existing.get('edition',installed_edition)
    settings = {**existing, "format": FORMAT, "interval": args.interval,
                "bundled_catalog": True, "enabled_at": timestamp()}
    settings.update(edition=channel,state_dir=str(directory),auto_update=not getattr(args,'no_code_updates',False),
                    model_sync=not getattr(args,'no_model_sync',False))
    architecture=getattr(patch,'ARCHITECTURE',None) or existing.get('architecture')
    if architecture:
        settings['architecture']=architecture
    for name in ("app", "codex_home", "baseline", "keychain_account", "keychain_service"):
        value = getattr(args, name, None)
        if value is not None:
            settings[name] = str(value.resolve()) if isinstance(value, Path) else value
    if not settings.get("app") and baseline:
        installed = load_json(Path(baseline)/'manifest.json', {})
        if installed.get('architecture') == 'official-client-v1':
            settings['app'] = installed.get('target')
    if not settings.get("app"):
        raise ValueError("Specify --app on first enable")
    settings.setdefault("codex_home", str(Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).resolve()))
    settings.setdefault("keychain_account", __import__("getpass").getuser())
    settings.setdefault("keychain_service", "Codex DeepSeek API Key")
    settings["python_path"] = str(Path(sys.executable).resolve())
    node = shutil.which("node")
    if not node and architecture!='official-client-v1':
        raise ValueError("Node.js is required for offline JavaScript syntax validation")
    if node:
        settings["node_path"] = str(Path(node).resolve())
    elif architecture=='official-client-v1':
        settings.pop('node_path',None)
    # Registration validates local app identity; network discovery starts on the
    # first scheduled tick, after settings/runtime registration is committed.
    if baseline and load_json(Path(baseline)/'manifest.json',{}).get('architecture')=='official-client-v1':
        from official_client_update import NativeMonitorBackend
        backend = NativeMonitorBackend(settings)
    else:
        backend = patch.MonitorBackend(settings)
    backend.snapshot()  # Validate the selected target before registering any service.
    with state_lock(directory) as locked:
        if not locked:
            raise ValueError("Monitor is busy; try enabling after the current check finishes")
        previous_settings = settings_path.read_bytes() if settings_path.exists() else None
        state_path = directory / "state.json"
        previous_state = state_path.read_bytes() if state_path.exists() else None
        identity_fields = ("app", "codex_home", "baseline", "keychain_account", "keychain_service", "architecture")
        reset_state = bool(existing) and any(existing.get(key) != settings.get(key) for key in identity_fields)
        if reset_state and previous_state is not None:
            shutil.copy2(state_path, directory / ("state-backup-" + uuid.uuid4().hex[:12] + ".json"))
        if previous_settings is not None:
            shutil.copy2(settings_path, directory / ("settings-backup-" + uuid.uuid4().hex[:12] + ".json"))
        previous_runtime = stage_runtime(directory)
        try:
            atomic_json(settings_path, settings)
            if reset_state:
                atomic_json(state_path, {"format": FORMAT, "phase": "idle", "terminal": {}, "notices": {}})
            Scheduler(directory).enable(args.interval)
        except BaseException:
            if previous_settings is None:
                settings_path.unlink(missing_ok=True)
            else:
                # The exact prior document is preserved, including formatting.
                fd, temporary = tempfile.mkstemp(prefix="settings-restore-", dir=directory)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(previous_settings)
                os.replace(temporary, settings_path)
            if reset_state:
                if previous_state is None:
                    state_path.unlink(missing_ok=True)
                else:
                    fd, temporary = tempfile.mkstemp(prefix="state-restore-", dir=directory)
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(previous_state)
                    os.replace(temporary, state_path)
            runtime = directory / "runtime"
            failed = directory / ("runtime-failed-" + uuid.uuid4().hex[:12])
            os.replace(runtime, failed)
            if previous_runtime is not None:
                os.replace(previous_runtime, runtime)
            raise
    return {"enabled": True, "interval": args.interval, "state_dir": str(directory),
            "service": Scheduler(directory).status()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=default_directory())
    subs = parser.add_subparsers(dest="action", required=True)
    p = subs.add_parser("enable", help="Enable per-user checks at login and every five minutes")
    p.add_argument("--app", type=Path)
    p.add_argument("--codex-home", type=Path)
    p.add_argument("--baseline", "--adopt-existing", type=Path, help="Existing installed patch build to adopt")
    p.add_argument("--keychain-account")
    p.add_argument("--keychain-service")
    p.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="Seconds, minimum 60")
    p.add_argument('--no-code-updates',action='store_true')
    p.add_argument('--no-model-sync',action='store_true')
    p = subs.add_parser("run-once", help="Check once; unchanged versions remain quiet")
    p.add_argument("--retry", action="store_true", help="Explicitly retry a failed or incompatible fingerprint")
    p.add_argument("--quiet", action="store_true", help="For native scheduling: write events without polling output")
    p.add_argument('--sync-now',action='store_true',help='Refresh the pinned code channel and model specifications now')
    p.add_argument('--install-ready',action='store_true',help=argparse.SUPPRESS)
    p=subs.add_parser('model-options',help='Keep a model hidden or preserve a smaller context across synchronization')
    p.add_argument('--model',required=True)
    p.add_argument('--context-window',type=int)
    p.add_argument('--compact-limit',type=int)
    visibility=p.add_mutually_exclusive_group();visibility.add_argument('--hide',action='store_true');visibility.add_argument('--show',action='store_true')
    p.add_argument('--reset-context',action='store_true',help='Return to the provider maximum')
    subs.add_parser("status", help="Print machine-readable service and update state")
    subs.add_parser("disable", help="Disable checks, preserving builds, logs and recovery data")
    args = parser.parse_args(argv)
    try:
        if args.action == "enable":
            result = enable(args)
        elif args.action == "disable":
            Scheduler(args.state_dir).disable()
            result = {"enabled": False, "state_dir": str(args.state_dir.resolve())}
        elif args.action == "status":
            result = {"service": Scheduler(args.state_dir).status(),
                      "settings": load_json(args.state_dir / "settings.json", {}),
                      "state": load_json(args.state_dir / "state.json", {}),
                      "event_log": str(args.state_dir.resolve() / "events.jsonl")}
            result['updates']=load_json(args.state_dir/'updates.json',{})
            result['model_preferences']=load_json(args.state_dir/'model-preferences.json',{})
        elif args.action == 'model-options':
            from model_sync import validate_preferences
            with state_lock(args.state_dir) as locked:
                if not locked:raise ValueError('Monitor is busy; retry after its current check')
                path=args.state_dir/'model-preferences.json';value=load_json(path,{})
                for name,number in [('context_windows',args.context_window),('compact_limits',args.compact_limit)]:
                    if number is not None:value.setdefault(name,{})[args.model]=number
                if args.reset_context:
                    value.setdefault('context_windows',{})[args.model]=None
                    value.setdefault('compact_limits',{})[args.model]=None
                hidden=set(value.get('hidden',[]))
                if args.hide:hidden.add(args.model)
                if args.show:hidden.discard(args.model)
                value['hidden']=sorted(hidden);validate_preferences(value);atomic_json(path,value)
                updates=load_json(args.state_dir/'updates.json',{});updates['models_checked']=0;atomic_json(args.state_dir/'updates.json',updates)
                result={'model':args.model,'preferences':value,'applies':'next synchronization, after normal app exit'}
        else:
            prepare_background_work()
            from updater import reconcile_native_activation
            reconcile_native_activation(args.state_dir)
            settings = load_json(args.state_dir / "settings.json")
            if settings is None:
                raise ValueError("Monitor settings are missing; run enable first")
            settings['state_dir']=str(args.state_dir.resolve())
            if getattr(args,'install_ready',False):
                # An exit-triggered installation uses already validated local
                # artifacts; network discovery must not consume the quit gap.
                settings.update(auto_update=False,model_sync=False,force_sync=False)
            if getattr(args,'sync_now',False):settings['force_sync']=True
            saved_state = load_json(args.state_dir / "state.json", {})
            baseline = saved_state.get("installed", {}).get("baseline")
            if baseline:
                settings["baseline"] = baseline
            if saved_state.get("phase") == "installing":
                recovery = saved_state.get("pending", {}).get("build", {}).get("directory")
                if recovery:
                    settings["recovery_build"] = recovery
            instance=Monitor(args.state_dir, CoreBackend(settings))
            result = run_requested(instance, retry=args.retry)
            if result.get('phase') in ('installed','idle'):
                from management import maintenance
                maintenance(args.state_dir)
            if result.get('phase') != 'busy' and (args.state_dir / 'monitor.request.json').exists():
                # The running owner drains the next coalesced request after its
                # lock is released. Contending callers do not spawn a loop.
                wake_monitor(args.state_dir)
        if not getattr(args, "quiet", False):
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("phase") in ("failed", "incompatible", "unavailable") else 0
    except Exception as error:
        print(json.dumps({"error": safe_error(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
