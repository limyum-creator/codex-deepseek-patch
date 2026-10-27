import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch as mock

import patch as p
import macos_signing as m

FINGERPRINT = 'A' * 40


class IdentityTests(unittest.TestCase):
    def test_requirement_pins_certificate_and_identifier_not_build_hash(self):
        value = m.requirement('com.example.app', FINGERPRINT)
        self.assertIn('identifier "com.example.app"', value)
        self.assertIn('certificate leaf = H"' + 'a' * 40 + '"', value)
        self.assertNotIn('cdhash', value)
        for name, key in [('bad" or true', FINGERPRINT), ('com.example', 'bad')]:
            with self.assertRaises(p.PatchError):m.requirement(name, key)

    def test_keep_capabilities_without_claiming_vendor_identity_or_groups(self):
        original = {'com.apple.security.device.audio-input': True, 'com.apple.security.device.camera': True,
                    'com.apple.security.automation.apple-events': True, 'com.apple.security.cs.allow-jit': True,
                    'com.apple.security.app-sandbox': False, 'com.apple.application-identifier': 'VENDOR.app',
                    'com.apple.developer.team-identifier': 'VENDOR', 'com.apple.developer.aps-environment': 'production',
                    'com.apple.security.application-groups': ['VENDOR.shared'], 'keychain-access-groups': ['VENDOR.*']}
        result = m.capability_entitlements(original)
        self.assertEqual(result, {key:value for key,value in original.items() if key.startswith('com.apple.security.') and key != 'com.apple.security.application-groups'})
        self.assertIn('keychain-access-groups', original)

    def test_saved_identity_is_never_rotated_if_keychain_entry_is_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();keychain=root/'login.keychain-db';keychain.touch()
            record={'scheme':m.SCHEME,'fingerprint':FINGERPRINT,'keychain':str(keychain),'name':m.IDENTITY_NAME}
            p.save_json(root/'identity.json',record)
            with mock.object(m.sys,'platform','darwin'), mock.object(m,'identities',return_value={}), mock.object(m,'create_identity') as create:
                with self.assertRaisesRegex(p.PatchError,'refusing to generate'):m.ensure_identity(root)
            create.assert_not_called()
            self.assertEqual(json.loads((root/'identity.json').read_text()),record)

    def test_missing_metadata_recovers_existing_key_without_rotation(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();keychain=root/'login.keychain-db';keychain.touch()
            with mock.object(m.sys,'platform','darwin'), mock.object(m,'command',return_value=Mock(stdout=('"'+str(keychain)+'"\n').encode())), \
                 mock.object(m,'identities',return_value={FINGERPRINT:m.IDENTITY_NAME}), mock.object(m,'create_identity') as create:
                record=m.ensure_identity(root)
            create.assert_not_called()
            self.assertEqual(record['fingerprint'],FINGERPRINT)
            self.assertEqual(json.loads((root/'identity.json').read_text()),record)

    def test_ambiguous_existing_keys_stop_before_generating_another(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();keychain=root/'login.keychain-db';keychain.touch()
            with mock.object(m.sys,'platform','darwin'), mock.object(m,'command',return_value=Mock(stdout=('"'+str(keychain)+'"').encode())), \
                 mock.object(m,'identities',return_value={FINGERPRINT:m.IDENTITY_NAME,'B'*40:m.IDENTITY_NAME}), mock.object(m,'create_identity') as create:
                with self.assertRaisesRegex(p.PatchError,'Several'):m.ensure_identity(root)
            create.assert_not_called()

    def test_build_cannot_silently_replace_an_installed_certificate(self):
        identity={'fingerprint':FINGERPRINT}
        with mock.object(m,'validate_identity',return_value=identity), mock.object(m.p,'framework_path') as framework:
            with self.assertRaisesRegex(p.PatchError,'different local signing certificate'):
                m.sign_app(Path('/unused/app'),identity,previous={'fingerprint':'B'*40})
        framework.assert_not_called()

    def test_signature_record_cannot_reference_code_outside_app(self):
        with tempfile.TemporaryDirectory() as temp:
            app=Path(temp).resolve()
            record={'scheme':m.SCHEME,'fingerprint':FINGERPRINT,'components':[
                {'path':'../outside','identifier':'com.example','requirement':m.requirement('com.example',FINGERPRINT),'entitlements':{}},
                {'path':'.','identifier':'com.example','requirement':m.requirement('com.example',FINGERPRINT),'entitlements':{}}]}
            with mock.object(m,'command') as command:
                with self.assertRaisesRegex(p.PatchError,'escapes'):m.verify_app_identity(app,record)
            command.assert_not_called()


if __name__ == '__main__':unittest.main()
