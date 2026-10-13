"""Content-free summary of a due-date evaluation. Run after the runner and acceptance screen stop.

Reports measured tokens, decision/rework counts, acceptance state, per-phase human minutes and
independent check rounds. Never writes prompts, review text or file contents.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from team_evaluation import snapshot
from team_optimization_metrics import attempts
from team_store import Store
from eval.development_readiness.run_due_date import PHASES, validate_base


def human_time(path):
    sheet = json.loads(Path(path).read_text(encoding='utf-8'))
    phases = sheet.get('phases')
    if sheet.get('observer') != 'real_user' or not isinstance(phases, dict) or set(phases) != set(PHASES):
        raise ValueError('human_time.json must list exactly the five phases for the real user')
    for value in phases.values():
        if value is not None and (type(value) is not int or not 0 <= value <= 60000):
            raise ValueError('Each phase is null (not measured) or 0-60000 whole minutes')
    measured = [v for v in phases.values() if v is not None]
    return {'phases': phases, 'unmeasured_phases': sorted(p for p, v in phases.items() if v is None),
            'total_minutes': sum(measured) if len(measured) == len(PHASES) else None,
            'measured_minutes_lower_bound': sum(measured) if measured else None}


def review_split(tasks):
    counts = {'normal': 0, 'security': 0}
    for task in tasks:
        if task.get('role') != 'reviewer':
            continue
        kind = 'security' if task.get('agent_name') == 'common-security-reviewer' else 'normal'
        reports = [h.get('result') or {} for h in task.get('review_result_history', [])]
        reports.append(task.get('result') or {})
        counts[kind] += sum(r.get('status') == 'needs_changes' for r in reports)
    return counts


def summarize(base):
    base = validate_base(base)
    manifest = json.loads((base/'manifest.json').read_text(encoding='utf-8'))
    rows, _, _ = attempts(base/'runtime'/'team.sqlite3')
    store = Store(base/'runtime')
    try:
        job_id = manifest['job_id']
        counts = snapshot(store, job_id)
        tasks = store.for_job('task', job_id)
    finally:
        store.db.close()
    rounds = sorted((base/'checks').glob('round-*.json'), key=lambda p: int(p.stem.split('-')[1]))
    checks = [json.loads(p.read_text(encoding='utf-8')) for p in rounds]
    return {
        'case_id': manifest['case_id'], 'job_id': job_id, 'final_status': counts['status'],
        'normal_acceptance': counts['status'] == 'accepted',
        'conditional_acceptance': counts['status'] == 'accepted_with_pending_checks',
        'total_tokens': sum((r.get('input_total') or 0)+(r.get('output') or 0) for r in rows),
        'unknown_runs': sum(r['measurement'] == 'unknown' for r in rows),
        'partial_runs': sum(r['measurement'] == 'partial' for r in rows),
        'budget': manifest['budget'],
        'runs': [{k: r.get(k) for k in ('role', 'status', 'measurement', 'input_total', 'output',
                                       'first_call_input')} for r in rows],
        'decisions': {k: counts[k] for k in ('approved_decisions', 'denied_decisions', 'pending_decisions')},
        'user_rework_count': counts['user_rework_count'],
        'review_needs_changes_reports': review_split(tasks),
        'task_retry_count': counts['task_retry_count'],
        'effort_record': counts['record'] and {k: counts['record'][k] for k in ('minutes', 'observer', 'revision')},
        'human_time': human_time(base/'human_time.json'),
        'independent_checks': [{k: c[k] for k in ('round', 'all_passed', 'error', 'readme_changed')} for c in checks],
        'readme_example_verified': 'human_check_required',
    }


def main():
    base = Path(sys.argv[1]).resolve()
    result = summarize(base)
    (base/'evaluation_summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('final_status', 'total_tokens', 'unknown_runs', 'partial_runs')},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
