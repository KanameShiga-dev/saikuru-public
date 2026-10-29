"""Real local engine/storage boundaries; no provider or native CLI is started."""
import shutil
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from eval.development_readiness.reacceptance_fixture import make_fixture, close


class RecoveryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / 'source'
        self.server = make_fixture(self.base)
        self.addCleanup(lambda: close(self.server))
        self.bind()
        self.task_id = self.store.all('task')[0]['id']
        self.approval_id = self.store.all('approval')[0]['id']

    def bind(self):
        self.engine = self.server.app.engine
        self.store = self.server.app.store

    def reopen(self):
        close(self.server)
        self.server = make_fixture(self.base)
        self.bind()

    def test_cancel_survives_restart_and_scheduler_does_not_launch(self):
        artifact = self.base / 'project' / 'synthetic.txt'
        artifact.write_text('synthetic edit', encoding='utf-8')
        self.store.update(self.task_id, status='running')
        self.store.update('synthetic-reacceptance', status='running')
        cancel = threading.Event()
        self.engine.active[self.task_id] = SimpleNamespace(cancel=cancel)
        self.engine.cancel_job('synthetic-reacceptance')
        self.assertTrue(cancel.is_set())
        self.engine.active.clear()
        self.reopen()
        launched = threading.Event()
        self.engine._run = lambda task: launched.set()
        self.engine.paused = False
        for _ in range(3): self.engine.tick()
        self.assertFalse(launched.is_set())
        for ident in ('synthetic-reacceptance', self.task_id, self.approval_id):
            self.assertEqual(self.store.get(ident)['status'], 'cancelled')
        self.assertEqual(artifact.read_text(encoding='utf-8'), 'synthetic edit')
        with self.assertRaises(ValueError): self.engine.decide(self.approval_id, True, '')

    def test_restart_requires_retry_and_dispatches_once(self):
        self.store.update(self.task_id, status='awaiting_approval')
        self.store.update('synthetic-reacceptance', status='running')
        self.store.update(self.approval_id, kind='tool')
        self.reopen()
        self.assertEqual(self.store.get(self.task_id)['status'], 'interrupted')
        self.assertEqual(self.store.get(self.approval_id)['status'], 'expired')
        launches = []
        launched = threading.Event()
        def synthetic_worker(task):
            launches.append(task['id'])
            launched.set()
        self.engine._run = synthetic_worker
        self.engine.paused = False
        self.engine.tick()
        self.assertEqual(launches, [])
        self.engine.retry(self.task_id, 'Synthetic changes inspected')
        with self.assertRaises(ValueError): self.engine.retry(self.task_id, 'duplicate')
        self.engine.tick()
        self.assertTrue(launched.wait(2))
        self.engine.tick()
        self.assertEqual(launches, [self.task_id])
        self.assertEqual(self.store.get(self.approval_id)['status'], 'expired')

    def test_restore_both_databases_and_preserve_acceptance(self):
        self.engine.acknowledge_completion_review(self.approval_id, True)
        self.engine.accept_completion(self.approval_id, '正式受領')
        new_id = self.engine.record_completion_evidence(self.approval_id, 0, 0, 'Synthetic observation', True)['approval_id']
        self.engine.acknowledge_completion_review(new_id, True)
        self.engine.accept_completion(new_id, '正式受領')
        primary = self.base / 'runtime' / self.store.backup()
        handoff = self.base / 'runtime' / ('handoff-' + primary.name)
        self.engine.handoff.backup(handoff)
        target = Path(self.temp.name) / 'restored'
        (target / 'runtime').mkdir(parents=True)
        shutil.copy2(primary, target / 'runtime' / 'team.sqlite3')
        shutil.copy2(handoff, target / 'runtime' / 'handoff.sqlite3')
        restored = make_fixture(target)
        try:
            for name in ('team.sqlite3', 'handoff.sqlite3'):
                with closing(sqlite3.connect((target/'runtime'/name).as_uri()+'?mode=ro', uri=True)) as db:
                    self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                    original = self.store.db if name == 'team.sqlite3' else self.engine.handoff.db
                    for table in tables:
                        quoted = '"' + table.replace('"', '""') + '"'
                        self.assertEqual(db.execute('SELECT * FROM '+quoted).fetchall(), original.execute('SELECT * FROM '+quoted).fetchall())
            self.assertEqual(restored.app.store.get('synthetic-reacceptance')['status'], 'accepted')
            self.assertEqual(len(restored.app.store.all('approval')), 2)
            restored.app.store.update('synthetic-reacceptance', title='Synthetic restored copy')
            self.assertNotEqual(restored.app.store.get('synthetic-reacceptance')['title'], self.store.get('synthetic-reacceptance')['title'])
        finally:
            close(restored)
        # On Windows this also verifies that backup destinations are no longer held open.
        primary.rename(primary.with_suffix('.checked'))
        handoff.rename(handoff.with_suffix('.checked'))


