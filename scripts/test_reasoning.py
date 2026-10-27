"""Verify selectable efforts and per-turn changes through the real bundled CLI.

All inference responses come from a loopback stub. No provider is contacted.
"""
from pathlib import Path
import http.server
import json
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import patch
from scripts.test_context import ResponsesStub, Rpc
from scripts.test_offline import isolated_environment, launch_options, stop_probe, probe_command


def main(directory):
    directory,manifest=patch.load_build(Path(directory))
    catalog=json.loads((directory/'desktop-models.json').read_text(encoding="utf-8"))
    routes=manifest.get('model_routes')
    if routes is None:
        routes={patch.MODEL:{'provider':patch.PROVIDER},
                **{m:{'provider':'easycli-desktop'} for m in manifest.get('proxy',{}).get('models',[])}}
    selected={e['slug']:e for e in catalog['models'] if e['slug'] in routes}
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),ResponsesStub)
    server.daemon_threads=True;server.requests=[];server.usage_tokens=10
    threading.Thread(target=server.serve_forever,daemon=True).start()
    report={'status':'failed','external_inference_turns':0,'models':[],
            'catalog_sha256':patch.file_sha(directory/'desktop-models.json')}
    manifest['reasoning_probe']='running';patch.save_json(directory/'manifest.json',manifest)
    app=directory/'patched.app' if manifest['status']=='prepared' else Path(manifest.get('deployment_target',manifest['target']))
    try:
        with tempfile.TemporaryDirectory(prefix='codex-reasoning-probe-') as tmp:
            base=Path(tmp);env=isolated_environment(base);env['REASONING_TEST_KEY']='placeholder'
            config='model_catalog_json='+json.dumps(str(directory/'desktop-models.json'))+'\n'
            for provider in {r['provider'] for r in routes.values()}:
                config+=f'[model_providers.{provider}]\nname="Reasoning test"\nwire_api="responses"\n'+\
                        f'base_url="http://127.0.0.1:{server.server_port}/v1"\n'+\
                        'env_key="REASONING_TEST_KEY"\nrequires_openai_auth=false\nrequest_max_retries=0\nstream_max_retries=0\n'
            (base/'home/config.toml').write_text(config,encoding="utf-8")
            work=base/'work';work.mkdir()
            with (directory/'reasoning-probe.log').open('w',encoding='utf-8') as log:
                process=subprocess.Popen(probe_command(app),stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,stderr=log,text=True,encoding="utf-8",errors="replace",env=env,cwd=work,**launch_options())
                try:
                    rpc=Rpc(process)
                    rpc.request('initialize',{'clientInfo':{'name':'reasoning_regression','version':'1'},'capabilities':{'experimentalApi':True}})
                    exposed=rpc.request('model/list',{'includeHidden':False,'limit':100})['data']
                    for model,entry in selected.items():
                        levels=[r['effort'] for r in entry['supported_reasoning_levels']]
                        actual=next(m for m in exposed if m['model']==model)
                        offered=[r['reasoningEffort'] for r in actual['supportedReasoningEfforts']]
                        patch.require(offered==levels,'Selectable reasoning efforts differ: '+model)
                        thread=rpc.request('thread/start',{'model':model,'modelProvider':routes[model]['provider'],
                            'cwd':str(work),'ephemeral':True,'approvalPolicy':'never','sandbox':'read-only',
                            'baseInstructions':'You are a loopback transport test.',
                            'config':{'web_search':'disabled','features.memories':False}})
                        # Include a final return to the first level to catch sticky choices.
                        for effort in (levels+[levels[0]] if levels else [None]):
                            start=len(server.requests);rpc.turn(thread['thread']['id'],effort)
                            sent=server.requests[start:]
                            patch.require(len(sent)==1 and sent[0]['model']==model and sent[0]['effort']==effort,
                                          'Selected effort did not reach provider request: '+model+'/'+str(effort))
                        report['models'].append({'model':model,'selectable_efforts':offered,'transmitted_efforts':levels+[levels[0]] if levels else [None]})
                        print('PASS:',model,'selectable and transmitted:',','.join(levels),flush=True)
                    report['status']='passed'
                finally:stop_probe(process)
    finally:
        server.shutdown();server.server_close()
        manifest['reasoning_probe']=report['status'];patch.save_json(directory/'manifest.json',manifest)
        patch.save_json(directory/'reasoning-probe-result.json',report)
    return report


if __name__=='__main__':main(sys.argv[1])
