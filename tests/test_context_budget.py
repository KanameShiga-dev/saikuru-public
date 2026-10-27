"""Synthetic context budget thresholds/observations; no Provider is invoked."""
import copy
import json
import math
from types import SimpleNamespace
import unittest
from unittest.mock import patch

if __package__:
    from . import test_versioned_context as fixtures
else:
    import test_versioned_context as fixtures
from team_adapters import observe_codex_context
from team_adapters import ProviderError
from team_context_optimizer import context_checkpoint_decision
from team_engine import Context
from team_store import StaleAttempt
from team_usage_metrics import codex_context_observation


class BudgetPolicyTests(unittest.TestCase):
    def measured(self, used=7499, capacity=10000):
        return {'schema_version': 1, 'measurement': 'measured', 'metric': 'context_occupancy_tokens',
                'capacity_tokens': capacity, 'used_tokens': used, 'observed_at': 100}

    def decide(self, value, at=100, **policy):
        return context_checkpoint_decision(value, at=at, policy=dict(handoff_reserve_tokens=1500, safety_fraction=.10, **policy))

    def test_calibrated_boundary_keeps_handoff_and_safety_reserves(self):
        before, at = self.decide(self.measured(7499)), self.decide(self.measured(7500))
        self.assertEqual(at['threshold_tokens'], 7500)
        self.assertFalse(before['save_at_boundary'])
        self.assertTrue(at['save_at_boundary'])
        self.assertEqual(at['remaining_tokens'], 2500)
        self.assertEqual(at['calibration'], 'synthetic_formula_v1_not_provider_calibrated')

    def test_unknown_characters_or_cumulative_totals_never_mean_zero(self):
        for row in (None, {}, {'characters': 1, 'totalTokens': 0}, {'capacity_tokens': 10000, 'used_tokens': None}):
            result = self.decide(row)
            self.assertTrue(result['save_at_boundary'])
            self.assertEqual(result['measurement'], 'unknown')
            self.assertIsNone(result['remaining_tokens'])

    def test_codex_last_input_is_only_proxy_even_when_low_or_zero(self):
        for used in (0, 100, 9000):
            row = codex_context_observation({'last': {'inputTokens': used}, 'modelContextWindow': 10000,
                                            'total': {'inputTokens': 999999}}, received_at=100)
            result = self.decide(row)
            self.assertEqual(result['measurement'], 'proxy')
            self.assertIsNone(result['remaining_tokens'])
            self.assertTrue(result['save_at_boundary'])
            self.assertEqual(result['proxy_pressure_ratio'], used / 10000)

    def test_proxy_cannot_be_relabelled_as_measured(self):
        row = codex_context_observation({'last': {'inputTokens': 1}, 'modelContextWindow': 10000}, received_at=100)
        row['measurement'] = 'measured'
        self.assertEqual(self.decide(row)['measurement'], 'unknown')

    def test_stale_future_and_compacted_observations_are_unknown(self):
        for row, at in ((self.measured(), 131), (self.measured(), 99),
                        (dict(self.measured(), invalidated_by='compaction'), 100)):
            self.assertEqual(self.decide(row, at)['measurement'], 'unknown')
        self.assertEqual(self.decide(self.measured(), 130)['measurement'], 'measured')

    def test_invalid_numbers_policies_and_large_values_are_refused_or_unknown(self):
        for capacity, used in ((True, 1), (0, 0), (10**1000, 1), (100, True), (100, -1), (100, 101)):
            row = codex_context_observation({'modelContextWindow': capacity, 'last': {'inputTokens': used}}, received_at=100)
            self.assertEqual(row['measurement'], 'unknown')
        for policy in ({'handoff_reserve_tokens': True}, {'safety_fraction': math.nan}, {'model': 'fictional-model'}):
            with self.assertRaises(ValueError): context_checkpoint_decision(None, at=100, policy=policy)
        with self.assertRaises(ValueError): codex_context_observation({}, received_at=math.inf)

    def test_small_window_reserves_immediately_and_raw_content_is_not_kept(self):
        result = self.decide(self.measured(0, 1000))
        self.assertEqual(result['threshold_tokens'], 0)
        self.assertTrue(result['save_at_boundary'])
        row = codex_context_observation({'prompt': 'fictional-confidential-value', 'last': {'inputTokens': 0},
                                        'modelContextWindow': 10000}, received_at=100)
        self.assertNotIn('fictional-confidential-value', json.dumps(row))
        self.assertEqual(row['used_tokens'], 0)


class BudgetRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.VersionedContextTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        for name in ('engine', 'store', 'task', 'job'):
            setattr(self, name, getattr(self.fixture, name))
        task = self.store.update(self.task['id'], status='queued')
        self.task = self.store.reserve_attempt(task['id'], task['state_revision'])
        self.ctx = Context.__new__(Context)
        self.ctx.engine, self.ctx.task = self.engine, self.task
        self.ctx.agent_run = {'id': 'fictional-budget-run', 'provider': 'codex',
            'attempt_id': self.task['current_attempt']['id'], 'session_id': 'fictional-budget-session'}

    def observe(self, usage=None):
        self.ctx.record_context_observation(usage or {'last': {'inputTokens': 100}, 'modelContextWindow': 10000},
                                            'fictional-budget-session')

    def test_protocol_hook_records_only_matching_session_and_generation(self):
        before = self.store.get(self.task['id'])
        observe_codex_context(self.ctx, 'thread/tokenUsage/updated', {'threadId': 'foreign-thread',
            'tokenUsage': {'last': {'inputTokens': 1}, 'modelContextWindow': 10000}})
        self.assertEqual(before, self.store.get(self.task['id']))
        observe_codex_context(self.ctx, 'thread/tokenUsage/updated', {'threadId': 'fictional-budget-session',
            'tokenUsage': {'last': {'inputTokens': 100}, 'modelContextWindow': 10000}})
        row = self.store.get(self.task['id'])['agent_run']['context_observation']
        self.assertEqual(row['measurement'], 'proxy')
        self.assertEqual(row['attempt_id'], self.task['current_attempt']['id'])
        self.assertNotIn('fictional-budget-session', json.dumps(row))

    def test_compaction_invalidates_observation(self):
        self.observe()
        observe_codex_context(self.ctx, 'item/started', {'threadId': 'fictional-budget-session', 'item': {'type': 'contextCompaction'}})
        row = self.store.get(self.task['id'])['agent_run']['context_observation']
        self.assertEqual(row['measurement'], 'unknown')
        self.assertEqual(row['invalidated_by'], 'compaction')
        self.assertIsNone(row['used_tokens'])

    def test_wrong_or_missing_protocol_hook_is_optional_for_legacy_fakes(self):
        observe_codex_context(SimpleNamespace(), 'thread/tokenUsage/updated', {})
        observe_codex_context(self.ctx, 'thread/tokenUsage/updated', [])
        self.assertNotIn('agent_run', self.store.get(self.task['id']))

    def test_old_attempt_cannot_write_context_or_book_save(self):
        self.store.close_attempt(self.task['id'], 'fenced', 'fictional-stop')
        before = self.store.get(self.task['id'])
        with self.assertRaises(StaleAttempt): self.observe()
        with self.assertRaises(StaleAttempt): self.engine._reserve_context_save(self.task, safe_boundary=True)
        self.assertEqual(before, self.store.get(self.task['id']))

    def test_reservation_is_at_boundary_once_and_never_ready_or_resume(self):
        self.observe()
        with self.assertRaises(ValueError): self.engine._reserve_context_save(self.task)
        self.engine.active[self.task['id']] = self.ctx
        with self.assertRaises(ValueError): self.engine._reserve_context_save(self.task, safe_boundary=True)
        self.engine.active.clear()
        first = self.engine._reserve_context_save(self.task, safe_boundary=True)
        before = self.store.get(self.task['id'])
        again = self.engine._reserve_context_save(self.task, safe_boundary=True)
        self.assertEqual(first, again)
        self.assertEqual(before, self.store.get(self.task['id']))
        self.assertFalse(first['ready'])
        self.assertFalse(first['resume_allowed'])
        self.assertEqual(len(self.store.job_events(self.job['id'], 'context_save_reserved')), 1)
        self.assertFalse(self.store.for_job('checkpoint', self.job['id']))

    def test_unknown_or_new_session_reserves_without_fake_remaining(self):
        self.observe()
        self.ctx.agent_run['session_id'] = 'fictional-new-session'
        self.store.update(self.task['id'], agent_run=self.ctx.agent_run)
        result = self.engine._reserve_context_save(self.task, safe_boundary=True)
        self.assertEqual(result['decision']['measurement'], 'unknown')
        self.assertIsNone(result['decision']['remaining_tokens'])

    def test_no_observation_or_invalid_policy_still_requests_safe_boundary(self):
        self.engine.config['context_checkpoint_policy'] = {'safety_fraction': True}
        result = self.engine._reserve_context_save(self.task, safe_boundary=True)
        self.assertEqual(result['decision']['reason'], 'invalid_context_policy')
        self.assertEqual(result['decision']['measurement'], 'unknown')

    def test_reservation_event_and_row_are_atomic(self):
        original = self.store.update
        def fail(identifier, **values):
            if 'context_save_request' in values: raise RuntimeError('fictional metadata failure')
            return original(identifier, **values)
        with patch.object(self.store, 'update', side_effect=fail), self.assertRaises(RuntimeError):
            self.engine._reserve_context_save(self.task, safe_boundary=True)
        self.assertFalse(self.store.job_events(self.job['id'], 'context_save_reserved'))
        self.assertNotIn('context_save_request', self.store.get(self.task['id']))

    def test_embedded_native_run_is_not_misread_as_no_worker_effect(self):
        self.observe()
        self.store.close_attempt(self.task['id'], 'fenced', 'fictional-restart')
        self.store.update(self.task['id'], status='interrupted', recovery_check_required=True)
        self.store.update(self.job['id'], status='interrupted')
        task, job = self.store.get(self.task['id']), self.store.get(self.job['id'])
        with self.assertRaises(ValueError):
            self.engine.confirm_recovery(task['id'], task['state_revision'], job['state_revision'],
                task['current_attempt']['id'], writer_stopped=True, checked_changes=True)
        self.assertTrue(self.store.get(self.task['id'])['recovery_check_required'])

    def test_worker_return_automatically_reserves_then_closes_attempt(self):
        with patch('team_engine.Context', side_effect=ProviderError('Fictional pre-start failure')):
            self.engine._run(self.task)
        current = self.store.get(self.task['id'])
        self.assertEqual(current['current_attempt']['status'], 'closed')
        self.assertEqual(current['context_save_request']['state'], 'reserved')
        self.assertEqual(current['context_save_request']['decision']['measurement'], 'unknown')
        self.assertFalse(current['context_save_request']['resume_allowed'])

    def test_reservation_failure_still_closes_attempt(self):
        with patch('team_engine.Context', side_effect=ProviderError('Fictional pre-start failure')):
            with patch.object(self.engine, '_reserve_context_save', side_effect=RuntimeError('fictional save failure')):
                with self.assertRaises(RuntimeError): self.engine._run(self.task)
        self.assertEqual(self.store.get(self.task['id'])['current_attempt']['status'], 'closed')


if __name__ == '__main__':
    unittest.main()
