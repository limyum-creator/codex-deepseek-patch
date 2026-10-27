"""One sleeping process-exit observer, only while a validated update is pending.

No GUI, application replacement, polling service, model calls or credentials.
macOS uses kqueue; Windows uses process handles. Periodic discovery remains a
fallback. A fast relaunch is rechecked by the installer and safely deferred.
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
import select
import selectors
import subprocess
import sys
import time

import patch as p
from monitor import Scheduler, atomic_json, load_json, service_command, state_lock, timestamp
from official_client_host import windowless_python


def wait_any_exit(pids, timeout=30):
    pids=sorted(set(pid for pid in pids if type(pid) is int and pid>0))
    if not pids:
        return True
    if sys.platform=='win32':
        import ctypes
        from ctypes import wintypes
        api=ctypes.WinDLL('kernel32',use_last_error=True)
        api.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        api.OpenProcess.restype=wintypes.HANDLE
        api.CloseHandle.argtypes=[wintypes.HANDLE]
        api.WaitForMultipleObjects.argtypes=[wintypes.DWORD,ctypes.POINTER(wintypes.HANDLE),wintypes.BOOL,wintypes.DWORD]
        api.WaitForMultipleObjects.restype=wintypes.DWORD
        handles=[]
        try:
            for pid in pids[:64]:
                handle=api.OpenProcess(0x00100000,False,pid)
                if not handle:
                    if ctypes.get_last_error()==87:return True
                    raise ctypes.WinError(ctypes.get_last_error())
                handles.append(handle)
            array=(wintypes.HANDLE*len(handles))(*handles)
            result=api.WaitForMultipleObjects(len(handles),array,False,max(0,int(timeout*1000)))
            if result==0xffffffff:raise ctypes.WinError(ctypes.get_last_error())
            return result!=258
        finally:
            for handle in handles:api.CloseHandle(handle)
    if sys.platform=='darwin':
        queue=select.kqueue()
        try:
            events=[select.kevent(pid,filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD|select.KQ_EV_ONESHOT,fflags=select.KQ_NOTE_EXIT) for pid in pids]
            return bool(queue.control(events,1,timeout))
        except OSError as error:
            if error.errno==errno.ESRCH:return True
            raise
        finally:queue.close()
    # Linux CI exercises the real event-wait contract without a desktop app.
    descriptors=[]
    try:
        with selectors.DefaultSelector() as selector:
            for pid in pids:
                try:descriptor=os.pidfd_open(pid)
                except ProcessLookupError:return True
                descriptors.append(descriptor);selector.register(descriptor,selectors.EVENT_READ)
            return bool(selector.select(timeout))
    finally:
        for descriptor in descriptors:os.close(descriptor)


def pending(directory):
    state=load_json(Path(directory)/'state.json',{})
    return state.get('pending') if state.get('phase')=='waiting_for_exit' else None


def watched_pids(app):
    rows=p.active_app_processes(app,read_only_source=True)
    executable=str(p.app_layout(app)['exe'])
    equal=lambda value:os.path.normcase(value)==os.path.normcase(executable)
    main=[row for row in rows if equal(row['exe'])]
    ids={row['pid'] for row in main}
    roots=[row['pid'] for row in main if row['ppid'] not in ids]
    return roots or [row['pid'] for row in rows if row['pid']>0]


def install_ready(directory):
    settings=load_json(Path(directory)/'settings.json',{})
    command=service_command(directory,settings.get('python_path'))+['--install-ready']
    with (Path(directory)/'exit-watch/worker.log').open('ab') as log:
        subprocess.run(command,cwd=directory,stdin=subprocess.DEVNULL,stdout=log,
                       stderr=log,timeout=1800,**p.background_options())


def watch(directory, *, process_ids=None, enabled=None, invoke=None, wait=None):
    directory=Path(directory).resolve()
    process_ids=process_ids or watched_pids
    enabled=enabled or (lambda:Scheduler(directory).status()['enabled'])
    invoke=invoke or (lambda:install_ready(directory))
    wait=wait or wait_any_exit
    with state_lock(directory/'exit-watch') as locked:
        if not locked:return
        while pending(directory) and enabled():
            settings=load_json(directory/'settings.json')
            ids=process_ids(Path(settings['app']))
            atomic_json(directory/'exit-watch/state.json',{'format':1,'pid':os.getpid(),
                'phase':'waiting' if ids else 'checking_exit','observed_pids':ids,'at':timestamp()})
            from update_progress import Progress,pending_activity,wait_expired
            progress=Progress(directory)
            if progress.paused() or wait_expired(progress):break
            if ids and progress.requested() and progress.read().get('phase')!='close_failed':
                activity=pending_activity(progress)
                if activity.get('idle'):
                    # The installer rechecks and atomically acquires a drain lease.
                    invoke()
                    if pending(directory):time.sleep(1)
                    continue
                if not wait(ids,2 if activity.get('known') else 30):continue
            elif ids and not wait(ids,30):continue
            if not pending(directory) or not enabled():break
            # The main process can disappear before its CLI finishes. Watch
            # remaining children, or the new main process after a fast reopen.
            if process_ids(Path(settings['app'])):continue
            atomic_json(directory/'exit-watch/state.json',{'format':1,'pid':os.getpid(),
                'phase':'exit_detected','at':timestamp()})
            invoke()
            if pending(directory):time.sleep(.25)
        atomic_json(directory/'exit-watch/state.json',{'format':1,'pid':os.getpid(),
            'phase':'finished','at':timestamp()})


def arm(directory):
    directory=Path(directory).resolve()
    if not pending(directory):return False
    with state_lock(directory/'exit-watch') as locked:
        if not locked:return False
        settings=load_json(directory/'settings.json')
        executable=windowless_python(settings.get('python_path') or sys.executable)
        options=p.background_options() if os.name=='nt' else {'start_new_session':True}
        with (directory/'exit-watch/observer.log').open('ab') as log:
            subprocess.Popen([str(executable),str(directory/'runtime/exit_watch.py'),str(directory)],
                cwd=directory,stdin=subprocess.DEVNULL,stdout=log,stderr=log,**options)
    return True


if __name__=='__main__':
    try:watch(Path(sys.argv[1]))
    except Exception as error:
        # Keep periodic checks available and retain a credential-free failure.
        atomic_json(Path(sys.argv[1])/'exit-watch/state.json',{
            'format':1,'phase':'failed','error_type':type(error).__name__,'at':timestamp()})
        raise SystemExit(1)
