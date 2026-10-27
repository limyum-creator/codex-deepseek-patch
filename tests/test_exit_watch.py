import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch,Mock

import exit_watch as subject
from monitor import atomic_json,state_lock


class ExitWatchTests(unittest.TestCase):
    def test_real_process_exit_wakes_without_periodic_poll_delay(self):
        child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(.3)'],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**subject.p.background_options())
        try:
            before=time.monotonic()
            self.assertTrue(subject.wait_any_exit([child.pid],5))
            self.assertLess(time.monotonic()-before,3)
        finally:child.wait(timeout=5)

    def fixture(self,root):
        atomic_json(root/'settings.json',{'app':'/official/app'})
        atomic_json(root/'state.json',{'phase':'waiting_for_exit','pending':{'fingerprint':'prepared'}})

    def test_observer_installs_once_after_exit_and_finishes(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.fixture(root);calls=[]
            def install():
                calls.append('installed');atomic_json(root/'state.json',{'phase':'installed'})
            wait=Mock(return_value=True)
            subject.watch(root,process_ids=Mock(side_effect=[[101],[]]),enabled=lambda:True,invoke=install,wait=wait)
            self.assertEqual(calls,['installed']);wait.assert_called_once_with([101],30)
            self.assertEqual(json.loads((root/'exit-watch/state.json').read_text())['phase'],'finished')

    def test_fast_reopen_waits_for_new_process_instead_of_installing(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.fixture(root);wait=Mock(return_value=True);install=Mock()
            def apply():
                install();atomic_json(root/'state.json',{'phase':'installed'})
            subject.watch(root,process_ids=Mock(side_effect=[[101],[202],[202],[]]),
                enabled=lambda:True,invoke=apply,wait=wait)
            self.assertEqual([c.args[0] for c in wait.call_args_list],[[101],[202]])
            install.assert_called_once()

    def test_disable_or_cleared_pending_never_launches_an_install(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.fixture(root);install=Mock();process=Mock()
            subject.watch(root,enabled=lambda:False,invoke=install,process_ids=process)
            install.assert_not_called();process.assert_not_called()

    def test_existing_observer_prevents_another_background_child(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.fixture(root)
            with state_lock(root/'exit-watch'),patch.object(subject.subprocess,'Popen') as spawn:
                self.assertFalse(subject.arm(root))
                spawn.assert_not_called()

    def test_main_process_is_preferred_over_renderer_churn(self):
        rows=[{'pid':11,'ppid':1,'exe':'/official/app'},
              {'pid':12,'ppid':11,'exe':'/official/app'},
              {'pid':13,'ppid':11,'exe':'/official/helper'}]
        with patch.object(subject.p,'active_app_processes',return_value=rows), \
             patch.object(subject.p,'app_layout',return_value={'exe':'/official/app'}):
            self.assertEqual(subject.watched_pids(Path('/official')),[11])

    def test_windows_shared_service_is_ignored_but_real_app_still_blocks(self):
        import windows_support
        root='C:\\Official'
        service={'ProcessId':10,'ParentProcessId':1,'ExecutablePath':root+'\\resources\\codex-windows-sandbox-service.exe'}
        main={'ProcessId':20,'ParentProcessId':1,'ExecutablePath':root+'\\Codex.exe'}
        with patch.object(subject.p.sys,'platform','win32'), \
             patch.object(windows_support,'_app_root',return_value=Path(root)), \
             patch.object(windows_support,'_powershell',return_value=json.dumps([service,main])):
            self.assertEqual([row['pid'] for row in subject.p.active_app_processes(Path(root),read_only_source=True)],[20])
            with self.assertRaises(subject.p.AppRunningError):subject.p.require_stopped(Path(root),read_only_source=True)
        with patch.object(subject.p.sys,'platform','win32'), \
             patch.object(windows_support,'_app_root',return_value=Path(root)), \
             patch.object(windows_support,'_powershell',return_value=json.dumps([service])):
            subject.p.require_stopped(Path(root),read_only_source=True)
