"""Optional existing OpenAI Responses proxy; never logs or copies OAuth tokens."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

PROVIDER = "easycli-desktop"
MODELS = {
    "grok-4.5": "Grok 4.5",
    "grok-4.7": "Grok 4.7",
    "gemini-3.8-flash-high": "Gemini 3.8 Flash",
    "claude-opus-4-6-thinking": "Claude Opus 4.6 Thinking",
}

# Provider specifications checked 2026-10-18; see README for exact sources.
# Keep route.js defaults in sync (the regression test checks both together).
MODEL_CONTEXT_WINDOWS = {
    "grok-4.5": 500000,
    "grok-4.7": 500000,
    "gemini-3.8-flash-high": 1048576,
    "claude-opus-4-6-thinking": 1000000,
}


def read_api_key(path):
    """Read only the client api-keys list in CPA v1/v2 generated YAML.

    Deliberately reject unsupported YAML syntax instead of interpreting tags,
    aliases, or management/OAuth credentials. No credential enters an exception.
    """
    text = Path(path).read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    stack, candidates = [], []
    for i, line in enumerate(lines):
        match = re.match(r'''^( *)(["']?)([\w-]+)\2:\s*(.*)$''', line)
        if not match:
            continue
        spaces, _, name, value = match.groups()
        level = len(spaces)
        while stack and stack[-1][0] >= level:
            stack.pop()
        parents = [item[1] for item in stack]
        if name == "api-keys" and parents in ([], ["access"]):
            if value.strip().startswith("["):
                try:
                    values = json.loads(value)
                    if isinstance(values, list):
                        candidates.extend(values)
                except ValueError:
                    raise ValueError("Unsupported client API key list syntax") from None
            elif value.strip() and not value.lstrip().startswith("#"):
                raise ValueError("Unsupported client API key list syntax")
            else:
                for following in lines[i + 1:]:
                    if not following.strip() or following.lstrip().startswith("#"):
                        continue
                    depth = len(following) - len(following.lstrip(" "))
                    if depth < level or (depth == level and not following.lstrip().startswith("-")):
                        break
                    item = re.match(r"^\s*-\s+(.*?)\s*$", following)
                    if not item:
                        raise ValueError("Unsupported client API key item")
                    raw = item.group(1)
                    try:
                        if raw.startswith('"'):
                            value, end = json.JSONDecoder().raw_decode(raw)
                            if raw[end:].strip() and not raw[end:].lstrip().startswith("#"):
                                raise ValueError()
                        elif raw.startswith("'"):
                            match = re.fullmatch(r"'((?:[^']|'')*)'\s*(?:#.*)?", raw)
                            if not match:
                                raise ValueError()
                            value = match.group(1).replace("''", "'")
                        else:
                            value = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
                            if not re.fullmatch(r"[A-Za-z0-9_.:/+=-]+", value):
                                raise ValueError()
                        candidates.append(value)
                    except (ValueError, TypeError):
                        raise ValueError("Unsupported client API key scalar") from None
            break
        stack.append((level, name))
    if not candidates or not isinstance(candidates[0], str) or not candidates[0] or any(c in candidates[0] for c in "\r\n\0"):
        raise ValueError("No usable client API key in the existing proxy configuration")
    return candidates[0]


def validate(settings):
    if not isinstance(settings, dict) or set(settings) != {"base_url", "config", "models"}:
        raise ValueError("Invalid proxy settings")
    url = urlsplit(settings["base_url"])
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("Proxy base URL must have no credentials, query, or fragment")
    if url.scheme == "http" and url.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("Remote proxy connections require HTTPS")
    models = settings["models"]
    import model_sync
    if not isinstance(models, list) or len(set(models)) != len(models) or any(m not in MODELS and model_sync.family(m) not in ('grok','gemini-flash','opus') for m in models):
        raise ValueError("Choose unique supported proxy model IDs")
    if not Path(settings["config"]).is_absolute():
        raise ValueError("Proxy configuration path must be absolute")
    return copy.deepcopy(settings)


def provider_config(settings, helper):
    validate(settings)
    from local_support import bridge_port
    endpoint='http://127.0.0.1:'+str(bridge_port(helper,PROVIDER))+'/v1'
    command = Path(sys.executable)
    if command.name.lower() == "pythonw.exe":
        command = command.with_name("python.exe")
        if not command.is_file():
            raise ValueError("Console Python is required by the credential helper")
    return (f'\n[model_providers.{PROVIDER}]\nname = "EasyCLIProxyAPI"\n'
            + 'base_url = ' + json.dumps(endpoint) + '\n'
            + 'wire_api = "responses"\nrequires_openai_auth = false\n'
            + 'supports_websockets = false\nrequest_max_retries = 0\nstream_max_retries = 0\n'
            + f'\n[model_providers.{PROVIDER}.auth]\ncommand = ' + json.dumps(str(command)) + '\n'
            + 'args = ' + json.dumps([str(helper), "credential-get", "--config", settings["config"]]) + '\n'
            # Native zero means cache forever. Recheck the idle-exiting listener
            # on each inference request so an existing chat can wake it again.
            + 'refresh_interval_ms = 1\n')


def add_catalog(catalog, models):
    """Provider context maxima; leave all bundled GPT and DS entries intact."""
    result = copy.deepcopy(catalog)
    import patch
    template = patch.catalog_template(result)
    result["models"] = [m for m in result["models"] if m["slug"] not in MODELS]
    for priority, model in enumerate(models, start=91):
        if model not in MODELS:
            raise ValueError("Unsupported proxy model")
        entry = copy.deepcopy(template)
        entry.update(slug=model, display_name=MODELS[model], description=MODELS[model]+" · EasyCLIProxyAPI",
                     default_reasoning_level="high", supported_reasoning_levels=[
                         {"effort":"high", "description":"Provider reasoning preset"}],
                     shell_type="shell_command", visibility="list", supported_in_api=True, priority=priority,
                     additional_speed_tiers=[], service_tiers=[], default_service_tier=None,
                     available_access_programs=None, availability_nux=None, upgrade=None,
                     context_window=MODEL_CONTEXT_WINDOWS[model], max_context_window=MODEL_CONTEXT_WINDOWS[model],
                     effective_context_window_percent=100,
                     prefer_websockets=False, use_responses_lite=False, tool_mode=None,
                     experimental_supported_tools=[], multi_agent_reasoning_effort="high",
                     web_search_tool_type="text", supports_search_tool=True,
                     input_modalities=["text"], supports_image_detail_original=False,
                     supports_reasoning_effort_updates=False, support_verbosity=False,
                     default_verbosity=None)
        result["models"].append(entry)
        from model_sync import configure_reasoning
        configure_reasoning(entry)
    return result


def stage_helpers(directory, settings):
    import secrets, shutil
    import patch
    from local_support import bridge_port
    directory=Path(directory)
    for name in ('proxy_support.py','local_bridge.py','responses_compat.py'):
        shutil.copy2(Path(__file__).with_name(name),directory/name)
    patch.save_json(directory/(PROVIDER+'-bridge.json'),{
        'base_url':settings['base_url'],'proxy_config':settings['config'],
        'native_tools':True,'port':bridge_port(directory/'proxy_support.py',PROVIDER),
        'token':secrets.token_urlsafe(32)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["credential-get"])
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    profile=Path(__file__).with_name(PROVIDER+'-bridge.json')
    if profile.is_file():
        from local_bridge import credential
        credential(profile)
        return 0
    try:
        key = read_api_key(args.config)
    except (ValueError, OSError):
        print("Cannot read the existing proxy client credential", file=sys.stderr)
        return 1
    print(key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
