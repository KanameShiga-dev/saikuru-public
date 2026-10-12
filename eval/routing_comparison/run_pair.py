"""Run one isolated full/light synthetic canary. No production DB or project is opened."""
import ast
import hashlib
import http.cookiejar
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from server import make_server
from team_config import default_config
from team_optimization_metrics import attempts
from eval.routing_comparison.review_sample_metrics import reviewer_prompts

BASE = ROOT / 'data' / ('routing-comparison-20261012-' + os.environ.get('ROUTING_EVAL_RUN', 'validated3'))
DATA = BASE / 'runtime'
PORT = int(os.environ.get('ROUTING_EVAL_PORT', '8802'))
LIMIT = int(os.environ.get('ROUTING_EVAL_LIMIT', '2000000'))
PREVIOUS_MEASURED = int(os.environ.get('ROUTING_EVAL_PREVIOUS', '914953'))
STOP_NEW_AT = int(os.environ.get('ROUTING_EVAL_STOP_NEW_AT', '1750000'))  # Margin for the active call.
ORDER = tuple(os.environ.get('ROUTING_EVAL_ORDER', 'full,light_eval').split(','))
if sorted(ORDER) != ['full', 'light_eval'] and not (ORDER == ('full',)
        and os.environ.get('ROUTING_EVAL_SINGLE_ROUTE') == 'full'):
    raise ValueError('ROUTING_EVAL_ORDER must contain full and light_eval, or explicit full-only sample')
if not 0 <= PREVIOUS_MEASURED < STOP_NEW_AT < LIMIT:
    raise ValueError('The measured stop threshold must leave room below the approved limit')
CASE = os.environ.get('ROUTING_EVAL_CASE', 'calculator')
if CASE not in ('calculator', 'labels'):
    raise ValueError('ROUTING_EVAL_CASE must be calculator or labels')
REVIEW_CONTEXT_MODE = os.environ.get('ROUTING_REVIEW_CONTEXT_MODE', 'off')
if REVIEW_CONTEXT_MODE not in ('off', 'shadow', 'on'):
    raise ValueError('ROUTING_REVIEW_CONTEXT_MODE must be off, shadow or on')
if REVIEW_CONTEXT_MODE != 'off' and any(key not in os.environ for key in
                                        ('ROUTING_EVAL_RUN', 'ROUTING_EVAL_LIMIT', 'ROUTING_EVAL_PREVIOUS',
                                         'ROUTING_EVAL_STOP_NEW_AT')):
    raise ValueError('Review-context comparison requires an explicit run and fresh approved budget values')
if CASE == 'labels':
    from eval.routing_comparison import case_labels
    FILE, INITIAL = case_labels.FILE, case_labels.INITIAL
else:
    FILE, INITIAL = 'calculator.py', 'def total(values):\n    return sum(values[1:])\n'
GOAL = ('架空の開発評価です。calculator.py だけを変更してください。total(values) が先頭の値を落とす不具合を直し、'
        '空配列では0を返してください。負数と1件の入力にも対応し、入力を変更しません。標準Pythonのみを使います。'
        '1人の制作担当で修正し、通常レビューとセキュリティレビューへ進めてください。'
        '外部通信、別ファイルの編集、削除、インストール、生成コードやテストの実行は禁止です。'
        'プロジェクト内の読み取りだけを許可します。制作後、評価側が独立採点を実施し、'
        'EVALUATION_CHECKS.md に結果を記録します。'
        'レビュー担当はこの証拠を読み、採点の未実施を推測せずに判断してください。'
        '予定済みの後続レビュー工程そのものは未確認の必須検証へ列挙しません。'
        '追加質問が必要なら停止してください。')
ACCEPTANCE = '空配列で0、[4]で4、[3, 5, -2]で6、[-5, -1]で-6を返し、入力リストを変更しない。'
if CASE == 'labels':
    GOAL, ACCEPTANCE = case_labels.GOAL, case_labels.ACCEPTANCE
PROFILE = {'adapter': 'claude', 'model': 'sonnet', 'effort': 'medium'}


