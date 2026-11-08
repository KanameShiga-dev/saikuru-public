"""Standalone, fail-closed snapshot diagnostics; no normal server imports.

Production invocation requires an approved absolute interpreter and -I -S -B.
The process never creates an Engine, Store, provider, updater or auth monitor.
The audit policy is application defence, not a native-code/OS sandbox.
"""
import argparse
from contextlib import ExitStack
from datetime import datetime
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import socket
from socketserver import TCPServer
import stat
import sys
from types import ModuleType
from urllib.parse import urlsplit


SOURCE_NAMES = ('safe_server.py', 'safe_start_runtime.py', 'web/safe-start.html')
SAFE_GET = frozenset(('/health', '/', '/diagnostics', '/api/safe-start/status',
                      '/api/safe-start/jobs', '/api/safe-start/approvals'))
LEGACY_GET = frozenset('''/history /history.js /history.css /app.js /style.css
/theme.css /shell.js /skills /skills.js /skills.css /operation-tests
/operation-tests.js /context-optimization /context-optimization.js /attachments.js
/skill-picker.js /ledger /ledger.js /ledger-consultation.js /ledger.css
/api/project-history /api/decision-wait-stats /api/attachments/meta
/api/attachments/image /api/state /api/notifications /api/context-optimization
/api/job/artifact /api/tasks/detail /api/jobs/evaluation /api/quality/job
/api/jobs/archive /api/ledger/consultation/models /api/ledger/consultation
/api/ui-automation/history /api/instruction-health /api/ledger /api/ledger/export
/api/ledger/preview-status /api/ledger/move/preview-status /api/jobs/previews
/api/previews/image /api/skills /api/handoff /api/mobile'''.split())


class BootstrapError(RuntimeError):
    pass


def _plain_path(value, *, missing=False, directory=False):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise BootstrapError('absolute_path_required')
    if path.drive.startswith('\\\\') or any(':' in p for p in path.parts[1:]):
        raise BootstrapError('network_or_stream_path_forbidden')
    if any(p.lower() in {'.credentials.json', '.env', '.claude', '.claude-sairai'}
           for p in path.parts):
        raise BootstrapError('credential_path_forbidden')
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            if missing and part == path:
                continue
            raise BootstrapError('path_missing') from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise BootstrapError('reparse_path_forbidden')
    if missing:
        if path.exists():
            raise BootstrapError('new_evidence_directory_required')
    elif directory:
        if not path.is_dir():
            raise BootstrapError('directory_required')
    elif not path.is_file() or path.stat().st_nlink != 1:
        raise BootstrapError('regular_single_link_file_required')
    return path.resolve(strict=not missing)


def _bounded_read(path, limit):
    with path.open('rb') as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise BootstrapError('input_too_large')
    return value


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise BootstrapError('duplicate_manifest_key')
        value[key] = item
    return value


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='Read-only safe snapshot diagnostics',
                                     allow_abbrev=False)
    parser.add_argument('--approved-base', required=True,
                        help='Absolute folder approved for the manifest and diagnostics')
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--evidence-dir', required=True)
    parser.add_argument('--port', type=int, choices=(8877,), default=8877)
    args = parser.parse_args(argv)
    if not re.fullmatch('[0-9a-f]{64}', args.manifest_sha256):
        parser.error('manifest SHA-256 must be 64 lowercase hexadecimal characters')
    if not Path(args.approved_base).is_absolute():
        parser.error('approved base must be an absolute path')
    return args


