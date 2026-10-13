"""Budgeted live evaluation for synthetic-due-date-v1. Run only with a user-authorized AI budget.

Only the bounded loop below starts work. After every successful builder run the loop waits for
the evaluator to inspect the code and record checks with check_due_date.py --round N.
Acceptance is left to the real user (accept_due_date.py); this runner never accepts.
"""
import argparse
import hashlib
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

CASE_ID = 'synthetic-due-date-v1'
BUDGET = 300000
STOP_NEW_AT = 195000   # 65%: no new task starts at or above this measured total.
CANCEL_AT = 240000     # 80%: cancel the running job. Usage notifications lag, so this is not exact.
HASHED = ('team_engine.py', 'team_adapters.py', 'team_store.py', 'team_handoff.py',
          'team_evaluation.py', 'server.py', 'web/app.js')
PHASES = ('preparation', 'operation', 'confirmation', 'correction', 'recovery')
GOAL = ('架空の図書室の返却期限を扱う小さな修正です。対象は due_date.py と README.md の2ファイルだけです。'
        'due_date(start, days, closed_weekdays) は、start（datetime.date）から days 日後の期限を返します。'
        'closed_weekdays は date.weekday() の番号（月曜0〜日曜6）の集まりです。'
        '期限が休館日に当たる場合は、次の開館日まで後ろへ送ってください。連続する休館日、月末・年末をまたぐ場合、days=0 も同じ規則です。'
        '負の days と、全曜日が休館の指定は ValueError のままにします。引数の closed_weekdays を変更しないでください。'
        'README.md に使用例と制約（休館日の扱い、エラーになる入力）を追記してください。'
        '2ファイル以外の変更、外部通信、インストール、削除、シェル、生成コードの実行は禁止です。'
        'テストは評価側が制作後に実施し EVALUATION_CHECKS.md へ記録します。レビューはその証拠を確認してください。'
        '未確認を確認済みにせず、受領は実利用者へ残してください。')
RULES = ('Synthetic evaluation only. Read inside this project only. No shell, network, installs, deletion, '
         'credentials, or external files. Edit due_date.py and README.md only. '
         'Independent checks are supplied by the evaluator in EVALUATION_CHECKS.md.')


def validate_base(base):
    base = Path(base).resolve()
    if base.parent != (ROOT/'data').resolve() or not base.name.startswith('due-date-eval-'):
        raise ValueError('Use a new data/due-date-eval-* directory')
    return base


