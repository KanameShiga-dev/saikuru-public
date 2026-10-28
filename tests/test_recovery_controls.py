"""Fictional recovery control and loopback HTTP authorization tests only."""
import http.client
from http.server import ThreadingHTTPServer
import json
import sqlite3
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

if __package__:
    from . import test_checkpoint_collection as fixtures
else:
    import test_checkpoint_collection as fixtures
from server import Handler


class RecoveryControlTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.EngineDraftTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        for name in ('engine', 'store', 'task', 'job', 'app', 'project', 'root'):
            setattr(self, name, getattr(self.fixture, name))
        self.store.update(self.task['id'], status='interrupted', recovery_check_required=True)
        self.store.update(self.job['id'], status='interrupted')

    def payload(self):
        task = self.store.get(self.task['id'])
        job = self.store.get(self.job['id'])
        return {'id': task['id'], 'task_revision': task['state_revision'], 'job_revision': job['state_revision'],
                'attempt_id': task['current_attempt']['id'], 'writer_stopped': True, 'checked_changes': True}

    def confirm(self, **changes):
        body = dict(self.payload(), **changes)
        return self.engine.confirm_recovery(body['id'], body['task_revision'], body['job_revision'], body['attempt_id'],
                                           writer_stopped=body['writer_stopped'], checked_changes=body['checked_changes'])

    def checkpoint(self):
        draft = self.engine.collect_checkpoint(self.task['id'], writer_stopped=True)
        self.assertTrue(self.engine.finalize_checkpoint(draft['id'], writer_stopped=True)['handoff_synced'])
        return draft

    def test_confirmation_clears_fence_without_queuing_or_dispatch(self):
        with patch.object(self.store, 'reserve_attempt', side_effect=AssertionError('no dispatch')):
            result = self.confirm()
        task = self.store.get(self.task['id'])
        self.assertFalse(task['recovery_check_required'])
        self.assertEqual(task['status'], 'interrupted')
        self.assertFalse(result['queued'])
        self.assertFalse(result['resume_allowed'])
        self.assertEqual(len(self.store.job_events(self.job['id'], 'recovery_confirmed')), 1)

    def test_generic_store_cannot_clear_flag_or_forge_confirmation(self):
        with self.assertRaises(ValueError):
            self.store.update(self.task['id'], recovery_check_required=False)
        with self.assertRaises(ValueError):
            self.store.update(self.task['id'], recovery_confirmation={'event_seq': 1})
        self.assertTrue(self.store.get(self.task['id'])['recovery_check_required'])

    def test_stale_revision_wrong_attempt_and_nonboolean_confirmation_refused(self):
        for changes in ({'task_revision': True}, {'job_revision': True}, {'task_revision': 999999},
                        {'attempt_id': 'fictional-stale'}, {'writer_stopped': 'true'}, {'checked_changes': 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.confirm(**changes)
        self.assertFalse(self.store.job_events(self.job['id'], 'recovery_confirmed'))

    def test_operation_history_even_success_result_cannot_be_declared_no_effect(self):
        self.store.event('operation_result', 'Fictional command exited zero', self.task['id'], self.job['id'])
        with self.assertRaises(ValueError): self.confirm()
        with self.assertRaises(ValueError): self.engine.retry(self.task['id'], 'Fictional request')
        self.assertTrue(self.store.get(self.task['id'])['recovery_check_required'])

    def test_operation_after_confirmation_invalidates_manual_retry(self):
        self.confirm()
        self.store.event('operation_bash', 'Fictional uncertain action', self.task['id'], self.job['id'])
        with self.assertRaises(ValueError): self.engine.retry(self.task['id'], 'Fictional request')
        self.assertEqual(self.store.get(self.task['id'])['status'], 'interrupted')

    def test_untracked_native_run_is_not_proof_of_no_effect(self):
        task = self.store.get(self.task['id'])
        run = self.store.put('agent_run', {'id': 'fictional-native-run', 'job_id': self.job['id'],
            'task_id': task['id'], 'attempt_id': task['current_attempt']['id'], 'status': 'failed', 'provider': 'codex'})
        self.store.update(task['id'], agent_run_id=run['id'])
        self.assertFalse(self.store.has_operation_history(self.job['id']))
        with self.assertRaises(ValueError): self.confirm()
        self.assertTrue(self.store.get(self.task['id'])['recovery_check_required'])

    def test_manual_retry_requires_current_checkpoint_after_confirmation(self):
        self.confirm()
        with self.assertRaises(ValueError): self.engine.retry(self.task['id'], 'Fictional changes checked')
        self.checkpoint()
        old_attempt = self.store.get(self.task['id'])['attempt']
        self.engine.retry(self.task['id'], 'Fictional changes checked')
        task = self.store.get(self.task['id'])
        self.assertEqual(task['status'], 'queued')
        self.assertEqual(task['attempt'], old_attempt)
        self.assertIsNone(task.get('recovery_confirmation'))
        with self.assertRaises(ValueError): self.engine.retry(self.task['id'], 'duplicate')

    def test_changed_files_after_checkpoint_refuse_retry_without_restoring(self):
        self.confirm()
        self.checkpoint()
        self.app.write_text('value = 999\n', encoding='utf-8', newline='\n')
        with self.assertRaises(ValueError): self.engine.retry(self.task['id'], 'Fictional checked changes')
        self.assertEqual(self.app.read_text(encoding='utf-8'), 'value = 999\n')
        self.assertEqual(self.store.get(self.task['id'])['status'], 'interrupted')

    def test_expired_plan_cannot_confirm_or_retry(self):
        self.store.update(self.job['plan_approval_id'], status='expired')
        with self.assertRaises(ValueError): self.confirm()
        with self.assertRaises(ValueError): self.engine.retry(self.task['id'], 'Fictional request')

    def test_expired_separate_requirements_approval_is_not_reused(self):
        requirement = self.store.put('approval', {'id': 'fictional-requirements', 'job_id': self.job['id'],
            'kind': 'plan', 'status': 'approved', 'payload': {}})
        self.store.update(self.job['id'], requirements_approval_id=requirement['id'])
        self.store.update(requirement['id'], status='expired')
        with self.assertRaises(ValueError): self.confirm()
        self.assertTrue(self.store.get(self.task['id'])['recovery_check_required'])

    def test_confirmation_event_fault_does_not_clear_flag(self):
        original = self.store.event
        def fail(kind, *args, **kwargs):
            if kind == 'recovery_confirmed': raise sqlite3.OperationalError('fictional control fault')
            return original(kind, *args, **kwargs)
        with patch.object(self.store, 'event', side_effect=fail), self.assertRaises(sqlite3.Error): self.confirm()
        self.assertTrue(self.store.get(self.task['id'])['recovery_check_required'])
        self.assertFalse(self.store.job_events(self.job['id'], 'recovery_confirmed'))

    def test_cancelled_job_never_revived_by_recover_or_confirmation(self):
        self.engine.cancel_job(self.job['id'])
        self.store.recover()
        self.assertEqual(self.store.get(self.job['id'])['status'], 'cancelled')
        with self.assertRaises(ValueError): self.confirm()
        with self.assertRaises(ValueError): self.engine.retry(self.task['id'], 'Fictional request')

    def test_terminal_job_with_stray_running_task_stays_terminal(self):
        self.store.update(self.task['id'], status='running')
        self.store.update(self.job['id'], status='cancelled')
        self.store.recover()
        self.assertEqual(self.store.get(self.job['id'])['status'], 'cancelled')
        self.assertEqual(self.store.get(self.task['id'])['status'], 'cancelled')

    def test_restart_after_manual_queue_requires_another_manual_confirmation(self):
        self.confirm()
        self.checkpoint()
        self.engine.retry(self.task['id'], 'Fictional checked changes')
        self.store.recover()
        task = self.store.get(self.task['id'])
        self.assertEqual(task['status'], 'interrupted')
        self.assertTrue(task['recovery_check_required'])
        self.engine.paused = False
        with patch.object(self.engine, '_run', side_effect=AssertionError('must not dispatch')):
            self.engine.tick()
        self.assertEqual(self.store.get(self.task['id'])['status'], 'interrupted')

    def test_newflow_tool_approval_expires_and_attempt_fences_on_restart(self):
        self.store.update(self.task['id'], status='awaiting_approval')
        approval = self.engine.new_approval(self.store.get(self.task['id']), 'tool', {'summary': 'Fictional tool'})
        self.store.recover()
        self.assertEqual(self.store.get(approval['id'])['status'], 'expired')
        self.assertTrue(self.store.get(self.task['id'])['recovery_check_required'])
        self.assertFalse(self.store.job_events(self.job['id'], 'retry'))

    def test_preplan_restart_manual_retry_requests_fresh_g2_instead_of_dispatch(self):
        job = self.store.put('job', fixtures.initial_workflow(dict(id='fictional-preplan-job',
            title='Fictional preplan', goal='Fictional preplan', goal_contract=fixtures.initial('Fictional preplan'),
            project=str(self.project), status='awaiting_approval', request_origin='new', route='full', document_source=None,
            p0_contract_version=1, auto_execute=False, created_at=1, g2_gate_version=1, g2_status='consumed'), enabled=True))
        job = self.store.update(job['id'], status='planning')
        task = self.engine.new_task(job, 'Fictional planner', 'Fictional plan only', 'planner')
        self.store.reserve_attempt(task['id'], task['state_revision'])
        self.store.recover()
        task = self.store.get(task['id'])
        job = self.store.get(job['id'])
        self.engine.confirm_recovery(task['id'], task['state_revision'], job['state_revision'], task['current_attempt']['id'],
                                     writer_stopped=True, checked_changes=True)
        with patch.object(self.engine, '_request_g2') as request, patch.object(self.store, 'reserve_attempt') as dispatch:
            self.engine.retry(task['id'], 'Fictional stopped planner inspected')
            request.assert_called_once()
            self.assertEqual(request.call_args.args[:2], (job['id'], task['id']))
            dispatch.assert_not_called()
        self.assertEqual(self.store.get(task['id'])['status'], 'queued')

    def start_http(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        server.app = SimpleNamespace(engine=self.engine, store=self.store, port=server.server_port,
            origin=f'http://127.0.0.1:{server.server_port}', cookie='fictional-browser-session',
            maintenance_token='fictional-maintenance-capability')
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        def stop():
            server.shutdown(); thread.join(2); server.server_close()
        self.addCleanup(stop)
        return server

    def post(self, server, path, body, *, maintenance=False, cookie=True, ui=True, origin=None):
        headers = {'Content-Type': 'application/json'}
        if cookie: headers['Cookie'] = 'agent_team=' + server.app.cookie
        if ui: headers['X-Agent-Team-UI'] = '1'
        if maintenance: headers['X-Saikuru-Maintenance'] = server.app.maintenance_token
        if origin: headers['Origin'] = origin
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        self.addCleanup(connection.close)
        connection.request('POST', path, json.dumps(body), headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    def test_http_cookie_and_ui_header_cannot_selfgrant_maintenance(self):
        server = self.start_http()
        status, result = self.post(server, '/api/maintenance/recovery/confirm', self.payload())
        self.assertEqual(status, 403)
        self.assertTrue(self.store.get(self.task['id'])['recovery_check_required'])
        self.assertNotIn(server.app.maintenance_token, json.dumps(result))

    def test_http_maintenance_without_browser_identity_or_same_origin_is_refused(self):
        server = self.start_http()
        for options in ({'cookie': False}, {'ui': False}, {'origin': 'https://example.invalid'}):
            status, result = self.post(server, '/api/maintenance/recovery/confirm', self.payload(), maintenance=True, **options)
            self.assertEqual(status, 403)
        self.assertTrue(self.store.get(self.task['id'])['recovery_check_required'])

    def test_http_authenticated_confirmation_is_not_a_resume_and_retry_still_separate(self):
        server = self.start_http()
        status, result = self.post(server, '/api/maintenance/recovery/confirm', self.payload(), maintenance=True)
        self.assertEqual(status, 200)
        self.assertFalse(result['queued'])
        self.assertFalse(result['resume_allowed'])
        self.assertEqual(self.store.get(self.task['id'])['status'], 'interrupted')
        self.checkpoint()
        body = {'id':self.task['id'],'checked_changes':True,
                'workflow_target':self.engine.workflow_control_target(self.job['id'],'retry_task',self.task['id'])}
        status, result = self.post(server, '/api/retry', body)
        self.assertEqual(status, 403)
        status, result = self.post(server, '/api/retry', body, maintenance=True)
        self.assertEqual(status, 200)
        self.assertEqual(self.store.get(self.task['id'])['status'], 'queued')

    def test_http_unknown_fields_or_stale_versions_are_refused_without_mutation(self):
        server = self.start_http()
        for body in (dict(self.payload(), auto_resume=True), dict(self.payload(), task_revision=True)):
            status, result = self.post(server, '/api/maintenance/recovery/confirm', body, maintenance=True)
            self.assertEqual(status, 400)
        self.assertTrue(self.store.get(self.task['id'])['recovery_check_required'])


if __name__ == '__main__':
    unittest.main()
