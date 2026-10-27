"""Stage a model adapter for the original signed Codex client.

Staging reads the official app and creates only configuration/runtime artifacts.
It never copies, changes or signs any part of the app. Native installation and
legacy recovery are separate transactions, after the staged probes pass.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile

import patch as p
from official_client_config import adapt_catalog, migrate_legacy_routes, prepare_config


ARCHITECTURE = "official-client-v1"
ADAPTER_FILES = ("inference_adapter.py", "inference_transport.py", "official_client_host.py",
                 "responses_compat.py", "chat_completions_compat.py", "proxy_support.py", "windows_support.py", "legacy_history.py", "reasoning_origin.py", "local_history.py")


def endpoint(port, token):
    return "http://127.0.0.1:" + str(port) + "/" + token + "/backend-api/codex"


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def compression_node(app, node=None):
    """Resolve the vendor's codec; never install another runtime or change flags."""
    try:
        from compression import zstd
        return None
    except ImportError:
        pass
    app = Path(app)
    if sys.platform == 'linux':
        node = Path(node or shutil.which('node') or '/missing-node').resolve()
    else:
        node = app / ('Contents/Resources/cua_node/bin/node' if sys.platform == 'darwin'
                      else 'resources/cua_node/bin/node.exe')
    p.require(node.is_file(), 'The official app request compression runtime is unavailable')
    from inference_transport import zstd_body
    sample = b'native-request-codec\x00'
    p.require(zstd_body(zstd_body(sample, decompress=False, node=node), node=node) == sample,
              'The official app request compression runtime failed validation')
    return str(node)


def inspect(app):
    from official_client_update import verify_official
    app = Path(app).resolve()
    publisher = verify_official(app)
    layout = p.app_layout(app)
    return {"compatible":True, "architecture":ARCHITECTURE, "publisher":publisher,
            "source_asar":p.file_sha(layout.get("source_artifact", layout.get("asar"))),
            "app_version":layout["version"], "app_build":layout["build"]}


def adapter_routes(manifest, routes, platform=None):
    """Resolve known model routes without reading credentials or changing services."""
    platform = platform or sys.platform
    result = {}
    for model, route in routes.items():
        provider = route["provider"]
        if provider == p.PROVIDER:
            credential = ({"kind":"windows", "target":manifest["keychain_service"]}
                          if platform == "win32" else
                          {"kind":"keychain", "account":manifest["keychain_account"],
                           "service":manifest["keychain_service"]})
            result[model] = {"base_url":"https://api.deepseek.com", "credential":credential}
        elif provider == "easycli-desktop":
            proxy = manifest.get("proxy")
            p.require(proxy, "An existing proxy route has no connection configuration")
            result[model] = {"base_url":proxy["base_url"],
                             "credential":{"kind":"proxy", "path":proxy["config"]},
                             "tool_adapter":True}
        else:
            import local_support
            connection = next((item for item in manifest.get("locals", [])
                               if provider in dict(local_support.provider_ids(item))), None)
            p.require(connection, "A local model route has no connection configuration")
            value = {"base_url":connection["base_url"],
                     "wire_api":connection.get("wire_api", "responses"),
                     "supports_reasoning_effort":connection.get("supports_reasoning_effort", False),
                     "tool_adapter":model in connection.get("tool_adapter_models", []),
                     "flatten_namespaces":model in connection.get("tool_adapter_models", []),
                     "chat_only":model in connection.get("chat_only_models", []),
                     "normalize_instructions":model in connection.get("normalize_instructions_models", [])}
            if value["wire_api"]=="anthropic_messages":
                spec=manifest.get("model_specifications",{}).get(model,{})
                spec={**spec,**connection.get("model_specs",{}).get(model,{})}
                value.update({k:spec[k] for k in ("max_output_tokens","reasoning_levels","thinking_mode") if spec.get(k) is not None})
            if connection.get("key_file"):
                value["credential"] = {"kind":"file", "path":connection["key_file"]}
            result[model] = value
        result[model]['context_window'] = route.get('context_window')
    return result


