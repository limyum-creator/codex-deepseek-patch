"""Local HTTP/WS protocol tests for capability-selected Chat Completions peers."""
import http.client
import http.server
import json
import threading
import unittest

import inference_adapter as relay
from inference_transport import connect_websocket
from reasoning_origin import resolve_request


def encoded(value):
    return json.dumps(value, ensure_ascii=False).encode()


class ChatUpstream(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.seen.append((self.path, dict(self.headers), body))
        message = {'role':'assistant','content':'Answer.','reasoning_content':'Public task facts.'}
        finish = 'stop'
        if body.get('tools') and not any(m['role'] == 'tool' for m in body['messages']):
            message = {'role':'assistant','tool_calls':[{'id':'call_native','type':'function',
                'function':{'name':body['tools'][0]['function']['name'],
                            'arguments':'{"input":"exact native patch"}'}}]}
            finish = 'tool_calls'
        if not body.get('stream'):
            raw = encoded({'model':body['model'],'choices':[{'index':0,'message':message,'finish_reason':finish}]})
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_response(200)
        self.send_header('Content-Type','text/event-stream')
        self.send_header('Connection','close')
        self.end_headers()
        self.close_connection = True
        if 'tool_calls' in message:
            delta = {**message,'tool_calls':[{**message['tool_calls'][0],'index':0}]}
        else:
            delta = message
        records = [{'id':'chat_fixture','model':body['model'],
                    'choices':[{'index':0,'delta':delta,'finish_reason':None}]}]
        if self.server.mode == 'error':
            records = [{'error':{'code':'synthetic_quota','message':'Fixture quota'}}]
        elif self.server.mode != 'truncated':
            records.append({'choices':[{'index':0,'delta':{},'finish_reason':finish}]})
        for record in records:
            self.wfile.write(b'data: ' + encoded(record) + b'\n\n')
            self.wfile.flush()
        if self.server.mode == 'normal':
            self.wfile.write(b'data: [DONE]\n\n')


class ChatRelayTests(unittest.TestCase):
    def setUp(self):
        self.upstream = http.server.ThreadingHTTPServer(('127.0.0.1',0),ChatUpstream)
        self.upstream.daemon_threads = True
        self.upstream.seen, self.upstream.mode = [], 'normal'
        threading.Thread(target=self.upstream.serve_forever, kwargs={'poll_interval':.01}, daemon=True).start()
        base = f'http://127.0.0.1:{self.upstream.server_port}/v1'
        profile = {'format':1,'edition':'proxy','identity':'chat-fixture','token':'t'*40,'port':0,
                   'gpt':{'chatgpt_base_url':base,'api_base_url':base},
                   'routes':{'any-local-model':{'base_url':base,'wire_api':'chat_completions',
                     'tool_adapter':False,'credential':{'kind':'file','path':'/synthetic/key'}}}}
        self.server = relay.create_server(profile, credential_reader=lambda _: 'independent-peer-key')
        threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval':.01}, daemon=True).start()
        self.path = '/' + profile['token'] + '/backend-api/codex/responses'
        self.wsurl = f'ws://127.0.0.1:{self.server.server_port}' + self.path

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()

    def request(self, body):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        conn.request('POST',self.path,encoded(body),headers={'Content-Type':'application/json',
            'Authorization':'Bearer official-must-not-leak','ChatGPT-Account-ID':'original-account',
            'X-OpenAI-Fixture':'private'})
        response = conn.getresponse()
        status, raw = response.status, response.read()
        conn.close()
        return status, raw

    def terminal(self, ws):
        events = []
        while True:
            opcode, raw = ws.recv()
            self.assertEqual(opcode,1)
            events.append(json.loads(raw))
            if events[-1]['type'] in relay.TERMINAL:
                return events

    def declarations(self):
        return {'type':'additional_tools','tools':[{'type':'namespace','name':'files','tools':[
            {'type':'custom','name':'patch'}]}]}

    def test_http_json_native_lazy_custom_tools_and_auth_isolation(self):
        status, raw = self.request({'model':'any-local-model','input':[self.declarations(),
            {'role':'user','content':'Make patch'}]})
        self.assertEqual(status,200)
        call = json.loads(raw)['output'][0]
        self.assertEqual((call['type'],call['namespace'],call['input'],call['call_id']),
                         ('custom_tool_call','files','exact native patch','call_native'))
        path, headers, body = self.upstream.seen[-1]
        self.assertEqual(path,'/v1/chat/completions')
        self.assertEqual(headers['Authorization'],'Bearer independent-peer-key')
        self.assertNotIn('ChatGPT-Account-ID',headers)
        self.assertFalse(any(k.lower().startswith('x-openai') for k in headers))
        self.assertEqual(body['tools'][0]['function']['name'],'files__patch')
        self.assertEqual(body['messages'],[{'role':'user','content':'Make patch'}])
        self.assertNotIn('reasoning_effort',body)

    def test_ws_incremental_tool_result_and_public_reasoning_provenance(self):
        ws = connect_websocket(self.wsurl,timeout=3)[0]
        try:
            ws.send(encoded({'type':'response.create','model':'any-local-model',
                             'input':[self.declarations(),{'role':'user','content':'Patch'}]}))
            first = self.terminal(ws)[-1]['response']
            self.assertEqual(first['output'][0]['type'],'custom_tool_call')
            ws.send(encoded({'type':'response.create','model':'any-local-model',
                'previous_response_id':first['id'],'input':[{'type':'custom_tool_call_output',
                    'call_id':'call_native','output':'Patched.'}]}))
            events = self.terminal(ws)
            result = events[-1]['response']
            self.assertEqual(result['status'],'completed')
            messages = self.upstream.seen[-1][2]['messages']
            self.assertEqual(messages[-1],{'role':'tool','tool_call_id':'call_native','content':'Patched.'})
            self.assertEqual(messages[-2]['tool_calls'][0]['id'],'call_native')
            reasoning = result['output'][0]
            self.assertTrue(reasoning['encrypted_content'].startswith('codex-model-adapter-origin:v1:'))
            same, protected, changed = resolve_request({'input':[reasoning]},'any-local-model')
            self.assertTrue(changed)
            self.assertTrue(protected)
            self.assertNotIn('encrypted_content',same['input'][0])
            foreign, _, _ = resolve_request({'input':[reasoning]},'gpt-fixture')
            self.assertEqual(foreign['input'][0]['role'],'assistant')
            self.assertEqual(foreign['input'][0]['content'][0]['text'],'Public task facts.')
            item = next(e['item'] for e in events if e['type']=='response.output_item.done'
                        and e['item']['type']=='reasoning')
            self.assertEqual(item,reasoning)
        finally:
            ws.close()

    def test_http_sse_reasoning_effort_is_explicit_capability(self):
        route = self.server.profile['routes']['any-local-model']
        for enabled, effort in ((False,'high'),(True,'low'),(True,'high')):
            route['supports_reasoning_effort'] = enabled
            status, raw = self.request({'model':'any-local-model','input':'Hello','stream':True,
                                       'reasoning':{'effort':effort}})
            self.assertEqual(status,200)
            events = [json.loads(line[5:]) for line in raw.splitlines() if line.startswith(b'data:')]
            self.assertEqual(events[-1]['type'],'response.completed')
            actual = self.upstream.seen[-1][2]
            self.assertEqual(actual.get('reasoning_effort'),effort if enabled else None)
            self.assertEqual(events[-1]['response']['output'][-1]['content'][0]['text'],'Answer.')

    def test_ws_error_and_truncated_stream_fail_without_completion(self):
        for mode, code in (('error','synthetic_quota'),('truncated',None)):
            with self.subTest(mode=mode):
                self.upstream.mode = mode
                ws = connect_websocket(self.wsurl,timeout=3)[0]
                try:
                    ws.send(encoded({'type':'response.create','model':'any-local-model','input':'Hello'}))
                    events = self.terminal(ws)
                    self.assertEqual(events[-1]['type'],'response.failed')
                    self.assertFalse(any(e['type']=='response.completed' for e in events))
                    if code:
                        self.assertEqual(events[-1]['response']['error']['code'],code)
                finally:
                    ws.close()

    def test_chat_only_drops_lazy_declarations_and_invalid_capability_rejected(self):
        self.server.profile['routes']['any-local-model'].update(chat_only=True,normalize_instructions=True)
        status, _ = self.request({'model':'any-local-model','input':[self.declarations(),
            {'role':'user','content':'Hello'}],'tools':[{'type':'custom','name':'patch'}]})
        self.assertEqual(status,200)
        self.assertNotIn('tools',self.upstream.seen[-1][2])
        self.server.profile['routes']['any-local-model']['supports_reasoning_effort'] = 'yes'
        with self.assertRaises(ValueError):
            relay.validate_profile(self.server.profile)


if __name__ == '__main__':
    unittest.main()


class MessagesUpstream(ChatUpstream):
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.seen.append((self.path,dict(self.headers),body))
        results=[b for m in body['messages'] for b in m['content'] if b['type']=='tool_result']
        blocks=([{'type':'tool_use','id':'call_native','name':body['tools'][0]['name'],'input':{'input':'exact native patch'}}]
                if body.get('tools') and not results else [{'type':'text','text':'Messages answer.'}])
        reason='tool_use' if blocks[0]['type']=='tool_use' else 'end_turn'
        message={'type':'message','model':body['model'],'content':blocks,'stop_reason':reason,'usage':{'input_tokens':8,'output_tokens':2}}
        if body.get('stream'):
            events=[{'type':'message_start','message':{**message,'content':[],'stop_reason':None}}]
            for i,b in enumerate(blocks):events.extend([{'type':'content_block_start','index':i,'content_block':b},{'type':'content_block_stop','index':i}])
            events.extend([{'type':'message_delta','delta':{'stop_reason':reason},'usage':message['usage']},{'type':'message_stop'}])
            if self.server.mode=='truncated':events=events[:-1]
            raw=b''.join(b'data: '+encoded(v)+b'\n\n' for v in events)
        else:raw=encoded(message)
        self.send_response(200);self.send_header('Content-Type','text/event-stream' if body.get('stream') else 'application/json')
        self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)


