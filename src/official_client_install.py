"""Transactional activation of a small adapter beside the official app.

Only the first migration restores an existing patched Mac app from its verified
official backup. Subsequent updates never write the app. Windows always uses the
registered Store installation and never copies its package.
"""
from __future__ import annotations

import base64
import copy
import datetime as dt
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import time
import uuid

import patch as p
from monitor import atomic_json, state_lock, windows_identity_sid, windows_xml_text
from official_client import ARCHITECTURE
from official_client_config import prepare_config, rollback_config, restore_config
from official_client_host import adapter_launchd_plist, adapter_windows_task_xml, healthy, health_status
from official_client_migration import _set_provider, inventory, migrate, native_session, rollback
from official_client_update import verify_official


class AdapterService:
    def __init__(self, edition, directory):
        p.require(edition in ('ds', 'proxy'), 'Unknown adapter service edition')
        self.edition, self.directory = edition, Path(directory)
        self.label = 'com.limyum.codex-model-adapter.' + edition
        self.plist = Path.home() / 'Library/LaunchAgents' / (self.label + '.plist')

    def snapshot(self):
        if sys.platform == 'darwin':
            loaded = subprocess.run(['/bin/launchctl', 'print', f'gui/{os.getuid()}/{self.label}'],
                                    capture_output=True).returncode == 0
            disabled = p.run(['/bin/launchctl', 'print-disabled', f'gui/{os.getuid()}'],
                             capture_output=True, text=True, timeout=30).stdout
            enabled = not re.search(r'"' + re.escape(self.label) + r'"\s*=>\s*true', disabled)
            return {'registered': self.plist.exists(), 'loaded': loaded, 'enabled': enabled,
                    'definition': self.plist.read_text(encoding='utf-8') if self.plist.exists() else None}
        # Task existence says nothing about its Running/Ready/Disabled state.
        # COM exposes numeric states independently of localized schtasks text.
        script = r'''$ErrorActionPreference='Stop';[Console]::OutputEncoding=[Text.UTF8Encoding]::new();
$s=New-Object -ComObject 'Schedule.Service';$s.Connect();$f=$s.GetFolder('\');
$t=@($f.GetTasks(1) | Where-Object {$_.Name -eq '__LABEL__'});
if($t.Count -eq 0){@{registered=$false;loaded=$false;enabled=$false;state=0;definition=$null}|ConvertTo-Json -Compress}
elseif($t.Count -ne 1){throw 'Ambiguous adapter task'}
else{$t=$t[0];@{registered=$true;loaded=($t.State -eq 4);enabled=[bool]$t.Enabled;state=[int]$t.State;definition=[string]$t.Xml}|ConvertTo-Json -Compress}'''
        script = script.replace('__LABEL__', self.label)
        result = p.run(['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand',
                        base64.b64encode(script.encode('utf-16le')).decode()],
                       capture_output=True, timeout=30, **p.background_options())
        return json.loads(result.stdout.decode('utf-8-sig'))

    def _windows(self, *args):
        return p.run(['schtasks.exe', *args], capture_output=True, timeout=30, **p.background_options())

    def _wait_running(self, expected):
        until = time.monotonic() + 15
        while time.monotonic() < until:
            if self.snapshot()['loaded'] is expected:
                return
            time.sleep(.1)
        raise RuntimeError('The adapter service did not reach its expected running state')

    def stop(self, snapshot):
        if not snapshot['loaded']:
            return
        if sys.platform == 'darwin':
            p.run(['/bin/launchctl', 'bootout', f'gui/{os.getuid()}/{self.label}'], capture_output=True, timeout=30)
            # bootout returns before launchd finishes terminating the old job.
            # Reusing its label immediately can fail bootstrap with EIO (5).
            # Wait for removal on updates, rollback and uninstall alike.
            self._wait_running(False)
        else:
            try:
                self._windows('/End', '/TN', self.label)
            except Exception:
                if self.snapshot()['loaded']:
                    raise
            self._wait_running(False)

    def activate(self, settings):
        previous = self.snapshot()
        if previous['definition'] is not None:
            p.require('inference_adapter.py' in previous['definition'], 'An unrelated task owns the adapter service name')
        self.stop(previous)
        if sys.platform == 'darwin':
            self.plist.parent.mkdir(parents=True, exist_ok=True)
            p.atomic_text(self.plist, plistlib.dumps(adapter_launchd_plist(settings, self.label)).decode())
            p.run(['/bin/launchctl', 'enable', f'gui/{os.getuid()}/{self.label}'], capture_output=True, timeout=30)
            p.run(['/bin/launchctl', 'bootstrap', f'gui/{os.getuid()}', str(self.plist)], capture_output=True, timeout=30)
        else:
            definition = self.directory / 'adapter-task.xml'
            definition.write_text(adapter_windows_task_xml(settings, windows_identity_sid()), encoding='utf-16')
            self._windows('/Create', '/TN', self.label, '/XML', str(definition), '/F')
            self._windows('/Change', '/TN', self.label, '/Enable')
            self._windows('/Run', '/TN', self.label)
        self._wait_ready(settings)

    def _wait_ready(self, settings, timeout=60):
        """Cold starts get a bounded deadline; failures leave safe evidence."""
        started = time.monotonic()
        until = started + timeout
        status = {'ready': False, 'reason': 'unreachable'}
        while time.monotonic() < until:
            status = health_status(settings, timeout=min(1.5, max(.01, until - time.monotonic())))
            if status['ready']:
                atomic_json(self.directory / 'service-startup.json', {
                    'ready': True, 'elapsed_seconds': round(time.monotonic() - started, 3)})
                return
            # Task Scheduler may report End/Run before the former listener has
            # released its socket. Only the new exact identity is accepted, but
            # a transient old response must not abort the handover immediately.
            time.sleep(min(.25, max(0, until - time.monotonic())))
        atomic_json(self.directory / 'service-startup.json', {
            **status, 'elapsed_seconds': round(time.monotonic() - started, 3)})
        raise RuntimeError('The native adapter service failed its identity health check: ' + status['reason'])

    def remove(self):
        current = self.snapshot()
        if current['definition'] is not None:
            p.require('inference_adapter.py' in current['definition'], 'An unrelated task owns the adapter service name')
        self.stop(current)
        if sys.platform == 'darwin':
            self.plist.unlink(missing_ok=True)
        elif current['registered']:
            self._windows('/Delete', '/TN', self.label, '/F')
        p.require(not self.snapshot()['registered'], 'The adapter service definition was not removed')

    def restore(self, previous):
        current = self.snapshot()
        comparable = ('registered', 'loaded', 'enabled', 'definition')
        if all(current.get(key) == previous.get(key) for key in comparable):
            return
        if current['definition'] is not None:
            p.require('inference_adapter.py' in current['definition'], 'An unrelated task owns the adapter service name')
        self.stop(current)
        if sys.platform == 'darwin':
            if previous['definition'] is None:
                self.plist.unlink(missing_ok=True)
            else:
                p.atomic_text(self.plist, previous['definition'])
            p.run(['/bin/launchctl', 'enable' if previous.get('enabled', True) else 'disable',
                   f'gui/{os.getuid()}/{self.label}'], capture_output=True, timeout=30)
            if previous['loaded']:
                p.run(['/bin/launchctl', 'bootstrap', f'gui/{os.getuid()}', str(self.plist)], capture_output=True, timeout=30)
        elif previous['definition'] is None:
            if current['registered']:
                self._windows('/Delete', '/TN', self.label, '/F')
        else:
            definition = self.directory / 'adapter-task-restore.xml'
            definition.write_text(previous['definition'], encoding='utf-16')
            self._windows('/Create', '/TN', self.label, '/XML', str(definition), '/F')
            if previous['loaded']:
                self._windows('/Change', '/TN', self.label, '/Enable')
                self._windows('/Run', '/TN', self.label)
                self._wait_running(True)
            self._windows('/Change', '/TN', self.label, '/Enable' if previous.get('enabled', True) else '/Disable')
        restored = self.snapshot()
        p.require(all(restored.get(key) == previous.get(key) for key in ('registered', 'loaded', 'enabled')),
                  'The previous adapter service state was not restored')

