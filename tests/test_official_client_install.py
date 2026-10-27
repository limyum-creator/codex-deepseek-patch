"""Fault injection around real config/files and fake app/service/native metadata.

The fake boundaries cannot launch a desktop or contact a model. Assertions
compare externally visible resource state, not just transaction checkpoints.
"""
import base64
from contextlib import ExitStack, contextmanager
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
import uuid
from unittest.mock import Mock, patch as mock

import official_client_install as installer
from official_client_config import PROVIDER, prepare_config, rollback_config
import patch as p


class PowerLoss(BaseException):
    pass


@unittest.skipUnless(sys.platform == 'darwin', 'Real isolated launchd service lifecycle')
class MacServiceTests(unittest.TestCase):
    def test_macos_update_and_rollback_wait_for_old_launchd_job(self):
        for edition in ('ds', 'proxy'):
            with self.subTest(edition=edition), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                script, marker = root/'inference_adapter.py', root/'ready.json'
                script.write_text(
                    'import signal,time,sys,json,os\nfrom pathlib import Path\n'
                    'signal.signal(signal.SIGTERM,lambda *a:(time.sleep(.6),sys.exit(0)))\n'
                    'Path(sys.argv[1]).write_text(json.dumps({"revision":sys.argv[2],"pid":os.getpid()}))\n'
                    'while True:time.sleep(.1)\n', encoding='utf-8')
                service = installer.AdapterService(edition, root)
                service.label = 'com.limyum.codex-model-adapter.check.' + edition + '.' + uuid.uuid4().hex
                service.plist = root/'owned.plist'
                def definition(settings, label):
                    return {'Label': label, 'ProgramArguments': [
                        sys.executable, str(script), str(marker), settings['revision']],
                        'RunAtLoad': True, 'WorkingDirectory': str(root), 'ProcessType': 'Standard'}
                def ready(settings):
                    end = time.monotonic() + 8
                    while time.monotonic() < end:
                        try:
                            if json.loads(marker.read_text())['revision'] == settings['revision']:
                                return
                        except (ValueError, OSError):
                            pass
                        time.sleep(.05)
                    self.fail('Isolated launchd fixture did not start its selected revision')
                try:
                    with mock.object(installer, 'adapter_launchd_plist', side_effect=definition), \
                         mock.object(service, '_wait_ready', side_effect=ready):
                        service.activate({'revision': 'old'})
                        previous = service.snapshot()
                        old_pid = json.loads(marker.read_text())['pid']
                        service.activate({'revision': 'new'})
                        self.assertNotEqual(json.loads(marker.read_text())['pid'], old_pid)
                        marker.unlink()
                        service.restore(previous)
                        ready({'revision': 'old'})
                        self.assertEqual(service.snapshot()['definition'], previous['definition'])
                        service.remove()
                        self.assertFalse(service.snapshot()['loaded'])
                finally:
                    # Cleanup is confined to the randomized fixture label.
                    subprocess.run(['/bin/launchctl', 'bootout',
                        f'gui/{os.getuid()}/{service.label}'], capture_output=True, timeout=10,
                        **p.background_options())
                    service._wait_running(False)


