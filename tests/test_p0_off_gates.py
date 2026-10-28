"""The G1/G2/G3 gates with the P0 contract switched off (p0_contract_enabled = false).

2026-10-19: a production folder keeps the P0 contract off. The gates must not depend on it: the AI-start approval
(G2) is created and single-use, and the plan approval (G3) is issued after the plan, as for a P0 job.
This subclasses the existing gate suite, so every one of its tests also runs with the switch off, and adds
explicit checks for the records Codex listed as not yet covered. Synthetic temporary data; no provider starts.
"""
import unittest
from unittest.mock import patch

from tests import test_g1_g2_gate as gate_tests


class P0OffGateTests(gate_tests.G1G2GateTests):
    def setUp(self):
        super().setUp()
        self.engine.config['p0_contract_enabled'] = False

    def test_a_request_is_a_legacy_job_with_the_gates_in_place(self):
        job = self.create_job()
        saved = self.store.get(job['id'], 'job')
        self.assertNotIn('p0_contract_version', saved)
        self.assertNotIn('goal_contract', saved)
        self.assertEqual(saved['g2_gate_version'], 1)
        self.assertEqual(saved['g2_status'], 'pending')
        self.assertEqual(saved['status'], 'awaiting_approval')
        pending = [a for a in self.store.for_job('approval', job['id']) if a['status'] == 'pending']
        self.assertEqual([a['kind'] for a in pending], ['ai_start'])
        self.assertEqual(self.planner(job['id'])['status'], 'queued')

    def test_the_plan_approval_g3_is_issued_after_g2_with_the_switch_off(self):
        job = self.create_job()
        task = self.planner(job['id'])
        self.engine.decide(self.g2(job['id'])['id'], True, 'Synthetic explicit G2 approval')
        self.assertEqual(self.store.get(job['id'])['g2_status'], 'approved')
        self.store.update(task['id'], status='running', attempt=1)
        self.assertTrue(self.engine._claim_g2_for_run(task['id'], 1))
        self.store.update(job['id'], g2_status='consumed')
        with patch('team_plan_check.issues', return_value=[]), \
                patch('team_document_capabilities.require_plan'):
            self.engine._finish(task, {'status': 'done', 'summary': 'Synthetic plan only', 'tasks': []})
        pending = [a for a in self.store.for_job('approval', job['id']) if a['status'] == 'pending']
        self.assertEqual([a['kind'] for a in pending], ['plan'])
        self.assertEqual(self.store.get(job['id'])['status'], 'awaiting_approval')
        self.assertNotIn('p0_contract_version', self.store.get(job['id']))


