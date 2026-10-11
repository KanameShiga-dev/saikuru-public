"""Usage measurement report (改修① Phase 1). Read-only over 采来's database: no API calls, no task text.

One row per agent run attempt (task.agent_run_history + task.agent_run). Totals add only measured values and
count UNKNOWN attempts separately, so a missing measurement is never read as zero use.
Quota windows (team_usage.py) are a different measure and are not part of this report.

    python -X utf8 team_optimization_metrics.py                 # JSON summary
    python -X utf8 team_optimization_metrics.py --csv out.csv   # one row per attempt
"""
import csv
import json
from contextlib import closing
import sqlite3
from pathlib import Path

from team_usage_metrics import normalize

TOKEN_KEYS = ('input_uncached', 'cache_read', 'cache_creation', 'output', 'input_total')
ATTEMPT_FIELDS = ('job_id', 'task_id', 'role', 'agent', 'attempt', 'provider', 'model', 'status', 'failure_code',
                  'measurement', *TOKEN_KEYS, 'num_turns', 'duration_ms', 'tool_calls', 'experience_refs',
                  'skills_presented', 'skills_read', 'context_chars', 'saikuru_version', 'optimization_mode',
                  'evaluation_mode', 'budget_mode', 'reuse_chars_before', 'reuse_chars_after', 'experience_dropped',
                  'quality_fallback', 'candidate_reuse_chars_after',
                  'started_at', 'finished_at')


def _attempt(task, run):
    usage = run.get('usage_normalized') or normalize(run.get('provider'), run.get('usage'),
                                                     'recorded_before_phase1' if run.get('usage') else None, {})
    refs = run.get('context_refs') or {}
    raw = run.get('usage_raw') or {}
    return {'job_id': task['job_id'], 'task_id': task['id'], 'role': task.get('role'), 'agent': run.get('name'),
            'attempt': run.get('attempt'), 'provider': run.get('provider'), 'model': run.get('model') or (task.get('profile') or {}).get('model'),
            'status': run.get('status'), 'failure_code': run.get('failure_code'), 'measurement': usage['measurement'],
            **{k: usage.get(k) for k in TOKEN_KEYS},
            'num_turns': raw.get('num_turns'), 'duration_ms': raw.get('duration_ms'),
            'tool_calls': sum((run.get('tool_calls') or {}).values()) if 'tool_calls' in run else None,
            'experience_refs': len(refs['experience']) if 'experience' in refs else None,
            'skills_presented': len(refs['skills']) if 'skills' in refs else None,
            'skills_read': len(run.get('skill_reads') or []) if 'context_refs' in run else None,
            'context_chars': refs.get('context_chars'),
            'saikuru_version': run.get('saikuru_version'), 'optimization_mode': (run.get('optimization') or {}).get('mode'),
            'evaluation_mode': refs.get('evaluation_mode'), 'budget_mode': (refs.get('budget') or {}).get('mode'),
            'reuse_chars_before': (refs.get('budget') or {}).get('chars_before'),
            'reuse_chars_after': (refs.get('budget') or {}).get('chars_after'),
            'experience_dropped': (refs.get('budget') or {}).get('experience_dropped'),
            'quality_fallback': (refs.get('budget') or {}).get('quality_fallback'),
            'candidate_reuse_chars_after': (refs.get('budget') or {}).get('candidate_chars_after'),
            'started_at': run.get('started_at'), 'finished_at': run.get('finished_at')}


def attempts(database):
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        tasks = [json.loads(body) for (body,) in db.execute("SELECT body FROM objects WHERE kind='task'")]
        jobs = {j['id']: j for j in (json.loads(body) for (body,) in db.execute("SELECT body FROM objects WHERE kind='job'"))}
        approvals = [json.loads(body) for (body,) in db.execute("SELECT body FROM objects WHERE kind='approval'")]
    rows = []
    for task in tasks:
        runs = list(task.get('agent_run_history') or []) + ([task['agent_run']] if task.get('agent_run') else [])
        seen = set()
        for run in runs:
            if not isinstance(run, dict) or run.get('id') in seen:
                continue
            seen.add(run.get('id'))
            rows.append(_attempt(task, run))
    return rows, jobs, approvals


def _total(rows):
    measured = [r for r in rows if r['measurement'] in ('final', 'partial')]
    out = {'attempts': len(rows), 'measured_attempts': len(measured),
           'partial_attempts': sum(r['measurement'] == 'partial' for r in rows),
           'unknown_attempts': sum(r['measurement'] == 'unknown' for r in rows)}
    for key in TOKEN_KEYS:
        values = [r[key] for r in measured if isinstance(r[key], int)]
        out[key] = sum(values) if values else None
    return out