class WorkflowRecoveryApiIntegrationTests(unittest.TestCase):
    def setUp(self):
        from tests import test_workflow_controls as fixtures
        self.f = fixtures.WorkflowControlTests(); self.addCleanup(self.f.doCleanups); self.f.setUp()

    def test_recover_stale_target_confirm_checkpoint_then_separate_retry(self):
        f = self.f
        stale = f.body('retry_task')
        old_attempt = f.store.get(f.task_id)['current_attempt']['id']
        f.store.recover()  # Real fictional Store restart recovery, no App process.
        before = f.state()
        self.assertEqual(f.request('/api/workflow/control', stale, maintenance=True)[0], 400)
        self.assertEqual(f.state(), before)
        body = f.body('confirm_recovery')
        self.assertEqual(f.request('/api/workflow/control', body)[0], 403)
        code, result = f.request('/api/workflow/control', body, maintenance=True)
        self.assertEqual(code, 200, result); self.assertFalse(result['queued'])
        self.assertFalse(result['resume_allowed'])
        checkpoint = f.f.checkpoint()
        self.assertTrue(checkpoint)
        self.assertEqual(f.request('/api/workflow/control', stale, maintenance=True)[0], 400)
        retry = f.body('retry_task')
        code, result = f.request('/api/workflow/control', retry, maintenance=True)
        self.assertEqual(code, 200, result); self.assertTrue(result['queued'])
        self.assertFalse(result['started']); self.assertFalse(result['formal_receipt'])
        self.assertEqual(f.request('/api/workflow/control', retry, maintenance=True)[0], 400)
        task = f.store.get(f.task_id)
        reserved = f.store.reserve_attempt(f.task_id, task['state_revision'])
        self.assertNotEqual(reserved['current_attempt']['id'], old_attempt)
        self.assertEqual(reserved['current_attempt']['status'], 'running')
        f.store.close_attempt(f.task_id, 'closed', 'synthetic_recovery_probe_only')

    def test_unknown_operation_cannot_pass_recovery_even_with_current_target(self):
        f = self.f
        f.store.event('operation_result', 'SYNTHETIC unresolved external effect', f.task_id, f.job_id)
        before = f.state()
        for action in ('confirm_recovery', 'retry_task'):
            code, _ = f.request('/api/workflow/control', f.body(action), maintenance=True)
            self.assertEqual(code, 400)
            self.assertEqual(f.state(), before)
        self.assertTrue(f.store.get(f.task_id)['recovery_check_required'])

    def test_restart_retry_cannot_reuse_an_old_tool_approval(self):
        f = self.f
        old = f.engine.new_approval(f.store.get(f.task_id), 'tool',
                                   {'operation': 'Bash', 'details': {'command': 'fictional external action'}})
        f.store.recover()
        self.assertEqual(f.store.get(old['id'])['status'], 'expired')
        self.assertEqual(f.request('/api/workflow/control', f.body('confirm_recovery'), maintenance=True)[0], 200)
        f.f.checkpoint()
        self.assertEqual(f.request('/api/workflow/control', f.body('retry_task'), maintenance=True)[0], 200)
        before = f.state()
        self.assertEqual(f.request('/api/decide', {'id': old['id'], 'allow': True})[0], 400)
        self.assertEqual(f.state(), before)
        task = f.store.get(f.task_id)
        task = f.store.reserve_attempt(f.task_id, task['state_revision'])
        fresh = f.engine.new_approval(task, 'tool', old['payload'])
        self.assertNotEqual(fresh['id'], old['id'])
        self.assertEqual(fresh['status'], 'pending')
        self.assertNotEqual(fresh['attempt'], old['attempt'])


