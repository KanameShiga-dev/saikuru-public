"""2026-10-09: command results as review evidence (exit code + masked output tail, user decision)."""
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from team_security_audit import RESULT_TAIL, result_message


class ResultMessageTest(unittest.TestCase):
    def test_secrets_are_masked_and_only_the_tail_is_kept(self):
        output = 'x' * 5000 + '\nAPI_KEY=sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789\nmail taro@example.com\nOK'
        message = result_message('python run.py', 0, output)
        self.assertIn('python run.py → 終了コード 0', message)
        self.assertNotIn('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', message)
        self.assertNotIn('taro@example.com', message)
        self.assertIn('［伏せ字］', message)
        self.assertLess(len(message), RESULT_TAIL + 400)
        self.assertIn('時間切れ', result_message('sleep', None, '', timed_out=True))
        self.assertIn('終了コード 不明', result_message('x', None, 'y'))


class ClaudeHookTest(unittest.TestCase):
    def test_hook_sends_the_bash_result(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(handler):
                seen.append((handler.path, handler.headers['Authorization'],
                             json.loads(handler.rfile.read(int(handler.headers['Content-Length'])))))
                handler.send_response(200)
                handler.send_header('Content-Length', '2')
                handler.end_headers()
                handler.wfile.write(b'{}')
            def log_message(handler, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        env = dict(os.environ, AGENT_TEAM_TASK='t1', AGENT_TEAM_RUN_TOKEN='tok',
                   AGENT_TEAM_ENDPOINT='http://127.0.0.1:%d' % server.server_port)
        hook = Path(__file__).with_name('result_hook.py')
        data = {'tool_name': 'Bash', 'tool_input': {'command': 'python -m unittest'},
                'tool_response': {'stdout': 'Ran 4 tests\nOK', 'stderr': '', 'interrupted': False}}
        done = subprocess.run([sys.executable, str(hook)], input=json.dumps(data), text=True, env=env,
                              capture_output=True, timeout=30)
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout.strip(), '')  # never changes the tool result
        path, auth, body = seen[0]
        self.assertEqual((path, auth), ('/worker/tool-result', 'Bearer tok'))
        self.assertEqual((body['task_id'], body['tool'], body['command']), ('t1', 'Bash', 'python -m unittest'))
        self.assertIn('Ran 4 tests', body['output'])
        # Other tools and an unreachable 采来 are ignored silently.
        data['tool_name'] = 'Read'
        subprocess.run([sys.executable, str(hook)], input=json.dumps(data), text=True, env=env, timeout=30)
        self.assertEqual(len(seen), 1)
        env['AGENT_TEAM_ENDPOINT'] = 'http://127.0.0.1:1'
        data['tool_name'] = 'Bash'
        done = subprocess.run([sys.executable, str(hook)], input=json.dumps(data), text=True, env=env, timeout=60)
        self.assertEqual(done.returncode, 0)


class ClaudeHookPayloadTest(unittest.TestCase):
    """Payload shapes seen from Claude Code 2.1.289 (probed 2026-10-09)."""

    def test_success_failure_and_interrupt(self):
        sys.path.insert(0, str(Path(__file__).parent))
        import result_hook
        ok = {'hook_event_name': 'PostToolUse', 'tool_name': 'Bash',
              'tool_response': {'stdout': 'ok-probe', 'stderr': '', 'interrupted': False, 'isImage': False}}
        self.assertEqual(result_hook.result_of(ok), (0, 'ok-probe', False))
        failed = {'hook_event_name': 'PostToolUseFailure', 'tool_name': 'Bash', 'error': 'Exit code 3\nfail-probe',
                  'is_interrupt': False}
        self.assertEqual(result_hook.result_of(failed), (3, 'fail-probe', False))
        self.assertEqual(result_hook.result_of(dict(failed, error='Error: Exit code 1\nx'))[0], 1)
        stopped = {'hook_event_name': 'PostToolUseFailure', 'tool_name': 'Bash', 'error': 'Command timed out',
                   'is_interrupt': True}
        self.assertEqual(result_hook.result_of(stopped), (None, 'Command timed out', True))

    def test_both_hook_events_are_registered(self):
        source = Path(__file__).with_name('team_adapters.py').read_text(encoding='utf-8')
        self.assertIn("for event in ('PostToolUse', 'PostToolUseFailure')", source)


class CopilotToolCountTest(unittest.TestCase):
    def test_tool_calls_are_counted_like_claude(self):
        from types import SimpleNamespace
        from unittest import mock
        import team_adapters
        messages = [{'type': 'tool.execution_start', 'data': {'toolName': 'project_work-read_file'}},
                    {'type': 'tool.execution_start', 'data': {'toolName': 'project_work-read_file'}},
                    {'type': 'tool.execution_start', 'data': {'toolName': 'project_work-run_command'}},
                    {'type': 'assistant.message', 'data': {'content': '{}'}}, {'type': 'result', 'exitCode': 0}]
        class FakeProcess:
            def __init__(self, *args):
                self.proc = SimpleNamespace(stdin=io.StringIO(), wait=lambda timeout=None: 0)
                self.stderr = []
            def receive(self, ctx):
                return messages.pop(0)
            def close(self):
                pass
        counted = []
        ctx = SimpleNamespace(event=lambda *a: None, count_tool=counted.append, check=lambda: None)
        with mock.patch.object(team_adapters, 'Process', FakeProcess):
            team_adapters.CopilotAdapter()._call(ctx, ['copilot'], 'x', '.', {}, ['project_work-read_file', 'project_work-run_command'])
        self.assertEqual(counted, ['project_work-read_file', 'project_work-read_file', 'project_work-run_command'])


class CopilotTokensTest(unittest.TestCase):
    def test_usage_file_is_summed_and_removed(self):
        from team_adapters import copilot_tokens
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'usage.json')
            path.write_text(json.dumps({'modelMetrics': {
                'gpt-6-sol': {'usage': {'inputTokens': 1000, 'outputTokens': 200, 'cacheReadTokens': 50, 'cacheWriteTokens': 0, 'reasoningTokens': 30}},
                'gpt-5-mini': {'usage': {'inputTokens': 10, 'outputTokens': 2}}}}), encoding='utf-8')
            self.assertEqual(copilot_tokens(path), {'inputTokens': 1010, 'outputTokens': 202, 'cacheReadTokens': 50,
                                                    'cacheWriteTokens': 0, 'reasoningTokens': 30})
            self.assertFalse(path.exists())
            self.assertIsNone(copilot_tokens(Path(folder, 'missing.json')))


