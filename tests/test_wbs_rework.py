"""Fictional feature decisions, bounded repairs and preserved independent evidence."""
import copy
import hashlib
import json
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from team_wbs import (digest, dispatch_state, feature_review_receipt, approved_source, repair_impact,
                      package_members, task_evidence_digest)


class ReworkTests(unittest.TestCase):
    def setUp(self):
        from tests.test_workspace_boundary import WorkspaceTests
        from tests.test_plan_revision import RevisionTests
        self.workspace = WorkspaceTests(); self.addCleanup(self.workspace.doCleanups); self.workspace.setUp()
        self.fixture = RevisionTests(); self.addCleanup(self.fixture.doCleanups); self.fixture.setUp()
        f = self.fixture; self.engine, self.store, self.job_id = f.engine, f.store, f.job_id
        self.store.update(f.approval['id'],status='superseded')
        self.store.update(f.job_id,project=str(self.workspace.project),validation_commands=[])
        self.engine.config['approved_roots'].append(str(self.workspace.project))
        self.engine.config['security_review'] = 'always'; self.engine.paused = False
        (self.workspace.project/'tests').mkdir()
        (self.workspace.project/'tests/test_app.py').write_text(
            'import unittest\nfrom pathlib import Path\nclass ArtifactTest(unittest.TestCase):\n'
            ' def test_artifact(self): self.assertTrue(Path("app.py").read_bytes())\n',encoding='utf-8')
        f.payload['checkpoint_paths'].append('tests/test_app.py')
        f.payload['code_test_plan']['command'] = 'python -B -m unittest discover -s tests'
        f.planner = self.store.update(f.planner['id'],result=copy.deepcopy(f.payload))
        f.approval = self.engine._new_workflow_plan_approval(f.planner,f.payload); f.approve()
        self.first = next(t for t in self.tasks() if t.get('plan_task_index') == 0)

    def job(self): return self.store.get(self.job_id,'job')
    def tasks(self): return self.store.for_job('task',self.job_id)
    def wp(self): return self.store.get(self.first['work_package_id'],'work_package')
    def state(self):
        return (self.store.db.execute('SELECT * FROM objects ORDER BY id').fetchall(),
                self.store.db.execute('SELECT * FROM events ORDER BY seq').fetchall())

    def finish_next(self, status='done', summary='Fictional inspected result'):
        view = self.engine.workflow_dispatch_view(self.job_id)
        self.assertEqual(view['status'],'ready',view)
        task = self.store.get(view['candidate_task_id'],'task')
        task = self.engine._reserve_workflow_attempt(task,view)
        if task['role'] == 'builder':
            from team_engine import Context
            command = self.job()['code_test_plan']['command']
            ran = subprocess.run([sys.executable,'-B','-m','unittest','discover','-s','tests'],
                                 cwd=self.workspace.project,capture_output=True,text=True,timeout=15)
            self.assertEqual(ran.returncode,0)
            context = Context.__new__(Context); context.engine = self.engine; context.task = task
            context.command_result(command,ran.returncode,'Fictional offline unit test completed')
        artifact = 'other.py' if (self.workspace.project/'other.py').exists() and task.get('work_package_id') != self.first['work_package_id'] else 'app.py'
        tests = sorted(p.relative_to(self.workspace.project).as_posix() for p in (self.workspace.project/'tests').glob('test_*.py'))
        artifacts = [artifact]
        if getattr(self,'report_owned_test_as_artifact',False):
            artifacts.append('tests/test_app.py' if artifact == 'app.py' else 'tests/test_other.py')
        result = {'status':status,'summary':summary,'checks':['Fictional inspection'],
                  'artifacts':artifacts if task['role'] == 'builder' else [],
                  'test_files':tests,'test_change_reason':'Fictional approved test','blocking_reasons':[],
                  'pending_checks':[],'questions':[],'question':'','context_updates':[]}
        self.engine._finish(task,result)
        self.store.close_attempt(task['id'],'closed','fictional_offline_result')
        return self.store.get(task['id'])

    def ready_feature(self):
        for _ in range(3): self.finish_next()
        wp = self.wp()
        self.assertEqual((wp['execution_state'],wp['review_status']),('succeeded','not_requested'))
        return self.engine.request_feature_review(self.job_id,wp['id'],expected_wp_revision=wp['state_revision'])

    def confirmation(self, review, allow=True):
        return {'action':'feature_review_confirmation','target_digest':review['target_digest'],
                'decision':'approved' if allow else 'rejected'}

    def approve(self,review):
        return self.engine.decide_feature_review(review['approval_id'],True,self.confirmation(review))

    def independent_features(self):
        payload = copy.deepcopy(self.fixture.payload)
        payload['work_packages'][1]['depends_on'] = []
        payload['checkpoint_paths'] += ['other.py','tests/test_other.py']
        (self.workspace.project/'other.py').write_text('Fictional independent artifact\n',encoding='utf-8')
        (self.workspace.project/'tests/test_other.py').write_text(
            'import unittest\nfrom pathlib import Path\nclass OtherTest(unittest.TestCase):\n'
            ' def test_other(self): self.assertTrue(Path("other.py").read_bytes())\n',encoding='utf-8')
        approval = self.fixture.propose(payload); self.fixture.approve(approval)
        self.first = next(t for t in self.tasks() if t.get('plan_task_index') == 0 and t.get('plan_revision') == self.job()['plan_revision'])
        for _ in range(6): self.finish_next()
        reviews = []
        for wp in self.store.for_job('work_package',self.job_id):
            if wp['plan_revision'] != self.job()['plan_revision']: continue
            review = self.engine.request_feature_review(self.job_id,wp['id'],expected_wp_revision=wp['state_revision'])
            self.approve(review); reviews.append(review)
        return reviews

    def rework_approved_first(self,review):
        wp = self.wp()
        return self.engine.request_package_rework(self.job_id,wp['id'],
            {'action':'request_feature_rework','target_digest':review['target_digest']},'Fictional bounded defect',
            expected_wp_revision=wp['state_revision'])

    def test_builder_gets_independent_package_reviews_before_next_feature(self):
        self.finish_next()
        reviews = [t for t in self.tasks() if t.get('workflow_authority',{}).get('kind') == 'package_review']
        self.assertEqual({t['agent_name'] for t in reviews},{'common-reviewer','common-security-reviewer'})
        self.assertTrue(all(t['work_package_id'] == self.wp()['id'] for t in reviews))
        self.assertEqual(self.engine.workflow_dispatch_view(self.job_id)['candidate_task_id'],reviews[0]['id'])

    def test_explicit_feature_decision_unlocks_dependent_without_formal_receipt(self):
        review = self.ready_feature()
        self.assertNotEqual(self.engine.workflow_dispatch_view(self.job_id)['status'],'ready')
        result = self.approve(review)
        self.assertFalse(result['formal_receipt'])
        self.assertEqual(self.job()['status'],'running')
        candidate = self.store.get(self.engine.workflow_dispatch_view(self.job_id)['candidate_task_id'])
        self.assertEqual(candidate['plan_task_index'],1)

    def test_missing_wrong_actor_or_generic_decision_cannot_approve(self):
        review = self.ready_feature(); before = self.state()
        with self.assertRaises(ValueError): self.engine.decide(review['approval_id'],True,'Fictional generic')
        with self.assertRaises(ValueError): self.engine.decide_feature_review(review['approval_id'],True,None)
        with self.assertRaises(ValueError): self.engine.decide_feature_review(review['approval_id'],True,self.confirmation(review),actor='worker')
        self.assertEqual(self.state(),before)

    def test_changed_target_files_reject_without_partial_decision(self):
        review = self.ready_feature(); before = self.state()
        (self.workspace.project/'app.py').write_text('Fictional changed after preview\n',encoding='utf-8')
        with self.assertRaises(ValueError): self.approve(review)
        self.assertEqual(self.state(),before)

    def test_rejection_creates_repair_and_only_affected_rechecks_with_trace(self):
        review = self.ready_feature()
        old = copy.deepcopy(self.store.get(review['approval_id']))
        result = self.engine.decide_feature_review(review['approval_id'],False,self.confirmation(review,False),'Fictional defect')
        self.assertEqual(len(result['affected_ids']),2)
        fixes = [self.store.get(i) for i in result['task_ids']]
        self.assertEqual({t['workflow_authority']['kind'] for t in fixes},{'package_repair','package_recheck'})
        self.assertEqual(self.store.get(review['approval_id'])['payload'],old['payload'])
        view = self.engine.workflow_dispatch_view(self.job_id)
        self.assertEqual(self.store.get(view['candidate_task_id'])['workflow_authority']['kind'],'package_repair')
        self.assertTrue(any(e['type'] == 'feature_defect_recorded' for e in self.store.events()))

    def test_technical_defect_starts_bounded_repair_without_fake_user_confirmation(self):
        self.finish_next(); self.finish_next('needs_changes','Fictional technical defect')
        self.assertEqual(self.wp()['workflow_generation'],1)
        self.assertFalse(any(a['kind'] == 'feature_review' for a in self.store.for_job('approval',self.job_id)))
        self.assertEqual(self.store.get(self.engine.workflow_dispatch_view(self.job_id)['candidate_task_id'])['role'],'builder')

    def test_repair_limit_stops_instead_of_automatic_infinite_loop(self):
        self.engine.config['max_repairs'] = 0
        self.finish_next('needs_changes','Fictional defect cannot repeat')
        self.assertEqual(self.job()['status'],'blocked')
        self.assertEqual(self.wp()['execution_state'],'blocked')

    def test_issued_task_spec_tampering_is_blocked(self):
        self.finish_next()
        review = next(t for t in self.tasks() if t.get('workflow_authority'))
        self.store.update(review['id'],instruction='Fictional unrecorded replacement')
        self.assertNotEqual(self.engine.workflow_dispatch_view(self.job_id)['status'],'ready')

    def test_pending_checks_never_become_feature_approval(self):
        self.ready_feature()
        approval = next(a for a in self.store.for_job('approval',self.job_id) if a['kind'] == 'feature_review')
        self.store.update(approval['id'],status='superseded')
        wp = self.store.update(self.wp()['id'],review_status='not_requested')
        task = next(t for t in self.tasks() if t.get('workflow_authority'))
        self.store.update(task['id'],result=dict(task['result'],pending_checks=['Fictional missing check']))
        before = self.state()
        with self.assertRaises(ValueError): self.engine.request_feature_review(self.job_id,wp['id'],expected_wp_revision=wp['state_revision'])
        self.assertEqual(self.state(),before)

    def test_independent_approved_feature_and_receipt_are_preserved(self):
        reviews = self.independent_features()
        other = next(w for w in self.store.for_job('work_package',self.job_id)
                     if w['plan_revision'] == self.job()['plan_revision'] and w['id'] != self.wp()['id'])
        old_wp = copy.deepcopy(other); old_receipt = self.store.get(other['feature_review_approval_id'])
        result = self.rework_approved_first(reviews[0])
        self.assertEqual(result['affected_ids'],[self.wp()['id']])
        self.assertIn(other['id'],result['preserved_ids'])
        self.assertEqual(self.store.get(other['id']),old_wp)
        self.assertEqual(self.store.get(other['feature_review_approval_id']),old_receipt)

    def test_reported_owned_test_artifact_does_not_invalidate_independent_feature(self):
        self.report_owned_test_as_artifact = True
        reviews = self.independent_features()
        other = next(w for w in self.store.for_job('work_package',self.job_id)
                     if w['plan_revision'] == self.job()['plan_revision'] and w['id'] != self.wp()['id'])
        old_wp = copy.deepcopy(other); old_receipt = self.store.get(other['feature_review_approval_id'])
        target = self.store.get(reviews[0]['approval_id'])['payload']['feature_target']
        self.assertIn('tests/test_app.py',target['source_snapshot'])
        self.assertIn('tests/test_app.py',old_receipt['payload']['feature_target']['test_snapshot'])
        self.assertNotIn('tests/test_app.py',old_receipt['payload']['feature_target']['source_snapshot'])
        result = self.rework_approved_first(reviews[0])
        self.assertEqual(result['affected_ids'],[self.wp()['id']])
        self.assertIn(other['id'],result['preserved_ids'])
        self.assertEqual(self.store.get(other['id']),old_wp)
        self.assertEqual(self.store.get(other['feature_review_approval_id']),old_receipt)

    def test_reported_test_artifact_invalidates_other_evidence_only_when_actually_changed(self):
        self.report_owned_test_as_artifact = True
        reviews = self.independent_features()
        result = self.rework_approved_first(reviews[0]); other_id = result['preserved_ids'][0]
        path = self.workspace.project/'tests/test_app.py'
        path.write_text(path.read_text(encoding='utf-8')+'\n# Fictional actual test change\n',encoding='utf-8')
        self.finish_next()
        self.assertEqual(self.store.get(other_id)['review_status'],'invalidated')
        event = next(e for e in self.store.events() if e['type'] == 'feature_impact_reassessed')
        self.assertIn('tests/test_app.py',event['details']['changed_paths'])
        self.assertIn(other_id,event['details']['additional_ids'])

    def test_actual_shared_test_change_expands_recheck_scope_after_repair(self):
        reviews = self.independent_features(); result = self.rework_approved_first(reviews[0])
        other_id = result['preserved_ids'][0]
        path = self.workspace.project/'tests/test_app.py'
        path.write_text(path.read_text(encoding='utf-8')+'\n# Fictional approved test refinement\n',encoding='utf-8')
        self.finish_next()
        other = self.store.get(other_id)
        self.assertEqual(other['review_status'],'invalidated')
        self.assertEqual(other['workflow_generation'],1)
        event = next(e for e in self.store.events() if e['type'] == 'feature_impact_reassessed')
        self.assertIn(other_id,event['details']['additional_ids'])

    def test_repair_reviews_and_new_feature_decision_release_affected_recheck(self):
        review = self.ready_feature()
        self.engine.decide_feature_review(review['approval_id'],False,self.confirmation(review,False),'Fictional defect')
        for _ in range(3): self.finish_next()
        wp = self.wp()
        repaired = self.engine.request_feature_review(self.job_id,wp['id'],expected_wp_revision=wp['state_revision'])
        self.approve(repaired)
        next_task = self.store.get(self.engine.workflow_dispatch_view(self.job_id)['candidate_task_id'])
        self.assertEqual(next_task['workflow_authority']['kind'],'package_recheck')
        self.assertEqual(next_task['workflow_generation'],1)

    def test_feature_rejection_after_job_acceptance_keeps_unrelated_feature_history(self):
        reviews = self.independent_features()
        for task in self.tasks():
            if task.get('workflow_job_task') and task['status'] == 'queued': self.store.update(task['id'],status='cancelled')
        self.store.update(self.job_id,status='accepted')
        result = self.rework_approved_first(reviews[0])
        self.assertTrue(result['preserved_ids'])
        self.assertEqual(self.job()['status'],'running')

    def test_stale_revision_and_duplicate_rework_cannot_issue_duplicate_fixes(self):
        reviews = self.independent_features()
        self.rework_approved_first(reviews[0]); before = self.state()
        with self.assertRaises(ValueError): self.rework_approved_first(reviews[0])
        self.assertEqual(self.state(),before)

    def test_task_validation_evidence_stays_attached_to_its_own_builder(self):
        self.independent_features()
        builders = [t for t in self.tasks() if t['role'] == 'builder' and t.get('plan_revision') == self.job()['plan_revision']
                    and t.get('work_package_id') and t.get('status') == 'succeeded']
        self.assertEqual(len(builders),2)
        for task in builders:
            self.assertEqual(task['workflow_validation_evidence']['0']['task_id'],task['id'])

    def test_empty_rejection_reason_rolls_back_decision_and_repair(self):
        review = self.ready_feature(); before = self.state()
        with self.assertRaises(ValueError):
            self.engine.decide_feature_review(review['approval_id'],False,self.confirmation(review,False),'')
        self.assertEqual(self.state(),before)

    def test_concurrent_feature_decisions_commit_once(self):
        review = self.ready_feature(); barrier = threading.Barrier(2); results = []
        observe = self.engine._feature_observation
        def paired(*args):
            value = observe(*args); barrier.wait(timeout=15); return value
        def decide():
            try: self.approve(review); results.append('approved')
            except ValueError: results.append('stale')
        with patch.object(self.engine,'_feature_observation',side_effect=paired):
            threads = [threading.Thread(target=decide) for _ in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(timeout=30)
        self.assertEqual(sorted(results),['approved','stale'])
        events = [e for e in self.store.events() if e['type'] == 'feature_review_decided']
        self.assertEqual(len(events),1)

    def test_old_actor_or_expired_decision_does_not_unlock_dependencies(self):
        review = self.ready_feature()
        self.store.update(review['approval_id'],expires_at=0)
        before = self.state()
        with self.assertRaises(ValueError): self.approve(review)
        self.assertEqual(self.state(),before)
        self.assertNotEqual(self.engine.workflow_dispatch_view(self.job_id)['status'],'ready')

    def test_upstream_shared_file_impact_holds_with_plan_revision_reason(self):
        first_review = self.ready_feature(); self.approve(first_review)
        for _ in range(3): self.finish_next()
        other = next(w for w in self.store.for_job('work_package',self.job_id) if w['id'] != self.wp()['id'])
        review = self.engine.request_feature_review(self.job_id,other['id'],expected_wp_revision=other['state_revision'])
        self.approve(review); other = self.store.get(other['id'])
        result = self.engine.request_package_rework(self.job_id,other['id'],
            {'action':'request_feature_rework','target_digest':review['target_digest']},'Fictional shared-file defect',
            expected_wp_revision=other['state_revision'])
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(result['reason'],'workflow_upstream_impact_requires_plan_revision')
        self.assertEqual(self.job()['status'],'blocked')
        self.assertTrue(all(self.store.get(i)['status'] == 'blocked' for i in result['task_ids']))
        self.assertEqual(self.store.get(first_review['approval_id'])['status'],'approved')
        proposal = self.fixture.propose(copy.deepcopy(self.fixture.payload))
        self.assertEqual(proposal['status'],'pending')

    def test_recorded_retry_note_preserves_issued_spec_authority(self):
        from team_instructions import compact_instruction
        self.finish_next()
        review = next(t for t in self.tasks() if t.get('workflow_authority'))
        note = 'Fictional explicit reviewer clarification'
        self.store.update(review['id'],instruction=compact_instruction(review['instruction'],note),
            instruction_history=[{'instruction':review['instruction'],'reason':'再試行前の指示整理','submitted_note':note}])
        self.assertEqual(self.engine.workflow_dispatch_view(self.job_id)['candidate_task_id'],review['id'])

    def test_expiry_during_observation_does_not_commit_a_decision(self):
        review = self.ready_feature(); before = self.state()
        expiry = self.store.get(review['approval_id'])['expires_at']
        with patch('team_engine.now',side_effect=[expiry-1,expiry+1]):
            with self.assertRaises(ValueError): self.approve(review)
        self.assertEqual(self.state(),before)

    def test_expired_feature_target_can_be_represented_without_reusing_consent(self):
        review = self.ready_feature()
        self.store.update(review['approval_id'],expires_at=0)
        wp = self.wp()
        renewed = self.engine.request_feature_review(self.job_id,wp['id'],expected_wp_revision=wp['state_revision'])
        self.assertNotEqual(renewed['approval_id'],review['approval_id'])
        self.assertEqual(self.store.get(review['approval_id'])['status'],'superseded')
        self.assertEqual(self.store.get(renewed['approval_id'])['status'],'pending')

    def test_unanswered_question_or_handoff_conflict_cannot_be_ignored_by_confirmation(self):
        review = self.ready_feature()
        with patch.object(self.engine.handoff,'pending_conflicts',return_value=[{'id':'fictional-conflict'}]):
            before = self.state()
            with self.assertRaises(ValueError): self.approve(review)
            self.assertEqual(self.state(),before)
        task = self.store.get(self.store.get(review['approval_id'])['task_id'])
        self.engine.new_approval(task,'question',{'summary':'Fictional unresolved requirement','questions':[]})
        before = self.state()
        with self.assertRaises(ValueError): self.approve(review)
        self.assertEqual(self.state(),before)


class TransferTests(unittest.TestCase):
    """A plan revision may reuse an unaffected package's confirmed evidence only by explicit per-package choice."""
    def setUp(self):
        self.base = ReworkTests(); self.addCleanup(self.base.doCleanups); self.base.setUp()
        b = self.base; self.engine, self.store, self.job_id, self.project = b.engine, b.store, b.job_id, b.workspace.project

    def job(self): return self.store.get(self.job_id,'job')
    def state(self): return self.base.state()
    def wps(self):
        return {w['wbs_id']:w for w in self.store.for_job('work_package',self.job_id) if w['plan_revision'] == self.job()['plan_revision']}
    def events(self,kind): return [e for e in self.store.events() if e['type'] == kind]

    def approved_shared(self):
        """Two approved, independent packages that both own app.py."""
        b = self.base; payload = copy.deepcopy(b.fixture.payload); payload['work_packages'][1]['depends_on'] = []
        payload['tasks'][0]['instruction'] += ' (both packages are rebuilt in this revision)'
        approval = b.fixture.propose(payload); b.fixture.approve(approval)
        b.first = next(t for t in b.tasks() if t.get('plan_task_index') == 0 and t.get('plan_revision') == self.job()['plan_revision'])
        for _ in range(6): b.finish_next()
        for wp in self.wps().values(): b.approve(self.engine.request_feature_review(self.job_id,wp['id'],expected_wp_revision=wp['state_revision']))

    def revise(self, key=1):
        payload = copy.deepcopy(self.store.get(self.job()['plan_approval_id'])['payload'])
        payload['tasks'][key]['instruction'] += ' with fictional fix'
        return self.base.fixture.propose(payload)

    def approve(self, revised, choices=None):
        return self.engine.decide(revised['id'],True,'Fictional explicit consent',
            workflow_confirmation=revised['workflow_revision_digest'],transfer_decisions=choices)

    def test_unaffected_confirmed_package_is_offered_and_proposal_changes_nothing(self):
        self.base.independent_features(); before = self.job()
        rev = self.revise()['workflow_revision']
        self.assertEqual(rev['affected_work_packages'],['W1.02'])
        self.assertEqual([c['wbs_id'] for c in rev['transfer_candidates']],['W1.01'])
        self.assertEqual((rev['transfer_blocked'],rev['rerun_required']),([],['W1.02']))
        self.assertEqual(self.job()['plan_revision'],before['plan_revision'])
        self.assertFalse(self.events('workflow_evidence_transferred'))

    def test_without_a_choice_unaffected_package_stays_stopped_and_unconfirmed(self):
        self.base.independent_features(); self.approve(self.revise())
        first = self.wps()['W1.01']
        self.assertEqual(first['review_status'],'not_requested')
        stopped = [t for t in self.store.for_job('task',self.job_id) if t.get('work_package_id') == first['id']]
        self.assertTrue(stopped and all(t['status'] == 'blocked' and t['failure_code'] == 'workflow_transfer_required' for t in stopped))
        self.assertNotEqual(self.engine.workflow_dispatch_view(self.job_id)['status'],'ready')
        self.assertFalse(self.events('workflow_evidence_transferred'))

    def test_reuse_keeps_history_rechecks_the_changed_package_and_reaches_final_review(self):
        self.base.independent_features(); old = self.wps()['W1.01']
        old_receipt = copy.deepcopy(self.store.get(old['feature_review_approval_id']))
        self.approve(self.revise(),{'W1.01':'reuse'})
        first = self.wps()['W1.01']
        self.assertEqual((first['execution_state'],first['review_status']),('succeeded','approved'))
        self.assertNotEqual(first['feature_review_approval_id'],old['feature_review_approval_id'])
        self.assertEqual(self.store.get(old['feature_review_approval_id']),old_receipt)
        record = self.events('workflow_evidence_transferred')[0]['details']
        self.assertEqual((record['source_work_package_id'],record['new_work_package_id'],record['source_review_approval_id']),
                         (old['id'],first['id'],old['feature_review_approval_id']))
        self.assertEqual((record['source_plan_revision'],record['plan_revision']),(old['plan_revision'],self.job()['plan_revision']))
        packages = {p['wbs_id']:p for p in self.engine.workflow_progress(self.job_id)['packages']}
        self.assertTrue(packages['W1.01']['feature_approved']); self.assertFalse(packages['W1.02']['feature_approved'])
        view = self.engine.workflow_dispatch_view(self.job_id)
        self.assertEqual((view['status'],self.store.get(view['candidate_task_id'])['work_package_id']),('ready',self.wps()['W1.02']['id']))
        for _ in range(3): self.base.finish_next()
        second = self.wps()['W1.02']
        self.base.approve(self.engine.request_feature_review(self.job_id,second['id'],expected_wp_revision=second['state_revision']))
        progress = self.engine.workflow_progress(self.job_id)
        self.assertEqual(progress['counts']['feature_approved'],2)
        for _ in range(8):
            if self.job()['status'] == 'awaiting_acceptance': break
            self.base.finish_next()
        self.assertEqual(self.job()['status'],'awaiting_acceptance')
        self.assertFalse(self.engine.workflow_progress(self.job_id)['formal_receipt'])

    def test_reused_package_can_still_be_reworked_without_touching_independent_evidence(self):
        self.base.independent_features(); self.approve(self.revise(),{'W1.01':'reuse'})
        for _ in range(3): self.base.finish_next()
        second = self.wps()['W1.02']
        self.base.approve(self.engine.request_feature_review(self.job_id,second['id'],expected_wp_revision=second['state_revision']))
        first = self.wps()['W1.01']; kept = copy.deepcopy(self.store.get(second['id']))
        result = self.engine.request_package_rework(self.job_id,first['id'],
            {'action':'request_feature_rework','target_digest':first['feature_target_digest']},'Fictional bounded defect',
            expected_wp_revision=first['state_revision'])
        self.assertEqual(result['affected_ids'],[first['id']]); self.assertIn(second['id'],result['preserved_ids'])
        self.assertEqual(self.store.get(second['id']),kept)

    def test_shared_file_with_a_changed_package_is_not_offered_and_can_only_rerun(self):
        self.approved_shared(); revised = self.revise(); rev = revised['workflow_revision']
        self.assertEqual(rev['transfer_candidates'],[])
        self.assertEqual(rev['transfer_blocked'],[{'wbs_id':'W1.01','reasons':['shared_file_with_changed_package']}])
        before = self.state()
        with self.assertRaises(ValueError): self.approve(revised,{'W1.01':'reuse'})
        self.assertEqual(self.state(),before)
        self.approve(revised,{'W1.01':'rerun'})
        first = [t for t in self.store.for_job('task',self.job_id) if t.get('work_package_id') == self.wps()['W1.01']['id']]
        self.assertTrue(first and all(t['status'] == 'queued' for t in first))
        self.assertEqual(self.wps()['W1.01']['review_status'],'not_requested')
        self.assertFalse(self.events('workflow_evidence_transferred'))

    def test_file_changed_before_or_after_the_proposal_prevents_reuse(self):
        self.base.independent_features(); revised = self.revise()
        (self.project/'app.py').write_text('Fictional change after the preview\n',encoding='utf-8'); before = self.state()
        with self.assertRaises(ValueError): self.approve(revised,{'W1.01':'reuse'})
        self.assertEqual(self.state(),before)
        self.engine.decide(revised['id'],False,'Fictional decline')
        again = self.revise()['workflow_revision']
        self.assertEqual(again['transfer_candidates'],[])
        self.assertEqual(again['transfer_blocked'][0]['reasons'],['dependency_artifact_changed'])

    def test_invalid_or_foreign_evidence_cannot_be_reused(self):
        self.base.independent_features(); revised = self.revise()
        # A second fictional job owns a complete, approved package and review record of its own.
        mine = self.wps()['W1.01']; review = self.store.get(mine['feature_review_approval_id'])
        other_id, job = 'fictional-other-job', self.job()
        for name in ('plan_approval_id','requirements_approval_id'):
            self.store.put('approval',dict(self.store.get(job[name]),id='fictional-other-'+name,job_id=other_id))
        self.store.put('job',{k:v for k,v in dict(job,id=other_id,title='Fictional other job',
            plan_approval_id='fictional-other-plan_approval_id',
            requirements_approval_id='fictional-other-requirements_approval_id').items() if k != 'state_revision'})
        foreign_id, foreign_review_id = 'fictional-other-package', 'fictional-other-review'
        # The other job's own tasks, with evidence digests recomputed for their new ids.
        members = [dict(t,id='fictional-other-task-%d' % i,job_id=other_id,work_package_id=foreign_id,
                        spec_source_approval_id='fictional-other-plan_approval_id')
                   for i,t in enumerate(package_members(self.store,self.job_id,mine))]
        members = [{k:v for k,v in t.items() if k != 'state_revision'} for t in members]
        # Its feature target names the other job and package, and every digest is recomputed to match.
        target = dict(review['payload']['feature_target'],job_id=other_id,work_package_id=foreign_id,
                      evidence_refs={t['id']:task_evidence_digest(t) for t in members})
        target_hash = digest(target)
        payload = dict(review['payload'],feature_target=target,feature_target_digest=target_hash)
        foreign_review = dict(review,id=foreign_review_id,job_id=other_id,payload=payload,
            digest=hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()).hexdigest(),
            review_acknowledgement=dict(review['review_acknowledgement'],target_digest=target_hash))
        foreign = {k:v for k,v in dict(mine,id=foreign_id,job_id=other_id,
            spec_source_approval_id='fictional-other-plan_approval_id',feature_target_digest=target_hash,
            feature_review_approval_id=foreign_review_id).items() if k != 'state_revision'}
        self.store.put('approval',foreign_review); foreign = self.store.put('work_package',foreign)
        for t in members: self.store.put('task',t)
        self.store.event('feature_review_decided','Fictional other job decision',job_id=other_id,details={
            'approval_id':foreign_review_id,'work_package_id':foreign_id,'target_digest':target_hash,
            'decision':'approved','actor':'authenticated_local_app_session',
            'requirement_revision':job['requirement_revision'],'plan_revision':job['plan_revision']})
        self.assertEqual((foreign['review_status'],foreign_review['status']),('approved','approved'))
        # Control: the other job's evidence is genuinely valid for the other job (so ownership is what is tested).
        other_job = self.store.get(other_id,'job'); plan = self.store.get(other_job['plan_approval_id'],'approval')
        requirements = self.store.get(other_job['requirements_approval_id'],'approval')
        records = [r for r in self.store.for_job('work_package',other_id) if r.get('plan_revision') == other_job['plan_revision']]
        specs = {s['wbs_id']:s for s in approved_source(other_job,plan,requirements)}
        proof = feature_review_receipt(self.store,other_job,plan,requirements,foreign,specs,records,{})
        self.assertEqual((proof['approval_id'],proof['target_digest']),(foreign_review_id,target_hash))
        # ...yet the same record is refused as evidence for this job.
        with self.assertRaises(ValueError):
            feature_review_receipt(self.store,job,self.store.get(job['plan_approval_id'],'approval'),
                self.store.get(job['requirements_approval_id'],'approval'),foreign,specs,records,{})
        tampered = copy.deepcopy(revised['workflow_revision'])
        tampered['transfer_candidates'][0].update(source_work_package_id=foreign['id'],source_approval_id=foreign_review['id'])
        self.store.update(revised['id'],workflow_revision=tampered); before = self.state()
        with self.assertRaises(ValueError): self.approve(revised,{'W1.01':'reuse'})
        self.assertEqual(self.state(),before)
        self.assertFalse(self.events('workflow_evidence_transferred'))
        self.store.update(revised['id'],workflow_revision=revised['workflow_revision'])
        self.engine.decide(revised['id'],False,'Fictional decline')
        self.store.update(self.wps()['W1.01']['feature_review_approval_id'],status='superseded')
        blocked = self.revise()['workflow_revision']['transfer_blocked']
        self.assertEqual(blocked,[{'wbs_id':'W1.01','reasons':['dependency_feature_review_invalid']}])

    def test_second_decision_and_declined_revision_leave_one_transfer_or_none(self):
        self.base.independent_features(); declined = self.revise(); before = self.state()
        with self.assertRaises(ValueError):
            self.engine.decide(declined['id'],False,'Fictional decline',transfer_decisions={'W1.01':'reuse'})
        self.assertEqual(self.state(),before)
        self.engine.decide(declined['id'],False,'Fictional keep the published plan')
        self.assertFalse(self.events('workflow_evidence_transferred'))
        self.assertEqual(self.wps()['W1.01']['review_status'],'approved')
        revised = self.revise(); self.approve(revised,{'W1.01':'reuse'}); settled = self.state()
        with self.assertRaises(ValueError): self.approve(revised,{'W1.01':'reuse'})
        self.assertEqual(self.state(),settled); self.assertEqual(len(self.events('workflow_evidence_transferred')),1)

    def test_revision_after_acceptance_reuses_only_the_chosen_package_and_is_not_accepted(self):
        self.base.independent_features()
        for _ in range(8):
            if self.job()['status'] == 'awaiting_acceptance': break
            self.base.finish_next()
        self.assertEqual(self.job()['status'],'awaiting_acceptance')
        completion = next(a for a in self.store.for_job('approval',self.job_id) if a['kind'] == 'completion' and a['status'] == 'pending')
        # The user's own operation checks are recorded one by one, exactly as the screen does.
        while completion['payload'].get('pending_items'):
            replaced = self.engine.record_completion_evidence(completion['id'],0,0,'Fictional local operation verified',True)
            completion = self.store.get(replaced['approval_id'])
        self.engine.acknowledge_completion_review(completion['id'],True)
        self.engine.accept_completion(completion['id'],'正式受領')
        self.assertEqual(self.job()['status'],'accepted')
        receipt = self.events('formal_acceptance_decided')
        self.assertEqual(len(receipt),1)
        self.assertTrue(self.engine.workflow_progress(self.job_id)['formal_receipt'])
        recorded = self.store.get(completion['id'])
        self.approve(self.revise(),{'W1.01':'reuse'})
        self.assertEqual(self.job()['status'],'running')
        self.assertEqual(self.store.get(completion['id']),recorded)
        self.assertEqual(self.events('formal_acceptance_decided'),receipt)
        packages = {p['wbs_id']:p for p in self.engine.workflow_progress(self.job_id)['packages']}
        self.assertEqual((packages['W1.01']['feature_approved'],packages['W1.02']['feature_approved']),(True,False))
        self.assertFalse(self.engine.workflow_progress(self.job_id)['formal_receipt'])

    def test_http_choice_is_part_of_the_approval_request_only(self):
        self.base.independent_features(); revised = self.revise(); request = self.base.fixture.api_server()
        body = {'id':revised['id'],'allow':True,'workflow_confirmation':revised['workflow_revision_digest']}
        before = self.state()
        for extra in ({'transfer_decisions':['W1.01']},{'transfer_decisions':{'W1.01':'keep'}},{'transfer_decisions':{'W9.99':'reuse'}}):
            self.assertEqual(request('/api/decide',dict(body,**extra))[0],400)
            self.assertEqual(self.state(),before)
        self.assertEqual(request('/api/decide',dict(body,transfer_decisions={'W1.01':'reuse'}))[0],200)
        self.assertEqual(self.wps()['W1.01']['review_status'],'approved')


if __name__ == '__main__': unittest.main()
