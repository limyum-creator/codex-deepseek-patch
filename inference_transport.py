"""Small direct HTTP/WebSocket transport for the local inference relay.

No proxy discovery, redirects, credentials in errors, or third party runtime
dependencies. WebSocket compression uses stdlib zlib and RFC 7692 negotiation.
A WebSocket has one reader; writes are safe across threads.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import http.client
import os
import re
import socket
import ssl
import struct
import subprocess
import threading
import time
import zlib
from urllib.parse import unquote, urlsplit


_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
        'te', 'trailer', 'transfer-encoding', 'upgrade', 'host', 'content-length'}


def zstd_body(data, *, decompress=True, node=None, cancelled=None):
    """Use stdlib zstd, or the untouched Codex-bundled Node codec on older Python.

    The short-lived codec has no network, credentials, files or model policy.
    Bytes travel over pipes; Windows never creates a console. No pip dependency.
    """
    try:
        from compression import zstd
    except ImportError:
        zstd = None
    if cancelled and cancelled.is_set():
        raise OSError('Request cancelled')
    if zstd is not None:
        try:
            return zstd.decompress(data) if decompress else zstd.compress(data, level=3)
        except zstd.ZstdError:
            raise ValueError('Invalid native compressed request') from None
    if not node:
        raise ValueError('The native request compression codec is unavailable')
    script = ('const fs=require("node:fs"),z=require("node:zlib");'
              'try{const b=fs.readFileSync(0);process.stdout.write(z.' +
              ('zstdDecompressSync' if decompress else 'zstdCompressSync') +
              '(b));}catch(e){process.exitCode=1;}')
    env = {k:v for k,v in os.environ.items() if k not in ('NODE_OPTIONS', 'NODE_PATH')}
    process = subprocess.Popen([str(node), '-e', script], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
        **({'creationflags':0x08000000} if os.name == 'nt' else {}))
    # One communicate() owns both pipes. On Python 3.11, retrying it with
    # input=None after a short timeout can leave unsent input unregistered.
    # A lightweight waiter keeps cancellation responsive without restarting I/O.
    finished=threading.Event();result=[];errors=[]
    def exchange():
        try:result.append(process.communicate(data))
        except BaseException as error:errors.append(error)
        finally:finished.set()
    thread=threading.Thread(target=exchange,daemon=True);thread.start()
    until=time.monotonic()+120
    try:
        while not finished.wait(.1):
            if cancelled and cancelled.is_set():raise OSError('Request cancelled')
            if time.monotonic()>=until:raise TimeoutError('Native request compression timed out')
        if errors:raise errors[0]
        if process.returncode:raise ValueError('Invalid native compressed request or unavailable codec')
        return result[0][0]
    finally:
        if process.poll() is None:process.kill()
        thread.join()



class WebSocketError(ConnectionError):
    """A malformed or unsupported WebSocket exchange."""


def _pairs(headers):
    return list(headers.items()) if hasattr(headers, 'items') else list(headers or ())


def _checked_headers(headers):
    result = []
    for name, value in _pairs(headers):
        if not isinstance(name, str) or not _TOKEN.fullmatch(name):
            raise ValueError('Invalid HTTP header name')
        if not isinstance(value, str) or any(ord(c) < 32 and c != '\t' or ord(c) == 127 for c in value):
            raise ValueError('Invalid HTTP header value')
        # http.client uses ISO-8859-1; reject unusable values before connecting.
        value.encode('latin-1')
        result.append((name, value))
    return result


def sanitize_headers(headers, *, websocket=False, request=True):
    """Return safe end-to-end header pairs, preserving duplicate response headers.

    Recompute Host/Content-Length at the destination. Proxy credentials never
    leave this boundary. WebSocket handshakes are generated independently.
    ``request`` documents caller intent; the same hop rules apply both ways.
    """
    pairs = _checked_headers(headers)
    drop = _HOP | {token.strip().lower() for name, value in pairs
                   if name.lower() == 'connection' for token in value.split(',')}
    return [(name, value) for name, value in pairs if name.lower() not in drop
            and not (websocket and name.lower().startswith('sec-websocket-'))]


def _tokens(value):
    return {s.strip().lower() for s in value.split(',')}


def _accept(key):
    return base64.b64encode(hashlib.sha1((key + _GUID).encode('ascii')).digest()).decode('ascii')


def _valid_close(code):
    return code in {1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014} or 3000 <= code <= 4999


def _deflate_parameters(value, *, offer=False):
    parts = [part.strip() for part in value.split(';')]
    if not parts or parts[0].lower() != 'permessage-deflate':
        raise WebSocketError('Unsupported WebSocket extension')
    parameters = {}
    for part in parts[1:]:
        key, equal, raw = part.partition('=')
        key, raw = key.strip().lower(), raw.strip()
        if raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1]
        if key in parameters:
            raise WebSocketError('Duplicate WebSocket extension parameter')
        if key in ('server_no_context_takeover', 'client_no_context_takeover'):
            if equal:
                raise WebSocketError('Invalid WebSocket context takeover parameter')
            parameters[key] = True
        elif key in ('server_max_window_bits', 'client_max_window_bits'):
            if not equal and offer and key == 'client_max_window_bits':
                parameters[key] = 15
            elif re.fullmatch(r'(?:9|1[0-5])', raw):
                parameters[key] = int(raw)
            else:
                raise WebSocketError('Unsupported WebSocket compression window')
        else:
            raise WebSocketError('Unsupported WebSocket extension parameter')
    return parameters


def _deflate_configuration(parameters, *, is_client):
    incoming, outgoing = ('server', 'client') if is_client else ('client', 'server')
    return {'incoming_bits': parameters.get(incoming + '_max_window_bits', 15),
            'outgoing_bits': parameters.get(outgoing + '_max_window_bits', 15),
            'incoming_reset': bool(parameters.get(incoming + '_no_context_takeover')),
            'outgoing_reset': bool(parameters.get(outgoing + '_no_context_takeover'))}


def _accept_deflate(value):
    for offer in value.split(','):
        if offer.strip().split(';', 1)[0].lower() != 'permessage-deflate':
            continue
        try:
            params = _deflate_parameters(offer, offer=True)
        except WebSocketError:
            continue  # Decline unsupported offers; never guess a codec.
        chosen = {'server_no_context_takeover': True, 'client_no_context_takeover': True}
        for key in ('server_max_window_bits', 'client_max_window_bits'):
            if key in params:
                chosen[key] = params[key]
        header = 'permessage-deflate; server_no_context_takeover; client_no_context_takeover'
        header += ''.join('; ' + key + '=' + str(value) for key, value in chosen.items() if type(value) is int)
        return header, _deflate_configuration(chosen, is_client=False)
    return None, None


class WebSocket:
    def __init__(self, sock, reader=None, *, is_client=False, max_message=64_000_000, deflate=None,
                 unbounded_send=False):
        if max_message is not None and (not isinstance(max_message, int) or max_message < 1):
            raise ValueError('max_message must be positive')
        self.sock = sock
        self.reader = reader if reader is not None else sock.makefile('rb')
        self.is_client = is_client
        self.max_message = max_message
        self.unbounded_send = unbounded_send
        self._write_lock = threading.Lock()
        self._message_lock = threading.Lock()
        self._closed = False
        self._close_sent = False
        self.on_control = None
        self.on_close = None
        self.deflate = deflate
        self._compressor = self._decompressor = None

    def _read(self, size, *, eof_ok=False):
        chunks = bytearray()
        while len(chunks) < size:
            chunk = self.reader.read(size - len(chunks))
            if not chunk:
                if not chunks and eof_ok:
                    return None
                raise WebSocketError('Truncated WebSocket frame')
            chunks.extend(chunk)
        return bytes(chunks)

    def _frame(self, remaining, compressed_message=False):
        header = self._read(2, eof_ok=True)
        if header is None:
            return None
        first, second = header
        final, opcode = bool(first & 128), first & 15
        masked, length = bool(second & 128), second & 127
        compressed = bool(first & 64)
        if (first & 48 or opcode not in {0, 1, 2, 8, 9, 10}
                or compressed and (not self.deflate or opcode not in (1, 2))):
            raise WebSocketError('Unsupported WebSocket frame flags or opcode')
        if masked == self.is_client:
            raise WebSocketError('Invalid WebSocket masking direction')
        if length == 126:
            length = struct.unpack('!H', self._read(2))[0]
            if length < 126:
                raise WebSocketError('Nonminimal WebSocket length')
        elif length == 127:
            length = struct.unpack('!Q', self._read(8))[0]
            if length < 65536 or length >> 63:
                raise WebSocketError('Invalid WebSocket length')
        if opcode >= 8:
            if not final or length > 125:
                raise WebSocketError('Invalid WebSocket control frame')
        elif self.max_message is not None and length > (self.max_message + self.max_message // 1000 + 64
                       if compressed or compressed_message else remaining):
            raise _MessageTooLarge('WebSocket message exceeds limit')
        key = self._read(4) if masked else None
        payload = self._read(length)
        if key:
            payload = bytes(value ^ key[i % 4] for i, value in enumerate(payload))
        return final, opcode, payload, compressed

    def _send_frame(self, payload, opcode, *, compressed=False, final=True):
        with self._write_lock:
            if self._closed or self._close_sent and opcode != 8:
                raise WebSocketError('WebSocket is closed')
            length = len(payload)
            first = (128 if final else 0) | opcode | (64 if compressed else 0)
            header = bytes([first, (128 if self.is_client else 0) | min(length, 126)])
            if length >= 65536:
                header = bytes([first, (128 if self.is_client else 0) | 127]) + struct.pack('!Q', length)
            elif length >= 126:
                header += struct.pack('!H', length)
            if self.is_client:
                key = os.urandom(4)
                header += key
                payload = bytes(value ^ key[i % 4] for i, value in enumerate(payload))
            self.sock.sendall(header + payload)
            if opcode == 8:
                self._close_sent = True

    def send(self, payload, opcode=1):
        """Send byte-exact text (1) or binary (2), with negotiated wire compression."""
        if opcode not in (1, 2):
            raise ValueError('send requires text or binary opcode')
        if isinstance(payload, str):
            payload = payload.encode('utf-8')
        else:
            payload = bytes(payload)
        if not self.unbounded_send and self.max_message is not None and len(payload) > self.max_message:
            raise _MessageTooLarge('WebSocket message exceeds limit')
        if opcode == 1:
            payload.decode('utf-8')
        # Serialize data messages while allowing observed control frames between
        # fragments. A large image history must not block pong until upload ends.
        with self._message_lock:
            if self.deflate:
                if self._compressor is None or self.deflate['outgoing_reset']:
                    self._compressor = zlib.compressobj(wbits=-self.deflate['outgoing_bits'])
                payload = self._compressor.compress(payload) + self._compressor.flush(zlib.Z_SYNC_FLUSH)
                if not payload.endswith(b'\x00\x00\xff\xff'):
                    raise WebSocketError('Invalid WebSocket deflate output')
                payload = payload[:-4]
            for start in range(0, max(1, len(payload)), 65536):
                self._send_frame(payload[start:start + 65536], opcode if start == 0 else 0,
                                 compressed=bool(self.deflate) and start == 0,
                                 final=start + 65536 >= len(payload))

    def send_control(self, opcode, payload=b''):
        """Forward an observed heartbeat; never synthesize model progress."""
        payload = bytes(payload)
        if opcode not in (9, 10) or len(payload) > 125:
            raise ValueError('Invalid WebSocket heartbeat')
        self._send_frame(payload, opcode)

    def recv(self):
        """Read a complete message as (opcode, bytes), or None after peer close.

        Control frames are handled here; continuation frames are reassembled.
        Invalid input closes the connection and raises WebSocketError.
        """
        if self._closed:
            return None
        opcode = None
        chunks = bytearray()
        compressed_message = False
        try:
            while True:
                frame = self._frame(self.max_message - len(chunks) if self.max_message is not None else None,
                                    compressed_message)
                if frame is None:
                    if opcode is not None:
                        raise WebSocketError('Truncated fragmented WebSocket message')
                    self._shutdown()
                    return None
                final, kind, payload, compressed = frame
                if kind in (9, 10):
                    forwarded = self.on_control and self.on_control(kind, payload)
                    if kind == 9 and not forwarded:
                        self._send_frame(payload, 10)
                    continue
                if kind == 8:
                    if len(payload) == 1:
                        raise WebSocketError('Invalid WebSocket close payload')
                    if payload:
                        if not _valid_close(struct.unpack('!H', payload[:2])[0]):
                            raise WebSocketError('Invalid WebSocket close code')
                        payload[2:].decode('utf-8')
                    if self.on_close:
                        self.on_close(struct.unpack('!H', payload[:2])[0] if payload else 1000,
                                      payload[2:].decode('utf-8') if payload else '')
                    if not self._close_sent:
                        self._send_frame(payload, 8)
                    self._shutdown()
                    return None
                if kind == 0:
                    if opcode is None:
                        raise WebSocketError('Unexpected WebSocket continuation')
                elif opcode is not None:
                    raise WebSocketError('Interleaved WebSocket data messages')
                else:
                    opcode = kind
                    compressed_message = compressed
                    if compressed_message and (self._decompressor is None or self.deflate['incoming_reset']):
                        self._decompressor = zlib.decompressobj(-self.deflate['incoming_bits'])
                if compressed_message:
                    payload = self._decompressor.decompress(payload + (b'\x00\x00\xff\xff' if final else b''),
                        self.max_message - len(chunks) + 1 if self.max_message is not None else 0)
                    if (self.max_message is not None and len(payload) > self.max_message - len(chunks)
                            or self._decompressor.unconsumed_tail or self._decompressor.unused_data):
                        raise _MessageTooLarge('WebSocket decompressed message exceeds limit')
                chunks.extend(payload)
                if final:
                    result = bytes(chunks)
                    if opcode == 1:
                        result.decode('utf-8')
                    return opcode, result
        except (WebSocketError, UnicodeError, zlib.error) as exc:
            self.close(1009 if isinstance(exc, _MessageTooLarge) else 1007 if isinstance(exc, UnicodeError) else 1002)
            if isinstance(exc, UnicodeError):
                raise WebSocketError('Invalid WebSocket UTF-8') from None
            if isinstance(exc, zlib.error):
                raise WebSocketError('Invalid WebSocket compressed payload') from None
            raise
        except OSError:
            self._shutdown()
            raise

    def _shutdown(self):
        self._closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        self.reader.close()

    def close(self, code=1000, reason=''):
        if not _valid_close(code):
            raise ValueError('Invalid WebSocket close code')
        payload = struct.pack('!H', code) + reason.encode('utf-8')
        if len(payload) > 125:
            raise ValueError('WebSocket close reason is too long')
        if self._closed:
            return
        try:
            if not self._close_sent:
                self._send_frame(payload, 8)
        except OSError:
            pass
        finally:
            self._shutdown()


class _MessageTooLarge(WebSocketError):
    pass


def accept_websocket(handler, *, max_message=64_000_000, response_headers=None):
    """Upgrade an authenticated BaseHTTPRequestHandler. Caller checks Origin/auth.

    Invalid upgrades raise before writing a response, so the caller can return
    its own HTTP error. Existing buffered request bytes stay on handler.rfile.
    """
    headers = handler.headers
    key = headers.get('Sec-WebSocket-Key', '')
    try:
        valid_key = len(base64.b64decode(key, validate=True)) == 16
    except (ValueError, binascii.Error):
        valid_key = False
    if (handler.command != 'GET' or handler.request_version != 'HTTP/1.1'
            or _tokens(headers.get('Upgrade', '')) != {'websocket'}
            or 'upgrade' not in _tokens(headers.get('Connection', ''))
            or headers.get('Sec-WebSocket-Version') != '13' or not valid_key
            or any(len(headers.get_all(name, [])) != 1 for name in
                   ('Sec-WebSocket-Key', 'Sec-WebSocket-Version', 'Upgrade'))
            or headers.get('Transfer-Encoding') or headers.get('Content-Length', '0') != '0'):
        raise WebSocketError('Invalid WebSocket upgrade request')
    handler.send_response_only(101, 'Switching Protocols')
    handler.send_header('Upgrade', 'websocket')
    handler.send_header('Connection', 'Upgrade')
    handler.send_header('Sec-WebSocket-Accept', _accept(key))
    extension, deflate = _accept_deflate(','.join(headers.get_all('Sec-WebSocket-Extensions', [])))
    if extension:
        handler.send_header('Sec-WebSocket-Extensions', extension)
    for name, value in sanitize_headers(response_headers, websocket=True, request=False):
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.flush()
    handler.close_connection = True
    return WebSocket(handler.connection, handler.rfile, max_message=max_message, deflate=deflate)


def create_http_connection(url, *, timeout=60, proxy_url=None, ssl_context=None):
    """Create a direct or explicitly HTTP-CONNECT-tunnelled HTTP(S) connection.

    A proxy URL may carry proxy credentials. They appear only on CONNECT, never
    on the request to the origin. No environment proxy discovery happens here.
    """
    parsed = urlsplit(url)
    secure = parsed.scheme in ('https', 'wss')
    if ssl_context is not None and (not ssl_context.check_hostname or ssl_context.verify_mode != ssl.CERT_REQUIRED):
        raise ValueError('TLS requires hostname and certificate verification')
    cls = http.client.HTTPSConnection if secure else http.client.HTTPConnection
    kwargs = {'timeout': timeout}
    if secure:
        kwargs['context'] = ssl_context or ssl.create_default_context()
    host, port = parsed.hostname, parsed.port or (443 if secure else 80)
    tunnel_headers = {}
    if proxy_url:
        proxy = urlsplit(proxy_url)
        if (proxy.scheme != 'http' or not proxy.hostname or proxy.path not in ('', '/')
                or proxy.query or proxy.fragment or any(ord(c) < 33 for c in proxy_url)):
            raise ValueError('An HTTP CONNECT proxy URL is required')
        if proxy.username is not None:
            credentials = unquote(proxy.username) + ':' + unquote(proxy.password or '')
            if any(ord(c) < 32 or ord(c) == 127 for c in credentials):
                raise ValueError('Invalid proxy credentials')
            tunnel_headers['Proxy-Authorization'] = 'Basic ' + base64.b64encode(credentials.encode()).decode('ascii')
        connection = cls(proxy.hostname, proxy.port or 80, **kwargs)
        connection.set_tunnel(host, port, headers=tunnel_headers)
    else:
        connection = cls(host, port, **kwargs)
    return connection


def connect_websocket(url, headers=None, *, timeout=60, max_message=64_000_000, ssl_context=None, proxy_url=None,
                      unbounded_send=False):
    """Connect directly to WS/WSS; return (WebSocket, response header pairs).

    TLS verifies hostname and trust by default. A custom context is useful for
    an explicitly trusted local test certificate. Redirects are never followed.
    """
    parsed = urlsplit(url)
    if (parsed.scheme not in ('ws', 'wss') or not parsed.hostname or parsed.username
            or parsed.password or parsed.fragment or any(ord(c) < 33 for c in url)):
        raise ValueError('Invalid WebSocket URL')
    pairs = sanitize_headers(headers, websocket=True)
    key = base64.b64encode(os.urandom(16)).decode('ascii')
    target = parsed.path or '/'
    if parsed.query:
        target += '?' + parsed.query
    connection = create_http_connection(url, timeout=timeout, proxy_url=proxy_url, ssl_context=ssl_context)
    response = None
    try:
        connection.putrequest('GET', target, skip_accept_encoding=True)
        for name, value in pairs:
            connection.putheader(name, value)
        for name, value in [('Upgrade', 'websocket'), ('Connection', 'Upgrade'),
                            ('Sec-WebSocket-Key', key), ('Sec-WebSocket-Version', '13'),
                            ('Sec-WebSocket-Extensions', 'permessage-deflate; client_max_window_bits')]:
            connection.putheader(name, value)
        connection.endheaders()
        # Keep HTTPResponse's buffered reader: it may already contain WS bytes.
        response = http.client.HTTPResponse(connection.sock)
        response.begin()
        result_headers = response.getheaders()
        if response.status != 101:
            raise WebSocketError(f'WebSocket upgrade rejected (HTTP {response.status})')
        if (_tokens(response.getheader('Upgrade', '')) != {'websocket'}
                or 'upgrade' not in _tokens(response.getheader('Connection', ''))
                or response.getheader('Sec-WebSocket-Accept') != _accept(key)
                or response.getheader('Sec-WebSocket-Protocol')):
            raise WebSocketError('Invalid WebSocket upgrade response')
        extension = response.getheader('Sec-WebSocket-Extensions')
        deflate = (_deflate_configuration(_deflate_parameters(extension), is_client=True)
                   if extension else None)
        ws = WebSocket(connection.sock, response.fp, is_client=True,
                       max_message=max_message, deflate=deflate, unbounded_send=unbounded_send)
        # Ownership moves to WebSocket. HTTPResponse.close() otherwise flushes
        # this reader again during GC after WS shutdown (an error on Python 3.13).
        response.fp = None
        response.close()
        ws._http_connection = connection
        return ws, result_headers
    except BaseException:
        connection.close()
        if response is not None:
            response.close()
        raise