class OperationCorrelationTests(unittest.TestCase):
    def setUp(self):
        from tests import test_checkpoint_collection as fixtures
        from team_engine import Context
        self.f = fixtures.EngineDraftTests(); self.addCleanup(self.f.doCleanups); self.f.setUp()
        task = self.f.store.update(self.f.task['id'], status='queued')
        task = self.f.store.reserve_attempt(task['id'], task['state_revision'])
        self.ctx = Context.__new__(Context); self.ctx.engine = self.f.engine; self.ctx.task = task

    def events(self):
        return (self.f.store.job_events(self.f.job['id'], 'operation_bash'),
                self.f.store.job_events(self.f.job['id'], 'operation_result'))

    def test_explicit_provider_id_pairs_start_and_result_without_persisting_raw_id(self):
        raw = 'fictional-provider-operation'
        self.ctx.command_started('python -B -m unittest', raw)
        self.ctx.command_result('python -B -m unittest', 0, 'SYNTHETIC OK', operation_id=raw)
        starts, results = self.events()
        a, b = starts[-1]['details'], results[-1]['details']
        self.assertEqual(a['operation_id'], b['operation_id'])
        self.assertEqual(a['attempt_id'], self.ctx.task['current_attempt']['id'])
        self.assertTrue(a['correlated']); self.assertTrue(b['correlated'])
        self.assertNotIn(raw, str(starts + results))
        self.assertEqual(b['exit_code'], 0); self.assertFalse(b['timed_out'])

    def test_missing_or_invalid_provider_ids_are_not_inferred_from_same_command(self):
        for value in (None, '', 'x' * 257, 'fictional\ninvalid'):
            self.ctx.command_started('python -B -m unittest', value)
            self.ctx.command_result('python -B -m unittest', 0, 'SYNTHETIC OK', operation_id=value)
        starts, results = self.events()
        self.assertTrue(all(not e['details']['correlated'] for e in starts + results))
        self.assertTrue(set(e['details']['operation_id'] for e in starts).isdisjoint(
            e['details']['operation_id'] for e in results))

    def test_fenced_attempt_cannot_record_a_late_correlated_result(self):
        from team_store import StaleAttempt
        self.ctx.command_started('python -B -m unittest', 'fictional-one')
        self.f.store.close_attempt(self.ctx.task['id'], 'fenced', 'synthetic interrupted')
        before = self.events()
        with self.assertRaises(StaleAttempt):
            self.ctx.command_result('python -B -m unittest', 0, 'SYNTHETIC late', operation_id='fictional-one')
        self.assertEqual(self.events(), before)

    def test_pair_assessment_is_read_only_and_grants_no_resume_permission(self):
        self.ctx.command_started('python -B -m unittest', 'fictional-assessed')
        self.ctx.command_result('python -B -m unittest', 0, 'SYNTHETIC OK', operation_id='fictional-assessed')
        before = self.events()
        view = self.f.store.operation_assessment(self.f.job['id'])
        self.assertEqual(len(view['result_observed']), 1); self.assertEqual(view['unresolved'], [])
        self.assertEqual(view['execution_permission'], 'none'); self.assertFalse(view['effects_reconciled'])
        self.assertEqual(self.events(), before)

    def test_unmatched_start_and_legacy_result_remain_unresolved(self):
        self.ctx.command_started('python -B -m unittest', 'fictional-unfinished')
        self.f.store.event('operation_result', 'SYNTHETIC legacy result', self.ctx.task['id'], self.f.job['id'])
        view = self.f.store.operation_assessment(self.f.job['id'])
        self.assertEqual(view['result_observed'], [])
        self.assertEqual({x['code'] for x in view['unresolved']},
                         {'operation_identity_unresolved', 'operation_pair_incomplete_or_duplicate'})

    def test_duplicate_result_is_not_reconciled_and_invalidates_digest(self):
        self.ctx.command_started('python -B -m unittest', 'fictional-duplicate')
        self.ctx.command_result('python -B -m unittest', 0, 'SYNTHETIC OK', operation_id='fictional-duplicate')
        first = self.f.store.operation_assessment(self.f.job['id'])
        self.ctx.command_result('python -B -m unittest', 0, 'SYNTHETIC duplicate', operation_id='fictional-duplicate')
        second = self.f.store.operation_assessment(self.f.job['id'])
        self.assertNotEqual(first['operation_digest'], second['operation_digest'])
        self.assertEqual(second['result_observed'], []); self.assertTrue(second['unresolved'])

    def test_foreign_attempt_and_unknown_operation_type_fail_closed(self):
        details = dict(self.ctx._operation_details('fictional-forged'), attempt_id='fictional-foreign')
        self.f.store.event('operation_result', 'SYNTHETIC foreign attempt', self.ctx.task['id'], self.f.job['id'], details)
        self.f.store.event('operation_unknown', 'SYNTHETIC unknown effect', self.ctx.task['id'], self.f.job['id'])
        view = self.f.store.operation_assessment(self.f.job['id'])
        self.assertEqual(view['result_observed'], []); self.assertEqual(len(view['unresolved']), 2)

    def test_same_id_does_not_pair_different_commands(self):
        self.ctx.command_started('python -B -m unittest', 'fictional-mismatch')
        self.ctx.command_result('python different.py', 0, 'SYNTHETIC different result', operation_id='fictional-mismatch')
        view = self.f.store.operation_assessment(self.f.job['id'])
        self.assertEqual(view['result_observed'], [])
        self.assertEqual(view['unresolved'][0]['code'], 'operation_command_changed')

    def test_timeout_and_unknown_exit_code_are_uncertain_even_with_matching_ids(self):
        for identifier, code, timeout in [('fictional-timeout', 0, True), ('fictional-unknown', None, False)]:
            self.ctx.command_started('python -B -m unittest', identifier)
            self.ctx.command_result('python -B -m unittest', code, 'SYNTHETIC uncertain', timeout, operation_id=identifier)
        view = self.f.store.operation_assessment(self.f.job['id'])
        self.assertEqual(view['result_observed'], []); self.assertEqual(len(view['unresolved']), 2)
        self.assertTrue(all(x['code'] == 'operation_result_uncertain' for x in view['unresolved']))

    def test_result_before_start_is_not_a_valid_pair(self):
        self.ctx.command_result('python -B -m unittest', 0, 'SYNTHETIC early', operation_id='fictional-reordered')
        self.ctx.command_started('python -B -m unittest', 'fictional-reordered')
        view = self.f.store.operation_assessment(self.f.job['id'])
        self.assertEqual(view['result_observed'], [])
        self.assertEqual(view['unresolved'][0]['code'], 'operation_order_invalid')

    def test_denied_worker_command_does_not_leave_an_execution_start(self):
        from unittest.mock import patch
        self.ctx.token = 'fictional-operation-worker'
        self.f.engine.active[self.ctx.task['id']] = self.ctx
        self.addCleanup(self.f.engine.active.clear)
        body = {'task_id': self.ctx.task['id'], 'tool': 'Bash',
                'input': {'command': 'python -B -m unittest'}, 'operation_id': 'fictional-denial'}
        with patch.object(self.f.engine, '_tool_request_context', return_value={'allow': False}):
            self.assertFalse(self.f.engine.tool_request(self.ctx.token, body)['allow'])
        self.assertEqual(self.events(), ([], []))
        with patch.object(self.f.engine, '_tool_request_context', return_value={'allow': True}):
            self.assertTrue(self.f.engine.tool_request(self.ctx.token, body)['allow'])
        self.assertEqual(len(self.events()[0]), 1)
        self.assertTrue(self.f.store.operation_assessment(self.f.job['id'])['unresolved'])

    def test_launched_native_worker_without_effect_reconciliation_still_blocks(self):
        self.f.store.update(self.ctx.task['id'], agent_run={
            'id': 'fictional-native-run', 'attempt_id': self.ctx.task['current_attempt']['id']})
        self.f.store.recover()
        before = self.f.store.get(self.ctx.task['id'])
        with self.assertRaisesRegex(ValueError, '担当プロセスの作用が未照合'):
            self.f.engine._recovery_control_inputs(self.ctx.task['id'])
        self.assertEqual(self.f.store.get(self.ctx.task['id']), before)


