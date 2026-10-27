"""Real CLI regression for saved-chat provider switching and post-compaction windows."""
from pathlib import Path
import http.server
import io
import json
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import patch
from scripts.test_context import ResponsesStub,Rpc
from scripts.test_offline import isolated_environment,launch_options,stop_probe, probe_command

class Stub(ResponsesStub):
    def do_POST(self):
        raw=self.rfile.read(int(self.headers['Content-Length']));body=json.loads(raw);self.rfile=io.BytesIO(raw)
        self.server.payloads.append(body)
        self.server.usage_tokens=self.server.next_usage;self.server.next_usage=10
        super().do_POST()

def finish(rpc,tid,turn):
    values=[];deadline=time.monotonic()+60
    while time.monotonic()<deadline:
        event=rpc.pending.pop(0) if rpc.pending else rpc.receive_event(60)
        if event.get('params',{}).get('threadId')!=tid:continue
        values.append(event)
        if event.get('method')=='turn/completed' and event['params']['turn']['id']==turn['turn']['id']:
            patch.require(event['params']['turn']['status']=='completed','Switching turn failed: '+str(event['params']['turn'].get('error',{})))
            return values
    raise TimeoutError('Switching turn did not complete')

def main(directory):
    directory,manifest=patch.load_build(Path(directory))
    routes=manifest.get('model_routes',{})
    candidates=[m for m in routes if routes[m].get('context_window',0)>0]
    patch.require(candidates,'At least one managed model is required for switching verification')
    first=next((m for m in candidates if routes[m]['provider']=='easycli-desktop'),candidates[0])
    second=next((m for m in candidates if routes[m]['provider']!=routes[first]['provider']),None)
    app=directory/'patched.app' if manifest['status']=='prepared' else Path(manifest.get('deployment_target',manifest['target']))
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Stub);server.daemon_threads=True
    server.requests=[];server.payloads=[];server.next_usage=10
    threading.Thread(target=server.serve_forever,daemon=True).start()
    report={'status':'failed','external_inference_turns':0,'checks':[]}
    try:
        with tempfile.TemporaryDirectory(prefix='codex-switching-') as tmp:
            base=Path(tmp);env=isolated_environment(base);env['SWITCH_KEY']='placeholder';env['OPENAI_API_KEY']='placeholder';env['OPENAI_BASE_URL']=f'http://127.0.0.1:{server.server_port}/openai';work=base/'work';work.mkdir()
            # Keep real catalog/routing defaults; disable native-only wire features for the fake OpenAI endpoint.
            catalog=json.loads((directory/'desktop-models.json').read_text(encoding='utf-8'))
            native=next(m['slug'] for m in catalog['models'] if m['slug'].startswith('gpt-'))
            for entry in catalog['models']:
                entry.update(use_responses_lite=False,prefer_websockets=False,multi_agent_version=None)
            patch.save_json(base/'models.json',catalog)
            config='openai_base_url='+json.dumps(f'http://127.0.0.1:{server.server_port}/openai')+'\nmodel_catalog_json='+json.dumps(str(base/'models.json'))+'\nmodel_context_window=128000\nmodel_auto_compact_token_limit=90000\n'
            for provider in {r['provider'] for r in routes.values()}:
                config+=f'[model_providers.{provider}]\nname="test"\nwire_api="responses"\nbase_url="http://127.0.0.1:{server.server_port}/{provider}"\nenv_key="SWITCH_KEY"\nrequires_openai_auth=false\nrequest_max_retries=0\nstream_max_retries=0\n'
            (base/'home/config.toml').write_text(config,encoding='utf-8')
            patch.save_json(base/'home/auth.json',{'OPENAI_API_KEY':'placeholder'})
            with (directory/'switching-probe.log').open('w',encoding='utf-8') as log:
                process=subprocess.Popen(probe_command(app),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=log,
                    text=True,encoding='utf-8',env=env,cwd=work,**launch_options())
                driver=subprocess.Popen(['node',str(patch.ROOT/'scripts/route_driver.cjs'),str(directory/'compatibility.json')],
                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=log,text=True,encoding='utf-8',env=env,**launch_options())
                try:
                    rpc=Rpc(process);rpc.request('initialize',{'clientInfo':{'name':'switching_probe','version':'1'},'capabilities':{'experimentalApi':True}})
                    fresh=rpc.request('thread/start',{'model':first,'modelProvider':routes[first]['provider'],'cwd':str(work),'approvalPolicy':'never','sandbox':'read-only',
                        'baseInstructions':'You are a synthetic local test.','config':{'web_search':'disabled','features.memories':False,
                        'model_context_window':routes[first]['context_window'],'model_auto_compact_token_limit':routes[first]['compact_limit']}})
                    initial=rpc.request('thread/start',{'model':first,'modelProvider':routes[first]['provider'],'cwd':str(work),'approvalPolicy':'never','sandbox':'read-only',
                        'baseInstructions':'You are a synthetic local test.','config':{'web_search':'disabled','features.memories':False,'model_context_window':128000,'model_auto_compact_token_limit':90000}})
                    tid=initial['thread']['id'];rpc.turn(tid)
                    def turn(model,chat_id=None):
                        chat_id=chat_id or tid
                        driver.stdin.write(json.dumps({'method':'turn/start','params':{'threadId':chat_id,'model':model,'input':[{'type':'text','text':'Reply CONTEXT_OK.'}]}})+'\n');driver.stdin.flush()
                        while True:
                            line=driver.stdout.readline()
                            patch.require(line,'Patched request driver stopped')
                            value=json.loads(line)
                            if value.get('done'):
                                patch.require(not value.get('error'),str(value.get('error')))
                                return finish(rpc,chat_id,value['result'])
                            if chat_id==fresh['thread']['id']:
                                patch.require(value['method'] != 'thread/unsubscribe','First turn must retain its newly created session')
                            try:
                                result=rpc.request(value['method'],value['params'])
                                if value['method']=='thread/resume':
                                    patch.require(result['approvalPolicy']==initial['approvalPolicy'] and result['sandbox']==initial['sandbox'],
                                                  'Model switching changed chat permissions')
                                    patch.require(result['cwd']==initial['cwd'],'Model switching changed the working directory')
                                reply={'rpc':value['rpc'],'result':result}
                            except Exception as error:reply={'rpc':value['rpc'],'error':str(error)}
                            driver.stdin.write(json.dumps(reply)+'\n');driver.stdin.flush()
                    events=turn(first,fresh['thread']['id'])
                    windows=[e['params']['tokenUsage']['modelContextWindow'] for e in events if e.get('method')=='thread/tokenUsage/updated']
                    patch.require(windows and set(windows)=={routes[first]['context_window']},'First turn lost its model context')
                    report['checks'].append({'new_chat_first_turn':'passed','context_window':windows[-1]})
                    events=turn(first)
                    windows=[e['params']['tokenUsage']['modelContextWindow'] for e in events if e.get('method')=='thread/tokenUsage/updated']
                    patch.require(windows[-1]==routes[first]['context_window'],'Saved 128K window was not refreshed')
                    report['checks'].append({'legacy_window_refreshed_to':windows[-1]})
                    server.next_usage=routes[first]['compact_limit']+100
                    turn(first)
                    events=turn(first)
                    patch.require(any(e.get('params',{}).get('item',{}).get('type')=='contextCompaction' for e in events),'Compaction was not exercised')
                    windows=[e['params']['tokenUsage']['modelContextWindow'] for e in events if e.get('method')=='thread/tokenUsage/updated']
                    patch.require(windows and set(windows)=={routes[first]['context_window']},'Window changed after compaction')
                    report['checks'].append({'after_compaction':windows[-1]})
                    same_provider=next((m for m in candidates if routes[m]['provider']==routes[first]['provider'] and routes[m]['context_window']!=routes[first]['context_window']),None)
                    switches=list(dict.fromkeys(([second] if second else [])+([same_provider] if same_provider else [])+
                        [m for m in candidates if routes[m]['provider'].startswith('local-')]+[native,first]))
                    for model in switches:
                        events=turn(model);expected=routes[model]['provider'] if model in routes else 'openai'
                        patch.require(server.requests[-1]['model']==model and server.requests[-1]['path'].startswith('/'+expected+'/'),'Wrong provider after switching')
                        thread=rpc.request('thread/read',{'threadId':tid,'includeTurns':True})['thread']
                        patch.require(thread['id']==tid and len(thread['turns'])>=3,'Chat identity/history was lost')
                        report['checks'].append({'same_chat':True,'model':model,'provider':expected,'turns_retained':len(thread['turns'])})
                    report['status']='passed'
                finally:stop_probe(driver);stop_probe(process)
    finally:
        server.shutdown();server.server_close()
        manifest['switching_probe']=report['status'];patch.save_json(directory/'manifest.json',manifest)
        patch.save_json(directory/'switching-probe-result.json',report)
    print('PASS: saved-chat provider switching, old context repair and post-compaction window',flush=True)
    return report

if __name__=='__main__':main(sys.argv[1])
