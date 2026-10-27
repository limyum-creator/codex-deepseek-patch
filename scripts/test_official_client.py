"""Unmodified native CLI + loopback adapter regression, with synthetic auth/tools.

No real credentials or external inference. The same run(app, reportdir, edition)
entrypoint is used on macOS and Windows; --mcp reuses the 160-tool fixture.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import copy
import http.server
import io
import json
import hashlib
from pathlib import Path
import subprocess
import shutil
import ssl
import sys
import tempfile
import threading
import time
import struct
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import inference_adapter
from official_client_config import adapt_catalog, prepare_config
from inference_transport import accept_websocket, zstd_body
from responses_compat import SEARCH_NAME, ToolAdapter
import patch
from scripts.test_context import ResponsesStub, Rpc
from scripts.test_offline import isolated_environment, launch_options, probe_command, stop_probe
from scripts.test_switching import finish
from scripts.test_tools import Stub as ToolStub, mcp_fixture

PROVIDER = 'openai'
GPT = 'gpt-6.1-sol'
DS = 'deepseek-flash'

# Valid, synthetic 8x8 PNG. No provider call or image generation charge.
IMAGE_FIXTURE = 'iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAYAAADED76LAAAAEklEQVR4nGOwKPj1Hx9mGBkKAGsyqEFP7gEOAAAAAElFTkSuQmCC'


class NativeServicesFixture(http.server.BaseHTTPRequestHandler):
    """Native tool contract fixture, independent of the adapter's allowlist."""
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def json_reply(self, value):
        raw = json.dumps(value).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_CONNECT(self):
        self.send_error(503, 'External network disabled')

    def do_GET(self):
        if self.path.endswith('/accounts/check'):
            self.json_reply({'accounts': [{'id': 'synthetic-account', 'plan_type': 'plus',
                'structure': 'personal', 'workspace_backend_origin': 'NO_CONSTRAINT',
                'account_routing_override': 'NO_CONSTRAINT'}],
                'default_account_id': 'synthetic-account', 'account_ordering': ['synthetic-account']})
        elif self.headers.get('Upgrade', '').lower() == 'websocket':
            if getattr(self.server,'large_history',False):
                self.send_error(426,'Synthetic HTTP-only upstream')
                return
            ws = accept_websocket(self, max_message=None)
            self.server.compressed = bool(ws.deflate)
            try:
                while (message := ws.recv()) is not None:
                    self.respond(json.loads(message[1]), ws)
            except OSError:
                pass
            finally:
                ws.close()
        else:
            self.send_error(404)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers['Content-Length']))
        encoding = self.headers.get('Content-Encoding')
        if encoding == 'zstd':
            raw = zstd_body(raw, node=self.server.compression_node)
        body = json.loads(raw)
        if getattr(self.server,'large_history',False):
            images=[p['image_url'] for i in body.get('input',[]) for p in i.get('content',[])
                    if isinstance(p,dict) and p.get('type')=='input_image']
            self.server.large_requests.append({'encoding':encoding,'bytes':len(raw),
                'images':[hashlib.sha256(v.encode()).hexdigest() for v in images],
                'auth':self.headers.get('Authorization')=='Bearer '+self.server.token,
                'compact':self.path.endswith('/compact') or inference_adapter._is_compaction(body)})
            return self.respond(body)
        if self.path.startswith('/rejected/'):
            value = json.dumps({'error': {'message': 'Synthetic request rejected'}}).encode()
            self.send_response(400)
            self.send_header('Content-Length', str(len(value)))
            self.end_headers()
            self.wfile.write(value)
            return
        if self.path in ('/backend-api/codex/images/generations', '/backend-api/codex/images/edits'):
            self.server.images.append({'path': self.path, 'body': body,
                'account': self.headers.get('ChatGPT-Account-ID'),
                'auth': self.headers.get('Authorization') == 'Bearer ' + self.server.token})
            self.json_reply({'created': 1, 'data': [{'b64_json': IMAGE_FIXTURE}]})
        elif self.path.endswith('/responses'):
            self.respond(body)
        else:
            self.send_response(204)
            self.end_headers()

    def respond(self, body, ws=None):
        warmup = body.get('generate') is False
        if ws and not warmup and getattr(self.server, 'close_once', False):
            self.server.close_once = False
            ws.close(1009, 'Synthetic message too big')
            return
        if not warmup:
            self.server.turns += 1
        index = self.server.turns
        if ws and not warmup and index == 1:
            # Exercise native image tool calls over a connection that includes
            # upstream heartbeats; byte-exact control forwarding is separately
            # required by the transport regression gate.
            for i in range(6):
                ws.send_control(9, b'official-fixture-heartbeat')
                self.server.heartbeats += 1
                time.sleep(.04)
        if index <= 2:
            args = {'prompt': 'Synthetic image tool fixture', 'transparent_background': False}
            if index == 2:
                args['referenced_image_paths'] = [str(self.server.reference)]
            item = {'type': 'custom_tool_call', 'name': 'exec', 'call_id': 'image_call_' + str(index),
                    'id': 'image_item_' + str(index),
                    'input': 'text(await tools.image_gen__imagegen(' + json.dumps(args) + '));'}
        else:
            item = {'id': 'services_done', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': 'NATIVE_SERVICES_OK', 'annotations': []}]}
        if getattr(self.server,'large_history',False) and (self.path.endswith('/compact') or inference_adapter._is_compaction(body)):
            item={'id':'cmp_large_fixture','type':'compaction','encrypted_content':'synthetic-large-compaction'}
        response = {'id': 'service_response_' + str(index), 'object': 'response',
                    'created_at': int(time.time()), 'status': 'completed', 'model': body['model'],
                    'output': [] if warmup else [item],
                    'usage': {'input_tokens': 10, 'output_tokens': 3, 'total_tokens': 13}}
        events = [{'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}}]
        if not warmup:
            events += [{'type': 'response.output_item.added', 'output_index': 0, 'item': item},
                       {'type': 'response.output_item.done', 'output_index': 0, 'item': item}]
        events += [{'type': 'response.completed', 'response': response}]
        if self.path.endswith('/compact'):
            return self.json_reply(response)
        if ws:
            for i, event in enumerate(events):
                ws.send(json.dumps({**event, 'sequence_number': i}))
        else:
            raw = ''.join('event: ' + e['type'] + '\ndata: ' + json.dumps({**e, 'sequence_number': i})
                          + '\n\n' for i, e in enumerate(events)).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)


def native_image_contract(app, reportdir, edition):
    """Real bundled CLI calls both image tools, with isolated synthetic auth/TLS.

    API-key-only fixtures do not expose native imagegen and therefore cannot
    validate this contract. Only this child trusts the temporary TLS certificate.
    """
    openssl = shutil.which('openssl')
    if not openssl and sys.platform == 'win32':
        git = shutil.which('git')
        candidates = [Path(git).resolve().parent.parent / 'usr/bin/openssl.exe'] if git else []
        openssl = next((str(p) for p in candidates if p.is_file()), None)
    patch.require(openssl, 'Native service validation requires OpenSSL (Git for Windows includes it)')
    with tempfile.TemporaryDirectory(prefix='native-service-contract-') as temp, contextlib.ExitStack() as owned:
        base = Path(temp)
        env = isolated_environment(base)
        work = base / 'work'
        work.mkdir()
        key, cert, ca, ca_key = (base / name for name in ('key.pem', 'cert.pem', 'ca.pem', 'ca-key.pem'))
        extensions, csr = base / 'server.ext', base / 'server.csr'
        extensions.write_text('subjectAltName=IP:127.0.0.1\nbasicConstraints=critical,CA:false\n'
                              'keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n')
        for args in (
            ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(ca_key), '-out', str(ca),
             '-days', '2', '-subj', '/CN=Isolated Native Test CA', '-addext', 'basicConstraints=critical,CA:true',
             '-addext', 'keyUsage=critical,keyCertSign,cRLSign'],
            ['req', '-new', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-out', str(csr),
             '-subj', '/CN=127.0.0.1'],
            ['x509', '-req', '-in', str(csr), '-CA', str(ca), '-CAkey', str(ca_key),
             '-CAcreateserial', '-out', str(cert), '-days', '2', '-extfile', str(extensions)],
        ):
            subprocess.run([openssl, *args], check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, timeout=30, **patch.background_options())
        tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls_context.load_cert_chain(str(cert), str(key))
        token = '.'.join(base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=') for value in
                        ({'alg': 'none'}, {'exp': int(time.time()) + 3600, 'sub': 'synthetic-user',
                          'https://api.openai.com/auth': {'chatgpt_account_id': 'synthetic-account',
                                                       'chatgpt_plan_type': 'plus'}}, {}))
        servers = []
        def stop_servers():
            for server in reversed(servers):
                server.shutdown()
                server.server_close()
        owned.callback(stop_servers)
        def start(server):
            server.daemon_threads = True
            threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True).start()
            servers.append(server)
            return server
        upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), NativeServicesFixture)
        upstream.turns, upstream.images, upstream.token, upstream.heartbeats = 0, [], token, 0
        upstream.compressed = False
        from official_client import compression_node
        upstream.compression_node = compression_node(app)
        upstream.reference = work / 'reference.png'
        upstream.reference.write_bytes(base64.b64decode(IMAGE_FIXTURE))
        start(upstream)
        base_url = 'http://127.0.0.1:' + str(upstream.server_port)
        metadata = http.server.ThreadingHTTPServer(('127.0.0.1', 0), NativeServicesFixture)
        metadata.socket = tls_context.wrap_socket(metadata.socket, server_side=True, do_handshake_on_connect=False)
        start(metadata)
        env.update(SSL_CERT_FILE=str(ca), CODEX_CA_CERTIFICATE=str(ca), HTTP_PROXY=base_url, HTTPS_PROXY=base_url,
                   ALL_PROXY=base_url, NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost')
        adapter = start(inference_adapter.create_server({'format': 1, 'edition': edition,
            'compression_node':upstream.compression_node,
            'identity': 'native-services-fixture', 'token': 'synthetic-' + 'x' * 40, 'port': 0,
            'gpt': {'chatgpt_base_url': base_url + '/backend-api/codex', 'api_base_url': base_url + '/v1'}, 'routes': {}},
            credential_reader=lambda _: 'synthetic-external-key'))
        catalog = base / 'catalog.json'
        raw = subprocess.check_output([str(patch.cli_path(app)), 'debug', 'models', '--bundled'],
                                      env=env, stderr=subprocess.PIPE, timeout=60, **patch.background_options())
        entry = next(v for v in json.loads(raw)['models'] if v['slug'] == GPT)
        entry.update(prefer_websockets=True, use_responses_lite=True)
        patch.save_json(catalog, {'models': [entry]})
        current = ('chatgpt_base_url="https://127.0.0.1:' + str(metadata.server_port)
                   + '"\ncli_auth_credentials_store="ephemeral"\n[features]\n'
                   'memories=false\napps=false\nimage_generation=true\n')
        config, _ = prepare_config(current, catalog, 'http://127.0.0.1:' + str(adapter.server_port)
                                   + '/synthetic-' + 'x' * 40 + '/backend-api/codex', mode='builtin')
        (base / 'home/config.toml').write_text(config, encoding='utf-8')
        log = owned.enter_context((Path(reportdir) / ('native-services-' + edition + '.log')).open('w', encoding='utf-8'))
        process = subprocess.Popen(probe_command(app), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                                   text=True, encoding='utf-8', env=env, cwd=work, **launch_options())
        owned.callback(stop_probe, process)
        rpc = Rpc(process)
        rpc.request('initialize', {'clientInfo': {'name': 'native_service_contract', 'version': '1'},
                                  'capabilities': {'experimentalApi': True}})
        rpc.request('account/login/start', {'type': 'chatgptAuthTokens', 'accessToken': token,
                                          'chatgptAccountId': 'synthetic-account'})
        tid = rpc.request('thread/start', {'model': GPT, 'modelProvider': 'openai',
                          'cwd': str(work), 'approvalPolicy': 'never',
                          'sandbox': 'danger-full-access' if sys.platform == 'win32' else 'workspace-write'})['thread']['id']
        turn = rpc.request('turn/start', {'threadId': tid, 'input': [{'type': 'text', 'text': 'Run synthetic image fixtures.'}]})
        from scripts.test_official_connection import finish as finish_services
        events = finish_services(rpc, tid, turn, timeout=60)
        patch.require([v['path'] for v in upstream.images] ==
                      ['/backend-api/codex/images/generations', '/backend-api/codex/images/edits'],
                      'Native generation/edit did not reach the official service')
        patch.require(all(v['auth'] and v['account'] == 'synthetic-account' for v in upstream.images),
                      'Native image authentication changed')
        patch.require(upstream.images[1]['body']['images'], 'Native reference image was lost')
        patch.require(sum(v.get('method') == 'item/completed' and
                          v['params']['item']['type'] == 'imageGeneration' for v in events) == 2,
                      'Native image tools did not both complete')
        patch.require(len(list((base / 'home/generated_images').rglob('*.png'))) == 2,
                      'Native generated image files were not saved')
        patch.require(upstream.heartbeats == 6, 'Native WebSocket path was not exercised')
        patch.require(upstream.compressed, 'Native upstream WebSocket compression was disabled')
        upstream.close_once = True
        started = time.monotonic()
        turn = rpc.request('turn/start', {'threadId': tid,
            'input': [{'type': 'text', 'text': 'Exercise the synthetic connection close.'}]})
        finish_services(rpc, tid, turn, timeout=20)
        patch.require(not upstream.close_once and time.monotonic() - started < 20,
                      'Native close/retry waited for an idle deadline')
        # Keep the same native WebSocket and provoke a registered-service 400.
        # Without the adapter's status field the native client silently waits
        # until its response deadline instead of finishing this failed turn.
        adapter.profile['routes'][GPT] = {'base_url': base_url + '/rejected',
            'credential': {'kind': 'file', 'path': str(base / 'unused-synthetic-key')}}
        started = time.monotonic()
        turn = rpc.request('turn/start', {'threadId': tid,
            'input': [{'type': 'text', 'text': 'Exercise the synthetic rejection.'}]})
        try:
            finish_services(rpc, tid, turn, timeout=15)
        except RuntimeError as exc:
            patch.require('400' in str(exc), 'Native client received an unexpected failure')
        else:
            raise AssertionError('Native rejection did not finish as a failed turn')
        patch.require(time.monotonic() - started < 15, 'Native error waited for an idle deadline')
        # A fresh native HTTP session uses the vendor's default zstd setting.
        # Supply a valid PNG with a large inert text chunk, without user media.
        stop_probe(process)
        entry['prefer_websockets']=False
        patch.save_json(catalog,{'models':[entry]})
        adapter.profile['routes'].clear()
        upstream.large_history,upstream.large_requests=True,[]
        png=base64.b64decode(IMAGE_FIXTURE)
        chunk=b'tEXt'+b'fixture\x00'+b'x'*48_100_000
        png=png[:-12]+struct.pack('!I',len(chunk)-4)+chunk+struct.pack('!I',zlib.crc32(chunk))+png[-12:]
        image='data:image/png;base64,'+base64.b64encode(png).decode()
        expected=hashlib.sha256(image.encode()).hexdigest()
        del png,chunk
        process=subprocess.Popen(probe_command(app),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=log,
            text=True,encoding='utf-8',env=env,cwd=work,**launch_options())
        owned.callback(stop_probe,process)
        rpc=Rpc(process)
        rpc.request('initialize',{'clientInfo':{'name':'large_native_history','version':'1'},'capabilities':{'experimentalApi':True}})
        rpc.request('account/login/start',{'type':'chatgptAuthTokens','accessToken':token,'chatgptAccountId':'synthetic-account'})
        tid=rpc.request('thread/start',{'model':GPT,'modelProvider':'openai','cwd':str(work),
            'approvalPolicy':'never','sandbox':'danger-full-access' if sys.platform=='win32' else 'workspace-write'})['thread']['id']
        turn=rpc.request('turn/start',{'threadId':tid,'input':[{'type':'text','text':'Synthetic history fixture.'},
                                                             {'type':'image','url':image}]})
        finish_services(rpc,tid,turn,timeout=60)
        _compact(rpc,tid)
        large=upstream.large_requests
        patch.require(len(large)>=2 and any(v['compact'] for v in large), 'Native large history did not compact')
        patch.require(all(v['bytes']>64_000_000 and v['images']==[expected] and v['auth'] for v in large),
                      'Native large image history was reduced, altered or lost authentication')
        patch.require(large[0]['encoding']=='zstd','Native HTTP request compression was not preserved')
    return {'native_image_generation': 'passed', 'native_image_edit': 'passed',
            'native_image_auth_and_saved_files': 'passed', 'native_image_websocket_roundtrip': 'passed',
            'native_websocket_compression': 'passed',
            'native_close_retries_without_idle_wait': 'passed',
            'native_adapter_errors_finish_without_idle_wait': 'passed',
            'native_large_image_history_and_compaction':'passed','native_http_request_compression':'passed'}


def mcp_runtime(executable=None):
    """Stdio helpers need python.exe even when an updater owns pythonw.exe.

    The official native client owns spawning this command with piped standard
    streams and its Windows CREATE_NO_WINDOW process option.
    """
    command = Path(executable or sys.executable)
    if command.name.lower() == 'pythonw.exe':
        command = command.with_name('python.exe')
        if not command.is_file():
            raise ValueError('Console Python is required for the stdio MCP fixture')
    return str(command)


class Fixture(ToolStub):
    def recovery_text(self, body):
        text = json.dumps(body, ensure_ascii=False)
        if str(body.get('instructions', '')).startswith('Create a factual handoff') or (
                body.get('messages') and 'Create a factual handoff' in str(body['messages'][0])):
            patch.require('INTEGER_TOOL_OK' in text and 'CONTEXT_OK' in text,
                          'Readable native tool/history evidence missing from recovery')
            patch.require('opaque-native-quota-fixture' not in text, 'Opaque history reached the selected provider')
            self.server.recovery_summaries.append(body['model'])
            return 'RECOVERY_FACTS INTEGER_TOOL_OK CONTEXT_OK'
        if getattr(self.server, 'recovering', False):
            patch.require('RECOVERY_FACTS' in text, 'Recovered summary missing from the continued native turn')
        return None

    def do_CONNECT(self):
        self.server.blocked_external += 1
        self.send_error(503, 'External connections disabled in this probe')

    def do_GET(self):
        if self.headers.get('Upgrade', '').lower() != 'websocket':
            self.send_error(404)
            return
        self.server.upgrades += 1
        ws = accept_websocket(self, response_headers=[('X-Codex-Turn-State', 'synthetic-sticky-state')])
        try:
            while (message := ws.recv()) is not None:
                opcode, raw = message
                patch.require(opcode == 1, 'Native Responses request must be JSON text')
                body = json.loads(raw)
                self.record(body, raw, 'websocket')
                if self.reject_exhausted_gpt(body, ws=ws):
                    continue
                response, events = self.simple_events(body, warmup=body.get('generate') is False)
                for index, event in enumerate(events):
                    ws.send(json.dumps({**event, 'sequence_number': index}))
        except OSError:
            pass
        finally:
            ws.close()

    def record(self, body, raw, transport):
        if body.get('model')==GPT:
            from reasoning_origin import MESSAGE_PREFIX, LEGACY_MESSAGE_ID
            patch.require(not any(isinstance(i,dict) and i.get('type')=='message' and isinstance(i.get('id'),str)
                and (i['id'].startswith(MESSAGE_PREFIX) or LEGACY_MESSAGE_ID.fullmatch(i['id'])) for i in body.get('input',[])),
                'Official endpoint received a foreign persisted message ID')
        self.server.wire.append({'model': body.get('model'), 'transport': transport, 'path': self.path,
            'history_summary': str(body.get('instructions', '')).startswith(('Create a factual handoff', 'Compact the ongoing task')),
            'request_bytes': len(raw), 'effort': body.get('reasoning', {}).get('effort'),
            'generate': body.get('generate'), 'previous_response_id': bool(body.get('previous_response_id')),
            'tool_count': len(body.get('tools', [])), 'input_types': [v.get('type', 'message')
                for v in body.get('input', []) if isinstance(v, dict)],
            'auth_is_official_fixture': self.headers.get('Authorization') == 'Bearer official-api-fixture',
            'auth_is_route_fixture': (self.headers.get('x-api-key') == 'independent-route-fixture' and self.headers.get('Authorization') is None) if self.path.endswith('/messages') else self.headers.get('Authorization') == 'Bearer independent-route-fixture',
            'auth_is_anonymous': self.headers.get('Authorization') is None and self.headers.get('x-api-key') is None,
            'official_account_header': self.headers.get('ChatGPT-Account-ID') is not None})

    def reject_exhausted_gpt(self, body, ws=None):
        if body.get('model') != GPT or not getattr(self.server, 'official_exhausted', False):
            return False
        error = {'type':'usage_limit_reached', 'code':'usage_limit_reached',
                 'message':'Synthetic ChatGPT usage limit reached', 'plan_type':'plus',
                 'resets_at':int(time.time()) + 3600}
        if ws is not None:
            ws.send(json.dumps({'type':'error', 'status':429, 'error':error}))
        else:
            payload = json.dumps({'error':error}).encode()
            self.send_response(429)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        return True

    def simple_events(self, body, warmup=False, item=None):
        index = len(self.server.wire)
        usage = 0 if warmup else self.server.next_usage
        if not warmup:
            self.server.next_usage = 10
        item = item or self.message('CONTEXT_OK')
        if body['model'] == GPT and inference_adapter._is_compaction(body):
            item = {'type':'compaction', 'id':'cmp_native_fixture_' + str(index),
                    'encrypted_content':'opaque-native-fixture-' + str(index)}
        response = {'id': 'resp_native_' + str(index), 'object': 'response', 'created_at': int(time.time()),
                    'status': 'completed', 'model': body['model'], 'output': [] if warmup else [item],
                    'usage': {'input_tokens': usage, 'input_tokens_details': {'cached_tokens': 0},
                              'output_tokens': 0 if warmup else 3, 'total_tokens': usage + (0 if warmup else 3)}}
        events = [{'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}}]
        if not warmup:
            events += [{'type': 'response.output_item.added', 'output_index': 0, 'item': {**item, 'status': 'in_progress'}},
                       {'type': 'response.output_item.done', 'output_index': 0, 'item': item}]
        events.append({'type': 'response.completed', 'response': response})
        return response, events

    def chat_response(self, raw, messages_protocol=False):
        body = json.loads(raw)
        if messages_protocol:
            patch.require(self.headers.get('x-api-key')=='independent-route-fixture' and not self.headers.get('Authorization'),'Messages authentication isolation failed')
            messages=[{'role':'system','content':'\n'.join(b['text'] for b in body.get('system',[]))}] if body.get('system') else []
            for message in body['messages']:
                plain=[b.get('text','') for b in message['content'] if b['type']=='text']
                if plain:messages.append({'role':message['role'],'content':'\n'.join(plain)})
                for block in message['content']:
                    if block['type']=='tool_result':messages.append({'role':'tool','tool_call_id':block['tool_use_id'],'content':block['content']})
            body={**body,'messages':messages,'reasoning_effort':body.get('output_config',{}).get('effort'),
                  'tools':[{'function':{'name':t['name'],'parameters':t['input_schema']}} for t in body.get('tools',[])]}
        tools = [v['function'] for v in body.get('tools', [])]
        messages = body['messages']
        self.record({'model':body['model'], 'reasoning':{'effort':body.get('reasoning_effort')},
                     'tools':tools, 'input':messages,
                     'instructions':messages[0].get('content','') if messages else ''}, raw, 'http')
        phase, error = self.server.phase, None
        call = None
        text = self.recovery_text(body) or 'CONTEXT_OK'
        if self.server.tool_mode and phase == 'search':
            if len(json.dumps(tools).encode()) > 120000 or not any(v['name'] == SEARCH_NAME for v in tools):
                error = 'Chat route eager catalog or missing native discovery'
            call = (SEARCH_NAME, 'chat_search', '{"query":"probe_tool_42","limit":1}')
            self.server.phase = 'call'
        elif self.server.tool_mode and phase == 'call':
            alias = ToolAdapter.alias('mcp__bulk', 'probe_tool_42')
            loaded = [v['name'] for v in tools if v['name'].startswith('mcp__bulk__')]
            if loaded != [alias]:
                error = 'Chat route did not expose exactly the discovered MCP tool'
            call = (alias, 'chat_tool', '{"value":"check"}')
            self.server.phase = 'answer'
        elif self.server.tool_mode and phase == 'answer':
            if not any(v.get('role') == 'tool' and 'LAZY_TOOL_OK' in str(v.get('content')) for v in messages):
                error = 'Chat route native discovered tool did not execute'
            text = 'LAZY_TOOL_OK'
            self.server.phase = 'search'
        elif phase == 'custom_call':
            declaration = next(v for v in tools if v['name'].endswith('apply_patch'))
            call = (declaration['name'], 'chat_custom', json.dumps({'input':
                '*** Begin Patch\n*** Add File: CHAT_CUSTOM_TOOL_OK.txt\n+CHAT_CUSTOM_TOOL_OK\n*** End Patch'}))
            self.server.phase = 'custom_answer'
        elif phase == 'custom_answer':
            if not any(v.get('role') == 'tool' and v.get('tool_call_id') == 'chat_custom' for v in messages):
                error = 'Chat route custom tool result did not return'
            text = 'CHAT_CUSTOM_TOOL_OK'
            self.server.phase = 'message'
        if self.server.tool_mode or phase.startswith('custom_'):
            self.server.records.append({'model':body['model'],'request_bytes':len(raw),'phase':phase,'error':error})
        if error:
            payload = json.dumps({'error':{'message':error}}).encode()
            self.send_response(400)
            self.send_header('Content-Length',str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        delta = ({'tool_calls':[{'index':0,'id':call[1],'type':'function',
            'function':{'name':call[0],'arguments':call[2]}}]} if call else {'role':'assistant','content':text})
        chunks = [{'id':'chat_fixture_'+str(len(self.server.wire)), 'model':body['model'],
                   'choices':[{'index':0,'delta':delta,'finish_reason':None}]},
                  {'choices':[{'index':0,'delta':{},'finish_reason':'tool_calls' if call else 'stop'}],
                   'usage':{'prompt_tokens':3000,'completion_tokens':3,'total_tokens':3003}}]
        if messages_protocol:
            block={'type':'tool_use','id':'messages_'+call[1],'name':call[0],'input':json.loads(call[2])} if call else {'type':'text','text':text}
            chunks=[{'type':'message_start','message':{'usage':{'input_tokens':3000,'output_tokens':0}}},
                    {'type':'content_block_start','index':0,'content_block':block}, {'type':'content_block_stop','index':0},
                    {'type':'message_delta','delta':{'stop_reason':'tool_use' if call else 'end_turn'},'usage':{'output_tokens':3}}, {'type':'message_stop'}]
        payload = b''.join(b'data: '+json.dumps(v).encode()+b'\n\n' for v in chunks)+(b'' if messages_protocol else b'data: [DONE]\n\n')
        self.send_response(200)
        self.send_header('Content-Type','text/event-stream')
        self.send_header('Content-Length',str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        if self.path.endswith('/messages'):return self.chat_response(raw,True)
        if self.path.endswith('/chat/completions'):
            return self.chat_response(raw)
        if not self.path.endswith('/responses'):
            self.server.auxiliary_requests += 1
            self.send_error(503)
            return
        patch.require(not self.headers.get('Content-Encoding'), 'Native compression must be disabled in config')
        body = json.loads(raw)
        self.record(body, raw, 'http')
        if self.reject_exhausted_gpt(body):
            return
        recovery = self.recovery_text(body)
        compact = not body.get('tools')
        original_reader = self.rfile
        try:
            self.rfile = io.BytesIO(raw)
            if self.server.tool_mode and self.server.phase not in ('custom_call', 'custom_answer'):
                return super().do_POST()
            if self.server.phase == 'custom_call' and not compact:
                candidates = [(None, t) for t in body.get('tools', []) if t.get('type') == 'function']
                candidates += [(t['name'], child) for t in body.get('tools', []) if t.get('type') == 'namespace'
                               for child in t.get('tools', [])]
                namespace, tool = next((ns, t) for ns, t in candidates if t.get('name', '').endswith('apply_patch'))
                item = {'type': 'function_call', 'id': 'fc_custom_fixture', 'call_id': 'custom_fixture',
                        'status': 'completed', 'name': tool['name'], 'arguments': json.dumps({'input':
                        '*** Begin Patch\n*** Add File: CUSTOM_TOOL_OK.txt\n+CUSTOM_TOOL_OK\n*** End Patch'})}
                if namespace:
                    item['namespace'] = namespace
                self.server.phase = 'custom_answer'
            elif self.server.phase == 'custom_answer' and not compact:
                patch.require(any(v.get('type') == 'function_call_output' and v.get('call_id') == 'custom_fixture'
                    for v in body.get('input', [])), 'Custom tool output did not return through native execution')
                item = self.message('CUSTOM_TOOL_OK')
                self.server.phase = 'message'
            else:
                item = self.message(recovery or ('A concise synthetic summary. Continue the pending test.' if compact else 'CONTEXT_OK'))
            if compact:
                self.server.compactions += 1
            _, events = self.simple_events(body, item=item)
            payload = ''.join('event: ' + v['type'] + '\ndata: ' + json.dumps({**v, 'sequence_number': n}) + '\n\n'
                              for n, v in enumerate(events)).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        finally:
            self.rfile = original_reader


class ObservedSession(inference_adapter.WebSocketSession):
    """Record only synthetic frame metadata, without altering relay behavior."""
    def receive(self, opcode, payload):
        if opcode == 1:
            body = json.loads(payload)
            self.handler.server.native_frames.append({'model': body.get('model'),
                'type': body.get('type'), 'generate': body.get('generate'), 'bytes': len(payload)})
        return super().receive(opcode, payload)


def _windows(events):
    return sorted({event['params']['tokenUsage']['modelContextWindow'] for event in events
                   if event.get('method') == 'thread/tokenUsage/updated'})


def _compact(rpc, tid):
    rpc.request('thread/compact/start', {'threadId': tid})
    values, deadline = [], time.monotonic() + 60
    while time.monotonic() < deadline:
        event = rpc.pending.pop(0) if rpc.pending else rpc.receive_event(max(.1, deadline - time.monotonic()))
        if event.get('params', {}).get('threadId') != tid:
            continue
        values.append(event)
        if event.get('method') == 'turn/completed':
            patch.require(event['params']['turn']['status'] == 'completed', 'Native plain compaction failed')
            return values
    raise TimeoutError('Native plain compaction did not finish')


def require_tool_success(events, kind, marker=None):
    """Require native execution evidence, not an error quoting the tool input."""
    items = [event.get('params', {}).get('item', {}) for event in events
             if event.get('method') == 'item/completed']
    for item in items:
        if item.get('type') != kind or item.get('status') != 'completed':
            continue
        if kind == 'commandExecution':
            if type(item.get('exitCode')) is not int or item['exitCode'] != 0:
                continue
            if marker not in item.get('aggregatedOutput', '').splitlines():
                continue
        return item
    raise patch.PatchError('Native ' + kind + ' did not report successful execution')


def run(app, reportdir, edition, *, services=False, progress=None):
    app, reportdir = Path(app), Path(reportdir)
    patch.require(edition in ('ds', 'proxy', 'unified'), 'Unknown edition')
    reportdir.mkdir(parents=True, exist_ok=True)
    report = {'status': 'failed', 'edition': edition, 'external_inference_turns': 0,
              'real_credentials_used': False, 'provider': PROVIDER, 'mcp_tools': 160, 'checks': {}}
    if progress:
        class Checks(dict):
            def __setitem__(self,key,value):
                super().__setitem__(key,value)
                progress(len(self),(20 if edition!='ds' else 16)+(9 if services else 0))
        report['checks']=Checks()
        progress(0,(20 if edition!='ds' else 16)+(9 if services else 0))
    report.update(runner_runtime=Path(sys.executable).name, mcp_runtime=Path(mcp_runtime()).name)
    process, adapter, upstream = None, None, None
    started_servers = []

    def stop_owned():
        nonlocal process
        if process:
            stop_probe(process)
            process = None

    def close_servers():
        nonlocal adapter, upstream
        if upstream:
            report.setdefault('wire', upstream.wire)
            report.setdefault('tool_records', upstream.records)
        for server in (adapter, upstream):
            if server:
                if server in started_servers:
                    server.shutdown()
                server.server_close()
        adapter = upstream = None

    try:
        with tempfile.TemporaryDirectory(prefix='codex-official-client-') as tmp, contextlib.ExitStack() as owned:
            base = Path(tmp)
            env = isolated_environment(base)
            work = base / 'work'
            work.mkdir()
            raw = subprocess.check_output([str(patch.cli_path(app)), 'debug', 'models', '--bundled'],
                env=env, stderr=subprocess.PIPE, timeout=60, **patch.background_options())
            native = json.loads(raw)
            template = patch.catalog_template(native)
            models = [(GPT, 272000), (DS, 1048576)]
            if edition != 'ds':
                models += [('grok-4.7', 500000), ('gemini-3.8-flash-high', 1048576), ('local-probe', 256000), ('messages-probe', 1000000)]
            entries = []
            for slug, window in models:
                entry = copy.deepcopy(template)
                levels = ['low', 'medium', 'high'] if slug != DS else ['low', 'high', 'max']
                entry.update(slug=slug, display_name=slug, context_window=window, max_context_window=window,
                    auto_compact_token_limit=int(window * .7), effective_context_window_percent=100,
                    supported_reasoning_levels=[{'effort': v, 'description': v} for v in levels],
                    default_reasoning_level='high', supports_reasoning_effort_updates=True,
                    use_responses_lite=slug == GPT, prefer_websockets=slug == GPT, tool_mode=None,
                    experimental_supported_tools=[], visibility='list',
                    supports_search_tool=True, shell_type='unified_exec')
                entries.append(entry)
            catalog = base / 'models.json'
            catalog_routes = {slug: {'context_window': window, 'max_context_window': window,
                'compact_limit': int(window * .7)} for slug, window in models if slug != GPT}
            patch.save_json(catalog, adapt_catalog({'models': entries}, catalog_routes, {}))
            upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
            upstream.daemon_threads = True
            upstream.records, upstream.requests, upstream.wire = [], [], []
            upstream.phase, upstream.answer_usage, upstream.next_usage = 'message', 3000, 10
            upstream.tool_mode, upstream.compactions, upstream.upgrades = False, 0, 0
            upstream.blocked_external, upstream.auxiliary_requests = 0, 0
            upstream.recovery_summaries = []
            endpoint = 'http://127.0.0.1:' + str(upstream.server_port)
            profile = {'format': 1, 'edition': edition, 'identity': 'synthetic-official-client-' + edition,
                'token': 'synthetic-local-token-' + 'x' * 32, 'port': 0, 'official_models': [GPT],
                'history_home':str(base/'home'), 'legacy_history_dir':str(base/'cache'), 'summary_model':GPT,
                'gpt': {'chatgpt_base_url': endpoint + '/official', 'api_base_url': endpoint + '/official'},
                'routes': {slug: {'base_url': endpoint + '/route', 'credential': {'kind': 'file', 'path': str(base / 'route.key')},
                    'tool_adapter': True, 'flatten_namespaces': slug == 'local-probe',
                    'normalize_instructions': slug == 'local-probe', 'chat_only': False,
                    'context_window':window} for slug, window in models if slug != GPT}}
            if 'local-probe' in profile['routes']:
                profile['routes']['local-probe'].update(credential={'kind':'none'},
                    wire_api='chat_completions', supports_reasoning_effort=True)
            if 'messages-probe' in profile['routes']:
                profile['routes']['messages-probe'].update(wire_api='anthropic_messages',max_output_tokens=65536,reasoning_levels=['low','medium','high'],flatten_namespaces=True)
            (base / 'route.key').write_text('independent-route-fixture')
            adapter = inference_adapter.create_server(profile, session_factory=ObservedSession)
            adapter.native_frames = []
            for server in (upstream, adapter):
                threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True).start()
                started_servers.append(server)
            owned.callback(close_servers)
            relay_url = 'http://127.0.0.1:' + str(adapter.server_port) + '/' + profile['token'] + '/backend-api/codex'
            current = ('cli_auth_credentials_store="file"\nweb_search="disabled"\nchatgpt_base_url=' + json.dumps(endpoint) + '\n'
                '[features]\nmemories=false\napps=false\n'
                '[mcp_servers.bulk]\ncommand=' + json.dumps(mcp_runtime()) + '\nargs=' +
                json.dumps([str(Path(__file__).resolve()), '--mcp']) + '\n')
            legacy_config, legacy_ownership = prepare_config(current, catalog, relay_url, mode='native-summary')
            config, _ = prepare_config(legacy_config, catalog, relay_url, mode='builtin', previous=legacy_ownership)
            (base / 'home/config.toml').write_text(config, encoding='utf-8')
            patch.save_json(base / 'home/auth.json', {'OPENAI_API_KEY': 'official-api-fixture'})
            env.update(OPENAI_API_KEY='official-api-fixture', HTTP_PROXY=endpoint, HTTPS_PROXY=endpoint,
                       ALL_PROXY=endpoint, NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost')
            with (reportdir / ('official-client-' + edition + '.log')).open('w', encoding='utf-8') as log:
                process = subprocess.Popen(probe_command(app), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                    text=True, encoding='utf-8', errors='replace', env=env, cwd=work, **launch_options())
                owned.callback(stop_owned)
                rpc = Rpc(process)
                rpc.request('initialize', {'clientInfo': {'name': 'official_client_probe', 'version': '1'},
                                          'capabilities': {'experimentalApi': True}})
                listed = rpc.request('model/list', {'includeHidden': False, 'limit': 100})['data']
                for slug, _ in models:
                    item = next(v for v in listed if v.get('model') == slug)
                    patch.require(len(item.get('supportedReasoningEfforts', [])) >= 2, 'Native reasoning choices missing: ' + slug)
                report['checks']['native_catalog_reasoning'] = 'passed'
                # Only this disposable synthetic home/work directory uses the
                # Windows unrestricted fixture. Never change user permissions.
                sandbox = 'danger-full-access' if sys.platform == 'win32' else 'workspace-write'
                report['sandbox_requested'] = sandbox
                report['sandbox_scope'] = 'isolated temporary home and work directory'
                initial = rpc.request('thread/start', {'model': DS, 'modelProvider': 'codex-model-adapter', 'cwd': str(work),
                    'approvalPolicy': 'never', 'sandbox': sandbox, 'baseInstructions': 'This is a synthetic local regression.',
                    'config': {'web_search': 'disabled', 'features.memories': False, 'features.apps': False}})
                report['sandbox_effective'] = initial.get('sandbox')
                if sys.platform == 'win32':
                    patch.require(isinstance(initial.get('sandbox'), dict) and initial['sandbox'].get('type') == 'dangerFullAccess',
                                  'Native Windows did not grant the isolated synthetic fixture sandbox')
                tid = initial['thread']['id']
                turns = []

                def turn(model, label, effort='high', resumed_window=False):
                    before_wire = len(upstream.wire)
                    result = rpc.request('turn/start', {'threadId': tid, 'model': model, 'effort': effort,
                        'input': [{'type': 'text', 'text': 'Run the synthetic local test and reply briefly.'}]})
                    events = finish(rpc, tid, result)
                    windows = _windows(events)
                    expected = dict(models)[model]
                    last_window = next((v['params']['tokenUsage']['modelContextWindow'] for v in reversed(events)
                                        if v.get('method')=='thread/tokenUsage/updated'), None)
                    patch.require((windows == [expected] or resumed_window and last_window == expected) and expected > 128000,
                                  'Native catalog window changed: ' + model + ' ' + str(windows))
                    turns.append({'model': model, 'label': label, 'context_windows': windows,
                        'effort': effort,
                        'compaction': any(v.get('params', {}).get('item', {}).get('type') == 'contextCompaction' for v in events)})
                    requests = [v for v in upstream.wire[before_wire:] if v['generate'] is not False and not v['history_summary']]
                    patch.require(requests and all(v['effort'] == effort for v in requests),
                                  'Selected reasoning effort did not reach inference: ' + model + ' ' + effort)
                    return events

                turn(DS, 'legacy adapter chat before native identity migration')
                # Installation runs metadata migration with the desktop stopped
                # and a fresh native process; an already loaded thread intentionally
                # ignores provider overrides. Reproduce that lifecycle exactly.
                stop_owned()
                process = subprocess.Popen(probe_command(app), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                    text=True, encoding='utf-8', errors='replace', env=env, cwd=work, **launch_options())
                rpc = Rpc(process)
                rpc.request('initialize', {'clientInfo':{'name':'native_identity_migration_probe','version':'1'},
                    'capabilities':{'experimentalApi':True}})
                from official_client_migration import migrate
                migration = migrate(rpc, {'codex-model-adapter'}, base/'migration.json', target='openai')
                stop_owned()
                process = subprocess.Popen(probe_command(app), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                    text=True, encoding='utf-8', errors='replace', env=env, cwd=work, **launch_options())
                rpc = Rpc(process)
                rpc.request('initialize', {'clientInfo':{'name':'native_post_migration_probe','version':'1'},
                    'capabilities':{'experimentalApi':True}})
                resumed = rpc.request('thread/resume', {'threadId':tid, 'excludeTurns':True,
                    'approvalPolicy':'never', 'sandbox':sandbox})
                patch.require(migration['migrated_chats'] == 1 and resumed['modelProvider'] == 'openai'
                    and resumed['thread']['id'] == tid, 'Existing chat did not regain native provider identity')
                report['checks']['existing_chat_native_identity_migration'] = 'passed'
                turn(DS, 'registered model local WS prewarm')
                for model, _ in models:
                    turn(model, 'same-thread switch')
                turn(GPT, 'return to GPT')
                report['checks']['same_thread_model_switching'] = 'passed'
                report['checks']['native_cross_provider_message_ids'] = 'passed'
                target = DS if edition == 'ds' else 'grok-4.7'
                turn(target, 'enter compaction model')
                turn(target, 'reasoning switch to low', 'low')
                turn(target, 'reasoning switch back to high', 'high')
                report['checks']['reasoning_high_low_high'] = 'passed'
                upstream.next_usage = int(dict(models)[target] * .7) + 100
                turn(target, 'synthetic usage above compaction threshold')
                auto = turn(target, 'automatic native compaction bridge')
                patch.require(any(v.get('params', {}).get('item', {}).get('type') == 'contextCompaction' for v in auto),
                              'Automatic native compaction did not run')
                _compact(rpc, tid)
                turn(target, 'after manual plain compaction')
                turn(GPT, 'return to GPT after plain compaction')
                turn(target, 'return to registered model after plain compaction')
                report['checks']['native_auto_manual_compaction_windows'] = 'passed'
                upstream.tool_mode, upstream.phase = True, 'search'
                before = len(upstream.records)
                turn(target, '160-tool lazy discovery and execution')
                records = upstream.records[before:]
                patch.require([v['phase'] for v in records] == ['search', 'call', 'answer'] and not any(v['error'] for v in records),
                              'Native lazy MCP discovery/execution failed')
                report['checks']['native_lazy_160_mcp_tools'] = 'passed'
                upstream.phase = 'integer_call'
                integer_events = turn(target, 'integral-decimal native tool arguments')
                integer = require_tool_success(integer_events, 'commandExecution', 'INTEGER_TOOL_OK')
                report['integer_execution'] = {'exit_code':integer['exitCode'], 'exact_marker_line':True}
                patch.require(not any(v['error'] for v in upstream.records), 'Native integral-decimal execution failed')
                report['checks']['native_integer_arguments'] = 'passed'
                upstream.tool_mode, upstream.phase = False, 'custom_call'
                custom_events = turn(target, 'native custom apply_patch execution')
                custom = require_tool_success(custom_events, 'fileChange')
                patch.require((work / 'CUSTOM_TOOL_OK.txt').read_text(encoding='utf-8').strip() == 'CUSTOM_TOOL_OK', 'Custom apply_patch did not execute')
                report['custom_execution'] = {'status':custom['status'], 'file_content_exact':True}
                report['checks']['native_custom_tool_execution'] = 'passed'
                if edition != 'ds':
                    upstream.tool_mode, upstream.phase = True, 'search'
                    turn('local-probe', 'flattened namespace lazy tool execution')
                    patch.require(not any(v['error'] for v in upstream.records), 'Local flattened namespace tool execution failed')
                    report['checks']['local_flattened_tools'] = 'passed'
                    report['checks']['native_chat_completions_lazy_tools'] = 'passed'
                    upstream.tool_mode, upstream.phase = False, 'custom_call'
                    chat_custom_events = turn('local-probe', 'Chat Completions native custom tool execution')
                    require_tool_success(chat_custom_events, 'fileChange')
                    patch.require((work / 'CHAT_CUSTOM_TOOL_OK.txt').read_text().strip() == 'CHAT_CUSTOM_TOOL_OK',
                                  'Chat Completions custom apply_patch did not execute')
                    turn(GPT, 'return to GPT after Chat Completions tools')
                    report['checks']['native_chat_custom_tools_return_gpt'] = 'passed'
                    upstream.tool_mode,upstream.phase=True,'search'
                    turn('messages-probe','Anthropic native discovery and tool execution')
                    patch.require(not any(v['error'] for v in upstream.records),'Anthropic native tools failed')
                    upstream.tool_mode,upstream.phase=False,'message'
                    turn(GPT,'return to GPT after Anthropic Messages')
                    report['checks']['native_anthropic_tools_return_gpt']='passed'
                # A real native usage-limit error must not poison the same
                # thread when its next turn selects an independent provider.
                # Carry a native usage count above the destination's threshold.
                # A short quota fixture misses pre-turn compaction during switching.
                upstream.next_usage = int(dict(models)[DS] * .7) + 100
                turn(GPT, 'long GPT history before quota exhaustion')
                upstream.official_exhausted = True
                result = rpc.request('turn/start', {'threadId':tid, 'model':GPT, 'effort':'high',
                    'input':[{'type':'text','text':'Exercise the synthetic exhausted GPT quota.'}]})
                deadline = time.monotonic() + 60
                quota_turn = None
                while time.monotonic() < deadline:
                    event = rpc.pending.pop(0) if rpc.pending else rpc.receive_event(max(.1, deadline-time.monotonic()))
                    if (event.get('method') == 'turn/completed'
                            and event.get('params',{}).get('threadId') == tid
                            and event['params']['turn']['id'] == result['turn']['id']):
                        quota_turn = event['params']['turn']
                        break
                patch.require(quota_turn is not None and quota_turn['status'] == 'failed',
                              'Synthetic exhausted GPT quota did not fail the native turn')
                quota_error = (quota_turn.get('error') or {}).get('codexErrorInfo')
                patch.require(quota_error == 'usageLimitExceeded',
                              'Native client did not classify the quota error: ' + str(quota_error))
                before = len(upstream.wire)
                for model, _ in models:
                    if model != GPT:
                        turn(model, 'independent provider after exhausted GPT quota')
                patch.require(not any(v['model'] == GPT for v in upstream.wire[before:]),
                              'Independent turn contacted GPT after quota exhaustion')
                report['checks']['same_thread_external_after_gpt_quota_exhausted'] = 'passed'
                patch.require(any(t['compaction'] for t in turns if t['label']=='independent provider after exhausted GPT quota'), 'Long quota switch did not exercise external compaction')
                report['checks']['long_history_compaction_after_gpt_quota_exhausted'] = 'passed'
                report['quota_error'] = quota_error
                upstream.official_exhausted = False
                turn(GPT, 'return after synthetic GPT quota reset')
                native_identity = rpc.request('thread/resume', {'threadId':tid})
                patch.require(native_identity['modelProvider'] == 'openai', 'Native prediction eligibility provider identity changed')
                fork = rpc.request('thread/fork', {'threadId':tid, 'ephemeral':True,
                    'experimentalPredictionMode':True, 'excludeTurns':True})
                patch.require(fork['modelProvider'] == 'openai', 'Prediction fork lost native provider identity')
                predicted = rpc.request('turn/start', {'threadId':fork['thread']['id'],
                    'input':[{'type':'text','text':'Return one brief suggested next instruction.'}]})
                finish(rpc, fork['thread']['id'], predicted)
                report['checks']['native_prediction_provider_and_fork'] = 'passed'
                thread = rpc.request('thread/read', {'threadId': tid, 'includeTurns': True})['thread']
                patch.require(thread['id'] == tid and len(thread['turns']) >= len(turns), 'Native thread history was lost')
                official = [v for v in upstream.wire if v['model'] == GPT]
                routed = [v for v in upstream.wire if v['model'] != GPT]
                patch.require(official and all(v['auth_is_official_fixture'] and not v['auth_is_route_fixture'] for v in official),
                              'Official GPT credential boundary failed')
                patch.require(routed and all((v['auth_is_anonymous'] if v['model'] == 'local-probe' else v['auth_is_route_fixture']) and not v['auth_is_official_fixture']
                    and not v['official_account_header'] and v['transport'] == 'http' for v in routed),
                              'Registered model credential/HTTP boundary failed')
                patch.require(any(v['transport'] == 'websocket' for v in official), 'Native GPT WebSocket transport was not exercised')
                report['checks']['gpt_ws_registered_http_auth_isolation'] = 'passed'
                patch.require(any(v['model'] == DS and v['generate'] is False for v in adapter.native_frames),
                              'Native registered-model WebSocket prewarm was not exercised')
                patch.require(not any(v['model'] != GPT and v['generate'] is False for v in upstream.wire),
                              'Registered-model prewarm reached the inference endpoint')
                report['checks']['native_registered_ws_local_prewarm'] = 'passed'
                # Seed an old encrypted-compaction event ONLY in this disposable
                # native rollout. The genuine CLI must resume and use local
                # visible history while the simulated official quota is empty.
                path = Path(thread['path'])
                from reasoning_origin import _source_path
                path = _source_path(path, base/'home')
                patch.require(path.suffix == '.jsonl', 'Native probe needs a disposable readable rollout')
                stop_owned()
                opaque = {'type':'compaction', 'id':'cmp_quota_fixture',
                          'encrypted_content':'opaque-native-quota-fixture'}
                prior_records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
                next_ordinal = max((v.get('ordinal', -1) for v in prior_records), default=-1) + 1
                with path.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps({'timestamp':'2026-01-01T00:00:00Z','ordinal':next_ordinal,'type':'compacted',
                        'payload':{'message':'','replacement_history':[opaque]}})+'\n')
                prefix = path.read_bytes()
                process = subprocess.Popen(probe_command(app), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                    text=True, encoding='utf-8', errors='replace', env=env, cwd=work, **launch_options())
                rpc = Rpc(process)
                rpc.request('initialize', {'clientInfo':{'name':'native_history_recovery_probe','version':'1'},
                                          'capabilities':{'experimentalApi':True}})
                resumed = rpc.request('thread/resume', {'threadId':tid,'model':DS,'modelProvider':PROVIDER})
                patch.require(resumed['thread']['id']==tid, 'History recovery changed the chat identity')
                rpc.pending.clear()  # Discard the old window snapshot emitted by resume.
                upstream.official_exhausted, upstream.recovering = True, True
                upstream.recovery_summaries = []
                before = len(upstream.wire)
                for model, _ in models:
                    if model != GPT:
                        turn(model, 'recover old encrypted history with no GPT quota', resumed_window=model==DS)
                turn(DS, 'reuse recovered history cache')
                upstream.tool_mode, upstream.phase = True, 'integer_call'
                after_recovery = turn(target, 'native tool execution after old history recovery')
                require_tool_success(after_recovery, 'commandExecution', 'INTEGER_TOOL_OK')
                report['checks']['native_tools_after_old_history_recovery'] = 'passed'
                patch.require(upstream.recovery_summaries == [m for m,_ in models if m!=GPT],
                              'Recovery did not summarize once per selected model/cache')
                patch.require(all(v['model']!=GPT for v in upstream.wire[before:]),
                              'Local history recovery contacted the official GPT service')
                patch.require(path.read_bytes().startswith(prefix), 'History recovery rewrote the existing native rollout')
                report['checks']['old_encrypted_history_without_gpt_quota'] = 'passed'
                report['checks']['selected_model_summary_cache_and_original_prefix'] = 'passed'
                report.update(status='passed', turns=turns, retained_turns=len(thread['turns']),
                    upstream_websocket_connections=upstream.upgrades, compact_requests=upstream.compactions,
                    blocked_external_connections=upstream.blocked_external, wire=upstream.wire,
                    tool_records=upstream.records, native_frames=adapter.native_frames)
        if services:
            for name,status in native_image_contract(app, reportdir, edition).items():report['checks'][name]=status
    except Exception as exc:
        report['status'] = 'failed'
        report['error_type'], report['error'] = type(exc).__name__, str(exc)[:1000]
        if upstream:
            report.update(wire=upstream.wire, tool_records=upstream.records)
        raise
    finally:
        stop_owned()
        close_servers()
        patch.save_json(reportdir / ('official-client-' + edition + '-result.json'), report)
    return report


if __name__ == '__main__':
    if sys.argv[1:] == ['--mcp']:
        mcp_fixture()
    else:
        parser = argparse.ArgumentParser()
        parser.add_argument('--app', type=Path, required=True)
        parser.add_argument('--reportdir', type=Path, required=True)
        parser.add_argument('--edition', choices=['ds', 'proxy', 'unified'], required=True)
        parser.add_argument('--services', action='store_true',
                            help='Required release gate: native image tools with synthetic TLS/auth')
        args = parser.parse_args()
        result = run(args.app, args.reportdir, args.edition, services=args.services)
        print(json.dumps({'status': result['status'], 'edition': args.edition, 'checks': result['checks']}), flush=True)
