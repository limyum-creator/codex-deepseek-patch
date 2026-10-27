"""On-demand native settings and redacted diagnostics; no HTTP server or daemon."""
from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
import hashlib
import ctypes
import math
import json
import os
from pathlib import Path
import re
import plistlib
import shlex
import subprocess
import sys
import time
import tempfile
from types import SimpleNamespace
import urllib.error
import uuid

import monitor
import patch as p

ROOT = Path(__file__).resolve().parent
PHASES = {'installed': '已生效', 'idle': '等待检查', 'stabilizing': '正在确认更新',
          'building': '正在准备', 'waiting_for_exit': '准备完成，退出 Codex 后应用',
          'installing': '正在安装', 'failed': '更新暂未完成', 'incompatible': '等待兼容适配',
          'unavailable': '暂时无法检查', 'busy': '后台任务进行中'}
READ_ACTIONS = {'snapshot', 'diagnose', 'probe_connection', 'cleanup_preview', 'compare_models', 'hub'}
ACTIONS = READ_ACTIONS | {'save_connection', 'remove_connection', 'save_model', 'save_options',
                         'save_proxy', 'save_deepseek', 'save_images', 'check_updates', 'retry_update',
                         'show_progress', 'update_action', 'sync_models', 'cleanup', 'recover', 'restore', 'cancel_recovery', 'launch_codex'}


class SettingsError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def require(value, message, code='invalid_input'):
    if not value:raise SettingsError(code, message)


def find_directory(explicit=None):
    if explicit is not None:return Path(explicit).expanduser().resolve()
    default = monitor.default_directory()
    candidates=[]
    for path in (default, default.parent/'monitor-proxy', default.parent/'monitor-ds'):
        settings=monitor.load_json(path/'settings.json', {})
        if not p.runtime_accepts(p.EDITION, settings.get('edition')):continue
        candidates.append(path)
        try:
            current=p.tomllib.loads(p.read_config(Path(settings['codex_home'])/'config.toml'))
            baseline=monitor.load_json(path/'state.json',{}).get('installed',{}).get('baseline') or settings.get('baseline')
            if baseline and current.get('model_catalog_json') and Path(current['model_catalog_json']).parent.resolve()==Path(baseline).resolve():return path
        except (KeyError,OSError,ValueError):pass
    require(len(candidates)<=1,'找到多个旧安装，请用 --state-dir 指定当前使用的设置目录。','ambiguous_installation')
    return candidates[0] if candidates else default


# First installation uses the same native cards and the existing installer.
def needs_setup(directory):
    return not (Path(directory)/'settings.json').exists()


def setup_deepseek(draft):
    return draft.get('connection_mode', 'deepseek' if p.EDITION in ('ds','unified') else 'compatible') == 'deepseek'


def setup_read(directory):
    value=monitor.load_json(Path(directory)/'setup/draft.json',{})
    require(not value or value.get('edition')==p.EDITION,'首次安装草稿属于另一版本。','edition_mismatch')
    return value


def setup_revision(directory):
    path=Path(directory)/'setup/draft.json'
    return hashlib.sha256(path.read_bytes() if path.exists() else b'').hexdigest()


def setup_app():
    if sys.platform=='win32':
        try:return str(__import__('windows_support').discover_app())
        except (OSError,ValueError,RuntimeError):return ''
    return next((str(v) for v in (Path('/Applications/ChatGPT.app'),Path('/Applications/Codex.app')) if v.is_dir()),'')


def setup_view(directory, notice=''):
    from update_worker import first_setup_view
    return first_setup_view(directory, notice)


from update_worker import setup_command


def setup_guard(draft):
    home=Path(draft['home']);current=p.read_config(home/'config.toml')
    require(hashlib.sha256(current.encode()).hexdigest()==draft['config_digest'],
            'Codex 配置已变化，请重新保存并准备。','stale_revision')
    value=p.tomllib.loads(current)
    require(not value.get('openai_base_url') and value.get('model_provider','openai')=='openai'
            and not value.get('model_providers',{}).get('openai',{}).get('base_url'),
            '检测到其他模型管理器正在接管 Codex。请先在原管理器中断开 Codex，再进行首次安装。','configuration_conflict')


def setup_dispatch(directory, payload, expected=None, cancelled=None):
    directory=Path(directory);intent=payload.get('intent','refresh');values=payload.get('values') or {}
    require(isinstance(values,dict),'无效的首次安装表单。')
    if intent in ('navigate','refresh'):return setup_view(directory)
    if intent in ('setup_deepseek','setup_compatible'):
        root=monitor.private_directory(directory/'setup')
        with monitor.state_lock(root) as locked:
            require(locked,'首次设置正在处理中。','busy')
            require(expected==setup_revision(directory),'草稿已改变，请刷新后重试。','stale_revision')
            draft=setup_read(directory)
            require(draft.get('phase','draft')=='draft','请先取消当前准备状态。')
            mode='deepseek' if intent=='setup_deepseek' else 'compatible'
            key=root/'credential.key'
            if key.exists() and mode!=draft.get('connection_mode','deepseek' if setup_deepseek(draft) else 'compatible'):
                key.replace(root/('credential-before-switch-'+uuid.uuid4().hex+'.key'))
            draft.update(edition=p.EDITION,phase='draft',connection_mode=mode)
            monitor.atomic_json(root/'draft.json',draft)
        return setup_view(directory)
    require(intent in ('setup_save','setup_prepare','setup_install'),'请先完成首次设置。','not_installed')
    root=monitor.private_directory(directory/'setup')
    with monitor.state_lock(root) as locked:
        require(locked,'首次设置正在处理中。','busy')
        require(expected==setup_revision(directory),'草稿已改变，请刷新后重试。','stale_revision')
        draft=setup_read(directory)
        if cancelled and cancelled():raise SettingsError('cancelled','操作已取消。')
        if intent=='setup_save':
            require(draft.get('phase')!='installed','安装已完成，请继续完成设置。')
            app=Path(str(values.get('app',''))).expanduser();home=Path(str(values.get('home',''))).expanduser()
            require(app.is_absolute() and app.is_dir() and home.is_absolute(),'请指定有效的应用和配置目录。')
            app,home=app.resolve(),home.resolve()
            require(not root.resolve().is_relative_to(home) and not root.resolve().is_relative_to(app),'安装材料不能放在应用或 Codex 配置目录内。')
            secret=values.get('key','');require(isinstance(secret,str) and len(secret)<=2560 and not any(ord(c)<33 for c in secret),'API Key 格式不正确。')
            new={'edition':p.EDITION,'phase':'draft','app':str(app),'home':str(home),
                 'maintenance':values.get('maintenance') is True,
                 'connection_mode':'deepseek' if setup_deepseek(draft) else 'compatible',
                 'config_digest':hashlib.sha256(p.read_config(home/'config.toml').encode()).hexdigest()}
            setup_guard(new)
            __import__('official_client').inspect(app)  # Signed source, no launch or login access.
            key_path=root/'credential.key';old_key=key_path.read_bytes() if key_path.exists() else None
            try:
                if secret:private_write(key_path,secret.encode())
                if setup_deepseek(draft):
                    require(key_path.is_file(),'请填写 DeepSeek API Key。')
                    new['key_service']=draft.get('key_service') or ('Codex DeepSeek API Key '+uuid.uuid4().hex[:12])
                    notice='官方应用检查通过，密钥已私密保存。'
                else:
                    models=([model for i,model in enumerate(draft.get('available',[])) if values.get('model_'+str(i)) is True]
                            if any(k.startswith('model_') for k in values) else hub_list(values.get('models')))
                    maximum=hub_number(values.get('maximum'))
                    require(maximum is None or 1024<=maximum<=100_000_000,'请填写模型实际支持的最大上下文。')
                    connection=_validate_connection({'id':'local-setup','name':str(values.get('name','我的模型')),
                        'kind':values.get('kind','openai'),'base_url':str(values.get('url','')).strip(),
                        'wire_api':values.get('wire','chat_completions'),'models':models,
                        'respect_server_context':values.get('respect') is True,
                        'model_specs':{},
                        **({'key_file':str(key_path)} if key_path.exists() else {})})
                    result=probe_connection(connection,cancelled)
                    require(result['available'],'接口没有返回可用型号。')
                    require(all(m in result['available'] for m in models),'所选型号不在接口返回的模型列表中。')
                    output=hub_number(values.get('output'))
                    connection['model_specs']={model:{**({'context_window':maximum} if maximum else {}),**({'max_output_tokens':output} if output else {})} for model in models if model not in result['specs']}
                    new.update(connection=connection,maximum=maximum,output=output,available=result['available'])
                    notice='发现 '+str(len(result['available']))+' 个型号，可在下方勾选。'
                if cancelled and cancelled():raise SettingsError('cancelled','检测已取消。')
                monitor.atomic_json(root/'draft.json',new)
            except BaseException:
                if secret:
                    if old_key is None:key_path.unlink(missing_ok=True)
                    else:private_write(key_path,old_key)
                raise
            return setup_view(directory,notice)
        require(draft,'请先保存并检测连接。')
        if intent=='setup_prepare':
            require(draft['phase'] in ('draft','prepared'),'已有安装事务，请先完成设置。')
            setup_guard(draft)
            output=directory/'builds'/('setup-'+uuid.uuid4().hex)
            args=['build','--app',draft['app'],'--codex-home',draft['home'],'--output',str(output),'--bundled-catalog','--verify']
            if setup_deepseek(draft):args+=['--with-deepseek','--keychain-service',draft['key_service']]
            else:
                require(draft['connection']['models'],'请先选择要接入的型号。')
                monitor.atomic_json(root/'connections.json',{'locals':[draft['connection']]})
                args+=['--without-deepseek','--connections',str(root/'connections.json')]
            setup_command(args,cancelled)
            setup_guard(draft)
            draft.update(phase='prepared',build=str(output),manifest_digest=p.file_sha(output/'manifest.json'))
            monitor.atomic_json(root/'draft.json',draft)
            return setup_view(directory,'验证通过，可在退出 Codex 后安装。')
        require(draft['phase'] in ('prepared','installed'),'请先准备并完成验证。')
        build=Path(draft['build']);manifest=monitor.load_json(build/'manifest.json',{})
        require(manifest.get('edition')==p.EDITION and manifest.get('codex_home')==draft['home'],'候选安装身份不一致。')
        if draft['phase']=='prepared' and manifest.get('status')=='installed':
            from official_client_install import recover
            result=recover(build)
            require(result.get('status')=='committed','请先恢复中断的安装事务。')
            draft['phase']='installed';monitor.atomic_json(root/'draft.json',draft)
            if setup_deepseek(draft):(root/'credential.key').unlink(missing_ok=True)
        if draft['phase']=='prepared':
            setup_guard(draft)
            require(p.file_sha(build/'manifest.json')==draft['manifest_digest'],'候选已改变，请重新准备。')
            p.require_stopped(Path(draft['app']),read_only_source=True)
            if setup_deepseek(draft):
                save_deepseek_key({'keychain_service':draft['key_service'],'keychain_account':__import__('getpass').getuser()},manifest,(root/'credential.key').read_text(encoding='utf-8'))
            p.install(SimpleNamespace(directory=build))
            draft['phase']='installed';monitor.atomic_json(root/'draft.json',draft)
            if setup_deepseek(draft):(root/'credential.key').unlink(missing_ok=True)
        # A failed monitor registration is resumable without another install.
        monitor.enable(SimpleNamespace(state_dir=directory,baseline=build,app=Path(draft['app']),codex_home=Path(draft['home']),
            interval=300,no_code_updates=not draft['maintenance'],no_model_sync=not draft['maintenance'],
            keychain_service=draft.get('key_service'),keychain_account=__import__('getpass').getuser()))
        ensure_launcher(directory)
        return hub_view(directory,'models',notice='安装完成，可启动 Codex 使用新增模型。')


