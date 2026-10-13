"""Resume only the security review of a budget-stopped due-date evaluation. User-authorized runs only.

Preconditions are checked read-only first: the run stopped on the new-work threshold, every earlier
task succeeded, only the security review is cancelled and the last independent check passed.
Exactly one task (the security review) may start. If it asks for changes, no repair work starts
and the job is cancelled again. The resume is recorded separately in the manifest.
"""
import argparse
import json
import sqlite3
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from eval.development_readiness.run_due_date import BUDGET, validate_base

RESUME_REFUSE_AT = 250000  # Do not resume when less than ~50k (one review) is left.
RESUME_CANCEL_AT = 280000
SECURITY = 'common-security-reviewer'
NOTE = '予算停止で未開始だったセキュリティレビューを、利用者の判断で再開する。追加の修正工程は行わない。'


def preflight(base):
    """Read-only check. Returns (job_id, security_task_id, measured_total)."""
    from team_optimization_metrics import attempts
    manifest = json.loads((base/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('purpose') != 'due-date-live-evaluation' or 'resume' in manifest:
        raise ValueError('Not a due-date evaluation, or it was already resumed')
    if not manifest['results'] or manifest['results'][-1]['stop_reason'] != 'unknown_usage_or_budget_stop':
        raise ValueError('Only a run stopped on the new-work threshold can be resumed')
    database = base/'runtime'/'team.sqlite3'
    with closing(sqlite3.connect(f'file:{database.as_posix()}?mode=ro', uri=True)) as db:
        rows = [(kind, json.loads(body)) for kind, body in
                db.execute("SELECT kind, body FROM objects WHERE kind IN ('job', 'task', 'approval')")]
    jobs = [b for k, b in rows if k == 'job']
    tasks = [b for k, b in rows if k == 'task']
    if len(jobs) != 1 or jobs[0]['status'] != 'cancelled' or jobs[0]['id'] != manifest['job_id']:
        raise ValueError('Expected exactly the one cancelled evaluation job')
    security = [t for t in tasks if t.get('agent_name') == SECURITY]
    others = [t for t in tasks if t.get('agent_name') != SECURITY]
    if (len(security) != 1 or security[0]['status'] != 'cancelled'
            or not others or any(t['status'] != 'succeeded' for t in others)
            or any(t.get('after') == security[0]['id'] for t in tasks)):
        raise ValueError('Only a cancelled final security review with all earlier tasks succeeded can resume')
    if any(b.get('status') == 'pending' for k, b in rows if k == 'approval'):
        raise ValueError('A pending decision exists')
    built = sum(t['role'] == 'builder' for t in others)
    check = base/'checks'/f'round-{built}.json'
    if not check.exists() or json.loads(check.read_text(encoding='utf-8'))['all_passed'] is not True:
        raise ValueError('The latest independent check must exist and pass')
    usage, _, _ = attempts(database)
    if any(r['measurement'] in ('unknown', 'partial') for r in usage):
        raise ValueError('Unknown or partial usage exists')
    total = sum((r.get('input_total') or 0)+(r.get('output') or 0) for r in usage)
    if total >= RESUME_REFUSE_AT:
        raise ValueError('Too little budget is left to resume one review')
    return jobs[0]['id'], security[0]['id'], total


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--approved-budget', type=int, required=True, choices=[BUDGET])
    parser.add_argument('--port', type=int, default=8818)
    args = parser.parse_args()
    base = validate_base(args.base)
    job_id, security_id, start_total = preflight(base)
    from server import make_server
    from team_optimization_metrics import attempts
    runtime = base/'runtime'
    manifest = json.loads((base/'manifest.json').read_text(encoding='utf-8'))
    manifest['resume'] = {'reason': 'user_authorized_after_budget_stop', 'task': 'security_review_only',
                          'start_tokens': start_total, 'cancel_at': RESUME_CANCEL_AT, 'repairs_allowed': False}

    def save():
        (base/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')

    save()
    server = make_server(runtime, args.port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    engine, store = server.app.engine, server.app.store

    def usage():
        rows, _, _ = attempts(runtime/'team.sqlite3')
        total = sum((r.get('input_total') or 0)+(r.get('output') or 0) for r in rows)
        unknown = sum(r['measurement'] in ('unknown', 'partial') for r in rows
                      if r.get('status') not in ('running', 'starting'))
        return total, unknown

    reason, started = '', False
    try:
        engine.retry(security_id, NOTE)
        began, last = time.monotonic(), None
        while True:
            job = store.get(job_id)
            security = store.get(security_id)
            total, unknown = usage()
            if (job['status'], security['status']) != last:
                print('STATUS', job['status'], 'security', security['status'], 'tokens', total, flush=True)
                last = (job['status'], security['status'])
            if total >= RESUME_CANCEL_AT or time.monotonic()-began > 900:
                reason = 'budget_or_time_stop'
                engine.cancel_job(job_id)
                break
            if job['status'] in ('blocked', 'failed', 'cancelled', 'interrupted', 'awaiting_acceptance'):
                reason = job['status']
                break
            if not engine.active:
                if security['status'] == 'queued' and not started:
                    engine.tick()
                    started = store.get(security_id)['status'] != 'queued'
                elif security['status'] == 'succeeded':
                    # Review asked for changes: repair tasks were queued. Do not start them.
                    reason = 'changes_requested_no_repair'
                    engine.cancel_job(job_id)
                    break
                elif unknown:
                    reason = 'unknown_usage'
                    engine.cancel_job(job_id)
                    break
            pending = [a for a in store.for_job('approval', job_id)
                       if a['status'] == 'pending' and a['kind'] != 'completion']
            if pending:
                print('NEEDS_DECISION', [a['kind'] for a in pending], flush=True)
                reason = 'needs_human_decision'
                engine.cancel_job(job_id)
                break
            time.sleep(1)
        deadline = time.monotonic()+45
        while engine.active and time.monotonic() < deadline:
            time.sleep(.5)
        if engine.active:
            raise RuntimeError('Worker cancellation not confirmed')
    finally:
        total, unknown = usage()
        manifest['resume'].update(stop_reason=reason or 'runner_error', status=store.get(job_id)['status'],
                                  cumulative_tokens=total, unknown_runs=unknown,
                                  security_review_result=(store.get(security_id).get('result') or {}).get('status'))
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
        print('RESUME_STOPPED', reason, total, unknown, flush=True)


if __name__ == '__main__':
    main()
