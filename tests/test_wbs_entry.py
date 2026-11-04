"""Normal intake entry for the WBS workflow (M01-I): V01 / V10 / U1 registration checks.

Synthetic data only. Requests go through the real /api/jobs handler; no provider starts.
"""
import copy
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import team_engine
from eval.development_readiness import reacceptance_fixture as fixture
from eval.development_readiness.reacceptance_fixture import make_fixture
from server import Handler

ROOT = Path(__file__).resolve().parent.parent
INITIAL = {'workflow_contract_version': 1, 'requirement_revision': 0, 'requirements_approval_id': None,
           'plan_revision': 0, 'plan_approval_id': None}


class WbsFixture(unittest.TestCase):
    """Real HTTP handler and engine on a temporary store; subclasses choose the project folder."""

    def make_root(self, base):
        root = (base / 'project').resolve()
        root.mkdir(parents=True, exist_ok=True)
        (root / 'SCOPE.md').write_text('# Synthetic scope\n', encoding='utf-8')
        return root

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='wbs-entry-')
        self.addCleanup(temp.cleanup)
        self.server = make_fixture(Path(temp.name))
        self.addCleanup(lambda: (self.server.server_close(), self.server.app.engine.handoff.close(),
                                 self.server.app.store.db.close(), self.server.app.data_lock.close()))
        app = self.server.app
        app.attachments = SimpleNamespace(select=lambda ids: [])
        self.root = self.make_root(Path(temp.name))
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

    def post(self, path, body):
        request = Request(self.server.app.origin + path, data=json.dumps(body, ensure_ascii=False).encode(),
                          headers={'Content-Type': 'application/json', 'X-Agent-Team-UI': '1',
                                   'Origin': self.server.app.origin,
                                   'Cookie': 'agent_team=' + self.server.app.cookie}, method='POST')
        try:
            with urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def register(self, **extra):
        body = {'consent': True, 'title': '架空のWBS依頼', 'goal': '合成の依頼を登録する。',
                'project': str(self.root), 'auto_execute': False, 'attachment_ids': [], 'skill_mode': 'auto', **extra}
        return self.post('/api/jobs', body)[0]

    def jobs(self):
        # The fixture seeds one finished synthetic job; only jobs registered by these tests count.
        return [j for j in self.store.all('job') if j['id'] != 'synthetic-reacceptance']


class WbsEntryTests(WbsFixture):
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

    def test_v01_registered_wbs_job_reaches_a_wbs_plan_through_the_real_planner_with_a_fake_provider(self):
        self.enable()
        self.assertEqual(self.register(workflow='wbs'), 200)
        engine, job = self.server.app.engine, self.jobs()[0]
        start = next(a for a in self.store.for_job('approval', job['id']) if a['kind'] == 'ai_start')
        engine.decide(start['id'], True, '架空の開始承認')
        self.assertEqual(self.store.get(job['id'], 'job')['status'], 'planning')
        planner = next(t for t in self.store.for_job('task', job['id']) if t['role'] == 'planner')
        task = self.store.reserve_attempt(planner['id'], planner['state_revision'])
        engine.commands[task['profile']['adapter']] = ['fictional-never-executed-cli']
        deliverable, criterion = '架空の成果物', '架空の受入条件'
        plan = {'summary': '架空の計画', 'questions': [], 'context_updates': [], 'warnings': [],
                'security_review_required': False, 'checkpoint_paths': ['output.txt'],
                'code_test_plan': {'required': True, 'reason': '架空の試験', 'cases': ['架空の条件'],
                                   'command': 'python -I run_fictional_tests.py', 'existing_tests': []},
                'goal_contract': {'primary_goal': '架空の目的', 'deliverables': [deliverable],
                                  'acceptance_criteria': [criterion], 'non_goals': [], 'constraints': []},
                'work_packages': [{'wbs_id': 'WP1', 'title': '架空の機能', 'goal_links': [deliverable, criterion],
                                   'done_when': ['架空の完了条件'], 'depends_on': [], 'input_artifacts': []}],
                'tasks': [{'title': '架空の作業', 'instruction': '架空の指示', 'role': 'builder',
                           'goal_links': [deliverable, criterion], 'wbs_id': 'WP1'}]}
        seen = []

        class FakeProvider:
            def run(adapter, ctx, prompt, schema):
                seen.append((prompt, schema))
                return plan

        with patch.dict('team_engine.ADAPTERS', {task['profile']['adapter']: FakeProvider}), \
                patch('team_local_summary.review_plan', lambda *a, **k: {'status': 'skipped'}):
            engine._run(task)
        self.assertEqual(len(seen), 1)
        self.assertIn('work_packages', seen[0][1]['properties'])
        self.assertIn('work_packages', seen[0][0])
        done = self.store.get(task['id'], 'task')
        self.assertEqual(done['status'], 'succeeded', done.get('summary'))
        after = self.store.get(job['id'], 'job')
        self.assertEqual(after['workflow_contract_version'], 1)
        self.assertEqual(after['g2_status'], 'consumed')
        approval = next(a for a in self.store.for_job('approval', job['id']) if a['kind'] == 'plan')
        self.assertEqual(approval['status'], 'pending')
        self.assertEqual([p['wbs_id'] for p in approval['payload']['work_packages']], ['WP1'])

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


