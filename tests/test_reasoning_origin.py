import copy
import hashlib
import http.client
import http.server
import json
import threading
from pathlib import Path
import os
import tempfile
import unittest

from inference_adapter import create_server
from inference_transport import accept_websocket, connect_websocket
from legacy_history import visible_reasoning
from reasoning_origin import (PREFIX, LegacyOriginIndex, LegacyOriginIndexError, ReasoningOriginError,
                              index_legacy_origins, resolve_request, stamp_event, stamp_item, stamp_response)

SIGNATURE = 'gemini-original-thought-signature'
REASONING = {'type': 'reasoning', 'id': 'rs_external_fixture', 'summary': [], 'encrypted_content': SIGNATURE}
CALL = {'type': 'function_call', 'id': 'fc_fixture', 'call_id': 'same_call', 'name': 'fixture_tool', 'arguments': '{}'}
RESULT = {'type': 'function_call_output', 'call_id': 'same_call', 'output': 'VISIBLE_TOOL_RESULT'}


class OriginTests(unittest.TestCase):
    def test_message_origins_restore_same_service_and_inline_cross_service_without_reasoning(self):
        from chat_completions_compat import _Completion
        from reasoning_origin import MESSAGE_PREFIX
        completion=_Completion('glm-fixture');list(completion.add_text('4','text'))
        original=completion.response('completed')['output'][0]
        stamped=stamp_item(original,'glm-fixture')
        self.assertTrue(stamped['id'].startswith(MESSAGE_PREFIX))
        event=stamp_event({'type':'response.output_text.delta','item_id':original['id'],'delta':'4'},'glm-fixture')
        self.assertEqual(event['item_id'],stamped['id'])
        same,_,changed=resolve_request({'input':[stamped,CALL,RESULT]},'glm-fixture')
        self.assertTrue(changed);self.assertEqual(same['input'],[original,CALL,RESULT])
        crossed,_,changed=resolve_request({'input':[stamped,CALL,RESULT]},'gpt-fixture',official=True)
        self.assertTrue(changed);self.assertNotIn('id',crossed['input'][0])
        self.assertEqual(crossed['input'][0]['content'],original['content'])
        self.assertEqual(crossed['input'][1:],[CALL,RESULT])
        legacy,_,changed=resolve_request({'input':[original]},'gpt-fixture',official=True)
        self.assertTrue(changed);self.assertNotIn('id',legacy['input'][0])
        native={**original,'id':'msg_official_persisted'}
        untouched,_,changed=resolve_request({'input':[native]},'gpt-fixture',official=True)
        self.assertFalse(changed);self.assertEqual(untouched['input'],[native])
        reference={'type':'item_reference','id':original['id']}
        self.assertEqual(resolve_request({'input':[reference]},'gpt-fixture',official=True)[0]['input'],[reference])

    def test_same_model_reasoning_restore_does_not_signal_foreign_message_ids(self):
        native={'type':'message','role':'assistant','id':'msg_native','content':[{'type':'output_text','text':'keep'}]}
        result,_,changed=resolve_request({'input':[stamp_item(REASONING,'gpt-fixture'),native]},'gpt-fixture',official=True)
        self.assertTrue(changed);self.assertEqual(result['input'][-1],native)

    def test_same_model_restores_exact_signature_and_legacy_does_not_remove_it(self):
        stamped = stamp_item(REASONING, 'gemini-fixture')
        self.assertTrue(stamped['encrypted_content'].startswith(PREFIX))
        self.assertNotEqual(stamped['encrypted_content'], SIGNATURE)
        body, protected, changed = resolve_request({'input': [stamped, CALL, RESULT]}, 'gemini-fixture')
        self.assertTrue(changed)
        self.assertEqual(body['input'], [REASONING, CALL, RESULT])
        self.assertEqual(visible_reasoning(body, protected_reasoning=protected)['input'], body['input'])
        self.assertEqual(REASONING['encrypted_content'], SIGNATURE)

    def test_cross_model_drops_only_foreign_private_state_and_keeps_visible_history(self):
        stamped = stamp_item(REASONING, 'gemini-fixture')
        official = {'type': 'reasoning', 'id': 'rs_official', 'encrypted_content': 'original-official-state'}
        compact = {'type': 'compaction', 'encrypted_content': 'original-official-compaction'}
        visible = [{'role': 'user', 'content': 'exact task facts'}, CALL, RESULT]
        body, protected, changed = resolve_request({'input': [official, compact, stamped] + visible}, 'gpt-fixture')
        self.assertTrue(changed)
        self.assertEqual(protected, set())
        self.assertEqual(body['input'], [official, compact] + visible)
        plain, _, changed = resolve_request({'input': [official, compact] + visible}, 'gpt-fixture')
        self.assertFalse(changed)
        self.assertEqual(plain['input'], [official, compact] + visible)

    def test_public_summary_survives_and_json_sse_ws_stamps_are_consistent(self):
        item = {**REASONING, 'summary': [{'type': 'summary_text', 'text': 'Already visible summary.'}]}
        expected = stamp_item(item, 'gemini-fixture')
        response = stamp_response({'output': [item]}, 'gemini-fixture')
        event = stamp_event({'type': 'response.output_item.done', 'item': item}, 'gemini-fixture')
        terminal = stamp_event({'type': 'response.completed', 'response': {'output': [item]}}, 'gemini-fixture')
        self.assertEqual(response['output'][0], expected)
        self.assertEqual(event['item'], expected)
        self.assertEqual(terminal['response']['output'][0], expected)
        converted, _, _ = resolve_request({'input': [expected]}, 'gpt-fixture')
        self.assertEqual(converted['input'][0]['role'], 'assistant')
        self.assertEqual(converted['input'][0]['content'][0]['text'], 'Already visible summary.')
        self.assertNotIn('encrypted_content', converted['input'][0])

    def test_public_reasoning_normalizes_by_protocol_shapes_not_model_names(self):
        shapes = [('summary', [{'type': 'summary_text', 'text': 'visible exact text'}]),
                  ('summary', 'visible exact text'), ('content', 'visible exact text'),
                  ('content', [{'type': 'reasoning_text', 'text': 'visible exact text'}]),
                  ('content', {'type': 'text', 'text': {'value': 'visible exact text'}}),
                  ('content', [{'type': 'thinking', 'thinking': 'visible exact text'}]),
                  ('content', ['visible exact text'])]
        for source in ('local-model', 'claude-compatible', 'glm-compatible', 'arbitrary-provider'):
            for key, content in shapes:
                original = {'type': 'reasoning', 'id': 'vendor-id', key: content,
                            'vendor_metadata': {'private_schema': True}, 'status': 'completed'}
                stamped = stamp_item(original, source)
                same, _, _ = resolve_request({'input': [stamped]}, source)
                self.assertEqual(same['input'][0], original)
                crossed, _, _ = resolve_request({'input': [stamped]}, 'gpt-fixture')
                self.assertEqual(crossed['input'], [{'role': 'assistant', 'content': [
                    {'type': 'output_text', 'text': 'visible exact text'}]}])

    def test_indexed_legacy_state_restores_only_its_source_and_official_state_stays_unchanged(self):
        origins = {hashlib.sha256(SIGNATURE.encode()).hexdigest(): 'gemini-fixture'}
        same, protected, _ = resolve_request({'input': [REASONING]}, 'gemini-fixture', legacy_origins=origins)
        self.assertEqual(visible_reasoning(same, protected_reasoning=protected)['input'], [REASONING])
        public = {**REASONING, 'content': [{'type': 'reasoning_text', 'text': 'Public facts.'}]}
        crossed, _, _ = resolve_request({'input': [public]}, 'gpt-fixture', legacy_origins=origins)
        self.assertEqual(crossed['input'], [{'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Public facts.'}]}])
        official = {**REASONING, 'encrypted_content': 'official-original-cipher'}
        untouched, _, changed = resolve_request({'input': [official]}, 'gpt-fixture', legacy_origins=origins)
        self.assertFalse(changed)
        self.assertEqual(untouched['input'], [official])

    def test_malformed_or_unknown_version_is_explicit_and_never_silently_restored(self):
        for cipher in (PREFIX + 'invalid', 'codex-model-adapter-origin:v2:opaque'):
            with self.assertRaises(ReasoningOriginError):
                resolve_request({'input': [{**REASONING, 'encrypted_content': cipher}]}, 'gemini-fixture')