def validate_build(directory, manifest):
    directory = Path(directory).resolve()
    p.require(manifest.get('architecture') == ARCHITECTURE and manifest.get('status') in ('prepared','prepared_idle'),
              'A prepared native-client build is required')
    p.require(manifest.get('edition') == p.EDITION, 'Native build belongs to another edition')
    p.require(manifest.get('native_probe') == 'passed', 'Run the native adapter regression before installation')
    checks = {'desktop-models.json':manifest['catalog_sha256'], 'adapter-profile.json':manifest['profile_sha256'],
              'host-settings.json':manifest['host_settings_sha256'],
              **manifest['runtime_sha256']}
    for name, expected in checks.items():
        path = directory / name
        p.require(path.is_file() and not path.is_symlink() and p.file_sha(path) == expected,
                  'Native build changed after validation: ' + name)
    from inference_adapter import load_profile
    from official_client import endpoint
    profile = load_profile(directory / 'adapter-profile.json')
    settings = json.loads((directory / 'host-settings.json').read_text(encoding='utf-8'))
    command = settings.get('adapter_command', [])
    p.require(len(command) == 4 and Path(command[0]).is_absolute() and Path(command[0]).is_file()
              and command[1:] == [str(directory / 'inference_adapter.py'), 'serve', str(directory / 'adapter-profile.json')],
              'Native service command does not belong to the validated build')
    inference_url = endpoint(profile['port'], profile['token'])
    ownership = manifest.get('native_config')
    p.require(isinstance(ownership, dict) and isinstance(ownership.get('root'), dict)
              and 'provider' in ownership, 'Native configuration ownership is missing or invalid')
    root, provider = ownership['root'], ownership['provider']
    selected = root.get('model_provider')
    # Fresh builtin integrations have no owned provider table. Migrations may
    # retain the old table dormant for rollback; its endpoint still must match.
    provider_matches = ((provider is None and selected == 'openai')
                        or (isinstance(provider, dict) and selected in ('openai', 'codex-model-adapter')
                            and provider.get('base_url') == inference_url))
    p.require(profile['port'] > 0 and settings.get('edition') == profile['edition'] == manifest['edition']
              and settings.get('health_identity') == profile['identity']
              and settings.get('health_url') == inference_url.removesuffix('/backend-api/codex') + '/health'
              and root.get('openai_base_url') == inference_url and provider_matches,
              'Native service, catalog configuration and profile identity disagree')
    source = Path(manifest['source_target'])
    verify_official(source)
    layout = p.app_layout(source)
    p.require(p.file_sha(layout.get('source_artifact', layout.get('asar'))) == manifest['original_asar'],
              'The original app changed after native validation')
    if 'composer_compat' in manifest:
        plan=manifest['composer_compat']
        p.require((plan is None and (not profile['routes'] or manifest.get('client_kind') == 'cli')) or
                  isinstance(plan,dict) and plan.get('models')==sorted(profile['routes']),
                  'Composer compatibility and independent model routes disagree')
        if plan is not None:
            from official_client_host import assets
            assets(source,plan)



