"""Fictional consent/version tests; no native provider or application deploy."""
import copy
import json
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import unittest
from unittest.mock import patch

from team_plan_revision import analysis, approval_digest
from tests.test_work_packages import proposal


class RevisionTests(unittest.TestCase):
    def setUp(self):
        from tests.test_versioned_context import VersionedContextTests
        self.fixture = VersionedContextTests(); self.addCleanup(self.fixture.doCleanups); self.fixture.setUp()
        self.engine, self.store = self.fixture.engine, self.fixture.store
        self.job_id = 'fictional-revision-job'
        self.store.close_attempt(self.fixture.task['id'], 'closed', 'fictional_fixture_finished')
        self.store.update(self.fixture.task['id'], status='cancelled')
        from team_p0_contract import initial
        initial_job = copy.deepcopy(self.fixture.job)
        initial_job.pop('state_revision', None)
        initial_job.update(id=self.job_id, status='awaiting_approval', code_test_contract_version=1,
                           requirement_revision=0, requirements_approval_id=None, plan_revision=0, plan_approval_id=None,
                           goal_contract=initial(self.fixture.job['goal']), p0_artifacts=[])
        self.store.put('job', initial_job)
        self.payload = proposal(two=True)
        self.planner = self.engine.new_task(self.job(), 'Fictional plan', 'Fictional proposal only', 'planner')
        self.planner = self.store.update(self.planner['id'], status='succeeded', result=self.payload)
        self.approval = self.engine._new_workflow_plan_approval(self.planner, self.payload)

    def job(self): return self.store.get(self.job_id, 'job')

    def state(self):
        return (self.store.db.execute('SELECT * FROM objects ORDER BY id').fetchall(),
                self.store.db.execute('SELECT * FROM events ORDER BY seq').fetchall())

    def approve(self, approval=None):
        approval = approval or self.approval
        self.engine.decide(approval['id'], True, 'Fictional explicit consent',
                           workflow_confirmation=approval['workflow_revision_digest'])

    def stop_queued(self):
        for task in self.store.for_job('task', self.job_id):
            if task['status'] in ('queued', 'awaiting_approval'):
                self.store.update(task['id'], status='succeeded')

    def propose(self, payload):
        result = self.engine.propose_plan_revision(self.job_id, payload, self.job()['state_revision'],
                                                   actor='authenticated_local_app_session')
        return self.store.get(result['approval_id'], 'approval')

    def test_initial_g3_versions_membership_and_after_published_atomically(self):
        self.approve()
        job = self.job()
        self.assertEqual((job['requirement_revision'], job['plan_revision']), (1, 1))
        self.assertEqual(job['plan_approval_id'], self.approval['id'])
        tasks = [t for t in self.store.for_job('task', self.job_id) if t.get('work_package_id')]
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]['after'], self.planner['id'])
        self.assertEqual(tasks[1]['after'], tasks[0]['id'])
        self.assertEqual(job['status'], 'running')
        self.assertNotIn(job['status'], ('accepted', 'accepted_with_pending_checks'))

    def test_confirmation_missing_or_wrong_changes_nothing(self):
        for confirmation in (None, 'incorrect'):
            before = self.state()
            with self.assertRaises(ValueError): self.engine.decide(self.approval['id'], True, '', workflow_confirmation=confirmation)
            self.assertEqual(self.state(), before)

    def test_stale_job_and_tampered_payload_or_analysis_are_rejected(self):
        self.store.update(self.job_id, title='Fictional title changed')
        before = self.state()
        with self.assertRaises(ValueError): self.approve()
        self.assertEqual(self.state(), before)
        for field in ('payload', 'workflow_revision'):
            original = copy.deepcopy(self.approval[field]); changed = copy.deepcopy(original)
            changed['summary' if field == 'payload' else 'job_id'] = 'Fictional changed'
            self.store.update(self.approval['id'], **{field: changed})
            with self.assertRaises(ValueError): self.approve()
            self.store.update(self.approval['id'], **{field: original})

    def test_rejection_does_not_publish_or_create_builders(self):
        self.engine.decide(self.approval['id'], False, 'Fictional needs revision')
        self.assertEqual(self.job()['plan_revision'], 0)
        self.assertFalse(any(t.get('work_package_id') for t in self.store.for_job('task', self.job_id)))
        self.assertEqual(self.job()['status'], 'blocked')

    def test_initial_publish_failure_rolls_back_consent_versions_tasks_and_events(self):
        before = self.state()
        original = self.engine.new_task
        def fail(*args, **kwargs):
            if kwargs.get('plan_task_index') == 1: raise ValueError('Fictional second Task failure')
            return original(*args, **kwargs)
        with patch.object(self.engine, 'new_task', side_effect=fail):
            with self.assertRaises(ValueError): self.approve()
        self.assertEqual(self.state(), before)

    def test_requirement_changes_remain_proposals_until_exact_consent(self):
        self.approve(); self.stop_queued()
        old = self.job(); value = copy.deepcopy(self.payload)
        value['goal_contract']['constraints'].append('Fictional local scope constraint')
        revised = self.propose(value)
        self.assertEqual(self.job()['goal_contract'], old['goal_contract'])
        self.assertEqual(self.job()['requirement_revision'], 1)
        self.assertTrue(revised['workflow_revision']['requirements_changed'])
        self.assertIn('constraints', revised['workflow_revision']['scope_changes'])
        self.approve(revised)
        current = self.job()
        self.assertEqual((current['requirement_revision'], current['plan_revision']), (2, 2))
        self.assertEqual(current['requirements_approval_id'], revised['id'])
        self.assertEqual(current['goal'], old['goal'])
        self.assertEqual(self.store.get(self.approval['id'])['payload'], self.payload)

    def test_plan_only_change_keeps_requirement_version_and_invalidates_old_proofs(self):
        self.approve(); self.stop_queued()
        old_wp = self.store.for_job('work_package', self.job_id)[0]
        self.store.update(old_wp['id'], review_status='approved', execution_state='succeeded')
        self.store.update(self.job_id, validation_evidence={'fictional': 'old evidence'})
        completion = self.engine.new_approval(self.planner, 'completion', {'fictional': 'old target'})
        value = copy.deepcopy(self.payload); value['tasks'][0]['instruction'] += ' with fictional fix'
        revised = self.propose(value)
        self.assertEqual(revised['workflow_revision']['affected_work_packages'], ['W1.01', 'W1.02'])
        self.approve(revised)
        self.assertEqual((self.job()['requirement_revision'], self.job()['plan_revision']), (1, 2))
        self.assertEqual(self.job()['requirements_approval_id'], self.approval['id'])
        self.assertEqual(self.store.get(old_wp['id'])['review_status'], 'invalidated')
        self.assertEqual(self.store.get(completion['id'])['status'], 'superseded')
        self.assertIsNone(self.job()['validation_evidence'])

    def test_unaffected_branch_is_not_automatically_reexecuted_or_transferred(self):
        self.approve(); self.stop_queued()
        value = copy.deepcopy(self.payload); value['tasks'][1]['instruction'] += ' with fictional fix'
        revised = self.propose(value)
        self.assertEqual(revised['workflow_revision']['affected_work_packages'], ['W1.02'])
        self.approve(revised)
        current = [t for t in self.store.for_job('task', self.job_id) if t.get('plan_revision') == 2]
        first = next(t for t in current if t.get('plan_task_index') == 0)
        self.assertEqual(first['status'], 'blocked')
        self.assertEqual(first['failure_code'], 'workflow_transfer_required')

    def test_worker_cannot_publish_or_modify_approved_requirements_even_while_planning(self):
        self.approve()
        self.store.update(self.job_id, status='planning')
        contract = copy.deepcopy(self.job()['goal_contract']); contract['primary_goal'] = 'Fictional silent replacement'
        with self.assertRaises(ValueError): self.store.update(self.job_id, goal_contract=contract)
        with self.assertRaises(ValueError): self.store.update(self.job_id, goal='Fictional replacement')

    def test_pending_operation_and_active_writer_prevent_revision(self):
        self.approve(); self.stop_queued()
        queued = self.engine.new_task(self.job(), 'Fictional active', 'Fictional work', 'builder')
        self.store.update(queued['id'], status='running')
        before = self.state()
        with self.assertRaises(ValueError): self.propose(self.payload)
        self.assertEqual(self.state(), before)
        self.store.update(queued['id'], status='succeeded')
        pending = self.engine.new_approval(queued, 'question', {'questions': []})
        with self.assertRaises(ValueError): self.propose(self.payload)
        self.assertEqual(self.store.get(pending['id'])['status'], 'pending')

    def test_revision_is_bound_to_expected_version_and_authenticated_route(self):
        self.approve(); self.stop_queued()
        before = self.state()
        for actor, revision in (('worker', self.job()['state_revision']), ('authenticated_local_app_session', -1)):
            with self.assertRaises(ValueError):
                self.engine.propose_plan_revision(self.job_id, self.payload, revision, actor=actor)
        self.assertEqual(self.state(), before)

    def api_server(self):
        from server import Handler
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        origin = 'http://127.0.0.1:' + str(server.server_port)
        server.app = SimpleNamespace(engine=self.engine, store=self.store, port=server.server_port,
                                     origin=origin, cookie='fictional-test-session')
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def close():
            server.shutdown(); thread.join(timeout=3); server.server_close()
        self.addCleanup(close)
        def request(path, body, cookie=True, ui=True, request_origin=None):
            headers = {'Content-Type': 'application/json', 'Origin': request_origin or origin}
            if cookie: headers['Cookie'] = 'agent_team=fictional-test-session'
            if ui: headers['X-Agent-Team-UI'] = '1'
            req = Request(origin + path, data=json.dumps(body).encode(), headers=headers, method='POST')
            try:
                with urlopen(req, timeout=3) as response: return response.status, json.loads(response.read())
            except HTTPError as error:
                with error: return error.code, json.loads(error.read())
        return request

    def test_http_auth_origin_ui_header_and_exact_body_guard(self):
        self.approve(); self.stop_queued()
        request = self.api_server()
        body = {'id': self.job_id, 'plan': self.payload, 'job_revision': self.job()['state_revision']}
        before = self.state()
        self.assertEqual(request('/api/jobs/plan-revision', body, cookie=False)[0], 403)
        self.assertEqual(request('/api/jobs/plan-revision', body, ui=False)[0], 403)
        self.assertEqual(request('/api/jobs/plan-revision', body, request_origin='http://fictional.invalid')[0], 403)
        self.assertEqual(request('/api/jobs/plan-revision', dict(body, actor='worker'))[0], 400)
        self.assertEqual(self.state(), before)

    def test_http_revision_preview_and_exact_confirmation_publish(self):
        self.approve(); self.stop_queued()
        request = self.api_server()
        value = copy.deepcopy(self.payload); value['tasks'][1]['instruction'] += ' revised fictional step'
        code, response = request('/api/jobs/plan-revision', {'id': self.job_id, 'plan': value, 'job_revision': self.job()['state_revision']})
        self.assertEqual(code, 200)
        self.assertEqual(self.job()['plan_revision'], 1)
        self.assertEqual(response['workflow_revision']['task_changes'][0]['after'][0]['instruction'], value['tasks'][1]['instruction'])
        body = {'id': response['approval_id'], 'allow': True, 'note': 'Fictional user confirmation'}
        before = self.state()
        self.assertEqual(request('/api/decide', body)[0], 400)
        self.assertEqual(self.state(), before)
        body['workflow_confirmation'] = response['workflow_revision_digest']
        self.assertEqual(request('/api/decide', body)[0], 200)
        self.assertEqual(self.job()['plan_revision'], 2)
        self.assertEqual(request('/api/decide', body)[0], 400)

    def test_input_change_after_preview_prevents_consent_without_mutation(self):
        import hashlib
        self.approve(); self.stop_queued()
        record = {'path': 'app.py', 'sha256': hashlib.sha256(self.fixture.app.read_bytes()).hexdigest()}
        self.store.update(self.job_id, p0_artifacts=[record])
        value = copy.deepcopy(self.payload)
        value['work_packages'][0]['input_artifacts'] = [dict(record, job_id=self.job_id)]
        revised = self.propose(value)
        self.fixture.app.write_text('fictional = "changed"\n', encoding='utf-8')
        before = self.state()
        with self.assertRaises(ValueError): self.approve(revised)
        self.assertEqual(self.state(), before)

    def test_real_finish_path_attaches_initial_g3_revision_confirmation(self):
        self.store.update(self.approval['id'], status='superseded')
        self.store.update(self.planner['id'], status='queued', result=None)
        self.store.update(self.job_id, status='planning')
        task = self.store.reserve_attempt(self.planner['id'], self.store.get(self.planner['id'])['state_revision'])
        value = dict(self.payload, status='done')
        with patch('team_plan_check.issues', return_value=[]), patch('team_document_capabilities.require_plan'):
            self.engine._finish(task, value)
        self.store.close_attempt(task['id'], 'closed', 'fictional_finish')
        pending = [a for a in self.store.for_job('approval', self.job_id) if a['status'] == 'pending']
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['workflow_revision']['base_versions']['plan_revision'], 0)
        self.approve(pending[0])
        self.assertEqual(self.job()['plan_revision'], 1)

    def test_old_and_new_graph_reverse_closure_includes_removed_dependency(self):
        self.approve(); self.stop_queued()
        value = copy.deepcopy(self.payload)
        value['work_packages'][1]['depends_on'] = []
        value['tasks'][0]['instruction'] += ' fictional changed predecessor'
        revised = self.propose(value)
        self.assertEqual(revised['workflow_revision']['affected_work_packages'], ['W1.01', 'W1.02'])

    def test_worker_attempt_cannot_publish_revision(self):
        self.approve(); self.stop_queued()
        revised = self.propose(self.payload)
        self.store.update(self.job_id, status='running')
        task = self.engine.new_task(self.job(), 'Fictional worker', 'Fictional attempt', 'builder')
        task = self.store.reserve_attempt(task['id'], task['state_revision'])
        before = self.state()
        with self.store.attempt_scope(task):
            with self.assertRaises(ValueError):
                self.store.publish_workflow_plan(self.job_id, revised['id'], self.job()['state_revision'])
        self.assertEqual(self.state(), before)

    def test_recovery_check_prevents_proposal_without_bypassing_maintenance(self):
        self.approve(); self.stop_queued()
        current = self.store.for_job('task', self.job_id)[0]
        self.store.update(current['id'], recovery_check_required=True)
        with self.assertRaises(ValueError): self.propose(self.payload)

    def test_malformed_test_plan_prevents_proposal(self):
        self.approve(); self.stop_queued()
        value = copy.deepcopy(self.payload); value['code_test_plan']['command'] = 'python -B -m unittest\nsecond command'
        before = self.state()
        with self.assertRaises(ValueError): self.propose(value)
        self.assertEqual(self.state(), before)

    def test_declined_revision_restores_all_prior_states_without_scope_or_evidence_loss(self):
        self.approve(); self.stop_queued()
        for status in ('running', 'blocked', 'failed', 'awaiting_acceptance', 'accepted', 'accepted_with_pending_checks'):
            self.store.update(self.job_id, status=status, validation_evidence={'fictional': 'retained'})
            before = self.job()
            tasks_before = self.store.for_job('task', self.job_id)
            revised = self.propose(self.payload)
            self.assertEqual(revised['workflow_revision']['proposal_context']['prior_status'], status)
            self.assertEqual(self.job()['status'], status if status.startswith('accepted') else 'awaiting_approval')
            self.engine.decide(revised['id'], False, 'Fictional keep published plan')
            self.assertEqual(self.job()['status'], status)
            for key in ('goal', 'goal_contract', 'requirement_revision', 'requirements_approval_id',
                        'plan_revision', 'plan_approval_id', 'validation_evidence'):
                self.assertEqual(self.job()[key], before[key])
            self.assertEqual(self.store.get(revised['id'])['status'], 'denied')
            for task in tasks_before: self.assertEqual(self.store.get(task['id']), task)

    def test_new_stop_while_proposal_pending_is_not_undone_on_rejection(self):
        self.approve(); self.stop_queued()
        for old_status, new_status in (('running', 'blocked'), ('accepted', 'failed')):
            self.store.update(self.job_id, status=old_status)
            revised = self.propose(self.payload)
            self.store.update(self.job_id, status=new_status)
            self.engine.decide(revised['id'], False, 'Fictional decline but retain new failure')
            self.assertEqual(self.job()['status'], new_status)
            event = next(e for e in self.store.events() if e.get('type') == 'workflow_revision_rejected')
            self.assertEqual(event.get('details', {}).get('reason'), 'proposal_state_changed')

    def test_accepted_revision_only_leaves_accepted_after_explicit_consent(self):
        self.approve(); self.stop_queued()
        for status in ('accepted', 'accepted_with_pending_checks'):
            self.store.update(self.job_id, status=status)
            previous = self.job()['plan_revision']
            revised = self.propose(self.payload)
            self.assertEqual(self.job()['status'], status)
            self.approve(revised)
            self.assertEqual(self.job()['status'], 'running')
            self.assertEqual(self.job()['plan_revision'], previous + 1)
            self.stop_queued()

    def test_changed_proposal_state_requires_reconfirmation_instead_of_restoring_running(self):
        self.approve(); self.stop_queued()
        revised = self.propose(self.payload)
        self.store.update(self.job_id, validation_evidence={'fictional': 'changed while pending'})
        self.engine.decide(revised['id'], False, 'Fictional decline')
        self.assertEqual(self.job()['status'], 'blocked')

    def test_rejection_preserves_pending_completion_and_does_not_accept_it(self):
        self.approve(); self.stop_queued()
        self.store.update(self.job_id, status='awaiting_acceptance')
        completion = self.engine.new_approval(self.planner, 'completion', {'fictional': 'unaccepted evidence'})
        completion = self.store.get(completion['id'])
        revised = self.propose(self.payload)
        self.engine.decide(revised['id'], False, 'Fictional no revision')
        self.assertEqual(self.job()['status'], 'awaiting_acceptance')
        self.assertEqual(self.store.get(completion['id']), completion)


if __name__ == '__main__': unittest.main()