def report(database):
    rows, jobs, approvals = attempts(database)
    by = lambda key: {name: _total([r for r in rows if key(r) == name]) for name in sorted({str(key(r)) for r in rows})}
    per_job = {}
    for job_id in sorted({r['job_id'] for r in rows}):
        job_rows = [r for r in rows if r['job_id'] == job_id]
        job = jobs.get(job_id, {})
        waits = [a['decided_at'] - a['created_at'] for a in approvals
                 if a.get('job_id') == job_id and isinstance(a.get('decided_at'), (int, float)) and isinstance(a.get('created_at'), (int, float))]
        per_job[job_id] = dict(_total(job_rows), title=str(job.get('title', ''))[:80], job_status=job.get('status'),
                               accepted=job.get('status') in ('accepted', 'accepted_with_pending_checks'),
                               tasks=len({r['task_id'] for r in job_rows}),
                               retry_attempts=len(job_rows) - len({r['task_id'] for r in job_rows}),
                               failed_or_cancelled=_total([r for r in job_rows if r['status'] in ('failed', 'cancelled')]),
                               approvals=len(waits), approval_wait_seconds=round(sum(waits)) if waits else None)
    evaluation = {}
    for mode in ('A', 'B', 'C', 'D'):
        ids = [j for j, job in jobs.items() if (job.get('evaluation') or {}).get('mode') == mode]
        mode_rows = [r for r in rows if r['job_id'] in ids]
        accepted = [j for j in ids if jobs[j].get('status') in ('accepted', 'accepted_with_pending_checks')]
        total = _total(mode_rows)
        evaluation[mode] = dict(total, jobs=len(ids), accepted_jobs=len(accepted),
                                acceptance_rate=round(len(accepted) / len(ids), 3) if ids else None,
                                # Main KPI: cloud usage per accepted deliverable, failures and retries included.
                                input_total_per_accepted=round(total['input_total'] / len(accepted)) if accepted and total['input_total'] is not None else None,
                                output_per_accepted=round(total['output'] / len(accepted)) if accepted and total['output'] is not None else None)
    shadow = [r for r in rows if r['budget_mode'] in ('shadow', 'on') and isinstance(r['reuse_chars_before'], int)]
    budget = {'attempts': len(shadow),
              'reuse_chars_before': sum(r['reuse_chars_before'] for r in shadow) if shadow else None,
              'reuse_chars_after': sum(r['reuse_chars_after'] for r in shadow) if shadow else None,
              'experience_dropped': sum(r['experience_dropped'] or 0 for r in shadow) if shadow else None,
              'unit': 'characters of reuse context (not tokens)'}
    return {'overall': _total(rows),
            'evaluation_modes': evaluation,
            'context_budget': budget,
            'by_provider_model': by(lambda r: str(r['provider']) + '/' + str(r['model'])),
            'by_status': by(lambda r: r['status']),
            'by_role': by(lambda r: r['role']),
            'by_saikuru_version': by(lambda r: r['saikuru_version']),
            'per_job': per_job,
            'limitations': [
                '集計は測定できた試行（final/partial）だけの合計。unknown の試行は0として数えず件数だけ示す。',
                'partial は最終集計が届かなかった試行（失敗・中止）で、途中までの呼び出しごとの値の合計。',
                'Phase 1 より前の試行は、トークン以外（ツール呼び出し・参照した経験/Skill・版）が記録されていない（None）。',
                'approval_wait_seconds は承認の待ち時間であり、人の実作業時間ではない（実作業時間は未測定）。',
                '利用枠（%）は別の指標で、このレポートには含めない。',
                '削減効果は evaluation_modes（同じ課題・同じ受入条件で A〜D を比べた試験）からだけ判断する。普段の依頼の合計からは主張しない。',
                'context_budget の SHADOW 値は「減らしていたら減った文字数」の見込みで、トークンの実測ではない。']}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', default=str(Path(__file__).parent / 'data/team.sqlite3'))
    parser.add_argument('--csv', help='write one row per attempt to this CSV file')
    args = parser.parse_args()
    if args.csv:
        rows, _, _ = attempts(args.database)
        with open(args.csv, 'w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=ATTEMPT_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        print(f'{len(rows)} attempts -> {args.csv}')
    else:
        print(json.dumps(report(args.database), ensure_ascii=False, indent=2))