CHECK = 'python -B -m unittest discover -s tests'
DELIVERABLES = ['架空の一覧・検索', '架空の詳細・編集']
CRITERIA = ['架空の検索が大文字小文字を問わない', '架空の編集が詳細に反映される']
REJECTION = '架空の欠陥：検索が大文字小文字を区別している'
FILES = {
    'W1.01': {'catalog.py': ("ITEMS = {'1': 'Apple fictional', '2': 'banana fictional'}\n\n\n"
                             "def listing():\n    return sorted(ITEMS)\n\n\n"
                             "def search(term):\n    return sorted(k for k, v in ITEMS.items() if term in v)\n"),
              'tests/test_catalog.py': ("import unittest\n\nimport catalog\n\n\nclass CatalogTest(unittest.TestCase):\n"
                                        "    def test_listing_and_exact_search(self):\n"
                                        "        self.assertEqual(catalog.listing(), ['1', '2'])\n"
                                        "        self.assertEqual(catalog.search('Apple'), ['1'])\n")},
    'W1.02': {'detail.py': ("import catalog\n\n\ndef show(item_id):\n    return catalog.ITEMS[item_id]\n\n\n"
                            "def edit(item_id, text):\n    catalog.ITEMS[item_id] = text\n    return show(item_id)\n"),
              'tests/test_detail.py': ("import unittest\n\nimport catalog\nimport detail\n\n\nclass DetailTest(unittest.TestCase):\n"
                                       "    def test_edit_is_reflected_in_detail(self):\n"
                                       "        before = detail.show('2')\n"
                                       "        self.addCleanup(detail.edit, '2', before)\n"
                                       "        self.assertEqual(detail.edit('2', 'Edited fictional'), 'Edited fictional')\n"
                                       "        self.assertEqual(catalog.ITEMS['2'], 'Edited fictional')\n")}}
REPAIRED = {'catalog.py': FILES['W1.01']['catalog.py'].replace('term in v', 'term.lower() in v.lower()'),
            'tests/test_catalog.py': FILES['W1.01']['tests/test_catalog.py'] +
                                     "\n    def test_search_ignores_case(self):\n        self.assertEqual(catalog.search('apple'), ['1'])\n"}
