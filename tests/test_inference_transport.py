from windows_support import background_options
import base64
import gc
import hashlib
import http.server
import io
import os
from pathlib import Path
import shutil
import select
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import unittest
import zlib
from unittest.mock import patch

from inference_transport import (WebSocket, WebSocketError, accept_websocket,
                                 connect_websocket, create_http_connection, sanitize_headers,
                                 _accept_deflate, _deflate_configuration, _deflate_parameters)


class NativeCompressionTests(unittest.TestCase):
    def test_native_zstd_roundtrip_invalid_data_and_cancellation(self):
        from inference_transport import zstd_body
        node=os.environ.get('CODEX_TEST_COMPRESSION_NODE') or shutil.which('node')
        if not node:self.skipTest('A zstd-capable vendor/test runtime is unavailable')
        value=b'Native compression preserves images, tools and bytes.\x00\xff'*100000
        wire=zstd_body(value,decompress=False,node=node)
        self.assertLess(len(wire),len(value))
        self.assertEqual(zstd_body(wire,node=node),value)
        with self.assertRaises(ValueError):zstd_body(b'not-zstd-private-fixture',node=node)
        cancelled=threading.Event();cancelled.set()
        with self.assertRaisesRegex(OSError,'cancelled'):
            zstd_body(wire,node=node,cancelled=cancelled)

    def test_codec_slow_pipe_writes_finish_and_midflight_cancel_reaps_child(self):
        from inference_transport import zstd_body
        node=os.environ.get('CODEX_TEST_COMPRESSION_NODE') or shutil.which('node')
        if not node:self.skipTest('A zstd-capable vendor/test runtime is unavailable')
        original=subprocess.Popen;children=[];delay=[250]
        def delayed(args,**kwargs):
            args=list(args)
            args[2]='Atomics.wait(new Int32Array(new SharedArrayBuffer(4)),0,0,'+str(delay[0])+');'+args[2]
            child=original(args,**kwargs);children.append(child);return child
        raw=b'pipe payload with complete input'*100000
        with patch('inference_transport.subprocess.Popen',side_effect=delayed),patch.dict('sys.modules',{'compression':None}):
            compressed=zstd_body(raw,decompress=False,node=node)
            self.assertEqual(zstd_body(compressed,node=node),raw)
            delay[0]=30000;cancelled=threading.Event();timer=threading.Timer(.2,cancelled.set);timer.start()
            try:
                with self.assertRaisesRegex(OSError,'cancelled'):
                    zstd_body(raw,decompress=False,node=node,cancelled=cancelled)
            finally:timer.cancel();timer.join()
        self.assertTrue(all(child.poll() is not None for child in children))
        self.assertTrue(all(child.stdin.closed and child.stdout.closed for child in children))


def frame(payload=b'', opcode=1, *, final=True, masked=True, rsv=0):
    key = b'abcd'
    prefix = bytes([(128 if final else 0) | rsv | opcode])
    n = len(payload)
    if n < 126:
        prefix += bytes([(128 if masked else 0) | n])
    elif n <= 65535:
        prefix += bytes([(128 if masked else 0) | 126]) + struct.pack('!H', n)
    else:
        prefix += bytes([(128 if masked else 0) | 127]) + struct.pack('!Q', n)
    return prefix + (key + bytes(v ^ key[i % 4] for i, v in enumerate(payload)) if masked else payload)


class RecordingSocket:
    def __init__(self):
        self.output = bytearray()
        self.closed = False

    def sendall(self, payload):
        self.output.extend(payload)

    def shutdown(self, direction):
        pass

    def close(self):
        self.closed = True


class PartialReader(io.BytesIO):
    def read(self, n=-1):
        return super().read(min(n, 3) if n >= 0 else 3)


