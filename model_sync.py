"""Discover models and refresh provider specifications without inference calls."""
from __future__ import annotations

import concurrent.futures
import copy
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.parse
import urllib.request

import patch as p

MAX_DOCUMENT = 6_000_000
FAMILIES = {
    'grok': re.compile(r'^grok-\d+(?:\.\d+)?$'),
    'gemini-flash': re.compile(r'^gemini-\d+(?:\.\d+)?-flash(?:-high)?$'),
    'opus': re.compile(r'^claude-opus-\d+(?:-\d+)?(?:-thinking)?$'),
    'deepseek-flash': re.compile(r'^deepseek-(?:v\d+(?:\.\d+)?-)?flash$'),
}
OFFICIAL_HOSTS = {'docs.x.ai', 'ai.google.dev', 'platform.claude.com', 'api-docs.deepseek.com'}
EFFORTS = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')
# Verified against provider docs and CLIProxyAPI 8.0.32's actual adapters.
# A model ID ending in -high or -thinking still accepts a per-request effort.
REASONING_PRESETS = {
    'grok-4.5': ['low', 'medium', 'high'],
    'grok-4.7': ['low', 'medium', 'high', 'xhigh'],
    'gemini-3.8-flash-high': ['low', 'medium', 'high'],
    'claude-opus-4-6-thinking': ['low', 'medium', 'high', 'max'],
}


def configure_reasoning(entry, levels=None):
    model=entry['slug']
    values=REASONING_PRESETS.get(model, levels or ['high'])
    values=list(dict.fromkeys(x for x in values if x in EFFORTS))
    p.require(values, 'No supported reasoning efforts for '+model)
    entry['supported_reasoning_levels']=[{'effort':x,'description':x.title()} for x in values]
    entry['default_reasoning_level']='high' if 'high' in values else values[0]
    entry['supports_reasoning_effort_updates']=len(values)>1
    return entry


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ValueError('Unexpected redirect; credentials were not forwarded')


def fetch(url, key=None, json_data=False):
    parsed = urllib.parse.urlsplit(url)
    p.require(parsed.scheme == 'https' or parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost', '::1'),
              'Model metadata requires HTTPS or loopback HTTP')
    p.require(not parsed.username and not parsed.password and not parsed.fragment, 'Invalid metadata URL')
    headers = {'User-Agent':'CodexModelPatch/1.0', 'Accept':'application/json' if json_data else 'text/markdown,text/html'}
    if key:
        headers['Authorization'] = 'Bearer ' + key
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.build_opener(NoRedirect).open(request, timeout=25) as response:
        raw = response.read(MAX_DOCUMENT + 1)
    p.require(len(raw) <= MAX_DOCUMENT, 'Metadata document exceeds the size limit')
    return json.loads(raw) if json_data else raw.decode('utf-8')


def family(model):
    return next((name for name, expression in FAMILIES.items() if expression.fullmatch(model)), None)


def specification_url(model):
    kind = family(model)
    if kind == 'grok':
        return 'https://docs.x.ai/developers/models/' + model
    if kind == 'gemini-flash':
        return 'https://ai.google.dev/gemini-api/docs/models/' + model.removesuffix('-high')
    if kind == 'opus':
        return 'https://platform.claude.com/docs/en/models/' + model.removeprefix('claude-').removesuffix('-thinking') + '/overview.md'
    if kind == 'deepseek-flash':
        return 'https://api-docs.deepseek.com/quick_start/pricing'
    raise ValueError('No official specification resolver for model: ' + model)