def preflight(args, source_root, approved_base):
    """Only fixed manifest/source reads occur before the guard is installed."""
    base = _plain_path(approved_base, directory=True)
    if not Path(args.manifest).is_absolute() or not Path(args.manifest).is_relative_to(base):
        raise BootstrapError('manifest_outside_scope')
    manifest_path = _plain_path(args.manifest)
    if manifest_path == base or not manifest_path.is_relative_to(base):
        raise BootstrapError('manifest_outside_scope')
    data = _bounded_read(manifest_path, 131072)
    if hashlib.sha256(data).hexdigest() != args.manifest_sha256:
        raise BootstrapError('manifest_hash_mismatch')
    try:
        raw = json.loads(data.decode('utf-8'), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError):
        raise BootstrapError('manifest_json_invalid') from None
    required = {'schema_version', 'mode', 'host', 'port', 'diagnostic_root',
                'source_sha256', 'snapshot', 'allowed_job_ids'}
    if not isinstance(raw, dict) or set(raw) != required:
        raise BootstrapError('manifest_keys_invalid')
    if (type(raw['schema_version']) is not int or raw['schema_version'] != 1
            or raw['mode'] != 'safe' or raw['host'] != '127.0.0.1'
            or type(raw['port']) is not int or raw['port'] != args.port):
        raise BootstrapError('safe_mode_required')
    root = _plain_path(raw['diagnostic_root'], directory=True)
    if root == base or not root.is_relative_to(base) or not manifest_path.is_relative_to(root):
        raise BootstrapError('diagnostic_root_outside_scope')
    evidence = _plain_path(args.evidence_dir, missing=True)
    if evidence == root or not evidence.is_relative_to(root):
        raise BootstrapError('evidence_outside_scope')
    snapshot_raw = raw['snapshot']
    if (not isinstance(snapshot_raw, dict)
            or set(snapshot_raw) != {'path', 'sha256', 'size', 'created_at'}
            or not isinstance(snapshot_raw['path'], str)
            or Path(snapshot_raw['path']).is_absolute()
            or '..' in Path(snapshot_raw['path']).parts):
        raise BootstrapError('snapshot_fields_invalid')
    snapshot = _plain_path(root / snapshot_raw['path'])
    if snapshot == root or not snapshot.is_relative_to(root) or snapshot.suffix != '.sqlite3':
        raise BootstrapError('snapshot_path_invalid')
    hashes = raw['source_sha256']
    if not isinstance(hashes, dict) or set(hashes) != set(SOURCE_NAMES):
        raise BootstrapError('source_hashes_invalid')
    code = {}
    for name in SOURCE_NAMES:
        path = _plain_path(source_root / name)
        value = _bounded_read(path, 2 * 1024 * 1024)
        expected = hashes[name]
        if (not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected)
                or hashlib.sha256(value).hexdigest() != expected):
            raise BootstrapError('source_hash_mismatch')
        code[name] = value
    return {'raw': raw, 'manifest_path': manifest_path, 'root': root,
            'snapshot': snapshot, 'evidence': evidence, 'code': code,
            'source_root': source_root}


