"""Reconstruct recorded waits, merging overlapping sources per request."""
import math
import time
from team_worktime import DEFAULT, business_segments, split_seconds

WAIT = {'blocked', 'failed', 'awaiting_approval', 'awaiting_acceptance'}
END = {'cancelled', 'accepted', 'accepted_with_pending_checks', 'completed'}


def collect(store, job, cfg=None, current=None):
    cfg = cfg or DEFAULT
    current = time.time() if current is None else current
    with store.lock:
        started = store.db.execute("SELECT value FROM metrics_meta WHERE key='started_at'").fetchone()[0]
        rows = store.db.execute('SELECT kind,id,to_status,at,reason FROM transitions WHERE job_id=? ORDER BY seq', (job['id'],)).fetchall()
    result = {'measured': (job.get('created_at') or 0) >= started, 'measurement_started_at': started,
              'intervals': [], 'inside_seconds': 0, 'outside_seconds': 0, 'system_seconds': 0,
              'business_hours': cfg, 'as_of': current}
    if not result['measured']:
        return result
    opened, raw = {}, []
    for kind, ident, status, at, reason in rows:
        source = (kind, ident)
        waiting = status in WAIT if kind == 'job' else status == 'awaiting_approval'
        category = 'system' if status == 'interrupted' else 'human' if waiting else None
        if source in opened and opened[source][2] != category:
            a, why, cat = opened.pop(source)
            raw.append((a, at, why, cat, False))
        if kind == 'job' and (status in END or category == 'system'):
            for key in list(opened):
                if key[0] == 'task':
                    a, why, cat = opened.pop(key); raw.append((a, at, why, cat, False))
        if category and source not in opened:
            opened[source] = (at, reason, category)
    raw.extend((a, current, why, cat, True) for a, why, cat in opened.values())
    # System stops are counted from the request, not again from each task.
    for cat in ('human', 'system'):
        merged = []
        for a, b, why, category, active in sorted(raw):
            if category != cat or b < a:
                continue
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(b, merged[-1][1]); merged[-1][3] |= active
            else:
                merged.append([a, b, why, active])
        for a, b, why, active in merged:
            item = {'opened_at': a, 'closed_at': None if active else b, 'as_of': b,
                    'reason': why, 'category': cat, 'open': active}
            try:
                inside, outside = split_seconds(a, b, cfg)
                item.update(inside_seconds=inside, outside_seconds=outside,
                            business_segments=business_segments(a, b, cfg))
                if cat == 'human':
                    result['inside_seconds'] += inside; result['outside_seconds'] += outside
                else:
                    result['system_seconds'] += b-a
            except (ValueError, OverflowError, OSError) as exc:
                item.update(inside_seconds=None, outside_seconds=None, error=str(exc))
                result['incomplete'] = True
            result['intervals'].append(item)
    result['intervals'].sort(key=lambda i:i['opened_at'])
    return result


def statistics(store, cfg, start=None, end=None, current=None):
    current = time.time() if current is None else current
    for v in (start, end):
        if v is not None and (not math.isfinite(v) or v < 0):
            raise ValueError('集計期間が不正です。')
    if start is not None and end is not None and start > end:
        raise ValueError('集計期間が逆転しています。')
    grouped, outside, system, old, errors = {}, 0, 0, 0, 0
    for job in store.all('job'):
        waits = collect(store, job, cfg, current)
        if not waits['measured']:
            old += 1; continue
        for interval in waits['intervals']:
            a, b = max(interval['opened_at'], start or 0), min(interval['as_of'], current if end is None else end)
            if b <= a:
                continue
            try:
                inside, extra = split_seconds(a, b, cfg)
            except (ValueError, OverflowError, OSError):
                errors += 1; continue
            if interval['category'] == 'system':
                system += b-a; continue
            outside += extra
            grouped.setdefault(interval['reason'], []).append(inside)
    rows = []
    for reason, values in sorted(grouped.items()):
        values.sort(); n = len(values)
        median = (values[(n-1)//2]+values[n//2])/2
        rows.append({'reason':reason, 'count':n, 'median_seconds':median,
                     'p90_seconds':values[max(0, math.ceil(n*.9)-1)], 'max_seconds':values[-1]})
    return {'rows':rows, 'outside_seconds':outside, 'system_seconds':system,
            'unmeasured_jobs':old, 'out_of_range':errors, 'business_hours':cfg,
            'from':start, 'to':end, 'as_of':current}
