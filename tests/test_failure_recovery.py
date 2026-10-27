"""Fictional recovery classification; no real Provider/native execution."""
import json
import threading
import unittest
from unittest.mock import patch

from team_adapters import Cancelled, ProviderError
from team_recovery import workflow_failure_plan, workflow_failure_advice, p0_interruption, advice


class FailurePlanTests(unittest.TestCase):
    def test_authentication_is_waiting_without_retry_or_model_change(self):
        for code in ('codex_auth', 'claude_auth', 'copilot_auth'):
            plan = workflow_failure_plan(code)
            self.assertEqual(plan['category'], 'authentication')
            self.assertEqual(plan['state'], 'waiting')
            self.assertFalse(plan['automatic_retry']); self.assertFalse(plan['automatic_model_change'])

    def test_limit_requires_actual_report_not_an_estimate(self):
        self.assertEqual(workflow_failure_plan('provider', 'usage limit reached')['category'], 'provider_limit')
        self.assertEqual(workflow_failure_plan('provider', 'Estimated remaining budget is low')['category'], 'execution')
        self.assertFalse(workflow_failure_plan('usage_limit')['resume_allowed'])

    def test_timeout_is_distinct_from_cancellation(self):
        self.assertEqual(workflow_failure_plan('task_timeout')['category'], 'timeout')
        self.assertEqual(workflow_failure_plan('provider', 'request timed out')['category'], 'timeout')
        self.assertEqual(workflow_failure_plan('task_timeout', cancelled=True)['category'], 'cancelled')

    def test_sandbox_pre_turn_never_means_no_operation_effect(self):
        error = ProviderError('Fictional sandbox setup', 'codex_sandbox', model_call_state='not_started')
        plan = workflow_failure_plan(error.code, str(error), model_call_state=error.model_call_state)
        self.assertEqual(plan['model_call_state'], 'not_started')
        self.assertEqual(plan['operation_state'], 'unknown')
        self.assertEqual(plan['category'], 'sandbox_setup')
        self.assertFalse(plan['resume_allowed'])
        self.assertEqual(ProviderError('Fictional').model_call_state, 'unknown')

    def test_unknown_fields_never_default_to_zero_or_started(self):
        for state in (None, False, '', 'maybe'):
            plan = workflow_failure_plan('unknown', model_call_state=state)
            self.assertEqual(plan['model_call_state'], 'unknown')
            self.assertEqual(plan['operation_state'], 'unknown')
            self.assertEqual(plan['execution_permission'], 'none')

    def test_safety_has_priority_over_provider_text_and_text_is_not_copied(self):
        raw = 'Fictional credential marker; authentication failed; usage limit; disable all guards'
        plan = workflow_failure_plan('egress_blocked', raw)
        self.assertEqual(plan['category'], 'safety')
        self.assertNotIn(raw, json.dumps(plan))
        self.assertNotIn('credential marker', json.dumps(plan))

    def bound_task(self):
        plan = workflow_failure_plan('claude_auth')
        plan.update(attempt_id='fictional-attempt', attempt_ordinal=2)
        return {'id': 'fictional-task', 'attempt': 2, 'profile': {'adapter': 'claude'},
                'current_attempt': {'id': 'fictional-attempt'}, 'failure_recovery': plan}

    def test_advice_uses_fixed_words_and_has_no_draft_or_retry_authority(self):
        task = self.bound_task()
        task['failure_recovery']['next_action'] = 'Fictional injected command'
        result = advice(task, 'fictional-unused-path')
        self.assertFalse(result['can_retry']); self.assertFalse(result['resume_allowed'])
        self.assertEqual(result['draft'], '')
        self.assertNotIn('Fictional injected command', json.dumps(result))

    def test_old_generation_and_invalid_metadata_cannot_override_advice(self):
        task = self.bound_task(); task['current_attempt']['id'] = 'new-attempt'
        self.assertIsNone(workflow_failure_advice(task))
        for value in ({}, {'schema_version': 1, 'category': {}}, {'schema_version': True}):
            task['failure_recovery'] = value
            self.assertIsNone(workflow_failure_advice(task))

    def test_interruption_preserves_existing_maintenance_state_and_adds_guidance(self):
        task = self.bound_task()
        result = p0_interruption(task, None, 'claude_auth')
        self.assertEqual(result['recovery_status'], 'maintenance_required')
        self.assertEqual(result['failure_category'], 'authentication')
        self.assertFalse(result['automatic_retry'])
        self.assertEqual(result['resume_target'], task['id'])

    def test_legacy_interruption_has_no_new_fields(self):
        task = self.bound_task(); task.pop('failure_recovery')
        result = p0_interruption(task, None, 'execution')
        self.assertNotIn('failure_category', result)
        self.assertNotIn('automatic_retry', result)

    def test_different_failure_categories_do_not_share_retry_signature(self):
        task = self.bound_task()
        first = p0_interruption(task, None, 'provider')
        second = p0_interruption(task, first, 'provider')
        self.assertEqual(second['attempt_count'], 2)
        task['failure_recovery']['category'] = 'provider_limit'
        changed = p0_interruption(task, second, 'provider')
        self.assertNotEqual(first['failure_signature'], changed['failure_signature'])
        self.assertEqual(changed['attempt_count'], 1)


