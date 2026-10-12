"""Size-only inventory of review handoff sections in synthetic evaluation DBs."""
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NAMES = ('validated3', 'validated4', 'validated5', 'labels-pair1', 'labels-pair2', 'document2')


def items(database, query):
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as db:
        return db.execute(query).fetchall()


def main():
    result = []
    for name in NAMES:
        base = ROOT / 'data' / ('routing-comparison-20261012-' + name)
        runtime = base / 'runtime'
        if not (runtime / 'team.sqlite3').exists():
            continue
        objects = [(kind, json.loads(body)) for kind, body in items(
            runtime / 'team.sqlite3', "SELECT kind,body FROM objects WHERE kind IN ('job','task')")]
        jobs = {obj['id']: obj for kind, obj in objects if kind == 'job'}
        tasks = [obj for kind, obj in objects if kind == 'task']
        records = [(job_id, task_id, kind, happened_at, json.loads(payload)) for job_id, task_id, kind, happened_at, payload in items(
            runtime / 'handoff.sqlite3',
            "SELECT job_id,task_id,kind,happened_at,payload FROM records WHERE kind IN ('instruction','result','decision','scope_handoff','scope_transfer') ORDER BY seq")]
        by_job = defaultdict(list)
        for record in records:
            by_job[record[0]].append(record)
        for task in tasks:
            if task.get('role') != 'reviewer' or task.get('status') not in ('succeeded', 'cancelled', 'queued'):
                continue
            job = jobs[task['job_id']]
            started = (task.get('agent_run') or {}).get('started_at')
            if started is None:
                continue
            instructions, reports = {}, {}
            decisions = []
            for _, task_id, kind, happened_at, payload in by_job[job['id']]:
                if happened_at > started:
                    continue
                if kind == 'instruction':
                    instructions[task_id] = payload
                elif kind == 'result':
                    reports[task_id] = payload
                else:
                    decisions.append(payload)
            refs = ((task.get('agent_run') or {}).get('context_refs') or {})
            context_size = ((task.get('agent_run') or {}).get('context_size') or {})
            usage = ((task.get('agent_run') or {}).get('usage') or {})
            chars = lambda obj: len(json.dumps(obj, ensure_ascii=False))
            result.append({
                'run': name, 'route': job.get('route', 'full'), 'reviewer': task.get('agent_name', 'reviewer'),
                'status': task['status'], 'context_chars_recorded': refs.get('context_chars'),
                'user_prompt_chars': context_size.get('user_prompt_chars'),
                'native_instructions_chars': context_size.get('native_instructions_chars'),
                'provider_usage_input': usage.get('input_tokens'),
                'provider_usage_cache_read': usage.get('cache_read_input_tokens'),
                'provider_usage_output': usage.get('output_tokens'),
                'job_goal_chars': len(job.get('goal') or ''), 'task_instruction_chars': len(task.get('instruction') or ''),
                'instructions_chars': chars(instructions), 'reports_chars': chars(reports),
                'decisions_chars': chars(decisions), 'instruction_count': len(instructions),
                'report_count': len(reports), 'own_instruction_present': task['id'] in instructions,
                'own_instruction_duplicate_chars': len(task['instruction'])
                if task['id'] in instructions and
                instructions[task['id']].get('instruction') == task['instruction'] else 0,
            })
    if '--summary' in sys.argv:
        succeeded = [item for item in result if item['status'] == 'succeeded']
        summed = lambda key: sum(item.get(key) or 0 for item in succeeded)
        print(json.dumps({'succeeded_reviews': len(succeeded),
                          'user_prompt_chars': summed('user_prompt_chars'),
                          'handoff_context_chars': summed('context_chars_recorded'),
                          'exact_duplicate_instruction_chars': summed('own_instruction_duplicate_chars'),
                          'provider_input_and_cache_tokens': summed('provider_usage_input')
                          + summed('provider_usage_cache_read'),
                          'provider_output_tokens': summed('provider_usage_output'),
                          'note': 'Characters are a proxy; token savings have not been measured.'},
                         ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
