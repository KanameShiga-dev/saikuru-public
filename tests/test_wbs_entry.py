"""Normal intake entry for the WBS workflow (M01-I): V01 / V10 / U1 registration checks.

Synthetic data only. Requests go through the real /api/jobs handler; no provider starts.
"""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from eval.development_readiness import reacceptance_fixture as fixture
from eval.development_readiness.reacceptance_fixture import make_fixture
from server import Handler

ROOT = Path(__file__).resolve().parent.parent
INITIAL = {'workflow_contract_version': 1, 'requirement_revision': 0, 'requirements_approval_id': None,
           'plan_revision': 0, 'plan_approval_id': None}


class WbsEntryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='wbs-entry-')
        self.addCleanup(temp.cleanup)
        self.server = make_fixture(Path(temp.name))
        self.addCleanup(lambda: (self.server.server_close(), self.server.app.engine.handoff.close(),
                                 self.server.app.store.db.close(), self.server.app.data_lock.close()))
        app = self.server.app
        app.attachments = SimpleNamespace(select=lambda ids: [])
        self.root = (Path(temp.name) / 'project').resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'SCOPE.md').write_text('# Synthetic scope\n', encoding='utf-8')
        app.engine.config['approved_roots'] = app.config['approved_roots'] = [str(self.root)]
        self.store = app.store
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), thread.join(timeout=3)))
        patcher = patch.object(fixture.FixtureHandler, 'do_POST', Handler.do_POST)
        patcher.start()
        self.addCleanup(patcher.stop)

    def enable(self, value=True):
        self.server.app.engine.config['workflow_contract_enabled'] = value

    def register(self, **extra):
        body = {'consent': True, 'title': '架空のWBS依頼', 'goal': '合成の依頼を登録する。',
                'project': str(self.root), 'auto_execute': False, 'attachment_ids': [], 'skill_mode': 'auto', **extra}
        request = Request(self.server.app.origin + '/api/jobs', data=json.dumps(body, ensure_ascii=False).encode(),
                          headers={'Content-Type': 'application/json', 'X-Agent-Team-UI': '1',
                                   'Origin': self.server.app.origin,
                                   'Cookie': 'agent_team=' + self.server.app.cookie}, method='POST')
        try:
            with urlopen(request, timeout=5) as response:
                return response.status
        except HTTPError as error:
            error.close()
            return error.code

    def jobs(self):
        # The fixture seeds one finished synthetic job; only jobs registered by these tests count.
        return [j for j in self.store.all('job') if j['id'] != 'synthetic-reacceptance']

    def test_v01_u1_selected_wbs_job_has_initial_state_in_its_first_save(self):
        self.enable()
        real_put = self.store.put
        with patch.object(self.store, 'put', wraps=real_put) as put:
            self.assertEqual(self.register(workflow='wbs'), 200)
        first = next(c.args[1] for c in put.call_args_list if c.args[0] == 'job')
        self.assertEqual({k: first.get(k) for k in INITIAL}, INITIAL)
        job = self.jobs()[0]
        self.assertEqual({k: job.get(k) for k in INITIAL}, INITIAL)
        self.assertEqual((job['status'], job['g2_status'], job['route']), ('awaiting_approval', 'pending', 'full'))
        self.assertEqual(job['p0_contract_version'], 1)
        pending = [a['kind'] for a in self.store.for_job('approval', job['id']) if a['status'] == 'pending']
        self.assertEqual(pending, ['ai_start'])
        launched = []
        self.server.app.engine._run = launched.append
        self.server.app.engine.paused = False
        for _ in range(3):
            self.server.app.engine.tick()
        self.assertEqual(launched, [])

    def test_v10_default_standard_and_disabled_config_keep_legacy_jobs(self):
        for enabled in (False, True):
            self.enable(enabled)
            self.assertEqual(self.register(), 200)
            self.assertEqual(self.register(workflow='standard'), 200)
        self.assertEqual(len(self.jobs()), 4)
        for job in self.jobs():
            self.assertNotIn('workflow_contract_version', job)
            self.assertEqual(job['p0_contract_version'], 1)

    def test_selecting_wbs_while_disabled_is_rejected_without_a_job(self):
        self.enable(False)
        self.assertEqual(self.register(workflow='wbs'), 400)
        self.assertEqual(self.jobs(), [])

    def test_invalid_selection_is_rejected_without_fallback(self):
        self.enable()
        for value in ('WBS', 'wbs ', '', 1, True, None, ['wbs']):
            with self.subTest(value=value):
                self.assertEqual(self.register(workflow=value), 400)
        self.assertEqual(self.jobs(), [])

    def test_v10_documentation_p0_off_and_light_route_are_not_wbs_targets(self):
        self.enable()
        self.assertEqual(self.register(workflow='wbs', kind='documentation', document_format='md'), 400)
        self.server.app.engine.config['p0_contract_enabled'] = False
        self.assertEqual(self.register(workflow='wbs'), 400)
        self.server.app.engine.config['p0_contract_enabled'] = True
        self.assertEqual(self.register(workflow='wbs', route='light_eval'), 400)
        self.assertEqual(self.jobs(), [])

    def test_u1_intake_screen_offers_the_choice_and_sends_it_only_for_development(self):
        html = (ROOT / 'web' / 'index.html').read_text(encoding='utf-8')
        script = (ROOT / 'web' / 'app.js').read_text(encoding='utf-8')
        self.assertIn('name="workflow"', html)
        self.assertIn('<option value="standard">', html)
        self.assertIn('<option value="wbs">機能ごとに分けて進める（WBS）</option>', html)
        self.assertIn("f.get('workflow')==='wbs'?{workflow:'wbs'}", script)
        self.assertIn("docs?{kind:'documentation',document_format:f.get('document_format')}:f.get", script)
        self.assertIn("$('job-workflow-box').hidden=$('job-workflow-hint').hidden=docs", script)


if __name__ == '__main__':
    unittest.main()
