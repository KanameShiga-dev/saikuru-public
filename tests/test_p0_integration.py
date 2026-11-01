"""Synthetic P0 path from approved plan through an actual local test and acceptance."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from team_config import default_config
from team_engine import Context, Engine
from team_p0_contract import initial
from team_store import Store


COMMAND = 'python -B -X utf8 -m unittest test_lookup'


class P0IntegrationTests(unittest.TestCase):
    def test_plan_to_artifact_to_user_acceptance(self):
        temp = tempfile.TemporaryDirectory(prefix='saikuru-p0-synthetic-')
        self.addCleanup(temp.cleanup)
        folder = temp.name
        root = Path(folder)
        store = Store(root / 'runtime')
        config = default_config()
        config['approved_roots'] = [str(root)]
        engine = Engine(store, config, {}, 'http://127.0.0.1:0')
        engine.paused = True
        self.addCleanup(store.db.close)
        self.addCleanup(engine.handoff.close)
        goal = 'Create a fictional lookup and show no result for an unknown ID'
        job = store.put('job', {'id': 'fictional-p0-job', 'title': 'Fictional lookup',
            'goal': goal, 'goal_contract': initial(goal), 'p0_contract_version': 1,
            'project': str(root), 'status': 'awaiting_approval', 'auto_execute': False,
            'validation_commands': [], 'code_test_contract_version': 1})
        planner = engine.new_task(job, 'Plan', 'Plan fictional lookup', 'planner')
        plan = {'summary': 'Create a fictional lookup', 'security_review_required': False,
            'goal_contract': {'primary_goal': 'Search fictional items',
                              'deliverables': ['lookup.py'],
                              'acceptance_criteria': ['Unknown ID shows no result'],
                              'non_goals': [], 'constraints': ['Fictional data only']},
            'tasks': [{'title': 'Build fictional lookup', 'instruction': 'Build and test lookup',
                       'role': 'builder', 'goal_links': ['lookup.py', 'Unknown ID shows no result']}],
            'code_test_plan': {'required': True, 'reason': 'New code',
                               'cases': ['Unknown ID shows no result'],
                               'command': COMMAND, 'existing_tests': []}}
        store.update(planner['id'], status='succeeded', result=plan)
        g3 = engine.new_approval(planner, 'plan', plan)
        self.assertEqual(store.get(job['id'])['status'], 'awaiting_approval')
        engine.decide(g3['id'], True, 'Fictional plan approved')
        self.assertEqual(store.get(g3['id'], 'approval')['status'], 'approved')
        tasks = store.for_job('task', job['id'])
        builder = next(task for task in tasks if task['role'] == 'builder')
        reviews = [task for task in tasks if task['role'] == 'reviewer']
        self.assertEqual(store.get(job['id'])['goal_contract']['original_request'], goal)
        self.assertEqual(store.get(builder['id'])['goal_links'],
                         ['lookup.py', 'Unknown ID shows no result'])
        self.assertTrue(all(store.get(task['id'])['goal_links'] ==
                            ['lookup.py', 'Unknown ID shows no result'] for task in reviews))

        (root / 'lookup.py').write_text(
            'def lookup(item_id):\n    return "No result" if item_id != "A001" else "Fictional item"\n',
            encoding='utf-8')
        (root / 'test_lookup.py').write_text(
            'import unittest\nfrom lookup import lookup\n'
            'class Check(unittest.TestCase):\n'
            '    def test_unknown(self): self.assertEqual(lookup("X999"), "No result")\n',
            encoding='utf-8')
        run = subprocess.run(['python', '-B', '-X', 'utf8', '-m', 'unittest', 'test_lookup'],
                             cwd=root, capture_output=True, text=True, timeout=15)
        self.assertEqual(run.returncode, 0, run.stderr)
        context = Context.__new__(Context)
        context.engine, context.task = engine, builder
        context.command_result(COMMAND, run.returncode, run.stdout + run.stderr)
        done = {'status': 'done', 'summary': 'Fictional lookup exists', 'checks': [],
                'pending_checks': [], 'question': '', 'questions': [], 'context_updates': [],
                'test_files': ['test_lookup.py'], 'test_change_reason': '',
                'artifacts': ['lookup.py']}
        engine._finish(builder, done)
        self.assertEqual(store.get(builder['id'])['result']['status'], 'done')
        review = {'status': 'done', 'summary': 'Synthetic review passed', 'checks': [],
                  'pending_checks': [], 'question': '', 'questions': [],
                  'context_updates': []}
        for reviewer in reviews:
            engine._finish(reviewer, dict(review))
        job = store.get(job['id'])
        self.assertEqual(job['status'], 'awaiting_acceptance')
        approval = next(item for item in store.for_job('approval', job['id'])
                        if item['kind'] == 'completion')
        self.assertEqual(len(approval['payload']['pending_items']), 2)
        with self.assertRaises(ValueError):
            engine.accept_completion(approval['id'], '正式受領')
        current_id = approval['id']
        for _ in range(2):
            response = engine.record_completion_evidence(
                current_id, 0, 0, '合成成果物の操作を確認', True)
            current_id = response['approval_id']
        engine.acknowledge_completion_review(current_id, True)
        self.assertEqual(engine.accept_completion(current_id, '正式受領')['status'], 'accepted')
        self.assertEqual(store.get(job['id'])['goal_contract']['completion_status'], 'accepted')

# Self-contained fictional W06.02 fixtures; no workspace-relative reads.
K3_PLAN_JSON = (
    '{"summary": "Fictional equipment ledger with list/search, detail and edit screens.", "security_review_required": true, "questions": [], "context_updates": [], "warnings": [], "goal_contract": {"primary_goal": "架空の備品約200件を一覧・検索・詳細・編集の3画面で扱えるようにする。", "deliverables": ["一覧・検索画面", "詳細画面", "編集画面", "架空データ・自動テスト・README"], "acceptance_criteria": ["E0001〜E0200 の200件が架空の名前・カテゴリ・在庫数・備考を持ち、一覧に件数200が表示される", "名前の一部とカテゴリで絞り込め、該当0件では「該当する備品はありません」と表示される", "長すぎる・記号だけの検索語では形式の誤りを表示し、入力をそのまま実行・描画しない", "一覧から選んだ備品の全項目が表示され、存在しないIDでは「見つかりません」と表示される", "名前・カテゴリ・在庫数・備考を編集して保存できる", "空の名前・範囲外の在庫数・長すぎる備考・HTMLのタグを含む入力は保存されず、項目ごとの誤りが表示され、入力が消えない", "保存後に詳細・一覧・検索の結果へ反映され、再起動後も残る", "先に保存された備品を古い版のまま保存しようとすると上書きせず、読み直しを求める"], "non_goals": ["利用者の認証・権限", "貸出の記録・期限", "複数人の同時編集の合流", "外部公開・push・デプロイ"], "constraints": ["架空データだけを使う", "127.0.0.1 だけで待ち受ける", "依存パッケージを追加しない"]}, "code_test_plan": {"required": true, "reason": "Search, validation, persistence and conflict behaviour are checked automatically.", "cases": ["200 fictional items and count", "filter and empty result", "invalid search input", "detail and unknown id", "save", "field validation keeps input", "redisplay and persistence", "version conflict"], "command": "python -B -m unittest discover -s tests", "existing_tests": []}, "checkpoint_paths": ["app.py", "fixtures/equipment.json", "static/index.html", "static/app.js", "tests/test_app.py", "README.md"], "work_packages": [{"wbs_id": "W1.01", "title": "架空データと一覧・検索", "goal_links": ["一覧・検索画面", "架空データ・自動テスト・README", "E0001〜E0200 の200件が架空の名前・カテゴリ・在庫数・備考を持ち、一覧に件数200が表示される", "名前の一部とカテゴリで絞り込め、該当0件では「該当する備品はありません」と表示される", "長すぎる・記号だけの検索語では形式の誤りを表示し、入力をそのまま実行・描画しない"], "done_when": ["一覧・絞込み・0件・不正入力の自動テストが通る"], "depends_on": [], "input_artifacts": []}, {"wbs_id": "W1.02", "title": "詳細の表示", "goal_links": ["詳細画面", "一覧から選んだ備品の全項目が表示され、存在しないIDでは「見つかりません」と表示される"], "done_when": ["詳細と存在しないIDの自動テストが通る"], "depends_on": ["W1.01"], "input_artifacts": []}, {"wbs_id": "W1.03", "title": "編集と保存時の検査", "goal_links": ["編集画面", "名前・カテゴリ・在庫数・備考を編集して保存できる", "空の名前・範囲外の在庫数・長すぎる備考・HTMLのタグを含む入力は保存されず、項目ごとの誤りが表示され、入力が消えない"], "done_when": ["保存と項目ごとの誤り・入力の保持の自動テストが通る"], "depends_on": ["W1.02"], "input_artifacts": []}, {"wbs_id": "W1.04", "title": "保存後の再表示と版の衝突", "goal_links": ["一覧・検索画面", "編集画面", "保存後に詳細・一覧・検索の結果へ反映され、再起動後も残る", "先に保存された備品を古い版のまま保存しようとすると上書きせず、読み直しを求める"], "done_when": ["保存後の再表示・再起動後の永続化・版の衝突の自動テストが通る"], "depends_on": ["W1.03"], "input_artifacts": []}], "tasks": [{"wbs_id": "W1.01", "role": "builder", "title": "架空データと一覧・検索を作る", "instruction": "SCOPE.md の範囲で、架空データの生成、一覧・件数・絞込み・0件・検索語の検査と自動テストを作る。", "goal_links": ["一覧・検索画面", "架空データ・自動テスト・README", "E0001〜E0200 の200件が架空の名前・カテゴリ・在庫数・備考を持ち、一覧に件数200が表示される", "名前の一部とカテゴリで絞り込め、該当0件では「該当する備品はありません」と表示される", "長すぎる・記号だけの検索語では形式の誤りを表示し、入力をそのまま実行・描画しない"]}, {"wbs_id": "W1.02", "role": "builder", "title": "詳細画面を作る", "instruction": "一覧から選んだ備品の詳細と、存在しないIDの表示と自動テストを作る。", "goal_links": ["詳細画面", "一覧から選んだ備品の全項目が表示され、存在しないIDでは「見つかりません」と表示される"]}, {"wbs_id": "W1.03", "role": "builder", "title": "編集と保存時の検査を作る", "instruction": "編集・保存と、項目ごとの誤りの表示・入力の保持と自動テストを作る。", "goal_links": ["編集画面", "名前・カテゴリ・在庫数・備考を編集して保存できる", "空の名前・範囲外の在庫数・長すぎる備考・HTMLのタグを含む入力は保存されず、項目ごとの誤りが表示され、入力が消えない"]}, {"wbs_id": "W1.04", "role": "builder", "title": "保存後の再表示と版の衝突を作る", "instruction": "保存後の詳細・一覧・検索への反映、再起動後の永続化、版の衝突で上書きしない処理と自動テストを作る。", "goal_links": ["一覧・検索画面", "編集画面", "保存後に詳細・一覧・検索の結果へ反映され、再起動後も残る", "先に保存された備品を古い版のまま保存しようとすると上書きせず、読み直しを求める"]}]}'
)

K3_EQUIPMENT_APP = (
    '"""Fictional W06.02 equipment app fixture, standard-library WSGI only."""\n'
    'import html\n'
    'import json\n'
    'import re\n'
    'from pathlib import Path\n'
    'from urllib.parse import parse_qs\n'
    '\n'
    '\n'
    'class EquipmentApp:\n'
    '    def __init__(self, root):\n'
    "        self.path = Path(root) / 'fixtures' / 'equipment.json'\n"
    '        self.path.parent.mkdir(parents=True, exist_ok=True)\n'
    '        if not self.path.exists():\n'
    "            self.save([{'id': 'E%04d' % n, 'name': '備品%04d' % n, 'category': '種類%d' % (n % 5),\n"
    "                        'stock': n % 100, 'notes': '', 'version': 1} for n in range(1, 201)])\n"
    '\n'
    '    def load(self):\n'
    "        return json.loads(self.path.read_text(encoding='utf-8'))\n"
    '\n'
    '    def save(self, rows):\n'
    "        self.path.write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')\n"
    '\n'
    '    def __call__(self, env, start):\n'
    "        path = env.get('PATH_INFO', '/')\n"
    "        query = parse_qs(env.get('QUERY_STRING', ''), keep_blank_values=True)\n"
    '        rows = self.load()\n'
    '        esc = lambda value: html.escape(str(value), quote=True)\n'
    '        errors = []\n'
    "        if path == '/':\n"
    "            term = query.get('q', [''])[0]\n"
    "            category = query.get('category', [''])[0]\n"
    '            if len(term) > 100 or (term and not any(ch.isalnum() for ch in term)):\n'
    "                errors.append('検索語の形式が不正です')\n"
    '                shown = []\n'
    '            else:\n'
    "                shown = [row for row in rows if term in row['name'] and (not category or category == row['category'])]\n"
    "            body = '<h1>一覧・検索</h1><p>全件数: %d</p><p>%s</p>' % (len(rows), esc('; '.join(errors)))\n"
    '            body += \'\'.join(\'<a href="/detail?id=%s">%s</a>\' % (esc(row[\'id\']), esc(row[\'name\'])) for row in shown)\n'
    "            if not shown and not errors: body += '<p>該当する備品はありません</p>'\n"
    "        elif path in ('/detail', '/edit'):\n"
    "            item_id = query.get('id', [''])[0]\n"
    "            item = next((r for r in rows if r['id'] == item_id), None)\n"
    '            if item is None:\n'
    "                body = '<p>見つかりません</p>'\n"
    "            elif path == '/detail':\n"
    '                body = \'<h1>詳細</h1><p>%s</p><a href="/edit?id=%s">編集</a>\' % (esc(json.dumps(item, ensure_ascii=False)), esc(item_id))\n'
    '            else:\n'
    '                values = dict(item)\n'
    "                if env.get('REQUEST_METHOD') == 'POST':\n"
    "                    length = int(env.get('CONTENT_LENGTH') or 0)\n"
    "                    form = parse_qs(env['wsgi.input'].read(length).decode('utf-8'), keep_blank_values=True)\n"
    "                    for key in ('name', 'category', 'stock', 'notes', 'version'):\n"
    "                        values[key] = form.get(key, [''])[0]\n"
    "                    if not values['name'].strip(): errors.append('名前')\n"
    "                    if values['category'] not in ['種類%d' % i for i in range(5)]: errors.append('カテゴリ')\n"
    "                    try: stock = int(values['stock'])\n"
    '                    except ValueError: stock = -1\n'
    "                    if stock < 0 or stock > 99: errors.append('在庫数')\n"
    "                    if len(values['notes']) > 2000: errors.append('備考')\n"
    "                    if any(re.search(r'<[^>]*>', values[key]) for key in ('name', 'category', 'notes')):\n"
    "                        errors.append('HTMLタグ')\n"
    "                    if values['version'] != str(item['version']): errors.append('版の衝突: 読み直してください')\n"
    '                    if not errors:\n'
    "                        item.update(name=values['name'], category=values['category'], stock=stock,\n"
    "                                    notes=values['notes'], version=item['version'] + 1)\n"
    '                        self.save(rows)\n'
    "                        start('303 See Other', [('Location', '/detail?id=' + item_id), ('Content-Length', '0')])\n"
    "                        return [b'']\n"
    '                body = \'<h1>編集</h1><p>%s</p><form method="post">\' % esc(\'; \'.join(errors))\n'
    '                body += \'\'.join(\'<input name="%s" value="%s">\' % (key, esc(values[key]))\n'
    "                                for key in ('name', 'category', 'stock', 'notes', 'version')) + '</form>'\n"
    '        else:\n'
    "            body = '<p>見つかりません</p>'\n"
    '        data = (\'<!doctype html><html><meta charset="utf-8"><body>\' + body + \'</body></html>\').encode(\'utf-8\')\n'
    "        start('200 OK', [('Content-Type', 'text/html; charset=utf-8'), ('Content-Length', str(len(data)))])\n"
    '        return [data]\n'
)

K3_EQUIPMENT_TESTS = (
    'import io\n'
    'import threading\n'
    'import tempfile\n'
    'import unittest\n'
    'from urllib.parse import urlencode\n'
    'from urllib.request import Request, urlopen\n'
    'from wsgiref.simple_server import make_server\n'
    'from app import EquipmentApp\n'
    '\n'
    '\n'
    'class FictionalEquipmentScenarios(unittest.TestCase):\n'
    '    def setUp(self):\n'
    '        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)\n'
    '        self.app = EquipmentApp(self.tmp.name)\n'
    '\n'
    "    def call(self, path, query='', form=None):\n"
    "        encoded = urlencode(form or {}).encode('utf-8')\n"
    "        env = {'PATH_INFO': path, 'QUERY_STRING': query, 'REQUEST_METHOD': 'POST' if form is not None else 'GET',\n"
    "               'CONTENT_LENGTH': str(len(encoded)), 'wsgi.input': io.BytesIO(encoded)}\n"
    '        response = {}\n'
    "        body = b''.join(self.app(env, lambda status, headers: response.update(status=status, headers=dict(headers))))\n"
    "        return response, body.decode('utf-8')\n"
    '\n'
    '    def test_s1_s4_normal_empty_invalid_and_persistence(self):\n'
    "        response, listing = self.call('/')\n"
    "        self.assertIn('全件数: 200', listing); self.assertIn('備品0001', listing)\n"
    "        self.assertIn('E0001', self.call('/detail', 'id=E0001')[1])\n"
    "        self.assertIn('見つかりません', self.call('/detail', 'id=E9999')[1])\n"
    "        self.assertIn('該当する備品はありません', self.call('/', 'q=NO_SUCH_ITEM')[1])\n"
    "        self.assertIn('検索語の形式が不正です', self.call('/', 'q=!!!')[1])\n"
    "        self.assertIn('検索語の形式が不正です', self.call('/', 'q=' + 'a' * 101)[1])\n"
    '        original = self.app.load()[0]\n'
    "        form = {'name': '', 'category': original['category'], 'stock': '-1', 'notes': '<script>', 'version': '1'}\n"
    "        _, invalid = self.call('/edit', 'id=E0001', form)\n"
    "        for label in ('名前', '在庫数', 'HTMLタグ'): self.assertIn(label, invalid)\n"
    "        self.assertIn('&lt;script&gt;', invalid); self.assertNotIn('<script>', invalid)\n"
    '        self.assertEqual(self.app.load()[0], original)\n'
    "        form.update(name='変更済備品', stock='99', notes='正常')\n"
    "        response, _ = self.call('/edit', 'id=E0001', form)\n"
    "        self.assertTrue(response['status'].startswith('303'))\n"
    "        self.assertIn('変更済備品', self.call('/detail', 'id=E0001')[1])\n"
    "        self.assertIn('変更済備品', self.call('/', 'q=' + urlencode({'q': '変更済備品'}).split('=', 1)[1])[1])\n"
    '        self.app = EquipmentApp(self.tmp.name)\n'
    "        self.assertEqual(self.app.load()[0]['name'], '変更済備品')\n"
    "        _, conflict = self.call('/edit', 'id=E0001', form)\n"
    "        self.assertIn('版の衝突', conflict); self.assertEqual(self.app.load()[0]['version'], 2)\n"
    "        form.update(version='2', stock='100', notes='x' * 2001)\n"
    "        _, invalid = self.call('/edit', 'id=E0001', form)\n"
    "        self.assertIn('在庫数', invalid); self.assertIn('備考', invalid)\n"
    "        self.assertEqual(self.app.load()[0]['version'], 2)\n"
    '\n'
    '    def test_s4_loopback_server_restart_keeps_saved_fictional_data(self):\n'
    '        def start():\n'
    "            server = make_server('127.0.0.1', 0, EquipmentApp(self.tmp.name))\n"
    '            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()\n'
    '            return server, thread\n'
    '        def stop(server, thread):\n'
    '            server.shutdown(); thread.join(5); server.server_close()\n'
    '        server, thread = start()\n'
    '        try:\n'
    "            address = 'http://127.0.0.1:%d' % server.server_port\n"
    "            body = urlencode({'name': '再表示備品', 'category': '種類1', 'stock': '3', 'notes': '架空', 'version': '1'}).encode('utf-8')\n"
    "            with urlopen(Request(address + '/edit?id=E0001', data=body), timeout=5) as response:\n"
    "                self.assertIn('再表示備品', response.read().decode('utf-8'))\n"
    '        finally:\n'
    '            stop(server, thread)\n'
    '        server, thread = start()\n'
    '        try:\n'
    "            with urlopen('http://127.0.0.1:%d/detail?id=E0001' % server.server_port, timeout=5) as response:\n"
    "                self.assertIn('再表示備品', response.read().decode('utf-8'))\n"
    '        finally:\n'
    '            stop(server, thread)\n'
    '\n'
    '\n'
    "if __name__ == '__main__': unittest.main()\n"
)


class WorkflowApiIntegrationTests(unittest.TestCase):
    """Synthetic worker results and synthetic user actions, never real receipt."""

    def test_four_fictional_work_packages_run_in_order_to_synthetic_receipt(self):
        import json
        from tests import test_workflow_api as fixtures
        from unittest.mock import patch
        def four_packages(two=False):
            value = json.loads(K3_PLAN_JSON)
            value['checkpoint_paths'] = ['app.py', 'w101.py', 'w102.py', 'w103.py', 'w104.py',
                                         'tests/test_equipment.py']
            return value
        with patch('tests.test_plan_revision.proposal', side_effect=four_packages):
            f = fixtures.WorkflowApiTests(); self.addCleanup(f.doCleanups); f.setUp()
        engine, store, job_id = f.engine, f.store, f.f.job_id
        (f.f.workspace.project / 'app.py').write_text(K3_EQUIPMENT_APP, encoding='utf-8')
        (f.f.workspace.project / 'tests' / 'test_equipment.py').write_text(K3_EQUIPMENT_TESTS, encoding='utf-8')
        for index in range(1, 5):
            (f.f.workspace.project / ('w10%d.py' % index)).write_text('# SYNTHETIC package artifact\n', encoding='utf-8')
        original_finish = engine._finish
        def finish_with_package_artifact(task, result):
            if task.get('role') == 'builder' and task.get('work_package_id'):
                wp = store.get(task['work_package_id'], 'work_package')
                artifacts = [wp['wbs_id'].replace('.', '').lower() + '.py']
                if wp['wbs_id'] == 'W1.01': artifacts.append('app.py')
                result = dict(result, artifacts=artifacts)
            return original_finish(task, result)
        finish_bridge = patch.object(engine, '_finish', side_effect=finish_with_package_artifact)
        finish_bridge.start(); self.addCleanup(finish_bridge.stop)
        original_result = Context.command_result
        def correlated_result(context, command, exit_code, output, timed_out=False):
            operation_id = 'synthetic-' + str(len(store.job_events(job_id, 'operation_result')))
            context.command_started(command, operation_id)
            return original_result(context, command, exit_code, output, timed_out, operation_id=operation_id)
        operation_bridge = patch.object(Context, 'command_result', new=correlated_result)
        operation_bridge.start(); self.addCleanup(operation_bridge.stop)
        packages = store.for_job('work_package', job_id)
        self.assertEqual(len(packages), 4)
        decisions = []
        for index in range(4):
            builder = next(t for t in f.f.tasks() if t.get('plan_task_index') == index)
            if index == 1:
                first = next(t for t in f.f.tasks() if t.get('plan_task_index') == 0)
                first_before = store.get(first['id'], 'task')
                running = engine._reserve_workflow_attempt(builder, engine.workflow_dispatch_view(job_id))
                previous_attempt = running['current_attempt']['id']
                baseline = engine.prepare_native_scope_baseline(running)
                store.update(builder['id'], agent_run={'id': 'fictional-s6-native',
                    'provider': running['profile']['adapter'], 'model': running['profile']['model'],
                    'effort': running['profile']['effort'], 'attempt_id': previous_attempt,
                    'started_at': running['current_attempt']['started_at'],
                    'measurement_ids': {'job_id': job_id, 'task_id': builder['id'], 'attempt_id': previous_attempt},
                    'tool_observation_version': 1, 'tool_calls': {'fileChange': 1}, 'scope_baseline': baseline})
                path = f.f.workspace.project / 'w102.py'
                path.write_bytes(path.read_bytes() + b'# SYNTHETIC S6 edit before any command\n')
                # A new isolated Engine and SQLite connections exercise the
                # persisted restart/fencing path without touching the running App.
                from http.server import ThreadingHTTPServer
                import threading
                from server import Handler
                f.http.shutdown(); f.thread.join(5); f.http.server_close()
                from team_store import Store
                from team_engine import Engine
                restarted_store = Store(store.directory)
                self.addCleanup(restarted_store.db.close)
                restarted_engine = Engine(restarted_store, engine.config, engine.commands, engine.endpoint)
                restarted_engine.paused = True
                self.addCleanup(restarted_engine.handoff.close)
                finish_bridge.stop()
                engine, store = restarted_engine, restarted_store
                f.engine = f.f.engine = f.app.engine = engine
                f.store = f.f.store = f.app.store = store
                f.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                f.http.app = f.app
                f.app.port = f.http.server_port; f.app.origin = 'http://127.0.0.1:' + str(f.app.port)
                f.thread = threading.Thread(target=f.http.serve_forever, daemon=True); f.thread.start()
                original_finish = engine._finish
                finish_bridge = patch.object(engine, '_finish', side_effect=finish_with_package_artifact)
                finish_bridge.start(); self.addCleanup(finish_bridge.stop)
                self.assertIn(store.get(builder['id'])['current_attempt']['status'], ('closed', 'fenced'))
                code, assessed = f.request('/api/workflow/recovery/assess', {
                    'action': 'assess_recovery', 'job_id': job_id, 'task_id': builder['id']}, maintenance=True)
                self.assertEqual(code, 200, assessed); self.assertTrue(assessed['eligible_for_reconciliation'], assessed)
                code, reconciled = f.request('/api/workflow/recovery/reconcile', {
                    'action': 'reconcile_recovery', 'job_id': job_id, 'task_id': builder['id'],
                    'target': assessed['target'], 'writer_stopped': True, 'checked_changes': True,
                    'external_effects': 'none_confirmed', 'note': 'SYNTHETIC file-only S6 observation'}, maintenance=True)
                self.assertEqual(code, 200, reconciled)
                code, confirmed = f.request('/api/workflow/control', {
                    'action': 'confirm_recovery', 'job_id': job_id, 'task_id': builder['id'],
                    'target': engine.workflow_control_target(job_id, 'confirm_recovery', builder['id']),
                    'writer_stopped': True, 'checked_changes': True}, maintenance=True)
                self.assertEqual(code, 200, confirmed); self.assertFalse(confirmed['queued'])
                code, retried = f.request('/api/workflow/control', {
                    'action': 'retry_task', 'job_id': job_id, 'task_id': builder['id'],
                    'target': engine.workflow_control_target(job_id, 'retry_task', builder['id']),
                    'checked_changes': True, 'note': 'SYNTHETIC S6 retry', 'handoff_allowed': False}, maintenance=True)
                self.assertEqual(code, 200, retried); self.assertTrue(retried['queued'])
                self.assertEqual(store.get(first['id'], 'task'), first_before)
                original_reserve = engine._reserve_workflow_attempt
                def verify_fresh_session(candidate, view):
                    from types import SimpleNamespace
                    reserved = original_reserve(candidate, view)
                    if reserved['id'] == builder['id']:
                        self.assertNotEqual(reserved['current_attempt']['id'], previous_attempt)
                        inherited = json.loads(engine._checkpoint_session_context(SimpleNamespace(task=reserved, agent_run={})))
                        self.assertFalse(inherited['checkpoint_reference']['native_resume'])
                    return reserved
                reserve_guard = patch.object(engine, '_reserve_workflow_attempt', side_effect=verify_fresh_session)
                reserve_guard.start()
                try:
                    f.f.finish_next(summary='SYNTHETIC resumed W1.02')
                finally:
                    reserve_guard.stop()
                self.assertEqual(store.get(first['id'], 'task'), first_before)
                for _ in range(2):
                    f.f.finish_next(summary='SYNTHETIC offline package review')
            else:
                for _ in range(3):
                    f.f.finish_next(summary='SYNTHETIC offline package result')
            package = store.get(builder['work_package_id'], 'work_package')
            code, review = f.request('/api/workflow/feature-review/prepare', {
                'job_id': job_id, 'work_package_id': package['id'],
                'work_package_revision': package['state_revision'], 'action': 'request_feature_review'})
            self.assertEqual(code, 200, review)
            if index == 2:
                earlier = [store.get(ident, 'approval') for ident in decisions]
                code, rejection = f.request('/api/workflow/feature-review/decide', f.decision_body(review, False))
                self.assertEqual(code, 200, rejection)
                self.assertEqual([store.get(ident, 'approval') for ident in decisions], earlier)
                self.assertEqual(set(rejection['affected_ids']), {package['id'], packages[3]['id']},
                    [(w.get('wbs_id'), w.get('id')) for w in store.for_job('work_package', job_id)])
                for _ in range(3):
                    f.f.finish_next(summary='SYNTHETIC bounded repair and review')
                package = store.get(builder['work_package_id'], 'work_package')
                code, review = f.request('/api/workflow/feature-review/prepare', {
                    'job_id': job_id, 'work_package_id': package['id'],
                    'work_package_revision': package['state_revision'], 'action': 'request_feature_review'})
                self.assertEqual(code, 200, review)
            code, decision = f.request('/api/workflow/feature-review/decide', f.decision_body(review))
            self.assertEqual(code, 200, decision); self.assertFalse(decision['formal_receipt'])
            decisions.append(review['approval_id'])
        for _ in [t for t in f.f.tasks() if t.get('workflow_job_task') and t.get('status') == 'queued']:
            f.f.finish_next(summary='SYNTHETIC integration review')
        self.assertEqual(f.f.job()['status'], 'awaiting_acceptance')
        completion = next(a for a in store.for_job('approval', job_id)
                          if a['kind'] == 'completion' and a['status'] == 'pending')
        for _ in range(12):
            if not completion['payload'].get('pending_items'): break
            code, result = f.request('/api/completion/evidence', {'approval_id': completion['id'],
                'item_index': 0, 'check_index': 0, 'evidence': 'SYNTHETIC local inspection', 'confirmed': True})
            self.assertEqual(code, 200, result)
            completion = store.get(result['approval_id'], 'approval')
        self.assertFalse(completion['payload'].get('pending_items'))
        self.assertEqual(f.request('/api/jobs/review-confirmation',
            {'approval_id': completion['id'], 'confirmed': True})[0], 200)
        code, result = f.request('/api/jobs/formal-acceptance', {'approval_id': completion['id'],
            'confirmation': '正式受領', 'note': 'SYNTHETIC ONLY'})
        self.assertEqual(code, 200, result); self.assertEqual(result['status'], 'accepted')
        self.assertEqual(len(decisions), 4)

    def test_serial_features_to_global_review_and_explicit_synthetic_receipt(self):
        from tests import test_workflow_api as fixtures
        f = fixtures.WorkflowApiTests(); self.addCleanup(f.doCleanups); f.setUp()
        engine, store, job_id = f.engine, f.store, f.f.job_id
        from unittest.mock import patch
        original_result = Context.command_result
        def correlated_result(context, command, exit_code, output, timed_out=False):
            # The synthetic provider explicitly echoes its operation ID on both
            # messages. Production does not infer IDs from command text.
            operation_id = 'synthetic-' + str(len(store.job_events(job_id, 'operation_result')))
            context.command_started(command, operation_id)
            return original_result(context, command, exit_code, output, timed_out, operation_id=operation_id)
        operation_bridge = patch.object(Context, 'command_result', new=correlated_result)
        operation_bridge.start(); self.addCleanup(operation_bridge.stop)
        # The fixture publishes its fictional plan through the real G3 engine.
        self.assertEqual(f.f.job()['plan_revision'], 1)
        self.assertEqual(f.f.job()['status'], 'running')
        decided = []
        for index in range(2):
            builder = next(t for t in f.f.tasks() if t.get('plan_task_index') == index)
            if index == 1:
                reserved = engine._reserve_workflow_attempt(builder, engine.workflow_dispatch_view(job_id))
                engine.store.recover()  # Fictional crash, actual persistence/fencing path.
                target = engine.workflow_control_target(job_id, 'confirm_recovery', builder['id'])
                confirm = {'action': 'confirm_recovery', 'job_id': job_id, 'task_id': builder['id'],
                           'target': target, 'writer_stopped': True, 'checked_changes': True}
                self.assertEqual(f.request('/api/workflow/control', confirm)[0], 403)
                code, result = f.request('/api/workflow/control', confirm, maintenance=True)
                if code != 200:
                    # Preserve the real refusal reason in the sanitized local
                    # test report, rather than weakening the operation fence.
                    engine._recovery_control_inputs(builder['id'])
                self.assertEqual(code, 200, result); self.assertFalse(result['queued'])
                draft = engine.collect_checkpoint(builder['id'], writer_stopped=True)
                self.assertTrue(engine.finalize_checkpoint(draft['id'], writer_stopped=True)['handoff_synced'])
                self.assertTrue(engine._checkpoint_work_package_dispatch_valid(builder['id']))
                assessment = engine.assess_checkpoint_resume(builder['id'], writer_stopped=True)
                self.assertEqual(assessment['status'], 'candidate', assessment)
                retry = {'action': 'retry_task', 'job_id': job_id, 'task_id': builder['id'],
                         'target': engine.workflow_control_target(job_id, 'retry_task', builder['id']),
                         'checked_changes': True, 'note': 'SYNTHETIC crash inspected', 'handoff_allowed': False}
                code, result = f.request('/api/workflow/control', retry, maintenance=True)
                self.assertEqual(code, 200, result); self.assertFalse(result['started'])
                self.assertNotEqual(store.get(builder['id'])['current_attempt']['status'], 'running')
            for _ in range(3):
                f.f.finish_next(summary='SYNTHETIC Provider result; no native model')
            wp = store.get(builder['work_package_id'], 'work_package')
            body = {'job_id': job_id, 'work_package_id': wp['id'],
                    'work_package_revision': wp['state_revision'], 'action': 'request_feature_review'}
            code, review = f.request('/api/workflow/feature-review/prepare', body)
            self.assertEqual(code, 200, review)
            self.assertNotEqual(engine.workflow_dispatch_view(job_id)['status'], 'ready')
            code, result = f.request('/api/workflow/feature-review/decide', f.decision_body(review))
            self.assertEqual(code, 200, result); self.assertFalse(result['formal_receipt'])
            decided.append(review['approval_id'])
            self.assertEqual(f.f.job()['status'], 'running')
        global_reviews = [t for t in f.f.tasks() if t.get('workflow_job_task')]
        self.assertTrue(global_reviews)
        for _ in global_reviews:
            f.f.finish_next(summary='SYNTHETIC independent integration review')
        self.assertEqual(f.f.job()['status'], 'awaiting_acceptance')
        completion = next(a for a in store.for_job('approval', job_id)
                          if a['kind'] == 'completion' and a['status'] == 'pending')
        body = {'approval_id': completion['id'], 'confirmation': '正式受領', 'note': 'SYNTHETIC ONLY'}
        self.assertEqual(f.request('/api/jobs/formal-acceptance', body)[0], 400)
        # Clear only the actual generated checks, via the real HTTP evidence route.
        for _ in range(12):
            pending = completion['payload'].get('pending_items') or []
            if not pending: break
            code, result = f.request('/api/completion/evidence', {'approval_id': completion['id'],
                'item_index': 0, 'check_index': 0, 'evidence': 'SYNTHETIC local inspection only', 'confirmed': True})
            self.assertEqual(code, 200, result)
            completion = store.get(result['approval_id'], 'approval')
        self.assertFalse(completion['payload'].get('pending_items'))
        self.assertEqual(f.request('/api/jobs/review-confirmation',
                                  {'approval_id': completion['id'], 'confirmed': True})[0], 200)
        body['approval_id'] = completion['id']
        code, result = f.request('/api/jobs/formal-acceptance', body)
        self.assertEqual(code, 200, result); self.assertEqual(result['status'], 'accepted')
        self.assertEqual(f.request('/api/jobs/formal-acceptance', body)[0], 400)
        self.assertEqual(len(store.job_events(job_id, 'formal_acceptance_decided')), 1)
        self.assertEqual(len(decided), 2)


class ReviewedOperationRecoveryTests(unittest.TestCase):
    def setUp(self):
        from tests import test_workflow_api as fixtures
        from unittest.mock import patch
        self.f = fixtures.WorkflowApiTests(); self.addCleanup(self.f.doCleanups); self.f.setUp()
        original = Context.command_result
        def correlated(context, command, code, output, timed_out=False):
            operation_id = 'synthetic-prior-' + str(len(self.f.store.job_events(self.f.f.job_id, 'operation_result')))
            context.command_started(command, operation_id)
            return original(context, command, code, output, timed_out, operation_id=operation_id)
        bridge = patch.object(Context, 'command_result', new=correlated)
        bridge.start(); self.addCleanup(bridge.stop)
        review = self.f.f.ready_feature(); self.f.f.approve(review)
        view = self.f.engine.workflow_dispatch_view(self.f.f.job_id)
        self.target = self.f.engine._reserve_workflow_attempt(self.f.store.get(view['candidate_task_id']), view)

    def stop(self):
        self.f.store.recover()
        return self.f.store.get(self.target['id']), self.f.f.job()

    def test_prior_review_is_valid_only_while_current_feature_receipt_is_approved(self):
        task, job = self.stop()
        basis = self.f.engine._reviewed_operation_basis(task, job)
        self.assertTrue(basis['reviewed_prior_operations'])
        self.f.store.update(self.f.f.wp()['id'], review_status='invalidated')
        with self.assertRaises(ValueError): self.f.engine._reviewed_operation_basis(task, job)

    def test_prior_artifact_change_invalidates_basis_without_mutating_state(self):
        task, job = self.stop()
        path = self.f.f.workspace.project / 'app.py'
        path.write_bytes(path.read_bytes() + b'\n# SYNTHETIC changed after review\n')
        before = self.f.f.state()
        with self.assertRaises(ValueError): self.f.engine._reviewed_operation_basis(task, job)
        self.assertEqual(self.f.f.state(), before)

    def test_target_attempt_operation_is_not_covered_by_another_feature_review(self):
        context = Context.__new__(Context); context.engine = self.f.engine; context.task = self.target
        context.command_result('python -B -m unittest', 0, 'SYNTHETIC target action')
        task, job = self.stop()
        with self.assertRaises(ValueError): self.f.engine._reviewed_operation_basis(task, job)

    def test_store_recovery_without_engine_revalidation_still_refuses_prior_operations(self):
        task, job = self.stop()
        before = self.f.f.state()
        with self.assertRaises(ValueError):
            self.f.store.confirm_recovery(task['id'], task['state_revision'], job['state_revision'],
                                          task['current_attempt']['id'])
        self.assertEqual(self.f.f.state(), before)

    def test_proposed_retry_checks_the_real_wp_approval_without_queuing(self):
        task, job = self.stop()
        self.f.engine.confirm_recovery(task['id'], task['state_revision'], job['state_revision'],
                                       task['current_attempt']['id'], writer_stopped=True, checked_changes=True)
        self.assertTrue(self.f.engine._checkpoint_work_package_dispatch_valid(task['id']))
        self.assertEqual(self.f.store.get(task['id'])['status'], 'interrupted')
        self.f.store.update(task['id'], spec_source_approval_id='fictional-foreign-plan')
        before = self.f.f.state()
        try:
            valid = self.f.engine._checkpoint_work_package_dispatch_valid(task['id'])
        except (ValueError, KeyError, TypeError):
            valid = False
        self.assertFalse(valid)
        self.assertEqual(self.f.f.state(), before)

    def test_interrupted_wp_cannot_reuse_the_completed_successor_session_binding(self):
        task, job = self.stop()
        self.f.engine.confirm_recovery(task['id'], task['state_revision'], job['state_revision'],
                                       task['current_attempt']['id'], writer_stopped=True, checked_changes=True)
        draft = self.f.engine.collect_checkpoint(task['id'], writer_stopped=True)
        self.assertTrue(self.f.engine.finalize_checkpoint(draft['id'], writer_stopped=True)['handoff_synced'])
        before = self.f.f.state()
        with self.assertRaises(ValueError):
            self.f.engine.prepare_checkpoint_session(task['id'], task['id'], writer_stopped=True)
        self.assertEqual(self.f.f.state(), before)
        self.assertFalse(self.f.store.get(task['id']).get('checkpoint_session_binding'))


class NativeReconciliationTests(unittest.TestCase):
    def setUp(self):
        from tests.test_recovery_integration_synthetic import NativeAssessmentTests
        NativeAssessmentTests.setUp(self)

    def assess(self):
        return self.f.request('/api/workflow/recovery/assess', self.body, maintenance=True)[1]

    def request_body(self):
        assessment = self.assess()
        self.assertTrue(assessment['eligible_for_reconciliation'], assessment)
        return {'action': 'reconcile_recovery', 'job_id': self.job_id, 'task_id': self.task_id,
            'target': assessment['target'], 'writer_stopped': True, 'checked_changes': True,
            'external_effects': 'none_confirmed', 'note': 'SYNTHETIC maintenance judgment; files inspected, no external effect.'}

    def reconcile(self, body, **options):
        return self.f.request('/api/workflow/recovery/reconcile', body, maintenance=True, **options)

    def test_record_is_durable_immutable_and_does_not_confirm_or_queue(self):
        body = self.request_body(); task = self.store.get(self.task_id); job = self.store.get(self.job_id)
        code, result = self.reconcile(body)
        self.assertEqual(code, 200, result); self.assertTrue(result['recorded'])
        self.assertFalse(result['queued']); self.assertFalse(result['resume_allowed'])
        self.assertEqual(result['execution_permission'], 'none')
        self.assertEqual(self.store.get(self.task_id), task); self.assertEqual(self.store.get(self.job_id), job)
        record = self.store.native_reconciliation(self.task_id, body['target'])
        self.assertEqual(record['id'], result['reconciliation_id'])
        other = Store(self.store.directory)
        try: self.assertEqual(other.native_reconciliation(self.task_id, body['target']), record)
        finally: other.db.close()
        with self.assertRaises(ValueError): self.store.update(record['id'], note='SYNTHETIC replacement')
        self.assertEqual(self.reconcile(body)[0], 400)  # Same evidence cannot overwrite the first judgment.
        after = self.assess()
        self.assertEqual(after['target'], body['target']); self.assertTrue(after['reconciliation_recorded'])
        with self.assertRaises(ValueError):
            self.engine.confirm_recovery(self.task_id, task['state_revision'], job['state_revision'],
                task['current_attempt']['id'], writer_stopped=True, checked_changes=True)

    def test_browser_and_maintenance_identity_and_exact_schema_required(self):
        body = self.request_body(); before = self.f.f.state()
        self.assertEqual(self.f.request('/api/workflow/recovery/reconcile', body)[0], 403)
        for options in ({'cookie': False}, {'ui': False}, {'origin': 'https://example.invalid'},
                        {'cookie': False, 'headers': {'Authorization': 'Bearer fictional-worker'}}):
            self.assertEqual(self.reconcile(body, **options)[0], 403)
        for extra in ({'actor': 'authenticated_maintenance_session'}, {'job_id': 'fictional-other-job'}, {'target': None}):
            self.assertEqual(self.reconcile(dict(body, **extra))[0], 400)
        self.assertEqual(self.f.f.state(), before)

    def test_confirmation_note_type_length_and_dlp_fail_without_writes(self):
        body = self.request_body(); before = self.f.f.state()
        for changes in ({'writer_stopped': False}, {'writer_stopped': 1}, {'checked_changes': False},
                        {'external_effects': 'unknown'}, {'note': None}, {'note': ''}, {'note': 'x' * 1001},
                        {'note': 'fictional-internal-tag'}):
            self.assertEqual(self.reconcile(dict(body, **changes))[0], 400, changes.keys())
        self.assertEqual(self.f.f.state(), before)

    def test_changed_files_or_own_operation_refuse_stale_target(self):
        body = self.request_body()
        path = self.f.f.workspace.project / 'app.py'
        path.write_bytes(path.read_bytes() + b'\n# SYNTHETIC changed after inspection\n')
        before = self.f.f.state()
        self.assertEqual(self.reconcile(body)[0], 400); self.assertEqual(self.f.f.state(), before)
        body = self.request_body()
        self.store.event('operation_result', 'SYNTHETIC uncorrelated effect', self.task_id, self.job_id)
        before = self.f.f.state()
        self.assertEqual(self.reconcile(body)[0], 400); self.assertEqual(self.f.f.state(), before)

    def test_other_running_task_requires_idle_without_stopping_it(self):
        body = self.request_body()
        job = self.store.put('job', {'id': 'fictional-other-job', 'goal': 'SYNTHETIC unrelated',
            'project': str(self.f.f.workspace.project.parent / 'fictional-other'), 'status': 'running'})
        task = self.engine.new_task(job, 'SYNTHETIC other worker', 'SYNTHETIC', 'builder')
        self.store.update(task['id'], status='running')
        before = self.f.f.state(); assessment = self.assess()
        self.assertFalse(assessment['eligible_for_reconciliation'])
        self.assertIn('native_maintenance_idle_required', assessment['reasons'])
        self.assertIsNone(assessment['target'])
        self.assertEqual(self.reconcile(body)[0], 400)
        self.assertEqual(self.f.f.state(), before); self.assertEqual(self.store.get(task['id'])['status'], 'running')

    def test_edit_between_two_observations_refuses_commit(self):
        from unittest.mock import patch
        body = self.request_body(); read = self.engine.native_recovery_assessment; count = []
        def observe(*args, **kwargs):
            result = read(*args, **kwargs); count.append(True)
            if len(count) == 1:
                path = self.f.f.workspace.project / 'app.py'
                path.write_bytes(path.read_bytes() + b'\n# SYNTHETIC between observations\n')
            return result
        before = self.f.f.state()
        with patch.object(self.engine, 'native_recovery_assessment', side_effect=observe):
            self.assertEqual(self.reconcile(body)[0], 400)
        self.assertEqual(len(count), 2); self.assertEqual(self.f.f.state(), before)

    def test_database_update_before_cas_refuses_without_a_record(self):
        from unittest.mock import patch
        body = self.request_body(); commit = self.store.commit_native_reconciliation
        def changed(*args, **kwargs):
            self.store.update(self.task_id, synthetic_concurrent_update=True)
            return commit(*args, **kwargs)
        with patch.object(self.store, 'commit_native_reconciliation', side_effect=changed):
            self.assertEqual(self.reconcile(body)[0], 400)
        self.assertEqual(self.store.for_job('native_effect_reconciliation', self.job_id), [])
        self.assertEqual(self.store.job_events(self.job_id, 'recovery_effects_reconciled'), [])

    def test_concurrent_submission_records_exactly_one_judgment(self):
        from concurrent.futures import ThreadPoolExecutor
        body = self.request_body()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.reconcile(body)[0], range(2)))
        self.assertEqual(sorted(results), [200, 400])
        self.assertEqual(len(self.store.for_job('native_effect_reconciliation', self.job_id)), 1)
        self.assertEqual(len(self.store.job_events(self.job_id, 'recovery_effects_reconciled')), 1)

    def test_worker_and_generic_store_writes_cannot_forge_a_judgment(self):
        from unittest.mock import patch
        body = self.request_body(); before = self.f.f.state()
        with self.assertRaises(ValueError):
            self.store.put('native_effect_reconciliation', {'id': 'fictional-forged-judgment'})
        with self.assertRaises(ValueError):
            self.store.put('fictional_other_kind', {'id': 'native-reconciliation-fictional-preempt'})
        with patch.object(self.store._worker, 'task', self.store.get(self.task_id), create=True):
            with self.assertRaises(ValueError):
                self.engine.reconcile_native_recovery(self.task_id, body['target'], writer_stopped=True,
                    checked_changes=True, external_effects='none_confirmed', note=body['note'],
                    actor='authenticated_maintenance_session')
            with self.assertRaisesRegex(ValueError, 'worker_cannot'):
                self.store.commit_native_reconciliation(self.task_id, body['target'], {}, body['note'], (), lambda: None)
        self.assertEqual(self.f.f.state(), before)

    def test_event_failure_rolls_back_judgment_and_restores_guard(self):
        from unittest.mock import patch
        body = self.request_body(); before = self.f.f.state()
        with patch.object(self.store, 'event', side_effect=RuntimeError('SYNTHETIC commit failure')):
            with self.assertRaises(RuntimeError):
                self.engine.reconcile_native_recovery(self.task_id, body['target'], writer_stopped=True,
                    checked_changes=True, external_effects='none_confirmed', note=body['note'],
                    actor='authenticated_maintenance_session')
        self.assertEqual(self.f.f.state(), before)
        self.assertIsNone(getattr(self.store._worker, 'native_reconciliation_commit', None))

    def test_new_file_edit_invalidates_record_display_without_changing_old_record(self):
        body = self.request_body(); self.assertEqual(self.reconcile(body)[0], 200)
        record = self.store.native_reconciliation(self.task_id, body['target'])
        path = self.f.f.workspace.project / 'app.py'
        path.write_bytes(path.read_bytes() + b'\n# SYNTHETIC after reconciliation\n')
        self.assertFalse(self.assess()['reconciliation_recorded'])
        self.assertEqual(self.store.native_reconciliation(self.task_id, body['target']), record)

    def test_active_worker_before_cas_refuses_without_a_record(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        body = self.request_body(); commit = self.store.commit_native_reconciliation
        def changed(*args, **kwargs):
            self.engine.active['fictional-concurrent-worker'] = SimpleNamespace(project='fictional-other-project')
            return commit(*args, **kwargs)
        try:
            with patch.object(self.store, 'commit_native_reconciliation', side_effect=changed):
                self.assertEqual(self.reconcile(body)[0], 400)
        finally: self.engine.active.clear()
        self.assertEqual(self.store.for_job('native_effect_reconciliation', self.job_id), [])

    def test_duplicated_reconciliation_event_is_not_a_valid_record(self):
        body = self.request_body(); self.assertEqual(self.reconcile(body)[0], 200)
        record = self.store.native_reconciliation(self.task_id, body['target'])
        self.store.event('recovery_effects_reconciled', 'SYNTHETIC duplicate', self.task_id, self.job_id,
            {'attempt_id': record['attempt_id'], 'reconciliation_id': record['id'],
             'target_digest': body['target']['state_digest'], 'record_digest': record['record_digest']})
        assessment = self.assess()
        self.assertFalse(assessment['eligible_for_reconciliation'])
        self.assertIn('native_reconciliation_record_unresolved', assessment['reasons'])
        self.assertIsNone(assessment['target'])

    def test_fresh_diagnostic_target_cannot_override_unsupported_tools(self):
        body = self.request_body()
        self.store.update(self.task_id, agent_run=dict(self.run, tool_calls={'mcpToolCall': 1}))
        assessment = self.assess()
        self.assertFalse(assessment['eligible_for_reconciliation'])
        self.assertIsNotNone(assessment['target'])
        body['target'] = assessment['target']; before = self.f.f.state()
        self.assertEqual(self.reconcile(body)[0], 400)
        self.assertEqual(self.f.f.state(), before)

    def test_other_connection_archive_update_before_cas_is_rejected(self):
        from unittest.mock import patch
        body = self.request_body(); commit = self.store.commit_native_reconciliation
        def changed(*args, **kwargs):
            other = Store(self.store.directory)
            try:
                other.put('agent_run', dict(self.run, job_id=self.job_id, task_id=self.task_id,
                    status='interrupted', tool_calls={'mcpToolCall': 1}))
            finally: other.db.close()
            return commit(*args, **kwargs)
        with patch.object(self.store, 'commit_native_reconciliation', side_effect=changed):
            self.assertEqual(self.reconcile(body)[0], 400)
        self.assertEqual(self.store.for_job('native_effect_reconciliation', self.job_id), [])

    def test_identifier_types_are_bounded_before_storage_lookup(self):
        from unittest.mock import patch
        body = self.request_body(); before = self.f.f.state()
        with patch.object(self.store, 'get', side_effect=AssertionError('SYNTHETIC unexpected storage lookup')) as get:
            for key in ('task_id', 'job_id'):
                for value in (None, True, [], {}, '', ' ', 'x' * 129):
                    self.assertEqual(self.reconcile(dict(body, **{key: value}))[0], 400)
            get.assert_not_called()
        self.assertEqual(self.f.f.state(), before)


if __name__ == '__main__':
    unittest.main()
