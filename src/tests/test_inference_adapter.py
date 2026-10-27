import copy
import gzip
import http.client
import http.server
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch

import inference_adapter as relay
from inference_transport import accept_websocket, connect_websocket


def encoded(value):
    return json.dumps(value, ensure_ascii=False).encode()


def observe_disconnect(connection, disconnected):
    """TCP cancellation may reach Windows as EOF or an explicit reset/abort."""
    try:
        while connection.recv(1):
            pass
    except (ConnectionResetError, ConnectionAbortedError):
        pass
    except OSError:
        return  # Timeouts and unrelated socket failures do not prove closure.
    disconnected.set()


class Upstream(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        if '/native-tool-hang' in self.path:
            self.server.hanging.set()
            self.connection.settimeout(3)
            observe_disconnect(self.connection, self.server.cancelled)
            self.close_connection = True
            return
        if '/native-tool' in self.path:
            self.server.seen.append((self.path, dict(self.headers), raw, None))
            payload = b'\x00opaque-native-response\xff'
            self.send_response(200 if self.command in ('GET', 'HEAD') else 422)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(payload)
            return
        body = json.loads(raw)
        self.server.seen.append((self.path, dict(self.headers), raw, body))
        if '/images/' in self.path:
            payload = encoded({'created': 1, 'data': [{'b64_json': 'synthetic-image-bytes',
                                                      'generation_id': 'fixture-generation'}]})
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if body.get('metadata',{}).get('fixture_delay'):
            time.sleep(.12)
        if self.path.startswith('/redirect/'):
            self.send_response(302)
            self.send_header('Location', 'https://credential-leak.invalid/')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if self.path.startswith('/fail/'):
            self.send_response(429)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        output = [{'id': 'message_' + str(len(self.server.seen)), 'type': 'message', 'role': 'assistant',
                   'content': [{'type': 'output_text', 'text': 'OK'}]}]
        if body.get('tools') and body['tools'][0].get('name') == 'apply_patch':
            output = [{'id': 'fc_fixture', 'type': 'function_call', 'name': 'apply_patch',
                       'call_id': 'call_fixture', 'arguments': '{"input":"exact tool input"}'}]
        response = {'id': 'resp_' + str(len(self.server.seen)), 'status': 'completed',
                    'model': body['model'], 'output': output}
        if not body.get('stream'):
            raw = encoded(response)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('X-Official-Fixture', 'preserved')
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        if self.path.startswith('/hang/'):
            self.server.hanging.set()
            self.connection.settimeout(3)
            observe_disconnect(self.connection, self.server.cancelled)
            return
        events = [{'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
                  {'type': 'response.output_item.done', 'output_index': 0, 'item': output[0]},
                  {'type': 'response.completed', 'response': response}]
        for event in events:
            self.wfile.write(b'event: ' + event['type'].encode() + b'\ndata: ' + encoded(event) + b'\n\n')
            self.wfile.flush()

    def do_GET(self):
        if self.headers.get('Upgrade', '').lower() != 'websocket':
            return self.do_POST()
        self.server.upgrades.append((self.path, dict(self.headers)))
        ws = accept_websocket(self, response_headers=[('X-Codex-Turn-State', 'fixture-sticky'),
                                                     ('X-Models-Etag', 'fixture-models-etag')])
        try:
            while (message := ws.recv()) is not None:
                opcode, raw = message
                self.server.ws_seen.append((opcode, raw))
                if opcode == 2:
                    ws.send(raw, 2)
                    continue
                body = json.loads(raw)
                if body.get('type') == 'response.cancel':
                    ws.send(encoded({'type': 'response.failed', 'response': {'id': 'gpt-cancelled'}}))
                elif body.get('input') == 'hang':
                    self.server.hanging.set()
                elif body.get('input') == 'oversized-history':
                    ws.close(1009, 'message too big')
                    return
                else:
                    if body.get('input') == 'native-heartbeats':
                        for i in range(3):
                            ws.send_control(9, b'native-ping-' + str(i).encode())
                            time.sleep(.04)
                    if body.get('metadata',{}).get('fixture_delay'):
                        time.sleep(.12)
                    ws.send(encoded({'type': 'response.completed', 'response': {'id': 'gpt-fixture', 'output': []}}))
        except OSError:
            pass
        finally:
            self.server.cancelled.set()
            ws.close()

    do_HEAD = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_POST


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        self.upstream.daemon_threads = True
        self.upstream.seen, self.upstream.upgrades, self.upstream.ws_seen = [], [], []
        self.upstream.hanging, self.upstream.cancelled = threading.Event(), threading.Event()
        threading.Thread(target=self.upstream.serve_forever, kwargs={'poll_interval': .01}, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.upstream.server_port}'
        self.profile = {'format': 1, 'edition': 'ds', 'identity': 'fixture-relay', 'token': 't' * 40, 'port': 0,
                        'gpt': {'chatgpt_base_url': self.base + '/chatgpt', 'api_base_url': self.base + '/api'},
                        'routes': {'deepseek-fixture': {'base_url': self.base + '/ds',
                                  'credential': {'kind': 'file', 'path': '/synthetic/no-secret'},
                                  'tool_adapter': True, 'flatten_namespaces': True}}}
        self.server = relay.create_server(self.profile, credential_reader=lambda _: 'independent-ds-fixture-key')
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.path = '/' + self.profile['token'] + '/backend-api/codex/responses'
        self.wsurl = f'ws://127.0.0.1:{self.server.server_port}' + self.path

    def test_official_close_and_external_error_status_reach_native_client_promptly(self):
        ws, _ = connect_websocket(self.wsurl, timeout=2)
        closes = []
        ws.on_close = lambda code, reason: closes.append((code, reason))
        ws.send(encoded({'type':'response.create','model':'gpt-fixture','input':'oversized-history'}))
        self.assertIsNone(ws.recv())
        self.assertEqual(closes, [(1009, 'message too big')])
        ws.close()
        self.server.profile['routes']['deepseek-fixture']['base_url'] = self.base + '/fail'
        ws, _ = connect_websocket(self.wsurl, timeout=2)
        ws.send(encoded({'type':'response.create','model':'deepseek-fixture','input':[]}))
        value = json.loads(ws.recv()[1])
        self.assertEqual(value['type'], 'error')
        self.assertEqual(value['status'], 429)
        self.assertEqual(value['error']['code'], 'upstream_http_429')
        ws.close()

    def test_native_streamed_body_still_cancels_when_client_disconnects(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        original = relay._open
        def slow_connect(*args, **kwargs):
            time.sleep(.25)  # Upload is queued while the upstream handshake runs.
            return original(*args, **kwargs)
        with patch.object(relay, '_open', side_effect=slow_connect):
            connection.request('POST', self.path.removesuffix('responses') + 'native-tool-hang',
                               body=b'\x00opaque-body\xff' * 10000)
            self.assertTrue(self.upstream.hanging.wait(2))
            connection.close()
            self.assertTrue(self.upstream.cancelled.wait(2), 'Native request did not cancel after body upload')

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.upstream.shutdown()
        self.upstream.server_close()

    def request(self, body=None, *, path=None, headers=None, method='POST'):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        conn.request(method, path or self.path, body=encoded(body) if body is not None else None,
                     headers={'Content-Type': 'application/json', **(headers or {})})
        result = conn.getresponse()
        status, response_headers, raw = result.status, dict(result.getheaders()), result.read()
        conn.close()
        return status, response_headers, raw

    def connect(self, **headers):
        return connect_websocket(self.wsurl, headers, timeout=3)[0]

    def test_native_search_uses_official_origin_and_auth_with_external_model(self):
        for edition in ('ds', 'proxy'):
            for account in (None, 'fixture-account'):
                with self.subTest(edition=edition, account=account):
                    self.server.profile['edition'] = edition
                    body = {'model':'deepseek-fixture', 'commands':{'search_query':[{'q':'fixture'}]}}
                    headers = {'Authorization':'Bearer official-search-fixture'}
                    if account:
                        headers['ChatGPT-Account-ID'] = account
                    status, _, raw = self.request(body,
                        path=self.path.removesuffix('/responses') + '/alpha/search', headers=headers)
                    self.assertEqual(status, 200, raw)
                    path, seen, transmitted, _ = self.upstream.seen[-1]
                    self.assertEqual(path, ('/chatgpt' if account else '/api') + '/alpha/search')
                    self.assertEqual(seen['Authorization'], headers['Authorization'])
                    self.assertEqual(transmitted, encoded(body))
                    self.assertNotIn(b'independent-ds-fixture-key', raw)
        for suffix in ('/alpha/search/../responses', '/alpha/%2e%2e/responses', '//evil.invalid'):
            status, _, _ = self.request({'model':'deepseek-fixture'},
                path=self.path.removesuffix('/responses') + suffix)
            self.assertEqual(status, 404)

    def receive_terminal(self, ws):
        result = []
        while True:
            opcode, raw = ws.recv()
            self.assertEqual(opcode, 1)
            value = json.loads(raw)
            result.append(value)
            if value['type'] in relay.TERMINAL:
                return result

    def test_native_image_generate_and_edit_keep_official_auth_and_bytes(self):
        # Native image tools use a different model and schema from Responses.
        # They must never be routed to the selected DS/Grok/etc. provider.
        for edition in ('ds', 'proxy'):
            for account in (None, 'official-fixture-account'):
                for action in ('generations', 'edits'):
                    with self.subTest(edition=edition, account=account, action=action):
                        self.server.profile['edition'] = edition
                        body = {'model': 'gpt-image-2', 'prompt': 'Synthetic native image fixture',
                                'n': 1, 'background': 'transparent'}
                        if action == 'edits':
                            body['images'] = [{'image_url': 'data:image/png;base64,c3ludGhldGlj'}]
                        headers = {'Authorization': 'Bearer official-image-fixture',
                                   'x-codex-imagegen-request-id': 'fixture-image-request',
                                   'x-codex-image-turn-id': 'fixture-image-turn'}
                        if account:
                            headers['ChatGPT-Account-ID'] = account
                        status, _, raw = self.request(body,
                            path=self.path.removesuffix('/responses') + '/images/' + action,
                            headers=headers)
                        self.assertEqual(status, 200, raw)
                        path, seen, transmitted, _ = self.upstream.seen[-1]
                        self.assertEqual(path, ('/chatgpt' if account else '/api') + '/images/' + action)
                        self.assertEqual(transmitted, encoded(body))
                        for name, value in headers.items():
                            self.assertEqual(seen[name], value)
                        self.assertEqual(json.loads(raw)['data'][0]['generation_id'], 'fixture-generation')
        before = len(self.upstream.seen)
        for suffix in ('/images/generations/../responses', '/images/%252e%252e/responses',
                       '/images/%2e%2e/responses', '/images//generations'):
            self.assertEqual(self.request({'model': 'gpt-image-2'},
                path=self.path.removesuffix('/responses') + suffix)[0], 404)
        self.assertEqual(len(self.upstream.seen), before)

    def test_new_native_http_services_are_opaque_and_stay_on_official_origin(self):
        payload = gzip.compress(b'Native binary payload: not a Responses model request')
        for edition in ('ds', 'proxy'):
            self.server.profile['edition'] = edition
            for method in ('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'):
                with self.subTest(edition=edition, method=method):
                    conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
                    conn.request(method, self.path.removesuffix('/responses') + '/native-tool?name=a%2Bb&model=external',
                        body=payload if method not in ('GET', 'HEAD') else None,
                        headers={'Authorization': 'Bearer native-service-fixture',
                                 'ChatGPT-Account-ID': 'fixture-account', 'Content-Encoding': 'gzip',
                                 'Content-Type': 'application/octet-stream'})
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200 if method in ('GET', 'HEAD') else 422)
                    self.assertEqual(response.read(), b'' if method == 'HEAD' else b'\x00opaque-native-response\xff')
                    conn.close()
                    path, headers, transmitted, _ = self.upstream.seen[-1]
                    self.assertEqual(path, '/chatgpt/native-tool?name=a%2Bb&model=external')
                    self.assertEqual(headers['Authorization'], 'Bearer native-service-fixture')
                    self.assertEqual(headers['Content-Encoding'], 'gzip')
                    self.assertEqual(transmitted, b'' if method in ('GET', 'HEAD') else payload)

    def test_native_heartbeats_reach_client_during_silent_reasoning(self):
        for edition in ('ds', 'proxy'):
            self.server.profile['edition'] = edition
            for early in (False, True):
                with self.subTest(edition=edition, early=early):
                    headers = {'x-codex-turn-metadata': '{"model":"gpt-fixture"}'} if early else {}
                    ws = self.connect(**headers)
                    seen = []
                    ws.on_control = lambda opcode, payload: seen.append((opcode, payload))
                    try:
                        ws.send(encoded({'type': 'response.create', 'model': 'gpt-fixture',
                                         'input': 'native-heartbeats'}))
                        self.assertEqual(self.receive_terminal(ws)[-1]['type'], 'response.completed')
                        self.assertEqual(seen, [(9, b'native-ping-' + str(i).encode()) for i in range(3)])
                    finally:
                        ws.close()

    def test_native_silent_http_wait_and_standalone_compaction_keep_original_auth(self):
        factory=relay.create_http_connection
        def short_connect(*args,**kwargs):
            kwargs['timeout']=.03  # A connect deadline must not become an inference deadline.
            return factory(*args,**kwargs)
        for edition in ('ds','proxy'):
            self.server.profile['edition']=edition
            for suffix in ('','/compact'):
                with self.subTest(edition=edition,suffix=suffix),patch.object(relay,'create_http_connection',side_effect=short_connect):
                    body={'model':'gpt-fixture','input':[{'type':'compaction_trigger'}],
                          'metadata':{'fixture_delay':True}}
                    status,_,raw=self.request(body,path=self.path+suffix,
                        headers={'Authorization':'Bearer native-fixture','ChatGPT-Account-ID':'account-fixture'})
                    self.assertEqual(status,200,raw)
                    path,headers,transmitted,_=self.upstream.seen[-1]
                    self.assertEqual(path,'/chatgpt/responses'+suffix)
                    self.assertEqual(transmitted,encoded(body))
                    self.assertEqual(headers['Authorization'],'Bearer native-fixture')

    def test_external_slow_reasoning_uses_native_deadline_and_separate_auth(self):
        factory=relay.create_http_connection
        def short_connect(*args,**kwargs):
            kwargs['timeout']=.03
            return factory(*args,**kwargs)
        for edition in ('ds','proxy'):
            self.server.profile['edition']=edition
            with self.subTest(edition=edition),patch.object(relay,'create_http_connection',side_effect=short_connect):
                status,_,raw=self.request({'model':'deepseek-fixture','input':[],
                    'metadata':{'fixture_delay':True}},headers={'Authorization':'Bearer native-fixture'})
                self.assertEqual(status,200,raw)
                self.assertEqual(self.upstream.seen[-1][1]['Authorization'],'Bearer independent-ds-fixture-key')

    def test_native_silent_websocket_wait_is_owned_by_client_for_early_and_late_connect(self):
        factory=relay.connect_websocket
        sockets=[]
        def short_connect(*args,**kwargs):
            value=factory(*args,**kwargs);value[0].sock.settimeout(.03);sockets.append(value[0].sock)
            return value
        for edition in ('ds','proxy'):
            self.server.profile['edition']=edition
            for hint in ({},{'x-codex-turn-metadata':json.dumps({'model':'gpt-fixture'})}):
                with self.subTest(edition=edition,early=bool(hint)),patch.object(relay,'connect_websocket',side_effect=short_connect):
                    ws=self.connect(**hint)
                    try:
                        ws.send(encoded({'type':'response.create','model':'gpt-fixture','input':[],
                                         'metadata':{'fixture_delay':True}}))
                        self.assertEqual(self.receive_terminal(ws)[-1]['type'],'response.completed')
                        self.assertIsNone(sockets[-1].gettimeout())
                    finally:ws.close()

    def test_native_silent_http_cancel_closes_upstream(self):
        self.server.profile['gpt']['api_base_url']=self.base+'/hang'
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        conn.request('POST',self.path,body=encoded({'model':'gpt-fixture','stream':True,'input':[]}))
        response=conn.getresponse()
        self.assertTrue(self.upstream.hanging.wait(2))
        response.close();conn.close()
        self.assertTrue(self.upstream.cancelled.wait(2),'Native cancellation left a silent upstream open')

    def test_idle_websocket_and_authenticated_drain_release(self):
        ws,_=connect_websocket(self.wsurl,timeout=2)
        try:
            # An idle long-lived transport is not an active generation.
            self.assertEqual(self.server.activity.status()['requests'],0)
            self.server.activity.last-=11
            path='/'+self.profile['token']+'/maintenance'
            status,_,raw=self.request({'action':'drain','lease':'a'*32},path=path)
            self.assertEqual(status,200);self.assertTrue(json.loads(raw)['acquired'])
            status,_,raw=self.request({'model':'deepseek-fixture','input':[]})
            self.assertEqual(status,503);self.assertEqual(len(self.upstream.seen),0)
            self.assertEqual(self.request({'action':'release','lease':'a'*32},path='/wrong/maintenance')[0],401)
            self.assertTrue(self.server.activity.status()['draining'])
            self.assertEqual(self.request({'action':'release','lease':'a'*32},path=path)[0],200)
            self.assertFalse(self.server.activity.status()['draining'])
            status,_,raw=self.request({'model':'deepseek-fixture','input':[]})
            self.assertEqual(status,200)
        finally:ws.close()

    def test_private_request_diagnostics(self):
        path='/'+self.profile['token']+'/diagnostics'
        for edition in ('ds','proxy'):
            self.server.profile['edition']=edition
            status,_,_=self.request({'model':'deepseek-fixture','input':'private-prompt-not-logged','stream':True})
            self.assertEqual(status,200)
            status,_,_=self.request({'prompt':'private-image-prompt'},path=self.path.removesuffix('responses')+'native-tool')
            self.assertEqual(status,422)
            status,_,raw=self.request(path=path,method='GET');self.assertEqual(status,200)
            value=json.loads(raw);self.assertEqual(value['edition'],edition);self.assertTrue(value['requests'])
            self.assertEqual(value['requests'][0]['status'],422);self.assertEqual(value['requests'][0]['error_source'],'upstream')
            self.assertNotIn('private-image-prompt',raw.decode())
            self.assertNotIn('private-prompt',raw.decode());self.assertNotIn('independent-ds-fixture-key',raw.decode())
            status,_,_=self.request(path='/wrong/diagnostics',method='GET');self.assertEqual(status,401)

    def test_health_path_token_host_origin_and_encoding_boundaries(self):
        status, _, raw = self.request(path='/' + self.profile['token'] + '/health', method='GET')
        self.assertEqual(status, 200)
        value=json.loads(raw)
        self.assertEqual({key:value[key] for key in ('ok','identity','edition')}, {'ok': True, 'identity': 'fixture-relay', 'edition': 'ds'})
        self.assertEqual(value['update_control'],1)
        self.assertEqual(value['activity']['requests'],0)
        for path, headers, expected in [('/wrong/health', {}, 401),
            (self.path, {'Host': 'evil.invalid'}, 403),
            (self.path, {'Origin': 'https://evil.invalid'}, 403),
            (self.path, {'Content-Encoding': 'br'}, 415),
            (self.path + '/compact', {}, 501)]:
            with self.subTest(path=path, headers=headers):
                actual, _, _ = self.request({'model': 'deepseek-fixture'}, path=path, headers=headers)
                self.assertEqual(actual, expected)
        self.assertFalse(self.upstream.seen)

    def test_rejected_headers_send_complete_reply_and_drain_body_without_forwarding(self):
        drains = []
        drained = threading.Event()
        original = relay.Handler.drain_rejected_body
        def observed(handler):
            original(handler)
            drains.append(handler.rfile.peek(1) == b'')
            drained.set()
        # Send headers separately: the client can read the error before sending
        # its remaining body, and the server must consume it before closing.
        for header, expected in (("Host: evil.invalid",403),("Content-Encoding: br",415)):
            with self.subTest(header=header):
                drained.clear()
                wire = socket.create_connection(('127.0.0.1',self.server.server_port),timeout=2)
                body = encoded({'model':'deepseek-fixture','input':'x'*65536})
                host = '' if header.startswith('Host:') else f'Host: 127.0.0.1:{self.server.server_port}\r\n'
                request = (f'POST {self.path} HTTP/1.1\r\n' + host + header +
                           f'\r\nContent-Length: {len(body)}\r\n\r\n').encode()
                with patch.object(relay.Handler,'drain_rejected_body',observed):
                    wire.sendall(request)
                    response = http.client.HTTPResponse(wire)
                    response.begin()
                    self.assertEqual(response.status,expected)
                    self.assertIn('error',json.loads(response.read()))
                    wire.sendall(body)
                    wire.shutdown(socket.SHUT_WR)
                    self.assertEqual(wire.recv(1),b'')
                    wire.close()
                    self.assertTrue(drained.wait(1))
                self.assertTrue(drains[-1])
        self.assertFalse(self.upstream.seen)

    def test_native_large_image_requests_and_compaction_keep_bytes_and_auth(self):
        # A legitimate image history can exceed 64 MB with few text tokens.
        raw = encoded({'model':'gpt-fixture','input':[{'role':'user','content':[
            {'type':'input_image','image_url':'data:image/png;base64,' + 'A' * 64_000_001}]}]})
        for edition in ('ds', 'proxy'):
            self.server.profile['edition'] = edition
            for suffix in ('', '/compact'):
                with self.subTest(edition=edition, suffix=suffix):
                    conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=30)
                    conn.request('POST', self.path + suffix, body=raw,
                        headers={'Authorization':'Bearer native-fixture','ChatGPT-Account-ID':'fixture-account'})
                    response = conn.getresponse()
                    self.assertEqual(response.status,200,response.read()[:200])
                    response.read();conn.close()
                    path, headers, received, _ = self.upstream.seen[-1]
                    self.assertEqual(path,'/chatgpt/responses'+suffix)
                    self.assertEqual(received,raw)
                    self.assertEqual(headers['Authorization'],'Bearer native-fixture')
                    self.upstream.seen.clear()

    def test_upstream_payload_rejection_is_returned_without_local_retries(self):
        original = self.upstream.RequestHandlerClass.do_POST
        def reject(handler):
            handler.rfile.read(int(handler.headers['Content-Length']))
            handler.server.seen.append('rejected')
            body=b'{"error":{"code":"provider_limit","message":"Provider payload limit"}}'
            handler.send_response(413)
            handler.send_header('Content-Length',str(len(body)))
            handler.end_headers();handler.wfile.write(body)
        with patch.object(self.upstream.RequestHandlerClass,'do_POST',reject):
            status,_,raw=self.request({'model':'gpt-fixture','input':[]})
        self.assertEqual(status,413)
        self.assertEqual(json.loads(raw)['error']['code'],'provider_limit')
        self.assertEqual(self.upstream.seen,['rejected'])

    def test_explicit_catalog_rejects_unknown_http_ws_and_hint_without_any_upstream(self):
        # Names are arbitrary: membership, rather than vendor-looking prefixes,
        # decides whether an unregistered model can reach the official service.
        self.server.profile['official_models'] = ['official-native-id']
        relay.validate_profile(self.server.profile)
        for model in ('removed-external','gpt-looking-but-unregistered'):
            with self.subTest(model=model):
                status, _, raw = self.request({'model':model},headers={'Authorization':'Bearer official-fixture'})
                self.assertEqual(status,400)
                error = json.loads(raw)['error']
                self.assertEqual(error['code'],'model_unavailable')
                self.assertIn('Select an available model',error['message'])
                ws = self.connect(**{'x-codex-routing-hint':'model=' + model})
                try:
                    self.assertFalse(self.upstream.upgrades)
                    ws.send(encoded({'type':'response.create','model':model,'input':[]}))
                    self.assertEqual(self.receive_terminal(ws)[-1]['error']['code'],'model_unavailable')
                    ws.send(encoded({'type':'response.create','model':model,'input':[],'generate':False}))
                    self.assertEqual(self.receive_terminal(ws)[-1]['error']['code'],'model_unavailable')
                finally:
                    ws.close()
        self.assertFalse(self.upstream.seen)
        self.assertFalse(self.upstream.upgrades)
        self.assertFalse(self.upstream.ws_seen)
        # Both explicitly registered classes still use their existing routes.
        self.assertEqual(self.request({'model':'official-native-id'})[0],200)
        self.assertEqual(self.upstream.seen[-1][0],'/api/responses')
        self.assertEqual(self.request({'model':'deepseek-fixture'})[0],200)
        self.assertEqual(self.upstream.seen[-1][0],'/ds/responses')
        ws = self.connect(**{'x-codex-routing-hint':'model=official-native-id'})
        try:
            ws.send(encoded({'type':'response.create','model':'official-native-id','input':[]}))
            self.assertEqual(self.receive_terminal(ws)[-1]['type'],'response.completed')
            ws.send(encoded({'type':'response.create','model':'deepseek-fixture','input':[]}))
            self.assertEqual(self.receive_terminal(ws)[-1]['type'],'response.completed')
        finally:
            ws.close()

    def test_official_http_body_headers_and_auth_reach_only_official_route(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        raw = b'{ "model" : "gpt-fixture", "input":"literal whitespace" }'
        headers = {'Authorization': 'Bearer original-official-fixture', 'ChatGPT-Account-ID': 'account-fixture',
                   'X-OpenAI-Fixture': 'native', 'Proxy-Authorization': 'never-forward'}
        conn.request('POST', self.path, body=raw, headers=headers)
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader('X-Official-Fixture'), 'preserved')
        response.read()
        conn.close()
        path, actual, forwarded, _ = self.upstream.seen[-1]
        self.assertEqual(path, '/chatgpt/responses')
        self.assertEqual(forwarded, raw)
        self.assertEqual(actual['Authorization'], headers['Authorization'])
        self.assertEqual(actual['X-OpenAI-Fixture'], 'native')
        self.assertNotIn('Proxy-Authorization', actual)
        self.request({'model': 'gpt-fixture'}, headers={'Authorization': 'Bearer api-fixture'})
        self.assertEqual(self.upstream.seen[-1][0], '/api/responses')

    def test_registered_route_has_independent_credential_and_tool_conversion(self):
        body = {'model': 'deepseek-fixture', 'input': [], 'tools': [{'type': 'custom', 'name': 'apply_patch'}]}
        status, _, raw = self.request(body, headers={'Authorization': 'Bearer official-secret',
            'ChatGPT-Account-ID': 'official-account', 'X-OpenAI-Fixture': 'secret-context'})
        self.assertEqual(status, 200)
        output = json.loads(raw)['output'][0]
        self.assertEqual((output['type'], output['input'], output['call_id']),
                         ('custom_tool_call', 'exact tool input', 'call_fixture'))
        path, headers, _, actual = self.upstream.seen[-1]
        self.assertEqual(path, '/ds/responses')
        self.assertEqual(headers['Authorization'], 'Bearer independent-ds-fixture-key')
        self.assertNotIn('ChatGPT-Account-ID', headers)
        self.assertFalse(any(k.lower().startswith('x-openai') for k in headers))
        self.assertEqual(actual['tools'][0]['type'], 'function')

    def test_anonymous_local_routes_omit_auth_in_http_and_websocket_translation(self):
        for reference in (None, {'kind': 'none'}):
            route = self.server.profile['routes']['deepseek-fixture']
            if reference is None:
                route.pop('credential', None)
            else:
                route['credential'] = reference
            relay.validate_profile(self.server.profile)
            self.request({'model': 'deepseek-fixture'}, headers={'Authorization': 'Bearer official-must-not-leak'})
            self.assertNotIn('Authorization', self.upstream.seen[-1][1])
            ws = self.connect(Authorization='Bearer official-must-not-leak')
            ws.send(encoded({'type': 'response.create', 'model': 'deepseek-fixture', 'input': []}))
            self.receive_terminal(ws)
            self.assertNotIn('Authorization', self.upstream.seen[-1][1])
            ws.close()

    def test_normalize_chat_only_and_remote_compaction(self):
        route = self.server.profile['routes']['deepseek-fixture']
        route.update(normalize_instructions=True, chat_only=True)
        self.request({'model': 'deepseek-fixture', 'instructions': 'initial',
                      'input': [{'role': 'developer', 'content': [{'text': 'developer text'}]},
                                {'role': 'user', 'content': 'hello'}],
                      'tools': [{'type': 'function', 'name': 'unused'}], 'tool_choice': 'auto'})
        actual = self.upstream.seen[-1][3]
        self.assertEqual(actual['instructions'], 'initial\n\ndeveloper text')
        self.assertEqual(actual['input'], [{'role': 'user', 'content': 'hello'}])
        self.assertNotIn('tools', actual)
        count = len(self.upstream.seen)
        status, _, raw = self.request({'model': 'deepseek-fixture', 'input': [{'type': 'compaction_trigger'}]})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(raw)['error']['code'], 'empty_compaction')
        self.assertEqual(len(self.upstream.seen), count)

    def test_native_external_compaction_http_and_ws_and_return_to_official(self):
        route = self.server.profile['routes']['deepseek-fixture']
        route['context_window'] = 32768
        compact = {'model':'deepseek-fixture', 'input':[
            {'role':'user', 'content':'Preserve the current task and pending work.'},
            {'type':'compaction_trigger'}]}
        for edition in ('ds', 'proxy'):
            self.server.profile['edition'] = edition
            status, _, raw = self.request(compact, headers={'Authorization':'Bearer official-fixture'})
            self.assertEqual(status, 200, raw)
            item = json.loads(raw)['output'][0]
            self.assertEqual(item['type'], 'compaction')
            self.assertTrue(item['encrypted_content'].startswith(relay.handoff_item.__globals__['HANDOFF_PREFIX']))
            path, headers, _, body = self.upstream.seen[-1]
            self.assertEqual(path, '/ds/responses')
            self.assertEqual(headers['Authorization'], 'Bearer independent-ds-fixture-key')
            self.assertNotIn('compaction_trigger', json.dumps(body))
            self.assertEqual(body['tool_choice'], 'none')
            status, _, _ = self.request({'model':'gpt-fixture', 'input':[item]},
                headers={'Authorization':'Bearer official-fixture'})
            self.assertEqual(status, 200)
            self.assertNotIn('encrypted_content', json.dumps(self.upstream.seen[-1][3]))
            self.assertEqual(self.upstream.seen[-1][3]['input'][0]['role'], 'assistant')
            ws = self.connect(Authorization='Bearer official-fixture')
            try:
                ws.send(encoded({**compact, 'type':'response.create', 'stream':True}))
                events = self.receive_terminal(ws)
                response = events[-1]['response']
                self.assertEqual(response['output'][0]['type'], 'compaction')
                self.assertLess(response['usage']['total_tokens'], 4096)
                ws.send(encoded({'type':'response.create','model':'deepseek-fixture','input':response['output']}))
                self.receive_terminal(ws)
                self.assertNotIn('encrypted_content', json.dumps(self.upstream.seen[-1][3]))
            finally:
                ws.close()

    def test_handoff_invalid_data_and_official_compaction_are_not_reinterpreted(self):
        item = relay.handoff_item('Retain exact file paths and outstanding steps.')
        item['encrypted_content'] += '!'
        before = len(self.upstream.seen)
        status, _, raw = self.request({'model':'gpt-fixture','input':[item]})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(raw)['error']['code'], 'invalid_adapter_handoff')
        self.assertEqual(len(self.upstream.seen), before)
        official = {'model':'gpt-fixture','input':[{'type':'compaction','encrypted_content':'official-opaque'}]}
        self.request(official)
        self.assertEqual(self.upstream.seen[-1][3], official)

    def test_ws_warmup_and_incremental_native_history(self):
        ws = self.connect(Authorization='Bearer official-fixture')
        first = {'model': 'deepseek-fixture', 'type': 'response.create', 'input': [{'role': 'user', 'content': 'hello'}]}
        ws.send(encoded({**first, 'generate': False}))
        warmup = self.receive_terminal(ws)[-1]['response']
        self.assertEqual(self.upstream.seen, [])
        ws.send(encoded({**first, 'previous_response_id': warmup['id'], 'input': []}))
        complete = self.receive_terminal(ws)[-1]['response']
        self.assertEqual(self.upstream.seen[-1][3]['input'], first['input'])
        delta = [{'role': 'user', 'content': 'continue'}]
        ws.send(encoded({**first, 'previous_response_id': complete['id'], 'input': delta}))
        self.receive_terminal(ws)
        self.assertEqual(self.upstream.seen[-1][3]['input'], first['input'] + __import__('reasoning_origin').resolve_request({'input':complete['output']},first['model'])[0]['input'] + delta)
        ws.send(encoded({**first, 'previous_response_id': 'unknown', 'input': delta}))
        self.assertEqual(self.receive_terminal(ws)[-1]['error']['code'], 'previous_response_not_found')
        ws.close()

    def test_ws_model_change_rejects_delta_and_accepts_full_input(self):
        self.server.profile['routes']['another-model'] = copy.deepcopy(self.profile['routes']['deepseek-fixture'])
        ws = self.connect()
        ws.send(encoded({'type': 'response.create', 'model': 'deepseek-fixture', 'input': []}))
        complete = self.receive_terminal(ws)[-1]['response']
        ws.send(encoded({'type': 'response.create', 'model': 'another-model', 'previous_response_id': complete['id'], 'input': []}))
        self.assertEqual(self.receive_terminal(ws)[-1]['error']['code'], 'previous_response_not_found')
        ws.send(encoded({'type': 'response.create', 'model': 'another-model', 'input': []}))
        self.assertEqual(self.receive_terminal(ws)[-1]['type'], 'response.completed')
        ws.close()

    def test_ws_custom_tool_roundtrip_preserves_native_output(self):
        ws = self.connect()
        body = {'type': 'response.create', 'model': 'deepseek-fixture', 'input': [],
                'tools': [{'type': 'custom', 'name': 'apply_patch'}]}
        ws.send(encoded(body))
        events = self.receive_terminal(ws)
        output = events[-1]['response']['output'][0]
        self.assertEqual(output['type'], 'custom_tool_call')
        self.assertEqual(output['input'], 'exact tool input')
        self.assertEqual(output['call_id'], 'call_fixture')
        ws.send(encoded({**body, 'previous_response_id': events[-1]['response']['id'],
                         'input': [{'type': 'custom_tool_call_output', 'call_id': 'call_fixture', 'output': 'done'}]}))
        self.receive_terminal(ws)
        self.assertEqual(self.upstream.seen[-1][3]['input'][0]['type'], 'function_call')
        self.assertEqual(self.upstream.seen[-1][3]['input'][1]['type'], 'function_call_output')
        ws.close()

    def test_ws_official_connection_is_reused_binary_and_cancel_are_transparent(self):
        ws = self.connect(Authorization='Bearer original-official-fixture', **{'ChatGPT-Account-ID': 'account-fixture'})
        for _ in range(2):
            ws.send(encoded({'type': 'response.create', 'model': 'gpt-fixture', 'input': []}))
            self.assertEqual(self.receive_terminal(ws)[-1]['type'], 'response.completed')
        self.assertEqual(len(self.upstream.upgrades), 1)
        self.assertEqual(self.upstream.upgrades[0][0], '/chatgpt/responses')
        self.assertEqual(self.upstream.upgrades[0][1]['Authorization'], 'Bearer original-official-fixture')
        ws.send(b'\x00\xffopaque', opcode=2)
        self.assertEqual(ws.recv(), (2, b'\x00\xffopaque'))
        ws.send(encoded({'type': 'response.create', 'model': 'gpt-fixture', 'input': 'hang'}))
        self.assertTrue(self.upstream.hanging.wait(1))
        ws.send(encoded({'type': 'response.cancel'}))
        self.assertEqual(self.receive_terminal(ws)[-1]['type'], 'response.failed')
        ws.close()
        self.assertTrue(self.upstream.cancelled.wait(1))

    def test_initial_gpt_hints_preserve_upstream_handshake_sticky_headers(self):
        for hint in ({'x-codex-routing-hint': 'model=gpt-fixture;tier=priority'},
                     {'x-codex-turn-metadata': json.dumps({'model': 'gpt-fixture'})}):
            ws, headers = connect_websocket(self.wsurl, hint, timeout=3)
            self.assertIn(('X-Codex-Turn-State', 'fixture-sticky'), headers)
            self.assertIn(('X-Models-Etag', 'fixture-models-etag'), headers)
            ws.send(encoded({'type': 'response.create', 'model': 'gpt-fixture', 'input': []}))
            self.receive_terminal(ws)
            # Frame routing still selects the registered route after this 101.
            ws.send(encoded({'type': 'response.create', 'model': 'deepseek-fixture', 'input': []}))
            self.receive_terminal(ws)
            self.assertEqual(self.upstream.seen[-1][0], '/ds/responses')
            ws.close()

    def test_ws_close_and_cancel_close_owned_http_connection_promptly(self):
        self.server.profile['routes']['deepseek-fixture']['base_url'] = self.base + '/hang'
        for cancel in (False, True):
            self.upstream.hanging.clear()
            self.upstream.cancelled.clear()
            ws = self.connect()
            ws.send(encoded({'type': 'response.create', 'model': 'deepseek-fixture', 'input': []}))
            self.assertTrue(self.upstream.hanging.wait(1))
            if cancel:
                ws.send(encoded({'type': 'response.cancel'}))
                self.assertEqual(self.receive_terminal(ws)[-1]['error']['code'], 'request_cancelled')
            else:
                ws.close()
            self.assertTrue(self.upstream.cancelled.wait(1), 'upstream socket remained open after cancellation')
            ws.close()

    def test_redirects_and_upstream_error_codes(self):
        route = self.server.profile['routes']['deepseek-fixture']
        route['base_url'] = self.base + '/redirect'
        status, _, raw = self.request({'model': 'deepseek-fixture'})
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(raw)['error']['code'], 'upstream_redirect')
        route['base_url'] = self.base + '/fail'
        ws = self.connect()
        ws.send(encoded({'type': 'response.create', 'model': 'deepseek-fixture'}))
        self.assertEqual(self.receive_terminal(ws)[-1]['error']['code'], 'upstream_http_429')
        ws.close()

    def test_credential_failure_is_safe_and_never_falls_back_to_official_auth(self):
        def unavailable(_):
            raise relay.RelayError(503, 'credential_unavailable', 'Credential unavailable')
        self.server.credential_reader = unavailable
        status, _, raw = self.request({'model': 'deepseek-fixture'}, headers={'Authorization': 'Bearer official-secret'})
        self.assertEqual(status, 503)
        self.assertNotIn(b'official-secret', raw)
        self.assertEqual(self.upstream.seen, [])