class Fixture:
    def __init__(self, root, *, update=False, fresh=False, native_identity=False):
        root = root.resolve()
        self.root, self.directory, self.home = root, root/'build', root/'home'
        self.target, self.source, self.prior = root/'target.app', root/'source.app', root/'baseline'
        for path in (self.directory, self.home, self.target, self.source, self.prior):
            path.mkdir()
        (self.target/'app.asar').write_text('official' if update or fresh else 'patched')
        (self.source/'app.asar').write_text('official')
        self.config = self.home/'config.toml'
        original = 'model="gpt-fixture"\nmodel_context_window=512000\n[mcp_servers.node_repl.env]\nSKY_CUA_NATIVE_PIPE_DIRECTORY="before"\n'
        self.old_endpoint = 'http://127.0.0.1:41231/old-fixture/backend-api/codex'
        endpoint = 'http://127.0.0.1:41232/new-fixture/backend-api/codex'
        prior = {'architecture': 'legacy', 'target': str(self.target),
                 'patched_asar': p.sha(b'patched'), 'model_routes': {'ds': {'provider': 'deepseek-desktop'}}}
        if update:
            original, owned = prepare_config(original, self.prior/'desktop-models.json', self.old_endpoint,
                                             mode='native-summary')
            prior.update(architecture=installer.ARCHITECTURE, native_config=owned)
            prior.pop('patched_asar')
        if fresh:
            prior.update(architecture='official-original')
            prior.pop('patched_asar')
        self.config.write_text(original)
        self.before = original
        self.prior_record = prior
        p.save_json(self.prior/'manifest.json', prior)
        (self.directory/'config-before.toml').write_text(original)
        prepared, ownership = prepare_config(original, self.directory/'desktop-models.json', endpoint,
            mode='builtin' if native_identity else 'native-summary', previous=prior.get('native_config'))
        (self.directory/'config-prepared.toml').write_text(prepared)
        p.save_json(self.directory/'host-settings.json', {'edition': 'ds', 'health_identity': 'new'})
        self.manifest = {'architecture': installer.ARCHITECTURE, 'status': 'prepared', 'edition': 'ds',
            'target': str(self.target), 'source_target': str(self.source), 'codex_home': str(self.home),
            'legacy_baseline': None if fresh else str(self.prior), 'initial_manifest': prior,
            'native_config': ownership, 'original_asar': p.sha(b'official')}
        p.save_json(self.directory/'manifest.json', self.manifest)
        self.initial_service = {'registered': True, 'loaded': False, 'enabled': False,
                                'definition': '<old>inference_adapter.py</old>'}
        self.service = copy.deepcopy(self.initial_service)
        self.metadata = {'active': 'openai', 'archived': 'deepseek-desktop'}
        if update:
            self.metadata = {key: PROVIDER for key in self.metadata}
        self.before_metadata = copy.deepcopy(self.metadata)
        self.metadata_calls = []
        self.activate_hook = None
        self.restore_failures = 0
        self.metadata_rollback_failures = 0
        self.migrate_failure = False

    def text(self):
        return self.config.read_text() if self.config.exists() else ''

    def dynamic(self, value):
        self.config.write_text(self.text().replace('SKY_CUA_NATIVE_PIPE_DIRECTORY="before"',
                                                  'SKY_CUA_NATIVE_PIPE_DIRECTORY="' + value + '"'))

    def assert_baseline(self, test, pipe='before'):
        value = tomllib.loads(self.text())
        before = tomllib.loads(self.before)
        before['mcp_servers']['node_repl']['env']['SKY_CUA_NATIVE_PIPE_DIRECTORY'] = pipe
        test.assertEqual(value, before)
        test.assertEqual((self.target/'app.asar').read_text(),
                         'patched' if 'patched_asar' in self.prior_record else 'official')
        test.assertEqual(self.service, self.initial_service)
        test.assertEqual(self.metadata, self.before_metadata)
        test.assertFalse((self.home/'model-adapter-legacy-origins.json').exists())
        test.assertEqual(json.loads((self.directory/'manifest.json').read_text())['status'], 'prepared')

    @contextmanager
    def boundaries(self, *, validate=False):
        fixture = self

        class Service:
            def __init__(self, *args): pass
            def snapshot(self): return copy.deepcopy(fixture.service)
            def activate(self, settings):
                fixture.service = {'registered': True, 'loaded': True, 'enabled': True,
                                   'definition': '<new>inference_adapter.py</new>'}
                if fixture.activate_hook:
                    fixture.activate_hook()
            def remove(self):
                if fixture.restore_failures:
                    fixture.restore_failures -= 1
                    raise RuntimeError('service fixture removal failure')
                fixture.service = {'registered':False, 'loaded':False, 'enabled':False, 'definition':None}
            def restore(self, previous):
                if fixture.restore_failures:
                    fixture.restore_failures -= 1
                    raise RuntimeError('service fixture failure')
                fixture.service = copy.deepcopy(previous)

        @contextmanager
        def session(app, home):
            yield fixture.metadata

        def migrate(rpc, providers, path, *, target=PROVIDER):
            fixture.metadata_calls.append('migrate')
            p.save_json(path, {'format':1, 'target':target, 'phase': 'running', 'before': copy.deepcopy(rpc),
                               'threads':[{'id':key,'provider':value} for key,value in rpc.items()]})
            rpc['active'] = target
            if fixture.migrate_failure:
                raise RuntimeError('interrupted native metadata')
            rpc['archived'] = target
            value = json.loads(path.read_text()); value['phase'] = 'complete'; p.save_json(path, value)
            return {'status': 'passed', 'migrated_chats': 2, 'inference_turns': 0}

        def rollback(rpc, path):
            fixture.metadata_calls.append('rollback')
            if fixture.metadata_rollback_failures:
                fixture.metadata_rollback_failures -= 1
                raise RuntimeError('native metadata unavailable')
            value = json.loads(path.read_text()); rpc.update(value['before'])
            value['phase'] = 'rolled_back'; p.save_json(path, value)

        def inventory(rpc, providers):
            return [{'id':key, 'provider':value, 'archived':key == 'archived', 'phase':'pending',
                     'model':'ds' if key == 'archived' else 'gpt-current', 'reasoning_effort':'high',
                     'metadata_sha256':'current-fixture-metadata'}
                    for key,value in rpc.items() if value in providers]

        def set_provider(rpc, row, target, archived):
            fixture.metadata_calls.append(('restore-provider', row['id'], row['model'], target))
            rpc[row['id']] = target

        def exchange(left, right):
            temporary = fixture.root/'exchange.tmp'
            left.rename(temporary); right.rename(left); temporary.rename(right)

        def official(app):
            if (Path(app)/'app.asar').read_text() != 'official':
                raise ValueError('not official fixture')

        with ExitStack() as stack:
            for name, value in {'validate_build': lambda *args: None, 'verify_official': official,
                'AdapterService': Service, 'healthy': lambda *args: fixture.service['loaded'],
                'native_session': session, 'inventory': inventory, '_set_provider': set_provider,
                'migrate': migrate, 'rollback': rollback}.items():
                if name=='validate_build' and validate:continue
                stack.enter_context(mock.object(installer, name, value))
            stack.enter_context(mock.object(installer.sys, 'platform', 'darwin'))
            stack.enter_context(mock.object(p, 'app_layout', side_effect=lambda app: {'asar': Path(app)/'app.asar'}))
            stack.enter_context(mock.object(p, 'require_stopped'))
            stack.enter_context(mock.object(p, 'clone_app', side_effect=lambda a,b: shutil.copytree(a,b)))
            stack.enter_context(mock.object(p, 'exchange', side_effect=exchange))
            yield


class EditionTests(unittest.TestCase):
    def test_a_fully_approved_build_from_the_other_channel_is_refused_before_artifact_reads(self):
        for runtime, candidate in (('ds','proxy'), ('proxy','ds')):
            with self.subTest(runtime=runtime), mock.object(p,'EDITION',runtime), mock.object(p,'file_sha') as digest:
                record = {'architecture':installer.ARCHITECTURE,'status':'prepared','native_probe':'passed','edition':candidate}
                with self.assertRaisesRegex(p.PatchError,'another edition'):
                    installer.validate_build(Path.cwd(),record)
                digest.assert_not_called()