PLAN = {'summary': '架空の二機能計画', 'questions': [], 'context_updates': [], 'warnings': [],
        'security_review_required': False,
        'checkpoint_paths': ['catalog.py', 'detail.py', 'tests/test_catalog.py', 'tests/test_detail.py'],
        'code_test_plan': {'required': True, 'reason': '架空の試験', 'cases': ['一覧・検索', '詳細・編集'],
                           'command': CHECK, 'existing_tests': []},
        'goal_contract': {'primary_goal': '架空の一覧・検索と詳細・編集', 'deliverables': DELIVERABLES,
                          'acceptance_criteria': CRITERIA, 'non_goals': [], 'constraints': ['架空のデータだけを使う']},
        'work_packages': [
            {'wbs_id': 'W1.01', 'title': '一覧・検索', 'goal_links': [DELIVERABLES[0], CRITERIA[0]],
             'done_when': ['一覧と検索が動く'], 'depends_on': [], 'input_artifacts': []},
            {'wbs_id': 'W1.02', 'title': '詳細・編集', 'goal_links': [DELIVERABLES[1], CRITERIA[1]],
             'done_when': ['詳細表示と編集が動く'], 'depends_on': ['W1.01'], 'input_artifacts': []}],
        'tasks': [
            {'title': '一覧・検索を作る', 'instruction': '一覧と検索を作り、試験を書いて実行する。', 'role': 'builder',
             'goal_links': [DELIVERABLES[0], CRITERIA[0]], 'wbs_id': 'W1.01'},
            {'title': '詳細・編集を作る', 'instruction': '詳細表示と編集を作り、試験を書いて実行する。', 'role': 'builder',
             'goal_links': [DELIVERABLES[1], CRITERIA[1]], 'wbs_id': 'W1.02'}]}


