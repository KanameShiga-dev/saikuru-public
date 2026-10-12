"""Isolated, bounded three-arm experiment. Never opens production task databases."""
import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from team_config import ROOT, default_config
from team_decisions import route_agent, DecisionRouter, DecisionInput
from team_engine import Context
from team_auto_read import auto_read_reason, _script
from team_auto_edit import auto_edit_reason
from team_store import now, uid

ARMS = {
    'A-disabled': {'provider': 'disabled', 'model': 'tev1:0.8b', 'shadow': True},
    'B-shadow': {'provider': 'ollama', 'model': 'tev1:0.8b', 'shadow': True},
    'C-adopted': {'provider': 'ollama', 'model': 'tev1:0.8b', 'shadow': False},
}
SPEC = '''# 固定仕様：既存タスク管理Webアプリの機能追加
このフォルダだけを対象に、既存コードを調査し以下を実装する。
Python標準ライブラリのみ。外部通信・依存追加・GUI・公開・Git操作・子Agentは禁止。
質問は不要。未指定のデザインは簡潔な日本語UI、既存機能互換を優先する。

1. TaskStore.list(query='', status=None, page=1, page_size=20)を追加する。
タイトル・説明の部分一致検索、todo/doing/doneの状態フィルタ、id昇順、ページング。
返却は {'items': [...], 'total': 件数, 'page': page, 'page_size': page_size}。
page>=1、1<=page_size<=100のみ許可。boolや整数以外、不正statusはValueError。
SQLはパラメータ化する。queryの%・_は文字として扱う。
2. TaskStore.update(id, title=None, description=None, status=None)を追加。
titleは空白除去後1〜100文字。statusは上記3値。未知idはKeyError。
Noneの項目は変更しない。失敗時は既存データを変えない。
3. TaskStore.export_csv(query='', status=None)を追加。CSV文字列を返す。
列順id,title,description,status。ヘッダ必須、全該当行をid昇順で出力。
csvモジュールを使い、タイトル・説明が=,+,-,@で始まる場合は先頭に'を付ける。
4. GET /api/tasksにquery/status/page/page_sizeを接続する。
GET /api/exportでUTF-8 CSVを返す。入力不正は400、未知idは404。
PUT /api/tasks/<id>でJSONによる更新。外部接続・認証機能は追加しない。
5. web/index.htmlに検索・状態・ページ移動・CSVダウンロード・状態更新を追加。
利用者の文字列はtextContentで表示し、innerHTMLに埋め込まない。
6. tests/test_store.pyとtests/test_http.pyを追加して、通常・空・境界・不正入力・既存互換を検証。
README.mdに仕様、起動方法、テスト方法、未検証事項を記載する。

許可編集：app/*.py、web/index.html、tests/test_*.py、README.mdのみ。
AGENTS.md・SPEC.md・統括コード・受入テストは変更禁止。
許可実行：python -m unittest discover -s tests -v、python -m compileall -q app。
質問や承認依頼で停止せず、許可範囲で完了する。範囲外操作が必要ならblockedと根拠を返す。
doneは実機UI確認を意味しない。テスト実施結果と未確認事項を分ける。
初期コードは統括が内容とハッシュを確認して指示へ添付する。読み取りコマンドを重ねる必要はない。
編集はapply_patchを使う。コマンドが拒否された場合、同じ要求を繰り返さない。
テストは統括が各実装終了後に外部受入テストとサンプル内unittestを実行する。
担当自身でテストを実行できなくても、統括で検証する予定と明記し質問なしで引き渡す。
この計測では永久的な事実登録は不要。context_updates=[]とし、工程結果と根拠はsummary/checksに記録する。
'''
SEED = {
 'app/__init__.py': '',
 'app/store.py': '''import sqlite3

class TaskStore:
    def __init__(self, database=":memory:"):
        self.db = sqlite3.connect(database, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY, title TEXT, description TEXT, status TEXT)")
        self.db.commit()

    def create(self, title, description="", status="todo"):
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 100:
            raise ValueError("invalid title")
        if status not in ("todo", "doing", "done"):
            raise ValueError("invalid status")
        row = self.db.execute("INSERT INTO tasks(title,description,status) VALUES(?,?,?)", (title.strip(), description, status))
        self.db.commit()
        return row.lastrowid

    def all(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM tasks ORDER BY id")]
''',
 'app/server.py': '''import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from app.store import TaskStore

def make_server(store, port=0):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def send_json(self, code, value):
            data = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_GET(self):
            if self.path == "/api/tasks":
                self.send_json(200, store.all())
            else:
                self.send_json(404, {"error": "not found"})
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)

if __name__ == "__main__":
    make_server(TaskStore("tasks.sqlite3"), 8800).serve_forever()
''',
 'web/index.html': '''<!doctype html><html lang="ja"><meta charset="utf-8"><title>タスク管理サンプル</title><h1>タスク管理</h1><ul id="tasks"></ul><script>fetch('/api/tasks').then(r=>r.json()).then(rows=>rows.forEach(t=>{const li=document.createElement('li');li.textContent=t.title;document.getElementById('tasks').append(li);}));</script></html>''',
 'tests/__init__.py': '',
 'README.md': '# タスク管理サンプル\nPython標準ライブラリのみ。ローカル比較用。\n',
 'SPEC.md': SPEC,
 'AGENTS.md': '# 計測専用ハーネス\n\n'+SPEC,
}