def prepare(base):
    """Create the isolated project, runtime config, manifest and blank human-time sheet. No AI."""
    from team_config import default_config
    base.mkdir(parents=True, exist_ok=False)
    project, runtime = base/'development', base/'runtime'
    project.mkdir()
    runtime.mkdir()
    (project/'due_date.py').write_bytes((HERE/'due_date_seed.py').read_bytes())
    (project/'README.md').write_bytes((HERE/'due_date_readme_seed.md').read_bytes())
    for name in ('AGENTS.md', 'CLAUDE.md', 'SCOPE.md'):
        (project/name).write_text(RULES, encoding='utf-8')
    config = default_config()
    config.update(approved_roots=[str(project)], max_parallel_projects=1, automatic_operations=False,
                  task_timeout_seconds=300, approval_timeout_seconds=240, max_repairs=1,
                  routing_evaluation={'enabled': True, 'root': str(base)}, review_context_mode='off',
                  context_budget={'mode': 'off', 'reuse_chars': 8000})
    config['local_summary']['mode'] = 'off'
    config['context_optimization'] = {'mode': 'off'}
    config['provider_settings']['enabled'] = ['claude']
    for role in config['roles']:
        config['roles'][role] = 'claude-standard'
    (runtime/'config.json').write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
    manifest = {'purpose': 'due-date-live-evaluation', 'case_id': CASE_ID, 'budget': BUDGET,
                'stop_new_at': STOP_NEW_AT, 'cancel_at': CANCEL_AT, 'provider': 'claude', 'model': 'sonnet',
                'effort': 'medium', 'route': 'full', 'max_repairs': 1, 'gemini_used': False,
                'separate_from_previous_200000_budget': True, 'human_acceptance': 'pending', 'results': [],
                'source_hashes': {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in HASHED}}
    (base/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    # Outside the AI project. Only the real user fills it; null means not measured, 0 means none.
    (base/'human_time.json').write_text(json.dumps(
        {'observer': 'real_user', 'unit': 'minutes', 'phases': {p: None for p in PHASES}},
        ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--approved-budget', type=int, required=True, choices=[BUDGET])
    parser.add_argument('--port', type=int, default=8818)
    args = parser.parse_args()
    base = validate_base(args.base)
    manifest = prepare(base)
    from server import make_server
    from team_optimization_metrics import attempts
    project, runtime = base/'development', base/'runtime'
    server = make_server(runtime, args.port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    engine, store = server.app.engine, server.app.store

    def usage():
        rows, _, _ = attempts(runtime/'team.sqlite3')
        total = sum((r.get('input_total') or 0)+(r.get('output') or 0) for r in rows)
        unknown = sum(r['measurement'] in ('unknown', 'partial') for r in rows
                      if r.get('status') not in ('running', 'starting'))
        return total, unknown

    def save():
        (base/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')

    job, reason = None, ''
    try:
        profile = {'adapter': 'claude', 'model': 'sonnet', 'effort': 'medium'}
        job = engine.create_job('架空の返却期限の修正', GOAL, str(project), True,
                                evaluation_mode='A', evaluation_confirmed=True,
                                role_profiles={r: profile for r in ('planner', 'builder', 'researcher', 'reviewer')})
        manifest['job_id'] = job['id']
        save()
        started, waited, last, waiting_round = time.monotonic(), 0.0, None, None
        while True:
            current = store.get(job['id'])
            tasks = store.for_job('task', job['id'])
            total, unknown = usage()
            if current['status'] != last:
                print('STATUS', current['status'], 'tokens', total, flush=True)
                last = current['status']
            # Waiting for the evaluator's checks is not AI time and is excluded from the time limit.
            if total >= CANCEL_AT or time.monotonic()-started-waited > 1800:
                reason = 'budget_or_time_stop'
                engine.cancel_job(job['id'])
                break
            if current['status'] in ('blocked', 'failed', 'cancelled', 'interrupted', 'awaiting_acceptance'):
                reason = current['status']
                break
            if not engine.active:
                if unknown or total >= STOP_NEW_AT:
                    reason = 'unknown_usage_or_budget_stop'
                    engine.cancel_job(job['id'])
                    break
                built = sum(t['role'] == 'builder' and t['status'] == 'succeeded' for t in tasks)
                if built and not (base/'checks'/f'round-{built}.json').exists():
                    if waiting_round != built:
                        print('WAITING_INDEPENDENT_CHECKS round', built, flush=True)
                        waiting_round = built
                    time.sleep(2)
                    waited += 2
                    continue
                engine.tick()
            pending = [a for a in store.for_job('approval', job['id'])
                       if a['status'] == 'pending' and a['kind'] != 'completion']
            if pending:
                print('NEEDS_DECISION', [a['kind'] for a in pending], flush=True)
                reason = 'needs_human_decision'
                engine.cancel_job(job['id'])
                break
            time.sleep(1)
        deadline = time.monotonic()+45
        while engine.active and time.monotonic() < deadline:
            time.sleep(.5)
        if engine.active:
            raise RuntimeError('Worker cancellation not confirmed')
    finally:
        total, unknown = usage()
        manifest['results'].append({'job_id': job and job['id'], 'status': job and store.get(job['id'])['status'],
                                    'stop_reason': reason or 'runner_error', 'cumulative_tokens': total,
                                    'unknown_runs': unknown})
        save()
        engine.shutdown.set()
        for context in list(engine.active.values()):
            context.cancel.set()
        server.shutdown()
        server.server_close()
        if not engine.active:
            engine.handoff.close()
            store.db.close()
            server.app.data_lock.close()
        print('EVALUATION_STOPPED', reason, total, unknown, flush=True)


if __name__ == '__main__':
    main()
