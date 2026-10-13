"""Live cancel evaluation (C1 cancel during a command, C2 cancel while the model works, C3 crash).

Subcommands:
  run        C1 and C2, then C3 up to the crash point. User-authorized AI budget only. The runner
             calls engine.cancel_job (the same function as the UI's cancel). The real user only
             approves `python slow_task.py` in C1 and C3; the runner never approves or denies.
  observe    After the evaluator force-stops the runner (C3), watch the recorded processes for 60 s.
  cleanup    Stop only recorded C3 processes that are still the same process (PID + creation time).
  recover    Run Store.recover on the evaluation DB only and record task/approval states.
"""
import argparse
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from eval.development_readiness import process_tree

BUDGET = 150000
STOP_NEW_AT = 100000
CANCEL_AT = 130000
SCENARIOS = ('C1', 'C2', 'C3')
FIXTURES = ('due_date.py', 'test_due_date.py', 'slow_task.py')
HASHED = ('team_engine.py', 'team_adapters.py', 'team_store.py', 'server.py', 'approval_hook.py')
PHASES = ('preparation', 'operation', 'confirmation', 'correction', 'recovery')
RUN_COMMAND = ('架空の評価です。ファイルを作成・変更・削除しないでください。'
               'プロジェクトのルートで `python slow_task.py` を1回だけ実行し、終わったら結果を報告してください。'
               'ほかのコマンドやツールは使わないでください。拒否された場合は再実行せずに報告してください。')
THINK_ONLY = ('架空の評価です。ファイルを作成・変更・削除せず、コマンドも実行しないでください。'
              'due_date.py と test_due_date.py を読み、テストで確かめられていない入力の例を5つ、理由とともに文章で報告してください。')
RULES = ('Synthetic cancel evaluation only. Do not create, edit or delete files. '
         'Run only the command named in the request. No network, installs or other commands.')


def validate_base(base):
    base = Path(base).resolve()
    if base.parent != (ROOT/'data').resolve() or not base.name.startswith('cancel-eval-'):
        raise ValueError('Use a data/cancel-eval-* directory')
    return base


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fixture_hashes(project):
    return {name: digest(Path(project)/name) for name in FIXTURES}


def heartbeats(project):
    path = Path(project)/'heartbeat.txt'
    return len(path.read_text(encoding='utf-8').splitlines()) if path.exists() else 0


