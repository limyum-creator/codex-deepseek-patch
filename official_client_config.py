"""Configuration-only model integration for an unmodified official client.

All transforms are pure and compare parsed TOML before returning. Installation
owns backups/atomic writes; these helpers never touch authentication or chats.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import tomllib


PROVIDER = "codex-model-adapter"
def _literal(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return json.dumps(value, ensure_ascii=False)
    raise ValueError("Unsupported managed configuration value")


def _table_name(line):
    match = re.match(r"\s*(\[[^\n]+?\])\s*(?:#.*)?$", line)
    if not match:
        return None
    # Let the TOML parser interpret quoted table components.
    try:
        value = tomllib.loads(match[1])
        parts = []
        while isinstance(value, dict) and len(value) == 1:
            name, value = next(iter(value.items()))
            parts.append(name)
        return tuple(parts)
    except tomllib.TOMLDecodeError:
        return ("<unmanaged-table>",)


def _assignment_key(line):
    match = re.match(r'''\s*((?:[A-Za-z0-9_-]+)|(?:"[^"\\]+")|(?:'[^']+'))\s*=''', line)
    if not match:
        return None
    try:
        return next(iter(tomllib.loads(match[1] + "=0")))
    except tomllib.TOMLDecodeError:
        return None


def _replace_section(text, table, updates):
    """Change scalar keys in one exact table; preserve every other byte."""
    current = tomllib.loads(text)
    value = current
    for key in table:
        value = value.get(key, {})
        if not isinstance(value, dict):
            raise ValueError("Managed table is not a table")
    if all(value.get(key) == replacement for key, replacement in updates.items()):
        return text
    expected = copy.deepcopy(current)
    cursor = expected
    for key in table:
        cursor = cursor.setdefault(key, {})
    for key, replacement in updates.items():
        if replacement is None:
            cursor.pop(key, None)
        else:
            cursor[key] = replacement
    output, seen = [], set()
    active = ()
    for line in text.splitlines(keepends=True):
        header = _table_name(line.rstrip("\r\n"))
        if header is not None:
            active = header
        key = _assignment_key(line) if active == table else None
        if key in updates:
            # Refuse ambiguous multiline values, dotted aliases and duplicates.
            if key in seen:
                raise ValueError("Duplicate managed configuration assignment")
            try:
                parsed_line = tomllib.loads(line)
            except tomllib.TOMLDecodeError:
                raise ValueError("Managed configuration needs a single-line scalar") from None
            if parsed_line.get(key) != value.get(key):
                raise ValueError("Managed configuration assignment is ambiguous")
            seen.add(key)
            continue
        output.append(line)
    additions = "".join(key + " = " + _literal(val) + "\n"
                        for key, val in updates.items() if val is not None)
    result = "".join(output)
    if not table:
        result = additions + result
    elif additions:
        # Insert inside an existing table; otherwise append a new table.
        rows = result.splitlines(keepends=True)
        index = next((i + 1 for i, line in enumerate(rows)
                      if _table_name(line.rstrip("\r\n")) == table), None)
        if index is None:
            result = result.rstrip("\r\n") + "\n\n[" + ".".join(table) + "]\n" + additions
        else:
            rows.insert(index, additions)
            result = "".join(rows)
    if tomllib.loads(result) != expected:
        raise ValueError("Unrelated configuration changed while preparing native integration")
    return result


def adapt_catalog(catalog, routes, current_config, *, apply_root_preferences=True):
    """Move native window preferences into each model, retaining custom maxima."""
    result = copy.deepcopy(catalog)
    # Root preferences move into each native model at first integration. Later
    # catalogs already carry resolved per-model preferences and manual edits.
    window = current_config.get("model_context_window") if apply_root_preferences else None
    compact = current_config.get("model_auto_compact_token_limit") if apply_root_preferences else None
    for value in (window, compact):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
            raise ValueError("Context preferences must be positive integers")
    # Only native metadata can define native tool variants. External catalog
    # builders previously disabled these even for models with tool support.
    templates = [m for m in catalog["models"] if m["slug"] not in routes
                 and m["slug"].startswith("gpt-") and m.get("visibility", "list") == "list"]
    template = next((m for m in templates if m["slug"] == "gpt-6.1-sol"),
                    templates[0] if templates else None)
    seen = set()
    for model in result["models"]:
        slug = model["slug"]
        if slug in seen:
            raise ValueError("Model IDs must be unique across providers")
        seen.add(slug)
        if slug in routes:
            route = routes[slug]
            if route.get("supports_tools", True) and not route.get("chat_only", False) and template:
                for key in ("node_repl_disabled", "apply_patch_tool_type", "multi_agent_version"):
                    if key in template:
                        model[key] = copy.deepcopy(template[key])
                    else:
                        model.pop(key, None)
            model.update(context_window=route["context_window"],
                         max_context_window=route["max_context_window"],
                         auto_compact_token_limit=route["compact_limit"],
                         effective_context_window_percent=100,
                         use_responses_lite=False, supports_experimental_context=False,
                         supports_reasoning_effort_updates=False, comp_hash=None)
        else:
            if window is not None and model.get("context_window"):
                model["context_window"] = min(window, model.get("max_context_window") or model["context_window"])
            if compact is not None and model.get("context_window"):
                model["auto_compact_token_limit"] = min(compact, model["context_window"])
    if not set(routes).issubset(seen):
        raise ValueError("Routing metadata refers to a model absent from the native catalog")
    return result


def migrate_legacy_routes(routes, current_config, preferences):
    """Separate the old GPT root window from explicitly reduced external models.

    The legacy DS builder inherited the global GPT limit even when no per-model
    reduction existed. A model-specific preference remains authoritative.
    """
    result = copy.deepcopy(routes)
    explicit = preferences.get("context_windows", {})
    compact_preferences = preferences.get("compact_limits", {})
    for model, route in result.items():
        maximum = route["max_context_window"]
        compact = compact_preferences.get(model)
        if model in explicit:
            route["context_window"] = min(explicit[model], maximum) if explicit[model] is not None else maximum
            cap = route["context_window"] * 9 // 10
            route["compact_limit"] = min(compact, route["context_window"]) if compact is not None else cap
        elif (route["provider"] == "deepseek-desktop"
              and current_config.get("model_provider", "openai") == "openai"
              and current_config.get("model_context_window") == route["context_window"]
              and route["context_window"] < maximum):
            route["context_window"] = maximum
            cap = maximum * 9 // 10
            route["compact_limit"] = min(compact, maximum) if compact is not None else cap
    return result


def prepare_config(current, catalog_path, base_url, *, mode="builtin", previous=None):
    """Prepare scoped native config and an ownership record, with no auth edits.

    ``builtin`` keeps OpenAI's provider. ``native-summary`` uses the documented
    custom provider capability and preserves native OpenAI authentication.
    Legacy native-summary installations migrate through their exact ownership
    record, preserving the original uninstall baseline and unrelated edits.
    """
    from urllib.parse import urlsplit
    url = urlsplit(base_url)
    if (url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port
            or url.username or url.password or url.query or url.fragment
            or not url.path.endswith("/backend-api/codex")):
        raise ValueError("Native inference endpoint must be a private loopback path")
    if not Path(catalog_path).is_absolute():
        raise ValueError("Native model catalog path must be absolute")
    if mode not in ("builtin", "native-summary"):
        raise ValueError("Unknown native integration mode")
    if mode == 'builtin' and previous and previous.get('provider') is not None:
        # Keep the owned legacy definition dormant so native metadata rollback
        # can still resume old provider IDs after an interrupted migration.
        result, ownership = prepare_config(current, catalog_path, base_url, mode='native-summary', previous=previous)
        result = _replace_section(result, (), {'model_provider':'openai'})
        ownership['root']['model_provider'] = 'openai'
        return result, ownership
    before = tomllib.loads(current)
    if previous:
        for key, value in previous["root"].items():
            if before.get(key) != value:
                raise ValueError("A managed setting was changed: " + key)
    elif before.get("openai_base_url"):
        raise ValueError("An existing OpenAI endpoint needs an explicit migration source")
    prior_provider = before.get("model_providers", {}).get(PROVIDER)
    if prior_provider is not None and (not previous or prior_provider != previous.get("provider")):
        raise ValueError("The adapter provider name is already in use")
    root = {"model_catalog_json": str(catalog_path), "openai_base_url": base_url,
            "model_context_window": None, "model_auto_compact_token_limit": None}
    if mode == "native-summary":
        root["model_provider"] = PROVIDER
    else:
        if before.get("model_provider", "openai") != "openai":
            raise ValueError("Keep the user's chosen provider until migration is verified")
        root['model_provider'] = 'openai'
    result = _replace_section(current, (), root)
    # Older adapters disabled native compression. Undo that owned override once;
    # subsequent user choices belong to Codex and are not managed by the patch.
    if (previous and previous.get('compression_policy') != 'native'
            and before.get('features', {}).get('enable_request_compression') is False):
        result = _replace_section(result, ('features',),
                                  {'enable_request_compression':previous['original_compression']})
    provider = None
    if mode == "native-summary":
        provider = {"name": "OpenAI", "base_url": base_url, "wire_api": "responses",
                    "requires_openai_auth": True, "supports_websockets": True}
        result = _replace_section(result, ("model_providers", PROVIDER), provider)
        result = _replace_section(result, ("model_providers", PROVIDER, "capabilities"),
                                  {"remote_compaction": "unsupported"})
        provider = {**provider, "capabilities": {"remote_compaction": "unsupported"}}
    after = tomllib.loads(result)
    ownership = {"format": 1, "root": {key: after.get(key) for key in root}, "provider": provider,
                 "compression_policy":"native",
                 "compression_after":after.get('features', {}).get('enable_request_compression'),
                 "original_root": previous["original_root"] if previous else {key: before.get(key) for key in root},
                 "original_compression": previous["original_compression"] if previous else
                     before.get("features", {}).get("enable_request_compression")}
    return result, ownership


def restore_config(current, ownership):
    """Undo only owned settings; preserve later unrelated user changes."""
    before = tomllib.loads(current)
    if ownership.get("format") != 1:
        raise ValueError("Unknown native configuration ownership format")
    for key, value in ownership["root"].items():
        if before.get(key) != value:
            raise ValueError("A managed setting changed; review before restoring: " + key)
    legacy_compression = ownership.get('compression_policy') != 'native'
    if legacy_compression and before.get("features", {}).get("enable_request_compression") is not False:
        raise ValueError("Managed compression setting changed; review before restoring")
    result = _replace_section(current, (), ownership["original_root"])
    if legacy_compression:
        result = _replace_section(result, ("features",),
                                  {"enable_request_compression": ownership["original_compression"]})
    provider = ownership.get("provider")
    if provider is not None:
        parsed = tomllib.loads(result)
        if parsed.get("model_providers", {}).get(PROVIDER) != provider:
            raise ValueError("Managed provider changed; review before restoring")
        expected = copy.deepcopy(parsed)
        expected["model_providers"].pop(PROVIDER)
        lines, skipping = [], False
        for line in result.splitlines(keepends=True):
            table = _table_name(line.rstrip("\r\n"))
            if table is not None:
                skipping = table[:2] == ("model_providers", PROVIDER)
            if not skipping:
                lines.append(line)
        result = "".join(lines)
        actual = tomllib.loads(result)
        # Removing the last provider naturally removes its empty implicit parent.
        if not expected["model_providers"]:
            expected.pop("model_providers")
        if actual != expected:
            raise ValueError("Unrelated provider configuration changed during restore")
    return result


def rollback_config(current, activated_ownership, previous_text):
    """Reverse one activation to its immediate baseline, retaining other edits.

    ``original_root`` is the uninstall baseline and can be several revisions
    older. A failed update must instead restore the configuration read just
    before that update. Repeated recovery is a no-op; independent edits to an
    owned setting require review rather than being overwritten.
    """
    before, previous = tomllib.loads(current), tomllib.loads(previous_text)
    if activated_ownership.get("format") != 1:
        raise ValueError("Unknown native configuration ownership format")
    keys = activated_ownership["root"]

    def owned_values(value):
        return ({key: value.get(key) for key in keys},
                value.get("features", {}).get("enable_request_compression"),
                value.get("model_providers", {}).get(PROVIDER))

    if owned_values(before) == owned_values(previous):
        return current
    scoped = copy.deepcopy(activated_ownership)
    scoped["original_root"] = {key: previous.get(key) for key in keys}
    scoped["original_compression"] = previous.get("features", {}).get("enable_request_compression")
    result = restore_config(current, scoped)
    restore_compression = (activated_ownership.get('compression_policy') != 'native'
        or before.get('features', {}).get('enable_request_compression') == activated_ownership.get('compression_after'))
    if restore_compression and activated_ownership.get('compression_policy') == 'native':
        result = _replace_section(result, ('features',),
            {'enable_request_compression':previous.get('features', {}).get('enable_request_compression')})
    prior_provider = previous.get("model_providers", {}).get(PROVIDER)
    if prior_provider is not None:
        # Restore the previous managed table verbatim, including its subtables.
        rows, copying = [], False
        for line in previous_text.splitlines(keepends=True):
            table = _table_name(line.rstrip("\r\n"))
            if table is not None:
                copying = table[:2] == ("model_providers", PROVIDER)
            if copying:
                rows.append(line)
        block = "".join(rows)
        if tomllib.loads(block) != {"model_providers": {PROVIDER: prior_provider}}:
            raise ValueError("Previous managed provider cannot be restored unambiguously")
        result = result.rstrip("\r\n") + "\n\n" + block
    expected = copy.deepcopy(before)
    for key in keys:
        if key in previous:
            expected[key] = previous[key]
        else:
            expected.pop(key, None)
    compression = (previous if restore_compression else before).get("features", {}).get("enable_request_compression")
    if compression is None:
        expected.setdefault("features", {}).pop("enable_request_compression", None)
    else:
        expected.setdefault("features", {})["enable_request_compression"] = compression
    providers = expected.setdefault("model_providers", {})
    if prior_provider is None:
        providers.pop(PROVIDER, None)
        if not providers:
            expected.pop("model_providers")
    else:
        providers[PROVIDER] = prior_provider
    if not expected.get("features") and "features" not in previous:
        expected.pop("features", None)
        result = "".join(line for line in result.splitlines(keepends=True)
                         if _table_name(line.rstrip("\r\n")) != ("features",))
    if tomllib.loads(result) != expected:
        raise ValueError("Unrelated configuration changed during activation rollback")
    return result
