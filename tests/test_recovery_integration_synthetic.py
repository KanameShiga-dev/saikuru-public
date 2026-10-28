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
            'tool_observation_version': 1, 'tool_calls': {'fileChange': 1}}
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
        self.assertGreaterEqual(result['summary']['scope_changed_files'], 1)
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


if __name__ == '__main__': unittest.main()
