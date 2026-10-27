"""Local/compatible Responses connections with optional idle-exiting chat adaptation."""
from __future__ import annotations
import argparse
import copy
import json
import os
from pathlib import Path
import re
import sys
import urllib.parse
import urllib.request


def validate(value):
    if not isinstance(value,dict):
        raise ValueError('Connection must be an object')
    result=copy.deepcopy(value)
    allowed={'id','name','kind','base_url','models','key_file','model_specs','chat_only_models','normalize_instructions_models','tool_adapter_models','respect_server_context','wire_api','supports_reasoning_effort'}
    if set(result)-allowed or not re.fullmatch(r'local-[a-z0-9-]+',result.get('id','')):
        raise ValueError('Use a unique local-... connection ID')
    if result.get('kind') not in ('ollama','lmstudio','openai'):
        raise ValueError('Connection kind must be ollama, lmstudio or openai')
    url=urllib.parse.urlsplit(result.get('base_url',''))
    if url.scheme not in ('http','https') or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('Use an HTTP(S) API URL without credentials or query parameters')
    result['base_url']=result['base_url'].rstrip('/')
    result.setdefault('name',result['id'])
    result.setdefault('models',[])
    if not isinstance(result['models'],list) or any(not isinstance(m,str) or not m for m in result['models']):
        raise ValueError('Model selection must be a list of IDs')
    if result.get('key_file') and not Path(result['key_file']).is_absolute():
        raise ValueError('Key file must use an absolute path')
    result.setdefault('respect_server_context',False)
    if type(result['respect_server_context']) is not bool:raise ValueError('Invalid server context preference')
    result.setdefault('wire_api','responses')
    if result['wire_api'] not in ('responses','chat_completions','anthropic_messages'):raise ValueError('Unsupported model API protocol')
    result.setdefault('supports_reasoning_effort',False)
    if type(result['supports_reasoning_effort']) is not bool:raise ValueError('Invalid reasoning-effort capability')
    result.setdefault('chat_only_models',[])
    result.setdefault('normalize_instructions_models',[])
    result.setdefault('tool_adapter_models',[])
    if not isinstance(result['tool_adapter_models'],list) or any(not isinstance(m,str) for m in result['tool_adapter_models']):
        raise ValueError('Invalid tool-adaptation model list')
    if not isinstance(result['normalize_instructions_models'],list) or any(not isinstance(m,str) for m in result['normalize_instructions_models']):
        raise ValueError('Invalid instruction-adaptation model list')
    if not isinstance(result['chat_only_models'],list) or any(not isinstance(m,str) for m in result['chat_only_models']):
        raise ValueError('Invalid chat-only model list')
    result.setdefault('model_specs',{})
    if not isinstance(result['model_specs'],dict):raise ValueError('Invalid model specifications')
    return result


def load(path):
    value=json.loads(Path(path).read_text(encoding='utf-8'))
    rows=value.get('locals',value.get('providers',[]))
    if not isinstance(rows,list):raise ValueError('Connections must contain a provider list')
    result=[validate(row) for row in rows]
    if len({r['id'] for r in result})!=len(result):raise ValueError('Connection IDs must be unique')
    return result


def request(connection,path='',body=None):
    from model_sync import NoRedirect,MAX_DOCUMENT
    headers={'Accept':'application/json'}
    if connection.get('key_file'):
        key=Path(connection['key_file']).read_text(encoding='utf-8').strip()
        if not key or any(c in key for c in '\r\n\0'):raise ValueError('Invalid local API key file')
        headers['x-api-key' if connection.get('wire_api')=='anthropic_messages' else 'Authorization']=key if connection.get('wire_api')=='anthropic_messages' else 'Bearer '+key
    if connection.get('wire_api')=='anthropic_messages':headers['anthropic-version']='2023-06-01'
    root=connection['base_url']
    url=root+path if not path.startswith('@') else root.removesuffix('/v1')+path[1:]
    raw=None if body is None else json.dumps(body).encode()
    if raw is not None:headers['Content-Type']='application/json'
    req=urllib.request.Request(url,data=raw,headers=headers)
    with urllib.request.build_opener(NoRedirect).open(req,timeout=30) as response:data=response.read(MAX_DOCUMENT+1)
    if len(data)>MAX_DOCUMENT:raise ValueError('Model metadata is too large')
    return json.loads(data)