class AnthropicRelayTests(unittest.TestCase):
    request=ChatRelayTests.request
    terminal=ChatRelayTests.terminal
    declarations=ChatRelayTests.declarations
    tearDown=ChatRelayTests.tearDown

    def setUp(self):
        ChatRelayTests.setUp(self)
        self.upstream.RequestHandlerClass=MessagesUpstream
        self.server.profile['routes']['any-local-model'].update(wire_api='anthropic_messages',max_output_tokens=65536,reasoning_levels=['low','high'])

    def test_anthropic_http_websocket_tools_and_auth_isolation(self):
        status,raw=self.request({'model':'any-local-model','input':[{'role':'user','content':'hello'}]})
        self.assertEqual(status,200,raw);self.assertEqual(json.loads(raw)['status'],'completed')
        path,headers,body=self.upstream.seen[-1]
        self.assertEqual(path,'/v1/messages');self.assertEqual(headers['x-api-key'],'independent-peer-key')
        self.assertNotIn('Authorization',headers);self.assertNotIn('ChatGPT-Account-ID',headers)
        self.assertEqual(body['max_tokens'],65536)
        ws=connect_websocket(self.wsurl,timeout=3)[0]
        try:
            ws.send(encoded({'type':'response.create','model':'any-local-model','input':[self.declarations(),{'role':'user','content':'patch'}]}))
            first=self.terminal(ws)[-1]['response'];self.assertEqual(first['status'],'completed')
            self.assertEqual(first['output'][0]['type'],'custom_tool_call')
            ws.send(encoded({'type':'response.create','model':'any-local-model','previous_response_id':first['id'],
                             'input':[{'type':'custom_tool_call_output','call_id':'call_native','output':'done'}]}))
            final=self.terminal(ws)[-1];self.assertEqual(final['type'],'response.completed')
            self.assertEqual(self.upstream.seen[-1][2]['messages'][-1]['content'][0]['tool_use_id'],'call_native')
        finally:ws.close()
        self.upstream.mode='truncated'
        status,raw=self.request({'model':'any-local-model','input':'hello','stream':True})
        events=[json.loads(line[5:]) for line in raw.splitlines() if line.startswith(b'data:')]
        self.assertEqual(events[-1]['type'],'response.failed')
