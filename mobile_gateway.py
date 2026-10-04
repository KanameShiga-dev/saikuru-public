"""Authenticated TLS gateway for one private Wi-Fi address; desktop stays loopback-only."""
import base64
import hashlib
import hmac
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import threading
import time
from urllib.parse import urlsplit
import urllib.request
import urllib.error


PORT = 8788
COOKIE = 'agent_team_mobile'
VALID_PATHS = {'/', '/app.js', '/style.css', '/api/state', '/api/handoff', '/api/jobs', '/api/decide',
    '/api/cancel', '/api/retry', '/api/tasks/model', '/api/tasks/restore', '/api/tasks/handoff', '/api/pause', '/api/stop',
    '/api/backup', '/api/folders/list', '/api/folders/create', '/api/projects',
    '/api/models', '/api/profiles', '/api/handoff/fact'}
VALID_PATHS.update({'/api/tasks/advice', '/api/tasks/transfer', '/api/conflicts/investigate'})
VALID_PATHS.update({'/ledger', '/ledger.js', '/ledger.css', '/api/ledger', '/api/ledger/export',
                    '/api/ledger/update', '/api/ledger/add', '/api/ledger/scan', '/api/ledger/backup'})
VALID_PATHS.update({'/api/ledger/archive/preview', '/api/ledger/archive/start'})
VALID_PATHS.update({'/api/ledger/delete/preview', '/api/ledger/delete/start'})
VALID_PATHS.update({'/api/ledger/preview-status'})
VALID_PATHS.update({'/api/ledger/move/preview', '/api/ledger/move/start', '/api/ledger/move/preview-status'})
VALID_PATHS.update({'/theme.css', '/shell.js'})  # 2026-10-03 共通デザイン
VALID_PATHS.update({'/attachments.js', '/api/attachments', '/api/attachments/image', '/api/attachments/meta',
    '/ledger-consultation.js', '/api/ledger/consultation', '/api/ledger/consultation/models',
    '/api/ledger/consultation/open', '/api/ledger/consultation/send',
    '/api/ledger/consultation/cancel', '/api/ledger/consultation/submit'})
VALID_PATHS.add('/api/instruction-health')
VALID_PATHS.add('/api/decision-wait-stats')
VALID_PATHS.add('/api/notifications')
VALID_PATHS.add('/api/notifications/test')
VALID_PATHS.update({'/operation-tests', '/operation-tests.js', '/api/ui-automation/start',
                    '/api/ui-automation/next', '/api/ui-automation/cancel', '/api/ui-automation/history'})