def model_list(connection, fetch=request):
    page=fetch(connection,'/models');rows=page.get('data',[])
    if connection.get('wire_api')=='anthropic_messages':
        cursors=set()
        while page.get('has_more'):
            cursor=page.get('last_id')
            if not isinstance(cursor,str) or not cursor or cursor in cursors:raise ValueError('Invalid model pagination cursor')
            cursors.add(cursor)
            page=fetch(connection,'/models?after_id='+urllib.parse.quote(cursor,safe=''))
            more=page.get('data')
            if not isinstance(more,list):raise ValueError('Invalid model list page')
            rows.extend(more)
    if not isinstance(rows,list):raise ValueError('Invalid compatible model list')
    return {'data':rows,'has_more':False}


def discover(connection, fetch=request):
    """Query metadata only; no model download, loading or inference."""
    connection=validate(connection);kind=connection['kind']
    rows=model_list(connection,fetch)['data']
    lms={}
    if kind=='lmstudio':
        try:
            records=fetch(connection,'@/api/v1/models').get('models',[])
            lms={r['key']:r for r in records if r.get('type')=='llm'}
            for row in records:
                for instance in row.get('loaded_instances',[]):lms[instance['id']]=row
        except Exception:
            records=fetch(connection,'@/api/v0/models').get('data',[])
            lms={r['id']:r for r in records if r.get('type') in ('llm','vlm')}
    loaded={}
    if kind=='ollama' and connection['respect_server_context']:
        try:loaded={r.get('name',r.get('model')):r.get('context_length') for r in fetch(connection,'@/api/ps').get('models',[])}
        except Exception:pass
    result={};warnings=[]
    for row in rows:
        model=row.get('id')
        if not isinstance(model,str) or not model:continue
        if connection['models'] and model not in connection['models']:continue
        if row.get('type') in ('embedding','embeddings'):continue
        info=copy.deepcopy(row);modalities=['text'];levels=[];tools=True
        try:
            if connection['wire_api']=='anthropic_messages':
                capabilities=info.get('capabilities') or {}
                if info.get('max_input_tokens'):info['context_window']=info['max_input_tokens']
                if info.get('max_tokens'):info['max_output_tokens']=info['max_tokens']
                efforts=capabilities.get('effort') or {}
                info['reasoning_levels']=[k for k in ('low','medium','high','xhigh','max') if isinstance(efforts.get(k),dict) and efforts[k].get('supported') is True]
                if capabilities.get('image_input',{}).get('supported'):info['input_modalities']=['text','image']
                if capabilities.get('thinking',{}).get('types',{}).get('adaptive',{}).get('supported'):info['thinking_mode']='adaptive'
            if kind=='ollama':
                details=fetch(connection,'@/api/show',{'model':model})
                capabilities=details.get('capabilities',[])
                if 'completion' not in capabilities:continue
                maximum=next((v for k,v in details.get('model_info',{}).items() if k.endswith('.context_length')),None)
                tools='tools' in capabilities
                if 'vision' in capabilities:modalities.append('image')
                if 'thinking' in capabilities:
                    levels=['low','medium','high'] if 'gpt-oss' in model else ['none','high']
                info.update(context_window=maximum,supports_tools=tools,reasoning_levels=levels,input_modalities=modalities)
                info['normalize_instructions']=details.get('model_info',{}).get('general.architecture')=='qwen35'
                configured=re.search(r'(?m)^num_ctx\s+(\d+)',details.get('parameters',''))
                if configured:info['configured_context_window']=int(configured[1])
                if type(loaded.get(model)) is int:info['configured_context_window']=loaded[model]
            elif kind=='lmstudio':
                if model not in lms:continue
                info.update(lms[model])
                info['context_window']=info.get('max_context_length')
                if isinstance(info.get('capabilities'),dict) and info['capabilities'].get('vision'):
                    modalities.append('image')
                if isinstance(info.get('capabilities'),dict):
                    tools=info['capabilities'].get('trained_for_tool_use',info['capabilities'].get('tools',True))
                if 'gpt-oss' in model:levels=['low','medium','high']
            # Explicit metadata supplied by an operator is used when an API omits it.
            declared=connection['model_specs'].get(model,{})
            reported=info.get('max_context_window') or info.get('max_context_length') or info.get('context_window')
            sources={key:('user' if key in declared else 'service' if key in info else 'inferred')
                     for key in ('context_window','max_output_tokens','input_modalities','reasoning_levels','supports_tools')}
            info.update(declared)
            maximum=info.get('max_context_window') or info.get('max_context_length') or info.get('context_window')
            if type(maximum) is not int or not 1024<=maximum<=100_000_000:
                raise ValueError('Model maximum context is missing')
            maximum_key=next(k for k in ('max_context_window','max_context_length','context_window') if info.get(k))
            sources['context_window']='user' if maximum_key in declared else 'service'
            if connection['wire_api']=='anthropic_messages' and (type(info.get('max_output_tokens')) is not int or info['max_output_tokens']<=0):raise ValueError('Anthropic maximum output is missing')
            result[model]={'max_output_tokens':info.get('max_output_tokens'),'thinking_mode':info.get('thinking_mode'),'spec_sources':sources,'reported_context_window':reported,
                           'context_conflict':bool(declared.get('context_window') and reported and declared['context_window']!=reported),
                           'context_window':maximum,'display_name':info.get('display_name',model),
                           'input_modalities':info.get('input_modalities',modalities),
                           'reasoning_levels':info.get('reasoning_levels',levels),
                           'supports_tools':info.get('supports_tools',tools),
                           'normalize_instructions':bool(info.get('normalize_instructions',False)),
                           'native_tool_search':info.get('native_tool_search') is True,
                           'source':connection['base_url']+'/models',
                           'configured_context_window':info.get('configured_context_window')}
        except Exception as error:warnings.append(model+': '+type(error).__name__+'; check local model metadata')
    return result,warnings