class EngineResultTest(unittest.TestCase):
    def test_wrong_token_is_refused_and_only_bash_is_recorded(self):
        from team_config import default_config
        from team_engine import Context, Engine
        from team_store import Store
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp, 'p')
            project.mkdir()
            store = Store(temp)
            config = default_config()
            config['approved_roots'] = [str(project)]
            engine = Engine(store, config, {'codex': ['fixture'], 'claude': ['fixture']}, 'http://127.0.0.1:1')
            try:
                job = engine.create_job('開発', '作る', str(project), True)
                ctx = Context(engine, engine.new_task(job, '実装', '実装する', 'builder'))
                engine.active[ctx.task['id']] = ctx
                with self.assertRaises(ValueError):
                    engine.tool_result('wrong', {'task_id': ctx.task['id'], 'tool': 'Bash', 'command': 'x'})
                self.assertEqual(engine.tool_result(ctx.token, {'task_id': ctx.task['id'], 'tool': 'Read'}), {'ok': False})
                engine.tool_result(ctx.token, {'task_id': ctx.task['id'], 'tool': 'Bash', 'command': 'pytest',
                                               'exit_code': 1, 'output': '1 failed'})
                from team_security_audit import command_evidence
                self.assertIn('pytest → 終了コード 1', command_evidence(store, job))
                # Copilot tool counts land in agent_run.tool_calls and are saved (same field as Claude).
                ctx.agent_run = {'id': 'run'}
                ctx.count_tool('project_work-write_file')
                ctx.count_tool('project_work-write_file')
                self.assertEqual(store.get(ctx.task['id'])['agent_run']['tool_calls'], {'project_work-write_file': 2})
            finally:
                engine.active.clear()
                engine.shutdown.set()
                engine.handoff.close()
                store.db.close()


if __name__ == '__main__':
    unittest.main()
