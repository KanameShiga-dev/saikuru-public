"""Synthetic G1/G2/G3 gates. These tests use temporary data and never start a provider."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from eval.development_readiness.reacceptance_fixture import make_fixture, close


class G1G2GateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server = make_fixture(Path(self.temp.name))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.project = Path(self.temp.name) / 'project'
        (self.project / 'SCOPE.md').write_text(
            '# Synthetic Scope\n\nOnly synthetic test files may change.\n', encoding='utf-8')
        self.engine = self.server.app.engine
        self.store = self.server.app.store

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        close(self.server)

    def api(self, path, body, authenticated=True, origin=None):
        headers = {'Content-Type': 'application/json', 'X-Agent-Team-UI': '1',
                   'Origin': origin or self.server.app.origin}
        if authenticated:
            headers['Cookie'] = 'agent_team=' + self.server.app.cookie
        request = Request(self.server.app.origin + path,
                          data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
                          headers=headers, method='POST')
        try:
            with urlopen(request, timeout=3) as response:
                return response.status
        except HTTPError as error:
            error.close()
            return error.code

    def create_job(self, **values):
        return self.engine.create_job('Synthetic gate', 'Test a fictional job gate.', str(self.project),
                                      True, **values)

    def planner(self, job_id):
        return next(t for t in self.store.for_job('task', job_id) if t['role'] == 'planner')

    def g2(self, job_id):
        return next(a for a in self.store.for_job('approval', job_id)
                    if a['kind'] == 'ai_start' and a['status'] == 'pending')

    def test_g1_registers_durable_pending_gate_and_tick_does_not_dispatch(self):
        job = self.create_job()
        task = self.planner(job['id'])
        self.assertEqual(job['status'], 'awaiting_approval')
        saved = self.store.get(job['id'], 'job')
        self.assertEqual(saved['g2_gate_version'], 1)
        self.assertEqual(saved['g2_status'], 'pending')
        self.assertEqual(task['status'], 'queued')
        self.assertTrue(self.g2(job['id'])['payload']['purpose'].startswith('AI計画生成のみ'))
        self.assertEqual([a['kind'] for a in self.store.for_job('approval', job['id'])], ['ai_start'])

        launched = []
        self.engine.paused = False
        self.engine._run = lambda queued: launched.append(queued['id'])
        self.engine.tick()
        self.assertEqual(launched, [])

    def test_g2_is_single_use_and_g3_remains_required_even_when_auto_execute_true(self):
        job = self.create_job()
        task = self.planner(job['id'])
        approval = self.g2(job['id'])
        self.engine.decide(approval['id'], True, 'Synthetic explicit G2 approval')
        self.assertEqual(self.store.get(job['id'])['g2_status'], 'approved')
        self.assertEqual(self.store.get(job['id'])['status'], 'planning')
        self.assertEqual([a['kind'] for a in self.store.for_job('approval', job['id'])
                          if a['status'] == 'pending'], [])

        self.store.update(task['id'], status='running', attempt=1)
        barrier = threading.Barrier(3)
        claims = []
        def claim():
            barrier.wait()
            claims.append(self.engine._claim_g2_for_run(task['id'], 1))
        workers = [threading.Thread(target=claim) for _ in range(2)]
        for worker in workers: worker.start()
        barrier.wait()
        for worker in workers: worker.join(timeout=3)
        self.assertEqual(sorted(claims), [False, True])
        self.store.update(job['id'], g2_status='consumed')
        with patch('team_plan_check.issues', return_value=[]), \
                patch('team_document_capabilities.require_plan'):
            self.engine._finish(task, {'status': 'done', 'summary': 'Synthetic plan only', 'tasks': []})
        pending = [a for a in self.store.for_job('approval', job['id']) if a['status'] == 'pending']
        self.assertEqual([a['kind'] for a in pending], ['plan'])
        self.assertEqual(self.store.get(job['id'])['status'], 'awaiting_approval')

    def test_changed_scope_supersedes_g2_and_requires_fresh_approval(self):
        job = self.create_job()
        task = self.planner(job['id'])
        old = self.g2(job['id'])
        self.engine.decide(old['id'], True, 'Synthetic explicit G2 approval')
        (self.project / 'SCOPE.md').write_text('# Synthetic Scope v2\n', encoding='utf-8')
        self.engine.paused = False
        self.engine.tick()
        self.assertEqual(self.store.get(old['id'])['status'], 'superseded')
        new = self.g2(job['id'])
        self.assertNotEqual(old['payload']['fingerprint'], new['payload']['fingerprint'])
        self.assertEqual(self.store.get(job['id'])['g2_status'], 'pending')
        self.assertEqual(self.store.get(job['id'])['status'], 'awaiting_approval')
        self.assertEqual(self.store.get(task['id'])['status'], 'queued')

    def test_malformed_gate_fails_closed(self):
        job = self.create_job()
        task = self.planner(job['id'])
        self.store.update(job['id'], g2_gate_version=99, status='planning')
        self.store.update(task['id'], status='queued')
        self.engine.paused = False
        self.engine.tick()
        self.assertEqual(self.store.get(job['id'])['status'], 'blocked')
        self.assertEqual(self.store.get(job['id'])['g2_status'], 'invalid')
        self.assertEqual(self.store.get(task['id'])['status'], 'blocked')

    def test_g2_requires_authenticated_local_ui_and_origin(self):
        job = self.create_job()
        approval = self.g2(job['id'])
        self.assertEqual(self.api('/api/decide', {'id': approval['id'], 'allow': True}, authenticated=False), 403)
        self.assertEqual(self.store.get(approval['id'])['status'], 'pending')
        self.assertEqual(self.api('/api/decide', {'id': approval['id'], 'allow': True},
                                  origin='http://example.invalid'), 403)
        self.assertEqual(self.store.get(approval['id'])['status'], 'pending')
        self.assertEqual(self.api('/api/decide', {'id': approval['id'], 'allow': True}), 200)
        self.assertEqual(self.store.get(approval['id'])['status'], 'approved')

    def test_registration_rejects_implicit_skill_file_writes(self):
        selected = {'ids': ['synthetic-skill'], 'mode': 'auto', 'names': ['synthetic']}
        with patch.object(self.engine, '_skill_selection', return_value=selected):
            with self.assertRaisesRegex(ValueError, 'スキルを適用しません'):
                self.create_job(skill_ids=['synthetic-skill'])
        self.assertFalse(self.store.all('job')[-1].get('g2_gate_version') == 1)

    def test_failed_registration_rolls_back_partial_runtime_records(self):
        before = {kind: len(self.store.all(kind)) for kind in ('job', 'task', 'approval')}
        with patch.object(self.engine, '_request_g2', side_effect=ValueError('synthetic registration failure')):
            with self.assertRaisesRegex(ValueError, 'synthetic registration failure'):
                self.create_job()
        after = {kind: len(self.store.all(kind)) for kind in ('job', 'task', 'approval')}
        self.assertEqual(before, after)

    def test_provider_change_after_g2_requires_new_approval(self):
        job = self.create_job()
        task = self.planner(job['id'])
        old = self.g2(job['id'])
        self.engine.decide(old['id'], True, 'Synthetic explicit G2 approval')
        profile = dict(task['profile'], model='synthetic-model-v2')
        self.store.update(task['id'], profile=profile)
        self.engine.paused = False
        self.engine.tick()
        self.assertEqual(self.store.get(old['id'])['status'], 'superseded')
        fresh = self.g2(job['id'])
        self.assertNotEqual(old['payload']['fingerprint'], fresh['payload']['fingerprint'])
        self.assertEqual(self.store.get(task['id'])['status'], 'queued')

    def test_prepare_new_request_is_read_only(self):
        before = {p.relative_to(self.project).as_posix(): p.read_bytes()
                  for p in self.project.rglob('*') if p.is_file()}
        note = self.server.app.prepare_new_request(str(self.project))
        after = {p.relative_to(self.project).as_posix(): p.read_bytes()
                 for p in self.project.rglob('*') if p.is_file()}
        self.assertIn('分離しました', note[0])
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
