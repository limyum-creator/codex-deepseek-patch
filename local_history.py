"""Read one native chat's visible pre-compaction history, without changing it.

No database access, credentials, decryption, native process, or whole-library
content index. A matching chat identity AND exact compaction item are required.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import uuid

from legacy_history import LegacyHistoryError
from reasoning_origin import _source_path, MAX_ROLLOUT_LINE

MAX_SOURCE_BYTES = 512 * 1024 * 1024
MAX_VISIBLE_BYTES = 64 * 1024 * 1024


def unavailable(detail='The readable local history is missing or incomplete'):
    return LegacyHistoryError('local_history_unavailable', detail +
        '. Restore the original local chat history or supply a readable handoff. No GPT request was made.', 409)


def thread_identity(headers):
    values = []
    for key, value in headers.items():
        if key.lower() in ('thread_id', 'session_id'):
            values.append(value)
        elif key.lower() == 'x-codex-turn-metadata':
            try:
                metadata = json.loads(value)
                values.extend(metadata[k] for k in ('thread_id', 'session_id') if k in metadata)
            except (ValueError, TypeError):
                raise unavailable('The native chat identity is invalid') from None
    try:
        normalized = {str(uuid.UUID(value)) for value in values if isinstance(value, str)}
        if not values or len(normalized) != 1 or any(not isinstance(v, str) for v in values):
            raise ValueError()
        return normalized.pop()
    except (ValueError, AttributeError):
        raise unavailable('The native chat identity is absent or ambiguous') from None


def _public(value):
    if isinstance(value, dict):
        return {key:_public(item) for key,item in value.items()
                if key not in ('encrypted_content', 'reasoning_content', 'signature')}
    if isinstance(value, list):
        return [_public(item) for item in value]
    if isinstance(value, str) and value.startswith(('data:image/', 'data:audio/', 'data:video/')):
        return '[Historical media attachment; pixels/audio are not part of this text recovery]'
    return value


def visible_record(item):
    if not isinstance(item, dict):
        raise unavailable('A native history item is malformed')
    kind = item.get('type', 'message')
    if kind in ('reasoning', 'additional_tools'):
        return None
    if kind == 'message':
        if item.get('role') not in ('user', 'assistant') or item.get('channel') == 'analysis':
            return None
        return _public({key:item[key] for key in ('role', 'content', 'phase') if key in item})
    if kind == 'agent_message':
        return _public({key:item[key] for key in ('type', 'author', 'recipient', 'content') if key in item})
    if kind == 'image_generation_call':
        return _public({**item, 'result':'[Generated image; encoded pixels are not part of text recovery]'})
    if kind in ('function_call', 'function_call_output', 'custom_tool_call', 'custom_tool_call_output',
                'local_shell_call', 'local_shell_call_output', 'web_search_call', 'image_generation_call',
                'computer_call', 'computer_call_output', 'tool_search_call', 'tool_search_output'):
        return _public(item)
    if kind in ('compaction', 'compaction_trigger'):
        return None
    raise unavailable('This native history item format is not yet supported')


def _matches(candidate, item):
    return (isinstance(candidate, dict) and candidate.get('type') == 'compaction'
            and candidate.get('encrypted_content') == item.get('encrypted_content')
            and candidate.get('id') == item.get('id'))


def _find_source(home, thread_id):
    candidates = []
    for name in ('sessions', 'archived_sessions'):
        directory = home / name
        if directory.is_symlink():
            raise unavailable('The local history directory is a symbolic link')
        for parent, dirs, files in os.walk(directory, followlinks=False):
            dirs[:] = [d for d in dirs if not (Path(parent)/d).is_symlink()]
            for filename in files:
                if filename.endswith(thread_id + '.jsonl'):
                    candidates.append(_source_path(Path(parent)/filename, home))
    if len(candidates) != 1:
        raise unavailable('A unique readable file for this native chat was not found')
    return candidates[0]


def _events(home, thread_id, cancelled, budget, ancestry=(), boundary=None):
    if thread_id in ancestry or len(ancestry) >= 32:
        raise unavailable('The native shared-history ancestry is invalid')
    path = _find_source(home, thread_id)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise unavailable('The native history is not a regular file')
        first = True
        previous_ordinal = None
        while True:
            offset = stream.tell()
            if boundary and offset == boundary['end_byte_offset']:
                if previous_ordinal != boundary['end_ordinal_exclusive'] - 1:
                    raise unavailable('The native shared-history boundary does not match its records')
                return
            if boundary and offset > boundary['end_byte_offset']:
                raise unavailable('The native shared-history byte boundary is incomplete')
            raw = stream.readline(MAX_ROLLOUT_LINE + 1)
            if not raw:
                if boundary or first:
                    raise unavailable('A native history prefix is missing')
                return
            if cancelled.is_set():
                raise LegacyHistoryError('request_cancelled', 'History recovery was cancelled', 499)
            budget[0] += len(raw)
            if len(raw) > MAX_ROLLOUT_LINE or budget[0] > MAX_SOURCE_BYTES or not raw.endswith(b'\n'):
                raise unavailable('The readable history exceeds the recovery size limit or has an incomplete record')
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise unavailable('A native history record is malformed')
            ordinal = event.get('ordinal')
            if ordinal is not None:
                if type(ordinal) is not int or ordinal < 0 or previous_ordinal is not None and ordinal != previous_ordinal + 1:
                    raise unavailable('The native history has a missing or reordered record')
                previous_ordinal = ordinal
            elif boundary or previous_ordinal is not None:
                raise unavailable('The native history ordinal is missing')
            if boundary and (ordinal >= boundary['end_ordinal_exclusive'] or stream.tell() > boundary['end_byte_offset']):
                raise unavailable('The native shared-history boundary is inconsistent')
            if first:
                first = False
                meta = event.get('payload')
                if event.get('type') != 'session_meta' or not isinstance(meta, dict) or meta.get('id') != thread_id:
                    raise unavailable('The native history belongs to a different chat')
                parent = meta.get('history_base')
                if parent:
                    if (not isinstance(parent, dict) or type(parent.get('end_byte_offset')) is not int
                            or parent['end_byte_offset'] <= 0 or type(parent.get('end_ordinal_exclusive')) is not int
                            or parent['end_ordinal_exclusive'] <= 0):
                        raise unavailable('The native shared-history reference is invalid')
                    parent_id = thread_identity({'thread_id':parent.get('thread_id')})
                    if meta.get('forked_from_id') not in (None, parent_id):
                        raise unavailable('The native shared-history parent is inconsistent')
                    if ordinal is not None and ordinal != parent['end_ordinal_exclusive']:
                        raise unavailable('The native shared-history continuation has a gap')
                    yield from _events(home, parent_id, cancelled, budget, ancestry + (thread_id,), parent)
                elif ordinal is not None and ordinal != 0:
                    raise unavailable('The initial native history prefix is absent')
                continue
            if event.get('type') == 'session_meta':
                raise unavailable('The native history has a duplicate chat identity')
            yield event


def read_visible_history(home, thread_id, item, *, cancelled):
    home = Path(home)
    if not home.is_absolute() or home.is_symlink():
        raise unavailable('The local history root is invalid')
    records, visible_bytes, missing_prefix = [], 0, False
    events = _events(home, thread_id, cancelled, [0])
    try:
        for event in events:
            kind, payload = event.get('type'), event.get('payload')
            if kind == 'event_msg' and isinstance(payload, dict) and payload.get('type') == 'thread_rolled_back':
                raise unavailable('This history contains a rollback that needs a native readable export')
            if kind == 'compacted':
                if not isinstance(payload, dict) or not isinstance(payload.get('replacement_history'), list):
                    raise unavailable('The native compaction record is incomplete')
                replacement = payload['replacement_history']
                if any(_matches(part, item) for part in replacement):
                    if missing_prefix or not records:
                        raise unavailable('The text preceding this encrypted compaction is absent')
                    return records
                if not records and any(isinstance(v,dict) and v.get('type') == 'compaction' for v in replacement):
                    missing_prefix = True
            elif kind == 'response_item':
                if _matches(payload, item):
                    if missing_prefix or not records:
                        raise unavailable('The text preceding this encrypted compaction is absent')
                    return records
                if isinstance(payload,dict) and payload.get('type') == 'compaction' and not records:
                    missing_prefix = True
                record = visible_record(payload)
                if record is not None:
                    visible_bytes += len(json.dumps(record, ensure_ascii=False).encode())
                    if visible_bytes > MAX_VISIBLE_BYTES:
                        raise unavailable('The readable history exceeds the recovery size limit')
                    records.append(record)
            elif kind not in ('event_msg', 'turn_context', 'token_usage_record', 'world_state',
                              'inter_agent_communication_metadata'):
                raise unavailable('This native history storage format needs a readable export')
    except (OSError, ValueError, TypeError, AttributeError):
        raise unavailable('The readable local history could not be verified') from None
    finally:
        events.close()
    raise unavailable('The requested compaction was not found in this chat')


def _chunks(text, byte_limit):
    raw = text.encode()
    while raw:
        size = min(byte_limit, len(raw))
        while size and size < len(raw) and raw[size] & 0xc0 == 0x80:
            size -= 1
        if not size:
            raise unavailable('The selected model context is too small for history recovery')
        yield raw[:size].decode()
        raw = raw[size:]


def recovery_limits(context_window):
    if type(context_window) is not int or context_window < 4096:
        raise unavailable('The selected model needs a declared context of at least 4096 tokens')
    output_tokens = min(8192, max(512, context_window // 8))
    chunk_bytes = min(MAX_VISIBLE_BYTES, context_window - output_tokens - 2048)
    output_bytes = min(24000, max(1024, chunk_bytes // 3))
    return output_tokens, chunk_bytes, output_bytes


def summarize_records(records, context_window, summarize, *, cancelled):
    """Bounded hierarchical summaries using the selected model's actual window.

    UTF-8 bytes give a conservative token upper bound without a tokenizer or
    extra dependency. This is a recovery chunk size, never a model window cap.
    """
    output_tokens, chunk_bytes, output_bytes = recovery_limits(context_window)
    text = '\n'.join(json.dumps(row, ensure_ascii=False, separators=(',', ':')) for row in records)
    calls = 0
    for level in range(8):
        if calls + (len(text.encode()) + chunk_bytes - 1) // chunk_bytes > 128:
            raise unavailable('This history needs a larger-context model or a readable handoff')
        parts = list(_chunks(text, chunk_bytes))
        if calls + len(parts) > 128:
            raise unavailable('This history needs a larger-context model or a readable handoff')
        summaries, depths, index = [], [0] * len(parts), 0
        while index < len(parts):
            if cancelled.is_set():
                raise LegacyHistoryError('request_cancelled', 'History recovery was cancelled', 499)
            if calls >= 128:
                raise unavailable('This history needs a larger-context model or a readable handoff')
            part = parts[index]
            calls += 1
            try:
                summary = summarize(part, output_tokens, output_bytes, level, index, len(parts))
            except LegacyHistoryError as error:
                # Never cache partial text or repeat the same truncated request.
                # Split only the failing input, keeping all UTF-8 bytes in order.
                # Provider quota/auth/filter/transport failures are not retried.
                size = len(part.encode())
                if error.code != 'local_summary_output_limit' or size <= 1024 or depths[index] >= 8:
                    raise
                left = next(_chunks(part, size // 2))
                smaller = [left, part[len(left):]]
                depth = depths[index] + 1
                parts[index:index+1] = smaller
                depths[index:index+1] = [depth] * len(smaller)
                continue
            if not isinstance(summary, str) or not summary.strip() or len(summary.encode()) > output_bytes:
                raise LegacyHistoryError('local_summary_invalid',
                    'The selected model did not return a complete, bounded history summary. Try another model.', 502)
            summaries.append(summary.strip())
            index += 1
        if len(summaries) == 1:
            return summaries[0]
        reduced = '\n\n'.join(summaries)
        if len(reduced.encode()) >= len(text.encode()):
            raise LegacyHistoryError('local_summary_not_reduced', 'The selected model did not shorten the history', 502)
        text = reduced
    raise LegacyHistoryError('local_summary_not_reduced', 'History summarization reached its bounded reduction limit', 502)
