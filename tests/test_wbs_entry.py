"""Normal intake entry for the WBS workflow (M01-I): V01 / V10 / U1 registration checks.

Synthetic data only. Requests go through the real /api/jobs handler; no provider starts.
"""
import copy
import json
import shutil
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
        # A WBS job is accepted only in a formal Worktree (M09 live run), so the fixture builds a fictional one.
        from tests.test_workspace_boundary import WorkspaceTests
        self.workspace = WorkspaceTests()
        self.addCleanup(self.workspace.doCleanups)
        self.workspace.setUp()  # skips when the formal worktree tools are not installed
        root = self.workspace.project.resolve()
        (root / 'SCOPE.md').write_text('# Synthetic scope\n', encoding='utf-8')
        # Files outside the approved scope must not be dirty, so the scope file is committed in the fictional repository.
        self.workspace.git(root, 'add', '--', 'SCOPE.md')
        self.workspace.git(root, 'commit', '-m', 'Fictional scope')
        (root / '.local' / 'test-tmp').mkdir(parents=True)  # the Claude builder's test temp (empty, so Git ignores it)
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

    def test_v01_state_screen_shows_a_wbs_job_waiting_for_its_unexpiring_start_approval(self):
        # M09 live run: the ai_start approval has expires_at=None (no expiry) and the state screen failed on it.
        self.enable()
        self.assertEqual(self.register(workflow='wbs'), 200)
        job = self.jobs()[0]
        start = next(a for a in self.store.for_job('approval', job['id']) if a['kind'] == 'ai_start')
        self.assertIsNone(start['expires_at'])
        state = self.server.app.public_state()
        self.assertIn(job['id'], json.dumps(state, ensure_ascii=False))

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
                                   'command': 'python -I run_fictional_tests.py', 'existing_tests': [],
                                   'test_methods': ['unit'], 'preconditions': ['架空の実行環境と .local/test-tmp'],
                                   'setup_steps': ['架空のデータを準備する'], 'synthetic_accounts': [], 'ui_test_required': False,
                                   'ui_test_tool': 'not_applicable', 'ui_test_reason': '', 'ui_test_command': '',
                                   'cleanup_steps': ['架空のデータを消す']},
                'goal_contract': {'primary_goal': '架空の目的', 'deliverables': [deliverable],
                                  'acceptance_criteria': [criterion], 'non_goals': [], 'constraints': []},
                'work_packages': [{'wbs_id': 'WP1', 'title': '架空の機能', 'goal_links': [deliverable, criterion],
                                   'done_when': ['架空の完了条件'], 'depends_on': [], 'input_artifacts': [],
                                   'owned_paths': ['output.txt']}],
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

    def test_wbs_job_tasks_get_the_wbs_command_rule(self):
        # M09 live run: the WBS rule omits the COMMAND_ALLOWLIST.md record, a change outside the approved files.
        from team_engine import Context
        self.enable()
        self.assertEqual(self.register(workflow='wbs'), 200)
        planner = next(t for t in self.store.for_job('task', self.jobs()[0]['id']) if t['role'] == 'planner')
        engine = self.server.app.engine
        engine.commands[planner['profile']['adapter']] = ['fictional-never-executed-cli']
        self.assertTrue(Context(engine, planner).workflow_contract)

    def test_wbs_in_a_folder_that_is_not_a_formal_worktree_is_rejected_with_the_reason(self):
        # M09 live run: a plain folder was accepted, the plan was made, and the job then stalled without the reason.
        self.enable()
        plain = self.root.parent.parent.parent / 'plain-project'
        plain.mkdir()
        (plain / 'SCOPE.md').write_text('# Synthetic scope\n', encoding='utf-8')
        self.server.app.engine.config['approved_roots'].append(str(plain))
        status, result = self.post('/api/jobs', {'consent': True, 'title': '架空のWBS依頼', 'goal': '合成の依頼を登録する。',
                                                 'project': str(plain), 'auto_execute': False, 'attachment_ids': [],
                                                 'skill_mode': 'auto', 'workflow': 'wbs'})
        self.assertEqual(status, 400)
        self.assertIn('workspace_formal_worktree_required', json.dumps(result, ensure_ascii=False))
        self.assertEqual(self.jobs(), [])
        self.assertEqual(self.register(workflow='wbs'), 200)  # the formal Worktree is still accepted

    def test_a_new_empty_folder_needs_no_preparation_and_is_made_ready_by_the_start_step(self):
        self.enable()
        engine = self.server.app.engine
        new = self.root.parent.parent.parent / 'new-project'
        new.mkdir()
        engine.config['approved_roots'].append(str(new))
        self.assertEqual(engine.wbs_entry_problems(str(new)), [])
        job = engine.create_job('架空のWBS依頼', '合成の依頼を登録する。', str(new), False, workflow='wbs')
        start = next(a for a in self.store.for_job('approval', job['id']) if a['kind'] == 'ai_start')
        engine.prepare_plan_start(start['id'], start['payload']['fingerprint'], True)
        self.assertTrue((new / '.git').is_dir())
        self.assertTrue((new / '.local' / 'test-tmp').is_dir())
        from team_harness import execution_workspace
        boundary = execution_workspace(str(new), ['memo.py'])
        self.assertEqual((boundary['status'], boundary['workspace_layout'], boundary['dirty_changes']),
                         ('ready', 'project_repository', []))

    def test_with_the_p0_switch_off_only_a_wbs_request_carries_the_contract(self):
        # The production settings: the switch stays off for ordinary requests.
        self.enable()
        self.server.app.engine.config['p0_contract_enabled'] = False
        self.assertEqual(self.register(), 200)
        ordinary = self.jobs()[0]
        self.assertNotIn('p0_contract_version', ordinary)
        self.server.app.engine.cancel_job(ordinary['id'])
        self.assertEqual(self.register(workflow='wbs'), 200)
        selected = next(j for j in self.jobs() if j['id'] != ordinary['id'])
        self.assertEqual(selected['p0_contract_version'], 1)

    def test_wbs_without_the_builder_test_temp_folder_is_rejected_with_the_reason(self):
        # M09 live run: the plan was made, then the Claude builder stopped before its call for want of .local/test-tmp.
        self.enable()
        (self.root / '.local' / 'test-tmp').rmdir()
        config = self.server.app.engine.config
        builder = config['profiles'][config['roles']['builder']]
        self.assertEqual(builder['adapter'], 'claude')
        status, result = self.post('/api/jobs', {'consent': True, 'title': '架空のWBS依頼', 'goal': '合成の依頼を登録する。',
                                                 'project': str(self.root), 'auto_execute': False, 'attachment_ids': [],
                                                 'skill_mode': 'auto', 'workflow': 'wbs'})
        self.assertEqual(status, 400)
        self.assertIn('.local/test-tmp', json.dumps(result, ensure_ascii=False))
        self.assertEqual(self.jobs(), [])

    def test_wbs_with_a_non_claude_builder_does_not_need_the_claude_test_temp_folder(self):
        # M09-Q review #29: only the Claude builder uses .local/test-tmp, so another builder must not be refused.
        self.enable()
        (self.root / '.local' / 'test-tmp').rmdir()
        engine = self.server.app.engine
        settings = dict(engine.config.get('provider_settings') or {}, enabled=['codex', 'claude'])
        patcher = patch.dict(engine.config, {'provider_settings': settings})
        patcher.start()
        self.addCleanup(patcher.stop)
        codex = {'adapter': 'codex', 'model': 'gpt-6-sol', 'effort': 'medium'}
        # Called on the engine: the fixture App has no model catalogue for the HTTP handler's per-request check.
        job = engine.create_job('架空のWBS依頼', '合成の依頼を登録する。', str(self.root), False, workflow='wbs',
                                role_profiles={'builder': codex})
        self.assertEqual(job['role_overrides']['builder']['adapter'], 'codex')
        with self.assertRaisesRegex(ValueError, '.local/test-tmp'):
            engine.create_job('架空のWBS依頼', '合成の依頼を登録する。', str(self.root), False, workflow='wbs',
                              role_profiles={'builder': {'adapter': 'claude', 'model': 'sonnet', 'effort': 'medium'}})

    def test_invalid_selection_is_rejected_without_fallback(self):
        self.enable()
        for value in ('WBS', 'wbs ', '', 1, True, None, ['wbs']):
            with self.subTest(value=value):
                self.assertEqual(self.register(workflow=value), 400)
        self.assertEqual(self.jobs(), [])

    def test_v10_documentation_and_light_route_are_not_wbs_targets(self):
        self.enable()
        self.assertEqual(self.register(workflow='wbs', kind='documentation', document_format='md'), 400)
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
        # A document request hides the choice and says how it proceeds instead of going silent.
        self.assertIn("$('job-workflow-box').hidden=docs;$('job-workflow-hint').textContent=docs?DOCUMENT_FLOW_HINT:WORKFLOW_HINT", script)
        self.assertIn('資料作成は、計画 → 資料の作成 → レビュー → 受け取りの順に進みます。', script)

    def test_plan_screen_shows_each_features_files(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js is required for JavaScript behavior checks')
        check = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const context={el:(tag,text)=>text};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function ownedPathsLine('),source.indexOf('// 新方式の計画の確認（承認前）。')),context);
assert.match(context.ownedPathsLine({owned_paths:['架空.py','tests/test_架空.py']}),/架空\.py、tests\/test_架空\.py/);
assert.match(context.ownedPathsLine({owned_paths:[]}),/なし/);
assert.equal(context.ownedPathsLine({}),null);
assert.ok(source.includes('const owned=ownedPathsLine(w);if(owned)item.append(owned);'));
"""
        ran = subprocess.run([node, '-e', check, str(ROOT / 'web' / 'app.js')], capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(ran.returncode, 0, ran.stderr)

    def test_feature_confirmation_asks_the_person_only_for_their_own_checks(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js is required for JavaScript behavior checks')
        check = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const context={};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function pendingCheckGroups('),source.indexOf('// 機能ごとの担当ファイル。')),context);
const groups=context.pendingCheckGroups({pending_checks:[{check:'架空の本人確認',owner:'user'},{check:'架空の後続確認',owner:'ai'},{check:'架空の旧形式'},{check:''}]});
assert.deepEqual([...groups.person],['架空の本人確認','架空の旧形式']);
assert.deepEqual([...groups.ai],['架空の後続確認']);
const none=context.pendingCheckGroups({});
assert.equal(none.person.length+none.ai.length,0);
assert.ok(source.includes('後の工程でAIが確かめる項目（この確認には含みません）'));
"""
        ran = subprocess.run([node, '-e', check, str(ROOT / 'web' / 'app.js')], capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(ran.returncode, 0, ran.stderr)

    def test_feature_review_to_start_is_shown_as_the_users_decision(self):
        # Live run: the job waited for the person while the screen said "running" and "your decisions: 0".
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js is required for JavaScript behavior checks')
        check = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const piece=(from,to)=>source.slice(source.indexOf(from),source.indexOf(to));
const wp={id:'wp1',title:'架空の機能',state_revision:3,task_ids:['b','r1','r2'],available_actions:['request_feature_review']};
const job={id:'j',title:'架空の依頼',status:'running',workflow_contract_version:1,workflow:{work_packages:[wp]}};
const task=(id,role,status,finished_at)=>({id,role,status,finished_at,job_id:'j',title:id,attempt:1});
const context={state:{tasks:[task('b','builder','succeeded',1),task('r1','reviewer','succeeded',2),task('r2','reviewer','succeeded',3),task('later','reviewer','queued')],jobs:[job],approvals:[],config:{max_repairs:2}},
  endedJob:()=>false,p0Phase:()=>null,userDecision:()=>true,stopReason:()=>null,startFeatureReview:()=>'started',
  isWorkflowJob:()=>true,needsMaintenance:()=>false};
vm.createContext(context);
vm.runInContext(piece('function pending(id)','function stopReason(')+piece('function stalledReview(','const completedFolds')
  +piece('function needsUserDecision(','function deliverableGroup(')+piece('function decisionItems(','function openNotifiedTask('),context);
const items=context.decisionItems();
assert.equal(items.length,1);
assert.equal(items[0].task.id,'r2');
assert.match(items[0].detail,/架空の機能/);
assert.equal(items[0].start(),'started');
assert.equal(context.needsUserDecision(job),true);
assert.deepEqual(context.state.tasks.filter(t=>context.group(t)===2).map(t=>t.id),['r2']);
wp.available_actions=[];
assert.equal(context.decisionItems().length,0);
assert.equal(context.needsUserDecision(job),false);
assert.deepEqual(context.state.tasks.filter(t=>context.group(t)===2).map(t=>t.id),[]);
"""
        ran = subprocess.run([node, '-e', check, str(ROOT / 'web' / 'app.js')], capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(ran.returncode, 0, ran.stderr)


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
                           'command': CHECK, 'existing_tests': [],
                           'test_methods': ['unit'], 'preconditions': ['架空の実行環境と .local/test-tmp'],
                           'setup_steps': ['架空のデータを準備する'], 'synthetic_accounts': [], 'ui_test_required': False,
                           'ui_test_tool': 'not_applicable', 'ui_test_reason': '', 'ui_test_command': '',
                           'cleanup_steps': ['架空のデータを消す']},
        'goal_contract': {'primary_goal': '架空の一覧・検索と詳細・編集', 'deliverables': DELIVERABLES,
                          'acceptance_criteria': CRITERIA, 'non_goals': [], 'constraints': ['架空のデータだけを使う']},
        'work_packages': [
            {'wbs_id': 'W1.01', 'title': '一覧・検索', 'goal_links': [DELIVERABLES[0], CRITERIA[0]],
             'done_when': ['一覧と検索が動く'], 'depends_on': [], 'input_artifacts': [],
             'owned_paths': ['catalog.py', 'tests/test_catalog.py']},
            {'wbs_id': 'W1.02', 'title': '詳細・編集', 'goal_links': [DELIVERABLES[1], CRITERIA[1]],
             'done_when': ['詳細表示と編集が動く'], 'depends_on': ['W1.01'], 'input_artifacts': [],
             'owned_paths': ['detail.py', 'tests/test_detail.py']}],
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
        report = {'status': 'done', 'summary': '架空の報告', 'checks': ['架空の確認'], 'pending_checks': [], 'pending_check_details': [],
                  'question': '', 'questions': [], 'context_updates': [], 'warnings': [], 'stage_complete': True,
                  'blocking_reasons': [], 'rule_results': []}
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
            if task.get('agent_name') == 'common-security-reviewer' and getattr(self, 'reviewer_pending', None):
                report.update(pending_checks=[self.reviewer_pending])
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

    def test_reviewer_pending_check_goes_to_the_feature_review_instead_of_stalling(self):
        # M09 live run: a read-only reviewer could not verify one condition, and no one could start the feature review.
        self.reviewer_pending = '架空の未確認：変更が2つのファイルだけか（Gitの差分を読めない）'
        self.assertEqual(self.register(workflow='wbs'), 200)
        job = self.jobs()[0]
        self.decide(self.approval('ai_start'), True, '架空の開始承認')
        self.drain()
        plan = self.approval('plan')
        self.decide(plan, True, '架空の計画承認', workflow_confirmation=plan['workflow_revision_digest'], feature_checks=['W1.01', 'W1.02'])
        self.drain()
        first = self.packages()['W1.01']
        self.assertEqual((first['execution_state'], first['review_status']), ('succeeded', 'not_requested'), self.dump())
        view = self.server.app.engine.workflow_view(job['id'])
        actions = next(w for w in view['work_packages'] if w['id'] == first['id'])['available_actions']
        self.assertIn('request_feature_review', actions, self.dump())
        prepared = self.ok('/api/workflow/feature-review/prepare', {
            'job_id': job['id'], 'work_package_id': first['id'], 'work_package_revision': first['state_revision'],
            'action': 'request_feature_review'})
        target = self.store.get(prepared['approval_id'], 'approval')['payload']['feature_target']
        self.assertEqual([p['check'] for p in target['pending_checks']], [self.reviewer_pending])
        self.ok('/api/workflow/feature-review/decide', {
            'approval_id': prepared['approval_id'], 'allow': True, 'note': '', 'action': 'decide_feature_review',
            'confirmation': {'action': 'feature_review_confirmation', 'target_digest': prepared['target_digest'],
                             'decision': 'approved'}})
        self.assertEqual(self.packages()['W1.01']['review_status'], 'approved')

    def test_without_marks_the_features_run_through_to_the_final_acceptance(self):
        # D2: the only person's step after the plan is the final acceptance, and it still shows what no reviewer could verify.
        self.reviewer_pending = '架空の未確認：変更が2つのファイルだけか（Gitの差分を読めない）'
        self.assertEqual(self.register(workflow='wbs'), 200)
        self.decide(self.approval('ai_start'), True, '架空の開始承認')
        self.drain()
        plan = self.approval('plan')
        status, _ = self.post('/api/decide', {'id': plan['id'], 'allow': True, 'feature_checks': ['W9.99'],
                                              'workflow_confirmation': plan['workflow_revision_digest']})
        self.assertNotEqual(status, 200)
        self.decide(plan, True, '架空の計画承認', workflow_confirmation=plan['workflow_revision_digest'])
        self.drain()
        self.assertEqual({w['review_basis'] for w in self.packages().values()}, {'ai_review'}, self.dump())
        self.assertEqual(self.jobs()[0]['status'], 'awaiting_acceptance', self.dump())
        payload = self.approval('completion')['payload']
        checks = [c for item in payload['pending_items'] + payload['reference_items'] for c in item['checks']]
        self.assertIn(self.reviewer_pending, checks)

    def test_a_marked_feature_stops_while_an_unmarked_one_moves_on(self):
        self.assertEqual(self.register(workflow='wbs'), 200)
        self.decide(self.approval('ai_start'), True, '架空の開始承認')
        self.drain()
        plan = self.approval('plan')
        self.decide(plan, True, '架空の計画承認', workflow_confirmation=plan['workflow_revision_digest'], feature_checks=['W1.02'])
        self.drain()
        packages = self.packages()
        self.assertEqual(packages['W1.01']['review_basis'], 'ai_review', self.dump())
        self.assertEqual((packages['W1.02']['execution_state'], packages['W1.02']['review_status']), ('succeeded', 'not_requested'), self.dump())
        self.assertEqual(self.jobs()[0]['status'], 'running')
        self.feature_decision('W1.02', True, '架空の確認')
        self.drain()
        self.assertEqual(self.jobs()[0]['status'], 'awaiting_acceptance', self.dump())

    def test_each_package_task_is_told_which_feature_it_is_on(self):
        # The whole plan reaches every assignee as history, but a review's "this feature" named no feature.
        self.assertEqual(self.register(workflow='wbs'), 200)
        job = self.jobs()[0]
        self.decide(self.approval('ai_start'), True, '架空の開始承認')
        self.drain()
        plan = self.approval('plan')
        self.decide(plan, True, '架空の計画承認', workflow_confirmation=plan['workflow_revision_digest'], feature_checks=['W1.01', 'W1.02'])
        self.drain()
        self.feature_decision('W1.01', True, '架空の確認')
        self.drain()
        self.feature_decision('W1.02', True, '架空の確認')
        self.drain()
        specs = {s['wbs_id']: s for s in PLAN['work_packages']}
        seen = {}
        for task in self.store.for_job('task', job['id']):
            prompt = self.prompts.get(task['id'])
            if prompt is None:
                continue
            if not task.get('work_package_id'):
                self.assertNotIn('今回の機能:', prompt, task['title'])
                continue
            label = self.store.get(task['work_package_id'], 'work_package')['wbs_id']
            seen.setdefault(label, set()).add(task['role'])
            section = prompt.split('今回の機能:', 1)[1].split('今回の担当:', 1)[0]
            self.assertIn(f"{label}「{specs[label]['title']}」", section)
            for text in specs[label]['done_when'] + specs[label]['goal_links']:
                self.assertIn(text, section)
            other = next(s for name, s in specs.items() if name != label)
            self.assertNotIn(other['wbs_id'], section)
            self.assertNotIn(other['done_when'][0], section)
        self.assertEqual(seen, {'W1.01': {'builder', 'reviewer'}, 'W1.02': {'builder', 'reviewer'}})

    def test_reviews_receive_the_changed_files_they_cannot_read_from_git(self):
        # M09 live run: a read-only reviewer left "only two files changed?" unverified.
        self.assertEqual(self.register(workflow='wbs'), 200)
        job = self.jobs()[0]
        self.decide(self.approval('ai_start'), True, '架空の開始承認')
        self.drain()
        plan = self.approval('plan')
        self.decide(plan, True, '架空の計画承認', workflow_confirmation=plan['workflow_revision_digest'], feature_checks=['W1.01', 'W1.02'])
        self.drain()
        self.feature_decision('W1.01', True, '架空の確認')
        self.drain()
        marker = '采来がGitで確認した作業状態'
        checked = 0
        for task in self.store.for_job('task', job['id']):
            prompt = self.prompts.get(task['id'])
            if prompt is None:
                continue
            if task['role'] != 'reviewer':
                self.assertNotIn(marker, prompt, task['title'])
                continue
            section = prompt.split(marker, 1)[1].split('今回の担当:', 1)[0]
            self.assertIn('catalog.py', section)
            self.assertIn('tests/test_catalog.py', section)
            self.assertNotIn('SCOPE.md', section)
            checked += 1
        self.assertGreaterEqual(checked, 4, self.dump())

    def test_web_rule_is_left_out_only_after_a_clearly_non_web_plan(self):
        self.assertEqual(self.register(workflow='wbs'), 200)
        job = self.jobs()[0]
        self.decide(self.approval('ai_start'), True, '架空の開始承認')
        self.drain()
        plan = self.approval('plan')
        self.decide(plan, True, '架空の計画承認', workflow_confirmation=plan['workflow_revision_digest'], feature_checks=['W1.01', 'W1.02'])
        self.drain()
        rule = 'Webサーバーを作成・変更する依頼では'
        for task in self.store.for_job('task', job['id']):
            prompt = self.prompts.get(task['id'])
            if prompt is None:
                continue
            if task['role'] == 'planner':
                self.assertIn(rule, prompt)
            else:
                self.assertNotIn(rule, prompt, task['title'])
            # What protects the user stays for every assignee.
            for kept in ('情報の取り扱い（Security by Default）', '公開・push・課金は禁止', '【依頼条件を代用品へ置き換えない】'):
                self.assertIn(kept, prompt, task['title'])

    def test_a_feature_review_may_cite_the_features_own_completion_condition(self):
        # Live run: the reviewer is told to report on done_when, and its finding citing one stopped the whole job.
        from team_engine import ProviderError
        self.assertEqual(self.register(workflow='wbs'), 200)
        job = self.jobs()[0]
        self.decide(self.approval('ai_start'), True, '架空の開始承認')
        self.drain()
        plan = self.approval('plan')
        self.decide(plan, True, '架空の計画承認', workflow_confirmation=plan['workflow_revision_digest'], feature_checks=['W1.01', 'W1.02'])
        self.drain()
        review = next(t for t in self.store.for_job('task', job['id'])
                      if t['role'] == 'reviewer' and t.get('work_package_id'))
        condition = self.store.get(review['work_package_id'], 'work_package')['done_when'][0]
        self.assertIn('機能別のレビューでは、この機能の完了条件の文面でもよい', self.prompts[review['id']])
        engine = self.server.app.engine

        def finding(requirement):
            return {'status': 'needs_changes', 'summary': '架空の指摘', 'checks': ['架空の確認'],
                    'blocking_reasons': [{'requirement': requirement, 'impact': '架空の影響'}]}

        engine._validate(review, finding(condition))
        with self.assertRaises(ProviderError) as refused:
            engine._validate(review, finding('架空の計画にない条件'))
        self.assertEqual(refused.exception.code, 'format')
        self.assertIn('架空の計画にない条件', str(refused.exception))

    def attempted_writes(self, planned, label, paths):
        """Run the flow up to the builder of `label`; what that builder's Write requests get back, per path."""
        results, simulated = {}, self.answer
        engine = self.server.app.engine

        def answer(ctx, prompt, schema):
            task = self.store.get(ctx.task['id'], 'task')
            wbs = self.store.get(task['work_package_id'], 'work_package')['wbs_id'] if task.get('work_package_id') else None
            if wbs == label and task['role'] == 'builder':
                stub = SimpleNamespace(task=task, project=ctx.project, writable=True, context_files=[],
                                       approve=lambda *a, **k: {'allow': False, 'note': 'ordinary approval reached'})
                with patch('team_auto_edit.approved_scope_edit', return_value=False):
                    for path in paths:
                        results[path] = engine._tool_request_context(stub, {'tool': 'Write', 'input': {'file_path': path}})['note']
            return simulated(ctx, prompt, schema)

        self.answer = answer
        with patch(f'{__name__}.PLAN', planned):
            self.assertEqual(self.register(workflow='wbs'), 200)
            self.decide(self.approval('ai_start'), True, '架空の開始承認')
            self.drain()
            plan = self.approval('plan')
            self.decide(plan, True, '架空の計画承認', workflow_confirmation=plan['workflow_revision_digest'], feature_checks=['W1.01', 'W1.02'])
            self.drain()
            if label != 'W1.01':
                self.feature_decision('W1.01', True, '架空の確認')
                self.drain()
        return results

    def test_builder_cannot_write_another_features_file(self):
        notes = self.attempted_writes(copy.deepcopy(PLAN), 'W1.02', ['catalog.py', 'detail.py'])
        self.assertIn('W1.02', notes['catalog.py'])
        self.assertEqual(notes['detail.py'], 'ordinary approval reached')

    def test_plan_without_owned_paths_keeps_the_plan_wide_scope(self):
        # Plans approved before the field existed: reproduce them with the schema of that time.
        import team_wbs
        current = team_wbs.plan_schema

        def earlier_schema(base):
            schema = current(base)
            item = schema['properties']['work_packages']['items']
            del item['properties']['owned_paths']
            item['required'].remove('owned_paths')
            return schema

        planned = copy.deepcopy(PLAN)
        for spec in planned['work_packages']:
            del spec['owned_paths']
        with patch('team_wbs.plan_schema', earlier_schema):
            notes = self.attempted_writes(planned, 'W1.01', ['detail.py'])
        self.assertEqual(notes['detail.py'], 'ordinary approval reached')

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
        self.decide(plan, True, '架空の計画承認', workflow_confirmation=plan['workflow_revision_digest'], feature_checks=['W1.01', 'W1.02'])
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

    def test_marker_text_in_an_approved_plan_instruction_does_not_hide_unbuilt_builders(self):
        marker = '上流の機能が差し戻されたため、この機能は未着手のまま現行版の上で作成する。'
        text = self.call([self.task(0, instruction='架空の作成A' + marker), self.task(1, 'queued')])
        self.assertIn('架空の作成B', text); self.assertNotIn('架空の作成A', text)


if __name__ == '__main__':
    unittest.main()


class WbsTableTests(unittest.TestCase):
    """The always-visible WBS table (web/app.js wbsRows): rows come from the server's projection and task records."""

    def test_rows_follow_plan_features_overall_review_and_receipt(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js is required for JavaScript behavior checks')
        check = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const piece=(from,to)=>source.slice(source.indexOf(from),source.indexOf(to));
const task=(id,role,status,extra)=>({id,role,status,job_id:'j',title:'架空の'+id,attempt:1,created_at:Number(id.slice(1)),...extra});
const review={workflow_authority:{kind:'package_review'}};
const tasks=[task('t1','planner','succeeded'),
  task('t2','builder','succeeded',{work_package_id:'w1',attempt:2}),
  task('t3','reviewer','succeeded',{work_package_id:'w1',...review,result:{status:'needs_changes',rule_results:[{rule:'R2',outcome:'fail'}]}}),
  task('t4','builder','interrupted',{work_package_id:'w1',workflow_authority:{kind:'package_repair'}}),
  task('t5','reviewer','cancelled',{work_package_id:'w1',...review}),
  task('t6','builder','queued',{work_package_id:'w2'}),
  task('t7','reviewer','succeeded',{agent_name:'common-security-reviewer',slim:true,result_status:'done',rule_results:[{rule:'R5',outcome:'pass'}]})];
const job={id:'j',title:'架空の依頼',status:'interrupted',workflow_contract_version:1,workflow:{continue_task_ids:['t4'],
  work_packages:[{id:'w1',title:'架空の機能1',workflow_generation:1,available_actions:[]},{id:'w2',title:'架空の機能2',available_actions:['request_feature_review']}]}};
const context={state:{tasks,jobs:[job],approvals:[]},statuses:{},userDecision:()=>true,isWorkflowJob:()=>true,
  RULE_NAMES:{R2:'完了条件',R5:'安全'},RULE_OUTCOMES:{pass:'合格',fail:'不合格'},
  wpState:wp=>wp.id==='w1'?{key:'rework',label:'差し戻しの修正中',owner:'采来'}:{key:'preparing',label:'確認の準備中',owner:'采来'}};
vm.createContext(context);
vm.runInContext(piece('function pending(id)','function maintenanceHeld(')+piece('function wbsRowState(','function renderWbsBoard('),context);
const rows=context.wbsRows(job);
assert.deepEqual(Array.from(rows,r=>r.number),['0','1','1.1','1.2','1.3','2','2.1','3.1','4']);
assert.deepEqual(Array.from(rows,r=>r.kind),['計画','機能','作業','レビュー','差し戻しの修正','機能','作業','全体のセキュリティのレビュー','受け取り']);
const by=n=>rows.find(r=>r.number===n);
assert.equal(by('1.1').label,'完了（やり直し 1 回）');
assert.equal(by('1.2').key,'returned'); assert.match(by('1.2').rules,/完了条件：不合格/); assert.equal(by('1.2').title,'レビュー');
assert.equal(by('1.3').key,'stopped'); assert.match(by('1.3').label,/続ける／やめる/); assert.equal(by('1.3').owner,'あなた');
assert.match(by('1').label,/差し戻し 1 回/);
assert.equal(by('2').key,'mine'); assert.equal(by('2').owner,'あなた');
assert.equal(by('2.1').label,'開始待ち');
assert.equal(by('3.1').key,'done'); assert.match(by('3.1').rules,/安全：合格/);
assert.equal(by('4').label,'まだ');
job.status='awaiting_acceptance'; assert.equal(context.wbsRows(job).at(-1).key,'mine');
job.status='accepted'; assert.equal(context.wbsRows(job).at(-1).label,'受領済み');
"""
        ran = subprocess.run([node, '-e', check, str(ROOT / 'web' / 'app.js')], capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(ran.returncode, 0, ran.stderr)

    def test_requests_without_features_list_their_work_in_order(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js is required for JavaScript behavior checks')
        check = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const piece=(from,to)=>source.slice(source.indexOf(from),source.indexOf(to));
const task=(id,role,status,extra)=>({id,role,status,job_id:'d',title:'架空の'+id,attempt:1,created_at:Number(id.slice(1)),...extra});
const tasks=[task('t1','planner','succeeded'),task('t2','builder','succeeded'),task('t3','reviewer','succeeded'),
  task('t4','reviewer','failed',{agent_name:'common-security-reviewer'}),task('t5','builder','cancelled'),
  task('t9','builder','running',{job_id:'other'})];
const job={id:'d',title:'架空の資料',status:'failed',document_source:'C:/fictional'};
const context={state:{tasks,jobs:[job,{id:'w',status:'running',workflow_contract_version:1},{id:'x',status:'cancelled'}],approvals:[]},statuses:{},
  userDecision:()=>true,isWorkflowJob:j=>j?.workflow_contract_version===1,RULE_NAMES:{},RULE_OUTCOMES:{},wpState:()=>({})};
vm.createContext(context);
vm.runInContext(piece('function pending(id)','function maintenanceHeld(')+piece('function wbsJobs(','function wbsPick('),context);
assert.deepEqual(Array.from(context.wbsJobs(),j=>j.id).sort(),['d','w']);   // every request except cancelled ones
const rows=context.wbsRows(job);
assert.deepEqual(Array.from(rows,r=>[r.number,r.kind]),[['0','計画'],['1','資料の作成'],['2','レビュー'],['3','セキュリティのレビュー'],['4','受け取り']]);
assert.equal(rows[3].key,'stopped'); assert.equal(rows[3].owner,'あなた'); assert.match(rows[3].label,/行を押して対応/);
assert.equal(rows[4].label,'まだ');
delete job.document_source; assert.equal(context.wbsRows(job)[1].kind,'作業');
job.status='awaiting_acceptance'; assert.equal(context.wbsRows(job).at(-1).key,'mine');
"""
        ran = subprocess.run([node, '-e', check, str(ROOT / 'web' / 'app.js')], capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(ran.returncode, 0, ran.stderr)

    def test_the_table_moves_to_a_newly_registered_request_and_otherwise_keeps_the_choice(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js is required for JavaScript behavior checks')
        check = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const context={};vm.createContext(context);
vm.runInContext('var wbsJobId=null,wbsSeen=null;'+source.slice(source.indexOf('function wbsPick('),source.indexOf('function renderWbsBoard(')),context);
const pick=jobs=>vm.runInContext('wbsJobId=wbsPick('+JSON.stringify(jobs)+')',context);
const old={id:'old',status:'accepted'},running={id:'run',status:'running'},fresh={id:'new',status:'awaiting_approval'};
assert.equal(pick([running,old]),'run');            // first load: the newest request still in progress
context.wbsJobId='old';
assert.equal(pick([running,old]),'old');            // the person's choice survives a refresh
assert.equal(pick([fresh,running,old]),'new');      // a request registered just now is shown
assert.equal(pick([fresh,running,old]),'new');
context.wbsJobId='old';
assert.equal(pick([fresh,running,old]),'old');      // and the person can still go back
assert.equal(pick([fresh,running]),'new');          // the chosen request is gone: the newest in progress
"""
        ran = subprocess.run([node, '-e', check, str(ROOT / 'web' / 'app.js')], capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(ran.returncode, 0, ran.stderr)

    def test_a_finished_request_keeps_each_reviews_outcome_but_not_its_report(self):
        from server import App
        slim = App.slim_task({'id': 't', 'status': 'succeeded', 'summary': 'Fictional long report',
                              'result': {'status': 'done', 'summary': 'Fictional long report',
                                         'rule_results': [{'rule': 'R2', 'outcome': 'pass', 'note': 'Fictional'}]}})
        self.assertEqual((slim['result_status'], slim['rule_results'][0]['outcome']), ('done', 'pass'))
        self.assertNotIn('result', slim)
        self.assertNotIn('result_status', App.slim_task({'id': 't', 'status': 'queued'}))