class DocumentText(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts=[]; self.skip=0

    def handle_starttag(self, tag, _attrs):
        if tag in ('script', 'style'): self.skip += 1

    def handle_endtag(self, tag):
        if tag in ('script', 'style'): self.skip = max(0, self.skip-1)

    def handle_data(self, text):
        if not self.skip: self.parts.append(text)


def token_number(number, suffix):
    value = float(number.replace(',', '')) * {'':1, 'k':1000, 'm':1000000}[suffix.lower()]
    p.require(value.is_integer() and 8192 <= value <= 100_000_000, 'Invalid provider context specification')
    return int(value)


def parse_spec(model, document, url):
    p.require(urllib.parse.urlsplit(url).hostname in OFFICIAL_HOSTS, 'Untrusted specification origin')
    parser = DocumentText(); parser.feed(document)
    text = ' '.join(parser.parts) if '<html' in document.lower() or '<!doctype' in document.lower() else document
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'[|*`]', ' ', text)
    text = ' '.join(text.split())
    label = r'input token limit' if family(model) == 'gemini-flash' else r'context (?:window|length)'
    matches = re.findall(label + r'\s*:?\s*([\d][\d,]*(?:\.\d+)?)\s*([kKmM]?)(?=\s|$|[·,.])', text, re.I)
    values = {token_number(n, unit) for n, unit in matches}
    # DeepSeek's official Codex catalog defines its 1M shorthand as 1,048,576.
    if family(model)=='deepseek-flash':
        values={int(float(n.replace(',',''))*(1048576 if unit.lower()=='m' else 1024 if unit.lower()=='k' else 1)) for n,unit in matches}
    p.require(len(values) == 1, 'Official context specification is missing or ambiguous: ' + model)
    p.require(all(8192<=value<=100_000_000 for value in values),'Invalid provider context specification')
    modalities = ['text']
    if re.search(r'(?:input|modalities|supported data types).{0,160}\bimage', text, re.I) or re.search(r'\btext and images\b', text, re.I):
        modalities.append('image')
    levels = ['high']
    match = re.search(r'(?:reasoning efforts?|thinking levels?|thinking supported)\s*(?:\(\s*supported\s*\)|supported)?\s*[:(]?\s*((?:(?:none|minimal|low|medium|high|xhigh|max)[,\s()/]*)+)', text, re.I)
    if match:
        levels = list(dict.fromkeys(re.findall(r'\b(none|minimal|low|medium|high|xhigh|max)\b', match[1].lower()))) or levels
    levels=REASONING_PRESETS.get(model, levels)
    return {'context_window':values.pop(), 'input_modalities':modalities, 'reasoning_levels':levels,
            'source':url, 'spec_sources':{'context_window':'official','input_modalities':'official' if 'image' in modalities else 'inferred','reasoning_levels':'official' if match or model in REASONING_PRESETS else 'inferred'}}


def provider_models(provider):
    auth = provider.get('auth')
    if 'credential' in provider:
        from inference_adapter import read_credential
        key = read_credential(provider['credential'])
    elif auth:
        result = subprocess.run([auth['command']]+auth.get('args', []), capture_output=True, text=True,encoding="utf-8",errors="replace", timeout=15,
                                cwd=auth.get('cwd'), **p.background_options())
        p.require(result.returncode == 0 and result.stdout.strip(), 'Provider credential helper is unavailable')
        key = result.stdout.strip()
    else:
        key = os.environ.get(provider.get('env_key', ''), '')
        p.require(key, 'Provider client credential is unavailable')
    url = provider['base_url'].rstrip('/') + '/models'
    value = fetch(url, key=key, json_data=True)
    rows = value.get('data')
    p.require(isinstance(rows, list) and rows, 'Provider returned an empty or invalid model list')
    p.require(all(isinstance(row, dict) and isinstance(row.get('id'), str) for row in rows), 'Invalid provider model entry')
    return {row['id']:row for row in rows}


def cloud_catalog(home, client_version):
    """Read full native metadata using existing access, with no login refresh.

    This is the same fixed models endpoint used by the official CLI. Tokens
    stay in memory and are never copied into a disposable authentication file,
    emitted in diagnostics, or sent across a redirect. Keychain-only logins
    use the bundled fallback rather than prompting during a background tick.
    """
    auth=Path(home)/'auth.json'
    p.require(auth.is_file() and auth.stat().st_size<=1_000_000,'Existing file-backed ChatGPT access is unavailable')
    tokens=json.loads(auth.read_text(encoding='utf-8')).get('tokens') or {}
    access,account=tokens.get('access_token'),tokens.get('account_id')
    p.require(all(isinstance(value,str) and value and not any(c in value for c in '\r\n\0') for value in (access,account)),
              'Existing ChatGPT access is unavailable')
    p.require(re.fullmatch(r'\d+\.\d+\.\d+',client_version),'Official CLI version is unavailable')
    url='https://chatgpt.com/backend-api/codex/models?'+urllib.parse.urlencode({'client_version':client_version})
    request=urllib.request.Request(url,headers={'Authorization':'Bearer '+access,'ChatGPT-Account-ID':account,
        'Accept':'application/json','User-Agent':'codex_cli_rs/'+client_version})
    with urllib.request.build_opener(NoRedirect).open(request,timeout=20) as response:
        raw=response.read(MAX_DOCUMENT+1)
    p.require(len(raw)<=MAX_DOCUMENT,'Native model catalog exceeds the size limit')
    value=json.loads(raw)
    p.require(isinstance(value.get('models'),list) and value['models'],'Native cloud catalog is empty')
    p.catalog_template(value)
    value['_model_sync']={'mode':'cloud','authentication':'existing-chatgpt-access',
                          'account_refresh':True,'login_refresh':False,'auth_copied':False}
    return value


