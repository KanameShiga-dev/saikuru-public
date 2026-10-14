"""Token estimates from past runs. Read-only over local 采来 DBs; no AI or network calls.

Each rerun rebuilds everything from the runs recorded so far, so the estimates refine themselves:

- Per-call table: tokens per call by (provider, model, role).
- After planning (structure known): sum of calls x per-call median, with a band from the observed
  actual/estimate ratios of other jobs.
- At submission (structure unknown): the call structures of past jobs of the same kind and route,
  priced with this job's role -> model assignment; the band comes from that spread.
- Band calibration: the narrowest symmetric quantile pair whose leave-one-job-out coverage reaches the
  target (default 80%) is chosen automatically, and the coverage actually reached is reported.

Counts are provider-reported input (including cache reads) + output, not billing or quota.

  python -B -X utf8 eval/token_estimate/estimate.py [--target-coverage 0.8] [--json OUT]
"""
import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# Copies of the main DB (backups, patch reviews, public export) would count the same runs twice.
SKIP_PARTS = {'backups', 'patch-review-20261012', 'github-export', 'backup'}
MIN_SAMPLES = 3
MIN_PEERS = 5
QUANTILE_GRID = (0.25, 0.2, 0.15, 0.1, 0.05, 0.0)   # lower quantile; the upper one is 1 - q
WIDEN_GRID = (1.1, 1.25, 1.5, 2.0)                  # used only when [min, max] does not reach the target


def databases(root=ROOT / 'data'):
    main = ROOT / 'data' / 'team.sqlite3'
    found = [main] if main.exists() else []
    for path in sorted(Path(root).rglob('team.sqlite3')):
        if path not in found and not SKIP_PARTS & set(path.relative_to(ROOT).parts):
            found.append(path)
    return found


def role_key(row):
    return 'security' if row.get('agent') == 'common-security-reviewer' else row.get('role') or 'unknown'


def job_kind(job):
    kind = 'document' if (job.get('document_format') or job.get('document_source')) else 'development'
    return kind, job.get('route', 'full')


def collect(paths):
    """Final-measured runs, deduplicated across databases, and jobs keyed by (db, job_id)."""
    from team_optimization_metrics import attempts
    runs, seen, jobs = [], set(), {}
    for path in paths:
        try:
            rows, job_rows, _ = attempts(path)
        except Exception:
            continue
        for row in rows:
            key = (row['job_id'], row['task_id'], row.get('attempt'), row.get('agent'),
                   row.get('input_total'), row.get('output'))
            if key in seen:
                continue
            seen.add(key)
            job = (str(Path(path).relative_to(ROOT)) if Path(path).is_relative_to(ROOT) else str(path), row['job_id'])
            entry = jobs.setdefault(job, {'kind': job_kind(job_rows.get(row['job_id']) or {}),
                                          'runs': [], 'complete': True})
            if (row['measurement'] != 'final' or not isinstance(row.get('input_total'), int)
                    or not isinstance(row.get('output'), int)):
                entry['complete'] = False
                continue
            item = {'provider': row.get('provider'), 'model': row.get('model'), 'role': role_key(row),
                    'tokens': row['input_total'] + row['output'], 'job': job}
            runs.append(item)
            entry['runs'].append(item)
    return runs, {k: v for k, v in jobs.items() if v['complete'] and v['runs']}