class WorkflowProgressTests(unittest.TestCase):
    """One fictional current workflow, then read-only record mutations per case."""
    @classmethod
    def setUpClass(cls):
        import copy
        from tests.test_wbs_rework import ReworkTests
        cls.fixture = ReworkTests(); cls.addClassCleanup(cls.fixture.doCleanups); cls.fixture.setUp()
        f = cls.fixture; cls.snapshots = {}
        def capture(name):
            cls.snapshots[name] = copy.deepcopy({'job':f.job(),**f.engine._workflow_projection_records(f.job())})
        # A real dependency chain plus a new plan leaves cancelled history;
        # exercise the record-only dependency reader and current-cycle gate.
        payload = copy.deepcopy(f.fixture.payload)
        payload['checkpoint_paths'] += ['other.py','tests/test_other.py']
        (f.workspace.project/'other.py').write_text('Fictional dependent artifact\n',encoding='utf-8')
        (f.workspace.project/'tests/test_other.py').write_text(
            'import unittest\nfrom pathlib import Path\nclass OtherTest(unittest.TestCase):\n'
            ' def test_other(self): self.assertTrue(Path("other.py").read_bytes())\n',encoding='utf-8')
        plan = f.fixture.propose(payload); f.fixture.approve(plan)
        f.first = next(t for t in f.tasks() if t.get('plan_task_index') == 0 and t.get('plan_revision') == f.job()['plan_revision'])
        f.approve(f.ready_feature())
        for _ in range(3): f.finish_next()
        wp = next(w for w in f.store.for_job('work_package',f.job_id)
                  if w['plan_revision'] == f.job()['plan_revision'] and w['id'] != f.wp()['id'])
        review = f.engine.request_feature_review(f.job_id,wp['id'],expected_wp_revision=wp['state_revision'])
        f.approve(review); capture('features')
        f.finish_next(); f.finish_next(); capture('technical')
        approval = [a for a in f.store.for_job('approval',f.job_id) if a['kind'] == 'completion'][-1]
        while approval['payload'].get('pending_items'):
            answer = f.engine.record_completion_evidence(approval['id'],0,0,'Fictional local operation verified',True)
            approval = f.store.get(answer['approval_id'])
        f.engine.acknowledge_completion_review(approval['id'],True); capture('ready')
        f.engine.accept_completion(approval['id'],'正式受領'); capture('accepted')

    @classmethod
    def tearDownClass(cls): cls.fixture.doCleanups()

    def records(self, stage='accepted'):
        import copy
        return copy.deepcopy(self.snapshots[stage])

    def project(self, records):
        from team_p0_contract import workflow_progress
        return workflow_progress(**records)

    def test_execution_and_feature_success_are_not_uat_or_whole_receipt(self):
        for stage in ('features','technical'):
            result = self.project(self.records(stage))
            self.assertEqual(result['counts']['feature_approved'],2)
            self.assertGreater(result['tasks']['execution_succeeded'],0)
            self.assertEqual(result['uat']['confirmed'],0)
            self.assertFalse(result['complete'])

    def test_formal_ready_and_received_states_use_separate_typed_evidence(self):
        ready = self.project(self.records('ready'))
        self.assertTrue(ready['ready_for_formal_acceptance'])
        self.assertFalse(ready['formal_receipt'])
        accepted = self.project(self.records())
        self.assertTrue(accepted['complete'])
        self.assertEqual(accepted['uat']['confirmed'],accepted['uat']['required'])
        self.assertGreater(accepted['historical_tasks'],0)
        self.assertEqual(accepted['tasks']['incomplete'],0)
        self.assertFalse(accepted['filesystem_verified'])

    def test_handoff_interruption_and_pending_checks_remain_incomplete(self):
        for state in ('handed_off','interrupted','pending'):
            records = self.records()
            task = next(t for t in records['tasks'] if t.get('work_package_id') and t.get('plan_revision') == records['job']['plan_revision'])
            if state == 'pending': task['result']['pending_checks'] = ['Fictional missing check']
            else: task['status'] = state
            result = self.project(records)
            self.assertFalse(result['complete'])
            self.assertEqual(result['uat']['confirmed'],0)
            self.assertGreater(result['tasks']['incomplete'],0)

    def test_active_attempt_after_report_is_not_completed(self):
        records = self.records()
        task = next(t for t in records['tasks'] if t['id'] in self.project(records)['current_task_ids'])
        task['current_attempt'] = dict(task.get('current_attempt') or {},status='running')
        result = self.project(records)
        self.assertGreater(result['tasks']['running'],0)
        self.assertFalse(result['complete'])

    def test_approved_flags_without_feature_decision_evidence_do_not_count(self):
        records = self.records()
        records['events'] = [e for e in records['events'] if e['type'] != 'feature_review_decided']
        result = self.project(records)
        self.assertEqual(result['counts']['feature_approved'],0)
        self.assertFalse(result['formal_receipt'])

    def test_stale_latest_feature_actor_invalidates_its_receipt(self):
        import copy
        records = self.records()
        event = copy.deepcopy(next(e for e in records['events'] if e['type'] == 'feature_review_decided'))
        event['at'] += 1000; event['details']['actor'] = 'worker'; records['events'].append(event)
        self.assertFalse(self.project(records)['complete'])

    def test_wall_clock_rollback_does_not_resurrect_an_older_feature_decision(self):
        import copy
        records = self.records()
        event = copy.deepcopy(next(e for e in records['events'] if e['type'] == 'feature_review_decided'))
        event['at'] -= 1000; event['details']['actor'] = 'worker'; records['events'].append(event)
        self.assertFalse(self.project(records)['complete'])

    def test_superseded_or_unconfirmed_whole_receipt_is_not_current(self):
        for mutation in ('superseded','no_event','no_ack'):
            records = self.records()
            approval = [a for a in records['approvals'] if a['kind'] == 'completion'][-1]
            if mutation == 'superseded': approval['status'] = 'superseded'
            elif mutation == 'no_ack': approval.pop('review_acknowledgement')
            else: records['events'] = [e for e in records['events'] if e['type'] != 'formal_acceptance_decided']
            self.assertFalse(self.project(records)['complete'])

    def test_changed_requirement_or_generation_does_not_carry_old_success(self):
        for mutation in ('requirement','generation'):
            records = self.records()
            if mutation == 'requirement': records['job']['goal_contract']['primary_goal'] = 'Fictional changed goal'
            else:
                wp = next(w for w in records['work_packages'] if w['plan_revision'] == records['job']['plan_revision'])
                wp['workflow_generation'] = wp.get('workflow_generation',0)+1
            self.assertFalse(self.project(records)['complete'])

    def test_foreign_records_do_not_affect_current_job(self):
        import copy
        records = self.records(); expected = self.project(records)
        task = copy.deepcopy(records['tasks'][-1]); task.update(job_id='fictional-other-job',status='interrupted')
        records['tasks'].append(task)
        approval = copy.deepcopy(records['approvals'][-1]); approval.update(job_id='fictional-other-job',status='pending')
        records['approvals'].append(approval)
        self.assertEqual(self.project(records),expected)

    def test_missing_records_or_unknown_marker_never_fall_back_to_accepted(self):
        from team_p0_contract import progress
        from team_quality_view import quality_view
        records = self.records()
        result = progress(records['job'],records['tasks'],records['approvals'])
        self.assertFalse(result['workflow']['complete'])
        self.assertNotEqual(result['completion_status'],'accepted')
        self.assertEqual(quality_view(records['job'],records['tasks'],records['approvals'])['overall'],'inconclusive')
        for version in (True,0,2,None):
            records['job']['workflow_contract_version'] = version
            self.assertEqual(self.project(records)['state'],'inconclusive')

    def test_projection_is_readonly_and_has_no_file_or_database_reads(self):
        import copy
        records = self.records(); before = copy.deepcopy(records)
        with patch('team_p0_contract.artifact_records',side_effect=AssertionError('file access')):
            first = self.project(records); second = self.project(records)
        self.assertEqual(first,second)
        self.assertEqual(records,before)

    def test_quality_separates_whole_workflow_from_technical_passes(self):
        from team_quality_view import quality_view
        records = self.records('technical')
        view = quality_view(**records)
        self.assertNotEqual(view['overall'],'passed')
        self.assertFalse(view['approval']['accepted'])
        records = self.records()
        accepted = quality_view(**records)
        self.assertTrue(accepted['workflow']['complete'])
        self.assertEqual(accepted['overall'],'passed')

    def test_legacy_and_p0_off_projection_shapes_stay_unchanged(self):
        from team_p0_contract import progress, _legacy_progress
        from team_quality_view import quality_view
        records = self.records()
        records['job'].pop('workflow_contract_version')
        self.assertEqual(progress(**records),_legacy_progress(records['job'],records['tasks'],records['approvals']))
        self.assertNotIn('workflow',quality_view(**records))
        records['job'].pop('p0_contract_version')
        self.assertNotIn('workflow',progress(**records))

    def test_new_global_review_cycle_cannot_reuse_older_received_cycle(self):
        import copy
        records = self.records()
        old_ids = {t['id'] for t in records['tasks'] if t.get('workflow_job_task')}
        template = next(t for t in records['tasks'] if t.get('workflow_job_task')
                        and t.get('plan_revision') == records['job']['plan_revision'] and t['status'] != 'cancelled')
        created = max(t.get('created_at',0) for t in records['tasks'])+100
        fix = copy.deepcopy(template)
        fix.update(id='fictional-new-global-fix',role='builder',status='queued',result=None,
                   current_attempt=None,created_at=created)
        review = copy.deepcopy(fix)
        review.update(id='fictional-new-global-review',role='reviewer',review_cycle=fix['id'],after=fix['id'],created_at=created+1)
        records['tasks'] += [fix,review]
        result = self.project(records)
        self.assertFalse(old_ids & set(result['current_task_ids']))
        self.assertFalse(result['complete'])
        self.assertGreater(result['tasks']['incomplete'],0)

    def test_wp_state_change_invalidates_review_binding_without_file_change(self):
        from team_p0_contract import workflow_state_digest
        records = self.records()
        def token(): return workflow_state_digest(records['job'],records['tasks'],records['approvals'],records['work_packages'])
        before = token()
        wp = next(w for w in records['work_packages'] if w['plan_revision'] == records['job']['plan_revision'])
        wp['state_revision'] += 1
        self.assertNotEqual(token(),before)
        self.assertFalse(self.project(records)['complete'])

    def test_changed_project_binding_does_not_reuse_the_previous_receipt(self):
        records = self.records(); records['job']['project'] = 'fictional-different-project'
        projected = self.project(records)
        self.assertFalse(projected['complete'])
        self.assertEqual(projected['uat']['confirmed'],0)

    def test_engine_target_token_binds_work_package_state_as_well_as_files(self):
        f = self.fixture
        approval = [a for a in f.store.for_job('approval',f.job_id) if a['kind'] == 'completion'][-1]
        before = f.engine._acceptance_target_token(f.job())
        self.assertEqual(before,approval['review_acknowledgement']['target_version_token'])
        wp = next(w for w in f.store.for_job('work_package',f.job_id) if w['plan_revision'] == f.job()['plan_revision'])
        f.store.update(wp['id'],invalidation_reason='Fictional metadata changed after review')
        self.assertNotEqual(before,f.engine._acceptance_target_token(f.job()))

    def test_conditional_receipt_does_not_hide_remaining_work(self):
        from team_p0_contract import progress
        records = self.records(); records['job']['status'] = 'accepted_with_pending_checks'
        projected = progress(**records)
        self.assertFalse(projected['workflow']['complete'])
        self.assertEqual(projected['completion_status'],'accepted_with_pending_checks')
        self.assertTrue(projected['remaining'])

    def test_pending_uat_keeps_evidence_actions_without_offering_early_acceptance(self):
        from team_p0_contract import progress
        pending = progress(**self.records('technical'))
        self.assertIn('record_completion_evidence',pending['available_actions'])
        self.assertIn('review_completion',pending['available_actions'])
        self.assertNotIn('accept_completion',pending['available_actions'])
        ready = progress(**self.records('ready'))
        self.assertIn('accept_completion',ready['available_actions'])
        self.assertEqual(ready['unmet_acceptance_criteria'],[])

    def test_unapproved_future_plan_preserves_receipt_but_blocks_new_acceptance(self):
        for stage in ('accepted','ready'):
            records = self.records(stage)
            records['approvals'].append({'id':'fictional-future-plan','job_id':records['job']['id'],
                'kind':'plan','status':'pending','payload':{'summary':'Fictional future proposal'}})
            projected = self.project(records)
            self.assertEqual(projected['complete'],stage == 'accepted')
            self.assertFalse(projected['ready_for_formal_acceptance'])


if __name__ == '__main__':
    unittest.main()