def prepare():
    BASE.mkdir(parents=True, exist_ok=False)
    DATA.mkdir()
    projects = {}
    for route in ('full', 'light_eval'):
        project = BASE / route
        project.mkdir()
        (project / FILE).write_text(INITIAL, encoding='utf-8')
        text = ('Synthetic routing comparison. Read only inside this folder. Edit ' + FILE + ' only. '
                'No network, installs, deletion, shell execution of generated code, or external files. '
                'The evaluation harness checks the result independently.\n')
        for name in ('AGENTS.md', 'CLAUDE.md', 'SCOPE.md'):
            (project / name).write_text(text, encoding='utf-8')
        projects[route] = str(project)
    config = default_config()
    config.update(approved_roots=list(projects.values()), routing_evaluation={'enabled': True, 'root': str(BASE)},
                  max_parallel_projects=1, automatic_operations=False, task_timeout_seconds=180,
                  approval_timeout_seconds=180, max_repairs=1,
                  review_context_mode=REVIEW_CONTEXT_MODE,
                  context_budget={'mode': 'off', 'reuse_chars': 8000})
    config['local_summary']['mode'] = 'off'
    config['context_optimization'] = {'mode': 'off'}
    for role in config['roles']:
        config['roles'][role] = 'claude-standard'
    (DATA / 'config.json').write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    manifest = {'purpose': 'isolated full/light one-pair canary', 'routes': list(ORDER),
                'model': PROFILE, 'max_total_tokens': LIMIT, 'stop_new_at': STOP_NEW_AT, 'order': ORDER,
                'previous_measured_tokens': PREVIOUS_MEASURED,
                'case': CASE, 'review_context_mode': REVIEW_CONTEXT_MODE,
                'goal_sha256': hashlib.sha256(GOAL.encode()).hexdigest(),
                'fixture_sha256': hashlib.sha256((BASE / 'full' / FILE).read_bytes()).hexdigest(),
                'results': []}
    (BASE / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return projects, manifest


def quality(project):
    if CASE == 'labels':
        return case_labels.quality(project)
    path = Path(project) / FILE
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'))
        forbidden = (ast.Import, ast.ImportFrom, ast.ClassDef, ast.With, ast.AsyncWith, ast.While,
                     ast.Global, ast.Nonlocal, ast.Lambda)
        if any(isinstance(node, forbidden) or isinstance(node, ast.Attribute)
               or isinstance(node, ast.Name) and node.id.startswith('__') for node in ast.walk(tree)):
            return False
        namespace = {'__builtins__': {'sum': sum, 'len': len, 'range': range, 'enumerate': enumerate}}
        exec(compile(tree, str(path), 'exec'), namespace)
        fn = namespace['total']
        checks = [([], 0), ([4], 4), ([3, 5, -2], 6), ([-5, -1], -6)]
        for values, expected in checks:
            original = values[:]
            if fn(values) != expected or values != original:
                return False
        return True
    except Exception:
        return False


def token_total(database):
    rows, _, _ = attempts(database)
    return PREVIOUS_MEASURED + sum((r.get('input_total') or 0) + (r.get('output') or 0) for r in rows)


def main():
    projects, manifest = prepare()
    server = make_server(DATA, PORT)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    jar = http.cookiejar.CookieJar()
    client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    client.open(f'http://127.0.0.1:{PORT}/', timeout=30).close()

    def api(path, body=None):
        request = urllib.request.Request(f'http://127.0.0.1:{PORT}' + path,
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers={'Content-Type': 'application/json', 'X-Agent-Team-UI': '1'})
        with client.open(request, timeout=40) as response:
            return json.load(response)

    try:
        for route in ORDER:
            if token_total(DATA / 'team.sqlite3') >= STOP_NEW_AT:
                print('BUDGET_STOP before', route, flush=True)
                break
            payload = {'title': '架空の文字列修正' if CASE == 'labels' else '架空の合計修正', 'goal': GOAL, 'project': projects[route],
                       'kind': 'development', 'consent': True, 'auto_execute': True,
                       'evaluation_mode': 'A', 'evaluation_confirmed': True,
                       'role_profiles': {role: PROFILE for role in ('planner', 'builder', 'researcher', 'reviewer')}}
            if route == 'light_eval':
                payload.update(route='light_eval', light_scope_file=FILE, light_acceptance=ACCEPTANCE)
            job = api('/api/jobs', payload)
            print('SUBMITTED', route, job['id'], flush=True)
            started = time.time()
            last_status = None
            independent = None
            evidence_written = False
            while True:
                state = api('/api/state')
                current = next(item for item in state['jobs'] if item['id'] == job['id'])
                status = current['status']
                if not evidence_written and any(task['job_id'] == job['id'] and task['role'] == 'builder'
                                                and task['status'] == 'succeeded' for task in state['tasks']):
                    independent = quality(projects[route])
                    (Path(projects[route]) / 'EVALUATION_CHECKS.md').write_text(
                        '# 独立採点結果（評価ランナー）\n\n'
                        + ('判定: 合格\n' if independent else '判定: 不合格\n')
                        + ('確認: 空配列、前後空白、空白のみ、重複、入力リスト不変。\n' if CASE == 'labels'
                           else '確認: 空配列、1件、正負混合、負数のみ、入力リスト不変。\n')
                        + '対象: ' + FILE + '\n',
                        encoding='utf-8')
                    server.app.store.event('evaluation_check', '分離評価側の独立採点を記録しました。', job_id=job['id'])
                    evidence_written = True
                    print('INDEPENDENT_CHECK', route, independent, flush=True)
                server.app.engine.tick()
                if status != last_status:
                    print('STATUS', route, status, flush=True)
                    last_status = status
                for approval in state['approvals']:
                    if approval['job_id'] != job['id'] or approval['status'] != 'pending':
                        continue
                    if approval['kind'] == 'completion':
                        independent = quality(projects[route])
                        # Synthetic evaluation proxy; record separately from real human acceptance.
                        if independent:
                            api('/api/decide', {'id': approval['id'], 'allow': True,
                                                'note': 'Synthetic independent checker passed.'})
                        else:
                            print('QUALITY_FAIL', route, flush=True)
                            break
                    elif approval['kind'] == 'tool':
                        api('/api/decide', {'id': approval['id'], 'allow': False,
                                            'note': 'Synthetic comparison allows only its predefined scope.'})
                    elif approval['kind'] in ('question', 'plan'):
                        print('NEEDS_HUMAN_DECISION', route, approval['kind'], flush=True)
                        break
                measured = token_total(DATA / 'team.sqlite3')
                if measured >= STOP_NEW_AT or time.time() - started > 900:
                    for context in list(server.app.engine.active.values()):
                        if context.task['job_id'] == job['id']:
                            context.cancel.set()
                    print('STOP_LIMIT', route, measured, flush=True)
                    break
                if status in ('accepted', 'accepted_with_pending_checks', 'blocked', 'failed', 'cancelled', 'interrupted'):
                    break
                time.sleep(3)
            while any(context.task['job_id'] == job['id'] for context in server.app.engine.active.values()):
                time.sleep(1)
            rows, _, _ = attempts(DATA / 'team.sqlite3')
            used = [r for r in rows if r['job_id'] == job['id']]
            result = {'route': route, 'job_id': job['id'], 'status': server.app.store.get(job['id'])['status'],
                      'independent_pass': independent, 'tokens_measured': sum((r.get('input_total') or 0) + (r.get('output') or 0) for r in used),
                      'unknown_usage': sum(r['measurement'] == 'unknown' for r in used),
                      'attempts': len(used), 'roles': [r['role'] for r in used],
                      'reviewer_prompts': reviewer_prompts(server.app.store, job['id']),
                      'seconds': round(time.time() - started, 1)}
            manifest['results'].append(result)
            (BASE / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
            print('RESULT', json.dumps(result, ensure_ascii=False), flush=True)
            if result['status'] not in ('accepted', 'accepted_with_pending_checks') or result['unknown_usage']:
                break
    finally:
        server.app.engine.shutdown.set()
        for context in list(server.app.engine.active.values()):
            context.cancel.set()
        server.shutdown()
        server.server_close()
        server.app.store.db.close()
        server.app.engine.handoff.close()
        server.app.data_lock.close()
        print('EVALUATION_SERVER_STOPPED', flush=True)


if __name__ == '__main__':
    main()