def records(directory):
    directory = Path(directory)
    settings = monitor.load_json(directory/'settings.json', {})
    require(p.runtime_accepts(p.EDITION, settings.get('edition')), '请使用与已安装版本对应的设置入口。', 'edition_mismatch')
    require(settings.get('baseline') and settings.get('codex_home'), '尚未找到已安装的适配层，请先完成首次安装。', 'not_installed')
    build = Path(settings['baseline'])
    manifest = monitor.load_json(build/'manifest.json', {})
    require(p.runtime_accepts(p.EDITION, manifest.get('edition')) and manifest.get('architecture') == 'official-client-v1',
            '请先迁移到原生适配版本，再使用设置窗口。', 'migration_required')
    return settings, build, manifest


def revision(directory):
    settings, _, _ = records(directory)
    paths = [Path(directory)/name for name in ('settings.json','connections.json','model-preferences.json')]
    paths.append(Path(settings['codex_home'])/'config.toml')
    return hashlib.sha256(b'\0'.join(path.read_bytes() if path.is_file() else b'' for path in paths)).hexdigest()


def connection_document(directory, manifest):
    value = monitor.load_json(Path(directory)/'connections.json')
    if value is None:
        value = {'locals': copy.deepcopy(manifest.get('locals', []))}
    value = copy.deepcopy(value)
    value.setdefault('deepseek_enabled', manifest.get('deepseek_enabled', p.EDITION == 'ds'))
    value.setdefault('proxy', copy.deepcopy(manifest.get('proxy')))
    value.setdefault('proxy_auto_add', True)
    policy = monitor.load_json(Path(directory)/'models.json',{}).get('policy',{}).get('easycli-desktop',{})
    if value.get('proxy') and policy.get('selected') and (
            not value.get('proxy_selection_id') or value['proxy_selection_id']==policy.get('selection_revision')):
        value['proxy']['models'] = list(policy['selected'])
    return value


def _models(directory, build, manifest):
    import model_sync
    preferences = model_sync.validate_preferences(monitor.load_json(Path(directory)/'model-preferences.json', {}))
    cache = monitor.load_json(Path(directory)/'models.json', {})
    installed = monitor.load_json(build/'desktop-models.json', {'models': []})['models']
    entries = {row['slug']: row for row in cache.get('catalog', {}).get('models', [])}
    entries.update({row['slug']: row for row in installed})
    specs = {**manifest.get('model_specifications',{}),**cache.get('specs', {})}
    routes = {**cache.get('routes', {}), **manifest.get('model_routes', {})}
    for model in preferences.get('hidden', []):
        if model not in entries:
            spec = specs.get(model, {})
            entries[model] = {'slug': model, 'display_name': spec.get('display_name', model),
                'max_context_window': spec.get('context_window'), 'context_window': spec.get('context_window'),
                'input_modalities': spec.get('input_modalities', ['text']),
                'supported_reasoning_levels': [{'effort': v} for v in spec.get('reasoning_levels', [])]}
    rows = []
    for model, item in entries.items():
        spec = specs.get(model, {})
        maximum = item.get('max_context_window') or item.get('context_window')
        selected = preferences.get('context_windows', {}).get(model)
        overridden = model in preferences.get('context_windows', {})
        effective = (min(selected, maximum) if selected else maximum) if overridden and maximum else item.get('context_window')
        rows.append({'id': model, 'name': item.get('display_name', model),
            'provider': routes.get(model, {}).get('provider', 'GPT'),
            'visible': model not in preferences.get('hidden', []),
            'maximum': maximum, 'effective': effective, 'installed_window': item.get('context_window'),
            'server_window': spec.get('configured_context_window'), 'context_override': selected,
            'compact_override': preferences.get('compact_limits', {}).get(model),
            'compact': preferences.get('compact_limits', {}).get(model) or
                (item.get('auto_compact_token_limit') if model not in routes else int(effective*.9) if effective else None),
            'tools': routes.get(model, {}).get('supports_tools', spec.get('supports_tools', item.get('shell_type') != 'disabled')),
            'images': 'image' in item.get('input_modalities', []),
            'reasoning': [v['effort'] for v in item.get('supported_reasoning_levels', [])],
            'source': spec.get('source', 'official-catalog'),
            'spec_sources':spec.get('spec_sources',{}), 'context_conflict':spec.get('context_conflict',False),
            'reported_context_window':spec.get('reported_context_window')})
    return sorted(rows, key=lambda row: (row['provider'] != 'GPT', row['provider'], row['name'].casefold()))


def snapshot(directory):
    directory = Path(directory)
    if needs_setup(directory):
        setup_read(directory)
        return {'format':1,'edition':p.EDITION,'revision':setup_revision(directory),'models':[],'connections':[],'progress':{},'recovery':[]}
    settings, build, manifest = records(directory)
    state = monitor.load_json(directory/'state.json', {})
    document = connection_document(directory, manifest)
    current = p.tomllib.loads(p.read_config(Path(settings['codex_home'])/'config.toml'))
    jobs = recovery_jobs(settings['codex_home'])
    return {'format': 1, 'revision': revision(directory), 'edition': p.EDITION,
        'edition_label': '统一版',
        'version': manifest.get('tool_version'), 'app_version': manifest.get('app_version'),
        'phase': state.get('phase', 'idle'), 'status': PHASES.get(state.get('phase'), '等待检查'),
        'queued': (directory/'monitor.request.json').is_file(),
        'models': _models(directory, build, manifest), 'connections': document.get('locals', []),
        'proxy': document.get('proxy'), 'disabled_proxy':document.get('disabled_proxy'),
        'deepseek_enabled': document['deepseek_enabled'],
        'proxy_auto_add': document['proxy_auto_add'],
        'options': {name: settings.get(name, default) for name, default in
                    [('auto_update', True), ('model_sync', True), ('interval', 300), ('progress_ui', True)]},
        'images_enabled': 'cpa_images' in current.get('mcp_servers', {}),
        'recovery': jobs, 'recovery_directory':str(Path(settings['codex_home'])/'model-adapter-operations'),
        'last_checked_at': state.get('last_checked_at'),
        'progress': hub_progress(directory),
        'help_url': 'https://github.com/limyum-creator/codex-deepseek-patch/tree/' +
                    'main'}


def recovery_jobs(home):
    root = Path(home)/'model-adapter-operations'
    if not root.is_dir() or root.is_symlink():return []
    result = []
    for path in root.glob('*.json'):
        if path.is_symlink() or not re.fullmatch(r'[a-f0-9]{32}\.json', path.name):continue
        try:
            if path.stat().st_size > 8192:continue
            value = monitor.load_json(path, {})
            if value.get('format') == 1 and time.time()-value.get('updated_at', 0) < 3600:
                result.append({k: value[k] for k in ('id','model','phase','part','total','level','cache','updated_at') if k in value})
        except (OSError, ValueError, TypeError):continue
    return sorted(result, key=lambda row: row.get('updated_at',0), reverse=True)[:20]


@contextmanager
def edit_lock(directory, expected, cancelled=None, timeout=120):
    deadline = time.monotonic()+timeout
    while True:
        if cancelled and cancelled():raise SettingsError('cancelled', '操作已取消。')
        with monitor.state_lock(directory) as locked:
            if locked:
                require(expected == revision(directory), '设置已被其他操作更新，请刷新后再保存。', 'revision_conflict')
                yield
                return
        if time.monotonic() >= deadline:raise SettingsError('busy', '后台检查仍在进行，请稍后再试。')
        time.sleep(.2)


def _queue(directory):
    monitor.request_update(directory, sync_now=True)
    monitor.wake_monitor(directory)


def probe_connection(connection, cancelled=None):
    import local_support
    connection = local_support.validate(connection)
    def check():
        if cancelled and cancelled():raise SettingsError('cancelled','操作已取消。')
    check()
    catalog = local_support.model_list(connection,lambda row,path: (check(),local_support.request(row,path))[1])
    check()
    def fetch(row,path,body=None):
        check()
        value=catalog if path=='/models' else local_support.request(row,path,body)
        check()
        return value
    specs, warnings = local_support.discover({**connection,'models':[]},fetch=fetch)
    check()
    available = [row['id'] for row in catalog.get('data', []) if isinstance(row,dict) and isinstance(row.get('id'),str)]
    return {'available': available, 'specs': specs, 'warnings': warnings,
            'message': '接口已连接；规格未提供的模型可在高级设置中补充。' if warnings else '连接和模型规格检查通过。'}