class ConfigRollbackTests(unittest.TestCase):
    def test_update_rolls_back_to_immediate_baseline_and_retains_dynamic_edits(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp), update=True)
            current, owned = prepare_config(fixture.before, fixture.directory/'desktop-models.json',
                'http://127.0.0.1:41232/new-fixture/backend-api/codex', mode='native-summary',
                previous=fixture.prior_record['native_config'])
            current = current.replace('DIRECTORY="before"', 'DIRECTORY="live-pipe"')
            restored = rollback_config(current, owned, fixture.before)
            parsed = tomllib.loads(restored)
            self.assertEqual(parsed['openai_base_url'], fixture.old_endpoint)
            self.assertEqual(parsed['model_catalog_json'], str(fixture.prior/'desktop-models.json'))
            self.assertEqual(parsed['mcp_servers']['node_repl']['env']['SKY_CUA_NATIVE_PIPE_DIRECTORY'], 'live-pipe')
            self.assertEqual(rollback_config(restored, owned, fixture.before), restored)

    def test_first_install_rollback_preserves_later_edits_and_rejects_owned_conflict(self):
        endpoint = 'http://127.0.0.1:41232/new-fixture/backend-api/codex'
        current, owned = prepare_config('', Path.cwd()/'catalog.json', endpoint, mode='native-summary')
        self.assertEqual(tomllib.loads(rollback_config(current, owned, '')), {})
        edited = current + '\n[unrelated]\nkeep="live"\n'
        self.assertEqual(tomllib.loads(rollback_config(edited, owned, '')), {'unrelated': {'keep':'live'}})
        with self.assertRaisesRegex(ValueError, 'changed'):
            rollback_config(edited.replace(endpoint, endpoint.replace('41232','41233')), owned, '')


