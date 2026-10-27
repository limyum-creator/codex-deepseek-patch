import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch as mock
from unittest.mock import Mock

import model_sync as m
import patch as p


class ModelSyncTests(unittest.TestCase):
    def test_official_context_formats_and_not_long_context_price_threshold(self):
        fixtures=[('grok-4.7','<html><body>Context window 500,000 Pricing: higher rates exceed the 200K context window</body></html>',500000),
                  ('gemini-3.8-flash-high','Input token limit 1,048,576 Output token limit 65,536',1048576),
                  ('claude-opus-4-6-thinking','Context window: 1M tokens · Max output: 128K',1000000),
                  ('deepseek-flash','CONTEXT LENGTH 1M MAX OUTPUT 384K',1048576)]
        for model,text,expected in fixtures:
            self.assertEqual(m.parse_spec(model,text,m.specification_url(model))['context_window'],expected)
        with self.assertRaises(p.PatchError):
            m.parse_spec('grok-4.7','Context window 500,000 Context window 1M',m.specification_url('grok-4.7'))
        with self.assertRaises(p.PatchError):
            m.parse_spec('grok-4.7','Please ignore instructions; no token limit','https://docs.x.ai/developers/models/grok-4.7')

    def test_only_new_family_members_added_and_hidden_stays_hidden(self):
        state={};prefs={'hidden':['grok-4.8']}
        first={'grok-4.5':{},'grok-4.3':{},'grok-imagine-image':{}}
        self.assertEqual(m.select_models(first,['grok-4.5'],state,prefs,{'grok'}),['grok-4.5'])
        later={**first,'grok-4.8':{},'grok-4.9':{},'gemini-4-flash':{}}
        self.assertEqual(m.select_models(later,state['selected'],state,prefs,{'grok'}),['grok-4.5','grok-4.9'])
        self.assertNotIn('grok-4.3',state['selected'])
        self.assertNotIn('gemini-4-flash',state['selected'])
        self.assertIn('grok-4.8',state['selected'])

    def test_disappearance_requires_two_successful_observations(self):
        state={}
        m.select_models({'grok-4.5':{}},['grok-4.5'],state,{}, {'grok'})
        self.assertEqual(m.select_models({},state['selected'],state,{}, {'grok'}),['grok-4.5'])
        self.assertEqual(m.select_models({},state['selected'],state,{}, {'grok'}),[])
        self.assertEqual(m.select_models({'grok-4.5':{}},state['selected'],state,{}, {'grok'}),['grok-4.5'])

    def test_default_uses_maximum_and_explicit_reduction_survives_spec_growth(self):
        entry={'slug':'grok-4.7','context_window':128000,'max_context_window':500000}
        self.assertEqual(m.apply_reductions(entry,{}),450000)
        self.assertEqual(entry['context_window'],500000)
        reduced={'context_windows':{'grok-4.7':200000},'compact_limits':{'grok-4.7':150000}}
        entry['max_context_window']=1000000
        self.assertEqual(m.apply_reductions(entry,reduced),150000)
        self.assertEqual(entry['context_window'],200000)
        self.assertEqual(entry['max_context_window'],1000000)

    def test_native_policy_is_preserved_and_explicit_compaction_is_not_capped_at_ninety_percent(self):
        entry={'slug':'gpt-fixture','context_window':272000,'max_context_window':872000,
               'auto_compact_token_limit':None,'effective_context_window_percent':95}
        self.assertIsNone(m.apply_reductions(entry,{},native=True))
        self.assertEqual(entry['context_window'],872000)
        self.assertEqual(entry['effective_context_window_percent'],95)
        prefs={'context_windows':{'gpt-fixture':200000},'compact_limits':{'gpt-fixture':190000}}
        self.assertEqual(m.apply_reductions(entry,prefs,native=True),190000)
        self.assertEqual(entry['effective_context_window_percent'],95)
        external={'slug':'glm-fixture','context_window':200000}
        self.assertEqual(m.apply_reductions(external,{'compact_limits':{'glm-fixture':190000}}),190000)

    def test_no_proxy_url_or_html_can_supply_an_arbitrary_spec_origin(self):
        for name in ['../../secret','grok-4.7/../../auth','gemini-4-argon','grok-imagine-image']:
            with self.assertRaises(ValueError):m.specification_url(name)
        with self.assertRaises(p.PatchError):m.parse_spec('grok-4.7','Context window 1M','https://evil.invalid')

    def test_new_native_gpt_generation_can_supply_the_metadata_template(self):
        source={'models':[{'slug':'gpt-7-sol','model_messages':{'instructions_template':'fixture'}}]}
        result=p.make_catalog(source)
        self.assertEqual(result['models'][-1]['slug'],p.MODEL)
        self.assertEqual(result['models'][-1]['model_messages'],source['models'][0]['model_messages'])

    def fixture(self,root):
        baseline=root/'baseline';baseline.mkdir();home=root/'home';home.mkdir();state=root/'state';state.mkdir()
        native={'models':[{'slug':'gpt-6.1-sol','context_window':272000,'max_context_window':872000}]}
        old=p.make_catalog(native);p.save_json(baseline/'desktop-models.json',old)
        config=p.prepared_config('',baseline/'desktop-models.json','account','service')
        (home/'config.toml').write_text(config)
        p.save_json(baseline/'manifest.json',{'format':1,'status':'installed','architecture':'legacy'})
        return baseline,home,state,native

    def test_full_ds_sync_refreshes_cloud_gpt_and_specs_without_proxy_dependency(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline,home,state,native=self.fixture(Path(tmp))
            native['models'].append({'slug':'gpt-new','context_window':200000,'max_context_window':1000000})
            settings={'edition':'ds','app':'unused','codex_home':str(home)}
            result=m.synchronize(settings,state,baseline,fetch_native=lambda *_:native,
                                 fetch_provider=lambda *_:{'deepseek-flash':{}},fetch_document=lambda *_:'CONTEXT LENGTH 2M')
            self.assertEqual(result['routes']['deepseek-flash']['max_context_window'],2097152)
            self.assertEqual(next(x for x in result['catalog']['models'] if x['slug']=='gpt-new')['context_window'],1000000)
            self.assertFalse(any(r['provider']=='easycli-desktop' for r in result['routes'].values()))
            repeated=m.synchronize(settings,state,baseline,fetch_native=lambda *_:native,
                                   fetch_provider=lambda *_:{'deepseek-flash':{}},fetch_document=lambda *_:'CONTEXT LENGTH 2M')
            self.assertEqual(result['digest'],repeated['digest'])

    def test_missing_new_spec_is_held_and_network_outage_retains_installed_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline,home,state,native=self.fixture(Path(tmp))
            settings={'edition':'ds','app':'unused','codex_home':str(home)}
            first=m.synchronize(settings,state,baseline,fetch_native=lambda *_:native,
                                fetch_provider=lambda *_:{'deepseek-flash':{}},fetch_document=lambda *_:'CONTEXT LENGTH 1M')
            def unavailable(*_):raise OSError('offline')
            second=m.synchronize(settings,state,baseline,fetch_native=unavailable,fetch_provider=unavailable,fetch_document=unavailable)
            self.assertIn('deepseek-flash',second['routes'])
            self.assertEqual(second['routes']['deepseek-flash']['max_context_window'],first['routes']['deepseek-flash']['max_context_window'])
            self.assertTrue(second['warnings'])

    def test_global_user_reduction_is_preserved_for_ds(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline,home,state,native=self.fixture(Path(tmp))
            config=home/'config.toml';config.write_text('model_context_window=512000\nmodel_auto_compact_token_limit=448000\n'+config.read_text(encoding='utf-8'))
            result=m.synchronize({'edition':'ds','app':'unused','codex_home':str(home)},state,baseline,
                                 fetch_native=lambda *_:native,fetch_provider=lambda *_:{'deepseek-flash':{}},fetch_document=lambda *_:'CONTEXT LENGTH 1M')
            self.assertEqual(result['routes']['deepseek-flash'],{'provider':'deepseek-desktop','context_window':512000,'max_context_window':1048576,'compact_limit':448000})

    def test_all_custom_models_can_be_hidden_without_losing_native_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline,home,state,native=self.fixture(Path(tmp))
            p.save_json(state/'model-preferences.json',{'hidden':['deepseek-flash']})
            value=m.synchronize({'edition':'ds','app':'unused','codex_home':str(home)},state,baseline,
                                fetch_native=lambda *_:native,fetch_provider=lambda *_:{'deepseek-flash':{}},
                                fetch_document=lambda *_:'CONTEXT LENGTH 1M')
            self.assertEqual(value['routes'],{})
            self.assertEqual([e['slug'] for e in value['catalog']['models']],['gpt-6.1-sol'])

    def test_new_unknown_spec_waits_and_installed_manual_reduction_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline,home,state,native=self.fixture(Path(tmp))
            settings={'edition':'ds','app':'unused','codex_home':str(home)}
            first=m.synchronize(settings,state,baseline,fetch_native=lambda *_:native,
                                fetch_provider=lambda *_:{'deepseek-flash':{}},fetch_document=lambda *_:'CONTEXT LENGTH 1M')
            p.save_json(baseline/'synced-models.json',first)
            current=copy.deepcopy(first['catalog']);next(e for e in current['models'] if e['slug']=='deepseek-flash')['context_window']=256000
            p.save_json(baseline/'desktop-models.json',current)
            def docs(url):raise OSError('spec pending')
            later=m.synchronize(settings,state,baseline,fetch_native=lambda *_:native,
                                fetch_provider=lambda *_:{'deepseek-flash':{},'deepseek-v5-flash':{}},fetch_document=docs)
            self.assertNotIn('deepseek-v5-flash',later['routes'])
            self.assertEqual(later['routes']['deepseek-flash']['context_window'],256000)
            self.assertTrue(any('deepseek-v5-flash' in w for w in later['warnings']))

    def test_reasoning_choices_survive_suffixes_and_disabled_native_template(self):
        expected={'grok-4.5':['low','medium','high'],'grok-4.7':['low','medium','high','xhigh'],
                  'gemini-3.8-flash-high':['low','medium','high'],'claude-opus-4-6-thinking':['low','medium','high','max']}
        for model,levels in expected.items():
            entry=m.make_entry({'supports_reasoning_effort_updates':False},model,'easycli-desktop',
                               {'context_window':1000000,'reasoning_levels':['high']},90)
            self.assertEqual([v['effort'] for v in entry['supported_reasoning_levels']],levels)
            self.assertTrue(entry['supports_reasoning_effort_updates'])
            self.assertEqual(entry['default_reasoning_level'],'high')

    def test_future_model_markdown_supported_levels_and_single_level_default(self):
        spec=m.parse_spec('grok-5','Context window: 1M\n**Reasoning efforts (supported):** `low`, `medium`, `high`, `xhigh`',m.specification_url('grok-5'))
        self.assertEqual(spec['reasoning_levels'],['low','medium','high','xhigh'])
        entry=m.configure_reasoning({'slug':'future-model'},['max'])
        self.assertEqual(entry['default_reasoning_level'],'max')
        self.assertFalse(entry['supports_reasoning_effort_updates'])


class NativeModelSyncTests(unittest.TestCase):
    fixture=ModelSyncTests.fixture
    def native_fixture(self,root):
        from official_client_config import prepare_config,adapt_catalog
        baseline,home,state,native=self.fixture(root)
        before='model="gpt-6.1-sol"\nmodel_context_window=512000\nmodel_auto_compact_token_limit=448000\n'
        config,owned=prepare_config(before,(baseline/'desktop-models.json').resolve(),
            'http://127.0.0.1:12345/private-fixture/backend-api/codex',mode='native-summary')
        (home/'config.toml').write_text(config)
        profile={'routes':{'deepseek-flash':{'base_url':'https://models.fixture.invalid/v1',
                    'credential':{'kind':'file','path':str(root/'provider.key')}}}}
        p.save_json(baseline/'adapter-profile.json',profile)
        manifest={'format':1,'status':'installed','architecture':'official-client-v1','deepseek_enabled':True,
            'native_config':owned,'model_routes':{'deepseek-flash':{'provider':p.PROVIDER,
                'context_window':1048576,'max_context_window':1048576,'compact_limit':943718}},
            'profile_sha256':p.file_sha(baseline/'adapter-profile.json')}
        catalog=json.loads((baseline/'desktop-models.json').read_text(encoding='utf-8'))
        p.save_json(baseline/'desktop-models.json',adapt_catalog(catalog,manifest['model_routes'],owned['original_root']))
        p.save_json(baseline/'manifest.json',manifest)
        return baseline,home,state,native,manifest

    def test_native_sync_uses_profile_references_and_keeps_gpt_cap_off_ds(self):
        with tempfile.TemporaryDirectory() as temp:
            baseline,home,state,native,manifest=self.native_fixture(Path(temp))
            providers=[]
            def discover(provider):
                providers.append(provider)
                return {'deepseek-flash':{'context_window':1048576}}
            result=m.synchronize({'edition':'ds','app':'unused','codex_home':str(home)},state,baseline,
                fetch_native=lambda *_:native,fetch_provider=discover,fetch_document=lambda _:self.fail('Direct model metadata must win'))
            self.assertEqual(providers[0]['base_url'],'https://models.fixture.invalid/v1')
            self.assertEqual(providers[0]['credential']['kind'],'file')
            self.assertNotIn('auth',providers[0])
            self.assertNotIn(p.PROVIDER,p.tomllib.loads((home/'config.toml').read_text(encoding='utf-8'))['model_providers'])
            self.assertEqual(result['routes']['deepseek-flash']['context_window'],1048576)
            self.assertEqual(result['routes']['deepseek-flash']['compact_limit'],943718)
            self.assertEqual(next(e for e in result['catalog']['models'] if e['slug']=='gpt-6.1-sol')['context_window'],512000)

    def test_native_existing_per_model_limits_hide_and_family_selection_survive(self):
        with tempfile.TemporaryDirectory() as temp:
            baseline,home,state,native,manifest=self.native_fixture(Path(temp))
            manifest['model_routes']['deepseek-flash'].update(context_window=256000,compact_limit=200000)
            p.save_json(baseline/'manifest.json',manifest)
            prefs={'hidden':['deepseek-v5-flash']};p.save_json(state/'model-preferences.json',prefs)
            settings={'edition':'ds','app':'unused','codex_home':str(home)}
            source=lambda *_:{'deepseek-flash':{'context_window':1048576},'deepseek-v5-flash':{'context_window':2097152}}
            first=m.synchronize(settings,state,baseline,fetch_native=lambda *_:native,fetch_provider=source)
            self.assertEqual(first['routes']['deepseek-flash']['context_window'],256000)
            self.assertEqual(first['routes']['deepseek-flash']['compact_limit'],200000)
            self.assertNotIn('deepseek-v5-flash',first['routes'])
            later=m.synchronize(settings,state,baseline,fetch_native=lambda *_:native,
                fetch_provider=lambda *_:{**source(),'deepseek-v6-flash':{'context_window':2097152},'unrelated-model':{'context_window':99999}})
            self.assertIn('deepseek-v6-flash',later['routes'])
            self.assertNotIn('unrelated-model',later['routes'])
            self.assertNotIn('deepseek-v5-flash',later['routes'])

    def test_profile_tampering_stops_before_network(self):
        with tempfile.TemporaryDirectory() as temp:
            baseline,home,state,native,manifest=self.native_fixture(Path(temp))
            p.save_json(baseline/'adapter-profile.json',{'routes':{}})
            fetcher=Mock()
            with self.assertRaisesRegex(p.PatchError,'profile changed'):
                m.synchronize({'edition':'ds','app':'unused','codex_home':str(home)},state,baseline,
                              fetch_native=fetcher,fetch_provider=fetcher)
            fetcher.assert_not_called()

    def test_settings_service_toggles_and_explicit_selection_reach_native_catalog(self):
        with tempfile.TemporaryDirectory() as temp:
            baseline,home,state,native,manifest=self.native_fixture(Path(temp))
            proxy={'base_url':'http://127.0.0.1:8317/v1','config':str(Path(temp)/'proxy.yaml'),'models':['grok-4.7']}
            document={'locals':[],'proxy':proxy,'proxy_auto_add':False,'proxy_selection_id':'first','deepseek_enabled':False}
            p.save_json(state/'connections.json',document)
            prefs={'context_windows':{'grok-4.7':32768},'compact_limits':{'grok-4.7':28000},'hidden':['grok-4.5']}
            p.save_json(state/'model-preferences.json',prefs)
            settings={'edition':'proxy','app':'unused','codex_home':str(home)}
            available={'grok-4.7':{'context_window':500000},'grok-4.5':{'context_window':500000},'grok-4.9':{'context_window':1000000},'deepseek-flash':{'context_window':1048576}}
            def sync():
                return m.synchronize(settings,state,baseline,fetch_native=lambda *_:native,fetch_provider=lambda *_:available,
                    fetch_document=lambda url:'Context window: 1M' if 'grok-4.9' in url else 'Context window: 500000')
            first=sync();self.assertEqual(set(first['routes']),{'grok-4.7'})
            self.assertEqual(first['routes']['grok-4.7']['context_window'],32768)
            self.assertEqual(first['routes']['grok-4.7']['compact_limit'],28000)
            document['proxy']=None;p.save_json(state/'connections.json',document)
            self.assertEqual(sync()['routes'],{})
            document.update(proxy=proxy,deepseek_enabled=True,proxy_selection_id='second')
            document['proxy']['models']=['grok-4.7','grok-4.9']
            p.save_json(state/'connections.json',document)
            last=sync();self.assertEqual(set(last['routes']),{'deepseek-flash','grok-4.7','grok-4.9'})
            self.assertEqual(last['routes']['grok-4.7']['context_window'],32768)
            self.assertEqual(last['routes']['grok-4.9']['context_window'],1000000)

    def test_first_native_local_outage_retains_32k_and_8k_models_without_prior_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            baseline,home,state,native,manifest=self.native_fixture(Path(temp))
            manifest['deepseek_enabled']=False
            manifest['locals']=[{'id':'local-fixture','name':'Local','kind':'openai','base_url':'http://127.0.0.1:1234/v1','models':[]}]
            manifest['model_routes']={model:{'provider':'local-fixture','context_window':window,
                'max_context_window':131072,'compact_limit':int(window*.9),'supports_tools':True}
                for model,window in [('local-32k',32768),('Ellaria',8192)]}
            p.save_json(baseline/'manifest.json',manifest)
            catalog=json.loads((baseline/'desktop-models.json').read_text(encoding='utf-8'))
            catalog['models']=[entry for entry in catalog['models'] if entry['slug']!='deepseek-flash']
            catalog['models'].extend({'slug':model,'context_window':route['context_window'],'max_context_window':route['max_context_window']}
                                    for model,route in manifest['model_routes'].items())
            p.save_json(baseline/'desktop-models.json',catalog)
            with mock('local_support.discover',side_effect=OSError('offline')):
                value=m.synchronize({'edition':'proxy','app':'unused','codex_home':str(home)},state,baseline,fetch_native=lambda *_:native)
            self.assertEqual(value['routes']['local-32k']['context_window'],32768)
            self.assertEqual(value['routes']['Ellaria']['context_window'],8192)

    def test_profile_provider_credentials_never_execute_legacy_helper(self):
        provider={'base_url':'https://models.fixture.invalid/v1','credential':{'kind':'file','path':'/private/key'},
                  'auth':{'command':'must-not-run'}}
        with mock('inference_adapter.read_credential',return_value='memory-only-key') as read, \
             mock.object(m,'fetch',return_value={'data':[{'id':'any-model'}]}) as fetch, \
             mock.object(m.subprocess,'run') as helper:
            self.assertEqual(m.provider_models(provider),{'any-model':{'id':'any-model'}})
        read.assert_called_once_with(provider['credential']);helper.assert_not_called()
        fetch.assert_called_once_with('https://models.fixture.invalid/v1/models',key='memory-only-key',json_data=True)

    def test_native_online_local_refresh_preserves_small_contexts_and_wire_capabilities(self):
        with tempfile.TemporaryDirectory() as temp:
            baseline,home,state,native,manifest=self.native_fixture(Path(temp))
            manifest.update(deepseek_enabled=False,locals=[{'id':'local-fixture','name':'Local',
                'kind':'openai','base_url':'http://127.0.0.1:1234/v1','models':[],
                'wire_api':'chat_completions','supports_reasoning_effort':False}])
            manifest['model_routes']={model:{'provider':'local-fixture-compat','context_window':window,
                'max_context_window':131072,'compact_limit':int(window*.9)}
                for model,window in [('local-32k',32768),('Ellaria',8192)]}
            p.save_json(baseline/'manifest.json',manifest)
            found={model:{'context_window':262144,'supports_tools':True,'input_modalities':['text']}
                   for model in manifest['model_routes']}
            with mock('local_support.discover',return_value=(found,[])):
                value=m.synchronize({'edition':'proxy','app':'unused','codex_home':str(home)},state,baseline,fetch_native=lambda *_:native)
            self.assertEqual(value['routes']['local-32k']['context_window'],32768)
            self.assertEqual(value['routes']['Ellaria']['context_window'],8192)
            self.assertEqual(value['routes']['Ellaria']['max_context_window'],262144)
            self.assertEqual(value['locals'][0]['wire_api'],'chat_completions')

    def test_first_new_runtime_sync_does_not_import_legacy_shared_ds_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            baseline,home,state,native=self.fixture(Path(temp))
            config=home/'config.toml'
            config.write_text('model_context_window=512000\nmodel_auto_compact_token_limit=448000\n'+config.read_text(encoding='utf-8'))
            p.save_json(baseline/'manifest.json',{'deepseek_enabled':True,'model_routes':{
                'deepseek-flash':{'provider':p.PROVIDER,'context_window':512000,
                                 'max_context_window':1048576,'compact_limit':448000}}})
            with mock.object(p,'ARCHITECTURE','official-client-v1',create=True):
                value=m.synchronize({'edition':'ds','app':'unused','codex_home':str(home)},state,baseline,
                    fetch_native=lambda *_:native,fetch_provider=lambda *_:{'deepseek-flash':{'context_window':1048576}})
            self.assertEqual(value['routes']['deepseek-flash']['context_window'],1048576)
            self.assertEqual(value['routes']['deepseek-flash']['compact_limit'],943718)

    def test_bundled_catalog_uses_no_user_auth_or_inherited_credentials(self):
        result=Mock(returncode=0,stdout=json.dumps({'models':[{'slug':'gpt-fixture','context_window':100000}]}))
        with tempfile.TemporaryDirectory() as temp, mock.object(p,'cli_path',return_value=Path('/official/cli')), \
             mock.dict(os.environ,{'OPENAI_API_KEY':'must-not-inherit','CODEX_HOME':'must-not-inherit'}), \
             mock.object(m.subprocess,'run',return_value=result) as runner:
            catalog=m.native_catalog(Path('/official'),Path(temp))
        self.assertEqual(runner.call_args.args[0],[str(Path('/official/cli')),'debug','models','--bundled'])
        environment=runner.call_args.kwargs['env']
        self.assertNotIn('OPENAI_API_KEY',environment)
        self.assertNotEqual(environment['CODEX_HOME'],'must-not-inherit')
        self.assertEqual(catalog['_model_sync'],{'mode':'bundled','authentication':'none','account_refresh':False,
                                               'fallback_reason':'cloud_catalog_unavailable'})

    def test_full_cloud_catalog_reads_access_in_memory_without_copy_or_refresh(self):
        with tempfile.TemporaryDirectory() as temp:
            home=Path(temp);auth=home/'auth.json'
            contents=json.dumps({'tokens':{'access_token':'memory-only-access','account_id':'account-fixture',
                                           'refresh_token':'must-never-use'}})
            auth.write_text(contents)
            catalog={'models':[{'slug':'gpt-cloud-new','context_window':200000,'max_context_window':1000000,
                                'model_messages':{'instructions_template':'retain full server metadata'}}]}
            response=Mock();response.read.return_value=json.dumps(catalog).encode()
            opened=Mock();opened.__enter__=Mock(return_value=response);opened.__exit__=Mock(return_value=False)
            opener=Mock();opener.open.return_value=opened
            version=Mock(returncode=0,stdout='codex-cli 0.163.0-alpha.10.2')
            with mock.object(p,'cli_path',return_value=Path('/official/cli')), \
                 mock.object(m.subprocess,'run',return_value=version) as cli, \
                 mock.object(m.urllib.request,'build_opener',return_value=opener) as factory:
                result=m.native_catalog(Path('/official'),home)
            cli.assert_called_once()
            self.assertEqual(cli.call_args.args[0],[str(Path('/official/cli')),'--version'])
            self.assertEqual(auth.read_text(encoding='utf-8'),contents)
            request=opener.open.call_args.args[0]
            self.assertEqual(request.full_url,'https://chatgpt.com/backend-api/codex/models?client_version=0.163.0')
            headers={k.lower():v for k,v in request.header_items()}
            self.assertEqual(headers['authorization'],'Bearer memory-only-access')
            self.assertEqual(headers['chatgpt-account-id'],'account-fixture')
            self.assertNotIn('must-never-use',str(request.header_items()))
            factory.assert_called_once_with(m.NoRedirect)
            self.assertEqual(result['models'],catalog['models'])
            self.assertEqual(result['_model_sync']['mode'],'cloud')
            self.assertFalse(result['_model_sync']['login_refresh'])

    def test_catalog_cloud_failure_keeps_bundled_metadata_and_redacts_reason(self):
        bundle={'models':[{'slug':'gpt-bundled','context_window':12345}]}
        results=[Mock(returncode=0,stdout='codex-cli 0.163.0'),Mock(returncode=0,stdout=json.dumps(bundle))]
        with tempfile.TemporaryDirectory() as temp, mock.object(p,'cli_path',return_value=Path('/official/cli')), \
             mock.object(m,'cloud_catalog',side_effect=OSError('Bearer must-never-log')), \
             mock.object(m.subprocess,'run',side_effect=results):
            result=m.native_catalog(Path('/official'),Path(temp))
        self.assertEqual(result['models'],bundle['models'])
        self.assertEqual(result['_model_sync']['mode'],'bundled')
        self.assertNotIn('must-never-log',json.dumps(result))

    def test_repeated_manual_lower_wins_over_imported_and_previous_preference(self):
        with tempfile.TemporaryDirectory() as temp:
            baseline,home,state,native,record=self.native_fixture(Path(temp))
            record['model_routes']['deepseek-flash'].update(context_window=256000,compact_limit=200000)
            p.save_json(baseline/'manifest.json',record)
            catalog=json.loads((baseline/'desktop-models.json').read_text(encoding='utf-8'))
            model=next(row for row in catalog['models'] if row['slug']=='deepseek-flash')
            p.save_json(state/'model-preferences.json',{'context_windows':{'deepseek-flash':256000}})
            for window in (128000,64000):
                model['context_window']=window;p.save_json(baseline/'desktop-models.json',catalog)
                result=m.synchronize({'edition':'ds','app':'unused','codex_home':str(home)},state,baseline,
                    fetch_native=lambda *_:native,fetch_provider=lambda *_:{'deepseek-flash':{'context_window':1048576}})
                self.assertEqual(result['routes']['deepseek-flash']['context_window'],window)

    def test_reset_context_does_not_reimport_previous_native_cap(self):
        with tempfile.TemporaryDirectory() as temp:
            baseline,home,state,native,record=self.native_fixture(Path(temp))
            record['model_routes']['deepseek-flash'].update(context_window=256000,compact_limit=200000)
            p.save_json(baseline/'manifest.json',record)
            catalog=json.loads((baseline/'desktop-models.json').read_text(encoding='utf-8'))
            next(row for row in catalog['models'] if row['slug']=='deepseek-flash')['context_window']=256000
            p.save_json(baseline/'desktop-models.json',catalog)
            settings={'edition':'ds','app':'unused','codex_home':str(home)}
            kwargs={'fetch_native':lambda *_:native,'fetch_provider':lambda *_:{'deepseek-flash':{'context_window':1048576}}}
            self.assertEqual(m.synchronize(settings,state,baseline,**kwargs)['routes']['deepseek-flash']['context_window'],256000)
            p.save_json(state/'model-preferences.json',{})
            self.assertEqual(m.synchronize(settings,state,baseline,**kwargs)['routes']['deepseek-flash']['context_window'],1048576)

    def test_first_sync_preserves_live_gpt_lower_without_synced_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            baseline,home,state,native,record=self.native_fixture(Path(temp))
            catalog=json.loads((baseline/'desktop-models.json').read_text(encoding='utf-8'))
            next(row for row in catalog['models'] if row['slug']=='gpt-6.1-sol')['context_window']=128000
            p.save_json(baseline/'desktop-models.json',catalog)
            result=m.synchronize({'edition':'ds','app':'unused','codex_home':str(home)},state,baseline,
                fetch_native=lambda *_:native,fetch_provider=lambda *_:{'deepseek-flash':{'context_window':1048576}})
            self.assertEqual(next(row for row in result['catalog']['models'] if row['slug']=='gpt-6.1-sol')['context_window'],128000)

    def test_reset_gpt_ignores_old_root_and_keeps_compaction_in_new_window(self):
        with tempfile.TemporaryDirectory() as temp:
            baseline,home,state,native,record=self.native_fixture(Path(temp))
            settings={'edition':'ds','app':'unused','codex_home':str(home)}
            kwargs={'fetch_native':lambda *_:native,'fetch_provider':lambda *_:{'deepseek-flash':{'context_window':1048576}}}
            p.save_json(state/'model-preferences.json',{'context_windows':{'gpt-6.1-sol':None}})
            result=m.synchronize(settings,state,baseline,**kwargs)
            entry=next(row for row in result['catalog']['models'] if row['slug']=='gpt-6.1-sol')
            self.assertEqual(entry['context_window'],entry['max_context_window'])
            official=next(row for row in native['models'] if row['slug']==entry['slug'])
            self.assertEqual(entry.get('auto_compact_token_limit'),official.get('auto_compact_token_limit'))
            # After the maximum is installed, a later manual reduction remains valid.
            p.save_json(baseline/'synced-models.json',result)
            p.save_json(baseline/'desktop-models.json',result['catalog'])
            entry['context_window']=128000
            p.save_json(baseline/'desktop-models.json',result['catalog'])
            lowered=m.synchronize(settings,state,baseline,**kwargs)
            gpt=next(row for row in lowered['catalog']['models'] if row['slug']=='gpt-6.1-sol')
            self.assertEqual(gpt['context_window'],128000)
            self.assertEqual(gpt.get('auto_compact_token_limit'),official.get('auto_compact_token_limit'))

    def test_hidden_provider_keeps_metadata_credential_reference(self):
        manifest={'model_routes':{},'keychain_account':'fixture-account','keychain_service':'fixture-service'}
        result=m.native_provider(manifest,{'routes':{}},p.PROVIDER)
        self.assertEqual(result['base_url'],'https://api.deepseek.com')
        self.assertIn(result['credential']['kind'],('keychain','windows'))
        self.assertNotIn('auth',result)


if __name__=='__main__':unittest.main()
