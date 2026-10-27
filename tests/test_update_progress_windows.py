"""Static safety/protocol contract and optional headless PowerShell parsing.

Native self-tests build real controls without showing them. Clicks invoke only
a temporary recorder controller, never an update or application launch.
"""
import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'update_progress.ps1'


class WindowsProgressContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SCRIPT.read_text(encoding='utf-8-sig')

    def test_windows_powershell_can_read_chinese_and_native_dependencies_only(self):
        self.assertTrue(SCRIPT.read_bytes().startswith(b'\xef\xbb\xbf'))
        self.assertIn("$form.Text = 'Codex 模型补丁更新'",self.source)
        self.assertEqual(re.findall(r'Add-Type -AssemblyName ([\w.]+)',self.source),
                         ['System.Windows.Forms','System.Drawing'])
        self.assertIn("ApartmentState -ne 'STA'",self.source)

    def test_controller_protocol_reads_only_known_state_file_and_fields(self):
        for parameter in ('StateDirectory','PythonExecutable','ControllerPath'):
            self.assertIn('[string]$' + parameter,self.source)
        self.assertIn("[IO.Path]::Combine($StateDirectory, 'progress.json')",self.source)
        for field in ('format','id','phase','title','detail','completed','total','can_start','can_launch'):
            self.assertIn('$next.' + field,self.source)
        self.assertIn('$info.Length -gt 65536',self.source)
        self.assertIn('if ($script:runId -and $next.id -ne $script:runId) { $form.Close(); return }',self.source)
        self.assertIn('$next.phase -notin $phases',self.source)
        self.assertIn('Short-Text $next.detail 500',self.source)
        self.assertNotIn('ActionFile',self.source)

    def test_actions_are_fixed_controller_argv_hidden_and_asynchronous(self):
        action = self.source.split('function Send-Action',1)[1].split('function Short-Text',1)[0]
        self.assertIn("$Action -notin @('start','launch','dismiss','cancel')",action)
        self.assertIn("@($ControllerPath, '--state-dir', $StateDirectory, '--action', $Action, '--id', $script:runId)",action)
        self.assertIn("$arguments += @('--phase', [string]$script:state.phase)",action)
        self.assertIn('Quote-Argument $_',action)
        self.assertIn('$info.FileName = $PythonExecutable',action)
        self.assertIn('$info.UseShellExecute = $false',action)
        self.assertIn('$info.CreateNoWindow = $true',action)
        self.assertIn("$info.WindowStyle = 'Hidden'",action)
        self.assertEqual(action.count('[Diagnostics.Process]::Start'),1)
        for forbidden in ('Start-Process','Invoke-Expression','Invoke-WebRequest','DownloadFile',
                          'Stop-Process','taskkill','cmd.exe','WaitForExit','ShowDialog','Start-Sleep'):
            self.assertNotIn(forbidden,self.source)
        self.assertNotRegex(self.source,r'\$next\.(?:command|executable|arguments|url|script)\b')

    def test_close_is_dismiss_only_and_launch_requires_explicit_button(self):
        close = next(line for line in self.source.splitlines() if '$form.Add_FormClosing(' in line)
        self.assertIn("Send-Action 'dismiss'",close)
        self.assertNotIn('start',close)
        self.assertNotIn('launch',close)
        self.assertIn("$launch.Add_Click({ Send-Action 'launch'",self.source)
        self.assertIn("$start.Add_Click({ Send-Action $(if($script:state.can_cancel){'cancel'}else{'start'})",self.source)
        self.assertIn("$Action -eq 'launch' -and $script:state.can_launch -ne $true",self.source)
        self.assertIn('$form.TopMost = $false',self.source)
        self.assertIn('[Windows.Forms.Application]::Run($form)',self.source)

    def test_determinate_progress_comes_from_real_work_counts(self):
        self.assertIn('100 * $completed / $total',self.source)
        self.assertIn("$percent.Text = [string][Math]::Floor(100*$completed/$total) + '% · ' + [string]$next.completed + ' / ' + [string]$next.total",self.source)
        self.assertIn("$progress.Style = 'Marquee'",self.source)
        self.assertIn('$null -ne $next.completed',self.source)
        self.assertNotRegex(self.source,r'\$(?:completed|total|progress\.Value)\s*\+\+')

    @unittest.skipUnless(shutil.which('powershell') or shutil.which('pwsh'), 'PowerShell parser unavailable')
    def test_powershell_parser_accepts_file_without_running_ui(self):
        executable = shutil.which('powershell') or shutil.which('pwsh')
        script_path = str(SCRIPT).replace("'","''")
        code = ("$tokens=$null;$errors=$null;"
                "[System.Management.Automation.Language.Parser]::ParseFile('" + script_path +
                "',[ref]$tokens,[ref]$errors)|Out-Null;"
                "if($errors.Count){$errors|ForEach-Object{$_.Message};exit 1}")
        encoded = base64.b64encode(code.encode('utf-16-le')).decode('ascii')
        result = subprocess.run([executable,'-NoLogo','-NoProfile','-NonInteractive','-EncodedCommand',encoded],
            capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=15,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(result.returncode,0,result.stdout + result.stderr)


@unittest.skipUnless(sys.platform == 'win32', 'Real WinForms controls require Windows')
class WindowsProgressNativeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='codex-progress-native-')
        self.addCleanup(self.temporary.cleanup)
        # PowerShell resolves Windows 8.3 aliases before invoking the controller.
        self.directory = Path(self.temporary.name).resolve() / '状态 有空格'
        self.directory.mkdir()
        self.record = self.directory / 'record.json'
        self.controller = self.directory / 'fake controller.py'
        self.controller.write_text(
            "import ctypes,json,os,sys\n"
            "from pathlib import Path\n"
            "value={'argv':sys.argv[1:],'console_window':bool(ctypes.windll.kernel32.GetConsoleWindow())}\n"
            "target=Path(os.environ['CODEX_PROGRESS_TEST_RECORD'])\n"
            "temporary=target.with_suffix('.tmp')\n"
            "temporary.write_text(json.dumps(value),encoding='utf-8')\n"
            "temporary.replace(target)\n",encoding='utf-8')
        executable = Path(sys.executable)
        self.python = executable.with_name('pythonw.exe')
        self.assertTrue(self.python.is_file(), 'Headless click tests require sibling pythonw.exe')
        self.powershell = shutil.which('powershell')
        self.assertIsNotNone(self.powershell, 'Windows PowerShell is required for native progress UI')
        self.run_id = 'fixture "quoted" & 中文'

    def run_view(self, phase, flag='-SelfTest', action=None, **overrides):
        state = {'format':1,'id':self.run_id,'edition':'ds','version':'synthetic',
            'phase':phase,'title':'原生更新界面','detail':'后台继续更新，窗口保持隐藏。',
            'completed':None,'total':None,'can_start':phase == 'available',
            'can_launch':phase == 'completed', **overrides}
        self.state_path = self.directory / 'progress.json'
        self.state_path.write_text(json.dumps(state,ensure_ascii=False),encoding='utf-8')
        self.before = self.state_path.read_bytes()
        from windows_support import native_ui_command
        command=native_ui_command(SCRIPT,{'StateDirectory':str(self.directory),
            'PythonExecutable':str(self.python),'ControllerPath':str(self.controller),
            flag.removeprefix('-'):action if action is not None else True})
        result = subprocess.run(command,capture_output=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW,
            env={**os.environ,'CODEX_PROGRESS_TEST_RECORD':str(self.record)})
        def diagnostic(raw):
            try:
                return raw.decode('utf-8')
            except UnicodeDecodeError:
                return raw.decode('gbk',errors='replace')
        self.assertEqual(result.returncode,0,diagnostic(result.stdout) + diagnostic(result.stderr))
        snapshot = json.loads(result.stdout.decode('utf-8').strip())
        self.assertFalse(snapshot['form_visible'])
        self.assertEqual(snapshot['form_title'],'Codex 模型补丁更新')
        self.assertEqual(snapshot['title'],'原生更新界面')
        self.assertEqual(self.state_path.read_bytes(),self.before)
        return snapshot

    def action_record(self):
        deadline = time.monotonic() + 5
        while not self.record.is_file() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(self.record.is_file(), 'The hidden fake controller did not record the click')
        result = json.loads(self.record.read_text(encoding='utf-8'))
        self.assertFalse(result['console_window'])
        return result['argv']

    def test_three_phases_render_real_hidden_controls(self):
        for phase, primary, enabled, style, value in (
            ('available','开始更新',True,'Marquee',0),
            ('installing','开始更新',False,'Continuous',50),
            ('completed','启动 Codex',True,'Continuous',100)):
            with self.subTest(phase=phase):
                counts = {} if phase == 'available' else {'completed':2 if phase == 'installing' else 4,'total':4}
                snapshot = self.run_view(phase,**counts)
                self.assertEqual(snapshot['primary_text'],primary)
                self.assertEqual(snapshot['primary_enabled'],enabled)
                self.assertEqual(snapshot['progress_style'],style)
                self.assertEqual(snapshot['progress_value'],value)
                self.assertEqual(snapshot['start_enabled'],phase == 'available')
                self.assertEqual(snapshot['launch_enabled'],phase == 'completed')
                self.assertEqual(snapshot['detail'],'后台继续更新，窗口保持隐藏。')

    def test_terminal_phases_stop_progress_and_keep_close_available(self):
        for phase, value in (('failed',0),('completed',100)):
            with self.subTest(phase=phase):
                snapshot=self.run_view(phase)
                self.assertEqual(snapshot['progress_style'],'Continuous')
                self.assertEqual(snapshot['progress_value'],value)
                self.assertFalse(snapshot['start_enabled'])
                self.assertFalse(self.record.exists())

    def test_real_button_events_send_only_fixed_quoted_controller_argv(self):
        for phase, action in (('available','start'),('completed','launch')):
            with self.subTest(action=action):
                self.record.unlink(missing_ok=True)
                snapshot = self.run_view(phase,'-SelfTestAction',action)
                self.assertEqual(self.action_record(),['--state-dir',str(self.directory),
                    '--action',action,'--id',self.run_id])
                self.assertFalse(snapshot[action + '_enabled'])

    def test_disabled_buttons_do_not_start_any_controller(self):
        for action in ('start','launch'):
            with self.subTest(action=action):
                self.run_view('installing','-SelfTestAction',action)
                self.assertFalse(self.record.exists())

    def test_replaced_run_closes_old_hidden_form_instead_of_leaving_stale_view(self):
        snapshot = self.run_view('available','-SelfTestStaleId','superseded-run')
        self.assertTrue(snapshot['form_disposed'])
        self.assertFalse(snapshot['form_visible'])
        self.assertFalse(self.record.exists())

    def test_closing_hidden_form_emits_only_phase_bound_dismiss(self):
        snapshot = self.run_view('installing','-SelfTestDismiss')
        self.assertEqual(self.action_record(),['--state-dir',str(self.directory),
            '--action','dismiss','--id',self.run_id,'--phase','installing'])
        self.assertEqual(snapshot['phase'],'installing')
        self.assertEqual(self.state_path.read_bytes(),self.before)


if __name__ == '__main__':
    unittest.main()