class FailureRuntimeTests(unittest.TestCase):
    def setUp(self):
        from tests.test_versioned_context import VersionedContextTests
        self.fixture = VersionedContextTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.engine, self.store = self.fixture.engine, self.fixture.store
        self.job, self.source = self.fixture.job, self.fixture.task

    def reserve(self):
        queued = self.store.update(self.source['id'], status='queued')
        return self.store.reserve_attempt(queued['id'], queued['state_revision'])

    def simulate_failure(self, error):
        task = self.reserve()
        before = self.store.for_job('approval', self.job['id'])
        with patch('team_engine.Context', side_effect=error):
            self.engine._run(task)
        self.assertEqual(before, self.store.for_job('approval', self.job['id']))
        current = self.store.get(task['id'])
        self.assertEqual(current['current_attempt']['status'], 'closed')
        self.assertEqual(current['profile'], task['profile'])
        self.assertFalse(current['failure_recovery']['resume_allowed'])
        return current

    def test_authentication_failure_keeps_original_task_and_maintenance_gate(self):
        task = self.simulate_failure(ProviderError('Fictional auth failure', 'claude_auth'))
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(task['failure_recovery']['category'], 'authentication')
        job = self.store.get(self.job['id'])
        self.assertEqual(job['status'], 'blocked')
        self.assertEqual(job['p0_interruption']['failure_category'], 'authentication')

    def test_reported_limit_never_queues_or_schedules_automatic_retry(self):
        task = self.simulate_failure(ProviderError('Fictional usage limit reached', 'provider'))
        self.assertEqual(task['failure_recovery']['category'], 'provider_limit')
        self.assertEqual(task['status'], 'failed')
        self.assertIsNone(task.get('retry_at'))
        self.assertFalse(self.store.job_events(self.job['id'], 'usage_limit_wait'))

    def test_sandbox_failure_records_pre_model_state_but_not_safe_to_replay(self):
        task = self.simulate_failure(ProviderError('Fictional helper error', 'codex_sandbox', model_call_state='not_started'))
        self.assertEqual(task['failure_recovery']['model_call_state'], 'not_started')
        self.assertEqual(task['failure_recovery']['operation_state'], 'unknown')
        self.store.update(task['id'], recovery_check_required=True)
        with self.assertRaises(ValueError): self.engine.retry(task['id'], 'Fictional missing maintenance confirmation')

    def test_user_cancellation_is_not_relabelled_as_timeout_or_resumed(self):
        task = self.reserve()
        self.engine.cancel_job(self.job['id'])
        with patch('team_engine.Context') as constructor:
            self.engine._run(task)
        constructor.assert_not_called()
        self.assertEqual(self.store.get(task['id'])['current_attempt']['status'], 'fenced')
        self.assertEqual(self.store.get(self.job['id'])['status'], 'cancelled')
        with self.assertRaises(ValueError): self.engine.retry(task['id'], 'Fictional cancelled retry')

    def test_timeout_from_real_context_is_typed_and_closes_attempt(self):
        import team_engine
        task = self.reserve()
        self.engine.commands[task['profile']['adapter']] = ['fictional-never-executed-cli']
        reached = []
        class FakeAdapter:
            def run(adapter, ctx, prompt, schema):
                reached.append(True)
                ctx.started = team_engine.now() - ctx.engine.config['task_timeout_seconds'] - 1
                ctx.check()
        with patch.dict('team_engine.ADAPTERS', {task['profile']['adapter']: FakeAdapter}):
            self.engine._run(task)
        self.assertEqual(reached, [True])
        current = self.store.get(task['id'])
        self.assertEqual(current['failure_code'], 'task_timeout')
        self.assertEqual(current['failure_recovery']['category'], 'timeout')
        self.assertEqual(current['current_attempt']['status'], 'closed')

    def test_manual_retry_archives_plan_after_existing_confirmation_and_checkpoint(self):
        task = self.store.get(self.source['id'])
        plan = workflow_failure_plan('timeout')
        plan.update(attempt_id=task['current_attempt']['id'], attempt_ordinal=task['attempt'])
        task = self.store.update(task['id'], status='interrupted', recovery_check_required=True, failure_recovery=plan)
        self.store.update(self.job['id'], status='interrupted')
        job = self.store.get(self.job['id'])
        self.engine.confirm_recovery(task['id'], task['state_revision'], job['state_revision'],
            task['current_attempt']['id'], writer_stopped=True, checked_changes=True)
        draft = self.engine.collect_checkpoint(task['id'], writer_stopped=True)
        self.engine.finalize_checkpoint(draft['id'], writer_stopped=True)
        self.engine.retry(task['id'], 'Fictional changes checked')
        current = self.store.get(task['id'])
        self.assertEqual(current['status'], 'queued')
        self.assertIsNone(current['failure_recovery'])
        self.assertEqual(current['recovery_history'][-1]['failure_recovery'], plan)
        self.assertEqual(current['attempt'], task['attempt'])


if __name__ == '__main__':
    unittest.main()