def quantile(values, q):
    values = sorted(values)
    if not values:
        return None
    pos = (len(values) - 1) * q
    low, high = int(pos), min(int(pos) + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (pos - low)


def table(runs):
    groups = defaultdict(list)
    for run in runs:
        groups[(run['provider'], run['model'], run['role'])].append(run['tokens'])
    return {key: {'n': len(v), 'p25': round(quantile(v, .25)), 'median': round(quantile(v, .5)),
                  'p75': round(quantile(v, .75))} for key, v in groups.items()}


def structure(entry):
    return Counter((r['provider'], r['model'], r['role']) for r in entry['runs'])


def role_models(entry):
    """role -> (provider, model) as used by this job (the assignment known when the job is submitted)."""
    votes = defaultdict(Counter)
    for run in entry['runs']:
        votes[run['role']][(run['provider'], run['model'])] += 1
    return {role: counter.most_common(1)[0][0] for role, counter in votes.items()}


def point(calls, stats):
    """Sum of calls x per-call median, or None when any part has too few samples."""
    total = 0
    for key, count in calls.items():
        row = stats.get(key)
        if not row or row['n'] < MIN_SAMPLES:
            return None
        total += count * row['median']
    return total


def priced(role_counts, assignment, stats):
    """A past job's per-role call counts priced with another job's role -> model assignment."""
    calls = Counter()
    for role, count in role_counts.items():
        model = assignment.get(role) or (assignment.get('reviewer') if role == 'security' else None)
        if model is None:
            return None
        calls[(model[0], model[1], role)] += count
    return point(calls, stats)


def band(values, q, widen=1.0):
    return quantile(values, q) / widen, quantile(values, 1 - q) * widen


def choose_quantile(samples, target):
    """samples: [(values, actual)] — pick the narrowest band whose coverage reaches target.

    First the quantile pair [q, 1-q] narrows or widens; when even the full range [min, max] is not enough,
    both ends are pushed outward by a factor. Returns (q, widen, coverage, reached)."""
    usable = [(v, a) for v, a in samples if len(v) >= 2]
    if not usable:
        return None, None, None, False
    steps = [(q, 1.0) for q in QUANTILE_GRID] + [(0.0, w) for w in WIDEN_GRID]
    for q, widen in steps:
        covered = sum(lo <= a <= hi for v, a in usable for lo, hi in [band(v, q, widen)]) / len(usable)
        if covered >= target:
            return q, widen, covered, True
    return q, widen, covered, False


def after_planning(runs, jobs, target):
    """Structure known. Band = estimate x quantiles of the other jobs' actual/estimate ratios."""
    ratios, rows = {}, {}
    for job, entry in jobs.items():
        others = [r for r in runs if r['job'] != job]
        estimate = point(structure(entry), table(others))
        if estimate:
            actual = sum(r['tokens'] for r in entry['runs'])
            ratios[job], rows[job] = actual / estimate, (estimate, actual)
    # Nested leave-one-out: each job's band uses only the other jobs' ratios.
    samples = [([estimate * r for other, r in ratios.items() if other != job], actual)
               for job, (estimate, actual) in rows.items()]
    q, widen, coverage, reached = choose_quantile(samples, target)
    errors = [abs(e - a) / a for e, a in rows.values()]
    return {'jobs': len(rows), 'chosen_quantile': q, 'widen': widen, 'target_reached': reached,
            'coverage': round(coverage, 3) if coverage is not None else None,
            'median_abs_error_ratio': round(statistics.median(errors), 3) if errors else None,
            'within_25pct': round(sum(e <= .25 for e in errors) / len(errors), 3) if errors else None,
            'ratio_band_all_jobs': [round(x, 3) for x in band(list(ratios.values()), q, widen)]
            if q is not None and ratios else None}


def candidates(kind, assignment, pool, stats):
    """Totals that past jobs of the same kind would have cost with this assignment."""
    values = []
    for entry in pool:
        if entry['kind'] != kind:
            continue
        total = priced(Counter(r['role'] for r in entry['runs']), assignment, stats)
        if total:
            values.append(total)
    return values


def at_submission(runs, jobs, target):
    """Structure unknown: price same-kind past structures with the job's own role -> model assignment."""
    samples, misses = [], 0
    for job, entry in jobs.items():
        others = [r for r in runs if r['job'] != job]
        pool = [e for j, e in jobs.items() if j != job]
        values = candidates(entry['kind'], role_models(entry), pool, table(others))
        if len(values) < MIN_PEERS:
            misses += 1
            continue
        samples.append((values, sum(r['tokens'] for r in entry['runs'])))
    q, widen, coverage, reached = choose_quantile(samples, target)
    errors = [abs(statistics.median(v) - a) / a for v, a in samples]
    return {'jobs': len(samples), 'skipped_too_few_peers': misses, 'chosen_quantile': q, 'widen': widen,
            'target_reached': reached, 'coverage': round(coverage, 3) if coverage is not None else None,
            'median_abs_error_ratio': round(statistics.median(errors), 3) if errors else None,
            'within_25pct': round(sum(e <= .25 for e in errors) / len(errors), 3) if errors else None}


def estimate_for(kind, assignment, runs, jobs, q, widen=1.0):
    """Submission-time estimate for a new job: (low, median, high, peers) or None with too few peers.

    q and widen come from at_submission() so the band carries the coverage measured there."""
    values = candidates(kind, assignment, list(jobs.values()), table(runs))
    if len(values) < MIN_PEERS:
        return None
    low, high = band(values, q, widen)
    return round(low), round(statistics.median(values)), round(high), len(values)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--target-coverage', type=float, default=0.8)
    parser.add_argument('--json', type=Path)
    args = parser.parse_args()
    if not 0.5 <= args.target_coverage < 1:
        raise SystemExit('--target-coverage must be between 0.5 and 1')
    paths = databases()
    runs, jobs = collect(paths)
    stats = table(runs)
    result = {'databases': len(paths), 'final_runs': len(runs), 'complete_jobs': len(jobs),
              'target_coverage': args.target_coverage,
              'jobs_by_kind': {'/'.join(k): n for k, n in Counter(e['kind'] for e in jobs.values()).items()},
              'table': [{'provider': k[0], 'model': k[1], 'role': k[2], **v}
                        for k, v in sorted(stats.items(), key=lambda kv: tuple(map(str, kv[0])))],
              'after_planning': after_planning(runs, jobs, args.target_coverage),
              'at_submission': at_submission(runs, jobs, args.target_coverage)}
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k != 'table'}, ensure_ascii=False, indent=1, default=str))


if __name__ == '__main__':
    main()
