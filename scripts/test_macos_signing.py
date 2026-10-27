"""Isolated macOS signing regression: changed builds, stable DR, real keychain ACL.

Uses disposable identities/items only. Never requests microphone or screen
access and never reads or resets the user's privacy permissions.
"""
from pathlib import Path
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import macos_signing as signing

SOURCE = r'''
#include <Security/Security.h>
#include <stdio.h>
#include <string.h>
int main(int argc, char **argv) {
    if (argc != 4) return 2;
    SecKeychainSetUserInteractionAllowed(false);
    SecKeychainRef keychain = NULL;
    OSStatus status = SecKeychainOpen(argv[1], &keychain);
    if (status) return 3;
    const char *service = argv[2], *account = "probe", *value = "synthetic-only";
    if (strcmp(argv[3], "create") == 0) {
        status = SecKeychainAddGenericPassword(keychain, (UInt32)strlen(service), service,
                     (UInt32)strlen(account), account, (UInt32)strlen(value), value, NULL);
    } else {
        UInt32 length = 0; void *data = NULL;
        status = SecKeychainFindGenericPassword(keychain, (UInt32)strlen(service), service,
                    (UInt32)strlen(account), account, &length, &data, NULL);
        if (!status && (length != strlen(value) || memcmp(data, value, length))) status = -1;
        if (data) SecKeychainItemFreeContent(NULL, data);
    }
    CFRelease(keychain);
    printf("build=%d status=%d\n", PROBE_BUILD, (int)status);
    return status == 0 ? 0 : 1;
}
'''


def run(output=None):
    if sys.platform != 'darwin':
        return {'status': 'skipped', 'reason': 'macOS only'}
    previous = Path.cwd()
    with tempfile.TemporaryDirectory(prefix='codex-signing-regression-') as temp:
        root = Path(temp); os.chmod(root, 0o700)
        keychain = root / 'disposable.keychain-db'
        created = False
        try:
            signing.command(['/usr/bin/security', 'create-keychain', '-p', '', keychain]); created = True
            signing.command(['/usr/bin/security', 'unlock-keychain', '-p', '', keychain])
            fingerprint = signing.create_identity(keychain, root)
            # codesign requires the selected keychain to be in the user's search
            # list, even when --keychain is supplied. Retain every existing entry.
            old = shlex.split(signing.command(['/usr/bin/security', 'list-keychains', '-d', 'user']).stdout.decode())
            signing.command(['/usr/bin/security', 'list-keychains', '-d', 'user', '-s', *old, keychain])
            identity = {'scheme': signing.SCHEME, 'fingerprint': fingerprint, 'keychain': str(keychain), 'name': signing.IDENTITY_NAME}
            signing.validate_identity(identity)
            source = root / 'probe.c'; source.write_text(SOURCE)
            apps = []
            capabilities = {'com.apple.security.device.audio-input': True, 'com.apple.security.device.camera': True,
                            'com.apple.security.automation.apple-events': True}
            expected = None
            hashes = []
            for version in [1, 2]:
                app = root / ('Build' + str(version) + '.app'); binary = app / 'Contents/MacOS/probe'
                binary.parent.mkdir(parents=True)
                (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': 'com.example.codex-patch-signing-probe',
                    'CFBundleExecutable': 'probe', 'CFBundlePackageType': 'APPL', 'CFBundleVersion': str(version)}))
                signing.command(['/usr/bin/clang', '-Wno-deprecated-declarations', '-DPROBE_BUILD=' + str(version),
                                 source, '-framework', 'Security', '-framework', 'CoreFoundation', '-o', binary])
                signing.command(['/usr/bin/codesign', '--sign', '-', app])
                record = signing.sign_target(app, identity, capabilities)
                if expected is None: expected = record['requirement']
                assert record['requirement'] == expected
                signing.command(['/usr/bin/codesign', '--verify', '--strict', '--test-requirement', '=' + expected, app])
                details = signing.command(['/usr/bin/codesign', '--display', '--verbose=4', app]).stderr.decode()
                hashes.append(next(line for line in details.splitlines() if line.startswith('CDHash=')))
                apps.append((app, binary))
            assert hashes[0] != hashes[1], 'Fixtures must have different code hashes'
            service = 'CodexPatchDisposable-' + uuid.uuid4().hex
            create = signing.command([apps[0][1], keychain, service, 'create'])
            read = signing.command([apps[1][1], keychain, service, 'read'])
            # Negative control: replacing the certificate with ad-hoc signing
            # must neither satisfy the old DR nor silently read its keychain item.
            control = root / 'Adhoc.app'; shutil.copytree(apps[1][0], control)
            # Re-sign with normal ad-hoc defaults, so the control uses its own hash.
            signing.command(['/usr/bin/codesign', '--remove-signature', control])
            signing.command(['/usr/bin/codesign', '--sign', '-', control])
            mismatch = signing.command(['/usr/bin/codesign', '--verify', '--test-requirement', '=' + expected, control], check=False)
            denied = signing.command([control / 'Contents/MacOS/probe', keychain, service, 'read'], check=False)
            assert mismatch.returncode != 0 and denied.returncode != 0, 'Ad-hoc control unexpectedly inherited access'
            result = {'status': 'passed', 'changed_code_hashes': True, 'stable_designated_requirement': True,
                      'capabilities_retained': list(capabilities), 'keychain_read_without_prompt_after_update': True,
                      'ad_hoc_control_denied': True, 'external_inference_turns': 0}
        finally:
            os.chdir(previous)
            if created:
                signing.command(['/usr/bin/security', 'delete-keychain', keychain], check=False)
                current = shlex.split(signing.command(['/usr/bin/security', 'list-keychains', '-d', 'user']).stdout.decode())
                retained = [value for value in current if Path(value).resolve() != keychain.resolve()]
                if retained != current:
                    signing.command(['/usr/bin/security', 'list-keychains', '-d', 'user', '-s', *retained])
    if output:
        Path(output).write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))
    return result


if __name__ == '__main__':
    run(sys.argv[1] if len(sys.argv) > 1 else None)
