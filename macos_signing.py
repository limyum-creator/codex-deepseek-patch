"""Stable per-user code identity for locally patched macOS applications.

No TCC records, global trust settings or user credential items are changed.
The signing private key stays in the user's login keychain; build directories
contain only public signing metadata. Both editions reuse this identity.
"""
from pathlib import Path
import hashlib
import json
import os
import plistlib
import re
import shlex
import subprocess
import sys
import tempfile

import patch as p

IDENTITY_NAME = 'Codex Model Patch Local Signing'
# These claims require the vendor's signing/provisioning identity. A local
# signature retains ordinary capabilities but must not claim vendor groups.
RESTRICTED_KEYS = {'com.apple.application-identifier', 'application-identifier',
                   'keychain-access-groups', 'com.apple.security.application-groups'}
SCHEME = 'local-certificate-v1'


def default_directory():
    return Path.home() / 'Library/Application Support/CodexDeepSeekPatch/signing'


def command(args, *, check=True):
    result = subprocess.run([str(a) for a in args], capture_output=True, timeout=90)
    if check and result.returncode:
        raise p.PatchError('macOS signing command failed: ' + result.stderr.decode('utf-8', 'replace')[-1200:])
    return result


def identities(keychain):
    text = command(['/usr/bin/security', 'find-identity', '-p', 'codesigning', keychain]).stdout.decode('utf-8')
    # Self-signed local identities are usable for DRs without installing a new
    # trusted root. Do not filter with -v, which tests global certificate trust.
    return {fingerprint.upper(): name for fingerprint, name in
            re.findall(r'\b([0-9A-Fa-f]{40})\s+"([^"\n]+)"', text)}


def validate_identity(record):
    p.require(record.get('scheme') == SCHEME, 'Unknown macOS signing identity format')
    fingerprint = record.get('fingerprint', '')
    p.require(re.fullmatch(r'[0-9A-F]{40}', fingerprint), 'Invalid signing certificate fingerprint')
    keychain = Path(record['keychain'])
    p.require(keychain.is_absolute() and keychain.is_file(), 'Signing keychain is unavailable; unlock or restore it')
    p.require(fingerprint in identities(keychain),
              'The saved signing identity is missing. Restore its keychain; refusing to generate a different identity and reset permissions')
    return record


def ensure_identity(directory=None):
    p.require(sys.platform == 'darwin', 'Local signing identities are only used on macOS')
    from monitor import private_directory, state_lock
    directory = private_directory(Path(directory or default_directory()).resolve())
    with state_lock(directory) as locked:
        p.require(locked, 'Signing identity setup is already running')
        path = directory / 'identity.json'
        if path.exists():
            p.require(not path.is_symlink(), 'Signing identity metadata cannot be a symlink')
            return validate_identity(json.loads(path.read_text(encoding='utf-8')))
        output = command(['/usr/bin/security', 'default-keychain', '-d', 'user']).stdout.decode('utf-8')
        values = shlex.split(output)
        p.require(len(values) == 1, 'Cannot identify the user login keychain')
        keychain = Path(values[0]).resolve()
        found = {fp: name for fp, name in identities(keychain).items() if name == IDENTITY_NAME}
        p.require(len(found) <= 1, 'Several local patch identities exist; choose the original identity before updating')
        if found:
            fingerprint = next(iter(found))  # Recover metadata without rotating the key.
        else:
            fingerprint = create_identity(keychain, directory)
        record = {'scheme': SCHEME, 'fingerprint': fingerprint, 'keychain': str(keychain), 'name': IDENTITY_NAME}
        validate_identity(record)
        p.save_json(path, record)
        return record


def create_identity(keychain, directory):
    with tempfile.TemporaryDirectory(prefix='identity-create-', dir=directory) as temp:
        root = Path(temp)
        config = root / 'certificate.cnf'
        config.write_text('[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n'
            '[dn]\nCN=' + IDENTITY_NAME + '\n[ext]\nbasicConstraints=critical,CA:FALSE\n'
            'keyUsage=critical,digitalSignature\nextendedKeyUsage=critical,codeSigning\n'
            'subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n', encoding='utf-8')
        key, rsa, cert = root / 'private.pem', root / 'private-rsa.pem', root / 'certificate.pem'
        command(['/usr/bin/openssl', 'req', '-x509', '-newkey', 'rsa:3072', '-nodes', '-days', '3650',
                 '-config', config, '-keyout', key, '-out', cert])
        key.chmod(0o600)
        # Apple security import accepts the traditional RSA container; importing
        # PKCS#8/PKCS#12 varies with the system OpenSSL version.
        command(['/usr/bin/openssl', 'rsa', '-in', key, '-out', rsa]); rsa.chmod(0o600)
        der = command(['/usr/bin/openssl', 'x509', '-in', cert, '-outform', 'DER']).stdout
        fingerprint = hashlib.sha1(der).hexdigest().upper()
        command(['/usr/bin/security', 'import', cert, '-k', keychain])
        try:
            command(['/usr/bin/security', 'import', rsa, '-k', keychain, '-f', 'openssl', '-t', 'priv',
                     '-P', '', '-x', '-T', '/usr/bin/codesign'])
        except BaseException:
            command(['/usr/bin/security', 'delete-certificate', '-Z', fingerprint, keychain], check=False)
            raise
        # Temporary private material is deleted as this context exits. -x keeps
        # the imported private key non-extractable; codesign receives explicit ACL access.
        return fingerprint