def native_catalog(app, home):
    """Refresh full cloud metadata; fall back to the untouched CLI's bundle."""
    with tempfile.TemporaryDirectory(prefix='codex-model-catalog-') as temp:
        from scripts.test_offline import isolated_environment
        environment=isolated_environment(Path(temp))
        p.atomic_text(Path(environment['CODEX_HOME'])/'config.toml', 'cli_auth_credentials_store="ephemeral"\n')
        cli=str(p.cli_path(app))
        try:
            version=subprocess.run([cli,'--version'],capture_output=True,text=True,encoding='utf-8',errors='replace',
                                   timeout=15,env=environment,**p.background_options())
            match=re.search(r'\b(\d+\.\d+\.\d+)\b',version.stdout)
            p.require(version.returncode==0 and match,'Official CLI version is unavailable')
            return cloud_catalog(home,match[1])
        except Exception:
            pass  # Static fallback metadata never includes token/error details.
        result = subprocess.run([cli, 'debug', 'models', '--bundled'], capture_output=True, text=True,encoding="utf-8",errors="replace",
                                timeout=90, env=environment, **p.background_options())
        p.require(result.returncode == 0, 'Native GPT catalog refresh failed')
        value = json.loads(result.stdout)
        p.require(isinstance(value.get('models'), list) and value['models'], 'Native GPT catalog is empty')
        p.catalog_template(value)
        value['_model_sync']={'mode':'bundled','authentication':'none','account_refresh':False,
                              'fallback_reason':'cloud_catalog_unavailable'}
        return value


def native_provider(manifest, profile, provider):
    """Resolve metadata access from existing route references, never a helper."""
    models=[model for model,route in manifest.get('model_routes',{}).items() if route['provider']==provider]
    matches=[profile.get('routes',{}).get(model) for model in models]
    matches=[value for value in matches if value is not None]
    if not matches:
        # A fully hidden provider still needs metadata discovery for unhide and
        # future family versions; its credential reference stays in the manifest.
        from official_client import adapter_routes
        matches=list(adapter_routes(manifest, {'metadata-only':{'provider':provider}}).values())
    p.require(matches,'The native provider has no managed connection reference')
    values=[{'base_url':row['base_url'],'credential':copy.deepcopy(row.get('credential'))} for row in matches]
    p.require(all(value==values[0] for value in values),'The native provider has ambiguous connection references')
    return values[0]


def validate_preferences(value):
    p.require(isinstance(value, dict) and set(value) <= {'hidden', 'context_windows', 'compact_limits'}, 'Invalid model preferences')
    p.require(isinstance(value.get('hidden', []), list) and all(isinstance(x, str) for x in value.get('hidden', [])), 'Invalid hidden model list')
    for name in ('context_windows', 'compact_limits'):
        table = value.get(name, {})
        p.require(isinstance(table, dict) and all(isinstance(k, str) and (type(v) is int and v > 0 or v is None) for k,v in table.items()), 'Invalid model reduction settings')
    return copy.deepcopy(value)


def select_models(available, selected, state, preferences, allowed):
    """Only new members of enabled families are added; old exclusions persist."""
    hidden = set(preferences.get('hidden', []))
    seen = set(state.get('seen', []))
    if not state.get('initialized'):
        seen = set(available)  # Do not resurrect deliberately omitted old models.
    added = sorted(model for model in set(available)-seen if family(model) in allowed)
    selected = list(dict.fromkeys([*selected, *added]))
    missing = dict(state.get('missing', {}))
    retained = []
    for model in selected:
        missing[model] = 0 if model in available else missing.get(model, 0)+1
        if model not in hidden and missing[model] < 2:
            retained.append(model)
    # Store preference-hidden models in selection, so unhiding can restore them.
    state.update(initialized=True, seen=sorted(seen|set(available)), missing=missing,
                 selected=selected)
    return retained


