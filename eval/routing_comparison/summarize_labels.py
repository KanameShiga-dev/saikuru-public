"""Print privacy-minimal totals for the two synthetic labels pairs."""
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from team_optimization_metrics import attempts, report


def main():
    cases = []
    roles = Counter()
    review_statuses = Counter()
    route_outcomes = []
    for name in ('labels-pair1', 'labels-pair2'):
        base = ROOT / 'data' / ('routing-comparison-20261012-' + name)
        manifest = json.loads((base / 'manifest.json').read_text(encoding='utf-8'))
        rows, _, _ = attempts(base / 'runtime' / 'team.sqlite3')
        for route, outcome in report(base / 'runtime' / 'team.sqlite3')['by_route'].items():
            route_outcomes.append({'pair': name, 'route': route,
                                   'accepted_jobs': outcome['accepted_jobs'],
                                   'conditional_accepted_jobs': outcome['conditional_accepted_jobs'],
                                   'complete_usage': outcome['complete_usage'],
                                   'input_total_per_accepted': outcome['input_total_per_accepted'],
                                   'output_per_accepted': outcome['output_per_accepted']})
        for result in manifest['results']:
            job_id = result['job_id']
            selected = [row for row in rows if row['job_id'] == job_id]
            for row in selected:
                if row.get('input_total') is not None and row.get('output') is not None:
                    roles[(result['route'], row['role'])] += row['input_total'] + row['output']
            cases.append({key: result[key] for key in ('route', 'status', 'independent_pass',
                                                       'tokens_measured', 'unknown_usage', 'seconds')}
                         | {'pair': name, 'roles': [row['role'] for row in selected]})
        # Query only status/role from task rows. No task text or paths enter the output.
        import sqlite3
        with sqlite3.connect(base / 'runtime' / 'team.sqlite3') as db:
            for (body,) in db.execute("SELECT body FROM objects WHERE kind='task'"):
                task = json.loads(body)
                if task.get('role') == 'reviewer':
                    review_statuses[task.get('status', 'unknown')] += 1
    direct_base = ROOT / 'data' / 'routing-comparison-20261012-labels-pair1'
    direct = json.loads((direct_base / 'direct-result.json').read_text(encoding='utf-8'))
    # The last attempted direct run is in direct-result.json; earlier failure counts are recorded separately.
    print(json.dumps({'pairs': cases,
                      'role_tokens': {route: {role: value for (r, role), value in roles.items() if r == route}
                                      for route in ('full', 'light_eval')},
                      'review_task_statuses': dict(review_statuses),
                      'route_outcomes': route_outcomes,
                      'direct_latest': {key: direct.get(key) for key in ('returncode', 'independent_pass',
                                                                        'tokens_measured', 'usage_unknown', 'seconds')},
                      'pair_tokens': sum(c['tokens_measured'] for c in cases),
                      'measured_with_direct': sum(c['tokens_measured'] for c in cases)
                                              + (direct.get('tokens_measured') or 0)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
