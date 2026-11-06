"""2026-10-09: GitHub Copilot as a builder. Its writes, edits and commands go through copilot_work_tools, which asks
采来's /worker/tool (as Write / Edit / Bash, like a Claude builder's hook) and performs an operation only when allowed."""
import io
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import copilot_work_tools as work
from team_adapters import CopilotAdapter, COPILOT_WORK_TOOLS, ProviderError


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        (self.root / 'app.py').write_text('print("a")\nprint("a")\n', encoding='utf-8')
        self.out = io.StringIO()
        self.server = work.Server(self.root, {'endpoint': 'http://127.0.0.1:1', 'task_id': 't', 'token': 'x'})
        self.asked = []

    def call(self, name, args, allow=True, request_id=1):
        def fake_ask(config, tool, data):
            self.asked.append((tool, data))
            if not allow:
                raise work.Denied('采来が拒否')
            return 'ok'
        with mock.patch.object(work, 'ask', fake_ask), mock.patch('sys.stdout', self.out):
            self.server.call(request_id, name, args)
        answer = json.loads(self.out.getvalue().strip().splitlines()[-1])
        return answer['result']

    def test_write_only_when_allowed(self):
        result = self.call('write_file', {'path': 'new.txt', 'content': 'x\r\ny'})
        self.assertFalse(result.get('isError'))
        self.assertEqual((self.root / 'new.txt').read_bytes(), b'x\r\ny')
        self.assertEqual(self.asked[0][0], 'Write')
        self.assertEqual(self.asked[0][1]['file_path'], str(self.root / 'new.txt'))
        result = self.call('write_file', {'path': 'denied.txt', 'content': 'x'}, allow=False)
        self.assertTrue(result['isError'])
        self.assertFalse((self.root / 'denied.txt').exists())

    def test_edit_rules(self):
        result = self.call('edit_file', {'path': 'app.py', 'old_string': 'print("a")', 'new_string': 'print("b")'})
        self.assertTrue(result['isError'])  # two matches without replace_all
        result = self.call('edit_file', {'path': 'app.py', 'old_string': 'print("a")', 'new_string': 'print("b")', 'replace_all': True})
        self.assertFalse(result.get('isError'))
        self.assertEqual((self.root / 'app.py').read_text(encoding='utf-8'), 'print("b")\nprint("b")\n')
        self.assertEqual(self.asked[-1][0], 'Edit')

    def test_outside_project_is_refused_before_asking(self):
        result = self.call('write_file', {'path': '../outside.txt', 'content': 'x'})
        self.assertTrue(result['isError'])
        self.assertEqual(self.asked, [])

    def test_read_failure_reports_actionable_reason(self):
        result = self.call('read_file', {'path': '.local/hidden.txt'})
        self.assertTrue(result['isError'])
        self.assertIn('読み取り対象外', result['content'][0]['text'])

    def test_cancelled_request_is_not_performed_after_approval(self):
        self.server.cancelled.add(7)
        result = self.call('write_file', {'path': 'late.txt', 'content': 'x'}, request_id=7)
        self.assertTrue(result['isError'])
        self.assertFalse((self.root / 'late.txt').exists())

    @unittest.skipUnless(os.name == 'nt', 'PowerShell')
    def test_command_runs_in_the_project_only_when_allowed(self):
        result = self.call('run_command', {'command': 'Write-Output (Get-Location).Path'})
        value = json.loads(result['content'][0]['text'])
        self.assertEqual(value['exit_code'], 0)
        self.assertIn(self.root.name, value['output'])
        self.assertEqual(self.asked[-1], ('Bash', {'command': 'Write-Output (Get-Location).Path', 'shell': 'powershell'}))
        value = json.loads(self.call('run_command', {'command': "Write-Output 'こんにちは采来'"})['content'][0]['text'])
        self.assertEqual(value['output'].strip(), 'こんにちは采来')  # not garbled by the console code page
        # A Python child prints in cp932 when piped unless told otherwise (采来's server has no PYTHONIOENCODING).
        (self.root / 'say.py').write_text("print('語数: 3')\n", encoding='utf-8')
        with mock.patch.dict(os.environ):
            for name in ('PYTHONIOENCODING', 'PYTHONUTF8'):
                os.environ.pop(name, None)
            value = json.loads(self.call('run_command', {'command': 'python say.py'})['content'][0]['text'])
        self.assertEqual(value['output'].strip(), '語数: 3')
        result = self.call('run_command', {'command': 'New-Item x.txt'}, allow=False)
        self.assertTrue(result['isError'])
        self.assertFalse((self.root / 'x.txt').exists())

    def test_reads_use_the_read_broker(self):
        result = self.call('read_file', {'path': 'app.py'})
        self.assertIn('print', result['content'][0]['text'])
        self.assertEqual(self.asked, [])

    def test_powershell_modules_are_listed_read_and_searchable(self):
        source = self.root / 'src'
        source.mkdir()
        for name in ('Search.psm1', 'Search.psd1'):
            (source / name).write_text('# sample-module\n', encoding='utf-8')
        listing = json.loads(self.call('list_files', {})['content'][0]['text'])
        self.assertIn(str(Path('src', 'Search.psm1')), listing['files'])
        for name in ('Search.psm1', 'Search.psd1'):
            result = self.call('read_file', {'path': str(Path('src', name))})
            self.assertFalse(result.get('isError'), result)
            self.assertIn('sample-module', result['content'][0]['text'])
        matches = json.loads(self.call('search_files', {'query': 'sample-module'})['content'][0]['text'])
        self.assertEqual(len(matches['matches']), 2)
        self.assertEqual(self.asked, [])

    def test_data_requires_file_and_content_specific_approval(self):
        data = self.root / 'data'
        data.mkdir()
        path = data / 'prefectures.json'
        path.write_text('{"name":"Tokyo"}', encoding='utf-8')
        listing = json.loads(self.call('list_files', {})['content'][0]['text'])
        relative = str(Path('data', 'prefectures.json'))
        self.assertNotIn(relative, listing['files'])
        self.assertIn(relative, listing['approval_required_files'])
        denied = self.call('read_file', {'path': relative}, allow=False)
        self.assertTrue(denied['isError'])
        self.assertNotIn('Tokyo', denied['content'][0]['text'])
        self.asked.clear()
        for _ in range(2):
            result = self.call('read_file', {'path': relative})
            self.assertFalse(result.get('isError'), result)
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(self.asked[0][0], 'ReadProjectData')
        path.write_text('{"name":"Osaka"}', encoding='utf-8')
        self.call('read_file', {'path': relative})
        self.assertEqual(len(self.asked), 2)
        self.assertNotEqual(self.asked[0][1]['sha256'], self.asked[1][1]['sha256'])
        with self.assertRaises(ValueError):
            work.reader.execute(self.root, 'read_file', {'path': relative})
        matches = json.loads(self.call('search_files', {'query': 'Osaka'})['content'][0]['text'])
        self.assertEqual(matches['matches'], [])

    def test_data_changed_during_approval_is_not_returned(self):
        (self.root / 'data').mkdir()
        path = self.root / 'data' / 'items.json'
        path.write_text('{"name":"before"}', encoding='utf-8')
        def change_during_approval(*args):
            path.write_text('{"name":"after"}', encoding='utf-8')
            return 'ok'
        with mock.patch.object(work, 'ask', side_effect=change_during_approval), mock.patch('sys.stdout', self.out):
            self.server.call(1, 'read_file', {'path': str(Path('data', 'items.json'))})
        result = json.loads(self.out.getvalue().strip().splitlines()[-1])['result']
        self.assertTrue(result['isError'])
        self.assertNotIn('after', result['content'][0]['text'])

    def test_data_guards_still_refuse_runtime_settings_and_sensitive_content(self):
        (self.root / 'data').mkdir()
        for name in ('config.yaml', 'config.json', 'auth.json', 'records.json'):
            path = self.root / 'data' / name
            path.write_text('{"name":"example"}', encoding='utf-8')
        for name in ('config.yaml', 'config.json', 'auth.json'):
            result = self.call('read_file', {'path': str(Path('data', name))})
            self.assertTrue(result['isError'], result)
        with mock.patch.object(work.reader, 'screen_model_read', side_effect=ValueError('protected')):
            result = self.call('read_file', {'path': str(Path('data', 'records.json'))})
        self.assertTrue(result['isError'])
        self.assertEqual(self.asked, [])
        app = Path(work.reader.__file__).resolve().parent
        with self.assertRaises(ValueError):
            work.reader.safe_path(app, str(Path('data', 'project_ledger.sqlite3')), allow_data=True)

    def test_cancelled_data_read_does_not_request_or_return_content(self):
        (self.root / 'data').mkdir()
        (self.root / 'data' / 'items.json').write_text('{"sample":"value"}', encoding='utf-8')
        self.server.cancelled.add(1)
        result = self.call('read_file', {'path': str(Path('data', 'items.json'))})
        self.assertTrue(result['isError'])
        self.assertEqual(self.asked, [])
        self.assertNotIn('sample', result['content'][0]['text'])

    def test_parallel_reads_share_one_consent(self):
        (self.root / 'data').mkdir()
        (self.root / 'data' / 'items.json').write_text('{"sample":"value"}', encoding='utf-8')
        entered, release = threading.Event(), threading.Event()
        calls, replies = [], []
        def wait_for_consent(*args):
            calls.append(args)
            entered.set()
            if not release.wait(5):
                raise work.Denied('test consent timeout')
            return 'approved'
        threads = [threading.Thread(target=self.server.call, args=(
            i, 'read_file', {'path': str(Path('data', 'items.json'))})) for i in (1, 2)]
        with mock.patch.object(work, 'ask', side_effect=wait_for_consent), \
                mock.patch.object(self.server, 'send', side_effect=replies.append):
            try:
                for thread in threads:
                    thread.start()
                self.assertTrue(entered.wait(5))
            finally:
                release.set()
                for thread in threads:
                    thread.join(5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(replies), 2)
        self.assertTrue(all(not reply['result'].get('isError') for reply in replies))


