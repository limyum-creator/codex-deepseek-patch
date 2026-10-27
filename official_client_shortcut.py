"""Move an unchanged legacy Windows shortcut to the original vendor app.

Only the exact path/hash owned by the legacy installation is eligible. COM
creates a staged link; intent-written installation journals own activation and
rollback. Reading or writing a link never launches its target.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import tempfile

import patch as p


def _powershell(script):
    result = p.run(['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand',
                    base64.b64encode(script.encode('utf-16le')).decode()], capture_output=True, timeout=30,
                   **p.background_options())
    return json.loads(result.stdout.decode('utf-8-sig'))


def official_entry(app):
    """Prefer the registered package AUMID, which survives vendor updates."""
    from windows_support import is_store_app
    app = Path(app).resolve()
    if not is_store_app(app):
        return {'target': str(app / 'ChatGPT.exe'), 'arguments': '', 'working_directory': str(app),
                'icon': str(app / 'ChatGPT.exe') + ',0'}
    location = str(app.parent).replace("'", "''")
    script = r'''$ErrorActionPreference='Stop';[Console]::OutputEncoding=[Text.UTF8Encoding]::new();
$loc='__LOCATION__';$packages=@(Get-AppxPackage -Name OpenAI.Codex | Where-Object {$_.InstallLocation -eq $loc});
if($packages.Count -ne 1){throw 'Cannot identify one registered official Codex package'};
$pkg=$packages[0];[xml]$m=Get-Content -LiteralPath (Join-Path $loc 'AppxManifest.xml');
$a=@($m.Package.Applications.Application | Where-Object {$_.Executable.Replace('/','\') -ieq 'app\ChatGPT.exe'});
if($a.Count -ne 1){throw 'Cannot identify the official Codex application entry'};
@{family=[string]$pkg.PackageFamilyName;app_id=[string]$a[0].Id}|ConvertTo-Json -Compress'''
    identity = _powershell(script.replace('__LOCATION__', location))
    family, app_id = identity['family'], identity['app_id']
    p.require(family.startswith('OpenAI.Codex_') and app_id and
              all(char.isalnum() or char in '._-' for char in family + app_id), 'Unexpected official application entry')
    system = Path(os.environ['SystemRoot'])
    return {'target': str(system / 'explorer.exe'), 'arguments': 'shell:AppsFolder\\' + family + '!' + app_id,
            'working_directory': str(system), 'icon': str(app / 'ChatGPT.exe') + ',0',
            'app_user_model_id': family + '!' + app_id}


def write_link(path, entry):
    values = {'path': str(path), **entry}
    encoded = base64.b64encode(json.dumps(values).encode()).decode()
    script = r'''$ErrorActionPreference='Stop';[Console]::OutputEncoding=[Text.UTF8Encoding]::new();
$v=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__VALUES__'))|ConvertFrom-Json;
$s=New-Object -ComObject WScript.Shell;$l=$s.CreateShortcut($v.path);$l.TargetPath=$v.target;
$l.Arguments=$v.arguments;$l.WorkingDirectory=$v.working_directory;$l.IconLocation=$v.icon;$l.Save();
@{ok=$true}|ConvertTo-Json -Compress'''
    p.require(_powershell(script.replace('__VALUES__', encoded)).get('ok'), 'Official shortcut staging failed')


def read_link(path):
    """Read COM fields for diagnostics/tests without invoking the shortcut."""
    encoded = base64.b64encode(str(path).encode()).decode()
    script = r'''$ErrorActionPreference='Stop';[Console]::OutputEncoding=[Text.UTF8Encoding]::new();
$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PATH__'));
$s=New-Object -ComObject WScript.Shell;$l=$s.CreateShortcut($p);
@{target=[string]$l.TargetPath;arguments=[string]$l.Arguments;working_directory=[string]$l.WorkingDirectory}|ConvertTo-Json -Compress'''
    return _powershell(script.replace('__PATH__', encoded))


def _atomic(path, raw):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def prepare(prior, directory, app, transaction_id):
    record = prior.get('windows_install') or {}
    if not record.get('shortcut'):
        return None
    path = Path(record['shortcut'])
    p.require(path.is_absolute() and path.suffix.lower() == '.lnk' and not path.is_symlink(),
              'Unexpected legacy shortcut path')
    if not path.exists():
        return None  # A user-deleted shortcut is not recreated.
    p.require(p.file_sha(path) == record.get('shortcut_hash'), 'The legacy shortcut changed; it was not replaced')
    directory = Path(directory)
    backup = directory / ('legacy-shortcut-before.' + transaction_id + '.lnk')
    staged = directory / ('official-shortcut.' + transaction_id + '.lnk')
    p.require(not backup.exists() and not staged.exists(), 'Shortcut recovery artifact already exists')
    _atomic(backup, path.read_bytes())
    entry = official_entry(app)
    write_link(staged, entry)
    p.require(staged.is_file() and not staged.is_symlink(), 'The official shortcut was not staged')
    return {'path': str(path), 'before_sha256': p.file_sha(backup), 'after_sha256': p.file_sha(staged),
            'backup': str(backup), 'staged': str(staged), 'entry': entry}


def activate(ownership):
    path, staged = Path(ownership['path']), Path(ownership['staged'])
    p.require(not path.is_symlink() and path.is_file(), 'The legacy shortcut disappeared or became a symlink')
    current = p.file_sha(path)
    if current == ownership['after_sha256']:
        return
    p.require(current == ownership['before_sha256'], 'The legacy shortcut changed before activation')
    p.require(staged.is_file() and not staged.is_symlink() and p.file_sha(staged) == ownership['after_sha256'],
              'The staged official shortcut changed')
    _atomic(path, staged.read_bytes())


def restore(ownership):
    path, backup = Path(ownership['path']), Path(ownership['backup'])
    p.require(path.is_file() and not path.is_symlink(), 'The owned shortcut disappeared or became a symlink')
    actual = p.file_sha(path)
    if actual == ownership['before_sha256']:
        return
    p.require(actual == ownership['after_sha256'], 'The shortcut changed independently; retain it for review')
    p.require(backup.is_file() and not backup.is_symlink() and p.file_sha(backup) == ownership['before_sha256'],
              'The legacy shortcut backup changed')
    _atomic(path, backup.read_bytes())