class NativeAssessmentTests(unittest.TestCase):
    def setUp(self):
        from tests import test_workflow_api as fixtures
        self.f = fixtures.WorkflowApiTests(); self.addCleanup(self.f.doCleanups); self.f.setUp()
        self.engine, self.store = self.f.engine, self.f.store
        self.job_id = self.f.f.job_id
        task = self.engine._reserve_workflow_attempt(self.f.f.first, self.engine.workflow_dispatch_view(self.job_id))
        self.task_id = task['id']
        self.run = {'id': 'fictional-native-preview', 'provider': task['profile']['adapter'],
            'model': task['profile']['model'], 'effort': task['profile'].get('effort'),
            'attempt_id': task['current_attempt']['id'], 'started_at': task['current_attempt']['started_at'],
            'measurement_ids': {'job_id': self.job_id, 'task_id': self.task_id, 'attempt_id': task['current_attempt']['id']},
            'tool_observation_version': 1, 'tool_calls': {'fileChange': 1},
            'scope_baseline': self.engine.prepare_native_scope_baseline(task)}
        self.store.update(self.task_id, agent_run=self.run)
        path = self.f.f.workspace.project / 'app.py'
        path.write_bytes(path.read_bytes() + b'\n# SYNTHETIC native file edit\n')
        self.store.recover()
        self.body = {'action': 'assess_recovery', 'job_id': self.job_id, 'task_id': self.task_id}

    def assess(self, **options):
        return self.f.request('/api/workflow/recovery/assess', self.body, maintenance=True, **options)

    def test_preview_requires_maintenance_and_browser_identity(self):
        before = self.f.f.state()
        self.assertEqual(self.f.request('/api/workflow/recovery/assess', self.body)[0], 403)
        for options in ({'cookie': False}, {'ui': False}, {'origin': 'https://example.invalid'},
                        {'cookie': False, 'headers': {'Authorization': 'Bearer fictional-worker'}}):
            self.assertEqual(self.assess(**options)[0], 403)
        self.assertEqual(self.f.f.state(), before)

    def test_file_only_preview_is_read_only_and_does_not_confirm_or_resume(self):
        before = self.f.f.state()
        code, result = self.assess()
        self.assertEqual(code, 200, result); self.assertTrue(result['eligible_for_reconciliation'], result)
        self.assertEqual(result['execution_permission'], 'none'); self.assertFalse(result['reconciliation_recorded'])
        self.assertEqual(result['summary']['tool_calls_observed'], {'fileChange': 1})
        self.assertFalse(result['summary']['outside_scope_changes'])
        self.assertEqual(result['summary']['scope_changed_files'], 1)
        self.assertEqual(result['summary']['scope_change_basis'], 'attempt_start')
        self.assertGreaterEqual(result['summary']['git_head_changed_files'], 2)
        self.assertEqual(result['summary']['external_effects'], 'unconfirmed')
        self.assertEqual(result['summary']['stop_reason_code'], 'restart')
        self.assertEqual(self.f.f.state(), before)
        with self.assertRaisesRegex(ValueError, '担当プロセスの作用が未照合'):
            self.engine._recovery_control_inputs(self.task_id)

    def test_unsupported_tools_and_legacy_observation_cannot_be_reconciled(self):
        for calls in ({'WebSearch': 1}, {'fictional-private-tool-name': 1}, {'Bash': 1}):
            self.store.update(self.task_id, agent_run=dict(self.run, tool_calls=calls))
            code, result = self.assess()
            self.assertEqual(code, 200); self.assertFalse(result['eligible_for_reconciliation'])
            self.assertIn('native_tool_kind_unsupported', result['reasons'])
            self.assertNotIn('fictional-private-tool-name', str(result))
        old = dict(self.run); old.pop('tool_observation_version')
        self.store.update(self.task_id, agent_run=old)
        self.assertIn('native_tool_observation_unknown', self.assess()[1]['reasons'])
        self.store.update(self.task_id, agent_run=dict(self.run, tool_observation_version=True))
        self.assertIn('native_tool_observation_unknown', self.assess()[1]['reasons'])

    def test_outbound_guard_and_tool_approval_are_reported_and_block_eligibility(self):
        self.store.event('outbound_guard', 'fictional-private-guard-content', self.task_id, self.job_id,
                         {'category': 'network'})
        approval = self.engine.new_approval(self.store.get(self.task_id), 'tool',
                                            {'operation': 'Bash', 'details': {'command': 'fictional external action'}})
        self.store.update(approval['id'], status='denied')
        code, result = self.assess()
        self.assertEqual(code, 200); self.assertFalse(result['eligible_for_reconciliation'])
        self.assertEqual(result['summary']['outbound_guard'], {'count': 1, 'types': {'network': 1}})
        self.assertEqual(result['summary']['approvals']['tool'], {'denied': 1})
        self.assertNotIn('fictional-private-guard-content', str(result))

    def test_outside_scope_change_and_own_operation_keep_the_preview_ineligible(self):
        outside = self.f.f.workspace.project / 'fictional-unapproved.txt'
        outside.write_text('SYNTHETIC outside approved checkpoint scope', encoding='utf-8')
        code, result = self.assess()
        self.assertEqual(code, 200); self.assertFalse(result['eligible_for_reconciliation'])
        self.assertTrue(result['summary']['outside_scope_changes'])
        outside.unlink()
        self.store.event('operation_result', 'SYNTHETIC unmatched target effect', self.task_id, self.job_id)
        self.assertIn('native_state_or_effects_unresolved', self.assess()[1]['reasons'])

    def test_target_changes_after_edit_and_extra_or_foreign_fields_are_rejected(self):
        code, first = self.assess(); self.assertEqual(code, 200)
        path = self.f.f.workspace.project / 'app.py'; path.write_bytes(path.read_bytes() + b'\n# SYNTHETIC later edit\n')
        self.assertNotEqual(first['target']['state_digest'], self.assess()[1]['target']['state_digest'])
        before = self.f.f.state()
        for body in (dict(self.body, actor='maintainer'), dict(self.body, job_id='fictional-other')):
            self.assertEqual(self.f.request('/api/workflow/recovery/assess', body, maintenance=True)[0], 400)
        self.assertEqual(self.f.f.state(), before)

    def test_archived_run_must_agree_with_the_attempt_tool_observations(self):
        self.store.put('agent_run', dict(self.run, task_id=self.task_id, job_id=self.job_id, status='interrupted'))
        self.assertTrue(self.assess()[1]['eligible_for_reconciliation'])
        self.store.update(self.run['id'], tool_calls={'WebSearch': 1})
        result = self.assess()[1]
        self.assertFalse(result['eligible_for_reconciliation'])
        self.assertIn('native_run_records_inconsistent', result['reasons'])

    def test_foreign_attempt_or_changed_model_identity_is_not_an_eligible_preview(self):
        for changes in ({'attempt_id': 'fictional-other-attempt'}, {'model': 'fictional-other-model'},
                        {'effort': 'fictional-other-effort'},
                        {'measurement_ids': dict(self.run['measurement_ids'], task_id='fictional-other-task')}):
            self.store.update(self.task_id, agent_run=dict(self.run, **changes))
            before = self.f.f.state()
            code, result = self.assess()
            self.assertEqual(code, 200); self.assertFalse(result['eligible_for_reconciliation'])
            self.assertIn('native_run_identity_unresolved', result['reasons'])
            self.assertEqual(self.f.f.state(), before)

    def test_missing_baseline_is_unknown_and_does_not_use_head_as_attempt_evidence(self):
        run = dict(self.run); run.pop('scope_baseline')
        self.store.update(self.task_id, agent_run=run)
        result = self.assess()[1]
        self.assertFalse(result['eligible_for_reconciliation'])
        self.assertIn('native_scope_baseline_unresolved', result['reasons'])
        self.assertIsNone(result['summary']['scope_changed_files'])
        self.assertEqual(result['summary']['scope_change_basis'], 'unknown')

    def test_baseline_identity_and_file_set_must_match_current_approval(self):
        original = self.run['scope_baseline']
        for changes in ({'attempt_id': 'fictional-other-attempt'}, {'plan_digest': '0' * 64},
                        {'contract_version': True}, {'files': {}}, {'files': dict(original['files'], **{'app.py': 'unknown'})}):
            self.store.update(self.task_id, agent_run=dict(self.run, scope_baseline=dict(original, **changes)))
            result = self.assess()[1]
            self.assertFalse(result['eligible_for_reconciliation'])
            self.assertIn('native_scope_baseline_unresolved', result['reasons'])

    def test_unknown_stop_reason_is_not_eligible(self):
        attempt = self.store.get(self.task_id)['current_attempt']
        for reason, label in (('fictional_future_reason', 'other'), ('', 'unknown')):
            self.store.event('attempt_closed', 'SYNTHETIC unknown reason', self.task_id, self.job_id,
                details={'attempt_id': attempt['id'], 'reason': reason})
            result = self.assess()[1]
            self.assertFalse(result['eligible_for_reconciliation'])
            self.assertEqual(result['summary']['stop_reason_code'], label)
            self.assertIn('native_stop_reason_unknown', result['reasons'])

    def test_assessment_file_and_git_observations_do_not_hold_outer_store_lock(self):
        from unittest.mock import patch
        from team_wbs import scope_file_observation
        workspace_view = self.engine.workspace_view
        observed = []
        def unlocked():
            acquired = []
            def reader():
                ok = self.store.lock.acquire(blocking=False); acquired.append(ok)
                if ok: self.store.lock.release()
            thread = threading.Thread(target=reader); thread.start(); thread.join(2)
            self.assertEqual(acquired, [True]); observed.append(True)
        def files(job, plan): unlocked(); return scope_file_observation(job, plan)
        def workspace(job_id): unlocked(); return workspace_view(job_id)
        before = self.f.f.state()
        with patch('team_wbs.scope_file_observation', side_effect=files), patch.object(self.engine, 'workspace_view', side_effect=workspace):
            result = self.assess()[1]
        self.assertEqual(len(observed), 2)
        self.assertTrue(result['eligible_for_reconciliation'], result)
        self.assertTrue(result['summary']['observation_stable'])
        self.assertEqual(self.f.f.state(), before)

    def test_state_write_during_observation_invalidates_target(self):
        from unittest.mock import patch
        from team_wbs import scope_file_observation
        def files(job, plan):
            self.store.update(self.task_id, synthetic_observation_changed=True)
            return scope_file_observation(job, plan)
        with patch('team_wbs.scope_file_observation', side_effect=files): result = self.assess()[1]
        self.assertFalse(result['eligible_for_reconciliation'])
        self.assertIn('native_assessment_changed', result['reasons'])
        self.assertIsNone(result['target'])
        self.assertFalse(result['summary']['observation_stable'])

    def test_second_sqlite_connection_write_invalidates_target(self):
        from unittest.mock import patch
        from team_wbs import scope_file_observation
        from team_store import Store
        def files(job, plan):
            other = Store(self.store.directory)
            try: other.update(self.task_id, synthetic_external_connection=True)
            finally: other.db.close()
            return scope_file_observation(job, plan)
        with patch('team_wbs.scope_file_observation', side_effect=files): result = self.assess()[1]
        self.assertFalse(result['eligible_for_reconciliation'])
        self.assertIn('native_assessment_changed', result['reasons'])
        self.assertIsNone(result['target'])

    def test_rolled_back_write_still_invalidates_the_observation_interval(self):
        from unittest.mock import patch
        from team_wbs import scope_file_observation
        before = self.f.f.state()
        def files(job, plan):
            try:
                with self.store.atomic():
                    self.store.update(self.task_id, synthetic_rolled_back=True)
                    raise RuntimeError('SYNTHETIC rollback')
            except RuntimeError: pass
            return scope_file_observation(job, plan)
        with patch('team_wbs.scope_file_observation', side_effect=files): result = self.assess()[1]
        self.assertEqual(self.f.f.state(), before)
        self.assertFalse(result['eligible_for_reconciliation'])
        self.assertIn('native_assessment_changed', result['reasons'])
        self.assertIsNone(result['target'])

    def test_mutable_run_copy_cannot_replace_the_immutable_baseline(self):
        baseline = self.run['scope_baseline']
        forged = dict(baseline, files=dict(baseline['files'], **{'app.py': '0' * 64}))
        self.store.update(self.task_id, agent_run=dict(self.run, scope_baseline=forged))
        result = self.assess()[1]
        self.assertFalse(result['eligible_for_reconciliation'])
        self.assertIn('native_scope_baseline_record_unresolved', result['reasons'])

    def test_absent_or_duplicated_commit_event_invalidates_the_baseline(self):
        attempt_id = self.run['attempt_id']
        record = self.store.native_scope_baseline(self.task_id, attempt_id)
        details = {'attempt_id': attempt_id, 'baseline_id': record['id'], 'snapshot_digest': record['snapshot_digest']}
        self.store.event('native_scope_baseline_committed', 'SYNTHETIC duplicate', self.task_id, self.job_id, details)
        result = self.assess()[1]
        self.assertFalse(result['eligible_for_reconciliation'])
        self.assertIn('native_scope_baseline_record_unresolved', result['reasons'])
        # Fault injection into this fixture's SQLite only; never coordination or App DB.
        self.store.db.execute("DELETE FROM events WHERE json_extract(body,'$.type')='native_scope_baseline_committed'")
        self.store.db.commit()
        result = self.assess()[1]
        self.assertFalse(result['eligible_for_reconciliation'])
        self.assertIn('native_scope_baseline_record_unresolved', result['reasons'])

    def test_recreated_commit_event_changes_the_assessment_target(self):
        original = self.assess()[1]
        self.assertTrue(original['eligible_for_reconciliation'], original)
        record = self.store.native_scope_baseline(self.task_id, self.run['attempt_id'])
        self.store.db.execute("DELETE FROM events WHERE json_extract(body,'$.type')='native_scope_baseline_committed'")
        self.store.db.commit()
        self.store.event('native_scope_baseline_committed', 'SYNTHETIC recreated event', self.task_id, self.job_id,
            {'attempt_id': self.run['attempt_id'], 'baseline_id': record['id'], 'snapshot_digest': record['snapshot_digest']})
        after = self.assess()[1]
        self.assertTrue(after['eligible_for_reconciliation'], after)
        self.assertNotEqual(original['target']['state_digest'], after['target']['state_digest'])

    def test_snapshot_only_sqlite_tamper_is_not_valid_assessment_evidence(self):
        import json
        record = self.store.native_scope_baseline(self.task_id, self.run['attempt_id'])
        stored = self.store.get(record['id'])
        stored['snapshot']['files']['app.py'] = '0' * 64
        self.store.db.execute('UPDATE objects SET body=? WHERE id=?', (json.dumps(stored), record['id']))
        self.store.db.commit()
        result = self.assess()[1]
        self.assertFalse(result['eligible_for_reconciliation'])
        self.assertIn('native_scope_baseline_record_unresolved', result['reasons'])

    def test_baseline_proof_observation_is_bound_without_other_database_changes(self):
        from unittest.mock import patch
        original = self.assess()[1]; before = self.f.f.state()
        read = self.store.native_scope_baseline
        def proof_version(*args):
            record = read(*args)
            return dict(record, event_seq=record['event_seq'] + 1)
        # Isolate the proof component from the independently hashed event rows.
        # The actual event replacement is exercised by the preceding test.
        with patch.object(self.store, 'native_scope_baseline', side_effect=proof_version):
            after = self.assess()[1]
        self.assertTrue(after['eligible_for_reconciliation'], after)
        self.assertEqual(self.f.f.state(), before)
        self.assertNotEqual(original['target']['state_digest'], after['target']['state_digest'])


