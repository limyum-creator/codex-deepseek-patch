import argparse
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch as mock

import backend
import patch as p


def baseline_fixture(root):
    baseline = root / 'baseline'
    baseline.mkdir()
    catalog = baseline / 'desktop-models.json'
    catalog.write_text('{}')
    current = p.prepared_config('# retained comment\nmodel="gpt-original"\n[desktop]\nsize=12\n', catalog, 'account', 'service')
    (baseline / 'config-installed.toml').write_text(current)
    (baseline / 'manifest.json').write_text(json.dumps({'format': 1, 'status': 'installed', 'patched_asar': 'patched'}))
    parsed = p.tomllib.loads(current)
    managed = {'catalog': str(catalog), 'provider': parsed['model_providers'][p.PROVIDER], 'baseline': str(baseline.resolve())}
    return baseline, current, managed


class ManagedConfigTests(unittest.TestCase):
    def test_refresh_preserves_unrelated_user_changes_and_tables(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline, current, managed = baseline_fixture(root)
            current = current.replace('gpt-original', 'gpt-user-choice').replace('size=12', 'size=99')
            current += '\n# user addition\n[model_providers.other]\nname="Custom"\nbase_url="https://example.invalid"\n'
            result = backend.refresh_config(current, root / 'next/catalog.json', managed)
            expected = p.tomllib.loads(current)
            expected['model_catalog_json'] = str(root / 'next/catalog.json')
            self.assertEqual(p.tomllib.loads(result), expected)
            self.assertIn('# retained comment\nmodel="gpt-user-choice"', result)
            self.assertIn('# user addition\n[model_providers.other]\nname="Custom"', result)
            self.assertIn('size=99', result)

    def test_refresh_refuses_managed_fields_changed_by_user(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, current, managed = baseline_fixture(root)
            for changed in [current.replace('desktop-models.json', 'user-catalog.json'),
                            current.replace('https://api.deepseek.com/', 'https://user.invalid/')]:
                with self.subTest(changed=changed), self.assertRaises(p.PatchError):
                    backend.refresh_config(changed, root / 'next.json', managed)

    def test_prepared_config_delegates_managed_refresh(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, current, managed = baseline_fixture(root)
            result = p.prepared_config(current, root / 'next.json', 'unused-account', 'unused-service', managed=managed)
            self.assertEqual(result, backend.refresh_config(current, root / 'next.json', managed))

    def test_baseline_reads_exact_installed_provider_and_requires_catalog(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline, _, managed = baseline_fixture(root)
            self.assertEqual(backend.managed_baseline(baseline, 'different', 'different'), managed)
            Path(managed['catalog']).unlink()
            with self.assertRaises(p.PatchError):
                backend.managed_baseline(baseline, 'account', 'service')


class AdoptionTests(unittest.TestCase):
    def make_backend(self, root, baseline):
        obj = object.__new__(backend.MonitorBackend)
        obj.settings = {'app': str(root / 'source'), 'codex_home': str(root / 'home'),
                        'keychain_account': 'account', 'keychain_service': 'service'}
        obj.app = root / 'source'
        obj.baseline = baseline
        obj.store = False
        obj.inspection = None
        obj.snapshot = lambda: {'fingerprint': 'current-source-fingerprint'}
        return obj

    def test_adopts_verified_baseline_and_preserves_user_edits(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline, current, _ = baseline_fixture(root)
            app = root / 'source'; app.mkdir(); (app / 'app.asar').write_bytes(b'patched')
            home = root / 'home'; home.mkdir(); (home / 'config.toml').write_text(current + '\n[user]\nvalue=7\n')
            manifest = {'format': 1, 'status': 'installed', 'patched_asar': p.sha(b'patched'), 'original_asar': p.sha(b'original')}
            (baseline / 'manifest.json').write_text(json.dumps(manifest))
            obj = self.make_backend(root, baseline)
            with mock('backend.p.app_layout', side_effect=lambda app: {'asar': app / 'app.asar'}), mock('backend.p.verify_app') as verify:
                adopted = obj.adopt()
            self.assertEqual(adopted['fingerprint'], 'current-source-fingerprint')
            self.assertEqual(adopted['baseline'], str(baseline))
            verify.assert_called_once_with(app, manifest['patched_asar'])
            self.assertTrue((home / 'config.toml').read_text().endswith('[user]\nvalue=7\n'))

    def test_returns_none_when_installed_archive_was_replaced(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline, _, _ = baseline_fixture(root)
            app = root / 'source'; app.mkdir(); (app / 'app.asar').write_bytes(b'official-update')
            obj = self.make_backend(root, baseline)
            with mock('backend.p.app_layout', side_effect=lambda app: {'asar': app / 'app.asar'}), mock('backend.p.verify_app') as verify:
                self.assertIsNone(obj.adopt())
            verify.assert_not_called()

    def test_separate_windows_deployment_does_not_adopt_new_unchecked_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline, current, _ = baseline_fixture(root)
            source = root / 'source'; source.mkdir(); (source / 'app.asar').write_bytes(b'new-official-version')
            deployed = root / 'deployment'; deployed.mkdir(); (deployed / 'app.asar').write_bytes(b'old-patched-version')
            home = root / 'home'; home.mkdir(); (home / 'config.toml').write_text(current)
            manifest = {'format': 1, 'status': 'installed', 'deployment_target': str(deployed),
                        'original_asar': p.sha(b'old-official-version'), 'patched_asar': p.sha(b'old-patched-version')}
            (baseline / 'manifest.json').write_text(json.dumps(manifest))
            obj = self.make_backend(root, baseline)
            with mock('backend.sys.platform', 'win32'), mock('backend.p.app_layout', side_effect=lambda app: {'asar': app / 'app.asar'}), mock('backend.p.verify_app'):
                self.assertIsNone(obj.adopt(), 'Old deployment must not mark a newly updated source as already handled')


class InstallAdapterTests(unittest.TestCase):
    def test_proxy_install_requires_context_probe_before_touching_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);catalog=root/'desktop-models.json';catalog.write_text('{}')
            manifest={'status':'prepared','target':str(root/'app'),'codex_home':str(root),
                      'original_asar':'old','patched_asar':'new','catalog_sha256':p.file_sha(catalog),
                      'boot_probe':'passed','offline_probe':'passed','proxy':{'config':'must-not-read'}}
            with mock('backend.p.require_stopped'), mock('backend.p.verify_app'), \
                 mock('backend.credential_available',return_value=True), mock('backend.p.install_transaction') as install, \
                 mock('proxy_support.read_api_key') as credential:
                with self.assertRaisesRegex(p.PatchError,'context/compaction probe'):
                    backend.install_compatible(root,manifest)
            credential.assert_not_called();install.assert_not_called()
            self.assertFalse((root/'config.toml').exists())

    def test_refresh_verifies_signed_source_and_exact_previous_target_before_exchange(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, current, managed = baseline_fixture(root)
            home = root/'home';home.mkdir();config = home/'config.toml';config.write_text(current)
            build = root/'build';build.mkdir();catalog = build/'desktop-models.json';catalog.write_text('{}')
            source, target = root/'official-backup', root/'installed'
            source.mkdir();target.mkdir()
            manifest = {'status':'prepared', 'source_target':str(source), 'target':str(target), 'codex_home':str(home),
                        'original_asar':'official', 'replaced_asar':'old-patch', 'patched_asar':'new-patch',
                        'catalog_sha256':p.file_sha(catalog), 'keychain_account':'a', 'keychain_service':'s',
                        'managed':managed, 'boot_probe':'passed', 'offline_probe':'passed'}
            def transaction(app, staged, config_arg, before, after, commit):
                self.assertEqual(app,target)
                self.assertEqual(before,current)
                config_arg.write_text(after);commit()
            with mock('backend.sys.platform','darwin'), mock('backend.p.require_stopped') as stopped, \
                 mock('backend.p.verify_app') as verify, mock('backend.credential_available',return_value=True), \
                 mock('backend.p.install_transaction',side_effect=transaction):
                backend.install_compatible(build,manifest)
            self.assertIn(((source,'official'),), [(call.args,) for call in verify.call_args_list])
            self.assertIn(((target,'old-patch'),), [(call.args,) for call in verify.call_args_list])
            self.assertEqual(stopped.call_args_list[-1].args,(target,))
            self.assertEqual(manifest['status'],'installed')

    def test_refresh_rejects_changed_target_before_transaction(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp);target = root/'target';target.mkdir()
            manifest = {'status':'prepared','source_target':str(root/'source'),'target':str(target),'codex_home':str(root),
                        'original_asar':'official','replaced_asar':'old-patch'}
            def verify(app, expected):
                if expected=='old-patch': raise p.PatchError('App archive changed')
            with mock('backend.p.require_stopped'), mock('backend.p.verify_app',side_effect=verify), \
                 mock('backend.p.install_transaction') as exchange:
                with self.assertRaisesRegex(p.PatchError,'archive changed'):
                    backend.install_compatible(root,manifest)
            exchange.assert_not_called()

    def test_mac_install_keeps_unrelated_config_and_records_committed_baseline(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline, current, managed = baseline_fixture(root)
            current += '\n[user]\nkeep="yes"\n'
            home = root / 'home'; home.mkdir(); config = home / 'config.toml'; config.write_text(current)
            directory = root / 'new-build'; directory.mkdir()
            catalog = directory / 'desktop-models.json'; catalog.write_text('{}')
            app = root / 'source'; app.mkdir()
            manifest = {'status': 'prepared', 'target': str(app), 'codex_home': str(home),
                        'original_asar': 'original', 'patched_asar': 'patched', 'catalog_sha256': p.file_sha(catalog),
                        'keychain_account': 'account', 'keychain_service': 'service', 'managed': managed,
                        'boot_probe': 'passed', 'offline_probe': 'passed'}
            def transaction(app_arg, stage, config_arg, before, after, commit):
                self.assertEqual(app_arg, app)
                self.assertEqual(stage, directory / 'patched.app')
                self.assertEqual(before, current)
                config_arg.write_text(after)
                commit()
            with mock('backend.sys.platform', 'darwin'), mock('backend.p.require_stopped'), mock('backend.p.verify_app'), mock('backend.credential_available', return_value=True), mock('backend.p.install_transaction', side_effect=transaction) as install:
                result = backend.install_compatible(directory, manifest)
            install.assert_called_once()
            self.assertEqual(result['status'], 'installed')
            self.assertEqual((directory / 'config-before.toml').read_text(), current)
            self.assertEqual(p.tomllib.loads(config.read_text())['user'], {'keep': 'yes'})
            self.assertEqual(p.tomllib.loads(config.read_text())['model_catalog_json'], str(catalog))
            self.assertEqual(json.loads((directory / 'manifest.json').read_text())['installed_config_hash'], p.sha(p.read_config(config).encode()))

    def test_windows_adapter_receives_transaction_with_managed_refresh(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, current, managed = baseline_fixture(root)
            home = root / 'home'; home.mkdir(); config = home / 'config.toml'; config.write_text(current)
            directory = root / 'build'; directory.mkdir(); catalog = directory / 'desktop-models.json'; catalog.write_text('{}')
            source = root / 'official'; source.mkdir()
            deployed = root / 'deployed'; deployed.mkdir()
            manifest = {'status': 'prepared', 'target': str(source), 'deployment_target': str(deployed),
                        'codex_home': str(home), 'original_asar': 'original', 'patched_asar': 'patched',
                        'catalog_sha256': p.file_sha(catalog), 'keychain_account': 'account', 'keychain_service': 'service',
                        'managed': managed, 'boot_probe': 'passed', 'offline_probe': 'passed'}
            calls = []
            def install_windows(received, build, config_arg, before, after, commit):
                calls.append((received, build, config_arg, before))
                config_arg.write_text(after)
                commit()
            module = types.SimpleNamespace(install=install_windows)
            with mock('backend.sys.platform', 'win32'), mock.dict('sys.modules', {'windows_install': module}), mock('backend.p.require_stopped'), mock('backend.p.verify_app'), mock('backend.credential_available', return_value=True), mock('backend.p.install_transaction') as mac_install:
                backend.install_compatible(directory, manifest)
            self.assertEqual(calls, [(manifest, directory, config, current)])
            self.assertEqual(manifest['status'], 'installed')
            mac_install.assert_not_called()

    def test_build_rejects_stale_cached_inspection_before_cli_or_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            app = root / 'source'; app.mkdir()
            archive = app / 'app.asar'; archive.write_bytes(b'new-official-app')
            home = root / 'home'; home.mkdir()
            output = root / 'output'
            inspection = {'source_asar': p.sha(b'older-official-app'), 'patched': 'old-client',
                          'central': 'webview/assets/app-initial-old.js', 'probe': {'status': 'passed', 'assertions': 227}}
            args = argparse.Namespace(app=app, codex_home=home, output=output, baseline=None,
                                      keychain_account='account', keychain_service='service', inspection=inspection)
            layout = {'app': app, 'asar': archive, 'version': 'new', 'build': '2'}
            with mock('backend.p.mac_only'), mock('backend.p.app_layout', return_value=layout), mock('backend.p.cli_path', return_value=app / 'codex'), mock('backend.subprocess.check_output', side_effect=AssertionError('CLI must not run')) as cli:
                with self.assertRaisesRegex(p.PatchError, '(?i)source|inspection|asar|changed'):
                    backend.build_compatible(args)
            cli.assert_not_called()
            self.assertFalse(output.exists())

    def test_stable_signing_mismatch_stops_install_before_credentials_or_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = {'status': 'prepared', 'target': str(root/'app'), 'codex_home': str(root/'home'),
                        'original_asar': 'original', 'patched_asar': 'patched',
                        'stable_signing_required': True, 'macos_signing': {'scheme': 'invalid'}}
            with mock('backend.p.require_stopped'), mock('backend.p.verify_app'), \
                 mock('macos_signing.verify_app_identity', side_effect=p.PatchError('identity changed')) as verify, \
                 mock('backend.credential_available') as credential, mock('backend.p.install_transaction') as install:
                with self.assertRaisesRegex(p.PatchError, 'identity changed'):
                    backend.install_compatible(root, manifest)
            verify.assert_called_once_with(root/'patched.app', manifest['macos_signing'])
            credential.assert_not_called()
            install.assert_not_called()

    def test_install_refuses_missing_offline_probe_before_transaction(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'; home.mkdir()
            directory = root / 'build'; directory.mkdir(); catalog = directory / 'desktop-models.json'; catalog.write_text('{}')
            manifest = {'status': 'prepared', 'target': str(root / 'source'), 'codex_home': str(home),
                        'original_asar': 'original', 'patched_asar': 'patched', 'catalog_sha256': p.file_sha(catalog),
                        'boot_probe': 'passed'}
            with mock('backend.p.require_stopped'), mock('backend.p.verify_app'), mock('backend.p.install_transaction') as install:
                with self.assertRaisesRegex(p.PatchError, 'Both isolated boot and offline'):
                    backend.install_compatible(directory, manifest)
            install.assert_not_called()


if __name__ == '__main__':
    unittest.main()
