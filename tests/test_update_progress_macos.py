from windows_support import background_options
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform=='darwin','Native AppKit controls')
class NativePanelTests(unittest.TestCase):
    def render(self, state, flag=None):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);record=directory/'actions.json'
            (directory/'progress.json').write_text(json.dumps({'format':1,'id':'fixture-native-progress',
                'edition':'proxy','version':'fixture','title':'模型更新','detail':'关闭窗口继续后台更新',**state},ensure_ascii=False),encoding='utf-8')
            controller=directory/'controller.py'
            controller.write_text('import json,sys\nfrom pathlib import Path\nPath('+repr(str(record))+').write_text(json.dumps(sys.argv[1:]))\n',encoding='utf-8')
            command=['/usr/bin/osascript','-l','JavaScript',str(ROOT/'update_progress_macos.js'),
                     str(directory),sys.executable,str(controller),'--self-test']
            if flag:command.append(flag)
            result=subprocess.run(command,check=True,capture_output=True,text=True,timeout=20,**background_options())
            return json.loads(result.stdout) if result.stdout.strip() else {}, json.loads(record.read_text()) if record.exists() else None

    def test_start_button_uses_fixed_controller_arguments_without_showing_panel(self):
        result, action=self.render({'phase':'available','can_start':True,'completed':2,'total':5},'--self-test-action')
        self.assertFalse(result['visible']);self.assertTrue(result['nonactivating'])
        self.assertFalse(result['indeterminate']);self.assertEqual(result['action'],'start')
        self.assertEqual(action[2:],['--action','start','--id','fixture-native-progress','--phase','available'])

    def test_installing_has_indeterminate_progress_and_no_active_start(self):
        result, action=self.render({'phase':'installing','can_start':False,'can_launch':False},'--self-test-action')
        self.assertFalse(result['enabled']);self.assertTrue(result['indeterminate'])
        self.assertIsNone(action);self.assertFalse(result['visible'])

    def test_completion_only_launches_on_button_action(self):
        state={'phase':'completed','can_start':False,'can_launch':True,'completed':1,'total':1}
        result, action=self.render(state)
        self.assertEqual(result['primary'],'启动 Codex');self.assertIsNone(action)
        result, action=self.render(state,'--self-test-action')
        self.assertEqual(result['action'],'launch');self.assertEqual(action[3],'launch')

    def test_native_application_event_loop_dispatches_close_without_showing_panel(self):
        _, action=self.render({'phase':'failed','can_start':False},'--self-test-loop')
        self.assertIsNotNone(action)
        self.assertEqual(action[3],'dismiss')
        self.assertEqual(action[-1],'failed')

    def test_failed_and_completed_progress_stop_animating(self):
        for phase in ('failed', 'completed'):
            with self.subTest(phase=phase):
                result, action=self.render({'phase':phase,'can_start':False,'can_launch':phase=='completed'})
                self.assertFalse(result['indeterminate'])
                self.assertIsNone(action)

    def test_closing_window_sends_dismiss_not_cancel_or_launch(self):
        result, action=self.render({'phase':'verifying','can_start':False},'--self-test-dismiss')
        self.assertEqual(action[3],'dismiss');self.assertEqual(action[-1],'verifying')
        self.assertIsNone(result['action']);self.assertFalse(result['visible'])


if __name__=='__main__':unittest.main()
