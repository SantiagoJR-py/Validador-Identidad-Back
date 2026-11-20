"""Demo host application. Host key never sent to browser.

Run: cd repository && python examples/host_server.py
"""
import html
import json
import os
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.parse import urlparse, parse_qs

IDS_BASE = os.getenv('IDS_BASE_URL', 'http://localhost:8000')
IDS_INTERNAL_BASE = os.getenv('IDS_INTERNAL_BASE_URL', IDS_BASE)
HOST_KEY = os.getenv('IDS_HOST_API_KEY', '')
PUBLIC_ORIGIN = os.getenv('IDS_DEMO_ORIGIN', 'http://localhost:8080')
TLS_CONTEXT = ssl.create_default_context(cafile=os.getenv('IDS_TLS_CA_FILE'))


def api(path, body=None):
    payload = json.dumps(body).encode() if body is not None else None
    req = Request(IDS_INTERNAL_BASE + path, data=payload, headers={'X-API-Key': HOST_KEY,'Content-Type':'application/json'}, method='POST' if body is not None else 'GET')
    with urlopen(req, timeout=20, context=TLS_CONTEXT) as response:
        return json.load(response)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path)
        if path.path != '/': return self.send_error(404)
        try: session=api('/api/v1/host/sessions', {'parent_origin': PUBLIC_ORIGIN, 'external_reference':'demo'})
        except Exception: return self.send_error(502, 'Servicio de captura no disponible')
        sid = html.escape(session['session_id'])
        src = html.escape(IDS_BASE + session['embed_path'],quote=True)
        origin = json.dumps(IDS_BASE)
        html_body=f'''<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"><title>Aplicación anfitriona IDS</title>
        <style>*{{box-sizing:border-box}}html,body{{margin:0;background:#f3f5fb;font:14px system-ui,sans-serif;color:#17233b}}
        .wrap{{max-width:760px;margin:0 auto;padding:16px;display:flex;flex-direction:column;min-height:100dvh}}
        h1{{font-size:20px;margin:0 0 4px}}#status{{font-weight:600;color:#294267;margin:0 0 12px}}
        iframe{{display:block;border:1px solid #bcc7dc;border-radius:12px;width:100%;flex:1;min-height:640px;background:#f4f7fb}}
        @media(max-width:600px){{.wrap{{padding:0}}header{{padding:10px 14px 6px calc(14px + env(safe-area-inset-left))}}h1{{font-size:16px}}#status{{margin:0;font-size:13px}}iframe{{border:0;border-radius:0;min-height:0;height:calc(100dvh - 62px)}}}}
        @media(max-height:500px) and (orientation:landscape){{header{{display:none}}.wrap{{padding:0}}iframe{{border:0;border-radius:0;min-height:0;height:100dvh}}}}</style></head><body>
        <div class="wrap"><header><h1>Aplicación anfitriona</h1><p id="status">Esperando captura del documento…</p></header>
        <iframe src="{src}" title="Escáner de cédula" allow="camera"></iframe></div>
        <script>const SID={json.dumps(sid)}, IDS_ORIGIN={origin};
        const LABELS={{IDS_READY:'Listo para capturar.',IDS_CAPTURE_STARTED:'Capturando documento…',IDS_SIDE_PROCESSED:'Lado procesado.',IDS_ERROR:'Reintentando captura…',IDS_COMPLETE:'Captura completada.'}};
        window.addEventListener('message',e=>{{
          if(e.origin!==IDS_ORIGIN || e.source!==document.querySelector('iframe').contentWindow || e.data?.source!=='IDS' || e.data.sessionId!==SID)return;
          // Completion payload contains sensitive data: send it to your backend, never render or log it.
          if(LABELS[e.data.type])document.getElementById('status').textContent=LABELS[e.data.type];
        }});</script></body></html>'''
        data=html_body.encode();self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(data)


if __name__ == '__main__':
    bind_host = os.getenv('IDS_DEMO_BIND_HOST', '127.0.0.1')
    bind_port = int(os.getenv('IDS_DEMO_BIND_PORT', '8080'))
    certfile = os.getenv('IDS_TLS_CERTFILE')
    keyfile = os.getenv('IDS_TLS_KEYFILE')
    if bool(certfile) != bool(keyfile):
        raise SystemExit('Set both IDS_TLS_CERTFILE and IDS_TLS_KEYFILE for HTTPS.')
    server = ThreadingHTTPServer((bind_host, bind_port), Handler)
    if certfile:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile, keyfile)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    print(f'Host demo: {PUBLIC_ORIGIN}', flush=True)
    server.serve_forever()
