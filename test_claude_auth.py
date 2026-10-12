"""Claude sign-in for workers (team_claude_auth, 2026-10-12): long-lived token (plan A) and hold + notify (plan C)."""
import os
import tempfile
import time
import unittest
from pathlib import Path

from team_claude_auth import ENV, REMIND_SECONDS, ClaudeAuth, is_auth_error, read_stored_token

# Fictional token, assembled at run time so secret scanners do not mistake the source for a real key.
TOKEN = '-'.join(('sk', 'ant', 'oat01', 'x' * 40))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class TokenTests(unittest.TestCase):
    def test_missing_credential_is_none(self):
        self.assertIsNone(read_stored_token('saikuru/test-missing-' + str(os.getpid())))

    def test_env_gets_token_only_when_stored(self):
        auth = ClaudeAuth(tempfile.gettempdir(), reader=lambda: TOKEN)
        env = auth.env({'PATH': 'x'})
        self.assertEqual(env[ENV], TOKEN)
        self.assertTrue(auth.configured())
        none = ClaudeAuth(tempfile.gettempdir(), reader=lambda: None)
        self.assertNotIn(ENV, none.env({'PATH': 'x'}))
        self.assertFalse(none.configured())

    def test_snapshot_never_holds_the_token(self):
        auth = ClaudeAuth(tempfile.gettempdir(), reader=lambda: TOKEN)
        auth.on_failure('Failed to authenticate: OAuth session expired')
        self.assertNotIn(TOKEN, repr(auth.snapshot()))
        self.assertNotIn(TOKEN, repr(auth.hold))

    def test_token_is_cached_for_a_minute(self):
        calls, clock = [], Clock()
        auth = ClaudeAuth(tempfile.gettempdir(), reader=lambda: calls.append(1) or TOKEN, clock=clock)
        auth.token(); auth.token()
        self.assertEqual(len(calls), 1)
        clock.now += 61
        auth.token()
        self.assertEqual(len(calls), 2)


class HoldTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.creds = Path(self.temp.name) / '.credentials.json'
        self.creds.write_text('{}', encoding='utf-8')
        self.told, self.clock, self.token = [], Clock(), None
        self.auth = ClaudeAuth(self.temp.name, notify=self.told.append, reader=lambda: self.token, clock=self.clock)

    def tearDown(self):
        self.temp.cleanup()

    def test_auth_errors(self):
        for text in ('Claude Code: Failed to authenticate: OAuth session expired and could not be refreshed',
                     'Claude Code CLIは未ログイン、または認証期限切れです。'):
            self.assertTrue(is_auth_error(text))
        for text in ('テストが不合格', "You've hit your session limit", 'GitHub Copilotの実行が完了しませんでした'):
            self.assertFalse(is_auth_error(text))

    def test_other_failures_do_not_hold(self):
        self.assertFalse(self.auth.on_failure('ValueError: schema'))
        self.assertTrue(self.auth.available(True))
        self.assertEqual(self.told, [])

    def test_sign_in_failure_holds_and_tells_once(self):
        self.assertTrue(self.auth.on_failure('Failed to authenticate'))
        self.assertTrue(self.auth.on_failure('Failed to authenticate'))
        self.assertFalse(self.auth.available(True))       # a stale "logged in" status does not start it again
        self.assertEqual(len(self.told), 1)
        self.assertEqual(self.auth.refresh(), 'holding')

    def test_new_login_releases(self):
        self.auth.on_failure('OAuth session expired')
        later = time.time() + 5
        os.utime(self.creds, (later, later))              # a new login rewrites the credentials file
        self.assertEqual(self.auth.refresh(), 'released')
        self.assertTrue(self.auth.available(True))

    def test_new_token_releases(self):
        self.token = TOKEN
        self.auth.on_failure('Failed to authenticate')
        self.assertEqual(self.auth.refresh(), 'holding')   # same token: still held
        self.token = TOKEN + 'new'
        self.assertEqual(self.auth.refresh(), 'released')

    def test_reminder_while_held(self):
        self.auth.on_failure('Failed to authenticate')
        self.clock.now += REMIND_SECONDS + 1
        self.auth.refresh()
        self.assertEqual(len(self.told), 2)

    def test_notification_failure_never_raises(self):
        auth = ClaudeAuth(self.temp.name, notify=lambda m: 1 / 0, reader=lambda: None)
        self.assertTrue(auth.on_failure('Failed to authenticate'))

    def test_without_token_follows_cli_status(self):
        self.assertTrue(self.auth.available(True))
        self.assertFalse(self.auth.available(False))
        self.token = TOKEN
        self.clock.now += 61                               # the stored token is read again after a minute
        self.assertTrue(self.auth.available(False))       # the stored token is used instead of the login


class EngineTests(unittest.TestCase):
    def setUp(self):
        from team_config import default_config
        from team_engine import Engine
        from team_store import Store
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        config = default_config()
        config['approved_roots'] = [self.temp.name]
        config['roles']['builder'] = 'claude-standard'
        self.engine = Engine(self.store, config, {'codex': ['fixture'], 'claude': ['fixture']}, 'http://127.0.0.1:1')

    def tearDown(self):
        self.engine.shutdown.set()
        self.engine.handoff.close()
        self.store.db.close()
        self.temp.cleanup()

    def test_held_claude_task_stays_queued(self):
        job = self.engine.create_job('sample', 'local goal', self.temp.name, False)
        for t in self.store.all('task'):
            self.store.update(t['id'], status='cancelled')
        task = self.engine.new_task(job, 'build', 'build', 'builder')
        self.store.update(job['id'], status='running')
        self.assertEqual(task['profile']['adapter'], 'claude')
        auth = ClaudeAuth(self.temp.name, reader=lambda: None)
        auth.on_failure('Failed to authenticate')
        self.engine.claude_auth = auth
        self.engine.claude_logged_in = lambda: True
        self.engine.tick()
        self.assertEqual(self.store.get(task['id'])['status'], 'queued')

    def test_first_call_usage_is_kept(self):
        from team_common_agents import load_agent
        from team_engine import Context
        job = self.engine.create_job('sample', 'local goal', self.temp.name, True)
        task = self.engine.new_task(job, 'build', 'build', 'builder')
        ctx = Context(self.engine, task)
        ctx.prepare_agent(load_agent('builder', 'claude', 'common-implementer'))
        first = {'input_tokens': 2, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 900, 'output_tokens': 3}
        ctx.observe_message({'id': 'm1', 'usage': first})
        ctx.observe_message({'id': 'm1', 'usage': dict(first, output_tokens=9)})      # streamed again: last value wins
        ctx.observe_message({'id': 'm2', 'usage': dict(first, cache_read_input_tokens=900)})
        self.assertEqual(ctx.agent_run['first_call_usage']['output_tokens'], 9)
        self.assertEqual(ctx.agent_run['first_call_usage']['cache_read_input_tokens'], 0)


if __name__ == '__main__':
    unittest.main()