def stage(*, app, codex_home, output, baseline, edition=None, model_snapshot=None,
          install_target=None, update_metadata=None, model_preferences=None, initial=None,
          compression_runtime=None):
    """Create a private, reviewable build; require explicit validation to install."""
    from official_client_update import verify_official
    from official_client_host import windowless_python
    app, codex_home, output = map(lambda x: Path(x).resolve(), (app, codex_home, output))
    baseline = Path(baseline).resolve() if baseline else None
    p.require(not output.exists(), "Choose a new native-adapter build directory")
    p.require(not output.is_relative_to(app) and not app.is_relative_to(output)
              and output != codex_home, "Native adapter output overlaps production state")
    publisher = verify_official(app)
    source = p.app_layout(app)
    source_artifact = source.get("source_artifact", source.get("asar"))
    source_hash = p.file_sha(source_artifact)
    p.require(baseline is not None or isinstance(initial, dict), "Initial model configuration is required")
    prior = json.loads((baseline / "manifest.json").read_text(encoding="utf-8")) if baseline else initial['manifest']
    edition = edition or prior.get("edition", p.EDITION)
    p.require(edition in ("ds", "proxy"), "Unknown native adapter edition")
    p.require(prior.get("edition", edition) == edition, "Native migration cannot change editions")
    before = p.read_config(codex_home / "config.toml")
    current = p.tomllib.loads(before)
    previous = prior.get("native_config") if prior.get("architecture") == ARCHITECTURE else None
    if previous:
        # Native preferences were moved into the catalog during the first migration.
        preferences = previous["original_root"]
    else:
        preferences = current
    if model_snapshot:
        import model_sync
        p.require(model_snapshot.get("edition") == edition
                  and model_snapshot.get("digest") == model_sync.snapshot_digest(model_snapshot),
                  "Native model snapshot identity mismatch")
        catalog, routes = model_snapshot["catalog"], model_snapshot["routes"]
    else:
        catalog = (initial['catalog'] if initial else
                   json.loads((baseline / "desktop-models.json").read_text(encoding="utf-8")))
        routes = (initial['manifest'] if initial else prior).get("model_routes", {})
    # Only an explicit preferences source enables legacy global-limit migration.
    # Public updaters supply their existing monitor preference file, including
    # an explicit empty mapping when the user has no reductions.
    if model_preferences is not None:
        if isinstance(model_preferences, (str, Path)):
            model_preferences = json.loads(Path(model_preferences).read_text(encoding="utf-8"))
        from model_sync import validate_preferences
        routes = migrate_legacy_routes(routes, current, validate_preferences(model_preferences))
    p.require(isinstance(routes, dict), "External model routes must be a mapping")
    if edition == "ds":
        p.require(all(route["provider"] == p.PROVIDER for route in routes.values()),
                  "The DS edition cannot import extension routes")
    effective = copy.deepcopy(prior)
    if initial and baseline:
        for key in ('locals', 'model_specifications', 'deepseek_enabled', 'proxy'):
            if key in initial['manifest']:
                effective[key] = copy.deepcopy(initial['manifest'][key])
    if model_snapshot:
        effective["model_specifications"] = model_snapshot.get("specs",effective.get("model_specifications",{}))
        effective["locals"] = model_snapshot.get("locals", effective.get("locals", []))
        effective["deepseek_enabled"] = model_snapshot.get("deepseek_enabled", effective.get("deepseek_enabled", True))
        if 'proxy' in model_snapshot:effective['proxy'] = model_snapshot['proxy']
    effective['keychain_account'] = effective.get('keychain_account') or __import__('getpass').getuser()
    effective['keychain_service'] = effective.get('keychain_service') or p.KEYCHAIN_SERVICE
    mapped = adapter_routes(effective, routes)
    catalog = adapt_catalog(catalog, routes, preferences, apply_root_preferences=previous is None and not model_snapshot)
    for model in catalog['models']:
        if model['slug'] in mapped:
            levels = [v['effort'] for v in model.get('supported_reasoning_levels', [])]
            if levels:
                mapped[model['slug']]['summary_reasoning_effort'] = next(
                    (level for level in ('none','minimal','low','medium','high','max') if level in levels), levels[0])
    old_profile_path = baseline / "adapter-profile.json" if baseline else None
    old_profile = json.loads(old_profile_path.read_text(encoding="utf-8")) if previous and old_profile_path and old_profile_path.is_file() else None
    token = old_profile["token"] if old_profile else secrets.token_urlsafe(32)
    port = old_profile["port"] if old_profile else free_port()
    # Identity excludes secrets, paths and app code: each staged catalog/route
    # revision has its own health identity while the listener address is stable.
    code_identity = {name:p.file_sha(p.ROOT / name) for name in ADAPTER_FILES}
    identity = hashlib.sha256(json.dumps([edition, catalog, routes, mapped, code_identity],
                                         sort_keys=True).encode()).hexdigest()
    profile = {"format":1, "edition":edition, "identity":identity, "token":token, "port":port,
               "compression_node":compression_node(app, compression_runtime),
               "gpt":{"chatgpt_base_url":"https://chatgpt.com/backend-api/codex",
                      "api_base_url":"https://api.openai.com/v1"}, "routes":mapped,
               "official_models":[row['slug'] for row in catalog['models'] if row['slug'] not in routes],
               "legacy_history_dir":str(codex_home / "model-adapter-history"),
               "history_home":str(codex_home),
               "legacy_origins_file":str(codex_home / "model-adapter-legacy-origins.json"),
               "summary_model":p.catalog_template(catalog)["slug"]}
    if old_profile:
        profile["gpt"] = old_profile["gpt"]
        # This name identifies pre-upgrade cache entries only; it never selects
        # a network model now. Preserve it when the official catalog changes.
        profile['summary_model'] = old_profile.get('summary_model', profile['summary_model'])
        if "network_proxy" in old_profile:
            profile["network_proxy"] = old_profile["network_proxy"]
    prepared, ownership = prepare_config(before, output / "desktop-models.json", endpoint(port, token),
                                          mode="builtin", previous=previous)
    from official_client_host import prepare as prepare_composer
    composer = prepare_composer(app, sorted(mapped)) if mapped and sys.platform != 'linux' else None
    output.mkdir(parents=True, mode=0o700)
    for name in ADAPTER_FILES:
        shutil.copy2(p.ROOT / name, output / name)
    runtime = str(windowless_python(sys.executable))
    host = {"edition":edition, "adapter_command":[runtime, str(output / "inference_adapter.py"),
                                                  "serve", str(output / "adapter-profile.json")],
            "health_url":"http://127.0.0.1:" + str(port) + "/" + token + "/health",
            "health_identity":identity, "working_directory":str(output)}
    p.save_json(output / "adapter-profile.json", profile)
    p.save_json(output / "host-settings.json", host)
    p.save_json(output / "desktop-models.json", catalog)
    p.atomic_text(output / "config-before.toml", before)
    p.atomic_text(output / "config-prepared.toml", prepared)
    if model_snapshot:
        p.save_json(output / "synced-models.json", model_snapshot)
    target = install_target or (prior.get("target", app) if sys.platform == 'darwin' else app)
    manifest = {"format":1, "architecture":ARCHITECTURE, "status":"prepared", "edition":edition,
                "tool_version":p.VERSION, "target":str(target), "source_target":str(app),
                "codex_home":str(codex_home), "legacy_baseline":str(baseline) if baseline else None, "native_config":ownership,
                "app_version":source["version"], "app_build":source["build"],
                "original_asar":source_hash, "publisher":publisher,
                "catalog_sha256":p.file_sha(output / "desktop-models.json"),
                "runtime_sha256":{name:p.file_sha(output / name) for name in ADAPTER_FILES},
                "profile_sha256":p.file_sha(output / "adapter-profile.json"),
                "composer_compat":composer,
                "host_settings_sha256":p.file_sha(output / "host-settings.json"), "model_routes":routes,
                "keychain_account":effective.get("keychain_account"), "keychain_service":effective.get("keychain_service"),
                "deepseek_enabled":effective.get("deepseek_enabled", edition == "ds"),
                "locals":effective.get("locals", []), "probe_required":True,
                "model_specifications":(model_snapshot or {}).get("specs",effective.get("model_specifications",{}))}
    if sys.platform == 'linux':
        manifest.update(client_kind='cli', source_artifact=str(source_artifact))
    if (p.ROOT.parent / '.git').exists() and shutil.which('git'):
        revision = subprocess.run(['git', '-C', str(p.ROOT.parent), 'rev-parse', 'HEAD'],
                                  capture_output=True, text=True, check=True)
        dirty = subprocess.run(['git', '-C', str(p.ROOT.parent), 'status', '--porcelain'],
                               capture_output=True, text=True, check=True)
        manifest.update(source_revision=revision.stdout.strip(), source_dirty=bool(dirty.stdout))
    if effective.get("proxy"):
        manifest["proxy"] = effective["proxy"]
    if update_metadata:
        manifest["update"] = update_metadata
    if initial is not None:
        manifest['initial_manifest'] = prior
    p.require(p.file_sha(source_artifact) == source_hash, "Official app changed during native staging")
    p.save_json(output / "manifest.json", manifest)
    return manifest