def apply_reductions(entry, preferences, fallback=None, *, native=False):
    maximum = entry.get('max_context_window') or entry.get('context_window')
    p.require(type(maximum) is int and maximum > 0, 'Model context maximum is missing: '+entry['slug'])
    requested = preferences.get('context_windows', {}).get(entry['slug'], fallback)
    entry['max_context_window'] = maximum
    entry['context_window'] = min(requested, maximum) if requested else maximum
    if not native:
        entry['effective_context_window_percent'] = 100
    compact = entry.get('auto_compact_token_limit') if native else int(entry['context_window'] * .9)
    selected = preferences.get('compact_limits', {}).get(entry['slug']) or compact
    return min(selected, entry['context_window']) if selected is not None else None


def make_entry(template, model, provider, spec, priority):
    entry = copy.deepcopy(template)
    if provider == 'deepseek-desktop':
        display = spec.get('display_name') or ('DeepSeek Flash' if model == p.MODEL else model.replace('-', ' ').title())
    else:
        import proxy_support as ps
        display = ps.MODELS.get(model, model.replace('claude-', 'Claude ').replace('grok-', 'Grok ').replace('gemini-', 'Gemini ').replace('-', ' ').title())
    entry.update(slug=model, display_name=display, description=display+' · '+provider,
                 context_window=spec['context_window'], max_context_window=spec['context_window'],
                 effective_context_window_percent=100, shell_type='shell_command', visibility='list',
                 supported_in_api=True, priority=priority, additional_speed_tiers=[], service_tiers=[],
                 default_service_tier=None, available_access_programs=None, availability_nux=None, upgrade=None,
                 default_reasoning_level='high', supported_reasoning_levels=[{'effort':x,'description':x} for x in spec.get('reasoning_levels',['high'])],
                 input_modalities=spec.get('input_modalities',['text']), prefer_websockets=False, use_responses_lite=False,
                 tool_mode=None, experimental_supported_tools=[], multi_agent_reasoning_effort='high',
                 supports_search_tool=True, web_search_tool_type='text',
                 support_verbosity=False, default_verbosity=None)
    return configure_reasoning(entry, spec.get('reasoning_levels'))


def snapshot_digest(snapshot):
    values={k:snapshot[k] for k in ('catalog','routes','locals','deepseek_enabled','proxy') if k in snapshot}
    return p.sha(json.dumps(values,sort_keys=True,separators=(',',':')).encode())


