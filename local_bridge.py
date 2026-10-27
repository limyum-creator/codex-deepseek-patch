"""Idle-exiting adaptation for native tools and strict message templates."""
from pathlib import Path
import argparse
import hmac
import http.server
import json
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise ValueError('Redirects are disabled for local model credentials')


class Server(http.server.ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=True

    def server_bind(self):
        # A numeric loopback listener needs no reverse DNS. HTTPServer's default
        # getfqdn() can block startup on machines with a slow local resolver.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*args):pass

    def authorized(self):
        return hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+self.server.profile['token'])

    def reply(self,status,body):
        self.send_response(status);self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)

    def do_GET(self):
        if not self.authorized():self.reply(401,b'{"error":"unauthorized"}');return
        if self.path=='/health':self.reply(200,b'{"ok":true}');return
        self.forward(None)

    def do_POST(self):
        if not self.authorized():self.reply(401,b'{"error":"unauthorized"}');return
        length=int(self.headers.get('Content-Length','0'))
        if not 0<length<=64_000_000:self.reply(413,b'{"error":"request too large"}');return
        self.forward(self.rfile.read(length))

    def forward(self,raw):
        if self.path not in ('/v1/responses','/v1/models'):
            self.reply(404,b'{"error":"unsupported local endpoint"}');return
        profile=self.server.profile
        adapter=None
        if raw is not None:
            try:body=json.loads(raw)
            except ValueError:self.reply(400,b'{"error":"invalid JSON"}');return
            if body.get('model') in profile.get('chat_only_models',[]):
                for key in ('tools','tool_choice','parallel_tool_calls'):body.pop(key,None)
            if body.get('model') in profile.get('normalize_instructions_models',[]) and isinstance(body.get('input'),list):
                instructions=[body['instructions']] if body.get('instructions') else []
                messages=[]
                for item in body['input']:
                    if item.get('role') in ('system','developer'):
                        content=item.get('content','')
                        instructions.append(content if isinstance(content,str) else '\n'.join(x.get('text','') for x in content))
                    else:messages.append(item)
                body['instructions']='\n\n'.join(instructions);body['input']=messages
            if profile.get('native_tools') or body.get('model') in profile.get('tool_adapter_models',[]):
                from responses_compat import ToolAdapter
                adapter=ToolAdapter(body,flatten_namespaces=profile.get('flatten_namespaces',
                    body.get('model') in profile.get('tool_adapter_models',[])));body=adapter.body
            raw=json.dumps(body,ensure_ascii=False).encode('utf-8')
        headers={'Content-Type':'application/json'}
        if profile.get('key_file'):
            headers['Authorization']='Bearer '+Path(profile['key_file']).read_text(encoding='utf-8').strip()
        elif profile.get('proxy_config'):
            from proxy_support import read_api_key
            headers['Authorization']='Bearer '+read_api_key(profile['proxy_config'])
        url=profile['base_url'].rstrip('/')+self.path.removeprefix('/v1')
        self.server.active+=1
        try:
            req=urllib.request.Request(url,data=raw,headers=headers,method='POST' if raw is not None else 'GET')
            with urllib.request.build_opener(NoRedirect).open(req,timeout=600) as response:
                self.send_response(response.status)
                self.send_header('Content-Type',response.headers.get('Content-Type','application/json'))
                self.send_header('Connection','close');self.end_headers();self.close_connection=True
                if adapter and 'text/event-stream' in response.headers.get('Content-Type',''):
                    for chunk in adapter.stream(response):
                        self.wfile.write(chunk);self.wfile.flush()
                elif adapter:
                    self.wfile.write(json.dumps(adapter.response(json.load(response)),ensure_ascii=False).encode())
                else:
                    while True:
                        chunk=response.read1(16384)
                        if not chunk:break
                        self.wfile.write(chunk);self.wfile.flush()
        except urllib.error.HTTPError as error:
            self.reply(error.code,error.read(1000000))
        except (BrokenPipeError,ConnectionResetError):pass
        except Exception:
            if not self.close_connection:self.reply(502,b'{"error":"Local model service is unavailable"}')
        finally:
            self.server.active-=1;self.server.last_activity=time.monotonic()


def healthy(profile):
    request=urllib.request.Request('http://127.0.0.1:'+str(profile['port'])+'/health',
                                   headers={'Authorization':'Bearer '+profile['token']})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=.5) as r:return r.status==200
    except Exception:return False


def credential(profile_path):
    profile=json.loads(profile_path.read_text(encoding='utf-8'))
    if not healthy(profile):
        options={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}
        # Desktop auth helpers may read the profile without permission to write
        # beside it. Starting a connection must not depend on an optional log.
        process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'serve',str(profile_path)],
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**options)
        for _ in range(50):
            if healthy(profile):break
            if process.poll() is not None:
                # Another request may have won the simultaneous start.
                if healthy(profile):break
                raise RuntimeError('Local compatibility listener could not start')
            time.sleep(.1)
        else:raise RuntimeError('Local compatibility listener did not become ready')
    print(profile['token'],end='')


def serve(profile_path):
    profile=json.loads(profile_path.read_text(encoding='utf-8'))
    server=Server(('127.0.0.1',profile['port']),Handler)
    server.profile=profile;server.active=0;server.last_activity=time.monotonic()
    def idle():
        while True:
            time.sleep(5)
            if server.active==0 and time.monotonic()-server.last_activity>=120:
                server.shutdown();return
    threading.Thread(target=idle,daemon=True).start()
    try:server.serve_forever()
    finally:server.server_close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['credential-get','serve']);parser.add_argument('profile',type=Path)
    args=parser.parse_args()
    try:(credential if args.action=='credential-get' else serve)(args.profile)
    except Exception as error:print(type(error).__name__+': local compatibility connection failed',file=sys.stderr);raise SystemExit(1)
