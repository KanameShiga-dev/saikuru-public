"""S01-S15: fictional databases only. Execution requires separate approval.

Production main/8877 is never started by this suite. Local HTTP fixtures use
port 0. Only S02 starts a test Python child, to contain the permanent audit hook.
"""
import ast
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import safe_server as entry
import safe_start_runtime as runtime


SOURCE = Path(__file__).resolve().parents[1]
JOB = '1' * 32
FOREIGN = '2' * 32
APPROVAL = '3' * 32
SECRET = 'fictional-provider-secret-not-real'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SafeStartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='no007-safe-synthetic-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'diagnostic'
        self.root.mkdir()
        self.snapshot = self.root / 'snapshot.sqlite3'
        self.manifest_path = self.root / 'manifest.json'
        self.evidence = self.root / 'new-evidence'
        self.make_snapshot()
        # An original-like fixture is deliberately outside the diagnostic root.
        self.original = self.base / 'fictional-original'
        self.original.mkdir()
        for name in ('team.sqlite3', 'team.sqlite3-wal', 'team.sqlite3-shm', 'config.json'):
            (self.original / name).write_bytes(b'fictional-original-do-not-open')
        self.raw = {
            'schema_version': 1, 'mode': 'safe', 'host': '127.0.0.1', 'port': 8877,
            'diagnostic_root': str(self.root),
            'source_sha256': {n: sha(SOURCE / n) for n in entry.SOURCE_NAMES},
            'snapshot': {'path': self.snapshot.name, 'sha256': sha(self.snapshot),
                         'size': self.snapshot.stat().st_size,
                         'created_at': datetime.now(timezone.utc).isoformat()},
            'allowed_job_ids': [JOB],
        }
        self.publish()

    def make_snapshot(self):
        with closing(sqlite3.connect(self.snapshot)) as db, db:
            db.execute('CREATE TABLE objects (kind TEXT, id TEXT PRIMARY KEY, body TEXT NOT NULL)')
            db.execute("CREATE INDEX objects_job ON objects(kind,json_extract(body,'$.job_id'))")
            for kind, ident, body in (
                ('job', JOB, {'status': 'awaiting_approval', 'g2_gate_version': 1,
                              'g2_status': 'pending', 'goal': SECRET}),
                ('job', FOREIGN, {'status': 'running', 'goal': SECRET}),
                ('task', '4' * 32, {'job_id': JOB, 'role': 'planner', 'status': 'queued'}),
                ('task', '5' * 32, {'job_id': JOB, 'role': 'builder', 'status': 'queued'}),
                ('approval', APPROVAL, {'job_id': JOB, 'kind': 'ai_start', 'status': 'pending',
                                      'payload': {'not_for_display': SECRET}}),
            ):
                db.execute('INSERT INTO objects VALUES (?,?,?)', (kind, ident, json.dumps(body)))

    def publish(self):
        self.raw['snapshot']['sha256'] = sha(self.snapshot)
        self.raw['snapshot']['size'] = self.snapshot.stat().st_size
        self.manifest_path.write_text(json.dumps(self.raw), encoding='utf-8')
        self.args = SimpleNamespace(manifest=str(self.manifest_path),
                                    manifest_sha256=sha(self.manifest_path),
                                    evidence_dir=str(self.evidence), port=8877)

    def manifest(self):
        return runtime.SafeManifest(self.raw, self.manifest_path,
                                    sha(self.manifest_path), SOURCE, self.base)

    def prepared(self):
        return entry.preflight(self.args, SOURCE, self.base)

    @contextmanager
    def opened(self):
        value = runtime.DiagnosticRuntime(self.manifest())
        try:
            yield value
        finally:
            value.close()

    @contextmanager
    def http_fixture(self):
        """SQLite is created and used on the HTTP thread, never cross-thread."""
        manifest = self.manifest()
        guard = entry.BootstrapGuard(self.prepared(), bind_port=0)
        ready = queue.Queue()
        errors = []
        evidence = self.root / 'fixture-evidence'
        evidence.mkdir()

        def worker():
            value = server = None
            try:
                value = runtime.DiagnosticRuntime(manifest)
                server = entry.build_server(value, (SOURCE / 'web/safe-start.html').read_text(encoding='utf-8'), guard, port=0)
                server.timeout = 0.05
                ready.put(server)
                entry.serve_until_stop(server, guard, evidence)
            except BaseException as error:
                errors.append(type(error).__name__)
                if server is None:
                    ready.put(None)
            finally:
                if server is not None:
                    server.server_close()
                if value is not None:
                    value.close()

        thread = threading.Thread(target=worker, name='synthetic-safe-http', daemon=True)
        thread.start()
        try:
            server = ready.get(timeout=5)
            self.assertIsNotNone(server, 'fixture did not become ready')
            self.assertNotEqual(server.server_port, 8877)
            self.assertNotEqual(server.server_port, 8790)
            yield server
        finally:
            (evidence / 'stop.request').write_bytes(b'')
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), 'fixture thread did not stop')
            self.assertEqual(errors, [], 'fixture/cleanup error')

    def request(self, server, path, method='GET', headers=None):
        values = {'Origin': server.origin}
        values.update(headers or {})
        data = b'{}' if method == 'POST' else None
        request = Request(server.origin + path, data=data, method=method, headers=values)
        # Explicitly bypass proxy discovery for the synthetic localhost client.
        opener = build_opener(ProxyHandler({}))
        try:
            with opener.open(request, timeout=3) as response:
                return response.status, response.read(), dict(response.headers)
        except HTTPError as error:
            with error:
                return error.code, error.read(), dict(error.headers)

    def test_s01_no_engine_or_dispatch(self):
        for g2 in ('pending', 'approved', 'executing', 'consumed', 'unknown'):
            with self.subTest(g2=g2):
                with closing(sqlite3.connect(self.snapshot)) as db, db:
                    body = {'status': 'planning', 'g2_gate_version': 1, 'g2_status': g2}
                    db.execute('UPDATE objects SET body=? WHERE id=?', (json.dumps(body), JOB))
                    db.execute('UPDATE objects SET body=? WHERE id=?',
                               (json.dumps({'job_id': JOB, 'kind': 'plan', 'status': 'approved'}), APPROVAL))
                self.publish()
                with self.opened() as value:
                    for operation in ('dispatch', 'execute', 'claim', 'retry'):
                        with self.assertRaises(runtime.SafeStartError):
                            getattr(value, operation)()
                    self.assertFalse(value.status()['execution_enabled'])
                    self.assertFalse(hasattr(value, 'engine'))
                    self.assertFalse(value.snapshot.jobs()[0]['execution_enabled'])
        for file in ('safe_server.py', 'safe_start_runtime.py'):
            tree = ast.parse((SOURCE / file).read_text(encoding='utf-8'))
            imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
            self.assertFalse(any('team_' in ast.unparse(n) or 'from server ' in ast.unparse(n) for n in imports))

    def test_s02_no_cli_or_child_spawn(self):
        # This is the only Python child in this test file; the guarded child
        # attempts a second spawn, which must fail before a child is created.
        code = '''import runpy,sys
from pathlib import Path
entry=runpy.run_path(sys.argv[1],run_name='safe_entry_under_test')
args=entry['parse_args'](['--approved-base',sys.argv[5],'--manifest',sys.argv[2],'--manifest-sha256',sys.argv[3],'--evidence-dir',sys.argv[4]])
prepared=entry['preflight'](args,Path(sys.argv[1]).parent,Path(sys.argv[5]))
guard=entry['BootstrapGuard'](prepared,bind_port=0)
guard.install()
module=entry['load_verified_runtime'](prepared)
manifest=module.SafeManifest(prepared['raw'],prepared['manifest_path'],sys.argv[3],Path(sys.argv[1]).parent,Path(sys.argv[5]))
value=module.DiagnosticRuntime(manifest)
server=None
try:
    prepared['evidence'].mkdir()
    server=entry['build_server'](value,prepared['code']['web/safe-start.html'].decode('utf-8'),guard,port=0)
    assert server.server_port not in (8877,8790)
    assert value.snapshot.jobs()[0]['id']=='1'*32
    assert guard.snapshot_connections==1
    entry['record'](prepared['evidence']/'startup.json',{'mode':'safe','fixture':True})
finally:
    if server is not None:server.server_close()
    value.close()
import subprocess
try:
    subprocess.run([sys.executable,'-c','raise SystemExit(77)'],check=False)
except entry['BootstrapError']:
    assert guard.denied==['child_process_forbidden']
    print('spawn-denied')
else:
    raise SystemExit(91)
'''
        child = subprocess.run([sys.executable, '-I', '-S', '-B', '-W', 'error::ResourceWarning', '-c', code,
                                str(SOURCE / 'safe_server.py'), str(self.manifest_path),
                                sha(self.manifest_path), str(self.evidence), str(self.base)],
                               capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout.strip(), 'spawn-denied')
        self.assertNotIn('ResourceWarning', child.stderr)
        self.assertNotIn('Exception ignored', child.stderr)

    def test_s03_no_outbound_or_dns(self):
        for event, args in (
            ('socket.connect', (None, ('203.0.113.1', 443))),
            ('socket.connect', (None, ('127.0.0.1', 8877))),
            ('socket.getaddrinfo', ('example.invalid', 443, 0, 0, 0)),
            ('socket.gethostbyaddr', ('127.0.0.1',)),
            ('socket.bind', (None, ('0.0.0.0', 8877))),
        ):
            with self.subTest(event=event, args=args):
                guard = entry.BootstrapGuard(self.prepared())
                with self.assertRaises(entry.BootstrapError):
                    guard.check(event, args)
                self.assertTrue(guard.fatal)
        with patch('socket.getfqdn', side_effect=AssertionError('DNS forbidden')):
            with self.http_fixture() as server:
                self.assertEqual(server.server_name, '127.0.0.1')

    def test_s04_no_usage_monitor(self):
        guard = entry.BootstrapGuard(self.prepared())
        with self.assertRaises(entry.BootstrapError):
            guard.check('import', ('team_usage', None, None, None, None))
        with self.opened() as value:
            self.assertEqual(value.status()['provider_monitors'], 'not_created')
        self.assertFalse((self.root / 'claude-usage-schedule.json').exists())
        text = (SOURCE / 'safe_server.py').read_text(encoding='utf-8')
        self.assertNotIn('UsageMonitor(', text)

    def test_s05_no_update_monitor(self):
        guard = entry.BootstrapGuard(self.prepared())
        with self.assertRaises(entry.BootstrapError):
            guard.check('import', ('team_cli_update', None, None, None, None))
        text = (SOURCE / 'safe_server.py').read_text(encoding='utf-8')
        self.assertNotIn('CliUpdateMonitor(', text)
        self.assertNotIn('npm', text)
        with self.http_fixture() as server:
            for path in ('/api/cli-update/check', '/api/claude-update/install', '/api/copilot-update/check'):
                self.assertEqual(self.request(server, path, 'POST')[0], 403)

    def test_s06_no_provider_credentials(self):
        guard = entry.BootstrapGuard(self.prepared())
        for event, args in (
            ('open', (str(self.base / '.credentials.json'), 'r', 0)),
            ('ctypes.dlopen', ('advapi32.dll',)),
            ('import', ('team_claude_auth', None, None, None, None)),
        ):
            with self.subTest(event=event):
                with self.assertRaises(entry.BootstrapError):
                    guard.check(event, args)
        with patch.dict(os.environ, {'CLAUDE_CODE_OAUTH_TOKEN': SECRET}), self.opened() as value:
            self.assertEqual(value.status()['provider_auth'], 'not_checked')
            self.assertNotIn(SECRET, json.dumps(value.status()))
            self.assertNotIn(SECRET, json.dumps(value.snapshot.jobs()))
            self.assertNotIn(SECRET, json.dumps(value.snapshot.approvals()))
        for file in ('safe_server.py', 'safe_start_runtime.py'):
            self.assertNotIn('os.environ', (SOURCE / file).read_text(encoding='utf-8'))

    def test_s07_original_db_and_sidecars_unchanged(self):
        before = {p.name: sha(p) for p in self.original.iterdir()}
        guard = entry.BootstrapGuard(self.prepared())
        with self.assertRaises(entry.BootstrapError):
            guard.check('sqlite3.connect', (str(self.original / 'team.sqlite3'),))
        with self.opened() as value:
            for sql in ("UPDATE objects SET kind='bad'", 'CREATE TABLE bad(x)',
                        "ATTACH DATABASE ':memory:' AS other", 'PRAGMA query_only=OFF'):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                    value.snapshot.db.execute(sql)
        self.assertEqual(before, {p.name: sha(p) for p in self.original.iterdir()})

    def test_s08_jobs_and_approvals_unchanged(self):
        before = sha(self.snapshot)
        with self.opened() as value:
            jobs = value.snapshot.jobs()
            approvals = value.snapshot.approvals()
            self.assertEqual([row['id'] for row in jobs], [JOB])
            self.assertEqual(jobs[0]['g2_status'], 'pending')
            self.assertEqual(approvals[0]['status'], 'pending')
            for operation in ('claim', 'retry', 'dispatch'):
                with self.assertRaises(runtime.SafeStartError):
                    getattr(value, operation)()
        self.assertEqual(sha(self.snapshot), before)
        self.assertFalse(Path(str(self.snapshot) + '-wal').exists())
        self.assertFalse(Path(str(self.snapshot) + '-shm').exists())

    def test_s09_all_mutation_routes_denied(self):
        tree = ast.parse((SOURCE / 'server.py').read_text(encoding='utf-8'))
        paths = set()
        for cls in tree.body:
            if isinstance(cls, ast.ClassDef) and cls.name == 'Handler':
                for fn in cls.body:
                    if isinstance(fn, ast.FunctionDef) and fn.name == 'do_POST':
                        for node in ast.walk(fn):
                            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith(('/api/', '/worker/')):
                                paths.add(node.value)
        self.assertIn('/api/decide', paths)
        self.assertIn('/api/jobs/formal-acceptance', paths)
        before = sha(self.snapshot)
        with self.http_fixture() as server:
            for path in sorted(paths):
                with self.subTest(path=path):
                    self.assertEqual(self.request(server, path, 'POST')[0], 403)
            for path in sorted(entry.LEGACY_GET):
                with self.subTest(get_path=path):
                    self.assertEqual(self.request(server, path)[0], 403)
        self.assertEqual(sha(self.snapshot), before)

    def test_s10_engine_resume_denied(self):
        with self.http_fixture() as server:
            for path in ('/api/pause', '/api/retry', '/api/tasks/restore', '/api/resume', '/api/stop', '/api/shutdown'):
                self.assertEqual(self.request(server, path, 'POST')[0], 403)
            status, body, _ = self.request(server, '/api/safe-start/status')
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)['engine'], 'not_created')

    def test_s11_public_diagnostics_and_private_data_denied(self):
        forbidden = [JOB, FOREIGN, APPROVAL, SECRET, self.raw['snapshot']['sha256'],
                     str(self.base), str(self.root), str(self.snapshot), str(SOURCE),
                     'snapshot_sha256', 'source_sha256', 'allowed_job_ids',
                     'not_for_display', 'records', *self.raw['source_sha256'].values()]
        cases = [
            ('A01', '/health', 'GET', {}, 200),
            ('A02-A03', '/', 'GET', {}, 200),
            ('A04', '/diagnostics', 'GET', {}, 200),
            ('A05', '/api/safe-start/status', 'GET', {}, 200),
            ('A06', '/api/safe-start/jobs', 'GET', {}, 403),
            ('A07', '/api/safe-start/approvals', 'GET', {}, 403),
            ('A08', '/api/safe-start/jobs', 'GET', {'Cookie': 'agent_team_safe=old-fictional-session'}, 403),
            ('A09', '/api/safe-start/approvals', 'GET', {'Cookie': 'agent_team_safe=forged'}, 403),
            ('A13', '/diagnostics', 'GET', {'Origin': 'https://example.invalid'}, 403),
            ('A15', '/diagnostics', 'GET', {'Sec-Fetch-Site': 'cross-site'}, 403),
            ('A16', '/missing', 'GET', {}, 404),
            ('A12-encoded', '/api/safe-start/%6aobs', 'GET', {}, 403),
            ('A12-alias', '/api//safe-start/jobs', 'GET', {}, 403),
            ('A17', '/api/decide', 'POST', {}, 403),
            ('A18', '/health', 'HEAD', {}, 405),
        ]
        for route in ('jobs', 'approvals'):
            for case, query in (('A10', '?job_id=' + JOB), ('A11', '?job_id=' + FOREIGN),
                                ('A12', '?job_id='), ('A12', '?bad=%ZZ'),
                                ('A12', '?job_id=x&job_id=y'), ('A12', '?broken')):
                for cookie in ('', 'agent_team_safe=old-fictional-session', 'agent_team_safe=forged'):
                    cases.append((case, '/api/safe-start/' + route + query, 'GET', {'Cookie': cookie}, 403))
        with self.http_fixture() as server:
            cases.append(('A14', '/diagnostics', 'GET', {'Host': 'localhost:' + str(server.server_port)}, 403))
            with patch.object(server.runtime.snapshot, 'jobs', side_effect=AssertionError('private read')) as jobs, \
                 patch.object(server.runtime.snapshot, 'approvals', side_effect=AssertionError('private read')) as approvals, \
                 patch.object(runtime.DiagnosticRuntime, 'status', side_effect=AssertionError('internal status')) as internal_status:
                for case, path, method, values, expected in cases:
                    with self.subTest(case=case, path=path, headers=values, method=method):
                        status, body, headers = self.request(server, path, method, headers=values)
                        self.assertEqual(status, expected)
                        self.assertNotIn('set-cookie', {key.lower() for key in headers})
                        self.assertEqual(headers['Cache-Control'], 'no-store')
                        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
                        self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
                        self.assertIn("script-src 'none'", headers['Content-Security-Policy'])
                        output = body.decode('utf-8') + json.dumps(headers)
                        for value in forbidden:
                            self.assertNotIn(value, output)
                        if path == '/api/safe-start/status' and status == 200:
                            self.assertEqual(json.loads(body), {
                                'status': 'ok', 'mode': 'safe', 'engine': 'not_created',
                                'execution_enabled': False, 'provider_auth': 'not_checked',
                                'diagnostic_only': True})
                        if path.startswith(('/api/safe-start/jobs', '/api/safe-start/approvals')):
                            self.assertEqual(json.loads(body), {'error': 'safe_access_denied'})
                jobs.assert_not_called()
                approvals.assert_not_called()
                internal_status.assert_not_called()
        template = (SOURCE / 'web/safe-start.html').read_text(encoding='utf-8')
        self.assertNotIn('<script', template.lower())
        self.assertNotIn('<form', template.lower())

    def test_s12_invalid_start_fails_closed(self):
        common = ['--approved-base', str(self.base), '--manifest', str(self.manifest_path), '--manifest-sha256', sha(self.manifest_path),
                  '--evidence-dir', str(self.evidence)]
        for extra in (['--normal'], ['--resume'], ['--data', 'x'], ['--port', '8790'], ['--port', '0'], ['--host', '0.0.0.0']):
            with self.subTest(extra=extra), patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit) as error:
                entry.parse_args(common + extra)
            self.assertNotEqual(error.exception.code, 0)
        for key, value in (('mode', 'normal'), ('port', 8790), ('host', '0.0.0.0'), ('schema_version', True)):
            old = self.raw[key]
            self.raw[key] = value
            self.publish()
            with self.subTest(key=key), self.assertRaises(entry.BootstrapError):
                self.prepared()
            self.raw[key] = old
        self.publish()
        old = self.raw['source_sha256']['safe_server.py']
        self.raw['source_sha256']['safe_server.py'] = '0' * 64
        self.publish()
        with self.assertRaises(entry.BootstrapError):
            self.prepared()
        self.raw['source_sha256']['safe_server.py'] = old
        self.publish()
        wal = Path(str(self.snapshot) + '-wal')
        wal.write_bytes(b'fictional-unconsolidated-wal')
        with self.assertRaises(runtime.SafeStartError):
            runtime.DiagnosticRuntime(self.manifest())
        wal.unlink()
        self.assertFalse(self.evidence.exists())
        outside = SimpleNamespace(**vars(self.args))
        outside.manifest = str(SOURCE / 'server.py')
        with patch.object(entry, '_bounded_read') as reader, self.assertRaises(entry.BootstrapError):
            entry.preflight(outside, SOURCE, self.base)
        reader.assert_not_called()
        with self.assertRaises(entry.BootstrapError):
            entry._plain_path(r'\\fictional.invalid\share\manifest.json')
        self.raw['unexpected_provider_setting'] = True
        self.publish()
        with self.assertRaises(entry.BootstrapError):
            self.prepared()
        del self.raw['unexpected_provider_setting']
        self.publish()
        with patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
            entry.parse_args([])
        alias = self.root / 'hardlink.sqlite3'
        os.link(self.snapshot, alias)
        try:
            with self.assertRaises(runtime.SafeStartError):
                self.manifest()
        finally:
            alias.unlink()
        # Windows symlink creation need not be enabled for the rejection test.
        fake = SimpleNamespace(st_mode=0o120777, st_file_attributes=0x400)
        with patch.object(Path, 'lstat', return_value=fake), self.assertRaises(runtime.SafeStartError):
            runtime.checked_path(self.snapshot)
        duplicate = b'{"mode":"safe","mode":"normal"}'
        self.manifest_path.write_bytes(duplicate)
        self.args.manifest_sha256 = sha(self.manifest_path)
        with self.assertRaises(entry.BootstrapError):
            self.prepared()
        self.publish()
        original_snapshot = dict(self.raw['snapshot'])
        broken = self.root / 'broken.sqlite3'
        broken.write_bytes(b'fictional-not-a-sqlite-file')
        self.raw['snapshot'] = dict(original_snapshot, path=broken.name,
                                    sha256=sha(broken), size=broken.stat().st_size)
        self.manifest_path.write_text(json.dumps(self.raw), encoding='utf-8')
        with self.assertRaises((runtime.SafeStartError, sqlite3.DatabaseError)):
            runtime.DiagnosticRuntime(self.manifest())
        released = broken.with_suffix('.released')
        broken.rename(released)
        self.raw['snapshot'] = original_snapshot
        self.publish()

    def test_s13_normal_path_unchanged(self):
        expected = {
            'server.py': 'a67cc2f8564e1533c3fb1304673a0a626a610444c0486c291a3fc0845d7e8fb2',
            'team_engine.py': '906954f2b0f6df06e5421da50fea36c930370789098ddea9a638249938b4a77c',
            'team_store.py': '40f1f830bf07cf4561bc4ab344c599ddd1afe60e3535aaaf034ba6f72ab1d7d5',
            'web/app.js': '9af3e01ce6b04b4e3efeabd4308032ab5de6652c3ede7e81adf52ed4301be321',
        }
        for name, digest in expected.items():
            self.assertEqual(sha(SOURCE / name), digest)
            text = (SOURCE / name).read_text(encoding='utf-8')
            self.assertNotIn('safe_start_runtime', text)
            self.assertNotIn('safe_server', text)
        tree = ast.parse((SOURCE / 'server.py').read_text(encoding='utf-8'))
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
        self.assertIn('8790', ast.unparse(main))
        self.assertIn('engine.loop', ast.unparse(main))
        self.assertIn('usage.start', ast.unparse(main))
        # No normal module is imported or started to assess this boundary.

    def test_s14_no_in_process_mode_escape(self):
        manifest = self.manifest()
        with self.assertRaises(runtime.SafeStartError):
            manifest.allowed_job_ids = (FOREIGN,)
        with self.assertRaises(TypeError):
            manifest.source_hashes['safe_server.py'] = '0' * 64
        with self.opened() as value:
            with self.assertRaises(AttributeError):
                value.mode = 'normal'
            with self.assertRaises(runtime.SafeStartError):
                value.resume()
        with patch.dict(os.environ, {'SAIKURU_SAFE_START': '0'}):
            self.assertEqual(self.prepared()['raw']['mode'], 'safe')
        with self.http_fixture() as server:
            self.assertEqual(self.request(server, '/api/safe-start/mode', 'POST')[0], 403)
            self.assertEqual(self.request(server, '/api/safe-start/status')[0], 200)

    def test_s15_start_stop_and_failure_cleanup(self):
        before = sha(self.snapshot)
        value = runtime.DiagnosticRuntime(self.manifest())
        try:
            with self.assertRaises(OSError):
                with self.snapshot.open('r+b'):
                    pass
        finally:
            value.close()
            value.close()
        renamed = self.snapshot.with_suffix('.released')
        self.snapshot.rename(renamed)
        renamed.rename(self.snapshot)
        self.assertEqual(sha(self.snapshot), before)
        with self.http_fixture() as server:
            self.assertEqual(self.request(server, '/health')[0], 200)
        self.assertEqual(sha(self.snapshot), before)
        self.raw['snapshot']['sha256'] = '0' * 64
        self.manifest_path.write_text(json.dumps(self.raw), encoding='utf-8')
        with self.assertRaises(runtime.SafeStartError):
            runtime.DiagnosticRuntime(self.manifest())
        self.snapshot.rename(renamed)
        renamed.rename(self.snapshot)


if __name__ == '__main__':
    unittest.main()
