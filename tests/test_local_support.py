import copy
import http.server
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch as mock

import patch as p
import local_support as local
import local_bridge


class LocalTests(unittest.TestCase):
    def test_loopback_listener_never_waits_for_reverse_dns(self):
        with mock.object(socket,'getfqdn',side_effect=RuntimeError('A local resolver must not be consulted')):
            server=local_bridge.Server(('127.0.0.1',0),local_bridge.Handler)
            try:
                self.assertEqual(server.server_name,'127.0.0.1')
                self.assertGreater(server.server_port,0)
            finally:server.server_close()

    def test_native_listener_starts_when_auth_helper_cannot_write_any_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);profile=root/'readonly-bridge.json'
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1]
            value={'base_url':'http://127.0.0.1:9/v1','port':port,'token':'synthetic-readonly-probe'}
            profile.write_text(json.dumps(value),encoding='utf-8')
            original_open=Path.open;original_popen=subprocess.Popen;owned=[]
            def readonly_open(path,mode='r',*args,**kwargs):
                if any(flag in mode for flag in 'wax+'):
                    raise PermissionError('Desktop credential helper cannot write beside its profile')
                return original_open(path,mode,*args,**kwargs)
            def start(*args,**kwargs):
                command=list(args[0])
                diagnostic='import faulthandler,runpy,sys; faulthandler.dump_traceback_later(10); sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name="__main__")'
                command=[command[0],'-c',diagnostic,*command[1:]]
                kwargs['stderr']=subprocess.PIPE
                process=original_popen(command,**kwargs);owned.append(process);return process
            try:
                with mock.object(Path,'open',readonly_open), mock.object(local_bridge.subprocess,'Popen',start), mock('builtins.print') as output:
                    local_bridge.credential(profile)
                output.assert_called_once_with(value['token'],end='')
                self.assertTrue(local_bridge.healthy(value))
                self.assertFalse(profile.with_suffix('.log').exists())
                self.assertEqual(len(owned),1)
            except RuntimeError as error:
                diagnostics=[]
                for process in owned:
                    if process.poll() is None:process.terminate()
                    diagnostics.append(process.communicate(timeout=10)[1].decode('utf-8',errors='replace'))
                self.fail(str(error)+'\n'+''.join(diagnostics))
            finally:
                for process in owned:
                    if process.poll() is None:process.terminate()
                    process.communicate(timeout=10)

    def test_only_idle_compatibility_listener_needs_per_request_auth_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);connection={**self.connection(),'key_file':str(root/'key'),'tool_adapter_models':['probe']}
            normal=p.tomllib.loads(local.provider_config(connection,root/'local_support.py'))['model_providers']['local-test']
            adapted=p.tomllib.loads(local.provider_config(connection,root/'local_support.py',True))['model_providers']['local-test-compat']
            self.assertEqual(normal['auth']['refresh_interval_ms'],0)
            self.assertEqual(adapted['auth']['refresh_interval_ms'],1)

    def connection(self):
        return {'id':'local-test','name':'Test','kind':'ollama','base_url':'http://127.0.0.1:11434/v1','models':[]}

    def test_connection_change_triggers_new_snapshot_even_with_same_model_specs(self):
        import model_sync as ms
        value={'catalog':{'models':[]},'routes':{},'locals':[self.connection()],'deepseek_enabled':False}
        before=ms.snapshot_digest(value)
        value['locals'][0]['base_url']='http://127.0.0.1:21434/v1'
        self.assertNotEqual(before,ms.snapshot_digest(value))

    def test_server_reduction_survives_unload_and_offline_sync(self):
        import model_sync as ms
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);home=root/'home';baseline=root/'build';state=root/'state'
            for path in [home,baseline,state]:path.mkdir()
            native={'models':[{'slug':'gpt-6.1-sol','context_window':1000000,'max_context_window':1000000}]}
            p.save_json(baseline/'desktop-models.json',native)
            (home/'config.toml').write_text(p.prepared_config('',baseline/'desktop-models.json','unused','unused',deepseek=False),encoding='utf-8')
            p.save_json(baseline/'manifest.json',{'deepseek_enabled':False,'locals':[]})
            p.save_json(state/'connections.json',{'locals':[{**self.connection(),'respect_server_context':True}]})
            settings={'edition':'proxy','app':'unused','codex_home':str(home)}
            spec={'context_window':262144,'configured_context_window':32768,'supports_tools':True}
            with mock.object(local,'discover',return_value=({'qwen':spec},[])):
                first=ms.synchronize(settings,state,baseline,fetch_native=lambda *_:native)
            self.assertEqual(first['routes']['qwen']['context_window'],32768)
            spec={**spec,'configured_context_window':None}
            with mock.object(local,'discover',return_value=({'qwen':spec},[])):
                second=ms.synchronize(settings,state,baseline,fetch_native=lambda *_:native)
            self.assertEqual(second['routes']['qwen']['context_window'],32768)
            with mock.object(local,'discover',side_effect=OSError('offline')):
                third=ms.synchronize(settings,state,baseline,fetch_native=lambda *_:native)
            self.assertEqual(third['routes']['qwen'],second['routes']['qwen'])
            self.assertTrue(third['warnings'])

    def test_loaded_server_window_is_retained_only_when_requested(self):
        def fetch(_,path,body=None):
            if path=='/models':return {'data':[{'id':'qwen'}]}
            if path=='@/api/ps':return {'models':[{'name':'qwen','context_length':32768}]}
            return {'capabilities':['completion','tools','thinking'],'model_info':{'qwen.context_length':262144},'parameters':''}
        plain,_=local.discover(self.connection(),fetch)
        selected,_=local.discover({**self.connection(),'respect_server_context':True},fetch)
        self.assertIsNone(plain['qwen']['configured_context_window'])
        self.assertEqual(selected['qwen']['configured_context_window'],32768)
        self.assertEqual(selected['qwen']['context_window'],262144)

    def test_local_install_and_refresh_need_no_ds_provider_or_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);connection=self.connection()
            text=p.prepared_config('model = "gpt-6.1-sol"\n',root/'models.json','unused','unused',deepseek=False,locals=[connection])
            value=p.tomllib.loads(text)
            self.assertNotIn(p.PROVIDER,value['model_providers'])
            self.assertEqual(value['model_providers']['local-test']['base_url'],connection['base_url'])
            self.assertNotIn('auth',value['model_providers']['local-test'])
            managed={'catalog':str(root/'models.json'),'provider':None,'deepseek_enabled':False,'local_providers':value['model_providers']}
            refreshed=p.prepared_config(text,root/'next.json','unused','unused',managed=managed,deepseek=False,locals=[connection])
            self.assertNotIn(p.PROVIDER,p.tomllib.loads(refreshed)['model_providers'])
            self.assertEqual(p.tomllib.loads(refreshed)['model'],'gpt-6.1-sol')

    def test_ollama_maximum_capabilities_and_embedding_exclusion(self):
        def fetch(_,path,body=None):
            if path=='/models':return {'data':[{'id':'chat'},{'id':'agent'},{'id':'embedding'}]}
            name=body['model']
            return {'capabilities':['embedding'] if name=='embedding' else ['completion'] if name=='chat' else ['completion','tools','thinking'],
                    'model_info':{'qwen.context_length':262144},'parameters':'num_ctx 32768'}
        specs,warnings=local.discover(self.connection(),fetch)
        self.assertEqual(set(specs),{'chat','agent'})
        self.assertFalse(specs['chat']['supports_tools'])
        self.assertTrue(specs['agent']['supports_tools'])
        self.assertEqual(specs['agent']['context_window'],262144)
        self.assertEqual(specs['agent']['configured_context_window'],32768)
        self.assertFalse(warnings)

    def test_compatible_endpoint_missing_specs_are_explicit_and_operator_can_supply_them(self):
        connection={**self.connection(),'kind':'openai'}
        fetch=lambda *_:{'data':[{'id':'new-model'}]}
        specs,warnings=local.discover(connection,fetch)
        self.assertEqual(specs,{})
        self.assertTrue(warnings)
        connection['model_specs']={'new-model':{'context_window':1000000,'reasoning_levels':['low','high']}}
        specs,warnings=local.discover(connection,fetch)
        self.assertEqual(specs['new-model']['context_window'],1000000)

    def test_direct_and_chat_only_models_use_separate_transports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);connection={**self.connection(),'chat_only_models':['chat']}
            direct=p.tomllib.loads(local.provider_config(connection,root/'local_support.py'))['model_providers']['local-test']
            chat=p.tomllib.loads(local.provider_config(connection,root/'local_support.py',True))['model_providers']['local-test-compat']
            self.assertEqual(direct['base_url'],connection['base_url'])
            self.assertNotIn('auth',direct)
            self.assertIn('local_bridge.py',chat['auth']['args'][0])
            self.assertNotEqual(chat['base_url'],direct['base_url'])

    def test_chat_adapter_authenticates_and_strips_only_unsupported_model_tools(self):
        seen=[]
        class Upstream(http.server.BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_POST(self):
                seen.append((json.loads(self.rfile.read(int(self.headers['Content-Length']))),self.headers.get('Authorization')))
                raw=b'{"status":"completed"}';self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        upstream=http.server.ThreadingHTTPServer(('127.0.0.1',0),Upstream)
        bridge=local_bridge.Server(('127.0.0.1',0),local_bridge.Handler)
        bridge.profile={'base_url':f'http://127.0.0.1:{upstream.server_port}/v1','chat_only_models':['chat'],
                        'normalize_instructions_models':['agent'],'token':'test-only-token'}
        bridge.active=0;bridge.last_activity=0
        for server in [upstream,bridge]:threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            url=f'http://127.0.0.1:{bridge.server_port}/v1/responses'
            # An empty unauthorized POST tests authentication before body validation
            # without an unread body racing Windows TCP connection teardown.
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(urllib.request.Request(url,data=b''),timeout=2)
            self.assertEqual(error.exception.code,401)
            tools=[{'type':'function','name':'test'}]
            for model in ['chat','agent']:
                request=urllib.request.Request(url,data=json.dumps({'model':model,'instructions':'original',
                    'input':[{'role':'user','content':'safe test'},{'role':'developer','content':[{'type':'input_text','text':'retained instructions'}]}],'tools':tools}).encode(),
                    headers={'Authorization':'Bearer test-only-token','Content-Type':'application/json'})
                with urllib.request.urlopen(request,timeout=3) as response:self.assertEqual(json.load(response)['status'],'completed')
            self.assertNotIn('tools',seen[0][0])
            self.assertEqual(seen[1][0]['tools'],tools)
            self.assertEqual(seen[1][0]['instructions'],'original\n\nretained instructions')
            self.assertEqual(seen[1][0]['input'],[{'role':'user','content':'safe test'}])
            self.assertTrue(all(auth is None for _,auth in seen))
        finally:
            for server in [bridge,upstream]:server.shutdown();server.server_close()


if __name__=='__main__':unittest.main()


class SpecificationSourceTests(unittest.TestCase):
    def test_user_service_and_inferred_metadata_are_distinguished_without_policy_changes(self):
        connection={'id':'local-probe','kind':'openai','base_url':'http://127.0.0.1:1234/v1',
                    'models':['m'],'model_specs':{'m':{'context_window':65536,'supports_tools':False}}}
        rows=[{'id':'m','context_window':32768,'input_modalities':['text','image']}]
        result,warnings=local.discover(connection,fetch=lambda *a:{'data':rows})
        self.assertFalse(warnings);model=result['m'];self.assertEqual(model['context_window'],65536)
        self.assertEqual(model['reported_context_window'],32768);self.assertTrue(model['context_conflict'])
        self.assertEqual(model['spec_sources'],{'context_window':'user','max_output_tokens':'inferred','supports_tools':'user','input_modalities':'service','reasoning_levels':'inferred'})
        connection['model_specs']={}
        result,_=local.discover(connection,fetch=lambda *a:{'data':rows})
        self.assertEqual(result['m']['context_window'],32768);self.assertFalse(result['m']['context_conflict'])


class AnthropicDiscoveryTests(__import__('unittest').TestCase):
    def test_anthropic_pagination_and_declared_maximum_effort_are_preserved(self):
        import local_support as local
        connection={'id':'local-messages','kind':'openai','base_url':'https://example.invalid/v1','wire_api':'anthropic_messages'}
        row={'id':'unknown-name','max_input_tokens':1000000,'max_tokens':128000,'capabilities':{
            'effort':{'supported':True,'low':{'supported':True},'max':{'supported':True}},
            'thinking':{'types':{'adaptive':{'supported':True}}},'image_input':{'supported':True}}}
        paths=[]
        def fetch(c,path):
            paths.append(path)
            return {'data':[row],'has_more':True,'last_id':'a/b'} if path=='/models' else {'data':[{**row,'id':'second'}],'has_more':False}
        specs,warnings=local.discover(connection,fetch)
        self.assertFalse(warnings);self.assertEqual(set(specs),{'unknown-name','second'})
        self.assertEqual(paths,['/models','/models?after_id=a%2Fb'])
        self.assertEqual(specs['unknown-name']['context_window'],1000000)
        self.assertEqual(specs['unknown-name']['max_output_tokens'],128000)
        self.assertEqual(specs['unknown-name']['reasoning_levels'],['low','max'])
        self.assertEqual(specs['unknown-name']['thinking_mode'],'adaptive')
        with self.assertRaisesRegex(ValueError,'cursor'):local.discover(connection,lambda c,path:{'data':[],'has_more':True,'last_id':'repeated'})