class ProfileTests(unittest.TestCase):
    def test_official_model_registry_requires_exact_unique_string_ids(self):
        profile = {'format':1,'edition':'ds','identity':'fixture','token':'t'*40,'port':0,
                   'routes':{},'gpt':{'chatgpt_base_url':'https://example.invalid/codex',
                                     'api_base_url':'https://example.invalid/v1'}}
        for value in ('gpt-model',[None],['model','model'],['']):
            with self.subTest(value=value), self.assertRaises(ValueError):
                relay.validate_profile({**profile,'official_models':value})
        relay.validate_profile({**profile,'official_models':[]})
        relay.validate_profile({**profile,'official_models':['arbitrary-native-id']})

    def test_disconnect_fixture_accepts_only_eof_reset_or_abort_not_timeout(self):
        from unittest.mock import Mock
        for result in (b'',ConnectionResetError(10054,'fixture reset'),
                       ConnectionAbortedError(10053,'fixture abort'),
                       TimeoutError('still open'),OSError('unrelated socket failure')):
            with self.subTest(result=type(result).__name__):
                connection = Mock()
                if isinstance(result,Exception):
                    connection.recv.side_effect = result
                else:
                    connection.recv.return_value = result
                disconnected = threading.Event()
                observe_disconnect(connection,disconnected)
                self.assertEqual(disconnected.is_set(), isinstance(result,(bytes,ConnectionResetError,ConnectionAbortedError)))

    def test_existing_proxy_discovery_explicit_override_and_loopback_bypass(self):
        with patch('urllib.request.getproxies', return_value={'https': 'http://system-proxy.invalid:7890'}), \
             patch('urllib.request.proxy_bypass', return_value=False):
            self.assertEqual(relay.network_proxy('https://api.openai.com/v1', {}), 'http://system-proxy.invalid:7890')
            self.assertEqual(relay.network_proxy('wss://api.openai.com/v1', {'network_proxy': 'http://explicit.invalid:80'}),
                             'http://explicit.invalid:80')
            self.assertIsNone(relay.network_proxy('http://127.0.0.1:1234/v1', {'network_proxy': 'http://never.invalid:80'}))
            self.assertIsNone(relay.network_proxy('http://192.168.1.50:11434/v1', {'network_proxy': ''}))
        with patch('urllib.request.proxy_bypass', return_value=True):
            self.assertIsNone(relay.network_proxy('https://api.openai.com/v1', {}))

    def test_remote_http_and_short_token_are_rejected(self):
        profile = {'format': 1, 'edition': 'proxy', 'identity': 'fixture', 'token': 'x' * 32, 'port': 0,
                   'gpt': {'chatgpt_base_url': 'https://chatgpt.com/backend-api/codex',
                           'api_base_url': 'https://api.openai.com/v1'}, 'routes': {}}
        self.assertEqual(relay.validate_profile(copy.deepcopy(profile)), profile)
        for change in ({'token': 'short'}, {'gpt': {**profile['gpt'], 'api_base_url': 'http://public.invalid/v1'}}):
            with self.assertRaises(ValueError):
                relay.validate_profile({**profile, **change})
        local = {**profile, 'routes': {'local-fixture': {'base_url': 'http://192.168.1.50:11434/v1'}}}
        self.assertEqual(relay.validate_profile(local), local)

    def test_credential_reader_file_and_proxy_errors_are_sanitized(self):
        with patch('inference_adapter.Path.read_text', return_value='fixture-file-key\n'):
            self.assertEqual(relay.read_credential({'kind': 'file', 'path': '/fixture'}), 'fixture-file-key')
        with patch('inference_adapter.Path.read_text', side_effect=ValueError('SECRET NEVER IN ERROR')):
            with self.assertRaises(relay.RelayError) as ctx:
                relay.read_credential({'kind': 'file', 'path': '/fixture'})
            self.assertNotIn('SECRET', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()

class UpdateActivityTests(unittest.TestCase):
    def test_tools_hold_idle_until_outputs_arrive_and_lease_is_atomic(self):
        now=[0.0];a=relay.Activity(lambda:now[0]);owner=object()
        a.begin(owner)
        a.observe(owner,{'type':'response.output_item.done','item':{'type':'function_call','call_id':'call-1'}})
        a.end(owner);now[0]=20
        self.assertEqual(a.status()['requests'],0);self.assertEqual(a.status()['tools'],1)
        self.assertFalse(a.drain('a'*32))
        a.begin(object());self.assertFalse(a.drain('a'*32))
        a.active.clear();a.consume({'input':[{'type':'function_call_output','call_id':'call-1','output':'private'}]})
        now[0]=31;self.assertTrue(a.drain('a'*32))
        with self.assertRaises(relay.RelayError):a.begin(object())
        self.assertFalse(a.drain('b'*32));a.drain('b'*32,True)
        self.assertTrue(a.status()['draining']);a.drain('a'*32,True)
        a.begin(owner);a.end(owner)
        now[0]=42;self.assertTrue(a.drain('a'*32));now[0]=163
        a.begin(owner);self.assertFalse(a.status()['draining'])
        self.assertNotIn('private',json.dumps(a.status()))

    def test_observation_never_limits_stream_and_unknown_holds_install(self):
        a=relay.Activity(lambda:100);owner=object();observer=relay.ActivityStream(a,owner)
        observer.feed(b'data: '+b'x'*300000+b'\n\n')
        self.assertEqual(a.status()['unknown'],1)
        self.assertFalse(a.drain('a'*32))
        raw=encoded({'type':'response.completed','response':{'output':[{'type':'custom_tool_call','call_id':'x'}]}})
        for chunk in (b'data: '+raw[:20],raw[20:]+b'\n\n'):observer.feed(chunk)
        self.assertEqual(a.status()['unknown'],0);self.assertEqual(a.status()['tools'],1)
        a.cancel(owner);self.assertEqual(a.status()['tools'],0)

    def test_metadata_tools_do_not_hold_server_side_image_requests(self):
        a=relay.Activity();a.observe('r',{'output':[{'type':'image_generation_call','id':'image'},
            {'type':'web_search_call','id':'search'}]})
        self.assertEqual(a.status()['tools'],0)


class TraceTests(unittest.TestCase):
    def test_memory_bound_expiry_terminal_states_and_no_payload_fields(self):
        now=[0.0];trace=relay.RequestTrace(lambda:now[0])
        for i in range(80):trace.begin('model-'+str(i),True,'http')
        self.assertEqual(len(trace.recent()),64)
        i=trace.begin('selected-model',True,'websocket')
        now[0]=.2;trace.update(i,'streaming');now[0]=.5;trace.update(i,'failed',429)
        trace.update(i,'completed')
        row=trace.recent()[0];self.assertEqual(row['phase'],'failed');self.assertEqual(row['error_source'],'upstream')
        self.assertEqual(row['error_kind'],'rate_limit');self.assertEqual(row['first_byte_ms'],200);self.assertEqual(row['elapsed_ms'],500)
        self.assertFalse({'body','prompt','headers','url','error_message','input','output'} & set(row))
        now[0]=1801;self.assertFalse(any(v['id']==i for v in trace.recent()))
