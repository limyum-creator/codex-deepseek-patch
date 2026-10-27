from windows_support import background_options
"""Headless lifecycle tests: no desktop view, application or real upstream."""
import json
import base64
import re
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import monitor
import patch
import update_progress as u
import update_worker
import updater


class ProgressTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.progress=u.Progress(self.root)
        self.identity=self.progress.begin('proxy','0.5.0-proxy.1','candidate')
        self.spawn=mock.patch.object(u,'_spawn');self.spawn_mock=self.spawn.start()
        self.addCleanup(self.spawn.stop)
        self.spawn_mock.return_value.pid=12345

    def settings(self):
        value={'edition':'proxy','app':str(self.root/'Codex.app'), 'codex_home':str(self.root/'home'),
               'python_path':str(Path(__import__('sys').executable).resolve()),'state_dir':str(self.root)}
        monitor.atomic_json(self.root/'settings.json',value)
        return value

    def build(self,status='prepared'):
        directory=self.root/'build';directory.mkdir(exist_ok=True)
        home=self.root/'home';home.mkdir(exist_ok=True)
        (home/'config.toml').write_text('model="gpt"\n')
        (directory/'config-before.toml').write_text('model="gpt"\n')
        manifest={'architecture':'official-client-v1','status':status,'edition':'proxy','native_probe':'passed',
                  'target':str(self.root/'Codex.app'),'source_target':str(self.root/'official.app'),
                  'codex_home':str(home),'update':self.progress.metadata()}
        monitor.atomic_json(directory/'manifest.json',manifest)
        self.progress.publish('waiting_for_exit',build=directory)
        return directory,manifest

    def test_only_sanitized_fixed_ui_fields(self):
        self.progress.publish('failed')
        value=self.progress.read()
        self.assertEqual(set(value),{'format','id','edition','version','phase','title','detail','completed','total','can_start','can_launch','can_cancel','unit','percent','display','phase_started_at'})
        self.assertNotIn(str(self.root),json.dumps(value))
        self.assertFalse(value['can_start']);self.assertFalse(value['can_launch'])

    def test_counts_real_or_indeterminate(self):
        self.progress.publish('downloading',3,9)
        self.assertEqual((self.progress.read()['completed'],self.progress.read()['total']),(3,9))
        self.progress.publish('verifying')
        self.assertIsNone(self.progress.read()['completed'])
        for count,total in [(3,2),(1,None),(True,5),(0,0)]:
            self.assertIsNone(self.progress.publish('downloading',count,total))
            self.assertEqual(self.progress.read()['phase'],'verifying')

    def test_dismiss_does_not_cancel_or_request(self):
        u.action(self.root,self.identity,'dismiss','available')
        self.assertFalse(self.progress.requested());self.assertEqual(self.progress.read()['phase'],'available')
        self.spawn_mock.assert_not_called()

    def test_model_only_sync_never_reuses_a_prior_update_window(self):
        self.settings();directory,manifest=self.build()
        manifest['update']['quiet_model_sync']=True
        self.assertIsNone(u.from_manifest(manifest))
        self.assertIsNone(u.worker_progress('build',{'update':{'quiet_model_sync':True}},directory/'job.json'))
        self.spawn_mock.assert_not_called()

    def test_identity_poll_preserves_terminal_and_active_update_progress(self):
        job = self.root/'worker-identity.json'
        monitor.atomic_json(job, {})
        for phase in ('available','checking','failed','completed','waiting_for_exit',
                      'installing','verifying'):
            with self.subTest(phase=phase):
                self.identity=self.progress.begin('proxy','0.5.0-proxy.1','case-'+phase)
                self.progress.publish(phase)
                with mock.patch.object(u,'worker_progress',return_value=self.progress), \
                     mock.patch.object(update_worker,'execute',return_value={'identity':'same'}):
                    self.assertEqual(update_worker.main(['identity',str(job)]),0)
                expected = 'checking' if phase == 'available' else phase
                self.assertEqual(self.progress.read()['phase'],expected)
                if phase not in ('available','checking'):
                    with mock.patch.object(u,'worker_progress',return_value=self.progress), \
                         mock.patch.object(update_worker,'execute',side_effect=OSError('fixture')), \
                         mock.patch.object(update_worker.sys,'stderr'):
                        self.assertEqual(update_worker.main(['identity',str(job)]),1)
                    self.assertEqual(self.progress.read()['phase'],phase)

    def test_id_and_action_allowlist(self):
        for identity,action in [('old','start'),(self.identity,'exec')]:
            with self.assertRaises(ValueError):u.action(self.root,identity,action)
        self.assertFalse(self.progress.requested());self.spawn_mock.assert_not_called()

    def test_start_during_verification_is_durable_and_not_close(self):
        self.settings();self.progress.publish('verifying')
        with mock.patch.object(u,'graceful_close') as close:
            u.action(self.root,self.identity,'start')
        close.assert_not_called();self.assertTrue(self.progress.requested())
        self.assertFalse(self.progress.read()['can_start'])
        self.assertIn('run-once',self.spawn_mock.call_args.args[0])

    def test_native_worker_after_verification_consumes_start(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(u,'previous_host_settings',return_value={'fixture':True}).start()
        mock.patch('official_client_host.activity_status',return_value={'known':True,'acquired':True}).start()
        self.settings();directory,manifest=self.build()
        monitor.atomic_json(self.root/'progress.request.json',{'id':self.identity,'action':'start'})
        with mock.patch('official_client_install.validate_build') as validate, \
             mock.patch('official_client_install._guard_staged_config') as guard, \
             mock.patch.object(u,'graceful_close') as close, mock.patch.object(patch,'require_stopped'):
            self.assertTrue(u.validated_close(self.progress))
        validate.assert_called_once_with(directory,manifest);guard.assert_called_once();close.assert_called_once()
        self.assertEqual(self.progress.read()['phase'],'closing')

    def test_validation_failure_never_closes(self):
        self.settings();self.build()
        monitor.atomic_json(self.root/'progress.request.json',{'id':self.identity,'action':'start'})
        with mock.patch('official_client_install.validate_build',side_effect=ValueError('changed')),mock.patch.object(u,'graceful_close') as close:
            self.assertFalse(u.validated_close(self.progress))
        close.assert_not_called()

    def test_no_request_never_closes(self):
        self.build()
        with mock.patch.object(u,'graceful_close') as close:
            self.assertFalse(u.validated_close(self.progress))
        close.assert_not_called()

    def test_legacy_deployment_also_quits_on_migration(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(u,'previous_host_settings',return_value={'fixture':True}).start()
        mock.patch('official_client_host.activity_status',return_value={'known':True,'acquired':True}).start()
        directory,manifest=self.build();prior=self.root/'legacy';prior.mkdir()
        manifest['legacy_baseline']=str(prior)
        monitor.atomic_json(prior/'manifest.json',{'deployment_target':str(self.root/'legacy-app')})
        monitor.atomic_json(directory/'manifest.json',manifest)
        monitor.atomic_json(self.root/'progress.request.json',{'id':self.identity,'action':'start'})
        with mock.patch('official_client_install.validate_build'),mock.patch('official_client_install._guard_staged_config'), \
             mock.patch.object(u,'graceful_close') as close,mock.patch.object(patch,'require_stopped'):
            self.assertTrue(u.validated_close(self.progress))
        self.assertEqual(set(close.call_args.args[0]),{manifest['target'],str(self.root/'legacy-app')})

    def test_no_launch_before_completion_or_without_click(self):
        self.build(status='installed')
        with mock.patch.object(u,'launch_official') as launch:
            with self.assertRaises(ValueError):u.action(self.root,self.identity,'launch')
            self.progress.publish('completed')
            launch.assert_not_called()
            u.action(self.root,self.identity,'launch')
            launch.assert_called_once()

    def test_launch_claim_is_once_per_update_even_after_republish(self):
        self.build(status='installed');self.progress.publish('completed')
        with mock.patch.object(u,'launch_official') as launch:
            u.action(self.root,self.identity,'launch')
            with self.assertRaises(ValueError):u.action(self.root,self.identity,'launch')
            self.progress.publish('completed')
            with self.assertRaises(ValueError):u.action(self.root,self.identity,'launch')
        launch.assert_called_once();self.assertFalse(self.progress.read()['can_launch'])
        self.assertEqual(monitor.load_json(self.root/'progress.launch.json')['id'],self.identity)

    def test_launch_failure_releases_claim_and_allows_retry(self):
        self.build(status='installed');self.progress.publish('completed')
        with mock.patch.object(u,'launch_official',side_effect=ValueError('launch failed')):
            with self.assertRaises(ValueError):
                u.action(self.root,self.identity,'launch')
        self.assertTrue(self.progress.read()['can_launch'])
        self.assertFalse((self.root/'progress.launch.json').exists())
        with mock.patch.object(u,'launch_official') as launch:
            u.action(self.root,self.identity,'launch')
        launch.assert_called_once()

    def test_recheck_keeps_same_release_build_for_close_and_launch(self):
        for installed in (False, True):
            directory,manifest=self.build(status='installed' if installed else 'prepared')
            manifest['update'].update(runtime_digest='runtime-fixture',
                                      release={'version':'0.5.0-proxy.1'})
            monitor.atomic_json(directory/'manifest.json',manifest)
            state_key='installed' if installed else 'pending'
            monitor.atomic_json(self.root/'state.json',{state_key:{'build':{'directory':str(directory)}}})
            new_id=self.progress.begin('proxy','0.5.0-proxy.1','runtime-fixture:official-hash-'+str(installed),restart=True)
            self.assertNotEqual(new_id,manifest['update']['progress_id'])
            self.progress.publish('completed' if installed else 'waiting_for_exit')
            self.assertEqual(u._build(self.progress),(directory,manifest))
            manifest['update']['release']['version']='different-version'
            monitor.atomic_json(directory/'manifest.json',manifest)
            self.assertIsNone(u._build(self.progress))

    def test_dismissed_available_can_present_completed_once(self):
        self.settings()
        with mock.patch.object(u.sys,'platform','darwin'),mock.patch.object(u,'_alive',return_value=False):
            self.progress.present();self.progress.present()
            self.assertEqual(self.spawn_mock.call_count,1)
            u.action(self.root,self.identity,'dismiss','available')
            self.progress.publish('completed',present=True)
            self.progress.present()
            self.assertEqual(self.spawn_mock.call_count,2)
        command=self.spawn_mock.call_args.args[0]
        self.assertEqual(Path(command[1]).name,'management.py')
        self.assertEqual(command[2:4],['show','--state-dir'])
        self.assertEqual(command[-1],'--notify-update')
        self.assertNotIn('--page',command)

    def test_monitor_update_notice_uses_whale_instead_of_an_os_balloon(self):
        self.settings()
        with mock.patch.object(monitor,'native_notify') as notification, \
             mock.patch.object(u.Progress,'present') as present:
            watcher=monitor.Monitor(self.root,object(),notifier=notification)
            watcher.notify('installed','fixture','Ready')
        present.assert_called_once();notification.assert_not_called()

    def test_live_panel_not_duplicated_on_completion(self):
        self.settings()
        with mock.patch.object(u.sys,'platform','darwin'),mock.patch.object(u,'_alive',return_value=True):
            self.progress.present();self.progress.publish('completed',present=True)
        self.assertEqual(self.spawn_mock.call_count,1)

    def test_explicit_reopen_reuses_live_window_without_popup_loop(self):
        self.settings()
        with mock.patch.object(u.sys,'platform','darwin'),mock.patch.object(u,'_alive',return_value=False):
            self.progress.present()
            u.action(self.root,self.identity,'dismiss','available')
            self.progress.present(force=True)
            self.progress.present()
        self.assertEqual(self.spawn_mock.call_count,2)
        with mock.patch.object(u,'_alive',return_value=True):
            self.progress.present(force=True)
        self.assertEqual(self.spawn_mock.call_count,2)
        self.assertEqual(monitor.load_json(self.root/'progress.ui.json')['presented'],['available'])

    def test_failed_state_survives_reopen_and_same_version_not_spam(self):
        self.settings()
        with mock.patch.object(u.sys,'platform','darwin'),mock.patch.object(u,'_alive',return_value=False):
            self.progress.publish('failed',present=True)
            identity=u.Progress(self.root).begin('proxy','0.5.0-proxy.1','candidate')
            self.progress.present()
        self.assertEqual(identity,self.identity);self.assertEqual(self.spawn_mock.call_count,1)
        self.assertEqual(self.progress.read()['phase'],'failed')

    def test_new_candidate_invalidates_old_actions(self):
        self.progress.begin('proxy','0.5.1-proxy.1','new')
        with self.assertRaises(ValueError):u.action(self.root,self.identity,'start')

    def test_manifest_edition_and_id_must_match(self):
        _,manifest=self.build()
        self.assertIsNotNone(u.from_manifest(manifest))
        manifest['edition']='ds';self.assertIsNone(u.from_manifest(manifest))

    def test_bootstrap_identity_only_safe_job_location(self):
        self.settings();(self.root/'progress.json').unlink()
        with mock.patch.object(u,'for_settings',return_value=self.progress) as hook, \
             mock.patch.object(patch,'EDITION','proxy'):
            self.assertIsNone(u.worker_progress('identity',{},self.root/'random.json'))
            self.assertIs(u.worker_progress('identity',{},self.root/'identity/worker-identity.json'),self.progress)
        hook.assert_called_once()

    def test_bootstrap_preserves_previous_available_start_intent(self):
        settings=self.settings()
        monitor.atomic_json(self.root/'progress.request.json',{'id':self.identity,'action':'start'})
        with mock.patch.object(patch,'EDITION','proxy'),mock.patch.object(updater,'edition',return_value={'version':'0.5.0-proxy.1'}):
            progress=u.worker_progress('build',{'settings':settings},self.root/'worker-build.json')
        self.assertEqual(progress.read()['id'],self.identity);self.assertTrue(progress.requested())

    def test_background_process_disconnected_and_no_console(self):
        self.spawn.stop()
        try:
            with mock.patch.object(u.subprocess,'Popen') as popen, mock.patch.object(u.os,'name','nt'), \
                 mock.patch.object(u,'background_options',return_value={'creationflags':123}):
                u._spawn(['pythonw.exe','worker'],self.root)
            self.assertEqual(popen.call_args.kwargs['creationflags'],123)
            self.assertEqual(popen.call_args.kwargs['stdin'],subprocess.DEVNULL)
        finally:self.spawn.start()

    def test_windows_view_command_uses_fixed_argv(self):
        self.settings()
        with mock.patch.object(u.sys,'platform','win32'),mock.patch.object(u,'_python',return_value='C:/Python/pythonw.exe'):
            self.progress.present()
        args=self.spawn_mock.call_args.args[0]
        self.assertEqual(args[0],'C:/Python/pythonw.exe')
        self.assertEqual(Path(args[1]).name,'management.py')
        self.assertEqual(args[2:],['show','--state-dir',str(self.root),'--notify-update'])
        self.assertNotIn('powershell.exe',args)

    def test_existing_whale_receives_badge_then_explicit_page_without_another_window(self):
        self.settings()
        monitor.atomic_json(self.root/'settings-ui/host.json',{'edition':'proxy','pid':9876,'view':'whale-v1'})
        with mock.patch.object(u.sys,'platform','darwin'),mock.patch.object(u,'_alive',return_value=True):
            self.progress.present()
            notification=monitor.load_json(self.root/'settings-ui/raise.json')
            self.assertTrue(notification['notify']);self.assertNotIn('page',notification)
            self.progress.present(force=True)
            opened=monitor.load_json(self.root/'settings-ui/raise.json')
            self.assertEqual(opened['page'],'updates');self.assertNotIn('notify',opened)
        self.spawn_mock.assert_not_called()

    def test_first_upgrade_keeps_existing_legacy_progress_until_it_closes(self):
        self.settings()
        monitor.atomic_json(self.root/'progress.ui.json',{'id':self.identity,'pid':9876,'presented':['available']})
        with mock.patch.object(u.sys,'platform','darwin'),mock.patch.object(u,'_alive',return_value=True):
            self.progress.publish('completed',present=True)
            self.progress.present(force=True)
            self.spawn_mock.assert_not_called()
        self.assertEqual(monitor.load_json(self.root/'progress.ui.json')['presented'],['available','completed'])
        with mock.patch.object(u.sys,'platform','darwin'),mock.patch.object(u,'_alive',return_value=False):
            self.progress.present(force=True)
        self.spawn_mock.assert_called_once()
        self.assertEqual(Path(self.spawn_mock.call_args.args[0][1]).name,'management.py')
        self.assertEqual(monitor.load_json(self.root/'progress.ui.json')['view'],'whale')

    def test_legacy_editor_is_not_reused_as_whale_or_interrupted(self):
        self.settings()
        monitor.atomic_json(self.root/'settings-ui/host.json',{'edition':'proxy','pid':9876})
        with mock.patch.object(u.sys,'platform','darwin'),mock.patch.object(u,'_alive',return_value=True):
            self.progress.present()
            self.assertEqual(self.spawn_mock.call_count,1)
            self.assertNotEqual(monitor.load_json(self.root/'progress.ui.json')['pid'],9876)
            self.progress.present(force=True)
            self.assertEqual(self.spawn_mock.call_count,1)
        self.assertFalse((self.root/'settings-ui/raise.json').exists())
        self.assertEqual(monitor.load_json(self.root/'settings-ui/entry.json')['page'],'updates')
        self.assertEqual(monitor.load_json(self.root/'settings-ui/host.json')['pid'],9876)

    def test_native_view_hides_console_without_hiding_gui(self):
        self.spawn.stop()
        try:
            with mock.patch.object(u.subprocess,'Popen') as popen,mock.patch.object(u.os,'name','nt'):
                u._spawn(['powershell.exe','view'],self.root,view=True)
            self.assertEqual(popen.call_args.kwargs['creationflags'],0x08000000)
            self.assertNotIn('startupinfo',popen.call_args.kwargs)
        finally:self.spawn.start()

    def test_macos_gui_architecture_is_explicit_for_finder_launch(self):
        for machine in ('arm64','x86_64'):
            with mock.patch('platform.machine',return_value=machine):
                command=u.macos_ui_command('/private/test.js','/private/state with spaces')
            self.assertEqual(command[:3],['/usr/bin/arch','-'+machine,'/usr/bin/osascript'])
            self.assertEqual(command[-1],'/private/state with spaces')

    def test_same_candidate_recheck_preserves_start_and_verified_build(self):
        settings=self.settings()
        for phase in ('available','checking','waiting_for_exit','failed','completed'):
            with self.subTest(phase=phase),mock.patch('updater.runtime_digest',return_value='stable-'+phase), \
                 mock.patch('updater.edition',return_value={'version':'1.0'}), \
                 mock.patch('patch.app_layout',side_effect=ValueError('fixture')):
                progress=u.for_settings(settings,force=True)
                progress.publish(phase,build=self.root/'prepared')
                monitor.atomic_json(self.root/'progress.request.json',{'id':progress.read()['id'],'action':'start'})
                identity=progress.read()['id']
                again=u.for_settings(settings,force=True)
                self.assertEqual(again.read()['id'],identity);self.assertTrue(again.requested())
                self.assertEqual(again.read()['phase'],phase)
                self.assertEqual(monitor.load_json(self.root/'progress.context.json')['build'],str(self.root/'prepared'))

    def test_new_monitor_identity_probe_does_not_raise_or_rebind_progress(self):
        with mock.patch.object(u,'for_settings') as make:
            self.assertIsNone(u.worker_progress('identity',{'quiet_progress':True},self.root/'identity/worker-identity.json'))
        make.assert_not_called()

    def test_mac_normal_close_exact_paths_no_kill(self):
        with mock.patch.object(u.sys,'platform','darwin'),mock.patch.object(u.subprocess,'run') as run:
            u.graceful_close(['/Applications/Codex.app'])
        command=run.call_args.args[0]
        self.assertIn('runningApplicationsWithBundleIdentifier',command[-2])
        self.assertIn('app.terminate',command[-2]);self.assertNotIn('forceTerminate',command[-2])
        self.assertEqual(json.loads(command[-1]),['/Applications/Codex.app'])

    def test_windows_launch_only_official_registered_aumid(self):
        manifest={'status':'installed','target':str(self.root/'app')}
        entry={'target':'C:/Windows/explorer.exe','app_user_model_id':'OpenAI.Codex_abc123!App'}
        with mock.patch.object(u.sys,'platform','win32'),mock.patch('official_client_update.verify_official'), \
             mock.patch('official_client_shortcut.official_entry',return_value=entry), \
             mock.patch.object(patch,'app_layout',return_value={'exe':self.root/'app'/'Codex.exe'}), \
             mock.patch.object(patch,'active_app_processes',return_value=[{'exe':str(self.root/'app'/'Codex.exe')}]), \
             mock.patch('official_client_host.healthy',return_value=True):
            u.launch_official(self.root,manifest)
        self.assertEqual(self.spawn_mock.call_args.args[0],['C:/Windows/explorer.exe','shell:AppsFolder\\OpenAI.Codex_abc123!App'])
        self.spawn_mock.reset_mock()
        with mock.patch.object(u.sys,'platform','win32'),mock.patch('official_client_update.verify_official'), \
             mock.patch('official_client_shortcut.official_entry',return_value={'target':'legacy.exe'}):
            with self.assertRaises(ValueError):u.launch_official(self.root,manifest)
        self.spawn_mock.assert_not_called()

    def test_launch_uses_composer_entry_and_keeps_existing_health_checks(self):
        manifest={'status':'installed','target':str(self.root/'app'),'composer_compat':{'format':1}}
        executable=self.root/'app'/'Codex.exe'
        for platform in ('darwin','win32'):
            with self.subTest(platform=platform), mock.patch.object(u.sys,'platform',platform), \
                 mock.patch('official_client_update.verify_official'), \
                 mock.patch('official_client_host.start_composer') as start, \
                 mock.patch.object(patch,'app_layout',return_value={'exe':executable}), \
                 mock.patch.object(patch,'active_app_processes',return_value=[{'exe':str(executable)}]), \
                 mock.patch('official_client_host.healthy',return_value=True):
                u.launch_official(self.root,manifest)
            start.assert_called_once_with(self.root,manifest)
        self.spawn_mock.assert_not_called()

    def test_ready_start_uses_local_install_path_and_no_second_check(self):
        self.settings();self.build();self.progress.publish('waiting_for_exit')
        with mock.patch.object(u,'validated_close',return_value=True):
            u.action(self.root,self.identity,'start')
        self.assertIn('--install-ready',self.spawn_mock.call_args.args[0])

    def test_exit_failure_keeps_prepared_update_actionable(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(u,'previous_host_settings',return_value={'fixture':True}).start()
        mock.patch('official_client_host.activity_status',return_value={'known':True,'acquired':True}).start()
        self.settings();self.build();self.progress.publish('waiting_for_exit')
        monitor.atomic_json(self.root/'progress.request.json',{'id':self.identity,'action':'start'})
        with mock.patch('official_client_install.validate_build'),mock.patch('official_client_install._guard_staged_config'), \
             mock.patch.object(u,'graceful_close',side_effect=OSError('fixture denied')):
            self.assertFalse(u.validated_close(self.progress))
        self.assertEqual(self.progress.read()['phase'],'close_failed');self.assertTrue(self.progress.read()['can_start'])

    def test_quit_timeout_offers_manual_exit_or_retry(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(u,'previous_host_settings',return_value={'fixture':True}).start()
        mock.patch('official_client_host.activity_status',return_value={'known':True,'acquired':True}).start()
        self.build()
        monitor.atomic_json(self.root/'progress.request.json',{'id':self.identity,'action':'start'})
        with mock.patch('official_client_install.validate_build'),mock.patch('official_client_install._guard_staged_config'), \
             mock.patch.object(u,'graceful_close'),mock.patch.object(patch,'require_stopped',side_effect=ValueError('running')), \
             mock.patch.object(u.time,'sleep'):
            self.assertFalse(u.validated_close(self.progress))
        value=self.progress.read()
        self.assertEqual(value['phase'],'close_failed');self.assertTrue(value['can_start'])
        self.assertIn('手动退出',value['detail']);self.assertTrue(self.progress.requested())

    def test_corrupted_view_state_is_optional_and_actions_fail_closed(self):
        self.build()
        (self.root/'progress.json').write_text('{broken')
        self.assertIsNone(self.progress.publish('completed',present=True))
        self.assertIsNone(self.progress.present())
        self.assertFalse(u.validated_close(self.progress))
        self.assertEqual(self.progress.metadata(),{})
        self.assertEqual(u.main(['--state-dir',str(self.root),'--action','start','--id',self.identity]),1)
        self.spawn_mock.assert_not_called()

    def test_closing_available_racing_completion_reopens_final_once(self):
        self.settings()
        with mock.patch.object(u.sys,'platform','darwin'),mock.patch.object(u,'_alive',return_value=True):
            self.progress.present()
            self.progress.publish('completed',present=True)
            self.assertEqual(self.spawn_mock.call_count,1)
            u.action(self.root,self.identity,'dismiss','available')
            self.assertEqual(self.spawn_mock.call_count,2)
            u.action(self.root,self.identity,'dismiss','completed')
            self.progress.present()
            self.assertEqual(self.spawn_mock.call_count,2)

    def test_corrupted_optional_json_does_not_fail_new_worker_build(self):
        output=self.root/'next'
        (self.root/'progress.context.json').write_text('{broken')
        job={'app':str(self.root/'source'),'baseline':str(self.root/'baseline'),'output':str(output),
             'settings':{'codex_home':str(self.root/'home'),'edition':'proxy'}}
        with mock.patch.object(patch,'ARCHITECTURE','official-client-v1'),mock.patch('official_client.stage'), \
             mock.patch.object(update_worker,'verify_build'):
            result=update_worker.execute('build',job,self.progress)
        self.assertEqual(result['status'],'prepared')

    def test_real_new_candidate_identity_bridges_old_monitor_job(self):
        settings=self.settings();settings.update(edition=patch.EDITION,progress_ui=False)
        monitor.atomic_json(self.root/'settings.json',settings)
        # Empty identity job is exactly what old updater.worker sends.
        directory=self.root/'identity';directory.mkdir()
        job=directory/'worker-identity.json';job.write_text('{}')
        (self.root/'progress.json').unlink();(self.root/'progress.context.json').unlink()
        result=subprocess.run([__import__('sys').executable,str(patch.ROOT/'update_worker.py'),'identity',str(job)],
            capture_output=True,text=True,timeout=15,**background_options())
        self.assertEqual(result.returncode,0,result.stderr)
        value=monitor.load_json(job.with_suffix('.result.json'))
        self.assertEqual(value['architecture'],'official-client-v1')
        progress=self.progress.read()
        self.assertEqual(progress['phase'],'checking');self.assertEqual(progress['edition'],patch.EDITION)
        self.assertFalse((self.root/'progress.ui.json').exists())

    def test_download_progress_has_exact_completed_file_counts(self):
        from tests.test_updater import UpdateTests
        release,contents=UpdateTests().release()
        progress=mock.Mock()
        with mock.patch.object(updater.subprocess,'run',return_value=mock.Mock(returncode=0,stderr='')):
            updater.source_package(release,self.root,lambda url:contents[url.split('/'+release['commit']+'/',1)[1]],progress)
        counts=[v.args[1:] for v in progress.publish.call_args_list if v.args[0]=='downloading']
        self.assertTrue(counts)
        self.assertTrue(all(total is None for _,total in counts))
        self.assertTrue(all(call.kwargs.get('unit')=='bytes' for call in progress.publish.call_args_list if call.args[0]=='downloading'))
        self.assertEqual(progress.publish.call_args_list[-1].args,('checking_package',len(contents)+1,len(contents)+1))

    def test_corrupted_progress_does_not_block_committed_runtime_activation(self):
        directory,manifest=self.build(status='installed')
        (self.root/'progress.json').write_text('{broken')
        obj=object.__new__(updater.UpdateBackend)
        obj.directory=self.root;obj.package=self.root;obj.edition='proxy';obj.settings={};obj.progress=None
        obj.base=mock.Mock();obj.base.snapshot.return_value={'fingerprint':'after'}
        obj.snapshot=lambda:{'fingerprint':'candidate'}
        obj.desired_runtime='runtime';obj.set_baseline=mock.Mock();obj.promote=mock.Mock()
        with mock.patch.object(updater,'worker'):
            result=obj.install({'directory':str(directory)},{'fingerprint':'candidate','model_digest':'models'})
        obj.promote.assert_called_once_with(directory)
        self.assertFalse((self.root/'activation-pending.json').exists())
        self.assertEqual(result['baseline'],str(directory))

    def test_old_monitor_worker_marks_complete_only_after_runtime_activation(self):
        self.settings();directory,manifest=self.build(status='installed')
        manifest['update'].update(package=str(self.root/'package'),monitor_state_dir=str(self.root),runtime_digest='a'*64)
        progress=mock.Mock();timeline=[]
        progress.publish.side_effect=lambda phase,**kwargs:timeline.append(phase)
        with mock.patch.object(updater,'activate_runtime',side_effect=lambda *args:timeline.append('runtime_activated')) as activate, \
             mock.patch.object(updater,'reconcile_native_activation',side_effect=lambda *args,**kwargs:progress.publish('completed')):
            update_worker.finish_native_update(directory,manifest,progress)
        activate.assert_called_once_with(self.root/'package',self.root,'proxy')
        self.assertEqual(timeline,['activating','runtime_activated','completed'])
        settings=monitor.load_json(self.root/'settings.json')
        self.assertEqual(settings['baseline'],str(directory));self.assertEqual(settings['architecture'],'official-client-v1')

    def test_old_monitor_activation_failure_retains_recovery_not_complete(self):
        self.settings();directory,manifest=self.build(status='installed')
        manifest['update'].update(package=str(self.root/'package'),monitor_state_dir=str(self.root),runtime_digest='a'*64)
        progress=mock.Mock()
        with mock.patch.object(updater,'activate_runtime',side_effect=OSError('locked')):
            update_worker.finish_native_update(directory,manifest,progress)
        self.assertEqual([v.args[0] for v in progress.publish.call_args_list],['activating'])
        self.assertEqual(monitor.load_json(self.root/'activation-pending.json')['baseline'],str(directory))
        self.assertEqual(monitor.load_json(self.root/'settings.json')['architecture'],'official-client-v1')
        self.assertTrue((self.root/updater.NATIVE_RECOVERY).exists())

    def test_direct_install_without_update_package_finishes_without_activation(self):
        directory,manifest=self.build(status='installed');progress=mock.Mock()
        with mock.patch.object(updater,'activate_runtime') as activate:
            update_worker.finish_native_update(directory,manifest,progress)
        activate.assert_not_called();progress.publish.assert_called_once_with('completed',build=directory,present=True)

    def test_new_worker_build_progress_and_metadata_before_return(self):
        output=self.root/'native-build'
        job={'app':str(self.root/'source'),'baseline':str(self.root/'baseline'),'output':str(output),
             'settings':{'codex_home':str(self.root/'home'),'edition':'proxy'}}
        progress=mock.Mock();progress.metadata.return_value={'progress_id':'test','progress_state_dir':str(self.root)}
        with mock.patch.object(patch,'ARCHITECTURE','official-client-v1'),mock.patch('official_client.stage') as stage, \
             mock.patch.object(update_worker,'verify_build') as verify,mock.patch.object(u,'validated_close') as close:
            result=update_worker.execute('build',job,progress)
        verify.assert_called_once_with(output,progress);close.assert_called_once_with(progress)
        self.assertEqual(stage.call_args.kwargs['update_metadata']['progress_id'],'test')
        self.assertEqual([v.args[0] for v in progress.publish.call_args_list],['building','verifying','waiting_for_exit'])
        self.assertEqual(result['status'],'prepared')


class NativeActivationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.runtime=self.root/'runtime';self.runtime.mkdir()
        self.files=('edition.json','code.py')
        (self.runtime/'edition.json').write_text(json.dumps({'edition':'proxy','branch':updater.CHANNELS['proxy'],'version':'1.0'}))
        (self.runtime/'code.py').write_text('candidate\n')
        self.digest=updater.runtime_digest(self.runtime,self.files)
        self.legacy=self.root/'legacy';self.legacy.mkdir();monitor.atomic_json(self.legacy/'manifest.json',{'architecture':'legacy'})
        self.build=self.root/'native';self.build.mkdir()
        self.manifest={'architecture':'official-client-v1','status':'installed','edition':'proxy',
            'target':str(self.root/'official'),'installed_at':'2026-10-19T10:00:00+00:00',
            'legacy_baseline':str(self.legacy),'update':{'runtime_digest':self.digest,'package':str(self.root/'package'),
            'monitor_state_dir':str(self.root)}}
        monitor.atomic_json(self.build/'manifest.json',self.manifest)
        monitor.atomic_json(self.root/'settings.json',{'edition':'proxy','baseline':str(self.legacy),'app':'legacy-app'})
        monitor.atomic_json(self.root/'state.json',{'format':1,'phase':'failed','installed':{'baseline':str(self.legacy),
            'fingerprint':'old'},'terminal':{},'notices':{},'adoption_checked':True})
        self.patch_files=mock.patch.object(patch,'SERVICE_RUNTIME_FILES',self.files);self.patch_files.start();self.addCleanup(self.patch_files.stop)
        self.patch_edition=mock.patch.object(patch,'EDITION','proxy');self.patch_edition.start();self.addCleanup(self.patch_edition.stop)
        monitor.atomic_json(self.root/updater.NATIVE_RECOVERY,{'format':1,'baseline':str(self.build),
            'edition':'proxy','runtime_digest':self.digest})

    def test_independent_marker_recovers_after_old_parent_deleted_its_journal(self):
        self.assertFalse((self.root/'activation-pending.json').exists())
        result=updater.reconcile_native_activation(self.root)
        self.assertEqual(result['status'],'reconciled')
        state=monitor.load_json(self.root/'state.json');settings=monitor.load_json(self.root/'settings.json')
        self.assertEqual(state['installed']['baseline'],str(self.build));self.assertFalse(state['adoption_checked'])
        self.assertEqual(settings['baseline'],str(self.build));self.assertEqual(settings['architecture'],'official-client-v1')
        self.assertFalse((self.root/updater.NATIVE_RECOVERY).exists())
        from official_client_update import select_backend
        with mock.patch('official_client_update.NativeMonitorBackend') as native,mock.patch('backend.MonitorBackend') as legacy:
            select_backend(settings)
        native.assert_called_once();legacy.assert_not_called()

    def test_runtime_not_activated_retains_marker_and_never_completes(self):
        (self.runtime/'code.py').write_text('old')
        with self.assertRaisesRegex(patch.PatchError,'still pending'):
            updater.reconcile_native_activation(self.root)
        self.assertTrue((self.root/updater.NATIVE_RECOVERY).exists())
        self.assertEqual(monitor.load_json(self.root/'state.json')['installed']['baseline'],str(self.legacy))

    def test_newer_committed_native_baseline_wins_over_stale_marker_and_state(self):
        newer=self.root/'newer';newer.mkdir()
        new={**self.manifest,'legacy_baseline':str(self.build),'installed_at':'2026-10-19T11:00:00+00:00'}
        monitor.atomic_json(newer/'manifest.json',new)
        settings=monitor.load_json(self.root/'settings.json');settings['baseline']=str(newer)
        monitor.atomic_json(self.root/'settings.json',settings)
        monitor.atomic_json(self.root/'activation-pending.json',{'baseline':str(self.build)})
        updater.reconcile_native_activation(self.root)
        self.assertEqual(monitor.load_json(self.root/'settings.json')['baseline'],str(newer))
        self.assertEqual(monitor.load_json(self.root/'state.json')['installed']['baseline'],str(newer))
        self.assertFalse((self.root/'activation-pending.json').exists())

    def test_foreign_edition_marker_is_rejected(self):
        value=monitor.load_json(self.root/updater.NATIVE_RECOVERY);value['edition']='ds'
        monitor.atomic_json(self.root/updater.NATIVE_RECOVERY,value)
        with self.assertRaisesRegex(patch.PatchError,'another edition'):
            updater.reconcile_native_activation(self.root)
        self.assertTrue((self.root/updater.NATIVE_RECOVERY).exists())

    def test_monitor_reconciles_before_old_installed_baseline_override(self):
        with mock.patch.object(monitor,'CoreBackend') as backend,mock.patch.object(monitor,'Monitor') as runner,mock.patch('builtins.print'):
            runner.return_value.run_once.return_value={'phase':'installed'}
            self.assertEqual(monitor.main(['--state-dir',str(self.root),'run-once']),0)
        self.assertEqual(backend.call_args.args[0]['baseline'],str(self.build))
        self.assertEqual(backend.call_args.args[0]['architecture'],'official-client-v1')

    def test_child_activation_lock_is_not_core_install_failure_and_remains_recoverable(self):
        progress=mock.Mock()
        with mock.patch.object(updater,'activate_runtime',side_effect=OSError('parent cwd locked')):
            update_worker.finish_native_update(self.build,self.manifest,progress)
        self.assertEqual([v.args[0] for v in progress.publish.call_args_list],['activating'])
        self.assertTrue((self.root/updater.NATIVE_RECOVERY).exists())
        # Old parent may successfully promote and delete its own journal, then
        # crash before persist_baseline. The independent commit must still win.
        (self.root/'activation-pending.json').unlink()
        updater.reconcile_native_activation(self.root)
        self.assertEqual(monitor.load_json(self.root/'state.json')['installed']['baseline'],str(self.build))

    def test_repeated_actual_activation_is_a_noop_without_backup_churn(self):
        identity={'edition':'proxy','files':self.files,'runtime_digest':self.digest}
        with mock.patch.object(updater,'worker',return_value=identity),mock.patch.object(updater.os,'replace') as replace:
            updater.activate_runtime(self.root/'package',self.root,'proxy')
            updater.activate_runtime(self.root/'package',self.root,'proxy')
        replace.assert_not_called()


if __name__=='__main__':unittest.main()

class TransactionProgressTests(unittest.TestCase):
    setUp=ProgressTest.setUp
    settings=ProgressTest.settings
    build=ProgressTest.build
    def test_order_fence_and_actual_counts_survive_old_writer(self):
        self.progress.publish('downloading',80,100,unit='bytes')
        self.progress.publish('downloading',30,100,unit='bytes')
        self.assertEqual(self.progress.read()['percent'],80)
        self.progress.publish('verifying',4,16,unit='checks')
        outer=monitor.load_json(self.root/'progress.json');outer.update(phase='available',title='stale')
        monitor.atomic_json(self.root/'progress.json',outer)
        self.assertEqual(self.progress.read()['phase'],'verifying')
        self.progress.publish('building');self.assertEqual(self.progress.read()['percent'],25)
        old=u.Progress(self.root);self.progress.begin('ds','2.0','different')
        old.publish('failed');self.assertEqual(self.progress.read()['phase'],'available')

    def test_one_click_wait_cancel_and_restart_keep_prepared_package(self):
        self.settings();directory,_=self.build()
        with mock.patch('official_client_install.validate_build'),mock.patch('official_client_install._guard_staged_config'), \
             mock.patch.object(u,'previous_host_settings',return_value={'fixture':True}), \
             mock.patch('official_client_host.activity_status',return_value={'known':True,'requests':1,'tools':1,'acquired':False}), \
             mock.patch.object(u,'graceful_close') as close:
            u.action(self.root,self.identity,'start')
            self.assertEqual(self.progress.read()['phase'],'waiting_for_idle')
            self.assertTrue(self.progress.read()['can_cancel']);self.assertTrue(self.progress.requested())
            self.assertEqual(monitor.load_json(self.root/'progress.request.json')['action'],'wait_idle')
            close.assert_not_called()
            u.action(self.root,self.identity,'cancel')
            self.assertTrue(self.progress.paused());self.assertTrue(directory.exists())
            with self.assertRaises(patch.AppRunningError):update_worker.execute('install',{'directory':str(directory)},self.progress)
            u.action(self.root,self.identity,'start');self.assertFalse(self.progress.paused())
        with mock.patch('official_client_install.validate_build'),mock.patch('official_client_install._guard_staged_config'), \
             mock.patch.object(u,'previous_host_settings',return_value={'fixture':True}), \
             mock.patch('official_client_host.activity_status',return_value={'known':True,'acquired':True}), \
             mock.patch.object(u,'graceful_close') as close,mock.patch.object(patch,'require_stopped'):
            self.assertTrue(u.validated_close(self.progress));close.assert_called_once()
        self.assertFalse(self.progress.read()['can_cancel'])
        with self.assertRaises(ValueError):u.action(self.root,self.identity,'cancel')

    def test_unknown_activity_waits_for_exit_and_timeout_retains_build(self):
        self.settings();directory,_=self.build()
        with mock.patch('official_client_install.validate_build'),mock.patch('official_client_install._guard_staged_config'), \
             mock.patch.object(u,'graceful_close') as close:
            u.action(self.root,self.identity,'start');close.assert_not_called()
        request=monitor.load_json(self.root/'progress.request.json')
        self.assertTrue(u.wait_expired(self.progress,request['at']+3601))
        self.assertTrue(self.progress.paused());self.assertTrue(directory.exists())
        self.assertTrue(self.progress.read()['can_start'])

    def test_rollback_phase_is_terminal_until_explicit_retry(self):
        self.progress.publish('installing');self.progress.publish('recovering');self.progress.publish('rolled_back')
        for phase in ('completed','available','building','failed'):self.progress.publish(phase)
        self.assertEqual(self.progress.read()['phase'],'rolled_back')
        self.progress.retry();self.assertEqual(self.progress.read()['phase'],'available')

    def test_old_close_guard_is_fenced_and_new_controller_defers_unknown_activity(self):
        self.settings();directory,manifest=self.build()
        manifest['status']='prepared_idle';monitor.atomic_json(directory/'manifest.json',manifest)
        # Published old controllers check this state before any exit request.
        self.assertNotEqual(manifest['status'],'prepared')
        monitor.atomic_json(self.root/'progress.request.json',{'id':self.identity,'action':'start'})
        with mock.patch('official_client_install.validate_build'),mock.patch('official_client_install._guard_staged_config'), \
             mock.patch.object(u,'graceful_close') as close:
            self.assertFalse(u.validated_close(self.progress));close.assert_not_called()
        self.assertEqual(self.progress.read()['phase'],'waiting_for_idle')
        self.assertEqual(monitor.load_json(self.root/'progress.request.json')['action'],'wait_idle')