def capability_entitlements(original):
    return {key: value for key, value in original.items()
            if key not in RESTRICTED_KEYS and not key.startswith('com.apple.developer.')}


def read_entitlements(target):
    raw = command(['/usr/bin/codesign', '--display', '--entitlements', ':-', target]).stdout
    return plistlib.loads(raw) if raw.strip() else {}


def identifier(target):
    result = command(['/usr/bin/codesign', '--display', '--verbose=4', target])
    match = re.search(r'^Identifier=(.+)$', result.stderr.decode('utf-8'), re.M)
    p.require(match and re.fullmatch(r'[A-Za-z0-9._-]+', match.group(1)), 'Cannot preserve code signing identifier')
    return match.group(1)


def requirement(identifier_value, fingerprint):
    p.require(re.fullmatch(r'[A-Za-z0-9._-]+', identifier_value), 'Invalid code identifier')
    p.require(re.fullmatch(r'[0-9A-Fa-f]{40}', fingerprint), 'Invalid certificate fingerprint')
    return 'identifier "' + identifier_value + '" and certificate leaf = H"' + fingerprint.lower() + '"'


def designated_requirement(target):
    result = command(['/usr/bin/codesign', '--display', '--requirements', '-', target])
    text = (result.stdout + result.stderr).decode('utf-8')
    match = re.search(r'^(?:# )?designated => (.+)$', text, re.M)
    p.require(match, 'Missing code designated requirement')
    return match.group(1)


def sign_target(target, identity, entitlements=None):
    code_id = identifier(target)
    dr = requirement(code_id, identity['fingerprint'])
    args = ['/usr/bin/codesign', '--force', '--sign', identity['fingerprint'], '--keychain', identity['keychain'],
            '--timestamp=none', '--identifier', code_id, '--requirements', '=designated => ' + dr]
    with tempfile.TemporaryDirectory(prefix='codex-sign-entitlements-') as temp:
        if entitlements:
            path = Path(temp) / 'entitlements.plist'
            path.write_bytes(plistlib.dumps(entitlements))
            args += ['--entitlements', path, '--generate-entitlement-der']
        command([*args, target])
    p.require(designated_requirement(target) == dr, 'Signing changed the stable application identity')
    actual = read_entitlements(target)
    p.require(actual == (entitlements or {}), 'Signed capabilities differ from the checked entitlement plan')
    return {'identifier': code_id, 'requirement': dr, 'entitlements': actual}


def sign_app(app, identity=None, previous=None):
    app = Path(app).resolve()
    identity = validate_identity(identity) if identity else ensure_identity()
    if previous:
        p.require(previous.get('fingerprint') == identity['fingerprint'],
                  'The installed app uses a different local signing certificate. Restore that identity before updating')
    framework = p.framework_path(app)
    # Only the changed framework, its executable helpers, and the outer app
    # need local signatures. Unchanged vendor code (including CodexCLI.app and
    # Sparkle) keeps its original signing and provisioning identity.
    bundle = app / 'Contents/Frameworks/Codex Framework.framework'
    p.require(framework.is_relative_to(bundle.resolve()), 'Framework is outside its bundle')
    helpers = {path.resolve() for path in bundle.rglob('*.app') if (path / 'Contents/Info.plist').is_file()}
    p.require(all(path.is_relative_to(app) for path in helpers), 'A signing helper points outside the app')
    targets = sorted(helpers, key=lambda path: (-len(path.parts), str(path))) + [bundle, app]
    components = []
    for target in targets:
        original = read_entitlements(target)
        entitlements = {} if target == bundle else capability_entitlements(original)
        component = sign_target(target, identity, entitlements)
        component.update(path=str(target.relative_to(app)), removed_vendor_claims=sorted(set(original) - set(entitlements)))
        components.append(component)
    result = {'scheme': SCHEME, 'fingerprint': identity['fingerprint'], 'components': components}
    verify_app_identity(app, result)
    return result


def verify_app_identity(app, record):
    app = Path(app).resolve()
    p.require(record.get('scheme') == SCHEME, 'Missing stable signing record')
    p.require(record.get('components') and any(item.get('path') == '.' for item in record['components']),
              'Outer application identity is missing')
    for item in record['components']:
        target = (app / item['path']).resolve()
        p.require(target.is_relative_to(app), 'Signing record escapes the application')
        expected = requirement(item['identifier'], record['fingerprint'])
        p.require(item['requirement'] == expected and designated_requirement(target) == expected,
                  'Application signing identity changed; permissions would not be preserved')
        p.require(read_entitlements(target) == item['entitlements'], 'Application capabilities changed')
        command(['/usr/bin/codesign', '--verify', '--strict', '--test-requirement', '=' + expected, target])
