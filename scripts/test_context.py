"""Offline context regression: real CLI, patched route, synthetic loopback usage.

The stub reports 180k tokens (more than the old 128k preset) on two small
turns per model. It never contacts a model provider or uses real credentials.
"""
from pathlib import Path
import http.server
import json
import queue
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import patch
from scripts.test_offline import isolated_environment, launch_options, stop_probe, probe_command


class ResponsesStub(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.requests.append({'path': self.path, 'model': body.get('model'), 'effort':body.get('reasoning',{}).get('effort')})
        index = len(self.server.requests)
        usage_tokens=getattr(self.server,'usage_tokens',180000)
        message = {'id':f'msg_{index}', 'type':'message', 'status':'completed', 'role':'assistant',
                   'content':[{'type':'output_text','text':'CONTEXT_OK','annotations':[]}]}
        response = {'id':f'resp_{index}', 'object':'response','created_at':int(time.time()),'status':'completed',
                    'model':body['model'], 'output':[message],
                    'usage':{'input_tokens':usage_tokens,'input_tokens_details':{'cached_tokens':0},
                             'output_tokens':3,'output_tokens_details':{'reasoning_tokens':0},'total_tokens':usage_tokens+3}}
        created = {**response, 'status':'in_progress','output':[], 'usage':None}
        events = [
            {'type':'response.created','response':created},
            {'type':'response.output_item.added','output_index':0,'item':{**message,'status':'in_progress','content':[]}},
            {'type':'response.content_part.added','output_index':0,'content_index':0,'item_id':message['id'],
             'part':{'type':'output_text','text':'','annotations':[]}},
            {'type':'response.output_text.delta','output_index':0,'content_index':0,'item_id':message['id'],'delta':'CONTEXT_OK'},
            {'type':'response.output_text.done','output_index':0,'content_index':0,'item_id':message['id'],'text':'CONTEXT_OK'},
            {'type':'response.content_part.done','output_index':0,'content_index':0,'item_id':message['id'],'part':message['content'][0]},
            {'type':'response.output_item.done','output_index':0,'item':message},
            {'type':'response.completed','response':response},
        ]
        raw = ''.join('event: '+e['type']+'\ndata: '+json.dumps({**e,'sequence_number':i})+'\n\n' for i,e in enumerate(events)).encode()
        self.send_response(200)
        self.send_header('Content-Type','text/event-stream')
        self.send_header('Content-Length',str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
        self.wfile.flush()


class Rpc:
    def __init__(self, process):
        self.process = process
        self.events = queue.Queue()
        self.pending = []
        self.counter = 0
        def reader():
            for line in process.stdout:
                try: self.events.put(json.loads(line))
                except ValueError: pass
            self.events.put(None)
        threading.Thread(target=reader,daemon=True).start()

    def receive_event(self, timeout=45):
        value = self.events.get(timeout=timeout)
        patch.require(value is not None,'Isolated app-server stopped')
        return value

    def request(self, method, params):
        self.counter += 1
        self.process.stdin.write(json.dumps({'id':self.counter,'method':method,'params':params})+'\n')
        self.process.stdin.flush()
        end = time.monotonic()+45
        while True:
            event = self.receive_event(max(.1,end-time.monotonic()))
            if event.get('id') == self.counter:
                patch.require('error' not in event,'Local RPC error: '+json.dumps(event.get('error')))
                return event['result']
            self.pending.append(event)

    def turn(self, tid, effort=None):
        params={'threadId':tid,'input':[{'type':'text','text':'Reply CONTEXT_OK.'}]}
        if effort is not None:params['effort']=effort
        result = self.request('turn/start',params)
        turn_id = result['turn']['id']
        collected = []
        end = time.monotonic()+45
        while time.monotonic()<end:
            event = self.pending.pop(0) if self.pending else self.receive_event(max(.1,end-time.monotonic()))
            if event.get('params',{}).get('threadId') != tid:
                continue
            collected.append(event)
            if event.get('method')=='turn/completed' and event['params']['turn']['id']==turn_id:
                patch.require(event['params']['turn']['status']=='completed', 'Synthetic turn failed')
                return collected
        raise TimeoutError('Synthetic turn did not complete')


def main(directory):
    directory, manifest = patch.load_build(Path(directory))
    model_routes=manifest.get('model_routes')
    models = list(model_routes) if model_routes is not None else manifest.get('proxy',{}).get('models',[])
    evidence = json.loads((directory/'compatibility.json').read_text(encoding="utf-8"))['evidence']
    route = evidence['route_js']
    node = ("const fs=require('fs'),vm=require('vm');const x=JSON.parse(fs.readFileSync(0,'utf8'));"
            "const c=vm.createContext({});vm.runInContext(x.route,c);"
            "console.log(JSON.stringify(x.models.map(model=>c.codexPatchAppServerParams('thread/start',{model},'local'))));")
    routed = json.loads(subprocess.check_output(['node','-e',node],input=json.dumps({'route':route,'models':models}),text=True,encoding="utf-8",errors="replace",**patch.background_options()))
    expected={m['slug']:m['context_window'] for m in json.loads((directory/'desktop-models.json').read_text(encoding="utf-8"))['models']}
    app = directory/'patched.app' if manifest['status']=='prepared' else Path(manifest['target'])
    server = http.server.ThreadingHTTPServer(('127.0.0.1',0),ResponsesStub)
    server.daemon_threads = True
    server.requests = []
    threading.Thread(target=server.serve_forever,daemon=True).start()
    report = {'status':'failed','external_inference_turns':0,'synthetic_usage_tokens':180003,'models':[]}
    manifest['context_probe']='running'
    patch.save_json(directory/'manifest.json',manifest)
    try:
        with tempfile.TemporaryDirectory(prefix='codex-context-regression-') as tmp:
            base=Path(tmp);env=isolated_environment(base);env['CONTEXT_TEST_KEY']='placeholder'
            # Deliberately retain the old small global defaults. The selected
            # proxy model must supply its own window and compaction threshold.
            config=('model_context_window=128000\nmodel_auto_compact_token_limit=90000\n'
                    +'model_catalog_json='+json.dumps(str(directory/'desktop-models.json'))+'\n'
                    +'[model_providers.easycli-desktop]\nname="Local context test"\nwire_api="responses"\n'
                    +'base_url="http://127.0.0.1:'+str(server.server_port)+'/v1"\n'
                    +'env_key="CONTEXT_TEST_KEY"\nrequires_openai_auth=false\nrequest_max_retries=0\nstream_max_retries=0\n')
            config += config[config.index('[model_providers.easycli-desktop]'):].replace('model_providers.easycli-desktop','model_providers.deepseek-desktop')
            template=config[config.index('[model_providers.deepseek-desktop]'):]
            for provider in {r['provider'] for r in (model_routes or {}).values()}-{'deepseek-desktop','easycli-desktop'}:
                config+=template.replace('model_providers.deepseek-desktop','model_providers.'+provider)
            (base/'home/config.toml').write_text(config,encoding="utf-8")
            work=base/'work';work.mkdir()
            with (directory/'context-probe.log').open('w',encoding='utf-8') as log:
                process=subprocess.Popen(probe_command(app),stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE,stderr=log,text=True,encoding="utf-8",errors="replace",env=env,cwd=work,**launch_options())
                try:
                    rpc=Rpc(process)
                    rpc.request('initialize',{'clientInfo':{'name':'context_regression','version':'1'},'capabilities':{'experimentalApi':True}})
                    for parameters in routed:
                        model=parameters['model'];maximum=expected[model]
                        if parameters['config']['model_auto_compact_token_limit'] <= 180003:
                            server.usage_tokens=max(1000,parameters['config']['model_auto_compact_token_limit']//3)
                        else:server.usage_tokens=180000
                        parameters.update(cwd=str(work),ephemeral=True,approvalPolicy='never',sandbox='read-only',
                                          baseInstructions='You are a local transport test. Reply concisely.')
                        parameters['config']['features.memories']=False
                        thread=rpc.request('thread/start',parameters)
                        start=len(server.requests)
                        events=rpc.turn(thread['thread']['id'])+rpc.turn(thread['thread']['id'])
                        usages=[e['params']['tokenUsage']['modelContextWindow'] for e in events if e.get('method')=='thread/tokenUsage/updated']
                        compactions=[e for e in events if 'compact' in e.get('method','').lower() or
                                     'compact' in str(e.get('params',{}).get('item',{}).get('type','')).lower()]
                        patch.require(usages and set(usages)=={maximum},'Runtime window does not match the provider specification: '+model)
                        patch.require(not compactions and len(server.requests)-start==2,'Unexpected repeated compaction: '+model)
                        report['models'].append({'model':model,'runtime_context_window':maximum,
                                                 'auto_compact_token_limit':parameters['config']['model_auto_compact_token_limit'],
                                                 'synthetic_usage_tokens':server.usage_tokens+3,
                                                 'turns':2,'compactions':len(compactions),'requests':len(server.requests)-start})
                        print('PASS:',model,'window',maximum,'two synthetic turns, no compaction',flush=True)
                    report['status']='passed'
                finally:
                    stop_probe(process)
    finally:
        server.shutdown();server.server_close()
        patch.save_json(directory/'context-probe-result.json',report)
        manifest['context_probe']=report['status']
        patch.save_json(directory/'manifest.json',manifest)
    return report


if __name__=='__main__':
    main(sys.argv[1])