def private_write(path, data):
    path=Path(path);monitor.private_directory(path.parent)
    handle,temporary=tempfile.mkstemp(prefix='.credential-',dir=path.parent)
    try:
        with os.fdopen(handle,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:Path(temporary).unlink(missing_ok=True)


def diagnose(directory, *, endpoints=False):
    """Allowlisted report fields only. Never export raw exceptions/config/log text."""
    from official_client_host import health_status
    settings, build, manifest = records(directory)
    state = monitor.load_json(Path(directory)/'state.json', {})
    checks = []
    def add(code, status, message):checks.append({'code': code, 'status': status, 'message': message})
    try:
        from official_client_update import managed_configuration
        managed_configuration(build, manifest, settings['codex_home'])
        add('configuration','ok','受管配置和文件完整。')
    except Exception:add('configuration','error','受管配置或文件发生变化，可重新准备适配层。')
    host = monitor.load_json(build/'host-settings.json', {})
    health = health_status(host)
    add('adapter','ok' if health['ready'] else 'error',
        '本地适配服务正常。' if health['ready'] else '本地适配服务未就绪，请检查或恢复安装。')
    if manifest.get('composer_compat'):
        session=monitor.load_json(build/'composer-session.json',{})
        try:
            processes=p.active_app_processes(Path(manifest['target']),read_only_source=True)
            active=(session.get('status')=='active' and session.get('identity')==host.get('health_identity')
                    and any(row.get('pid')==session.get('pid') for row in processes))
            add('composer','ok' if active else 'warning' if processes else 'info',
                '本次 Codex 已启用独立模型发送兼容。' if active else
                '请正常退出 Codex，再从「偏好 → 启动 Codex」开启独立模型发送。' if processes else
                '发送兼容已准备；下次请从「偏好 → 启动 Codex」打开。')
        except Exception:add('composer','warning','暂时无法确认本次启动的发送兼容状态。')
    try:
        enabled = monitor.Scheduler(directory).status()['enabled']
        add('monitor','ok' if enabled else 'warning','自动监测已开启。' if enabled else '自动监测未开启。')
    except Exception:add('monitor','error','无法读取系统监测任务状态。')
    models = _models(directory, build, manifest)
    add('models','ok' if models else 'warning', '可识别模型：' + str(len(models)))
    for index, connection in enumerate(connection_document(directory,manifest).get('locals', []),1):
        if not endpoints:continue
        try:
            result = probe_connection(connection)
            add('endpoint_'+str(index),'warning' if result['warnings'] else 'ok',
                '接口可访问，发现 '+str(len(result['available']))+' 个模型。')
        except Exception as error:
            value = public_error(error)
            add('endpoint_'+str(index),'error',value['message'])
    report = {'format': 1, 'edition': p.EDITION, 'patch_version': manifest.get('tool_version'),
              'app_version': manifest.get('app_version'), 'platform': sys.platform,
              'phase': state.get('phase'), 'checks': checks,
              'summary': '检查通过' if all(v['status']=='ok' for v in checks) else '有需要处理的项目'}
    report['text'] = '\n'.join(['Codex 模型适配器 · '+report['summary'],
        '系统：'+sys.platform+'　版本：'+str(report['patch_version'])+'　通道：'+p.EDITION,
        'Codex：'+str(report['app_version']), *[v['status'].upper()+' · '+v['message'] for v in checks]])
    return report


def public_error(error):
    if isinstance(error, SettingsError):return {'code':error.code,'message':str(error)}
    if isinstance(error, urllib.error.HTTPError):
        messages = {401:'接口拒绝了凭证，请检查或更新 API Key。',403:'当前账户没有此接口的访问权限。',
                    404:'未找到模型接口，请检查服务地址是否包含正确的 /v1 路径。',429:'服务额度或请求频率受限。'}
        return {'code':'http_'+str(error.code),'message':messages.get(error.code,'上游服务暂时不可用，请稍后重试。')}
    if isinstance(error, (urllib.error.URLError, ConnectionError, TimeoutError)):
        return {'code':'endpoint_unavailable','message':'无法连接服务，请确认服务已启动、地址正确且网络可达。'}
    if isinstance(error, FileNotFoundError):return {'code':'file_missing','message':'配置或凭证文件不存在，请重新选择。'}
    if isinstance(error, PermissionError):return {'code':'permission_denied','message':'无法访问配置文件，请检查当前用户的文件权限。'}
    if isinstance(error, (ValueError, KeyError, TypeError, p.PatchError)):
        return {'code':'invalid_configuration','message':'配置未通过检查，请核对地址、型号、窗口和凭证文件。'}
    return {'code':'operation_failed','message':'操作未完成，现有配置已保留。请运行诊断后重试。'}


def _validate_connection(value):
    import local_support
    connection = local_support.validate(value)
    require(len(connection['id']) <= 100 and len(connection['name']) <= 100, '连接名称过长。')
    for model, spec in connection['model_specs'].items():
        require(isinstance(model,str) and isinstance(spec,dict), '模型规格格式不正确。')
        if 'context_window' in spec:
            require(type(spec['context_window']) is int and 1024 <= spec['context_window'] <= 100_000_000,
                    '请填写模型实际支持的最大上下文。')
        if 'max_output_tokens' in spec:require(type(spec['max_output_tokens']) is int and spec['max_output_tokens']>0,'最大输出须为正整数。')
        if 'supports_tools' in spec:require(type(spec['supports_tools']) is bool, '工具能力必须为开启或关闭。')
        if 'reasoning_levels' in spec:
            require(isinstance(spec['reasoning_levels'],list) and all(v in ('none','minimal','low','medium','high','xhigh','max','ultra') for v in spec['reasoning_levels']), '思考档位格式不正确。')
    return connection


def perform(directory, action, payload=None, expected=None, cancelled=None):
    directory = Path(directory)
    payload = payload or {}
    require(action in ACTIONS and isinstance(payload,dict), '未知操作。')
    if needs_setup(directory):
        if action=='hub':return setup_dispatch(directory,payload,expected,cancelled)
        if action=='snapshot':return snapshot(directory)
    records(directory)  # Every action remains bound to the installed edition.
    if action == 'hub':
        return hub_dispatch(directory, payload, expected, cancelled)
    if action == 'snapshot':return snapshot(directory)
    if action == 'launch_codex':
        _,build,manifest=records(directory)
        from official_client_host import ComposerAlreadyRunningError
        try:__import__('update_progress').launch_official(build,manifest)
        except ComposerAlreadyRunningError:
            raise SettingsError('codex_already_open','请先正常退出 Codex，再从这里启动，以启用外部模型独立发送。') from None
        return {'message':('Codex 已启动，外部模型发送兼容已随本次启动启用。'
                           if manifest.get('composer_compat') else 'Codex 已启动。')}
    if action == 'diagnose':return diagnose(directory,endpoints=payload.get('endpoints') is True)
    if action == 'probe_connection':
        connection=_validate_connection(payload['connection'])
        if not payload.get('key'):return probe_connection(connection,cancelled)
        with tempfile.TemporaryDirectory(prefix='credential-check-',dir=monitor.private_directory(directory/'settings-ui')) as temp:
            path=Path(temp)/'key';private_write(path,payload['key'].encode());connection['key_file']=str(path)
            return probe_connection(connection,cancelled)
    if action == 'compare_models':
        rows = {row['id']:row for row in snapshot(directory)['models']}
        source, target = rows[payload['from']], rows[payload['to']]
        smaller = bool(source['effective'] and target['effective'] and target['effective'] < source['effective'])
        return {'smaller':smaller,'from':source['effective'],'to':target['effective'],
            'message':'目标模型窗口更小，长对话可能需要先压缩；历史恢复使用目标模型的额度。' if smaller else '目标模型的窗口未缩小。',
            'tools':target['tools'],'images':target['images'],'reasoning':target['reasoning']}
    if action == 'show_progress':
        return {'message':'更新进度已整合到更新页。','progress':hub_progress(directory)}
    if action == 'update_action':
        from update_progress import action as update_action
        value=hub_progress(directory)
        require(value and value['id']==payload.get('id'),'更新状态已改变，请刷新后重试。')
        require(payload.get('command') in ('start','launch','cancel'),'未知更新操作。')
        return update_action(directory,value['id'],payload['command'],payload.get('phase'))
    if action in ('check_updates','retry_update','sync_models'):
        monitor.request_update(directory,sync_now=action!='check_updates',code_now=action!='sync_models',retry=action=='retry_update')
        monitor.wake_monitor(directory)
        return {'message':'模型同步已排队。' if action=='sync_models' else '检查已排队；可继续使用 Codex。'}
    if action in ('cleanup_preview','cleanup'):
        from updater import prune_update_cache
        return prune_update_cache(directory,dry_run=action=='cleanup_preview')
    settings, build, manifest = records(directory)
    if action == 'cancel_recovery':
        identifier = payload.get('id','')
        require(re.fullmatch(r'[a-f0-9]{32}',identifier),'无效的恢复任务。')
        root = Path(settings['codex_home'])/'model-adapter-operations'
        task = monitor.load_json(root/(identifier+'.json'),{})
        require(task.get('id')==identifier and task.get('phase') not in ('completed','failed','cancelled'), '该恢复任务已结束。')
        monitor.atomic_json(root/(identifier+'.cancel'),{'id':identifier})
        return {'message':'已请求取消历史恢复，对话记录会保留。'}
    if action in ('recover','restore'):
        require(payload.get('confirmed') is True,'请先确认恢复操作。')
        require(not p.active_app_processes(Path(settings['app']),read_only_source=True), '请先正常退出 Codex，再执行恢复。','app_running')
        if action=='restore':
            return p.uninstall(SimpleNamespace(state_dir=directory,settings_revision=expected))
        else:
            with edit_lock(directory,expected,cancelled):
                from official_client_install import recover
                from update_worker import recovery_target
                result=recover(recovery_target(directory,build))
                if result.get('status')=='no_transaction':return {'message':'没有需要恢复的安装事务。'}
        return {'message':'恢复操作已完成。'}
    changed_models = False
    with edit_lock(directory,expected,cancelled):
        settings, build, manifest = records(directory)
        document = connection_document(directory,manifest)
        if action in ('save_connection','remove_connection'):
            if action=='save_connection':
                connection = _validate_connection(payload['connection'])
                identifier = connection['id']
            else:identifier = payload['id']
            document['locals'] = [row for row in document.get('locals',[]) if row['id']!=identifier]
            key_path=None;previous_key=None
            if action=='save_connection':
                if payload.get('key'):
                    key=payload['key'];require(isinstance(key,str) and 0<len(key)<=8192 and not any(ord(c)<33 for c in key),'API Key 格式不正确。')
                    key_path=directory/'credentials'/(identifier+'.key')
                    require(not key_path.is_symlink(),'凭证路径无效。')
                    previous_key=key_path.read_bytes() if key_path.exists() else None
                    private_write(key_path,key.encode());connection['key_file']=str(key_path)
                document['locals'].append(connection)
            try:monitor.atomic_json(directory/'connections.json',document)
            except Exception:
                if key_path is not None:
                    if previous_key is None:key_path.unlink(missing_ok=True)
                    else:private_write(key_path,previous_key)
                raise
            changed_models=True
        elif action=='save_proxy':
            from proxy_support import validate
            value = payload.get('proxy')
            if value is None and document.get('proxy'):
                document['disabled_proxy'] = copy.deepcopy(document['proxy'])
            elif value is not None:
                document.pop('disabled_proxy',None)
            document['proxy'] = validate(value) if value is not None else None
            require(type(payload.get('auto_add',True)) is bool,'自动加入设置无效。')
            document['proxy_auto_add'] = payload.get('auto_add',True)
            document['proxy_selection_id'] = uuid.uuid4().hex
            monitor.atomic_json(directory/'connections.json',document);changed_models=True
        elif action=='save_deepseek':
            enabled = payload.get('enabled')
            require(type(enabled) is bool,'请选择是否启用 DeepSeek。')
            secret = payload.get('key')
            if secret:
                require(isinstance(secret,str) and len(secret)<=2560 and not any(ord(c)<33 for c in secret),'API Key 格式不正确。')
                save_deepseek_key(settings,manifest,secret)
            document['deepseek_enabled'] = enabled
            monitor.atomic_json(directory/'connections.json',document);changed_models=True
        elif action=='save_model':
            import model_sync
            models = {row['id']:row for row in _models(directory,build,manifest)}
            model = payload['id'];require(model in models,'模型已变更，请刷新列表。')
            value = model_sync.validate_preferences(monitor.load_json(directory/'model-preferences.json',{}))
            hidden = set(value.get('hidden',[]))
            if 'visible' in payload:
                require(type(payload['visible']) is bool,'显示设置无效。')
                hidden.discard(model) if payload['visible'] else hidden.add(model)
            value['hidden'] = sorted(hidden)
            for field, key in [('context','context_windows'),('compact','compact_limits')]:
                if field not in payload:continue
                number=payload[field];maximum=models[model]['maximum']
                require(number is None or type(number) is int and maximum and 0<number<=maximum,'窗口必须为正整数且不超过模型最大值。')
                value.setdefault(key,{})[model]=number
            requested=value.get('context_windows',{}).get(model) or models[model]['maximum']
            compact=value.get('compact_limits',{}).get(model)
            require(compact is None or requested and compact<=requested,'压缩阈值不能超过使用窗口。')
            monitor.atomic_json(directory/'model-preferences.json',model_sync.validate_preferences(value));changed_models=True
        elif action=='save_options':
            require(set(payload)<= {'auto_update','model_sync','interval','progress_ui'},'存在未知设置。')
            for key,value in payload.items():
                require(type(value) is int and 60<=value<=86400 if key=='interval' else type(value) is bool,'检查周期或开关格式无效。')
            previous=dict(settings);settings.update(payload);monitor.atomic_json(directory/'settings.json',settings)
            try:
                if settings['interval']!=previous['interval']:monitor.Scheduler(directory).enable(settings['interval'])
            except Exception:
                monitor.atomic_json(directory/'settings.json',previous)
                raise
        elif action=='save_images':
            from cpa_images import configure
            require(type(payload.get('enabled')) is bool,'图片工具开关无效。')
            proxy=document.get('proxy') or {}
            configure(SimpleNamespace(codex_home=Path(settings['codex_home']),disable=not payload['enabled'],
                proxy_config=payload.get('config') or proxy.get('config'),
                proxy_base_url=payload.get('url') or proxy.get('base_url','http://127.0.0.1:8317/v1'),
                script=directory/'runtime/cpa_images.py'))
        else:raise SettingsError('unknown_action','未知操作。')
    if changed_models:_queue(directory)
    return {'message':'已保存；模型变更将在验证通过、Codex 退出后应用。' if changed_models else '设置已保存。'}


def save_deepseek_key(settings, manifest, secret):
    service = manifest.get('keychain_service') or settings.get('keychain_service') or p.KEYCHAIN_SERVICE
    account = manifest.get('keychain_account') or settings.get('keychain_account') or __import__('getpass').getuser()
    if sys.platform=='win32':
        from windows_support import credential_set
        credential_set(service,account,secret)
        return
    require(sys.platform=='darwin','此系统不支持凭证保存。')
    import ctypes as c
    cf=c.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    sec=c.CDLL('/System/Library/Frameworks/Security.framework/Security')
    cf.CFStringCreateWithCString.argtypes=[c.c_void_p,c.c_char_p,c.c_uint32];cf.CFStringCreateWithCString.restype=c.c_void_p
    cf.CFDataCreate.argtypes=[c.c_void_p,c.c_void_p,c.c_long];cf.CFDataCreate.restype=c.c_void_p
    cf.CFDictionaryCreateMutable.argtypes=[c.c_void_p,c.c_long,c.c_void_p,c.c_void_p];cf.CFDictionaryCreateMutable.restype=c.c_void_p
    cf.CFDictionarySetValue.argtypes=[c.c_void_p,c.c_void_p,c.c_void_p];cf.CFRelease.argtypes=[c.c_void_p]
    sec.SecItemUpdate.argtypes=[c.c_void_p,c.c_void_p];sec.SecItemUpdate.restype=c.c_int32
    sec.SecItemAdd.argtypes=[c.c_void_p,c.c_void_p];sec.SecItemAdd.restype=c.c_int32
    keys=c.addressof(c.c_char.in_dll(cf,'kCFTypeDictionaryKeyCallBacks'))
    values=c.addressof(c.c_char.in_dll(cf,'kCFTypeDictionaryValueCallBacks'))
    allocated=[]
    def const(name):return c.c_void_p.in_dll(sec,name).value
    def string(value):
        result=cf.CFStringCreateWithCString(None,value.encode(),0x08000100);allocated.append(result);return result
    def dictionary():
        result=cf.CFDictionaryCreateMutable(None,0,keys,values);allocated.append(result);return result
    try:
        query=dictionary();attrs=dictionary()
        cf.CFDictionarySetValue(query,const('kSecClass'),const('kSecClassGenericPassword'))
        cf.CFDictionarySetValue(query,const('kSecAttrService'),string(service))
        cf.CFDictionarySetValue(query,const('kSecAttrAccount'),string(account))
        raw=secret.encode();data=cf.CFDataCreate(None,c.c_char_p(raw),len(raw));allocated.append(data)
        cf.CFDictionarySetValue(attrs,const('kSecValueData'),data)
        status=sec.SecItemUpdate(query,attrs)
        if status==-25300:
            cf.CFDictionarySetValue(query,const('kSecValueData'),data);status=sec.SecItemAdd(query,None)
        require(status==0,'系统未能保存凭证，请确认钥匙串可用后重试。','credential_save_failed')
    finally:
        for value in reversed(allocated):
            if value:cf.CFRelease(value)


def run_request(directory, identifier):
    require(re.fullmatch(r'[a-f0-9]{32}',identifier),'无效的操作编号。')
    jobs=monitor.private_directory(Path(directory)/'settings-ui/jobs')
    request=jobs/(identifier+'.request.json');result=jobs/(identifier+'.result.json')
    cancelled=lambda:(jobs/(identifier+'.cancel')).exists()
    def progress(value):
        message=value.get('message','正在处理…')
        if type(value.get('completed')) is int and type(value.get('total')) is int:
            message+=f" · {value['completed']}/{value['total']} 项"
        monitor.atomic_json(result,{'status':'running','message':message})
    cancelled.progress=progress
    progress({'message':'正在处理，可继续查看其他设置。'})
    try:
        require(request.is_file() and not request.is_symlink() and request.stat().st_size<=131072,'无效的设置请求。')
        try:value=json.loads(request.read_text(encoding='utf-8'))
        finally:request.unlink(missing_ok=True)  # Remove password input even if JSON was truncated.
        require(isinstance(value,dict),'无效的设置请求。')
        require(value.get('action') in ACTIONS,'未知设置操作。')
        data=perform(directory,value['action'],value.get('payload'),value.get('revision'),cancelled)
        response={'status':'completed','data':data}
        if value['action'] not in ('snapshot','probe_connection','diagnose','compare_models','restore','hub'):
            response['snapshot']=snapshot(directory)
    except Exception as error:response={'status':'failed','error':public_error(error)}
    finally:
        if request.is_file() and not request.is_symlink():request.unlink(missing_ok=True)
    monitor.atomic_json(result,response)
    return response


HOST_VIEW = 'whale-v1'


def queue_ui_entry(directory, *, page=None, notify=False):
    """Keep one entry request while an older settings host finishes its edits."""
    ui=monitor.private_directory(Path(directory)/'settings-ui')
    with monitor.state_lock(ui/'entry-control') as locked:
        require(locked,'设置入口正在处理，请稍后重试。','busy')
        previous=monitor.load_json(ui/'entry.json',{})
        value={'page':page or previous.get('page'),
               'notify':bool(notify and previous.get('notify',True))}
        monitor.atomic_json(ui/'entry.json',value)
        return value


def show(directory, *, self_test=False, loop_test=False, page=None, notify=False):
    directory=Path(directory).resolve();ui=monitor.private_directory(directory/'settings-ui')
    settings_access(ui)
    monitor.private_directory(ui/'jobs')
    if not self_test:
        from update_progress import _alive
        host=monitor.load_json(ui/'host.json',{})
        if host.get('view')==HOST_VIEW and host.get('edition')==p.EDITION and _alive(host.get('pid')):
            monitor.atomic_json(ui/'raise.json',{'time':uuid.uuid4().hex,'page':page,'notify':notify})
            return subprocess.CompletedProcess([],0,b'',b'')
        queue_ui_entry(directory,page=page,notify=notify)
    # A single windowless launcher waits for a legacy window to release its lock.
    # Do not kill an editor or expose an update badge over an unsaved old form.
    with monitor.state_lock(ui/'launch-control') as launching:
        if not launching:
            if self_test:raise SettingsError('window_busy','设置窗口正在运行，请先关闭窗口再检查。')
            return subprocess.CompletedProcess([],0,b'',b'')
        while True:
            with monitor.state_lock(ui) as locked:
                if locked:
                    entry=monitor.load_json(ui/'entry.json',{}) if not self_test else {}
                    (ui/'entry.json').unlink(missing_ok=True)
                    return _show_locked(directory,self_test=self_test,loop_test=loop_test,
                        page=entry.get('page',page),notify=entry.get('notify',notify))
                if self_test:raise SettingsError('window_busy','设置窗口正在运行，请先关闭窗口再检查。')
            time.sleep(.25)


def settings_access(directory):
    """Pin private UI IPC to the user SID, not Windows' mutable OWNER RIGHTS.

    Elevated and ordinary launches can create files with different owners even
    for the same account. Inherited explicit user access survives those writes.
    """
    if os.name!='nt':return
    directory=Path(directory)
    try:
        if monitor.load_json(directory/'access.json',{}).get('format')==1:return
    except OSError:pass
    from windows_support import grant_current_user
    sid=grant_current_user(directory)
    monitor.atomic_json(directory/'access.json',{'format':1,'sid':sid})



def _show_locked(directory, *, self_test=False, loop_test=False, page=None, notify=False):
    from official_client_host import windowless_python
    ui=Path(directory)/'settings-ui'
    initial=snapshot(directory)
    initial.update(hub=hub_shell(directory),view=hub_view(directory,page or 'models'),entry={'page':('setup' if needs_setup(directory) else page),'notify':notify})
    brand_assets(ui)
    bubble_assets(ui)
    monitor.atomic_json(ui/'view.json',initial)
    runtime=windowless_python(sys.executable)
    if sys.platform=='darwin':
        from update_progress import macos_ui_command
        command=macos_ui_command(ROOT/'settings_macos.js',directory,runtime,ROOT/'management.py')
    elif sys.platform=='win32':
        from windows_support import native_ui_command
        command=native_ui_command(ROOT/'settings_windows.ps1',{'StateDirectory':str(directory),
            'PythonExecutable':str(runtime),'ControllerPath':str(ROOT/'management.py'),
            'SelfTest':self_test,'LoopTest':loop_test})
    else:raise SettingsError('unsupported_platform','设置窗口支持 macOS 和 Windows。')
    if self_test and sys.platform=='darwin':command.append('--self-test')
    if loop_test and sys.platform=='darwin':command.append('--loop-test')
    if self_test and sys.platform=='win32':
        from windows_support import isolated_desktop_options, desktop_process
        import shutil
        command[0]=shutil.which(command[0]) or command[0]
        log_path=ui/'native-selftest.log'
        with isolated_desktop_options() as options, log_path.open('wb') as log:
            child=desktop_process(command,options['desktop'],cwd=directory,stdout=log,
                env={**os.environ,'CODEX_SETTINGS_PRIVATE_DESKTOP':'1'})
            try:code=child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.kill();child.wait(timeout=5)
                raise
        raw=log_path.read_bytes()
        return subprocess.CompletedProcess(command,code,raw if code==0 else b'',raw if code else b'')
    if not self_test:
        return run_host(command,directory,
            **(__import__('windows_support').native_ui_options() if os.name=='nt' else {}))
    return subprocess.run(command,stdin=subprocess.DEVNULL,capture_output=True,timeout=30)


def bubble_assets(directory):
    """Small original UI sounds, synthesized locally without an audio dependency."""
    import io, struct, wave
    rate=24000
    # Damped chirps imitate small air bubbles; encoded level stays soft on both hosts.
    profiles={'open':[(0,.095,850,1600,.15),(.047,.11,1200,2200,.075)],
              'close':[(0,.115,1100,540,.13),(.045,.075,750,450,.05)]}
    output={}
    for name,tones in profiles.items():
        length=max(start+duration for start,duration,*_ in tones)
        samples=[]
        for i in range(int(length*rate)+1):
            t=i/rate;value=0.
            for start,duration,low,high,level in tones:
                u=(t-start)/duration
                if 0<u<1:
                    phase=2*math.pi*duration*(low*u+(high-low)*u*u/2)
                    envelope=(1-math.exp(-u*55))*math.exp(-u*5)*(1-u)**2
                    value+=level*envelope*(math.sin(phase)+.12*math.sin(phase*2))
            samples.append(round(max(-.25,min(.25,value))*32767))
        stream=io.BytesIO()
        with wave.open(stream,'wb') as sound:
            sound.setparams((1,2,rate,0,'NONE','not compressed'))
            sound.writeframes(struct.pack('<'+'h'*len(samples),*samples))
        path=Path(directory)/('bubble-'+name+'.wav');data=stream.getvalue()
        if not path.is_file() or path.read_bytes()!=data:private_write(path,data)
        output[name]=path
    return output


def brand_assets(directory):
    from update_worker import brand_assets as render
    return render(directory, ROOT/'brand.svg')


def ensure_launcher(directory):
    """Create one per-user entry; no change to the official Codex shortcut/app."""
    from official_client_host import windowless_python
    directory=Path(directory).resolve()
    settings, _, manifest=records(directory)
    runtime=directory/'runtime'
    require((runtime/'management.py').is_file(), '设置组件尚未安装。')
    ui=monitor.private_directory(directory/'settings-ui')
    owned=monitor.load_json(ui/'launcher.json',{})
    python=str(windowless_python(settings.get('python_path') or sys.executable))
    command=[python,str(runtime/'management.py'),'launch','--state-dir',str(directory)]
    label='Codex 模型设置'
    brand_digest=p.file_sha(ROOT/'brand.svg')
    if sys.platform=='darwin':
        destination=Path.home()/'Applications'/(label+'.app')
        bundle='com.limyum.codex-model-settings.'+p.EDITION
        files={'Contents/MacOS/settings':('#!/bin/sh\nexec '+shlex.join(command)+'\n').encode(),
            'Contents/Info.plist':plistlib.dumps({'CFBundleIdentifier':bundle,'CFBundleName':label,
                'CFBundleDisplayName':label,'CFBundleExecutable':'settings','CFBundlePackageType':'APPL',
                'LSUIElement':True,'NSHighResolutionCapable':True,
                'CFBundleVersion':'2.'+str(int(brand_digest[:6],16)),
                'CFBundleIconFile':'model-patch-'+brand_digest[:12]+'.icns',
                'LSArchitecturePriority':['arm64','x86_64']})}
    elif sys.platform=='win32':
        destination=Path(os.environ['APPDATA'])/'Microsoft/Windows/Start Menu/Programs'/(label+'.lnk')
        files=None
    else:raise SettingsError('unsupported_platform','设置入口支持 macOS 和 Windows。')
    require(not destination.is_symlink(),'设置入口路径已被占用。')
    old=Path(owned['path']) if owned.get('path') else None
    if old and old!=destination and old.exists():
        require(old.parent==destination.parent and old.stem in ('Codex 模型设置（DS 版）','Codex 模型设置（扩展版）')
                and not old.is_symlink() and not destination.exists(),'请核对已有设置入口，文件已保留。')
        require(bool(owned.get('files')),'缺少旧入口的所有权记录。')
        for name,digest in owned['files'].items():
            path=old/name if old.is_dir() else old
            require(path.is_file() and not path.is_symlink() and p.file_sha(path)==digest,'旧设置入口已被修改，文件已保留。')
        old.rename(destination)
        owned['path']=str(destination)
        monitor.atomic_json(ui/'launcher.json',owned)
    if destination.exists():
        require(owned.get('path')==str(destination),'已有同名入口，请保留并手动调整名称。')
        for name,digest in owned.get('files',{}).items():
            path=destination/name if destination.is_dir() else destination
            require(path.is_file() and not path.is_symlink() and p.file_sha(path)==digest,'设置入口已被用户修改，已保留。')
        if owned.get('command')==command and owned.get('brand')==brand_digest:return str(destination)
    _,brand=brand_assets(ui)
    if files is not None:files['Contents/Resources/model-patch-'+brand_digest[:12]+'.icns']=brand['icns'].read_bytes()
    destination.parent.mkdir(parents=True,exist_ok=True)
    if files is not None:
        for name,raw in files.items():
            target=destination/name;target.parent.mkdir(parents=True,exist_ok=True)
            private_write(target,raw)
            target.chmod(0o755 if name.endswith('/settings') else 0o644)
        for name in owned.get('files',{}):
            if name not in files:
                (destination/name).unlink(missing_ok=True)
        hashes={name:p.sha(raw) for name,raw in files.items()}
        # Change the icon URL as well as its bytes; Finder caches icons by URL.
        os.utime(destination,None)
        registrar=Path('/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister')
        if registrar.is_file():
            subprocess.run([str(registrar),'-f',str(destination)],capture_output=True,timeout=10)
    else:
        from official_client_shortcut import write_link, _atomic
        staged=ui/'settings-entry.lnk'
        icon=ui/('brand-'+brand_digest[:12]+'.ico')
        private_write(icon,brand['ico'].read_bytes())
        write_link(staged,{'target':python,'arguments':subprocess.list2cmdline(command[1:]),
            'working_directory':str(directory),'icon':str(icon)+',0'})
        _atomic(destination,staged.read_bytes());staged.unlink()
        ctypes.windll.shell32.SHChangeNotify(0x00002000,0x0005,str(destination),None)
        hashes={'':p.file_sha(destination)}
    monitor.atomic_json(ui/'launcher.json',{'format':1,'edition':p.EDITION,'path':str(destination),'command':command,'files':hashes,'brand':brand_digest})
    return str(destination)


def maintenance(directory):
    """Reuse an idle monitor tick for bounded cleanup and a silent UI entry."""
    directory=Path(directory)
    if not (directory/'runtime/management.py').is_file():return
    ui=monitor.private_directory(directory/'settings-ui')
    previous=monitor.load_json(ui/'maintenance.json',{})
    fingerprint=p.file_sha(ROOT/'management.py')+p.file_sha(ROOT/'brand.svg')
    if previous.get('source')==fingerprint and time.time()-previous.get('checked_at',0)<86400:return
    with monitor.state_lock(ui/'maintenance') as locked:
        if not locked:return
        outcome={'checked_at':time.time(),'source':fingerprint}
        try:outcome['launcher']=ensure_launcher(directory)
        except Exception as error:outcome['launcher_error']=public_error(error)['code']
        try:
            from updater import prune_update_cache
            outcome['cleanup']=prune_update_cache(directory,dry_run=False)
        except Exception as error:outcome['cleanup_error']=public_error(error)['code']
        monitor.atomic_json(ui/'maintenance.json',outcome)


def remove_launcher(directory):
    """Remove only the exact generated entry, preserving any independent edits."""
    document=Path(directory)/'settings-ui/launcher.json'
    owned=monitor.load_json(document,{})
    if owned.get('format')!=1 or owned.get('edition')!=p.EDITION:return False
    path=Path(owned['path'])
    if path.is_symlink():return False
    if not path.exists():document.unlink(missing_ok=True);return True
    files=owned.get('files',{})
    if not files:return False
    if path.is_dir():
        existing={str(f.relative_to(path)) for f in path.rglob('*') if f.is_file() or f.is_symlink()}
        if existing!=set(files):return False
    for name,digest in files.items():
        item=path/name if path.is_dir() else path
        if not item.is_file() or item.is_symlink() or p.file_sha(item)!=digest:return False
    if path.is_dir():__import__('shutil').rmtree(path)
    else:path.unlink()
    document.unlink(missing_ok=True)
    return True


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('launch','show','close','execute','diagnose'))
    parser.add_argument('--state-dir',type=Path)
    parser.add_argument('--request')
    parser.add_argument('--page',choices=('models','connections','updates','tools','diagnostics','preferences'))
    parser.add_argument('--notify-update',action='store_true')
    parser.add_argument('--self-test',action='store_true')
    parser.add_argument('--loop-test',action='store_true')
    args=parser.parse_args(argv);directory=find_directory(args.state_dir)
    try:
        if args.action=='close':
            monitor.atomic_json(directory/'settings-ui/raise.json',{'time':uuid.uuid4().hex,'quit':True})
        elif args.action=='launch':
            # A launcher must exit promptly: LaunchServices otherwise sends a
            # reopen AppleEvent to a Python process with no application delegate.
            from official_client_host import windowless_python
            command=[str(windowless_python(sys.executable)),str(ROOT/'management.py'),
                     'show','--state-dir',str(directory)]
            if args.page:command+=['--page',args.page]
            options=__import__('windows_support').background_options() if os.name=='nt' else {'start_new_session':True}
            subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,**options)
        elif args.action=='execute':run_request(directory,args.request)
        elif args.action=='diagnose':print(json.dumps(diagnose(directory,endpoints=True),ensure_ascii=False,indent=2))
        else:
            require(not args.loop_test or args.self_test,'窗口交互检查需要独立测试模式。')
            result=show(directory,self_test=args.self_test,loop_test=args.loop_test,page=args.page,notify=args.notify_update)
            if args.self_test:print(result.stdout.decode('utf-8',errors='replace'))
            return result.returncode
        return 0
    except Exception as error:
        print(json.dumps(public_error(error),ensure_ascii=False),file=sys.stderr);return 1




