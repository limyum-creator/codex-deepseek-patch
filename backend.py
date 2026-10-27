"""Validated update builds and the native monitor's installation adapter."""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile

import patch as p


def refresh_config(current, catalog, managed):
    """Replace only a known root catalog assignment, preserving user edits verbatim."""
    parsed = p.tomllib.loads(current)
    p.require(parsed.get("model_catalog_json") == managed["catalog"], "The managed model catalog was changed; review the configuration")
    p.require(parsed.get("model_providers", {}).get(p.PROVIDER) == managed["provider"],
              "The DeepSeek provider was changed; review the configuration")
    if managed.get("proxy_provider") is not None:
        import proxy_support as ps
        p.require(parsed.get("model_providers", {}).get(ps.PROVIDER) == managed["proxy_provider"],
                  "The managed proxy provider was changed; review the configuration")
    for identifier,provider in managed.get("local_providers",{}).items():
        p.require(parsed.get("model_providers",{}).get(identifier)==provider,"A managed local provider changed")
    lines = current.splitlines(keepends=True)
    matches = []
    for i, line in enumerate(lines):
        if re.match(r"\s*\[", line):
            break
        if re.match(r'\s*(?:model_catalog_json|"model_catalog_json"|\'model_catalog_json\')\s*=', line):
            matches.append(i)
    p.require(len(matches) == 1, "Cannot safely identify the managed root catalog assignment")
    lines[matches[0]] = "model_catalog_json = " + json.dumps(str(catalog)) + "\n"
    result = "".join(lines)
    expected = copy.deepcopy(parsed)
    expected["model_catalog_json"] = str(catalog)
    p.require(p.tomllib.loads(result) == expected, "Unrelated configuration changed")
    return result


def managed_baseline(directory, account, service):
    directory = Path(directory).resolve()
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    installed = directory / "config-installed.toml"
    if installed.is_file():
        parsed = p.tomllib.loads(installed.read_text(encoding="utf-8"))
        catalog = parsed["model_catalog_json"]
        provider = parsed.get("model_providers",{}).get(p.PROVIDER)
    else:
        # Compatibility with the original local installer, before public v0.1.0.
        p.require(sys.platform == "darwin" and manifest.get("patched_asar"), "Baseline configuration record is missing")
        catalog = str(directory / "desktop-models.json")
        provider = p.tomllib.loads(p.provider_config(account, service))["model_providers"][p.PROVIDER]
    p.require(Path(catalog).is_file(), "The previous managed catalog is missing")
    result = {"catalog": catalog, "provider": provider, "baseline": str(directory)}
    if manifest.get("proxy"):
        import proxy_support as ps
        result["proxy"] = ps.validate(manifest["proxy"])
        result["proxy_provider"] = parsed["model_providers"][ps.PROVIDER]
    if "deepseek_enabled" in manifest:result["deepseek_enabled"]=manifest["deepseek_enabled"]
    if manifest.get("locals"):
        result["locals"]=manifest["locals"]
        import local_support
        result["local_providers"]={identifier:parsed.get("model_providers",{}).get(identifier)
                                   for r in result["locals"] for identifier,_ in local_support.provider_ids(r)}
    if manifest.get('macos_signing'):
        result['macos_signing'] = manifest['macos_signing']
    return result


def verify_official(app):
    p.verify_app(app)
    if sys.platform == "win32":
        from windows_support import verify_publisher
        return verify_publisher(app)
    result = subprocess.run(["/usr/bin/codesign", "-d", "--verbose=4", str(app)],
                            check=True, capture_output=True, text=True)
    p.require("TeamIdentifier=2DC432GLL2" in result.stderr.splitlines(),
              "Expected an original OpenAI-signed app; the selected source has a different signature")
    return {"team": "2DC432GLL2"}


