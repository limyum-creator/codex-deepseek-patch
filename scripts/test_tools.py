"""Real CLI regression for large MCP catalogs, lazy execution and compaction.

Only synthetic tools and loopback inference are used. --mcp is the fixture server.
"""
from pathlib import Path
import http.server
import json
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import patch
import local_bridge
from responses_compat import SEARCH_NAME, ToolAdapter
from scripts.test_context import Rpc
from scripts.test_offline import isolated_environment, launch_options, stop_probe, probe_command


def mcp_fixture():
    tools = [{'name': 'probe_tool_'+str(i), 'description': 'Bulk regression tool '+str(i)+'. '+('Detailed synthetic reference. '*250),
              'annotations': {'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': False},
              'inputSchema': {'type': 'object', 'properties': {'value': {'type': 'string'}}, 'required': ['value']}}
             for i in range(160)]
    for line in sys.stdin:
        request = json.loads(line)
        if 'id' not in request:
            continue
        method = request['method']
        if method == 'initialize':
            result = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'bulk-regression', 'version': '1'}}
        elif method == 'tools/list':
            result = {'tools': tools}
        elif method == 'tools/call':
            result = {'content': [{'type': 'text', 'text': 'LAZY_TOOL_OK'}]}
        else:
            result = {}
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)


class Stub(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *_):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers['Content-Length']))
        body = json.loads(raw)
        tools = body.get('tools', [])
        index = len(self.server.records)
        phase = self.server.phase
        error = None
        compact = not tools
        if compact:
            item = self.message('A short synthetic summary. Continue the requested test.')
        elif phase == 'integer_call':
            candidates = [(None, t) for t in tools if t.get('type') == 'function']
            candidates += [(t['name'], child) for t in tools if t.get('type') == 'namespace'
                           for child in t.get('tools', [])]
            command = next(((ns, t) for ns, t in candidates if t.get('name') == 'exec_command'), None)
            if not command:
                error = 'Native exec_command missing from the test catalog'
                namespace, declaration = None, {'name': 'exec_command'}
            else:
                namespace, declaration = command
            self.server.integer_schema = declaration.get('parameters')
            item = {'type': 'function_call', 'id': 'fc_integer_'+str(index), 'call_id': 'integer_'+str(index),
                    'status': 'completed', 'name': declaration['name'],
                    'arguments': '{"cmd":"echo INTEGER_TOOL_OK","yield_time_ms":1000.0,"max_output_tokens":300.0}'}
            if namespace:
                item['namespace'] = namespace
            self.server.phase = 'integer_answer'
        elif phase == 'integer_answer':
            outputs = [str(i.get('output')) for i in body.get('input', [])
                       if i.get('type') == 'function_call_output' and str(i.get('call_id', '')).startswith('integer_')]
            if not outputs or not any('INTEGER_TOOL_OK' in value for value in outputs) or any('failed to parse' in value for value in outputs):
                error = 'Integral-decimal native command did not execute: '+str(outputs)
            item = self.message('INTEGER_TOOL_OK')
        elif phase == 'search':
            if len(raw) > 120000 or not any(t.get('name') == SEARCH_NAME for t in tools):
                error = 'Eager tool catalog or missing native discovery'
            item = {'type': 'function_call', 'id': 'fc_search_'+str(index), 'call_id': 'search_'+str(index),
                    'status': 'completed', 'name': SEARCH_NAME,
                    'arguments': '{"query":"probe_tool_42","limit":1}'}
            self.server.phase = 'call'
        elif phase == 'call':
            loaded = next((t for t in tools if t.get('type') == 'namespace' and t.get('name') == 'mcp__bulk'), None)
            alias=ToolAdapter.alias('mcp__bulk','probe_tool_42')
            flat=next((t for t in tools if t.get('type')=='function' and t.get('name')==alias),None)
            if not flat and (not loaded or [t['name'] for t in loaded['tools']] != ['probe_tool_42']):
                error = 'Search did not expose exactly the requested MCP tool'
            item = {'type': 'function_call', 'id': 'fc_tool_'+str(index), 'call_id': 'tool_'+str(index),
                    'status': 'completed', 'namespace': 'mcp__bulk', 'name': 'probe_tool_42',
                    'arguments': '{"value":"check"}'}
            if flat:item.pop('namespace');item['name']=alias
            self.server.phase = 'answer'
        else:
            if not any(i.get('type') == 'function_call_output' and 'LAZY_TOOL_OK' in str(i.get('output'))
                       for i in body.get('input', [])):
                error = 'Discovered MCP tool did not execute: '+str([
                    i for i in body.get('input',[]) if i.get('type')=='function_call_output' and str(i.get('call_id','')).startswith('tool_')])
            item = self.message('LAZY_TOOL_OK')
            self.server.phase = 'search'
        self.server.records.append({'model': body.get('model'), 'request_bytes': len(raw),
                                    'phase': 'compact' if compact else phase, 'error': error})
        if error:
            payload = json.dumps({'error': {'message': error}}).encode()
            self.send_response(400); self.send_header('Content-Length', str(len(payload))); self.end_headers()
            self.wfile.write(payload); return
        usage=self.server.answer_usage if phase=='answer' and not compact else 3000
        if phase=='answer' and not compact:self.server.answer_usage=3000
        response = {'id': 'resp_'+str(index), 'object': 'response', 'created_at': int(time.time()),
                    'status': 'completed', 'model': body['model'], 'output': [item],
                    'usage': {'input_tokens': usage, 'output_tokens': 3, 'total_tokens': usage+3}}
        events = [{'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
                  {'type': 'response.output_item.added', 'output_index': 0, 'item': {**item, 'status': 'in_progress'}},
                  {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                  {'type': 'response.completed', 'response': response}]
        payload = ''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n' for e in events).encode()
        self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(payload))); self.end_headers(); self.wfile.write(payload)

    @staticmethod
    def message(text):
        return {'type': 'message', 'id': 'msg_probe', 'role': 'assistant', 'status': 'completed',
                'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}


def main(directory):
    directory, manifest = patch.load_build(Path(directory))
    catalog = json.loads((directory/'desktop-models.json').read_text(encoding='utf-8'))
    entries = {m['slug']: m for m in catalog['models']}
    routes = manifest.get('model_routes', {})
    models = [m for m, r in routes.items() if r.get('supports_tools', True)]
    for model in models:
        patch.require(entries[model].get('supports_search_tool') is True, 'Lazy discovery disabled: '+model)
    upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Stub)
    upstream.daemon_threads = True; upstream.records = []; upstream.phase = 'search'; upstream.answer_usage=3000
    bridge = local_bridge.Server(('127.0.0.1', 0), local_bridge.Handler)
    bridge.profile = {'base_url': 'http://127.0.0.1:'+str(upstream.server_port)+'/v1',
                      'native_tools': True, 'token': 'offline-tool-probe'}
    bridge.active = 0; bridge.last_activity = time.monotonic()
    for server in (upstream, bridge):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    report = {'status': 'failed', 'external_inference_turns': 0, 'mcp_tools': 160, 'models': []}
    manifest['tool_loading_probe'] = 'running'; patch.save_json(directory/'manifest.json', manifest)
    app = directory/'patched.app' if manifest['status'] == 'prepared' else Path(manifest.get('deployment_target', manifest['target']))
    try:
        with tempfile.TemporaryDirectory(prefix='codex-tool-loading-') as tmp:
            base = Path(tmp); env = isolated_environment(base); env['PROBE_KEY'] = bridge.profile['token']
            config = ('model_catalog_json='+json.dumps(str(directory/'desktop-models.json'))+'\n'
                      '[mcp_servers.bulk]\ncommand='+json.dumps(sys.executable)+'\nargs='+json.dumps([str(Path(__file__).resolve()), '--mcp'])+'\n'
                      '[model_providers.probe]\nname="Offline tools"\nwire_api="responses"\nrequires_openai_auth=false\n'
                      'base_url="http://127.0.0.1:'+str(bridge.server_port)+'/v1"\nenv_key="PROBE_KEY"\nrequest_max_retries=0\nstream_max_retries=0\n')
            (base/'home/config.toml').write_text(config, encoding='utf-8')
            with (directory/'tool-loading-probe.log').open('w', encoding='utf-8') as log:
                process = subprocess.Popen(probe_command(app),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True, encoding='utf-8', env=env, cwd=base, **launch_options())
                try:
                    rpc = Rpc(process); rpc.request('initialize', {'clientInfo': {'name': 'tool_loading_probe', 'version': '1'}, 'capabilities': {'experimentalApi': True}})
                    for model in models:
                        upstream.phase = 'search'; start = len(upstream.records)
                        route = routes[model]
                        bridge.profile['flatten_namespaces']=route['provider'].startswith('local-')
                        thread = rpc.request('thread/start', {'model': model, 'modelProvider': 'probe', 'cwd': str(base),
                            'ephemeral': True, 'approvalPolicy': 'never', 'sandbox': 'read-only', 'baseInstructions': 'Run the synthetic tool test.',
                            'config': {'web_search': 'disabled', 'features.memories': False, 'model_context_window': route['context_window'],
                                       'model_auto_compact_token_limit': route['compact_limit']}})
                        tid = thread['thread']['id']
                        first = rpc.turn(tid)
                        upstream.answer_usage=route['compact_limit']+100
                        second = rpc.turn(tid)
                        patch.require(len(upstream.records)-start == 6, 'Unexpected discovery retries: '+model)
                        after = rpc.turn(tid)
                        records = upstream.records[start:]
                        patch.require(len(records) == 10 and sum(r['phase'] == 'compact' for r in records) == 1,
                                      'Repeated compaction or missing tool calls: '+model)
                        patch.require(not any(r['error'] for r in records), 'Tool-loading request validation failed')
                        windows = [e['params']['tokenUsage']['modelContextWindow'] for e in first+second+after
                                   if e.get('method') == 'thread/tokenUsage/updated']
                        patch.require(windows and set(windows) == {route['context_window']}, 'Context changed after compaction')
                        report['models'].append({'model': model, 'turns': 3, 'requests': len(records), 'compactions': 1,
                                                 'context_window': route['context_window'], 'largest_request_bytes': max(r['request_bytes'] for r in records)})
                        print('PASS:', model, '160 MCP tools, discovery/execution before and after compaction', flush=True)
                    if models:
                        upstream.phase = 'integer_call'; start = len(upstream.records)
                        bridge.profile['flatten_namespaces'] = False
                        thread = rpc.request('thread/start', {'model': models[0], 'modelProvider': 'probe', 'cwd': str(base),
                            'ephemeral': True, 'approvalPolicy': 'never', 'sandbox': 'read-only',
                            'baseInstructions': 'Run the harmless synthetic command test.',
                            'config': {'web_search': 'disabled', 'features.memories': False}})
                        rpc.turn(thread['thread']['id'])
                        records = upstream.records[start:]
                        patch.require(len(records) == 2 and not any(r['error'] for r in records),
                                      'Native integral-decimal tool regression failed')
                        report['native_integer_arguments'] = 'passed'
                        print('PASS: native exec_command accepts schema-normalized integral decimals', flush=True)
                    report['status'] = 'passed'
                finally:
                    stop_probe(process)
    finally:
        for server in (bridge, upstream):
            server.shutdown(); server.server_close()
        report['requests']=upstream.records
        report['integer_argument_schema']=getattr(upstream,'integer_schema',None)
        patch.save_json(directory/'tool-loading-probe-result.json', report)
        manifest['tool_loading_probe'] = report['status']; patch.save_json(directory/'manifest.json', manifest)
    return report


if __name__ == '__main__':
    mcp_fixture() if sys.argv[1:] == ['--mcp'] else main(sys.argv[1])