# Shared native floating settings presentation and shortcut lifecycle.
CATEGORIES = [
    ('models', '模型', '上下文与压缩', 'M6 4H18V20H6Z M9 8H15 M9 12H15 M9 16H12'),
    ('connections', '连接', '云端与本地模型', 'M9 8L7 6C4 3 0 7 3 10L6 13 M15 11L18 14C21 17 17 21 14 18L12 16 M8 14L15 7'),
    ('updates', '更新', '补丁更新与模型同步', 'M5 8C8 2 17 3 20 9 M20 4V9H15 M19 16C16 22 7 21 4 15 M4 20V15H9'),
    ('tools', '工具', '图片工具与历史恢复', 'M4 7H20V20H4Z M8 7V4H16V7 M4 12H20 M10 11V14H14V11'),
    ('diagnostics', '诊断', '状态检查与故障排查', 'M3 12H7L10 5L14 19L17 12H21'),
    ('preferences', '偏好', '外观、快捷键与维护', 'M5 4V20 M12 4V20 M19 4V20 M2 8H8 M9 16H15 M16 10H22'),
]
PALETTES = {
    'light': {'surface':'#FFFFFF','field':'#F6F8FB','text':'#28344A','muted':'#8390A5',
              'alert':'#FDEBED','alert_line':'#F0CBD0','alert_ink':'#B85D6B','line':'#E7ECF3','accent':'#4D6BFE','hover':'#EDF2FF','danger':'#C64861'},
    'dark': {'surface':'#202A3C','field':'#151E2F','text':'#EEF3FF','muted':'#9AAAC5',
             'alert':'#543540','alert_line':'#805564','alert_ink':'#F2B2BD','line':'#354159','accent':'#8196FF','hover':'#344366','danger':'#FF9DB0'},
}
DEFAULTS = {'theme':'light', 'motion':'system', 'hotkey':True, 'sound':True}
# One layout in screen coordinates, shared by both native renderers. Every drop
# stays above the whale; the small asymmetries suggest spray rather than a wheel.
HUB_LAYOUT = {
    # Transparent headroom absorbs the spring/arc overshoot and hover shadow.
    # All visible y coordinates move together, preserving their screen spacing.
    'width':148, 'height':164, 'bubble':64, 'closed_width':96, 'closed_whale_x':32, 'whale_x':42, 'whale_y':100,
    'spout_x':66, 'spout_y':116,
    'badge':{'offset_x':-20,'offset_y':10,'size':28,'angle':-65,'shape':0,
             'icon_x':-1.25,'icon_y':0.5},
    'card_width':320, 'card_height':420,
    'dock':{'size':32, 'threshold':28, 'leave_ms':100, 'poll_ms':60, 'duration_ms':180,
            'angles':{'left':90,'right':-90,'top':180,'bottom':0}},
    'morph':{'duration_ms':340,'close_ms':250,'radius':16,'wave':0.018},
    'drops':[
        {'x':38,'y':46,'size':38,'angle':-18,'shape':0},
        {'x':78,'y':42,'size':38,'angle':-3,'shape':1},
        {'x':118,'y':50,'size':36,'angle':18,'shape':2},
        {'x':26,'y':85,'size':36,'angle':-28,'shape':1},
        {'x':66,'y':81,'size':38,'angle':-6,'shape':2},
        {'x':106,'y':89,'size':36,'angle':24,'shape':0},
    ],
    'shapes':[
        'M12 23 C10 20 3 17 2 11 C1 5 6 1 12 1 C19 1 23 6 22 12 C21 18 16 19 12 23 Z',
        'M12 23 C9 20 2 17 2 10 C2 4 8 1 14 2 C21 3 23 8 21 14 C19 19 15 19 12 23 Z',
        'M12 23 C10 19 3 18 2 12 C0 6 5 1 11 1 C18 0 23 5 22 11 C22 17 15 20 12 23 Z',
    ],
    # Filled liquid silhouettes share a (30, 42) source point. They stay close
    # to the spout and dissolve into particles instead of connecting to buttons.
    'water':[
        'M30 42 C28 32 24 25 13 20 C5 13 1 20 9 24 C17 26 22 34 26 42 Z',
        'M28 42 C25 30 26 18 30 11 C34 5 38 10 35 16 C32 26 33 35 32 42 Z',
        'M29 42 C30 30 37 20 48 15 C56 11 57 18 50 20 C40 23 35 33 33 42 Z',
    ],
    'particles':[[-22,-25,1.8],[-12,-39,2.0],[3,-44,1.6],
                 [21,-33,2.1],[29,-21,1.7],[10,-28,1.5]],
}

