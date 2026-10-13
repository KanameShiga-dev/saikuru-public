"""Content-free verdicts for a tool-approval evaluation. Reads the evaluation DB read-only."""
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from team_optimization_metrics import attempts
from eval.development_readiness.run_tool_approval import fixture_hashes, judge, validate_base
from eval.development_readiness.summarize_due_date import human_time


def scenario_records(database, job_id, task_id):
    """Tool-approval statuses (creation order) and the number of executed-command records for the task."""
    with closing(sqlite3.connect(f'file:{Path(database).as_posix()}?mode=ro', uri=True)) as db:
        approvals = [json.loads(b) for (b,) in db.execute(
            "SELECT body FROM objects WHERE kind='approval' ORDER BY rowid")]
        events = [json.loads(b) for (b,) in db.execute('SELECT body FROM events ORDER BY seq')]
    statuses = [a['status'] for a in approvals if a.get('job_id') == job_id and a.get('kind') == 'tool']
    executed = sum(e.get('type') == 'operation_result' and e.get('task_id') == task_id for e in events)
    return statuses, executed


def summarize(base):
    base = validate_base(base)
    manifest = json.loads((base/'manifest.json').read_text(encoding='utf-8'))
    database = base/'runtime'/'team.sqlite3'
    scenarios = {}
    for result in manifest['results']:
        name = result['scenario']
        expected = manifest['scenarios'][name]
        statuses, executed = scenario_records(database, result['job_id'], result['task_id'])
        hashes_ok = fixture_hashes(base/'projects'/name) == expected['fixture_hashes']
        scenarios[name] = {'expect': expected['expect'], 'approval_statuses': statuses,
                           'executed_commands': executed, 'fixture_unchanged': hashes_ok,
                           'stop_reason': result['stop_reason'],
                           'verdict': judge(expected['expect'], statuses, executed, hashes_ok)}
    rows, _, _ = attempts(database)
    return {'scenarios': scenarios,
            'not_run': [n for n in manifest['scenarios'] if n not in scenarios],
            'total_tokens': sum((r.get('input_total') or 0)+(r.get('output') or 0) for r in rows),
            'unknown_runs': sum(r['measurement'] == 'unknown' for r in rows),
            'partial_runs': sum(r['measurement'] == 'partial' for r in rows),
            'budget': manifest['budget'],
            'human_time': human_time(base/'human_time.json'),
            'late_decision_rejection': 'unit_test_only'}


def main():
    base = Path(sys.argv[1]).resolve()
    result = summarize(base)
    (base/'tool_approval_summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({n: s['verdict'] for n, s in result['scenarios'].items()}, ensure_ascii=False),
          result['total_tokens'], result['unknown_runs'])


if __name__ == '__main__':
    main()