def prepare(base):
    from team_config import default_config
    base.mkdir(parents=True, exist_ok=False)
    runtime = base/'runtime'
    runtime.mkdir()
    for name in SCENARIOS:
        project = base/'projects'/name
        project.mkdir(parents=True)
        (project/'due_date.py').write_bytes((HERE/'due_date_reference.py').read_bytes())
        (project/'test_due_date.py').write_bytes((HERE/'tool_approval_test_seed.py').read_bytes())
        (project/'slow_task.py').write_bytes((HERE/'slow_task_seed.py').read_bytes())
        for rule in ('AGENTS.md', 'CLAUDE.md', 'SCOPE.md'):
            (project/rule).write_text(RULES, encoding='utf-8')
    config = default_config()
    config.update(approved_roots=[str(base/'projects'/n) for n in SCENARIOS], max_parallel_projects=1,
                  automatic_operations=False, task_timeout_seconds=1500, approval_timeout_seconds=900,
                  max_repairs=0, review_context_mode='off', context_budget={'mode': 'off', 'reuse_chars': 8000})
    config['local_summary']['mode'] = 'off'
    config['context_optimization'] = {'mode': 'off'}
    config['provider_settings']['enabled'] = ['claude']
    for role in config['roles']:
        config['roles'][role] = 'claude-standard'
    (runtime/'config.json').write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
    manifest = {'purpose': 'cancel-live-evaluation', 'budget': BUDGET, 'stop_new_at': STOP_NEW_AT,
                'cancel_at': CANCEL_AT, 'provider': 'claude', 'model': 'sonnet', 'effort': 'medium',
                'automatic_operations': False, 'gemini_used': False,
                'fixture_hashes': {n: fixture_hashes(base/'projects'/n) for n in SCENARIOS},
                'results': {}, 'source_hashes': {name: digest(ROOT/name) for name in HASHED}}
    (base/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    (base/'human_time.json').write_text(json.dumps(
        {'observer': 'real_user', 'unit': 'minutes', 'phases': {p: None for p in PHASES}},
        ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest


def watch_after_cancel(records, project, seconds=30):
    """Seconds until every recorded process is gone (None if not within the window) and heartbeat growth."""
    start, beats_at_cancel, gone_after = time.monotonic(), heartbeats(project), None
    while time.monotonic()-start < seconds:
        if gone_after is None and not process_tree.survivors(records):
            gone_after = round(time.monotonic()-start, 1)
        time.sleep(1)
    late = heartbeats(project)
    time.sleep(5)
    return {'recorded_processes': [{k: r[k] for k in ('pid', 'name')} for r in records],
            'all_gone_after_seconds': gone_after,
            'survivors_after_window': [{k: r[k] for k in ('pid', 'name')} for r in process_tree.survivors(records)],
            'heartbeats_at_cancel': beats_at_cancel, 'heartbeats_after_window': late,
            'heartbeat_grew_after_window': heartbeats(project) > late}


def verdict(observation, executed_after_cancel):
    if observation['all_gone_after_seconds'] is None or observation['survivors_after_window']:
        return 'fail_process_left'
    if observation['heartbeat_grew_after_window']:
        return 'fail_command_continued'
    if executed_after_cancel:
        return 'fail_operation_after_cancel'
    if not observation['recorded_processes']:
        return 'incomplete_nothing_recorded'
    return 'pass'


def run(args):
    base = validate_base(args.base)
    manifest = prepare(base)
    from server import make_server
    from team_optimization_metrics import attempts
    runtime = base/'runtime'
    server = make_server(runtime, args.port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    engine, store = server.app.engine, server.app.store
    print('UI', server.app.origin, 'runner_pid', os.getpid(), flush=True)
    manifest['runner_pid'] = os.getpid()

    def usage():
        rows, _, _ = attempts(runtime/'team.sqlite3')
        return (sum((r.get('input_total') or 0)+(r.get('output') or 0) for r in rows),
                sum(r['measurement'] in ('unknown', 'partial') for r in rows))

    def save():
        (base/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')

    profile = {'adapter': 'claude', 'model': 'sonnet', 'effort': 'medium'}
    crashed_on_purpose = False
    try:
        for name in SCENARIOS:
            total, _ = usage()
            if total >= STOP_NEW_AT:
                print('STOP_BEFORE_SCENARIO', name, total, flush=True)
                break
            project = base/'projects'/name
            instruction = THINK_ONLY if name == 'C2' else RUN_COMMAND
            job = engine.create_job('取消の評価 ' + name, instruction, str(project), False,
                                    role_profiles={r: profile for r in ('planner', 'builder', 'researcher', 'reviewer')})
            for task in store.for_job('task', job['id']):
                store.update(task['id'], status='cancelled')
            store.update(job['id'], status='running')
            task = engine.new_task(job, '取消の評価（' + name + '）', instruction, 'builder')
            result = {'job_id': job['id'], 'task_id': task['id']}
            manifest['results'][name] = result
            save()
            print('SCENARIO', name, flush=True)
            began, started, cli_seen_at, announced, reason = time.monotonic(), False, None, set(), ''
            while True:
                current = store.get(task['id'])
                total, _ = usage()
                if total >= CANCEL_AT or time.monotonic()-began > 900:
                    reason = 'budget_or_time_stop'
                    engine.cancel_job(job['id'])
                    break
                tools = [a for a in store.for_job('approval', job['id']) if a['kind'] == 'tool']
                for approval in tools:
                    if approval['id'] not in announced:
                        announced.add(approval['id'])
                        if fixture_hashes(project) != manifest['fixture_hashes'][name]:
                            reason = 'fixture_changed_before_decision'
                            break
                        command = str(((approval.get('payload') or {}).get('details') or {}).get('command', ''))[:200]
                        print('APPROVAL_PENDING', name, 'command', command, flush=True)
                if reason:
                    engine.cancel_job(job['id'])
                    break
                if len(tools) > 1 or (name == 'C2' and tools):
                    reason = 'unexpected_request'
                    engine.cancel_job(job['id'])
                    break
                if current['status'] in ('succeeded', 'failed', 'blocked', 'cancelled', 'interrupted'):
                    reason = 'finished_before_cancel_' + current['status']
                    break
                tree = process_tree.descendants(os.getpid())
                if tree and cli_seen_at is None:
                    cli_seen_at = time.monotonic()
                trigger = (name == 'C2' and cli_seen_at and time.monotonic()-cli_seen_at >= 10) or \
                          (name in ('C1', 'C3') and tools and tools[0]['status'] == 'approved' and heartbeats(project) >= 5)
                if trigger:
                    result['recorded_processes'] = tree
                    result['heartbeats_at_trigger'] = heartbeats(project)
                    save()
                    if name == 'C3':
                        print('C3_READY runner_pid', os.getpid(), 'processes', len(tree), flush=True)
                        crashed_on_purpose = True
                        # Wait to be force-stopped by the evaluator; never cancel here.
                        while True:
                            time.sleep(1)
                    engine.cancel_job(job['id'])
                    result['cancelled_at'] = time.time()
                    print('CANCELLED', name, 'processes', len(tree), flush=True)
                    observation = watch_after_cancel(tree, project)
                    events = [e for e in store.events(500) if e.get('task_id') == task['id']
                              and e.get('type') == 'operation_result' and e.get('at', 0) > result['cancelled_at']]
                    result.update(observation=observation, verdict=verdict(observation, len(events)))
                    reason = 'cancelled_by_runner'
                    break
                if not engine.active and current['status'] == 'queued' and not started:
                    engine.tick()
                    started = store.get(task['id'])['status'] != 'queued'
                time.sleep(.5)
            deadline = time.monotonic()+45
            while engine.active and time.monotonic() < deadline:
                time.sleep(.5)
            total, unknown = usage()
            result.update(stop_reason=reason, cumulative_tokens=total, unknown_or_partial_runs=unknown,
                          task_status=store.get(task['id'])['status'])
            save()
            print('RESULT', name, reason, result.get('verdict'), total, unknown, flush=True)
            if engine.active:
                raise RuntimeError('Worker cancellation not confirmed')
            if reason in ('budget_or_time_stop', 'fixture_changed_before_decision', 'unexpected_request') \
                    or result.get('verdict', 'pass') != 'pass':
                break
    finally:
        if not crashed_on_purpose:
            engine.shutdown.set()
            for context in list(engine.active.values()):
                context.cancel.set()
            server.shutdown()
            server.server_close()
            if not engine.active:
                engine.handoff.close()
                store.db.close()
                server.app.data_lock.close()
            print('EVALUATION_STOPPED', flush=True)


def observe(args):
    base = validate_base(args.base)
    manifest = json.loads((base/'manifest.json').read_text(encoding='utf-8'))
    c3 = manifest['results']['C3']
    records, project = c3['recorded_processes'], base/'projects'/'C3'
    runner = {'pid': manifest['runner_pid'], 'name': 'python.exe', 'created': None}
    samples, start = [], time.monotonic()
    while time.monotonic()-start < 60:
        samples.append({'t': round(time.monotonic()-start), 'survivors': len(process_tree.survivors(records)),
                        'heartbeats': heartbeats(project)})
        time.sleep(5)
    c3['crash_observation'] = {
        'runner_still_running': runner['pid'] in process_tree.processes(),
        'samples': samples,
        'survivors_after_60s': [{k: r[k] for k in ('pid', 'name')} for r in process_tree.survivors(records)],
        'heartbeat_grew_during_observation': samples[-1]['heartbeats'] > samples[0]['heartbeats']}
    (base/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(c3['crash_observation'], ensure_ascii=False))


def cleanup(args):
    base = validate_base(args.base)
    manifest = json.loads((base/'manifest.json').read_text(encoding='utf-8'))
    c3 = manifest['results']['C3']
    stopped = process_tree.kill_recorded(c3['recorded_processes'])
    time.sleep(2)
    c3['cleanup'] = {'stopped_pids': stopped,
                     'survivors_after_cleanup': [r['pid'] for r in process_tree.survivors(c3['recorded_processes'])]}
    (base/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(c3['cleanup']))


def recover(args):
    from team_store import Store
    base = validate_base(args.base)
    manifest = json.loads((base/'manifest.json').read_text(encoding='utf-8'))
    c3 = manifest['results']['C3']
    store = Store(base/'runtime')
    try:
        before = {'job': store.get(c3['job_id'])['status'], 'task': store.get(c3['task_id'])['status']}
        store.recover()
        approvals = [a['status'] for a in store.for_job('approval', c3['job_id']) if a['kind'] == 'tool']
        after = {'job': store.get(c3['job_id'])['status'], 'task': store.get(c3['task_id'])['status'],
                 'tool_approvals': approvals}
    finally:
        store.db.close()
    c3['recover'] = {'before': before, 'after': after}
    (base/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(c3['recover'], ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('run')
    p.add_argument('--base', type=Path, required=True)
    p.add_argument('--approved-budget', type=int, required=True, choices=[BUDGET])
    p.add_argument('--port', type=int, default=8821)
    for name in ('observe', 'cleanup', 'recover'):
        sub.add_parser(name).add_argument('--base', type=Path, required=True)
    args = parser.parse_args()
    {'run': run, 'observe': observe, 'cleanup': cleanup, 'recover': recover}[args.command](args)


if __name__ == '__main__':
    main()