def vector_contours(data):
    """Flatten the small, bounded M/L/C/Z source paths used by our icon."""
    tokens=re.findall(r'[MLCZ]|[-+]?(?:\d*\.?\d+)(?:[eE][-+]?\d+)?',data)
    paths=[];points=[];i=0;command=None;current=(0.,0.)
    while i<len(tokens):
        if tokens[i] in ('M','L','C','Z'):
            command=tokens[i];i+=1
            if command=='Z':
                if points:paths.append(points);current=points[0];points=[]
                continue
        if command in ('M','L'):
            pair=(float(tokens[i]),float(tokens[i+1]));i+=2
            if command=='M' and points:paths.append(points);points=[]
            points.append(pair);current=pair;command='L'
        elif command=='C':
            values=list(map(float,tokens[i:i+6]));i+=6
            a=current;b=values[:2];c=values[2:4];d=values[4:]
            steps=max(4,min(40,int((math.dist(a,b)+math.dist(b,c)+math.dist(c,d))/3)+1))
            for step in range(1,steps+1):
                t=step/steps;u=1-t
                points.append(tuple(u**3*a[k]+3*u*u*t*b[k]+3*u*t*t*c[k]+t**3*d[k] for k in (0,1)))
            current=tuple(d)
        else:raise ValueError('Unsupported icon path')
    if points:paths.append(points)
    return paths