MODEL_KEYS = ('model', 'model_reasoning_effort', 'model_provider', 'model_catalog_json',
              'openai_base_url', 'model_context_window', 'model_auto_compact_token_limit')


def _record(directory, transaction, phase=None):
    if phase is not None:
        transaction['phase'] = phase
    atomic_json(Path(directory) / 'native-install-transaction.json', transaction)


def _guard_staged_config(current, staged):
    live, before = p.tomllib.loads(current), p.tomllib.loads(staged)
    for key in MODEL_KEYS:
        p.require(live.get(key) == before.get(key), 'A model setting changed since native staging: ' + key)
    p.require(live.get('features', {}).get('enable_request_compression') ==
              before.get('features', {}).get('enable_request_compression'),
              'The request compression setting changed since native staging')


def _owned_config(text, ownership):
    value = p.tomllib.loads(text)
    return (all(value.get(key) == expected for key, expected in ownership['root'].items())
            and (ownership.get('compression_policy') == 'native'
                 or value.get('features', {}).get('enable_request_compression') is False)
            and value.get('model_providers', {}).get('codex-model-adapter') == ownership.get('provider'))


def _app_hash(app):
    return p.file_sha(p.app_layout(Path(app))['asar'])


def _manifest_committed(directory, transaction):
    manifest = json.loads((Path(directory) / 'manifest.json').read_text(encoding='utf-8'))
    return manifest.get('status') == 'installed' and manifest.get('install_transaction') == transaction['id']


def _installed_result(manifest, restored=False):
    return {'status': 'installed', 'architecture': ARCHITECTURE, 'edition': manifest['edition'],
            'app_modified': False, 'restored_official_app': restored}


def _restore_origins(directory, ownership):
    """Undo only the exact origin-index revision written by this transaction."""
    path = Path(ownership['path'])
    p.require(not path.is_symlink(), 'Legacy origin index became a symlink')
    actual = p.file_sha(path) if path.exists() else None
    if actual == ownership['previous_sha256']:
        return
    p.require(actual == ownership['activated_sha256'], 'Legacy origin index changed independently; retain it for review')
    if ownership['previous_sha256'] is None:
        path.unlink()
    else:
        backup = Path(directory) / ownership['backup_file']
        p.require(backup.is_file() and not backup.is_symlink() and p.file_sha(backup) == ownership['previous_sha256'],
                  'Legacy origin index recovery backup changed')
        p.atomic_text(path, backup.read_bytes().decode('utf-8'))