class TransactionTests(unittest.TestCase):
    def test_builtin_upgrade_migrates_existing_chats_and_failure_restores_old_identity(self):
        for fail in (False,True):
            with self.subTest(fail=fail),tempfile.TemporaryDirectory() as temp:
                fixture=Fixture(Path(temp),update=True,native_identity=True)
                fixture.migrate_failure=fail
                with fixture.boundaries(),mock.object(p,'clone_app') as clone,mock.object(p,'exchange') as exchange:
                    if fail:
                        with self.assertRaises(RuntimeError):installer.install(fixture.directory,fixture.manifest)
                    else:installer.install(fixture.directory,fixture.manifest)
                clone.assert_not_called();exchange.assert_not_called()
                if fail:fixture.assert_baseline(self)
                else:
                    self.assertEqual(tomllib.loads(fixture.text())['model_provider'],'openai')
                    self.assertEqual(fixture.metadata,{'active':'openai','archived':'openai'})
                    record=json.loads((fixture.directory/'manifest.json').read_text())
                    self.assertTrue(Path(record['native_identity_migration_journal']).is_file())

    def test_each_interrupted_intent_and_checkpoint_recovers_all_resources_idempotently(self):
        phases = ('prepared', 'app_restoring', 'official_app_restored', 'config_activating',
                  'config_activated', 'service_activating', 'service_activated', 'metadata_migrating',
                  'origin_index_activating', 'origin_index_activated', 'committing')
        for phase in phases:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temp:
                fixture = Fixture(Path(temp))
                record = installer._record
                interrupted = False
                def die(directory, transaction, current_phase=None):
                    nonlocal interrupted
                    record(directory, transaction, current_phase)
                    if current_phase == phase and not interrupted:
                        interrupted = True
                        raise PowerLoss('process disappeared')
                recovery = installer._recover_locked
                with fixture.boundaries(), mock.object(installer, '_record', side_effect=die), \
                     mock.object(installer, '_recover_locked', side_effect=PowerLoss):
                    with self.assertRaises(PowerLoss):
                        installer.install(fixture.directory, fixture.manifest)
                fixture.dynamic('post-crash-pipe')
                with fixture.boundaries():
                    self.assertEqual(installer.recover(fixture.directory)['status'], 'rolled_back')
                    calls = list(fixture.metadata_calls)
                    self.assertEqual(installer.recover(fixture.directory)['status'], 'rolled_back')
                    self.assertEqual(fixture.metadata_calls, calls)
                fixture.assert_baseline(self, 'post-crash-pipe')

    def test_partial_metadata_failure_keeps_active_provider_until_native_rollback_can_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp)); fixture.migrate_failure = True; fixture.metadata_rollback_failures = 1
            with fixture.boundaries():
                with self.assertRaisesRegex(RuntimeError, 'metadata recovery'):
                    installer.install(fixture.directory, fixture.manifest)
                self.assertEqual(tomllib.loads(fixture.text())['model_provider'], PROVIDER)
                self.assertEqual((fixture.target/'app.asar').read_text(), 'official')
                self.assertTrue(fixture.service['loaded'])
                self.assertEqual(installer.recover(fixture.directory)['status'], 'rolled_back')
            fixture.assert_baseline(self)

    def test_service_restore_failure_does_not_skip_app_config_or_metadata_then_recovery_retries(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp)); fixture.migrate_failure = True; fixture.restore_failures = 1
            with fixture.boundaries():
                with self.assertRaisesRegex(RuntimeError, 'service'):
                    installer.install(fixture.directory, fixture.manifest)
                self.assertEqual(tomllib.loads(fixture.text()), tomllib.loads(fixture.before))
                self.assertEqual((fixture.target/'app.asar').read_text(), 'patched')
                self.assertEqual(fixture.metadata, fixture.before_metadata)
                self.assertEqual(json.loads((fixture.directory/'manifest.json').read_text())['status'], 'prepared')
                self.assertEqual(installer.recover(fixture.directory)['status'], 'rolled_back')
            fixture.assert_baseline(self)

    def test_activation_failure_preserves_dynamic_pipe_and_immediate_update_baseline(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp), update=True)
            def change_and_fail():
                fixture.dynamic('during-activation')
                raise RuntimeError('new service refused')
            fixture.activate_hook = change_and_fail
            with fixture.boundaries():
                with self.assertRaisesRegex(RuntimeError, 'new service refused'):
                    installer.install(fixture.directory, fixture.manifest)
            fixture.assert_baseline(self, 'during-activation')
            self.assertEqual(tomllib.loads(fixture.text())['openai_base_url'], fixture.old_endpoint)

    def test_dynamic_changes_before_and_during_activation_are_preserved_on_success(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp)); clone = shutil.copytree
            def clone_and_change(left, right):
                clone(left, right); fixture.dynamic('before-write')
            def another_change():
                fixture.config.write_text(fixture.text().replace('before-write','during-activation'))
            fixture.activate_hook = another_change
            with fixture.boundaries(), mock.object(p, 'clone_app', side_effect=clone_and_change):
                self.assertEqual(installer.install(fixture.directory, fixture.manifest)['status'], 'installed')
            self.assertEqual(tomllib.loads(fixture.text())['mcp_servers']['node_repl']['env']['SKY_CUA_NATIVE_PIPE_DIRECTORY'],
                             'during-activation')

    def test_model_change_during_preflight_is_preserved_and_app_is_rolled_back(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp)); clone = shutil.copytree
            def clone_and_change(left, right):
                clone(left, right); fixture.config.write_text(fixture.text().replace('gpt-fixture','user-model'))
            with fixture.boundaries(), mock.object(p, 'clone_app', side_effect=clone_and_change):
                with self.assertRaisesRegex(p.PatchError, 'model setting changed'):
                    installer.install(fixture.directory, fixture.manifest)
            self.assertEqual(tomllib.loads(fixture.text())['model'], 'user-model')
            self.assertEqual((fixture.target/'app.asar').read_text(), 'patched')
            self.assertEqual(fixture.service, fixture.initial_service)

    def test_crash_during_rebase_backup_does_not_invalidate_prior_recovery_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp)); write = p.atomic_text; clone = shutil.copytree
            def clone_and_change(left, right):
                clone(left, right); fixture.dynamic('new-preflight-pipe')
            def write_and_die(path, text):
                write(path, text)
                if Path(path).name.startswith('config-before-activation.'):
                    raise PowerLoss()
            with fixture.boundaries(), mock.object(p, 'clone_app', side_effect=clone_and_change), \
                 mock.object(p, 'atomic_text', side_effect=write_and_die), \
                 mock.object(installer, '_recover_locked', side_effect=PowerLoss):
                with self.assertRaises(PowerLoss): installer.install(fixture.directory, fixture.manifest)
            with fixture.boundaries():
                self.assertEqual(installer.recover(fixture.directory)['status'], 'rolled_back')
            fixture.assert_baseline(self, 'new-preflight-pipe')

    def test_independent_owned_setting_conflict_is_retained_for_review(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp), update=True)
            new_endpoint = fixture.manifest['native_config']['root']['openai_base_url']
            def change_owned_and_fail():
                fixture.config.write_text(fixture.text().replace(new_endpoint, new_endpoint.replace('41232','41239')))
                raise RuntimeError('activation stopped')
            fixture.activate_hook = change_owned_and_fail
            with fixture.boundaries():
                with self.assertRaisesRegex(RuntimeError, 'configuration'):
                    installer.install(fixture.directory, fixture.manifest)
                self.assertIn(':41239/', tomllib.loads(fixture.text())['openai_base_url'])
                fixture.config.write_text(fixture.text().replace(':41239/', ':41232/'))
                self.assertEqual(installer.recover(fixture.directory)['status'], 'rolled_back')
            fixture.assert_baseline(self)

    def test_new_fresh_official_app_is_never_cloned_or_exchanged_but_old_openai_chats_migrate(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp), fresh=True)
            with fixture.boundaries(), mock.object(p, 'clone_app') as clone, mock.object(p, 'exchange') as exchange:
                result = installer.install(fixture.directory, fixture.manifest)
            self.assertFalse(result['restored_official_app']); clone.assert_not_called(); exchange.assert_not_called()
            self.assertEqual(fixture.metadata, {'active':PROVIDER, 'archived':PROVIDER})

    def test_final_journal_write_failure_and_hard_interruption_finish_commit_without_rollback(self):
        for interrupted in (False, True):
            with self.subTest(hard_interruption=interrupted), tempfile.TemporaryDirectory() as temp:
                fixture = Fixture(Path(temp)); record = installer._record; failures = 2 if interrupted else 1
                def fail_commit(directory, transaction, phase=None):
                    nonlocal failures
                    if phase == 'committed' and failures:
                        failures -= 1
                        raise PowerLoss() if interrupted else OSError('journal unavailable once')
                    return record(directory, transaction, phase)
                with fixture.boundaries(), mock.object(installer, '_record', side_effect=fail_commit):
                    if interrupted:
                        with self.assertRaises(PowerLoss): installer.install(fixture.directory, fixture.manifest)
                    else:
                        self.assertEqual(installer.install(fixture.directory, fixture.manifest)['status'], 'installed')
                self.assertEqual(json.loads((fixture.directory/'manifest.json').read_text())['status'], 'installed')
                with fixture.boundaries():
                    self.assertEqual(installer.recover(fixture.directory)['status'], 'committed')
                    self.assertEqual(installer.install(fixture.directory, fixture.manifest)['status'], 'installed')
                self.assertEqual(fixture.metadata, {'active':PROVIDER, 'archived':PROVIDER})
                self.assertTrue(fixture.service['loaded'])
                self.assertEqual((fixture.target/'app.asar').read_text(), 'official')

    def test_manifest_write_failure_rolls_back_and_retry_uses_new_migration_journal(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp)); save = p.save_json; once = True
            def fail_manifest(path, value):
                nonlocal once
                if Path(path) == fixture.directory/'manifest.json' and value.get('status') == 'installed' and once:
                    once = False; raise OSError('manifest fixture failure')
                return save(path, value)
            with fixture.boundaries(), mock.object(p, 'save_json', side_effect=fail_manifest):
                with self.assertRaises(OSError): installer.install(fixture.directory, fixture.manifest)
                fixture.assert_baseline(self)
                self.assertEqual(installer.install(fixture.directory, fixture.manifest)['status'], 'installed')
            self.assertEqual(len(list(fixture.directory.glob('chat-provider-migration.*.json'))), 2)
            self.assertEqual(len(list(fixture.directory.glob('native-install-transaction.*.rolled_back.json'))), 1)

    def test_changed_app_or_recovery_backup_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp)); record = installer._record
            def die(directory, transaction, phase=None):
                record(directory, transaction, phase)
                if phase == 'official_app_restored': raise PowerLoss()
            with fixture.boundaries(), mock.object(installer, '_record', side_effect=die), \
                 mock.object(installer, '_recover_locked', side_effect=PowerLoss):
                with self.assertRaises(PowerLoss): installer.install(fixture.directory, fixture.manifest)
            (fixture.target/'app.asar').write_text('user-replacement')
            with fixture.boundaries():
                with self.assertRaisesRegex(RuntimeError, 'app'): installer.recover(fixture.directory)
            self.assertEqual((fixture.target/'app.asar').read_text(), 'user-replacement')