class NativeBaselineCaptureTests(unittest.TestCase):
    def setUp(self):
        from tests import test_workflow_api as fixtures
        self.f = fixtures.WorkflowApiTests(); self.addCleanup(self.f.doCleanups); self.f.setUp()
        self.engine, self.store = self.f.engine, self.f.store
        self.task = self.engine._reserve_workflow_attempt(self.f.f.first,
            self.engine.workflow_dispatch_view(self.f.f.job_id))

    def test_actual_context_captures_before_agent_start_and_never_recaptures_launched_attempt(self):
        from team_engine import Context
        from team_common_agents import load_agent
        self.engine.commands = {'codex': ['fictional-never-launched'], 'claude': ['fictional-never-launched']}
        self.engine.usage_snapshot = lambda: {}
        self.engine.prepare_native_scope_baseline(self.task)
        ctx = Context(self.engine, self.task)
        baseline = ctx.native_scope_baseline
        self.assertEqual(baseline['attempt_id'], self.task['current_attempt']['id'])
        path = self.f.f.workspace.project / 'app.py'
        path.write_bytes(path.read_bytes() + b'\n# SYNTHETIC after capture\n')
        ctx.prepare_agent(load_agent('builder', self.task['profile']['adapter']))
        self.assertEqual(self.store.get(self.task['id'])['agent_run']['scope_baseline'], baseline)
        self.assertIsNone(self.engine.capture_native_scope_baseline(self.task))
        self.assertEqual(self.engine.prepare_native_scope_baseline(self.task), baseline)
        self.assertEqual(self.store.get(self.task['id'])['agent_run']['scope_baseline'], baseline)

    def test_capture_observes_files_outside_store_lock_and_rejects_changed_authority(self):
        from unittest.mock import patch
        from team_store import StaleAttempt
        from team_wbs import scope_file_observation
        def observe(job, plan):
            acquired = []
            def independent_reader():
                ok = self.store.lock.acquire(blocking=False); acquired.append(ok)
                if ok: self.store.lock.release()
            thread = threading.Thread(target=independent_reader); thread.start(); thread.join(2)
            self.assertEqual(acquired, [True])
            self.store.update(job['id'], synthetic_changed_authority=True)
            return scope_file_observation(job, plan)
        with patch('team_wbs.scope_file_observation', side_effect=observe):
            with self.assertRaises(StaleAttempt):
                self.engine.capture_native_scope_baseline(self.task)

    def test_stale_attempt_cannot_capture_a_new_baseline(self):
        from team_store import StaleAttempt
        self.store.recover()
        with self.assertRaises(StaleAttempt):
            self.engine.capture_native_scope_baseline(self.task)

    def test_worker_cannot_prepare_missing_baseline_or_construct_a_native_context(self):
        from team_engine import Context
        from team_adapters import ProviderError
        self.engine.commands = {'codex': ['fictional-never-launched'], 'claude': ['fictional-never-launched']}
        with self.store.attempt_scope(self.task):
            with self.assertRaisesRegex(ValueError, 'worker_cannot'):
                self.engine.prepare_native_scope_baseline(self.task)
            with self.assertRaises(ProviderError): Context(self.engine, self.task)
        self.assertEqual(self.store.for_job('native_scope_baseline', self.task['job_id']), [])

    def test_run_prepares_original_baseline_before_entering_worker_scope(self):
        from unittest.mock import patch
        def probe(task, *, preflight_error=None):
            self.assertIsNone(preflight_error)
            self.assertIsNotNone(getattr(self.store._worker, 'task', None))
            record = self.store.native_scope_baseline(task['id'], task['current_attempt']['id'])
            self.assertIsNotNone(record)
            self.assertEqual(self.engine.native_scope_baseline_for_context(task), record['snapshot'])
        with patch.object(self.engine, '_run_attempt', side_effect=probe) as run:
            self.engine._run(self.task)
        run.assert_called_once()
        self.assertEqual(len(self.store.job_events(self.task['job_id'], 'native_scope_baseline_committed')), 1)

    def test_preflight_failure_closes_attempt_without_context_or_provider(self):
        from unittest.mock import patch
        from team_adapters import ProviderError
        with (patch.object(self.engine, 'capture_native_scope_baseline', side_effect=ProviderError(
                'SYNTHETIC file observation failure', 'native_baseline', model_call_state='not_started')),
              patch('team_engine.Context') as context):
            self.engine._run(self.task)
        context.assert_not_called()
        current = self.store.get(self.task['id'])
        self.assertEqual(current['status'], 'failed')
        self.assertEqual(current['current_attempt']['status'], 'closed')

    def assert_workspace_block(self):
        from unittest.mock import patch
        with patch('team_engine.Context') as context: self.engine._run(self.task)
        context.assert_not_called()
        current = self.store.get(self.task['id'])
        self.assertEqual(current['failure_code'], 'workspace_boundary')
        self.assertEqual(current['current_attempt']['status'], 'closed')

    def test_outside_scope_preflight_keeps_workspace_boundary_classification(self):
        path = self.f.f.workspace.project / 'outside.py'
        path.write_text('SYNTHETIC user edit\n', encoding='utf-8')
        self.assert_workspace_block()
        self.assertEqual(path.read_text(encoding='utf-8'), 'SYNTHETIC user edit\n')

    def test_worktree_identity_preflight_keeps_workspace_boundary_classification(self):
        workspace = self.f.f.workspace
        workspace.git(workspace.project, 'checkout', '-b', 'codex/fictional-other-boundary')
        self.assert_workspace_block()

    def test_source_head_preflight_keeps_workspace_boundary_classification(self):
        workspace = self.f.f.workspace
        workspace.git(workspace.source, 'commit', '--allow-empty', '-m', 'SYNTHETIC source movement')
        self.assert_workspace_block()

    def test_context_rejects_changed_plan_digest_after_baseline_commit(self):
        from team_engine import Context
        from team_adapters import ProviderError
        self.engine.commands = {'codex': ['fictional-never-launched'], 'claude': ['fictional-never-launched']}
        baseline = self.engine.prepare_native_scope_baseline(self.task)
        self.store.update(baseline['plan_approval_id'], digest='0' * 64)
        with self.assertRaises(ProviderError): Context(self.engine, self.task)

    def test_context_rejects_snapshot_only_sqlite_tamper(self):
        import json
        from team_engine import Context
        self.engine.commands = {'codex': ['fictional-never-launched'], 'claude': ['fictional-never-launched']}
        baseline = self.engine.prepare_native_scope_baseline(self.task)
        record = self.store.native_scope_baseline(self.task['id'], baseline['attempt_id'])
        stored = self.store.get(record['id']); stored['snapshot']['files']['app.py'] = '0' * 64
        self.store.db.execute('UPDATE objects SET body=? WHERE id=?', (json.dumps(stored), record['id']))
        self.store.db.commit()
        with self.assertRaisesRegex(ValueError, 'record_invalid'): Context(self.engine, self.task)


