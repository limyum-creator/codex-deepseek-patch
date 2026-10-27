"""Native update/recovery contracts; never install or launch a desktop app."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch as mock

import monitor
import official_client_update as native
import patch as p
import update_worker
import updater


class NativeIdentityTests(unittest.TestCase):
    def test_mac_verifies_official_chain_without_legacy_integrity_anchors(self):
        value = {'bundle_id':'com.openai.codex', 'team_id':'2DC432GLL2',
                 'cli':'/app/cli', 'code_identifiers':{'/app/cli':'codex'}}
        with mock.object(native.sys, 'platform', 'darwin'), \
             mock('official_client_host.mac_host_identity', return_value=value) as identity, \
             mock.object(p, 'verify_app') as legacy:
            self.assertEqual(native.verify_official('/app')['team'], '2DC432GLL2')
        identity.assert_called_once_with(Path('/app').resolve())
        legacy.assert_not_called()

    def test_mac_rejects_a_different_signed_cli_identifier(self):
        value = {'bundle_id':'com.openai.codex', 'team_id':'2DC432GLL2',
                 'cli':'/app/cli', 'code_identifiers':{'/app/cli':'other'}}
        with mock.object(native.sys, 'platform', 'darwin'), \
             mock('official_client_host.mac_host_identity', return_value=value):
            with self.assertRaisesRegex(p.PatchError, 'CLI identity'):
                native.verify_official('/app')

    def test_windows_keeps_vendor_publisher_verification(self):
        with mock.object(native.sys, 'platform', 'win32'), \
             mock('windows_support.verify_publisher', return_value=[{'name':'OpenAI OpCo, LLC'}]) as publisher, \
             mock.object(p, 'verify_app') as legacy:
            self.assertEqual(native.verify_official('/app')['publisher'][0]['name'], 'OpenAI OpCo, LLC')
        publisher.assert_called_once()
        legacy.assert_not_called()


class NativeBaselineTests(unittest.TestCase):
    def fixture(self, root):
        from official_client_config import prepare_config
        directory, home, app = root/'build', root/'home', root/'official.app'
        for path in (directory, home, app): path.mkdir()
        catalog = {'models':[{'slug':'gpt-fixture','context_window':100000,'max_context_window':100000},
                             {'slug':'deepseek-flash','context_window':100000,'max_context_window':100000}]}
        p.save_json(directory/'desktop-models.json', catalog)
        p.save_json(directory/'adapter-profile.json', {'identity':'owned-profile','token':'private-fixture'})
        (directory/'runtime.py').write_text('owned runtime')
        config, owned = prepare_config('model="gpt-fixture"\n', directory/'desktop-models.json',
            'http://127.0.0.1:12345/private-fixture/backend-api/codex', mode='native-summary')
        (home/'config.toml').write_text(config+'\n[unrelated]\nkeep=true\n')
        record = {'architecture':native.ARCHITECTURE,'status':'installed','target':str(app),
                  'edition':'ds','native_config':owned,'original_asar':'vendor-asar',
                  'catalog_sha256':p.file_sha(directory/'desktop-models.json'),
                  'profile_sha256':p.file_sha(directory/'adapter-profile.json'),
                  'runtime_sha256':{'runtime.py':p.file_sha(directory/'runtime.py')},
                  'model_routes':{'deepseek-flash':{}},
                  'update':{'edition':'ds','architecture':native.ARCHITECTURE,
                            'runtime_digest':'code-1','model_digest':'models-1'}}
        p.save_json(directory/'manifest.json',record)
        settings={'app':str(app),'baseline':str(directory),'codex_home':str(home),'edition':'ds',
                  'state_dir':str(root),'auto_update':False,'model_sync':False}
        return directory,home,app,record,settings

    def test_native_adoption_validates_owned_config_not_patched_asar(self):
        with tempfile.TemporaryDirectory() as temp:
            directory,home,app,record,settings=self.fixture(Path(temp))
            backend=native.NativeMonitorBackend(settings)
            with mock.object(backend,'snapshot',return_value={'fingerprint':'unchanged-vendor'}), \
                 mock.object(p,'app_layout',return_value={'asar':app/'app.asar'}), \
                 mock.object(p,'file_sha',side_effect=lambda path:'vendor-asar' if Path(path)==app/'app.asar' else
                             __import__('hashlib').sha256(Path(path).read_bytes()).hexdigest()):
                self.assertEqual(backend.adopt()['fingerprint'],'unchanged-vendor')
            self.assertNotIn('patched_asar',record)
            self.assertTrue(p.tomllib.loads((home/'config.toml').read_text())['unrelated']['keep'])

    def test_manual_context_reduction_is_accepted_and_runtime_changes_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            directory,home,app,record,settings=self.fixture(Path(temp))
            catalog=json.loads((directory/'desktop-models.json').read_text())
            catalog['models'][1]['context_window']=32000
            p.save_json(directory/'desktop-models.json',catalog)
            native.managed_configuration(directory,record,home)
            (directory/'runtime.py').write_text('unexpected runtime')
            with self.assertRaisesRegex(p.PatchError,'build changed'):
                native.managed_configuration(directory,record,home)

    def test_changed_provider_or_missing_model_stops_adoption(self):
        with tempfile.TemporaryDirectory() as temp:
            directory,home,app,record,settings=self.fixture(Path(temp))
            config=home/'config.toml'
            config.write_text(config.read_text().replace('requires_openai_auth = true','requires_openai_auth = false'))
            with self.assertRaisesRegex(p.PatchError,'provider changed'):
                native.managed_configuration(directory,record,home)
            config.write_text(config.read_text().replace('requires_openai_auth = false','requires_openai_auth = true'))
            catalog=json.loads((directory/'desktop-models.json').read_text());catalog['models'].pop()
            p.save_json(directory/'desktop-models.json',catalog)
            with self.assertRaisesRegex(p.PatchError,'route disappeared'):
                native.managed_configuration(directory,record,home)

    def test_native_running_check_ignores_obsolete_windows_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'CodexDeepSeekPatch/app').mkdir(parents=True)
            backend=object.__new__(native.NativeMonitorBackend);backend.app=root/'official-store-app'
            with mock.object(native.sys,'platform','win32'), mock.dict(os.environ,{'LOCALAPPDATA':str(root)}), \
                 mock.object(p,'require_stopped') as stopped:
                self.assertFalse(backend.is_running())
                stopped.assert_called_once_with(backend.app,read_only_source=True)

    def test_store_snapshot_follows_registered_official_app_and_does_not_write(self):
        backend=object.__new__(native.NativeMonitorBackend);backend.store=True;backend.app=Path('/old-store')
        with mock('windows_support.discover_app',return_value=Path('/new-store')) as discover, \
             mock.object(native,'verify_official') as verify, \
             mock('backend.snapshot_app',return_value={'fingerprint':'vendor-v2'}) as snapshot:
            self.assertEqual(backend.snapshot()['fingerprint'],'vendor-v2')
        discover.assert_called_once();verify.assert_called_once_with(Path('/new-store'))
        snapshot.assert_called_once_with(Path('/new-store'))

    def test_legacy_baseline_stays_on_legacy_stop_and_restore_path(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp);p.save_json(directory/'manifest.json',{'patched_asar':'legacy'})
            settings={'app':'/legacy','baseline':str(directory),'architecture':native.ARCHITECTURE}
            with mock('backend.MonitorBackend') as legacy:
                self.assertIs(native.select_backend(settings),legacy.return_value)
            legacy.assert_called_once()


class WorkerTests(unittest.TestCase):
    def test_candidate_identity_advertises_architecture(self):
        with mock.object(p,'ARCHITECTURE',native.ARCHITECTURE,create=True), \
             mock.object(updater,'runtime_digest',return_value='digest'):
            self.assertEqual(update_worker.execute('identity',{})['architecture'],native.ARCHITECTURE)

    def test_old_runtime_job_defaults_to_new_candidate_architecture(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);p.save_json(root/'model-preferences.json',{'context_windows':{'local':32000}})
            job={'app':str(root/'official'),'baseline':str(root/'prior'),'output':str(root/'next'),
                 'settings':{'codex_home':str(root/'home'),'state_dir':str(root),'edition':'ds'}}
            with mock.object(p,'ARCHITECTURE',native.ARCHITECTURE,create=True), \
                 mock('official_client.stage') as stage, mock.object(update_worker,'verify_build') as verify, \
                 mock.object(monitor,'migrate_windows_working_directory') as migrate:
                result=update_worker.execute('build',job)
            self.assertEqual(result['architecture'],native.ARCHITECTURE)
            self.assertEqual(stage.call_args.kwargs['model_preferences'],{'context_windows':{'local':32000}})
            self.assertEqual(stage.call_args.kwargs['update_metadata']['architecture'],native.ARCHITECTURE)
            self.assertEqual(stage.call_args.kwargs['app'],root/'official')
            verify.assert_called_once_with(root/'next');migrate.assert_called_once_with(str(root))

    def test_native_gate_requires_install_validation_before_marking_success(self):
        for status in ('passed','failed','invalid-install'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp:
                root=Path(temp);record={'architecture':native.ARCHITECTURE,'edition':'ds','source_target':'/official',
                    'native_probe':'passed','native_probe_report':'previous-run','status':'prepared','update':{'monitor_state_dir':str(root)}}
                from official_client import ADAPTER_FILES
                record['runtime_sha256']={}
                for name in ADAPTER_FILES:
                    (root/name).write_bytes((p.ROOT/name).read_bytes())
                    record['runtime_sha256'][name]=p.file_sha(root/name)
                for name,field in [('desktop-models.json','catalog_sha256'),('adapter-profile.json','profile_sha256'),('host-settings.json','host_settings_sha256')]:
                    (root/name).write_text('{}',encoding='utf-8');record[field]=p.file_sha(root/name)
                p.save_json(root/'manifest.json',record)
                with mock('scripts.test_official_client.run',return_value={'status':'passed' if status=='invalid-install' else status}) as run, \
                     mock('official_client_install.validate_build',side_effect=p.PatchError('install validation failed') if status=='invalid-install' else None) as validation, \
                     mock('scripts.test_boot.main') as old:
                    if status=='passed': update_worker.verify_build(root)
                    else:
                        with self.assertRaisesRegex(p.PatchError,'validation failed'):update_worker.verify_build(root)
                run.assert_called_once_with(Path('/official'),root/'native-probe','ds');old.assert_not_called()
                self.assertEqual(json.loads((root/'manifest.json').read_text()).get('native_probe'),
                                 'passed' if status=='passed' else None)
                if status=='passed':
                    saved=json.loads((root/'manifest.json').read_text());self.assertEqual(saved['status'],'prepared_idle')
                    self.assertEqual(saved['native_install_validation'],'passed');validation.assert_called_once()
                elif status=='failed':validation.assert_not_called()

    def test_native_install_does_not_call_legacy_bundle_installer(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);p.save_json(root/'manifest.json',{'architecture':native.ARCHITECTURE})
            install=Mock();module=types.SimpleNamespace(install=install)
            with mock.dict(sys.modules,{'official_client_install':module}), \
                 mock.object(update_worker.backend,'install_compatible') as legacy:
                self.assertEqual(update_worker.execute('install',{'directory':str(root)})['status'],'installed')
            install.assert_called_once();legacy.assert_not_called()


class NativeUpdateRecoveryTests(unittest.TestCase):
    def test_native_candidate_build_preserves_original_app_and_channel_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);app=root/'official';app.mkdir();(app/'vendor').write_text('unchanged')
            prior=root/'prior';prior.mkdir();p.save_json(prior/'manifest.json',{'architecture':native.ARCHITECTURE})
            obj=object.__new__(updater.UpdateBackend);obj.base=Mock(app=app,baseline=prior)
            obj.snapshot=lambda:{'fingerprint':'candidate'};obj.settings={'edition':'proxy'}
            obj.package=root/'package';obj.directory=root;obj.edition='proxy';obj.model_snapshot={'digest':'models'}
            obj.desired_runtime='code';obj.desired_architecture=native.ARCHITECTURE
            obj.cache={'release':{'edition':'proxy','branch':updater.CHANNELS['proxy'],'sequence':12}}
            with mock.object(updater,'worker') as worker:
                obj.build({'fingerprint':'candidate','model_digest':'models'},root/'next')
            job=worker.call_args.args[2]
            self.assertEqual(job['app'],str(app));self.assertIsNone(job['install_target'])
            self.assertEqual(job['update']['architecture'],native.ARCHITECTURE)
            self.assertEqual(job['update']['release']['branch'],updater.CHANNELS['proxy'])
            self.assertEqual((app/'vendor').read_text(),'unchanged')

    def test_committed_native_install_switches_backend_and_retries_activation(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);build=root/'build';build.mkdir()
            p.save_json(build/'manifest.json',{'status':'installed','architecture':native.ARCHITECTURE,'target':'/official'})
            obj=object.__new__(updater.UpdateBackend);obj.directory=root;obj.package=root;obj.edition='ds'
            obj.settings={'auto_update':False,'model_sync':False};obj.base=Mock()
            obj.snapshot=lambda:{'fingerprint':'candidate'};obj.desired_runtime='code'
            obj.promote=Mock(side_effect=OSError('locked runtime'));obj.cache={};obj.warnings=[]
            obj.cache_path=root/'updates.json'
            base=Mock(app=Path('/official'));base.snapshot.return_value={'fingerprint':'vendor'}
            with mock.object(native,'select_backend',return_value=base),mock.object(updater,'worker'):
                obj.install({'directory':str(build)},{'fingerprint':'candidate','model_digest':'models'})
            self.assertIs(obj.base,base);self.assertEqual(obj.settings['architecture'],native.ARCHITECTURE)
            self.assertTrue((root/'activation-pending.json').exists())
            obj.prepared=False;obj.promote=Mock()
            with mock.object(native,'select_backend',return_value=base), \
                 mock.object(updater,'worker',return_value={'edition':'ds','architecture':native.ARCHITECTURE,'runtime_digest':'code'}):
                obj.prepare()
            obj.promote.assert_called_once_with(build)
            self.assertFalse((root/'activation-pending.json').exists())

    def test_native_enable_has_no_external_node_requirement_and_saves_architecture(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);args=argparse.Namespace(state_dir=root,interval=300,app=Path('/official'),
                baseline=None,codex_home=root/'home',keychain_account=None,keychain_service=None)
            with mock.object(monitor.sys,'platform','darwin'), \
                 mock.object(p,'ARCHITECTURE',native.ARCHITECTURE,create=True), \
                 mock.object(p,'MonitorBackend') as backend, mock.object(monitor.shutil,'which',return_value=None), \
                 mock.object(monitor,'Scheduler') as scheduler, mock.object(monitor,'stage_runtime'):
                scheduler.return_value.status.return_value={'enabled':True}
                monitor.enable(args)
            saved=monitor.load_json(root/'settings.json')
            self.assertEqual(saved['architecture'],native.ARCHITECTURE);self.assertNotIn('node_path',saved)
            backend.return_value.snapshot.assert_called_once()

    def test_native_release_requires_native_runtime_and_keeps_channel_pin(self):
        from tests.test_updater import UpdateTests
        value,_=UpdateTests().release('ds');value['architecture']=native.ARCHITECTURE
        with self.assertRaisesRegex(p.PatchError,'Incomplete'):updater.validate_release(value,'ds')
        with self.assertRaisesRegex(p.PatchError,'different edition'):updater.validate_release(value,'proxy')

    def test_native_package_preflight_uses_python_without_external_node(self):
        from tests.test_updater import UpdateTests
        value,contents=UpdateTests().release('ds');value['architecture']=native.ARCHITECTURE
        names={'official_client.py','official_client_install.py','official_client_update.py',
               'official_client_config.py','official_client_host.py','inference_adapter.py',
               'inference_transport.py','legacy_history.py','reasoning_origin.py','chat_completions_compat.py',
               'official_client_migration.py','scripts/test_official_client.py',
               'update_progress.py','update_progress_macos.js','update_progress.ps1','tests/test_official_client_update.py'}
        names.update(name.replace('.','/')+'.py' for name in updater.NATIVE_PREFLIGHT_MODULES)
        contents.update({name:b'fixture\n' for name in names})
        value['files']={name:hashlib.sha256(raw).hexdigest() for name,raw in contents.items()}
        updater.validate_release(value,'ds')
        with tempfile.TemporaryDirectory() as temp, mock.object(updater.shutil,'which',return_value=None), \
             mock.object(updater.subprocess,'run',return_value=Mock(returncode=0,stderr='')) as runner:
            updater.source_package(value,temp,lambda url:contents[url.split('/'+value['commit']+'/',1)[1]])
        self.assertEqual(runner.call_count,2)
        self.assertEqual(runner.call_args_list[0].args[0][:2],[sys.executable,'-c'])
        self.assertIn('ast.parse',runner.call_args_list[0].args[0][2])
        command=runner.call_args.args[0]
        self.assertEqual(command[:3],[sys.executable,'-m','unittest'])
        self.assertIn('tests.test_official_client_config',command)
        self.assertNotIn('tests.test_management',command)
        self.assertNotIn('tests.test_official_client_install',command)
        self.assertNotIn('tests/route.test.cjs',command)

    def test_native_baseline_refuses_a_legacy_candidate_even_without_update_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);p.save_json(root/'manifest.json',{'architecture':native.ARCHITECTURE})
            obj=object.__new__(updater.UpdateBackend);obj.base=Mock(baseline=root)
            obj.directory=root;obj.prepared=False;obj.settings={'auto_update':False,'model_sync':False}
            obj.cache={};obj.edition='ds';obj.package=root;obj.warnings=[]
            with mock.object(updater,'worker',return_value={'edition':'ds','runtime_digest':'old'}):
                with self.assertRaisesRegex(p.PatchError,'cannot restore a legacy'):
                    obj.prepare()

    def test_architecture_is_imported_after_old_runtime_finished_activation(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);build=root/'build';build.mkdir()
            p.save_json(build/'manifest.json',{'architecture':native.ARCHITECTURE,'status':'installed','target':'/official'})
            monitor.atomic_json(root/'settings.json',{'edition':'ds','baseline':str(build),'custom':'preserved'})
            obj=object.__new__(updater.UpdateBackend);obj.directory=root;obj.edition='ds';obj.settings={}
            obj.persist_architecture(build)
            saved=monitor.load_json(root/'settings.json')
            self.assertEqual(saved['architecture'],native.ARCHITECTURE)
            self.assertEqual(saved['custom'],'preserved')
            self.assertEqual(saved['baseline'],str(build))


class DiscoveryOrderingTests(unittest.TestCase):
    def test_store_package_is_rediscovered_before_model_refresh(self):
        obj=object.__new__(updater.UpdateBackend)
        obj.package=p.ROOT;obj.prepared=False;obj.settings={'app':'/Store/removed-old'}
        obj.desired_runtime='code';obj.model_snapshot={'digest':'models'};obj.warnings=[]
        obj.base=Mock(spec=['app','baseline','snapshot'],app=Path('/Store/removed-old'),baseline=None)
        def discover():
            obj.base.app=Path('/Store/current')
            return {'fingerprint':'current-app'}
        obj.base.snapshot.side_effect=discover
        observed=[]
        def prepare():
            observed.append(obj.settings['app']);obj.prepared=True
        obj.prepare=prepare
        result=obj.snapshot()
        self.assertEqual(observed,[str(Path('/Store/current'))])
        self.assertEqual(result['app_fingerprint'],'current-app')

    def test_native_discovery_does_not_repeat_full_signature_and_hash_check(self):
        obj=object.__new__(updater.UpdateBackend)
        obj.package=p.ROOT;obj.prepared=False;obj.settings={'app':'/Store/old'}
        obj.desired_runtime='code';obj.model_snapshot={'digest':'models'};obj.warnings=[]
        obj.base=Mock(app=Path('/Store/old'),baseline=None)
        def discover():obj.base.app=Path('/Store/current')
        obj.base.refresh_source.side_effect=discover
        obj.base.snapshot.return_value={'fingerprint':'current-app'}
        observed=[]
        def prepare():observed.append(obj.settings['app']);obj.prepared=True
        obj.prepare=prepare
        obj.snapshot()
        self.assertEqual(observed,[str(Path('/Store/current'))])
        obj.base.refresh_source.assert_called_once()
        obj.base.snapshot.assert_called_once()

    def test_first_monitor_enable_can_use_native_installed_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);build=root/'build';build.mkdir()
            p.save_json(build/'manifest.json',{'architecture':native.ARCHITECTURE,'edition':p.EDITION,'target':str(root/'Official')})
            args=argparse.Namespace(state_dir=root/'state',interval=300,app=None,baseline=build,
                codex_home=root/'home',keychain_account=None,keychain_service=None)
            with mock.object(monitor.sys,'platform','darwin'), \
                 mock('official_client_update.NativeMonitorBackend') as backend, \
                 mock.object(monitor,'Scheduler') as scheduler, mock.object(monitor,'stage_runtime'):
                scheduler.return_value.status.return_value={'enabled':True}
                monitor.enable(args)
            saved=monitor.load_json(root/'state/settings.json')
            self.assertEqual(saved['app'],str(root/'Official'))
            backend.return_value.snapshot.assert_called_once()


if __name__=='__main__':unittest.main()


class RecoverySelectionTests(unittest.TestCase):
    def test_recovery_selects_owned_pending_candidate_and_refuses_crossed_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();base=root/'builds/current';pending=root/'builds/next';base.mkdir(parents=True);pending.mkdir()
            for edition in ('ds','proxy'):
                monitor.atomic_json(root/'settings.json',{'edition':edition,'codex_home':str(root/'home')})
                monitor.atomic_json(pending/'manifest.json',{'edition':edition,'codex_home':str(root/'home'),'tool_version':'next'})
                monitor.atomic_json(root/'state.json',{'pending':{'build':{'directory':str(pending)}}})
                monitor.atomic_json(pending/'native-install-transaction.json',{'phase':'config_installed'})
                self.assertEqual(update_worker.recovery_target(root,base),pending)
                value=update_worker.recovery_summary(root,base)
                self.assertTrue(value['interrupted']);self.assertEqual(value['candidate_version'],'next')
                monitor.atomic_json(pending/'native-install-transaction.json',{'phase':'rolled_back'})
                self.assertFalse(update_worker.recovery_summary(root,base)['interrupted'])
                monitor.atomic_json(pending/'manifest.json',{'edition':'foreign','codex_home':str(root/'home')})
                with self.assertRaises(p.PatchError):update_worker.recovery_target(root,base)
            monitor.atomic_json(root/'state.json',{'pending':{'build':{'directory':str(root.parent/'elsewhere')}}})
            with self.assertRaises(p.PatchError):update_worker.recovery_target(root,base)
