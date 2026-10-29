"""Synthetic P0 path from approved plan through an actual local test and acceptance."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from team_config import default_config
from team_engine import Context, Engine
from team_p0_contract import initial
from team_store import Store


COMMAND = 'python -B -X utf8 -m unittest test_lookup'


class P0IntegrationTests(unittest.TestCase):
    def test_plan_to_artifact_to_user_acceptance(self):
        temp = tempfile.TemporaryDirectory(prefix='saikuru-p0-synthetic-')
        self.addCleanup(temp.cleanup)
        folder = temp.name
        root = Path(folder)
        store = Store(root / 'runtime')
        config = default_config()
        config['approved_roots'] = [str(root)]
        engine = Engine(store, config, {}, 'http://127.0.0.1:0')
        engine.paused = True
        self.addCleanup(store.db.close)
        self.addCleanup(engine.handoff.close)
        goal = 'Create a fictional lookup and show no result for an unknown ID'
        job = store.put('job', {'id': 'fictional-p0-job', 'title': 'Fictional lookup',
            'goal': goal, 'goal_contract': initial(goal), 'p0_contract_version': 1,
            'project': str(root), 'status': 'awaiting_approval', 'auto_execute': False,
            'validation_commands': [], 'code_test_contract_version': 1})
        planner = engine.new_task(job, 'Plan', 'Plan fictional lookup', 'planner')
        plan = {'summary': 'Create a fictional lookup', 'security_review_required': False,
            'goal_contract': {'primary_goal': 'Search fictional items',
                              'deliverables': ['lookup.py'],
                              'acceptance_criteria': ['Unknown ID shows no result'],
                              'non_goals': [], 'constraints': ['Fictional data only']},
            'tasks': [{'title': 'Build fictional lookup', 'instruction': 'Build and test lookup',
                       'role': 'builder', 'goal_links': ['lookup.py', 'Unknown ID shows no result']}],
            'code_test_plan': {'required': True, 'reason': 'New code',
                               'cases': ['Unknown ID shows no result'],
                               'command': COMMAND, 'existing_tests': []}}
        store.update(planner['id'], status='succeeded', result=plan)
        g3 = engine.new_approval(planner, 'plan', plan)
        self.assertEqual(store.get(job['id'])['status'], 'awaiting_approval')
        engine.decide(g3['id'], True, 'Fictional plan approved')
        self.assertEqual(store.get(g3['id'], 'approval')['status'], 'approved')
        tasks = store.for_job('task', job['id'])
        builder = next(task for task in tasks if task['role'] == 'builder')
        reviews = [task for task in tasks if task['role'] == 'reviewer']
        self.assertEqual(store.get(job['id'])['goal_contract']['original_request'], goal)
        self.assertEqual(store.get(builder['id'])['goal_links'],
                         ['lookup.py', 'Unknown ID shows no result'])
        self.assertTrue(all(store.get(task['id'])['goal_links'] ==
                            ['lookup.py', 'Unknown ID shows no result'] for task in reviews))

        (root / 'lookup.py').write_text(
            'def lookup(item_id):\n    return "No result" if item_id != "A001" else "Fictional item"\n',
            encoding='utf-8')
        (root / 'test_lookup.py').write_text(
            'import unittest\nfrom lookup import lookup\n'
            'class Check(unittest.TestCase):\n'
            '    def test_unknown(self): self.assertEqual(lookup("X999"), "No result")\n',
            encoding='utf-8')
        run = subprocess.run(['python', '-B', '-X', 'utf8', '-m', 'unittest', 'test_lookup'],
                             cwd=root, capture_output=True, text=True, timeout=15)
        self.assertEqual(run.returncode, 0, run.stderr)
        context = Context.__new__(Context)
        context.engine, context.task = engine, builder
        context.command_result(COMMAND, run.returncode, run.stdout + run.stderr)
        done = {'status': 'done', 'summary': 'Fictional lookup exists', 'checks': [],
                'pending_checks': [], 'question': '', 'questions': [], 'context_updates': [],
                'test_files': ['test_lookup.py'], 'test_change_reason': '',
                'artifacts': ['lookup.py']}
        engine._finish(builder, done)
        self.assertEqual(store.get(builder['id'])['result']['status'], 'done')
        review = {'status': 'done', 'summary': 'Synthetic review passed', 'checks': [],
                  'pending_checks': [], 'question': '', 'questions': [],
                  'context_updates': []}
        for reviewer in reviews:
            engine._finish(reviewer, dict(review))
        job = store.get(job['id'])
        self.assertEqual(job['status'], 'awaiting_acceptance')
        approval = next(item for item in store.for_job('approval', job['id'])
                        if item['kind'] == 'completion')
        self.assertEqual(len(approval['payload']['pending_items']), 2)
        with self.assertRaises(ValueError):
            engine.accept_completion(approval['id'], '正式受領')
        current_id = approval['id']
        for _ in range(2):
            response = engine.record_completion_evidence(
                current_id, 0, 0, '合成成果物の操作を確認', True)
            current_id = response['approval_id']
        engine.acknowledge_completion_review(current_id, True)
        self.assertEqual(engine.accept_completion(current_id, '正式受領')['status'], 'accepted')
        self.assertEqual(store.get(job['id'])['goal_contract']['completion_status'], 'accepted')

class WorkflowApiIntegrationTests(unittest.TestCase):
    """Synthetic worker results and synthetic user actions, never real receipt."""

    def test_serial_features_to_global_review_and_explicit_synthetic_receipt(self):
        from tests import test_workflow_api as fixtures
        f = fixtures.WorkflowApiTests(); self.addCleanup(f.doCleanups); f.setUp()
        engine, store, job_id = f.engine, f.store, f.f.job_id
        from unittest.mock import patch
        original_result = Context.command_result
        def correlated_result(context, command, exit_code, output, timed_out=False):
            # The synthetic provider explicitly echoes its operation ID on both
            # messages. Production does not infer IDs from command text.
            operation_id = 'synthetic-' + str(len(store.job_events(job_id, 'operation_result')))
            context.command_started(command, operation_id)
            return original_result(context, command, exit_code, output, timed_out, operation_id=operation_id)
        operation_bridge = patch.object(Context, 'command_result', new=correlated_result)
        operation_bridge.start(); self.addCleanup(operation_bridge.stop)
        # The fixture publishes its fictional plan through the real G3 engine.
        self.assertEqual(f.f.job()['plan_revision'], 1)
        self.assertEqual(f.f.job()['status'], 'running')
        decided = []
        for index in range(2):
            builder = next(t for t in f.f.tasks() if t.get('plan_task_index') == index)
            if index == 1:
                reserved = engine._reserve_workflow_attempt(builder, engine.workflow_dispatch_view(job_id))
                engine.store.recover()  # Fictional crash, actual persistence/fencing path.
                target = engine.workflow_control_target(job_id, 'confirm_recovery', builder['id'])
                confirm = {'action': 'confirm_recovery', 'job_id': job_id, 'task_id': builder['id'],
                           'target': target, 'writer_stopped': True, 'checked_changes': True}
                self.assertEqual(f.request('/api/workflow/control', confirm)[0], 403)
                code, result = f.request('/api/workflow/control', confirm, maintenance=True)
                if code != 200:
                    # Preserve the real refusal reason in the sanitized local
                    # test report, rather than weakening the operation fence.
                    engine._recovery_control_inputs(builder['id'])
                self.assertEqual(code, 200, result); self.assertFalse(result['queued'])
                draft = engine.collect_checkpoint(builder['id'], writer_stopped=True)
                self.assertTrue(engine.finalize_checkpoint(draft['id'], writer_stopped=True)['handoff_synced'])
                self.assertTrue(engine._checkpoint_work_package_dispatch_valid(builder['id']))
                assessment = engine.assess_checkpoint_resume(builder['id'], writer_stopped=True)
                self.assertEqual(assessment['status'], 'candidate', assessment)
                retry = {'action': 'retry_task', 'job_id': job_id, 'task_id': builder['id'],
                         'target': engine.workflow_control_target(job_id, 'retry_task', builder['id']),
                         'checked_changes': True, 'note': 'SYNTHETIC crash inspected', 'handoff_allowed': False}
                code, result = f.request('/api/workflow/control', retry, maintenance=True)
                self.assertEqual(code, 200, result); self.assertFalse(result['started'])
                self.assertNotEqual(store.get(builder['id'])['current_attempt']['status'], 'running')
            for _ in range(3):
                f.f.finish_next(summary='SYNTHETIC Provider result; no native model')
            wp = store.get(builder['work_package_id'], 'work_package')
            body = {'job_id': job_id, 'work_package_id': wp['id'],
                    'work_package_revision': wp['state_revision'], 'action': 'request_feature_review'}
            code, review = f.request('/api/workflow/feature-review/prepare', body)
            self.assertEqual(code, 200, review)
            self.assertNotEqual(engine.workflow_dispatch_view(job_id)['status'], 'ready')
            code, result = f.request('/api/workflow/feature-review/decide', f.decision_body(review))
            self.assertEqual(code, 200, result); self.assertFalse(result['formal_receipt'])
            decided.append(review['approval_id'])
            self.assertEqual(f.f.job()['status'], 'running')
        global_reviews = [t for t in f.f.tasks() if t.get('workflow_job_task')]
        self.assertTrue(global_reviews)
        for _ in global_reviews:
            f.f.finish_next(summary='SYNTHETIC independent integration review')
        self.assertEqual(f.f.job()['status'], 'awaiting_acceptance')
        completion = next(a for a in store.for_job('approval', job_id)
                          if a['kind'] == 'completion' and a['status'] == 'pending')
        body = {'approval_id': completion['id'], 'confirmation': '正式受領', 'note': 'SYNTHETIC ONLY'}
        self.assertEqual(f.request('/api/jobs/formal-acceptance', body)[0], 400)
        # Clear only the actual generated checks, via the real HTTP evidence route.
        for _ in range(12):
            pending = completion['payload'].get('pending_items') or []
            if not pending: break
            code, result = f.request('/api/completion/evidence', {'approval_id': completion['id'],
                'item_index': 0, 'check_index': 0, 'evidence': 'SYNTHETIC local inspection only', 'confirmed': True})
            self.assertEqual(code, 200, result)
            completion = store.get(result['approval_id'], 'approval')
        self.assertFalse(completion['payload'].get('pending_items'))
        self.assertEqual(f.request('/api/jobs/review-confirmation',
                                  {'approval_id': completion['id'], 'confirmed': True})[0], 200)
        body['approval_id'] = completion['id']
        code, result = f.request('/api/jobs/formal-acceptance', body)
        self.assertEqual(code, 200, result); self.assertEqual(result['status'], 'accepted')
        self.assertEqual(f.request('/api/jobs/formal-acceptance', body)[0], 400)
        self.assertEqual(len(store.job_events(job_id, 'formal_acceptance_decided')), 1)
        self.assertEqual(len(decided), 2)


class ReviewedOperationRecoveryTests(unittest.TestCase):
    def setUp(self):
        from tests import test_workflow_api as fixtures
        from unittest.mock import patch
        self.f = fixtures.WorkflowApiTests(); self.addCleanup(self.f.doCleanups); self.f.setUp()
        original = Context.command_result
        def correlated(context, command, code, output, timed_out=False):
            operation_id = 'synthetic-prior-' + str(len(self.f.store.job_events(self.f.f.job_id, 'operation_result')))
            context.command_started(command, operation_id)
            return original(context, command, code, output, timed_out, operation_id=operation_id)
        bridge = patch.object(Context, 'command_result', new=correlated)
        bridge.start(); self.addCleanup(bridge.stop)
        review = self.f.f.ready_feature(); self.f.f.approve(review)
        view = self.f.engine.workflow_dispatch_view(self.f.f.job_id)
        self.target = self.f.engine._reserve_workflow_attempt(self.f.store.get(view['candidate_task_id']), view)

    def stop(self):
        self.f.store.recover()
        return self.f.store.get(self.target['id']), self.f.f.job()

    def test_prior_review_is_valid_only_while_current_feature_receipt_is_approved(self):
        task, job = self.stop()
        basis = self.f.engine._reviewed_operation_basis(task, job)
        self.assertTrue(basis['reviewed_prior_operations'])
        self.f.store.update(self.f.f.wp()['id'], review_status='invalidated')
        with self.assertRaises(ValueError): self.f.engine._reviewed_operation_basis(task, job)

    def test_prior_artifact_change_invalidates_basis_without_mutating_state(self):
        task, job = self.stop()
        path = self.f.f.workspace.project / 'app.py'
        path.write_bytes(path.read_bytes() + b'\n# SYNTHETIC changed after review\n')
        before = self.f.f.state()
        with self.assertRaises(ValueError): self.f.engine._reviewed_operation_basis(task, job)
        self.assertEqual(self.f.f.state(), before)

    def test_target_attempt_operation_is_not_covered_by_another_feature_review(self):
        context = Context.__new__(Context); context.engine = self.f.engine; context.task = self.target
        context.command_result('python -B -m unittest', 0, 'SYNTHETIC target action')
        task, job = self.stop()
        with self.assertRaises(ValueError): self.f.engine._reviewed_operation_basis(task, job)

    def test_store_recovery_without_engine_revalidation_still_refuses_prior_operations(self):
        task, job = self.stop()
        before = self.f.f.state()
        with self.assertRaises(ValueError):
            self.f.store.confirm_recovery(task['id'], task['state_revision'], job['state_revision'],
                                          task['current_attempt']['id'])
        self.assertEqual(self.f.f.state(), before)

    def test_proposed_retry_checks_the_real_wp_approval_without_queuing(self):
        task, job = self.stop()
        self.f.engine.confirm_recovery(task['id'], task['state_revision'], job['state_revision'],
                                       task['current_attempt']['id'], writer_stopped=True, checked_changes=True)
        self.assertTrue(self.f.engine._checkpoint_work_package_dispatch_valid(task['id']))
        self.assertEqual(self.f.store.get(task['id'])['status'], 'interrupted')
        self.f.store.update(task['id'], spec_source_approval_id='fictional-foreign-plan')
        before = self.f.f.state()
        try:
            valid = self.f.engine._checkpoint_work_package_dispatch_valid(task['id'])
        except (ValueError, KeyError, TypeError):
            valid = False
        self.assertFalse(valid)
        self.assertEqual(self.f.f.state(), before)

    def test_interrupted_wp_cannot_reuse_the_completed_successor_session_binding(self):
        task, job = self.stop()
        self.f.engine.confirm_recovery(task['id'], task['state_revision'], job['state_revision'],
                                       task['current_attempt']['id'], writer_stopped=True, checked_changes=True)
        draft = self.f.engine.collect_checkpoint(task['id'], writer_stopped=True)
        self.assertTrue(self.f.engine.finalize_checkpoint(draft['id'], writer_stopped=True)['handoff_synced'])
        before = self.f.f.state()
        with self.assertRaises(ValueError):
            self.f.engine.prepare_checkpoint_session(task['id'], task['id'], writer_stopped=True)
        self.assertEqual(self.f.f.state(), before)
        self.assertFalse(self.f.store.get(task['id']).get('checkpoint_session_binding'))


class NativeReconciliationTests(unittest.TestCase):
    def setUp(self):
        from tests.test_recovery_integration_synthetic import NativeAssessmentTests
        NativeAssessmentTests.setUp(self)

    def assess(self):
        return self.f.request('/api/workflow/recovery/assess', self.body, maintenance=True)[1]

    def request_body(self):
        assessment = self.assess()
        self.assertTrue(assessment['eligible_for_reconciliation'], assessment)
        return {'action': 'reconcile_recovery', 'job_id': self.job_id, 'task_id': self.task_id,
            'target': assessment['target'], 'writer_stopped': True, 'checked_changes': True,
            'external_effects': 'none_confirmed', 'note': 'SYNTHETIC maintenance judgment; files inspected, no external effect.'}

    def reconcile(self, body, **options):
        return self.f.request('/api/workflow/recovery/reconcile', body, maintenance=True, **options)

    def test_record_is_durable_immutable_and_does_not_confirm_or_queue(self):
        body = self.request_body(); task = self.store.get(self.task_id); job = self.store.get(self.job_id)
        code, result = self.reconcile(body)
        self.assertEqual(code, 200, result); self.assertTrue(result['recorded'])
        self.assertFalse(result['queued']); self.assertFalse(result['resume_allowed'])
        self.assertEqual(result['execution_permission'], 'none')
        self.assertEqual(self.store.get(self.task_id), task); self.assertEqual(self.store.get(self.job_id), job)
        record = self.store.native_reconciliation(self.task_id, body['target'])
        self.assertEqual(record['id'], result['reconciliation_id'])
        other = Store(self.store.directory)
        try: self.assertEqual(other.native_reconciliation(self.task_id, body['target']), record)
        finally: other.db.close()
        with self.assertRaises(ValueError): self.store.update(record['id'], note='SYNTHETIC replacement')
        self.assertEqual(self.reconcile(body)[0], 400)  # Same evidence cannot overwrite the first judgment.
        after = self.assess()
        self.assertEqual(after['target'], body['target']); self.assertTrue(after['reconciliation_recorded'])
        with self.assertRaises(ValueError):
            self.engine.confirm_recovery(self.task_id, task['state_revision'], job['state_revision'],
                task['current_attempt']['id'], writer_stopped=True, checked_changes=True)

    def test_browser_and_maintenance_identity_and_exact_schema_required(self):
        body = self.request_body(); before = self.f.f.state()
        self.assertEqual(self.f.request('/api/workflow/recovery/reconcile', body)[0], 403)
        for options in ({'cookie': False}, {'ui': False}, {'origin': 'https://example.invalid'},
                        {'cookie': False, 'headers': {'Authorization': 'Bearer fictional-worker'}}):
            self.assertEqual(self.reconcile(body, **options)[0], 403)
        for extra in ({'actor': 'authenticated_maintenance_session'}, {'job_id': 'fictional-other-job'}, {'target': None}):
            self.assertEqual(self.reconcile(dict(body, **extra))[0], 400)
        self.assertEqual(self.f.f.state(), before)

    def test_confirmation_note_type_length_and_dlp_fail_without_writes(self):
        body = self.request_body(); before = self.f.f.state()
        for changes in ({'writer_stopped': False}, {'writer_stopped': 1}, {'checked_changes': False},
                        {'external_effects': 'unknown'}, {'note': None}, {'note': ''}, {'note': 'x' * 1001},
                        {'note': 'fictional-internal-tag'}):
            self.assertEqual(self.reconcile(dict(body, **changes))[0], 400, changes.keys())
        self.assertEqual(self.f.f.state(), before)

    def test_changed_files_or_own_operation_refuse_stale_target(self):
        body = self.request_body()
        path = self.f.f.workspace.project / 'app.py'
        path.write_bytes(path.read_bytes() + b'\n# SYNTHETIC changed after inspection\n')
        before = self.f.f.state()
        self.assertEqual(self.reconcile(body)[0], 400); self.assertEqual(self.f.f.state(), before)
        body = self.request_body()
        self.store.event('operation_result', 'SYNTHETIC uncorrelated effect', self.task_id, self.job_id)
        before = self.f.f.state()
        self.assertEqual(self.reconcile(body)[0], 400); self.assertEqual(self.f.f.state(), before)

    def test_other_running_task_requires_idle_without_stopping_it(self):
        body = self.request_body()
        job = self.store.put('job', {'id': 'fictional-other-job', 'goal': 'SYNTHETIC unrelated',
            'project': str(self.f.f.workspace.project.parent / 'fictional-other'), 'status': 'running'})
        task = self.engine.new_task(job, 'SYNTHETIC other worker', 'SYNTHETIC', 'builder')
        self.store.update(task['id'], status='running')
        before = self.f.f.state(); assessment = self.assess()
        self.assertFalse(assessment['eligible_for_reconciliation'])
        self.assertIn('native_maintenance_idle_required', assessment['reasons'])
        self.assertIsNone(assessment['target'])
        self.assertEqual(self.reconcile(body)[0], 400)
        self.assertEqual(self.f.f.state(), before); self.assertEqual(self.store.get(task['id'])['status'], 'running')

    def test_edit_between_two_observations_refuses_commit(self):
        from unittest.mock import patch
        body = self.request_body(); read = self.engine.native_recovery_assessment; count = []
        def observe(*args, **kwargs):
            result = read(*args, **kwargs); count.append(True)
            if len(count) == 1:
                path = self.f.f.workspace.project / 'app.py'
                path.write_bytes(path.read_bytes() + b'\n# SYNTHETIC between observations\n')
            return result
        before = self.f.f.state()
        with patch.object(self.engine, 'native_recovery_assessment', side_effect=observe):
            self.assertEqual(self.reconcile(body)[0], 400)
        self.assertEqual(len(count), 2); self.assertEqual(self.f.f.state(), before)

    def test_database_update_before_cas_refuses_without_a_record(self):
        from unittest.mock import patch
        body = self.request_body(); commit = self.store.commit_native_reconciliation
        def changed(*args, **kwargs):
            self.store.update(self.task_id, synthetic_concurrent_update=True)
            return commit(*args, **kwargs)
        with patch.object(self.store, 'commit_native_reconciliation', side_effect=changed):
            self.assertEqual(self.reconcile(body)[0], 400)
        self.assertEqual(self.store.for_job('native_effect_reconciliation', self.job_id), [])
        self.assertEqual(self.store.job_events(self.job_id, 'recovery_effects_reconciled'), [])

    def test_concurrent_submission_records_exactly_one_judgment(self):
        from concurrent.futures import ThreadPoolExecutor
        body = self.request_body()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.reconcile(body)[0], range(2)))
        self.assertEqual(sorted(results), [200, 400])
        self.assertEqual(len(self.store.for_job('native_effect_reconciliation', self.job_id)), 1)
        self.assertEqual(len(self.store.job_events(self.job_id, 'recovery_effects_reconciled')), 1)

    def test_worker_and_generic_store_writes_cannot_forge_a_judgment(self):
        from unittest.mock import patch
        body = self.request_body(); before = self.f.f.state()
        with self.assertRaises(ValueError):
            self.store.put('native_effect_reconciliation', {'id': 'fictional-forged-judgment'})
        with self.assertRaises(ValueError):
            self.store.put('fictional_other_kind', {'id': 'native-reconciliation-fictional-preempt'})
        with patch.object(self.store._worker, 'task', self.store.get(self.task_id), create=True):
            with self.assertRaises(ValueError):
                self.engine.reconcile_native_recovery(self.task_id, body['target'], writer_stopped=True,
                    checked_changes=True, external_effects='none_confirmed', note=body['note'],
                    actor='authenticated_maintenance_session')
            with self.assertRaisesRegex(ValueError, 'worker_cannot'):
                self.store.commit_native_reconciliation(self.task_id, body['target'], {}, body['note'], (), lambda: None)
        self.assertEqual(self.f.f.state(), before)

    def test_event_failure_rolls_back_judgment_and_restores_guard(self):
        from unittest.mock import patch
        body = self.request_body(); before = self.f.f.state()
        with patch.object(self.store, 'event', side_effect=RuntimeError('SYNTHETIC commit failure')):
            with self.assertRaises(RuntimeError):
                self.engine.reconcile_native_recovery(self.task_id, body['target'], writer_stopped=True,
                    checked_changes=True, external_effects='none_confirmed', note=body['note'],
                    actor='authenticated_maintenance_session')
        self.assertEqual(self.f.f.state(), before)
        self.assertIsNone(getattr(self.store._worker, 'native_reconciliation_commit', None))

    def test_new_file_edit_invalidates_record_display_without_changing_old_record(self):
        body = self.request_body(); self.assertEqual(self.reconcile(body)[0], 200)
        record = self.store.native_reconciliation(self.task_id, body['target'])
        path = self.f.f.workspace.project / 'app.py'
        path.write_bytes(path.read_bytes() + b'\n# SYNTHETIC after reconciliation\n')
        self.assertFalse(self.assess()['reconciliation_recorded'])
        self.assertEqual(self.store.native_reconciliation(self.task_id, body['target']), record)

    def test_active_worker_before_cas_refuses_without_a_record(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        body = self.request_body(); commit = self.store.commit_native_reconciliation
        def changed(*args, **kwargs):
            self.engine.active['fictional-concurrent-worker'] = SimpleNamespace(project='fictional-other-project')
            return commit(*args, **kwargs)
        try:
            with patch.object(self.store, 'commit_native_reconciliation', side_effect=changed):
                self.assertEqual(self.reconcile(body)[0], 400)
        finally: self.engine.active.clear()
        self.assertEqual(self.store.for_job('native_effect_reconciliation', self.job_id), [])

    def test_duplicated_reconciliation_event_is_not_a_valid_record(self):
        body = self.request_body(); self.assertEqual(self.reconcile(body)[0], 200)
        record = self.store.native_reconciliation(self.task_id, body['target'])
        self.store.event('recovery_effects_reconciled', 'SYNTHETIC duplicate', self.task_id, self.job_id,
            {'attempt_id': record['attempt_id'], 'reconciliation_id': record['id'],
             'target_digest': body['target']['state_digest'], 'record_digest': record['record_digest']})
        assessment = self.assess()
        self.assertFalse(assessment['eligible_for_reconciliation'])
        self.assertIn('native_reconciliation_record_unresolved', assessment['reasons'])
        self.assertIsNone(assessment['target'])

    def test_fresh_diagnostic_target_cannot_override_unsupported_tools(self):
        body = self.request_body()
        self.store.update(self.task_id, agent_run=dict(self.run, tool_calls={'mcpToolCall': 1}))
        assessment = self.assess()
        self.assertFalse(assessment['eligible_for_reconciliation'])
        self.assertIsNotNone(assessment['target'])
        body['target'] = assessment['target']; before = self.f.f.state()
        self.assertEqual(self.reconcile(body)[0], 400)
        self.assertEqual(self.f.f.state(), before)

    def test_other_connection_archive_update_before_cas_is_rejected(self):
        from unittest.mock import patch
        body = self.request_body(); commit = self.store.commit_native_reconciliation
        def changed(*args, **kwargs):
            other = Store(self.store.directory)
            try:
                other.put('agent_run', dict(self.run, job_id=self.job_id, task_id=self.task_id,
                    status='interrupted', tool_calls={'mcpToolCall': 1}))
            finally: other.db.close()
            return commit(*args, **kwargs)
        with patch.object(self.store, 'commit_native_reconciliation', side_effect=changed):
            self.assertEqual(self.reconcile(body)[0], 400)
        self.assertEqual(self.store.for_job('native_effect_reconciliation', self.job_id), [])

    def test_identifier_types_are_bounded_before_storage_lookup(self):
        from unittest.mock import patch
        body = self.request_body(); before = self.f.f.state()
        with patch.object(self.store, 'get', side_effect=AssertionError('SYNTHETIC unexpected storage lookup')) as get:
            for key in ('task_id', 'job_id'):
                for value in (None, True, [], {}, '', ' ', 'x' * 129):
                    self.assertEqual(self.reconcile(dict(body, **{key: value}))[0], 400)
            get.assert_not_called()
        self.assertEqual(self.f.f.state(), before)


if __name__ == '__main__':
    unittest.main()
