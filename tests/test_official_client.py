import argparse
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import official_client as native
import patch as p
import update_worker


class NativeStagingTests(unittest.TestCase):
    def test_manual_unified_upgrade_keeps_baseline_service_affinity(self):
        for affinity in ('ds','proxy'):
            with self.subTest(affinity=affinity),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);baseline=root/'old';baseline.mkdir()
                (baseline/'manifest.json').write_text(json.dumps({'edition':affinity}))
                args=argparse.Namespace(app=root/'app',baseline=baseline,codex_home=root/'home',output=root/'new')
                with patch.object(p,'EDITION','unified'),patch.object(native,'stage',return_value='prepared') as stage,patch('builtins.print'):
                    self.assertEqual(native.build(args),'prepared')
                self.assertEqual(stage.call_args.kwargs['edition'],affinity)
                self.assertEqual(stage.call_args.kwargs['baseline'],baseline)

    def test_fresh_generic_service_stages_without_app_or_configuration_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);app=root/'official';home=root/'home';output=root/'prepared'
            app.mkdir();home.mkdir();asar=app/'app.asar';asar.write_bytes(b'official-fixture')
            config='model="gpt-fixture"\n[mcp_servers.personal]\ncommand="untouched"\n'
            (home/'config.toml').write_text(config,encoding='utf-8')
            connection={'id':'local-any','base_url':'http://127.0.0.1:1234/v1','wire_api':'chat_completions',
                        'supports_reasoning_effort':True,'tool_adapter_models':['glm-fixture']}
            routes={'glm-fixture':{'provider':'local-any','context_window':32768,'max_context_window':202752,'compact_limit':29491}}
            catalog={'models':[{'slug':'gpt-fixture','context_window':1000000,'max_context_window':1000000},
                               {'slug':'glm-fixture','context_window':32768,'max_context_window':202752}]}
            initial={'manifest':{'architecture':'official-original','edition':'proxy','target':str(app),
                                'locals':[connection],'model_routes':routes,'deepseek_enabled':False},'catalog':catalog}
            with patch.object(native,'compression_node',return_value=None), \
                 patch('official_client_host.prepare',return_value={'format':1,'models':['glm-fixture']}), \
                 patch('official_client_update.verify_official',return_value={'verified':True}), \
                 patch.object(p,'app_layout',return_value={'asar':asar,'version':'fixture','build':'1'}):
                manifest=native.stage(app=app,codex_home=home,output=output,baseline=None,initial=initial)
            self.assertEqual(asar.read_bytes(),b'official-fixture')
            self.assertEqual((home/'config.toml').read_text(),config)
            self.assertEqual([x.name for x in app.iterdir()],['app.asar'])
            self.assertFalse(any(x.name.endswith('.app') for x in output.iterdir()))
            self.assertFalse(manifest['deepseek_enabled'])
            profile=json.loads((output/'adapter-profile.json').read_text())
            self.assertEqual(profile['routes']['glm-fixture']['wire_api'],'chat_completions')
            self.assertTrue(profile['routes']['glm-fixture']['supports_reasoning_effort'])
            self.assertNotIn('deepseek-flash',profile['routes'])
            self.assertIsNone(manifest['legacy_baseline'])
            self.assertEqual(manifest['initial_manifest']['architecture'],'official-original')
            # Runtime validation must test exactly the bytes that will be installed.
            (output/'inference_adapter.py').write_text('changed after stage',encoding='utf-8')
            with patch('scripts.test_official_client.run') as probe, self.assertRaisesRegex(p.PatchError,'differs'):
                update_worker.verify_build(output)
            probe.assert_not_called()
            self.assertNotIn('native_probe',json.loads((output/'manifest.json').read_text()))

    def test_unknown_wire_api_is_rejected_at_connection_boundary(self):
        import local_support
        value={'id':'local-test','kind':'openai','base_url':'https://example.invalid/v1','wire_api':'unrecognized_protocol'}
        with self.assertRaisesRegex(ValueError,'protocol'):local_support.validate(value)
        value['wire_api']='chat_completions';value['supports_reasoning_effort']='yes'
        with self.assertRaisesRegex(ValueError,'capability'):local_support.validate(value)

    def test_entrypoint_uses_native_build(self):
        args=argparse.Namespace()
        with patch.object(native,'build',return_value='staged') as build:
            self.assertEqual(p.build(args),'staged')
            build.assert_called_once_with(args)


class SyntheticToolEvidenceTests(unittest.TestCase):
    def test_denied_command_quoting_marker_is_not_execution_success(self):
        from scripts.test_official_client import require_tool_success
        for fields in ({'status':'failed','exitCode':None}, {'status':'completed','exitCode':1},
                       {'status':'completed','exitCode':None}, {'status':'completed','exitCode':False}):
            item = {'type':'commandExecution','aggregatedOutput':'echo INTEGER_TOOL_OK rejected: blocked by policy',**fields}
            with self.subTest(fields=fields), self.assertRaises(p.PatchError):
                require_tool_success([{'method':'item/completed','params':{'item':item}}],
                                     'commandExecution','INTEGER_TOOL_OK')
        good = {'type':'commandExecution','status':'completed','exitCode':0,'aggregatedOutput':'INTEGER_TOOL_OK\n'}
        events = [{'method':'item/completed','params':{'item':good}}]
        self.assertEqual(require_tool_success(events,'commandExecution','INTEGER_TOOL_OK'),good)
        good['aggregatedOutput'] = 'echo INTEGER_TOOL_OK rejected: blocked by policy'
        with self.assertRaises(p.PatchError):
            require_tool_success(events,'commandExecution','INTEGER_TOOL_OK')

    def test_patch_must_be_a_completed_native_file_change(self):
        from scripts.test_official_client import require_tool_success
        for method, status in (('item/completed','failed'),('item/started','completed')):
            with self.subTest(method=method,status=status), self.assertRaises(p.PatchError):
                require_tool_success([{'method':method,'params':{'item':{'type':'fileChange','status':status}}}], 'fileChange')
        item = {'type':'fileChange','status':'completed'}
        self.assertEqual(require_tool_success([{'method':'item/completed','params':{'item':item}}], 'fileChange'), item)


if __name__=='__main__':unittest.main()