def synchronize(settings, directory, baseline, *, fetch_native=native_catalog, fetch_provider=provider_models, fetch_document=fetch):
    """Return a complete validated snapshot; keep last good provider data on outage."""
    directory=Path(directory); baseline=Path(baseline)
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    current=p.tomllib.loads(p.read_config(Path(settings['codex_home'])/'config.toml'))
    manifest=json.loads((baseline/'manifest.json').read_text(encoding="utf-8"))
    native_mode=(manifest.get('architecture') or settings.get('architecture') or getattr(p,'ARCHITECTURE','legacy'))=='official-client-v1'
    profile_path=baseline/'adapter-profile.json'
    if manifest.get('architecture')=='official-client-v1':
        p.require(profile_path.is_file() and not profile_path.is_symlink()
                  and p.file_sha(profile_path)==manifest.get('profile_sha256'),
                  'The native model connection profile changed')
    profile=json.loads(profile_path.read_text(encoding='utf-8')) if manifest.get('architecture')=='official-client-v1' else None
    previous=json.loads((directory/'models.json').read_text(encoding="utf-8")) if (directory/'models.json').exists() else {}
    policy=copy.deepcopy(previous.get('policy', {}))
    preferences=validate_preferences(json.loads((directory/'model-preferences.json').read_text(encoding="utf-8")) if (directory/'model-preferences.json').exists() else {})
    old_catalog=json.loads(Path(current['model_catalog_json']).read_text(encoding="utf-8"))
    old_models={m['slug']:m for m in old_catalog['models']}
    # Recognize manual reductions to a managed catalog instead of overwriting them.
    installed_snapshot=baseline/'synced-models.json'
    installed_defaults=json.loads(installed_snapshot.read_text(encoding="utf-8")).get('defaults',{}) if installed_snapshot.exists() else {}
    initial_native_import=(native_mode and not installed_snapshot.exists()
                           and previous.get('architecture')!='official-client-v1')
    retained_limits=[]
    if native_mode:
        # Managed per-model reductions survive the first native sync even when
        # the legacy build predates synced-models.json. The old global DS cap
        # was shared with GPT and is handled separately during migration.
        for model,route in manifest.get('model_routes',{}).items():
            window,maximum=route.get('context_window'),route.get('max_context_window')
            inherited_ds=(manifest.get('architecture')!='official-client-v1' and route['provider']==p.PROVIDER
                          and current.get('model_provider','openai')=='openai' and current.get('model_context_window')==window)
            if initial_native_import and not inherited_ds:
                retained_limits.append((model,window,maximum,route.get('compact_limit')))
            if type(window) is int:installed_defaults.setdefault(model,window)
        original_preferences=manifest.get('native_config',{}).get('original_root',current)
        original_window=original_preferences.get('model_context_window')
        if type(original_window) is int:
            for model,entry in old_models.items():
                if model not in manifest.get('model_routes',{}) and model.startswith(('gpt-','codex-')):
                    installed_defaults.setdefault(model,min(original_window,entry.get('max_context_window') or original_window))
                    if initial_native_import:
                        retained_limits.append((model,original_window,entry.get('max_context_window') or original_window,
                                                original_preferences.get('model_auto_compact_token_limit')))
    for model, default in installed_defaults.items():
        value=old_models.get(model,{}).get('context_window')
        if type(value) is int and value < default:
            table=preferences.setdefault('context_windows',{})
            if model in table and table[model] is None:
                # A reset has priority until its maximum was actually installed.
                # Later direct edits to that installed maximum remain detectable.
                maximum=old_models.get(model,{}).get('max_context_window')
                if default != maximum:continue
                table[model]=value
                continue
            table[model]=min(value,table.get(model,value))
    # Import legacy/initial per-model choices only once and only after checking
    # live edits. A later reset-context must not re-import the old managed cap.
    for model,window,maximum,compact in retained_limits:
        if model in preferences.get('context_windows',{}) and preferences['context_windows'][model] is None:
            continue
        if type(window) is int and type(maximum) is int and window<maximum:
            preferences.setdefault('context_windows',{}).setdefault(model,window)
        if type(window) is int and type(compact) is int and 0<compact<int(window*.9):
            preferences.setdefault('compact_limits',{}).setdefault(model,compact)
    if preferences:
        p.save_json(directory/'model-preferences.json',preferences)
    warnings=[]
    try:
        native=fetch_native(Path(settings['app']),Path(settings['codex_home']))
    except Exception:
        warnings.append('Native GPT catalog could not refresh; retaining its last good entries')
        native={'models':[m for m in old_catalog['models'] if m['slug'].startswith(('gpt-','codex-')) and m['slug'] not in previous.get('routes',manifest.get('model_routes',{}))],
                '_model_sync':{'mode':'retained','authentication':'none','account_refresh':False}}
    native_source=copy.deepcopy(native.get('_model_sync',{'mode':'provided','account_refresh':None}))
    entries=[]; defaults={};routes={};specs=copy.deepcopy(previous.get('specs', {}))
    for raw in native['models']:
        entry=copy.deepcopy(raw);slug=entry['slug']
        if slug in preferences.get('hidden',[]):continue
        if entry.get('max_context_window') or entry.get('context_window'):
            compact=apply_reductions(entry,preferences,native=native_mode)
            if native_mode and (compact is not None or 'auto_compact_token_limit' in entry):
                entry['auto_compact_token_limit']=compact
            defaults[slug]=entry['context_window']
        entries.append(entry)
    template=p.catalog_template(native)
    connection_settings = json.loads((directory/'connections.json').read_text(encoding='utf-8')) if (directory/'connections.json').exists() else {}
    manifest = copy.deepcopy(manifest)
    if settings['edition'] in ('ds','proxy','unified'):
        if 'deepseek_enabled' in connection_settings:
            p.require(type(connection_settings['deepseek_enabled']) is bool,'Invalid DeepSeek preference')
            manifest['deepseek_enabled'] = connection_settings['deepseek_enabled']
        if 'proxy' in connection_settings:
            from proxy_support import validate
            manifest['proxy'] = validate(connection_settings['proxy']) if connection_settings['proxy'] else None
        manifest['keychain_service'] = manifest.get('keychain_service') or settings.get('keychain_service') or p.KEYCHAIN_SERVICE
        manifest['keychain_account'] = manifest.get('keychain_account') or settings.get('keychain_account') or __import__('getpass').getuser()
    deepseek=manifest.get('deepseek_enabled',p.PROVIDER in current.get('model_providers',{}))
    providers=[('deepseek-desktop',[p.MODEL],{'deepseek-flash'})] if deepseek else []
    locals=manifest.get('locals',[])
    connections=directory/'connections.json'
    if settings['edition'] in ('ds','proxy','unified') and connections.exists():
        import local_support
        locals=local_support.load(connections)
    if settings['edition'] in ('ds','proxy','unified') and manifest.get('proxy'):
        providers.append(('easycli-desktop',manifest['proxy']['models'],{'grok','gemini-flash','opus'} if connection_settings.get('proxy_auto_add',True) else set()))
    resolved={};direct_specs={}
    for provider, initial, allowed in providers:
        state=policy.setdefault(provider,{})
        selected=state.get('selected',initial)
        try:
            if profile is not None and provider in ('easycli-desktop','deepseek-desktop') and connection_settings:
                from official_client import adapter_routes
                route = adapter_routes(manifest, {'metadata-only':{'provider':provider}})['metadata-only']
                connection = {'base_url':route['base_url'],'credential':route.get('credential')}
                # An explicit selection edit replaces policy, while discovery
                # continues to retain deliberately hidden model preferences.
                if provider == 'easycli-desktop' and connection_settings.get('proxy_selection_id'):
                    if state.get('selection_revision') != connection_settings['proxy_selection_id']:
                        selected=list(initial);state['selection_revision']=connection_settings['proxy_selection_id']
            else:
                connection=native_provider(manifest,profile,provider) if profile is not None else current['model_providers'][provider]
            available=fetch_provider(connection)
            if provider=='deepseek-desktop':
                for model,row in available.items():
                    size=row.get('context_window')
                    if type(size) is int and 8192<=size<=100_000_000:
                        direct_specs[model]={'context_window':size,'input_modalities':[x for x in row.get('input_modalities',['text']) if x in ('text','image')],
                                             'reasoning_levels':row.get('effort',{}).get('supported_levels',['low','high','max']),
                                             'max_output_tokens':row.get('max_output_tokens'),
                                             'display_name':str(row.get('name') or model)[:100],
                                             'source':connection['base_url'].rstrip('/')+'/models'}
            chosen=select_models(available,selected,state,preferences,allowed)
        except Exception:
            warnings.append(provider+': model endpoint unavailable; retaining last good selection')
            chosen=[x for x in selected if x not in preferences.get('hidden',[])]
        resolved[provider]=chosen
    selected_all=[x for models in resolved.values() for x in models]
    def read_spec(model):
        if model in direct_specs:return model,direct_specs[model],None
        try:
            url=specification_url(model)
            return model,parse_spec(model,fetch_document(url),url),None
        except Exception:
            old=specs.get(model)
            if not old and model in old_models:
                entry=old_models[model]
                old={'context_window':entry.get('max_context_window') or entry['context_window'],
                     'input_modalities':entry.get('input_modalities',['text']),
                     'reasoning_levels':[x['effort'] for x in entry.get('supported_reasoning_levels',[])],
                     'source':'retained-installed-specification'}
            return model,old,'Official specification unavailable: '+model
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for model,spec,warning in pool.map(read_spec,selected_all):
            if warning:warnings.append(warning)
            if spec:specs[model]=spec
    for provider,chosen in resolved.items():
        for model in chosen:
            if model not in specs:
                continue  # New unknown specs never receive an arbitrary small cap.
            entry=make_entry(template,model,provider,specs[model],90+len(routes))
            fallback=current.get('model_context_window') if provider=='deepseek-desktop' and not native_mode else None
            compact=apply_reductions(entry,preferences,fallback)
            if provider=='deepseek-desktop' and not native_mode and current.get('model_auto_compact_token_limit'):
                compact=min(compact,current['model_auto_compact_token_limit'])
            defaults[model]=entry['context_window']
            routes[model]={'provider':provider,'context_window':entry['context_window'],
                           'max_context_window':entry['max_context_window'],'compact_limit':compact}
            entries.append(entry)
    if locals:
        import local_support as ls
        for connection in locals:
            identifier=connection['id']
            try:
                found,issues=ls.discover(connection)
                warnings.extend(identifier+': '+w for w in issues)
            except Exception:
                warnings.append(identifier+': local model service unavailable; retaining last good models')
                previous_models={m['slug']:m for m in previous.get('catalog',{}).get('models',[])}
                for model,route in previous.get('routes',manifest.get('model_routes',{})).items():
                    if route['provider'] not in (identifier,identifier+'-compat') or model in preferences.get('hidden',[]):continue
                    prior=old_models.get(model,previous_models.get(model))
                    if prior:
                        p.require(not any(e['slug']==model for e in entries),'Model ID belongs to more than one provider: '+model)
                        entry=copy.deepcopy(prior);compact=apply_reductions(entry,preferences,route.get('context_window'))
                        entries.append(entry);routes[model]={**route,'context_window':entry['context_window'],'compact_limit':compact}
                        defaults[model]=entry['context_window']
                continue
            # A metadata failure does not erase a previously working model.
            for issue in issues:
                model=issue.split(': ',1)[0]
                if previous.get('routes',{}).get(model,{}).get('provider') in (identifier,identifier+'-compat') and model in specs:
                    found.setdefault(model,specs[model])
            connection['chat_only_models']=[m for m,spec in found.items() if not spec.get('supports_tools',True)]
            connection['normalize_instructions_models']=[m for m,spec in found.items() if spec.get('normalize_instructions')]
            connection['tool_adapter_models']=[m for m,spec in found.items() if ls.needs_tool_adapter(spec)]
            for model in [m for m in found if m not in preferences.get('hidden',[])]:
                p.require(not any(e['slug']==model for e in entries),'Model ID belongs to more than one provider: '+model)
                spec=found[model];specs[model]=spec
                configured=spec.get('configured_context_window') if connection.get('respect_server_context') else None
                if type(configured) is int and 1024<=configured<spec['context_window']:
                    preferences.setdefault('context_windows',{}).setdefault(model,configured)
                entry=ls.make_entry(template,model,connection,spec,100+len(routes))
                compact=apply_reductions(entry,preferences,configured)
                defaults[model]=entry['context_window'];entries.append(entry)
                routes[model]={'provider':identifier+('-compat' if not spec.get('supports_tools',True) or spec.get('normalize_instructions') or ls.needs_tool_adapter(spec) else ''),'context_window':entry['context_window'],
                               'max_context_window':entry['max_context_window'],'compact_limit':compact,
                               'supports_tools':spec.get('supports_tools',True)}
        # Apply current transport policy even while an endpoint is offline and
        # its last verified model specifications must be retained.
        by_model={entry['slug']:entry for entry in entries}
        for connection in locals:
            identifier=connection['id']
            for model,route in routes.items():
                if route['provider'] not in (identifier,identifier+'-compat'):continue
                tools=route.get('supports_tools',True)
                by_model[model]['supports_search_tool']=tools
                if tools and ls.needs_tool_adapter(specs.get(model,{})):
                    route['provider']=identifier+'-compat'
                    selected=connection.setdefault('tool_adapter_models',[])
                    if model not in selected:selected.append(model)
    if preferences:p.save_json(directory/'model-preferences.json',preferences)
    p.require(entries, 'No models remain visible')
    snapshot={'format':1,'edition':settings['edition'],'catalog':{'models':entries},'routes':routes,
              'architecture':'official-client-v1' if native_mode else 'legacy',
              'specs':specs,'policy':policy,'defaults':defaults,'warnings':sorted(set(warnings)),
              'locals':locals,'deepseek_enabled':deepseek,'native_catalog_source':native_source}
    if 'proxy' in connection_settings:snapshot['proxy']=manifest.get('proxy')
    snapshot['digest']=snapshot_digest(snapshot)
    p.save_json(directory/'models.json',snapshot)
    return snapshot


def render_route(source, routes, native_contexts=None):
    pattern=r'(/\* MODEL_ROUTES_BEGIN \*/).*?(/\* MODEL_ROUTES_END \*/)'
    p.require(len(re.findall(pattern,source,re.S))==1,'Dynamic model routing marker is missing')
    source=re.sub(pattern,lambda m:m.group(1)+json.dumps(routes,separators=(',',':'))+m.group(2),source,flags=re.S)
    if native_contexts is not None:
        source=re.sub(r'(/\* NATIVE_CONTEXT_BEGIN \*/).*?(/\* NATIVE_CONTEXT_END \*/)',
                      lambda m:m.group(1)+json.dumps(native_contexts,separators=(',',':'))+m.group(2),source,flags=re.S)
    return source