class WbsNormalPathTests(WbsFixture):
    """M03-I: two dependent features from /api/jobs to a simulated receipt (V01 V03 V04 V11, U1-U8).

    Only the provider's answers and the person's clicks are simulated. Planner, scheduler (tick), worker (_run),
    plan validation, local test gate, reviews, feature decisions and acceptance are the product code.
    """

    def make_root(self, base):
        from tests.test_workspace_boundary import WorkspaceTests
        self.workspace = WorkspaceTests()
        self.addCleanup(self.workspace.doCleanups)
        self.workspace.setUp()  # skips when the formal worktree tools are not installed
        root = self.workspace.project.resolve()
        (root / 'SCOPE.md').write_text('# Synthetic scope\n', encoding='utf-8')
        # Files outside the approved scope must not be dirty, so the scope file is committed in the fictional repository.
        self.workspace.git(root, 'add', '--', 'SCOPE.md')
        self.workspace.git(root, 'commit', '-m', 'Fictional scope')
        return root

    def setUp(self):
        super().setUp()
        self.enable()
        engine = self.server.app.engine
        engine.paused = False
        self.runs = []  # (wbs_id, role, kind) of each provider call, in dispatch order
        self.prompts = {}
        self.dispatched = []
        test = self

        class Provider:
            def run(adapter, ctx, prompt, schema):
                return test.answer(ctx, prompt, schema)

        for name in team_engine.ADAPTERS:
            engine.commands[name] = ['fictional-never-executed-cli']
        patcher = patch.dict('team_engine.ADAPTERS', {name: Provider for name in team_engine.ADAPTERS})
        patcher.start()
        self.addCleanup(patcher.stop)
        summary = patch('team_local_summary.review_plan', lambda *a, **k: {'status': 'skipped'})
        summary.start()
        self.addCleanup(summary.stop)

    # --- simulated provider -------------------------------------------------------------------------------------
    def answer(self, ctx, prompt, schema):
        task = self.store.get(ctx.task['id'], 'task')
        wbs = self.store.get(task['work_package_id'], 'work_package')['wbs_id'] if task.get('work_package_id') else None
        kind = (task.get('workflow_authority') or {}).get('kind')
        self.runs.append((wbs, task['role'], kind))
        self.prompts[task['id']] = prompt
        if task['role'] == 'planner':
            return copy.deepcopy(PLAN)
        report = {'status': 'done', 'summary': '架空の報告', 'checks': ['架空の確認'], 'pending_checks': [],
                  'question': '', 'questions': [], 'context_updates': [], 'warnings': [], 'stage_complete': True,
                  'blocking_reasons': []}
        if task['role'] == 'builder':
            project = Path(ctx.project)
            # The simulated assignee can only do what its own task instruction asks (the prompt also carries the whole
            # plan as context): build when the planned build instruction is in it, fix when it is a repair, otherwise
            # only run the checks.
            planned = next((t['instruction'] for t in PLAN['tasks'] if t['wbs_id'] == wbs), None)
            files = REPAIRED if kind == 'package_repair' else FILES[wbs] if planned and planned in task['instruction'] else {}
            for name, text in files.items():
                (project / name).parent.mkdir(parents=True, exist_ok=True)
                (project / name).write_text(text, encoding='utf-8')
            ctx.command_started(CHECK)
            ran = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests'], cwd=project,
                                 capture_output=True, text=True, timeout=60)
            ctx.command_result(CHECK, ran.returncode, ran.stdout + ran.stderr)
            tests = sorted(p.relative_to(project).as_posix() for p in (project / 'tests').glob('test_*.py'))
            report.update(summary=f'架空の作業完了（試験終了コード{ran.returncode}）',
                          checks=[f'{CHECK} の終了コードは{ran.returncode}'],
                          artifacts=[n for n in files if not n.startswith('tests/')], test_files=tests,
                          test_change_reason='架空の試験を追加・更新した')
            if ran.returncode:
                report.update(status='needs_changes', stage_complete=False)
        else:
            report.update(summary='架空の独立確認：完了条件と記録を読み取り専用で確認した')
        return {key: report[key] for key in schema['properties']}

    # --- ordinary operations ------------------------------------------------------------------------------------
    def drain(self, limit=40):
        """Run the real scheduler until it starts nothing; each started task runs through the real worker."""
        engine = self.server.app.engine
        for _ in range(limit):
            started = []
            with patch.object(engine, '_start_task_thread', started.append):
                engine.tick()
            if not started:
                return
            for task in started:
                self.dispatched.append(task['id'])
                engine._run(task)
        self.fail('scheduler kept starting tasks\n' + self.dump())

    def dump(self):
        job = self.jobs()[0]
        rows = [f"{t['status']:10} {t['role']:9} {t.get('agent_name') or '-':24} "
                f"{(t.get('workflow_authority') or {}).get('kind') or '-':15} {t['title']}"
                for t in self.store.for_job('task', job['id'])]
        view = self.server.app.engine.workflow_dispatch_view(job['id'])
        space = self.server.app.engine.workspace_view(job['id']) if job.get('plan_approval_id') else {}
        return (f"job={job['status']} dispatch={view.get('status')} {view.get('reasons')} workspace={space.get('status')} "
                f"{space.get('reason')} {(space.get('current') or {}).get('reason')}\n" + '\n'.join(rows))

    def ok(self, path, body):
        status, result = self.post(path, body)
        if status != 200:
            self.fail(f'{path} {status} {result}\n{self.dump()}')
        return result

    def decide(self, approval, allow, note='', **extra):
        return self.ok('/api/decide', {'id': approval['id'], 'allow': allow, 'note': note, **extra})

    def approval(self, kind, status='pending'):
        found = [a for a in self.store.for_job('approval', self.jobs()[0]['id']) if a['kind'] == kind and a['status'] == status]
        if len(found) != 1:
            self.fail(f'{kind}/{status}: {len(found)}\n{self.dump()}')
        return found[0]

    def packages(self):
        job = self.jobs()[0]
        return {w['wbs_id']: w for w in self.store.for_job('work_package', job['id']) if w['plan_revision'] == job['plan_revision']}

    def feature_decision(self, wbs, allow, note=''):
        wp = self.packages()[wbs]
        job = self.jobs()[0]
        prepared = self.ok('/api/workflow/feature-review/prepare', {
            'job_id': job['id'], 'work_package_id': wp['id'], 'work_package_revision': wp['state_revision'],
            'action': 'request_feature_review'})
        return self.ok('/api/workflow/feature-review/decide', {
            'approval_id': prepared['approval_id'], 'allow': allow, 'note': note, 'action': 'decide_feature_review',
            'confirmation': {'action': 'feature_review_confirmation', 'target_digest': prepared['target_digest'],
                             'decision': 'approved' if allow else 'rejected'}})

    def test_two_dependent_features_run_from_normal_registration_to_simulated_receipt(self):
        # 1. Registration and start approval through the ordinary API (V01, U1).
        self.assertEqual(self.register(workflow='wbs'), 200)
        job = self.jobs()[0]
        self.assertEqual(job['workflow_contract_version'], 1)
        self.drain()
        self.assertEqual(self.runs, [], 'nothing may run before the start approval')
        self.decide(self.approval('ai_start'), True, '架空の開始承認')
        self.assertEqual(self.store.get(job['id'], 'job')['status'], 'planning')

        # 2. Real planner path with a simulated provider; plan approval through the ordinary API with its digest.
        self.drain()
        self.assertEqual(self.runs, [(None, 'planner', None)])
        plan = self.approval('plan')
        self.assertEqual([p['wbs_id'] for p in plan['payload']['work_packages']], ['W1.01', 'W1.02'])
        self.assertEqual(self.store.get(job['id'], 'job')['status'], 'awaiting_approval')
        self.decide(plan, True, '架空の計画承認', workflow_confirmation=plan['workflow_revision_digest'])
        self.assertEqual(self.store.get(job['id'], 'job')['status'], 'running')
        self.assertEqual({k: v['execution_state'] for k, v in self.packages().items()},
                         {'W1.01': 'planned', 'W1.02': 'planned'})

        # 3. Only the first feature is dispatched; its builder ran the local tests and both reviews passed.
        self.drain()
        self.assertEqual([r[:2] for r in self.runs[1:]],
                         [('W1.01', 'builder'), ('W1.01', 'reviewer'), ('W1.01', 'reviewer')], self.dump())
        self.assertEqual(self.packages()['W1.02']['execution_state'], 'planned')
        self.assertFalse((Path(self.root) / 'detail.py').exists(), 'the dependent feature must not start early')
        first = self.packages()['W1.01']
        self.assertEqual((first['execution_state'], first['review_status']), ('succeeded', 'not_requested'))
        builder = next(t for t in self.store.for_job('task', job['id']) if t['role'] == 'builder' and t.get('work_package_id') == first['id'])
        self.assertEqual(builder['workflow_validation_evidence']['0']['exit_code'], 0)

        # 4. The person rejects the first feature once; the reason reaches the repair task (V03/V04).
        rejected = self.feature_decision('W1.01', False, REJECTION)
        self.assertIn(first['id'], rejected['affected_ids'])
        repair = next(t for t in self.store.for_job('task', job['id']) if (t.get('workflow_authority') or {}).get('kind') == 'package_repair')
        self.assertIn(REJECTION, repair['instruction'])
        self.assertEqual(self.store.get(job['id'], 'job')['status'], 'running', self.dump())
        mark = len(self.runs)
        self.drain()
        self.assertIn(REJECTION, self.prompts[repair['id']])
        self.assertEqual(self.runs[mark][:3], ('W1.01', 'builder', 'package_repair'), self.dump())
        self.assertEqual(self.packages()['W1.02']['execution_state'], 'planned', self.dump())
        self.assertFalse((Path(self.root) / 'detail.py').exists(), 'the dependent feature must wait for the repaired feature\n' + self.dump())
        self.feature_decision('W1.01', True, '架空の再確認')
        self.assertEqual(self.packages()['W1.01']['review_status'], 'approved')

        # 5. Only now the dependent feature is dispatched, through the same path.
        mark = len(self.runs)
        self.drain()
        second = [r for r in self.runs[mark:] if r[0] == 'W1.02']
        self.assertTrue(second, self.dump())
        self.assertTrue((Path(self.root) / 'detail.py').exists(), self.dump())
        self.assertEqual([r[1] for r in second][:3], ['builder', 'reviewer', 'reviewer'], self.dump())
        self.feature_decision('W1.02', True)

        # 6. Job-level check and reviews, then the ordinary review confirmation and formal receipt.
        self.drain()
        self.assertEqual(self.store.get(job['id'], 'job')['status'], 'awaiting_acceptance', self.dump())
        completion = self.approval('completion')
        # Every pending check needs the person's recorded evidence; each record replaces the completion approval.
        while completion['payload'].get('pending_items'):
            recorded = self.ok('/api/completion/evidence', {
                'approval_id': completion['id'], 'item_index': 0, 'check_index': 0, 'confirmed': True,
                'evidence': '架空の利用者が模擬環境で確認した（実画面の目視ではない）'})
            completion = self.store.get(recorded['approval_id'], 'approval')
        self.ok('/api/jobs/review-confirmation', {'approval_id': completion['id'], 'confirmed': True})
        accepted = self.ok('/api/jobs/formal-acceptance', {'approval_id': completion['id'], 'confirmation': '正式受領'})
        self.assertIn(accepted['status'], ('accepted', 'accepted_with_pending_checks'))
        self.assertEqual(self.store.get(job['id'], 'job')['status'], accepted['status'])
        self.assertEqual(self.runs[0][1], 'planner')
        self.assertTrue(all(self.store.get(i, 'task')['status'] == 'succeeded' for i in self.dispatched), self.dump())


