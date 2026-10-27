"""Private update state/controller; native views render data and fixed actions only."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from monitor import atomic_json, load_json, state_lock
from windows_support import background_options, native_ui_options

ROOT = Path(__file__).resolve().parent
TEXT = {
    'current': ('补丁已是最新', '当前补丁已就绪，无需更新。'),
    'available': ('发现补丁新版本', '此次更新模型补丁。可以继续使用 Codex；点“开始更新”后，准备完成并等任务空闲再安装。'),
    'checking': ('正在检查补丁', '补丁已下载，正在检查来源和兼容性。可以继续使用 Codex。'),
    'downloading': ('正在下载补丁', '正在下载模型补丁。可以继续使用 Codex。'),
    'checking_package': ('正在校验下载', '正在核对下载文件和来源。可以继续使用 Codex。'),
    'waiting_for_idle': ('等待任务空闲', '已记录更新请求；任务空闲后会自动关闭 Codex 并安装，无需再次点击。'),
    'close_failed': ('Codex 尚未退出', '更新包已准备好。可手动退出 Codex，或点击重试关闭。'),
    'recovering': ('正在恢复上一版本', '新补丁未能正常启动，正在恢复之前的服务和配置。'),
    'rolled_back': ('已恢复上一版本', '更新未完成，之前的补丁已恢复。可查看诊断或手动重试。'),
    'building': ('正在准备补丁', '正在准备模型目录和本地适配服务。可以继续使用 Codex。'),
    'verifying': ('正在验证补丁', '正在进行本地兼容性测试；检查通过后再安装补丁。'),
    'waiting_for_exit': ('补丁已准备好', '可以继续使用 Codex；点“开始更新”后会等待任务空闲，再关闭 Codex 及其后台进程并安装。'),
    'closing': ('正在关闭 Codex', '正在关闭 Codex 及其后台进程，安装期间暂勿启动 Codex。'),
    'installing': ('正在安装补丁', '正在安装模型补丁，暂勿启动 Codex。关窗不会中断更新。'),
    'activating': ('正在启用补丁', '正在启用补丁后台组件，暂勿启动 Codex。'),
    'completed': ('补丁更新完成', '模型补丁已就绪。点“启动 Codex”打开原版应用。'),
    'failed': ('补丁更新暂未完成', '补丁更新遇到问题，已保留本地记录。可继续使用当前版本，稍后重试。'),
}
START_PHASES = {'available', 'checking', 'downloading', 'checking_package', 'building', 'verifying', 'waiting_for_exit','close_failed'}
PHASES = ('available','checking','downloading','checking_package','building','verifying',
          'waiting_for_exit','waiting_for_idle','closing','installing','activating','completed')
TERMINAL_PHASES = {'failed','rolled_back','completed','current'}


def write_progress(directory, value):
    # The canonical view also fences older monitor writers which only update
    # the legacy top-level fields during their first upgrade.
    value['display']={key:value.get(key) for key in ('id','edition','version','phase','title','detail',
        'completed','total','unit','percent','can_start','can_launch','can_cancel','phase_started_at')}
    atomic_json(Path(directory)/'progress.json',value)


def display_progress(value):
    stable=value.get('display',{})
    return stable if stable.get('id')==value.get('id') and stable.get('edition')==value.get('edition') else value



def optional_view(function):
    """View state is optional; invalid actions still use strict reads below."""
    from functools import wraps
    @wraps(function)
    def call(*args, **kwargs):
        try:return function(*args, **kwargs)
        except Exception:return None
    return call


@contextmanager
def control_lock(directory):
    # Independent of the monitor lock: a click remains responsive during a build.
    for attempt in range(40):
        with state_lock(Path(directory) / 'progress-control') as acquired:
            if acquired:
                yield
                return
        time.sleep(.025)
    raise ValueError('Update progress is busy')


def macos_ui_command(script, *arguments):
    """Match the Python host's architecture despite LaunchServices preferences.

    Finder can pass an Intel preference through a script-based .app launcher.
    Rosetta's JXA/libffi callback allocation can then hang before showing UI.
    """
    machine = __import__('platform').machine()
    prefix = ['/usr/bin/arch', '-' + machine] if machine in ('arm64', 'x86_64') else []
    return prefix + ['/usr/bin/osascript', '-l', 'JavaScript', str(script), *map(str, arguments)]


def _spawn(command, directory, *, view=False):
    return subprocess.Popen(command, cwd=directory, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        **((native_ui_options() if view else background_options()) if os.name == 'nt' else {'start_new_session': True}))


def _python(settings):
    value = Path(settings.get('python_path') or sys.executable)
    if sys.platform == 'win32' and value.name.lower() == 'python.exe':
        hidden = value.with_name('pythonw.exe')
        if hidden.is_file():
            value = hidden
    return str(value.resolve())


class CheckStatus:
    """Read-only discovery feedback, separate from an install/action identity."""
    def __init__(self, directory, edition):
        self.directory, self.edition = Path(directory), edition
        self.identity = uuid.uuid4().hex

    @optional_view
    def publish(self, phase, completed=None, total=None, present=False, unit=None):
        title, detail = {
            'checking': ('正在检查补丁更新', '正在读取本版本的更新来源。'),
            'downloading': ('正在下载候选补丁', '正在下载并校验文件，可继续使用 Codex。'),
            'checking_package': ('正在校验候选补丁', '正在核对文件摘要和预检结果。可以继续使用 Codex。'),
            'verifying': ('正在校验候选补丁', '正在隔离环境中验证兼容性，可继续使用 Codex。'),
            'current': TEXT['current'],
            'failed': ('更新检查未完成', '暂时无法取得有效更新，请稍后重试；现有补丁保持不变。'),
        }[phase]
        atomic_json(self.directory/'update.check.json', dict(id=self.identity, edition=self.edition,
            phase=phase,title=title,detail=detail,completed=completed,total=total,unit=unit,
            percent=completed*100//total if total else None,
            phase_started_at=(load_json(self.directory/'update.check.json',{}).get('phase_started_at') if load_json(self.directory/'update.check.json',{}).get('phase')==phase else time.time()),can_start=False,can_launch=False,
            active=phase not in ('current','failed')))

    @optional_view
    def finish(self):
        value=load_json(self.directory/'update.check.json', {})
        if value.get('id')==self.identity:
            value['active']=False
            atomic_json(self.directory/'update.check.json', value)


class Progress:
    def __init__(self, directory, root=None):
        self.directory = Path(directory).resolve()
        self.root = Path(root or ROOT).resolve()
        self.identity = self.read().get('id')

    def read(self):
        value=load_json(self.directory / 'progress.json', {})
        return {**value,**display_progress(value)}

    @optional_view
    def begin(self, edition, version, key, restart=False, initial='available'):
        if edition not in ('ds', 'proxy', 'unified') or not re.fullmatch(r'[\w.+-]{1,79}', version):
            raise ValueError('Invalid update identity')
        with control_lock(self.directory):
            meta = load_json(self.directory / 'progress.context.json', {})
            value = self.read()
            if meta.get('key') == key and value.get('edition') == edition and value.get('version') == version and not (restart and value.get('phase') in ('completed','failed')):
                self.identity=value['id']
                return value['id']
            identity = self.identity = uuid.uuid4().hex
            atomic_json(self.directory / 'progress.context.json', {'format': 1, 'id': identity,
                'key': key, 'root': str(self.root)})
            value = {'format': 1, 'id': identity, 'edition': edition, 'version': version,
                'phase': initial, 'title': TEXT[initial][0], 'detail': TEXT[initial][1],
                'completed': None, 'total': None, 'can_start': True, 'can_launch': False,
                'phase_started_at': time.time()}
            write_progress(self.directory,value)
            return identity

    @optional_view
    def requested(self):
        request = load_json(self.directory / 'progress.request.json', {})
        return request.get('id') == self.identity == self.read().get('id') and request.get('action') in ('start','wait_idle')

    def paused(self):
        value=load_json(self.directory/'progress.request.json',{})
        return value.get('id')==self.identity and value.get('action') in ('cancel','expired')

    def waiting(self, detail):
        self.publish('waiting_for_idle')
        with control_lock(self.directory):
            value=self.read()
            if value.get('id')==self.identity and value.get('phase')=='waiting_for_idle' and value.get('detail')!=detail:
                value['detail']=detail;write_progress(self.directory,value)

    @optional_view
    def reopened(self):
        # The install precondition detected a new native process before writes.
        with control_lock(self.directory):
            value=self.read()
            if value.get('id')!=self.identity or value.get('phase') not in ('closing','installing'):return
            value.update(phase='waiting_for_idle',title=TEXT['waiting_for_idle'][0],
                detail='Codex 已重新打开，继续等待任务空闲；更新包仍保留。',
                completed=None,total=None,unit=None,percent=None,can_start=False,
                can_cancel=True,can_launch=False,phase_started_at=time.time())
            write_progress(self.directory,value)
        release_idle(self)

    @optional_view
    def retry(self):
        with control_lock(self.directory):
            value=self.read()
            if value.get('id')!=self.identity or value.get('phase') not in ('failed','rolled_back'):
                return
            value.update(phase='available',title=TEXT['available'][0],detail=TEXT['available'][1],
                completed=None,total=None,percent=None,unit=None,can_start=not self.requested(),can_launch=False,
                phase_started_at=time.time(),attempt=value.get('attempt',1)+1)
            write_progress(self.directory,value)

    @optional_view
    def publish(self, phase, completed=None, total=None, build=None, present=False, unit=None):
        if phase not in TEXT:
            raise ValueError('Unknown update phase')
        if (completed is not None or total is not None) and not (
                type(completed) is int and type(total) is int and 0 <= completed <= total and total > 0) and not (
                unit=='bytes' and type(completed) is int and completed>=0 and total is None):
            raise ValueError('Progress counts must be actual completed/total units')
        with control_lock(self.directory):
            value = self.read()
            if not value or value.get('id') != self.identity:
                return
            prior=display_progress(value).get('phase')
            if prior in ('failed','rolled_back','completed','close_failed') and phase not in (prior,'current') and not (prior=='close_failed' and phase=='installing'):
                return
            if prior in PHASES and phase in PHASES and PHASES.index(phase)<PHASES.index(prior):
                return
            if prior=='recovering' and phase!='rolled_back' and phase!='failed':return
            if prior==phase and total and total==value.get('total') and (unit or 'items')==value.get('unit'):
                completed=max(completed,value.get('completed') or 0)
            title, detail = TEXT[phase]
            if value.get('phase') != phase or not value.get('phase_started_at'):
                value['phase_started_at'] = time.time()
            if self.requested() and phase in ('checking', 'downloading', 'checking_package', 'building', 'verifying'):
                detail += ' 准备好并等到任务空闲后继续，无需再次点击。'
            value.update(phase=phase, title=title, detail=detail, completed=completed, total=total,
                unit=unit or ('items' if total else None), percent=(completed*100//total) if total else None,
                can_cancel=self.requested() and phase in (*START_PHASES,'waiting_for_idle'),
                can_start=phase in START_PHASES and not self.requested(), can_launch=phase == 'completed' and load_json(self.directory/'progress.launch.json',{}).get('id')!=value['id'])
            write_progress(self.directory,value)
            if build is not None:
                meta = load_json(self.directory / 'progress.context.json', {})
                meta['build'] = str(Path(build).resolve())
                atomic_json(self.directory / 'progress.context.json', meta)
        if present:
            self.present()

    @optional_view
    def present(self, force=False):
        # Only installed monitor settings can display a view. Preflight/unit jobs
        # without a configured app/home/Python remain completely headless.
        settings = load_json(self.directory / 'settings.json', {})
        value = self.read()
        if not value or not all(settings.get(k) for k in ('app', 'codex_home', 'python_path', 'edition')):
            return
        if settings['edition'] != value['edition'] or settings.get('progress_ui') is False and not force:
            return
        with control_lock(self.directory):
            ui = load_json(self.directory / 'progress.ui.json', {})
            if ui.get('id') != value['id']:
                ui = {'id': value['id'], 'presented': []}
            category = 'completed' if value['phase'] == 'completed' else 'available'
            if category in ui['presented'] and not force:
                return
            # During the first upgrade an already-open legacy progress viewer
            # follows this same progress file. Let it finish without a second UI.
            if not ui.get('view') and ui.get('pid') and _alive(ui['pid']):
                if category not in ui['presented']:ui['presented'].append(category)
                atomic_json(self.directory/'progress.ui.json',ui)
                return
            # One on-demand whale host owns progress, settings and notifications.
            # A background notice reveals only the badge, never a settings card.
            host=load_json(self.directory/'settings-ui/host.json',{})
            from management import HOST_VIEW,queue_ui_entry
            host_is_whale=host.get('edition')==value['edition'] and host.get('view')==HOST_VIEW
            pid=host.get('pid') if host_is_whale else None
            if not pid and ui.get('view')=='whale':pid=ui.get('pid')
            if pid and _alive(pid):
                if not host_is_whale:
                    queue_ui_entry(self.directory,page='updates' if force else None,notify=not force)
                elif force:
                    atomic_json(self.directory/'settings-ui/raise.json',{'time':uuid.uuid4().hex,'page':'updates'})
                elif category not in ui['presented']:
                    atomic_json(self.directory/'settings-ui/raise.json',{'time':uuid.uuid4().hex,'notify':True})
                if category not in ui['presented']:ui['presented'].append(category)
                ui.update(pid=pid,view='whale')
                atomic_json(self.directory/'progress.ui.json',ui)
                return
            if sys.platform not in ('darwin','win32'):return
            command=[_python(settings),str(self.root/'management.py'),'show','--state-dir',str(self.directory)]
            command+=['--page','updates'] if force else ['--notify-update']
            try:
                child = _spawn(command, self.directory, view=True)
            except OSError:
                return  # A missing view never blocks a safe update.
            ui.update(pid=child.pid,view='whale')
            if category not in ui['presented']:ui['presented'].append(category)
            atomic_json(self.directory / 'progress.ui.json', ui)

    def metadata(self):
        try:return {'progress_state_dir': str(self.directory), 'progress_id': self.read().get('id')}
        except Exception:return {}


def _alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.restype = ctypes.c_void_p
        api.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        api.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        try:
            return bool(api.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@optional_view
def for_settings(settings, root=None, version=None, force=False, *, runtime=None, app_hash=None):
    directory = settings.get('state_dir')
    if not directory or settings.get('edition') not in ('ds', 'proxy', 'unified'):
        return None
    import patch as p
    import updater
    root = Path(root or p.ROOT)
    identity = runtime or updater.runtime_digest(root)
    baseline = load_json(Path(settings.get('baseline') or directory) / 'manifest.json', {})
    if not force and baseline.get('update', {}).get('runtime_digest') == identity:
        return None
    if app_hash is None:
        try:
            app_hash = p.file_sha(p.app_layout(Path(settings['app']))['asar'])
        except (OSError, ValueError, p.PatchError):
            app_hash = str(settings.get('app', ''))
    progress = Progress(directory, root)
    check=load_json(Path(directory)/'update.check.json',{})
    initial='checking_package' if check.get('edition')==settings['edition'] and check.get('phase') in ('verifying','checking_package') else 'available'
    progress.begin(settings['edition'], version or updater.edition(root)['version'], identity + ':' + app_hash, initial=initial)
    return progress


@optional_view
def from_manifest(manifest):
    update = manifest.get('update', {})
    if update.get('quiet_model_sync') is True:
        return None
    directory, identity = update.get('progress_state_dir'), update.get('progress_id')
    if not directory or not identity:
        return None
    progress = Progress(directory)
    value = progress.read()
    if value.get('edition') != manifest.get('edition'):
        return None
    if value.get('id') != identity and not _same_release(progress, manifest):
        return None
    return progress


def _same_release(progress, manifest):
    update = manifest.get('update', {})
    value = progress.read()
    meta = load_json(progress.directory / 'progress.context.json', {})
    return (bool(update.get('runtime_digest'))
            and meta.get('key', '').startswith(update['runtime_digest'] + ':')
            and update.get('release', {}).get('version') == value.get('version'))


def _build(progress):
    meta = load_json(progress.directory / 'progress.context.json', {})
    value = progress.read()
    state = load_json(progress.directory / 'state.json', {})
    candidates = [meta.get('build'), state.get('pending', {}).get('build', {}).get('directory')]
    if value.get('phase') == 'completed':
        candidates.append(state.get('installed', {}).get('build', {}).get('directory'))
    for candidate in candidates:
        if not candidate:
            continue
        directory = Path(candidate).resolve()
        manifest = load_json(directory / 'manifest.json', {})
        update = manifest.get('update', {})
        if manifest.get('edition') != value.get('edition'):
            continue
        # An explicit recheck can create a new UI request ID for an already
        # prepared/installed release. Bind to that exact runtime and version,
        # rather than losing the build when the pending state is retired.
        if update.get('progress_id') == value.get('id') or _same_release(progress, manifest):
            return directory, manifest
    return None


def previous_host_settings(progress):
    record=_build(progress)
    if not record:return None
    directory,manifest=record
    previous=manifest.get('legacy_baseline')
    if not previous:return None
    path=Path(previous)/'host-settings.json'
    prior=load_json(Path(previous)/'manifest.json',{})
    import patch as p
    if not path.is_file() or p.file_sha(path)!=prior.get('host_settings_sha256'):return None
    return load_json(path,{})


def pending_activity(progress):
    from official_client_host import activity_status
    settings=previous_host_settings(progress)
    return activity_status(settings) if settings else {'known':False,'reason':'older_adapter'}


@optional_view
def release_idle(progress):
    from official_client_host import activity_status
    settings=previous_host_settings(progress)
    if settings:activity_status(settings,lease=progress.identity,release=True)


def wait_expired(progress, now=None):
    now=time.time() if now is None else now
    request=load_json(progress.directory/'progress.request.json',{})
    if not progress.requested() or not request.get('at') or now-request['at']<3600:return False
    cancel_wait(progress,expired=True)
    return True


def cancel_wait(progress, expired=False):
    with control_lock(progress.directory):
        value=progress.read()
        if value.get('id')!=progress.identity or value.get('phase') not in (*START_PHASES,'waiting_for_idle'):
            raise ValueError('Installation has already begun')
        atomic_json(progress.directory/'progress.request.json',{'id':progress.identity,'action':'expired' if expired else 'cancel'})
        value.update(can_start=True,can_cancel=False)
        if value['phase']=='waiting_for_idle':value.update(phase='waiting_for_exit',title=TEXT['waiting_for_exit'][0])
        value['detail']='本次等待已结束，更新包保留。可稍后再次开始。' if expired else '已取消本次等待，更新包保留。'
        write_progress(progress.directory,value)
    release_idle(progress)


@optional_view
def validated_close(progress):
    """Consume explicit intent only after rechecking a prepared native build."""
    if not progress.requested() or progress.read().get('phase') not in ('waiting_for_exit','waiting_for_idle'):
        return False
    record = _build(progress)
    if not record:
        return False
    directory, manifest = record
    if manifest.get('architecture') != 'official-client-v1' or manifest.get('status') not in ('prepared','prepared_idle'):
        return False
    from official_client_install import validate_build, _guard_staged_config
    import patch as p
    validate_build(directory, manifest)
    _guard_staged_config(p.read_config(Path(manifest['codex_home']) / 'config.toml'),
                         (directory / 'config-before.toml').read_text(encoding='utf-8'))
    # A legacy parent must not turn a deferred start back into an immediate
    # close. New controllers understand wait_idle; old ones wait for app exit.
    with control_lock(progress.directory):
        request=load_json(progress.directory/'progress.request.json',{})
        if request.get('id')!=progress.identity or request.get('action') not in ('start','wait_idle'):return False
        request.update(action='wait_idle',at=request.get('at') or time.time())
        atomic_json(progress.directory/'progress.request.json',request)
    if wait_expired(progress):return False
    from official_client_host import activity_status
    old_settings=previous_host_settings(progress)
    activity=activity_status(old_settings,lease=progress.identity) if old_settings else {'known':False}
    if not activity.get('acquired'):
        detail=('等待任务完成：正在处理 '+str(activity.get('requests',0))+' 个请求，等待 '+str(activity.get('tools',0))+' 个工具结果。') if activity.get('known') else '当前服务尚不能报告任务状态；正常退出 Codex 后会继续安装，更新包已保留。'
        if activity.get('known') and not activity.get('requests') and not activity.get('tools'):
            detail='任务已结束，等待连续 10 秒空闲后自动安装。'
        if activity.get('unknown'):detail='正在等待未完成的任务状态确认；也可正常退出 Codex 后安装。'
        progress.waiting(detail)
        return False
    with control_lock(progress.directory):
        if not progress.requested():
            release_idle(progress);return False
        value=progress.read();value.update(phase='closing',title=TEXT['closing'][0],detail=TEXT['closing'][1],
            can_cancel=False,can_start=False,phase_started_at=time.time())
        write_progress(progress.directory,value)
    targets = {str(Path(manifest['target']).resolve())}
    previous = load_json(Path(manifest.get('legacy_baseline') or directory) / 'manifest.json', {})
    if previous.get('deployment_target'):
        targets.add(str(Path(previous['deployment_target']).resolve()))
    try:
        graceful_close(sorted(targets))
    except (OSError, ValueError, subprocess.SubprocessError):
        return close_incomplete(progress)
    # Windows waits for exact verified handles, including headless backends.
    # Reopened processes still block installation and are never included in that set.
    for attempt in range(2 if sys.platform == 'win32' else 40):
        try:
            for target in targets:
                p.require_stopped(Path(target), read_only_source=sys.platform == 'win32')
            return True
        except (ValueError, p.PatchError):
            time.sleep(.25)
    return close_incomplete(progress)


def close_incomplete(progress):
    release_idle(progress)
    progress.publish('close_failed')
    with control_lock(progress.directory):
        value=progress.read()
        if value.get('id')!=progress.identity:return False
        value.update(detail='Codex 尚未完全退出，补丁已准备好。可手动退出，或点击重试关闭。',can_start=True)
        write_progress(progress.directory,value)
    return False


def graceful_close(targets):
    if sys.platform == 'darwin':
        script = ("ObjC.import('AppKit'); function run(a){const paths=JSON.parse(a[0]); "
            "const apps=$.NSRunningApplication.runningApplicationsWithBundleIdentifier('com.openai.codex');"
            "for(let i=0;i<apps.count;i++){const app=apps.objectAtIndex(i);"
            "if(paths.indexOf(ObjC.unwrap(app.bundleURL.path))>=0)app.terminate;}}")
        subprocess.run(['/usr/bin/osascript', '-l', 'JavaScript', '-e', script, json.dumps(targets)],
            check=True, capture_output=True, timeout=15, **background_options())
    elif sys.platform == 'win32':
        from windows_support import close_app_handles
        close_app_handles(targets)
    else:
        raise ValueError('Unsupported desktop platform')


def launch_official(directory, manifest):
    if manifest.get('status') != 'installed':
        raise ValueError('Update installation is not complete')
    app = Path(manifest['target']).resolve()
    from official_client_update import verify_official
    verify_official(app)
    if manifest.get('composer_compat'):
        from official_client_host import start_composer
        start_composer(directory,manifest)
    elif sys.platform == 'darwin':
        _spawn(['/usr/bin/open', '-a', str(app)], directory)
    elif sys.platform == 'win32':
        from official_client_shortcut import official_entry
        entry=official_entry(app)
        aumid=entry.get('app_user_model_id','')
        if not re.fullmatch(r'OpenAI\.Codex_[\w.-]+![\w.-]+',aumid):
            raise ValueError('The official registered Store application could not be identified')
        _spawn([entry['target'],'shell:AppsFolder\\'+aumid],directory)
    else:
        raise ValueError('Unsupported desktop platform')
    import patch as p
    from official_client_host import healthy
    host=load_json(Path(directory)/'host-settings.json',{})
    executable=p.app_layout(app)['exe'].resolve()
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        rows=p.active_app_processes(app,read_only_source=True)
        if any(Path(row['exe']).resolve()==executable for row in rows) and healthy(host):
            atomic_json(Path(directory)/'app-startup.json',{'ready':True,'at':time.time()})
            return
        time.sleep(.25)
    atomic_json(Path(directory)/'app-startup.json',{'ready':False,'at':time.time()})
    raise ValueError('Codex startup has not been confirmed; retry is available')


def action(directory, identity, command, phase=None):
    if command not in ('start', 'launch', 'dismiss','cancel'):
        raise ValueError('Unknown update action')
    progress = Progress(directory)
    if command=='cancel':
        if identity!=progress.identity:raise ValueError('This update window is no longer current')
        cancel_wait(progress);return {'action':'cancelled'}
    with control_lock(progress.directory):
        value = progress.read()
        if not identity or value.get('id') != identity:
            raise ValueError('This update window is no longer current')
        if command == 'dismiss':
            if phase not in TEXT:
                raise ValueError('Unknown dismissed update phase')
            ui = load_json(progress.directory / 'progress.ui.json', {'id': identity, 'presented': []})
            if ui.get('id') == identity:
                ui.pop('pid', None)
                ui['dismissed_phase'] = phase
                category = 'completed' if phase == 'completed' else 'available'
                if category not in ui['presented']:ui['presented'].append(category)
                if phase!='completed' and value['phase']=='completed':
                    ui['presented']=[v for v in ui['presented'] if v!='completed']
                atomic_json(progress.directory / 'progress.ui.json', ui)
        if command == 'start':
            if value['phase'] not in START_PHASES or not value.get('can_start'):
                raise ValueError('This update cannot be started now')
            atomic_json(progress.directory / 'progress.request.json', {'format': 1, 'id': identity, 'action': 'wait_idle','at':time.time()})
            if value['phase']=='close_failed':value.update(phase='waiting_for_exit',title=TEXT['waiting_for_exit'][0])
            value.update(can_start=False,can_cancel=True,detail='已收到更新请求。准备好并等到任务空闲后自动安装。')
            write_progress(progress.directory,value)
        elif command=='launch':
            claim=load_json(progress.directory/'progress.launch.json',{})
            if not value.get('can_launch') or value['phase']!='completed' or claim.get('id')==identity:
                raise ValueError('This update is not ready to launch')
            atomic_json(progress.directory/'progress.launch.json',{'id':identity})
            value['can_launch']=False;write_progress(progress.directory,value)
    if command=='dismiss':
        if phase!='completed' and value['phase']=='completed':progress.present()
        return {'action':'dismissed'}
    if command == 'start':
        # Works for old monitors too: a ready build can quit immediately; an
        # in-flight new worker consumes this same durable request after verify.
        ready=validated_close(progress)
        settings = load_json(progress.directory / 'settings.json', {})
        from monitor import service_command
        command=service_command(progress.directory, _python(settings))
        if ready:command.append('--install-ready')
        _spawn(command, progress.directory)
    else:
        try:
            record = _build(progress)
            if not record:
                raise ValueError('The installed update record is unavailable')
            launch_official(*record)
        except Exception as error:
            # A failed launch must not permanently consume the user's button.
            from official_client_host import ComposerAlreadyRunningError
            detail=('请先正常退出 Codex，再点“启动 Codex”启用外部模型独立发送。'
                    if isinstance(error,ComposerAlreadyRunningError) else
                    '启动未完成，请再次点击“启动 Codex”。补丁已安装。')
            with control_lock(progress.directory):
                value = progress.read()
                claim = load_json(progress.directory / 'progress.launch.json', {})
                if claim.get('id') == identity and value.get('id') == identity:
                    (progress.directory / 'progress.launch.json').unlink(missing_ok=True)
                    value.update(can_launch=True, detail=detail)
                    write_progress(progress.directory,value)
            raise
    return {'action': command}


@optional_view
def worker_progress(action_name, job, job_path):
    """New candidate workers also bridge first upgrades from old monitors."""
    if job.get('quiet_progress') or job.get('update',{}).get('quiet_model_sync') is True:
        return None
    if action_name == 'install':
        return from_manifest(load_json(Path(job['directory']) / 'manifest.json', {}))
    settings = job.get('settings')
    if settings is None and action_name == 'identity':
        path = Path(job_path).resolve()
        if path.name != 'worker-identity.json' or path.parent.name != 'identity':
            return None
        directory = path.parent.parent
        settings = load_json(directory / 'settings.json', {})
        if settings.get('state_dir') and Path(settings['state_dir']).resolve() != directory:
            return None
        settings = {**settings, 'state_dir': str(directory)}
    if not settings:
        return None
    import patch as p
    if settings.get('edition') != p.EDITION:
        return None
    if action_name in ('identity','build') and settings.get('state_dir'):
        current=Progress(settings['state_dir'])
        value=current.read()
        if value.get('edition')==p.EDITION and value.get('version')==__import__('updater').edition()['version'] and value.get('phase') not in ('completed','failed'):
            return current
    return for_settings(settings, force=action_name == 'build')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--action', choices=('start', 'launch', 'dismiss', 'cancel'), required=True)
    parser.add_argument('--id', required=True)
    parser.add_argument('--phase', choices=tuple(TEXT))
    args = parser.parse_args(argv)
    try:
        action(args.state_dir, args.id, args.action, args.phase)
        return 0
    except Exception:
        # UI actions never print paths, endpoint tokens, or raw error details.
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
