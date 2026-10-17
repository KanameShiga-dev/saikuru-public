"""Startup must not refresh regular OAuth: no `claude auth status` at all (2026-10-15). The sign-in shown comes from
the stored token, or from the login file when no token is stored."""
import tempfile
import unittest
from unittest.mock import patch

import server


class ClaudeStartupAuthTests(unittest.TestCase):
    def test_initial_auth_probe_never_starts_the_status_command(self):
        for configured, file_login in ((True, None), (False, True), (False, False)):
            with self.subTest(configured=configured, file_login=file_login), tempfile.TemporaryDirectory() as directory:
                calls = []

                class FakeAuth:
                    def __init__(self, *args, **kwargs): pass
                    def configured(self): return configured
                    def logged_in(self): return True if configured else file_login
                    def token_problem(self): return None
                    def check_token(self): return None
                    def snapshot(self): return {'held': False}

                def fake_status(commands, check_auth=True):
                    calls.append(check_auth)
                    return {'claude': {'available': True}}

                with patch('team_claude_auth.ClaudeAuth', FakeAuth), \
                     patch.object(server, 'discover', return_value={'claude': ['synthetic-cli']}), \
                     patch.object(server, 'environment_status', side_effect=fake_status):
                    app = server.App(directory, 0)
                    try:
                        self.assertEqual(calls, [False])
                        self.assertEqual(app.health['claude'].get('authenticated'), True if configured else file_login)
                    finally:
                        app.engine.shutdown.set()
                        app.consultations.shutdown()
                        app.consultations.db.close()
                        app.engine.handoff.close()
                        app.store.db.close()
                        app.ledger.db.close()
                        app.data_lock.close()


if __name__ == '__main__': unittest.main()
