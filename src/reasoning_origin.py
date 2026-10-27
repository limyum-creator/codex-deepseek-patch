"""Explicit origin containers for provider-private reasoning signatures.

This is a versioned wire container, not encryption. Native history keeps each
signature; the adapter restores it only for the model that produced it.
"""
import base64
import copy
import hashlib
import json
import os
import re
from pathlib import Path
import stat
import tempfile
import threading

PREFIX = 'codex-model-adapter-origin:v1:'
FAMILY = 'codex-model-adapter-origin:'
MESSAGE_PREFIX = 'msg_cma1_'
LEGACY_MESSAGE_ID = re.compile(r'msg_[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}\Z')
MAX_INDEX_BYTES = 32 * 1024 * 1024
MAX_ROLLOUT_LINE = 64 * 1024 * 1024


class ReasoningOriginError(ValueError):
    pass


class LegacyOriginIndexError(ReasoningOriginError):
    pass


def _decode(cipher):
    if not cipher.startswith(FAMILY):
        return None
    if not cipher.startswith(PREFIX):
        raise ReasoningOriginError('Unsupported reasoning origin container version')
    try:
        payload = cipher[len(PREFIX):]
        value = json.loads(base64.b64decode(payload + '=' * (-len(payload) % 4), altchars=b'-_', validate=True))
        if (not isinstance(value, dict) or value.get('version') != 1
                or set(value) not in ({'version', 'model', 'cipher'}, {'version', 'model', 'cipher', 'had_encrypted'})
                or not isinstance(value['model'], str) or not 0 < len(value['model']) <= 1024
                or value['cipher'] is not None and not isinstance(value['cipher'], str)
                or 'had_encrypted' in value and type(value['had_encrypted']) is not bool):
            raise ValueError()
        value.setdefault('had_encrypted', True)
        return value
    except (ValueError, TypeError, KeyError):
        raise ReasoningOriginError('Invalid reasoning origin container') from None


def _texts(value):
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [text for part in value for text in _texts(part)]
    if isinstance(value, dict):
        text = next((value[key] for key in ('text', 'content', 'thinking', 'reasoning_content') if key in value), None)
        if isinstance(text, dict):
            text = text.get('value')
        return _texts(text)
    return []


def public_reasoning_message(item):
    """Normalize already public text into the shared Responses message schema."""
    texts = _texts(item.get('summary')) + _texts(item.get('content'))
    if not texts:
        return None
    return {'role': 'assistant', 'content': [{'type': 'output_text', 'text': text} for text in texts]}


