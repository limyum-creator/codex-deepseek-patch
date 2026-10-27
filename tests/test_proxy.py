from windows_support import background_options
import copy
import json
from pathlib import Path
import tempfile
import unittest
import subprocess

import backend
import patch
import proxy_support as ps
from scripts import test_offline
from test_offline import FakeRpc


class ProxyTests(unittest.TestCase):
    def test_idle_listener_auth_is_rechecked_for_subsequent_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            provider=patch.tomllib.loads(ps.provider_config(self.settings(root),root/'proxy_support.py'))['model_providers'][ps.PROVIDER]
            self.assertEqual(provider['auth']['refresh_interval_ms'],1)

    def settings(self, root):
        return {"config": str(root / "proxy.yaml"), "base_url": "http://127.0.0.1:8317/v1", "models": list(ps.MODELS)}

    def test_reads_only_client_credentials_in_old_and_new_generated_config(self):
        fixtures = [
            'api-keys:\n- sample-key\nmanagement:\n  secret-key: other\n',
            'access:\n  api-keys:\n    - "sample-key" # selected\noauth:\n  auth-dir: ../oauth\n',
            'access:\n  api-keys: ["sample-key", "second-key"]\n',
            "'access':\n  'api-keys':\n    - 'sample-key'\n",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.yaml'
            for value in fixtures:
                path.write_text(value)
                self.assertEqual(ps.read_api_key(path), 'sample-key')

    def test_rejects_unsupported_yaml_and_never_falls_back_to_oauth_or_management_key(self):
        fixtures = ['management:\n  secret-key: management-value\n',
                    'oauth:\n  api-keys: ["oauth-value"]\n',
                    'access:\n  api-keys:\n    - &alias secret-value\n',
                    'api-keys: ["bad\\nvalue"]\n',
                    'api-keys:\n  - ""\n', 'api-keys: []\n']
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.yaml'
            for text in fixtures:
                path.write_text(text)
                with self.assertRaises(ValueError) as caught:
                    ps.read_api_key(path)
                self.assertNotIn('secret-value', str(caught.exception))

    def test_catalog_preserves_gpt_and_deepseek_and_adds_only_selected_models(self):
        original = {"models": [{"slug":"gpt-6.1-sol", "model_messages":{"instructions_template":"fixture"}},
                                {"slug":patch.MODEL, "extra":"preserve"}]}
        saved = copy.deepcopy(original)
        result = ps.add_catalog(original, ['grok-4.7', 'gemini-3.8-flash-high'])
        self.assertEqual(original, saved)
        self.assertEqual(result['models'][:2], original['models'])
        self.assertEqual([m['slug'] for m in result['models'][2:]], ['grok-4.7', 'gemini-3.8-flash-high'])
        self.assertFalse(result['models'][2]['use_responses_lite'])
        self.assertEqual(result['models'][2]['service_tiers'], [])

    def test_settings_reject_secrets_in_urls_and_unknown_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = self.settings(Path(tmp))
            for field, value in [('base_url','http://secret@127.0.0.1/v1'), ('base_url','http://server/v1'),
                                 ('base_url','https://server/v1?key=secret'), ('models',['gemini-4']),
                                 ('models',['grok-4.5','grok-4.5']), ('config','relative.yaml')]:
                with self.assertRaises(ValueError):
                    ps.validate({**original,field:value})

    def test_context_defaults_match_provider_maxima_and_actual_javascript_route(self):
        expected = {'grok-4.5':500000, 'grok-4.7':500000, 'gemini-3.8-flash-high':1048576, 'claude-opus-4-6-thinking':1000000}
        catalog = ps.add_catalog({'models':[{'slug':'gpt-6.1-sol','effective_context_window_percent':95}]},list(ps.MODELS))
        script = ("const fs=require('fs'),vm=require('vm');const c=vm.createContext({});"
                  "vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),c);"
                  "const models=JSON.parse(process.argv[2]);"
                  "console.log(JSON.stringify(Object.fromEntries(models.map(model=>[model,c.codexPatchAppServerParams('thread/start',{model},'local').config]))));")
        routed = json.loads(subprocess.check_output(['node','-e',script,str(patch.ROOT/'route.js'),json.dumps(list(ps.MODELS))],text=True,**background_options()))
        for model in catalog['models'][1:]:
            slug=model['slug']
            self.assertEqual(model['context_window'],expected[slug])
            self.assertEqual(model['max_context_window'],expected[slug])
            self.assertEqual(model['effective_context_window_percent'],100)
            self.assertEqual(routed[slug]['model_context_window'],expected[slug])
            self.assertEqual(routed[slug]['model_auto_compact_token_limit'],int(expected[slug]*0.9))

    def test_opt_in_provider_and_managed_refresh_keep_user_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); settings = self.settings(root)
            current = '# preserved\nmodel="gpt-user"\n[desktop]\nkeep=true\n'
            old = root/'old'; old.mkdir()
            catalog = old/'desktop-models.json'; catalog.write_text('{}')
            ds_only = patch.prepared_config(current,catalog,'account','service')
            (old/'config-installed.toml').write_text(ds_only)
            (old/'manifest.json').write_text(json.dumps({'format':1}))
            managed = backend.managed_baseline(old,'account','service')
            new = root/'new';new.mkdir();new_catalog = new/'desktop-models.json';new_catalog.write_text('{}')
            updated = patch.prepared_config(ds_only,new_catalog,'account','service',managed=managed,proxy=settings)
            parsed = patch.tomllib.loads(updated)
            self.assertEqual(parsed['model'],'gpt-user')
            self.assertEqual(parsed['desktop'],{'keep':True})
            self.assertEqual(parsed['model_providers'][patch.PROVIDER],patch.tomllib.loads(ds_only)['model_providers'][patch.PROVIDER])
            self.assertEqual(parsed['model_providers'][ps.PROVIDER]['auth']['args'][-1],settings['config'])
            self.assertNotIn('api_key',parsed['model_providers'][ps.PROVIDER])
            (new/'config-installed.toml').write_text(updated)
            (new/'manifest.json').write_text(json.dumps({'format':1,'proxy':settings}))
            next_managed = backend.managed_baseline(new,'account','service')
            refreshed = patch.prepared_config(updated+'\n[user]\nkeep=12\n',root/'future.json','account','service',managed=next_managed,proxy=settings)
            refreshed_providers=patch.tomllib.loads(refreshed)['model_providers']
            self.assertEqual(refreshed_providers[patch.PROVIDER],parsed['model_providers'][patch.PROVIDER])
            self.assertEqual(refreshed_providers[ps.PROVIDER],patch.tomllib.loads(ps.provider_config(settings,root/'proxy_support.py'))['model_providers'][ps.PROVIDER])
            self.assertEqual(patch.tomllib.loads(refreshed)['user'],{'keep':12})
            with self.assertRaises(patch.PatchError):
                backend.refresh_config(updated.replace(parsed['model_providers'][ps.PROVIDER]['base_url'],'http://127.0.0.1:9999/v1'),root/'future.json',next_managed)
            selection = patch.prepared_config(updated,root/'future.json','account','service',managed=next_managed,proxy={**settings,'models':['grok-4.5']})
            self.assertEqual(patch.tomllib.loads(selection)['model_providers'],refreshed_providers)

    def test_unmanaged_same_named_provider_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = '[model_providers.easycli-desktop]\nname="existing"\n'
            with self.assertRaises(patch.PatchError):
                patch.prepared_config(current,root/'catalog.json','a','s',proxy=self.settings(root))

    def test_offline_all_proxy_threads_use_dummy_credentials_and_no_inference(self):
        rpc = FakeRpc()
        rpc.results[1]['data'].extend({'model':model} for model in ps.MODELS)
        rpc.results.extend({'model':model,'modelProvider':ps.PROVIDER,'thread':{'id':'probe-'+model}} for model in ps.MODELS)
        result = test_offline.check_session(rpc,Path('/tmp/work'),list(ps.MODELS))
        self.assertEqual(len(result['proxy_models']),4)
        self.assertEqual(result['inference_turns'],0)
        self.assertNotIn('turn/start',rpc.methods)
        providers = patch.tomllib.loads(test_offline.offline_config('/tmp/catalog.json',list(ps.MODELS)))['model_providers']
        for provider in providers.values():
            self.assertNotIn('auth',provider)
            self.assertEqual(provider['env_key'],test_offline.DUMMY_KEY_VARIABLE)
            self.assertEqual(provider['base_url'],test_offline.LOOPBACK_BASE_URL)


if __name__ == '__main__':
    unittest.main()