def wifi_binding():
    ps = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    code = "Get-NetIPAddress -InterfaceAlias 'Wi-Fi' -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object {$_.AddressState -eq 'Preferred' -and $_.IPAddress -notlike '169.254*'} | Select-Object -First 1 | ForEach-Object { '{0}/{1}' -f $_.IPAddress,$_.PrefixLength }"
    result = subprocess.run([str(ps), '-NoProfile', '-Command', code], capture_output=True,
        text=True, encoding='utf-8', errors='replace', timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError('Wi-Fi address unavailable')
    iface = ipaddress.ip_interface(result.stdout.strip())
    addr = iface.ip
    if addr.version != 4 or not addr.is_private or addr.is_loopback or addr.is_link_local:
        raise RuntimeError('Private Wi-Fi address unavailable')
    return str(addr), iface.network


def wifi_address():
    return wifi_binding()[0]


def private_file(path, content):
    path.write_bytes(content)
    if os.name == 'nt':
        account = os.environ.get('USERDOMAIN', '') + '\\' + os.environ.get('USERNAME', '')
        run = subprocess.run(['icacls.exe', str(path), '/inheritance:r', '/grant:r',
            account + ':F', 'SYSTEM:F', '*S-1-5-32-544:F'], capture_output=True,
            timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        if run.returncode:
            path.unlink(missing_ok=True)
            raise RuntimeError('Private file ACL could not be set')


def credentials(data):
    path = data / 'mobile-auth.json'
    if not path.exists():
        code = ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(16))
        info = {'code': code, 'signing_key': secrets.token_hex(32)}
        private_file(path, json.dumps(info).encode())
    info = json.loads(path.read_text(encoding='utf-8'))
    if not re.fullmatch('[A-Z2-9]{16}', info['code']) or len(bytes.fromhex(info['signing_key'])) != 32:
        raise RuntimeError('Invalid mobile credential file')
    return info


def certificate(data, address):
    cert, key = data / 'mobile-cert.pem', data / 'mobile-key.pem'
    openssl = shutil.which('openssl') or r'C:\Program Files\Git\usr\bin\openssl.exe'
    if cert.exists() and key.exists():
        check = subprocess.run([openssl, 'x509', '-in', str(cert), '-noout', '-checkip', address],
            capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        if check.returncode == 0:
            return cert, key
    tmp_cert, tmp_key = data / 'mobile-cert.tmp', data / 'mobile-key.tmp'
    try:
        make = subprocess.run([openssl, 'req', '-x509', '-newkey', 'rsa:3072', '-sha256',
            '-nodes', '-days', '365', '-subj', '/CN=Agent Team Local',
            '-addext', 'subjectAltName=IP:' + address, '-keyout', str(tmp_key),
            '-out', str(tmp_cert)], capture_output=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW)
        if make.returncode:
            raise RuntimeError('TLS certificate creation failed')
        private_file(tmp_cert, tmp_cert.read_bytes())
        private_file(tmp_key, tmp_key.read_bytes())
        os.replace(tmp_cert, cert)
        os.replace(tmp_key, key)
        return cert, key
    finally:
        tmp_cert.unlink(missing_ok=True)
        tmp_key.unlink(missing_ok=True)


class Gateway(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, data):
        self.ip = address
        self.network = wifi_binding()[1]
        self.origin = f'https://{address}:{PORT}'
        self.secret = credentials(data)
        self.attempts = {}
        self.attempt_lock = threading.Lock()
        cert, key = certificate(data, address)
        self.fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
        super().__init__((address, PORT), MobileHandler)
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(certfile=str(cert), keyfile=str(key))
            self.socket = context.wrap_socket(self.socket, server_side=True)
        except Exception:
            self.server_close()
            raise


class MobileHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, data, kind='text/html; charset=utf-8', cookie=None, location=None,
              referrer_policy='no-referrer'):
        body = data if isinstance(data, bytes) else data.encode('utf-8')
        self.send_response(status)
        for key, value in [('Content-Type', kind), ('Content-Length', str(len(body))),
                           ('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff'),
                           ('Referrer-Policy', referrer_policy),
                           ('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; form-action 'self'; frame-ancestors 'none'")]:
            self.send_header(key, value)
        if cookie:
            self.send_header('Set-Cookie', cookie)
        if location:
            self.send_header('Location', location)
        self.end_headers()
        self.wfile.write(body)

    def request_ok(self):
        return (self.headers.get('Host') == f'{self.server.ip}:{PORT}'
            and ipaddress.ip_address(self.client_address[0]) in self.server.network
            and self.headers.get('Sec-Fetch-Site', 'same-origin') != 'cross-site'
            and self.path.split('?', 1)[0] in VALID_PATHS | {'/mobile/login'})

    def signed_cookie(self):
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get('Cookie', ''))
            stamp, sig = jar[COOKIE].value.split('.', 1)
            if int(stamp) < time.time() or int(stamp) > time.time() + 31 * 86400:
                return False
            expected = hmac.new(bytes.fromhex(self.server.secret['signing_key']), stamp.encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(sig, expected)
        except (ValueError, KeyError, IndexError):
            return False

    def login_page(self, error=''):
        # No JavaScript, external files, account identifiers or secrets in URL.
        content = '<!doctype html><html lang="ja"><meta name="viewport" content="width=device-width,initial-scale=1"><meta charset="utf-8"><title>采来 — サイクル —</title><main><h1>采来 — サイクル —</h1><p>PCに表示されたスマホ用の合言葉を入力してください。</p>'
        if error:
            content += '<p role="alert">合言葉を確認してください。</p>'
        content += '<form method="post" action="/mobile/login"><label>合言葉 <input name="code" type="password" autocomplete="off" required></label><button type="submit">接続</button></form></main></html>'
        # HTML form POSTs under no-referrer send Origin: null in browsers.
        # Preserve the strict Origin check while suppressing cross-origin referrers.
        self.reply(200, content, referrer_policy='same-origin')

    def proxy(self, body=None):
        parsed = urlsplit(self.path)
        path = parsed.path
        if path not in VALID_PATHS:
            return self.reply(404, 'Not found', 'text/plain')
        # Obtain the loopback-only cookie server-side. Never expose it to phone clients.
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(__import__('http.cookiejar', fromlist=['CookieJar']).CookieJar()))
        try:
            opener.open('http://127.0.0.1:8790/', timeout=5).close()
            headers = {'X-Agent-Team-UI': '1'}
            if body is not None:
                headers['Content-Type'] = 'application/json'
            upstream_path = path + ('?' + parsed.query if parsed.query else '')
            req = urllib.request.Request('http://127.0.0.1:8790' + upstream_path, data=body, headers=headers)
            try:
                upstream = opener.open(req, timeout=130 if path in ('/api/attachments', '/api/attachments/meta', '/api/attachments/image') else 100 if path == '/api/ui-automation/next' else 35)
            except urllib.error.HTTPError as exc:
                upstream = exc
            with upstream:
                self.reply(upstream.status, upstream.read(5_242_881 if path == '/api/attachments/image' else 2_000_000), upstream.headers.get('Content-Type', 'application/json'))
        except Exception:
            self.reply(502, 'PC側のサービスに接続できません。', 'text/plain; charset=utf-8')

    def do_GET(self):
        if not self.request_ok():
            return self.reply(403, 'Forbidden', 'text/plain')
        if not self.signed_cookie():
            return self.login_page()
        return self.proxy()

    def do_POST(self):
        if not self.request_ok() or self.headers.get('Origin') != self.server.origin:
            return self.reply(403, 'Forbidden', 'text/plain')
        size = int(self.headers.get('Content-Length', '0'))
        limit = 7_100_000 if self.path.split('?', 1)[0] == '/api/attachments' else 100_000
        if not 0 < size <= limit:
            return self.reply(400, 'Bad request', 'text/plain')
        body = self.rfile.read(size)
        if self.path == '/mobile/login':
            from urllib.parse import parse_qs
            with self.server.attempt_lock:
                address = self.client_address[0]
                recent = [t for t in self.server.attempts.get(address, []) if t > time.time() - 900]
                if len(recent) >= 5:
                    return self.reply(429, 'しばらく待ってからやり直してください。', 'text/plain; charset=utf-8')
                self.server.attempts[address] = recent + [time.time()]
            supplied = parse_qs(body.decode('utf-8', errors='replace')).get('code', [''])[0]
            normalized = re.sub(r'[^A-Z2-9]', '', supplied.upper())
            if not hmac.compare_digest(normalized, self.server.secret['code']):
                return self.login_page('invalid')
            stamp = str(int(time.time() + 30 * 86400))
            sig = hmac.new(bytes.fromhex(self.server.secret['signing_key']), stamp.encode(), hashlib.sha256).hexdigest()
            return self.reply(303, b'', cookie=f'{COOKIE}={stamp}.{sig}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=2592000', location='/')
        if not self.signed_cookie() or self.headers.get_content_type() != 'application/json':
            return self.reply(403, 'Forbidden', 'text/plain')
        return self.proxy(body)


def start_gateway(data):
    address = wifi_address()
    gateway = Gateway(address, data)
    threading.Thread(target=gateway.serve_forever, daemon=True).start()
    return gateway