class BootstrapGuard:
    """Audit checks cover Python events; native/other-process activity is not covered."""
    def __init__(self, prepared, bind_port=8877):
        self.evidence = prepared['evidence']
        self.snapshot_uri = prepared['snapshot'].as_uri() + '?mode=ro&immutable=1'
        self.readable = {prepared['manifest_path'], prepared['snapshot'],
                         self.evidence / 'stop.request'}
        self.readable.update(prepared['source_root'] / name for name in SOURCE_NAMES)
        self.readable.add((Path(sys.base_prefix) /
                           ('python' + str(sys.version_info.major) + str(sys.version_info.minor) + '.zip')).resolve())
        self.stdlib_roots = tuple((Path(sys.base_prefix) / n).resolve() for n in ('Lib', 'DLLs'))
        self.writable = {self.evidence / n for n in ('startup.json', 'events.jsonl', 'stopped.json')}
        self.bind_port = bind_port
        self.fatal = False
        self.denied = []
        self.probes = 0
        self.snapshot_connections = 0

    def reject(self, category):
        self.denied.append(category)
        self.fatal = True
        raise BootstrapError(category)

    def _path(self, value):
        if not isinstance(value, (str, bytes, os.PathLike)):
            self.reject('unclassified_file_descriptor')
        try:
            path = Path(os.fsdecode(value))
            if path.drive.startswith('\\\\') or any(':' in p for p in path.parts[1:]):
                self.reject('network_or_stream_path_forbidden')
            path = path.resolve()
        except (OSError, ValueError):
            self.reject('invalid_file_path')
        return path

    def check(self, event, args):
        if event == 'saikuru.safe.guard_probe':
            self.probes += 1
        elif event == 'saikuru.safe.forbidden_probe':
            raise BootstrapError('guard_probe_denied')
        elif (event == 'subprocess.Popen' or event in {'os.system', 'os.startfile',
                'os.startfile/2', 'os.fork', 'os.forkpty', 'os.posix_spawn',
                'os.exec', 'os.spawn', '_winapi.CreateProcess'}):
            self.reject('child_process_forbidden')
        elif event.startswith('socket.get') or event == 'socket.connect':
            self.reject('outbound_or_dns_forbidden')
        elif event == 'socket.__new__':
            if args[1] != socket.AF_INET or args[2] != socket.SOCK_STREAM:
                self.reject('socket_kind_forbidden')
        elif event == 'socket.bind':
            if args[1] != ('127.0.0.1', self.bind_port):
                self.reject('bind_address_forbidden')
        elif event == 'import':
            name = args[0]
            if (name == 'server' or name.startswith(('team_', 'mobile_gateway',
                    'consultation_', 'attachment_', 'webbrowser'))):
                self.reject('normal_module_forbidden')
        elif event == 'sqlite3.connect':
            if args[0] != self.snapshot_uri:
                self.reject('sqlite_target_forbidden')
            self.snapshot_connections += 1
        elif event == 'sqlite3.enable_load_extension' and args[1]:
            self.reject('sqlite_extension_forbidden')
        elif event == 'sqlite3.load_extension':
            self.reject('sqlite_extension_forbidden')
        elif event == 'ctypes.dlopen':
            if args[0] not in (None, 'kernel32', 'kernel32.dll'):
                self.reject('native_library_forbidden')
        elif event in ('ctypes.dlsym', 'ctypes.dlsym/handle'):
            if args[1] not in ('CreateFileW', 'CloseHandle', 'GetLastError', 'GetFileInformationByHandle'):
                self.reject('native_symbol_forbidden')
        elif event == 'open':
            path = self._path(args[0])
            if any(p.lower() in {'.credentials.json', '.env', '.claude', '.claude-sairai'}
                   for p in path.parts):
                self.reject('credential_file_forbidden')
            mode, flags = args[1], args[2]
            writing = (any(c in (mode or '') for c in 'wax+')
                       or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if writing:
                if path not in self.writable:
                    self.reject('file_write_forbidden')
            elif path not in self.readable and not any(path.is_relative_to(p) for p in self.stdlib_roots):
                self.reject('file_read_forbidden')
        elif event == 'os.mkdir':
            if self._path(args[0]) != self.evidence:
                self.reject('mkdir_forbidden')
        elif event in {'os.remove', 'os.rmdir', 'os.rename', 'os.link', 'os.symlink',
                       'os.truncate', 'os.chmod', 'os.utime', 'os.chdir',
                       'os.putenv', 'os.unsetenv'}:
            self.reject('filesystem_or_environment_mutation_forbidden')

    def install(self):
        sys.addaudithook(self.check)
        sys.audit('saikuru.safe.guard_probe')
        if self.probes != 1:
            raise BootstrapError('audit_guard_install_failed')
        try:
            sys.audit('saikuru.safe.forbidden_probe')
        except BootstrapError:
            return
        raise BootstrapError('audit_guard_deny_failed')

    def status(self):
        return {'denied_attempts': list(self.denied),
                'snapshot_connections': self.snapshot_connections,
                'coverage': 'Python audit events; not OS/native isolation'}


def load_verified_runtime(prepared):
    name = 'saikuru_safe_runtime'
    if name in sys.modules:
        raise BootstrapError('runtime_already_loaded')
    module = ModuleType(name)
    module.__file__ = str(prepared['source_root'] / 'safe_start_runtime.py')
    sys.modules[name] = module
    try:
        # Execute the exact verified bytes, not a second pathname lookup.
        exec(compile(prepared['code']['safe_start_runtime.py'], module.__file__, 'exec'), module.__dict__)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


class SafeHTTPServer(HTTPServer):
    def server_bind(self):
        TCPServer.server_bind(self)  # HTTPServer's getfqdn would resolve a name.
        self.server_name = '127.0.0.1'
        self.server_port = self.socket.getsockname()[1]

    def get_request(self):
        conn, peer = super().get_request()
        if peer[0] != '127.0.0.1':
            conn.close()
            raise OSError('non_loopback_peer')
        conn.settimeout(2)
        return conn, peer


class SafeHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'
    server_version = 'SaikuruSafe/1'
    sys_version = ''

    def log_message(self, *args):
        pass

    def common(self):
        hosts = self.headers.get_all('Host', [])
        origin = self.headers.get_all('Origin', [])
        fetch = self.headers.get_all('Sec-Fetch-Site', [])
        if (hosts != [self.server.origin.removeprefix('http://')]
                or len(origin) > 1 or origin and origin[0] != self.server.origin
                or len(fetch) > 1 or fetch and fetch[0] not in {'none', 'same-origin', 'same-site'}):
            raise PermissionError('host_or_origin_denied')

    def send(self, status, value, *, html_body=False):
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8' if html_body else 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Connection', 'close')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'none'; style-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.close_connection = True
        if self.command != 'HEAD':
            self.wfile.write(data)

    def _page(self, content):
        return self.server.template.replace('{{CONTENT}}', content).encode('utf-8')

    def do_GET(self):
        try:
            self.common()
            parsed = urlsplit(self.path)
            if (parsed.scheme or parsed.netloc or parsed.fragment or '%' in parsed.path or '\\' in parsed.path
                    or '..' in parsed.path or '//' in parsed.path
                    or self.headers.get('Transfer-Encoding')
                    or self.headers.get('Content-Length', '0') != '0'):
                raise PermissionError('request_shape_denied')
            path = parsed.path
            # Private routes are denied before query parsing or runtime access.
            if path in ('/api/safe-start/jobs', '/api/safe-start/approvals'):
                return self.send(403, {'error': 'safe_access_denied'})
            if path not in SAFE_GET:
                return self.send(403 if path in LEGACY_GET else 404,
                                 {'error': 'safe_mode_read_only' if path in LEGACY_GET else 'not_found'})
            if parsed.query:
                raise PermissionError('query_not_allowed')
            if path == '/health':
                return self.send(200, {'status': 'ok', 'app': 'saikuru-safe', 'mode': 'safe'})
            if path == '/api/safe-start/status':
                return self.send(200, {
                    'status': 'ok', 'mode': 'safe', 'engine': 'not_created',
                    'execution_enabled': False, 'provider_auth': 'not_checked',
                    'diagnostic_only': True,
                })
            body = ('<h2>安全診断モード</h2><p>起動モード：SAFE。Engine：未生成。'
                    'AI実行：無効。Provider認証：未確認。</p>'
                    '<p>ジョブ登録・G2承認・G3承認・正式受領操作：無効。</p>'
                    '<p>ジョブ情報・承認情報：閲覧禁止。</p>'
                    '<p>外部Providerを起動しない設計です。実サービスから独立しており、'
                    'この画面は実サービスの状態を示しません。</p>')
            return self.send(200, self._page(body), html_body=True)
        except PermissionError:
            self.send(403, {'error': 'safe_access_denied'})
        except Exception:
            self.server.failed = True
            self.send(503, {'error': 'diagnostic_read_failed'})

    def do_POST(self):
        try:
            self.common()
        except PermissionError:
            return self.send(403, {'error': 'safe_access_denied'})
        self.send(403, {'error': 'safe_mode_read_only'})

    def unsupported(self):
        try:
            self.common()
        except PermissionError:
            return self.send(403, {'error': 'safe_access_denied'})
        self.send(405, {'error': 'method_not_allowed'})

    do_HEAD = unsupported
    do_OPTIONS = unsupported
    do_PUT = unsupported
    do_PATCH = unsupported
    do_DELETE = unsupported
    do_CONNECT = unsupported
    do_TRACE = unsupported