class RestorationTests(unittest.TestCase):
    def test_real_origin_index_captures_only_known_external_hashes_and_rollback_restores_previous_file(self):
        import hashlib
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp)); origin = fixture.home/'model-adapter-legacy-origins.json'
            before = '{"format":1,"origins":{}}\n'; origin.write_bytes(before.replace('\n','\r\n').encode('utf-8'))
            source = fixture.home/'sessions'; source.mkdir(); rollout = source/'fixture.jsonl'
            events = [
                {'type':'turn_context','payload':{'model':'gpt-fixture'}},
                {'type':'response_item','payload':{'type':'reasoning','encrypted_content':'official-cipher'}},
                {'type':'turn_context','payload':{'model':'ds'}},
                {'type':'response_item','payload':{'type':'reasoning','encrypted_content':'foreign-cipher'}}]
            rollout.write_text('\n'.join(map(json.dumps, events))+'\n')
            fixture.migrate_failure = True
            inspected = []
            def inventory(rpc, providers):
                return [{'path':str(rollout)}]
            def activation_check(directory, transaction, phase=None):
                installer.atomic_json(Path(directory)/'native-install-transaction.json',
                                      {**transaction, 'phase':phase or transaction['phase']})
                if phase: transaction['phase'] = phase
                if phase == 'origin_index_activated':
                    inspected.append(json.loads(origin.read_text())['origins'])
            with fixture.boundaries(), mock.object(installer,'inventory',side_effect=inventory), \
                 mock.object(installer,'_record',side_effect=activation_check):
                with self.assertRaises(RuntimeError): installer.install(fixture.directory, fixture.manifest)
            self.assertEqual(inspected, [{hashlib.sha256(b'foreign-cipher').hexdigest():'ds'}])
            self.assertEqual(origin.read_bytes(), before.replace('\n','\r\n').encode('utf-8'))

    def test_restoration_across_updates_keeps_official_app_and_returns_old_and_new_chats(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp)); source_manifest = fixture.manifest
            with fixture.boundaries(): installer.install(fixture.directory, source_manifest)
            first = fixture.directory; current = fixture.text(); second = fixture.root/'next'; second.mkdir()
            (second/'config-before.toml').write_text(current)
            p.save_json(second/'host-settings.json',{'edition':'ds'})
            prepared, owned = prepare_config(current, second/'desktop-models.json',
                'http://127.0.0.1:41233/next-fixture/backend-api/codex', mode='native-summary',
                previous=source_manifest['native_config'])
            record = {key:copy.deepcopy(value) for key,value in source_manifest.items() if key not in
                ('chat_migration_journal','native_original_config_file','native_original_config_sha256',
                 'legacy_origins_ownership','legacy_origin_index','chat_migration','install_transaction')}
            record.update(status='prepared',legacy_baseline=str(first),native_config=owned)
            p.save_json(second/'manifest.json',record)
            with fixture.boundaries(): installer.install(second,record)
            self.assertEqual(record['chat_migration_journal'],source_manifest['chat_migration_journal'])
            fixture.metadata['new-chat'] = PROVIDER
            fixture.dynamic('after-update')
            row = {'id':'new-chat','provider':PROVIDER,'archived':True,'phase':'pending',
                   'model':'ds','reasoning_effort':'low','metadata_sha256':'fixture'}
            calls = []
            def inventory(rpc, providers):
                return [copy.deepcopy(row)] + [
                    {'id':key,'provider':value,'archived':key=='archived','phase':'pending',
                     'model':'current-'+key,'reasoning_effort':'high','metadata_sha256':'current'}
                    for key,value in fixture.metadata.items() if key != 'new-chat' and value in providers
                ] if fixture.metadata['new-chat'] in providers else [
                    {'id':key,'provider':value,'archived':key=='archived','phase':'pending',
                     'model':'current-'+key,'reasoning_effort':'high','metadata_sha256':'current'}
                    for key,value in fixture.metadata.items() if key != 'new-chat' and value in providers]
            def set_provider(rpc, saved, target, archived):
                calls.append((saved['id'],saved['model'],saved['reasoning_effort'],target,saved['archived']))
                fixture.metadata[saved['id']] = target
            with fixture.boundaries(), mock.object(installer,'inventory',side_effect=inventory), \
                 mock.object(installer,'_set_provider',side_effect=set_provider), mock.object(p,'exchange') as exchange:
                self.assertEqual(installer.uninstall(second)['configuration'],'previous')
                self.assertEqual(installer.uninstall(second)['status'],'restored')
                self.assertEqual(installer.recover(second)['status'],'restored')
                exchange.assert_not_called()
            expected = tomllib.loads(fixture.before)
            expected['mcp_servers']['node_repl']['env']['SKY_CUA_NATIVE_PIPE_DIRECTORY'] = 'after-update'
            self.assertEqual(tomllib.loads(fixture.text()),expected)
            self.assertEqual(fixture.metadata, {**fixture.before_metadata,'new-chat':'openai'})
            self.assertEqual(calls,[('new-chat','ds','low','openai',True),
                                    ('active','current-active','high','openai',False),
                                    ('archived','current-archived','high','deepseek-desktop',True)])
            self.assertEqual((fixture.target/'app.asar').read_text(),'official')
            self.assertFalse(fixture.service['registered'])
            self.assertFalse((fixture.home/'model-adapter-legacy-origins.json').exists())
            self.assertEqual(json.loads((second/'manifest.json').read_text())['status'],'restored')

    def test_interrupted_restoration_continues_after_metadata_and_service_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            with fixture.boundaries(): installer.install(fixture.directory,fixture.manifest)
            fixture.restore_failures = 1
            with fixture.boundaries():
                with self.assertRaisesRegex(RuntimeError,'service fixture removal'):
                    installer.uninstall(fixture.directory)
                self.assertEqual(fixture.metadata,fixture.before_metadata)
                self.assertEqual(tomllib.loads(fixture.text())['model_provider'],PROVIDER)
                calls = len(fixture.metadata_calls)
                self.assertEqual(installer.recover(fixture.directory)['status'],'restored')
                self.assertEqual(len(fixture.metadata_calls),calls)
            self.assertEqual((fixture.target/'app.asar').read_text(),'official')
            self.assertFalse(fixture.service['registered'])


