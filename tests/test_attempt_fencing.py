"""Fictional dispatch races and delayed callbacks; no native/provider launch."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from team_config import default_config
from team_common_agents import load_agent
from team_engine import Context, Engine
from team_p0_contract import initial, initial_workflow
from team_store import Store, StaleAttempt


class AttemptFencingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='fictional-attempt-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.store = Store(self.root / 'runtime')
        config = default_config()
        config['approved_roots'] = [str(self.project)]
        self.engine = Engine(self.store, config, {}, 'http://127.0.0.1:0')
        self.engine.paused = True
        self.addCleanup(self.store.db.close)
        self.addCleanup(self.engine.handoff.close)
        job = self.store.put('job', initial_workflow(dict(id='fictional-job', goal='Fictional lookup',
            goal_contract=initial('Fictional lookup'), status='awaiting_approval', project=str(self.project),
            request_origin='new', route='full', document_source=None, p0_contract_version=1,
            auto_execute=False, validation_commands=[], title='Fictional lookup', created_at=1), enabled=True))
        self.store.put('approval', dict(id='fictional-plan', job_id=job['id'], kind='plan', status='approved',
            task_id='fictional-planner', attempt=0, payload={'summary': 'Fictional approved plan'},
            created_at=1, note='Fictional consent'))
        self.job = self.store.update(job['id'], status='running', plan_revision=1,
                                    plan_approval_id='fictional-plan', requirement_revision=1,
                                    requirements_approval_id='fictional-plan')
        self.task = self.engine.new_task(self.job, 'Fictional build', 'No provider calls', 'builder')

    def reserve(self):
        task = self.store.get(self.task['id'])
        return self.store.reserve_attempt(task['id'], task['state_revision'])

    def context(self, task):
        # Only callback fields; no Context preparation/provider/credential access.
        ctx = Context.__new__(Context)
        ctx.engine, ctx.task, ctx.token = self.engine, task, 'fictional-worker-token'
        ctx.agent_run = {'id': 'fictional-run', 'provider': 'codex'}
        ctx._partial_usage = {}
        return ctx

    def next_attempt(self):
        self.store.close_attempt(self.task['id'], 'fenced', 'fictional-retry')
        self.store.update(self.task['id'], status='failed')
        self.engine.retry(self.task['id'], 'Fictional stopped worker, changes checked')
        return self.reserve()

    def test_attempt_reserved_durably_before_worker_and_start_event_matches(self):
        task = self.reserve()
        attempt = task['current_attempt']
        self.assertEqual(task['attempt'], 1)
        self.assertEqual(attempt['ordinal'], task['attempt'])
        event = next(e for e in self.store.events() if e['seq'] == attempt['start_event_seq'])
        self.assertEqual(event['details']['attempt_id'], attempt['id'])
        self.assertEqual(event['task_id'], task['id'])
        self.assertEqual(self.store.get(task['id'])['current_attempt'], attempt)

    def test_two_connections_cannot_reserve_same_generation(self):
        other = Store(self.root / 'runtime')
        self.addCleanup(other.db.close)
        revision = self.task['state_revision']
        barrier = threading.Barrier(2)

        def dispatch(store):
            barrier.wait(timeout=5)
            try:
                store.reserve_attempt(self.task['id'], revision)
                return 'reserved'
            except StaleAttempt:
                return 'refused'

        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(dispatch, (self.store, other)))
        self.assertCountEqual(result, ['reserved', 'refused'])
        self.assertEqual(len(self.store.job_events(self.job['id'], 'attempt_started')), 1)

    def test_concurrent_retry_only_requeues_once(self):
        self.store.update(self.task['id'], status='failed')
        other = Store(self.root / 'runtime')
        engine = Engine(other, self.engine.config, {}, 'http://127.0.0.1:0')
        engine.paused = True
        self.addCleanup(other.db.close)
        self.addCleanup(engine.handoff.close)
        barrier = threading.Barrier(2)

        def retry(engine):
            barrier.wait(timeout=5)
            try:
                engine.retry(self.task['id'], 'Fictional checked retry')
                return 'queued'
            except ValueError:
                return 'refused'

        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(retry, (self.engine, engine)))
        self.assertCountEqual(result, ['queued', 'refused'])
        self.assertEqual(len(self.store.job_events(self.job['id'], 'retry')), 1)

    def test_old_usage_command_approval_and_finish_cannot_change_new_attempt(self):
        old = self.reserve()
        ctx = self.context(old)
        new = self.next_attempt()
        self.assertNotEqual(old['current_attempt']['id'], new['current_attempt']['id'])
        self.assertEqual(new['attempt'], old['attempt'] + 1)
        before = self.store.get(new['id'])
        events = self.store.events()
        calls = [lambda: ctx.record_usage({'inputTokens': 999}),
                 lambda: ctx.command_result('fictional-command', 0, 'fictional output'),
                 lambda: ctx.approve({'operation': 'fictional'}),
                 lambda: self.engine._finish(old, {'status': 'done', 'summary': 'stale'})]
        for call in calls:
            with self.assertRaises(StaleAttempt):
                call()
        self.assertEqual(self.store.get(new['id']), before)
        self.assertEqual(self.store.events(), events)
        self.context(new).record_usage({'inputTokens': 3})
        self.assertEqual(self.store.get(new['id'])['agent_run']['usage']['inputTokens'], 3)

    def test_http_hook_with_old_context_is_rejected_even_with_matching_token(self):
        old = self.reserve()
        ctx = self.context(old)
        self.next_attempt()
        self.engine.active[old['id']] = ctx
        before = self.store.get(old['id'])
        for method, body in [(self.engine.tool_result, {'tool': 'Bash', 'command': 'fictional', 'exit_code': 0}),
                             (self.engine.tool_request, {'tool': 'Write', 'input': {'file_path': 'fictional.py'}})]:
            with self.assertRaises(StaleAttempt):
                method(ctx.token, dict(body, task_id=old['id']))
        self.assertEqual(self.store.get(old['id']), before)
        self.engine.active.clear()

    def test_restart_fences_old_runtime_and_requires_stop_reconciliation(self):
        old = self.reserve()
        ctx = self.context(old)
        restarted = Store(self.root / 'runtime')
        self.addCleanup(restarted.db.close)
        restarted.recover()
        task = self.store.get(old['id'])
        self.assertEqual(task['status'], 'interrupted')
        self.assertTrue(task['recovery_check_required'])
        self.assertEqual(task['current_attempt']['status'], 'fenced')
        with self.assertRaises(StaleAttempt):
            ctx.record_usage({'inputTokens': 999})
        with self.assertRaises(ValueError):
            self.engine.retry(old['id'], 'Fictional retry without stop evidence')

    def test_cancel_and_late_worker_do_not_overwrite_cancel_or_new_active_context(self):
        old = self.reserve()
        self.engine.cancel_job(self.job['id'])
        with mock.patch.object(self.engine, '_run_attempt') as run:
            self.engine._run(old)
            run.assert_not_called()
        self.assertEqual(self.store.get(self.job['id'])['status'], 'cancelled')
        with self.assertRaises(StaleAttempt):
            self.context(old).record_usage({'inputTokens': 999})

    def test_bound_worker_guard_protects_related_job_event_and_handoff_transaction(self):
        old = self.reserve()
        with self.store.attempt_scope(old):
            self.store.update(self.job['id'], fictional_note='current generation')
        self.next_attempt()
        with self.assertRaises(StaleAttempt), self.store.attempt_scope(old):
            self.engine.sync_handoff(self.job['id'])
        self.assertEqual(self.store.get(self.job['id'])['fictional_note'], 'current generation')

    def test_plan_revision_change_rejects_existing_worker_callbacks(self):
        old = self.reserve()
        self.store.update(self.job['id'], plan_revision=2)
        with self.assertRaises(StaleAttempt):
            self.context(old).record_usage({'inputTokens': 999})
        self.assertNotIn('agent_run', self.store.get(old['id']))

    def test_worker_registration_race_does_not_remove_new_active_context(self):
        old = self.reserve()
        old_context = self.context(old)
        new_context = object()

        def interleave(*args):
            # Control requests use a different thread, not the worker's scope.
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(self.next_attempt).result(timeout=5)
            self.engine.active[old['id']] = new_context
            return old_context

        with mock.patch('team_engine.Context', side_effect=interleave):
            self.engine._run(old)
        self.assertIs(self.engine.active[old['id']], new_context)
        self.assertEqual(self.store.get(old['id'])['status'], 'running')
        self.engine.active.clear()

    def test_worker_failure_closes_generation_without_provider_launch(self):
        from team_adapters import ProviderError
        old = self.reserve()
        with mock.patch('team_engine.Context', side_effect=ProviderError('Fictional construction failure')):
            self.engine._run(old)
        self.assertEqual(self.store.get(old['id'])['current_attempt']['status'], 'closed')
        self.assertNotIn(old['id'], self.engine.active)
        self.assertEqual(len(self.store.job_events(self.job['id'], 'attempt_closed')), 1)

    def test_agent_run_and_history_retain_their_reserved_attempt_ids(self):
        self.engine.commands = {'codex': ['fictional-cli'], 'claude': ['fictional-cli']}
        self.engine.usage_snapshot = lambda: {}
        old = self.reserve()
        ctx = Context(self.engine, old)
        ctx.prepare_agent(load_agent('builder', old['profile']['adapter']))
        self.assertEqual(ctx.agent_run['attempt_id'], old['current_attempt']['id'])
        new = self.next_attempt()
        resumed = Context(self.engine, new)
        resumed.prepare_agent(load_agent('builder', new['profile']['adapter']))
        record = self.store.get(new['id'])
        self.assertEqual(record['agent_run']['attempt_id'], new['current_attempt']['id'])
        self.assertEqual(record['agent_run_history'][-1]['attempt_id'], old['current_attempt']['id'])
        self.assertNotEqual(record['agent_run']['attempt_id'], record['agent_run_history'][-1]['attempt_id'])

    def test_legacy_agent_run_is_not_stamped_with_a_workflow_attempt_id(self):
        self.engine.commands = {'codex': ['fictional-cli'], 'claude': ['fictional-cli']}
        self.engine.usage_snapshot = lambda: {}
        job = self.store.put('job', dict(id='legacy-job', goal='Fictional legacy', project=str(self.project),
                                       status='running', route='full', auto_execute=False))
        task = self.engine.new_task(job, 'Legacy build', 'Fictional only', 'builder')
        task = self.store.update(task['id'], attempt=1, status='running')
        ctx = Context(self.engine, task)
        ctx.prepare_agent(load_agent('builder', task['profile']['adapter']))
        self.assertNotIn('attempt_id', self.store.get(task['id'])['agent_run'])


if __name__ == '__main__':
    unittest.main()
