import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

import official_client_host as host


class HooksTests(unittest.TestCase):
    def test_merge_and_undo_preserve_user_hooks(self):
        original = {"description": "personal", "hooks": {
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "my-hook"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "finish"}]}]}}
        baseline = copy.deepcopy(original)
        entries = host.managed_hook_entries("/tmp/host settings.json", python=sys.executable)
        merged, ownership = host.merge_managed_hooks(original, entries)
        self.assertEqual(original, baseline)
        self.assertEqual(host.remove_managed_hooks(merged, ownership), original)
        repeated, repeated_ownership = host.merge_managed_hooks(merged, entries)
        self.assertEqual(repeated, merged)
        self.assertEqual(repeated_ownership["added"], {})

    def test_undo_refuses_user_modified_managed_entry(self):
        entries = host.managed_hook_entries("/tmp/host.json", python=sys.executable)
        merged, ownership = host.merge_managed_hooks({}, entries)
        merged["hooks"]["SessionStart"][0]["matcher"] = "resume"
        with self.assertRaises(host.HostError):
            host.remove_managed_hooks(merged, ownership)

    def test_windows_requires_pythonw_without_powershell(self):
        with tempfile.TemporaryDirectory() as temp:
            python = Path(temp) / "python.exe"
            python.write_bytes(b"fixture")
            with self.assertRaises(host.HostError):
                host.managed_hook_entries("/tmp/settings.json", python=python, platform="win32")
            python.with_name("pythonw.exe").write_bytes(b"fixture")
            entries = host.managed_hook_entries("/tmp/settings.json", python=python, platform="win32")
            command = entries["SessionStart"]["hooks"][0]["command"]
            self.assertIn("pythonw.exe", command)
            self.assertNotIn("powershell", command)
            self.assertEqual(entries["SessionStart"]["hooks"][0]["timeout"], 20)