def provider_ids(connection):
    return [(connection['id'],False)]+([(connection['id']+'-compat',True)] if connection.get('chat_only_models') or connection.get('normalize_instructions_models') or connection.get('tool_adapter_models') else [])


def needs_tool_adapter(spec):
    return spec.get('supports_tools',True) and not spec.get('native_tool_search',False)


def provider_config(connection,helper,chat_only=False):
    connection=validate(connection)
    if chat_only:connection['id']+='-compat'
    else:connection.update(chat_only_models=[],normalize_instructions_models=[],tool_adapter_models=[])
    adapted=bool(connection.get('chat_only_models') or connection.get('normalize_instructions_models') or connection.get('tool_adapter_models'))
    base_url=connection['base_url']
    if adapted:
        base_url='http://127.0.0.1:'+str(bridge_port(helper,connection['id']))+'/v1'
    value={'name':connection['name'],'base_url':base_url,'wire_api':'responses',
           'requires_openai_auth':False,'supports_websockets':False,'request_max_retries':0,'stream_max_retries':0}
    text='\n[model_providers.'+connection['id']+']\n'
    for key,v in value.items():text+=key+' = '+json.dumps(v,ensure_ascii=False)+'\n'
    if connection.get('key_file') or adapted:
        executable=Path(sys.executable)
        if executable.name.lower()=='pythonw.exe':executable=executable.with_name('python.exe')
        text+='\n[model_providers.'+connection['id']+'.auth]\ncommand = '+json.dumps(str(executable))+'\n'
        arguments=[str(helper),'credential-get','--key-file',connection['key_file']] if not adapted else [
            str(Path(helper).with_name('local_bridge.py')),'credential-get',str(Path(helper).parent/(connection['id']+'-bridge.json'))]
        text+='args = '+json.dumps(arguments)+'\nrefresh_interval_ms = '+('1' if adapted else '0')+'\n'
    return text


def bridge_port(helper,identifier):
    import hashlib
    return 40000+int(hashlib.sha256((str(Path(helper).parent)+identifier).encode()).hexdigest()[:6],16)%20000


def stage_helpers(directory,connections):
    import secrets,shutil
    directory=Path(directory)
    shutil.copy2(Path(__file__),directory/'local_support.py')
    for connection in connections:
        if not connection.get('chat_only_models') and not connection.get('normalize_instructions_models') and not connection.get('tool_adapter_models'):continue
        shutil.copy2(Path(__file__).with_name('local_bridge.py'),directory/'local_bridge.py')
        shutil.copy2(Path(__file__).with_name('responses_compat.py'),directory/'responses_compat.py')
        profile={k:v for k,v in connection.items() if k in ('base_url','key_file','chat_only_models','normalize_instructions_models','tool_adapter_models')}
        profile.update(port=bridge_port(directory/'local_support.py',connection['id']+'-compat'),token=secrets.token_urlsafe(32))
        import patch
        patch.save_json(directory/(connection['id']+'-compat-bridge.json'),profile)