def _prepare_origins(directory, home, rpc, providers, external_models, transaction):
    from reasoning_origin import index_legacy_origins
    rows = inventory(rpc, providers)
    paths = [row['path'] for row in rows if row.get('path')]
    staged = directory / ('legacy-origins-index.' + transaction['id'] + '.json')
    report = index_legacy_origins(paths, external_models, staged, home)
    target = home / 'model-adapter-legacy-origins.json'
    p.require(not target.is_symlink(), 'Legacy origin index is a symlink')
    backup = 'legacy-origins-before.' + transaction['id'] + '.json'
    previous_hash = p.file_sha(target) if target.exists() else None
    if target.exists():
        p.atomic_text(directory / backup, target.read_bytes().decode('utf-8'))
    ownership = {'path': str(target), 'previous_sha256': previous_hash,
                 'activated_sha256': p.file_sha(staged), 'backup_file': backup}
    transaction['origin_index'] = ownership
    transaction['actions']['origins'] = True
    _record(directory, transaction, 'origin_index_activating')
    p.atomic_text(target, staged.read_text(encoding='utf-8'))
    _record(directory, transaction, 'origin_index_activated')
    return report


def _read_transaction(directory):
    directory = Path(directory)
    transaction = json.loads((directory / 'native-install-transaction.json').read_text(encoding='utf-8'))
    p.require(transaction.get('format') == 2 and transaction.get('directory') == str(directory),
              'Unknown native installation transaction; retain it for manual review')
    original = transaction['previous_manifest']
    p.require(transaction['target'] == original['target'] and transaction['home'] == original['codex_home']
              and transaction['edition'] == original['edition'], 'Native transaction target identity changed')
    for key in ('previous_config_file', 'migration_file', 'app_backup'):
        name = transaction[key]
        p.require(Path(name).name == name and name not in ('', '.', '..'), 'Unsafe native recovery artifact path')
    backup = directory / transaction['previous_config_file']
    p.require(backup.is_file() and not backup.is_symlink() and p.file_sha(backup) == transaction['previous_config_sha256'],
              'Native configuration recovery backup changed')
    runtime=transaction.get('runtime_activation')
    if runtime:
        state=original.get('update',{}).get('monitor_state_dir')
        p.require(state and Path(state).resolve()==Path(runtime['directory']) and runtime['edition']==transaction['edition'],
                  'Native runtime recovery target changed')
    origin = transaction.get('origin_index')
    if origin:
        p.require(origin['path'] == str(Path(transaction['home']) / 'model-adapter-legacy-origins.json')
                  and Path(origin['backup_file']).name == origin['backup_file'], 'Unsafe native origin-index recovery path')
    return transaction


