"""Validate local model registration and thread routing without an inference turn."""
from __future__ import annotations

from pathlib import Path
import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import patch

DUMMY_KEY_VARIABLE = "CODEX_PATCH_OFFLINE_PROBE_KEY"
DUMMY_KEY = "local-offline-probe-placeholder"
LOOPBACK_BASE_URL = "http://127.0.0.1:9/"
ALLOWED_METHODS = frozenset(("initialize", "initialized", "model/list", "thread/start"))


def isolated_environment(base):
    """Carry OS runtime paths; no real provider credentials or user config."""
    base = Path(base)
    names = {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "LANG", "LC_ALL", "LC_CTYPE"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in names}
    home, codex, temporary = base / "user", base / "home", base / "temp"
    for directory in (home, codex, temporary, base / "appdata", base / "localappdata"):
        directory.mkdir(parents=True, exist_ok=True)
    environment.update(HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(codex),
                       TMPDIR=str(temporary), TEMP=str(temporary), TMP=str(temporary),
                       APPDATA=str(base / "appdata"), LOCALAPPDATA=str(base / "localappdata"),
                       USER="offline-probe", USERNAME="offline-probe", NO_PROXY="*", no_proxy="*")
    if os.name == "nt":
        drive, tail = os.path.splitdrive(str(home))
        environment.update(HOMEDRIVE=drive, HOMEPATH=tail)
    return environment


def offline_config(catalog, proxy_models=(), routes=None):
    result = ("model = " + json.dumps(patch.MODEL) + "\nmodel_provider = " + json.dumps(patch.PROVIDER) +
            "\nmodel_catalog_json = " + json.dumps(str(Path(catalog).resolve())) + "\n" +
            "web_search = \"disabled\"\n[features]\nmemories = false\nplugins = false\nremote_plugin = false\n" +
            "\n[model_providers." + patch.PROVIDER + "]\nname = \"DeepSeek offline routing probe\"\n" +
            "base_url = " + json.dumps(LOOPBACK_BASE_URL) + "\nwire_api = \"responses\"\n" +
            "env_key = " + json.dumps(DUMMY_KEY_VARIABLE) + "\nrequires_openai_auth = false\n")
    if proxy_models:
        import proxy_support as ps
        result += ('\n[model_providers.' + ps.PROVIDER + ']\nname = "Proxy offline probe"\n'
                   + 'base_url = ' + json.dumps(LOOPBACK_BASE_URL) + '\nwire_api = "responses"\n'
                   + 'env_key = ' + json.dumps(DUMMY_KEY_VARIABLE) + '\nrequires_openai_auth = false\n')
    if routes is not None:
        for provider in {r['provider'] for r in routes.values()}-{'deepseek-desktop','easycli-desktop'}:
            result+='\n[model_providers.'+provider+']\nname="Local offline probe"\nbase_url='+json.dumps(LOOPBACK_BASE_URL)+'\nwire_api="responses"\nenv_key='+json.dumps(DUMMY_KEY_VARIABLE)+'\nrequires_openai_auth=false\n'
    return result


def probe_command(app):
    """Keep installation checks independent of background plugin downloads."""
    return [str(patch.cli_path(app)), "--disable", "plugins", "--disable", "remote_plugin",
            "app-server", "--listen", "stdio://"]


