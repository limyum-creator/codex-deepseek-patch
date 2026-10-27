"""Explicit live regression through the new adapter; only synthetic chat content.

Existing ChatGPT access is supplied to the isolated CLI's ephemeral credential
store in memory. No login refresh, keychain changes, or production chat writes.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import ExitStack
import copy
import json
from pathlib import Path
import secrets
import re
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import inference_adapter
import patch
from official_client_config import PROVIDER, prepare_config
from scripts.test_context import Rpc
from scripts.test_offline import isolated_environment, launch_options, probe_command, stop_probe


def finish(rpc, tid, turn, timeout=180):
    result, until = [], time.monotonic() + timeout
    while time.monotonic() < until:
        item = rpc.pending.pop(0) if rpc.pending else rpc.receive_event(max(.1, until - time.monotonic()))
        if item.get('params', {}).get('threadId') != tid:
            continue
        result.append(item)
        if item.get('method') == 'turn/completed' and (turn is None or item['params']['turn']['id'] == turn['turn']['id']):
            if item['params']['turn']['status'] != 'completed':
                error = item['params']['turn'].get('error') or {}
                # Native/provider error text may include request data. Keep only
                # bounded protocol status for this public test result.
                message = str(error.get('message', ''))
                message = re.sub(r'(?i)(bearer\s+)[^\s\"\']+|sk-[\w-]+|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[redacted]', message)
                raise RuntimeError('Live native turn failed: ' + str(error.get('codexErrorInfo', 'unknown'))[:120] + ' ' + message[:500])
            return result
    raise TimeoutError('Live native turn did not complete')


def messages(events):
    return '\n'.join(value['params']['item'].get('text', '') for value in events
                     if value.get('method') == 'item/completed' and value['params']['item'].get('type') == 'agentMessage')


def run(app, build, auth_home, output, models, *, sandbox='workspace-write', provider_round=False):
    app, build, auth_home, output = map(Path, (app, build, auth_home, output))
    output.mkdir(parents=True, exist_ok=True)
    report = {'status':'failed', 'real_user_history_used':False, 'production_config_changed':False,
              'original_login_refreshed':False, 'checks':[], 'models':models, 'provider_round':provider_round}
    auth = json.loads((auth_home / 'auth.json').read_text(encoding='utf-8'))
    tokens = auth.get('tokens') or {}
    access, account = tokens['access_token'], tokens['account_id']
    claims = json.loads(base64.urlsafe_b64decode(access.split('.')[1] + '==='))
    patch.require(claims.get('exp', 0) > time.time() + 300, 'Original login access is too close to expiry for this test')
    profile = inference_adapter.load_profile(build / 'adapter-profile.json')
    profile = {**copy.deepcopy(profile), 'port':0}
    catalog = json.loads((build / 'desktop-models.json').read_text(encoding='utf-8'))
    entries = {entry['slug']:entry for entry in catalog['models']}
    patch.require(models and all(model in entries for model in models), 'A test model is absent from the configured catalog')
    try:
        with tempfile.TemporaryDirectory(prefix='codex-official-live-') as tmp:
            with ExitStack() as owned:
                base = Path(tmp)
                env = isolated_environment(base)
                work = base / 'work'
                work.mkdir()
                marker = 'CONTEXT_' + secrets.token_hex(6).upper()
                (work / 'probe.txt').write_text(marker + '\n', encoding='utf-8')
                adapter = inference_adapter.create_server(profile)
                threading.Thread(target=adapter.serve_forever, kwargs={'poll_interval':.05}, daemon=True).start()
                owned.callback(adapter.server_close)
                owned.callback(adapter.shutdown)
                url = 'http://127.0.0.1:' + str(adapter.server_port) + '/' + profile['token'] + '/backend-api/codex'
                config, _ = prepare_config('cli_auth_credentials_store="ephemeral"\nweb_search="disabled"\n'
                                            '[features]\napps=false\nmemories=false\n',
                                            (build / 'desktop-models.json').resolve(), url, mode='native-summary')
                (base / 'home/config.toml').write_text(config, encoding='utf-8')
                process = subprocess.Popen(probe_command(app), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, encoding='utf-8', env=env, cwd=work, **launch_options())
                def stop_owned_cli():
                    nonlocal process
                    if process is not None:
                        stop_probe(process)
                        process = None
                owned.callback(stop_owned_cli)
                rpc = Rpc(process)
                rpc.request('initialize', {'clientInfo':{'name':'official_client_live_probe', 'version':'1'},
                                          'capabilities':{'experimentalApi':True}})
                rpc.request('account/login/start', {'type':'chatgptAuthTokens', 'accessToken':access, 'chatgptAccountId':account})
                observed = rpc.request('account/read', {'refreshToken':False})
                report['account_type'] = (observed.get('account') or {}).get('type')
                report['requires_openai_auth'] = observed.get('requiresOpenaiAuth')
                patch.require(report['account_type'] == 'chatgpt' and report['requires_openai_auth'], 'Native ChatGPT login was not retained')
                thread = rpc.request('thread/start', {'model':models[0], 'modelProvider':PROVIDER, 'cwd':str(work),
                    'approvalPolicy':'never', 'sandbox':sandbox,
                    'baseInstructions':'This is a small connectivity and context-retention test. Follow each test exactly. '
                        'When asked, use the native shell tool to read only probe.txt in this directory. Do not edit any files.'})
                tid = thread['thread']['id']
                report['sandbox'] = thread.get('sandbox')
                remembered = []
                for index, model in enumerate(models):
                    final = provider_round and index == len(models)-1
                    if provider_round:
                        tag = 'STEP_' + str(index+1) + '_' + secrets.token_hex(5).upper()
                        remembered.append(tag)
                        prompt = ('This is the next step of our same-chat model switching test. Remember the new tag ' + tag + '. '
                                  'List every STEP_ tag given by the user so far, including this one, in their original order. '
                                  'Do not invent tags or consult external conversation histories. ')
                        if 0 < index < len(models)-1:
                            prompt += 'Also use the native shell tool to read only probe.txt in the current directory and append its exact contents.'
                        elif final:
                            prompt += 'Do not use any tools. Also recall and append the CONTEXT_ value read earlier from probe.txt.'
                    else:
                        prompt = ('Remember this synthetic task marker: ' + marker + '. Reply exactly with that marker.'
                                  if index == 0 else 'Use the native shell tool to read probe.txt in the current directory. Reply with its exact contents.')
                    levels = [level['effort'] for level in entries[model].get('supported_reasoning_levels', [])]
                    params = {'threadId':tid, 'model':model, 'input':[{'type':'text','text':prompt}]}
                    if levels:
                        params['effort'] = 'low' if 'low' in levels else levels[0]
                    turn = rpc.request('turn/start', params)
                    events = finish(rpc, tid, turn)
                    item_types = [value['params']['item']['type'] for value in events if value.get('method') == 'item/completed']
                    reply = messages(events)
                    if not provider_round or index:
                        patch.require(marker in reply, 'Live response did not retain the synthetic task marker')
                    if provider_round:
                        positions = [reply.find(tag) for tag in remembered]
                        patch.require(all(position >= 0 for position in positions) and positions == sorted(positions),
                                      'A prior model step was lost or reordered')
                    tool_required = index > 0 and not final
                    if tool_required:
                        patch.require('commandExecution' in item_types, 'Native file tool was not executed')
                    if final:
                        patch.require('commandExecution' not in item_types, 'Final recall used a tool instead of conversation history')
                    report['checks'].append({'model':model, 'reply_marker':'passed', 'tool':'passed' if tool_required else 'not_requested',
                        'prior_step_tags_recalled':len(remembered) if provider_round else None,
                        'item_types':item_types, 'contexts':sorted({value['params']['tokenUsage']['modelContextWindow']
                            for value in events if value.get('method') == 'thread/tokenUsage/updated'})})
                    print(json.dumps({'model':model, 'reply':'passed', 'tool':report['checks'][-1]['tool']}), flush=True)
                if not provider_round:
                    rpc.request('thread/compact/start', {'threadId':tid})
                    finish(rpc, tid, None)
                    report['manual_plain_compaction'] = 'passed'
                    model = models[0]
                    turn = rpc.request('turn/start', {'threadId':tid,'model':model,
                        'input':[{'type':'text','text':'Without tools, recall the exact CONTEXT_ marker from our prior conversation. Reply only with the marker.'}]})
                    result = finish(rpc, tid, turn)
                    patch.require(marker in messages(result), 'Task marker was lost after compaction and model return')
                    report['return_after_compaction'] = 'passed'
                # Windows holds active SQLite files with sharing restrictions.
                # Stop the owned process before inspecting or deleting its files.
                stop_owned_cli()
                report['credentials_written_in_fixture'] = any(access.encode() in path.read_bytes()
                    for path in base.rglob('*') if path.is_file())
                patch.require(not report['credentials_written_in_fixture'], 'Ephemeral test wrote the original credential')
                report['status'] = 'passed'
    except Exception as error:
        report['error_type'] = type(error).__name__
        report['error'] = str(error)[:240] if isinstance(error, (patch.PatchError, RuntimeError, TimeoutError)) else 'See private execution stage'
        raise
    finally:
        patch.save_json(output / 'live-result.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('app','build','auth-home','output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--model', action='append', required=True)
    parser.add_argument('--run', action='store_true', required=True, help='Perform the explicitly requested small live test')
    parser.add_argument('--provider-round', action='store_true', help='Exactly one ordered multi-provider recall/tool round')
    args = parser.parse_args()
    result = run(args.app, args.build, args.auth_home, args.output, args.model, provider_round=args.provider_round)
    print(json.dumps({'status':result['status'], 'account_type':result['account_type']}), flush=True)