class EnsureTests(unittest.TestCase):
    def settings(self):
        return {"adapter_command": [str(Path(sys.executable).resolve()), "/tmp/adapter.py", "serve", "/tmp/profile.json"],
                "health_url": "http://127.0.0.1:9876/private-token/health",
                "health_identity": "test-installation", "edition": "ds"}

    def test_existing_identity_does_not_spawn(self):
        with patch.object(host, "healthy", return_value=True), patch.object(host.subprocess, "Popen") as spawn:
            self.assertEqual(host.ensure_adapter(self.settings()), {"status": "ready", "started": False})
            spawn.assert_not_called()

    def test_concurrent_winner_accepted_after_losing_child_exit(self):
        with patch.object(host, "healthy", side_effect=[False, False, True]), \
                patch.object(host.subprocess, "Popen") as spawn:
            spawn.return_value.poll.return_value = 1
            value = host.ensure_adapter(self.settings(), poll_interval=0)
            self.assertEqual(value["status"], "ready")
            kwargs = spawn.call_args.kwargs
            self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
            self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
            self.assertNotIn("shell", kwargs)

    def test_startup_timeout_redacts_private_url(self):
        with patch.object(host, "healthy", return_value=False), patch.object(host.subprocess, "Popen"):
            with self.assertRaises(host.HostError) as captured:
                host.ensure_adapter(self.settings(), timeout=0)
        self.assertNotIn("private-token", str(captured.exception))

    def test_non_loopback_or_credentials_rejected_before_spawn(self):
        for url in ("https://127.0.0.1:1/health", "http://localhost:1/health", "http://127.0.0.1:1/health?q=x",
                    "http://token@127.0.0.1:1/health", "http://example.com/health"):
            settings = self.settings()
            settings["health_url"] = url
            with patch.object(host.subprocess, "Popen") as spawn, self.assertRaises(host.HostError):
                host.ensure_adapter(settings)
            spawn.assert_not_called()

    def test_health_checks_identity_and_edition(self):
        class Response(io.BytesIO):
            status = 200
        settings = self.settings()
        for identity, edition, expected in (("test-installation", "ds", True), ("other", "ds", False),
                                             ("test-installation", "proxy", False)):
            response = Response(json.dumps({"ok": True, "identity": identity, "edition": edition}).encode())
            with patch.object(host.urllib.request, "build_opener") as opener:
                opener.return_value.open.return_value = response
                self.assertEqual(host.healthy(settings), expected)

    def test_mcp_waits_for_health_and_exposes_no_tools(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        output = io.StringIO()
        def ensure(settings):
            self.assertEqual(output.getvalue(), "")
        with patch.object(host, "ensure_adapter", side_effect=ensure) as called:
            host.mcp_lifecycle(self.settings(), io.StringIO("\n".join(map(json.dumps, messages))), output)
        called.assert_called_once()
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(replies[1]["result"], {"tools": []})

    def test_mcp_failure_does_not_expose_private_endpoint(self):
        output = io.StringIO()
        with patch.object(host, "ensure_adapter", side_effect=ValueError("private-token")):
            host.mcp_lifecycle(self.settings(), io.StringIO('{"id":1,"method":"initialize"}\n'), output)
        self.assertNotIn("private-token", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], -32603)

    def test_service_plans_keep_original_app_out_of_startup_command(self):
        settings = self.settings()
        mac = host.adapter_launchd_plist(settings)
        self.assertEqual(mac["ProgramArguments"], settings["adapter_command"])
        self.assertTrue(mac["KeepAlive"])
        self.assertEqual(mac["ProcessType"], "Standard")
        settings["adapter_command"][0] = str(Path(sys.executable).resolve().with_name("pythonw.exe"))
        xml = host.adapter_windows_task_xml(settings, "S-1-5-21-123-456-789-1001")
        # Parse the actual bytes written by the Windows installer. A Unicode
        # string parse alone misses a UTF-8 declaration inside UTF-16 bytes.
        root = ET.fromstring(xml.encode("utf-16"))
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        self.assertEqual(root.findtext("t:Actions/t:Exec/t:Command", namespaces=ns), settings["adapter_command"][0])
        self.assertEqual(root.findtext("t:Principals/t:Principal/t:RunLevel", namespaces=ns), "LeastPrivilege")
        self.assertEqual(root.findtext("t:Settings/t:ExecutionTimeLimit", namespaces=ns), "PT0S")
        self.assertNotIn("ChatGPT.exe", xml)
        self.assertNotIn("private-token", xml)


class OfficialHostPlanTests(unittest.TestCase):
    def test_mac_plan_isolates_state_and_keeps_official_executable(self):
        with patch.dict(host.os.environ, {"CODEX_CLI_PATH": "/untrusted/wrapper", "OPENAI_API_KEY": "secret"}):
            plan = host.isolated_mac_plan("/Official.app", "/private/probe", verify=False)
        self.assertEqual(plan["argv"][0], str(Path("/Official.app") / "Contents/MacOS/ChatGPT"))
        self.assertIn("--use-mock-keychain", plan["argv"])
        self.assertEqual(plan["environment"]["CODEX_HOME"], str(Path("/private/probe").resolve() / "home"))
        self.assertEqual(plan["environment"]["CODEX_ELECTRON_USER_DATA_PATH"], str(Path("/private/probe").resolve() / "browser"))
        self.assertNotIn("CODEX_CLI_PATH", plan["environment"])
        self.assertNotIn("OPENAI_API_KEY", plan["environment"])

    def test_mac_plan_rejects_socket_paths_that_cannot_bind(self):
        with self.assertRaises(host.HostError):
            host.isolated_mac_plan("/Official.app", "/tmp/" + "x" * 110, verify=False)


