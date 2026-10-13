"""Write content-free evaluation evidence from a chosen isolated run."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from team_optimization_metrics import attempts
from eval.development_readiness.evaluate import score
from eval.development_readiness.check_cli import check


def main():
    base = Path(sys.argv[1]).resolve()
    rows, _, _ = attempts(base/'runtime/team.sqlite3')
    review = score(json.loads((base/'review_judgments.json').read_text(encoding='utf-8')))
    result = {
        'total_tokens': sum((r.get('input_total') or 0)+(r.get('output') or 0) for r in rows),
        'unknown_runs': sum(r['measurement']=='unknown' for r in rows),
        'partial_runs': sum(r['measurement']=='partial' for r in rows),
        'runs': [{k: r.get(k) for k in ('role', 'provider', 'model', 'status', 'measurement',
                                       'input_total', 'output', 'first_call_input')} for r in rows],
        'seeded_review': review,
        'development_checks': check(base/'development'),
        'human_active_minutes': None,
        'human_acceptance': 'not_performed',
        'complete_workflow': False,
    }
    (base/'evaluation_summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('total_tokens', 'unknown_runs', 'partial_runs', 'seeded_review', 'complete_workflow')}, ensure_ascii=False))


if __name__ == '__main__': main()