class NativeBaselineStorageTests(unittest.TestCase):
    setUp = NativeBaselineCaptureTests.setUp

    def commit(self, snapshot=None, task_revision=None, job_revision=None):
        task = self.store.get(self.task['id']); job = self.store.get(task['job_id'])
        return self.store.commit_native_scope_baseline(task['id'],
            task['state_revision'] if task_revision is None else task_revision,
            job['state_revision'] if job_revision is None else job_revision,
            self.engine.capture_native_scope_baseline(self.task) if snapshot is None else snapshot)

    def test_record_and_event_survive_reopen_and_cannot_be_overwritten(self):
        from team_store import Store
        record = self.commit(); before = self.f.f.state()
        with self.assertRaises(ValueError): self.store.update(record['id'], snapshot={})
        with self.assertRaises(ValueError): self.commit(record['snapshot'])
        self.assertEqual(self.f.f.state(), before)
        other = Store(self.store.directory)
        try:
            self.assertEqual(other.get(record['id'], 'native_scope_baseline'), record)
            self.assertEqual(len(other.job_events(self.task['job_id'], 'native_scope_baseline_committed')), 1)
        finally: other.db.close()

    def test_worker_and_generic_save_cannot_forge_a_baseline(self):
        snapshot = self.engine.capture_native_scope_baseline(self.task)
        before = self.f.f.state()
        with self.assertRaises(ValueError):
            self.store.put('native_scope_baseline', {'id': 'fictional-forged-baseline', 'snapshot': snapshot})
        with self.store.attempt_scope(self.task):
            with self.assertRaisesRegex(ValueError, 'worker_cannot'):
                self.commit(snapshot)
            with self.assertRaises(ValueError):
                self.store.put('native_scope_baseline', {'id': 'fictional-worker-forgery', 'snapshot': snapshot})
        self.assertEqual(self.f.f.state(), before)

    def test_stale_revision_foreign_attempt_and_scope_are_rejected_atomically(self):
        snapshot = self.engine.capture_native_scope_baseline(self.task)
        before = self.f.f.state()
        for kwargs in ({'task_revision': 0}, {'job_revision': 0}, {'task_revision': True},
                       {'snapshot': dict(snapshot, attempt_id='fictional-other')},
                       {'snapshot': dict(snapshot, files={'outside.py': '0' * 64})},
                       {'snapshot': dict(snapshot, workspace_identity={})}):
            with self.assertRaises(ValueError): self.commit(**dict({'snapshot': snapshot}, **kwargs))
            self.assertEqual(self.f.f.state(), before)

    def test_post_launch_capture_is_refused_without_persisting_record_or_event(self):
        snapshot = self.engine.capture_native_scope_baseline(self.task)
        self.store.update(self.task['id'], agent_run={'attempt_id': snapshot['attempt_id']})
        before = self.f.f.state()
        with self.assertRaisesRegex(ValueError, 'after_launch'): self.commit(snapshot)
        self.assertEqual(self.f.f.state(), before)

    def test_event_failure_rolls_back_record_and_restores_write_guard(self):
        from unittest.mock import patch
        snapshot = self.engine.capture_native_scope_baseline(self.task)
        before = self.f.f.state()
        with patch.object(self.store, 'event', side_effect=RuntimeError('SYNTHETIC interrupted commit')):
            with self.assertRaises(RuntimeError): self.commit(snapshot)
        self.assertEqual(self.f.f.state(), before)
        self.assertIsNone(getattr(self.store._worker, 'native_baseline_commit', None))
        with self.assertRaises(ValueError):
            self.store.put('native_scope_baseline', {'id': 'fictional-after-failure', 'snapshot': snapshot})

    def clone_task(self, ident, **changes):
        task = dict(self.task, id=ident, **changes); task.pop('state_revision')
        return self.store.put('task', task)

    def test_true_is_rejected_when_task_revision_is_exactly_one(self):
        task = self.clone_task('fictional-revision-one')
        self.assertEqual(task['state_revision'], 1)
        snapshot = dict(self.engine.capture_native_scope_baseline(self.task), task_id=task['id'])
        job = self.store.get(task['job_id'])
        with self.assertRaises(ValueError):
            self.store.commit_native_scope_baseline(task['id'], True, job['state_revision'], snapshot)
        self.store.commit_native_scope_baseline(task['id'], 1, job['state_revision'], snapshot)

    def test_reviewer_and_task_without_work_package_cannot_commit(self):
        for ident, changes in (('fictional-reviewer', {'role': 'reviewer'}),
                               ('fictional-no-wp', {'work_package_id': None})):
            task = self.clone_task(ident, **changes)
            snapshot = dict(self.engine.capture_native_scope_baseline(self.task), task_id=ident)
            before = self.f.f.state()
            with self.assertRaises(ValueError):
                self.store.commit_native_scope_baseline(ident, task['state_revision'],
                    self.store.get(task['job_id'])['state_revision'], snapshot)
            self.assertEqual(self.f.f.state(), before)

    def test_not_ready_workspace_binding_is_refused_even_when_identity_matches(self):
        from team_harness import workspace_identity
        snapshot = self.engine.capture_native_scope_baseline(self.task)
        plan = self.store.get(snapshot['plan_approval_id'])
        boundary = dict(plan['workflow_revision']['workspace_boundary'], status='blocked')
        self.store.update(plan['id'], workflow_revision=dict(plan['workflow_revision'], workspace_boundary=boundary))
        snapshot['workspace_identity'] = workspace_identity(boundary)
        with self.assertRaisesRegex(ValueError, 'workspace_binding'): self.commit(snapshot)

    def test_noncanonical_hash_values_are_refused(self):
        snapshot = self.engine.capture_native_scope_baseline(self.task)
        for value in ('short', 'A' * 64, True):
            with self.assertRaisesRegex(ValueError, 'observation_invalid'):
                self.commit(dict(snapshot, files=dict(snapshot['files'], **{'app.py': value})))

    def test_closed_attempt_cannot_commit_with_fresh_revisions_and_matching_identity(self):
        snapshot = self.engine.capture_native_scope_baseline(self.task)
        self.store.close_attempt(self.task['id'], 'closed', 'completed')
        with self.assertRaises(ValueError): self.commit(snapshot)

    def test_reserved_baseline_prefix_cannot_be_occupied_by_another_kind(self):
        with self.assertRaisesRegex(ValueError, 'id_reserved'):
            self.store.put('fictional_unrelated_kind', {'id': 'native-baseline-fictional-preempt'})