def inspect_source(app, node=None, routes=None, native_contexts=None):
    from compatibility import locate_bundle, apply_source
    p.mac_only()
    layout = p.app_layout(app)
    publisher = verify_official(layout["app"])
    source_hash = p.file_sha(layout["asar"])
    central, source, _ = locate_bundle(layout["asar"])
    route_source=(p.ROOT / "route.js").read_text(encoding="utf-8")
    if routes is not None:
        from model_sync import render_route
        route_source=render_route(route_source,routes,native_contexts)
    patched, evidence = apply_source(source, route_source)
    if routes is not None:evidence['model_routes']=routes
    node = node or shutil.which("node")
    p.require(node, "Node.js is required for offline compatibility checks")
    with tempfile.TemporaryDirectory(prefix="codex-ds-contract-") as temp:
        path = Path(temp) / "contracts.json"
        path.write_text(json.dumps(evidence), encoding="utf-8")
        result = p.run([node, p.ROOT / "scripts/check_compatibility.cjs", path], capture_output=True, text=True, timeout=60)
        probe = json.loads(result.stdout)
        p.require(probe.get("status") == "passed", "App-server request contract probe failed")
    windows_update = None
    if sys.platform == "win32":
        from windows_update_ui import locate_bundle as locate_updater
        member, updated, contract = locate_updater(layout["asar"])
        with tempfile.TemporaryDirectory(prefix="codex-update-contract-") as temp:
            script = Path(temp)/'updater.cjs'
            script.write_text(updated, encoding='utf-8')
            p.run([node, '--check', script], capture_output=True, timeout=60)
        windows_update = {"member": member, "patched": updated, "evidence": contract}
    p.require(p.file_sha(layout["asar"]) == source_hash, "App changed during compatibility checks")
    return {"central": central, "patched": patched, "evidence": evidence, "probe": probe,
            "publisher": publisher, "source_asar": source_hash, "windows_update": windows_update}