class UnbuiltInstructionTests(unittest.TestCase):
    """M03-Q-B1: a feature with several planned builder tasks keeps every unbuilt instruction."""

    PLAN = {'payload': {'tasks': [
        {'wbs_id': 'W1.01', 'role': 'builder', 'instruction': '架空の作成A'},
        {'wbs_id': 'W1.01', 'role': 'builder', 'instruction': '架空の作成B'},
        {'wbs_id': 'W1.01', 'role': 'researcher', 'instruction': '架空の調査'},
        {'wbs_id': 'W1.02', 'role': 'builder', 'instruction': '架空の別機能'}]}}
    JOB = {'id': 'job1'}
    WP = {'id': 'wp1', 'wbs_id': 'W1.01'}

    def call(self, tasks):
        engine = SimpleNamespace(store=SimpleNamespace(for_job=lambda kind, job_id: tasks))
        return team_engine.Engine._unbuilt_instruction(engine, self.JOB, self.PLAN, self.WP)

    @staticmethod
    def task(index, status='succeeded', role='builder', instruction='x', wp='wp1'):
        return {'work_package_id': wp, 'role': role, 'status': status, 'plan_task_index': index, 'instruction': instruction}

    def test_nothing_built_returns_every_planned_builder_instruction(self):
        text = self.call([self.task(0, 'queued'), self.task(1, 'queued')])
        self.assertIn('架空の作成A', text); self.assertIn('架空の作成B', text)
        self.assertNotIn('架空の調査', text); self.assertNotIn('架空の別機能', text)

    def test_one_succeeded_and_one_queued_builder_returns_only_the_remaining_one(self):
        text = self.call([self.task(0), self.task(1, 'queued')])
        self.assertIn('架空の作成B', text); self.assertNotIn('架空の作成A', text)

    def test_all_builders_succeeded_returns_none(self):
        self.assertIsNone(self.call([self.task(0), self.task(1)]))

    def test_other_feature_success_does_not_count(self):
        text = self.call([self.task(0, wp='wp2'), self.task(1, wp='wp2')])
        self.assertIn('架空の作成A', text); self.assertIn('架空の作成B', text)

    def test_succeeded_replacement_task_covers_the_remaining_builders(self):
        first = self.call([self.task(0, 'queued'), self.task(1, 'queued')])
        self.assertIsNone(self.call([self.task(None, instruction=first)]))


if __name__ == '__main__':
    unittest.main()