class CodexItemObservationTests(unittest.TestCase):
    def protocol(self, item_types):
        from types import SimpleNamespace
        from unittest.mock import patch
        from team_adapters import CodexAdapter
        observed, sent = [], []
        class FakeProcess:
            def __init__(self, *args): self.messages = []
            def send(self, value):
                sent.append(value)
                if 'id' not in value: return
                method = value['method']
                result = ({'thread': {'id': 'fictional-thread'}} if method == 'thread/start' else
                          {'exitCode': 0, 'stdout': 'AGENT_TEAM_NATIVE_OK'} if method == 'command/exec' else {})
                self.messages.append({'id': value['id'], 'result': result})
                if method == 'turn/start':
                    for index, kind in enumerate(item_types):
                        item = {'id': 'fictional-item-' + str(index), 'type': kind,
                                'command': 'fictional command', 'exitCode': 0, 'aggregatedOutput': 'SYNTHETIC'}
                        self.messages.append({'method': 'item/started', 'params': {'item': item}})
                        self.messages.append({'method': 'item/completed', 'params': {'item': item}})
                    self.messages += [
                        {'method': 'item/completed', 'params': {'item': {'type': 'agentMessage', 'text': '{"status":"done"}'}}},
                        {'method': 'turn/completed', 'params': {'turn': {'status': 'completed'}}}]
            def receive(self, ctx): return self.messages.pop(0)
            def close(self): pass
        ctx = SimpleNamespace(task={'profile': {'model': 'fictional-no-provider', 'effort': 'medium'}},
            command=['fictional-codex-never-launched'], project=str(Path.cwd()), engine=SimpleNamespace(config={}),
            agent_system_instructions='SYNTHETIC protocol observation only',
            agent_definition=SimpleNamespace(sandbox='workspace-write', description='SYNTHETIC agent'),
            check=lambda: None, agent_started=lambda *a: None, event=lambda *a: None,
            count_tool=observed.append, command_started=lambda *a: None, command_result=lambda *a, **k: None)
        with patch('team_adapters.Process', FakeProcess):
            result = CodexAdapter().run(ctx, 'SYNTHETIC request',
                {'type': 'object', 'properties': {'status': {'type': 'string'}}, 'required': ['status']})
        self.assertEqual(result, {'status': 'done'})
        self.assertIn('thread/start', [v.get('method') for v in sent])
        self.assertNotIn('thread/resume', [v.get('method') for v in sent])
        return observed

    def test_control_messages_do_not_make_a_file_only_turn_ineligible(self):
        self.assertEqual(self.protocol(['userMessage', 'plan', 'sleep', 'enteredReviewMode',
            'exitedReviewMode', 'reasoning', 'contextCompaction', 'fileChange']), ['fileChange'])

    def test_effectful_and_unknown_items_are_still_observed(self):
        effects = ['commandExecution', 'dynamicToolCall', 'collabAgentToolCall', 'subAgentActivity',
                   'imageGeneration', 'imageView', 'functionCallOutput', 'hookPrompt',
                   'mcpToolCall', 'webSearch', 'fictionalFutureItem']
        self.assertEqual(self.protocol(effects), effects)


if __name__ == '__main__': unittest.main()
