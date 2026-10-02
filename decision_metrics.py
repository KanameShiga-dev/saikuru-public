"""Read-only aggregate of decision evidence; no API calls or raw task text."""
import json
import sqlite3
from pathlib import Path


def report(database):
    with sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True) as db:
        records = []
        for (body,) in db.execute("SELECT body FROM objects WHERE kind='task'"):
            task = json.loads(body)
            if isinstance(task.get('decision_record'), dict):
                records.append((task['decision_record'], task.get('status')))
    counts = {}
    tokens = {}
    local_tokens = {}
    latencies = []
    for decision, status in records:
        key = str(decision.get('provider')) + '/' + str(decision.get('status'))
        counts[key] = counts.get(key, 0) + 1
        latencies.append(decision.get('latency_ms', 0))
        for key, value in decision.get('usage', {}).items():
            if type(value) is int and value >= 0:
                target = local_tokens if decision.get('provider') == 'ollama' else tokens
                target[key] = target.get(key, 0) + value
    latencies.sort()
    return {'records': len(records), 'provider_status_counts': counts,
            'differences': sum(bool(d.get('differs')) for d, _ in records),
            'shadow_records': sum(bool(d.get('shadow')) for d, _ in records),
            'human_candidates': sum(d.get('decision') == 'HUMAN' for d, _ in records),
            'task_successes': sum(s == 'succeeded' for _, s in records),
            'latency_ms_mean': sum(latencies)/len(latencies) if latencies else None,
            'latency_ms_p95': latencies[max(0, (len(latencies)*95+99)//100-1)] if latencies else None,
            'reported_api_tokens': tokens or None,
            'reported_local_tokens': local_tokens or None,
            'limitations': 'Latest decision per task only. Differences are not accuracy; no inferred tokens or quota savings.'}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', default=str(Path(__file__).parent/'data/team.sqlite3'))
    print(json.dumps(report(parser.parse_args().database), ensure_ascii=False, indent=2))
