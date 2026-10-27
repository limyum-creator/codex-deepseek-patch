"""Check tracked release contents for accidentally included local or vendor data."""
from pathlib import Path
import re
import subprocess

SOURCE = Path(__file__).resolve().parents[1]
ROOT = SOURCE.parent if SOURCE.name == "src" else SOURCE
ALLOWED = {
    'brand.svg',
    'management.py', 'settings_macos.js', 'settings_windows.ps1', 'tests/test_management.py',
    'local_history.py', 'tests/test_local_history.py',
    'exit_watch.py', 'tests/test_exit_watch.py',
    'tests/test_inference_chat.py', 'official_client_shortcut.py', 'tests/test_official_client_shortcut.py', 'update_progress.py', 'update_progress_macos.js', 'update_progress.ps1', 'tests/test_update_progress.py', 'tests/test_update_progress_macos.py', 'tests/test_update_progress_windows.py',
    'official_client.py', 'official_client_config.py', 'official_client_host.py', 'official_client_install.py', 'official_client_migration.py', 'official_client_update.py', 'inference_adapter.py', 'inference_transport.py', 'legacy_history.py', 'reasoning_origin.py', 'chat_completions_compat.py', 'scripts/test_official_client.py', 'scripts/test_official_connection.py', 'tests/test_official_client.py', 'tests/test_official_client_config.py', 'tests/test_official_client_host.py', 'tests/test_official_client_install.py', 'tests/test_official_client_migration.py', 'tests/test_official_client_update.py', 'tests/test_inference_adapter.py', 'tests/test_inference_transport.py', 'tests/test_legacy_history.py', 'tests/test_reasoning_origin.py', 'tests/test_chat_completions_compat.py',
    "windows_update_ui.py", "windows_update_ui.js", "tests/test_windows_update_ui.py", "tests/test_windows_update_contract.py",
    "cpa_images.py", "tests/test_cpa_images.py",
    "macos_signing.py", "tests/test_macos_signing.py", "scripts/test_macos_signing.py",
    "responses_compat.py", "tests/test_responses_compat.py", "scripts/test_tools.py",
    "local_support.py", "local_bridge.py", "tests/test_local_support.py", "scripts/test_switching.py", "scripts/route_driver.cjs",
    "model_sync.py", "updater.py", "update_worker.py", "edition.json", "update.json",
    "scripts/test_reasoning.py", "scripts/release_manifest.py", "scripts/test_context.py", "tests/test_model_sync.py", "tests/test_updater.py",
    ".gitignore", ".github/workflows/tests.yml", "LICENSE", "NOTICE", "README.md", "README.en.md",
    "CONTRIBUTING.md", "patch.py", "route.js", "docs/ARCHITECTURE.md", "docs/VALIDATION.md",
    "scripts/__init__.py", "scripts/test_boot.py", "scripts/test_connection.py", "scripts/check_release.py",
    "tests/test_patch.py", "tests/route.test.cjs",
    "backend.py", "compatibility.py", "monitor.py", "windows_support.py", "windows_install.py",
    "scripts/test_offline.py", "scripts/check_compatibility.cjs",
    "tests/test_backend.py", "tests/test_compatibility.py", "tests/test_monitor.py",
    "tests/test_offline.py", "tests/test_windows.py", "tests/test_windows_install.py",
    "proxy_support.py", "tests/test_proxy.py",
    "scripts/test_context.py",
}
files = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
files = [name for name in files if name]
if 'unified-update.json' in files:ALLOWED.add('unified-update.json')
if SOURCE != ROOT:
    ALLOWED = {("src/" + name if name.startswith(("scripts/", "tests/")) or "/" not in name and (Path(name).suffix in (".py", ".js", ".ps1", ".svg") or name == "edition.json") else name) for name in ALLOWED} | {"patch.py", "monitor.py"}
if (set(files) | {"update.json"}) != ALLOWED:
    raise SystemExit("Release file allowlist mismatch: " + repr(set(files) ^ ALLOWED))
patterns = [re.compile("/" + "Users" + r"/[^\s/]+/"), re.compile(r"gh[pousr]_[A-Za-z0-9]{25,}"),
            re.compile(r"sk-[A-Za-z0-9_-]{20,}"), re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")]
total = 0
for name in files:
    path = ROOT / name
    if path.is_symlink():
        raise SystemExit("Release cannot contain symlinks: " + name)
    data = path.read_bytes(); total += len(data)
    if len(data) > 100_000:
        raise SystemExit("Unexpected large release file: " + name)
    text = data.decode("utf-8")
    if any(pattern.search(text) for pattern in patterns):
        raise SystemExit("Possible private data in: " + name)
print(f"PASS: {len(files)} allowlisted source/documentation files, {total:,} bytes; no matched local paths or credential patterns")