def _recover_locked(directory, transaction):
    """Recover intent-written operations, including a crash before checkpointing."""
    directory = Path(directory)
    home, target = Path(transaction['home']), Path(transaction['target'])
    service = AdapterService(transaction['edition'], directory)
    config = home / 'config.toml'
    if _manifest_committed(directory, transaction):
        # The installed manifest is the commit fact. Never undo it just because
        # the following journal fsync was interrupted or the listener restarted.
        verify_official(target)
        p.require(_owned_config(p.read_config(config), transaction['ownership']),
                  'Committed native configuration changed; review before recovery')
        settings = json.loads((directory / 'host-settings.json').read_text(encoding='utf-8'))
        if not healthy(settings):
            service.activate(settings)
        _record(directory, transaction, 'committed')
        return {'status': 'committed', 'transaction': transaction['id']}
    if transaction['phase'] == 'committed':
        raise RuntimeError('Committed native manifest is missing; retain the transaction for review')
    if transaction['phase'] == 'rolled_back':
        return {'status': 'rolled_back', 'transaction': transaction['id']}
    p.require_stopped(target, read_only_source=sys.platform == 'win32')
    previous = transaction['previous_manifest']
    prior_deployment = transaction.get('previous_deployment')
    if sys.platform == 'win32' and prior_deployment:
        p.require_stopped(Path(prior_deployment))
    done = transaction.setdefault('recovered', {})
    migration_journal = directory / transaction['migration_file']
    # Chat provider rollback needs the activated provider definition. If it
    # fails, leave that definition/service/app intact for the next safe attempt.
    if migration_journal.exists() and not done.get('metadata'):
        try:
            migration = json.loads(migration_journal.read_text(encoding='utf-8'))
            if migration.get('phase') != 'rolled_back':
                verify_official(target)
                with native_session(target, home) as rpc:
                    rollback(rpc, migration_journal)
            done['metadata'] = True
            _record(directory, transaction)
        except Exception as error:
            transaction['recovery_errors'] = [{'resource': 'metadata', 'error_type': type(error).__name__}]
            _record(directory, transaction, 'metadata_recovery_required')
            raise RuntimeError('Native chat metadata recovery is required; its provider definition was retained') from error
    errors = []

    def attempt(resource, action):
        if done.get(resource):
            return
        try:
            action()
            done[resource] = True
            _record(directory, transaction)
        except Exception as error:
            errors.append({'resource': resource, 'error_type': type(error).__name__})

    if transaction.get('runtime_activation'):
        from updater import rollback_runtime
        attempt('runtime',lambda:rollback_runtime(transaction['runtime_activation']))

    if transaction['actions'].get('service'):
        def restore_service():
            service.restore(transaction['previous_service'])
            baseline=previous.get('legacy_baseline')
            if transaction['previous_service'].get('loaded') and baseline:
                prior_path=Path(baseline);prior=json.loads((prior_path/'manifest.json').read_text(encoding='utf-8'))
                if prior.get('architecture')==ARCHITECTURE:
                    host=prior_path/'host-settings.json'
                    p.require(p.file_sha(host)==prior['host_settings_sha256'],'Previous service settings changed')
                    service._wait_ready(json.loads(host.read_text(encoding='utf-8')))
        attempt('service',restore_service)

    def restore_app():
        # The exchange and its journal write are separate atomic operations.
        # Check both actual hashes, never infer exchange state from phase alone.
        backup = directory / transaction['app_backup']
        actual = _app_hash(target)
        old, official = transaction['previous_app_sha256'], transaction['official_app_sha256']
        if actual == old:
            return
        p.require(actual == official and backup.is_dir() and _app_hash(backup) == old,
                  'Native app recovery hashes changed; no app was exchanged')
        verify_official(target)
        p.exchange(target, backup)
        p.require(_app_hash(target) == old, 'The previous app was not restored')

    if transaction['actions'].get('app'):
        attempt('app', restore_app)

    def restore_configuration():
        before = (directory / transaction['previous_config_file']).read_text(encoding='utf-8')
        current = p.read_config(config)
        restored = rollback_config(current, transaction['ownership'], before)
        if not transaction['config_existed'] and not p.tomllib.loads(restored):
            config.unlink(missing_ok=True)
        elif restored != current:
            p.atomic_text(config, restored)

    if transaction['actions'].get('config'):
        attempt('configuration', restore_configuration)
    if transaction['actions'].get('origins'):
        attempt('origins', lambda: _restore_origins(directory, transaction['origin_index']))
    if transaction['actions'].get('shortcut'):
        from official_client_shortcut import restore as restore_shortcut
        attempt('shortcut', lambda: restore_shortcut(transaction['shortcut']))
    # A rollback must also undo an in-memory status mutation or a manifest
    # write interrupted before its atomic replacement. Never label a restored
    # resource set installed.
    attempt('manifest', lambda: p.save_json(directory / 'manifest.json', previous))
    transaction['recovery_errors'] = errors
    _record(directory, transaction, 'resource_recovery_required' if errors else 'rolled_back')
    if errors:
        raise RuntimeError('Native resource recovery is incomplete: ' + ', '.join(row['resource'] for row in errors))
    return {'status': 'rolled_back', 'transaction': transaction['id']}


def recover(directory):
    """Idempotently finish a committed install or reverse its incomplete work.

    Uses only native metadata APIs, exact service ownership and recorded app
    hashes. All artifacts remain available for diagnosis; no SQLite edits.
    """
    directory = Path(directory).resolve()
    restore_journal = directory / 'native-uninstall-transaction.json'
    if restore_journal.exists():
        restoration = json.loads(restore_journal.read_text(encoding='utf-8'))
        if restoration.get('phase') != 'complete':
            return uninstall(directory)
        return {'status': 'restored', 'configuration': 'previous'}
    journal = directory / 'native-install-transaction.json'
    if not journal.exists():
        return {'status': 'no_transaction'}
    transaction = _read_transaction(directory)
    with state_lock(Path(transaction['home']) / 'model-adapter-install') as locked:
        p.require(locked, 'Another model adapter installation is already running')
        return _recover_locked(directory, _read_transaction(directory))


