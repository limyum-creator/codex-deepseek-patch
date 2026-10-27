from windows_support import background_options
import argparse
import base64
import http.server
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import cpa_images as images

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jX1EAAAAASUVORK5CYII=')
ENCODED = base64.b64encode(PNG).decode()
MODELS = ['grok-imagine-image', 'grok-imagine-image-quality', 'grok-imagine-image-2.0', 'gpt-image-2', 'gemini-3.1-flash-image', 'grok-4.7']


class Stub(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.server.records.append(('GET', self.path, None, self.headers.get('Authorization')))
        self.reply({'data': [{'id': model} for model in MODELS]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.records.append(('POST', self.path, body, self.headers.get('Authorization')))
        if self.server.mode == 'redirect':
            self.send_response(302)
            self.send_header('Location', self.server.endpoint + '/credential-leak')
            self.end_headers()
            return
        if self.server.mode == 'spending-limit':
            self.reply({'error': {'message': json.dumps({'code': 'personal-team-blocked:spending-limit', 'error': 'You have run out of credits or need a Grok subscription. Add credits at https://grok.com/?_s=usage'})}}, 403)
            return
        if self.server.mode == 'secret-error':
            self.reply({'error': {'code': 'permission_denied', 'message': 'Denied synthetic-client-key Authorization: Bearer private-header-value access_token=private-oauth-value https://example.test/?token=private-query-value'}}, 403)
            return
        if self.server.mode == 'http-error':
            self.reply({'secret': 'must-never-appear'}, 429)
            return
        if self.server.mode == 'url-only':
            self.reply({'data': [{'url': 'http://127.0.0.1/private'}]})
            return
        if self.path.startswith('/v1beta/'):
            result={'candidates': [{'content': {'parts': [
                {'thought': True, 'inlineData': {'mimeType': 'image/png', 'data': ENCODED}},
                {'text': 'Synthetic fixture'}, {'inlineData': {'mimeType': 'image/png', 'data': ENCODED}}]}}]}
            if self.server.mode=='large':result['provider_metadata']='x'*64_000_001
            self.reply(result)
        else:
            self.reply({'data': [{'b64_json': ENCODED, 'revised_prompt': 'Synthetic fixture'}] * (2 if self.server.mode == 'two' else 1)})

    def reply(self, body, status=200):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class ImagesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = self.root / 'cpa.yaml'
        self.config.write_text('api-keys:\n  - synthetic-client-key\n')
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Stub)
        self.server.daemon_threads = True
        self.server.records, self.server.mode = [], 'normal'
        self.server.endpoint = 'http://127.0.0.1:' + str(self.server.server_port)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.client = images.Images(self.server.endpoint + '/v1', self.config)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temporary.cleanup()

    def args(self, model='grok-imagine-image', **extra):
        return {'model': model, 'prompt': 'Synthetic image fixture', 'output_dir': str(self.root / 'output'), **extra}

    def test_models_filters_chat_models_without_inference(self):
        self.assertEqual([row['model'] for row in self.client.models()], MODELS[:-1])
        self.assertEqual([r[0] for r in self.server.records], ['GET'])

    def test_provider_owns_reference_count_image_and_response_sizes(self):
        large=self.root/'large.png';large.write_bytes(PNG+b'\x00'*16_000_001)
        small=self.root/'small.png';small.write_bytes(PNG)
        self.server.mode='large'
        self.client.generate(self.args('gemini-3.1-flash-image',prompt='x'*100001,
            referenced_image_paths=[str(large)]+[str(small)]*5))
        posts=[row for row in self.server.records if row[0]=='POST']
        self.assertEqual(len(posts),1)
        parts=posts[0][2]['contents'][0]['parts']
        self.assertEqual(len(parts),7)
        self.assertEqual(len(parts[0]['text']),100001)
        self.assertEqual(base64.b64decode(parts[1]['inlineData']['data']),large.read_bytes())

    def test_grok_generation_saves_and_returns_exact_bytes(self):
        args = self.args(aspect_ratio='16:9', resolution='1k')
        result = self.client.generate(args)
        request = self.server.records[-1]
        self.assertEqual(request[1], '/v1/images/generations')
        self.assertEqual(request[2], {'model': args['model'], 'prompt': args['prompt'], 'response_format': 'b64_json', 'n': 1, 'aspect_ratio': '16:9', 'resolution': '1k'})
        path = Path(result['structuredContent']['files'][0])
        self.assertEqual(path.read_bytes(), PNG)
        self.assertEqual(result['content'][1], {'type': 'image', 'mimeType': 'image/png', 'data': ENCODED})

    def test_edits_send_reference_to_images_endpoint(self):
        reference = self.root / 'reference.png'
        reference.write_bytes(PNG)
        for model in ['grok-imagine-image-2.0', 'gpt-image-2']:
            self.client.generate(self.args(model, referenced_image_paths=[str(reference)]))
            request = self.server.records[-1]
            self.assertEqual(request[1], '/v1/images/edits')
            self.assertEqual(request[2]['images'], [{'image_url': 'data:image/png;base64,' + ENCODED}])

    def test_gpt_generation_uses_own_options(self):
        self.client.generate(self.args('gpt-image-2', size='1536x1024', background='transparent'))
        self.assertEqual(self.server.records[-1][2]['background'], 'transparent')
        self.assertEqual(self.server.records[-1][2]['size'], '1536x1024')

    def test_gemini_native_generation_and_edit_exclude_thought_images(self):
        reference = self.root / 'ref.png'
        reference.write_bytes(PNG)
        result = self.client.generate(self.args('gemini-3.1-flash-image', referenced_image_paths=[str(reference)], aspect_ratio='16:9', resolution='2K'))
        request = self.server.records[-1]
        self.assertEqual(request[1], '/v1beta/models/gemini-3.1-flash-image:generateContent')
        self.assertEqual(request[2]['generationConfig'], {'responseModalities': ['TEXT', 'IMAGE'], 'imageConfig': {'aspectRatio': '16:9', 'imageSize': '2K'}})
        self.assertEqual(request[2]['contents'][0]['parts'][1]['inlineData']['data'], ENCODED)
        self.assertEqual(len(result['structuredContent']['files']), 1)

    def test_invalid_models_and_cross_backend_options_do_not_submit_generation(self):
        for args in [self.args('grok-4.7'), self.args('gemini-3.1-flash-image', background='transparent'), self.args('gpt-image-2', aspect_ratio='16:9'), self.args('grok-imagine-image', unknown=True), self.args(prompt='')]:
            with self.assertRaises(images.ImageError):
                self.client.generate(args)
        self.assertFalse(any(r[0] == 'POST' for r in self.server.records))

    def test_unwritable_destination_fails_before_generation(self):
        occupied = self.root / 'occupied'
        occupied.write_text('existing')
        with self.assertRaises(OSError):
            self.client.generate(self.args(output_dir=str(occupied)))
        self.assertFalse(any(r[0] == 'POST' for r in self.server.records))

    def test_missing_relative_and_invalid_references_do_not_submit_generation(self):
        invalid = self.root / 'bad.png'
        invalid.write_text('not an image')
        for refs in [['relative.png'], [str(self.root / 'missing')], [str(invalid)]]:
            with self.assertRaises((ValueError, OSError)):
                self.client.generate(self.args(referenced_image_paths=refs))
        self.assertFalse(any(r[0] == 'POST' for r in self.server.records))

    def test_no_url_download_or_automatic_retry(self):
        self.server.mode = 'url-only'
        with self.assertRaisesRegex(images.ImageError, 'no inline image'):
            self.client.generate(self.args())
        self.assertEqual([r[:2] for r in self.server.records], [('GET', '/v1/models'), ('POST', '/v1/images/generations')])

    def test_http_failure_hides_body_and_is_not_retried(self):
        self.server.mode = 'http-error'
        with self.assertRaisesRegex(images.ImageError, 'HTTP 429') as captured:
            self.client.generate(self.args())
        self.assertNotIn('must-never-appear', str(captured.exception))
        self.assertEqual(len(self.server.records), 2)

    def test_spending_limit_json_code_is_safely_reported(self):
        self.server.mode = 'spending-limit'
        with self.assertRaises(images.ImageError) as captured:
            self.client.generate(self.args())
        message = str(captured.exception)
        self.assertIn('HTTP 403', message)
        self.assertIn('personal-team-blocked:spending-limit', message)
        self.assertIn('run out of credits', message)
        self.assertNotIn('https://', message)
        self.assertEqual(len(self.server.records), 2)

    def test_error_echoed_credentials_and_urls_are_not_reported(self):
        self.server.mode = 'secret-error'
        with self.assertRaises(images.ImageError) as captured:
            self.client.generate(self.args())
        message = str(captured.exception)
        for secret in ['synthetic-client-key', 'private-header-value', 'private-oauth-value', 'private-query-value']:
            self.assertNotIn(secret, message)
        self.assertIn('permission_denied', message)

    def test_non_json_errors_are_omitted_and_json_messages_are_bounded(self):
        self.assertEqual(images.safe_error_detail(b'<html>private secret</html>', 'secret'), '')
        value = images.safe_error_detail(json.dumps({'error': {'code': 'denied', 'message': 'x' * 10000}}).encode(), 'secret')
        self.assertLess(len(value), 340)

    def test_redirect_is_not_followed_and_key_never_forwarded(self):
        self.server.mode = 'redirect'
        with self.assertRaises(ValueError):
            self.client.generate(self.args())
        self.assertEqual([r[1] for r in self.server.records], ['/v1/models', '/v1/images/generations'])

    def test_latest_key_is_read_per_request(self):
        self.client.models()
        self.config.write_text('api-keys:\n  - synthetic-key-two\n')
        self.client.models()
        self.assertEqual([r[3] for r in self.server.records], ['Bearer synthetic-client-key', 'Bearer synthetic-key-two'])

    def test_repeat_output_never_overwrites(self):
        first = self.client.generate(self.args())['structuredContent']['files'][0]
        second = self.client.generate(self.args())['structuredContent']['files'][0]
        self.assertNotEqual(first, second)
        self.assertEqual(Path(first).read_bytes(), PNG)

    def test_partial_save_failure_reports_exact_completed_files(self):
        self.server.mode = 'two'
        original_open = Path.open
        count = 0
        def failing_open(path, mode='r', *args, **kwargs):
            nonlocal count
            if mode == 'xb':
                count += 1
                if count == 2:
                    raise OSError('synthetic disk failure')
            return original_open(path, mode, *args, **kwargs)
        with patch.object(Path, 'open', failing_open):
            result = self.client.generate(self.args())
        self.assertTrue(result['isError'])
        files = result['structuredContent']['files']
        self.assertEqual(len(files), 1)
        self.assertEqual(Path(files[0]).read_bytes(), PNG)
        self.assertEqual(list((self.root / 'output').glob('*.png')), [Path(files[0])])

    def test_exclusive_creation_failure_never_deletes_existing_file(self):
        output = self.root / 'output'
        output.mkdir()
        existing = output / 'cpa-fixed.png'
        existing.write_text('preserve-me')
        fixed = type('FixedUuid', (), {'hex': 'fixed'})()
        with patch.object(images.uuid, 'uuid4', return_value=fixed):
            result = self.client.generate(self.args())
        self.assertTrue(result['isError'])
        self.assertEqual(result['structuredContent']['files'], [])
        self.assertEqual(existing.read_text(), 'preserve-me')

    def test_nonloopback_or_embedded_credential_endpoint_is_rejected(self):
        for endpoint in ['https://example.com', 'http://x:y@localhost:8317', 'http://localhost:8317/admin', 'file:///tmp/test', 'http://localhost:8317?key=secret']:
            with self.assertRaises(images.ImageError):
                images.Images(endpoint, self.config)

    def test_stdio_mcp_initialize_discovery_and_read_only_call(self):
        requests = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26'}},
                    {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
                    {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'list_image_models', 'arguments': {}}}]
        result = subprocess.run([sys.executable, str(Path(images.__file__)), '--config', str(self.config), '--endpoint', self.server.endpoint],
                                input='\n'.join(json.dumps(r) for r in requests)+'\n', capture_output=True, text=True, check=True, **background_options())
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([r['id'] for r in replies], [1, 2, 3])
        self.assertEqual([t['name'] for t in replies[1]['result']['tools']], ['list_image_models', 'generate_image'])
        self.assertNotIn('synthetic-client-key', result.stdout + result.stderr)
        self.assertEqual([r[0] for r in self.server.records], ['GET'])

    def test_stdio_main_accepts_in_memory_text_streams(self):
        input_stream = io.StringIO(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26'}}) + '\n')
        output_stream = io.StringIO()
        arguments = [str(Path(images.__file__)), '--config', str(self.config), '--endpoint', self.server.endpoint]
        with patch.object(sys, 'stdin', input_stream), patch.object(sys, 'stdout', output_stream), patch.object(sys, 'argv', arguments):
            images.main()
        self.assertEqual(json.loads(output_stream.getvalue())['result']['serverInfo']['name'], 'cpa-images')

    def test_stdio_utf8_chinese_generation_with_gbk_process_default(self):
        prompt = '中文提示词：蓝色方块'
        output_dir = self.root / '中文输出'
        requests = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26'}},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'generate_image', 'arguments': self.args(prompt=prompt, output_dir=str(output_dir))}},
        ]
        environment = {**os.environ, 'PYTHONIOENCODING': 'gbk'}
        result = subprocess.run([sys.executable, str(Path(images.__file__)), '--config', str(self.config), '--endpoint', self.server.endpoint],
            input=('\n'.join(json.dumps(request, ensure_ascii=False) for request in requests) + '\n').encode('utf-8'),
            capture_output=True, env=environment, **background_options())
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        replies = [json.loads(line) for line in result.stdout.decode('utf-8').splitlines()]
        self.assertFalse(replies[1]['result'].get('isError'), replies[1])
        self.assertEqual(self.server.records[-1][2]['prompt'], prompt)
        files = replies[1]['result']['structuredContent']['files']
        self.assertEqual(Path(files[0]).parent, output_dir)
        self.assertEqual(Path(files[0]).read_bytes(), PNG)


class ConfigureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.proxy = self.root / 'cpa.yaml'
        self.proxy.write_text('api-keys:\n  - synthetic-config-secret\n')
        self.path = self.root / 'config.toml'
        self.original = 'model = "unchanged-model"\n[features]\nmemories = false\n[mcp_servers.other]\ncommand = "other"\n'
        self.path.write_text(self.original)
        self.args = argparse.Namespace(proxy_config=self.proxy, proxy_base_url='http://127.0.0.1:8317/v1', codex_home=self.root, script=Path(images.__file__), disable=False)

    def tearDown(self):
        self.temporary.cleanup()

    def test_configure_preserves_and_backs_up_then_is_idempotent(self):
        result = images.configure(self.args)
        self.assertEqual(Path(result['backup']).read_text(), self.original)
        content = self.path.read_text()
        self.assertTrue(content.startswith(self.original))
        self.assertNotIn('synthetic-config-secret', content)
        self.assertEqual(images.configure(self.args)['status'], 'unchanged')
        self.assertEqual(len(list(self.root.glob('config.toml.cpa-images-backup-*'))), 1)

    def test_disable_only_own_section_preserves_following_tables(self):
        images.configure(self.args)
        following = '\n[another]\nvalue = 8\n'
        self.path.write_text(self.path.read_text() + following)
        self.args.disable = True
        result = images.configure(self.args)
        self.assertEqual(result['status'], 'disabled')
        self.assertEqual(self.path.read_text(), self.original + '\n[another]\nvalue = 8\n')

    def test_unknown_same_name_is_preserved_without_backup(self):
        existing = self.original + '\n[mcp_servers.cpa_images]\ncommand="someone-else"\n'
        self.path.write_text(existing)
        for disable in [False, True]:
            self.args.disable = disable
            with self.assertRaisesRegex(images.ImageError, 'another tool'):
                images.configure(self.args)
        self.assertEqual(self.path.read_text(), existing)
        self.assertFalse(list(self.root.glob('*backup*')))

    def test_atomic_update_keeps_other_content_and_new_args(self):
        images.configure(self.args)
        self.args.proxy_base_url = 'http://127.0.0.1:9999/v1'
        images.configure(self.args)
        self.assertIn('http://127.0.0.1:9999', self.path.read_text())
        self.assertTrue(self.path.read_text().startswith(self.original))
        self.assertEqual(len(list(self.root.glob('*backup*'))), 2)

    def test_literal_fake_table_headers_are_preserved(self):
        literal = 'note = \"\"\"\n[mcp_servers.cpa_images]\ncommand = \"fake\"\n\"\"\"\n'
        self.path.write_text(literal + self.original)
        images.configure(self.args)
        self.assertTrue(self.path.read_text().startswith(literal + self.original))
        self.assertEqual(images.configure(self.args)['status'], 'unchanged')
        self.args.disable = True
        images.configure(self.args)
        self.assertIn(literal, self.path.read_text())

    def test_existing_crlf_bytes_are_preserved(self):
        original = self.original.replace('\n', '\r\n').encode()
        self.path.write_bytes(original)
        images.configure(self.args)
        self.assertTrue(self.path.read_bytes().startswith(original))
        self.assertEqual(images.configure(self.args)['status'], 'unchanged')

    def test_invalid_toml_or_missing_script_preserves_config(self):
        self.path.write_text('broken = [')
        with self.assertRaises(ValueError):
            images.configure(self.args)
        self.assertEqual(self.path.read_text(), 'broken = [')
        self.path.write_text(self.original)
        self.args.script = self.root / 'missing.py'
        with self.assertRaises(images.ImageError):
            images.configure(self.args)
        self.assertEqual(self.path.read_text(), self.original)


if __name__ == '__main__':
    unittest.main()
