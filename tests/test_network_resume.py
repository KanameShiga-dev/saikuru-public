"""Fictional outages and manual resume; no real Provider calls."""
import json
import subprocess
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from team_recovery import (is_network_failure, network_operations_known,
                           probe_provider_connection, workflow_failure_plan)


class NetworkClassificationTests(unittest.TestCase):
    def test_only_explicit_transport_failures_are_network_waits(self):
        for message in ('dns error: ENOTFOUND', 'error resolving DNS (os error 11001)',
                        'EAI_AGAIN', 'connection reset by peer', 'ENETUNREACH'):
            self.assertTrue(is_network_failure('provider', message))
            self.assertEqual(workflow_failure_plan('provider', message)['category'], 'network')
        for code, message in (('security', 'ENOTFOUND'), ('format', 'ENOTFOUND'),
                              ('provider', 'authentication failed; ENOTFOUND'),
                              ('provider', 'quota exceeded; ENOTFOUND'),
                              ('provider', 'request timed out'), ('provider', 'unknown failure')):
            self.assertFalse(is_network_failure(code, message))

    def test_probe_is_bounded_and_does_not_send_http_or_credentials(self):
        with patch('team_recovery.subprocess.run', return_value=SimpleNamespace(returncode=0)) as run:
            self.assertTrue(probe_provider_connection('copilot')['reachable'])
        args, kwargs = run.call_args
        self.assertEqual(args[0][-1], 'api.business.githubcopilot.com')
        self.assertEqual(kwargs['timeout'], 8)
        self.assertNotIn('requests', args[0][2])
        self.assertNotIn('env', kwargs)
        for code in (2, 3, 4, 99):
            with patch('team_recovery.subprocess.run', return_value=SimpleNamespace(returncode=code)):
                self.assertFalse(probe_provider_connection('copilot')['reachable'])
        with patch('team_recovery.subprocess.run', side_effect=subprocess.TimeoutExpired('fictional', 8)):
            self.assertFalse(probe_provider_connection('copilot')['reachable'])
        with patch('team_recovery.subprocess.run') as run:
            with self.assertRaises(ValueError):
                probe_provider_connection('https://fictional.invalid')
            run.assert_not_called()

    def operation(self, at, **details):
        return {'at': at, 'details': dict(command_digest='a' * 64, attempt_id=None,
                                         correlated=False, **details)}

    def test_unknown_unfinished_or_timed_out_operations_are_not_replayed(self):
        start = self.operation(1)
        result = self.operation(2, exit_code=0, timed_out=False)
        self.assertTrue(network_operations_known([start], [result]))
        self.assertTrue(network_operations_known([], []))
        self.assertFalse(network_operations_known([start], []))
        self.assertFalse(network_operations_known([], [result]))
        self.assertFalse(network_operations_known([start], [self.operation(2, exit_code=None, timed_out=False)]))
        self.assertFalse(network_operations_known([start], [self.operation(2, exit_code=0, timed_out=True)]))
        self.assertFalse(network_operations_known([{'at': 1}], [result]))
        self.assertFalse(network_operations_known([start, self.operation(3)], [result]))