def install(directory, manifest):
    directory = Path(directory).resolve()
    journal_path = directory / 'native-install-transaction.json'
    if journal_path.exists():
        result = recover(directory)
        if result['status'] == 'committed':
            saved = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
            manifest.clear()
            manifest.update(saved)
            return _installed_result(saved, bool(saved.get('legacy_app_backup')))
        saved = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        manifest.clear()
        manifest.update(saved)
    validate_build(directory, manifest)
    target, source = Path(manifest['target']), Path(manifest['source_target'])
    home = Path(manifest['codex_home'])
    baseline = manifest.get('legacy_baseline')
    prior = (json.loads((Path(baseline) / 'manifest.json').read_text(encoding='utf-8'))
             if baseline else manifest['initial_manifest'])
    legacy = prior.get('architecture') != ARCHITECTURE
    native_provider = manifest['native_config']['root'].get('model_provider') or 'openai'
    config_mode = 'builtin' if native_provider == 'openai' else 'native-summary'
    restore_app = legacy and sys.platform == 'darwin' and bool(prior.get('patched_asar'))
    p.require_stopped(target, read_only_source=sys.platform == 'win32')
    previous_deployment = prior.get('deployment_target') if sys.platform == 'win32' and legacy else None
    if previous_deployment:
        p.require_stopped(Path(previous_deployment))
    config = home / 'config.toml'
    staged = (directory / 'config-before.toml').read_text(encoding='utf-8')
    settings = json.loads((directory / 'host-settings.json').read_text(encoding='utf-8'))
    service = AdapterService(manifest['edition'], directory)
    with state_lock(home / 'model-adapter-install') as locked:
        p.require(locked, 'Another model adapter installation is already running')
        if journal_path.exists():
            old = _read_transaction(directory)
            p.require(old['phase'] == 'rolled_back', 'A prior native installation requires recovery')
            journal_path.replace(directory / ('native-install-transaction.' + old['id'] + '.rolled_back.json'))
        current = p.read_config(config)
        _guard_staged_config(current, staged)
        prepared, ownership = prepare_config(current, directory / 'desktop-models.json',
            manifest['native_config']['root']['openai_base_url'], mode=config_mode,
            previous=prior.get('native_config') if not legacy else None)
        txid = uuid.uuid4().hex
        backup_file = 'config-before-install.' + txid + '.toml'
        p.atomic_text(directory / backup_file, current)
        transaction = {'format': 2, 'id': txid, 'directory': str(directory), 'home': str(home),
            'target': str(target), 'edition': manifest['edition'], 'previous_manifest': copy.deepcopy(manifest),
            'previous_deployment': previous_deployment, 'previous_service': service.snapshot(),
            'config_existed': config.exists(), 'previous_config_file': backup_file,
            'previous_config_sha256': p.sha(current.encode()), 'ownership': ownership,
            'migration_file': 'chat-provider-migration.' + txid + '.json',
            'app_backup': 'legacy-patched-backup.' + txid + '.app',
            'previous_app_sha256': prior.get('patched_asar'), 'official_app_sha256': manifest['original_asar'],
            'actions': {}, 'recovered': {}, 'legacy': legacy}
        _record(directory, transaction, 'prepared')
        try:
            if sys.platform == 'win32' and legacy:
                from official_client_shortcut import prepare as prepare_shortcut
                shortcut = prepare_shortcut(prior, directory, target, txid)
                if shortcut:
                    transaction['shortcut'] = shortcut
                    _record(directory, transaction, 'shortcut_prepared')
            if restore_app:
                p.require(source != target, 'The verified official backup must differ from the patched installation')
                p.require(_app_hash(target) == prior['patched_asar'], 'Legacy target changed before restoration')
                backup = directory / transaction['app_backup']
                p.clone_app(source, backup)
                verify_official(backup)
                transaction['actions']['app'] = True
                _record(directory, transaction, 'app_restoring')
                p.exchange(target, backup)
                _record(directory, transaction, 'official_app_restored')
            # Re-read immediately before activation, under the lock. Cloning a
            # legacy app can take time; dynamic desktop fields remain current.
            current = p.read_config(config)
            _guard_staged_config(current, staged)
            prepared, ownership = prepare_config(current, directory / 'desktop-models.json',
                manifest['native_config']['root']['openai_base_url'], mode=config_mode,
                previous=prior.get('native_config') if not legacy else None)
            activation_backup = 'config-before-activation.' + txid + '.toml'
            p.atomic_text(directory / activation_backup, current)
            transaction.update(previous_config_file=activation_backup,
                               previous_config_sha256=p.sha(current.encode()), ownership=ownership,
                               config_existed=config.exists())
            p.atomic_text(directory / 'config-installed.toml', prepared)
            transaction['actions']['config'] = True
            _record(directory, transaction, 'config_activating')
            p.atomic_text(config, prepared)
            _record(directory, transaction, 'config_activated')
            transaction['actions']['service'] = True
            _record(directory, transaction, 'service_activating')
            service.activate(settings)
            _record(directory, transaction, 'service_activated')
            if legacy:
                providers = {'openai'} | {row['provider'] for row in prior.get('model_routes', {}).values()}
                _record(directory, transaction, 'metadata_migrating')
                with native_session(target, home) as rpc:
                    manifest['legacy_origin_index'] = _prepare_origins(directory, home, rpc, providers,
                        set(prior.get('model_routes', {})), transaction)
                    manifest['chat_migration'] = migrate(rpc, providers, directory / transaction['migration_file'], target=native_provider)
                manifest['chat_migration_journal'] = str(directory / transaction['migration_file'])
                manifest['native_original_config_file'] = str(directory / activation_backup)
                manifest['native_original_config_sha256'] = transaction['previous_config_sha256']
                manifest['legacy_origins_ownership'] = {**transaction['origin_index'], 'directory': str(directory)}
            else:
                for key in ('chat_migration_journal', 'native_identity_migration_journal', 'legacy_origins_ownership', 'legacy_origin_index',
                            'native_original_config_file', 'native_original_config_sha256'):
                    if key in prior:
                        manifest[key] = copy.deepcopy(prior[key])
                if native_provider == 'openai' and prior.get('native_config', {}).get('root', {}).get('model_provider') == 'codex-model-adapter':
                    _record(directory, transaction, 'metadata_migrating')
                    with native_session(target, home) as rpc:
                        manifest['native_identity_migration'] = migrate(rpc, {'codex-model-adapter'},
                            directory / transaction['migration_file'], target='openai')
                    manifest['native_identity_migration_journal'] = str(directory / transaction['migration_file'])
            verify_official(target)
            p.require(healthy(settings), 'The native adapter stopped after activation')
            activated = p.read_config(config)
            p.require(_owned_config(activated, ownership), 'A managed configuration changed during native activation')
            if transaction.get('shortcut'):
                from official_client_shortcut import activate as activate_shortcut
                transaction['actions']['shortcut'] = True
                _record(directory, transaction, 'shortcut_activating')
                activate_shortcut(transaction['shortcut'])
                _record(directory, transaction, 'shortcut_activated')
                manifest['windows_shortcut'] = transaction['shortcut']
            elif prior.get('windows_shortcut'):
                manifest['windows_shortcut'] = copy.deepcopy(prior['windows_shortcut'])
            update=manifest.get('update',{})
            if update.get('package') and update.get('monitor_state_dir'):
                from updater import activate_runtime
                state=Path(update['monitor_state_dir']).resolve()
                monitor_settings=json.loads((state/'settings.json').read_text(encoding='utf-8'))
                p.require(monitor_settings.get('edition')==manifest['edition'],'Update runtime channel changed')
                def checkpoint(record):
                    transaction['runtime_activation']=record
                    _record(directory,transaction,'runtime_activating')
                from update_progress import from_manifest
                progress=from_manifest(manifest)
                if progress:progress.publish('activating')
                activate_runtime(Path(update['package']),state,manifest['edition'],checkpoint=checkpoint)
                _record(directory,transaction,'runtime_ready')
                p.require(healthy(settings),'Adapter stopped while activating the update runtime')
            manifest.update(status='installed', installed_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                            installed_config_hash=p.sha(activated.encode()), native_config=ownership,
                            install_transaction=txid,
                            legacy_app_backup=str(directory / transaction['app_backup']) if restore_app else None)
            _record(directory, transaction, 'committing')
            p.save_json(directory / 'manifest.json', manifest)
            _record(directory, transaction, 'committed')
        except BaseException:
            # If the atomic installed manifest exists, the commit has happened.
            # A failed final journal write must never trigger resource rollback.
            if _manifest_committed(directory, transaction):
                _record(directory, transaction, 'committed')
            else:
                from update_progress import from_manifest,release_idle
                progress=from_manifest(manifest)
                if progress:progress.publish('recovering')
                _recover_locked(directory, transaction)
                if progress:
                    release_idle(progress)
                    progress.publish('rolled_back',present=True)
                manifest.clear()
                manifest.update(transaction['previous_manifest'])
                raise
    return _installed_result(manifest, restore_app)