def hub_options(directory):
    value = monitor.load_json(Path(directory)/'settings-ui/options.json', {})
    return {**DEFAULTS, **{k:v for k,v in value.items() if k in DEFAULTS}}


def hub_save_options(directory, value):
    from management import require
    require(set(value) <= set(DEFAULTS), '存在未知的外观选项。')
    require(value.get('theme','system') in ('system','light','dark'), '外观选项无效。')
    require(value.get('motion','system') in ('system','reduced'), '动效选项无效。')
    require(type(value.get('hotkey',True)) is bool, '快捷键选项无效。')
    require(type(value.get('sound',True)) is bool, '音效选项无效。')
    result = {**hub_options(directory), **value}
    monitor.atomic_json(Path(directory)/'settings-ui/options.json', result)
    return result


def hub_shell(directory):
    return {'categories':[{'id':key,'name':name,'description':description,'icon':icon}
                           for key,name,description,icon in CATEGORIES],
            'palettes':PALETTES, 'options':hub_options(directory),
            'shortcut':'⌃⌥D' if sys.platform=='darwin' else 'Ctrl+Alt+D',
            'layout':copy.deepcopy(HUB_LAYOUT),
            'motion':{'duration_ms':300,'close_ms':170,'stagger_ms':10,'hover_ms':90},
            'sound':{'cooldown_ms':140}}



def hub_progress(directory):
    from update_progress import TEXT,Progress
    value=Progress(directory).read()
    if not isinstance(value,dict) or value.get('edition')!=p.EDITION or value.get('phase') not in TEXT:
        return {}
    if not isinstance(value.get('id'),str) or not re.fullmatch(r'[a-f0-9]{32}',value['id']):return {}
    return {k:value[k] for k in ('format','id','edition','version','phase','title','detail',
            'completed','total','unit','percent','can_cancel','can_start','can_launch','phase_started_at') if k in value}


def hub_field(key, label, value='', kind='text', *, choices=None, hint=None):
    row={'type':'field','key':key,'label':label,'value':value if value is not None else '',
         'kind':kind}
    if choices is not None:row['choices']=choices
    if hint:row['hint']=hint
    return row


def hub_item(title, subtitle='', route=None, key=None):
    return {'type':'item','title':title,'subtitle':subtitle,'route':route,'key':key}


def hub_button(title, intent, *, style='secondary', **values):
    return {'title':title,'intent':intent,'style':style,**values}