class ComposerQuotaTests(unittest.TestCase):
    # Small synthetic components with the same verified native data bindings.
    ADAPTER = ('function render(K,U,Tn,E,other){let reserve=K.settings.model===`gpt-reserve`;'
               'let Ur=E(U===`local`&&Tn);return other||Ur}')
    BANNER = 'function banner(i,v,h,r,U_){let re=i===`local`&&v||U_(h,r);return re}'

    def test_composer_pipe_protocol_error_is_not_browser_exit(self):
        import queue
        pipe=host.Pipe.__new__(host.Pipe);pipe.reader=123;pipe.messages=queue.Queue()
        with patch.object(host.os,'read',side_effect=[b'bad-json\0',b'{"id":1}\0',b'']):
            pipe._read()
        self.assertEqual(pipe.messages.get(),{'method':'__composer_pipe_error'})
        self.assertEqual(pipe.messages.get(),{'id':1})
        self.assertIsNone(pipe.messages.get())
        with patch.object(host.os,'read',side_effect=OSError('fixture')):pipe._read()
        self.assertEqual(pipe.messages.get(),{'method':'__composer_pipe_error'})
        self.assertTrue(pipe.messages.empty())

    def javascript(self, source):
        import shutil
        from windows_support import background_options
        node = shutil.which('node')
        if not node:
            self.skipTest('Node is required for renderer expression execution')
        return json.loads(subprocess.check_output([node, '-e', source], text=True, **background_options()))

    def test_composer_quota_model_switch_keeps_gpt_and_other_submit_guards(self):
        from official_client_host import transform
        for edition, models in [('ds', ['deepseek-flash']),
                                ('proxy', ['deepseek-flash', 'grok-4.7', 'local-model'])]:
            source = transform(self.ADAPTER, 'adapter', models)
            result = self.javascript(source + ';let out=[];for(const window of [300,10080])'
                'for(const chat of ["new","existing"])for(const model of '
                + json.dumps(['gpt-fixture', *models, 'unknown', 'gpt-fixture']) + '){'
                'let calls=0;const hook=x=>(calls++,Boolean(x));'
                'let args=[{settings:{model}},"local",true,hook];'
                'out.push({window,chat,model,blocked:render(...args,false),'
                'otherGuard:render(...args,true),calls});}console.log(JSON.stringify(out))')
            for row in result:
                with self.subTest(edition=edition, **row):
                    self.assertEqual(row['blocked'], row['model'] not in models)
                    self.assertTrue(row['otherGuard'])
                    self.assertEqual(row['calls'], 2)

    def test_composer_quota_does_not_rewrite_account_state_or_remote_model_limits(self):
        from official_client_host import transform
        source = transform(self.BANNER, 'banner', ['deepseek-flash'])
        result = self.javascript(source + ';const status=Object.freeze({allowed:false});'
            'console.log(JSON.stringify(['
            'banner("local",true,status,"deepseek-flash",()=>true),'
            'banner("local",true,status,"gpt-fixture",()=>true),'
            'banner("remote",false,status,"deepseek-flash",()=>true),status]))')
        self.assertEqual(result, [False, True, True, {'allowed': False}])

    def test_composer_quota_rejects_unknown_or_ambiguous_native_layouts(self):
        from official_client_host import transform, CompatibilityError
        for source in (self.ADAPTER.replace('===`local`', '===`changed`'),
                       self.ADAPTER + self.ADAPTER,
                       self.ADAPTER.replace('settings.model===`gpt-reserve`', 'unrelated')):
            with self.assertRaises(CompatibilityError):
                transform(source, 'adapter', ['deepseek-flash'])
        with self.assertRaises(CompatibilityError):
            transform(transform(self.ADAPTER, 'adapter', ['ds']), 'adapter', ['ds'])

    def test_composer_quota_escapes_route_ids_and_preserves_other_bytes(self):
        from official_client_host import transform, MARKER
        model = 'vendor/quote"\\\n</script>\u2028'
        out = transform(self.ADAPTER, 'adapter', [model])
        self.assertEqual(self.javascript(out+';console.log(JSON.stringify(render('
            + json.dumps({'settings': {'model': model}})+',"local",true,x=>x,false)))'), False)
        self.assertEqual(out.replace(MARKER, '').split('let Ur=', 1)[0],
                         self.ADAPTER.split('let Ur=', 1)[0])

    def test_composer_pipe_intercepts_only_prepared_assets_and_resumes_on_failure(self):
        from official_client_host import Controller, CompatibilityError
        class Pipe:
            def __init__(self): self.calls = []
            def send(self, *args, **kwargs):
                self.calls.append((args, kwargs)); return len(self.calls)
        pipe = Pipe(); url = 'app://-/assets/composer-fixture.js'
        controller = Controller(pipe, {url: b'prepared'})
        controller.handle({'method': 'Target.attachedToTarget', 'params': {'sessionId': 's'}})
        controller.handle({'id': len(pipe.calls), 'result': {}})
        self.assertEqual(pipe.calls[-1][0][0], 'Runtime.runIfWaitingForDebugger')

        controller.handle({'method':'Page.frameNavigated','sessionId':'s',
                           'params':{'frame':{'id':'main','url':'app://-/index.html'}}})

        for request in ({'url': url, 'method': 'POST'}, {'url': url+'?other', 'method': 'GET'},
                        {'url': 'https://example.invalid/assets/composer-fixture.js', 'method': 'GET'}):
            controller.handle({'method': 'Fetch.requestPaused', 'sessionId': 's',
                               'params': {'requestId': 'r', 'frameId':'main', 'request': request}})
            self.assertEqual(pipe.calls[-1][0][0], 'Fetch.continueRequest')
        controller.handle({'method': 'Fetch.requestPaused', 'sessionId': 's',
            'params': {'requestId': 'r', 'frameId':'main','request': {'url': url, 'method': 'GET'}}})
        self.assertEqual(pipe.calls[-1][0][0], 'Fetch.fulfillRequest')
        self.assertFalse(controller.applied)
        controller.handle({'id': len(pipe.calls), 'result': {}})
        self.assertEqual(controller.applied, {url})
        controller.handle({'method':'Page.frameNavigated','sessionId':'s',
                           'params':{'frame':{'id':'main','url':'https://example.invalid'}}})
        controller.handle({'method':'Fetch.requestPaused','sessionId':'s',
            'params':{'requestId':'foreign','frameId':'main','request':{'url':url,'method':'GET'}}})
        self.assertEqual(pipe.calls[-1][0][0],'Fetch.continueRequest')
        controller.handle({'method': 'Target.attachedToTarget', 'params': {'sessionId': 's2'}})
        with self.assertRaises(CompatibilityError):
            controller.handle({'id': len(pipe.calls), 'error': {'message': 'unsupported'}})
        self.assertEqual(pipe.calls[-1][0][0], 'Runtime.runIfWaitingForDebugger')

    def test_composer_interception_failure_keeps_pipe_until_native_exit(self):
        import contextlib
        import plistlib
        from unittest.mock import MagicMock
        import patch as p
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);app=directory/'official';(app/'Contents').mkdir(parents=True)
            (app/'Contents/Info.plist').write_bytes(plistlib.dumps({'LSEnvironment':{}}))
            profile={'identity':'fixture','routes':{'ds':{}}}
            p.save_json(directory/'adapter-profile.json',profile)
            p.save_json(directory/'host-settings.json',{})
            p.save_json(directory/'manifest.json',{'status':'installed','target':str(app),'codex_home':str(directory/'home'),
                'profile_sha256':p.file_sha(directory/'adapter-profile.json'),
                'runtime_sha256':{'official_client_host.py':p.file_sha(host.__file__)},
                'composer_compat':{'models':['ds'],'source_asar':'fixture'}})
            pipe=MagicMock();pipe.process.pid=123;pipe.process.poll.side_effect=[None,None,0]
            pipe.messages.get.return_value={'method':'fixture'}
            controller=MagicMock();controller.handle.side_effect=host.CompatibilityError('fixture')
            with patch('official_client_update.verify_official'),patch('monitor.state_lock',return_value=contextlib.nullcontext(True)), \
                 patch.object(p,'active_app_processes',return_value=[]),patch.object(p,'app_layout',return_value={'exe':app/'app'}), \
                 patch.object(host,'assets',return_value={}),patch.object(host,'ensure_adapter'), \
                 patch.object(host,'Pipe',return_value=pipe),patch.object(host,'Controller',return_value=controller):
                host.run_composer(directory,'fixture-launch')
            controller.detach.assert_called_once()
            controller.handle.assert_called_once()
            pipe.process.terminate.assert_not_called()
            pipe.close.assert_called_once()
            status=json.loads((directory/'composer-session.json').read_text())
            self.assertEqual(status['status'],'stopped')
            self.assertEqual(status['error'],'composer_interception_failed')

