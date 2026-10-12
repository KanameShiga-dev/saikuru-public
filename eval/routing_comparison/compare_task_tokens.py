"""Compare measured tokens for matching tasks in two isolated evaluation runs.

Only the original task instruction is used for matching. The assembled model
prompt intentionally differs when an optimization changes context. No prompt,
instruction, task title, or hash is emitted.
"""
import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from team_optimization_metrics import attempts


def _tasks(database):
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as db:
        return [json.loads(body) for (body,) in db.execute("SELECT body FROM objects WHERE kind='task'")]


def _load(base, role):
    manifest = json.loads((base / 'manifest.json').read_text(encoding='utf-8'))
    database = base / 'runtime' / 'team.sqlite3'
    rows, jobs, _ = attempts(database)
    if len(jobs) != 1:
        raise ValueError('Expected exactly one isolated job')
    job_id = next(iter(jobs))
    by_task = defaultdict(list)
    for row in rows:
        by_task[row['task_id']].append(row)
    selected = []
    for task in _tasks(database):
        if task['job_id'] != job_id or task['role'] != role:
            continue
        profile = task.get('profile') or {}
        runs = by_task[task['id']]
        versions = tuple(sorted({str(r.get('saikuru_version')) for r in runs}))
        key = (task.get('role'), task.get('agent_name'), task.get('instruction'),
               task.get('repair', 0), profile.get('adapter'), profile.get('model'), profile.get('effort'),
               versions)
        complete = bool(runs) and all(r['measurement'] == 'final'
                                      and isinstance(r.get('input_total'), int)
                                      and isinstance(r.get('output'), int) for r in runs)
        prompt_complete = complete and all(isinstance(r.get('first_call_input'), int) for r in runs)
        selected.append({'key': key, 'agent': task.get('agent_name'), 'repair': task.get('repair', 0),
                         'task_status': task.get('status'),
                         'result_status': (task.get('result') or {}).get('status'),
                         'attempts': len(runs), 'complete_usage': complete,
                         'tokens': sum(r['input_total'] + r['output'] for r in runs) if complete else None,
                         'prompt_input': sum(r['first_call_input'] for r in runs) if prompt_complete else None})
    return manifest, jobs[job_id].get('status'), selected


def compare(baseline_dir, candidate_dir, role='reviewer'):
    baseline, baseline_status, off = _load(Path(baseline_dir), role)
    candidate, candidate_status, on = _load(Path(candidate_dir), role)
    if not baseline.get('goal_sha256') or baseline['goal_sha256'] != candidate.get('goal_sha256'):
        raise ValueError('The original job requests differ')
    left, right = defaultdict(list), defaultdict(list)
    for task in off:
        left[task['key']].append(task)
    for task in on:
        right[task['key']].append(task)
    pairs, unmatched = [], []
    for key in left.keys() | right.keys():
        a, b = left[key], right[key]
        if len(a) != 1 or len(b) != 1:
            for label, group in (('baseline', a), ('candidate', b)):
                unmatched.extend({'side': label, 'agent': t['agent'], 'repair': t['repair'],
                                  'status': t['task_status'], 'prompt_input': t['prompt_input'],
                                  'task_tokens': t['tokens']} for t in group)
            continue
        a, b = a[0], b[0]
        task_comparable = a['complete_usage'] and b['complete_usage'] and a['tokens'] > 0
        prompt_comparable = (a['prompt_input'] is not None and b['prompt_input'] is not None
                             and a['prompt_input'] > 0 and a['attempts'] == b['attempts'])
        pairs.append({'agent': a['agent'], 'repair': a['repair'],
                      'same_instruction': True, 'same_result': a['result_status'] == b['result_status'],
                      'baseline_result': a['result_status'], 'candidate_result': b['result_status'],
                      'baseline_attempts': a['attempts'], 'candidate_attempts': b['attempts'],
                      'baseline_prompt_input': a['prompt_input'], 'candidate_prompt_input': b['prompt_input'],
                      'prompt_reduction_percent': round(100 * (a['prompt_input'] - b['prompt_input'])
                                                        / a['prompt_input'], 1) if prompt_comparable else None,
                      'baseline_task_tokens': a['tokens'], 'candidate_task_tokens': b['tokens'],
                      'task_reduction_percent': round(100 * (a['tokens'] - b['tokens']) / a['tokens'], 1)
                      if task_comparable else None})
    pairs.sort(key=lambda p: (p['repair'], p['agent'] or ''))
    unmatched.sort(key=lambda t: (t['side'], t['repair'], t['agent'] or ''))
    return {'primary_metric': 'first model call input tokens for matched individual tasks',
            'secondary_metric': 'whole individual task input_total + output',
            'pairing': 'same original request, role, agent, exact task instruction, repair number, model, effort and harness version',
            'pairs': pairs, 'unmatched': unmatched,
            'baseline_job_status': baseline_status,
            'candidate_job_status': candidate_status,
            'note': 'Total job usage is separate budget and outcome context; unmatched repair work is not zero.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('baseline_dir', type=Path)
    parser.add_argument('candidate_dir', type=Path)
    parser.add_argument('--role', default='reviewer')
    args = parser.parse_args()
    print(json.dumps(compare(args.baseline_dir, args.candidate_dir, args.role), ensure_ascii=False, indent=2))