def hub_view(directory, route='models', key=None, notice=None, *, extra=None):
    """Build one small card from the current authority, not a second settings store."""
    from management import snapshot, require
    if needs_setup(directory):return setup_view(directory,notice or '')
    state=snapshot(directory); opts=hub_options(directory)
    base=route.split('/')[0]
    require(base in {c[0] for c in CATEGORIES}, '未知设置分类。')
    title=next(c[1] for c in CATEGORIES if c[0]==base)
    rows=[];buttons=[];subtitle='';back=base if '/' in route else None
    data={'route':route,'key':key,'revision':state['revision'],'edition':state['edition'],
          'title':title,'subtitle':subtitle,'rows':rows,'buttons':buttons,'back':back,
          'notice':notice or '', 'options':opts}
    if route=='models':
        data['subtitle']='窗口按模型规格同步，手动调小会保留。'
        data['searchable']=True
        for model in state['models']:
            window=f"{model['effective']:,}" if model['effective'] else '未提供'
            rows.append(hub_item(model['name'], f"{window} tokens · "+('已显示' if model['visible'] else '已隐藏'),
                             'models/edit',model['id']))
    elif route=='models/edit':
        model=next((m for m in state['models'] if m['id']==key),None)
        require(model is not None,'模型列表已变更，请重新选择。')
        data['title']=model['name'];data['subtitle']=' / '.join(filter(None,[
            '工具' if model['tools'] else '纯对话', '图片输入' if model['images'] else '',
            '思考：'+', '.join(model['reasoning']) if model['reasoning'] else '']))
        maximum=f"{model['maximum']:,}" if model['maximum'] else '未提供'
        rows += [hub_item('模型最大窗口', maximum+' tokens'),
                 hub_field('context','使用窗口',model['context_override'],'number',hint='留空使用模型最大值'),
                 hub_field('compact','压缩阈值',model['compact_override'],'number',
                           hint='留空沿用 Codex 原生设置' if model['provider']=='GPT' else '留空默认使用窗口的 90%'),
                 hub_field('visible','在模型菜单中显示',model['visible'],'toggle')]
        labels={'user':'用户声明','service':'服务声明','official':'官方规格','inferred':'兼容性推定'}
        sources=model['spec_sources']
        rows.append(hub_item('规格来源',' · '.join(label+'：'+labels.get(sources.get(key),'原生目录' if model['provider']=='GPT' else '未记录') for key,label in [('context_window','窗口'),('input_modalities','图片'),('reasoning_levels','思考'),('supports_tools','工具')])))
        if model['context_override']:rows.append(hub_item('实际窗口','保留用户主动调小的设置'))
        if model['context_conflict']:rows.append({'type':'note','text':'用户声明与服务窗口不同，请核对。当前值和手动设置均已保留。'})
        if model['server_window']:rows.insert(1,hub_item('本地服务窗口',f"{model['server_window']:,} tokens"))
        buttons += [hub_button('恢复默认','reset_model'),hub_button('保存','save_model',style='primary')]
    elif route=='connections':
        data['subtitle']='凭证保留在本机；接入已有服务。'
        rows.append(hub_item('DeepSeek','已启用' if state['deepseek_enabled'] else '按需启用','connections/deepseek'))
        if state['edition'] in p.INSTALL_AFFINITIES:
            rows.append(hub_item('现成代理','已连接' if state['proxy'] else 'EasyCLI / CPA','connections/proxy'))
            for connection in state['connections']:
                rows.append(hub_item(connection.get('name') or connection['id'],
                                 connection['kind']+' · '+str(len(connection.get('models',[])))+' 个型号',
                                 'connections/edit',connection['id']))
            buttons.append(hub_button('添加连接','navigate',route='connections/edit',style='primary'))
    elif route=='connections/deepseek':
        data['title']='DeepSeek'
        rows += [hub_field('enabled','启用 DeepSeek',state['deepseek_enabled'],'toggle'),
                 hub_field('key','API Key','','secret',hint='留空保留已有密钥')]
        buttons.append(hub_button('保存','save_deepseek',style='primary'))
    elif route=='connections/proxy':
        proxy=state['proxy'] or state['disabled_proxy'] or {}
        data['title']='现成代理'
        rows += [hub_field('url','接口地址',proxy.get('base_url','http://127.0.0.1:8317/v1')),
                 hub_field('config','配置文件',proxy.get('config',''),'file'),
                 hub_field('models','使用型号',', '.join(proxy.get('models',[])),hint='多个型号用逗号分隔'),
                 hub_field('auto','加入现有系列的新版本',state['proxy_auto_add'],'toggle')]
        buttons += [hub_button('停用','disable_proxy'),hub_button('保存','save_proxy',style='primary')]
    elif route=='connections/edit':
        connection=next((c for c in state['connections'] if c['id']==key),None) if key else None
        require(not key or connection is not None,'连接已变更，请重新选择。')
        c=connection or {}
        data['title']=c.get('name') or '添加连接'
        rows += [hub_field('name','连接名称',c.get('name','')),
                 hub_field('kind','服务类型',c.get('kind','openai'),'choice',choices=[
                     ['openai','通用兼容接口'],['ollama','Ollama'],['lmstudio','LM Studio']]),
                 hub_field('url','接口地址',c.get('base_url',''),hint='例如 http://127.0.0.1:11434/v1'),
                 hub_field('wire','接口协议',c.get('wire_api','chat_completions'),'choice',
                       choices=[['chat_completions','Chat Completions'],['responses','Responses'],['anthropic_messages','Anthropic Messages']]),
                 hub_field('key','API Key','','secret',hint='本地接口可留空；已有密钥保持不变'),
                 hub_field('models','使用型号',', '.join(c.get('models',[])),hint='留空检测所有型号'),
                 hub_field('respect','沿用服务端窗口',c.get('respect_server_context',False),'toggle'),
                 hub_field('keyfile','凭证文件',c.get('key_file',''),'file'),
                 hub_field('reasoning','支持思考参数',c.get('supports_reasoning_effort',False),'toggle')]
        if connection:rows.append(hub_item('补充型号规格','最大窗口、工具与思考档位','connections/spec',key))
        buttons += [hub_button('检测型号','probe_connection'),hub_button('保存','save_connection',style='primary')]
        if connection:buttons.insert(0,hub_button('移除','confirm_remove',style='danger'))
    elif route=='connections/spec':
        data['title']='补充型号规格';data['back']='connections/edit'
        rows += [hub_field('model','准确型号 ID'),hub_field('maximum','最大窗口','','number'),hub_field('output','最大输出','','number',hint='Anthropic 必填；按服务规格填写'),
                 hub_field('levels','思考档位',hint='例如 low, medium, high'),
                 hub_field('tools','支持工具调用',True,'toggle')]
        buttons.append(hub_button('保存','save_spec',style='primary'))
    elif route=='connections/discover':
        require(isinstance(extra,dict) and isinstance(extra.get('available'),list),'检测结果已过期。')
        data['title']='选择型号';data['subtitle']='勾选要加入的模型；缺少规格时补充最大窗口。'
        data['draft']=extra['draft'];data['searchable']=True;data['back']='connections/edit'
        for index,model in enumerate(extra['available']):
            rows.append(hub_field('model_'+str(index),model,model in extra['selected'],'toggle'))
            if model not in extra['specs']:
                rows.append(hub_field('window_'+str(index),'最大窗口 · '+model,'','number'))
                if extra['draft']['connection'].get('wire_api')=='anthropic_messages':rows.append(hub_field('output_'+str(index),'最大输出 · '+model,'','number'))
        data['available']=extra['available'];data['specs']=extra['specs']
        buttons += [hub_button('全选','select_all'),hub_button('清空','clear_all'),hub_button('保存所选','save_discovered',style='primary')]
    elif route=='updates':
        data['subtitle']=state['edition_label']+' · 当前 '+str(state['version'])
        from update_worker import recovery_summary
        recovery=recovery_summary(directory,records(directory)[1])
        if recovery.get('candidate_version'):rows.append(hub_item('待安装版本',recovery['candidate_version']))
        rows += [{'type':'update','progress':state['progress']},
                 hub_item('安装与恢复',recovery['message'],'updates/maintenance'),
                 hub_item('自动更新与提醒','检查周期、模型同步、水滴提醒','updates/options'),
                 hub_item('诊断报告','查看失败阶段和近期请求','diagnostics')]
        buttons += [hub_button('同步模型','sync_models'),hub_button('检查更新','check_updates',style='primary')]
    elif route=='updates/options':
        data['title']='更新设置';data['subtitle']='统一更新，保留已有模型和个人设置。'
        rows += [hub_field('auto_update','自动更新补丁',state['options']['auto_update'],'toggle'),
                 hub_field('model_sync','同步模型与规格',state['options']['model_sync'],'toggle'),
                 hub_field('progress_ui','显示鲸鱼更新提醒',state['options']['progress_ui'],'toggle'),
                 hub_field('interval','检查周期（秒）',state['options']['interval'],'number'),
                 {'type':'note','text':'有更新时显示小水滴提示；开始更新后会正常关闭 Codex。'}]
        buttons.append(hub_button('保存','save_options',style='primary'))
    elif route=='tools':
        rows += [hub_item('模型能力比较','切换前查看窗口、图片与工具支持','tools/compare'),
                 hub_item('图片工具','已启用' if state['images_enabled'] else '按需启用','tools/images'),
                 hub_item('历史恢复','查看进度与取消恢复任务','tools/recovery')]
    elif route=='tools/images':
        data['title']='图片工具';proxy=state['proxy'] or {}
        rows += [hub_field('enabled','启用图片工具',state['images_enabled'],'toggle'),
                 hub_field('config','现成代理配置',proxy.get('config',''),'file'),
                 hub_field('url','接口地址',proxy.get('base_url','http://127.0.0.1:8317/v1'))]
        buttons.append(hub_button('保存','save_images',style='primary'))
    elif route=='tools/compare':
        data['title']='比较模型'
        choices=[[m['id'],m['name']] for m in state['models']]
        initial=choices[0][0] if choices else ''
        rows += [hub_field('from','当前模型',initial,'choice',choices=choices),
                 hub_field('to','目标模型',initial,'choice',choices=choices)]
        buttons.append(hub_button('比较','compare_models',style='primary'))
    elif route=='tools/recovery':
        data['title']='历史恢复'
        for job in state['recovery']:
            rows.append(hub_item(job['model'],job['phase']+f" · {job.get('part',0)}/{job.get('total',0)}"))
            if job['phase'] not in ('completed','failed','cancelled'):
                rows.append({'type':'action',**hub_button('取消此恢复','cancel_recovery',id=job['id'])})
        if not rows:rows.append({'type':'note','text':'当前没有历史恢复任务。'})
        buttons.append(hub_button('刷新','refresh'))
    elif route=='diagnostics':
        data['subtitle']=state['edition_label']+' · '+str(state['version'])
        rows += [hub_item('补丁状态',state['status']),hub_item('可用模型',str(len(state['models']))+' 个'),hub_item('Codex 版本',str(state['app_version'] or '未记录')),
                 {'type':'note','text':'报告不包含密钥、聊天内容和私人文件路径。'}]
        from official_client_host import request_diagnostics
        _,build,_=records(directory)
        recent=request_diagnostics(monitor.load_json(build/'host-settings.json',{}))
        phases={'preparing':'准备输入','compacting':'恢复或压缩历史','waiting_upstream':'等待服务','receiving':'已连接服务','streaming':'正在接收','completed':'模型已完成','transferred':'传输结束','failed':'失败','cancelled':'已取消'}
        for request in recent[:12]:
            rows.append(hub_item(str(request.get('model','未知型号')),('官方服务' if request.get('target')=='official' else str(request.get('service') or '所选外部服务'))+' · '+phases.get(request.get('phase'),'未知阶段')+' · '+str(request.get('elapsed_ms',0))+' ms'+(' · HTTP '+str(request['status']) if request.get('status') else '')))
        if extra:rows.append({'type':'report','text':extra.get('text','')})
        buttons += [hub_button('运行诊断','diagnose',style='primary')]
        if extra:buttons.append(hub_button('复制报告','copy',text=extra.get('text','')))
    elif route=='preferences':
        data['subtitle']='右键进入设置 · 隐藏后随时唤醒。'
        rows += [{'type':'action',**hub_button('启动 Codex','launch_codex',style='primary')},
                 {'type':'note','text':'GPT 额度耗尽时，请先正常退出 Codex，再从这里启动，以启用外部模型独立发送。'},
                 hub_field('theme','外观',opts['theme'],'choice',
                       choices=[['system','跟随系统'],['light','浅色'],['dark','深色']]),
                 hub_field('motion','动效',opts['motion'],'choice',choices=[['system','跟随系统'],['reduced','减少动效']]),
                 hub_field('sound','气泡音效',opts['sound'],'toggle'),
                 hub_field('hotkey','快捷键呼出 · '+hub_shell(directory)['shortcut'],opts['hotkey'],'toggle'),
                 {'type':'note','text':'隐藏后可按快捷键，或再次点击 App / 开始菜单中的「Codex 模型设置」。退出小窗会释放快捷键。'},
                 hub_item('版本与状态','当前补丁、诊断与脱敏报告','diagnostics')]
        buttons += [hub_button('隐藏','hide'),hub_button('退出小窗','quit'),hub_button('保存','save_ui_options',style='primary')]
    elif route in ('preferences/maintenance','updates/maintenance'):
        data['title']='维护与恢复'
        rows += [{'type':'action',**hub_button('重试补丁更新','retry_update')},
                 {'type':'action',**hub_button('清理过期缓存','cleanup_preview')},
                 {'type':'action',**hub_button('恢复中断的安装','confirm_recover')},
                 {'type':'action',**hub_button('卸载并恢复原生 Codex','confirm_restore',style='danger')},
                 {'type':'note','text':'恢复操作会保留官方应用、登录和聊天记录。'}]
    else:require(False,'未知设置页面。')
    data['footer']=footer_layout(buttons,bool(data.get('back')))
    return data


def footer_layout(buttons, back=False):
    """Shared bounded footer rows; long labels never compete with Back."""
    count=len(buttons)+(1 if back else 0)
    columns=2 if count>3 or any(len(b['title'])>6 for b in buttons) else max(1,count)
    rows=math.ceil(count/columns)
    return {'columns':columns,'rows':rows,'top':410-rows*36,
            'width':(292-(columns-1)*6)/columns,'height':30,'gap':6}


def hub_number(value):
    from management import require
    if value is None or value=='':return None
    require(not isinstance(value,bool) and re.fullmatch(r'[1-9][0-9]{0,10}',str(value)),'请输入正整数。')
    return int(value)


def hub_list(value):
    return [v.strip() for v in re.split(r'[,，\n]',str(value or '')) if v.strip()]