def initial_models(app, args):
    """Use the vendor catalog and explicitly configured services for a fresh setup."""
    from model_sync import apply_reductions
    deepseek = getattr(args, 'deepseek_enabled', None)
    if deepseek is None:
        deepseek = p.EDITION == 'ds'
    if p.EDITION == 'ds':
        p.require(deepseek, 'The DS edition requires its DeepSeek route')
        p.require(not getattr(args, 'proxy_config', None) and not getattr(args, 'connections', None),
                  'Use the extension edition for other model services')
    with tempfile.TemporaryDirectory(prefix='native-model-catalog-') as temporary:
        env = {**os.environ, 'CODEX_HOME':temporary}
        raw = subprocess.check_output([str(p.cli_path(app)), 'debug', 'models', '--bundled'],
                                      env=env, stderr=subprocess.PIPE, timeout=90, **p.background_options())
    catalog = p.make_catalog(json.loads(raw), deepseek=deepseek)
    manifest = {'architecture':'official-original', 'edition':p.EDITION, 'target':str(app),
                'deepseek_enabled':deepseek, 'locals':[],
                'keychain_account':args.keychain_account, 'keychain_service':args.keychain_service}
    if getattr(args, 'proxy_config', None):
        import proxy_support
        proxy = proxy_support.validate({'base_url':args.proxy_base_url, 'config':str(args.proxy_config.resolve()),
                                        'models':args.proxy_models or list(proxy_support.MODELS)})
        manifest['proxy'] = proxy
        catalog = proxy_support.add_catalog(catalog, proxy['models'])
    routes = {}
    for entry in catalog['models']:
        model = entry['slug']
        provider = (p.PROVIDER if deepseek and model == p.MODEL else 'easycli-desktop'
                    if model in manifest.get('proxy', {}).get('models', []) else None)
        if provider:
            compact = apply_reductions(entry, {})
            routes[model] = {'provider':provider, 'context_window':entry['context_window'],
                             'max_context_window':entry['max_context_window'], 'compact_limit':compact}
    if getattr(args, 'connections', None):
        import local_support as local
        manifest['locals'] = local.load(args.connections)
        template = p.catalog_template(catalog)
        for connection in manifest['locals']:
            specs, warnings = local.discover(connection)
            p.require(specs, 'No usable model specifications: ' + connection['id'])
            manifest.setdefault('model_specifications',{}).update(specs)
            connection['chat_only_models'] = [model for model,spec in specs.items() if not spec.get('supports_tools', True)]
            connection['normalize_instructions_models'] = [model for model,spec in specs.items() if spec.get('normalize_instructions')]
            connection['tool_adapter_models'] = [model for model,spec in specs.items() if local.needs_tool_adapter(spec)]
            for model, spec in specs.items():
                p.require(not any(row['slug'] == model for row in catalog['models']), 'Model ID belongs to more than one service: ' + model)
                entry = local.make_entry(template, model, connection, spec, 100+len(routes))
                compact = apply_reductions(entry, {}, spec.get('configured_context_window') if connection.get('respect_server_context') else None)
                catalog['models'].append(entry)
                routes[model] = {'provider':connection['id'], 'context_window':entry['context_window'],
                                 'max_context_window':entry['max_context_window'], 'compact_limit':compact,
                                 'supports_tools':spec.get('supports_tools', True)}
    manifest['model_routes'] = routes
    p.require(routes, 'Configure at least one external model service for initial setup')
    return {'manifest':manifest, 'catalog':catalog}


