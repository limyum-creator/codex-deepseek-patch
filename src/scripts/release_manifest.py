"""Publish pinned portable updates while keeping the development tree organized."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import io
import platform
import sys
import time
import zipfile
from contextlib import ExitStack

SOURCE = Path(__file__).resolve().parents[1]
ROOT = SOURCE.parent if SOURCE.name == "src" else SOURCE
LEGACY_CHANNELS = {'ds': 'main', 'proxy': 'codex/existing-proxy-models'}
CHANNELS = {**LEGACY_CHANNELS, 'unified':'main'}
VARIANTS = {(host, channel) for host in ('macos', 'windows') for channel in CHANNELS}
REQUIRED_NATIVE = frozenset((
    'prepared_build_install_validation',
    'native_catalog_reasoning', 'existing_chat_native_identity_migration', 'native_cross_provider_message_ids',
    'same_thread_model_switching', 'native_auto_manual_compaction_windows',
    'same_thread_external_after_gpt_quota_exhausted',
    'long_history_compaction_after_gpt_quota_exhausted',
    'native_lazy_160_mcp_tools', 'native_custom_tool_execution',
    'native_prediction_provider_and_fork', 'gpt_ws_registered_http_auth_isolation',
    'old_encrypted_history_without_gpt_quota', 'native_tools_after_old_history_recovery',
    'native_image_generation', 'native_image_edit', 'native_image_auth_and_saved_files',
    'native_image_websocket_roundtrip',
    'native_websocket_compression',
    'native_adapter_errors_finish_without_idle_wait',
    'native_close_retries_without_idle_wait',
    'native_large_image_history_and_compaction', 'native_http_request_compression',
))
REQUIRED_REGRESSIONS = frozenset((
    'test_message_origins_restore_same_service_and_inline_cross_service_without_reasoning',
    'test_cross_provider_message_ids_are_removed_on_http_and_websocket',
    'test_unified_runtime_preserves_legacy_affinity_and_reuses_exact_bridge_core',
    'test_unified_feed_and_legacy_bridges_are_bound_to_one_core',
    'test_unified_settings_reopens_active_legacy_directory',
    'test_manual_unified_upgrade_keeps_baseline_service_affinity',
    'test_unified_old_ds_can_disable_deepseek_without_losing_other_preferences',
    'test_uninstall_discovers_latest_owned_build_and_preserves_backups',
    'test_uninstall_rejects_another_manager_without_changes',
    'test_unified_launcher_renames_only_verified_legacy_entry',
    'test_native_composer_quota_button_before_inference',
    'test_composer_quota_model_switch_keeps_gpt_and_other_submit_guards',
    'test_composer_interception_failure_keeps_pipe_until_native_exit',
    'test_real_validate_build_accepts_builtin_none_legacy_and_dormant_provider',
    'test_real_validate_build_rejects_missing_invalid_provider_and_mismatched_identity',
    'test_real_validate_build_keeps_digest_health_command_signature_and_probe_guards',
    'test_fresh_builtin_install_runs_real_validation_before_transaction_commit',
    'test_native_gate_requires_install_validation_before_marking_success',
    'test_anthropic_tools_images_usage_thinking_roundtrip_and_foreign_isolation',
    'test_anthropic_http_websocket_tools_and_auth_isolation',
    'test_anthropic_truncation_errors_and_output_limit_are_explicit',
    'test_anthropic_pagination_and_declared_maximum_effort_are_preserved',
    'test_setup_worker_progress_is_visible_and_does_not_claim_installation',
    'test_setup_cancel_reaps_real_owned_child_during_verification',
    'test_recovery_selects_owned_pending_candidate_and_refuses_crossed_identity',
    'test_bootstrap_atomic_copy_reuse_edition_isolation_and_tamper_rejection',

    'test_both_editions_share_first_setup_without_touching_native_config',
    'test_manager_conflict_rejected_before_credentials_or_network',
    'test_failed_discovery_restores_previous_private_key_and_draft',
    'test_failed_or_cancelled_prepare_cannot_enable_install',
    'test_prepare_rechecks_config_and_install_is_resumable',
    'test_first_setup_native_card_opens_and_closes_without_installed_state',
    'test_first_setup_recovers_commit_before_ui_draft_was_updated',
    'test_setup_uses_secret_controls_and_keeps_service_windows_when_filling_missing_specs',
    'test_private_request_diagnostics',
    'test_memory_bound_expiry_terminal_states_and_no_payload_fields',
    'test_user_service_and_inferred_metadata_are_distinguished_without_policy_changes',

    'test_codec_slow_pipe_writes_finish_and_midflight_cancel_reaps_child',
    'test_old_close_guard_is_fenced_and_new_controller_defers_unknown_activity',
    'test_new_installer_commits_idle_prepared_build',
    'test_tools_hold_idle_until_outputs_arrive_and_lease_is_atomic',
    'test_observation_never_limits_stream_and_unknown_holds_install',
    'test_idle_websocket_and_authenticated_drain_release',
    'test_order_fence_and_actual_counts_survive_old_writer',
    'test_one_click_wait_cancel_and_restart_keep_prepared_package',
    'test_unknown_activity_waits_for_exit_and_timeout_retains_build',
    'test_rollback_phase_is_terminal_until_explicit_retry',
    'test_runtime_startup_failure_restores_exact_old_tree',
    'test_runtime_checkpoint_precedes_swap_and_recovery_rejects_user_edits',
    'test_runtime_failure_before_commit_restores_service_and_config',
    'test_public_download_reports_bytes_with_and_without_length',

    'test_current_verified_runtime_needs_no_download_or_package_preflight',
    'test_unchanged_runtime_clears_stale_notice_without_restarting_update',
    'test_same_candidate_recheck_preserves_start_and_verified_build',
    'test_single_start_crosses_stability_wait_and_installs_once',
    'test_native_widgets_complete_private_snapshot_and_close_headlessly',
    'test_native_image_generate_and_edit_keep_official_auth_and_bytes',
    'test_new_native_http_services_are_opaque_and_stay_on_official_origin',
    'test_native_heartbeats_reach_client_during_silent_reasoning',
    'test_relay_forwards_control_frames_once_without_consuming_or_inventing_them',
    'test_deflate_negotiation_large_history_and_server_decline',
    'test_deflate_handles_rfc_vector_fragmentation_context_and_uncompressed_messages',
    'test_deflate_bounds_expanded_data_and_rejects_invalid_frames',
    'test_large_upload_fragments_allow_heartbeat_before_message_finishes',
    'test_official_close_and_external_error_status_reach_native_client_promptly',
    'test_native_streamed_body_still_cancels_when_client_disconnects',
    'test_recheck_keeps_same_release_build_for_close_and_launch',
    'test_launch_failure_releases_claim_and_allows_retry',
    'test_identity_poll_preserves_terminal_and_active_update_progress',
    'test_native_large_image_requests_and_compaction_keep_bytes_and_auth',
    'test_upstream_payload_rejection_is_returned_without_local_retries',
    'test_native_requests_can_exceed_message_limit_without_unbounding_upstream_replies',
    'test_native_compression_preserves_defaults_and_user_choices_across_updates',
    'test_native_policy_is_preserved_and_explicit_compaction_is_not_capped_at_ninety_percent',
    'test_external_slow_reasoning_uses_native_deadline_and_separate_auth',
    'test_native_zstd_roundtrip_invalid_data_and_cancellation',
    'test_provider_owns_reference_count_image_and_response_sizes',
    'test_native_widgets_complete_private_snapshot_and_close_headlessly',
    'test_native_update_card_badge_and_completion_share_the_settings_host',
    'test_native_window_loop_refreshes_and_closes_without_hiding_windows_gui',
    'test_summary_terminal_reasons_are_specific_and_do_not_leak_upstream_text',
    'test_truncated_summary_is_split_then_cached_only_after_full_recovery',
    'test_output_exhaustion_splits_only_failing_input_without_losing_utf8',
    'test_other_provider_failures_are_not_resubmitted_as_smaller_requests',
    'test_output_exhaustion_stops_at_small_input_and_respects_cancellation',
))
PLATFORM_REGRESSIONS = {
    'macos': {'test_native_carbon_queue_reaches_registered_shortcut_handler',
              'test_macos_update_and_rollback_wait_for_old_launchd_job'},
    'windows': {'test_explicit_exit_closes_owned_headless_backend_and_keeps_other_processes',
                'test_windows_production_host_survives_hide_reopen_and_unreadable_wake',
                'test_windows_routed_buttons_drag_and_tooltips_reach_real_handlers'},
}


def validate_coverage(coverage, edition, host):
    required = REQUIRED_NATIVE | ({'local_flattened_tools', 'native_chat_completions_lazy_tools',
                                  'native_chat_custom_tools_return_gpt','native_anthropic_tools_return_gpt'} if edition != 'ds' else set())
    if (host not in PLATFORM_REGRESSIONS or coverage.get('platform') != host
            or coverage.get('format') != 1 or coverage.get('kind') != 'native-client-with-synthetic-services'
            or any(coverage.get('native', {}).get(name) != 'passed' for name in required)
            or any(coverage.get('regressions', {}).get(name) != 'passed'
                   for name in REQUIRED_REGRESSIONS | PLATFORM_REGRESSIONS[host])):
        raise ValueError('Required release coverage is missing, skipped, or failed')


def git(*args, **kwargs):
    if os.name == 'nt':kwargs.setdefault('creationflags',0x08000000)
    return subprocess.check_output(["git", *args], cwd=ROOT, **kwargs)


def distribution(commit):
    """Build a source-only tree at historical paths for installed old updaters.

    Uses a temporary Git index; never checks out, resets or changes user files.
    The returned commit must be pushed with its release tag before feed use.
    """
    names=git("ls-tree","-r","--name-only",commit,text=True).splitlines()
    if "src/edition.json" not in names:
        return commit
    entries=[];seen=set()
    for name in names:
        if name in ("patch.py","monitor.py","update.json","unified-update.json"):
            continue
        target=name.removeprefix("src/")
        if target in seen:
            raise ValueError("Portable release path collision: "+target)
        seen.add(target)
        object_id=git("rev-parse",commit+":"+name,text=True).strip()
        if name in ('README.md','README.en.md'):
            raw=git('show',commit+':'+name).replace(b'src/brand.svg',b'brand.svg')
            object_id=git('hash-object','-w','--stdin',input=raw).decode().strip()
        entries.append("100644 "+object_id+"\t"+target+"\n")
    with tempfile.TemporaryDirectory(prefix="codex-release-index-") as temp:
        env={**os.environ,"GIT_INDEX_FILE":str(Path(temp)/"index")}
        git("read-tree","--empty",env=env)
        git("update-index","--index-info",env=env,input="".join(entries).encode())
        tree=git("write-tree",env=env,text=True).strip()
    return git("commit-tree",tree,"-p",commit,"-m","Package portable source for existing update clients",text=True).strip()


def create(commit,sequence):
    if not re.fullmatch(r"[0-9a-f]{40}",commit) or sequence<1:
        raise ValueError("Use a full committed source SHA and a positive release sequence")
    def read(name):return git("show",commit+":"+name)
    channel=json.loads(read("edition.json"))
    names=git("ls-tree","-r","--name-only",commit,text=True).splitlines()
    contents={name:read(name) for name in names if name!="update.json"}
    files={name:hashlib.sha256(raw).hexdigest() for name,raw in contents.items()}
    if not 8 <= len(files) <= 100:
        raise ValueError('Portable inventory must remain compatible with installed updaters (8-100 files)')
    return {"format":1,**channel,"sequence":sequence,"commit":commit,"files":files,"file_sizes":{name:len(raw) for name,raw in contents.items()},
            "archive": {'format': 'github-zip-v1',
                        'url': 'https://codeload.github.com/limyum-creator/codex-deepseek-patch/zip/' + commit}}


def _json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()


def _tree(commit):
    entries = {}
    for row in git('ls-tree', '-r', commit, text=True).splitlines():
        head, name = row.split('\t', 1)
        mode, kind, identity = head.split()
        if mode != '100644' or kind != 'blob':
            raise ValueError('Release sources must be ordinary files: ' + name)
        entries[name] = identity
    return entries


def _commit(entries, parents, message):
    with tempfile.TemporaryDirectory(prefix='codex-paired-index-') as temp:
        env = {**os.environ, 'GIT_INDEX_FILE': str(Path(temp) / 'index')}
        git('read-tree', '--empty', env=env)
        git('update-index', '--index-info', env=env,
            input=''.join('100644 ' + identity + '\t' + name + '\n'
                          for name, identity in sorted(entries.items())).encode())
        tree = git('write-tree', env=env, text=True).strip()
    arguments = ['commit-tree', tree]
    for parent in dict.fromkeys(parents):
        arguments += ['-p', parent]
    return git(*arguments, '-m', message, text=True).strip()


def _blob(raw):
    return git('hash-object', '-w', '--stdin', input=raw).decode().strip()


def core_digest(release):
    return hashlib.sha256(_json({name: digest for name, digest in release['files'].items()
        if name not in ('edition.json', 'README.md', 'README.en.md', 'update.json')})).hexdigest()


def candidate_id(value):
    return hashlib.sha256(_json({k: v for k, v in value.items() if k != 'id'})).hexdigest()


def prepare_pair(commit, output, versions, *, bases=None):
    """Produce both editions from one core, without changing a working tree/ref/feed."""
    commit = git('rev-parse', commit, text=True).strip()
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError('Candidate output must be empty')
    output.mkdir(parents=True, exist_ok=True)
    entries = _tree(commit)
    if 'src/edition.json' not in entries:
        raise ValueError('Paired candidates require the canonical source tree')
    value = {'format': 1, 'source_commit': commit, 'editions': {}}
    for channel, branch in LEGACY_CHANNELS.items():
        version = versions[channel]
        if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[\w.-]+)?', version):
            raise ValueError('Use a release version for each independent channel')
        base = git('rev-parse', (bases or {}).get(channel, 'origin/' + branch), text=True).strip()
        old = json.loads(git('show', base + ':src/edition.json'))
        previous = json.loads(git('show', base + ':update.json'))
        if old.get('edition') != channel or previous.get('edition') != channel:
            raise ValueError('The channel baseline has a different edition')
        if old['version'] == version:
            raise ValueError('A published version cannot be reused')
        tree = dict(entries)
        metadata = {**old, 'version': version, 'branch': branch, 'edition': channel}
        tree['src/edition.json'] = _blob(_json(metadata))
        tree['update.json'] = _blob(git('show', base + ':update.json'))
        # Both readmes follow the shared candidate, with edition-specific title
        # and entry description supplied by explicit edition blocks.
        for name in ('README.md', 'README.en.md'):
            text = git('show', commit + ':' + name).decode()
            source_version = json.loads(git('show', commit + ':src/edition.json'))['version']
            text = text.replace(source_version, version)
            if channel == 'ds':
                prior = git('show', base + ':' + name).decode()
                # Until a document declares shared edition blocks, preserve the
                # edition's existing introduction and use shared feature docs.
                if '<!-- EDITION:proxy -->' in text:
                    text = re.sub(r'<!-- EDITION:proxy -->.*?<!-- /EDITION -->', '', text, flags=re.S)
            else:
                text = re.sub(r'<!-- EDITION:ds -->.*?<!-- /EDITION -->', '', text, flags=re.S)
            text = re.sub(r'<!-- /?EDITION(?::\w+)? -->\n?', '', text)
            tree[name] = _blob(text.encode())
        source = _commit(tree, [base, commit], 'Prepare ' + channel + ' release ' + version + ' from shared core')
        portable = distribution(source)
        release = create(portable, previous['sequence'] + 1)
        release.update(source_commit=source, core_sha256=core_digest(release))
        feed_tree = _tree(source)
        feed_tree['update.json'] = _blob(_json(release))
        feed_commit = _commit(feed_tree, [source], 'Publish verified ' + channel + ' release ' + version)
        package = git('archive', '--format=zip', portable)
        (output / (channel + '.zip')).write_bytes(package)
        value['editions'][channel] = {'release': release, 'feed_commit': feed_commit,
            'expected_head': base, 'archive': channel + '.zip',
            'archive_sha256': hashlib.sha256(package).hexdigest()}
    if len({v['release']['core_sha256'] for v in value['editions'].values()}) != 1:
        raise ValueError('The two editions do not have identical shared code')
    value['id'] = candidate_id(value)
    (output / 'candidate.json').write_bytes(_json(value))
    return value


def prepare_unified(commit, output, version):
    """One product feed; immutable compatibility bridges for pre-unified clients."""
    commit=git('rev-parse',commit,text=True).strip()
    output=Path(output).resolve()
    if output.exists() and any(output.iterdir()):raise ValueError('Candidate output must be empty')
    if not re.fullmatch(r'\d+\.\d+\.\d+',version):raise ValueError('Use a unified release version')
    output.mkdir(parents=True,exist_ok=True)
    base=git('rev-parse','origin/main',text=True).strip()
    original=_tree(base);bridges=None
    if 'unified-update.json' in original:
        previous=json.loads(git('show',base+':unified-update.json'))
        if previous['version']==version:raise ValueError('A published version cannot be reused')
        sequence=previous['sequence']+1
        legacy_feed=git('show',base+':update.json')
    else:
        bridges=prepare_pair(commit,output/'legacy',{'ds':version,'proxy':version})
        sequence=max(v['release']['sequence'] for v in bridges['editions'].values())+1
        legacy_feed=_json(bridges['editions']['ds']['release'])
    tree=_tree(commit)
    tree['src/edition.json']=_blob(_json({'edition':'unified','branch':'main','version':version,'architecture':'official-client-v1'}))
    tree['update.json']=_blob(legacy_feed)
    old_version=json.loads(git('show',commit+':src/edition.json'))['version']
    for name in ('README.md','README.en.md'):
        tree[name]=_blob(git('show',commit+':'+name).decode().replace(old_version,version).encode())
    source=_commit(tree,[base,commit],'Prepare unified release '+version)
    portable=distribution(source);release=create(portable,sequence)
    release.update(source_commit=source,core_sha256=core_digest(release))
    if bridges and any(v['release']['core_sha256']!=release['core_sha256'] for v in bridges['editions'].values()):
        raise ValueError('Legacy bridge and unified runtime differ')
    feed_tree=_tree(source);feed_tree['unified-update.json']=_blob(_json(release))
    feed=_commit(feed_tree,[source],'Publish unified release '+version)
    raw=git('archive','--format=zip',portable);(output/'unified.zip').write_bytes(raw)
    value={'format':1,'kind':'unified','source_commit':commit,'editions':{'unified':{
        'release':release,'feed_commit':feed,'expected_head':base,'archive':'unified.zip',
        'archive_sha256':hashlib.sha256(raw).hexdigest()}},'bridges':bridges}
    value['id']=candidate_id(value);(output/'candidate.json').write_bytes(_json(value))
    return value


def read_candidate(path):
    value = json.loads(Path(path).read_bytes())
    if value.get('format') != 1 or value.get('id') != candidate_id(value) or set(value.get('editions', {})) not in (set(LEGACY_CHANNELS), {'unified'}):
        raise ValueError('Invalid candidate identity')
    for channel, item in value['editions'].items():
        release = item['release']
        if release.get('edition') != channel or release.get('branch') != CHANNELS[channel] or core_digest(release) != release['core_sha256']:
            raise ValueError('Candidate channel/core identity changed')
    if len({v['release']['core_sha256'] for v in value['editions'].values()}) != 1:
        raise ValueError('Candidate editions have different cores')
    bridges=value.get('bridges')
    if bridges:
        if (bridges.get('id')!=candidate_id(bridges) or set(bridges.get('editions',{}))!=set(LEGACY_CHANNELS)
                or any(core_digest(v['release'])!=value['editions']['unified']['release']['core_sha256'] for v in bridges['editions'].values())):
            raise ValueError('Invalid legacy bridge identity')
    return value


def _windows_unit_check(work, app, env, log_path):
    """Run portable tests in the registered package, without a console/elevation."""
    import base64
    from official_client_shortcut import official_entry, _powershell
    entry=official_entry(app)
    family,app_id=entry['app_user_model_id'].split('!',1)
    python=Path(sys.executable).with_name('pythonw.exe')
    if not python.is_file():raise ValueError('Windowless Python is required for Windows UI validation')
    driver=work/'package-unit-driver.py';done=work/'package-unit-exit.json';started=work/'package-unit-start.json'
    # A retry in the same private work directory must await this run's worker.
    done.unlink(missing_ok=True)
    started.unlink(missing_ok=True)
    selected={k:env[k] for k in ('CODEX_COMPOSER_APP','CODEX_COMPOSER_FIXTURE','CODEX_COMPOSER_EDITION','CODEX_TEST_COMPRESSION_NODE') if k in env}
    driver.write_text('import os,sys,json,unittest,traceback\nfrom pathlib import Path\n'
        'def main():\n'
        ' os.chdir('+repr(str(work))+');sys.path.insert(0,'+repr(str(work))+')\n'
        ' os.environ.update('+repr(selected)+')\n'
        ' Path('+repr(str(started))+').write_text(json.dumps({"pid":os.getpid()}))\n'
        ' with open('+repr(str(log_path))+',"w",encoding="utf-8") as log:\n'
        '  sys.stdout=sys.stderr=log\n'
        '  try:\n'
        '   result=unittest.main(module=None,argv=["unittest","discover","-s","tests","-v"],exit=False).result\n'
        '   code=0 if result.wasSuccessful() else 1\n'
        '  except BaseException:\n'
        '   traceback.print_exc();code=1\n'
        ' tmp=Path('+repr(str(done)+'.tmp')+');tmp.write_text(json.dumps({"returncode":code}));tmp.replace('+repr(str(done))+')\n'
        'if __name__=="__main__":main()\n',encoding='utf-8')
    value={'family':family,'app_id':app_id,'python':str(python),'args':subprocess.list2cmdline([str(driver)])}
    encoded=base64.b64encode(json.dumps(value).encode()).decode()
    script=r'''$ErrorActionPreference='Stop';[Console]::OutputEncoding=[Text.UTF8Encoding]::new();
$v=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__VALUES__'))|ConvertFrom-Json;
Invoke-CommandInDesktopPackage -PackageFamilyName $v.family -AppId $v.app_id -Command $v.python -Args $v.args;
@{ok=$true}|ConvertTo-Json -Compress'''
    _powershell(script.replace('__VALUES__',encoded))
    deadline=time.monotonic()+600
    while time.monotonic()<deadline:
        if done.is_file():return json.loads(done.read_text())['returncode'],log_path.read_bytes()
        time.sleep(.25)
    if started.is_file():
        pid=json.loads(started.read_text())['pid']
        value={'pid':pid,'python':str(python),'driver':str(driver)}
        encoded=base64.b64encode(json.dumps(value).encode()).decode()
        _powershell(r'''$v=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__VALUES__'))|ConvertFrom-Json;
$p=Get-CimInstance Win32_Process -Filter ('ProcessId='+[int]$v.pid);
if($p.ExecutablePath -ieq $v.python -and $p.CommandLine.Contains($v.driver)){Stop-Process -Id $p.ProcessId};
@{ok=$true}|ConvertTo-Json -Compress'''.replace('__VALUES__',encoded))
    raise ValueError('Windows package unit validation timed out')


def verify_candidate(path, channel, app, output):
    """Run actual portable tests/native CLI on this host; emit a content-bound receipt."""
    value = read_candidate(path)
    item = value['editions'][channel]
    host = {'Darwin': 'macos', 'Windows': 'windows'}.get(platform.system())
    if host is None:
        raise ValueError('Native release receipts require macOS or Windows')
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    raw = (Path(path).parent / item['archive']).read_bytes()
    if hashlib.sha256(raw).hexdigest() != item['archive_sha256']:
        raise ValueError('Candidate archive changed')
    with tempfile.TemporaryDirectory(prefix='codex-candidate-') as temp, ExitStack() as owned:
        work = Path(temp)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if set(archive.namelist()) - {n for n in archive.namelist() if n.endswith('/')} != set(item['release']['files']):
                raise ValueError('Candidate inventory changed')
            for name, digest in item['release']['files'].items():
                if Path(name).is_absolute() or '..' in Path(name).parts or '\\' in name:
                    raise ValueError('Unsafe candidate path')
                data = archive.read(name)
                if hashlib.sha256(data).hexdigest() != digest:
                    raise ValueError('Candidate file changed')
                destination = work / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
        env = {**os.environ, 'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost'}
        fixture=owned.enter_context(tempfile.TemporaryDirectory(prefix='cq-'))
        env.update(CODEX_COMPOSER_APP=str(Path(app).resolve()),CODEX_COMPOSER_FIXTURE=fixture,
                   CODEX_COMPOSER_EDITION=channel)
        codec=Path(app)/('Contents/Resources/cua_node/bin/node' if host=='macos' else 'resources/cua_node/bin/node.exe')
        if codec.is_file():env['CODEX_TEST_COMPRESSION_NODE']=str(codec)
        options = {'creationflags': 0x08000000} if os.name == 'nt' else {}
        logs = {}
        prepared=work/'first-install-build'
        (work/'first-install-home').mkdir()
        connection_args=[]
        if channel!='ds':
            import http.server,threading
            class Metadata(http.server.BaseHTTPRequestHandler):
                def log_message(self,*args):pass
                def do_GET(self):
                    if self.path!='/v1/models':self.send_error(404);return
                    raw=b'{"data":[{"id":"validator-fixture","context_window":262144,"supports_tools":true}]}'
                    self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
                def do_POST(self):self.send_error(503,'Inference is disabled in this metadata fixture')
            server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Metadata)
            owned.callback(server.server_close);owned.callback(server.shutdown)
            threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.02},daemon=True).start()
            connections=work/'first-install-connections.json'
            connections.write_bytes(_json({'locals':[{'id':'local-validator','kind':'openai',
                'base_url':'http://127.0.0.1:'+str(server.server_port)+'/v1','models':['validator-fixture']}]}))
            connection_args=['--connections',str(connections)]
        for name, arguments in (
            ('unit', ['-m', 'unittest', 'discover', '-s', 'tests', '-v']),
            ('native', ['scripts/test_official_client.py', '--app', str(Path(app).resolve()),
                        '--edition', channel, '--services', '--reportdir', str(work / 'native-report')]),
            ('prepared-build', ['patch.py','build','--app',str(Path(app).resolve()),
                '--codex-home',str(work/'first-install-home'),'--output',str(prepared),'--bundled-catalog',
                '--with-deepseek' if channel=='ds' else '--without-deepseek','--verify',*connection_args]),
        ):
            if name=='unit' and host=='windows':
                returncode,data=_windows_unit_check(work,app,env,output/(host+'-'+channel+'-unit.log'))
            else:
                check_env=env if name=='unit' else {k:v for k,v in env.items() if not k.startswith('CODEX_COMPOSER_')}
                completed = subprocess.run([sys.executable, *arguments], cwd=work, env=check_env,
                    capture_output=True, timeout=600, **options)
                returncode,data=completed.returncode,completed.stdout+completed.stderr
            (output / (host + '-' + channel + '-' + name + '.log')).write_bytes(data)
            if returncode:
                raise ValueError(name + ' validation failed; see the private validation log')
            logs[name] = hashlib.sha256(data).hexdigest()
        manifest=json.loads((prepared/'manifest.json').read_bytes())
        if manifest.get('native_install_validation')!='passed' or manifest.get('native_config',{}).get('provider') is not None:
            raise ValueError('Fresh builtin build did not pass real installation validation')
        native = json.loads((work / 'native-report' / ('official-client-' + channel + '-result.json')).read_bytes())
        native['checks']['prepared_build_install_validation']='passed'
        unit_log = (output / (host + '-' + channel + '-unit.log')).read_text(encoding='utf-8', errors='replace')
        passed = set(re.findall(r'^(test_\w+) \([^\n]+\) \.\.\. ok$', unit_log, re.MULTILINE))
        coverage = {'format': 1, 'kind': 'native-client-with-synthetic-services', 'platform': host,
                    'native': native.get('checks', {}),
                    'regressions': {name: 'passed' for name in
                                    (REQUIRED_REGRESSIONS | PLATFORM_REGRESSIONS[host]) & passed}}
        validate_coverage(coverage, channel, host)
        data = _json(coverage)
        (output / (host + '-' + channel + '-coverage.json')).write_bytes(data)
        logs['coverage'] = hashlib.sha256(data).hexdigest()
        receipt = {'format': 1, 'candidate_id': value['id'], 'platform': host, 'edition': channel,
            'commit': item['release']['commit'], 'core_sha256': item['release']['core_sha256'],
            'status': 'passed', 'checks': {'unit': 'passed', 'native': 'passed', 'install_validation':'passed'}, 'logs': logs}
        if os.name == 'nt':
            import ctypes
            receipt['execution'] = {'console': int(ctypes.windll.kernel32.GetConsoleWindow()),
                                    'admin': bool(ctypes.windll.shell32.IsUserAnAdmin())}
            if receipt['execution'] != {'console': 0, 'admin': False}:
                raise ValueError('Windows release validation must run windowless as the normal user')
        (output / (host + '-' + channel + '.json')).write_bytes(_json(receipt))
    return receipt


def require_receipts(candidate, directory):
    receipts = []
    for host, channel in sorted((h,c) for h in ('macos','windows') for c in candidate['editions']):
        path = Path(directory) / (host + '-' + channel + '.json')
        if not path.is_file():
            raise ValueError('Missing validation: ' + host + '/' + channel)
        receipt = json.loads(path.read_bytes())
        release = candidate['editions'][channel]['release']
        expected = {'candidate_id': candidate['id'], 'platform': host, 'edition': channel,
                    'commit': release['commit'], 'core_sha256': release['core_sha256'], 'status': 'passed'}
        if any(receipt.get(k) != v for k, v in expected.items()) or receipt.get('checks') != {'unit': 'passed', 'native': 'passed', 'install_validation':'passed'}:
            raise ValueError('Validation belongs to another candidate or is incomplete: ' + str(path.name))
        if host == 'windows' and receipt.get('execution') != {'console': 0, 'admin': False}:
            raise ValueError('Windows validation did not use the ordinary hidden execution context')
        for name in ('unit', 'native', 'prepared-build', 'coverage'):
            log = Path(directory) / (host + '-' + channel + '-' + name + ('.json' if name == 'coverage' else '.log'))
            if not log.is_file() or hashlib.sha256(log.read_bytes()).hexdigest() != receipt.get('logs', {}).get(name):
                raise ValueError('Validation evidence is missing or changed')
            if name == 'coverage':
                validate_coverage(json.loads(log.read_bytes()), channel, host)
        receipts.append(receipt)
    return receipts


def promotion_commands(path, receipts):
    candidate = read_candidate(path)
    require_receipts(candidate, receipts)
    references = []
    for channel, item in candidate['editions'].items():
        branch=CHANNELS[channel]
        if git('rev-parse', 'origin/' + branch, text=True).strip() != item['expected_head']:
            raise ValueError('A published channel advanced; prepare and verify a new candidate')
        references += [item['release']['commit'] + ':refs/tags/v' + item['release']['version'],
                       item['feed_commit'] + ':refs/heads/' + branch]
    if candidate.get('bridges'):
        for channel,item in candidate['bridges']['editions'].items():
            branch=LEGACY_CHANNELS[channel]
            if git('rev-parse','origin/'+branch,text=True).strip()!=item['expected_head']:
                raise ValueError('Legacy channel advanced; rebuild the migration candidate')
            references.append(item['release']['commit']+':refs/tags/bridge-'+channel+'-v'+item['release']['version'])
            if channel=='proxy':references.append(item['feed_commit']+':refs/heads/'+branch)
    return ['git', '-c', 'http.version=HTTP/1.1', 'push', '--atomic', 'origin', *references]


def promote(path, receipts, *, execute=False):
    command = promotion_commands(path, receipts)
    if not execute:
        return {'ready': True, 'command': command, 'published': False}
    candidate = read_candidate(path)
    env = {k: v for k, v in os.environ.items() if k.lower() not in ('http_proxy', 'https_proxy', 'all_proxy')}
    runs = json.loads(subprocess.check_output(['gh', 'api',
        'repos/limyum-creator/codex-deepseek-patch/actions/runs?head_sha=' + candidate['source_commit']],
        env=env, timeout=45))['workflow_runs']
    matching = [v for v in runs if v.get('path') == '.github/workflows/tests.yml' and v.get('head_sha') == candidate['source_commit']]
    if not matching or max(matching, key=lambda v: v['run_number']).get('conclusion') != 'success':
        raise ValueError('The candidate source CI must pass before either channel is published')
    subprocess.run(command, cwd=ROOT, env=env, check=True, timeout=180)
    return {'published': True, 'candidate_id': candidate['id']}


# Optional first-install bundles are release assets, never portable feed files.
# Pins are vendor artifacts; changing one requires a new installer validation.
PYTHON_RUNTIMES = {
    'macos-arm64': ('https://github.com/astral-sh/python-build-standalone/releases/download/20261021/cpython-3.13.17%2B20261021-aarch64-apple-darwin-install_only.tar.gz', '9c985cbf327425873469896ce748c7134b6deca8edb81e93c589ad2fc5aff445'),
    'macos-x64': ('https://github.com/astral-sh/python-build-standalone/releases/download/20261021/cpython-3.13.17%2B20261021-x86_64-apple-darwin-install_only.tar.gz', 'b7d7c2dd63545cabe996191b6f20f0b652c7de123d9ca451dda7e5f1ebbcbbbc'),
    'windows-x64': ('https://www.python.org/ftp/python/3.13.17/python-3.13.17-embed-amd64.zip', '9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38'),
    'windows-arm64': ('https://www.python.org/ftp/python/3.13.17/python-3.13.17-embed-arm64.zip', '7b58787a047b400edbfcb539aa3cdb4e2ed77bf0f22cae42b7c01cedd7dd6614'),
}


def bundle_inventory(root):
    root=Path(root).resolve();result={}
    for path in sorted(root.rglob('*')):
        if '__pycache__' in path.relative_to(root).parts:continue
        name=path.relative_to(root).as_posix()
        if path.is_symlink():
            if not path.resolve().is_relative_to(root):raise ValueError('Runtime link escapes installer')
            result[name]={'link':os.readlink(path)}
        elif path.is_file():
            result[name]={'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                          'executable':bool(path.stat().st_mode & 0o111)}
        elif not path.is_dir():raise ValueError('Unsupported installer entry')
    return result


def verify_bootstrap(root):
    root=Path(root).resolve()
    value=json.loads((root/'bundle.json').read_bytes())
    if value.get('format')!=1 or value.get('edition') not in CHANNELS or value.get('target') not in PYTHON_RUNTIMES:
        raise ValueError('Invalid installer identity')
    expected=value['files'];actual=bundle_inventory(root/'payload')
    # NTFS does not retain POSIX executable bits; contents and link targets do.
    if os.name=='nt':
        expected={k:{a:b for a,b in v.items() if a!='executable'} for k,v in expected.items()}
        actual={k:{a:b for a,b in v.items() if a!='executable'} for k,v in actual.items()}
    if actual!=expected:raise ValueError('Installer files changed')
    channel=json.loads((root/'payload/source/edition.json').read_bytes())
    if channel['edition']!=value['edition'] or channel['version']!=value['version']:raise ValueError('Installer channel changed')
    return value


def build_bootstrap(source, runtime, target, destination):
    """Build offline native launchers from a portable candidate and pinned Python."""
    import shutil, tarfile, plistlib
    source,runtime,destination=map(Path,(source,runtime,destination))
    if target not in PYTHON_RUNTIMES:raise ValueError('Unsupported installer target')
    url,digest=PYTHON_RUNTIMES[target]
    if hashlib.sha256(runtime.read_bytes()).hexdigest()!=digest:raise ValueError('Python archive digest mismatch')
    channel=json.loads((source/'edition.json').read_bytes())
    if channel.get('edition') not in CHANNELS:raise ValueError('Portable source edition required')
    if destination.exists():raise ValueError('Use a new installer directory')
    destination.mkdir(parents=True)
    try:
        mac=target.startswith('macos-')
        app=destination/('Codex Model Setup.app' if mac else 'Codex Model Setup')
        root=app/'Contents/Resources' if mac else app
        payload=root/'payload';payload.mkdir(parents=True)
        # Strict portable inventory: no credentials, untracked files or user data.
        from pathlib import PurePosixPath
        def safe_release_path(name):
            path=PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name or '.git' in path.parts:raise ValueError('Unsafe installer path')
        names=json.loads((source/'.package.json').read_bytes())['release']['files'] if (source/'.package.json').is_file() else None
        if names is None:raise ValueError('A verified portable package marker is required')
        src=payload/'source';src.mkdir()
        for name,expected in names.items():
            safe_release_path(name)
            path=source/name
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:raise ValueError('Portable source digest mismatch')
            out=src/name;out.parent.mkdir(parents=True,exist_ok=True);out.write_bytes(path.read_bytes())
        rt=payload/'runtime';rt.mkdir()
        if mac:
            with tarfile.open(runtime) as archive:archive.extractall(rt,filter='data')
            executable='runtime/python/bin/python3.13'
        else:
            with zipfile.ZipFile(runtime) as archive:
                for entry in archive.infolist():
                    safe_release_path(entry.filename)
                    if entry.is_dir():continue
                    out=rt/entry.filename;out.parent.mkdir(parents=True,exist_ok=True);out.write_bytes(archive.read(entry))
            # Keep isolated stdlib lookup, but follow the invoked script after updates.
            (rt/'python313._pth').write_text('python313.zip\n.\nimport site\n',encoding='utf-8')
            (rt/'sitecustomize.py').write_text(
                "import os, sys\nsys.dont_write_bytecode=True\nentry=sys.argv[0] if sys.argv else ''\nsys.path.insert(0,os.path.dirname(os.path.abspath(entry)) if entry and not entry.startswith('-') else os.getcwd())\n",encoding='utf-8')
            executable='runtime/pythonw.exe'
        if not (payload/executable).is_file():raise ValueError('Python entry is missing')
        files=bundle_inventory(payload)
        metadata={'format':1,**{k:channel[k] for k in ('edition','version')},'target':target,
                  'runtime':{'url':url,'sha256':digest,'python':'3.13.17'},'python':executable,'files':files}
        (root/'bundle.json').write_bytes(_json(metadata))
        label='Unified'
        if mac:
            macos=app/'Contents/MacOS';macos.mkdir()
            entry=macos/'setup'
            entry.write_text('#!/bin/sh\nset -eu\nroot="$(CDPATH= cd -- "$(dirname -- "$0")/../Resources" && pwd)"\nexport PYTHONDONTWRITEBYTECODE=1\nexec "$root/payload/'+executable+'" -B "$root/payload/source/scripts/release_manifest.py" --bootstrap-install "$root"\n',encoding='utf-8');entry.chmod(0o755)
            (app/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':'com.codex-model-setup.'+channel['edition'],
                'CFBundleName':'Codex Model Setup · '+label,'CFBundleExecutable':'setup','CFBundlePackageType':'APPL',
                'CFBundleVersion':channel['version'],'LSUIElement':True,'NSHighResolutionCapable':True}))
        else:
            # WScript is a windowless entry point; no console or policy changes.
            (app/'Open Setup.vbs').write_text('Option Explicit\r\nDim shell, fs, root, q, command\r\nSet shell = CreateObject("WScript.Shell")\r\nSet fs = CreateObject("Scripting.FileSystemObject")\r\nroot = fs.GetParentFolderName(WScript.ScriptFullName)\r\nq = Chr(34)\r\ncommand = q & root & "\\payload\\runtime\\pythonw.exe" & q & " -B " & q & root & "\\payload\\source\\scripts\\release_manifest.py" & q & " --bootstrap-install " & q & root & q\r\nshell.Environment("PROCESS")("PYTHONDONTWRITEBYTECODE") = "1"\r\nshell.Run command, 0, False\r\n',encoding='utf-8')
        (destination/'README.txt').write_text('Codex 模型补丁首次设置 · '+label+'\n解压后打开 Codex Model Setup。运行时随包携带，不修改系统 Python。\n这是补丁设置，不是 Codex 更新；不包含官方应用。\nWindows 使用 Open Setup.vbs；若系统禁用 WScript，可运行 payload/runtime/python.exe payload/source/scripts/release_manifest.py --bootstrap-install .\nmacOS 公共分发包需完成自身签名和公证；请核对发布者。\n运行时许可证随 payload/runtime 保留；Python 来源与 SHA256 记录于 bundle.json。\n',encoding='utf-8')
        verify_bootstrap(root)
        return root
    except BaseException:
        shutil.rmtree(destination)
        raise


def bootstrap_install(root, *, base=None, launch=True):
    """Stage a self-contained launcher; native configuration changes remain in setup."""
    import shutil
    sys.path.insert(0,str(SOURCE))
    from monitor import private_directory, state_lock
    root=Path(root).resolve();value=verify_bootstrap(root)
    host='windows' if sys.platform=='win32' else 'macos' if sys.platform=='darwin' else 'unsupported'
    cpu='arm64' if platform.machine().lower() in ('arm64','aarch64') else 'x64'
    if value['target']!=host+'-'+cpu:raise ValueError('Installer architecture does not match this computer')
    if base is None:
        base=(Path(os.environ['LOCALAPPDATA']) if host=='windows' else Path.home()/'Library/Application Support')/'CodexDeepSeekPatch'
    base=private_directory(Path(base).resolve())
    identity=hashlib.sha256((root/'bundle.json').read_bytes()).hexdigest()
    parent=private_directory(base/'installers'/value['edition']);destination=parent/identity
    with state_lock(parent) as locked:
        if not locked:raise ValueError('Another installer is preparing; retry shortly')
        if destination.exists():verify_bootstrap(destination)
        else:
            with tempfile.TemporaryDirectory(prefix='.prepare-',dir=parent) as temporary:
                stage=Path(temporary)/'bundle';stage.mkdir()
                shutil.copytree(root/'payload',stage/'payload',symlinks=True)
                shutil.copy2(root/'bundle.json',stage/'bundle.json')
                verify_bootstrap(stage);os.replace(stage,destination)
    executable=destination/'payload'/value['python'];controller=destination/'payload/source/management.py'
    if launch:
        state=base/('monitor-'+value['edition'])
        for candidate in (base/'monitor',state):
            settings=candidate/'settings.json'
            if settings.is_file() and json.loads(settings.read_bytes()).get('edition')==value['edition']:
                state=candidate;break
        command=[str(executable),'-B',str(controller),'show','--state-dir',str(state)]
        subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                         env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'},
                         **({'creationflags':0x08000000} if os.name=='nt' else {'start_new_session':True}))
    return destination


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap-install",type=Path)
    parser.add_argument("--bootstrap-source",type=Path)
    parser.add_argument("--runtime-archive",type=Path)
    parser.add_argument("--target",choices=PYTHON_RUNTIMES)
    parser.add_argument("--commit")
    parser.add_argument("--sequence",type=int)
    parser.add_argument("--distribution",action="store_true",help="Build a portable source commit from src/")
    parser.add_argument("--output",type=Path,default=ROOT/"update.json")
    parser.add_argument('--prepare-pair', type=Path, metavar='DIRECTORY')
    parser.add_argument('--prepare-unified', type=Path)
    parser.add_argument('--version')
    parser.add_argument('--ds-version')
    parser.add_argument('--proxy-version')
    parser.add_argument('--verify-candidate', type=Path)
    parser.add_argument('--edition', choices=CHANNELS)
    parser.add_argument('--app', type=Path)
    parser.add_argument('--promote', type=Path)
    parser.add_argument('--receipts', type=Path)
    parser.add_argument('--execute', action='store_true', help='Publish only after bound four-variant receipts and CI pass')
    args=parser.parse_args()
    if args.bootstrap_install:
        try:bootstrap_install(args.bootstrap_install)
        except Exception:
            message='无法打开补丁设置。请核对下载包完整性、系统架构和当前用户文件权限，再重新打开安装包。'
            if sys.platform=='win32':__import__('ctypes').windll.user32.MessageBoxW(None,message,'Codex 模型补丁',0x10)
            elif sys.platform=='darwin':subprocess.run(['/usr/bin/osascript','-e','on run argv\ndisplay alert "Codex 模型补丁" message (item 1 of argv)\nend run',message],check=False)
            raise SystemExit(1)
        raise SystemExit(0)
    if args.bootstrap_source:
        if not args.runtime_archive or not args.target or args.output==ROOT/"update.json":parser.error("Installer needs source, runtime, target and new output directory")
        print(build_bootstrap(args.bootstrap_source,args.runtime_archive,args.target,args.output))
        raise SystemExit(0)
    if args.prepare_unified:
        if not args.commit or not args.version:parser.error('Unified preparation needs a commit and version')
        print(json.dumps(prepare_unified(args.commit,args.prepare_unified,args.version),indent=2))
        raise SystemExit(0)
    if args.prepare_pair:
        if not args.commit or not args.ds_version or not args.proxy_version:parser.error('Pair preparation needs a commit and both versions')
        print(json.dumps(prepare_pair(args.commit, args.prepare_pair, {'ds': args.ds_version, 'proxy': args.proxy_version}), indent=2))
        raise SystemExit(0)
    if args.verify_candidate:
        if not args.edition or not args.app or args.output == ROOT/'update.json':parser.error('Verification needs edition, app, and a private output directory')
        print(json.dumps(verify_candidate(args.verify_candidate, args.edition, args.app, args.output)))
        raise SystemExit(0)
    if args.promote:
        if not args.receipts:parser.error('Promotion needs four-variant validation receipts')
        print(json.dumps(promote(args.promote, args.receipts, execute=args.execute)))
        raise SystemExit(0)
    if not args.commit or args.sequence is None:parser.error('Use --commit and --sequence, or a candidate operation')
    commit=distribution(args.commit) if args.distribution else args.commit
    value=create(commit,args.sequence)
    if commit!=args.commit:value["source_commit"]=args.commit
    args.output.write_text(json.dumps(value,indent=2)+"\n")
    print(value["edition"],value["version"],"sequence",value["sequence"],"commit",value["commit"])
