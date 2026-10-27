import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from official_client_config import PROVIDER
from official_client_migration import migrate, rollback


class FakeNative:
    def __init__(self):
        self.threads = {str(i):{'id':str(i), 'cwd':'/original', 'name':'chat '+str(i),
                              'createdAt':1, 'updatedAt':2, 'modelProvider':provider}
                        for i, provider in enumerate(['openai','deepseek-desktop','openai','unmanaged'])}
        self.archived = {'2'}
        for row in self.threads.values():
            row.update(model='deepseek-flash' if row['id'] == '2' else 'gpt-6.1-sol',
                       reasoningEffort='low' if row['id'] == '2' else 'high')
        self.calls = []
        self.fail_once = None

    def request(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if self.fail_once == (method, params.get('threadId')):
            self.fail_once = None
            raise RuntimeError('Synthetic native error')
        if method == 'thread/list':
            return {'data':[copy.deepcopy(t) for t in self.threads.values()
                            if (t['id'] in self.archived) == params['archived']
                            and t['modelProvider'] in params['modelProviders']], 'nextCursor':None}
        tid = params['threadId']
        if method == 'thread/unarchive':
            self.archived.discard(tid)
        elif method == 'thread/archive':
            self.archived.add(tid)
        elif method == 'thread/resume':
            if tid in self.archived:
                raise RuntimeError('Archived thread')
            self.threads[tid]['modelProvider'] = params['modelProvider']
            self.threads[tid]['cwd'] = params.get('cwd', '/initial-rollout-cwd')
            self.threads[tid]['model'] = params.get('model', 'gpt-6.1-sol')
            self.threads[tid]['reasoningEffort'] = params.get('config', {}).get('model_reasoning_effort', 'high')
            return {'thread':copy.deepcopy(self.threads[tid]), 'modelProvider':params['modelProvider']}
        elif method == 'thread/read':
            return {'thread':copy.deepcopy(self.threads[tid])}
        elif method != 'thread/unsubscribe':
            raise AssertionError('Migration must not generate a turn or compact history')
        return {}


class MigrationTest(unittest.TestCase):
    def test_native_identity_upgrade_only_touches_legacy_adapter_and_can_roll_back(self):
        native = FakeNative()
        native.threads['1']['modelProvider'] = PROVIDER
        native.threads['2']['modelProvider'] = PROVIDER
        before = copy.deepcopy(native.threads)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'native-identity.json'
            result = migrate(native, {PROVIDER, 'openai'}, path, target='openai')
            self.assertEqual(result['migrated_chats'], 2)
            self.assertEqual(native.threads['0'], before['0'])
            self.assertEqual(native.threads['3'], before['3'])
            self.assertEqual(native.archived, {'2'})
            self.assertTrue(all(native.threads[t]['modelProvider']=='openai' for t in ('1','2')))
            rollback(native, path)
            self.assertEqual(native.threads, before)

    def test_unset_model_and_effort_resolve_natively_without_blocking_migration_or_recovery(self):
        import json
        native=FakeNative()
        native.threads['2'].update(model=None,reasoningEffort=None)
        before=copy.deepcopy(native.threads)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'migration.json'
            migrate(native,{'openai','deepseek-desktop'},path)
            snapshot=next(row for row in json.loads(path.read_text())['threads'] if row['id']=='2')
            self.assertIsNone(snapshot['metadata']['model'])
            self.assertIsNone(snapshot['metadata']['reasoningEffort'])
            self.assertEqual(native.threads['2']['model'],'gpt-6.1-sol')
            rollback(native,path)
            self.assertEqual(native.threads['2']['modelProvider'],before['2']['modelProvider'])
            self.assertEqual(native.archived,{'2'})
            from official_client_migration import metadata_matches
            for tid in before:self.assertTrue(metadata_matches(before[tid],native.threads[tid]))

    def test_only_unset_choices_may_resolve_and_all_other_metadata_remains_strict(self):
        from official_client_migration import metadata_matches
        before={'id':'chat','model':None,'reasoningEffort':None,'cwd':'/original','recencyAt':3}
        actual={**before,'model':'gpt-current','reasoningEffort':'high'}
        self.assertTrue(metadata_matches(before,actual,resolved_defaults={'model':'gpt-current','reasoningEffort':'high'}))
        self.assertFalse(metadata_matches(before,actual,resolved_defaults={'model':'another','reasoningEffort':'high'}))
        for field,value in (('cwd','/other'),('recencyAt',4),('reasoningEffort','not-valid')):
            self.assertFalse(metadata_matches(before,{**actual,field:value}))
        self.assertFalse(metadata_matches({**before,'model':'chosen'},actual))
        self.assertFalse(metadata_matches({**before,'reasoningEffort':'low'},actual))

    def test_preserves_history_metadata_archive_and_unmanaged_provider(self):
        native = FakeNative()
        before = copy.deepcopy(native.threads)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'migration.json'
            result = migrate(native, {'openai','deepseek-desktop'}, path)
            self.assertEqual(result['migrated_chats'], 3)
            self.assertEqual(result['inference_turns'], 0)
            self.assertEqual(native.archived, {'2'})
            self.assertEqual(native.threads['3'], before['3'])
            self.assertTrue(all(native.threads[str(i)]['modelProvider'] == PROVIDER for i in range(3)))
            call_count = len(native.calls)
            migrate(native, {'openai','deepseek-desktop'}, path)
            self.assertEqual(len(native.calls), call_count)
            rollback(native, path)
            self.assertEqual(native.threads, before)
            self.assertEqual(native.archived, {'2'})

    def test_windows_directory_aliases_compare_equally_without_ignoring_folder_changes(self):
        from official_client_migration import _digest
        slash=chr(92)
        path='C:'+slash+'Users'+slash+'editor'+slash+'Work'
        normal={'id':'synthetic','cwd':path,'name':'same','model':'model'}
        duplicate={**normal,'cwd':path.replace(slash,slash+slash)}
        verbatim={**normal,'cwd':slash+slash+'?'+slash+path}
        self.assertEqual(_digest(normal),_digest(duplicate))
        self.assertEqual(_digest(normal),_digest(verbatim))
        self.assertNotEqual(_digest(normal),_digest({**normal,'cwd':path+'2'}))
        self.assertNotEqual(_digest(normal),_digest({**normal,'name':'changed'}))

    def test_archived_chat_keeps_workspace_moved_after_original_rollout(self):
        native=FakeNative();native.threads['2']['cwd']='/later-moved-workspace'
        before=copy.deepcopy(native.threads)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'migration.json'
            migrate(native,{'openai','deepseek-desktop'},path)
            self.assertEqual(native.threads['2']['cwd'],'/later-moved-workspace')
            rollback(native,path)
            self.assertEqual(native.threads,before)
            self.assertEqual(native.archived,{'2'})

    def test_failure_restores_archive_and_can_rollback_completed_mutations(self):
        native = FakeNative()
        before = copy.deepcopy(native.threads)
        native.fail_once = ('thread/resume', '2')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'migration.json'
            with self.assertRaises(RuntimeError):
                migrate(native, {'openai','deepseek-desktop'}, path)
            self.assertEqual(native.archived, {'2'})
            rollback(native, path)
            self.assertEqual(native.threads, before)

    def test_failed_migration_resumes_without_inference(self):
        native = FakeNative()
        native.fail_once = ('thread/resume','1')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'migration.json'
            with self.assertRaises(RuntimeError):
                migrate(native, {'openai','deepseek-desktop'}, path)
            self.assertEqual(migrate(native, {'openai','deepseek-desktop'}, path)['status'], 'passed')


class NativeSessionConfigurationTests(unittest.TestCase):
    def test_legacy_home_does_not_get_an_incomplete_adapter_provider(self):
        from unittest import mock
        import official_client_migration as subject
        import patch as core
        for configured in (False,True):
            with self.subTest(configured=configured), tempfile.TemporaryDirectory() as tmp:
                home=Path(tmp)
                text='model="gpt"\n[mcp_servers.local-tools]\ncommand="unused-tool"\n'
                if configured:text+='[model_providers.'+PROVIDER+']\nname="OpenAI"\nbase_url="http://127.0.0.1:1234/v1"\n'
                (home/'config.toml').write_text(text,encoding='utf-8')
                process=mock.Mock();process.poll.return_value=0
                with mock.patch.object(core,'cli_path',return_value=home/'cli'), mock.patch.object(subject.subprocess,'Popen',return_value=process) as spawn, mock.patch.object(subject,'MetadataRpc'):
                    with subject.native_session(home/'app',home):pass
                arguments=spawn.call_args.args[0]
                override='model_providers.'+PROVIDER+'.base_url="http://127.0.0.1:9/backend-api/codex"'
                self.assertEqual(override in arguments,configured)
                self.assertIn('mcp_servers.local-tools.enabled=false',arguments)
                self.assertNotIn('mcp_servers."local-tools".enabled=false',arguments)
                self.assertEqual((home/'config.toml').read_text(encoding='utf-8'),text)


if __name__ == '__main__':
    unittest.main()
