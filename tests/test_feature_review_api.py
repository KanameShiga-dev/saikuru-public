"""Offline HTTP authority and final-transaction races using fictional jobs only."""
import copy
import unittest
from unittest.mock import patch

from tests import test_workflow_api as api
from tests import test_plan_revision as plans


class PlanEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.f = plans.RevisionTests(); self.addCleanup(self.f.doCleanups); self.f.setUp()
        self.request = self.f.api_server()
        self.body = {'id': self.f.approval['id'], 'allow': True,
                     'workflow_confirmation': self.f.approval['workflow_revision_digest']}

    def test_plan_rejects_extra_authority_and_nonplan_fields_without_writes(self):
        before = self.f.state()
        for key, value in [('actor', 'end_user'), ('role', 'maintainer'), ('answers', {}),
                           ('attachment_ids', []), ('review_checklist', {}), ('job_id', 'fictional-other')]:
            with self.subTest(key=key):
                code, result = self.request('/api/decide', dict(self.body, **{key: value}))
                self.assertEqual((code, result['code']), (400, 'workflow_request_invalid'))
                self.assertEqual(self.f.state(), before)

    def test_exact_plan_envelope_succeeds_once_without_receipt(self):
        self.assertEqual(self.request('/api/decide', dict(self.body, note='Fictional consent'))[0], 200)
        before = self.f.state()
        self.assertEqual(self.request('/api/decide', self.body)[0], 400)
        self.assertEqual(self.f.state(), before)
        self.assertEqual(self.f.job()['status'], 'running')

    def test_missing_confirmation_or_foreign_identity_cannot_approve(self):
        before = self.f.state()
        for body in ({'id': self.body['id'], 'allow': True}, dict(self.body, id='fictional-foreign')):
            self.assertEqual(self.request('/api/decide', body)[0], 400)
            self.assertEqual(self.f.state(), before)

    def test_ui_shaped_denial_without_digest_succeeds_and_replay_is_refused(self):
        body = {'id': self.body['id'], 'allow': False, 'note': 'Fictional declined plan'}
        self.assertEqual(self.request('/api/decide', body)[0], 200)
        self.assertEqual(self.f.store.get(body['id'], 'approval')['status'], 'denied')
        self.assertEqual(self.f.job()['plan_revision'], 0)
        before = self.f.state()
        self.assertEqual(self.request('/api/decide', body)[0], 400)
        self.assertEqual(self.f.state(), before)

    def test_denial_with_current_digest_is_supported_without_publishing_plan(self):
        self.assertEqual(self.request('/api/decide', dict(self.body, allow=False))[0], 200)
        self.assertEqual(self.f.store.get(self.body['id'], 'approval')['status'], 'denied')
        self.assertEqual(self.f.job()['plan_revision'], 0)

    def test_denial_rejects_supplied_wrong_digest_and_extra_fields_without_writes(self):
        before = self.f.state()
        for body in (dict(self.body, allow=False, workflow_confirmation='fictional-stale'),
                     dict(self.body, allow=False, workflow_confirmation=None),
                     {'id': self.body['id'], 'allow': False, 'actor': 'end_user'}):
            self.assertEqual(self.request('/api/decide', body)[0], 400)
            self.assertEqual(self.f.state(), before)


class FinalTransactionTests(unittest.TestCase):
    def setUp(self):
        self.f = api.WorkflowApiTests(); self.addCleanup(self.f.doCleanups); self.f.setUp()
        self.engine, self.store = self.f.engine, self.f.store
        self.job_id, self.task_id = self.f.f.job_id, self.f.f.first['id']
        self.store.update(self.task_id, status='failed')
        self.assertFalse(self.store.get(self.task_id).get('recovery_check_required'))
        self.assertFalse(self.store.get(self.task_id).get('recovery_confirmation'))

    def body(self, action):
        value = {'job_id': self.job_id, 'action': action,
                 'target': self.engine.workflow_control_target(
                     self.job_id, action, self.task_id if action == 'retry_task' else None)}
        if action == 'cancel_job': return dict(value, confirmed=True)
        return dict(value, task_id=self.task_id, checked_changes=True,
                    note='Fictional inspected retry', handoff_allowed=False)

    def test_plain_failed_retry_valid_target_is_queued_without_dispatch(self):
        with patch.object(self.store, 'reserve_attempt', side_effect=AssertionError('No dispatch')):
            code, result = self.f.request('/api/workflow/control', self.body('retry_task'))
        self.assertEqual(code, 200, result)
        self.assertTrue(result['queued']); self.assertFalse(result['started'])
        self.assertFalse(result['formal_receipt'])

    def test_retry_rechecks_target_inside_final_transaction_without_recovery_guard(self):
        body = self.body('retry_task'); original = self.engine._retry_confirmed; snapshots = []
        def raced(*args, **kwargs):
            self.store.update(self.task_id, summary='Fictional concurrent annotation')
            snapshots.append(self.f.f.state())
            return original(*args, **kwargs)
        with patch.object(self.engine, '_retry_confirmed', side_effect=raced):
            code, result = self.f.request('/api/workflow/control', body)
        self.assertEqual(code, 400, result); self.assertEqual(len(snapshots), 1)
        self.assertEqual(self.f.f.state(), snapshots[0])
        self.assertEqual(self.store.get(self.task_id)['status'], 'failed')

    def test_cancel_rechecks_target_after_http_validation_even_if_job_stays_active(self):
        body = self.body('cancel_job'); original = self.engine.cancel_job; snapshots = []
        def raced(*args, **kwargs):
            self.store.update(self.task_id, summary='Fictional concurrent annotation')
            snapshots.append(self.f.f.state())
            return original(*args, **kwargs)
        with patch.object(self.engine, 'cancel_job', side_effect=raced):
            code, result = self.f.request('/api/workflow/control', body)
        self.assertEqual(code, 400, result); self.assertEqual(len(snapshots), 1)
        self.assertEqual(self.f.f.state(), snapshots[0])
        self.assertEqual(self.store.get(self.job_id)['status'], 'running')

    def test_all_authority_failures_leave_objects_and_events_unchanged(self):
        body = self.body('retry_task'); before = self.f.f.state()
        for options in ({'cookie': False}, {'ui': False}, {'origin': 'https://example.invalid'},
                        {'cookie': False, 'headers': {'Authorization': 'Bearer fictional-worker'}}):
            self.assertEqual(self.f.request('/api/workflow/control', body, **options)[0], 403)
        for field, value in [('job_id', 'fictional-other'), ('task_id', 'fictional-other'),
                             ('job_revision', True), ('state_digest', 'fictional-tampered')]:
            altered = copy.deepcopy(body); altered['target'][field] = value
            self.assertEqual(self.f.request('/api/workflow/control', altered)[0], 400)
        self.assertEqual(self.f.f.state(), before)
