"""Owned-link transaction contracts; no target is ever launched."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch as mock

import official_client_shortcut as shortcut
import patch as p


class ShortcutTests(unittest.TestCase):
    def fixture(self, root):
        link = root/'User kept name.lnk'; link.write_bytes(b'old-managed-link\r\n')
        prior = {'windows_install':{'shortcut':str(link),'shortcut_hash':p.file_sha(link)}}
        return link, prior

    def test_unchanged_owned_link_can_activate_and_restore_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); link,prior=self.fixture(root)
            with mock.object(shortcut,'official_entry',return_value={'target':'official-entry'}), \
                 mock.object(shortcut,'write_link',side_effect=lambda path,entry:path.write_bytes(b'official-link')):
                record=shortcut.prepare(prior,root,root/'official','fixture')
            shortcut.activate(record); self.assertEqual(link.read_bytes(),b'official-link')
            shortcut.activate(record)
            shortcut.restore(record); self.assertEqual(link.read_bytes(),b'old-managed-link\r\n')
            shortcut.restore(record)
            self.assertEqual(Path(record['backup']).read_bytes(),b'old-managed-link\r\n')

    def test_user_changed_link_or_backup_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); link,prior=self.fixture(root)
            link.write_bytes(b'user-change')
            with self.assertRaisesRegex(p.PatchError,'changed'):
                shortcut.prepare(prior,root,root/'official','fixture')
            link.write_bytes(b'old-managed-link\r\n')
            with mock.object(shortcut,'official_entry',return_value={}), \
                 mock.object(shortcut,'write_link',side_effect=lambda path,entry:path.write_bytes(b'official')):
                record=shortcut.prepare(prior,root,root/'official','fixture')
            shortcut.activate(record); Path(record['backup']).write_bytes(b'changed-backup')
            with self.assertRaisesRegex(p.PatchError,'backup changed'):shortcut.restore(record)
            self.assertEqual(link.read_bytes(),b'official')
            link.write_bytes(b'new-user-change')
            with self.assertRaisesRegex(p.PatchError,'independently'):shortcut.restore(record)
            self.assertEqual(link.read_bytes(),b'new-user-change')

    def test_absent_or_deleted_old_shortcut_is_not_recreated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); link,prior=self.fixture(root);link.unlink()
            self.assertIsNone(shortcut.prepare(prior,root,root/'official','fixture'))
            self.assertIsNone(shortcut.prepare({},root,root/'official','fixture'))
            self.assertFalse(link.exists())

    def test_registered_store_entry_uses_stable_aumid_and_does_not_launch(self):
        with mock('windows_support.is_store_app',return_value=True), \
             mock.dict(shortcut.os.environ,{'SystemRoot':str(Path.cwd()/'Windows')}), \
             mock.object(shortcut,'_powershell',return_value={'family':'OpenAI.Codex_publisher','app_id':'App'}) as query:
            entry=shortcut.official_entry(Path.cwd()/'WindowsApps/Package/app')
        self.assertEqual(entry['arguments'],'shell:AppsFolder\\OpenAI.Codex_publisher!App')
        self.assertEqual(Path(entry['target']).name,'explorer.exe')
        self.assertNotIn('Start-Process',query.call_args.args[0])


if __name__=='__main__':unittest.main()