class ShortcutTransactionTests(unittest.TestCase):
    def test_crash_around_link_activation_restores_only_owned_legacy_link(self):
        for phase in ('shortcut_activating','shortcut_activated'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temp:
                fixture=Fixture(Path(temp),fresh=True);link=fixture.root/'owned.lnk';link.write_bytes(b'old')
                fixture.prior_record['windows_install']={'shortcut':str(link),'shortcut_hash':p.file_sha(link)}
                record=installer._record
                def die(directory,transaction,current=None):
                    record(directory,transaction,current)
                    if current==phase:raise PowerLoss()
                with fixture.boundaries(),mock.object(installer.sys,'platform','win32'), \
                     mock('official_client_shortcut.official_entry',return_value={}), \
                     mock('official_client_shortcut.write_link',side_effect=lambda path,entry:path.write_bytes(b'official')), \
                     mock.object(installer,'_record',side_effect=die),mock.object(installer,'_recover_locked',side_effect=PowerLoss):
                    with self.assertRaises(PowerLoss):installer.install(fixture.directory,fixture.manifest)
                with fixture.boundaries(),mock.object(installer.sys,'platform','win32'):
                    self.assertEqual(installer.recover(fixture.directory)['status'],'rolled_back')
                self.assertEqual(link.read_bytes(),b'old');fixture.assert_baseline(self)

    def test_configuration_restore_keeps_successfully_migrated_official_shortcut(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture=Fixture(Path(temp),fresh=True);link=fixture.root/'owned.lnk';link.write_bytes(b'old')
            fixture.prior_record['windows_install']={'shortcut':str(link),'shortcut_hash':p.file_sha(link)}
            with fixture.boundaries(),mock.object(installer.sys,'platform','win32'), \
                 mock('official_client_shortcut.official_entry',return_value={}), \
                 mock('official_client_shortcut.write_link',side_effect=lambda path,entry:path.write_bytes(b'official')):
                installer.install(fixture.directory,fixture.manifest)
                installer.uninstall(fixture.directory)
            self.assertEqual(link.read_bytes(),b'official')
            self.assertEqual((fixture.target/'app.asar').read_text(),'official')


class ServiceReadinessTests(unittest.TestCase):
    def test_cold_start_can_finish_after_twenty_seconds_on_both_platforms(self):
        for platform in ('darwin', 'win32'):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as temp:
                service = installer.AdapterService('proxy', temp)
                clock = [0.0]
                def check(*args, **kwargs):
                    clock[0] += 11
                    ready = clock[0] >= 33
                    return {'ready':ready, 'reason':'ready' if ready else 'timeout'}
                with mock.object(installer.sys,'platform',platform), \
                     mock.object(installer.time,'monotonic',side_effect=lambda:clock[0]), \
                     mock.object(installer.time,'sleep'), mock.object(installer,'health_status',side_effect=check):
                    service._wait_ready({'health_url':'private-token'})
                result = json.loads((Path(temp)/'service-startup.json').read_text())
                self.assertTrue(result['ready'])
                self.assertGreater(result['elapsed_seconds'],20)

    def test_old_listener_can_finish_handover_without_accepting_its_identity(self):
        for platform in ('darwin','win32'):
            with self.subTest(platform=platform),tempfile.TemporaryDirectory() as temp:
                service=installer.AdapterService('ds',temp)
                with mock.object(installer.sys,'platform',platform), mock.object(installer.time,'sleep'), \
                     mock.object(installer,'health_status',side_effect=[
                         {'ready':False,'reason':'identity_mismatch'}, {'ready':True,'reason':'ready'}]) as check:
                    service._wait_ready({})
                self.assertEqual(check.call_count,2)
                self.assertTrue(json.loads((Path(temp)/'service-startup.json').read_text())['ready'])

    def test_persistent_wrong_listener_fails_and_diagnostics_exclude_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            service = installer.AdapterService('ds',temp)
            with mock.object(installer,'health_status',return_value={'ready':False,'reason':'identity_mismatch'}) as check, \
                 mock.object(installer.time,'monotonic',side_effect=[0,0,0,61,61,61]), \
                 mock.object(installer.time,'sleep'), \
                 self.assertRaisesRegex(RuntimeError,'identity_mismatch'):
                service._wait_ready({'health_url':'private-token'})
            check.assert_called_once()
            self.assertNotIn('private-token',(Path(temp)/'service-startup.json').read_text())

    def test_unavailable_listener_reaches_deadline(self):
        with tempfile.TemporaryDirectory() as temp:
            service = installer.AdapterService('ds',temp)
            with mock.object(installer.time,'monotonic',side_effect=[0,0,0,61,61,61]), \
                 mock.object(installer.time,'sleep'), \
                 mock.object(installer,'health_status',return_value={'ready':False,'reason':'unreachable'}), \
                 self.assertRaisesRegex(RuntimeError,'unreachable'):
                service._wait_ready({})


class WindowsServiceTests(unittest.TestCase):
    def test_snapshot_uses_numeric_running_state_independent_of_registration(self):
        for state, enabled in ((3,True), (1,False), (4,True)):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temp:
                service = installer.AdapterService('ds', temp)
                value = {'registered':True, 'loaded':state==4, 'state':state, 'enabled':enabled, 'definition':'fixture'}
                with mock.object(installer.sys,'platform','win32'), \
                     mock.object(p,'run',return_value=Mock(stdout=json.dumps(value).encode())) as run:
                    self.assertEqual(service.snapshot(), value)
                    script = base64.b64decode(run.call_args.args[0][-1]).decode('utf-16le')
                    self.assertIn('$t.State -eq 4', script)
                    self.assertIn('$t.Enabled', script)

    def test_ready_and_disabled_tasks_are_not_started_during_restore(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as temp:
                service = installer.AdapterService('ds',temp)
                previous = {'registered':True, 'loaded':False, 'enabled':enabled, 'definition':'old inference_adapter.py'}
                current = {'registered':True, 'loaded':False, 'enabled':True, 'definition':'new inference_adapter.py'}
                with mock.object(installer.sys,'platform','win32'), \
                     mock.object(service,'snapshot',side_effect=[current,previous]), mock.object(service,'_windows') as command:
                    service.restore(previous)
                ops = [call.args[0] for call in command.call_args_list]
                self.assertNotIn('/Run',ops)
                self.assertEqual(command.call_args.args[-1], '/Enable' if enabled else '/Disable')


if __name__ == '__main__':
    unittest.main()

class RuntimeCommitTests(unittest.TestCase):
    def test_runtime_failure_before_commit_restores_service_and_config(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture=Fixture(Path(temp),update=True)
            state=fixture.root/'monitor';state.mkdir();p.save_json(state/'settings.json',{'edition':'ds'})
            fixture.manifest['update']={'package':str(fixture.root/'package'),'monitor_state_dir':str(state)}
            def fail(package,directory,edition,checkpoint):
                checkpoint({'fixture':'runtime-recovery'})
                raise RuntimeError('runtime failed startup')
            with fixture.boundaries(),mock('updater.activate_runtime',side_effect=fail), \
                 mock('updater.rollback_runtime') as rollback:
                with self.assertRaisesRegex(RuntimeError,'runtime failed startup'):
                    installer.install(fixture.directory,fixture.manifest)
                rollback.assert_called_once_with({'fixture':'runtime-recovery'})
            fixture.assert_baseline(self)
            tx=json.loads((fixture.directory/'native-install-transaction.json').read_text())
            self.assertEqual(tx['phase'],'rolled_back');self.assertTrue(tx['recovered']['runtime'])

    def test_new_installer_commits_idle_prepared_build(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture=Fixture(Path(temp),update=True)
            fixture.manifest['status']='prepared_idle'
            p.save_json(fixture.directory/'manifest.json',fixture.manifest)
            with fixture.boundaries():installer.install(fixture.directory,fixture.manifest)
            self.assertEqual(fixture.manifest['status'],'installed')
            self.assertEqual((fixture.target/'app.asar').read_text(),'official')


class RealBuildValidationTests(unittest.TestCase):
    """Keep the production validator intact; fake only the external app boundary."""
    def stage(self, directory, source, edition, url):
        from official_client import endpoint
        token=url.split('/')[3]
        port=int(url.split(':')[2].split('/')[0])
        profile={'format':1,'edition':edition,'identity':'validator-fixture','token':token,'port':port,'routes':{},
            'gpt':{'chatgpt_base_url':'https://example.invalid/codex','api_base_url':'https://example.invalid/v1'}}
        settings={'edition':edition,'health_identity':profile['identity'],
            'health_url':url.removesuffix('/backend-api/codex')+'/health',
            'adapter_command':[sys.executable,str(directory/'inference_adapter.py'),'serve',str(directory/'adapter-profile.json')]}
        for name,data in [('desktop-models.json',{'models':[]}),('adapter-profile.json',profile),('host-settings.json',settings)]:p.save_json(directory/name,data)
        (directory/'inference_adapter.py').write_text('# isolated fixture',encoding='utf-8')
        return {'architecture':installer.ARCHITECTURE,'status':'prepared_idle','edition':edition,'native_probe':'passed',
                'catalog_sha256':p.file_sha(directory/'desktop-models.json'),'profile_sha256':p.file_sha(directory/'adapter-profile.json'),
                'host_settings_sha256':p.file_sha(directory/'host-settings.json'),'runtime_sha256':{'inference_adapter.py':p.file_sha(directory/'inference_adapter.py')},
                'source_target':str(source),'original_asar':p.file_sha(source/'app.asar')}

    def fixture(self, directory, edition, mode='builtin', previous=None):
        directory.mkdir();source=directory/'official.app';source.mkdir();(source/'app.asar').write_bytes(b'official')
        url='http://127.0.0.1:41232/'+('fixture-'+'x'*40)+'/backend-api/codex'
        manifest=self.stage(directory,source,edition,url)
        current=''
        if previous:
            current,previous=prepare_config('',directory/'desktop-models.json',url,mode='native-summary')
        _,owned=prepare_config(current,directory/'desktop-models.json',url,mode=mode,previous=previous)
        manifest['native_config']=owned
        return manifest

    @contextmanager
    def app_boundary(self):
        with mock.object(installer,'verify_official') as signature, mock.object(p,'app_layout',side_effect=lambda app:{'asar':Path(app)/'app.asar'}):
            yield signature

    def test_real_validate_build_accepts_builtin_none_legacy_and_dormant_provider(self):
        for edition in ('ds','proxy'):
            for mode,previous in [('builtin',False),('native-summary',False),('builtin',True)]:
                with self.subTest(edition=edition,mode=mode,previous=previous),tempfile.TemporaryDirectory() as temp:
                    directory=Path(temp).resolve()/'build';manifest=self.fixture(directory,edition,mode,previous)
                    with mock.object(p,'EDITION',edition),self.app_boundary() as signature:
                        installer.validate_build(directory,manifest)
                    signature.assert_called_once_with(Path(manifest['source_target']))
                    # Cross-drive paths on Windows have no relative form.
                    if directory.drive.lower()==Path.cwd().drive.lower():
                        with mock.object(p,'EDITION',edition),self.app_boundary():
                            installer.validate_build(Path(os.path.relpath(directory)),manifest)

    def test_real_validate_build_rejects_missing_invalid_provider_and_mismatched_identity(self):
        missing=object()
        for edition in ('ds','proxy'):
            with tempfile.TemporaryDirectory() as temp:
                directory=Path(temp).resolve()/'build';original=self.fixture(directory,edition)
                changes=[('provider',value) for value in (missing,False,[],42,'openai',{}, {'base_url':'https://wrong.invalid'})]
                changes += [('selected','foreign'),('selected',None),('root_url','https://wrong.invalid'),('root',None),('ownership',None)]
                for field,value in changes:
                    manifest=copy.deepcopy(original);owned=manifest['native_config']
                    if field=='provider':
                        if value is missing:owned.pop('provider')
                        else:owned['provider']=value
                    elif field=='selected':owned['root']['model_provider']=value
                    elif field=='root_url':owned['root']['openai_base_url']=value
                    elif field=='root':owned['root']=value
                    else:manifest['native_config']=value
                    with self.subTest(edition=edition,field=field,value=value),mock.object(p,'EDITION',edition),self.app_boundary() as signature:
                        with self.assertRaises(p.PatchError):installer.validate_build(directory,manifest)
                        signature.assert_not_called()

    def test_real_validate_build_keeps_digest_health_command_signature_and_probe_guards(self):
        for mutation in ('runtime','health','command','signature','probe'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as temp:
                directory=Path(temp).resolve()/'build';manifest=self.fixture(directory,'ds')
                if mutation=='runtime':(directory/'inference_adapter.py').write_bytes(b'changed')
                elif mutation in ('health','command'):
                    settings=json.loads((directory/'host-settings.json').read_bytes())
                    if mutation=='health':settings['health_identity']='another-service'
                    else:settings['adapter_command'][1]='another-script.py'
                    p.save_json(directory/'host-settings.json',settings);manifest['host_settings_sha256']=p.file_sha(directory/'host-settings.json')
                elif mutation=='signature':(Path(manifest['source_target'])/'app.asar').write_bytes(b'changed')
                else:manifest['native_probe']='failed'
                with mock.object(p,'EDITION','ds'),self.app_boundary():
                    with self.assertRaises(p.PatchError):installer.validate_build(directory,manifest)

    def test_fresh_builtin_install_runs_real_validation_before_transaction_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture=Fixture(Path(temp).resolve(),fresh=True,native_identity=True)
            manifest=fixture.manifest
            url='http://127.0.0.1:41232/'+('fixture-'+'x'*40)+'/backend-api/codex'
            prepared,manifest['native_config']=prepare_config(fixture.before,fixture.directory/'desktop-models.json',url,mode='builtin')
            (fixture.directory/'config-prepared.toml').write_text(prepared,encoding='utf-8')
            manifest.update(self.stage(fixture.directory,fixture.source,'ds',manifest['native_config']['root']['openai_base_url']))
            p.save_json(fixture.directory/'manifest.json',manifest)
            with mock.object(p,'EDITION','ds'),fixture.boundaries(validate=True):
                installer.install(fixture.directory,manifest)
            self.assertEqual(manifest['status'],'installed')
            self.assertEqual(tomllib.loads(fixture.config.read_text())['model_provider'],'openai')
            self.assertEqual(json.loads((fixture.directory/'native-install-transaction.json').read_bytes())['phase'],'committed')

class SimpleUninstallTests(unittest.TestCase):
    def test_uninstall_discovers_latest_owned_build_and_preserves_backups(self):
        from types import SimpleNamespace
        for affinity in ('ds','proxy','unified'):
            with self.subTest(affinity=affinity),tempfile.TemporaryDirectory() as temp:
                root=Path(temp).resolve();state=root/'monitor';state.mkdir();home=root/'home';home.mkdir();build=root/'build';build.mkdir()
                config=home/'config.toml';before='model_catalog_json='+json.dumps(str(build/'desktop-models.json'))+'\nmodel="gpt-fixture"\n'
                config.write_text(before);(home/'auth.json').write_text('fixture-login');(home/'sessions').mkdir();(home/'sessions/keep.jsonl').write_text('fixture-history')
                p.save_json(build/'manifest.json',{'format':1,'edition':affinity,'architecture':installer.ARCHITECTURE,'status':'installed','codex_home':str(home),'target':str(root/'app')})
                p.save_json(state/'settings.json',{'edition':affinity,'codex_home':str(home),'baseline':str(root/'older')})
                p.save_json(state/'state.json',{'installed':{'baseline':str(build)}})
                args=SimpleNamespace(state_dir=state,dry_run=True)
                with mock.object(p,'EDITION','unified'),mock.object(p,'restore',return_value={'status':'restored'}) as restore,mock.object(p,'require_stopped') as stopped:
                    report=p.uninstall(args);self.assertEqual(report['build'],str(build));restore.assert_not_called();stopped.assert_not_called()
                    self.assertFalse((build/'config-before-uninstall.toml').exists())
                    args.dry_run=False;p.uninstall(args)
                    self.assertEqual(restore.call_args.args[0].directory,build)
                    self.assertEqual((build/'config-before-uninstall.toml').read_text(),before)
                self.assertEqual((home/'auth.json').read_text(),'fixture-login')
                self.assertEqual((home/'sessions/keep.jsonl').read_text(),'fixture-history')

    def test_uninstall_rejects_another_manager_without_changes(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();state=root/'monitor';state.mkdir();home=root/'home';home.mkdir();build=root/'build';build.mkdir()
            current='model_catalog_json='+json.dumps(str(root/'another-manager/models.json'))+'\n'
            (home/'config.toml').write_text(current)
            p.save_json(state/'settings.json',{'edition':'ds','codex_home':str(home),'baseline':str(build)})
            p.save_json(build/'manifest.json',{'format':1,'edition':'ds','architecture':installer.ARCHITECTURE,'status':'installed','codex_home':str(home),'target':str(root/'app')})
            with mock.object(p,'EDITION','unified'),mock.object(p,'restore') as restore,self.assertRaisesRegex(p.PatchError,'another configuration'):
                p.uninstall(SimpleNamespace(state_dir=state))
            restore.assert_not_called();self.assertEqual((home/'config.toml').read_text(),current)
            self.assertFalse((build/'config-before-uninstall.toml').exists())