def _original_config(text, ownership):
    parsed = p.tomllib.loads(text)
    return (all(parsed.get(key) == value for key, value in ownership['original_root'].items())
            and (ownership.get('compression_policy') == 'native'
                 or parsed.get('features', {}).get('enable_request_compression') == ownership['original_compression'])
            and parsed.get('model_providers', {}).get('codex-model-adapter') is None)


def uninstall(directory):
    """Restore configuration from before this adapter; keep the official app.

    This is a forward-only, resumable restoration. Old recorded chats return to
    their old providers; chats created since installation use the previous
    default provider. Every change uses the native metadata API, never SQL.
    """
    directory = Path(directory).resolve()
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    journal_path = directory / 'native-uninstall-transaction.json'
    p.require(manifest.get('architecture') == ARCHITECTURE, 'A native adapter baseline is required')
    home, target = Path(manifest['codex_home']), Path(manifest['target'])
    p.require_stopped(target, read_only_source=sys.platform == 'win32')
    verify_official(target)
    with state_lock(home / 'model-adapter-install') as locked:
        p.require(locked, 'Another model adapter operation is already running')
        if journal_path.exists():
            transaction = json.loads(journal_path.read_text(encoding='utf-8'))
            p.require(transaction.get('format') == 1 and transaction.get('directory') == str(directory)
                      and transaction.get('home') == str(home) and transaction.get('target') == str(target),
                      'Unknown native configuration restoration transaction')
            if transaction['phase'] == 'complete':
                return {'status': 'restored', 'configuration': 'previous', 'app_modified': False}
        else:
            p.require(manifest.get('status') == 'installed', 'Only an installed native adapter can be restored')
            transaction = {'format': 1, 'directory': str(directory), 'home': str(home), 'target': str(target),
                           'phase': 'prepared', 'manifest': manifest, 'completed': {}, 'threads': None}
            atomic_json(journal_path, transaction)
        original = transaction['manifest']
        ownership = original['native_config']
        completed = transaction['completed']
        service = AdapterService(original['edition'], directory)
        stage = 'metadata'
        try:
            if not completed.get('metadata'):
                with native_session(target, home) as rpc:
                    if transaction['threads'] is None:
                        previous_providers = {}
                        migration_file = original.get('chat_migration_journal')
                        if migration_file:
                            migration_file = Path(migration_file)
                            p.require(migration_file.is_file() and not migration_file.is_symlink(),
                                      'The first native chat migration journal is unavailable')
                            migration = json.loads(migration_file.read_text(encoding='utf-8'))
                            p.require(migration.get('format') == 1 and migration.get('target') in ('codex-model-adapter', 'openai'),
                                      'Unknown initial native migration journal')
                            previous_providers = {row['id']: row['provider'] for row in migration['threads']}
                        # Only provider destinations come from the old journal.
                        # Preserve today's model/effort/recency/archive metadata.
                        destination = ownership['original_root'].get('model_provider') or 'openai'
                        native_provider = ownership['root'].get('model_provider') or 'openai'
                        rows = inventory(rpc, {'codex-model-adapter', native_provider})
                        rows = [row for row in rows if previous_providers.get(row['id'], destination) != row['provider']]
                        transaction['threads'] = [{**row, 'restore_provider': previous_providers.get(row['id'], destination)}
                                                      for row in rows]
                        transaction['phase'] = 'restoring_metadata'
                        atomic_json(journal_path, transaction)
                    rows = transaction['threads']
                    providers = {'codex-model-adapter', 'openai'} | {row['restore_provider'] for row in rows}
                    archived_ids = {row['id'] for row in inventory(rpc, providers) if row['archived']}
                    for row in rows:
                        if row['phase'] == 'restored':
                            continue
                        row['phase'] = 'changing'
                        atomic_json(journal_path, transaction)
                        _set_provider(rpc, row, row['restore_provider'], archived_ids)
                        row['phase'] = 'restored'
                        atomic_json(journal_path, transaction)
                completed['metadata'] = True
                atomic_json(journal_path, transaction)
            stage = 'service'
            if not completed.get('service'):
                service.remove()
                completed['service'] = True
                atomic_json(journal_path, transaction)
            stage = 'configuration'
            if not completed.get('configuration'):
                config = home / 'config.toml'
                current = p.read_config(config)
                if not _original_config(current, ownership):
                    before_file = original.get('native_original_config_file')
                    if before_file:
                        before_file = Path(before_file)
                        p.require(before_file.is_file() and not before_file.is_symlink()
                                  and p.file_sha(before_file) == original['native_original_config_sha256'],
                                  'The initial native configuration backup changed')
                        restored_config = rollback_config(current, ownership, before_file.read_text(encoding='utf-8'))
                    else:
                        restored_config = restore_config(current, ownership)
                    p.atomic_text(config, restored_config)
                completed['configuration'] = True
                atomic_json(journal_path, transaction)
            stage = 'origins'
            if not completed.get('origins'):
                origin = original.get('legacy_origins_ownership')
                if origin:
                    p.require(origin['path'] == str(home / 'model-adapter-legacy-origins.json')
                              and Path(origin['backup_file']).name == origin['backup_file'],
                              'Unknown native origin-index ownership')
                    _restore_origins(Path(origin['directory']), origin)
                completed['origins'] = True
                atomic_json(journal_path, transaction)
            stage = 'manifest'
            restored = {**original, 'status': 'restored', 'restored_configuration': 'previous',
                        'restored_at': dt.datetime.now(dt.timezone.utc).isoformat()}
            p.save_json(directory / 'manifest.json', restored)
            transaction.update(phase='complete', recovery_errors=[])
            atomic_json(journal_path, transaction)
        except BaseException as error:
            transaction.update(phase='restore_recovery_required',
                               recovery_errors=[{'resource': stage, 'error_type': type(error).__name__}])
            atomic_json(journal_path, transaction)
            raise
    return {'status': 'restored', 'configuration': 'previous', 'app_modified': False,
            'chats_restored': len(transaction['threads'] or [])}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['recover', 'restore'])
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    print(json.dumps((recover if args.action == 'recover' else uninstall)(args.directory), ensure_ascii=False))