@unittest.skipUnless(os.environ.get('CODEX_COMPOSER_APP'), 'Explicit isolated native renderer check')
class NativeComposerQuotaTests(unittest.TestCase):
    def test_native_composer_quota_button_before_inference(self):
        # Shipped quota/submit expressions on real DOM buttons, with synthetic
        # state. Keep this scope separate from a signed-in user's complete UI.
        import queue
        import re
        import time
        import patch as p
        app=Path(os.environ['CODEX_COMPOSER_APP'])
        from official_client_update import verify_official
        verify_official(app)
        root=Path(os.environ['CODEX_COMPOSER_FIXTURE']).resolve()
        edition=os.environ.get('CODEX_COMPOSER_EDITION','ds')
        root.mkdir(parents=True,exist_ok=True)
        models=['deepseek-flash'] + (['grok-4.7','gemini-3.8-flash','arbitrary-local'] if edition!='ds' else [])
        plan=host.prepare(app,models)
        replacements=host.assets(app,plan)
        archive=p.app_layout(app)['asar'];header,base,_=p.archive_header(archive)
        inventory=dict(p.entries(header))
        raw=p.read_member(archive,inventory[plan['assets']['adapter']['member']],base).decode()
        gate=host._one(r'(?P<quota>'+host.IDENT+r')=(?P<hook>'+host.IDENT+r')\((?P<hostname>'+host.IDENT+r')===`local`&&(?P<atom>'+host.IDENT+r')\)',raw,'quota')
        selected=host._one(r'(?P<mode>'+host.IDENT+r')\.settings\.model===`gpt-reserve`',raw[max(0,gate.start()-1500):gate.start()],'model')
        prop=host._one(r'recoveryNotices:'+host.IDENT+r',submitDisabled:(?P<guard>'+host.IDENT+r'),',raw,'submit prop')
        aggregate=host._one(r'let '+prop['guard']+r'=(?P<expr>[^,;]+),',raw,'submit guard')['expr']
        status_source=None
        for name,entry in inventory.items():
            if re.fullmatch(r'webview/assets/app-primary-[\w-]+\.js',name):
                data=p.read_member(archive,entry,base);p.check_member(data,entry)
                text=data.decode()
                anchor='authMethod!==`chatgpt`'
                for match in re.finditer(re.escape(anchor),text):
                    start=text.rfind('({get:',0,match.start());end=text.find('})})))()',match.end())
                    candidate=text[start:end+1]
                    if '.rate_limit?.allowed!==!1' in candidate and len(candidate)<2400:
                        if status_source is not None:raise AssertionError('Ambiguous native quota state')
                        status_source=candidate
        self.assertIsNotNone(status_source)
        binding=host._one(r'\(\{get:(?P<get>'+host.IDENT+r')\}\)=>\{let (?P<auth>'+host.IDENT+r')=(?P=get)\((?P<a>'+host.IDENT+r')\),(?P<data>'+host.IDENT+r')=(?P=get)\((?P<b>'+host.IDENT+r')\)\.data;',status_source,'account binding')
        get=binding['get'];data=binding['data']
        credit=host._one(r'\|\|(?P<name>'+host.IDENT+r')\('+data+r'\)\|\|',status_source,'credits')['name']
        workspace=host._one(r'\|\|(?P<name>'+host.IDENT+r')\(\{rateLimitStatus:',status_source,'workspace')['name']
        windows=host._one(r'=(?P<name>'+host.IDENT+r')\('+data+r'\)\.filter',status_source,'windows')['name']
        atoms=set(re.findall(r'\b'+get+r'\(('+host.IDENT+r')',status_source))
        names=set(re.findall(host.IDENT,aggregate)) - {gate['quota'],'isLoading'}
        declarations=';'.join('let '+name+'='+('{}' if name+'?.isLoading' in aggregate else 'false') for name in names)+';'
        declarations+=';'.join('const '+name+'='+json.dumps(name) for name in atoms)+';'
        declarations+='const '+credit+'=()=>false,'+workspace+'=()=>false,'+windows+'=s=>s.windows;'
        declarations+='const quota='+status_source+';'
        patched=host.transform(raw,'adapter',models)
        adapted=host._one(re.escape(gate[0])+r'&&![^;]+?\.includes\('+re.escape(selected['mode'])+r'\.settings\.model\)',patched,'adapted predicate')[0].split('=',1)[1]
        original=gate[0].split('=',1)[1]
        guards=''
        for term in aggregate.split('||'):
            if term==gate['quota']:continue
            term_names=set(re.findall(host.IDENT,term))-{'isLoading'}
            guards+=';'.join(name+'='+('{isLoading:true}' if name+'?.isLoading' in term else 'true') for name in term_names)+';'
            guards+='button.disabled='+aggregate+';if(!button.disabled)throw Error("Lost native submit guard");constGuard=calls;button.click();if(calls!==constGuard)throw Error("Native guard dispatched");'
            guards+=';'.join(name+'='+('{}' if name+'?.isLoading' in aggregate else 'false') for name in term_names)+';'
        # The exact native aggregate is reused, including every non-quota guard.
        js='''(async()=>{while(!document.body)await new Promise(r=>setTimeout(r,10));
        const root=document.createElement('div');root.hidden=true;document.body.append(root);
        let calls=0,constGuard=0;const button=document.createElement('button');root.append(button);
        button.onclick=()=>calls++;const out=[];try{'''+declarations+'''
        for(const windowMinutes of [300,10080])for(const chat of ['new','existing'])
        for(const model of '''+json.dumps(['gpt-fixture',*models,'unknown','gpt-fixture'])+'''){
          const status={account_id:'fixture-account',user_id:'fixture-user',plan_type:'plus',
            rate_limit:{allowed:false},windows:[{remainingPercent:0,windowMinutes,resetsAt:2100000000}]};
          const auth={authMethod:'chatgpt',authLoading:false,accountLoading:false,
            authenticatedAccountId:'fixture-account',accountId:'fixture-account',userId:'fixture-user',plan:'plus',accountStructure:'personal'};
          const limited=quota({get:key=>key==='''+json.dumps(binding['a'])+'''?auth:key==='''+json.dumps(binding['b'])+'''?{data:status}:false});
          const '''+gate['hook']+'''=x=>x,'''+gate['hostname']+'''='local','''+gate['atom']+'''=limited,
          '''+selected['mode']+'''={settings:{model}};
          let '''+gate['quota']+'='+original+''';button.disabled='''+aggregate+''';
          const before=calls;button.click();if(calls!==before)throw Error('Old native quota did not block the click');
          '''+gate['quota']+'='+adapted+''';button.disabled='''+aggregate+''';button.click();
          out.push({windowMinutes,chat,model,blocked:button.disabled,dispatched:calls-before});
          '''+guards+'''
        }return out;}finally{root.remove()}})()'''
        if sys.platform=='darwin':
            launch=host.isolated_mac_plan(app,root,verify=False)
            for path in launch['directories']:Path(path).mkdir(parents=True,exist_ok=True)
            env=launch['environment'];argv=launch['argv']
        else:
            for part in ('user','home','browser','temp'):(root/part).mkdir(exist_ok=True)
            env={k:v for k,v in os.environ.items() if k.upper() in
                 {'PATH','SYSTEMROOT','WINDIR','COMSPEC','APPDATA','LOCALAPPDATA','PROGRAMDATA',
                  'PROGRAMFILES','PROGRAMFILES(X86)','USERPROFILE','USERNAME','USERDOMAIN','TEMP','TMP'}}
            env.update(CODEX_HOME=str(root/'home'),CODEX_ELECTRON_USER_DATA_PATH=str(root/'browser'))
            argv=[str(p.app_layout(app)['exe']),'--user-data-dir='+str(root/'browser')]
        (root/'home/config.toml').write_text('cli_auth_credentials_store="file"\nweb_search="disabled"\n[features]\napps=false\nmemories=false\n')
        env['CODEX_ELECTRON_START_IN_BACKGROUND']='1'
        pipe=host.Pipe(argv,env=env,cwd=root,package=sys.platform=='win32')
        package_identity=None
        controller=host.Controller(pipe,replacements);controller.start()
        pending={};evaluated=set();result=None;deadline=time.monotonic()+25
        try:
            package_identity=None
            if sys.platform=='darwin':
                import ctypes
                responsible=ctypes.CDLL(None).responsibility_get_pid_responsible_for_pid
                responsible.argtypes=[ctypes.c_int];responsible.restype=ctypes.c_int
                self.assertEqual(responsible(pipe.process.pid),pipe.process.pid)
            if sys.platform=='win32':
                import ctypes
                from official_client_shortcut import official_entry
                kernel=ctypes.WinDLL('kernel32',use_last_error=True)
                identity=kernel.GetApplicationUserModelId
                identity.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint32),ctypes.c_wchar_p]
                size=ctypes.c_uint32()
                self.assertEqual(identity(int(pipe.process._handle),ctypes.byref(size),None),122)
                value=ctypes.create_unicode_buffer(size.value)
                self.assertEqual(identity(int(pipe.process._handle),ctypes.byref(size),value),0)
                package_identity=value.value
                self.assertEqual(package_identity,official_entry(app)['app_user_model_id'])
            while time.monotonic()<deadline:
                try:message=pipe.messages.get(timeout=.5)
                except queue.Empty:continue
                if message is None:break
                controller.handle(message)
                if message.get('method')=='Target.attachedToTarget':
                    sid=message['params']['sessionId'];pipe.send('Runtime.enable',session=sid)
                if message.get('method')=='Runtime.executionContextCreated':
                    context=message['params']['context'];sid=message.get('sessionId')
                    if context.get('auxData',{}).get('isDefault') and sid not in evaluated:
                        evaluated.add(sid)
                        # Import the prepared assets in this original renderer too.
                        pipe.send('Runtime.evaluate',{'expression':'Promise.all('+json.dumps(list(replacements))+'.map(u=>import(u)))','awaitPromise':True},sid)
                        pending[pipe.send('Runtime.evaluate',{'expression':js,'awaitPromise':True,'returnByValue':True},sid)]=sid
                if message.get('id') in pending:
                    self.assertNotIn('exceptionDetails',message.get('result',{}),str(message)[:1600])
                    result=message.get('result',{}).get('result',{}).get('value')
                if result is not None and len(controller.applied)==2:break
            self.assertIsNotNone(result,'The isolated renderer did not complete its button contract')
            self.assertEqual(len(controller.applied),2,'Prepared composer assets were not both loaded')
            for row in result:
                self.assertEqual(row['blocked'],row['model'] not in models,row)
                self.assertEqual(row['dispatched'],int(row['model'] in models),row)
            report={'kind':'source-bound-native-renderer-button-contract','platform':sys.platform,'edition':edition,
                    'checks':len(result),'status':'passed','plan':plan,'application_identity':package_identity,
                    'mac_permission_owner_verified':sys.platform=='darwin',
                    'scope':'Native quota and submit expressions; synthetic account/model state and DOM controls. No signed-in full-composer claim.'}
            (root/'composer-result.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        finally:
            try:pipe.send('Browser.close')
            except OSError:pass
            pipe.process.wait(timeout=10);pipe.close()



if __name__ == "__main__":
    unittest.main()
