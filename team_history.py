"""Read-only project history assembled from persisted workflow records."""
import json


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
    return {'jobs': jobs, 'tasks': tasks, 'approvals': approvals, 'events': events, 'next_before': next_before}
