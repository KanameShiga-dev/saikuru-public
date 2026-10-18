"""Adversarial G1/G2/G3 checks using synthetic data and isolated SQLite stores.

These tests never invoke a provider. Provider dispatch is replaced with a spy,
and all files and databases are created under unittest temporary directories.
"""
import json
import multiprocessing
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from eval.development_readiness import reacceptance_fixture as fixture
from eval.development_readiness.reacceptance_fixture import make_fixture
from server import Handler
from team_engine import Engine
from team_store import Store


def _claim_in_spawned_process(runtime, task_id, queue):
    """Spawn target: open only the synthetic test DB and attempt the G2 claim."""
    store = Store(runtime)
    engine = Engine.__new__(Engine)
    engine.store = store
    engine.config = fixture.default_config()
    engine.config['approved_roots'] = [str(Path(runtime).parent / 'project')]
    engine.config['provider_settings']['enabled'] = []
    engine.config['local_summary']['mode'] = 'off'
    try:
        queue.put(engine._claim_g2_for_run(task_id, 1))
    finally:
        store.db.close()


class G1G2AdversarialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='g123-p0-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.server = make_fixture(self.base)
        self.addCleanup(self.close_fixture)
        self.project = self.base / 'project'
        self.project.mkdir(exist_ok=True)
        self.scope = self.project / 'SCOPE.md'
        self.scope.write_text('# Synthetic scope\nOnly temporary test data.\n', encoding='utf-8')
        self.engine = self.server.app.engine
        self.store = self.server.app.store

    def close_fixture(self):
        self.server.server_close()
        self.server.app.engine.shutdown.set()
        self.server.app.engine.handoff.close()
        self.server.app.store.db.close()
        self.server.app.data_lock.close()

    def tree_snapshot(self):
        return {p.relative_to(self.project).as_posix(): p.read_bytes()
                for p in self.project.rglob('*') if p.is_file()}

    def create_job(self, auto_execute=False):
        return self.engine.create_job('架空のゲート試験', '合成データで承認境界を確認する。',
                                      str(self.project), auto_execute)

    def planner(self, job):
        return next(task for task in self.store.for_job('task', job['id'])
                    if task.get('role') == 'planner')

    def g2(self, job):
        return next(approval for approval in self.store.for_job('approval', job['id'])
                    if approval.get('kind') == 'ai_start'
                    and approval.get('status') == 'pending')

    def post_api(self, path, body, server=None):
        server = server or self.server
        request = Request(server.app.origin + path,
            data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'X-Agent-Team-UI': '1',
                     'Origin': server.app.origin,
                     'Cookie': 'agent_team=' + server.app.cookie}, method='POST')
        try:
            with urlopen(request, timeout=5) as response:
                return response.status
        except HTTPError as error:
            error.close()
            return error.code

    def start_http_fixture(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive(), 'HTTP fixture thread did not stop')

        self.addCleanup(stop)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if not thread.is_alive():
                self.fail('HTTP fixture thread exited before becoming ready')
            try:
                with urlopen(server.app.origin + '/health', timeout=0.5) as response:
                    if response.status == 200:
                        return thread
            except (OSError, URLError):
                pass
            time.sleep(0.02)
        self.fail('HTTP fixture did not become ready at /health')

    def _api_fixture(self):
        """Expose only the existing /api/jobs handler in the test-only fixture."""
        server = make_fixture(self.base / 'api')
        server.app.attachments = SimpleNamespace(select=lambda ids: [])
        server.app.engine.config['approved_roots'] = [str((self.base / 'api' / 'project').resolve())]
        server.app.config['approved_roots'] = server.app.engine.config['approved_roots']
        target = Path(server.app.config['approved_roots'][0])
        target.mkdir(parents=True, exist_ok=True)
        (target / 'SCOPE.md').write_text('# Synthetic API scope\n', encoding='utf-8')
        # Use the real Handler.do_POST while retaining the loopback fixture and
        # its authentication/origin checks; this changes no application source.
        return server

    def test_api_g1_registration_does_not_dispatch_or_change_project_files(self):
        server = self._api_fixture()
        self.addCleanup(lambda: (server.server_close(), server.app.engine.handoff.close(),
                                 server.app.store.db.close(), server.app.data_lock.close()))
        before = {p.relative_to(Path(server.app.config['approved_roots'][0])).as_posix(): p.read_bytes()
                  for p in Path(server.app.config['approved_roots'][0]).rglob('*') if p.is_file()}
        launched = []
        launched_event = threading.Event()

        def record_dispatch(task):
            launched.append(task['id'])
            launched_event.set()
        server.app.engine._run = record_dispatch
        self.start_http_fixture(server)
        # FixtureHandler rejects /api/jobs by design; invoke the same production
        # handler method only for this request without changing its common/auth path.
        with patch.object(fixture.FixtureHandler, 'do_POST', Handler.do_POST):
            status = self.post_api('/api/jobs', {
                'consent': True, 'title': '架空登録', 'goal': '合成の承認待ちを作る。',
                'project': server.app.config['approved_roots'][0], 'auto_execute': True,
                'attachment_ids': [], 'skill_mode': 'auto',
            }, server)
        self.assertEqual(status, 200)
        jobs = [job for job in server.app.store.all('job') if job.get('title') == '架空登録']
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job['g2_status'], 'pending')
        self.assertEqual(job['status'], 'awaiting_approval')
        server.app.engine.paused = False
        for _ in range(3):
            server.app.engine.tick()
        launched_event.wait(0.1)
        self.assertEqual(launched, [])
        after = {p.relative_to(Path(server.app.config['approved_roots'][0])).as_posix(): p.read_bytes()
                 for p in Path(server.app.config['approved_roots'][0]).rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_api_registration_failure_rolls_back_runtime_records_and_project_files(self):
        server = self._api_fixture()
        self.addCleanup(lambda: (server.server_close(), server.app.engine.handoff.close(),
                                 server.app.store.db.close(), server.app.data_lock.close()))
        root = Path(server.app.config['approved_roots'][0])
        before_files = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}
        before_rows = {kind: len(server.app.store.all(kind)) for kind in ('job', 'task', 'approval')}
        self.start_http_fixture(server)
        with patch.object(fixture.FixtureHandler, 'do_POST', Handler.do_POST), \
             patch.object(server.app.engine, '_request_g2', side_effect=ValueError('synthetic fail')):
            status = self.post_api('/api/jobs', {
                'consent': True, 'title': '架空失敗登録', 'goal': '部分登録を残さない。',
                'project': str(root), 'auto_execute': False, 'attachment_ids': [], 'skill_mode': 'auto',
            }, server)
        self.assertEqual(status, 400)
        self.assertEqual(before_rows, {kind: len(server.app.store.all(kind))
                                       for kind in ('job', 'task', 'approval')})
        self.assertEqual(before_files,
                         {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()})

    def test_g3_pending_never_dispatches_followup_for_either_auto_execute_setting(self):
        for auto_execute in (False, True):
            with self.subTest(auto_execute=auto_execute):
                job = self.create_job(auto_execute)
                planner = self.planner(job)
                approval = self.g2(job)
                self.engine.decide(approval['id'], True, 'synthetic G2 approval')
                self.store.update(planner['id'], status='running', attempt=1)
                self.store.update(job['id'], g2_status='consumed')
                with patch('team_plan_check.issues', return_value=[]), \
                     patch('team_document_capabilities.require_plan'):
                    self.engine._finish(planner, {'status': 'done', 'summary': 'synthetic plan', 'tasks': []})
                child = self.engine.new_task(self.store.get(job['id']), 'synthetic child',
                                             'never dispatch before G3', 'builder', planner['id'])
                launched = []
                self.engine.paused = False
                self.engine._run = lambda task: launched.append(task['id'])
                self.engine.tick()
                self.assertEqual(self.store.get(job['id'])['status'], 'awaiting_approval')
                self.assertEqual(self.store.get(child['id'])['status'], 'queued')
                self.assertEqual(launched, [])
                self.engine.paused = True

    def test_pending_g2_survives_store_and_engine_recreation(self):
        job = self.create_job()
        task = self.planner(job)
        approval = self.g2(job)
        runtime = self.store.directory
        self.server.app.engine.shutdown.set()
        self.server.app.engine.handoff.close()
        self.store.db.close()
        self.server.app.data_lock.close()
        self.server.app.data_lock = SimpleNamespace(close=lambda: None)
        reopened = Store(runtime)
        config = fixture.default_config()
        config['approved_roots'] = [str(self.project.resolve())]
        config['provider_settings']['enabled'] = []
        config['local_summary']['mode'] = 'off'
        engine = Engine(reopened, config, {}, 'http://127.0.0.1:1')
        self.server.app.store, self.server.app.engine = reopened, engine
        engine.paused = False
        launched = []
        engine._run = lambda queued: launched.append(queued['id'])
        engine.tick()
        self.assertEqual(reopened.get(job['id'])['g2_status'], 'pending')
        self.assertEqual(reopened.get(approval['id'])['status'], 'pending')
        self.assertEqual(reopened.get(task['id'])['status'], 'queued')
        self.assertEqual(launched, [])

    def test_consumed_g2_cannot_be_reused_after_restart_and_planner_retry(self):
        job = self.create_job()
        task = self.planner(job)
        approval = self.g2(job)
        self.engine.decide(approval['id'], True, 'synthetic G2 approval')
        self.store.update(task['id'], status='running', attempt=1)
        self.assertTrue(self.engine._claim_g2_for_run(task['id'], 1))
        self.store.update(job['id'], g2_status='consumed', status='failed')
        self.store.update(task['id'], status='failed')
        runtime = self.store.directory
        self.server.app.engine.handoff.close()
        self.store.db.close()
        self.server.app.data_lock.close()
        self.server.app.data_lock = SimpleNamespace(close=lambda: None)
        reopened = Store(runtime)
        config = fixture.default_config()
        config['approved_roots'] = [str(self.project.resolve())]
        config['provider_settings']['enabled'] = []
        config['local_summary']['mode'] = 'off'
        engine = Engine(reopened, config, {}, 'http://127.0.0.1:1')
        self.server.app.store, self.server.app.engine = reopened, engine
        engine.retry(task['id'], 'synthetic retry')
        fresh_job = reopened.get(job['id'])
        fresh = reopened.get(fresh_job['g2_approval_id'], 'approval')
        self.assertNotEqual(approval['id'], fresh['id'])
        self.assertEqual(fresh['status'], 'pending')
        self.assertEqual(fresh['attempt'], 2)
        self.assertEqual(fresh_job['g2_status'], 'pending')
        self.assertFalse(engine._claim_g2_for_run(task['id'], 1))

    def test_switch_model_invalidates_previous_g2_authorization(self):
        job = self.create_job()
        task = self.planner(job)
        old = self.g2(job)
        self.engine.decide(old['id'], True, 'synthetic G2 approval')
        profile = dict(task['profile'], model=str(task['profile']['model']) + '-synthetic-v2')
        enabled = set(self.engine.config['provider_settings'].get('enabled', []))
        enabled.add(profile['adapter'])
        self.engine.config['provider_settings']['enabled'] = sorted(enabled)
        self.engine.switch_model(task['id'], profile)
        self.engine.paused = False
        launched = []
        self.engine._run = lambda queued: launched.append(queued['id'])
        self.engine.tick()
        fresh_job = self.store.get(job['id'])
        fresh = self.store.get(fresh_job['g2_approval_id'], 'approval')
        self.assertNotEqual(old['id'], fresh['id'])
        self.assertEqual(fresh['status'], 'pending')
        self.assertEqual(fresh_job['g2_status'], 'pending')
        self.assertEqual(launched, [])

    def test_manual_planner_retry_always_issues_new_g2(self):
        job = self.create_job()
        task = self.planner(job)
        approval = self.g2(job)
        self.engine.decide(approval['id'], True, 'synthetic G2 approval')
        self.store.update(task['id'], status='running', attempt=1)
        self.assertTrue(self.engine._claim_g2_for_run(task['id'], 1))
        self.store.update(job['id'], g2_status='consumed', status='failed')
        self.store.update(task['id'], status='failed')
        self.engine.retry(task['id'], 'synthetic manual retry')
        fresh_job = self.store.get(job['id'])
        fresh = self.store.get(fresh_job['g2_approval_id'], 'approval')
        self.assertNotEqual(approval['id'], fresh['id'])
        self.assertEqual(fresh['status'], 'pending')
        self.assertEqual(fresh['attempt'], 2)
        self.assertEqual(fresh_job['g2_status'], 'pending')

    def test_two_store_instances_can_claim_g2_only_once(self):
        job = self.create_job()
        task = self.planner(job)
        approval = self.g2(job)
        self.engine.decide(approval['id'], True, 'synthetic G2 approval')
        self.store.update(task['id'], status='running', attempt=1)
        other_store = Store(self.store.directory)
        other_engine = Engine.__new__(Engine)
        other_engine.store = other_store
        other_engine.config = fixture.default_config()
        other_engine.config['approved_roots'] = [str(self.project.resolve())]
        other_engine.config['provider_settings']['enabled'] = []
        other_engine.config['local_summary']['mode'] = 'off'
        barrier = threading.Barrier(3)
        claims = []
        def claim(engine):
            barrier.wait(timeout=5)
            claims.append(engine._claim_g2_for_run(task['id'], 1))
        workers = [threading.Thread(target=claim, args=(engine,))
                   for engine in (self.engine, other_engine)]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=5)
        for worker in workers:
            worker.join(timeout=10)
        other_store.db.close()
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(sorted(claims), [False, True])

    def test_spawned_processes_can_claim_g2_only_once(self):
        job = self.create_job()
        task = self.planner(job)
        approval = self.g2(job)
        self.engine.decide(approval['id'], True, 'synthetic G2 approval')
        self.store.update(task['id'], status='running', attempt=1)
        context = multiprocessing.get_context('spawn')
        queue = context.Queue()
        workers = [context.Process(target=_claim_in_spawned_process,
                                   args=(str(self.store.directory), task['id'], queue))
                   for _ in range(2)]
        for worker in workers:
            worker.start()
        values = []
        try:
            values = [queue.get(timeout=15) for _ in workers]
        finally:
            for worker in workers:
                worker.join(timeout=15)
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=3)
            queue.close()
        self.assertEqual([worker.exitcode for worker in workers], [0, 0])
        self.assertEqual(sorted(values), [False, True])

    def test_scope_change_before_claim_rejects_old_g2(self):
        job = self.create_job()
        task = self.planner(job)
        old = self.g2(job)
        self.engine.decide(old['id'], True, 'synthetic G2 approval')
        self.scope.write_text('# Synthetic scope changed before claim\n', encoding='utf-8')
        self.store.update(task['id'], status='running', attempt=1)
        self.assertFalse(self.engine._claim_g2_for_run(task['id'], 1))
        current = self.store.get(job['id'])
        fresh = self.store.get(current['g2_approval_id'], 'approval')
        self.assertNotEqual(old['id'], fresh['id'])
        self.assertEqual(fresh['status'], 'pending')
        self.assertEqual(current['g2_status'], 'pending')

    def test_malformed_g2_records_fail_closed(self):
        mutations = (
            ('unknown_gate_version', lambda job, approval: {'g2_gate_version': 99}),
            ('unknown_gate_status', lambda job, approval: {'g2_status': 'mystery'}),
            ('missing_approval_link', lambda job, approval: {'g2_approval_id': 'missing'}),
            ('wrong_job_link', None),
            ('wrong_revision', lambda job, approval: {'g2_revision': job['g2_revision'] + 1}),
        )
        for name, mutate in mutations:
            with self.subTest(corruption=name):
                job = self.create_job()
                task = self.planner(job)
                approval = self.g2(job)
                self.engine.decide(approval['id'], True, 'synthetic G2 approval')
                self.store.update(task['id'], status='running', attempt=1)
                if name == 'wrong_job_link':
                    foreign_job = self.create_job()
                    foreign_approval = self.g2(foreign_job)
                    foreign_job_before = self.store.get(foreign_job['id'])
                    foreign_approval_before = self.store.get(foreign_approval['id'], 'approval')
                    self.assertIsNone(self.engine._job_g2_approval(job['id'], foreign_approval['id']))
                    self.store.update(job['id'], g2_approval_id=foreign_approval['id'])
                else:
                    self.store.update(job['id'], **mutate(job, approval))
                self.assertFalse(self.engine._claim_g2_for_run(task['id'], 1))
                current_job = self.store.get(job['id'])
                self.assertNotEqual(current_job['g2_status'], 'executing')
                if name in ('missing_approval_link', 'wrong_job_link'):
                    current_approval = self.store.get(current_job['g2_approval_id'], 'approval')
                    self.assertEqual(current_job['g2_status'], 'pending')
                    self.assertEqual(current_job['status'], 'awaiting_approval')
                    self.assertEqual(current_approval['status'], 'pending')
                if name == 'wrong_job_link':
                    self.assertEqual(self.store.get(foreign_job['id']), foreign_job_before)
                    self.assertEqual(self.store.get(foreign_approval['id'], 'approval'), foreign_approval_before)

        with self.subTest(corruption='database_read_failure_is_not_missing_link'):
            job = self.create_job()
            task = self.planner(job)
            approval = self.g2(job)
            self.engine.decide(approval['id'], True, 'synthetic G2 approval')
            self.store.update(task['id'], status='running', attempt=1)
            with patch.object(self.store, 'for_job', side_effect=sqlite3.OperationalError('synthetic db read failure')):
                with self.assertRaises(sqlite3.OperationalError):
                    self.engine._claim_g2_for_run(task['id'], 1)
            current_job = self.store.get(job['id'])
            self.assertEqual(current_job['g2_status'], 'approved')
            self.assertEqual(current_job['status'], 'planning')


if __name__ == '__main__':
    unittest.main()