class NetworkResumeTests(unittest.TestCase):
    def setUp(self):
        from tests.test_user_recovery import UserRecoveryTests
        self.fixture = UserRecoveryTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        for key in ('engine', 'store', 'server', 'job_id', 'task_id'):
            setattr(self, key, getattr(self.fixture, key))
        self.store.update(self.task_id, failure_code='provider', summary='dns error: ENOTFOUND',
                          profile={'adapter': 'copilot', 'model': 'fictional-model', 'effort': 'low'})
        self.engine._hold_p0_task(self.store.get(self.task_id), 'provider')

    def target(self):
        current = self.engine.user_recovery_target(self.job_id)
        return {'task_id': current['task_id'], 'binding': current['binding']}

    def test_outage_and_reclassification_never_dispatch_or_change_approvals(self):
        before = self.store.for_job('approval', self.job_id)
        self.assertEqual(self.store.get(self.job_id)['p0_interruption']['recovery_status'], 'network_wait')
        stop = self.store.get(self.job_id)['p0_interruption']
        self.store.update(self.job_id, p0_interruption=dict(stop, recovery_status='maintenance_required'))
        with patch.object(self.engine, 'retry', side_effect=AssertionError('must not retry')):
            self.engine._restore_network_waits()
        self.assertEqual(self.store.for_job('approval', self.job_id), before)
        self.assertEqual(self.store.get(self.task_id)['status'], 'failed')
        self.assertEqual(self.store.get(self.job_id)['p0_interruption']['recovery_status'], 'network_wait')
        self.engine._restore_network_waits()
        self.assertEqual(len(self.store.job_events(self.job_id, 'network_wait_restored')), 1)
        public = self.server.app.public_state()
        stopped = next(t for t in public['tasks'] if t['id'] == self.task_id)
        self.assertEqual(stopped['title'], '通信回復後の再開待ち')
        self.assertNotIn('ENOTFOUND', json.dumps(public))

    def test_safety_and_terminal_history_are_not_migrated(self):
        for status, code in (('accepted', 'provider'), ('cancelled', 'provider'), ('blocked', 'security')):
            self.store.update(self.task_id, failure_code=code)
            stop = self.store.get(self.job_id)['p0_interruption']
            self.store.update(self.job_id, status=status, p0_interruption=dict(stop, recovery_status='maintenance_required'))
            self.engine._restore_network_waits()
            self.assertEqual(self.store.get(self.job_id)['p0_interruption']['recovery_status'], 'maintenance_required')
            self.assertEqual(self.store.get(self.job_id)['status'], status)

    def record_command(self, complete=True):
        details = {'command_digest': 'a' * 64, 'attempt_id': None, 'correlated': False}
        self.store.event('operation_bash', 'Fictional command', self.task_id, self.job_id, details)
        if complete:
            self.store.event('operation_result', 'Fictional test exited 0', self.task_id, self.job_id,
                             dict(details, exit_code=0, timed_out=False))

    def test_probe_does_not_resume_and_offline_resume_is_refused(self):
        target = self.target()
        with patch('team_recovery.probe_provider_connection', return_value={'reachable': True, 'reason': 'Fictional TLS success'}):
            self.assertTrue(self.engine.user_network_check(self.job_id, target)['reachable'])
        self.assertEqual(self.store.get(self.task_id)['status'], 'failed')
        with patch('team_recovery.probe_provider_connection', return_value={'reachable': False, 'reason': 'Fictional offline'}):
            with self.assertRaisesRegex(ValueError, 'Fictional offline'):
                self.engine.user_recovery_retry(self.job_id, target, True)
        self.assertEqual(self.store.get(self.task_id)['status'], 'failed')
        self.assertFalse(self.store.job_events(self.job_id, 'retry'))

    def test_recovered_connection_resumes_only_stopped_task_once_with_saved_results(self):
        self.record_command()
        target = self.target()
        before = self.store.get(self.task_id)
        other_tasks = [t for t in self.store.for_job('task', self.job_id) if t['id'] != self.task_id]
        approvals = self.store.for_job('approval', self.job_id)
        self.store.update(self.job_id, validation_evidence={'fictional': {'exit_code': 0}})
        target = self.target()
        with patch('team_recovery.probe_provider_connection', return_value={'reachable': True, 'reason': 'Fictional TLS success'}) as probe:
            self.engine.user_recovery_retry(self.job_id, target, True)
            self.assertEqual(probe.call_count, 1)
            with self.assertRaises(ValueError):
                self.engine.user_recovery_retry(self.job_id, target, True)
        current = self.store.get(self.task_id)
        self.assertEqual(current['status'], 'queued')
        self.assertEqual(current['profile'], before['profile'])
        self.assertEqual(current['attempt'], before['attempt'])
        self.assertIn('Fictional test exited 0', current['instruction'])
        self.assertIn('実行済みの操作を無条件に再送せず', current['instruction'])
        self.assertEqual(current['recovery_history'][-1]['summary'], before['summary'])
        self.assertEqual(self.store.for_job('approval', self.job_id), approvals)
        self.assertEqual([t for t in self.store.for_job('task', self.job_id) if t['id'] != self.task_id], other_tasks)
        self.assertEqual(self.store.get(self.job_id)['validation_evidence'], {'fictional': {'exit_code': 0}})
        self.assertEqual(self.store.get(self.job_id)['p0_interruption']['recovery_status'], 'retrying')

    def test_unknown_operation_and_unconfirmed_resume_never_probe_or_queue(self):
        self.record_command(complete=False)
        target = self.target()
        self.assertTrue(self.engine.user_recovery_target(self.job_id)['workflow_confirmation_required'])
        with patch('team_recovery.probe_provider_connection') as probe:
            for confirmed in (False, True):
                with self.assertRaises(ValueError):
                    self.engine.user_recovery_retry(self.job_id, target, confirmed)
            probe.assert_not_called()
        self.assertEqual(self.store.get(self.task_id)['status'], 'failed')

    def test_change_during_probe_invalidates_resume(self):
        target = self.target()
        def changed(adapter):
            self.store.event('fictional_change', 'Fictional operation changed', self.task_id, self.job_id)
            return {'reachable': True, 'reason': 'Fictional TLS success'}
        with patch('team_recovery.probe_provider_connection', side_effect=changed):
            with self.assertRaises(ValueError):
                self.engine.user_recovery_retry(self.job_id, target, True)
        self.assertEqual(self.store.get(self.task_id)['status'], 'failed')

    def test_network_api_requires_browser_session_and_preserves_maintenance_boundary(self):
        from server import Handler
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen
        self.server.RequestHandlerClass = Handler
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), thread.join(timeout=3)))
        origin = self.server.app.origin

        def call(path, body, authenticated=True):
            headers = {'Content-Type': 'application/json', 'X-Agent-Team-UI': '1', 'Origin': origin}
            if authenticated:
                headers['Cookie'] = 'agent_team=' + self.server.app.cookie
            request = Request(origin + path, data=json.dumps(body).encode(), headers=headers)
            try:
                with urlopen(request, timeout=5) as response:
                    return response.status
            except HTTPError as error:
                with error:
                    return error.code

        payload = {'job_id': self.job_id, 'target': self.target()}
        with patch('team_recovery.probe_provider_connection', return_value={'reachable': True, 'reason': 'Fictional success'}):
            self.assertEqual(call('/api/recovery/network-check', payload, False), 403)
            self.assertEqual(call('/api/recovery/network-check', dict(payload, host='fictional.invalid')), 400)
            self.assertEqual(call('/api/recovery/network-check', payload), 200)
            self.assertEqual(self.store.get(self.task_id)['status'], 'failed')
            self.assertEqual(call('/api/retry', {'id': self.task_id, 'checked_changes': True}), 403)
            self.assertEqual(call('/api/recovery/user-retry', dict(payload, confirmed=False)), 400)
            self.assertEqual(call('/api/recovery/user-retry', dict(payload, confirmed=True)), 200)
        self.assertEqual(self.store.get(self.task_id)['status'], 'queued')
