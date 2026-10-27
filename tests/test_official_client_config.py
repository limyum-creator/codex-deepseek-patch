import copy
import argparse
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch as mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from official_client_config import PROVIDER, adapt_catalog, migrate_legacy_routes, prepare_config, restore_config


class NativeConfigTest(unittest.TestCase):
    endpoint = "http://127.0.0.1:53991/private-fixture-token/backend-api/codex"
    catalog = str(Path(__file__).resolve().parent / "fixture-catalog.json")

    def test_native_compression_preserves_defaults_and_user_choices_across_updates(self):
        from official_client_config import rollback_config
        for setting in (None,True,False):
            text = '' if setting is None else '[features]\nenable_request_compression='+str(setting).lower()+'\n'
            with self.subTest(setting=setting):
                fresh, owned = prepare_config(text,self.catalog,self.endpoint)
                self.assertEqual(tomllib.loads(fresh).get('features',{}).get('enable_request_compression'),setting)
                # Recreate ownership from the released adapter, which forced false.
                old=dict(owned);old.pop('compression_policy');old.pop('compression_after')
                from official_client_config import _replace_section
                legacy=_replace_section(fresh,('features',),{'enable_request_compression':False})
                updated, next_owned=prepare_config(legacy,self.catalog,self.endpoint,previous=old)
                self.assertEqual(tomllib.loads(updated)['features'].get('enable_request_compression'),setting)
                self.assertEqual(tomllib.loads(rollback_config(updated,next_owned,legacy)),tomllib.loads(legacy))
                changed=_replace_section(updated,('features',),{'enable_request_compression':True})
                again, final_owned=prepare_config(changed,self.catalog,self.endpoint,previous=next_owned)
                self.assertTrue(tomllib.loads(again)['features']['enable_request_compression'])
                restored=restore_config(again,final_owned)
                self.assertTrue(tomllib.loads(restored)['features']['enable_request_compression'])
                from official_client_install import _original_config
                self.assertTrue(_original_config(restored,final_owned),
                                'Interrupted uninstall must recognize the restored config with a user compression choice')

    def test_changes_only_owned_settings_and_can_restore(self):
        text = ('# user comment\nmodel="gpt-6-astra"\nmodel_context_window=512000\n'
                'model_auto_compact_token_limit=448000\n'
                '[features]\nmemories=true # preserve\n'
                '[model_providers.personal]\nname="Personal"\nbase_url="https://example.invalid/v1"\n'
                '[mcp_servers.example]\ncommand="unchanged"\n')
        output, owned = prepare_config(text, self.catalog, self.endpoint, mode="native-summary")
        parsed = tomllib.loads(output)
        self.assertEqual(parsed["model"], "gpt-6-astra")
        self.assertEqual(parsed["model_provider"], PROVIDER)
        self.assertEqual(parsed["model_providers"][PROVIDER]["name"], "OpenAI")
        self.assertTrue(parsed["model_providers"][PROVIDER]["requires_openai_auth"])
        self.assertEqual(parsed["model_providers"][PROVIDER]["capabilities"]["remote_compaction"], "unsupported")
        self.assertNotIn("chatgpt_base_url", parsed)
        self.assertNotIn("model_context_window", parsed)
        self.assertIn("memories=true # preserve", output)
        self.assertEqual(tomllib.loads(restore_config(output, owned)), tomllib.loads(text))

    def test_update_preserves_original_ownership(self):
        text = 'model_context_window=256000\n[features]\nenable_request_compression=true\n'
        first, ownership = prepare_config(text, self.catalog, self.endpoint, mode="native-summary")
        second, next_ownership = prepare_config(first, self.catalog, self.endpoint.replace('53991', '53992'),
                                                mode="native-summary", previous=ownership)
        self.assertEqual(tomllib.loads(restore_config(second, next_ownership)), tomllib.loads(text))

    def test_legacy_custom_provider_migrates_to_native_identity_and_rolls_back(self):
        from official_client_config import rollback_config
        text = 'model="gpt-6.1-sol"\nmodel_context_window=512000\n[features]\nmemories=true\n'
        legacy, old = prepare_config(text, self.catalog, self.endpoint, mode='native-summary')
        legacy += '\n[mcp_servers.user_owned]\ncommand="preserved"\n'
        migrated, owned = prepare_config(legacy, self.catalog, self.endpoint, previous=old)
        parsed = tomllib.loads(migrated)
        self.assertEqual(parsed['model_provider'], 'openai')
        self.assertEqual(parsed['model_providers'][PROVIDER], owned['provider'])
        self.assertNotIn('chatgpt_base_url', parsed)
        self.assertEqual(parsed['mcp_servers']['user_owned']['command'], 'preserved')
        self.assertEqual(tomllib.loads(rollback_config(migrated, owned, legacy)), tomllib.loads(legacy))
        expected = tomllib.loads(text)
        expected['mcp_servers'] = {'user_owned':{'command':'preserved'}}
        self.assertEqual(tomllib.loads(restore_config(migrated, owned)), expected)

    def test_restore_preserves_unrelated_user_edits(self):
        output, owned = prepare_config('model="gpt-6-astra"\n', self.catalog, self.endpoint, mode="native-summary")
        output = output.replace('model="gpt-6-astra"', 'model="gpt-6.1-sol"')
        restored = tomllib.loads(restore_config(output, owned))
        self.assertEqual(restored["model"], "gpt-6.1-sol")
        self.assertNotIn("model_provider", restored)

    def test_user_changed_managed_endpoint_is_not_overwritten(self):
        output, owned = prepare_config('', self.catalog, self.endpoint)
        altered = output.replace(self.endpoint, "https://example.invalid/v1")
        with self.assertRaises(ValueError):
            prepare_config(altered, self.catalog, self.endpoint, previous=owned)
        with self.assertRaises(ValueError):
            restore_config(altered, owned)

    def test_existing_provider_and_endpoint_are_not_claimed(self):
        for text in ('openai_base_url="https://example.invalid/v1"\n',
                     '[model_providers.codex-model-adapter]\nname="User owned"\n'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                prepare_config(text, self.catalog, self.endpoint, mode="native-summary")

    def test_quoted_keys_and_sections_keep_semantics(self):
        text = '"model_context_window"=128000\n["features"]\n"memories"=true\n'
        output, owned = prepare_config(text, self.catalog, self.endpoint)
        self.assertEqual(tomllib.loads(restore_config(output, owned)), tomllib.loads(text))

    def test_unrelated_dotted_or_multiline_configuration_untouched(self):
        text = 'instructions="""Long\nmodel_context_window=3\n"""\n[features]\nmemories=true\n'
        # A false key anchor inside a multiline value must fail instead of mutating it.
        with self.assertRaises(ValueError):
            prepare_config(text, self.catalog, self.endpoint)

    def test_native_preferences_do_not_reduce_external_models(self):
        catalog = {"models": [
            {"slug":"gpt", "context_window":1000000, "max_context_window":1000000},
            {"slug":"ds", "context_window":1048576, "max_context_window":1048576, "comp_hash":"official"},
            {"slug":"local", "context_window":32768, "max_context_window":262144}]}
        before = copy.deepcopy(catalog)
        routes = {"ds": {"context_window":1048576,"max_context_window":1048576,"compact_limit":943718},
                  "local": {"context_window":32768,"max_context_window":262144,"compact_limit":29491}}
        values = adapt_catalog(catalog, routes, {"model_context_window":512000,"model_auto_compact_token_limit":448000})["models"]
        self.assertEqual(values[0]["context_window"], 512000)
        self.assertEqual(values[0]["auto_compact_token_limit"], 448000)
        self.assertEqual(values[1]["context_window"], 1048576)
        self.assertIsNone(values[1]["comp_hash"])
        self.assertEqual(values[2]["context_window"], 32768)
        self.assertEqual(catalog, before)

    def test_tool_capable_routes_inherit_native_variants_and_chat_only_stays_disabled(self):
        native = {"slug":"gpt-6.1-sol", "node_repl_disabled":False,
                  "apply_patch_tool_type":"freeform", "multi_agent_version":"v2"}
        disabled = {"node_repl_disabled":True,"apply_patch_tool_type":None,"multi_agent_version":None}
        names = ("local-any", "claude-compatible", "glm-compatible", "chat-only")
        catalog = {"models":[native] + [{"slug":name,**disabled} for name in names]}
        routes = {name:{"context_window":262144,"max_context_window":262144,"compact_limit":235929,
                        "supports_tools":name != "chat-only"} for name in names}
        before = copy.deepcopy(catalog)
        actual = adapt_catalog(catalog, routes, {})["models"]
        for model in actual[1:4]:
            self.assertEqual({k:model[k] for k in disabled}, {k:native[k] for k in disabled})
        self.assertEqual({k:actual[4][k] for k in disabled}, disabled)
        self.assertEqual(catalog, before)

    def test_legacy_gpt_limit_does_not_cap_ds_but_explicit_reduction_survives(self):
        routes = {'ds':{'provider':'deepseek-desktop', 'context_window':512000,
                        'max_context_window':1048576, 'compact_limit':448000}}
        native = {'model_context_window':512000, 'model_auto_compact_token_limit':448000}
        result = migrate_legacy_routes(routes, native, {})
        self.assertEqual(result['ds']['context_window'], 1048576)
        explicit = migrate_legacy_routes(routes, native, {'context_windows':{'ds':256000}})
        self.assertEqual(explicit['ds']['context_window'], 256000)
        self.assertEqual(explicit['ds']['compact_limit'], 230400)
        self.assertEqual(migrate_legacy_routes(routes, {**native, 'model_provider':'deepseek-desktop'}, {}), routes)

    def test_explicit_reset_of_compaction_migrates_to_current_model_window(self):
        routes = {'ds':{'provider':'deepseek-desktop', 'context_window':512000,
                        'max_context_window':1048576, 'compact_limit':448000}}
        native = {'model_context_window':512000}
        for context, expected in (({},1048576), ({'ds':None},1048576), ({'ds':32768},32768)):
            with self.subTest(context=context):
                actual=migrate_legacy_routes(routes,native,{'context_windows':context,'compact_limits':{'ds':None}})['ds']
                self.assertEqual(actual['context_window'],expected)
                self.assertEqual(actual['compact_limit'],expected*9//10)

    def test_existing_native_catalog_does_not_reapply_historical_root_preferences(self):
        catalog={'models':[{'slug':'gpt-fixture','context_window':128000,'max_context_window':1000000,
                            'auto_compact_token_limit':100000}]}
        result=adapt_catalog(catalog,{}, {'model_context_window':512000,'model_auto_compact_token_limit':448000},
                             apply_root_preferences=False)
        self.assertEqual(result,catalog)
        fresh=adapt_catalog(catalog,{}, {'model_context_window':512000,'model_auto_compact_token_limit':448000})
        self.assertEqual(fresh['models'][0]['context_window'],512000)


class NativeStagePreferencesTest(unittest.TestCase):
    def fixture(self,root):
        import patch as p
        from tests.test_model_sync import NativeModelSyncTests
        baseline,home,state,native,record=NativeModelSyncTests().native_fixture(root)
        profile=json.loads((baseline/'adapter-profile.json').read_text(encoding='utf-8'))
        profile.update(token='fixture-token-'*4,port=12345,
                       gpt={'chatgpt_base_url':'https://chatgpt.com/backend-api/codex','api_base_url':'https://api.openai.com/v1'})
        p.save_json(baseline/'adapter-profile.json',profile)
        record.update(profile_sha256=p.file_sha(baseline/'adapter-profile.json'),keychain_account='fixture',keychain_service='fixture')
        p.save_json(baseline/'manifest.json',record)
        app=root/'official.app';app.mkdir();(app/'app.asar').write_text('official')
        layout={'app':app,'asar':app/'app.asar','version':'1','build':'1'}
        return baseline,home,state,native,record,app,layout

    def test_all_hidden_external_models_stage_valid_gpt_only_profile(self):
        import patch as p
        import model_sync
        import official_client
        from inference_adapter import load_profile
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();baseline,home,state,native,record,app,layout=self.fixture(root)
            p.save_json(state/'model-preferences.json',{'hidden':['deepseek-flash']})
            snapshot=model_sync.synchronize({'edition':'ds','app':'unused','codex_home':str(home)},state,baseline,
                fetch_native=lambda *_:native,fetch_provider=lambda *_:{'deepseek-flash':{'context_window':1048576}})
            with mock('official_client.compression_node',return_value=None), \
                 mock('official_client_update.verify_official',return_value={}),mock.object(p,'app_layout',return_value=layout):
                result=official_client.stage(app=app,codex_home=home,output=root/'next',baseline=baseline,
                                             edition='ds',model_snapshot=snapshot)
            self.assertEqual(result['model_routes'],{})
            self.assertEqual(load_profile(root/'next/adapter-profile.json')['routes'],{})
            self.assertEqual([row['slug'] for row in json.loads((root/'next/desktop-models.json').read_text(encoding='utf-8'))['models']],['gpt-6.1-sol'])

    def test_existing_stage_preserves_live_gpt_model_reduction(self):
        import patch as p
        import official_client
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();baseline,home,state,native,record,app,layout=self.fixture(root)
            path=baseline/'desktop-models.json';catalog=json.loads(path.read_text(encoding='utf-8'))
            next(row for row in catalog['models'] if row['slug']=='gpt-6.1-sol').update(context_window=128000,auto_compact_token_limit=100000)
            p.save_json(path,catalog)
            with mock('official_client.compression_node',return_value=None), \
                 mock('official_client_host.prepare',return_value={'format':1,'models':['deepseek-flash']}), \
                 mock('official_client_update.verify_official',return_value={}),mock.object(p,'app_layout',return_value=layout):
                official_client.stage(app=app,codex_home=home,output=root/'next',baseline=baseline,edition='ds')
            gpt=next(row for row in json.loads((root/'next/desktop-models.json').read_text(encoding='utf-8'))['models'] if row['slug']=='gpt-6.1-sol')
            self.assertEqual((gpt['context_window'],gpt['auto_compact_token_limit']),(128000,100000))

    def test_fresh_initial_setup_still_requires_a_configured_service(self):
        import patch as p
        import official_client
        args=argparse.Namespace(deepseek_enabled=False,keychain_account='fixture',keychain_service='fixture')
        with mock.object(p,'EDITION','proxy'),mock.object(p,'cli_path',return_value=Path('/official/cli')), \
             mock.object(official_client.subprocess,'check_output',return_value=json.dumps({'models':[{'slug':'gpt-fixture'}]}).encode()):
            with self.assertRaisesRegex(p.PatchError,'Configure at least one'):
                official_client.initial_models(Path('/official'),args)


if __name__ == "__main__":
    unittest.main()