def build_compatible(args):
    p.mac_only()
    app = args.app
    if app is None and sys.platform == "win32":
        from windows_support import discover_app
        app = discover_app()
    app = p.app_layout(app)["app"]
    home, output = args.codex_home.resolve(), args.output.resolve()
    p.require(not output.exists(), "Choose a new output directory; builds are never overwritten")
    p.require(not output.is_relative_to(app) and not app.is_relative_to(output) and output != home,
              "Build directory overlaps the app or configuration root")
    p.require(home.is_dir(), "Codex home must exist")
    managed = getattr(args, "managed", None)
    if getattr(args, "baseline", None):
        managed = managed_baseline(args.baseline, args.keychain_account, args.keychain_service)
    proxy=managed.get("proxy") if managed else None
    locals=managed.get("locals",[]) if managed else []
    deepseek=getattr(args,"deepseek_enabled",None)
    if deepseek is None:deepseek=managed.get("deepseek_enabled",True) if managed else False
    if getattr(args,"connections",None):
        import local_support
        locals=local_support.load(args.connections)
    model_snapshot=getattr(args,"model_snapshot",None)
    if isinstance(model_snapshot,Path):model_snapshot=json.loads(model_snapshot.read_text(encoding="utf-8"))
    if model_snapshot:
        p.require(model_snapshot.get("edition")==p.EDITION,"Model snapshot belongs to another edition")
        from model_sync import snapshot_digest
        digest=snapshot_digest(model_snapshot)
        p.require(digest==model_snapshot["digest"],"Model snapshot digest mismatch")
        locals=model_snapshot.get("locals",locals)
        deepseek=model_snapshot.get("deepseek_enabled",deepseek)
        if proxy:proxy={**proxy,"models":[m for m,r in model_snapshot["routes"].items() if r["provider"]=="easycli-desktop"]}
    if getattr(args,"proxy_config",None):
        import proxy_support as ps
        proxy=ps.validate({"config":str(args.proxy_config.resolve()),"base_url":args.proxy_base_url,"models":args.proxy_models or list(ps.MODELS)})
        ps.read_api_key(proxy["config"])
    install_target = getattr(args, "install_target", None)
    replaced_asar = None
    if install_target:
        p.require(sys.platform == "darwin" and managed, "Refreshing an installed app requires a macOS managed baseline")
        install_target = install_target.resolve()
        baseline_manifest = json.loads((Path(managed["baseline"]) / "manifest.json").read_text(encoding="utf-8"))
        p.require(install_target != app and not output.is_relative_to(install_target) and not install_target.is_relative_to(output),
                  "Install target overlaps the source or output")
        if baseline_manifest.get("target"):
            p.require(install_target == Path(baseline_manifest["target"]).resolve(), "Install target differs from the baseline")
        replaced_asar = baseline_manifest["patched_asar"]
        p.verify_app(install_target, replaced_asar)
    current=p.read_config(home/"config.toml")
    p.prepared_config(current,output/"desktop-models.json",args.keychain_account,args.keychain_service,
                      managed=managed,helper=output/"windows_support.py",proxy=proxy,deepseek=deepseek,locals=locals)
    cached=getattr(args,"inspection",None)
    if cached:p.require(p.file_sha(p.app_layout(app)["asar"])==cached["source_asar"],"App changed after compatibility checks")
    if model_snapshot:
        catalog=model_snapshot["catalog"];routes=model_snapshot["routes"]
    else:
        verify_official(app)
        with tempfile.TemporaryDirectory(prefix="codex-model-catalog-") as temp:
            raw=subprocess.check_output([str(p.cli_path(app)),"debug","models","--bundled"],stderr=subprocess.PIPE,
                                        env={**os.environ,"CODEX_HOME":temp},timeout=90,**p.background_options())
        catalog=p.make_catalog(json.loads(raw),deepseek=deepseek)
        if proxy:
            import proxy_support
            catalog=proxy_support.add_catalog(catalog,proxy["models"])
        from model_sync import apply_reductions
        routes={}
        native_template=p.catalog_template(catalog)
        for entry in catalog["models"]:
            provider=p.PROVIDER if deepseek and entry["slug"]==p.MODEL else "easycli-desktop" if proxy and entry["slug"] in proxy["models"] else None
            if provider:
                config=p.tomllib.loads(current)
                compact=apply_reductions(entry,{},config.get("model_context_window") if provider==p.PROVIDER else None)
                if provider==p.PROVIDER and config.get("model_auto_compact_token_limit"):compact=min(compact,config["model_auto_compact_token_limit"])
                routes[entry["slug"]]={"provider":provider,"context_window":entry["context_window"],"max_context_window":entry["max_context_window"],"compact_limit":compact}
        if locals:
            import local_support as ls
            for connection in locals:
                specs,warnings=ls.discover(connection)
                connection["chat_only_models"]=[m for m,spec in specs.items() if not spec.get("supports_tools",True)]
                connection["normalize_instructions_models"]=[m for m,spec in specs.items() if spec.get("normalize_instructions")]
                connection["tool_adapter_models"]=[m for m,spec in specs.items() if ls.needs_tool_adapter(spec)]
                p.require(specs,"No usable model specifications for "+connection["id"]+": "+"; ".join(warnings))
                for model,spec in specs.items():
                    p.require(not any(e["slug"]==model for e in catalog["models"]),"Model ID belongs to more than one provider: "+model)
                    entry=ls.make_entry(native_template,model,connection,spec,100+len(routes))
                    compact=apply_reductions(entry,{},spec.get("configured_context_window") if connection.get("respect_server_context") else None)
                    catalog["models"].append(entry)
                    routes[model]={"provider":connection["id"]+("-compat" if not spec.get("supports_tools",True) or spec.get("normalize_instructions") or ls.needs_tool_adapter(spec) else ""),"context_window":entry["context_window"],"max_context_window":entry["max_context_window"],"compact_limit":compact,
                                   "supports_tools":spec.get("supports_tools",True)}
    allowed={p.PROVIDER} if deepseek else set()
    if proxy:allowed.add("easycli-desktop")
    if locals:
        import local_support
        allowed.update(identifier for r in locals for identifier,_ in local_support.provider_ids(r))
    p.require(all(r["provider"] in allowed for r in routes.values()),"Snapshot references an unconfigured provider")
    native_contexts={m["slug"]:{"window":m["context_window"],"maximum":m.get("max_context_window",m["context_window"])}
                     for m in catalog["models"] if m["slug"].startswith(("gpt-","codex-")) and m.get("context_window")}
    prepared=inspect_source(app,getattr(args,"node_path",None),routes,native_contexts)
    layout=p.app_layout(app);original_hash=p.file_sha(layout["asar"])
    p.require(original_hash==prepared["source_asar"],"App changed after compatibility checks")
    output.mkdir(parents=True, mode=0o700)
    patched = prepared["patched"].encode()
    source = output / "patched-client.mjs"
    source.write_bytes(patched)
    p.run([getattr(args, "node_path", None) or shutil.which("node"), "--check", source], capture_output=True, timeout=60)
    p.save_json(output / "desktop-models.json", catalog)
    if model_snapshot:p.save_json(output/'synced-models.json',model_snapshot)
    if proxy:
        import proxy_support
        proxy_support.stage_helpers(output,proxy)
    if locals:
        import local_support
        local_support.stage_helpers(output,locals)
    p.save_json(output / "compatibility.json", {k: v for k, v in prepared.items() if k not in ("patched",)})
    if sys.platform == "win32":
        shutil.copy2(p.ROOT / "windows_support.py", output / "windows_support.py")
    original, stage = output / "original.app", output / "patched.app"
    p.clone_app(app, original)
    p.clone_app(app, stage)
    p.verify_app(original, original_hash)
    archive = p.app_layout(original)["asar"]
    stage_archive = p.app_layout(stage)["asar"]
    replacements = {prepared["central"]: patched}
    if prepared.get("windows_update"):
        update = prepared["windows_update"]
        replacements[update["member"]] = update["patched"].encode('utf-8')
    new_hash = p.repack(archive, output / "patched.asar", replacements)
    os.replace(output / "patched.asar", stage_archive)
    if sys.platform == "win32":
        from windows_support import update_integrity
        update_integrity(stage, new_hash)
    else:
        info = p.app_info(stage)
        old = copy.deepcopy(info["ElectronAsarIntegrity"])
        info["ElectronAsarIntegrity"]["Resources/app.asar"]["hash"] = new_hash
        plist = stage / "Contents/Info.plist"
        fmt = plistlib.FMT_BINARY if plist.read_bytes().startswith(b"bplist") else plistlib.FMT_XML
        plist.write_bytes(plistlib.dumps(info, fmt=fmt, sort_keys=False))
        framework = p.framework_path(stage)
        framework.write_bytes(p.patch_framework_bytes(framework.read_bytes(), old, info["ElectronAsarIntegrity"]))
        from macos_signing import sign_app
        signing = sign_app(stage, previous=(managed or {}).get("macos_signing"))
    patched_hash = p.file_sha(stage_archive)
    p.verify_app(stage, patched_hash)
    count = p.compare_archives(archive, stage_archive, list(replacements))
    manifest = {"format": 1, "adaptive": True, "tool_version": p.VERSION,
                "app_version": layout["version"], "app_build": layout["build"],
                "target": str(app), "codex_home": str(home), "status": "prepared",
                "original_asar": original_hash, "patched_asar": patched_hash,
                "catalog_sha256": p.file_sha(output / "desktop-models.json"),
                "keychain_account": args.keychain_account, "keychain_service": args.keychain_service,
                "changed_members": list(replacements), "archive_members": count,
                "managed": managed, "contract_assertions": prepared["probe"].get("assertions"),
                "built_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    if sys.platform == "win32":
        manifest["deployment_target"] = str(Path(os.environ["LOCALAPPDATA"]) / "CodexDeepSeekPatch/app")
    if proxy:
        manifest["proxy"] = proxy
    manifest.update(edition=p.EDITION,deepseek_enabled=deepseek,locals=locals,model_routes=routes)
    manifest['reasoning_probe_required']=True
    manifest['switching_probe_required']=bool(routes)
    manifest['tool_loading_probe_required']=bool(routes)
    if model_snapshot:
        manifest['model_routes']=model_snapshot['routes']
    if sys.platform == 'darwin':
        manifest.update(macos_signing=signing, stable_signing_required=True)
    if getattr(args,'update_metadata',None):manifest['update']=args.update_metadata
    if install_target:
        manifest.update(source_target=str(app), target=str(install_target), replaced_asar=replaced_asar)
    p.save_json(output / "manifest.json", manifest)
    print("Prepared and verified:", output, flush=True)
    return manifest


def credential_available(manifest):
    if sys.platform == "win32":
        from windows_support import credential_get
        try:
            return bool(credential_get(manifest["keychain_service"]))
        except OSError:
            return False
    result = subprocess.run(["/usr/bin/security", "find-generic-password", "-a", manifest["keychain_account"],
                             "-s", manifest["keychain_service"], "-w"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.returncode == 0


def install_compatible(directory, manifest):
    source = Path(manifest.get("source_target", manifest["target"]))
    app = Path(manifest.get("deployment_target", manifest["target"]))
    config = Path(manifest["codex_home"]) / "config.toml"
    p.require(manifest["status"] == "prepared", "Build is not prepared")
    p.require_stopped(source, read_only_source=sys.platform == "win32")
    if app != source and app.exists():
        p.require_stopped(app)
    p.verify_app(source, manifest["original_asar"])
    if manifest.get("replaced_asar"):
        p.verify_app(app, manifest["replaced_asar"])
    p.verify_app(directory / "patched.app", manifest["patched_asar"])
    if manifest.get("stable_signing_required"):
        from macos_signing import verify_app_identity
        verify_app_identity(directory / "patched.app", manifest["macos_signing"])
    p.require(p.file_sha(directory / "desktop-models.json") == manifest["catalog_sha256"], "Catalog changed")
    p.require(manifest.get("boot_probe") == "passed" and manifest.get("offline_probe") == "passed",
              "Both isolated boot and offline app-server probes must pass before installation")
    if manifest.get('reasoning_probe_required'):
        p.require(manifest.get('reasoning_probe')=='passed','Run the reasoning selection probe before installation')
    if manifest.get('switching_probe_required'):
        p.require(manifest.get('switching_probe')=='passed','Run the saved-chat switching probe before installation')
    if manifest.get('tool_loading_probe_required'):
        p.require(manifest.get('tool_loading_probe')=='passed','Run the large-tool discovery/compaction probe before installation')
    if manifest.get("deepseek_enabled",True):
        p.require(credential_available(manifest),"DeepSeek credential is unavailable; run setup-key")
    if manifest.get('model_routes'):
        p.require(manifest.get('context_probe')=='passed','Run the offline context/compaction probe before installation')
    if manifest.get("proxy"):
        import proxy_support as ps
        p.require(manifest.get("context_probe") == "passed", "Run the offline context/compaction probe before installing proxy models")
        ps.read_api_key(manifest["proxy"]["config"])
    p.require(manifest.get('edition',p.EDITION)==p.EDITION,'Build belongs to a different edition')
    current = p.read_config(config)
    updated = p.prepared_config(current, directory / "desktop-models.json", manifest["keychain_account"],
                                manifest["keychain_service"], managed=manifest.get("managed"),
                                helper=directory / "windows_support.py",proxy=manifest.get("proxy"),
                                deepseek=manifest.get("deepseek_enabled",True),locals=manifest.get("locals",[]))
    existed = config.exists()
    p.atomic_text(directory / "config-before.toml", current)
    p.atomic_text(directory / "config-installed.toml", updated)
    def commit():
        p.verify_app(app, manifest["patched_asar"])
        if manifest.get("stable_signing_required"):
            verify_app_identity(app, manifest["macos_signing"])
        p.require(p.read_config(config) == updated, "Configuration verification failed")
        manifest.update(status="installed", config_existed=existed, installed_config_hash=p.sha(updated.encode()),
                        installed_at=dt.datetime.now(dt.timezone.utc).isoformat())
        p.save_json(directory / "manifest.json", manifest)
    if sys.platform == "win32":
        from windows_install import install as install_windows
        install_windows(manifest, directory, config, current, updated, commit)
    else:
        p.install_transaction(app, directory / "patched.app", config, current, updated, commit)
    print("Installed verified DeepSeek patch.", flush=True)
    return manifest


def restore_windows(directory, manifest):
    from windows_install import restore
    return restore(manifest, directory)


def snapshot_app(app):
    layout = p.app_layout(app)
    files = [layout["asar"], layout["exe"], layout["cli"]]
    if sys.platform == "darwin":
        files.extend([app / "Contents/Info.plist", p.framework_path(app)])
    else:
        carrier = app / "chrome.dll"
        if carrier.is_file():
            files.append(carrier)
    hashes = {str(path.relative_to(app)): p.file_sha(path) for path in files}
    identity = {"path": str(app), "version": layout["version"], "build": layout["build"], "hashes": hashes}
    return {**identity, "fingerprint": p.sha(json.dumps(identity, sort_keys=True).encode())}


class MonitorBackend:
    def __init__(self, settings):
        self.settings = settings
        self.app = Path(settings["app"])
        self.inspection = None
        self.baseline = Path(settings["baseline"]) if settings.get("baseline") else None
        self.store = False
        if sys.platform == "win32":
            from windows_support import is_store_app
            self.store = is_store_app(self.app)

    def snapshot(self):
        if self.store:
            from windows_support import discover_app
            self.app = discover_app()
        return snapshot_app(self.app)

    def inspect(self, snapshot):
        try:
            self.inspection = inspect_source(self.app, self.settings.get("node_path"))
            return {"compatible": True, "assertions": self.inspection["probe"].get("assertions")}
        except Exception as error:
            return {"compatible": False, "reason": str(error)}

    def build(self, snapshot, output):
        p.require(self.snapshot()["fingerprint"] == snapshot["fingerprint"], "App changed before build")
        args = argparse.Namespace(app=self.app, codex_home=Path(self.settings["codex_home"]), output=output,
                                  compatible=True, bundled_catalog=True, baseline=self.baseline,
                                  keychain_account=self.settings["keychain_account"],
                                  keychain_service=self.settings["keychain_service"],
                                  node_path=self.settings.get("node_path"), inspection=self.inspection)
        built = build_compatible(args)
        from update_worker import verify_build
        verify_build(output)
        return {"directory": str(output)}

    def is_running(self):
        try:
            p.require_stopped(self.app, read_only_source=sys.platform == "win32")
            if sys.platform == "win32":
                target = Path(os.environ["LOCALAPPDATA"]) / "CodexDeepSeekPatch/app"
                if target.exists():
                    p.require_stopped(target)
            return False
        except (p.PatchError, ValueError):
            return True

    def install(self, build, snapshot):
        p.require(self.snapshot()["fingerprint"] == snapshot["fingerprint"], "App changed before installation")
        directory, manifest = p.load_build(Path(build["directory"]))
        install_compatible(directory, manifest)
        self.baseline = directory
        return {"installed_fingerprint": self.snapshot()["fingerprint"], "baseline": str(directory)}

    def adopt(self):
        recovery = self.settings.get("recovery_build")
        if recovery:
            candidate = Path(recovery)
            record = json.loads((candidate / "manifest.json").read_text(encoding="utf-8"))
            if record.get("status") == "installed":
                self.baseline = candidate
        if not self.baseline:
            return None
        manifest = json.loads((self.baseline / "manifest.json").read_text(encoding="utf-8"))
        app = Path(manifest.get("deployment_target", str(self.app)))
        if not app.exists():
            return None
        if p.file_sha(p.app_layout(app)["asar"]) != manifest.get("patched_asar"):
            return None
        p.verify_app(app, manifest["patched_asar"])
        if manifest.get("deployment_target"):
            if p.file_sha(p.app_layout(self.app)["asar"]) != manifest["original_asar"]:
                return None
        managed = managed_baseline(self.baseline, self.settings["keychain_account"], self.settings["keychain_service"])
        current = p.read_config(Path(self.settings["codex_home"]) / "config.toml")
        refresh_config(current, managed["catalog"], managed)
        return {"fingerprint": self.snapshot()["fingerprint"], "baseline": str(self.baseline),
                "build": {"directory": str(self.baseline)}}