def stop_probe(process, timeout=5):
    """Only stop a process/group created by this probe, never a named app."""
    if sys.platform == "win32":
        if process.poll() is None:
            subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, **patch.background_options())
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Popen's owned process handle cannot refer to another app instance.
            process.kill()
            process.wait(timeout=timeout)
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=timeout)
    # A helper may outlive its parent after SIGTERM. Its isolated process group
    # remains the group started by this probe.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def launch_options():
    if sys.platform == "win32":
        options = patch.background_options()
        options['creationflags'] = options.get('creationflags', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
        return options
    return {"start_new_session": True}


class RpcClient:
    def __init__(self, process):
        self.process = process
        self.events = queue.Queue()
        self.counter = 0
        self.methods = []
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    self.events.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
        finally:
            self.events.put(None)

    def send(self, method, parameters=None, notification=False):
        patch.require(method in ALLOWED_METHODS, "The offline probe may not submit inference or tool requests")
        message = {"method": method}
        if parameters is not None:
            message["params"] = parameters
        if not notification:
            self.counter += 1
            message["id"] = self.counter
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()
        self.methods.append(method)
        return message.get("id")

    def receive(self, identifier, timeout=45):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Offline app-server response timed out")
            try:
                event = self.events.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError("Offline app-server response timed out") from None
            patch.require(event is not None, "Offline app-server closed its output before responding")
            if event.get("id") != identifier:
                continue
            patch.require("error" not in event, "Offline app-server rejected a local request: " + json.dumps(event.get("error")))
            patch.require("result" in event, "Offline app-server response has no result")
            return event["result"]


def check_session(client, work, proxy_models=(), routes=None):
    client.receive(client.send("initialize", {"clientInfo": {"name": "deepseek_patch_offline_probe", "version": "1.0"},
                                              "capabilities": {"experimentalApi": True}}))
    client.send("initialized", notification=True)
    models = []
    cursor = None
    seen = set()
    for _ in range(20):
        parameters = {"includeHidden": False, "limit": 100}
        if cursor is not None:
            parameters["cursor"] = cursor
        page = client.receive(client.send("model/list", parameters))
        patch.require(isinstance(page.get("data"), list), "Offline model catalog has an unknown shape")
        models.extend(page["data"])
        cursor = page.get("nextCursor")
        if not cursor:
            break
        patch.require(cursor not in seen, "Offline model catalog repeated its pagination cursor")
        seen.add(cursor)
    else:
        raise patch.PatchError("Offline model catalog pagination limit exceeded")
    deepseek = [model for model in models if str(model.get("model", "")).startswith("deepseek-")]
    ds_models=[m for m,r in routes.items() if r['provider']==patch.PROVIDER] if routes is not None else [patch.MODEL]
    patch.require(sorted(m.get('model') for m in deepseek)==sorted(ds_models),
                  "Expected exactly one DeepSeek model in the local catalog")
    for ds_model in ds_models:
        result = client.receive(client.send("thread/start", {"model": ds_model, "modelProvider": patch.PROVIDER,
                              "cwd": str(work), "ephemeral": True, "approvalPolicy": "never", "sandbox": "read-only",
                              "config": {"web_search": "disabled", "features.memories": False}}))
        patch.require(result.get("model") == ds_model, "Offline thread selected a different model")
        patch.require(result.get("modelProvider") == patch.PROVIDER, "Offline thread selected a different provider")
        patch.require(bool(result.get("thread", {}).get("id")), "Offline thread was not created")
    known=set(proxy_models)
    found = [m.get("model") for m in models if m.get("model") in known]
    patch.require(sorted(found) == sorted(proxy_models), "Proxy catalog differs from the selected model IDs")
    checked = []
    for model in proxy_models:
        thread = client.receive(client.send("thread/start", {"model": model, "modelProvider": 'easycli-desktop',
                                "cwd": str(work), "ephemeral": True, "approvalPolicy": "never", "sandbox": "read-only",
                                "config": {"web_search": "disabled", "features.memories": False}}))
        patch.require(thread.get("model") == model and thread.get("modelProvider") == 'easycli-desktop'
                      and thread.get("thread", {}).get("id"), "Incorrect proxy thread model/provider")
        checked.append({"model": model, "provider": 'easycli-desktop'})
    local_checked=[]
    for model,route in (routes or {}).items():
        if route['provider'] in (patch.PROVIDER,'easycli-desktop'):continue
        patch.require(any(m.get('model')==model for m in models),"Local model missing from catalog")
        thread=client.receive(client.send("thread/start",{"model":model,"modelProvider":route['provider'],
                   "cwd":str(work),"ephemeral":True,"approvalPolicy":"never","sandbox":"read-only",
                   "config":{"web_search":"disabled","features.memories":False}}))
        patch.require(thread.get("model")==model and thread.get("modelProvider")==route['provider'],"Incorrect local model route")
        local_checked.append(model)
    return {"model": patch.MODEL, "provider": patch.PROVIDER, "deepseek_models": len(deepseek),
            "proxy_models": checked, "local_models":local_checked,
            "catalog_models": len(models), "ephemeral_thread": True, "inference_turns": 0,
            "provider_base_url": LOOPBACK_BASE_URL, "methods": client.methods}


def main(directory):
    directory, manifest = patch.load_build(Path(directory))
    patch.require(manifest["status"] == "prepared", "Only prepared builds can be offline-tested")
    app = directory / "patched.app"
    patch.verify_app(app, manifest["patched_asar"])
    manifest["offline_probe"] = "running"
    patch.save_json(directory / "manifest.json", manifest)
    evidence = {"status": "failed", "inference_turns": 0}
    try:
        with tempfile.TemporaryDirectory(prefix="codex-deepseek-offline-") as temporary:
            base = Path(temporary)
            environment = isolated_environment(base)
            environment[DUMMY_KEY_VARIABLE] = DUMMY_KEY
            work = base / "work"
            work.mkdir()
            proxy_models = manifest.get("proxy", {}).get("models", [])
            (base / "home/config.toml").write_text(offline_config(directory / "desktop-models.json", proxy_models, manifest.get("model_routes")), encoding="utf-8")
            with (directory / "offline-stderr.log").open("w", encoding="utf-8") as log:
                process = subprocess.Popen(probe_command(app),
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                                           text=True, encoding="utf-8", errors="replace", env=environment,
                                           cwd=work, **launch_options())
                try:
                    evidence.update(check_session(RpcClient(process), work, proxy_models, manifest.get('model_routes')))
                    evidence["status"] = "passed"
                finally:
                    stop_probe(process)
                    if process.stdin:
                        process.stdin.close()
                    if process.stdout:
                        process.stdout.close()
        manifest["offline_probe"] = "passed"
    except BaseException as error:
        manifest["offline_probe"] = "failed"
        evidence["error"] = str(error)[:1000]
        raise
    finally:
        patch.save_json(directory / "offline-result.json", evidence)
        patch.save_json(directory / "manifest.json", manifest)
    print("PASS: local DeepSeek catalog and ephemeral thread routing; no inference turns submitted.")
    return evidence


if __name__ == "__main__":
    main(sys.argv[1])