def make_entry(template,model,provider,spec,priority):
    import model_sync as ms
    entry=copy.deepcopy(template)
    entry.update(slug=model,display_name=spec.get('display_name',model),description=provider['name']+' · '+('聊天 · ' if not spec.get('supports_tools',True) else '')+model,
        context_window=spec['context_window'],max_context_window=spec['context_window'],effective_context_window_percent=100,
        shell_type='shell_command' if spec.get('supports_tools',True) else 'disabled',visibility='list',supported_in_api=True,
        priority=priority,additional_speed_tiers=[],service_tiers=[],default_service_tier=None,available_access_programs=None,
        availability_nux=None,upgrade=None,prefer_websockets=False,use_responses_lite=False,tool_mode=None,
        experimental_supported_tools=[],supports_search_tool=spec.get('supports_tools',True),web_search_tool_type='text',support_verbosity=False,
        default_verbosity=None,input_modalities=spec.get('input_modalities',['text']),supports_image_detail_original=False,
        supports_reasoning_effort_updates=False,default_reasoning_level=None,supported_reasoning_levels=[])
    entry.update(node_repl_disabled=True,apply_patch_tool_type=None,multi_agent_version=None)
    if not spec.get('supports_tools',True):
        entry.update(node_repl_disabled=True,apply_patch_tool_type=None,multi_agent_version=None,
                     include_apps_usage_instructions=False,include_skills_usage_instructions=False,
                     include_plugin_usage_instructions=False,model_messages={'instructions_template':'You are a helpful conversational assistant. This model does not call tools.','instructions_variables':None})
    if spec.get('reasoning_levels'):
        ms.configure_reasoning(entry,spec['reasoning_levels'])
        # Native configuration_update items are not part of compatible Responses.
        entry['supports_reasoning_effort_updates']=False
    return entry


def connect(args):
    import patch,monitor
    directory=args.state_dir.resolve();directory.mkdir(parents=True,exist_ok=True)
    settings=monitor.load_json(directory/'settings.json',{})
    if settings.get('edition','unified') not in patch.INSTALL_AFFINITIES:raise ValueError('Unknown model-adapter installation')
    url=args.url or input('Local / compatible API URL (ends in /v1): ').strip()
    kind=args.kind or ('ollama' if ':11434' in url or ':21434' in url else 'lmstudio' if ':1234' in url else 'openai')
    identifier='local-'+args.name
    row={'id':identifier,'name':args.name,'kind':kind,'base_url':url,'models':args.models or [],'respect_server_context':getattr(args,'respect_server_context',False)}
    row.update(wire_api=getattr(args,'wire_api','responses'),supports_reasoning_effort=getattr(args,'supports_reasoning_effort',False))
    if args.key_file:row['key_file']=str(args.key_file.resolve())
    if args.context_window:
        if not args.models:raise ValueError('Select model IDs when supplying a context maximum')
        row['model_specs']={m:{'context_window':args.context_window} for m in args.models}
    specs,warnings=discover(row)
    if not specs:raise ValueError('No models with usable specifications: '+'; '.join(warnings))
    row['chat_only_models']=[m for m,spec in specs.items() if not spec.get('supports_tools',True)]
    row['normalize_instructions_models']=[m for m,spec in specs.items() if spec.get('normalize_instructions')]
    row['tool_adapter_models']=[m for m,spec in specs.items() if needs_tool_adapter(spec)]
    path=directory/'connections.json';value=monitor.load_json(path,{'locals':[]})
    value['locals']=[r for r in value.get('locals',[]) if r['id']!=identifier]+[validate(row)]
    with monitor.state_lock(directory) as locked:
        if not locked:raise ValueError('Monitor is busy; retry after the current check')
        patch.save_json(path,value)
        cache=monitor.load_json(directory/'updates.json',{});cache['models_checked']=0;patch.save_json(directory/'updates.json',cache)
    print(json.dumps({'connection':identifier,'models':specs,'warnings':warnings,'saved':str(path),
                      'next':'build --connections FILE for first installation; enabled monitors apply on their next check'},ensure_ascii=False,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['credential-get']);parser.add_argument('--key-file',type=Path,required=True)
    args=parser.parse_args()
    try:
        value=args.key_file.read_text(encoding='utf-8').strip()
        if not value or any(c in value for c in '\r\n\0'):raise ValueError()
        print(value,end='')
    except Exception:print('Local API key unavailable',file=sys.stderr);raise SystemExit(1)
