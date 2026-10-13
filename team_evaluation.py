"""Local, explicit human-effort records; never inferred from elapsed or waiting time."""
from team_store import now


def snapshot(store, job_id):
    with store.lock:
        job = store.get(job_id, 'job')
        tasks = store.for_job('task', job_id)
        approvals = store.for_job('approval', job_id)
        records = store.for_job('evaluation', job_id)
    record = records[0] if records else None
    review_reports = []
    for task in tasks:
        if task.get('role') == 'reviewer':
            reports = [h.get('result') or {} for h in task.get('review_result_history', [])]
            reports.append(task.get('result') or {})
            review_reports.extend(r for r in reports if r.get('status') == 'needs_changes')
    return {
        'job_id': job_id, 'title': job.get('title', job_id), 'status': job['status'], 'record': record,
        'revision': record['revision'] if record else 0,
        'approved_decisions': sum(a.get('status') == 'approved' for a in approvals),
        'denied_decisions': sum(a.get('status') == 'denied' for a in approvals),
        'pending_decisions': sum(a.get('status') == 'pending' for a in approvals),
        'user_rework_count': job.get('rework_count', 0),
        'review_needs_changes_reports': len(review_reports),
        'task_retry_count': sum(max(0, t.get('attempt', 0) - 1) for t in tasks),
        'final_acceptance': job['status'] == 'accepted',
        'human_acceptance_verified': None,
    }


def record_effort(store, body):
    if set(body) != {'job_id', 'revision', 'minutes', 'observer', 'confirmed'}:
        raise ValueError('計測項目が不正です。')
    if (not isinstance(body['job_id'], str) or type(body['revision']) is not int
            or body['revision'] < 0 or body['confirmed'] is not True
            or type(body['minutes']) is not int or not 0 <= body['minutes'] <= 60000
            or body['observer'] not in ('real_user', 'evaluation_runner')):
        raise ValueError('実作業時間は0〜60000分の整数で、記録者と確認を指定してください。')
    with store.atomic():
        current = snapshot(store, body['job_id'])
        if body['revision'] != current['revision']:
            raise ValueError('別の画面で更新されています。再読み込みしてください。')
        record = {'id': 'evaluation:' + body['job_id'], 'job_id': body['job_id'],
                  'revision': current['revision'] + 1, 'minutes': body['minutes'],
                  'observer': body['observer'], 'measured_at': now(), 'method': 'self_reported_cumulative'}
        # Preserve corrections without free text, project content, or personal identifiers.
        old = current['record']
        record['history'] = ((old.get('history', []) + [{k: old[k] for k in
                              ('revision', 'minutes', 'observer', 'measured_at')}]) if old else [])
        store.put('evaluation', record)
    return snapshot(store, body['job_id'])
