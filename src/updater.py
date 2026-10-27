"""Edition-pinned code updates and model snapshots for the native monitor."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import urllib.error
import io
import stat
import zipfile

import patch as p
from windows_support import background_options

REPOSITORY = 'limyum-creator/codex-deepseek-patch'
CHANNELS = {'ds':'main', 'proxy':'codex/existing-proxy-models', 'unified':'main'}
UNIFIED_FEED = 'https://raw.githubusercontent.com/' + REPOSITORY + '/main/unified-update.json'
CHECK_SECONDS = 3600
# Full native/UI/failure matrices run once at release promotion. Each user's
# download runs a small, headless contract smoke check; app compatibility is
# verified separately against their bundled CLI before installation.
NATIVE_PREFLIGHT_MODULES = ('tests.test_official_client_config','tests.test_responses_compat',
    'tests.test_model_sync','tests.test_monitor')


def edition(root=None):
    value=json.loads((Path(root or p.ROOT)/'edition.json').read_text(encoding="utf-8"))
    p.require(value.get('edition') in CHANNELS and value.get('branch')==CHANNELS[value['edition']], 'Unknown or crossed update channel')
    return value


def runtime_digest(root, names=None):
    root=Path(root)
    values={name:p.file_sha(root/name) for name in (names or p.SERVICE_RUNTIME_FILES)}
    return p.sha(json.dumps(values,sort_keys=True,separators=(',',':')).encode())


def public_get(url, limit=7_000_000, progress=None):
    parsed=urllib.parse.urlsplit(url)
    raw = parsed.hostname == 'raw.githubusercontent.com' and parsed.path.startswith('/' + REPOSITORY + '/')
    archive = parsed.hostname == 'codeload.github.com' and re.fullmatch('/' + re.escape(REPOSITORY) + '/zip/[0-9a-f]{40}', parsed.path)
    p.require(parsed.scheme=='https' and (raw or archive) and not parsed.query and not parsed.fragment and not parsed.username and not parsed.password,
              'Update files must come from the pinned HTTPS repository')
    from model_sync import NoRedirect
    with urllib.request.build_opener(NoRedirect).open(urllib.request.Request(url,headers={'User-Agent':'CodexModelPatch/1.0'}),timeout=30) as response:
        header=response.headers.get('Content-Length','')
        total=int(header) if header.isdecimal() and int(header)>0 else None
        chunks=[];done=0;last_report=0
        while chunk:=response.read(65536):
            chunks.append(chunk);done+=len(chunk)
            p.require(done<=limit,'Update file is too large')
            now=time.monotonic()
            if progress and (now-last_report>=.1 or total and done>=total):
                progress(done,total);last_report=now
        raw=b''.join(chunks)
        if progress:progress(done,total or done or None)
    p.require(len(raw)<=limit,'Update file is too large')
    return raw


def validate_release(value, channel, highest=0):
    p.require(isinstance(value,dict) and value.get('format')==1,'Unknown release manifest')
    p.require(p.runtime_accepts(value.get('edition'),channel) and value.get('branch')==CHANNELS[value['edition']], 'Release belongs to a different edition')
    p.require(value.get('architecture') in (None,'legacy','official-client-v1'),'Unknown release architecture')
    p.require(type(value.get('sequence')) is int and value['sequence']>=highest and value['sequence']>0,'Release rollback refused')
    p.require(re.fullmatch(r'[0-9a-f]{40}',value.get('commit','')),'Release must pin an immutable commit')
    p.require(isinstance(value.get('version'),str) and len(value['version'])<80,'Invalid release version')
    files=value.get('files',{})
    p.require(isinstance(files,dict) and 8<=len(files)<=100,'Invalid release file inventory')
    for name,digest in files.items():
        path=PurePosixPath(name)
        p.require(isinstance(name,str) and not path.is_absolute() and '..' not in path.parts and '\\' not in name
                  and all(part not in ('','.git') and ':' not in part for part in path.parts),'Unsafe release path')
        p.require(re.fullmatch(r'[0-9a-f]{64}',digest),'Invalid release digest')
    sizes=value.get('file_sizes')
    if sizes is not None:
        p.require(isinstance(sizes,dict) and set(sizes)==set(files) and
                  all(type(n) is int and 0<=n<=100000 for n in sizes.values()),'Invalid release byte inventory')
    required={'patch.py','monitor.py','updater.py','update_worker.py','model_sync.py','edition.json',
              'tests/test_updater.py','tests/test_model_sync.py','tests/route.test.cjs'}
    if value.get('architecture')=='official-client-v1':
        required.update({'official_client.py','official_client_install.py','official_client_update.py',
                         'official_client_config.py','official_client_host.py','inference_adapter.py',
                         'inference_transport.py','legacy_history.py','reasoning_origin.py','chat_completions_compat.py',
                         'official_client_migration.py','scripts/test_official_client.py',
                         'update_progress.py','update_progress_macos.js','update_progress.ps1',
                         'tests/test_official_client_update.py'})
        required.update(name.replace('.','/')+'.py' for name in NATIVE_PREFLIGHT_MODULES)
    p.require(required<=files.keys(),'Incomplete update package')
    if value.get('archive') is not None:
        p.require(value['archive'] == archive_descriptor(value['commit']), 'Invalid pinned update archive')
    return value


def archive_descriptor(commit):
    return {'format': 'github-zip-v1', 'url': 'https://codeload.github.com/' + REPOSITORY + '/zip/' + commit}


def unpack_archive(raw, release, stage, progress=None):
    """An archive is transport only; inventory, size and each digest stay mandatory."""
    prefix = REPOSITORY.split('/')[-1] + '-' + release['commit'] + '/'
    files = release['files']
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = {}
        for item in archive.infolist():
            p.require(item.filename.startswith(prefix), 'Archive has a different root')
            name = item.filename[len(prefix):]
            path = PurePosixPath(name)
            p.require(not path.is_absolute() and '..' not in path.parts and '\\' not in name and ':' not in name,
                      'Unsafe archive path')
            p.require(not stat.S_ISLNK(item.external_attr >> 16), 'Archive links are not allowed')
            if item.is_dir():
                p.require(not name or any(n.startswith(name) for n in files), 'Unexpected archive directory')
                continue
            p.require(name in files and name not in members and 0 <= item.file_size <= 100_000,
                      'Invalid archive inventory or file size')
            members[name] = item
        p.require(set(members) == set(files), 'Incomplete update archive')
        for index, (name, item) in enumerate(members.items(), 1):
            data = archive.read(item)
            p.require(hashlib.sha256(data).hexdigest() == files[name], 'Downloaded update digest mismatch: ' + name)
            destination = Path(stage) / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            if progress:progress.publish('checking_package', index, len(files)+1,unit='files')


def source_package(release, directory, fetch=public_get, progress=None):
    directory=Path(directory)
    target=directory/'packages'/release['commit']
    def verify(root, report=False):
        for index,(name,digest) in enumerate(release['files'].items(),1):
            path=root/name
            if release.get('file_sizes'):p.require(path.stat().st_size==release['file_sizes'][name],'Update size mismatch: '+name)
            p.require(path.is_file() and not path.is_symlink() and p.file_sha(path)==digest,'Update file digest mismatch: '+name)
            if report and progress:progress.publish('checking_package',index,len(release['files'])+1,unit='files')
        info=edition(root)
        p.require(info['edition']==release['edition'] and info['version']==release['version'],'Package edition/version mismatch')
    if target.exists():
        verify(target,report=True)
        if progress:progress.publish('checking_package',len(release['files'])+1,len(release['files'])+1,unit='files')
        return target
    target.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix='package-',dir=target.parent))
    try:
        import concurrent.futures
        import threading
        count_lock=threading.Lock();received={};last_report=[0]
        sizes=release.get('file_sizes',{})
        total=sum(sizes.values()) if sizes else None
        def file_progress(name, done, expected):
            if not progress:return
            with count_lock:
                received[name]=done
                now=time.monotonic()
                if now-last_report[0]>=.1 or total and sum(received.values())==total:
                    progress.publish('downloading',sum(received.values()),total,unit='bytes')
                    last_report[0]=now
        def download(item):
            name,digest=item
            url='https://raw.githubusercontent.com/'+REPOSITORY+'/'+release['commit']+'/'+name
            raw=public_get(url,progress=lambda done,total:file_progress(name,done,total)) if fetch is public_get else fetch(url)
            p.require(hashlib.sha256(raw).hexdigest()==digest,'Downloaded update digest mismatch: '+name)
            if sizes:p.require(len(raw)==sizes[name],'Downloaded update size mismatch: '+name)
            path=stage/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
            file_progress(name,len(raw),len(raw))
        archived = False
        if release.get('archive') is not None:
            p.require(release['archive'] == archive_descriptor(release['commit']), 'Invalid pinned update archive')
            try:
                raw = public_get(release['archive']['url'],progress=(lambda done,total:progress.publish('downloading',done,total,unit='bytes')) if progress else None) if fetch is public_get else fetch(release['archive']['url'])
            except (OSError, TimeoutError, urllib.error.URLError):
                # Older feeds and unavailable archive delivery retain the same
                # pinned file transport. Integrity failures never fall back.
                pass
            else:
                unpack_archive(raw, release, stage, progress)
                archived = True
        if not archived:
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(download,release['files'].items()))
        verify(stage,report=not archived)
        if progress:progress.publish('checking_package',len(release['files']),len(release['files'])+1,unit='files')
        # Do not expose user credentials/configuration to package preflight tests.
        with tempfile.TemporaryDirectory(prefix='codex-update-preflight-') as temp:
            from scripts.test_offline import isolated_environment
            env=isolated_environment(Path(temp))
            if release.get('architecture')=='official-client-v1':
                commands=[[sys.executable,'-c',
                    "import ast,pathlib;[ast.parse(p.read_bytes(),filename=str(p)) for p in pathlib.Path('.').rglob('*.py')]"],
                    [sys.executable,'-m','unittest',*NATIVE_PREFLIGHT_MODULES,'-q']]
            else:
                commands=[[sys.executable,'-m','unittest','discover','-s','tests','-q']]
                commands.append([shutil.which('node'),'tests/route.test.cjs'])
            for command in commands:
                result=subprocess.run(command,cwd=stage,env=env,capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=120,**background_options())
                p.require(result.returncode==0,'Downloaded package preflight failed: '+result.stderr[-1500:])
        if progress:progress.publish('checking_package',len(release['files'])+1,len(release['files'])+1,unit='files')
        from monitor import atomic_json
        atomic_json(stage / '.package.json', {'format': 1, 'release': release})
        os.replace(stage,target)
        return target
    finally:
        if stage.exists():shutil.rmtree(stage)


def worker(root, action, job, work_directory):
    root=Path(root);directory=Path(work_directory);directory.mkdir(parents=True,exist_ok=True)
    path=directory/('worker-'+action+'.json');p.save_json(path,job)
    with (directory/('worker-'+action+'.log')).open('w',encoding='utf-8') as log:
        result=subprocess.run([sys.executable,str(root/'update_worker.py'),action,str(path)],
                              cwd=root,stdout=log,stderr=log,timeout=1800,**background_options())
    if action=='install' and result.returncode==75:
        raise p.AppRunningError('Codex reopened before installation; the prepared update is retained')
    p.require(result.returncode==0,'Update worker failed during '+action+'; see its local log')
    return json.loads(path.with_suffix('.result.json').read_text(encoding="utf-8"))


def activate_runtime(package, directory, expected_edition, *, checkpoint=None):
    package=Path(package);directory=Path(directory)
    identity=worker(package,'identity',{},directory/'activation')
    p.require(p.runtime_accepts(identity['edition'],expected_edition),'Runtime edition mismatch')
    runtime=directory/'runtime'
    if runtime.resolve()==package.resolve():return
    try:
        if p.runtime_accepts(edition(runtime)['edition'],expected_edition) and runtime_digest(runtime,identity['files'])==identity['runtime_digest']:return
    except (OSError,ValueError,p.PatchError):pass
    stage=Path(tempfile.mkdtemp(prefix='runtime-stage-',dir=directory))
    backup=None
    try:
        for name in identity['files']:
            source=package/name
            p.require(source.is_file() and not source.is_symlink(),'Invalid runtime source')
            destination=stage/name;destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,destination)
        p.require(runtime_digest(stage,identity['files'])==identity['runtime_digest'],'Staged runtime differs from tested code')
        runtime=directory/'runtime'
        if Path.cwd().resolve().is_relative_to(runtime.resolve()):
            os.chdir(directory)  # Windows cannot rename its process working directory.
        backup=directory/('runtime-backup-'+str(time.time_ns())) if runtime.exists() else None
        old=worker(runtime,'identity',{'quiet_progress':True},directory/'activation-before') if backup else {}
        p.require(not backup or p.runtime_accepts(old['edition'],expected_edition),'Previous runtime edition changed')
        journal={'directory':str(directory.resolve()),'backup':backup.name if backup else None,
            'previous_digest':old.get('runtime_digest'),
            'previous_files':old.get('files',[]),
            'files':identity['files'],'digest':identity['runtime_digest'],'edition':expected_edition}
        # Persist rename intent before moving either directory. The install
        # journal owns recovery even if the process stops between the renames.
        if checkpoint:checkpoint(journal)
        if backup:os.replace(runtime,backup)
        try:
            os.replace(stage,runtime)
            ready=worker(runtime,'identity',{'quiet_progress':True},directory/'activation-confirm')
            p.require(p.runtime_accepts(ready['edition'],expected_edition) and ready['runtime_digest']==identity['runtime_digest'],
                      'Activated update runtime failed its startup identity check')
        except BaseException:
            rollback_runtime(journal)
            raise
    finally:
        if stage.exists():shutil.rmtree(stage)


def rollback_runtime(journal):
    """Restore only the two owned trees named by durable activation intent."""
    directory=Path(journal['directory'])
    p.require(directory.is_absolute() and directory.resolve()==directory and not directory.is_symlink(),
              'Unsafe runtime recovery directory')
    runtime=directory/'runtime';name=journal.get('backup')
    p.require(name is None or re.fullmatch(r'runtime-backup-\d+',name),'Unsafe runtime recovery backup')
    backup=directory/name if name else None
    files=journal['files'];oldfiles=journal.get('previous_files',[])
    for filename in [*files,*oldfiles]:
        p.require(isinstance(filename,str) and not Path(filename).is_absolute() and '..' not in Path(filename).parts,
                  'Unsafe runtime recovery inventory')
    def digest(path,inventory):
        p.require(not path.is_symlink() and all(not (path/f).is_symlink() for f in inventory),'Runtime recovery contains links')
        return runtime_digest(path,inventory)
    if backup and not backup.exists():
        p.require(runtime.exists() and digest(runtime,oldfiles)==journal['previous_digest'],
                  'Previous update runtime is unavailable')
        return
    if runtime.exists():
        if backup:
            try:
                if digest(runtime,oldfiles)==journal['previous_digest']:return
            except FileNotFoundError:pass
        p.require(digest(runtime,files)==journal['digest'],'Runtime changed independently; recovery retained')
    if backup:p.require(digest(backup,oldfiles)==journal['previous_digest'],'Runtime recovery backup changed')
    if Path.cwd().resolve().is_relative_to(runtime.resolve()):os.chdir(directory)
    if runtime.exists():shutil.rmtree(runtime)
    if backup:os.replace(backup,runtime)


NATIVE_RECOVERY = 'native-activation-recovery.json'


def prune_update_cache(directory, *, keep=2, min_age=7 * 86400, dry_run=True, now=None, already_locked=False):
    """Delete only recognized, old, unreferenced artifacts. Recovery chains win."""
    from contextlib import nullcontext
    from monitor import load_json, state_lock
    directory = Path(directory).resolve()
    now = time.time() if now is None else now
    p.require(type(keep) is int and keep >= 2 and min_age >= 86400, 'Keep at least two recent rollback artifacts')
    with (nullcontext(True) if already_locked else state_lock(directory)) as locked:
        p.require(locked, 'The update monitor is busy')
        settings = load_json(directory / 'settings.json', {})
        channel = settings.get('edition')
        p.require(channel in CHANNELS, 'Cleanup needs an installed channel')
        state = load_json(directory / 'state.json', {})
        p.require(state.get('phase') not in ('building', 'installing'), 'Wait for preparation or installation before cleanup')
        candidates = []
        for group, parent, pattern in [('packages', directory/'packages', '*'),
                ('builds', directory/'builds', '*'), ('runtime', directory, 'runtime-backup-*')]:
            if not parent.is_dir() or parent.is_symlink():continue
            for path in parent.glob(pattern):
                if path.is_symlink() or not path.is_dir():continue
                try:
                    if group == 'packages':
                        owner = load_json(path / '.package.json', {})
                        release = validate_release(owner.get('release'), channel)
                        if owner.get('format') != 1 or release['commit'] != path.name:continue
                    elif group == 'builds':
                        owner = load_json(path / 'manifest.json', {})
                        if owner.get('edition') != channel or owner.get('architecture') != 'official-client-v1':continue
                        if owner.get('status') not in ('prepared', 'installed', 'restored', 'rolled_back'):continue
                    elif edition(path).get('edition') != channel or not all((path/n).is_file() for n in ('patch.py','monitor.py')):
                        continue
                    candidates.append((group, path.resolve(), path.stat().st_mtime))
                except (OSError, ValueError, KeyError, p.PatchError):
                    continue  # Unknown data is never cleanup material.
        protected = set()
        documents = [settings, state]
        for name in ('updates.json', 'activation-pending.json', NATIVE_RECOVERY, 'progress.context.json'):
            documents.append(load_json(directory / name, {}))
        # Even an unreferenced failed build may contain an unfinished rollback.
        for _, path, _ in candidates:
            for name in ('native-install-transaction.json', 'native-uninstall-transaction.json'):
                transaction = load_json(path / name, {})
                if transaction and transaction.get('phase') not in ('committed', 'complete', 'completed', 'rolled_back'):
                    protected.add(path)
                    documents.append(transaction)
        visited = set()
        def visit(value):
            if isinstance(value, dict):
                for child in value.values():visit(child)
            elif isinstance(value, list):
                for child in value:visit(child)
            elif isinstance(value, str) and len(value) < 4096:
                path = Path(value)
                if not path.is_absolute():return
                path = path.resolve()
                for _, target, _ in candidates:
                    if path == target or path.is_relative_to(target):protected.add(target)
                if path in visited:return
                visited.add(path)
                if len(visited) > 4096:raise ValueError('Too many recovery references; cleanup deferred')
                manifest = path/'manifest.json'
                if path.is_dir() and manifest.is_file() and not manifest.is_symlink():
                    data = load_json(manifest, {})
                    if data.get('edition') == channel:
                        visit(data)
                        for name in ('native-install-transaction.json','native-uninstall-transaction.json'):
                            visit(load_json(path/name, {}))
        for value in documents:visit(value)
        for group in ('packages', 'builds', 'runtime'):
            recent = sorted((row for row in candidates if row[0] == group), key=lambda row: row[2], reverse=True)
            protected.update(path for _, path, _ in recent[:keep])
        selected = []
        for group, path, modified in candidates:
            if path in protected or now - modified < min_age:continue
            entries = list(path.rglob('*'))
            if any(item.is_symlink() for item in entries):continue
            size = sum(item.stat().st_size for item in entries if item.is_file())
            selected.append({'group': group, 'name': path.name, 'bytes': size})
            if not dry_run:shutil.rmtree(path)
        return {'dry_run': dry_run, 'items': selected, 'bytes': sum(v['bytes'] for v in selected),
                'protected': len(protected)}


def reconcile_native_activation(directory, already_locked=False):
    """Import a committed native build before old state can select legacy code."""
    from contextlib import nullcontext
    from monitor import atomic_json,load_json,state_lock
    directory=Path(directory).resolve();marker=directory/NATIVE_RECOVERY
    if not marker.exists():return None
    with (nullcontext(True) if already_locked else state_lock(directory)) as locked:
        if not locked:return None
        recovery=load_json(marker)
        settings=load_json(directory/'settings.json',{})
        p.require(isinstance(recovery,dict) and recovery.get('format')==1,
                  'Unknown native activation recovery record')
        p.require(recovery.get('edition')==settings.get('edition')==p.EDITION,
                  'Native activation recovery belongs to another edition')
        p.require(Path(recovery['baseline']).is_absolute(),'Unsafe native activation recovery build')
        build=Path(recovery['baseline']).resolve()
        p.require(not (build/'manifest.json').is_symlink(),'Unsafe native activation recovery build')
        manifest=load_json(build/'manifest.json',{})
        p.require(manifest.get('architecture')=='official-client-v1' and manifest.get('status')=='installed'
                  and manifest.get('edition')==recovery['edition'], 'Native activation commit is missing')
        expected=manifest.get('update',{}).get('runtime_digest')
        p.require(isinstance(expected,str) and re.fullmatch('[0-9a-f]{64}',expected)
                  and expected==recovery.get('runtime_digest'), 'Native activation runtime identity changed')
        state=load_json(directory/'state.json',{'format':1,'phase':'idle','terminal':{},'notices':{}})
        pending=load_json(directory/'activation-pending.json',{})
        # Choose the latest committed native fact; never let an older marker
        # or an old monitor's stale installed baseline reverse a later install.
        candidates=[settings.get('baseline'),state.get('installed',{}).get('baseline'),pending.get('baseline')]
        for current in candidates:
            if not current or Path(current).resolve()==build:continue
            current=Path(current).resolve();prior=load_json(current/'manifest.json',{})
            if prior.get('architecture')!='official-client-v1' or prior.get('status')!='installed':continue
            p.require(prior.get('edition')==recovery['edition'],'Native baseline belongs to another edition')
            direct=manifest.get('legacy_baseline')
            if direct and Path(direct).resolve()==current:continue
            reverse=prior.get('legacy_baseline')
            current_newer=bool(reverse and Path(reverse).resolve()==build)
            old_sequence=(prior.get('update',{}).get('release') or {}).get('sequence',0)
            new_sequence=(manifest.get('update',{}).get('release') or {}).get('sequence',0)
            if not current_newer and old_sequence!=new_sequence:
                current_newer=old_sequence>new_sequence
                if not current_newer:continue
            elif not current_newer:
                p.require(prior.get('installed_at') and manifest.get('installed_at')
                          and prior['installed_at']!=manifest['installed_at'],
                          'Cannot order two native activation commits')
                current_newer=prior['installed_at']>manifest['installed_at']
            if current_newer:build,manifest=current,prior
        expected=manifest.get('update',{}).get('runtime_digest')
        p.require(isinstance(expected,str) and re.fullmatch('[0-9a-f]{64}',expected)
                  and p.runtime_accepts(edition(directory/'runtime')['edition'],recovery['edition'])
                  and runtime_digest(directory/'runtime')==expected,
                  'Native runtime activation is still pending')
        if (build/'native-install-transaction.json').is_file():
            from official_client_install import recover
            p.require(recover(build).get('status')=='committed','Native installed resources need recovery')
        settings.update(architecture='official-client-v1',baseline=str(build),app=manifest['target'])
        state.setdefault('installed',{})['baseline']=str(build)
        state.update(phase='installed',adoption_checked=False)
        state.pop('pending',None);state.pop('reason',None)
        atomic_json(directory/'settings.json',settings)
        atomic_json(directory/'state.json',state)
        from update_progress import from_manifest
        progress=from_manifest(manifest)
        if progress:progress.publish('completed',build=build,present=True)
        if pending.get('baseline') and Path(pending['baseline']).resolve()!=build:
            (directory/'activation-pending.json').unlink(missing_ok=True)
        marker.unlink()
        return {'status':'reconciled','baseline':str(build)}


class UpdateBackend:
    """Existing monitor state machine, with app/code/model composite identities."""
    def __init__(self, settings):
        from official_client_update import select_backend
        self.settings=settings
        self.base=select_backend(settings)
        self.directory=Path(settings['state_dir'])
        self.edition=settings['edition']
        p.require(p.runtime_accepts(edition()['edition'],self.edition),'Installed runtime is from another edition')
        self.cache_path=self.directory/'updates.json'
        self.cache=json.loads(self.cache_path.read_text(encoding="utf-8")) if self.cache_path.exists() else {}
        p.require(self.cache.get('edition',self.edition)==self.edition,'Update state belongs to another edition')
        self.cache['edition']=self.edition
        installed=self.installed_metadata().get('release') or {}
        self.cache['highest_sequence']=max(self.cache.get('highest_sequence',0),installed.get('sequence',0))
        pending=self.directory/'activation-pending.json'
        if pending.exists():
            build=Path(json.loads(pending.read_text(encoding="utf-8"))['baseline'])
            record=json.loads((build/'manifest.json').read_text(encoding="utf-8"))
            if record.get('status')=='installed':
                self.settings['recovery_build']=str(build)
                self.set_baseline(build)
        self.prepared=False
        self.package=p.ROOT
        self.model_snapshot=None
        self.desired_runtime=runtime_digest(p.ROOT)
        self.desired_architecture=getattr(p,'ARCHITECTURE','legacy')
        self.warnings=[]
        self.progress=None

    def set_baseline(self, build):
        """Switch recovery semantics only after a native transaction committed."""
        self.base.baseline=Path(build)
        record=json.loads((Path(build)/'manifest.json').read_text(encoding='utf-8'))
        if record.get('architecture')=='official-client-v1':
            from official_client_update import select_backend
            self.settings.update(baseline=str(build),architecture='official-client-v1')
            self.base=select_backend(self.settings,build)
            self.settings['app']=str(self.base.app)

    def persist_architecture(self, baseline):
        record=json.loads((Path(baseline)/'manifest.json').read_text(encoding='utf-8'))
        if record.get('architecture')!='official-client-v1':return
        self.settings['architecture']='official-client-v1'
        settings_path=self.directory/'settings.json'
        if settings_path.exists():
            from monitor import atomic_json
            settings=json.loads(settings_path.read_text(encoding='utf-8'))
            p.require(settings.get('edition',self.edition)==self.edition,'Monitor edition changed during activation')
            previous=dict(settings)
            settings.update(architecture='official-client-v1',baseline=str(baseline),app=str(record['target']))
            if settings!=previous:atomic_json(settings_path,settings)

    def source_for_release(self, release, progress=None):
        installed=self.installed_metadata()
        # One-time legacy bridge already contains the unified core. Metadata
        # alone must not force a second install/restart of the same version.
        # Recheck every executable/runtime file against the canonical feed.
        current=edition(p.ROOT)
        if (release.get('edition')=='unified' and current['edition'] in ('ds','proxy')
                and current['version']==release['version']
                and all(name in release['files'] and (p.ROOT/name).is_file()
                        and p.file_sha(p.ROOT/name)==release['files'][name]
                        for name in p.SERVICE_RUNTIME_FILES if name!='edition.json')):
            return p.ROOT
        if ((installed.get('release') or {}).get('commit')==release['commit']
                and p.runtime_accepts(edition(p.ROOT)['edition'],self.edition)
                and installed.get('runtime_digest')==runtime_digest(p.ROOT)):
            return p.ROOT
        return source_package(release,self.directory,progress=progress)

    def prepare(self):
        if self.prepared:return
        pending=self.directory/'activation-pending.json'
        if pending.exists():
            build=Path(json.loads(pending.read_text(encoding="utf-8"))['baseline'])
            if json.loads((build/'manifest.json').read_text(encoding="utf-8")).get('status')=='installed':
                self.set_baseline(build)
                self.promote(build)
            pending.unlink()
        # An older monitor may have installed/promoted the new worker without
        # knowing this settings field. Import it from the committed baseline.
        if self.base.baseline:self.persist_architecture(self.base.baseline)
        self.prepared=True
        from update_progress import Progress, _build
        accepted=Progress(self.directory)
        pinned=_build(accepted) if accepted.requested() and accepted.read().get('phase') not in ('completed','failed','rolled_back') else None
        if pinned:
            _, saved=pinned;meta=saved.get('update',{})
            package=Path(meta.get('package') or p.ROOT)
            p.require(package.is_dir() and p.runtime_accepts(edition(package)['edition'],self.edition),'Accepted update package is unavailable')
            p.require(runtime_digest(package)==meta['runtime_digest'],'Accepted update package changed')
            self.package=package;self.desired_runtime=meta['runtime_digest']
            self.desired_architecture=saved['architecture'];self.progress=accepted
            models=self.directory/'models.json'
            if models.exists():
                self.model_snapshot=json.loads(models.read_text(encoding='utf-8'))
                p.require(self.model_snapshot.get('digest')==meta['model_digest'],'Accepted model snapshot changed; prepare again')
            return
        now=time.time();force=self.settings.get('force_sync',False)
        force_code=self.settings.get('force_code',False)
        if (self.settings.get('auto_update',True) or force_code) and (force or force_code or now-self.cache.get('code_checked',0)>=CHECK_SECONDS):
            self.cache['code_checked']=now
            from update_progress import CheckStatus
            self.check_status=CheckStatus(self.directory,self.edition)
            self.check_status.publish('checking')
            try:
                url=UNIFIED_FEED
                release=validate_release(json.loads(public_get(url,200000)),self.edition,self.cache.get('highest_sequence',0))
                previous=self.cache.get('release') or self.installed_metadata().get('release')
                if previous and release['sequence']==previous['sequence']:
                    p.require(release['commit']==previous['commit'],'A published release sequence was changed')
                numbers=lambda v:tuple(int(x) for x in re.findall(r'\d+',v))
                p.require(numbers(release['version'])>=numbers(edition()['version']),'Older patch version refused')
                self.cache['highest_sequence']=release['sequence']
                # A feed revision alone is not an installation change. Bind UI
                # intent once, after comparing the actual app/runtime/model state.
                self.package=self.source_for_release(release,progress=self.check_status)
                self.cache['release']=release
                self.cache['update_channel']='unified'
            except Exception as error:
                from monitor import safe_error
                self.check_status.publish('failed')
                self.warnings.append('Patch update check: '+safe_error(error))
                if self.progress:self.progress.publish('failed',present=True)
        if self.cache.get('release'):
            try:
                validate_release(self.cache['release'],self.edition,self.cache['highest_sequence'])
                self.package=self.source_for_release(self.cache['release'])
            except Exception:
                self.warnings.append('Cached update package is unavailable; using the installed runtime')
                self.package=p.ROOT
        identity=worker(self.package,'identity',{'quiet_progress':True},self.directory/'identity')
        p.require(p.runtime_accepts(identity['edition'],self.edition),'Candidate runtime belongs to another edition')
        self.desired_architecture=identity.get('architecture','legacy')
        p.require(self.desired_architecture in ('legacy','official-client-v1'),'Unknown candidate runtime architecture')
        from official_client_update import read_manifest
        if read_manifest(self.base.baseline).get('architecture')=='official-client-v1':
            p.require(self.desired_architecture=='official-client-v1','Native client updates cannot restore a legacy app patch')
        self.desired_runtime=identity['runtime_digest']
        if (not self.settings.pop('skip_model_sync', False)
                and (self.settings.get('model_sync',True) or force)
                and (force or now-self.cache.get('models_checked',0)>=CHECK_SECONDS)):
            self.cache['models_checked']=now
            try:
                job={'settings':self.settings,'directory':str(self.directory),'baseline':str(self.base.baseline)}
                worker(self.package,'sync-models',job,self.directory/'discovery')
            except Exception as error:
                from monitor import safe_error
                self.warnings.append('Model synchronization: '+safe_error(error))
        models=self.directory/'models.json'
        if models.exists():
            self.model_snapshot=json.loads(models.read_text(encoding="utf-8"))
            p.require(self.model_snapshot.get('edition')==self.edition,'Model snapshot belongs to another edition')
            self.warnings.extend(self.model_snapshot.get('warnings',[]))
        p.save_json(self.cache_path,self.cache)

    @staticmethod
    def combined(app_fingerprint,runtime,models):
        return p.sha(json.dumps([app_fingerprint,runtime,models],separators=(',',':')).encode())

    def snapshot(self):
        # Discover the current Store package before metadata refresh; an old
        # version directory may already have been removed by the vendor updater.
        if not self.prepared:
            refresh = getattr(self.base, 'refresh_source', None)
            if refresh:
                refresh()
            else:
                self.base.snapshot()
            self.settings['app']=str(self.base.app)
        self.prepare()
        app=self.base.snapshot()
        self.settings['app']=str(self.base.app)
        from official_client_update import read_manifest
        record=read_manifest(self.base.baseline)
        self.quiet_model_sync=self.model_only_change(app,self.desired_runtime,record)
        model_digest=self.model_snapshot['digest'] if self.model_snapshot else self.installed_metadata().get('model_digest','legacy')
        if not self.quiet_model_sync:
            from update_progress import for_settings
            self.progress=for_settings(self.settings,root=self.package,
                version=edition(self.package)['version'],force=True,runtime=self.desired_runtime,
                app_hash=next((v for k,v in app.get('hashes',{}).items() if k.endswith('app.asar')),None))
        else:
            from update_progress import Progress
            progress=Progress(self.directory)
            value=progress.read()
            installed_version=(record.get('update',{}).get('release') or {}).get('version')
            if value.get('version')==installed_version and value.get('phase') in ('available','checking','failed'):
                progress.publish('current')
        check=getattr(self,'check_status',None)
        if check:
            if self.quiet_model_sync and not self.warnings:check.publish('current')
            check.finish()
        return {**app,'app_fingerprint':app['fingerprint'],'runtime_digest':self.desired_runtime,
                'model_digest':model_digest,'fingerprint':self.combined(app['fingerprint'],self.desired_runtime,model_digest),
                'warnings':self.warnings}

    @staticmethod
    def model_only_change(app, runtime, installed):
        metadata=installed.get('update',{})
        asars=[digest for name,digest in app.get('hashes',{}).items() if name.endswith('/app.asar')]
        return (installed.get('architecture')=='official-client-v1' and installed.get('status')=='installed'
                and runtime==metadata.get('runtime_digest') and asars==[installed.get('original_asar')]
                and app.get('version')==installed.get('app_version') and app.get('build')==installed.get('app_build')
                and (not metadata.get('app_fingerprint') or metadata['app_fingerprint']==app.get('fingerprint')))

    def installed_metadata(self, baseline=None):
        baseline=baseline or self.base.baseline
        if not baseline:return {}
        return json.loads((Path(baseline)/'manifest.json').read_text(encoding="utf-8")).get('update',{})

    def source(self):
        manifest=json.loads((self.base.baseline/'manifest.json').read_text(encoding="utf-8"))
        if manifest.get('architecture')=='official-client-v1':
            return self.base.app,None
        if sys.platform=='darwin' and p.file_sha(p.app_layout(self.base.app)['asar'])==manifest.get('patched_asar'):
            original=self.base.baseline/'original.app'
            if not original.exists() and manifest.get('backup'):
                original=Path(manifest['backup'])/'ChatGPT.app'
            p.require(original.exists(),'The signed original backup is required for code/model updates')
            return original,self.base.app
        return self.base.app,None

    def inspect(self, snapshot):
        try:
            source,target=self.source()
            result=worker(self.package,'inspect',{'app':str(source),
                          'architecture':getattr(self,'desired_architecture',getattr(p,'ARCHITECTURE','legacy'))},self.directory/'inspection')
            p.require(result.get('compatible') is True,'Candidate rejected the official source')
            return {'compatible':True}
        except Exception as error:return {'compatible':False,'reason':str(error)}

    def build(self, snapshot, output):
        p.require(self.snapshot()['fingerprint']==snapshot['fingerprint'],'Update changed before building')
        source,target=self.source()
        output.parent.mkdir(parents=True,exist_ok=True)
        quiet=getattr(self,'quiet_model_sync',False) is True
        if not quiet and not getattr(self,'progress',None):
            from update_progress import for_settings
            self.progress=for_settings(self.settings,root=self.package,force=True)
        job={'app':str(source),'install_target':str(target) if target else None,'baseline':str(self.base.baseline),
             'output':str(output),'settings':self.settings,'models':self.model_snapshot,
             'architecture':getattr(self,'desired_architecture',getattr(p,'ARCHITECTURE','legacy')),
             'update':{'edition':self.edition,'runtime_digest':self.desired_runtime,
                       'architecture':getattr(self,'desired_architecture',getattr(p,'ARCHITECTURE','legacy')),
                       'model_digest':snapshot['model_digest'],'package':str(self.package),
                       'app_fingerprint':snapshot.get('app_fingerprint'), 'quiet_model_sync':quiet,
                       'monitor_state_dir':str(self.directory),
                       'release':self.cache.get('release'),
                       **(self.progress.metadata() if self.progress and not quiet else {})}}
        worker(self.package,'build',job,output.parent/(output.name+'-job'))
        return {'directory':str(output)}

    def progress_phase(self,phase):
        if phase=='failed' and getattr(self,'check_status',None):self.check_status.publish('failed')
        if getattr(self,'quiet_model_sync',False) is True:return
        try:
            if not getattr(self,'progress',None):
                from update_progress import Progress,for_settings
                if phase=='available':self.progress=for_settings(self.settings,root=self.package,force=True)
                value=Progress(self.directory).read()
                if value and value.get('edition')==self.edition:self.progress=Progress(self.directory)
            if getattr(self,'progress',None):
                # Re-observing the same candidate must not reset an in-flight
                # update, its prepared build or the user's single start click.
                if phase=='available' and self.progress.read().get('phase') not in ('available','current'):
                    return
                self.progress.publish(phase,present=phase in ('available','completed','failed'))
        except Exception:pass  # Optional view state cannot block runtime activation.

    def request_close(self,build):
        if getattr(self,'quiet_model_sync',False) is True:return
        from update_progress import from_manifest,validated_close
        record=json.loads((Path(build['directory'])/'manifest.json').read_text(encoding='utf-8'))
        progress=from_manifest(record)
        if progress:validated_close(progress)

    def is_running(self):return self.base.is_running()

    def install_allowed(self):
        from update_progress import Progress
        return not Progress(self.directory).paused()

    def watch_exit(self,build):
        from exit_watch import arm
        arm(self.directory)

    def promote(self, baseline):
        metadata=self.installed_metadata(baseline)
        package=metadata.get('package')
        self.progress_phase('activating')
        if package and Path(package).exists():activate_runtime(package,self.directory,self.edition)
        self.persist_architecture(baseline)
        self.progress_phase('completed')

    def install(self, build, snapshot):
        p.require(self.snapshot()['fingerprint']==snapshot['fingerprint'],'Update changed before installation')
        directory=Path(build['directory'])
        journal=self.directory/'activation-pending.json'
        p.save_json(journal,{'baseline':str(directory),'edition':self.edition})
        worker(self.package,'install',{'directory':str(directory)},directory/'install-job')
        self.set_baseline(directory)
        # App installation is already committed. Retain a separate durable
        # activation journal so even a caught promotion error retries next tick.
        try:
            self.promote(directory)
            journal.unlink()
            self.progress_phase('completed')
        except Exception:
            pass
        after=self.base.snapshot()
        return {'installed_fingerprint':self.combined(after['fingerprint'],self.desired_runtime,snapshot['model_digest']),
                'baseline':str(directory)}

    def adopt(self):
        result=self.base.adopt()
        if not result:return None
        metadata=self.installed_metadata()
        if metadata:
            p.require(metadata['edition']==self.edition,'Installed build belongs to another edition')
            if self.settings.get('recovery_build'):self.promote(self.base.baseline)
        return {**result,'fingerprint':self.combined(result['fingerprint'],metadata.get('runtime_digest','legacy'),metadata.get('model_digest','legacy'))}
