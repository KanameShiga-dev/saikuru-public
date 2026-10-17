"""Automatic failover between Claude and GitHub Copilot, both ways (2026-10-15, user decision: Public/Internal projects
only; when the other provider cannot be used either, the task waits or stops)."""
import tempfile
import time
import unittest
from pathlib import Path

from team_claude_auth import ClaudeAuth
from team_security_templates import project_data_class

CLASS_FILE = '# Data Classification — x\n\nProject default: **{}** (UNKNOWN until a person confirms).\n'


class DataClassTests(unittest.TestCase):
    def test_reads_project_default(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(project_data_class(folder))
            for name in ('Public', 'Internal', 'Confidential', 'Restricted'):
                Path(folder, 'DATA_CLASSIFICATION.md').write_text(CLASS_FILE.format(name), encoding='utf-8')
                self.assertEqual(project_data_class(folder), name)
            Path(folder, 'DATA_CLASSIFICATION.md').write_text('no default here', encoding='utf-8')
            self.assertIsNone(project_data_class(folder))


class FailoverTests(unittest.TestCase):
    def setUp(self):
        from team_config import default_config
        from team_engine import Engine
        from team_store import Store
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.project = root / 'p1'
        self.project.mkdir()
        self.store = Store(root / 'data')
        self.addCleanup(self.store.db.close)
        config = default_config()
        config['approved_roots'] = [str(self.project)]
        config['roles']['builder'] = 'claude-standard'
        config['roles']['researcher'] = 'claude-standard'
        config['provider_settings'] = {'enabled': ['claude', 'copilot'], 'copilot_monthly_credits': 30000}
        self.engine = Engine(self.store, config, {'claude': ['fixture'], 'copilot': ['fixture']}, 'http://127.0.0.1:1')
        self.addCleanup(self.engine.handoff.close)
        self.addCleanup(self.engine.shutdown.set)
        self.engine._run = lambda task: None                     # never start a CLI in this test
        self.told = []
        self.auth = ClaudeAuth(str(root / 'login'), notify=self.told.append, reader=lambda: None,
                               state_reader=lambda: {'state': 'missing'})
        self.engine.claude_auth = self.auth
        self.engine.claude_logged_in = lambda: True
        self.snapshot = {}
        self.engine.usage_snapshot = lambda: self.snapshot
        self.job = self.engine.create_job('p1', 'local goal', str(self.project), False)
        for t in self.store.all('task'):
            self.store.update(t['id'], status='cancelled')
        self.store.update(self.job['id'], status='running')
        self.job = self.store.get(self.job['id'])

    def classify(self, name):
        Path(self.project, 'DATA_CLASSIFICATION.md').write_text(CLASS_FILE.format(name), encoding='utf-8')

    def task(self, role='builder'):
        return self.engine.new_task(self.job, 'build', 'build', role)

    def _all_events(self):
        import json
        return [json.loads(b) for (b,) in self.store.db.execute('SELECT body FROM events ORDER BY seq')]

    def test_internal_project_switches_to_copilot_same_family(self):
        self.classify('Internal')
        task = self.task()
        self.assertTrue(self.engine._failover(task, self.job, 'claude_auth'))
        current = self.store.get(task['id'])
        self.assertEqual(current['profile'], {'adapter': 'copilot', 'model': 'claude-sonnet-5.5', 'effort': 'medium'})
        self.assertEqual(current['auto_return']['reason'], 'claude_auth')
        self.assertEqual(current['auto_return']['normal_profile']['adapter'], 'claude')
        self.assertTrue(any(e['type'] == 'model_failover' for e in self._all_events()))
        self.assertEqual(len(self.told), 1)

    def test_public_project_switches(self):
        self.classify('Public')
        self.assertTrue(self.engine._failover(self.task(), self.job, 'usage_limit', time.time() + 3600))

    def test_confidential_restricted_or_unknown_class_waits_and_is_told_once(self):
        for name in ('Confidential', 'Restricted', None):
            with self.subTest(name=name):
                path = Path(self.project, 'DATA_CLASSIFICATION.md')
                if name:
                    self.classify(name)
                elif path.exists():
                    path.unlink()
                self.engine._failover_told = set()
                task = self.task()
                self.assertFalse(self.engine._failover(task, self.job, 'claude_auth'))
                self.assertFalse(self.engine._failover(task, self.job, 'claude_auth'))
                self.assertEqual(self.store.get(task['id'])['profile']['adapter'], 'claude')
                skipped = [e for e in self._all_events() if e['type'] == 'model_failover_skipped']
                self.assertTrue(skipped)
                self.assertIn('自動で送りません', skipped[-1]['message'])

    def test_role_without_copilot_support_waits(self):
        self.classify('Internal')
        self.assertFalse(self.engine._failover(self.task('researcher'), self.job, 'claude_auth'))

    def test_copilot_disabled_waits(self):
        self.classify('Internal')
        self.engine.config['provider_settings']['enabled'] = ['claude']
        self.assertFalse(self.engine._failover(self.task(), self.job, 'claude_auth'))

    def test_copilot_low_credits_waits(self):
        self.classify('Internal')
        self.snapshot = {'copilot': {'status': 'ok', 'stale': False,
                                     'rows': [{'id': 'copilot', 'monthly': {'remaining_percent': 2}}]}}
        self.assertFalse(self.engine._failover(self.task(), self.job, 'claude_auth'))
        self.snapshot['copilot']['rows'][0]['monthly']['remaining_percent'] = 40
        self.assertTrue(self.engine._failover(self.task(), self.job, 'claude_auth'))

    def test_tick_switches_a_held_claude_task_and_restores_after_sign_in(self):
        self.classify('Internal')
        task = self.task()
        self.auth.on_failure('Failed to authenticate')           # Claude held: sign-in lost
        self.engine.tick()
        self.assertEqual(self.store.get(task['id'])['profile']['adapter'], 'copilot')
        self.engine.tick()                                       # the next tick starts it on Copilot
        self.assertEqual(self.store.get(task['id'])['status'], 'running')

    def test_not_yet_started_task_goes_back_when_claude_is_available(self):
        self.classify('Internal')
        task = self.task()
        self.auth.on_failure('Failed to authenticate')
        self.assertTrue(self.engine._failover(task, self.job, 'claude_auth'))
        self.engine._restore_recovered_models()
        self.assertEqual(self.store.get(task['id'])['profile']['adapter'], 'copilot')   # still held: stays on Copilot
        self.auth.release()
        self.engine._restore_recovered_models()
        restored = self.store.get(task['id'])
        self.assertEqual(restored['profile']['adapter'], 'claude')
        self.assertIsNone(restored['auto_return'])

    def copilot_task(self, model='gpt-6-luna'):
        task = self.task()
        return self.store.update(task['id'], profile={'adapter': 'copilot', 'model': model, 'effort': 'medium'})

    def test_copilot_usage_limit_moves_to_claude(self):
        self.classify('Internal')
        for model, expected in (('gpt-6-luna', 'sonnet'), ('claude-opus-5.5', 'opus')):
            with self.subTest(model=model):
                task = self.copilot_task(model)
                self.assertTrue(self.engine._failover(task, self.job, 'usage_limit', time.time() + 600))
                self.assertEqual(self.store.get(task['id'])['profile'], {'adapter': 'claude', 'model': expected, 'effort': 'medium'})

    def test_copilot_task_stays_when_claude_is_not_usable_either(self):
        self.classify('Internal')
        self.auth.on_failure('Failed to authenticate')           # Claude is down too
        task = self.copilot_task()
        self.assertFalse(self.engine._failover(task, self.job, 'usage_limit', time.time() + 600))
        self.assertEqual(self.store.get(task['id'])['profile']['adapter'], 'copilot')

    def test_no_ping_pong_back_to_a_provider_that_is_still_down(self):
        self.classify('Internal')
        task = self.task()
        self.auth.on_failure('Failed to authenticate')
        self.assertTrue(self.engine._failover(task, self.job, 'claude_auth'))        # Claude -> Copilot
        moved = self.store.get(task['id'])
        self.assertFalse(self.engine._failover(moved, self.job, 'usage_limit', time.time() + 600))  # Claude still down
        self.assertEqual(self.store.get(task['id'])['profile']['adapter'], 'copilot')

    def test_copilot_sign_in_failure_moves_to_claude_or_stops(self):
        self.classify('Internal')
        task = self.copilot_task()
        self.store.update(task['id'], failure_code='copilot_auth_wait')
        self.engine.tick()
        self.assertEqual(self.store.get(task['id'])['profile']['adapter'], 'claude')
        other = self.copilot_task()
        self.store.update(other['id'], failure_code='copilot_auth_wait')
        self.auth.on_failure('Failed to authenticate')           # Claude is down: nowhere to go
        self.engine.tick()
        stopped = self.store.get(other['id'])
        self.assertEqual((stopped['status'], stopped['failure_code']), ('failed', 'copilot_auth'))

    def test_messages_are_recognised(self):
        from team_recovery import COPILOT_AUTH, usage_limit_retry_at
        self.assertIsNotNone(usage_limit_retry_at('You have exceeded your premium request allowance'))
        self.assertIsNotNone(usage_limit_retry_at('Insufficient AI credits for this request'))
        self.assertIsNone(usage_limit_retry_at('Syntax error in file'))
        self.assertTrue(COPILOT_AUTH.search('Error: Not logged in. Run copilot login'))
        self.assertTrue(COPILOT_AUTH.search('HTTP 401 Unauthorized'))
        self.assertFalse(COPILOT_AUTH.search('tests failed: 2 of 30'))

    def test_usage_limit_wait_switches_instead_of_waiting(self):
        self.classify('Internal')
        task = self.task()
        retry_at = time.time() + 3 * 3600
        self.store.update(task['id'], retry_at=retry_at, failure_code='usage_limit_wait')
        self.engine.tick()
        current = self.store.get(task['id'])
        self.assertEqual(current['profile']['adapter'], 'copilot')
        self.assertIsNone(current['retry_at'])
        self.assertEqual(current['auto_return']['retry_at'], retry_at)


if __name__ == '__main__':
    unittest.main()
