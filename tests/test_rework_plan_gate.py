"""Offline regressions for plan consent and terminal cancellation boundaries."""
import tempfile
import unittest
from pathlib import Path

from team_config import default_config
from team_engine import Engine
from team_store import Store
from team_p0_contract import initial


class ReworkPlanGateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='fictional-rework-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / 'runtime')
        config = default_config()
        config['approved_roots'] = [str(self.root)]
        self.engine = Engine(self.store, config, {}, 'http://127.0.0.1:0')
        self.engine.paused = True
        self.addCleanup(self.store.db.close)
        self.addCleanup(self.engine.handoff.close)

    def job(self, p0, **extra):
        return self.store.put('job', dict(id='fictional-job', title='Fictional lookup',
            goal='Use fictional products only', project=str(self.root), status='blocked',
            auto_execute=False, created_at=1, g2_gate_version=1,
            **({'p0_contract_version': 1, 'goal_contract': initial('Use fictional products only')} if p0 else {}), **extra))

    def receipt(self, task, status):
        approval = self.engine.new_approval(task, 'plan', {'summary': 'Fictional plan'})
        return self.store.update(approval['id'], status=status)

    def finished_planner(self, job):
        task = self.engine.new_task(job, 'Fictional plan', 'Plan only', 'planner')
        return self.store.update(task['id'], status='succeeded', finished_at=2)

    def test_missing_or_latest_unapproved_plan_cannot_create_rework(self):
        for p0 in (False, True):
            for status in (None, 'rejected', 'pending', 'superseded'):
                with self.subTest(p0=p0, latest=status):
                    job = self.job(p0)
                    planner = self.finished_planner(job)
                    if status is not None:
                        self.receipt(planner, 'approved')
                        self.receipt(planner, status)
                    before = self.store.for_job('task', job['id'])
                    with self.assertRaisesRegex(ValueError, '承認済み'):
                        self.engine.rework_from_rejection(job['id'], 'Revise fictional output')
                    self.assertEqual(self.store.for_job('task', job['id']), before)
                    self.assertEqual(self.store.get(job['id'])['status'], 'blocked')
                    # Each subcase has its own job graph; retain no previous receipts.
                    self.tearDown_graph()

    def tearDown_graph(self):
        # Recreate the fictional Store for the next subcase via normal fixture lifecycle.
        self.doCleanups()
        self.setUp()

    def test_approved_plan_still_allows_rework(self):
        for p0 in (False, True):
            with self.subTest(p0=p0):
                job = self.job(p0)
                planner = self.finished_planner(job)
                self.receipt(planner, 'approved')
                result = self.engine.rework_from_rejection(job['id'], 'Fix fictional output')
                self.assertEqual(self.store.get(result['task_id'])['role'], 'builder')
                self.assertEqual(self.store.get(job['id'])['status'], 'running')
                self.tearDown_graph()

    def test_rejected_plan_through_decision_api_cannot_start_builder(self):
        for p0 in (False, True):
            with self.subTest(p0=p0):
                (self.root / 'SCOPE.md').write_text('# Fictional scope\nNo network.\n', encoding='utf-8')
                self.engine.config['p0_contract_enabled'] = p0
                job = self.engine.create_job('Fictional lookup', 'Use fictional products only',
                                             str(self.root), False)
                planner = self.store.for_job('task', job['id'])[0]
                g2 = self.store.for_job('approval', job['id'])[0]
                self.engine.decide(g2['id'], True, 'Fictional consent to planning only')
                planner = self.store.update(planner['id'], status='succeeded', finished_at=2)
                self.store.update(job['id'], status='awaiting_approval')
                approval = self.receipt(planner, 'pending')
                self.engine.decide(approval['id'], False, 'Fictional plan rejected')
                with self.assertRaisesRegex(ValueError, '承認済み'):
                    self.engine.rework_from_rejection(job['id'], 'Revise the rejected plan')
                self.assertEqual(self.store.get(job['id'])['status'], 'blocked')
                self.assertFalse(any(t['role'] == 'builder'
                                     for t in self.store.for_job('task', job['id'])))
                self.tearDown_graph()

    def test_cancelled_g2_job_cannot_retry_any_stage_or_mutate_history(self):
        for p0 in (False, True):
            for role in ('planner', 'builder', 'reviewer'):
                with self.subTest(p0=p0, role=role):
                    job = self.job(p0)
                    planner = self.finished_planner(job)
                    self.receipt(planner, 'approved')
                    task = (planner if role == 'planner' else
                            self.engine.new_task(job, 'Fictional stage', 'No dispatch', role, planner['id']))
                    self.store.update(task['id'], status='running')
                    self.engine.cancel_job(job['id'])
                    before = self.store.get(task['id'])
                    receipts = self.store.for_job('approval', job['id'])
                    with self.assertRaisesRegex(ValueError, '中止した依頼'):
                        self.engine.retry(task['id'], 'Explicit fictional retry')
                    self.assertEqual(self.store.get(task['id']), before)
                    self.assertEqual(self.store.for_job('approval', job['id']), receipts)
                    self.assertEqual(self.store.get(job['id'])['status'], 'cancelled')
                    self.tearDown_graph()

    def test_pre_g2_legacy_rework_and_nonplanner_retry_remain_compatible(self):
        job = self.job(False)
        del job['g2_gate_version']
        self.store.put('job', job)
        self.finished_planner(job)
        result = self.engine.rework_from_rejection(job['id'], 'Legacy fictional revision')
        self.assertEqual(self.store.get(result['task_id'])['role'], 'builder')
        # A terminal legacy reviewer without successors retains the old explicit retry.
        final = self.store.for_job('task', job['id'])[-1]
        for task in self.store.for_job('task', job['id']):
            self.store.update(task['id'], status='succeeded')
        self.store.update(final['id'], status='running')
        self.engine.cancel_job(job['id'])
        self.engine.retry(final['id'], 'Legacy explicit retry')
        self.assertEqual(self.store.get(final['id'])['status'], 'queued')
        self.assertEqual(self.store.get(job['id'])['status'], 'running')


if __name__ == '__main__':
    unittest.main()
