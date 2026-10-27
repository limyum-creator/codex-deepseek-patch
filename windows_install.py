"""Transactional installation of a separate Windows app copy and named shortcut."""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid

import windows_support as win


class WindowsInstallError(ValueError):
    pass


def _require(ok, message):
    if not ok:
        raise WindowsInstallError(message)


def _atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _json(path, data):
    _atomic(path, (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def _shortcut_path():
    _require(os.environ.get("APPDATA"), "APPDATA is missing")
    return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Codex DeepSeek.lnk"


def _make_shortcut(path, target):
    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"
    script = ("$w=New-Object -ComObject WScript.Shell; $s=$w.CreateShortcut(" + quote(path) + "); "
              "$s.TargetPath=" + quote(target / "ChatGPT.exe") + "; "
              "$s.WorkingDirectory=" + quote(target) + "; "
              "$s.Description='Codex with local DeepSeek model selection'; $s.Save()")
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                   check=True, capture_output=True, **win.background_options())
    _require(path.is_file(), "Shortcut creation did not produce a file")


def _snapshot(path):
    _require(not path.is_symlink(), "Refusing a symlink at " + str(path))
    return path.read_bytes() if path.exists() else None


def _restore_file(path, data):
    if data is None:
        if path.exists():
            path.unlink()
    else:
        _atomic(path, data)


def _paths(manifest, directory):
    directory = Path(directory).resolve()
    source = Path(manifest["target"]).resolve()
    target = Path(manifest["deployment_target"])
    _require(not target.is_symlink() and not getattr(target, "is_junction", lambda: False)(),
             "Managed app target must not be a link or junction")
    target = target.resolve()
    _require(not win.is_store_app(target), "Managed deployment cannot modify a Store package")
    _require(target != source and not target.is_relative_to(source) and not source.is_relative_to(target),
             "Managed target overlaps the original installation")
    _require(not target.is_relative_to(directory) and not directory.is_relative_to(target),
             "Build and managed target directories overlap")
    target.parent.mkdir(parents=True, exist_ok=True)
    _require(os.stat(directory).st_dev == os.stat(target.parent).st_dev,
             "Build and managed app must be on the same volume for recoverable directory moves")
    return directory, source, target


def _stopped(source, target):
    win.require_stopped(source, read_only_source=True)
    if target.exists():
        win.require_stopped(target)


def install(manifest, directory, config, before, after, commit):
    """Install a prepared copy; commit() verifies target/config and saves manifest."""
    directory, source, target = _paths(manifest, directory)
    config = Path(config)
    _require(manifest.get("status") == "prepared", "Build is not prepared")
    _require(config.read_text(encoding="utf-8") == before if config.exists() else before == "",
             "Configuration changed before Windows installation")
    _stopped(source, target)
    stage = directory / "patched.app"
    win.verify_integrity(stage, manifest["patched_asar"])
    previous = directory / "windows-previous.app"
    _require(not previous.exists(), "Previous managed app backup already exists")
    shortcut = _shortcut_path()
    shortcut.parent.mkdir(parents=True, exist_ok=True)
    old_shortcut, old_config = _snapshot(shortcut), _snapshot(config)
    manifest_path = directory / "manifest.json"
    old_manifest_bytes = _snapshot(manifest_path)
    old_manifest = copy.deepcopy(manifest)
    shortcut_backup = directory / "windows-shortcut-before.lnk"
    if old_shortcut is not None:
        _require(not shortcut_backup.exists() or shortcut_backup.read_bytes() == old_shortcut,
                 "Existing shortcut backup differs from the current shortcut")
        if not shortcut_backup.exists():
            _atomic(shortcut_backup, old_shortcut)
    staged = target.parent / (target.name + ".staged-" + uuid.uuid4().hex)
    shortcut_staged = shortcut.parent / ("Codex DeepSeek.staged-" + uuid.uuid4().hex + ".lnk")
    journal_path = directory / "windows-install-journal.json"
    journal = {"operation": "install", "source": str(source), "target": str(target),
               "staging": str(staged), "backup": str(previous), "phase": "preparing"}
    moved_previous = moved_stage = config_written = shortcut_written = manifest_touched = False
    try:
        _json(journal_path, journal)
        win.clone_app(stage, staged)
        win.verify_integrity(staged, manifest["patched_asar"])
        _make_shortcut(shortcut_staged, target)
        _stopped(source, target)
        _require((_snapshot(config) or b"") == (old_config or b""), "Configuration changed while copying the app")
        _require(_snapshot(shortcut) == old_shortcut, "Shortcut changed while copying the app")
        journal["phase"] = "prepared"
        _json(journal_path, journal)
        if target.exists():
            os.replace(target, previous)
            moved_previous = True
        os.replace(staged, target)
        moved_stage = True
        journal["phase"] = "app-moved"
        _json(journal_path, journal)
        _atomic(config, after.encode("utf-8"))
        config_written = True
        os.replace(shortcut_staged, shortcut)
        shortcut_written = True
        manifest_touched = True
        manifest["windows_install"] = {"previous_app": str(previous) if moved_previous else None,
            "shortcut": str(shortcut), "shortcut_backup": str(shortcut_backup) if old_shortcut is not None else None,
            "shortcut_hash": hashlib.sha256(shortcut.read_bytes()).hexdigest(),
            "shortcut_before_hash": hashlib.sha256(old_shortcut).hexdigest() if old_shortcut is not None else None,
            "config_before_hash": hashlib.sha256(before.encode("utf-8")).hexdigest()}
        commit()
        journal["phase"] = "committed"
        _json(journal_path, journal)
    except BaseException as original:
        errors = []
        def attempt(label, action):
            try:
                action()
            except BaseException as error:
                errors.append(label + ": " + str(error))
        if moved_stage:
            failed = directory / ("windows-failed-" + uuid.uuid4().hex + ".app")
            attempt("remove failed deployment", lambda: os.replace(target, failed))
        if moved_previous:
            attempt("restore previous app", lambda: os.replace(previous, target))
        if config_written:
            attempt("restore configuration", lambda: _restore_file(config, old_config))
        if shortcut_written:
            attempt("restore shortcut", lambda: _restore_file(shortcut, old_shortcut))
        if manifest_touched:
            attempt("restore manifest", lambda: _restore_file(manifest_path, old_manifest_bytes))
        manifest.clear()
        manifest.update(old_manifest)
        journal.update(phase="rollback-failed" if errors else "rolled-back", error=str(original), rollback_errors=errors)
        attempt("record rollback journal", lambda: _json(journal_path, journal))
        if errors:
            raise WindowsInstallError("Windows installation failed; recovery needs attention: " + "; ".join(errors)) from original
        raise


def restore(manifest, directory):
    """Restore the previous managed copy, shortcut and unchanged user config."""
    directory, source, target = _paths(manifest, directory)
    _require(manifest.get("status") == "installed", "Build is not installed")
    _stopped(source, target)
    win.verify_integrity(target, manifest["patched_asar"])
    record = manifest["windows_install"]
    config = Path(manifest["codex_home"]) / "config.toml"
    current = _snapshot(config)
    _require(current is not None and hashlib.sha256(config.read_text(encoding="utf-8").encode()).hexdigest() == manifest["installed_config_hash"],
             "Configuration changed after installation; review before restoring")
    shortcut = Path(record["shortcut"])
    shortcut_current = _snapshot(shortcut)
    _require(shortcut_current is not None and hashlib.sha256(shortcut_current).hexdigest() == record["shortcut_hash"],
             "Managed shortcut changed after installation; review before restoring")
    previous = Path(record["previous_app"]) if record["previous_app"] else None
    if previous:
        _require(previous == directory / "windows-previous.app" and previous.is_dir(), "Previous app backup is missing")
    shortcut_backup = Path(record["shortcut_backup"]) if record["shortcut_backup"] else None
    _require(shortcut_backup is None or shortcut_backup == directory / "windows-shortcut-before.lnk",
             "Unexpected shortcut backup path")
    old_shortcut = shortcut_backup.read_bytes() if shortcut_backup else None
    _require(old_shortcut is None or hashlib.sha256(old_shortcut).hexdigest() == record["shortcut_before_hash"],
             "Shortcut backup changed after installation")
    config_before = (directory / "config-before.toml").read_bytes() if manifest["config_existed"] else None
    _require(config_before is None or hashlib.sha256(config_before.decode("utf-8").replace("\r\n", "\n").encode()).hexdigest() == record["config_before_hash"],
             "Configuration backup changed after installation")
    manifest_path = directory / "manifest.json"
    old_manifest = _snapshot(manifest_path)
    original_manifest = copy.deepcopy(manifest)
    removed = directory / ("windows-restored-patched-" + uuid.uuid4().hex + ".app")
    moved_current = moved_previous = False
    journal = {"operation": "restore", "target": str(target), "phase": "preparing"}
    journal_path = directory / "windows-restore-journal.json"
    try:
        _json(journal_path, journal)
        os.replace(target, removed)
        moved_current = True
        if previous:
            os.replace(previous, target)
            moved_previous = True
        _restore_file(config, config_before)
        _restore_file(shortcut, old_shortcut)
        manifest.update(status="restored", restored_at=dt.datetime.now(dt.timezone.utc).isoformat())
        _json(manifest_path, manifest)
        journal["phase"] = "committed"
        _json(journal_path, journal)
    except BaseException as original:
        errors = []
        for label, action in [
            ("previous app", lambda: os.replace(target, previous) if moved_previous else None),
            ("patched app", lambda: os.replace(removed, target) if moved_current else None),
            ("configuration", lambda: _restore_file(config, current)),
            ("shortcut", lambda: _restore_file(shortcut, shortcut_current)),
            ("manifest", lambda: _restore_file(manifest_path, old_manifest)),
        ]:
            try:
                action()
            except BaseException as error:
                errors.append(label + ": " + str(error))
        manifest.clear()
        manifest.update(original_manifest)
        journal.update(phase="rollback-failed" if errors else "rolled-back", error=str(original), rollback_errors=errors)
        _json(journal_path, journal)
        if errors:
            raise WindowsInstallError("Windows restore needs recovery: " + "; ".join(errors)) from original
        raise
    print("Restored the previous managed app, shortcut and configuration. The original Store app is unchanged.")
