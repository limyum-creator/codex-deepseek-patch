"""Boot a prepared app with disposable data; stop only the probe process tree."""
from __future__ import annotations

from pathlib import Path
import json
import os
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import patch
from windows_support import isolated_desktop_options, desktop_process
from scripts.test_offline import isolated_environment, launch_options, stop_probe, offline_config, DUMMY_KEY_VARIABLE, DUMMY_KEY


def process_table():
    if sys.platform == "win32":
        script = ("$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); "
                  "@(Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,CommandLine) "
                  "| ConvertTo-Json -Compress")
        raw = subprocess.check_output(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                      text=True, encoding="utf-8", errors="replace", timeout=20, **patch.background_options())
        values = json.loads(raw) if raw.strip() else []
        if isinstance(values, dict):
            values = [values]
        return [{"pid": int(value["ProcessId"]), "ppid": int(value["ParentProcessId"]),
                 "command": value.get("CommandLine") or ""} for value in values]
    raw = subprocess.check_output(["/bin/ps", "-axo", "pid=,ppid=,command="], text=True, timeout=20)
    rows = []
    for line in raw.splitlines():
        match = re.match(r"\s*(\d+)\s+(\d+)\s+(.*)", line)
        if match:
            rows.append({"pid": int(match[1]), "ppid": int(match[2]), "command": match[3]})
    return rows


def owned_processes(rows, root_pid):
    owned = {root_pid}
    while True:
        found = {row["pid"] for row in rows if row["ppid"] in owned}
        if found <= owned:
            return [row for row in rows if row["pid"] in owned]
        owned |= found


def inspect_children(rows, root_pid, browser, cli):
    owned = owned_processes(rows, root_pid)
    root = [row for row in owned if row["pid"] == root_pid]
    isolated = any(str(browser).casefold() in row["command"].casefold() for row in root)
    renderer = [row for row in owned if "--type=renderer" in row["command"]]
    backend = [row for row in owned if str(cli).casefold() in row["command"].casefold()
               and re.search(r'(?:^|[\s"])app-server(?=$|[\s"])', row["command"])]
    return {"isolated_profile": isolated, "renderer": bool(renderer), "backend": bool(backend),
            "owned_processes": len(owned),
            "mock_keychain": any('--use-mock-keychain' in row['command'].split() for row in root)}


def boot_command(executable, browser):
    args = [str(executable), '--user-data-dir=' + str(browser), '--no-first-run']
    if sys.platform == 'darwin':
        # Only the disposable GUI probe uses Chromium's mock credential store.
        # An empty test HOME otherwise prompts for a missing login keychain.
        args.append('--use-mock-keychain')
    return args


def main(directory, timeout=360):
    directory, manifest = patch.load_build(Path(directory))
    patch.require(manifest["status"] == "prepared", "Only prepared builds can be boot-tested")
    app = directory / "patched.app"
    patch.verify_app(app, manifest["patched_asar"])
    layout = patch.app_layout(app)
    manifest["boot_probe"] = "running"
    patch.save_json(directory / "manifest.json", manifest)
    evidence = {"status": "failed", "renderer": False, "backend": False}
    try:
        with tempfile.TemporaryDirectory(prefix="codex-deepseek-boot-") as temporary:
            base = Path(temporary)
            environment = isolated_environment(base)
            environment[DUMMY_KEY_VARIABLE] = DUMMY_KEY
            # The desktop keeps its native default provider; proxy providers are
            # selected by the patched thread route, not as a global GUI default.
            config = offline_config(directory / "desktop-models.json", manifest.get("proxy", {}).get("models", []), manifest.get("model_routes"))
            config = "\n".join(config.splitlines()[2:]) + "\n"
            (base / "home/config.toml").write_text(config, encoding="utf-8")
            browser = base / "browser"
            started = time.monotonic()
            with (directory / "boot.log").open("w", encoding="utf-8") as log, isolated_desktop_options() as desktop_options:
                args=boot_command(layout['exe'], browser)
                if desktop_options:
                    process=desktop_process(args,**desktop_options,env=environment,stdout=log,cwd=base)
                else:
                    process=subprocess.Popen(args,env=environment,stdout=log,stderr=log,cwd=base,**launch_options())
                try:
                    while time.monotonic() - started < timeout:
                        patch.require(process.poll() is None, "Isolated boot probe exited: " + str(process.returncode))
                        checks = inspect_children(process_table(), process.pid, browser, patch.cli_path(app))
                        if sys.platform == 'darwin':
                            patch.require(checks['mock_keychain'], 'Isolated Mac probe must use its mock keychain')
                        evidence.update(checks)
                        if checks["isolated_profile"] and checks["renderer"] and checks["backend"]:
                            # Require the complete initialized process tree to stay up.
                            time.sleep(2)
                            patch.require(process.poll() is None, "Isolated boot probe exited after startup")
                            checks = inspect_children(process_table(), process.pid, browser, patch.cli_path(app))
                            evidence.update(checks)
                            if checks["isolated_profile"] and checks["renderer"] and checks["backend"]:
                                evidence.update(status="passed", elapsed_seconds=round(time.monotonic() - started, 2))
                                break
                        time.sleep(1)
                    patch.require(evidence["status"] == "passed", "Isolated renderer and app-server backend did not remain ready before timeout")
                finally:
                    stop_probe(process)
        manifest["boot_probe"] = "passed"
    except BaseException as error:
        manifest["boot_probe"] = "failed"
        evidence["error"] = str(error)[:1000]
        raise
    finally:
        patch.save_json(directory / "boot-result.json", evidence)
        patch.save_json(directory / "manifest.json", manifest)
    print("PASS: isolated patched app, renderer and app-server startup.")
    return evidence


if __name__ == "__main__":
    main(sys.argv[1])
