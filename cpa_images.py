"""Optional stdio MCP for image tools through an existing local CPA installation.

No SDK, listening service, credential copy or automatic generation retry.
"""
from pathlib import Path
import argparse
import base64
import json
import datetime
import os
import re
import shutil
import tempfile
import tomllib
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

from proxy_support import read_api_key


class ImageError(ValueError):
    """A credential-free, user-facing failure."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ImageError('Redirect refused')


def image_type(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png', '.png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg', '.jpg'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp', '.webp'
    raise ImageError('Expected PNG, JPEG or WebP image bytes')


def image_model(model):
    return isinstance(model, str) and (model.startswith(('gpt-image-', 'grok-imagine-image'))
           or model.startswith('gemini-') and 'image' in model)


def safe_error_detail(raw, client_key):
    """Select bounded JSON error fields; never return raw bodies or headers."""
    try:
        obj = json.loads(raw)
    except (ValueError, UnicodeError):
        return ''
    code, message = '', ''
    for _ in range(4):
        if not isinstance(obj, dict):
            break
        error = obj.get('error')
        if isinstance(error, dict):
            obj = error
            continue
        if isinstance(obj.get('code'), str):
            code = obj['code']
        candidate = obj.get('message', error)
        if not isinstance(candidate, str):
            break
        message = candidate
        try:
            nested = json.loads(candidate)
        except ValueError:
            break
        if not isinstance(nested, dict):
            break
        obj = nested
    def redact(value):
        if re.search(r'<(?:!doctype|/?[a-z][^>]*>)', value, flags=re.I):
            return ''
        value = value.replace(client_key, '[redacted]') if client_key else value
        value = re.sub(r'https?://[^\s<>"\\]+', '[link omitted]', value, flags=re.I)
        value = re.sub(r'(?i)\bbearer\s+[^\s,;\"\'}]+', '[credential omitted]', value)
        value = re.sub(r'(?i)\b(?:authorization|access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?key|token)\b[\s:=\"\']+[^\s,;\"\'}]+', '[credential omitted]', value)
        value = re.sub(r'\b(?:sk-|xai-)[A-Za-z0-9_-]{12,}', '[credential omitted]', value)
        value = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[credential omitted]', value)
        return ' '.join(value.split())
    code, message = redact(code), redact(message)
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,120}', code):
        code = ''
    return (' [' + code + ']' if code else '') + (' ' + message[:320] if message else '')


class Images:
    def __init__(self, endpoint, config):
        url = urllib.parse.urlsplit(endpoint)
        if url.scheme not in ('http', 'https') or url.hostname not in ('localhost', '127.0.0.1', '::1') or url.username or url.password or url.query or url.fragment or url.path.rstrip('/') not in ('', '/v1'):
            raise ImageError('CPA endpoint must be a loopback origin')
        self.endpoint = urllib.parse.urlunsplit((url.scheme, url.netloc, '', '', ''))
        self.config = config
        self.opener = urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({}))

    def request(self, path, payload=None):
        # Read current client key only in memory; never retain or return it.
        key = read_api_key(self.config)
        request = urllib.request.Request(self.endpoint + path,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=600) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            try:
                raw_error = error.read(8193)
                detail = safe_error_detail(raw_error, key) if len(raw_error) <= 8192 else ''
            finally:
                error.close()
            raise ImageError('CPA HTTP ' + str(error.code) + detail + '; request not retried') from None
        except (urllib.error.URLError, TimeoutError):
            raise ImageError('CPA connection failed; request not retried. Check original request before retrying.') from None
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ImageError('CPA returned invalid JSON; request not retried') from None
        if not isinstance(result, dict):
            raise ImageError('CPA returned an invalid response object')
        return result

    def models(self):
        rows = self.request('/v1/models').get('data', [])
        return [{'model': row['id'], 'backend': 'gemini-content' if row['id'].startswith('gemini-') else 'openai-images'}
                for row in rows if image_model(row.get('id'))]

    def generate(self, args):
        if not isinstance(args, dict):
            raise ImageError('Image arguments must be an object')
        allowed = {'model', 'prompt', 'output_dir', 'referenced_image_paths', 'aspect_ratio', 'resolution', 'size', 'background'}
        if set(args) - allowed:
            raise ImageError('Unknown image arguments')
        model, prompt = args.get('model'), args.get('prompt')
        if not isinstance(prompt, str) or not prompt.strip():
            raise ImageError('A nonempty prompt is required')
        if not image_model(model) or model not in {row['model'] for row in self.models()}:
            raise ImageError('Choose an image model returned by list_image_models')
        for field in ('output_dir', 'aspect_ratio', 'resolution', 'size', 'background'):
            if field in args and not isinstance(args[field], str):
                raise ImageError(field + ' must be a string')
        if args.get('background') not in (None, 'auto', 'opaque', 'transparent'):
            raise ImageError('Invalid background value')
        directory = Path(args.get('output_dir', ''))
        if not directory.is_absolute():
            raise ImageError('output_dir must be absolute')
        directory.mkdir(parents=True, exist_ok=True)
        # Check destination before consuming a generation; remove only our own probe.
        probe_fd, probe_path = tempfile.mkstemp(prefix='.cpa-output-check-', dir=directory)
        os.close(probe_fd)
        Path(probe_path).unlink()
        refs = args.get('referenced_image_paths', [])
        if not isinstance(refs, list):
            raise ImageError('Reference image paths must be a list')
        reference_parts = []
        for name in refs:
            if not isinstance(name, str) or not Path(name).is_absolute():
                raise ImageError('Reference paths must be absolute')
            with Path(name).open('rb') as reference:
                data = reference.read()
            mime, _ = image_type(data)
            reference_parts.append({'mimeType': mime, 'data': base64.b64encode(data).decode()})
        if model.startswith('gemini-'):
            if args.get('background') or args.get('size'):
                raise ImageError('Gemini uses aspect_ratio and resolution; put background requirements in the prompt')
            image_config = {}
            if args.get('aspect_ratio'):
                image_config['aspectRatio'] = args['aspect_ratio']
            if args.get('resolution'):
                image_config['imageSize'] = args['resolution']
            payload = {'contents': [{'role': 'user', 'parts': [{'text': prompt}] + [{'inlineData': r} for r in reference_parts]}],
                       'generationConfig': {'responseModalities': ['TEXT', 'IMAGE']}}
            if image_config:
                payload['generationConfig']['imageConfig'] = image_config
            result = self.request('/v1beta/models/' + urllib.parse.quote(model, safe='') + ':generateContent', payload)
            parts = [part for candidate in result.get('candidates', []) for part in candidate.get('content', {}).get('parts', []) if not part.get('thought')]
            images = [(part.get('inlineData') or part.get('inline_data', {})).get('data') for part in parts]
            texts = [part['text'] for part in parts if isinstance(part.get('text'), str)]
        else:
            if model.startswith('grok-') and args.get('background'):
                raise ImageError('Put Grok background requirements in the prompt')
            if model.startswith('gpt-') and (args.get('aspect_ratio') or args.get('resolution')):
                raise ImageError('GPT Image uses size, rather than aspect_ratio or resolution')
            payload = {'model': model, 'prompt': prompt, 'response_format': 'b64_json', 'n': 1}
            payload.update({k: args[k] for k in ('aspect_ratio', 'resolution', 'size', 'background') if args.get(k)})
            if reference_parts:
                payload['images'] = [{'image_url': 'data:' + r['mimeType'] + ';base64,' + r['data']} for r in reference_parts]
            result = self.request('/v1/images/' + ('edits' if reference_parts else 'generations'), payload)
            images = [row.get('b64_json') for row in result.get('data', [])]
            texts = [row['revised_prompt'] for row in result.get('data', []) if isinstance(row.get('revised_prompt'), str)]
        images = [value for value in images if isinstance(value, str) and value]
        if not images:
            raise ImageError('CPA returned no inline image; no automatic retry. Inspect the original task status.')
        # Validate every image before writing any files. Use exclusive creation.
        try:
            decoded = [base64.b64decode(value, validate=True) for value in images]
        except ValueError:
            raise ImageError('CPA returned invalid base64 image data; request not retried') from None
        formats = [image_type(data) for data in decoded]
        paths, content = [], []
        for data, encoded, (mime, suffix) in zip(decoded, images, formats):
            path = directory / ('cpa-' + uuid.uuid4().hex + suffix)
            created = False
            try:
                with path.open('xb') as output:
                    created = True
                    output.write(data)
                paths.append(str(path))
            except OSError:
                if created:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                metadata = {'model': model, 'files': paths, 'save_failed': True}
                return {'isError': True, 'content': [{'type': 'text', 'text': 'Generation completed, but saving failed. Do not generate again automatically. ' + json.dumps(metadata)}] + content, 'structuredContent': metadata}
            content.append({'type': 'image', 'mimeType': mime, 'data': encoded})
        metadata = {'model': model, 'files': paths, 'text': texts}
        return {'content': [{'type': 'text', 'text': json.dumps(metadata, ensure_ascii=False)}] + content,
                'structuredContent': metadata}


TOOLS = [
    {'name': 'list_image_models', 'description': 'List image-generation models available through the existing local CPA account connections. Read only.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
     'annotations': {'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': False}},
    {'name': 'generate_image', 'description': 'Generate or edit images through existing CPA account connections. Performs a real generation; call only when the user requests generation. Select an image model from list_image_models, independently of the conversation model. Returns inline images and exact local paths. Calls are never retried automatically.',
     'inputSchema': {'type': 'object', 'properties': {
         'model': {'type': 'string'}, 'prompt': {'type': 'string'}, 'output_dir': {'type': 'string', 'description': 'Absolute destination directory'},
         'referenced_image_paths': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 5},
         'aspect_ratio': {'type': 'string', 'description': 'Grok/Gemini only, e.g. 16:9'},
         'resolution': {'type': 'string', 'description': 'Grok: 1k/2k; Gemini: 1K/2K/4K'},
         'size': {'type': 'string', 'description': 'GPT Image only, e.g. 1536x1024'},
         'background': {'type': 'string', 'enum': ['auto', 'opaque', 'transparent'], 'description': 'GPT Image only'}},
         'required': ['model', 'prompt', 'output_dir'], 'additionalProperties': False},
     'annotations': {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': False, 'openWorldHint': True}}
]


MANAGED_MARKER = '# Codex CPA image tools - managed by codex-deepseek-patch'


def configure(args):
    """Set up or remove only this optional MCP entry, backing up full config first.

    Namespace: proxy_config, proxy_base_url, codex_home, script, disable.
    Never reads/copies CPA keys or changes model/provider/default selection.
    """
    home = Path(args.codex_home).expanduser().resolve()
    path = home / 'config.toml'
    original = path.read_bytes().decode('utf-8') if path.exists() else ''
    parsed = tomllib.loads(original)
    servers = parsed.get('mcp_servers', {})
    if not isinstance(servers, dict):
        raise ImageError('Existing mcp_servers is not a TOML table')
    # A prefix parses only outside a multiline string/array; preserve their text.
    headers = []
    for match in re.finditer(r'(?m)^[ \t]*\[', original):
        try:
            tomllib.loads(original[:match.start()])
        except ValueError:
            continue
        headers.append(match.start())
    section = next((match for match in re.finditer(r'(?m)^\[mcp_servers\.cpa_images\][ \t]*(?:#[^\r\n]*)?\r?$', original) if match.start() in headers), None)
    owned = False
    start = end = None
    if 'cpa_images' in servers:
        if not section:
            raise ImageError('Existing cpa_images uses an unsupported TOML layout; preserved')
        previous_line_start = original.rfind('\n', 0, max(0, section.start() - 1)) + 1
        owned = original[previous_line_start:section.start()].strip() == MANAGED_MARKER
        if not owned:
            raise ImageError('Existing cpa_images belongs to another tool; preserved')
        if any(re.match(r'\[\[?mcp_servers\.cpa_images[.\]]', original[index:]) for index in headers if index > section.start()):
            raise ImageError('Existing cpa_images has additional tables; preserved')
        start = previous_line_start
        end = next((index for index in headers if index > section.start()), len(original))
    if args.disable:
        if not owned:
            return {'status': 'unchanged', 'config': str(path)}
        updated = original[:start] + original[end:]
    else:
        script = Path(args.script).expanduser().resolve()
        proxy_config = Path(args.proxy_config).expanduser().resolve() if args.proxy_config else None
        if not script.is_file() or not proxy_config or not proxy_config.is_file():
            raise ImageError('Existing CPA config and image-tool script are required')
        client = Images(args.proxy_base_url, proxy_config)
        command = Path(sys.executable)
        if command.name.lower() == 'pythonw.exe':
            command = command.with_name('python.exe')
            if not command.is_file():
                raise ImageError('Console Python is required for stdio MCP')
        block = (MANAGED_MARKER + '\n[mcp_servers.cpa_images]\ncommand = ' + json.dumps(str(command)) +
                 '\nargs = ' + json.dumps([str(script), '--config', str(proxy_config), '--endpoint', client.endpoint]) +
                 '\nstartup_timeout_sec = 20\ntool_timeout_sec = 900\n')
        updated = original[:start] + block + original[end:] if owned else original + ('\n' if original and not original.endswith('\n') else '') + ('\n' if original else '') + block
    tomllib.loads(updated)
    if updated == original:
        return {'status': 'unchanged', 'config': str(path)}
    home.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        backup = home / ('config.toml.cpa-images-backup-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8])
        shutil.copy2(path, backup)
    handle, temporary = tempfile.mkstemp(prefix='.config-cpa-images-', dir=home)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8', newline='') as output:
            output.write(updated)
            output.flush()
            os.fsync(output.fileno())
        if path.exists():
            shutil.copymode(path, temporary)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {'status': 'disabled' if args.disable else 'configured', 'config': str(path), 'backup': str(backup) if backup else None}


def main():
    # MCP stdio is UTF-8 even when Windows redirects pipes using CP936/GBK.
    for stream in (sys.stdin, sys.stdout):
        reconfigure = getattr(stream, 'reconfigure', None)
        if callable(reconfigure):
            reconfigure(encoding='utf-8', errors='strict')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', default='http://127.0.0.1:8317')
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    client = Images(args.endpoint, args.config)
    for line in sys.stdin:
        request = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ImageError('Invalid request')
            if 'id' not in request:
                continue
            method, params = request.get('method'), request.get('params', {})
            if not isinstance(params, dict):
                raise ImageError('Invalid parameters')
            if method == 'initialize':
                result = {'protocolVersion': params.get('protocolVersion') if params.get('protocolVersion') in ('2024-11-05', '2025-03-26', '2025-06-18') else '2025-06-18', 'capabilities': {'tools': {}},
                          'serverInfo': {'name': 'cpa-images', 'version': '0.1.0'}}
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                result = {'tools': TOOLS}
            elif method == 'tools/call':
                try:
                    if params.get('name') == 'list_image_models':
                        result = {'content': [{'type': 'text', 'text': json.dumps(client.models())}]}
                    elif params.get('name') == 'generate_image':
                        result = client.generate(params.get('arguments', {}))
                    else:
                        raise ImageError('Unknown image tool')
                except (ValueError, OSError, TypeError, KeyError, AttributeError) as error:
                    # Return only our own messages; no raw network bodies or credentials.
                    message = str(error) if isinstance(error, ImageError) else 'Image request failed. Check model, parameters, paths and CPA status. Do not retry generation automatically.'
                    result = {'isError': True, 'content': [{'type': 'text', 'text': message}]}
            else:
                print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'error': {'code': -32601, 'message': 'Method not found'}}), flush=True)
                continue
            print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
        except (ValueError, TypeError):
            print(json.dumps({'jsonrpc': '2.0', 'id': request.get('id') if isinstance(request, dict) else None,
                              'error': {'code': -32700, 'message': 'Invalid request'}}), flush=True)


if __name__ == '__main__':
    main()
