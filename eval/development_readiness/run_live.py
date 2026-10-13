"""Explicitly budgeted synthetic live evaluation. Run only with user-authorized AI budget."""
import argparse
import hashlib
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from server import make_server
from team_config import default_config
from team_optimization_metrics import attempts
from eval.development_readiness.evaluate import prepare

GOAL = ('架空の在庫集計CLIを開発します。1人の制作担当で inventory.py、stock_cli.py、README.md の3ファイルを作成し、'
        '通常とセキュリティのレビューへ進めてください。inventory.low_stock(records, threshold)はitemとquantityを持つ辞書のリストを受け、'
        '同名を合算し、合計がしきい値以下の名前をソートして返します。負数をValueErrorにし、入力を変更しません。空リストは[]です。'
        'stock_cli.py は標準ライブラリのみで CSVパスと整数しきい値を引数に受けます。CSVのitem,quantityを読み、結果を1行1項目で表示。'
        '負数、不正整数、不足列、ファイルなしを説明付きエラーと非0終了にし、ヘッダーだけなら空出力で正常終了。'
        '日本語名と重複を扱い、入力CSVは変更しません。READMEに実行例とエラーを説明してください。'
        '対象3ファイル以外の変更、外部通信、インストール、削除、シェル、生成コードの実行は禁止。'
        'テストは評価側が制作後に実施しEVALUATION_CHECKS.mdへ記録します。レビューはその証拠を確認してください。'
        '未確認を確認済みにせず、受領は実利用者へ残してください。')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--approved-budget', type=int, required=True, choices=[200000])
    parser.add_argument('--port', type=int, default=8813)
    args = parser.parse_args()
    base = args.base.resolve()
    base.mkdir(parents=True, exist_ok=False)
    review, development, data = base/'review', base/'development', base/'runtime'
    prepare(review)
    development.mkdir()
    data.mkdir()
    for project in (review, development):
        text = ('Synthetic evaluation only. Read inside this project only. No shell, network, installs, deletion, '
                'credentials, or external files. ' + ('Read only, never edit files.' if project == review else
                'Create/edit inventory.py, stock_cli.py, README.md only. Independent checks are supplied by the evaluator.'))
        for name in ('AGENTS.md', 'CLAUDE.md', 'SCOPE.md'):
            (project/name).write_text(text, encoding='utf-8')
    config = default_config()
    config.update(approved_roots=[str(review), str(development)], max_parallel_projects=1,
                  automatic_operations=False, task_timeout_seconds=300, approval_timeout_seconds=240,
                  max_repairs=0, routing_evaluation={'enabled': True, 'root': str(base)},
                  review_context_mode='off', context_budget={'mode': 'off', 'reuse_chars': 8000})
    config['local_summary']['mode'] = 'off'
    config['context_optimization'] = {'mode': 'off'}
    config['provider_settings']['enabled'] = ['claude']
    for role in config['roles']:
        config['roles'][role] = 'claude-standard'
    (data/'config.json').write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
    manifest = {'budget': args.approved_budget, 'stop_new_at': 130000, 'cancel_at': 160000,
                'provider': 'claude', 'model': 'sonnet', 'effort': 'medium', 'gemini_used': False,
                'human_acceptance': 'pending', 'results': [],
                'source_hashes': {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
                                  for name in ('team_engine.py', 'team_adapters.py', 'team_store.py', 'server.py')}}
    (base/'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    server = make_server(data, args.port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    engine, store = server.app.engine, server.app.store
    # No background worker or update-monitor loops. Only the bounded tick below starts work.
    def usage():
        rows, _, _ = attempts(data/'team.sqlite3')
        total = sum((r.get('input_total') or 0)+(r.get('output') or 0) for r in rows)
        unknown = sum(r['measurement'] in ('unknown', 'partial') for r in rows
                      if r.get('status') not in ('running', 'starting'))
        return total, unknown
    try:
        for case, project, goal in (('review', review, (review/'REQUEST.md').read_text(encoding='utf-8')),
                                    ('development', development, GOAL)):
            total, unknown = usage()
            if total >= manifest['stop_new_at'] or unknown:
                print('STOP_BEFORE_CASE', case, total, unknown, flush=True)
                break
            profile = {'adapter': 'claude', 'model': 'sonnet', 'effort': 'medium'}
            job = engine.create_job('架空在庫評価 '+case, goal, str(project), True,
                                    evaluation_mode='A', evaluation_confirmed=True,
                                    role_profiles={r: profile for r in ('planner', 'builder', 'researcher', 'reviewer')})
            if case == 'review':
                # Isolated reviewer capability probe, explicitly not a normal end-to-end workflow.
                for task in store.for_job('task', job['id']): store.update(task['id'], status='cancelled')
                store.update(job['id'], status='running')
                engine.new_task(job, '既知欠陥の独立レビュー', goal, 'reviewer')
            start, last, reason, waiting_checks = time.monotonic(), None, '', False
            while True:
                current = store.get(job['id'])
                tasks = store.for_job('task', job['id'])
                total, unknown = usage()
                if current['status'] != last:
                    print('STATUS', case, current['status'], 'tokens', total, flush=True)
                    last = current['status']
                if total >= manifest['cancel_at'] or time.monotonic()-start > 900:
                    reason = 'budget_or_time_stop'
                    engine.cancel_job(job['id'])
                    break
                if current['status'] in ('blocked', 'failed', 'cancelled', 'interrupted', 'awaiting_acceptance'):
                    reason = current['status']
                    break
                if not engine.active:
                    if unknown or total >= manifest['stop_new_at']:
                        reason = 'unknown_usage_or_budget_stop'
                        engine.cancel_job(job['id'])
                        break
                    builders = [t for t in tasks if t['role']=='builder']
                    builders_done = builders and all(t['status']=='succeeded' for t in builders)
                    if case == 'development' and builders_done and not (project/'EVALUATION_CHECKS.md').exists():
                        if not waiting_checks:
                            print('WAITING_INDEPENDENT_CHECKS', flush=True)
                            waiting_checks = True
                        time.sleep(2)
                        continue
                    engine.tick()
                pending = [a for a in store.for_job('approval', job['id']) if a['status']=='pending' and a['kind']!='completion']
                if pending:
                    print('NEEDS_DECISION', case, [a['kind'] for a in pending], flush=True)
                    reason = 'needs_human_decision'
                    engine.cancel_job(job['id'])
                    break
                time.sleep(1)
            deadline = time.monotonic()+45
            while engine.active and time.monotonic()<deadline: time.sleep(.5)
            if engine.active: raise RuntimeError('Worker cancellation not confirmed')
            total, unknown = usage()
            manifest['results'].append({'case': case, 'job_id': job['id'], 'status': store.get(job['id'])['status'],
                                        'stop_reason': reason, 'cumulative_tokens': total, 'unknown_runs': unknown})
            (base/'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
            print('RESULT', case, reason, total, unknown, flush=True)
            if unknown: break
    finally:
        engine.shutdown.set()
        for context in list(engine.active.values()): context.cancel.set()
        server.shutdown()
        server.server_close()
        if not engine.active:
            engine.handoff.close()
            store.db.close()
            server.app.data_lock.close()
        print('EVALUATION_STOPPED', flush=True)


if __name__ == '__main__':
    main()
