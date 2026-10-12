"""Summary of the reference-context image optimisation records (SHADOW measurement and AUTO A/B comparison).

Reads only the per-run records (hashes, sizes, reasons, CLI token counts) kept in each task's agent_run; never any
text or image. The input tokens are the CLI's own report for the whole run; differences between runs also depend
on the task, so a small number of runs does not establish a saving.
Usage while 采来 is running:  python -X utf8 team_context_metrics.py
"""
import json
from collections import Counter


def input_total(usage):
    """Input tokens of a run as the CLI reported them, or None (unknown is not zero)."""
    usage = usage or {}
    claude = [usage.get(k) for k in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')]
    if all(isinstance(v, int) for v in claude):
        return sum(claude)
    return usage.get('inputTokens') if isinstance(usage.get('inputTokens'), int) else None


def _add(groups, name, task, run, record):
    group = groups.setdefault(name, {'runs': 0, 'succeeded': 0, 'input_tokens': [], 'output_tokens': [],
                                     'estimated_saving_ratio': []})
    group['runs'] += 1
    group['succeeded'] += task.get('status') in ('succeeded', 'handed_off')
    total = input_total(run.get('usage'))
    if total is not None:
        group['input_tokens'].append(total)
    usage = run.get('usage') or {}
    output = usage.get('output_tokens', usage.get('outputTokens'))
    if isinstance(output, int):
        group['output_tokens'].append(output)
    group['estimated_saving_ratio'] += [i['estimated_saving_ratio'] for i in record.get('references', [])
                                        if isinstance(i.get('estimated_saving_ratio'), (int, float))]


def _finish(groups):
    mean = lambda values, digits=0: round(sum(values) / len(values), digits) if values else None
    for group in groups.values():
        tokens, output, ratios = group.pop('input_tokens'), group.pop('output_tokens'), group.pop('estimated_saving_ratio')
        group.update(input_tokens_mean=mean(tokens), input_tokens_runs=len(tokens), output_tokens_mean=mean(output),
                     estimated_saving_ratio_mean=mean(ratios, 3))
        if group['input_tokens_mean'] is not None:
            group['input_tokens_mean'] = int(group['input_tokens_mean'])
        if group['output_tokens_mean'] is not None:
            group['output_tokens_mean'] = int(group['output_tokens_mean'])
    return groups


def summary(tasks):
    runs, reasons, by_adapter, chars = 0, Counter(), Counter(), []
    transport, mode = {}, {}
    for task in tasks:
        run = task.get('agent_run') or {}
        record = run.get('context_optimization')
        if not record:
            continue
        runs += 1
        for item in record.get('references', []):
            reasons[item.get('decision_reason')] += 1
            by_adapter[(record.get('adapter'), item.get('decision_reason'))] += 1
            if isinstance(item.get('reference_chars'), int):
                chars.append(item['reference_chars'])
        _add(transport, record.get('selected_transport', 'text'), task, run, record)
        # Token reduction ON (auto) vs OFF with recording (shadow): the comparison for the measurement.
        _add(mode, record.get('optimization_mode', 'shadow'), task, run, record)
    return {'runs_recorded': runs, 'decision_reasons': dict(reasons),
            'by_adapter': {f'{a}:{r}': n for (a, r), n in sorted(by_adapter.items(), key=str)},
            'by_transport': _finish(transport), 'by_mode': _finish(mode),
            'reference_chars': {'count': len(chars), 'min': min(chars, default=None), 'max': max(chars, default=None)},
            'note': '入力トークンは CLI が返した作業全体の値です。作業内容でも変わるため、少ない件数で削減を断定しません。'}


COMPARE_KEYS = ('input_total', 'input_uncached', 'cache_read', 'cache_creation', 'output', 'weighted', 'copilot_credits')


def comparisons(jobs, tasks):
    """The same request run with token reduction OFF and ON (created together, sharing a comparison id).
    Per side: every agent run of the request (retries included), summed from the providers' own counts. A run without
    a measurement is counted separately, never as zero; a total with unmeasured runs is marked incomplete."""
    from team_optimization_metrics import _attempt
    pairs = {}
    for job in jobs:
        mark = job.get('token_reduction') or {}
        if mark.get('comparison_id'):
            pairs.setdefault(mark['comparison_id'], {})[mark.get('side')] = job
    result = []
    for comparison_id, sides in pairs.items():
        row = {'comparison_id': comparison_id, 'created_at': min(j.get('created_at') or 0 for j in sides.values()), 'sides': {}}
        for side, job in sides.items():
            totals = dict.fromkeys(COMPARE_KEYS)
            runs = unmeasured = images = 0
            for task in (t for t in tasks if t.get('job_id') == job['id']):
                history = list(task.get('agent_run_history') or []) + ([task['agent_run']] if task.get('agent_run') else [])
                seen = set()
                for run in history:
                    if not isinstance(run, dict) or run.get('id') in seen or run.get('status') == 'held':
                        continue
                    seen.add(run.get('id'))
                    runs += 1
                    images += (run.get('context_optimization') or {}).get('selected_transport') == 'image'
                    attempt = _attempt(task, run)
                    if attempt['measurement'] in ('unknown', 'partial'):
                        unmeasured += 1
                    if attempt['measurement'] == 'unknown':
                        continue
                    for key in COMPARE_KEYS:
                        if isinstance(attempt.get(key), (int, float)):
                            totals[key] = (totals[key] or 0) + attempt[key]
            row['sides'][side] = {'job_id': job['id'], 'title': job.get('title'), 'status': job.get('status'),
                                  'runs': runs, 'unmeasured_runs': unmeasured, 'image_runs': images,
                                  'complete': unmeasured == 0 and job.get('status') == 'accepted',
                                  **{k: (round(v, 2) if isinstance(v, float) else v) for k, v in totals.items()}}
        on, off = row['sides'].get('on'), row['sides'].get('off')
        if on and off and on['complete'] and off['complete'] and on.get('input_total') and off.get('input_total'):
            row['input_change_ratio'] = round(on['input_total'] / off['input_total'] - 1, 3)
        if on and off and on['complete'] and off['complete'] and on.get('weighted') and off.get('weighted'):
            row['weighted_change_ratio'] = round(on['weighted'] / off['weighted'] - 1, 3)
        result.append(row)
    return sorted(result, key=lambda r: r['created_at'], reverse=True)


def main():
    import http.cookiejar
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    opener.open('http://127.0.0.1:8790/', timeout=10).read()
    state = json.loads(opener.open('http://127.0.0.1:8790/api/state', timeout=120).read())
    print(json.dumps(summary(state.get('tasks', [])), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