class AskTest(unittest.TestCase):
    """ask() sends the same request as the Claude hook and treats anything but allow=true as a denial."""

    def serve(self, answer):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(handler):
                body = json.loads(handler.rfile.read(int(handler.headers['Content-Length'])))
                seen.append((handler.path, handler.headers['Authorization'], body))
                data = json.dumps(answer).encode()
                handler.send_response(200)
                handler.send_header('Content-Type', 'application/json')
                handler.send_header('Content-Length', str(len(data)))
                handler.end_headers()
                handler.wfile.write(data)
            def log_message(handler, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return 'http://127.0.0.1:%d' % server.server_port, seen

    def test_request_shape_and_decisions(self):
        endpoint, seen = self.serve({'allow': True, 'note': 'ok'})
        config = {'endpoint': endpoint, 'task_id': 'task1', 'token': 'secret', 'root': 'C:/p'}
        self.assertEqual(work.ask(config, 'Bash', {'command': 'dir'}), 'ok')
        path, auth, body = seen[0]
        self.assertEqual((path, auth), ('/worker/tool', 'Bearer secret'))
        self.assertEqual(body, {'task_id': 'task1', 'tool': 'Bash', 'input': {'command': 'dir'}, 'cwd': 'C:/p', 'source': 'copilot'})
        endpoint, _ = self.serve({'allow': False, 'note': '送信の防御'})
        with self.assertRaisesRegex(work.Denied, '送信の防御'):
            work.ask(dict(config, endpoint=endpoint), 'Bash', {'command': 'curl x'})
        with self.assertRaises(work.Denied):
            work.ask(dict(config, endpoint='http://127.0.0.1:1', timeout=2), 'Bash', {'command': 'dir'})


class EngineDecisionTest(unittest.TestCase):
    """The real engine decides: a normal edit in the project runs (with a backup), a protected file is refused."""

    def setUp(self):
        from team_config import default_config
        from team_engine import Context, Engine
        from team_store import Store
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name, 'project')
        self.project.mkdir()
        (self.project / 'AGENTS.md').write_text('rules', encoding='utf-8')
        self.store = Store(self.temp.name)
        config = default_config()
        config['approved_roots'] = [str(self.project)]
        self.engine = Engine(self.store, config, {'codex': ['fixture'], 'claude': ['fixture'], 'copilot': ['fixture']},
                             'http://127.0.0.1:1')
        job = self.engine.create_job('開発', '作る', str(self.project), True)
        task = self.engine.new_task(job, '実装', '実装する', 'builder')
        self.ctx = Context(self.engine, task)
        self.engine.active[task['id']] = self.ctx
        engine = self.engine
        class Handler(BaseHTTPRequestHandler):
            def do_POST(handler):
                body = json.loads(handler.rfile.read(int(handler.headers['Content-Length'])))
                token = handler.headers['Authorization'].removeprefix('Bearer ')
                answer = (engine.tool_result(token, body) if handler.path == '/worker/tool-result'
                          else engine.tool_request(token, body))
                data = json.dumps(answer).encode()
                handler.send_response(200)
                handler.send_header('Content-Length', str(len(data)))
                handler.end_headers()
                handler.wfile.write(data)
            def log_message(handler, *args):
                pass
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.server = work.Server(self.project.resolve(), {'endpoint': 'http://127.0.0.1:%d' % self.http.server_port,
                                                           'task_id': task['id'], 'token': self.ctx.token, 'timeout': 10})

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.engine.active.clear()
        self.engine.shutdown.set()
        self.engine.handoff.close()
        self.store.db.close()
        self.temp.cleanup()

    def call(self, name, args):
        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            self.server.call(1, name, args)
        return json.loads(out.getvalue().strip().splitlines()[-1])['result']

    def test_normal_write_runs_and_protected_file_is_refused(self):
        result = self.call('write_file', {'path': 'src/main.py', 'content': 'print(1)\n'})
        self.assertFalse(result.get('isError'), result)
        self.assertEqual((self.project / 'src' / 'main.py').read_text(encoding='utf-8'), 'print(1)\n')
        result = self.call('write_file', {'path': 'AGENTS.md', 'content': 'changed'})
        self.assertTrue(result['isError'])
        self.assertIn('設定・方針', result['content'][0]['text'])
        self.assertEqual((self.project / 'AGENTS.md').read_text(encoding='utf-8'), 'rules')

    @unittest.skipUnless(os.name == 'nt', 'PowerShell')
    def test_command_result_is_recorded_as_review_evidence(self):
        # 2026-10-09: the reviewer gets the exit code and output 采来 itself saw, not only the assignee's report.
        self.engine.config['automatic_operations'] = True
        result = self.call('run_command', {'command': 'python -c "print(42)"'})
        self.assertFalse(result.get('isError'), result)
        from team_security_audit import command_evidence
        evidence = command_evidence(self.store, self.store.get(self.ctx.task['job_id']))
        self.assertIn('python -c "print(42)" → 終了コード 0', evidence)
        self.assertIn('42', evidence)

    def test_wrong_token_is_refused(self):
        self.server.config['token'] = 'not-the-token'
        result = self.call('write_file', {'path': 'x.py', 'content': 'x'})
        self.assertTrue(result['isError'])
        self.assertFalse((self.project / 'x.py').exists())

    def test_project_data_read_forces_manual_approval_even_with_automatic_operations(self):
        self.engine.config['automatic_operations'] = True
        (self.project / 'data').mkdir()
        path = self.project / 'data' / 'items.json'
        path.write_text('{"sample":"value"}', encoding='utf-8')
        with mock.patch.object(self.ctx, 'approve', return_value={'allow': True, 'note': 'confirmed'}) as approve:
            result = self.call('read_file', {'path': str(Path('data', 'items.json'))})
        self.assertFalse(result.get('isError'), result)
        payload = approve.call_args.args[0]
        self.assertTrue(payload['force_manual'])
        self.assertEqual(payload['operation'], 'ReadProjectData')
        self.assertEqual(payload['details']['file_path'], str(path.resolve()))
        self.assertNotIn('sample', json.dumps(payload))

    def test_unverified_data_request_is_denied_before_approval(self):
        (self.project / 'data').mkdir()
        path = self.project / 'data' / 'items.json'
        path.write_text('{"sample":"value"}', encoding='utf-8')
        with mock.patch.object(self.ctx, 'approve') as approve:
            result = self.engine.tool_request(self.ctx.token, {
                'task_id': self.ctx.task['id'], 'source': 'copilot', 'tool': 'ReadProjectData',
                'input': {'file_path': str(path), 'sha256': 'wrong'}})
        self.assertFalse(result['allow'])
        approve.assert_not_called()

    def test_one_real_decision_releases_parallel_reads(self):
        self.engine.config['automatic_operations'] = True
        (self.project / 'data').mkdir()
        (self.project / 'data' / 'items.json').write_text('{"sample":"value"}', encoding='utf-8')
        replies = []
        threads = [threading.Thread(target=self.server.call, args=(
            i, 'read_file', {'path': str(Path('data', 'items.json'))})) for i in (1, 2)]
        with mock.patch.object(self.server, 'send', side_effect=replies.append):
            for thread in threads:
                thread.start()
            try:
                deadline = time.monotonic() + 5
                pending = []
                while time.monotonic() < deadline:
                    pending = [a for a in self.store.for_job('approval', self.ctx.task['job_id'])
                               if a['status'] == 'pending' and a['kind'] == 'tool']
                    if pending:
                        break
                    time.sleep(.02)
                self.assertEqual(len(pending), 1)
                self.engine.decide(pending[0]['id'], True, 'synthetic explicit consent')
            finally:
                for thread in threads:
                    thread.join(12)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(len(replies), 2)
            self.assertTrue(all(not reply['result'].get('isError') for reply in replies), replies)
            self.server.call(3, 'read_file', {'path': str(Path('data', 'items.json'))})
        approvals = [a for a in self.store.for_job('approval', self.ctx.task['job_id']) if a['kind'] == 'tool']
        self.assertEqual([(a['kind'], a['status']) for a in approvals], [('tool', 'approved')])
        self.assertEqual(len(replies), 3)
        self.assertFalse(replies[-1]['result'].get('isError'), replies[-1])


