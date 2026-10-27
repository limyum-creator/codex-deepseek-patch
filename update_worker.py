"""Stable isolated process interface for staged patch code and model updates."""
from pathlib import Path
import argparse
import json
import sys

import patch as p
import backend


class SetupProgress:
    """Private, fixed-vocabulary progress for the first-install child."""
    def __init__(self, path):
        self.path = Path(path)

    def publish(self, phase, completed=None, total=None, **ignored):
        from monitor import atomic_json
        labels = {'building':'正在准备适配文件', 'verifying':'正在验证原生兼容性', 'ready':'准备和验证完成'}
        if phase not in labels:raise ValueError('Unknown setup phase')
        value = {'phase':phase, 'message':labels[phase]}
        if type(completed) is int and type(total) is int and 0 <= completed <= total and total > 0:
            value.update(completed=completed, total=total)
        atomic_json(self.path, value)


def setup_command(arguments, cancelled=None):
    """One cancellable child; only fixed progress metadata crosses the boundary."""
    import os, signal, subprocess, tempfile, time
    from management import SettingsError, require
    from official_client_host import windowless_python
    from monitor import load_json
    with tempfile.TemporaryDirectory(prefix='setup-progress-', dir=Path(arguments[arguments.index('--output')+1]).parent.parent/'setup') as temporary:
        progress_path=Path(temporary)/'progress.json'
        command=[str(windowless_python(sys.executable)),str(p.ROOT/'patch.py'),*map(str,arguments),'--progress-file',str(progress_path)]
        with tempfile.TemporaryFile() as log:
            child=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                **(__import__('windows_support').background_options() if os.name=='nt' else {'start_new_session':True}))
            try:
                previous=None
                while True:
                    if cancelled and cancelled():raise SettingsError('cancelled','操作已取消，正式配置未改变。')
                    value=load_json(progress_path,{})
                    if value!=previous and getattr(cancelled,'progress',None):cancelled.progress(value)
                    previous=value
                    if child.poll() is not None:break
                    time.sleep(.1)
                require(child.returncode==0,'准备或验证未通过；正式配置未改变，请检查应用版本、接口和模型规格。','setup_validation_failed')
            finally:
                if child.poll() is None:
                    if os.name=='nt':subprocess.run(['taskkill','/PID',str(child.pid),'/T','/F'],capture_output=True,**p.background_options())
                    else:os.killpg(child.pid,signal.SIGTERM)
                    try:child.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        if os.name=='nt':child.kill()
                        else:os.killpg(child.pid,signal.SIGKILL)
                        child.wait(timeout=5)