def allowed_command(ctx, payload):
    if payload.get('operation') != 'item/commandExecution/requestApproval':
        return False
    details = payload.get('details') or {}
    if not isinstance(details.get('cwd'), str) or Path(details['cwd']).resolve() != Path(ctx.project).resolve():
        return False
    if any(details.get(k) for k in ('reason','networkApprovalContext','additionalPermissions')):
        return False
    try:
        executable, script = _script(details)
    except (ValueError, KeyError, TypeError):
        return False
    allowed_shells = {
        (Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe').resolve(),
        (Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/native/powershell/pwsh.exe').resolve(),
    }
    if executable not in allowed_shells:
        return False
    return script.strip() in {
        'python -m unittest discover -s tests -v',
        'python -m compileall -q app',
        'python -X utf8 -m unittest discover -s tests -v',
    }


def benchmark_permission(settings, hint, authorized):
    """Record advisory judgment. Fixed harness alone authorizes execution."""
    decision = DecisionRouter(settings=settings).route(DecisionInput(
        'benchmark_permission', ('ALLOW', 'DENY'), 'ALLOW' if authorized else 'DENY',
        {'operation': 'evaluate_benchmark_permission', 'task_hints': [hint], 'scope_verified': bool(authorized)}, 'low'))
    decision.update(authority='advisory_only', execution_allowed=bool(authorized))
    return bool(authorized), decision


def permission_handoff(decision):
    # Shadow candidates stay out of the executor's input to preserve comparison.
    return {'recommendation': decision['effective'], 'authority': 'advisory_only',
            'execution_allowed': decision['execution_allowed'],
            'instruction': '判断は参考情報。実行可否は固定ハーネスで確認済み。許可範囲内の操作を担当が実施し、結果と根拠を報告する。範囲を超える操作は実施しない。'}


def automatic_benchmark_approval(ctx, payload):
    """Allow only existing safe readers, correlated scoped edits and fixed test commands."""
    operation = payload.get('operation')
    allowed = False
    if operation == 'item/fileChange/requestApproval':
        changes = payload.get('fileChanges') or []
        root = Path(ctx.project).resolve()
        def target_allowed(change):
            try:
                p = Path(change['path'])
                p = (p if p.is_absolute() else root / p).resolve()
                rel = p.relative_to(root).as_posix()
                return bool(re.fullmatch(r'(app/[^/]+\.py|tests/test_[^/]+\.py|web/index\.html|README\.md)', rel))
            except (ValueError, KeyError, TypeError):
                return False
        if changes and all(target_allowed(c) for c in changes):
            allowed = bool(auto_edit_reason(ctx, payload))
    elif operation == 'item/commandExecution/requestApproval':
        allowed = bool(auto_read_reason(payload, ctx.project)) or allowed_command(ctx, payload)
    # Provider supplies advice; authorization always remains the fixed harness.
    hint = ('scoped_edit' if operation == 'item/fileChange/requestApproval' else
            'fixed_test' if allowed_command(ctx, payload) else 'safe_read') if allowed else 'outside_allowlist'
    allowed, decision = benchmark_permission(
        getattr(ctx.engine, 'config', {'decision': {'provider': 'disabled'}}).get('decision'), hint, allowed)
    ctx.engine.store.event('benchmark_permission_decision', json.dumps(decision, ensure_ascii=False), ctx.task['id'], ctx.task['job_id'])
    ctx.engine.store.event('benchmark_permission', '計測用の固定許可範囲: '+('許可' if allowed else '拒否'), ctx.task['id'], ctx.task['job_id'])
    return {'allow': allowed, 'answers': {}, 'note': json.dumps(permission_handoff(decision),ensure_ascii=False)}


def create_run():
    root = ROOT / 'data' / 'benchmarks' / ('decision-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uid()[:6])
    root.mkdir(parents=True)
    hashes = {}
    for name in ARMS:
        project = root / name
        for rel, content in SEED.items():
            path = project / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
            hashes[rel] = hashlib.sha256(content.encode()).hexdigest()
    config = default_config()
    config['roles'] = {r: 'codex-standard' for r in config['roles']}
    config.update(approved_roots=[str(root/n) for n in ARMS], max_parallel_projects=1,
                  max_repairs=1, task_timeout_seconds=600, automatic_operations=False)
    state = root / 'state'
    state.mkdir()
    (state/'config.json').write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    (root/'manifest.json').write_text(json.dumps({'seed_hashes': hashes, 'arms': ARMS,
        'model': 'gpt-6-sol', 'effort': 'medium', 'repairs': 1,
        'scope': 'synthetic local projects only', 'real_ui_tested': False,
        'decision_timeout_ms': 10000}, indent=2), encoding='utf-8')
    return root


def summarize(app, job, name, elapsed):
    tasks = [t for t in app.store.all('task') if t['job_id'] == job['id']]
    calls = []
    decisions = []
    for task in tasks:
        calls.extend(task.get('agent_run_history', []))
        if task.get('agent_run'):
            calls.append(task['agent_run'])
        if task.get('decision_record'):
            decisions.append(task['decision_record'])
    def total(keys):
        values = [next((r['usage'][k] for k in keys if k in r.get('usage', {})), None) for r in calls]
        return sum(values) if values and all(v is not None for v in values) else None
    return {'arm': name, 'job_id': job['id'], 'status': app.store.get(job['id'])['status'],
        'elapsed_seconds': round(elapsed, 2), 'agent_calls': len(calls),
        'usage_reporting_calls': sum(bool(r.get('usage')) for r in calls),
        'input_tokens': total(('inputTokens', 'input_tokens')),
        'cached_input_tokens': total(('cachedInputTokens', 'cache_read_input_tokens')),
        'output_tokens': total(('outputTokens', 'output_tokens')),
        'retries': sum(max(0, t['attempt']-1) for t in tasks),
        'repair_cycles': max((t.get('repair',0) for t in tasks),default=0),
        'repair_builder_calls': sum(t['role']=='builder' and t.get('repair',0)>0 for t in tasks),
        'decisions': decisions,
        'permission_decisions': [json.loads(e['message']) for e in app.store.events(10000)
            if e.get('job_id')==job['id'] and e['type']=='benchmark_permission_decision'],
        'failure_codes': [t.get('failure_code') for t in tasks if t.get('failure_code')],
        'human_questions': sum(a['kind']=='question' for a in app.store.all('approval') if a['job_id']==job['id']),
        'calls': calls, 'independent_acceptance': None, 'ui_workflow': 'NOT TESTED'}


def acceptance(root, name):
    results = []
    project = root/name
    for tag, command in [('external', [sys.executable, str(ROOT/'benchmark_acceptance.py'), str(project)]),
                         ('project', [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'])]:
        try:
            check = subprocess.run(command, cwd=project, capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=90,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            (root/(name+'-'+tag+'-acceptance.txt')).write_text(check.stdout+check.stderr, encoding='utf-8')
            results.append({'suite': tag, 'passed': check.returncode==0, 'evidence':name+'-'+tag+'-acceptance.txt',
                            'summary':(check.stdout+check.stderr)[-6000:]})
        except subprocess.TimeoutExpired:
            results.append({'suite':tag,'passed':False,'summary':'timeout'})
    return results


def snapshot(project):
    files=[]
    for path in sorted(Path(project).rglob('*')):
        if path.is_file() and path.suffix in {'.py','.html','.md'} and '__pycache__' not in path.parts:
            text=path.read_text(encoding='utf-8')
            files.append('FILE '+path.relative_to(project).as_posix()+'\n'+text)
    text='\n\n'.join(files)
    if len(text)>100000:
        raise ValueError('実験のソース文脈が上限を超えました。無断で切り捨てません。')
    return text


def benchmark_context(app, job, allowed_files=None):
    """Keep full history in DB; avoid duplicating source snapshots into every prompt."""
    tasks=[t for t in app.store.all('task') if t['job_id']==job['id']]
    reports=[]
    for task in tasks:
        reports.append({'task_id':task['id'],'role':task['role'],'status':task['status'],
            'instruction_sha256':hashlib.sha256(task['instruction'].encode()).hexdigest(),
            'summary':task.get('summary',''), 'checks':(task.get('result') or {}).get('checks',[]),
            'operation_judgment_handoff':task.get('benchmark_permission_handoff')})
    package={'current_facts':app.engine.handoff.current_facts(job['id']),
             'earlier_user_decisions_in_same_project':[],
             'scope_transfers':job.get('scope_transfers',[]),
             'handoff_scope':job.get('handoff_scope',[]),
             'task_reports':reports,
             'context_note':'新規の合成プロジェクト。元仕様と最新コードは今回の担当指示に全文添付済み。全版の履歴はDB保持。別ページの読み取りや統括DB/APIへのアクセスは不要。'}
    text=json.dumps(package,ensure_ascii=False)
    if len(text)>100000:
        raise ValueError('計測用の引き継ぎが上限を超えました。事実は無断で切り捨てません。')
    return text


def run(root, port, order=None):
    from server import make_server
    os.environ['DECISION_TIMEOUT_MS'] = '10000'
    os.environ['OLLAMA_TIMEOUT_MS'] = '10000'
    server = make_server(root/'state', port)
    app = server.app
    app.engine.paused = True
    app.engine.handoff.context = lambda job, allowed_files=None: benchmark_context(app,job,allowed_files)
    # The isolated process alone uses the fixed experiment approval handler.
    Context.approve = automatic_benchmark_approval
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print('Experiment board: http://127.0.0.1:'+str(port), flush=True)
    print('Artifacts: '+str(root), flush=True)
    rows = []
    try:
        for name in (order or list(ARMS)):
            settings=ARMS[name]
            # Config validation and production UI stay shadow-only. This in-memory
            # experiment is explicitly authorized, same-role and same-permission.
            app.config['decision'] = copy.deepcopy(settings)
            job = app.store.put('job', {'id': uid(), 'title': '計測 '+name+'：タスク管理Webアプリ',
                'goal': SPEC, 'project': str(root/name), 'auto_execute': True,
                'status': 'running', 'created_at': now(), 'benchmark_arm': name})
            after = None
            steps = [('researcher', '計測ハーネスの許可編集範囲・禁止操作・修正上限・完了条件を読み取り専用で監査し、SPEC.mdの境界が明確か報告する。'),
                     ('researcher', '添付した既存コードを読み取り専用で調査し、SPEC.mdに沿う変更箇所と検証方法を報告する。'),
                     ('builder', 'app/store.pyだけを編集し、SPECのlist/update/export_csvを実装する。他の工程は後続担当が行う。このファイルを保存したらdoneで終了する。'),
                     ('builder', 'app/server.pyだけを編集し、SPECのGET検索・CSV・PUT・エラー処理を実装する。既存TaskStore APIは添付の最新ソースに従う。画面・テストは後続担当が行う。保存したらdoneで終了する。'),
                     ('builder', 'web/index.htmlだけを編集し、SPECの検索・状態・ページ・CSV・状態更新UIを実装する。デザインは最小限、150行以内を目安にする。テストは後続担当が行う。保存したらdoneで終了する。'),
                     ('builder', 'tests/test_store.py・tests/test_http.py・README.mdだけを作成または編集する。SPECのテストと説明を用意する。実行は統括が行うので、保存したらdoneで終了する。'),
                     ('reviewer', '添付した変更済みソースと統括のテスト結果を読み取り専用でレビューする。不足はneeds_changesで具体的に報告する。')]
            stage_instructions = {}
            for stage_index, (role, instruction) in enumerate(steps):
                task = app.engine.new_task(job, instruction, instruction+'\n'+SPEC+'\n\n統括が確認した初期コード：\n'+snapshot(root/name), role, after)
                stage_instructions[task['id']]=instruction
                app.store.update(task['id'], benchmark_routing_context={
                    'file_types': ['python','html','markdown'],
                    'task_hints': ['harness_audit' if stage_index == 0 else 'source_investigation'] if role == 'researcher' else ['code_review'] if role == 'reviewer' else []})
                after = task['id']
            started = time.monotonic()
            validated_builders = set()
            refreshed_tasks = set()
            permission_checked = set()
            checks = []
            print('Starting '+name, flush=True)
            while time.monotonic()-started < 1800:
                for task in app.store.all('task'):
                    if (task['job_id']==job['id'] and task['status']=='queued' and task['id'] not in refreshed_tasks
                            and task.get('after') and app.store.get(task['after'])['status']=='succeeded'):
                        refreshed_tasks.add(task['id'])
                        instruction=stage_instructions.get(task['id'],task['instruction'])
                        app.store.update(task['id'],instruction=instruction+'\n'+SPEC+'\n統括の現在ソース：\n'+snapshot(root/name))
                for task in app.store.all('task'):
                    if task['job_id']==job['id'] and task['role']=='builder' and task['status']=='succeeded' and task['id'] not in validated_builders:
                        validated_builders.add(task['id'])
                        if any(t['job_id']==job['id'] and t['role']=='builder' and t['status'] in ('queued','running') for t in app.store.all('task')):
                            continue
                        checks=acceptance(root,name)
                        for review in app.store.all('task'):
                            if review['job_id']==job['id'] and review['role']=='reviewer' and review['status']=='queued':
                                app.store.update(review['id'],instruction=steps[-1][1]+'\n'+SPEC+'\n統括の現在ソース：\n'+snapshot(root/name)+'\n統括の検証結果：\n'+json.dumps(checks,ensure_ascii=False))
                # Explicit judgment handoff even without native tool approval.
                # The executor follows fixed scope, not the advisory verdict.
                for task in app.store.all('task'):
                    if (task['job_id']==job['id'] and task['status']=='queued' and task['id'] not in permission_checked
                            and (not task.get('after') or app.store.get(task['after'])['status']=='succeeded')):
                        permission_checked.add(task['id'])
                        allow, decision = benchmark_permission(settings,
                            'scoped_edit' if task['role']=='builder' else 'safe_read', True)
                        app.store.event('benchmark_permission_decision', json.dumps(decision,ensure_ascii=False),task['id'],job['id'])
                        app.store.update(task['id'],benchmark_permission_handoff=permission_handoff(decision))
                        if not allow:
                            app.store.update(task['id'],status='blocked',summary='計測用許可判定で停止。利用者の承認は要求しない。')
                            app.store.update(job['id'],status='blocked')
                            app.engine.cancel_job(job['id'])
                            break
                app.engine.paused = False
                app.engine.tick()
                status = app.store.get(job['id'])['status']
                if status not in ('running', 'planning') and not app.engine.active:
                    break
                time.sleep(.5)
            else:
                app.engine.cancel_job(job['id'])
            app.engine.paused = True
            for _ in range(40):
                if not app.engine.active:
                    break
                time.sleep(.25)
            row = summarize(app, job, name, time.monotonic()-started)
            if row['status'] == 'awaiting_acceptance':
                checks=acceptance(root,name)
                row['independent_acceptance'] = all(c['passed'] for c in checks)
                if row['independent_acceptance']:
                    for approval in app.store.all('approval'):
                        if approval['job_id']==job['id'] and approval['kind']=='completion' and approval['status']=='pending':
                            app.engine.decide(approval['id'], True,
                                '計測用ハーネスの事前承認条件に基づく機械判定。固定受入・プロジェクトテスト成功。実機UI確認はNOT TESTED。')
                    app.store.update(job['id'], acceptance_by='benchmark_fixed_tests', real_ui_tested=False)
                    row['status']=app.store.get(job['id'])['status']
                    row['acceptance_by']='benchmark_fixed_tests'
            for approval in app.store.all('approval'):
                if approval['job_id']==job['id'] and approval['status']=='pending' and approval['kind']!='completion':
                    app.store.update(approval['id'], status='expired', note='計測用固定仕様のため人へ判断を依頼せず、停止として記録。')
            if row['status'] in ('blocked','failed','interrupted'):
                app.store.update(job['id'], benchmark_outcome=row['status'], no_manual_decision_required=True)
                app.engine.cancel_job(job['id'])
            row['acceptance_suites']=[{k:v for k,v in c.items() if k!='summary'} for c in checks]
            rows.append(row)
            (root/'results.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({k:v for k,v in row.items() if k not in ('calls','decisions')}, ensure_ascii=False), flush=True)
            if any(code in ('codex_sandbox', 'claude_auth') for code in row['failure_codes']):
                print('Shared environment failure: remaining arms not executed; no effectiveness conclusion.', flush=True)
                break
    finally:
        app.engine.shutdown.set()
        for ctx in list(app.engine.active.values()):
            ctx.cancel.set()
        deadline=time.monotonic()+30
        while app.engine.active and time.monotonic()<deadline:
            time.sleep(.1)
        server.shutdown()
        server.server_close()
    return rows


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--port', type=int, default=8791)
    parser.add_argument('--order', nargs=3, choices=list(ARMS), default=list(ARMS))
    parser.add_argument('--repetitions', type=int, choices=(1,2,3), default=1)
    args = parser.parse_args()
    if len(set(args.order))!=3:
        parser.error('各条件を1回ずつ指定してください。')
    for repetition in range(args.repetitions):
        root = create_run()
        order=args.order if repetition%2==0 else list(reversed(args.order))
        manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
        manifest.update(order=order, repetition=repetition+1)
        (root/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        print('Prepared '+str(root), flush=True)
        if args.run:
            rows=run(root, args.port, order)
            from benchmark_report import write_report
            print('Report: '+str(write_report(root)),flush=True)
            if len(rows)!=3 or any(r.get('independent_acceptance') is not True for r in rows):
                print('Incomplete or unequal quality: repeated measurement stopped.',flush=True)
                break