def reasoning_key(item):
    return hashlib.sha256(json.dumps(item, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def message_origin(identity):
    if not isinstance(identity, str) or not identity.startswith(MESSAGE_PREFIX):
        return None
    try:
        value = json.loads(base64.b64decode(identity[len(MESSAGE_PREFIX):] + '=' * (-len(identity[len(MESSAGE_PREFIX):]) % 4), altchars=b'-_', validate=True))
        if (not isinstance(value, list) or len(value) != 2 or
                not all(isinstance(v, str) and 0 < len(v) <= 4096 for v in value)):
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise ReasoningOriginError('Invalid external message identity') from None


def stamp_message_id(identity, model):
    if not isinstance(identity, str) or not identity or message_origin(identity):
        return identity
    raw = json.dumps([model, identity], ensure_ascii=True, separators=(',', ':')).encode()
    return MESSAGE_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip('=')


def stamp_item(item, model):
    item = copy.deepcopy(item)
    if isinstance(item, dict) and item.get('type') == 'message' and item.get('id'):
        item['id'] = stamp_message_id(item['id'], model)
        return item
    if not isinstance(item, dict) or item.get('type') != 'reasoning':
        return item
    cipher = item.get('encrypted_content')
    if isinstance(cipher, str) and cipher:
        if _decode(cipher) is not None:
            return item
    if cipher is not None and not isinstance(cipher, str):
        raise ReasoningOriginError('Unsupported reasoning signature format')
    raw = json.dumps({'version': 1, 'model': model, 'cipher': cipher,
                      'had_encrypted': 'encrypted_content' in item}, ensure_ascii=False, separators=(',', ':')).encode()
    item['encrypted_content'] = PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip('=')
    return item


def stamp_response(response, model):
    response = copy.deepcopy(response)
    if isinstance(response.get('output'), list):
        response['output'] = [stamp_item(v, model) for v in response['output']]
    return response


def stamp_event(event, model):
    event = copy.deepcopy(event)
    if event.get('type', '').startswith(('response.output_text.', 'response.content_part.', 'response.refusal.')) and 'item_id' in event:
        event['item_id'] = stamp_message_id(event['item_id'], model)
    if isinstance(event.get('item'), dict):
        event['item'] = stamp_item(event['item'], model)
    if isinstance(event.get('response'), dict):
        event['response'] = stamp_response(event['response'], model)
    return event


def resolve_request(body, target, *, legacy_origins=None, official=False):
    """Return (body, fingerprints of exact restored same-model items, changed).

    Unmarked official state and compaction items are untouched. Empty private
    reasoning is omitted only when its explicit origin differs from target.
    """
    result, protected, changed = copy.deepcopy(body), set(), False
    if not isinstance(result.get('input'), list):
        return result, protected, changed
    items = []
    foreign_reasoning = False
    for item in result['input']:
        if isinstance(item, dict) and item.get('type') == 'message':
            origin = message_origin(item.get('id'))
            if origin:
                changed = True
                if origin[0] == target:
                    item['id'] = origin[1]
                else:
                    item.pop('id')
            elif (official and item.get('role') == 'assistant' and item.get('content')
                  and isinstance(item.get('id'), str) and LEGACY_MESSAGE_ID.fullmatch(item['id'])):
                # Old Chat/Anthropic adapters minted UUIDv4 IDs with no origin.
                # Replay the full message inline; never strip a bare reference.
                item.pop('id'); changed = True
        if isinstance(item, dict) and item.get('type') == 'reasoning' and isinstance(item.get('encrypted_content'), str):
            origin = _decode(item['encrypted_content'])
            if origin is None and legacy_origins:
                source = legacy_origins.get(hashlib.sha256(item['encrypted_content'].encode()).hexdigest())
                if source:
                    origin = {'model': source, 'cipher': item['encrypted_content'], 'had_encrypted': True}
            if origin is not None:
                changed = True
                if origin['model'] == target:
                    if origin['had_encrypted']:
                        item['encrypted_content'] = origin['cipher']
                    else:
                        item.pop('encrypted_content')
                    protected.add(reasoning_key(item))
                else:
                    foreign_reasoning = True
                    item = public_reasoning_message(item)
                    if item is None:
                        continue
        items.append(item)
    if official and foreign_reasoning:
        # Mixed old histories may contain unmarked provider Responses IDs.
        # Full messages are replayed inline; tool call_id links stay intact.
        for item in items:
            if isinstance(item, dict) and item.get('type') == 'message' and item.get('content') and 'id' in item:
                item.pop('id'); changed = True
    result['input'] = items
    return result, protected, changed


def _source_path(path, home):
    def normalized(value):
        text = str(value)
        if os.name == 'nt':
            if text.startswith('\\\\?\\UNC\\'):
                text = '\\\\' + text[8:]
            elif len(text) >= 7 and text.startswith('\\\\?\\') and text[4].isalpha() and text[5:7] == ':\\':
                text = text[4:]
        return Path(text)
    source, home = normalized(path), normalized(home)
    if not source.is_absolute():
        raise ValueError()
    root = normalized(home.resolve(strict=True))
    try:
        relative = source.relative_to(home)
        base = home
    except ValueError:
        relative = source.relative_to(root)
        base = root
    if (len(relative.parts) < 2 or relative.parts[0] not in ('sessions', 'archived_sessions')
            or '..' in relative.parts):
        raise ValueError()
    cursor = base
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError()
    resolved = normalized(source.resolve(strict=True))
    resolved.relative_to(root / relative.parts[0])
    if not source.is_file():
        raise ValueError()
    return resolved


def index_legacy_origins(paths, external_models, output_file, allowed_home):
    """Read native rollout provenance once; persist only signature hashes/model IDs."""
    home, output = Path(allowed_home), Path(output_file)
    if not home.is_absolute() or home.is_symlink() or not output.is_absolute() or output.is_symlink():
        raise LegacyOriginIndexError('Private origin index paths must be absolute and not symlinks')
    external = set(external_models)
    if not all(isinstance(model, str) and 0 < len(model) <= 1024 for model in external):
        raise LegacyOriginIndexError('Invalid external model metadata')
    origins, seen, conflicts, visited = {}, {}, set(), set()
    report = {'files': 0, 'indexed': 0, 'conflicts': 0, 'skipped_files': 0, 'malformed_lines': 0}
    for path in paths:
        try:
            source = _source_path(path, home)
            if source in visited:
                continue
            visited.add(source)
            model = None
            with source.open('rb') as stream:
                while line := stream.readline(MAX_ROLLOUT_LINE + 1):
                    if len(line) > MAX_ROLLOUT_LINE:
                        while line and not line.endswith(b'\n'):
                            line = stream.readline(MAX_ROLLOUT_LINE + 1)
                        model = None
                        report['malformed_lines'] += 1
                        continue
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            raise ValueError()
                    except (ValueError, TypeError):
                        model = None
                        report['malformed_lines'] += 1
                        continue
                    payload = event.get('payload')
                    if event.get('type') == 'turn_context':
                        model = payload.get('model') if isinstance(payload, dict) else None
                        if not isinstance(model, str):
                            model = None
                    elif (event.get('type') == 'response_item' and model and isinstance(payload, dict)
                          and payload.get('type') == 'reasoning'):
                        cipher = payload.get('encrypted_content')
                        if not isinstance(cipher, str) or not cipher or cipher.startswith(FAMILY):
                            continue
                        key = hashlib.sha256(cipher.encode()).hexdigest()
                        if key in seen and seen[key] != model:
                            conflicts.add(key)
                            origins.pop(key, None)
                        seen.setdefault(key, model)
                        if model in external and key not in conflicts:
                            origins[key] = model
            report['files'] += 1
        except (OSError, ValueError, TypeError):
            report['skipped_files'] += 1
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if output.parent.is_symlink() or os.name != 'nt' and output.parent.stat().st_mode & 0o077:
        raise LegacyOriginIndexError('Origin index directory must be private')
    raw = json.dumps({'format': 1, 'origins': origins}, sort_keys=True, separators=(',', ':')).encode()
    if len(raw) > MAX_INDEX_BYTES:
        raise LegacyOriginIndexError('Legacy origin index exceeds the size limit')
    fd, temporary = tempfile.mkstemp(prefix='.legacy-origins-', dir=output.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.chmod(temporary, 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)
    report.update(indexed=len(origins), conflicts=len(conflicts))
    return report


class LegacyOriginIndex:
    """Lazily reload an installer-built private index after atomic replacement."""
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.is_absolute():
            raise LegacyOriginIndexError('Legacy origin index path must be absolute')
        self._signature, self._origins, self._lock = None, {}, threading.Lock()

    def get(self):
        with self._lock:
            try:
                info = self.path.lstat()
            except FileNotFoundError:
                self._signature, self._origins = None, {}
                return {}
            except OSError:
                raise LegacyOriginIndexError('Legacy origin index is unavailable') from None
            if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_INDEX_BYTES
                    or os.name != 'nt' and info.st_mode & 0o077):
                raise LegacyOriginIndexError('Legacy origin index is not a private regular file')
            signature = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
            if signature == self._signature:
                return self._origins
            try:
                value = json.loads(self.path.read_bytes())
                origins = value['origins']
                if (set(value) != {'format', 'origins'} or value['format'] != 1 or not isinstance(origins, dict)
                        or any(not isinstance(key, str) or len(key) != 64 or any(c not in '0123456789abcdef' for c in key)
                               or not isinstance(model, str) or not 0 < len(model) <= 1024 for key, model in origins.items())):
                    raise ValueError()
            except (OSError, ValueError, KeyError, TypeError):
                raise LegacyOriginIndexError('Legacy origin index has an invalid format') from None
            self._signature, self._origins = signature, origins
            return origins
