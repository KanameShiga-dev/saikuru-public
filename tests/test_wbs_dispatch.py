"""Offline single-series reservations and synthetic feature-review receipts."""
import copy
import hashlib
import threading
import time
import unittest
from unittest.mock import patch

from team_store import StaleAttempt, Store
from team_wbs import dispatch_state, digest, task_evidence_digest


class DispatchTests(unittest.TestCase):
    def setUp(self):
        from tests.test_workspace_boundary import WorkspaceTests
        self.fixture = WorkspaceTests(); self.addCleanup(self.fixture.doCleanups); self.fixture.setUp()
        self.base, self.first = self.fixture.bind_engine()
        self.engine, self.store, self.job_id = self.base.engine, self.base.store, self.base.job_id
        self.engine.paused = False

    def job(self): return self.store.get(self.job_id,'job')
    def tasks(self): return self.store.for_job('task',self.job_id)
    def plan_tasks(self): return sorted([t for t in self.tasks() if t.get('work_package_id') and type(t.get('plan_task_index')) is int],key=lambda t:t['plan_task_index'])
    def view(self): return self.engine.workflow_dispatch_view(self.job_id)
    def state(self):
        return (self.store.db.execute('SELECT * FROM objects ORDER BY id').fetchall(),
                self.store.db.execute('SELECT * FROM events ORDER BY seq').fetchall())

    def fake_feature(self):
        """Simulate W04.05's server decision; this is not a real user receipt."""
        first = self.store.get(self.first['id'])
        record = {'path':'app.py','sha256':hashlib.sha256((self.fixture.project/'app.py').read_bytes()).hexdigest()}
        first = self.store.update(first['id'],status='succeeded',result={'status':'done','artifacts':['app.py']},
                                  workflow_artifact_records=[record])
        self.store.update(self.job_id,p0_artifacts=[record])
        review = copy.deepcopy(first); review.pop('state_revision',None)
        review.update(id='fictional-wp-review',role='reviewer',agent_name='common-reviewer',
                      result={'status':'done','checks':['Fictional offline validation']},plan_task_index=None)
        review = self.store.put('task',review)
        wp = self.store.get(first['work_package_id'])
        plan = self.store.get(self.job()['plan_approval_id'])
        requirements = self.store.get(self.job()['requirements_approval_id'])
        target = {'schema_version':1,'job_id':self.job_id,'work_package_id':wp['id'],
            'requirement_revision':self.job()['requirement_revision'],'plan_revision':self.job()['plan_revision'],
            'requirements_approval_digest':requirements['digest'],'plan_approval_digest':plan['digest'],
            'criteria':{'goal_links':wp['goal_links'],'done_when':wp['done_when']},'artifacts':[record],
            'source_snapshot':{'app.py':record['sha256']},'test_snapshot':{'app.py':record['sha256']},
            'evidence_refs':{t['id']:task_evidence_digest(t) for t in (first,review)},
            'blocking_refs':[],'dependency_targets':{}}
        target_hash = digest(target)
        approval = self.engine.new_approval(review,'feature_review',{'feature_target':target,'feature_target_digest':target_hash})
        self.store.update(approval['id'],status='approved',intended_role='end_user',review_acknowledgement={
            'action':'feature_review_confirmation','actor':'authenticated_local_app_session','target_digest':target_hash})
        self.store.update(wp['id'],execution_state='succeeded',review_status='approved',
                          feature_review_approval_id=approval['id'],feature_target_digest=target_hash)
        self.store.event('feature_review_decided','Fictional offline decision, not human acceptance',review['id'],self.job_id,
            {'approval_id':approval['id'],'work_package_id':wp['id'],'target_digest':target_hash,'decision':'approved',
             'actor':'authenticated_local_app_session','requirement_revision':self.job()['requirement_revision'],
             'plan_revision':self.job()['plan_revision']})
        return self.store.get(approval['id']), self.store.get(wp['id'])

    def test_view_is_readonly_and_first_candidate_is_the_only_one(self):
        before = self.state(); result = self.view()
        self.assertEqual(result['status'],'ready')
        self.assertEqual(result['candidate_task_id'],self.first['id'])
        self.assertEqual(self.state(),before)

    def test_atomic_reservation_and_second_tick_do_not_start_twice(self):
        with patch.object(self.engine,'_start_task_thread') as thread:
            self.engine.tick(); self.engine.tick()
        self.assertEqual(thread.call_count,1)
        current = self.store.get(self.first['id'])
        self.assertEqual(current['status'],'running')
        self.assertEqual(current['attempt'],1)
        self.assertTrue(current['current_attempt']['workflow_resources'])
        self.assertEqual(self.view()['reasons'],['workspace_or_series_busy'])

    def test_two_reservations_compete_and_only_one_wins(self):
        observed = self.view(); barrier = threading.Barrier(2); results = []
        def reserve():
            barrier.wait()
            try: self.engine._reserve_workflow_attempt(self.first,observed); results.append('reserved')
            except StaleAttempt: results.append('stale')
        threads = [threading.Thread(target=reserve) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=10)
        self.assertEqual(sorted(results),['reserved','stale'])

    def test_changed_state_after_observation_rejects_without_side_effects(self):
        observed = self.view(); self.store.update(self.job_id,title='Fictional changed title')
        before = self.state()
        with self.assertRaises(StaleAttempt): self.engine._reserve_workflow_attempt(self.first,observed)
        self.assertEqual(self.state(),before)

    def test_task_success_and_wp_status_alone_never_unlock_dependency(self):
        self.store.update(self.first['id'],status='succeeded')
        wp = self.store.get(self.first['work_package_id'])
        self.store.update(wp['id'],execution_state='succeeded',review_status='approved')
        result = self.view()
        self.assertNotEqual(result['status'],'ready')

    def test_current_feature_receipt_and_actual_files_unlock_next_task(self):
        self.fake_feature(); result = self.view()
        self.assertEqual(result['status'],'ready')
        self.assertEqual(result['candidate_task_id'],self.plan_tasks()[1]['id'])
        self.assertTrue(result['descriptor']['dependency_targets'])

    def test_changed_artifact_after_review_keeps_dependency_waiting(self):
        self.fake_feature(); (self.fixture.project/'app.py').write_text('fictional changed artifact\n',encoding='utf-8')
        self.assertNotEqual(self.view()['status'],'ready')

    def test_ack_decision_and_evidence_changes_invalidate_old_review(self):
        approval, wp = self.fake_feature()
        for changes in ({'review_acknowledgement':{}},{'status':'denied'}):
            original = self.store.get(approval['id']); self.store.update(approval['id'],**changes)
            self.assertNotEqual(self.view()['status'],'ready')
            self.store.put('approval',original)
        first = self.store.get(self.first['id']); self.store.update(first['id'],result={'status':'done','artifacts':[]})
        self.assertNotEqual(self.view()['status'],'ready')

    def test_handed_off_is_not_success_for_new_flow_after(self):
        self.store.update(self.base.planner['id'],status='handed_off')
        self.assertNotEqual(self.view()['status'],'ready')

    def test_pending_decision_and_recovery_hold_are_visible(self):
        pending = self.engine.new_approval(self.first,'question',{'questions':[]})
        self.assertEqual(self.view()['reasons'],['pending_decision'])
        self.store.update(pending['id'],status='denied')
        self.store.update(self.first['id'],recovery_check_required=True)
        result = self.view()
        self.assertIn('recovery_confirmation_required',result['waiting'][0]['reasons'])

    def test_unauthorized_instruction_change_waits_but_recorded_user_supplement_is_preserved(self):
        from team_instructions import compact_instruction, instruction_revision
        original = self.store.get(self.first['id'])
        self.store.update(self.first['id'],instruction='Fictional unapproved replacement')
        self.assertNotEqual(self.view()['status'],'ready')
        current = self.store.get(self.first['id'])
        self.store.update(current['id'],instruction=original['instruction'])
        current = self.store.get(current['id'])
        revised = compact_instruction(current['instruction'],'Fictional user retry note')
        self.store.update(current['id'],**instruction_revision(current,revised,1,'再試行前の指示整理','Fictional user retry note'))
        self.assertEqual(self.view()['status'],'ready')

    def test_no_native_context_without_dispatch_reservation(self):
        task = self.store.reserve_attempt(self.first['id'],self.first['state_revision'])
        with patch('team_engine.Context') as context:
            self.engine._run(task)
        context.assert_not_called()
        self.assertEqual(self.store.get(task['id'])['failure_code'],'workflow_dispatch')

    def test_reservation_rechecks_before_native_context(self):
        task = self.engine._reserve_workflow_attempt(self.first,self.view())
        self.engine._assert_workflow_dispatch(task)
        self.fixture.git(self.fixture.project,'checkout','-b','codex/fictional-after-reservation')
        with patch('team_engine.Context') as context:
            self.engine._run(task)
        context.assert_not_called()

    def replan_inputs(self, same_package=False):
        record = {'path':'app.py','sha256':hashlib.sha256((self.fixture.project/'app.py').read_bytes()).hexdigest()}
        self.store.update(self.job_id,p0_artifacts=[record])
        payload = copy.deepcopy(self.base.payload)
        payload['work_packages'][0]['input_artifacts'] = [dict(record,job_id=self.job_id)]
        if same_package:
            payload['work_packages'] = payload['work_packages'][:1]
            payload['tasks'][1]['wbs_id'] = payload['tasks'][0]['wbs_id']
        result = self.engine.propose_plan_revision(self.job_id,payload,self.job()['state_revision'],actor='authenticated_local_app_session')
        self.base.approve(self.store.get(result['approval_id']))
        current = [t for t in self.tasks() if t.get('plan_revision') == self.job()['plan_revision'] and t.get('plan_task_index') == 0]
        self.first = current[0]
        return record

    def test_current_inputs_checked_without_breaking_historical_wp_view(self):
        self.replan_inputs()
        self.assertEqual(self.view()['status'],'ready')
        (self.fixture.project/'app.py').write_text('fictional changed input\n',encoding='utf-8')
        self.assertNotEqual(self.view()['status'],'ready')
        self.assertTrue(self.engine.work_package_view(self.job_id)['materialized'])

    def test_input_change_after_reservation_prevents_context_creation(self):
        self.replan_inputs()
        task = self.engine._reserve_workflow_attempt(self.first,self.view())
        (self.fixture.project/'app.py').write_text('fictional changed after reservation\n',encoding='utf-8')
        with patch('team_engine.Context') as context: self.engine._run(task)
        context.assert_not_called()

    def test_two_independent_db_connections_cannot_reserve_same_series(self):
        from team_engine import Engine
        other = Engine.__new__(Engine); other.store = Store(self.store.directory); other.active = {}
        self.addCleanup(other.store.db.close)
        observed = self.view(); barrier = threading.Barrier(2); results = []
        def reserve(engine):
            barrier.wait()
            try: engine._reserve_workflow_attempt(self.first,observed); results.append('reserved')
            except StaleAttempt: results.append('stale')
        threads = [threading.Thread(target=reserve,args=(e,)) for e in (self.engine,other)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=10)
        self.assertEqual(sorted(results),['reserved','stale'])

    def test_shared_integration_resource_blocks_other_worktree_until_fenced(self):
        from team_p0_contract import initial
        import subprocess, sys
        result = subprocess.run([sys.executable,str(self.fixture.home/'new_worktree.py'),'--name','second',
            '--branch','codex/fictional-second','--create-branch'],env=self.fixture.env,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0)
        project = self.fixture.home/'Worktrees/second'
        clone = copy.deepcopy(self.job()); clone.pop('state_revision',None)
        clone.update(id='fictional-second-job',project=str(project),status='awaiting_approval',
                     goal_contract=initial(clone['goal']),plan_revision=0,plan_approval_id=None,
                     requirement_revision=0,requirements_approval_id=None,p0_artifacts=[])
        clone = self.store.put('job',clone)
        self.engine.config['approved_roots'].append(str(project))
        planner = self.engine.new_task(clone,'Fictional other plan','Fictional independent work','planner')
        planner = self.store.update(planner['id'],status='succeeded',result=self.base.payload)
        approval = self.engine._new_workflow_plan_approval(planner,self.base.payload)
        self.base.approve(approval)
        first = self.engine._reserve_workflow_attempt(self.first,self.view())
        self.assertEqual(self.engine.workflow_dispatch_view(clone['id'])['reasons'],['workspace_or_series_busy'])
        self.store.update(first['id'],status='cancelled')
        self.assertEqual(self.engine.workflow_dispatch_view(clone['id'])['reasons'],['workspace_or_series_busy'])
        self.store.close_attempt(first['id'],'fenced','fictional stopped writer')
        self.assertEqual(self.engine.workflow_dispatch_view(clone['id'])['status'],'ready')

    def test_completion_kind_or_later_non_user_decision_never_unlocks_feature(self):
        approval, wp = self.fake_feature()
        original = self.store.get(approval['id'])
        self.store.update(approval['id'],kind='completion')
        self.assertNotEqual(self.view()['status'],'ready')
        self.store.put('approval',original)
        self.store.event('feature_review_decided','Fictional wrong actor',None,self.job_id,
            {'approval_id':approval['id'],'actor':'worker','decision':'approved'})
        self.assertNotEqual(self.view()['status'],'ready')

    def test_released_dependency_is_reserved_once_on_tick(self):
        self.fake_feature()
        with patch.object(self.engine,'_start_task_thread') as thread:
            self.engine.tick(); self.engine.tick()
        self.assertEqual(thread.call_count,1)
        self.assertEqual(thread.call_args.args[0]['id'],self.plan_tasks()[1]['id'])
        self.engine._assert_workflow_dispatch(thread.call_args.args[0])

    def test_same_wp_registered_output_can_advance_input_but_external_registry_update_cannot(self):
        self.replan_inputs(same_package=True)
        (self.fixture.project/'app.py').write_text('fictional authorized first step\n',encoding='utf-8')
        record = {'path':'app.py','sha256':hashlib.sha256((self.fixture.project/'app.py').read_bytes()).hexdigest()}
        self.store.update(self.first['id'],status='succeeded',result={'artifacts':['app.py']},workflow_artifact_records=[record])
        self.store.update(self.job_id,p0_artifacts=[record])
        self.assertEqual(self.view()['status'],'ready')
        (self.fixture.project/'app.py').write_text('fictional external update\n',encoding='utf-8')
        changed = dict(record,sha256=hashlib.sha256((self.fixture.project/'app.py').read_bytes()).hexdigest())
        self.store.update(self.job_id,p0_artifacts=[changed])
        self.assertNotEqual(self.view()['status'],'ready')

    def test_retry_delay_is_a_wait_reason_without_mutating_state(self):
        self.store.update(self.first['id'],retry_at=time.time()+600)
        before = self.state(); result = self.view()
        self.assertIn('retry_wait',result['waiting'][0]['reasons'])
        self.assertEqual(self.state(),before)


if __name__ == '__main__': unittest.main()