def brand_assets(directory, source):
    """Rasterize our small vector mark once, using only the standard library."""
    import struct,zlib,math,xml.etree.ElementTree as ET
    import monitor
    from management import vector_contours, private_write
    raw=Path(source).read_bytes();digest=p.sha(raw)
    directory=Path(directory);stamp=directory/'brand.json'
    paths={name:directory/('brand.'+name) for name in ('png','ico','icns')}
    if monitor.load_json(stamp,{}).get('source')==digest and all(path.is_file() for path in paths.values()):
        return digest,paths
    size=512;pixels=bytearray(size*size*4)
    for shape in ET.fromstring(raw):
        kind=shape.tag.rsplit('}',1)[-1]
        if kind not in ('polygon','path'):continue
        color=bytes.fromhex(shape.attrib['fill'].lstrip('#'))+b'\xff'
        contours=vector_contours(shape.attrib['d']) if kind=='path' else [
            [tuple(map(float,value.split(','))) for value in shape.attrib['points'].split()]]
        points=[v for contour in contours for v in contour]
        for y in range(max(0,math.floor(min(v[1] for v in points))),min(size,math.ceil(max(v[1] for v in points)))):
            scan=y+.5;crossings=[]
            for contour in contours:
                for (x1,y1),(x2,y2) in zip(contour,contour[1:]+contour[:1]):
                    if (y1<=scan<y2) or (y2<=scan<y1):crossings.append(x1+(scan-y1)*(x2-x1)/(y2-y1))
            crossings.sort()
            for left,right in zip(crossings[::2],crossings[1::2]):
                first=max(0,math.ceil(left-.5));last=min(size,math.ceil(right-.5))
                if last>first:pixels[(y*size+first)*4:(y*size+last)*4]=color*(last-first)
    smaller=bytearray()
    for y in range(256):
        smaller.append(0)  # PNG filter byte.
        for x in range(256):
            samples=[pixels[((y*2+dy)*size+x*2+dx)*4:((y*2+dy)*size+x*2+dx)*4+4] for dy in (0,1) for dx in (0,1)]
            opaque=[v for v in samples if v[3]]
            smaller.extend(bytes([sum(v[k] for v in opaque)//len(opaque) for k in range(3)]+[len(opaque)*255//4]) if opaque else b'\0\0\0\0')
    def chunk(name,data):return struct.pack('>I',len(data))+name+data+struct.pack('>I',zlib.crc32(name+data))
    png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',256,256,8,6,0,0,0))+chunk(b'IDAT',zlib.compress(smaller,9))+chunk(b'IEND',b'')
    outputs={'png':png,'ico':struct.pack('<HHHBBBBHHII',0,1,1,0,0,0,0,1,32,len(png),22)+png,
             'icns':b'icns'+struct.pack('>I',len(png)+16)+b'ic08'+struct.pack('>I',len(png)+8)+png}
    for kind,data in outputs.items():private_write(paths[kind],data)
    monitor.atomic_json(stamp,{'source':digest})
    return digest,paths


def first_setup_view(directory, notice=''):
    import os
    from management import (setup_read, setup_app, hub_field, hub_button,
                            hub_options, setup_revision, footer_layout, setup_deepseek)
    draft=setup_read(directory);phase=draft.get('phase','draft')
    rows=[hub_field('app','Codex 应用位置',draft.get('app') or setup_app()),
          hub_field('home','Codex 配置目录',draft.get('home') or str(Path(os.environ.get('CODEX_HOME',Path.home()/'.codex'))))]
    if setup_deepseek(draft):
        rows += [hub_field('key','DeepSeek API Key','','secret',hint='只保存于本机；无需重新登录 GPT。')]
    else:
        connection=draft.get('connection',{})
        rows += [hub_field('name','连接名称',connection.get('name','我的模型')),
                 hub_field('url','接口地址',connection.get('base_url','')),
                 hub_field('kind','服务类型',connection.get('kind','openai'),'choice',choices=[['openai','兼容接口'],['ollama','Ollama'],['lmstudio','LM Studio']]),
                 hub_field('wire','接口协议',connection.get('wire_api','chat_completions'),'choice',choices=[['chat_completions','Chat Completions'],['responses','Responses'],['anthropic_messages','Anthropic Messages']]),
                 hub_field('key','API Key','','secret',hint='本地服务可留空；留空保留已保存密钥。'),
                 hub_field('models','型号',', '.join(connection.get('models',[])),hint='填写检测到的型号，多个用逗号分隔。'),
                 hub_field('maximum','最大上下文',draft.get('maximum'),kind='number',hint='仅在服务未提供规格时填写实际最大值。'),
                 hub_field('output','最大输出',draft.get('output'),kind='number',hint='Anthropic 缺少规格时填写'),
                 hub_field('respect','保留本地服务加载窗口',connection.get('respect_server_context',True),'toggle')]
    if not setup_deepseek(draft) and draft.get('available'):
        rows.append({'type':'note','text':'勾选接入型号，再保存选择。'})
        rows += [hub_field('model_'+str(i),model,model in draft.get('connection',{}).get('models',[]),'toggle') for i,model in enumerate(draft['available'])]
    rows += [hub_field('maintenance','安装后自动检查补丁和模型更新',draft.get('maintenance',True),'toggle')]
    buttons=[hub_button('保存并检测','setup_save')]
    if phase=='draft':buttons.append(hub_button('切换到兼容接口 / 本地模型' if setup_deepseek(draft) else '切换到 DeepSeek 快速接入',
        'setup_compatible' if setup_deepseek(draft) else 'setup_deepseek'))
    if phase=='draft' and draft.get('config_digest'):buttons.append(hub_button('准备并验证','setup_prepare',style='primary'))
    if phase=='prepared':
        rows.append({'type':'note','text':'准备和隔离验证通过。请先正常退出 Codex，再点击安装。'})
        buttons.append(hub_button('安装','setup_install',style='primary'))
    if phase=='installed':
        rows.append({'type':'note','text':'适配层已安装，继续完成设置入口与自动维护。'})
        buttons=[hub_button('完成设置','setup_install',style='primary')]
    return {'route':'setup','title':'首次设置 · '+('DeepSeek 快速接入' if setup_deepseek(draft) else '兼容接口 / 本地模型'),
            'subtitle':'发现应用 → 检测模型 → 验证 → 安装','rows':rows,'buttons':buttons,
            'revision':setup_revision(directory),'edition':p.EDITION,'notice':notice,
            'options':hub_options(directory),'footer':footer_layout(buttons),'back':None}



def recovery_target(directory, baseline):
    """Select only the monitor's owned candidate; never recover another edition."""
    from monitor import load_json
    directory,baseline=Path(directory).resolve(),Path(baseline).resolve()
    settings=load_json(directory/'settings.json',{})
    pending=load_json(directory/'state.json',{}).get('pending',{}).get('build',{}).get('directory')
    if not pending:return baseline
    target=Path(pending).resolve()
    p.require(target.is_relative_to(directory/'builds'),'Recovery candidate is outside this installation')
    manifest=load_json(target/'manifest.json',{})
    p.require(manifest.get('edition')==settings.get('edition') and manifest.get('codex_home')==settings.get('codex_home'),
              'Recovery candidate identity changed')
    return target


def recovery_summary(directory, baseline):
    from monitor import load_json
    try:
        target=recovery_target(directory,baseline)
        manifest=load_json(target/'manifest.json',{})
        install=load_json(target/'native-install-transaction.json',{})
        uninstall=load_json(target/'native-uninstall-transaction.json',{})
        interrupted=(uninstall and uninstall.get('phase')!='complete') or (install and install.get('phase') not in ('committed','rolled_back'))
        return {'candidate_version':manifest.get('tool_version') if target!=Path(baseline).resolve() else None,
                'interrupted':bool(interrupted),'message':'安装中断，可从这里恢复' if interrupted else '当前安装记录完整'}
    except (OSError,ValueError,TypeError,p.PatchError):
        return {'interrupted':False,'message':'安装记录需要核对，请查看诊断'}


def verify_build(output, progress=None):
    output=Path(output)
    manifest=json.loads((output/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('architecture')=='official-client-v1':
        from scripts.test_official_client import run
        manifest.pop('native_probe',None)
        manifest.pop('native_probe_report',None)
        manifest.pop('native_install_validation',None)
        p.save_json(output/'manifest.json',manifest)
        from official_client import ADAPTER_FILES
        p.require(set(manifest.get('runtime_sha256',{})) == set(ADAPTER_FILES),'Native runtime inventory mismatch')
        for name, digest in manifest['runtime_sha256'].items():
            p.require(p.file_sha(output/name) == digest == p.file_sha(p.ROOT/name),
                      'Staged runtime differs from the source being tested: '+name)
        for name, field in [('desktop-models.json','catalog_sha256'),('adapter-profile.json','profile_sha256'),('host-settings.json','host_settings_sha256')]:
            p.require(p.file_sha(output/name) == manifest[field], 'Staged configuration changed: '+name)
        report=run(Path(manifest['source_target']),output/'native-probe',manifest['edition'],
            **({'progress':lambda done,total:progress.publish('verifying',done,total,unit='checks')} if progress else {}))
        p.require(report.get('status')=='passed','Native official-client validation failed')
        manifest.update(native_probe='passed',native_probe_report=str(output/'native-probe'))
        if manifest.get('update',{}).get('monitor_state_dir'):
            # Older UI controllers only know `prepared` and may close the app
            # without waiting for idle. They must defer to the new installer.
            manifest['status']='prepared_idle'
        if manifest.get('status') != 'installed':
            from official_client_install import validate_build
            validate_build(output,manifest)
            manifest['native_install_validation']='passed'
        p.save_json(output/'manifest.json',manifest)
        return report
    from scripts.test_offline import main as offline
    from scripts.test_reasoning import main as reasoning
    from scripts.test_boot import main as boot
    offline(output)
    reasoning(output)
    if manifest.get('proxy') or manifest.get('model_routes'):
        from scripts.test_context import main as context
        context(output)
    if manifest.get('switching_probe_required'):
        from scripts.test_switching import main as switching
        switching(output)
    if manifest.get('tool_loading_probe_required'):
        from scripts.test_tools import main as tools
        tools(output)
    boot(output)


def finish_native_update(directory, manifest, progress=None):
    """Commit independent recovery before old parent code touches its journal."""
    update=manifest.get('update',{})
    package=update.get('package')
    if not package:
        if progress:progress.publish('completed',build=directory,present=True)
        return
    state=update.get('monitor_state_dir') or update.get('progress_state_dir')
    p.require(state and Path(state).is_absolute(),'Native update monitor directory is missing')
    state=Path(state).resolve()
    settings=json.loads((state/'settings.json').read_text(encoding='utf-8'))
    p.require(settings.get('edition')==manifest['edition'],'Native update monitor edition changed')
    from updater import activate_runtime,reconcile_native_activation,NATIVE_RECOVERY
    from monitor import atomic_json
    atomic_json(state/NATIVE_RECOVERY,{'format':1,'edition':manifest['edition'],
        'baseline':str(Path(directory).resolve()),'runtime_digest':update['runtime_digest']})
    atomic_json(state/'activation-pending.json',{'baseline':str(directory),'edition':manifest['edition']})
    settings.update(architecture='official-client-v1',baseline=str(directory),app=manifest['target'])
    atomic_json(state/'settings.json',settings)
    if progress:progress.publish('activating')
    try:
        activate_runtime(Path(package),state,manifest['edition'])
        reconcile_native_activation(state,already_locked=True)
    except Exception:
        # The core install committed. An old parent can release its own cwd and
        # promote; otherwise both durable records survive for the next check.
        return


def execute(action, job, progress=None):
    if action=='identity':
        import updater
        return {**updater.edition(),'architecture':getattr(p,'ARCHITECTURE','legacy'),
                'runtime_digest':updater.runtime_digest(p.ROOT),'files':list(p.SERVICE_RUNTIME_FILES)}
    if action=='sync-models':
        import model_sync
        value=model_sync.synchronize(job['settings'],job['directory'],job['baseline'])
        return {'digest':value['digest'],'warnings':value['warnings']}
    if action=='inspect':
        if job.get('architecture',getattr(p,'ARCHITECTURE',None))=='official-client-v1':
            import official_client
            return official_client.inspect(Path(job['app']))
        value=backend.inspect_source(Path(job['app']))
        return {'compatible':True,'assertions':value['probe']['assertions']}
    if action=='build':
        settings=job['settings'];output=Path(job['output'])
        metadata=dict(job.get('update') or {})
        if settings.get('state_dir'):metadata['monitor_state_dir']=str(Path(settings['state_dir']).resolve())
        if progress:metadata.update(progress.metadata())
        job={**job,'update':metadata}
        if progress:
            progress.publish('building',build=output,present=True)
        if job.get('architecture',getattr(p,'ARCHITECTURE',None))=='official-client-v1':
            import official_client
            preferences={}
            if settings.get('state_dir'):
                path=Path(settings['state_dir'])/'model-preferences.json'
                if path.is_file():preferences=json.loads(path.read_text(encoding='utf-8'))
            official_client.stage(app=Path(job['app']),codex_home=Path(settings['codex_home']),
                output=output,baseline=Path(job['baseline']),edition=settings.get('edition'),
                model_snapshot=job.get('models'),install_target=Path(job['install_target']) if job.get('install_target') else None,
                update_metadata={**(job.get('update') or {}),'architecture':'official-client-v1'},model_preferences=preferences)
            if progress:progress.publish('verifying')
            verify_build(output,progress) if progress else verify_build(output)
            if progress:
                progress.publish('waiting_for_exit',build=output)
                from update_progress import validated_close
                validated_close(progress)
            if settings.get('state_dir'):
                from monitor import migrate_windows_working_directory
                migrate_windows_working_directory(settings['state_dir'])
            return {'directory':str(output),'status':'prepared','architecture':'official-client-v1'}
        args=argparse.Namespace(app=Path(job['app']),codex_home=Path(settings['codex_home']),output=output,
             compatible=True,baseline=Path(job['baseline']),keychain_account=settings['keychain_account'],
             keychain_service=settings['keychain_service'],node_path=settings.get('node_path'),
             install_target=Path(job['install_target']) if job.get('install_target') else None,
             model_snapshot=job.get('models'),update_metadata=job.get('update'))
        backend.build_compatible(args)
        if progress:progress.publish('verifying')
        verify_build(output,progress) if progress else verify_build(output)
        if progress:progress.publish('waiting_for_exit',build=output)
        if settings.get('state_dir'):
            from monitor import migrate_windows_working_directory
            migrate_windows_working_directory(settings['state_dir'])
        return {'directory':str(output),'status':'prepared'}
    if action=='install':
        if progress and progress.paused() is True:raise p.AppRunningError('Update is paused by the user')
        directory=Path(job['directory'])
        manifest=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
        if progress:progress.publish('installing',build=directory)
        if manifest.get('architecture')=='official-client-v1':
            from official_client_install import install
            install(directory,manifest)
            finish_native_update(directory,manifest,progress)
            return {'directory':str(directory),'status':'installed','architecture':'official-client-v1'}
        directory,manifest=p.load_build(Path(job['directory']))
        backend.install_compatible(directory,manifest)
        if progress:progress.publish('completed',build=directory,present=True)
        return {'directory':str(directory),'status':'installed'}
    raise ValueError('Unknown update worker action')


def main(argv=None):
    args=sys.argv[1:] if argv is None else argv
    action,path=args[0],Path(args[1])
    progress=None
    try:
        job=json.loads(path.read_text(encoding='utf-8'))
        from update_progress import worker_progress
        progress=worker_progress(action,job,path)
        if progress and action=='identity':
            # Periodic identity probes must not erase a failed/completed
            # update or replace the state of an update already in progress.
            if progress.read().get('phase') in ('available','checking'):
                progress.publish('checking',present=True)
            else:
                progress=None
        value=execute(action,job,progress)
        p.save_json(path.with_suffix('.result.json'),value)
        return 0
    except p.AppRunningError:
        if action=='install':
            if progress:progress.reopened()
            return 75
        if progress:progress.publish('failed',present=True)
        return 1
    except Exception as error:
        if progress:progress.publish('failed',present=True)
        from monitor import safe_error
        print(safe_error(error),file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
