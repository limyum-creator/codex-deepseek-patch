import json
from pathlib import Path
import tempfile
import sys
import os
import unittest
from unittest.mock import patch

import management as ui


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.build, self.home = self.root/'build', self.root/'home'
        self.build.mkdir(); self.home.mkdir()
        self.edition = patch.object(ui.p, 'EDITION', 'proxy')
        self.edition.start(); self.addCleanup(self.edition.stop)
        self.queue = patch.object(ui, '_queue')
        self.queued = self.queue.start(); self.addCleanup(self.queue.stop)
        self.settings = {'edition':'proxy','baseline':str(self.build),'codex_home':str(self.home),
            'app':str(self.root/'official'),'interval':300,'auto_update':True,'model_sync':True}
        self.manifest = {'edition':'proxy','architecture':'official-client-v1','tool_version':'fixture',
            'app_version':'native-fixture','deepseek_enabled':False,
            'locals':[{'id':'local-workpc','name':'Work PC','kind':'openai','base_url':'http://127.0.0.1:1234/v1',
                'key_file':str(self.root/'private-fixture-key'),'models':['local-test']}],
            'model_routes':{'local-test':{'provider':'local-workpc','context_window':32768,'max_context_window':262144}}}
        self.save(self.root/'settings.json', self.settings)
        self.save(self.build/'manifest.json', self.manifest)
        self.save(self.build/'desktop-models.json', {'models':[
            {'slug':'gpt-test','context_window':1000000,'max_context_window':1000000},
            {'slug':'local-test','context_window':32768,'max_context_window':262144}]})
        (self.home/'config.toml').write_text('model="gpt-test"\n[mcp_servers.personal]\ncommand="untouched"\n')

    def save(self, path, value):
        path.write_text(json.dumps(value), encoding='utf-8')

    def perform(self, action, payload):
        return ui.perform(self.root, action, payload, ui.revision(self.root))

    def test_model_reduction_visibility_and_explicit_reset_preserve_other_choices(self):
        self.save(self.root/'model-preferences.json', {'context_windows':{'gpt-test':500000},'hidden':['gpt-test']})
        self.perform('save_model', {'id':'local-test','context':16384,'compact':12000,'visible':False})
        value = json.loads((self.root/'model-preferences.json').read_text())
        self.assertEqual(value['context_windows'], {'gpt-test':500000,'local-test':16384})
        self.assertEqual(set(value['hidden']), {'gpt-test','local-test'})
        self.perform('save_model', {'id':'local-test','context':None,'compact':None,'visible':True})
        model = next(row for row in ui.snapshot(self.root)['models'] if row['id']=='local-test')
        self.assertEqual(model['effective'], 262144)
        self.assertEqual(model['installed_window'], 32768)
        self.assertTrue(model['visible'])
        self.assertEqual((self.home/'config.toml').read_text(), 'model="gpt-test"\n[mcp_servers.personal]\ncommand="untouched"\n')

    def test_invalid_window_and_stale_revision_never_overwrite_settings(self):
        before = ui.revision(self.root)
        for payload in ({'context':300000}, {'context':0}, {'context':True}, {'context':8192,'compact':8200}):
            with self.subTest(payload=payload), self.assertRaises(ui.SettingsError):
                ui.perform(self.root,'save_model',{'id':'local-test',**payload},before)
            self.assertEqual(ui.revision(self.root),before)
        self.perform('save_model', {'id':'local-test','context':8192})
        with self.assertRaisesRegex(ui.SettingsError,'刷新'):
            ui.perform(self.root,'save_model',{'id':'local-test','context':32768},before)

    def test_connection_save_keeps_other_connections_and_secret_out_of_document(self):
        connection={'id':'local-new','name':'New local','kind':'openai','base_url':'http://127.0.0.1:4567/v1',
            'wire_api':'chat_completions','models':['model-x'], 'model_specs':{'model-x':{'context_window':131072}}}
        self.perform('save_connection',{'connection':connection,'key':'synthetic-secret-not-exported'})
        value=json.loads((self.root/'connections.json').read_text())
        self.assertEqual([row['id'] for row in value['locals']],['local-workpc','local-new'])
        self.assertNotIn('synthetic-secret',json.dumps(value))
        stored=value['locals'][1]['key_file']
        self.assertEqual(Path(stored).read_text(),'synthetic-secret-not-exported')
        self.perform('remove_connection',{'id':'local-new'})
        self.assertEqual(len(json.loads((self.root/'connections.json').read_text())['locals']),1)
        self.assertTrue(Path(stored).exists())  # Disconnect is reversible; no credential purge.

    def test_key_write_is_rolled_back_when_connection_save_fails(self):
        original=ui.monitor.atomic_json
        def fail(path,value):
            if Path(path).name=='connections.json':raise OSError('synthetic write failure')
            return original(path,value)
        with patch.object(ui.monitor,'atomic_json',side_effect=fail), self.assertRaises(OSError):
            self.perform('save_connection',{'connection':{'id':'local-new','kind':'openai','base_url':'http://127.0.0.1:4567/v1'},'key':'secret'})
        self.assertFalse((self.root/'credentials/local-new.key').exists())
        self.assertFalse((self.root/'connections.json').exists())

    def test_disable_reenable_services_retains_connection_choices_and_model_reductions(self):
        proxy={'base_url':'http://127.0.0.1:8317/v1','config':str(self.root/'proxy.yaml'),'models':['grok-4.7']}
        preferences={'context_windows':{'grok-4.7':32768},'compact_limits':{'grok-4.7':28000},'hidden':['grok-4.5']}
        self.save(self.root/'model-preferences.json',preferences)
        self.perform('save_proxy',{'proxy':proxy,'auto_add':False})
        self.perform('save_proxy',{'proxy':None,'auto_add':False})
        snap=ui.snapshot(self.root)
        self.assertIsNone(snap['proxy']);self.assertEqual(snap['disabled_proxy'],proxy)
        self.perform('save_proxy',{'proxy':snap['disabled_proxy'],'auto_add':False})
        for enabled in (True,False,True):self.perform('save_deepseek',{'enabled':enabled})
        doc=json.loads((self.root/'connections.json').read_text())
        self.assertEqual(doc['proxy'],proxy);self.assertNotIn('disabled_proxy',doc)
        self.assertTrue(doc['deepseek_enabled']);self.assertFalse(doc['proxy_auto_add'])
        self.assertEqual(doc['locals'],self.manifest['locals'])
        self.assertEqual(json.loads((self.root/'model-preferences.json').read_text()),preferences)

    def test_wrong_edition_cannot_queue_or_change_other_channel(self):
        self.save(self.root/'settings.json',{**self.settings,'edition':'ds'})
        with patch.object(ui.monitor,'request_update') as enqueue, self.assertRaises(ui.SettingsError):
            self.perform('check_updates',{})
        enqueue.assert_not_called()

    def test_malformed_private_job_is_removed_and_returns_sanitized_result(self):
        jobs=self.root/'settings-ui/jobs';jobs.mkdir(parents=True)
        identifier='a'*32
        source=jobs/(identifier+'.request.json')
        source.write_text('{"key":"synthetic-secret",')
        result=ui.run_request(self.root,identifier)
        self.assertEqual(result['status'],'failed')
        self.assertFalse(source.exists())
        self.assertNotIn('synthetic-secret',json.dumps(result))
        self.assertEqual(json.loads((jobs/(identifier+'.result.json')).read_text(encoding='utf-8')),result)

    def test_diagnostics_do_not_export_connections_or_raw_errors(self):
        with patch('official_client_host.health_status',return_value={'ready':True}), \
             patch('official_client_update.managed_configuration'), \
             patch.object(ui.monitor.Scheduler,'status',return_value={'enabled':True}), \
             patch.object(ui,'probe_connection',side_effect=RuntimeError('secret /private/file https://private-host')):
            result=ui.diagnose(self.root,endpoints=True)
        raw=json.dumps(result)
        for private in ('secret','private-host','/private/file',str(self.root),'127.0.0.1'):
            self.assertNotIn(private,raw)
        self.assertTrue(any(row['status']=='error' for row in result['checks']))

    def test_schedule_failure_restores_previous_options(self):
        with patch.object(ui.monitor.Scheduler,'enable',side_effect=OSError('scheduler unavailable')), self.assertRaises(OSError):
            self.perform('save_options',{'interval':600,'auto_update':False})
        self.assertEqual(json.loads((self.root/'settings.json').read_text()),self.settings)

    def test_whale_cards_share_existing_model_rules_and_keep_editions_separate(self):
        self.assertEqual(len(ui.hub_shell(self.root)['categories']),6)
        for route in ('models','connections','updates','tools','diagnostics','preferences'):
            page=ui.hub_view(self.root,route)
            self.assertEqual(page['route'],route)
            self.assertEqual(page['revision'],ui.revision(self.root))
        result=ui.hub_dispatch(self.root,{'route':'models/edit','key':'local-test',
            'intent':'save_model','values':{'context':'8192','compact':'7000','visible':False}},
            ui.revision(self.root))
        self.assertEqual(result['route'],'models/edit')
        saved=json.loads((self.root/'model-preferences.json').read_text())
        self.assertEqual(saved['context_windows']['local-test'],8192)
        self.assertIn('local-test',saved['hidden'])
        with self.assertRaises(ui.SettingsError):
            ui.hub_dispatch(self.root,{'route':'models/edit','key':'local-test',
                'intent':'save_model','values':{'context':'NaN'}},ui.revision(self.root))
        self.save(self.root/'settings.json',{**self.settings,'edition':'ds'})
        self.save(self.build/'manifest.json',{**self.manifest,'edition':'ds'})
        with patch.object(ui.p,'EDITION','ds'):
            page=ui.hub_view(self.root,'connections')
            self.assertEqual([r['title'] for r in page['rows']],['DeepSeek','现成代理','Work PC'])
            self.assertEqual(ui.hub_view(self.root,'connections/edit')['route'],'connections/edit')

    def test_update_page_embeds_progress_and_actions_keep_current_update_identity(self):
        import update_progress
        progress=update_progress.Progress(self.root)
        identity=progress.begin('proxy','1.0.0-proxy.1','fixture')
        progress.publish('downloading',2,7)
        page=ui.hub_view(self.root,'updates')
        row=next(r for r in page['rows'] if r['type']=='update')
        self.assertEqual((row['progress']['completed'],row['progress']['total']),(2,7))
        self.assertNotIn('show_progress',[b['intent'] for b in page['buttons']])
        with patch.object(update_progress,'action') as action:
            ui.hub_dispatch(self.root,{'route':'updates','intent':'update_action','id':identity,
                'command':'start','phase':'downloading'},ui.revision(self.root))
            action.assert_called_once_with(self.root,identity,'start','downloading')
            progress.begin('proxy','1.0.1-proxy.1','new')
            with self.assertRaises(ui.SettingsError):
                self.perform('update_action',{'id':identity,'command':'start'})
        self.save(self.root/'progress.json',{'id':'a'*32,'edition':'ds','phase':'available'})
        self.assertEqual(ui.hub_progress(self.root),{})

    def test_manual_model_sync_does_not_request_patch_download(self):
        with patch.object(ui.monitor,'request_update') as request,patch.object(ui.monitor,'wake_monitor'):
            self.perform('sync_models',{})
        request.assert_called_once_with(self.root,sync_now=True,code_now=False,retry=False)

    @unittest.skipUnless(sys.platform in ('darwin','win32'),'Native integrated update page')
    def test_native_update_card_badge_and_completion_share_the_settings_host(self):
        import update_progress
        channel=json.loads((ui.ROOT/'edition.json').read_text())['edition']
        self.save(self.root/'settings.json',{**self.settings,'edition':channel})
        self.save(self.build/'manifest.json',{**self.manifest,'edition':channel})
        progress=update_progress.Progress(self.root)
        progress.begin(channel,'1.0.0','fixture')
        for phase,attention,primary in [('available',True,'开始更新'),('verifying',True,'开始更新'),
                ('waiting_for_idle',True,'取消等待'),('completed',False,'启动 Codex')]:
            if phase=='waiting_for_idle':
                self.save(self.root/'progress.request.json',{'id':progress.identity,'action':'wait_idle'})
            progress.publish(phase,4 if phase=='verifying' else None,16 if phase=='verifying' else None,unit='checks' if phase=='verifying' else None)
            with patch.object(ui.p,'EDITION',channel):result=ui.show(self.root,self_test=True,page='updates',notify=True)
            self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
            value=json.loads(result.stdout.decode('utf-8-sig'))
            self.assertEqual(value['route'],'updates')
            self.assertEqual(value['update_attention'],attention)
            self.assertTrue(value['badge_visible'])
            self.assertEqual(value['update_primary'],primary)
            if phase=='verifying':
                self.assertEqual(value['update_percent'],25)
                self.assertIn('25%',value['update_units'])
            self.assertFalse(value.get('draw_error'))

    def test_discovery_never_echoes_password_into_a_native_result(self):
        result={'available':['remote-x'],'specs':{'remote-x':{'context_window':131072}},'warnings':[]}
        with patch.object(ui,'probe_connection',return_value=result):
            page=ui.hub_dispatch(self.root,{'route':'connections/edit','intent':'probe_connection',
                'values':{'name':'Test','kind':'openai','wire':'chat_completions',
                          'url':'http://127.0.0.1:1234/v1','key':'synthetic-password'}},ui.revision(self.root))
        self.assertEqual(page['route'],'connections/discover')
        self.assertNotIn('synthetic-password',json.dumps(page))
        self.assertNotIn('key',page['draft'])

    def test_appearance_does_not_queue_a_model_rebuild_or_change_credentials(self):
        original=ui.revision(self.root)
        page=ui.hub_dispatch(self.root,{'route':'preferences','intent':'save_ui_options',
            'values':{'theme':'dark','motion':'reduced','hotkey':False}},original)
        self.assertEqual(page['options'],{'theme':'dark','motion':'reduced','hotkey':False,'sound':True})
        ui.hub_save_options(self.root,{'sound':False})
        self.assertFalse(ui.hub_options(self.root)['sound'])
        self.assertEqual(ui.revision(self.root),original)
        self.queued.assert_not_called()
        with self.assertRaises(ui.SettingsError):
            ui.hub_save_options(self.root,{'theme':'remote-script'})

    def test_upgrade_waits_for_old_editor_and_coalesces_one_new_host(self):
        import threading,time
        results=[]
        original=ui.revision(self.root)
        ui.monitor.atomic_json(self.root/'settings-ui/host.json',{'edition':'proxy','pid':os.getpid()})
        with patch.object(ui,'_show_locked',return_value='new-whale') as opened:
            with ui.monitor.state_lock(self.root/'settings-ui') as held:
                self.assertTrue(held)
                thread=threading.Thread(target=lambda:results.append(ui.show(self.root,notify=True)),daemon=True)
                thread.start()
                waiting=False
                for _ in range(100):
                    with ui.monitor.state_lock(self.root/'settings-ui/launch-control') as free:
                        if not free:waiting=True;break
                    time.sleep(.01)
                self.assertTrue(waiting,'Handoff launcher did not acquire its singleton lock')
                # An explicit reopen takes precedence over another background notice.
                ui.show(self.root,page='updates')
                ui.show(self.root,notify=True)
                opened.assert_not_called();self.assertTrue(thread.is_alive())
                self.assertFalse((self.root/'settings-ui/raise.json').exists())
            thread.join(3)
            self.assertFalse(thread.is_alive());self.assertEqual(results,['new-whale'])
            opened.assert_called_once()
            self.assertEqual(opened.call_args.kwargs['page'],'updates')
            self.assertFalse(opened.call_args.kwargs['notify'])
        self.assertEqual(ui.revision(self.root),original)

    def test_bubble_audio_is_soft_bounded_and_reused(self):
        import wave, struct
        sounds=ui.bubble_assets(self.root)
        before={name:path.stat().st_mtime_ns for name,path in sounds.items()}
        for path in sounds.values():
            with wave.open(str(path),'rb') as audio:
                self.assertEqual((audio.getnchannels(),audio.getsampwidth(),audio.getframerate()),(1,2,24000))
                duration=audio.getnframes()/audio.getframerate()
                self.assertGreater(duration,.1);self.assertLess(duration,.2)
                data=audio.readframes(audio.getnframes())
            values=struct.unpack('<'+'h'*(len(data)//2),data)
            self.assertEqual((values[0],values[-1]),(0,0))
            self.assertGreater(max(map(abs,values)),500)
            self.assertLess(max(map(abs,values)),8192)
        ui.bubble_assets(self.root)
        self.assertEqual(before,{name:path.stat().st_mtime_ns for name,path in sounds.items()})
        with self.assertRaises(ui.SettingsError):ui.hub_save_options(self.root,{'sound':'false'})

    def test_logo_has_one_whale_contour_and_one_terminal_mark(self):
        import xml.etree.ElementTree as ET
        paths=list(ET.fromstring((ui.ROOT/'brand.svg').read_bytes()).iter('{http://www.w3.org/2000/svg}path'))
        self.assertEqual(len(paths),2)
        self.assertEqual(len(ui.vector_contours(paths[0].attrib['d'])),1)
        self.assertEqual(len(ui.vector_contours(paths[1].attrib['d'])),2)

    @unittest.skipUnless(sys.platform in ('darwin','win32'),'Native settings widgets')
    def test_native_widgets_complete_private_snapshot_and_close_headlessly(self):
        channel=json.loads((ui.ROOT/'edition.json').read_text())['edition']
        self.save(self.root/'settings.json',{**self.settings,'edition':channel})
        self.save(self.build/'manifest.json',{**self.manifest,'edition':channel})
        with patch.object(ui.p,'EDITION',channel):result=ui.show(self.root,self_test=True)
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
        try:value=json.loads(result.stdout.decode('utf-8-sig'))
        except ValueError:self.fail('Native settings output: '+repr(result.stdout)+' stderr: '+repr(result.stderr))
        self.assertEqual(value['categories'],6)
        self.assertFalse(value.get('draw_error'))
        if sys.platform=='darwin':self.assertGreaterEqual(value['animation_top_clearance'],4)
        self.assertEqual(value['models'],2)
        self.assertTrue(value['request_completed'])
        self.assertEqual(value['status'],'操作完成。')
        self.assertTrue(value['closed'])
        motion=value['motion']
        self.assertGreaterEqual(motion['opened'],2)
        self.assertGreaterEqual(motion['closed'],2)
        for name in ('hover_revealed','leave_retracted','detached','form_held','reduced','idle_timer_stopped','release_stays_collapsed','reentry_reveals','arrival_hint','hint_cleared','spray_released','explicit_wake_holds','explicit_wake_retracts','edge_click_deferred','edge_click_coalesced','edge_click_revealed','hidden_click_cancelled'):
            self.assertTrue(motion[name],(name,motion))
        if sys.platform=='win32':self.assertTrue(motion['no_dotted_focus'])
        self.assertEqual(motion['edges'],[True]*4)
        self.assertEqual(motion['directions'],[90,-90,180,0])
        self.assertEqual(motion['audio_loaded'],2)
        self.assertFalse(value['update_attention'])

    @unittest.skipUnless(sys.platform in ('darwin','win32'),'Native settings event loop')
    def test_native_window_loop_refreshes_and_closes_without_hiding_windows_gui(self):
        channel=json.loads((ui.ROOT/'edition.json').read_text())['edition']
        self.save(self.root/'settings.json',{**self.settings,'edition':channel})
        self.save(self.build/'manifest.json',{**self.manifest,'edition':channel})
        with patch.object(ui.p,'EDITION',channel):result=ui.show(self.root,self_test=True,loop_test=True)
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
        value=json.loads(result.stdout.decode('utf-8-sig'))
        self.assertEqual(value['status'],'操作完成。')
        self.assertEqual(value['models'],2)
        self.assertTrue(value['event_loop']);self.assertTrue(value['closed'])
        if sys.platform=='win32':self.assertTrue(value['visible'])

    @unittest.skipUnless(sys.platform in ('darwin','win32'),'Per-user native entry')
    def test_settings_entry_is_separate_idempotent_and_preserves_user_edits(self):
        runtime=self.root/'runtime';runtime.mkdir();(runtime/'management.py').write_text('#fixture')
        self.save(self.root/'settings.json',{**self.settings,'python_path':sys.executable})
        with patch.object(Path,'home',return_value=self.root/'user'),patch.dict(os.environ,{'APPDATA':str(self.root/'appdata')}):
            destination=Path(ui.ensure_launcher(self.root))
            self.assertEqual(ui.ensure_launcher(self.root),str(destination))
            target=destination/'Contents/MacOS/settings' if destination.is_dir() else destination
            target.write_bytes(b'user changed entry')
            with self.assertRaises(ui.SettingsError):ui.ensure_launcher(self.root)
            self.assertFalse(ui.remove_launcher(self.root))
            self.assertEqual(target.read_bytes(),b'user changed entry')

    @unittest.skipUnless(sys.platform=='darwin','AppKit lazy list')
    def test_macos_long_model_list_keeps_search_and_scrolling(self):
        channel=json.loads((ui.ROOT/'edition.json').read_text())['edition']
        self.save(self.root/'settings.json',{**self.settings,'edition':channel})
        self.save(self.build/'manifest.json',{**self.manifest,'edition':channel})
        self.save(self.build/'desktop-models.json',{'models':[
            {'slug':f'gpt-{i:02d}','context_window':1000000} for i in range(40)]})
        with patch.object(ui.p,'EDITION',channel):result=ui.show(self.root,self_test=True,loop_test=True)
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
        value=json.loads(result.stdout.decode('utf-8-sig'))
        self.assertFalse(value.get('draw_error'))
        listing=value['motion']['list']
        self.assertEqual(listing['total'],40)
        self.assertLessEqual(listing['initial_rows'],8)
        for behavior in ('tail_visible','searchable','restored'):
            self.assertTrue(listing[behavior],behavior)

    def test_unified_old_ds_can_disable_deepseek_without_losing_other_preferences(self):
        self.save(self.root/'settings.json',{**self.settings,'edition':'ds'})
        self.save(self.build/'manifest.json',{**self.manifest,'edition':'ds','deepseek_enabled':True})
        choices={'hidden':['gpt-test'],'context_windows':{'local-test':8192},'compact_limits':{'local-test':7000}}
        self.save(self.root/'model-preferences.json',choices)
        with patch.object(ui.p,'EDITION','unified'):
            self.perform('save_deepseek',{'enabled':False,'key':''})
            value=ui.snapshot(self.root)
            self.assertFalse(value['deepseek_enabled'])
            self.assertEqual(len(value['connections']),1)
        self.assertEqual(ui.monitor.load_json(self.root/'model-preferences.json'),choices)

    def test_unified_settings_reopens_active_legacy_directory(self):
        default=self.root/'monitor';other=self.root/'monitor-proxy';default.mkdir();other.mkdir()
        self.save(default/'settings.json',{**self.settings,'edition':'ds','baseline':str(self.root/'inactive')})
        self.save(other/'settings.json',self.settings)
        (self.home/'config.toml').write_text('model_catalog_json='+json.dumps(str(self.build/'desktop-models.json')))
        with patch.object(ui.monitor,'default_directory',return_value=default),patch.object(ui.p,'EDITION','unified'):
            self.assertEqual(ui.find_directory(),other)
            (self.home/'config.toml').write_text('model="gpt-fixture"')
            with self.assertRaisesRegex(ui.SettingsError,'多个旧安装'):ui.find_directory()

    @unittest.skipUnless(sys.platform in ('darwin','win32'),'Per-user native entry')
    def test_unified_launcher_renames_only_verified_legacy_entry(self):
        runtime=self.root/'runtime';runtime.mkdir();(runtime/'management.py').write_text('#fixture')
        self.save(self.root/'settings.json',{**self.settings,'python_path':sys.executable})
        with patch.object(Path,'home',return_value=self.root/'user'),patch.dict(os.environ,{'APPDATA':str(self.root/'appdata')}):
            destination=Path(ui.ensure_launcher(self.root));old=destination.with_name('Codex 模型设置（扩展版）'+destination.suffix)
            destination.rename(old)
            info=ui.monitor.load_json(self.root/'settings-ui/launcher.json');info['path']=str(old)
            self.save(self.root/'settings-ui/launcher.json',info)
            with patch.object(ui.p,'EDITION','unified'):
                self.assertEqual(ui.ensure_launcher(self.root),str(destination))
            self.assertFalse(old.exists());self.assertTrue(destination.exists())
            self.assertEqual(ui.monitor.load_json(self.root/'settings-ui/launcher.json')['path'],str(destination))

    @unittest.skipUnless(sys.platform in ('darwin','win32'),'Per-user native entry')
    def test_uninstall_removes_only_unchanged_owned_launcher(self):
        runtime=self.root/'runtime';runtime.mkdir();(runtime/'management.py').write_text('#fixture')
        self.save(self.root/'settings.json',{**self.settings,'python_path':sys.executable})
        with patch.object(Path,'home',return_value=self.root/'user'),patch.dict(os.environ,{'APPDATA':str(self.root/'appdata')}):
            destination=Path(ui.ensure_launcher(self.root))
            self.assertTrue(ui.remove_launcher(self.root));self.assertFalse(destination.exists())


if __name__ == '__main__':unittest.main()


class WhaleReliabilityTests(unittest.TestCase):
    def test_footer_wraps_long_actions_and_reserves_back_without_overlap(self):
        for count in range(1,7):
            for back in (False,True):
                buttons=[{'title':'保存设置' if i%2 else '检查补丁更新'} for i in range(count)]
                layout=ui.footer_layout(buttons,back)
                rectangles=[]
                for i in range(count+int(back)):
                    x=14+(i%layout['columns'])*(layout['width']+layout['gap'])
                    y=layout['top']+(i//layout['columns'])*36
                    rect=(x,y,x+layout['width'],y+layout['height'])
                    self.assertGreaterEqual(x,14);self.assertLessEqual(rect[2],306)
                    self.assertGreater(y,200);self.assertLessEqual(rect[3],410)
                    for a,b,c,d in rectangles:
                        self.assertTrue(rect[2]<=a or x>=c or rect[3]<=b or y>=d)
                    rectangles.append(rect)

    def test_carbon_pump_dispatches_and_releases_events(self):
        import ctypes
        from unittest.mock import Mock
        key=ui.Hotkey(Path('/unused'));key.carbon=Mock()
        def queued(_count,_types,_timeout,_remove,event):
            ctypes.cast(event,ctypes.POINTER(ctypes.c_void_p))[0]=ctypes.c_void_p(123)
            return 0
        key.carbon.ReceiveNextEvent.side_effect=queued
        key.carbon.GetEventDispatcherTarget.return_value=456
        with patch.object(ui.sys,'platform','darwin'):key.pump()
        event,target=key.carbon.SendEventToEventTarget.call_args.args
        self.assertEqual((event.value,target),(123,456))
        self.assertEqual(key.carbon.ReleaseEvent.call_args.args[0].value,123)
        key.carbon.ReceiveNextEvent.side_effect=lambda *_:-9875
        key.carbon.SendEventToEventTarget.reset_mock()
        with patch.object(ui.sys,'platform','darwin'):key.pump()
        key.carbon.SendEventToEventTarget.assert_not_called()

    def test_crashed_renderer_recovers_without_losing_host_or_hotkey(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as root:
            state=Path(root);(state/'settings-ui').mkdir()
            first,second=Mock(),Mock()
            first.pid=123;first.returncode=-11;first.poll.side_effect=[None,-11]
            second.pid=456;second.returncode=0;second.poll.side_effect=[None,0]
            with patch.object(ui,'Hotkey') as key,patch.object(ui.subprocess,'Popen',side_effect=[first,second]) as start, \
                 patch.object(ui,'hub_options',return_value={'hotkey':True}),patch.object(ui.time,'sleep'):
                result=ui.run_host(['fixture'],state)
            self.assertEqual(result.returncode,0);self.assertEqual(start.call_count,2)
            self.assertEqual([call.kwargs['env']['CODEX_SETTINGS_RECOVERY'] for call in start.call_args_list],['0','1'])
            self.assertGreaterEqual(key.return_value.pump.call_count,2)
            key.return_value.close.assert_called_once()
            self.assertFalse((state/'settings-ui/host.json').exists())
            status=json.loads((state/'settings-ui/renderer-status.json').read_text())
            self.assertEqual((status['exit'],status['failures']),(-11,1))

    def test_app_launch_returns_without_waiting_for_resident_host(self):
        with tempfile.TemporaryDirectory() as root,patch.object(ui.subprocess,'Popen') as start:
            self.assertEqual(ui.main(['launch','--state-dir',root]),0)
            args=start.call_args.args[0]
            self.assertIn('show',args);self.assertIn(str(Path(root).resolve()),args)
            self.assertEqual(start.call_args.kwargs['stdin'],ui.subprocess.DEVNULL)

    @unittest.skipUnless(sys.platform=='win32','Real windowless launcher lifecycle')
    def test_windows_production_host_survives_hide_reopen_and_unreadable_wake(self):
        import time,subprocess,shutil
        from windows_support import isolated_desktop_options,desktop_process
        case=SettingsTests();case.setUp()
        self.addCleanup(case.doCleanups)
        channel=json.loads((ui.ROOT/'edition.json').read_text())['edition']
        case.save(case.root/'settings.json',{**case.settings,'edition':channel})
        case.save(case.build/'manifest.json',{**case.manifest,'edition':channel})
        ui_dir=case.root/'settings-ui'
        def read(name):
            try:return json.loads((ui_dir/name).read_text(encoding='utf-8'))
            except (FileNotFoundError,ValueError,PermissionError):return {}
        def wait_for(predicate):
            end=time.monotonic()+15
            while time.monotonic()<end:
                if predicate():return
                time.sleep(.1)
            detail=(case.root/'launcher.log').read_bytes().decode('utf-8',errors='replace')[-2000:]
            error=ui_dir/'renderer-error.log'
            if error.exists():detail+=' '+error.read_bytes().decode('utf-8',errors='replace')[-2000:]
            self.fail('Renderer lifecycle timed out: '+repr(read('renderer.json'))+' '+detail)
        command=[str(Path(sys.executable).with_name('pythonw.exe')),str(ui.ROOT/'management.py'),
                 'show','--state-dir',str(case.root)]
        with isolated_desktop_options() as options,(case.root/'launcher.log').open('wb') as log:
            child=desktop_process(command,options['desktop'],cwd=case.root,stdout=log,env=dict(os.environ))
            try:
                wait_for(lambda:read('renderer.json').get('visible'))
                renderer=read('renderer.json')['pid']
                # Readers/antivirus may temporarily deny replacement on Windows.
                # A status write must not terminate the WPF event loop.
                before=read('renderer.json')['time']
                with (ui_dir/'renderer.json').open('rb'):
                    time.sleep(2.5)
                wait_for(lambda:read('renderer.json').get('time',0)>before)
                self.assertEqual(read('renderer.json')['pid'],renderer)
                # A partially written IPC record must not kill a resident GUI.
                (ui_dir/'raise.json').write_text('{',encoding='utf-8')
                time.sleep(.6)
                ui.monitor.atomic_json(ui_dir/'raise.json',{'time':'hide','toggle':True})
                wait_for(lambda:read('renderer.json').get('visible') is False)
                subprocess.run(['taskkill','/PID',str(renderer),'/F'],capture_output=True,creationflags=0x08000000,check=True)
                wait_for(lambda:read('renderer.json').get('pid') not in (None,renderer) and read('renderer.json').get('visible') is False)
                renderer=read('renderer.json')['pid']
                ui.monitor.atomic_json(ui_dir/'raise.json',{'time':'reopen','page':'preferences'})
                wait_for(lambda:read('renderer.json').get('visible') and read('renderer.json').get('route')=='preferences')
                self.assertEqual(read('renderer.json')['pid'],renderer)
                self.assertEqual(read('access.json')['format'],1)
                ui.monitor.atomic_json(ui_dir/'raise.json',{'time':'finish','quit':True})
                self.assertEqual(child.wait(timeout=10),0)
                self.assertFalse((ui_dir/'host.json').exists())
            finally:
                if child.poll() is None:
                    ui.monitor.atomic_json(ui_dir/'raise.json',{'time':'cleanup','quit':True})
                    try:child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pid=read('host.json').get('renderer_pid')
                        if pid:subprocess.run(['taskkill','/PID',str(pid),'/F'],capture_output=True,creationflags=0x08000000)
                        child.kill();child.wait(timeout=5)

    @unittest.skipUnless(sys.platform=='win32','Actual WPF routed input on an isolated desktop')
    def test_windows_routed_buttons_drag_and_tooltips_reach_real_handlers(self):
        import shutil
        from windows_support import isolated_desktop_options,desktop_process,native_ui_command
        case=SettingsTests();case.setUp();self.addCleanup(case.doCleanups)
        channel=json.loads((ui.ROOT/'edition.json').read_text())['edition']
        case.save(case.root/'settings.json',{**case.settings,'edition':channel})
        case.save(case.build/'manifest.json',{**case.manifest,'edition':channel})
        folder=case.root/'settings-ui';folder.mkdir();(folder/'jobs').mkdir()
        ui.brand_assets(folder);ui.bubble_assets(folder)
        with patch.object(ui.p,'EDITION',channel):
            value=ui.snapshot(case.root)
            value.update(hub=ui.hub_shell(case.root),view=ui.hub_view(case.root,'models'),entry={})
        case.save(folder/'view.json',value)
        # The fixture adds only input and assertions. Production Button events,
        # navigation, worker jobs, capture and help handlers all remain intact.
        probe=r'''
function Check($Condition,$Message){if(!$Condition){throw $Message}}
function Press($Button){$Button.RaiseEvent([Windows.RoutedEventArgs]::new([Windows.Controls.Primitives.ButtonBase]::ClickEvent))}
function Mouse-Event($Control,$Event,$Button=$false){
 if($Button){$e=[Windows.Input.MouseButtonEventArgs]::new([Windows.Input.Mouse]::PrimaryDevice,0,[Windows.Input.MouseButton]::Left)}
 else{$e=[Windows.Input.MouseEventArgs]::new([Windows.Input.Mouse]::PrimaryDevice,0)}
 $e.RoutedEvent=$Event;$Control.RaiseEvent($e)
}
function Pointer-Position{return $script:ProbePointer}
foreach($sound in $script:Sounds.Values){$sound.Dispose()};$script:Sounds=@{}
$script:ProbePhase=0;$script:ProbeNode=0;$script:ProbeResult=@{}
$probe=[Windows.Threading.DispatcherTimer]::new();$probe.Interval=[TimeSpan]::FromMilliseconds(450)
$probe.Add_Tick({try{
 if($script:Pending -or $script:CardMotion){return}
 switch($script:ProbePhase){
 0 {
  $area=Work-Area $script:Hub.Left $script:Hub.Top;$script:Hub.Left=$area.Left+200;$script:Hub.Top=$area.Top+220
  $before=[Windows.Point]::new($script:Hub.Left,$script:Hub.Top);$script:ProbePointer=[Drawing.Point]::new(100,100)
  Mouse-Event $script:Whale ([Windows.UIElement]::PreviewMouseLeftButtonDownEvent) $true
  $script:ProbePointer=[Drawing.Point]::new(140,124);Mouse-Event $script:Whale ([Windows.Input.Mouse]::PreviewMouseMoveEvent)
  Check ($script:Hub.Left -gt $before.X -and $script:Hub.Top -gt $before.Y) 'Pointer movement did not drag the hub'
  Mouse-Event $script:Hub ([Windows.UIElement]::PreviewMouseLeftButtonUpEvent) $true
  Check (!$script:DragStart -and ![Windows.Input.Mouse]::Captured) 'Drag capture was not released'
  Mouse-Event $script:Whale ([Windows.UIElement]::PreviewMouseLeftButtonDownEvent) $true
  Mouse-Event $script:Hub ([Windows.UIElement]::PreviewMouseLeftButtonUpEvent) $true
  Press $script:Whale;Check $script:Expanded 'Whale click did not expand';$script:ProbeResult.drag=$true;$script:ProbePhase=1
 }
 1 {
  $node=$script:Nodes[$script:ProbeNode];$node.UpdateLayout()
  $point=$node.TranslatePoint([Windows.Point]::new(($node.ActualWidth/2),($node.ActualHeight/2)),$script:Canvas)
  $hit=$script:Canvas.InputHitTest($point)
  while($hit -and $hit -ne $node){$hit=[Windows.Media.VisualTreeHelper]::GetParent($hit)}
  Check ($hit -eq $node) 'Visible node has no hit target'
  Mouse-Event $node ([Windows.Input.Mouse]::MouseEnterEvent)
  Check ($node.ToolTip.IsOpen -and $node.ToolTip.Content.Text.Length -gt 0) 'Hover description did not open'
  Mouse-Event $node ([Windows.Input.Mouse]::MouseLeaveEvent);Check (!$node.ToolTip.IsOpen) 'Hover description did not close'
  Press $node;Check ([bool]$script:Card) 'Category click did not open a card';$script:ProbePhase=2
 }
 2 {
  Check ($script:View.route -eq $script:Shell.categories[$script:ProbeNode].id) ('Category routed to the wrong page: '+$script:View.route+' '+$script:Notice.Text)
  $script:ProbeNode++;if($script:ProbeNode -lt $script:Nodes.Count){$script:ProbePhase=1}else{$script:ProbePhase=3}
 }
 3 {
  $save=@($script:CardGrid.Children[4].Children|Where-Object {$_.Tag.data.intent -eq 'save_ui_options'})[0]
  $script:Fields.sound.control.IsChecked=$false;Press $save;Check ([bool]$script:Pending) 'Footer action did not submit';$script:ProbePhase=4
 }
 4 {
  Check ($script:View.options.sound -eq $false) 'Footer save did not persist'
  Press $script:Nodes[0];$script:ProbePhase=5
 }
 5 {
  $row=@($script:Rows|Where-Object {$_.row.type -eq 'item' -and $_.row.route})[0]
  Press $row.view.Children[0];Check ([bool]$script:Pending) 'Model row did not navigate';$script:ProbePhase=6
 }
 6 {
  Check ($script:View.route -eq 'models/edit') 'Model row opened the wrong page'
  $back=@($script:CardGrid.Children[4].Children|Where-Object {$_.Content -eq '返回'})[0]
  Press $back;Check ([bool]$script:Pending) 'Back button did not navigate';$script:ProbePhase=7
 }
 7 {
  Check ($script:View.route -eq 'models') 'Back button returned to wrong page'
  $script:ProbeResult.categories=$script:ProbeNode;$script:ProbeResult.tooltips=$true;$script:ProbeResult.save=$true;$script:ProbeResult.row_back=$true
  Write-Json (Join-Path $StateDirectory 'input-result.json') $script:ProbeResult;$probe.Stop();Quit-Hub
 }
 }
}catch{Write-Json (Join-Path $StateDirectory 'input-result.json') @{error=($_.ToString()+$_.ScriptStackTrace)};$probe.Stop();Quit-Hub}})
$probe.Start()
'''
        source=(ui.ROOT/'settings_windows.ps1').read_text(encoding='utf-8-sig')
        self.assertEqual(source.count('$script:Phase=0'),1)
        script=case.root/'input-probe.ps1'
        script.write_text(source.replace('$script:Phase=0',probe+'\n$script:Phase=0'),encoding='utf-8-sig')
        command=native_ui_command(script,{'StateDirectory':str(case.root),
            'PythonExecutable':str(Path(sys.executable).with_name('pythonw.exe')),
            'ControllerPath':str(ui.ROOT/'management.py')})
        command[0]=shutil.which(command[0])
        with isolated_desktop_options() as options,(case.root/'input.log').open('wb') as log:
            child=desktop_process(command,options['desktop'],cwd=case.root,stdout=log,env=dict(os.environ))
            try:self.assertEqual(child.wait(timeout=45),0)
            finally:
                if child.poll() is None:child.kill();child.wait(timeout=5)
        result=case.root/'input-result.json'
        self.assertTrue(result.exists(),(case.root/'input.log').read_bytes().decode('utf-8',errors='replace'))
        self.assertEqual(json.loads(result.read_text(encoding='utf-8')),
                         {'drag':True,'categories':6,'tooltips':True,'save':True,'row_back':True})

    @unittest.skipUnless(sys.platform=='darwin','Native Carbon event queue')
    def test_native_carbon_queue_reaches_registered_shortcut_handler(self):
        import ctypes,time
        with tempfile.TemporaryDirectory() as root:
            key=ui.Hotkey(Path(root),key_code=105)  # F13; production Ctrl-Alt-D stays available.
            key.configure(True)
            try:
                self.assertTrue(key.registered,key.error)
                c=key.carbon
                c.CreateEvent.argtypes=[ctypes.c_void_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_double,ctypes.c_uint32,ctypes.POINTER(ctypes.c_void_p)]
                c.GetMainEventQueue.restype=ctypes.c_void_p
                c.PostEventToQueue.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_int16]
                event=ctypes.c_void_p()
                self.assertEqual(c.CreateEvent(None,0x6b657962,6,0,0,ctypes.byref(event)),0)
                try:self.assertEqual(c.PostEventToQueue(c.GetMainEventQueue(),event,1),0)
                finally:c.ReleaseEvent(event)
                end=time.monotonic()+3
                path=Path(root)/'settings-ui/raise.json'
                while not path.exists() and time.monotonic()<end:key.pump()
                self.assertTrue(json.loads(path.read_text())['toggle'])
                key.configure(False)
                self.assertFalse(json.loads((path.parent/'hotkey.json').read_text())['registered'])
            finally:key.close()


class FirstSetupTests(unittest.TestCase):
    def test_unified_setup_switch_requires_new_detection_and_never_reuses_another_key(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(ui.p,'EDITION','unified'):
            root=Path(temp);setup=root/'setup';setup.mkdir()
            ui.monitor.atomic_json(setup/'draft.json',{'edition':'unified','phase':'draft','connection_mode':'deepseek',
                'app':str(root/'app'),'home':str(root/'home'),'config_digest':'previously-checked','key_service':'previous-key'})
            (setup/'credential.key').write_text('private-fixture-key')
            result=ui.setup_dispatch(root,{'intent':'setup_compatible'},ui.setup_revision(root))
            saved=ui.setup_read(root)
            self.assertEqual(saved['connection_mode'],'compatible')
            self.assertNotIn('config_digest',saved);self.assertNotIn('key_service',saved)
            self.assertFalse((setup/'credential.key').exists())
            self.assertEqual(len(list(setup.glob('credential-before-switch-*.key'))),1)
            self.assertNotIn('setup_prepare',[b['intent'] for b in result['buttons']])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.state=self.root/'state';self.home=self.root/'home';self.app=self.root/'app'
        self.home.mkdir();self.app.mkdir();self.config=self.home/'config.toml';self.config.write_text('model="gpt-original"\n')
        self.values={'app':str(self.app),'home':str(self.home),'name':'fixture','url':'http://127.0.0.1:1234/v1',
            'kind':'openai','wire':'chat_completions','key':'fixture-key','models':'fixture-model','maximum':'262144','maintenance':True}
        self.probe=patch.object(ui,'probe_connection',return_value={'available':['fixture-model'],'specs':{},'warnings':[]})
        self.probe.start();self.addCleanup(self.probe.stop)
        self.inspect=patch('official_client.inspect',return_value={'compatible':True})
        self.inspect.start();self.addCleanup(self.inspect.stop)

    def call(self,intent,values=None,expected=None,**kwargs):
        return ui.perform(self.state,'hub',{'intent':intent,'values':values or {}},
            ui.setup_revision(self.state) if expected is None else expected,**kwargs)

    def test_both_editions_share_first_setup_without_touching_native_config(self):
        original=self.config.read_bytes()
        for edition in ('ds','proxy'):
            with self.subTest(edition=edition),patch.object(ui.p,'EDITION',edition):
                state=self.root/edition
                view=ui.hub_view(state);self.assertEqual(view['route'],'setup')
                self.assertEqual(view['edition'],edition)
                ui.setup_dispatch(state,{'intent':'setup_save','values':self.values},ui.setup_revision(state))
                draft=ui.setup_read(state)
                self.assertNotIn('fixture-key',json.dumps(draft))
                self.assertEqual(self.config.read_bytes(),original)
                self.assertFalse((state/'settings.json').exists())
        with patch.object(ui.p,'EDITION','ds'),self.assertRaises(ui.SettingsError):ui.setup_read(self.root/'proxy')

    def test_manager_conflict_rejected_before_credentials_or_network(self):
        for config in ['openai_base_url="http://127.0.0.1:3425"\n','model_provider="other"\n',
                       '[model_providers.openai]\nbase_url="https://relay.invalid"\n']:
            self.config.write_text(config)
            with self.assertRaises(ui.SettingsError):self.call('setup_save',self.values)
            self.assertFalse((self.state/'setup/credential.key').exists())
            self.assertEqual(self.config.read_text(),config)

    def test_failed_discovery_restores_previous_private_key_and_draft(self):
        with patch.object(ui.p,'EDITION','proxy'):
            self.call('setup_save',self.values);before=ui.setup_revision(self.state)
            with patch.object(ui,'probe_connection',side_effect=OSError('private failure')),self.assertRaises(OSError):
                self.call('setup_save',{**self.values,'key':'replacement'})
            self.assertEqual(ui.setup_revision(self.state),before)
            self.assertEqual((self.state/'setup/credential.key').read_text(),'fixture-key')
            with self.assertRaises(ui.SettingsError):self.call('setup_save',self.values,expected='stale')

    def test_failed_or_cancelled_prepare_cannot_enable_install(self):
        with patch.object(ui.p,'EDITION','proxy'):
            self.call('setup_save',self.values)
            with patch.object(ui,'setup_command',side_effect=ui.SettingsError('setup_validation_failed','failed')),self.assertRaises(ui.SettingsError):self.call('setup_prepare')
            self.assertEqual(ui.setup_read(self.state)['phase'],'draft')
            with self.assertRaises(ui.SettingsError):self.call('setup_install')
            with self.assertRaises(ui.SettingsError):self.call('setup_prepare',cancelled=lambda:True)

    def test_prepare_rechecks_config_and_install_is_resumable(self):
        with patch.object(ui.p,'EDITION','proxy'):
            self.call('setup_save',self.values)
            def prepare(args,cancelled=None):
                out=Path(args[args.index('--output')+1]);out.mkdir(parents=True)
                ui.monitor.atomic_json(out/'manifest.json',{'edition':'proxy','codex_home':args[args.index('--codex-home')+1]})
            with patch.object(ui,'setup_command',side_effect=prepare):self.call('setup_prepare')
            self.assertEqual(ui.setup_read(self.state)['phase'],'prepared')
            with patch.object(ui.p,'require_stopped',side_effect=RuntimeError('running')),patch.object(ui.p,'install') as install,self.assertRaises(RuntimeError):self.call('setup_install')
            install.assert_not_called()
            with patch.object(ui.p,'require_stopped'),patch.object(ui.p,'install') as install,patch.object(ui.monitor,'enable',side_effect=OSError('registration failed')),self.assertRaises(OSError):self.call('setup_install')
            self.assertEqual(install.call_count,1);self.assertEqual(ui.setup_read(self.state)['phase'],'installed')
            with patch.object(ui.p,'install') as install,patch.object(ui.monitor,'enable'),patch.object(ui,'ensure_launcher'),patch.object(ui,'hub_view',return_value={'route':'models'}):
                self.assertEqual(self.call('setup_install')['route'],'models');install.assert_not_called()

    @unittest.skipUnless(sys.platform in ('darwin','win32'),'Native first-install card')
    def test_first_setup_native_card_opens_and_closes_without_installed_state(self):
        channel=json.loads((ui.ROOT/'edition.json').read_text())['edition']
        with patch.object(ui.p,'EDITION',channel):result=ui.show(self.state,self_test=True)
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
        value=json.loads(result.stdout.decode('utf-8-sig'))
        self.assertEqual(value['route'],'setup');self.assertTrue(value['request_completed'])
        self.assertFalse((self.state/'settings.json').exists())

    def test_first_setup_recovers_commit_before_ui_draft_was_updated(self):
        with patch.object(ui.p,'EDITION','proxy'):
            self.call('setup_save',self.values)
            draft=ui.setup_read(self.state);build=self.root/'built';build.mkdir()
            ui.monitor.atomic_json(build/'manifest.json',{'edition':'proxy','codex_home':draft['home'],'status':'installed'})
            draft.update(phase='prepared',build=str(build),manifest_digest='previous-manifest-digest')
            ui.monitor.atomic_json(self.state/'setup/draft.json',draft)
            self.config.write_text('model="installed-model"\n')
            with patch('official_client_install.recover',return_value={'status':'committed'}) as recover,patch.object(ui.p,'install') as install,patch.object(ui.monitor,'enable'),patch.object(ui,'ensure_launcher'),patch.object(ui,'hub_view',return_value={'route':'models'}):
                self.assertEqual(self.call('setup_install')['route'],'models')
                recover.assert_called_once_with(build);install.assert_not_called()

    def test_setup_uses_secret_controls_and_keeps_service_windows_when_filling_missing_specs(self):
        with patch.object(ui.p,'EDITION','proxy'):
            fields={v['key']:v for v in ui.setup_view(self.state)['rows'] if v.get('type')=='field'}
            self.assertEqual(fields['key']['kind'],'secret')
            with patch.object(ui,'probe_connection',return_value={'available':['fixture-model','missing'],'specs':{'fixture-model':{'context_window':1048576}},'warnings':[]}):
                self.call('setup_save',{**self.values,'models':'fixture-model,missing'})
                draft=ui.setup_read(self.state)
                self.assertEqual(draft['connection']['model_specs'],{'missing':{'context_window':262144}})
                self.call('setup_save',{**self.values,'model_0':True,'model_1':False})
                self.assertEqual(ui.setup_read(self.state)['connection']['models'],['fixture-model'])


class SetupProgressTests(unittest.TestCase):
    def test_setup_worker_progress_is_visible_and_does_not_claim_installation(self):
        from update_worker import SetupProgress
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);jobs=root/'settings-ui/jobs';jobs.mkdir(parents=True);identifier='a'*32
            ui.monitor.atomic_json(jobs/(identifier+'.request.json'),{'action':'hub','payload':{}})
            observed=[]
            def operation(directory,action,payload,revision,cancelled):
                progress=SetupProgress(root/'stage.json')
                for phase,done,total in [('building',None,None),('verifying',2,8),('ready',None,None)]:
                    progress.publish(phase,done,total)
                    cancelled.progress(ui.monitor.load_json(root/'stage.json'))
                    observed.append(ui.monitor.load_json(jobs/(identifier+'.result.json')))
                return {'route':'setup'}
            with patch.object(ui,'perform',side_effect=operation):result=ui.run_request(root,identifier)
            self.assertEqual(result['status'],'completed')
            self.assertIn('2/8',observed[1]['message'])
            self.assertTrue(all(v['status']=='running' for v in observed))
            self.assertFalse((root/'settings.json').exists());self.assertFalse((jobs/(identifier+'.request.json')).exists())

    def test_setup_cancel_reaps_real_owned_child_during_verification(self):
        import update_worker,os,time
        from update_progress import _alive
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'setup').mkdir();(root/'builds').mkdir()
            script=root/'patch.py'
            script.write_text("import sys,json,time,os\nfrom pathlib import Path\np=Path(sys.argv[sys.argv.index('--progress-file')+1]);Path(__file__).with_suffix('.pid').write_text(str(os.getpid()))\np.write_text(json.dumps({'phase':'verifying','message':'fixture','completed':1,'total':4}))\ntime.sleep(30)\n",encoding='utf-8')
            observed=[]
            cancelled=lambda:bool(observed)
            cancelled.progress=lambda value: observed.append(value) if value.get('phase')=='verifying' else None
            with patch.object(update_worker.p,'ROOT',root),self.assertRaises(ui.SettingsError) as caught:
                update_worker.setup_command(['build','--output',str(root/'builds/sample'),'--verify'],cancelled)
            self.assertEqual(caught.exception.code,'cancelled')
            self.assertFalse(_alive(int(script.with_suffix('.pid').read_text())))
            self.assertEqual(observed[0]['completed'],1)
            self.assertFalse((root/'settings.json').exists())