def build(args):
    app = args.app
    if app is None and sys.platform == 'win32':
        from windows_support import discover_app
        app = discover_app()
    elif app is None and sys.platform == 'linux':
        from linux_support import discover_app
        app = discover_app()
    app = Path(app).resolve()
    baseline = getattr(args, 'baseline', None)
    initial = initial_models(app, args) if not baseline or getattr(args, 'connections', None) else None
    snapshot = getattr(args, 'model_snapshot', None)
    if isinstance(snapshot, Path):
        snapshot = json.loads(snapshot.read_text(encoding='utf-8'))
    result = stage(app=app, codex_home=args.codex_home, output=args.output, baseline=baseline,
                   edition=p.EDITION, model_snapshot=snapshot, initial=initial,
                   install_target=getattr(args, 'install_target', None),
                   compression_runtime=getattr(args, 'compression_node', None),
                   model_preferences=getattr(args, 'model_preferences', None))
    print('Prepared native model adapter:', args.output, flush=True)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["stage"])
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--codex-home", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--model-preferences", type=Path)
    args = parser.parse_args()
    result = stage(app=args.app, codex_home=args.codex_home, output=args.output, baseline=args.baseline,
                   model_preferences=args.model_preferences)
    print(json.dumps({key:result[key] for key in ("architecture", "status", "edition", "app_version")}, ensure_ascii=False))
