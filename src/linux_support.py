"""Read-only discovery of the installed official Codex npm CLI on Linux."""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import shutil
import subprocess


def discover_app():
    launcher = shutil.which('codex')
    if not launcher:
        raise ValueError('Codex CLI is missing; install @openai/codex or pass --app')
    return Path(launcher).resolve().parent.parent


def inspect_app(app):
    app = Path(app).resolve()
    package = json.loads((app / 'package.json').read_text(encoding='utf-8'))
    repository = package.get('repository', {})
    if (package.get('name') != '@openai/codex'
            or not isinstance(repository, dict)
            or repository.get('url') != 'git+https://github.com/openai/codex.git'):
        raise ValueError('Expected the installed official @openai/codex npm package')
    architecture = {'x86_64': ('x64', 'x86_64'), 'aarch64': ('arm64', 'aarch64')}.get(platform.machine())
    if architecture is None:
        raise ValueError('Linux Codex CLI supports x86_64 and aarch64')
    npm_arch, target_arch = architecture
    target = target_arch + '-unknown-linux-musl'
    roots = [app / 'node_modules' / '@openai' / ('codex-linux-' + npm_arch), app]
    candidates = [root / 'vendor' / target / subdir / 'codex'
                  for root in roots for subdir in ('bin', 'codex')]
    cli = next((path for path in candidates if path.is_file()), None)
    if cli is None or not os.access(cli, os.X_OK):
        raise ValueError('The installed Linux Codex executable is missing')
    with cli.open('rb') as handle:
        if handle.read(4) != b'\x7fELF':
            raise ValueError('The Linux Codex executable is not ELF')
    version = subprocess.check_output([str(cli), '--version'], text=True, timeout=15).strip()
    if version != 'codex-cli ' + package.get('version', ''):
        raise ValueError('Codex npm metadata and native executable versions differ')
    return {'app': app, 'cli': cli, 'exe': cli, 'version': package['version'],
            'build': package['version'], 'source_artifact': cli}