def build_server(runtime, template, guard, port=8877):
    """Internal fixture may use port=0; production CLI has no such option."""
    server = SafeHTTPServer(('127.0.0.1', port), SafeHandler, bind_and_activate=False)
    try:
        server.runtime, server.template, server.guard = runtime, template, guard
        server.failed = False
        server.timeout = 0.5
        server.server_bind()
        server.server_activate()
        server.origin = 'http://127.0.0.1:' + str(server.server_port)
        return server
    except BaseException:
        server.server_close()
        raise


def record(path, value):
    # No request bodies, cookie values, tokens or raw error messages.
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def stop_requested(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400 or info.st_nlink != 1:
        raise BootstrapError('invalid_stop_marker')
    return True


def serve_until_stop(server, guard, evidence):
    """No scheduler thread. Only local HTTP and the local stop marker are polled."""
    while not stop_requested(evidence / 'stop.request'):
        if guard.fatal or server.failed:
            raise BootstrapError('safe_runtime_failed')
        server.handle_request()
    if guard.fatal or server.failed:
        raise BootstrapError('safe_runtime_failed')


def main(argv=None):
    args = parse_args(argv)
    if not (sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode):
        print('safe_start_failed: isolated_interpreter_required', file=sys.stderr)
        return 2
    evidence = guard = None
    try:
        source_root = Path(__file__).resolve().parent
        approved_base = Path(args.approved_base)
        prepared = preflight(args, source_root, approved_base)
        guard = BootstrapGuard(prepared)
        guard.install()
        module = load_verified_runtime(prepared)
        manifest = module.SafeManifest(prepared['raw'], prepared['manifest_path'],
                                       args.manifest_sha256, source_root, approved_base)
        template = prepared['code']['web/safe-start.html'].decode('utf-8')
        if template.count('{{CONTENT}}') != 1:
            raise BootstrapError('template_invalid')
        with ExitStack() as resources:
            runtime = module.DiagnosticRuntime(manifest)
            resources.callback(runtime.close)
            evidence = prepared['evidence']
            evidence.mkdir()
            server = build_server(runtime, template, guard)
            resources.callback(server.server_close)
            record(evidence / 'startup.json', {
                'started_at': datetime.now().astimezone().isoformat(), 'pid': os.getpid(),
                'port': 8877, 'mode': 'safe', 'manifest_sha256': args.manifest_sha256,
                'runtime': runtime.status(), 'guard': guard.status()})
            print('Saikuru safe diagnostics: http://127.0.0.1:8877/', flush=True)
            serve_until_stop(server, guard, evidence)
        # The snapshot/SQLite/socket have all been closed before the final check.
        actual, size = module.digest_file(manifest.snapshot, module.MAX_SNAPSHOT)
        if (actual, size) != (manifest.snapshot_sha256, manifest.snapshot_size):
            raise BootstrapError('snapshot_changed_at_stop')
        record(evidence / 'stopped.json', {
            'stopped_at': datetime.now().astimezone().isoformat(), 'mode': 'safe',
            'snapshot_hash_unchanged': True, 'guard': guard.status()})
        return 0
    except KeyboardInterrupt:
        print('safe_start_stopped: interrupted', file=sys.stderr)
        return 130
    except Exception:
        # Do not leak the exception text: it can contain private paths or input.
        print('safe_start_failed: fail_closed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