class AdapterTest(unittest.TestCase):
    def test_builder_gets_the_work_tools_and_the_token_stays_off_the_command_line(self):
        definition = SimpleNamespace(sandbox='workspace-write', tools=('Read', 'Edit', 'Bash'), name='common-implementer')
        project = tempfile.TemporaryDirectory()
        self.addCleanup(project.cleanup)
        ctx = SimpleNamespace(task={'id': 't1', 'role': 'builder', 'profile': {'model': 'gpt-6-luna', 'effort': 'low'}},
                              agent_definition=definition, text_only=False, attachment_ids=[], command=['copilot'],
                              agent_system_instructions='', agent_started=lambda *a: None, event=lambda *a: None,
                              project=project.name, endpoint='http://127.0.0.1:9', token='run-token-123',
                              approval_timeout=60)
        seen = {}
        def fake(adapter, ctx, command, text, cwd, env, allowed, baseline=None):
            seen.update(command=command, text=text, allowed=allowed)
            token_file = Path(json.loads(command[command.index('--additional-mcp-config') + 1])
                              ['mcpServers']['project_work']['args'][-1])
            seen['token'] = json.loads(token_file.read_text(encoding='utf-8'))['token']
            return json.dumps({'status': 'done', 'summary': 's', 'checks': ['c'], 'pending_checks': [], 'question': ''})
        from team_engine import RESULT_SCHEMA
        with mock.patch.object(CopilotAdapter, '_call', fake):
            CopilotAdapter().run(ctx, 'x', RESULT_SCHEMA)
        self.assertEqual(seen['allowed'], ['project_work-' + n for n in COPILOT_WORK_TOOLS])
        self.assertEqual(seen['token'], 'run-token-123')
        self.assertFalse(any('run-token-123' in part for part in seen['command']))
        for denied in ('--deny-tool=shell', '--deny-tool=write', '--deny-tool=url'):
            self.assertIn(denied, seen['command'])

    def test_researcher_and_artifact_are_refused(self):
        definition = SimpleNamespace(sandbox='workspace-write', tools=('Read', 'Bash'), name='common-explorer')
        base = dict(agent_definition=definition, text_only=False, attachment_ids=[], command=['copilot'])
        with self.assertRaises(ProviderError):
            CopilotAdapter().run(SimpleNamespace(task={'role': 'researcher', 'profile': {}}, **base), 'x', {})
        with self.assertRaises(ProviderError):
            CopilotAdapter().run(SimpleNamespace(task={'role': 'builder', 'profile': {}}, artifact_format='claude_design',
                                                 document_scope=True, **base), 'x', {})


if __name__ == '__main__':
    unittest.main()
