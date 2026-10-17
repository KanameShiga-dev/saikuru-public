"""Claude sign-in resilience (2026-10-15): the login is judged from its file, a release is not undone by a stale
status, only one Claude task starts near a refresh, and an empty or old stored token is reported."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from team_claude_auth import REFRESH_MARGIN, TOKEN_WARN_AGE, ClaudeAuth, login_file_state

TOKEN = 'sk-ant-oat01-' + 'x' * 60   # fixture value, not a real token


def write_login(folder, access='a' * 40, refresh='r' * 40, expires_in=8 * 3600, refresh_in=30 * 86400, now=None):
    now = time.time() if now is None else now
    body = {'claudeAiOauth': {'accessToken': access, 'refreshToken': refresh,
                              'expiresAt': int((now + expires_in) * 1000) if expires_in is not None else 0,
                              'refreshTokenExpiresAt': int((now + refresh_in) * 1000)}}
    (Path(folder) / '.credentials.json').write_text(json.dumps(body), encoding='utf-8')


class LoginFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = self.temp.name

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_file_is_logged_out(self):
        self.assertIs(login_file_state(self.dir)['logged_in'], False)

    def test_valid_login(self):
        write_login(self.dir)
        self.assertIs(login_file_state(self.dir)['logged_in'], True)

    def test_expired_access_with_valid_refresh_is_still_logged_in(self):
        write_login(self.dir, expires_in=-60)
        self.assertIs(login_file_state(self.dir)['logged_in'], True)

    def test_wiped_login_is_logged_out(self):
        # What Claude Code wrote on 10-15 10:38: both tokens empty, expiresAt 0, refresh expiry still in the future.
        write_login(self.dir, access='', refresh='', expires_in=None)
        self.assertIs(login_file_state(self.dir)['logged_in'], False)

    def test_unreadable_file_is_unknown(self):
        (Path(self.dir) / '.credentials.json').write_text('{broken', encoding='utf-8')
        self.assertIsNone(login_file_state(self.dir)['logged_in'])


class AuthStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = self.temp.name
        self.token = None
        self.state = {'state': 'missing', 'written_at': None}
        self.told = []
        self.auth = ClaudeAuth(self.dir, notify=self.told.append, reader=lambda: self.token,
                               state_reader=lambda: self.state)

    def tearDown(self):
        self.temp.cleanup()

    def test_logged_in_is_read_live_so_a_release_is_not_undone(self):
        write_login(self.dir, access='', refresh='', expires_in=None)
        self.assertIs(self.auth.logged_in(), False)
        self.assertTrue(self.auth.on_failure('Failed to authenticate'))
        write_login(self.dir)                                    # the user signs in again
        mtime = Path(self.dir, '.credentials.json').stat().st_mtime
        self.auth.hold['credentials_mtime'] = mtime - 10         # the file changed after the hold began
        self.assertEqual(self.auth.refresh(), 'released')
        self.assertIs(self.auth.logged_in(), True)               # no stale "logged out" left to re-hold the tasks
        self.assertTrue(self.auth.available(self.auth.logged_in()))

    def test_stored_token_counts_as_logged_in(self):
        self.token = TOKEN
        self.assertIs(self.auth.logged_in(), True)

    def test_refresh_window(self):
        write_login(self.dir, expires_in=REFRESH_MARGIN / 2)
        self.assertTrue(self.auth.refresh_window())
        write_login(self.dir, expires_in=3600)
        self.assertFalse(self.auth.refresh_window())
        write_login(self.dir, expires_in=-60)
        self.assertTrue(self.auth.refresh_window())
        self.token = TOKEN                                       # a stored token never refreshes
        self.auth._read_at = None
        self.assertFalse(self.auth.refresh_window())

    def test_empty_stored_token_is_reported_once(self):
        self.state = {'state': 'invalid', 'written_at': time.time()}
        problem = self.auth.check_token()
        self.assertIn('空か、形が正しくありません', problem)
        self.auth.check_token()
        self.assertEqual(len(self.told), 1)
        self.state = {'state': 'ok', 'written_at': time.time()}
        self.assertIsNone(self.auth.check_token())

    def test_old_stored_token_is_reported(self):
        self.state = {'state': 'ok', 'written_at': time.time() - TOKEN_WARN_AGE - 60}
        self.assertIn('約11か月', self.auth.check_token())
        self.state = {'state': 'ok', 'written_at': time.time() - 86400}
        self.assertIsNone(self.auth.token_problem())

    def test_missing_token_is_not_a_problem(self):
        self.assertIsNone(self.auth.token_problem())

    def test_only_the_old_roaming_name_left_is_reported(self):
        self.state = {'state': 'missing', 'written_at': None, 'persist': None, 'legacy': True}
        self.assertIn('保存先を', self.auth.token_problem())

    def test_roaming_copy_is_reported(self):
        # 10-15: a copy that roams with the domain profile was replaced by an old empty one.
        self.state = {'state': 'ok', 'written_at': time.time(), 'persist': 3}
        self.assertIn('同期される形', self.auth.token_problem())
        self.state = {'state': 'ok', 'written_at': time.time(), 'persist': 2}
        self.assertIsNone(self.auth.token_problem())


class AdapterTests(unittest.TestCase):
    """Inside 采来 the worker start does not run `claude auth status` when the login file answers."""

    def run_adapter(self, auth):
        import team_adapters
        from types import SimpleNamespace
        ctx = SimpleNamespace(task={'id': 't', 'profile': {'model': 'sonnet', 'effort': 'medium'}},
                              engine=SimpleNamespace(claude_auth=auth), command=['claude'], check=lambda: None,
                              event=lambda *a: None)
        with mock.patch.object(team_adapters.subprocess, 'run') as status, \
                mock.patch.object(team_adapters, 'Process', side_effect=RuntimeError('stop before the model')):
            try:
                team_adapters.ClaudeAdapter().run(ctx, 'PROMPT', {})
            except Exception as exc:   # noqa: BLE001 - the fixture stops the run right after the sign-in check
                error = exc
        return status, error

    def test_logged_in_file_skips_status_command(self):
        with tempfile.TemporaryDirectory() as folder:
            write_login(folder)
            status, error = self.run_adapter(ClaudeAuth(folder, reader=lambda: None))
        status.assert_not_called()
        self.assertNotEqual(getattr(error, 'code', None), 'claude_auth')

    def test_wiped_file_fails_as_sign_in_error_without_status_command(self):
        with tempfile.TemporaryDirectory() as folder:
            write_login(folder, access='', refresh='', expires_in=None)
            status, error = self.run_adapter(ClaudeAuth(folder, reader=lambda: None))
        status.assert_not_called()
        self.assertEqual(getattr(error, 'code', None), 'claude_auth')


class EngineGateTests(unittest.TestCase):
    """Without a stored token, only one Claude task starts while the shared login is due for a refresh."""

    def setUp(self):
        from team_config import default_config
        from team_engine import Engine
        from team_store import Store
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        projects = [root / 'p1', root / 'p2']
        for project in projects:
            project.mkdir()
        self.store = Store(root / 'data')
        self.addCleanup(self.store.db.close)
        config = default_config()
        config['approved_roots'] = [str(p) for p in projects]
        config['roles']['builder'] = 'claude-standard'
        config['max_parallel_projects'] = 4
        self.engine = Engine(self.store, config, {'codex': ['fixture'], 'claude': ['fixture']}, 'http://127.0.0.1:1')
        self.addCleanup(self.engine.handoff.close)
        self.addCleanup(self.engine.shutdown.set)
        self.engine._run = lambda task: None                     # never start a CLI in this test
        self.login = root / 'login'
        self.login.mkdir()
        self.engine.claude_auth = ClaudeAuth(str(self.login), reader=lambda: None)
        self.engine.claude_logged_in = self.engine.claude_auth.logged_in
        self.tasks = []
        for project in projects:
            job = self.engine.create_job(project.name, 'local goal', str(project), False)
            for t in self.store.all('task'):
                if t['job_id'] == job['id']:
                    self.store.update(t['id'], status='cancelled')
            self.store.update(job['id'], status='running')
            self.tasks.append(self.engine.new_task(job, 'build', 'build', 'builder'))

    def statuses(self):
        return [self.store.get(t['id'])['status'] for t in self.tasks]

    def test_one_claude_start_near_refresh(self):
        write_login(self.login, expires_in=60)                   # due for a refresh
        self.engine.tick()
        self.assertEqual(sorted(self.statuses()), ['queued', 'running'])
        self.engine.tick()
        self.assertEqual(sorted(self.statuses()), ['queued', 'running'])

    def test_both_start_when_login_is_fresh(self):
        write_login(self.login, expires_in=3600)
        self.engine.tick()
        self.assertEqual(self.statuses(), ['running', 'running'])


if __name__ == '__main__':
    unittest.main()
