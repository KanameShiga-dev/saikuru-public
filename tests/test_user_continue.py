"""「続ける／やめる」: the person at this PC restarts a stopped step. Fictional data only."""
import json
import unittest
from unittest.mock import patch

if __package__:
    from . import test_recovery_controls as controls
else:
    import test_recovery_controls as controls


class UserContinueTests(unittest.TestCase):
    """Starts from a task interrupted by a restart (recovery_check_required)."""
    setUp = controls.RecoveryControlTests.setUp
    payload = controls.RecoveryControlTests.payload
    confirm = controls.RecoveryControlTests.confirm
    start_http = controls.RecoveryControlTests.start_http
    post = controls.RecoveryControlTests.post

    def target(self):
        return self.engine.workflow_control_target(self.job['id'], 'retry_task', self.task['id'])

    def go(self):
        return self.engine.user_continue(self.job['id'], self.task['id'], self.target())

    def test_restart_stop_is_offered_and_continue_requeues_a_fresh_attempt(self):
        view = self.engine.workflow_view(self.job['id'])
        self.assertEqual(view['continue_task_ids'], [self.task['id']])
        self.assertEqual(view['wait_owner'], 'user')
        self.go()
        task = self.store.get(self.task['id'])
        self.assertEqual(task['status'], 'queued')
        self.assertFalse(task['recovery_check_required'])
        self.assertIsNone(task.get('recovery_confirmation'))
        self.assertEqual(self.store.get(self.job['id'])['status'], 'running')
        self.assertEqual(len(self.store.job_events(self.job['id'], 'user_continue_confirmed')), 1)
        self.assertEqual(len(self.store.job_events(self.job['id'], 'user_continue')), 1)
        self.assertFalse(self.store.job_events(self.job['id'], 'recovery_confirmed'))
        self.assertEqual(self.engine.workflow_view(self.job['id'])['continue_task_ids'], [])

    def test_continue_does_not_need_the_effects_of_the_stopped_attempt_certified(self):
        self.store.event('operation_bash', 'Fictional command with unknown result', self.task['id'], self.job['id'])
        task = self.store.get(self.task['id'])
        self.store.put('agent_run', {'id': 'fictional-native-run', 'job_id': self.job['id'], 'task_id': task['id'],
            'attempt_id': task['current_attempt']['id'], 'status': 'failed', 'provider': 'claude'})
        with self.assertRaises(ValueError): self.confirm()
        self.go()
        self.assertEqual(self.store.get(self.task['id'])['status'], 'queued')

    def test_failed_task_without_restart_fence_can_continue(self):
        task = self.store.get(self.task['id'])
        self.store.release_user_continue(task['id'], task['state_revision'], self.store.get(self.job['id'])['state_revision'],
                                         task['current_attempt']['id'])
        self.store.update(self.task['id'], status='failed', failure_code='task_timeout')
        self.store.update(self.job['id'], status='blocked')
        self.go()
        self.assertEqual(self.store.get(self.task['id'])['status'], 'queued')

    def test_safety_stop_cannot_continue(self):
        self.store.update(self.task['id'], failure_code='egress_blocked')
        self.assertEqual(self.engine.workflow_view(self.job['id'])['continue_task_ids'], [])
        with self.assertRaises(ValueError): self.go()
        self.assertTrue(self.store.get(self.task['id'])['recovery_check_required'])

    def test_running_worker_pending_decision_or_ended_job_cannot_continue(self):
        with patch.dict(self.engine.active, {'fictional': type('Ctx', (), {'project': str(self.project)})()}):
            with self.assertRaises(ValueError): self.go()
        approval = self.engine.new_approval(self.store.get(self.task['id']), 'question', {'summary': 'Fictional question'})
        with self.assertRaises(ValueError): self.go()
        self.store.update(approval['id'], status='expired')
        self.engine.cancel_job(self.job['id'])
        with self.assertRaises(ValueError): self.go()
        self.assertNotEqual(self.store.get(self.task['id'])['status'], 'queued')

    def test_stale_target_and_expired_plan_are_refused_without_change(self):
        stale = self.target()
        self.store.update(self.job['id'], title='Fictional change after the screen was drawn')
        with self.assertRaises(ValueError):
            self.engine.user_continue(self.job['id'], self.task['id'], stale)
        self.store.update(self.job['plan_approval_id'], status='expired')
        with self.assertRaises(ValueError): self.go()
        task = self.store.get(self.task['id'])
        self.assertEqual(task['status'], 'interrupted')
        self.assertTrue(task['recovery_check_required'])
        self.assertFalse(self.store.job_events(self.job['id'], 'user_continue_confirmed'))

    def test_restart_after_continue_asks_again(self):
        self.go()
        task = self.store.get(self.task['id'])
        self.store.reserve_attempt(task['id'], task['state_revision'])
        self.store.recover()
        self.assertTrue(self.store.get(self.task['id'])['recovery_check_required'])
        self.assertEqual(self.engine.workflow_view(self.job['id'])['continue_task_ids'], [self.task['id']])

    def test_screen_feed_names_a_continuable_stop_as_the_persons_decision(self):
        from server import App
        task = dict(self.store.get(self.task['id']), summary='Fictional raw diagnostic')
        job = dict(self.store.get(self.job['id']), status='blocked', p0_progress={'current': '保守対応待ち'},
                   p0_interruption={'recovery_status': 'maintenance_required', 'interrupted_task_id': task['id']})
        state = {'approvals': [], 'events': [], 'tasks': [task]}
        shown = App.redact_state(dict(state, jobs=[dict(job, workflow={'continue_task_ids': [task['id']]})]))
        self.assertEqual(shown['tasks'][0]['title'], '止まった作業')
        self.assertNotIn('Fictional raw diagnostic', json.dumps(shown, ensure_ascii=False))
        self.assertIn('続ける', shown['jobs'][0]['p0_progress']['current'])
        held = App.redact_state(dict(state, jobs=[dict(job, workflow={'continue_task_ids': []})]))
        self.assertEqual(held['tasks'][0]['title'], '保守対応待ち')
        self.assertEqual(held['jobs'][0]['p0_progress']['current'], '保守対応待ち')

    def test_http_browser_session_can_continue_but_maintenance_routes_stay_closed(self):
        server = self.start_http()
        body = {'job_id': self.job['id'], 'task_id': self.task['id'], 'target': self.target(), 'confirmed': True}
        for bad in (dict(body, confirmed='true'), dict(body, auto=True), {k: v for k, v in body.items() if k != 'target'}):
            self.assertEqual(self.post(server, '/api/recovery/user-continue', bad)[0], 400)
        for options in ({'cookie': False}, {'ui': False}, {'origin': 'https://example.invalid'}):
            self.assertEqual(self.post(server, '/api/recovery/user-continue', body, **options)[0], 403)
        self.assertEqual(self.post(server, '/api/maintenance/recovery/confirm', self.payload())[0], 403)
        self.assertTrue(self.store.get(self.task['id'])['recovery_check_required'])
        status, result = self.post(server, '/api/recovery/user-continue', body)
        self.assertEqual(status, 200, json.dumps(result, ensure_ascii=False))
        self.assertEqual(self.store.get(self.task['id'])['status'], 'queued')


if __name__ == '__main__':
    unittest.main()