def hub_dispatch(directory, payload, revision, cancelled=None):
    """Translate a UI intention to the existing, validated management action."""
    import management as m
    m.require(isinstance(payload,dict),'无效的界面请求。')
    route=payload.get('route','models');key=payload.get('key');intent=payload.get('intent','refresh')
    values=payload.get('values') or {}; extra=None;notice=None
    m.require(isinstance(values,dict),'无效的表单内容。')
    m.records(directory)
    if intent in ('navigate','refresh'):
        return hub_view(directory,route,key)
    state=m.snapshot(directory)
    def perform(action,data=None):
        return m.perform(directory,action,data or {},revision,cancelled)
    def connection():
        old=next((c for c in state['connections'] if c['id']==key),None) if key else None
        m.require(not key or old is not None,'连接已变更，请刷新。')
        c=copy.deepcopy(old or {'id':'local-'+uuid.uuid4().hex[:10],'model_specs':{}})
        c.update(name=str(values.get('name','')).strip(),kind=values.get('kind','openai'),
                 base_url=str(values.get('url','')).strip(),wire_api=values.get('wire','chat_completions'),
                 models=hub_list(values.get('models')),respect_server_context=values.get('respect') is True,
                 supports_reasoning_effort=values.get('reasoning') is True)
        if values.get('keyfile'):c['key_file']=values['keyfile']
        else:c.pop('key_file',None)
        return c
    if intent in ('save_model','reset_model'):
        perform('save_model',{'id':key,'context':None if intent=='reset_model' else hub_number(values.get('context')),
            'compact':None if intent=='reset_model' else hub_number(values.get('compact')),
            'visible':values.get('visible',True)})
    elif intent=='save_deepseek':
        perform(intent,{'enabled':values.get('enabled') is True,'key':values.get('key','')})
    elif intent in ('save_proxy','disable_proxy'):
        perform('save_proxy',{'proxy':None if intent=='disable_proxy' else
            {'base_url':values.get('url',''),'config':values.get('config',''),'models':hub_list(values.get('models'))},
            'auto_add':values.get('auto') is True})
    elif intent in ('save_connection','probe_connection'):
        c=connection();secret=values.get('key','')
        if intent=='save_connection':
            perform(intent,{'connection':c,'key':secret});key=c['id']
        else:
            result=perform(intent,{'connection':c,'key':secret})
            m.require(result['available'],'接口未返回可用型号。')
            # The native form retains the password in memory until Save.
            # Never echo it into a response/snapshot file.
            extra={'draft':{'connection':c},'available':result['available'],
                   'selected':c['models'] or result['available'],'specs':result['specs']}
            return hub_view(directory,'connections/discover',key,extra=extra)
    elif intent=='save_discovered':
        context=payload.get('context') or {}
        draft=context.get('draft') or {};c=copy.deepcopy(draft.get('connection') or {})
        available=context.get('available') or []
        m.require(isinstance(available,list) and len(available)<=10000,'检测结果无效。')
        chosen=[model for i,model in enumerate(available) if values.get('model_'+str(i)) is True]
        m.require(chosen,'至少选择一个型号。')
        c['models']=chosen;c.setdefault('model_specs',{})
        for i,model in enumerate(available):
            if model in chosen and model not in context.get('specs',{}):
                maximum=hub_number(values.get('window_'+str(i)))
                m.require(maximum is not None and maximum>=1024,'请补充 '+model+' 的最大窗口。')
                c['model_specs'][model]={'context_window':maximum,'supports_tools':True}
                if c.get('wire_api')=='anthropic_messages':c['model_specs'][model]['max_output_tokens']=hub_number(values.get('output_'+str(i)))
        perform('save_connection',{'connection':c,'key':draft.get('key','')})
        route='connections';key=None
    elif intent=='save_spec':
        c=next((copy.deepcopy(c) for c in state['connections'] if c['id']==key),None)
        m.require(c is not None,'连接已变更。');model=str(values.get('model','')).strip()
        m.require(model,'请填写准确型号。')
        c.setdefault('model_specs',{})[model]={'context_window':hub_number(values.get('maximum')),
            'reasoning_levels':hub_list(values.get('levels')),'supports_tools':values.get('tools') is True}
        if values.get('output'):c['model_specs'][model]['max_output_tokens']=hub_number(values['output'])
        perform('save_connection',{'connection':c})
    elif intent=='save_options':
        perform(intent,{**{k:values.get(k) is True for k in ('auto_update','model_sync','progress_ui')},
                        'interval':hub_number(values.get('interval'))})
    elif intent=='save_ui_options':
        hub_save_options(directory,values)
    elif intent=='save_images':
        perform(intent,{k:values.get(k) for k in ('enabled','url','config')})
    elif intent=='compare_models':
        result=perform(intent,values)
        notice=result['message']+f"\n{result['from']:,} → {result['to']:,} tokens"
    elif intent=='diagnose':
        extra=perform(intent,{'endpoints':True})
    elif intent in ('check_updates','retry_update','sync_models','show_progress','launch_codex'):
        result=perform(intent);notice=result.get('message')
    elif intent=='update_action':
        perform('update_action',{k:payload.get(k) for k in ('id','phase','command')});route='updates';key=None
    elif intent=='cancel_recovery':
        perform(intent,{'id':payload.get('id')})
    elif intent in ('confirm_remove','confirm_restore','confirm_recover','cleanup_preview'):
        labels={'confirm_remove':('移除连接','服务、凭证和聊天记录保留。','remove_connection'),
                'confirm_restore':('卸载并恢复原生 Codex','请先正常退出 Codex；保留原应用、账户与聊天。','restore'),
                'confirm_recover':('恢复安装','请先正常退出 Codex，再恢复中断的安装。','recover')}
        if intent=='cleanup_preview':
            result=perform(intent);labels[intent]=('清理缓存',f"预计释放 {result['bytes']/1048576:.1f} MB；保留当前安装和回退链。",'cleanup')
        title,message,action=labels[intent]
        return {**hub_view(directory,route,key),'title':title,'rows':[{'type':'note','text':message}],
                'buttons':[hub_button('返回','navigate',route=route,key=key),
                           hub_button('确认','confirmed',style='danger',action=action)],'confirmation':action}
    elif intent=='confirmed':
        action=payload.get('action')
        m.require(action in ('remove_connection','restore','recover','cleanup'),'无效的确认操作。')
        result=perform(action,{'id':key} if action=='remove_connection' else {'confirmed':True})
        if action=='restore':
            return {'route':'preferences','title':'已卸载并恢复原生 Codex','rows':[{'type':'note','text':result.get('message','设置窗口可以关闭。')}],
                    'buttons':[hub_button('退出','quit')],'revision':revision,'options':hub_options(directory)}
        if action=='remove_connection':route='connections';key=None
    else:m.require(False,'未知设置操作。')
    return hub_view(directory,route,key,notice or ('操作完成。' if intent!='diagnose' else None),extra=extra)


class Hotkey:
    """Register one shortcut, not a keyboard monitor. No accessibility permission."""
    def __init__(self, directory, *, key_code=2):
        self.directory=Path(directory);self.enabled=False;self.registered=False;self.error=None
        self.carbon=None;self.hotkey=ctypes.c_void_p();self.handler=ctypes.c_void_p()
        self.last=0;self.key_code=key_code
    def toggle(self):
        if time.monotonic()-self.last<.3:return
        self.last=time.monotonic()
        monitor.atomic_json(self.directory/'settings-ui/raise.json',{'time':uuid.uuid4().hex,'toggle':True})
    def configure(self, enabled):
        if enabled==self.enabled:return
        self.enabled=enabled
        if not enabled:
            self.close()
            monitor.atomic_json(self.directory/'settings-ui/hotkey.json',{'registered':False,'error':None})
            return
        self.error=None
        try:
            if sys.platform=='darwin':
                class EventType(ctypes.Structure):_fields_=[('kind',ctypes.c_uint32),('event',ctypes.c_uint32)]
                class HotkeyID(ctypes.Structure):_fields_=[('signature',ctypes.c_uint32),('id',ctypes.c_uint32)]
                self.carbon=ctypes.CDLL('/System/Library/Frameworks/Carbon.framework/Carbon')
                c=self.carbon
                c.GetApplicationEventTarget.restype=ctypes.c_void_p
                callback=ctypes.CFUNCTYPE(ctypes.c_int32,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p)
                self.callback=callback(lambda *_:(self.toggle(),0)[1])
                c.InstallEventHandler.argtypes=[ctypes.c_void_p,callback,ctypes.c_uint32,ctypes.POINTER(EventType),ctypes.c_void_p,ctypes.POINTER(ctypes.c_void_p)]
                c.RegisterEventHotKey.argtypes=[ctypes.c_uint32,ctypes.c_uint32,HotkeyID,ctypes.c_void_p,ctypes.c_uint32,ctypes.POINTER(ctypes.c_void_p)]
                event=EventType(0x6b657962,6)
                target=c.GetApplicationEventTarget()
                if c.InstallEventHandler(target,self.callback,1,ctypes.byref(event),None,ctypes.byref(self.handler)):raise OSError('handler')
                if c.RegisterEventHotKey(self.key_code,4096|2048,HotkeyID(0x57484c45,1),target,0,ctypes.byref(self.hotkey)):raise OSError('shortcut')
                c.ReceiveNextEvent.argtypes=[ctypes.c_uint32,ctypes.c_void_p,ctypes.c_double,ctypes.c_bool,ctypes.POINTER(ctypes.c_void_p)]
                c.GetEventDispatcherTarget.restype=ctypes.c_void_p
                c.SendEventToEventTarget.argtypes=[ctypes.c_void_p,ctypes.c_void_p]
                c.ReleaseEvent.argtypes=[ctypes.c_void_p]
            elif sys.platform=='win32':
                if not ctypes.windll.user32.RegisterHotKey(None,0x5748,0x4000|0x0001|0x0002,ord('D')):raise OSError('shortcut')
            self.registered=True
        except Exception:
            self.close();self.error='快捷键已被占用，可从 App 或开始菜单打开。'
        monitor.atomic_json(self.directory/'settings-ui/hotkey.json',{'registered':self.registered,'error':self.error})
    def pump(self):
        if sys.platform=='darwin' and self.carbon:
            # Carbon registration queues an EventRef; a CFRunLoop alone never
            # dispatches it to our application handler.
            event=ctypes.c_void_p()
            if self.carbon.ReceiveNextEvent(0,None,.2,True,ctypes.byref(event))==0:
                try:self.carbon.SendEventToEventTarget(event,self.carbon.GetEventDispatcherTarget())
                finally:self.carbon.ReleaseEvent(event)
        elif sys.platform=='win32':
            from ctypes import wintypes
            msg=wintypes.MSG()
            while ctypes.windll.user32.PeekMessageW(ctypes.byref(msg),None,0,0,1):
                if msg.message==0x0312:self.toggle()
            time.sleep(.2)
        else:time.sleep(.2)
    def close(self):
        if self.carbon:
            if self.hotkey.value:
                self.carbon.UnregisterEventHotKey.argtypes=[ctypes.c_void_p]
                self.carbon.UnregisterEventHotKey(self.hotkey);self.hotkey=ctypes.c_void_p()
            if self.handler.value:
                self.carbon.RemoveEventHandler.argtypes=[ctypes.c_void_p]
                self.carbon.RemoveEventHandler(self.handler);self.handler=ctypes.c_void_p()
        elif sys.platform=='win32' and self.registered:ctypes.windll.user32.UnregisterHotKey(None,0x5748)
        self.registered=False


def run_host(command, directory, **kwargs):
    """Keep shortcut handling beside the existing on-demand UI process."""
    hotkey=Hotkey(directory)
    host=Path(directory)/'settings-ui/host.json'
    monitor.atomic_json(host,{'pid':os.getpid(),'edition':p.EDITION,'view':HOST_VIEW})
    try:
        failures=0;last_raise=None;recovering=False
        while True:
            # Retain a bounded private error tail instead of losing the actual
            # renderer error. Never include it in exported diagnostics.
            with tempfile.TemporaryFile() as output:
                options={**kwargs,'env':{**os.environ,**kwargs.get('env',{}),
                         'CODEX_SETTINGS_RECOVERY':'1' if recovering else '0'}}
                child=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=output,stderr=output,**options)
                monitor.atomic_json(host,{'pid':os.getpid(),'renderer_pid':child.pid,'edition':p.EDITION,'view':HOST_VIEW})
                started=time.monotonic();last=0
                while child.poll() is None:
                    if time.monotonic()-last>1:
                        hotkey.configure(hub_options(directory)['hotkey']);last=time.monotonic()
                    hotkey.pump()
                if child.returncode==0:return subprocess.CompletedProcess(command,0,b'',b'')
                output.seek(0,os.SEEK_END);output.seek(max(0,output.tell()-16384))
                private_write(host.parent/'renderer-error.log',output.read())
            failures=1 if time.monotonic()-started>60 else failures+1
            monitor.atomic_json(host.parent/'renderer-status.json',{'exit':child.returncode,'failures':failures,'time':time.time()})
            if failures<3:
                recovering=True
                time.sleep(.5*failures)
                continue
            # Keep the shortcut and App entry alive after repeated failures,
            # without a relaunch storm. An explicit wake retries once.
            last_raise=monitor.load_json(host.parent/'raise.json',{}).get('time')
            while monitor.load_json(host.parent/'raise.json',{}).get('time')==last_raise:
                hotkey.configure(hub_options(directory)['hotkey']);hotkey.pump()
            failures=2;recovering=False
    finally:
        hotkey.close()
        if monitor.load_json(host,{}).get('pid')==os.getpid():host.unlink(missing_ok=True)


if __name__=='__main__':raise SystemExit(main())