class ProvenanceUpstream(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def response_value(self, body):
        has_result = any(v.get('type') == 'function_call_output' for v in body.get('input', []) if isinstance(v, dict))
        if has_result and body['model'] == 'gemini-fixture':
            self.server.signature_restored = any(v.get('type') == 'reasoning' and v.get('encrypted_content') == SIGNATURE
                                                  for v in body.get('input', []) if isinstance(v, dict))
            if not self.server.signature_restored:
                return None
        output = [REASONING, CALL] if not has_result else [{'type': 'message', 'role': 'assistant',
            'content': [{'type': 'output_text', 'text': 'TOOL_CONTINUATION_OK'}]}]
        return {'id': 'resp_second' if has_result else 'resp_first', 'status': 'completed', 'model': body['model'], 'output': output}

    def do_POST(self):
        raw = self.rfile.read(int(self.headers['Content-Length']))
        body = json.loads(raw)
        self.server.records.append((body, raw))
        if body['model'].startswith('gpt-'):
            self.server.gpt_clean = all(v.get('encrypted_content') != SIGNATURE and not str(v.get('encrypted_content', '')).startswith(PREFIX)
                for v in body.get('input', []) if isinstance(v, dict) and v.get('type') == 'reasoning')
            response = {'id': 'gpt_fixture', 'status': 'completed', 'model': body['model'], 'output': []}
        else:
            response = self.response_value(body)
        if response is None:
            self.send_error(400)
            return
        if body.get('stream'):
            values = [{'type': 'response.output_item.done', 'item': v, 'output_index': n} for n, v in enumerate(response['output'])]
            values.append({'type': 'response.completed', 'response': response})
            data = ''.join('data: ' + json.dumps(v) + '\n\n' for v in values).encode()
            content_type = 'text/event-stream'
        else:
            data, content_type = json.dumps(response).encode(), 'application/json'
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        ws = accept_websocket(self)
        try:
            while (message := ws.recv()) is not None:
                body = json.loads(message[1])
                self.server.records.append((body, message[1]))
                self.server.gpt_clean = all(v.get('encrypted_content') != SIGNATURE and not str(v.get('encrypted_content', '')).startswith(PREFIX)
                    for v in body.get('input', []) if isinstance(v, dict) and v.get('type') == 'reasoning')
                ws.send(json.dumps({'type': 'response.completed', 'response': {'id': 'gpt_fixture', 'output': []}}))
        except OSError:
            pass
        finally:
            ws.close()


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), ProvenanceUpstream)
        self.upstream.daemon_threads = True
        self.upstream.records, self.upstream.signature_restored, self.upstream.gpt_clean = [], False, False
        endpoint = f'http://127.0.0.1:{self.upstream.server_port}'
        self.profile = {'format': 1, 'edition': 'proxy', 'identity': 'origin-fixture', 'token': 'x' * 40, 'port': 0,
            'gpt': {'chatgpt_base_url': endpoint + '/official', 'api_base_url': endpoint + '/official'},
            'legacy_origins_file': str(Path(self.temporary.name) / 'origins.json'),
            'routes': {'gemini-fixture': {'base_url': endpoint + '/gemini', 'tool_adapter': True}}}
        self.adapter = create_server(self.profile)
        for server in (self.upstream, self.adapter):
            threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True).start()
        self.path = '/' + self.profile['token'] + '/backend-api/codex/responses'

    def tearDown(self):
        for server in (self.adapter, self.upstream):
            server.shutdown()
            server.server_close()
        self.temporary.cleanup()

    def http(self, body, *, raw=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.adapter.server_port, timeout=3)
        connection.request('POST', self.path, body=raw or json.dumps(body).encode(), headers={'Authorization': 'Bearer official-fixture'})
        response = connection.getresponse()
        status, data = response.status, response.read()
        connection.close()
        self.assertEqual(status, 200)
        return data

    def terminal(self, ws):
        while True:
            value = json.loads(ws.recv()[1])
            if value['type'] in ('response.completed', 'error'):
                return value

    def test_http_signature_is_stamped_and_restored_for_same_gemini_tool_result(self):
        response = json.loads(self.http({'model': 'gemini-fixture', 'input': []}))
        self.assertTrue(response['output'][0]['encrypted_content'].startswith(PREFIX))
        self.http({'model': 'gemini-fixture', 'input': response['output'] + [RESULT]})
        self.assertTrue(self.upstream.signature_restored)
        self.http({'model': 'gpt-fixture', 'input': response['output'] + [RESULT]})
        self.assertTrue(self.upstream.gpt_clean)
        self.assertEqual(self.upstream.records[-1][0]['input'], [CALL, RESULT])

    def test_ws_incremental_history_restores_gemini_signature_and_crosses_back_to_gpt(self):
        ws, _ = connect_websocket(f'ws://127.0.0.1:{self.adapter.server_port}' + self.path, timeout=3)
        ws.send(json.dumps({'type': 'response.create', 'model': 'gemini-fixture', 'input': []}))
        first = self.terminal(ws)['response']
        self.assertTrue(first['output'][0]['encrypted_content'].startswith(PREFIX))
        ws.send(json.dumps({'type': 'response.create', 'model': 'gemini-fixture', 'previous_response_id': first['id'], 'input': [RESULT]}))
        self.assertEqual(self.terminal(ws)['type'], 'response.completed')
        self.assertTrue(self.upstream.signature_restored)
        ws.send(json.dumps({'type': 'response.create', 'model': 'gpt-fixture', 'input': first['output'] + [RESULT]}))
        self.assertEqual(self.terminal(ws)['type'], 'response.completed')
        self.assertTrue(self.upstream.gpt_clean)
        self.assertEqual(self.upstream.records[-1][0]['input'], [CALL, RESULT])
        ws.close()

    def test_cross_provider_message_ids_are_removed_on_http_and_websocket(self):
        from chat_completions_compat import _Completion
        completion=_Completion('glm-fixture');list(completion.add_text('4','text'))
        old=completion.response('completed')['output'][0]
        for message in (old,stamp_item(old,'glm-fixture')):
            for reasoning in ([],[stamp_item(REASONING,'glm-fixture')]):
                body={'model':'gpt-fixture','input':reasoning+[message,CALL,RESULT]}
                self.http(body)
                sent=self.upstream.records[-1][0]['input']
                self.assertNotIn('id',next(i for i in sent if i.get('type')=='message'))
                self.assertEqual(sent[-2:],[CALL,RESULT])
                ws,_=connect_websocket(f'ws://127.0.0.1:{self.adapter.server_port}'+self.path,timeout=3)
                try:
                    ws.send(json.dumps({'type':'response.create',**body}));self.terminal(ws)
                    sent=self.upstream.records[-1][0]['input']
                    self.assertNotIn('id',next(i for i in sent if i.get('type')=='message'))
                    self.assertEqual(sent[-2:],[CALL,RESULT])
                finally:ws.close()

    def test_unmarked_original_gpt_http_bytes_are_unchanged(self):
        raw = b'{ "model":"gpt-fixture", "input":[{"type":"reasoning","encrypted_content":"official-original-cipher"}] }'
        self.http(None, raw=raw)
        self.assertEqual(self.upstream.records[-1][1], raw)

    def test_indexed_legacy_foreign_content_becomes_standard_message_for_gpt(self):
        source = Path(self.temporary.name) / 'home/sessions/legacy.jsonl'
        source.parent.mkdir(parents=True)
        public = {**REASONING, 'content': [{'type': 'reasoning_text', 'text': 'Public task facts.'}]}
        source.write_text(json.dumps({'type': 'turn_context', 'payload': {'model': 'gemini-fixture'}}) + '\n' +
                          json.dumps({'type': 'response_item', 'payload': public}) + '\n', encoding='utf-8')
        index_legacy_origins([source], ['gemini-fixture'], self.profile['legacy_origins_file'], source.parent.parent)
        self.http({'model': 'gpt-fixture', 'input': [public, CALL, RESULT]})
        self.assertEqual(self.upstream.records[-1][0]['input'], [
            {'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Public task facts.'}]}, CALL, RESULT])


class IndexTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Native Windows extended path aliases')
    def test_native_extended_rollout_path_is_scoped_to_same_home(self):
        from reasoning_origin import _source_path
        with tempfile.TemporaryDirectory() as temp:
            home=Path(temp);sessions=home/'sessions';sessions.mkdir()
            rollout=sessions/'fixture.jsonl';rollout.write_text('{}\n')
            self.assertEqual(_source_path('\\\\?\\'+str(rollout),home),rollout.resolve())
            other=home/'unrelated.jsonl';other.write_text('{}\n')
            with self.assertRaises(ValueError):_source_path('\\\\?\\'+str(other),home)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.home, self.output = self.base / 'home', self.base / 'private/origins.json'
        (self.home / 'sessions').mkdir(parents=True)
        (self.home / 'archived_sessions').mkdir()

    def rollout(self, name, events, *, archived=False):
        path = self.home / ('archived_sessions' if archived else 'sessions') / name
        path.write_text(''.join(json.dumps(v) + '\n' for v in events), encoding='utf-8')
        return path

    def records(self, model, cipher):
        return [{'type': 'turn_context', 'payload': {'model': model}},
                {'type': 'response_item', 'payload': {**REASONING, 'encrypted_content': cipher}}]

    def test_streamed_provenance_only_indexes_external_outputs_and_skips_conflicts(self):
        first = self.rollout('first.jsonl', self.records('gpt-official', 'OFFICIAL_SECRET') +
            self.records('external-a', 'EXTERNAL_A_SECRET') + self.records('external-a', 'CONFLICT_SECRET') +
            self.records('external-b', 'CONFLICT_SECRET') + self.records('external-a', 'SHARED_OFFICIAL_SECRET') +
            self.records('gpt-official', 'SHARED_OFFICIAL_SECRET') + [
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': 'DO_NOT_PERSIST_HISTORY'}}])
        second = self.rollout('archived.jsonl', self.records('external-b', 'EXTERNAL_B_SECRET'), archived=True)
        original = first.read_bytes()
        report = index_legacy_origins([first, second], ['external-a', 'external-b'], self.output, self.home)
        self.assertEqual(report['files'], 2)
        self.assertEqual(report['indexed'], 2)
        self.assertEqual(report['conflicts'], 2)
        raw = self.output.read_text(encoding='utf-8')
        for secret in ('OFFICIAL_SECRET', 'EXTERNAL_A_SECRET', 'EXTERNAL_B_SECRET', 'DO_NOT_PERSIST_HISTORY', 'CONFLICT_SECRET'):
            self.assertNotIn(secret, raw)
        self.assertEqual(first.read_bytes(), original)
        self.assertEqual(set(LegacyOriginIndex(self.output).get().values()), {'external-a', 'external-b'})
        if os.name != 'nt':
            self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.output.parent.stat().st_mode & 0o777, 0o700)

    def test_source_boundaries_symlinks_and_untrusted_nested_reasoning_are_not_indexed(self):
        allowed = self.rollout('allowed.jsonl', self.records('external-a', 'valid-secret'))
        outside = self.base / 'outside.jsonl'
        outside.write_text(allowed.read_text(encoding='utf-8'), encoding='utf-8')
        paths = [outside, self.home / 'sessions/../outside.jsonl']
        link = self.home / 'sessions/link.jsonl'
        try:
            link.symlink_to(allowed)
            paths.append(link)
        except OSError:
            pass
        report = index_legacy_origins(paths, ['external-a'], self.output, self.home)
        self.assertEqual(report['files'], 0)
        self.assertEqual(report['indexed'], 0)
        nested = self.rollout('nested.jsonl', [{'type': 'turn_context', 'payload': {'model': 'external-a'}},
            {'type': 'event_msg', 'payload': {'reasoning': REASONING}}])
        self.assertEqual(index_legacy_origins([nested], ['external-a'], self.output, self.home)['indexed'], 0)

    def test_lazy_reader_missing_reload_invalid_format_and_malformed_source_context(self):
        reader = LegacyOriginIndex(self.output)
        self.assertEqual(reader.get(), {})
        source = self.rollout('first.jsonl', self.records('external-a', 'first-secret'))
        index_legacy_origins([source], ['external-a'], self.output, self.home)
        self.assertIn(hashlib.sha256(b'first-secret').hexdigest(), reader.get())
        source.write_text(json.dumps({'type': 'turn_context', 'payload': {'model': 'external-a'}}) + '\n{bad\n' +
            json.dumps({'type': 'response_item', 'payload': REASONING}) + '\n' +
            ''.join(json.dumps(v) + '\n' for v in self.records('external-a', 'second-secret')), encoding='utf-8')
        report = index_legacy_origins([source], ['external-a'], self.output, self.home)
        self.assertEqual(report['malformed_lines'], 1)
        self.assertEqual(list(reader.get()), [hashlib.sha256(b'second-secret').hexdigest()])
        self.output.write_text('{"format":99,"origins":{}}', encoding='utf-8')
        with self.assertRaises(LegacyOriginIndexError):
            reader.get()