class FramesTests(unittest.TestCase):
    def reader(self, wire, **kwargs):
        sock = RecordingSocket()
        return WebSocket(sock, PartialReader(wire), **kwargs), sock

    def test_partial_reads_reassemble_utf8_fragment_with_interleaved_ping(self):
        wire = (frame(b'\xe4', final=False) + frame(b'ping', opcode=9)
                + frame(b'\xbd\xa0', opcode=0))
        ws, sock = self.reader(wire)
        self.assertEqual(ws.recv(), (1, '你'.encode()))
        self.assertEqual(bytes(sock.output), frame(b'ping', opcode=10, masked=False))
        ws.close()

    def test_deflate_handles_rfc_vector_fragmentation_context_and_uncompressed_messages(self):
        # RFC 7692 section 7.2.3.1, independent of this transport's compressor.
        params = _deflate_configuration({}, is_client=False)
        wire = (frame(bytes.fromhex('f248'), final=False, rsv=64)
                + frame(b'ping', opcode=9)
                + frame(bytes.fromhex('cdc9c90700'), opcode=0))
        ws, sock = self.reader(wire, deflate=params)
        self.assertEqual(ws.recv(), (1, b'Hello'))
        self.assertEqual(bytes(sock.output), frame(b'ping', opcode=10, masked=False))
        ws.close()
        for reset in (True, False):
            for bits in (9, 15):
                with self.subTest(reset=reset, bits=bits):
                    wire, compressor = b'', None
                    values = [b'', b'\x00\xff' * 500, b'\x00\xff' * 500, b'plain', b'end']
                    for i, value in enumerate(values):
                        if i == 3:
                            wire += frame(value, opcode=2)
                            continue
                        if compressor is None or reset:
                            compressor = zlib.compressobj(wbits=-bits)
                        payload = compressor.compress(value) + compressor.flush(zlib.Z_SYNC_FLUSH)
                        wire += frame(payload[:-4], opcode=2, rsv=64)
                    config = {'incoming_bits': bits, 'outgoing_bits': bits,
                              'incoming_reset': reset, 'outgoing_reset': reset}
                    ws, _ = self.reader(wire, deflate=config)
                    self.assertEqual([ws.recv() for _ in values], [(2, v) for v in values])
                    ws.close()

    def test_deflate_bounds_expanded_data_and_rejects_invalid_frames(self):
        params = _deflate_configuration({}, is_client=False)
        compressor = zlib.compressobj(wbits=-15)
        bomb = (compressor.compress(b'x' * 100_000) + compressor.flush(zlib.Z_SYNC_FLUSH))[:-4]
        for wire in (frame(bomb, rsv=64),
                     frame(bomb[:3], rsv=64, final=False) + frame(bomb[3:], opcode=0),
                     frame(b'bad', rsv=64, opcode=9),
                     frame(b'\xff\xff', rsv=64),
                     frame(b'x', rsv=64, opcode=0)):
            with self.subTest(wire=wire[:10]):
                ws, sock = self.reader(wire, deflate=params, max_message=256)
                with self.assertRaises(WebSocketError):
                    ws.recv()
                self.assertTrue(sock.closed)

    def test_deflate_send_preserves_payload_and_serializes_context(self):
        for reset in (False, True):
            params = _deflate_configuration(
                {'server_no_context_takeover': True, 'client_no_context_takeover': True} if reset else {},
                is_client=False)
            ws, sock = self.reader(b'', deflate=params)
            values = [b'', b'Hello' * 1000, b'Hello' * 1000, os.urandom(1000)]
            for value in values:
                ws.send(value, opcode=2)
            wire = bytes(sock.output)
            self.assertTrue(wire[0] & 64)
            self.assertLess(len(wire), sum(map(len, values)) // 2)
            peer, _ = self.reader(wire, deflate=params, is_client=True)
            self.assertEqual([peer.recv() for _ in values], [(2, v) for v in values])
            ws.close()
            peer.close()

    def test_large_upload_fragments_allow_heartbeat_before_message_finishes(self):
        ws, sock = self.reader(b'')
        written = sock.sendall
        observed = []
        def sendall(payload):
            written(payload)
            observed.append(payload[0])
        sock.sendall = sendall
        original = ws._send_frame
        def send_frame(payload, opcode, **kwargs):
            original(payload, opcode, **kwargs)
            if len(observed) == 1:
                # Same path as the reader's heartbeat callback during upload.
                thread = threading.Thread(target=lambda: ws.send_control(10, b'keepalive'))
                thread.start()
                thread.join(1)
                self.assertFalse(thread.is_alive(), 'Large upload blocked heartbeat')
        ws._send_frame = send_frame
        value = os.urandom(180_000)
        ws.send(value, opcode=2)
        self.assertEqual(observed, [2, 138, 0, 128])
        peer, _ = self.reader(bytes(sock.output), is_client=True)
        controls = []
        peer.on_control = lambda kind, data: controls.append((kind, data))
        self.assertEqual(peer.recv(), (2, value))
        self.assertEqual(controls, [(10, b'keepalive')])
        ws.close()
        peer.close()

    def test_relay_forwards_control_frames_once_without_consuming_or_inventing_them(self):
        wire = (frame(b'a', final=False) + frame(b'ping', opcode=9)
                + frame(b'pong', opcode=10) + frame(b'b', opcode=0))
        ws, sock = self.reader(wire)
        seen = []
        def forward(opcode, payload):
            seen.append((opcode, payload))
            return True
        ws.on_control = forward
        self.assertEqual(ws.recv(), (1, b'ab'))
        self.assertEqual(seen, [(9, b'ping'), (10, b'pong')])
        self.assertEqual(bytes(sock.output), b'')
        for opcode, payload in seen:
            ws.send_control(opcode, payload)
        self.assertEqual(bytes(sock.output), frame(b'ping', opcode=9, masked=False)
                         + frame(b'pong', opcode=10, masked=False))
        for opcode, payload in ((1, b''), (9, b'x' * 126)):
            with self.assertRaises(ValueError):
                ws.send_control(opcode, payload)
        ws.close()

    def test_binary_is_byte_exact_in_both_directions_and_large_lengths(self):
        for size in (0, 125, 126, 65535, 65536, 200_000):
            payload = (b'\x00\xff\x80binary' * (size // 9 + 1))[:size]
            for client in (False, True):
                with self.subTest(size=size, client=client):
                    ws, sock = self.reader(frame(payload, opcode=2, masked=not client), is_client=client)
                    self.assertEqual(ws.recv(), (2, payload))
                    ws.send(payload, opcode=2)
                    peer, _ = self.reader(bytes(sock.output), is_client=not client)
                    self.assertEqual(peer.recv(), (2, payload))
                    ws.close()
                    peer.close()

    def test_protocol_errors_send_close_and_do_not_accept_bad_input(self):
        cases = [frame(b'x', masked=False), frame(b'x', rsv=64),
                 frame(b'x', opcode=3), frame(b'x', opcode=0),
                 frame(b'a', final=False) + frame(b'b'),
                 frame(b'x', opcode=9, final=False), frame(b'x' * 126, opcode=9),
                 frame(b'x', opcode=8), frame(struct.pack('!H', 1005), opcode=8),
                 frame(b'\xff'), frame(struct.pack('!H', 1000) + b'\xff', opcode=8),
                 b'\x81\xfe\x00\x01', b'\x81\xff' + struct.pack('!Q', 2**63),
                 b'\x81', frame(b'a', final=False)]
        for wire in cases:
            with self.subTest(wire=wire[:12]):
                ws, sock = self.reader(wire)
                with self.assertRaises(WebSocketError):
                    ws.recv()
                self.assertTrue(sock.closed)
                self.assertEqual(sock.output[0], 136)

    def test_client_rejects_masked_server_frames(self):
        ws, _ = self.reader(frame(b'bad'), is_client=True)
        with self.assertRaises(WebSocketError):
            ws.recv()

    def test_size_limit_checked_before_payload_read_and_across_fragments(self):
        for wire in (b'\x82\xff' + struct.pack('!Q', 60_000_000),
                     frame(b'1234', final=False) + frame(b'5678', opcode=0)):
            ws, sock = self.reader(wire, max_message=7)
            with self.assertRaisesRegex(WebSocketError, 'limit'):
                ws.recv()
            self.assertEqual(bytes(sock.output), frame(struct.pack('!H', 1009), opcode=8, masked=False))

    def test_native_requests_can_exceed_message_limit_without_unbounding_upstream_replies(self):
        raw=b'native-image-history-' * 3_100_000
        self.assertGreater(len(raw),64_000_000)
        compressed=zlib.compressobj(wbits=-15)
        payload=(compressed.compress(raw)+compressed.flush(zlib.Z_SYNC_FLUSH))[:-4]
        sock=RecordingSocket()
        receiver=WebSocket(sock,io.BytesIO(frame(payload,rsv=64)),max_message=None,
                           deflate=_deflate_configuration({},is_client=False))
        self.assertEqual(receiver.recv(),(1,raw))
        receiver.close()
        sock=RecordingSocket()
        upstream=WebSocket(sock,io.BytesIO(frame(b'reply-too-large',masked=False)),is_client=True,
            max_message=8,unbounded_send=True,deflate=_deflate_configuration({},is_client=True))
        upstream.send(raw)
        self.assertTrue(sock.output)
        with self.assertRaisesRegex(WebSocketError,'limit'):
            upstream.recv()

    def test_empty_close_is_echoed_and_eof_closes(self):
        for payload in (b'', struct.pack('!H', 1000) + b'bye'):
            ws, sock = self.reader(frame(payload, opcode=8))
            self.assertIsNone(ws.recv())
            self.assertEqual(bytes(sock.output), frame(payload, opcode=8, masked=False))
            self.assertIsNone(ws.recv())
        ws, sock = self.reader(b'')
        self.assertIsNone(ws.recv())
        self.assertTrue(sock.closed)

    def test_send_validation(self):
        ws, _ = self.reader(b'', max_message=4)
        with self.assertRaises(ValueError):
            ws.send(b'x', opcode=9)
        with self.assertRaises(UnicodeError):
            ws.send(b'\xff')
        with self.assertRaises(WebSocketError):
            ws.send(b'12345')
        with self.assertRaises(ValueError):
            ws.close(1005)
        with self.assertRaises(ValueError):
            ws.close(reason='x' * 124)
        ws.close()

    def test_parallel_writes_are_whole_messages(self):
        client_sock, server_sock = socket.socketpair()
        client = WebSocket(client_sock, is_client=True)
        server = WebSocket(server_sock)
        received, failures = [], []

        def receive():
            try:
                for _ in range(80):
                    received.append(server.recv())
            except Exception as exc:
                failures.append(exc)

        reader = threading.Thread(target=receive)
        reader.start()
        writers = [threading.Thread(target=lambda j=j: [client.send(f'{j}:{i}') for i in range(20)]) for j in range(4)]
        for writer in writers:
            writer.start()
        for writer in writers:
            writer.join(3)
            self.assertFalse(writer.is_alive())
        reader.join(3)
        self.assertFalse(reader.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(set(received), {(1, f'{j}:{i}'.encode()) for j in range(4) for i in range(20)})
        client.close()
        server.close()


class HeadersTests(unittest.TestCase):
    def test_strip_hop_headers_and_keep_duplicate_end_to_end_headers(self):
        headers = [('Connection', 'keep-alive, X-Secret'), ('X-Secret', 'private'),
                   ('Proxy-Authorization', 'not-forwarded'), ('Host', 'old'),
                   ('Content-Length', '2'), ('Authorization', 'test-key'),
                   ('Sec-WebSocket-Extensions', 'permessage-deflate'),
                   ('Set-Cookie', 'a=1'), ('Set-Cookie', 'b=2')]
        self.assertEqual(sanitize_headers(headers, websocket=True),
                         [('Authorization', 'test-key'), ('Set-Cookie', 'a=1'), ('Set-Cookie', 'b=2')])

    def test_header_injection_is_rejected(self):
        for pairs in ([('X-Foo\r\nBar', 'x')], [('X-Foo', 'ok\r\nAuthorization: no')],
                      [('X-Foo', 'nul\x00')], [('bad name', 'x')]):
            with self.assertRaises(ValueError):
                sanitize_headers(pairs)


class EchoHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    seen = []
    failures = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.seen.append((self.path, dict(self.headers)))
        if self.path == '/redirect':
            self.send_response(302)
            self.send_header('Location', 'http://127.0.0.1:1/secret')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if self.path == '/bad-accept':
            self.send_response(101)
            self.send_header('Upgrade', 'websocket')
            self.send_header('Connection', 'Upgrade')
            self.send_header('Sec-WebSocket-Accept', 'wrong')
            self.end_headers()
            return
        if self.path == '/extension':
            self.send_response(101)
            self.send_header('Upgrade', 'websocket')
            self.send_header('Connection', 'Upgrade')
            key = self.headers['Sec-WebSocket-Key']
            self.send_header('Sec-WebSocket-Accept', base64.b64encode(hashlib.sha1(
                (key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode())
            self.send_header('Sec-WebSocket-Extensions', 'unknown-compression')
            self.end_headers()
            return
        if self.path == '/no-extension':
            del self.headers['Sec-WebSocket-Extensions']
        try:
            ws = accept_websocket(self)
        except WebSocketError:
            self.send_error(400)
            return
        try:
            ws.send('ready')  # Eager bytes can share the TCP packet with the 101.
            while (message := ws.recv()) is not None:
                ws.send(message[1], message[0])
        except (OSError, WebSocketError) as exc:
            self.failures.append(type(exc).__name__)
        finally:
            ws.close()


class ConnectProxy(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def do_CONNECT(self):
        self.server.seen.append((self.path, dict(self.headers)))
        host, port = self.path.rsplit(':', 1)
        if host not in ('127.0.0.1', 'localhost'):
            self.send_error(403)
            return
        # Both allowed names identify this IPv4 fixture; DNS is not under test.
        upstream = socket.create_connection(('127.0.0.1', int(port)), timeout=2)
        self.send_response(200)
        self.end_headers()
        self.wfile.flush()
        try:
            while True:
                ready, _, _ = select.select([self.connection, upstream], [], [], 3)
                if not ready:
                    return
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    (upstream if source is self.connection else self.connection).sendall(data)
        except OSError:
            pass
        finally:
            upstream.close()
            self.close_connection = True


class TLSFixtureServer(http.server.ThreadingHTTPServer):
    def __init__(self, address, context):
        super().__init__(address, EchoHandler)
        self.daemon_threads = True
        # Configure TLS before serve_forever starts. Handshake in each worker,
        # so an intentionally rejected certificate never blocks the accept loop.
        self.socket = context.wrap_socket(self.socket, server_side=True,
                                          do_handshake_on_connect=False)

    def finish_request(self, request, client_address):
        request.settimeout(2)
        try:
            request.do_handshake()
        except ssl.SSLError:
            return
        super().finish_request(request, client_address)


class HandshakeTests(unittest.TestCase):
    def setUp(self):
        EchoHandler.seen, EchoHandler.failures = [], []
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), EchoHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        self.url = f'ws://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def test_local_upgrade_echo_and_header_boundary(self):
        ws, headers = connect_websocket(self.url + '/responses?model=gpt',
            {'Authorization': 'fake-unit-test-token', 'Host': 'attacker.invalid',
             'Proxy-Authorization': 'proxy-secret', 'Sec-WebSocket-Extensions': 'permessage-deflate'}, timeout=2)
        self.assertEqual(ws.recv(), (1, b'ready'))
        ws.send(b'\x00\xff\x80ResponsesLite', opcode=2)
        self.assertEqual(ws.recv(), (2, b'\x00\xff\x80ResponsesLite'))
        ws.send('{"type":"response.create"}')
        self.assertEqual(ws.recv(), (1, b'{"type":"response.create"}'))
        ws.close()
        path, sent = EchoHandler.seen[-1]
        self.assertEqual(path, '/responses?model=gpt')
        self.assertEqual(sent['Authorization'], 'fake-unit-test-token')
        self.assertTrue(sent['Host'].startswith('127.0.0.1:'))
        self.assertNotIn('Proxy-Authorization', sent)
        self.assertEqual(sent['Sec-WebSocket-Extensions'], 'permessage-deflate; client_max_window_bits')
        self.assertIsNotNone(ws.deflate)
        self.assertIn(('Upgrade', 'websocket'), headers)

    def test_deflate_negotiation_large_history_and_server_decline(self):
        payload = b'{"history":"' + b'long history ' * 200_000 + b'"}'
        ws, _ = connect_websocket(self.url + '/large', timeout=5)
        self.assertEqual(ws.recv(), (1, b'ready'))
        self.assertIsNotNone(ws.deflate)
        ws.send(payload)
        self.assertEqual(ws.recv(), (1, payload))
        ws.close()
        ws, _ = connect_websocket(self.url + '/no-extension', timeout=2)
        self.assertIsNone(ws.deflate)
        self.assertEqual(ws.recv(), (1, b'ready'))
        ws.send('uncompressed')
        self.assertEqual(ws.recv(), (1, b'uncompressed'))
        ws.close()

    def test_deflate_offer_and_response_validation(self):
        for value in ('unknown', 'permessage-deflate; nope',
                      'permessage-deflate; client_max_window_bits=8',
                      'permessage-deflate; server_no_context_takeover=1',
                      'permessage-deflate; server_max_window_bits="12',
                      'permessage-deflate; client_max_window_bits=12; client_max_window_bits=13',
                      'permessage-deflate, permessage-deflate'):
            with self.subTest(value=value), self.assertRaises(WebSocketError):
                _deflate_parameters(value)
        self.assertEqual(_accept_deflate('permessage-deflate; unknown'), (None, None))
        header, params = _accept_deflate(
            'unrecognized, permessage-deflate; server_max_window_bits=10; client_max_window_bits="12"')
        self.assertIn('server_max_window_bits=10', header)
        self.assertEqual(params, {'incoming_bits': 12, 'outgoing_bits': 10,
                                 'incoming_reset': True, 'outgoing_reset': True})
        self.assertNotIn('client_max_window_bits', _accept_deflate('permessage-deflate')[0])

    def test_websocket_owns_upgraded_reader_without_http_response_double_close(self):
        unraisable = []
        with patch('sys.unraisablehook', side_effect=unraisable.append):
            ws, _ = connect_websocket(self.url + '/ownership', timeout=2)
            self.assertEqual(ws.recv(), (1, b'ready'))
            ws.close()
            del ws
            gc.collect()
        self.assertEqual(unraisable, [])

    def test_redirects_bad_accept_and_unsolicited_extension_fail(self):
        for path in ('/redirect', '/bad-accept', '/extension'):
            with self.subTest(path=path), self.assertRaises(WebSocketError) as ctx:
                connect_websocket(self.url + path, {'Authorization': 'fake-secret'}, timeout=2)
            self.assertNotIn('fake-secret', str(ctx.exception))
        self.assertEqual(len(EchoHandler.seen), 3)

    def test_bad_client_handshake_is_rejected_without_upgrade(self):
        import http.client
        for key, version in [('bad-key', '13'), (base64.b64encode(b'16 bytes exactly').decode(), '12')]:
            conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
            conn.request('GET', '/', headers={'Connection': 'Upgrade', 'Upgrade': 'websocket',
                         'Sec-WebSocket-Key': key, 'Sec-WebSocket-Version': version})
            self.assertEqual(conn.getresponse().status, 400)
            conn.close()

    def test_url_and_insecure_context_rejected_before_connect(self):
        for url in ('http://localhost/', 'ws://name:secret@localhost/', 'ws://localhost/#fragment',
                    'ws://localhost/\r\nFoo:evil'):
            with self.assertRaises(ValueError):
                connect_websocket(url)
        with self.assertRaises(ValueError):
            connect_websocket(self.url, ssl_context=ssl._create_unverified_context())

    def proxy(self):
        proxy = http.server.ThreadingHTTPServer(('127.0.0.1', 0), ConnectProxy)
        proxy.daemon_threads, proxy.seen = True, []
        thread = threading.Thread(target=proxy.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        return proxy

    def test_explicit_connect_proxy_keeps_its_auth_off_the_origin(self):
        proxy = self.proxy()
        ws, _ = connect_websocket(self.url + '/proxy', {'Authorization': 'Bearer origin-fixture'},
            proxy_url=f'http://proxy-user:proxy-password@127.0.0.1:{proxy.server_port}', timeout=2)
        self.assertEqual(ws.recv(), (1, b'ready'))
        ws.send(b'\x00\xffproxy-transparent', opcode=2)
        self.assertEqual(ws.recv(), (2, b'\x00\xffproxy-transparent'))
        ws.close()
        self.assertEqual(proxy.seen[0][0], f'127.0.0.1:{self.server.server_port}')
        self.assertEqual(proxy.seen[0][1]['Proxy-Authorization'],
                         'Basic ' + base64.b64encode(b'proxy-user:proxy-password').decode())
        origin_headers = EchoHandler.seen[-1][1]
        self.assertEqual(origin_headers['Authorization'], 'Bearer origin-fixture')
        self.assertNotIn('Proxy-Authorization', origin_headers)

    @unittest.skipUnless(shutil.which('openssl'), 'openssl is only needed to generate a temporary localhost test certificate')
    def test_wss_uses_explicit_local_trust_and_default_rejects_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert, key = Path(tmp) / 'cert.pem', Path(tmp) / 'key.pem'
            config = Path(tmp) / 'cert.conf'
            config.write_text('[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n'
                              '[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost\n')
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                            '-config', str(config), '-keyout', str(key), '-out', str(cert)],
                           check=True, capture_output=True, **background_options())
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(2)
            self.server = TLSFixtureServer(('127.0.0.1', 0), context)
            self.thread = threading.Thread(target=self.server.serve_forever,
                kwargs={'poll_interval': .01}, daemon=True)
            self.thread.start()
            url = f'wss://localhost:{self.server.server_port}/tls'
            with self.assertRaises(ssl.SSLCertVerificationError):
                connect_websocket(url, timeout=2)
            trusted = ssl.create_default_context(cafile=str(cert))
            with self.assertRaises(ssl.SSLCertVerificationError):
                connect_websocket(f'wss://127.0.0.1:{self.server.server_port}/tls', timeout=2, ssl_context=trusted)
            ws, _ = connect_websocket(url, timeout=2, ssl_context=trusted)
            self.assertEqual(ws.recv(), (1, b'ready'))
            ws.send('verified')
            self.assertEqual(ws.recv(), (1, b'verified'))
            ws.close()
            proxy = self.proxy()
            proxy_url = f'http://127.0.0.1:{proxy.server_port}'
            ws, _ = connect_websocket(url, timeout=2, ssl_context=trusted, proxy_url=proxy_url)
            self.assertEqual(ws.recv(), (1, b'ready'))
            ws.close()
            with self.assertRaises(ssl.SSLCertVerificationError):
                connect_websocket(url, timeout=2, proxy_url=proxy_url)
            connection = create_http_connection(url.replace('wss:', 'https:'), timeout=2,
                ssl_context=trusted, proxy_url=proxy_url)
            connection.request('GET', '/plain-http-test', headers={'Authorization': 'Bearer origin-only'})
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
            connection.close()
            self.assertNotIn('Proxy-Authorization', EchoHandler.seen[-1][1])


if __name__ == '__main__':
    unittest.main()
