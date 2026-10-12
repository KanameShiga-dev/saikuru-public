"""Read-only, privacy-minimal comparison of isolated routing evaluation jobs.

The verdict file maps local job IDs to independently checked booleans. No prompts,
paths, account names, or task titles are emitted. Missing usage stays unknown.
"""
import argparse
import json
from collections import Counter, defaultdict

from team_optimization_metrics import attempts


def summarize(database, verdicts):
    rows, jobs, _ = attempts(database)
    grouped = defaultdict(list)
    for row in rows:
        grouped[row['job_id']].append(row)
    result = []
    for job_id, job in jobs.items():
        if (job.get('evaluation') or {}).get('mode') != 'A':
            continue
        route = job.get('route', 'full')
        if route not in ('full', 'light_eval'):
            continue
        runs = grouped[job_id]
        missing = sum(r['measurement'] == 'unknown' or r['input_total'] is None or r['output'] is None for r in runs)
        partial = sum(r['measurement'] == 'partial' for r in runs)
        verdict = verdicts.get(job_id)
        complete = job.get('status') == 'accepted' and verdict is True
        tokens = sum(r['input_total'] + r['output'] for r in runs) if runs and not missing else None
        result.append({'job_id': job_id, 'route': route, 'status': job.get('status'),
                       'independent_pass': verdict, 'full_success': complete,
                       'attempts': len(runs), 'unknown_usage': missing, 'partial_usage': partial,
                       'total_tokens': tokens,
                       'by_role': {role: sum(r['input_total'] + r['output'] for r in runs
                                            if r['role'] == role) if all(r['input_total'] is not None and r['output'] is not None
                                                                           for r in runs if r['role'] == role) else None
                                   for role in sorted({r['role'] for r in runs})},
                       'models': sorted({str(r['provider']) + '/' + str(r['model']) for r in runs})})
    aggregate = {}
    for route in ('full', 'light_eval'):
        subset = [r for r in result if r['route'] == route]
        success = sum(r['full_success'] for r in subset)
        all_known = bool(subset) and all(r['total_tokens'] is not None for r in subset)
        total = sum(r['total_tokens'] for r in subset) if all_known else None
        aggregate[route] = {'jobs': len(subset), 'statuses': dict(Counter(r['status'] for r in subset)),
                            'full_success': success, 'unknown_usage': sum(r['unknown_usage'] for r in subset),
                            'total_tokens': total,
                            'tokens_per_full_success': round(total / success) if total is not None and success else None}
    return {'jobs': result, 'routes': aggregate,
            'note': '単発結果は予備測定。条件付き受領は成功に含めず、利用量欠損は0にしない。'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', required=True)
    parser.add_argument('--verdicts', required=True, help='Local JSON object: job_id -> true/false')
    args = parser.parse_args()
    with open(args.verdicts, encoding='utf-8') as handle:
        verdicts = json.load(handle)
    if not isinstance(verdicts, dict) or any(type(value) is not bool for value in verdicts.values()):
        parser.error('verdicts must map job IDs to booleans')
    print(json.dumps(summarize(args.database, verdicts), ensure_ascii=False, indent=2))
