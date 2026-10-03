"""Read-only project history assembled from persisted workflow records."""
import json


def consumption(task):
    runs = task.get('agent_run_history', []) + ([task['agent_run']] if task.get('agent_run') else [])
    seen = set()
    totals = {k: None for k in ('input', 'cached', 'cache_write', 'output')}
    measured = 0
    unique = {}
    for index, run in enumerate(runs):
        # Latest copy wins if the current run is also present in history.
        unique[run.get('id') or ('legacy', index)] = run
    per_run = []
    for ident, run in unique.items():
        seen.add(ident)
        usage = run.get('usage', {})
        fields = {'input': ('inputTokens', 'input_tokens'), 'cached': ('cachedInputTokens', 'cache_read_input_tokens'),
                  'cache_write': ('cache_creation_input_tokens',), 'output': ('outputTokens', 'output_tokens')}
        values = {name: next((usage[key] for key in keys if type(usage.get(key)) is int and usage[key] >= 0), None)
                  for name, keys in fields.items()}
        measured += any(v is not None for v in values.values())
        per_run.append(dict(values, id=run.get('id'), provider=run.get('provider'),
                            model=run.get('model'), status=run.get('status'), attempt=run.get('attempt')))
        for name, value in values.items():
            if value is not None:
                totals[name] = (totals[name] or 0) + value
    expected = max(task.get('attempt', 0), len(seen))
    current = task.get('agent_run') or {}
    return dict(totals, measured_runs=measured, expected_runs=expected, runs=per_run,
                quota_before=current.get('quota_before'), quota_after=current.get('quota_after'))


def snapshot(app, job_id='', before=0):
    origins = {}
    with app.consultations.lock:
        for (raw,) in app.consultations.db.execute('SELECT body FROM sessions'):
            item = json.loads(raw)
            if item.get('job_id'):
                origins[item['job_id']] = {'kind': 'ledger', 'consultation_id': item['id'],
                    'project': item.get('project', {}).get('path', '')}
    jobs = []
    for job in app.store.all('job'):
        fields = ('id', 'title', 'project', 'document_source', 'status', 'created_at', 'updated_at')
        value = {k: job.get(k) for k in fields}
        value['origin'] = origins.get(job['id'], {'kind': job.get('request_origin', 'unrecorded')})
        jobs.append(value)
    tasks = [{k: task.get(k) for k in ('id', 'job_id', 'title', 'role', 'status', 'profile',
              'created_at', 'started_at', 'finished_at', 'updated_at', 'attempt', 'summary')}
             for task in app.store.all('task') if not job_id or task['job_id'] == job_id]
    records = {t['id']: t for t in app.store.all('task')}
    for task in tasks:
        task['consumption'] = consumption(records[task['id']])
    approvals = [{k: a.get(k) for k in ('id', 'job_id', 'task_id', 'kind', 'status', 'created_at', 'decided_at', 'note')}
                 for a in app.store.all('approval') if job_id and a['job_id'] == job_id]
    events = []
    if job_id:
        app.store.get(job_id, 'job')
        with app.store.lock:
            rows = app.store.db.execute(
                "SELECT seq,body FROM events WHERE json_extract(body,'$.job_id')=? AND (?=0 OR seq<?) ORDER BY seq DESC LIMIT 201",
                (job_id, before, before)).fetchall()
        events = [dict(json.loads(raw), seq=seq) for seq, raw in rows[:200]]
        next_before = events[-1]['seq'] if len(rows) > 200 else None
    else:
        next_before = None
    waits = None
    if job_id:
        from team_waits import collect
        waits = collect(app.store, app.store.get(job_id, 'job'), app.config.get('business_hours'))
    return {'jobs': jobs, 'tasks': tasks, 'approvals': approvals, 'events': events, 'next_before': next_before,
            'quota': app.usage.snapshot(), 'waits': waits}
