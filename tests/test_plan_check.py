"""M02-I: workflow plan normalization and the bounded planner repair loop.

Synthetic data only. The real planner path (Engine._run -> _validate -> _finish) runs with a fake provider;
no CLI or AI provider is started.
"""
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request, urlopen

from eval.development_readiness import reacceptance_fixture as fixture
from eval.development_readiness.reacceptance_fixture import make_fixture
from server import Handler
from team_store import Store
from team_plan_check import MAX_REPAIRS, repair_note, workflow_plan_error

D1, D2 = '架空の成果物1', '架空の成果物2'
A1, A2 = '架空の受入条件1', '架空の受入条件2'


def plan(links=(D1, D2, A1, A2), task_links=None):
    links = list(links)
    return {'summary': '架空の計画', 'questions': [], 'context_updates': [], 'warnings': [],
            'security_review_required': False, 'checkpoint_paths': ['output.txt'],
            'code_test_plan': {'test_methods': ['unit'], 'preconditions': ['架空の実行環境と .local/test-tmp'],
                               'setup_steps': ['架空のデータを準備する'], 'synthetic_accounts': [], 'ui_test_required': False,
                               'ui_test_tool': 'not_applicable', 'ui_test_reason': '', 'ui_test_command': '',
                               'cleanup_steps': ['架空のデータを消す'],
                               'required': True, 'reason': '架空の試験', 'cases': ['架空の条件'],
                               'command': 'python -I run_fictional_tests.py', 'existing_tests': []},
            'goal_contract': {'primary_goal': '架空の目的', 'deliverables': [D1, D2],
                              'acceptance_criteria': [A1, A2], 'non_goals': [], 'constraints': []},
            'work_packages': [{'wbs_id': 'WP1', 'title': '架空の機能', 'goal_links': links,
                               'done_when': ['架空の完了条件'], 'depends_on': [], 'input_artifacts': []}],
            'tasks': [{'title': '架空の作業', 'instruction': '架空の指示', 'role': 'builder',
                       'goal_links': list(task_links or links), 'wbs_id': 'WP1'}]}


class PlanRepairLoopTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='plan-check-')
        self.addCleanup(temp.cleanup)
        self.server = make_fixture(Path(temp.name))
        self.addCleanup(lambda: (self.server.server_close(), self.server.app.engine.handoff.close(),
                                 self.server.app.store.db.close(), self.server.app.data_lock.close()))
        app = self.server.app
        app.attachments = SimpleNamespace(select=lambda ids: [])
        # A WBS job is accepted only in a formal Worktree, so a fictional one is built (skips without the tools).
        from tests.test_workspace_boundary import WorkspaceTests
        workspace = WorkspaceTests()
        self.addCleanup(workspace.doCleanups)
        workspace.setUp()
        root = workspace.project.resolve()
        (root / 'SCOPE.md').write_text('# Synthetic scope\n', encoding='utf-8')
        workspace.git(root, 'add', '--', 'SCOPE.md')
        workspace.git(root, 'commit', '-m', 'Fictional scope')
        (root / '.local' / 'test-tmp').mkdir(parents=True)  # the Claude builder's test temp (empty, so Git ignores it)
        app.engine.config['approved_roots'] = app.config['approved_roots'] = [str(root)]
        app.engine.config['workflow_contract_enabled'] = True
        self.store, self.engine = app.store, app.engine
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), thread.join(timeout=3)))
        patcher = patch.object(fixture.FixtureHandler, 'do_POST', Handler.do_POST)
        patcher.start()
        self.addCleanup(patcher.stop)
        body = {'consent': True, 'title': '架空のWBS依頼', 'goal': '合成の依頼を登録する。', 'project': str(root),
                'auto_execute': False, 'attachment_ids': [], 'skill_mode': 'auto', 'workflow': 'wbs'}
        request = Request(app.origin + '/api/jobs', data=json.dumps(body, ensure_ascii=False).encode(),
                          headers={'Content-Type': 'application/json', 'X-Agent-Team-UI': '1', 'Origin': app.origin,
                                   'Cookie': 'agent_team=' + app.cookie}, method='POST')
        with urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
        self.job_id = next(j['id'] for j in self.store.all('job') if j['id'] != 'synthetic-reacceptance')
        start = next(a for a in self.store.for_job('approval', self.job_id) if a['kind'] == 'ai_start')
        self.engine.decide(start['id'], True, '架空の開始承認')
        self.planner_id = next(t['id'] for t in self.store.for_job('task', self.job_id) if t['role'] == 'planner')
        self.prompts = []

    def run_planner(self, result):
        """One provider call through the real planner path; returns the task as stored afterwards.

        A regeneration needs a fresh AI-start approval (existing G2 rule), so after the first call the
        scheduler tick asks for it and the test user approves it before the next provider call.
        """
        if self.prompts:
            self.engine.paused = False
            with patch.object(self.engine, '_run', lambda task: None):
                self.engine.tick()
            pending = [a for a in self.store.for_job('approval', self.job_id)
                       if a['kind'] == 'ai_start' and a['status'] == 'pending']
            self.assertEqual(len(pending), 1)
            self.engine.decide(pending[0]['id'], True, '架空の再生成の承認')
        current = self.store.get(self.planner_id, 'task')
        task = self.store.reserve_attempt(current['id'], current['state_revision'])
        self.engine.commands[task['profile']['adapter']] = ['fictional-never-executed-cli']
        prompts = self.prompts

        class FakeProvider:
            def run(adapter, ctx, prompt, schema):
                prompts.append(prompt)
                return copy.deepcopy(result)

        with patch.dict('team_engine.ADAPTERS', {task['profile']['adapter']: FakeProvider}), \
                patch('team_local_summary.review_plan', lambda *a, **k: {'status': 'skipped'}):
            self.engine._run(task)
        return self.store.get(self.planner_id, 'task')

    def job(self):
        return self.store.get(self.job_id, 'job')

    def plan_approvals(self):
        return [a for a in self.store.for_job('approval', self.job_id) if a['kind'] == 'plan']

    def test_v02_numbered_links_become_contract_text_in_result_and_approval(self):
        done = self.run_planner(plan(links=('D1', 'D2', 'A1', 'A2'), task_links=('D1', 'A1')))
        self.assertEqual(done['status'], 'succeeded', done.get('summary'))
        approval, = self.plan_approvals()
        wp = approval['payload']['work_packages'][0]
        self.assertEqual(wp['goal_links'], [D1, D2, A1, A2])
        self.assertEqual(approval['payload']['tasks'][0]['goal_links'], [D1, A1])
        self.assertEqual(done['result'], approval['payload'])
        self.assertEqual(self.job()['status'], 'awaiting_approval')

    def test_v02_paraphrase_is_sent_back_then_a_corrected_plan_reaches_approval(self):
        done = self.run_planner(plan(links=(D1, D2, A1, A2), task_links=(D1, '受入条件を言い換えたもの')))
        self.assertEqual((done['status'], done['plan_check_repairs']), ('queued', 1))
        self.assertEqual(self.job()['status'], 'planning')
        self.assertEqual(self.plan_approvals(), [])
        done = self.run_planner(plan())
        self.assertEqual(done['status'], 'succeeded', done.get('summary'))
        self.assertTrue('不備：作業1に成果物・受入条件へ対応しない作業があります' in self.prompts[1])
        self.assertEqual(len(self.plan_approvals()), 1)
        self.assertEqual(self.job()['status'], 'awaiting_approval')

    def test_v02_unknown_or_ambiguous_numbers_and_missing_coverage_are_repair_targets(self):
        for links in (('D1', 'D2', 'A1', 'A9'), (D1, D2, A1, 'a2'), (D1, A1, A2)):
            with self.subTest(links=links):
                job = {'workflow_contract_version': 1, 'request_origin': 'new', 'route': 'full',
                       'document_source': None, 'p0_contract_version': 1, 'goal': '架空'}
                self.assertTrue(workflow_plan_error(job, plan(links=links)))

    def test_v02_after_two_repairs_the_third_failure_waits_for_the_user_not_failed(self):
        bad = plan(links=(D1, D2, A1))  # A2 is never covered
        for expected in range(1, MAX_REPAIRS + 1):
            done = self.run_planner(bad)
            self.assertEqual((done['status'], done['plan_check_repairs']), ('queued', expected))
            self.assertEqual(self.job()['status'], 'planning')
        done = self.run_planner(bad)
        self.assertEqual(len(self.prompts), MAX_REPAIRS + 1)
        self.assertEqual((done['status'], done['plan_check_repairs']), ('blocked', MAX_REPAIRS))
        self.assertIn('work_package_goal_coverage_missing', done['summary'])
        self.assertEqual(self.job()['status'], 'blocked')
        self.assertEqual(self.plan_approvals(), [])

    def test_v02_repair_count_is_kept_in_the_stored_task(self):
        self.run_planner(plan(links=(D1, D2, A1)))
        # Restart: close the Store and open a new one on the same files. The counter must come from disk.
        directory = self.store.directory
        self.store.db.close()
        reopened = Store(directory)
        self.addCleanup(reopened.db.close)
        self.assertIsNot(reopened, self.store)
        self.assertEqual(reopened.get(self.planner_id, 'task')['plan_check_repairs'], 1)
        # The runner continues on the reopened Store: the next failure counts 2, the one after is the third.
        self.store = self.server.app.store = self.engine.store = reopened
        done = self.run_planner(plan(links=(D1, D2, A1)))
        self.assertEqual((done['status'], done['plan_check_repairs']), ('queued', 2))
        done = self.run_planner(plan(links=(D1, D2, A1)))
        self.assertEqual((done['status'], done['plan_check_repairs']), ('blocked', MAX_REPAIRS))

    def test_repair_note_for_plan_errors_names_the_reason_and_keeps_tool_text_out(self):
        note = repair_note([], False, 'work_package_goal_label_unknown')
        self.assertIn('work_package_goal_label_unknown', note)
        self.assertNotIn('読み取り専用', note)


if __name__ == '__main__':
    unittest.main()
