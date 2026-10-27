"""Explicit live smoke check for any enabled cloud or local model."""
from pathlib import Path
import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import patch
from scripts.test_context import Rpc
from scripts.test_offline import isolated_environment,launch_options,stop_probe, probe_command


def main(directory,model='deepseek-flash'):
    directory,manifest=patch.load_build(Path(directory))
    routes=manifest.get('model_routes',{})
    if model not in routes:
        import proxy_support as ps
        patch.require(model==patch.MODEL or model in manifest.get('proxy',{}).get('models',[]),'Model is not enabled')
        route={'provider':patch.PROVIDER if model==patch.MODEL else ps.PROVIDER}
    else:route=routes[model]
    provider=route['provider'];text=''
    if provider==patch.PROVIDER:
        text=patch.provider_config(manifest['keychain_account'],manifest['keychain_service'],helper=directory/'windows_support.py')
    elif provider=='easycli-desktop':
        import proxy_support as ps
        text=ps.provider_config(manifest['proxy'],directory/'proxy_support.py')
    else:
        import local_support as ls
        connection=next(r for r in manifest['locals'] if provider in dict(ls.provider_ids(r)))
        text=ls.provider_config(connection,directory/'local_support.py',provider.endswith('-compat'))
    catalog=json.loads((directory/'desktop-models.json').read_text(encoding='utf-8'))
    entry=next(e for e in catalog['models'] if e['slug']==model)
    app=directory/'patched.app' if manifest['status']=='prepared' else Path(manifest.get('deployment_target',manifest['target']))
    report={'model':model,'provider':provider,'status':'failed','checks':[],
            'catalog_sha256':patch.file_sha(directory/'desktop-models.json'),'cli_sha256':patch.file_sha(patch.cli_path(app))}
    suffix=hashlib.sha256(model.encode()).hexdigest()[:12]
    try:
        with tempfile.TemporaryDirectory(prefix='codex-live-smoke-') as tmp:
            base=Path(tmp);env=isolated_environment(base);work=base/'work';work.mkdir()
            (work/'probe.txt').write_text('CODEX_TOOL_OK\n',encoding='utf-8')
            (base/'home/config.toml').write_text('model_catalog_json='+json.dumps(str(directory/'desktop-models.json'))+'\n'+text,encoding='utf-8')
            with (directory/('smoke-'+suffix+'.log')).open('w',encoding='utf-8') as log:
                process=subprocess.Popen(probe_command(app),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=log,
                    text=True,encoding='utf-8',env=env,cwd=work,**launch_options())
                try:
                    rpc=Rpc(process);rpc.request('initialize',{'clientInfo':{'name':'connection_probe','version':'2'},'capabilities':{'experimentalApi':True}})
                    exposed=rpc.request('model/list',{'includeHidden':False,'limit':100})['data']
                    patch.require(sum(e['model']==model for e in exposed)==1,'Model missing from native menu data')
                    config={'web_search':'disabled','features.memories':False}
                    if route.get('context_window'):config.update(model_context_window=route['context_window'],model_auto_compact_token_limit=route['compact_limit'])
                    thread=rpc.request('thread/start',{'model':model,'modelProvider':provider,'cwd':str(work),'ephemeral':True,'approvalPolicy':'never','sandbox':'read-only',
                        'config':config,'baseInstructions':'Follow the test request concisely. Use the shell tool when asked to read a file.'})
                    tid=thread['thread']['id'];patch.require(thread['modelProvider']==provider,'Wrong provider')
                    prompts=[('Reply exactly CONNECTION_OK.','CONNECTION_OK')]
                    if route.get('supports_tools',True):prompts.append(('Use the shell tool to read probe.txt in the current directory. Reply with its exact content.','CODEX_TOOL_OK'))
                    for prompt,expected in prompts:
                        params={'threadId':tid,'model':model,'input':[{'type':'text','text':prompt}]}
                        levels=[r['effort'] for r in entry.get('supported_reasoning_levels',[])]
                        if levels:params['effort']='none' if 'none' in levels else levels[0]
                        turn=rpc.request('turn/start',params);messages=[];types=[];windows=[];end=time.monotonic()+300
                        while time.monotonic()<end:
                            event=rpc.pending.pop(0) if rpc.pending else rpc.receive_event(max(.1,end-time.monotonic()))
                            if event.get('params',{}).get('threadId')!=tid:continue
                            if event.get('method')=='item/completed':
                                item=event['params']['item'];types.append(item['type'])
                                if item['type']=='agentMessage':messages.append(item.get('text',''))
                            if event.get('method')=='thread/tokenUsage/updated':windows.append(event['params']['tokenUsage']['modelContextWindow'])
                            if event.get('method')=='turn/completed' and event['params']['turn']['id']==turn['turn']['id']:
                                patch.require(event['params']['turn']['status']=='completed','Provider failed: '+json.dumps(event['params']['turn'].get('error')))
                                break
                        else:raise TimeoutError('Model reply did not complete')
                        matched=expected in '\n'.join(messages)
                        patch.require(matched if route.get('supports_tools',True) else any(m.strip() for m in messages),
                                      'Expected reply was not returned')
                        if expected=='CODEX_TOOL_OK':patch.require('commandExecution' in types,'Model did not call the shell tool')
                        if route.get('context_window'):patch.require(windows and set(windows)=={route['context_window']},'Wrong runtime context')
                        report['checks'].append({'expected':expected,'marker_matched':matched,'item_types':types,'context_windows':windows,
                                                'reply_characters':sum(len(m) for m in messages)})
                        print('PASS:',model,expected,flush=True)
                    report['status']='passed'
                finally:stop_probe(process)
    finally:patch.save_json(directory/('smoke-'+suffix+'-result.json'),report)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    parser.add_argument('--run',action='store_true',required=True,help='Send a short real reply and, if supported, a file-read tool test')
    parser.add_argument('--model',default=patch.MODEL)
    args=parser.parse_args();main(args.directory,args.model)
