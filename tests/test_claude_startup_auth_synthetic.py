"""Startup must not refresh regular OAuth when a stored token is configured."""
import tempfile
import unittest
from unittest.mock import patch

import server


class ClaudeStartupAuthTests(unittest.TestCase):
    def test_initial_auth_probe_depends_on_stored_token(self):
        for configured, expected_check_auth in ((True, False), (False, True)):
            with self.subTest(configured=configured), tempfile.TemporaryDirectory() as directory:
                calls = []

                class FakeAuth:
                    def __init__(self, *args, **kwargs): pass
                    def configured(self): return configured
                    def snapshot(self): return {'held': False}

                def fake_status(commands, check_auth=True):
                    calls.append(check_auth)
                    return {'claude': {'available': True}}

                with patch('team_claude_auth.ClaudeAuth', FakeAuth), \
                     patch.object(server, 'discover', return_value={'claude': ['synthetic-cli']}), \
                     patch.object(server, 'environment_status', side_effect=fake_status):
                    app = server.App(directory, 0)
                    try:
                        self.assertEqual(calls, [expected_check_auth])
                        self.assertEqual(app.health['claude'].get('authenticated'), True if configured else None)
                    finally:
                        app.engine.shutdown.set()
                        app.consultations.shutdown()
                        app.consultations.db.close()
                        app.engine.handoff.close()
                        app.store.db.close()
                        app.ledger.db.close()
                        app.data_lock.close()


if __name__ == '__main__': unittest.main()
