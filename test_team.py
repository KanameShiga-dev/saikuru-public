import http.cookiejar
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from team_config import default_config, validate_config
from team_engine import Engine, Context
from team_store import Store
from team_adapters import ADAPTERS
from server import make_server, DataLock


PLAN = {'summary': '計画', 'tasks': [{'title': '調査', 'instruction': '調査する', 'role': 'researcher'},
                                   {'title': '実装', 'instruction': '実装する', 'role': 'builder'}]}
DONE = {'status': 'done', 'summary': '完了報告', 'checks': ['fixtureのみ'], 'question': ''}


class FixtureAdapter:
    def run(self, ctx, prompt, schema):
        return PLAN if ctx.task['role'] == 'planner' else DONE


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.config = default_config()
        self.config['approved_roots'] = [self.temp.name]
        self.engine = Engine(self.store, self.config, {'codex': ['fixture'], 'claude': ['fixture']}, 'http://127.0.0.1:1')
        self.saved = ADAPTERS.copy()
        ADAPTERS.update(codex=FixtureAdapter, claude=FixtureAdapter)

    def tearDown(self):
        self.engine.shutdown.set()
        deadline = time.time() + 3
        while self.engine.active and time.time() < deadline:
            time.sleep(.01)
        ADAPTERS.update(self.saved)
        self.engine.handoff.close()
        self.store.db.close()
        self.temp.cleanup()

    def job(self, auto=True):
        return self.engine.create_job('sample', 'local goal', self.temp.name, auto)

    def until(self, predicate):
        deadline = time.time() + 4
        while not predicate() and time.time() < deadline:
            self.engine.tick()
            time.sleep(.01)
        self.assertTrue(predicate())

    def test_automatic_assignment_and_acceptance(self):
        job = self.job()
        self.until(lambda: self.store.get(job['id'])['status'] == 'awaiting_acceptance')
        tasks = self.store.all('task')
        self.assertEqual([t['profile']['adapter'] for t in tasks], ['codex', 'codex', 'claude', 'codex'])
        self.assertTrue(all(t['status'] == 'succeeded' for t in tasks))
        a = self.store.all('approval')[-1]
        self.engine.decide(a['id'], True, '人が確認')
        self.assertEqual(self.store.get(job['id'])['status'], 'accepted')
        with self.assertRaises(ValueError):
            self.engine.decide(a['id'], True, '')

    def test_plan_gate_and_rejection(self):
        job = self.job(False)
        self.until(lambda: self.store.get(job['id'])['status'] == 'awaiting_approval')
        self.assertEqual(len(self.store.all('task')), 1)
        self.engine.decide(self.store.all('approval')[0]['id'], False, '却下')
        self.engine.tick()
        self.assertEqual(len(self.store.all('task')), 1)
        self.assertEqual(self.store.get(job['id'])['status'], 'blocked')

    def test_recovery_does_not_auto_retry(self):
        job = self.job()
        task = self.store.all('task')[0]
        self.store.update(task['id'], status='running')
        self.engine.new_approval(task, 'tool', {'command': 'example'})
        self.store.recover()
        self.engine.tick()
        self.assertEqual(self.store.get(task['id'])['status'], 'interrupted')
        self.assertEqual(self.store.all('approval')[0]['status'], 'expired')

    def test_same_project_serialized(self):
        self.job(); self.job()
        entered = threading.Event(); release = threading.Event()
        class WaitingAdapter:
            def run(s, ctx, prompt, schema):
                entered.set(); release.wait(2)
                return PLAN
        ADAPTERS['codex'] = WaitingAdapter
        self.engine.tick(); entered.wait(1); self.engine.tick()
        self.assertEqual(sum(t['status'] == 'running' for t in self.store.all('task')), 1)
        release.set()
        self.until(lambda: not self.engine.active)

    def test_cancel_invalidates_approval(self):
        job = self.job(); task = self.store.all('task')[0]
        a = self.engine.new_approval(task, 'tool', {'command': 'anything'})
        self.engine.cancel_job(job['id'])
        with self.assertRaises(ValueError):
            self.engine.decide(a['id'], True, '')

    def test_profile_snapshot_and_policy(self):
        self.job(); task = self.store.all('task')[0]
        self.config['profiles']['codex-standard']['model'] = 'future-model'
        self.assertEqual(self.store.get(task['id'])['profile']['model'], 'gpt-6-sol')
        self.config['profiles']['codex-standard']['effort'] = 'high'
        with self.assertRaises(ValueError): validate_config(self.config)

    def test_invalid_plan_and_unregistered_project(self):
        job = self.job(); task = self.store.all('task')[0]
        with self.assertRaises(Exception): self.engine._validate(task, {'summary': '', 'tasks': []})
        with self.assertRaises(ValueError): self.engine.create_job('x', 'x', str(Path(self.temp.name).parent), True)

    def test_tool_approval_bound_to_attempt(self):
        self.job(); task = self.store.all('task')[0]
        a = self.engine.new_approval(task, 'tool', {'operation': 'test'})
        self.store.update(task['id'], attempt=2)
        with self.assertRaises(ValueError): self.engine.decide(a['id'], True, '')
        self.assertEqual(self.store.get(a['id'])['status'], 'expired')

    def test_transaction_rolls_back_partial_plan(self):
        with self.assertRaises(RuntimeError):
            with self.store.atomic():
                self.store.put('test', {'id': 'not-saved'})
                raise RuntimeError('simulated failure')
        self.assertEqual(self.store.all('test'), [])

    def test_single_instance_lock(self):
        first = DataLock(self.temp.name)
        try:
            with self.assertRaises(RuntimeError): DataLock(self.temp.name)
        finally:
            first.close()

    def test_scoped_edits_backup_and_protected_paths(self):
        job = self.job(); task = self.engine.new_task(job, 'change', 'change file', 'builder')
        ctx = Context(self.engine, task); self.engine.active[task['id']] = ctx
        target = Path(self.temp.name) / 'example.txt'; target.write_text('original', encoding='utf-8')
        try:
            result = self.engine.tool_request(ctx.token, {'task_id': task['id'], 'tool': 'Edit', 'input': {'file_path': str(target)}})
            self.assertTrue(result['allow'])
            saved = list((Path(self.temp.name) / 'file-backups').rglob('before.bin'))
            self.assertEqual(saved[0].read_bytes(), b'original')
            for path in ('AGENTS.md', '.env.local', '../outside.txt'):
                result = self.engine.tool_request(ctx.token, {'task_id': task['id'], 'tool': 'Write', 'input': {'file_path': path}})
                self.assertFalse(result['allow'])
        finally:
            self.engine.active.clear()

    def test_tool_boundary(self):
        self.job(); task = self.store.all('task')[0]
        ctx = Context(self.engine, task); self.engine.active[task['id']] = ctx
        try:
            result = self.engine.tool_request(ctx.token, {'task_id': task['id'], 'tool': 'Write', 'input': {}})
            self.assertFalse(result['allow'])
            result = self.engine.tool_request(ctx.token, {'task_id': task['id'], 'tool': 'Read', 'input': {'file_path': '../outside'}})
            self.assertFalse(result['allow'])
            with self.assertRaises(ValueError): self.engine.tool_request('wrong', {'task_id': task['id']})
        finally:
            self.engine.active.clear()


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.server = make_server(cls.temp.name, 0)
        cls.base = cls.server.app.origin
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()
        cls.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        cls.client.open(cls.base + '/').read()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=5)
        cls.server.app.consultations.shutdown()
        cls.server.app.consultations.db.close()
        cls.server.app.ledger.db.close()
        cls.server.app.engine.handoff.close()
        cls.server.app.store.db.close(); cls.server.app.data_lock.close(); cls.temp.cleanup()

    def post(self, path, body, headers=None):
        h = {'Content-Type': 'application/json', 'X-Agent-Team-UI': '1'}
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers=h)
        return json.load(self.client.open(req))

    def test_ui_and_state(self):
        html = self.client.open(self.base + '/').read().decode()
        self.assertIn('作業ボード', html)
        self.assertEqual(json.load(self.client.open(self.base + '/api/state'))['jobs'], [])

    def test_cross_origin_denied(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post('/api/pause', {'paused': True}, {'Origin': 'https://untrusted.example'})
        self.assertEqual(ctx.exception.code, 403)

    def test_unauthenticated_cannot_decide(self):
        req = urllib.request.Request(self.base + '/api/decide', data=b'{"id":"x","allow":true}',
                headers={'Content-Type': 'application/json', 'X-Agent-Team-UI': '1'})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 403)

    def test_private_files_not_served(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.client.open(self.base + '/data/config.json')
        self.assertEqual(ctx.exception.code, 404)

    def test_wait_api_requires_session(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base+'/api/decision-wait-stats')
        self.assertEqual(ctx.exception.code,403)
        data=json.load(self.client.open(self.base+'/api/decision-wait-stats'))
        self.assertIn('rows',data)

    def test_notification_settings(self):
        result=self.post('/api/notifications',{'windows_enabled':False})
        self.assertFalse(result['windows_enabled'])
        self.assertTrue(result['business_hours_only'])

    def test_wait_period_validation(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.client.open(self.base+'/api/decision-wait-stats?from=2&to=1')
        self.assertEqual(ctx.exception.code,400)


if __name__ == '__main__':
    unittest.main(verbosity=2)
