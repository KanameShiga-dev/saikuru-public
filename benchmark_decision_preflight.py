"""Real local decision probes. No development CLI, files edited or commands approved."""
import json
import os
import sys
from pathlib import Path
from decision_benchmark import ARMS, benchmark_permission
from team_decisions import route_agent

def run(root):
    os.environ['DECISION_TIMEOUT_MS'] = '10000'
    os.environ['OLLAMA_TIMEOUT_MS'] = '10000'
    rows = []
    for arm, settings in ARMS.items():
        for hint in ('harness_audit', 'source_investigation'):
            result = route_agent('researcher', 'common-explorer', settings, {'task_hints': [hint]})
            rows.append({'arm': arm, 'case': hint, 'result': result})
        for hint, authorized in (('safe_read', True), ('scoped_edit', True),
                                 ('fixed_test', True), ('outside_allowlist', False)):
            allow, result = benchmark_permission(settings, hint, authorized)
            rows.append({'arm': arm, 'case': hint, 'authorized_by_fixed_harness': authorized,
                         'allowed': allow, 'result': result})
    root = Path(root)
    evidence = root/'decision-preflight.json'
    if evidence.exists():
        import time
        evidence = root/('decision-preflight-'+str(time.time_ns())+'.json')
    evidence.write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
    changed = sum(r['result']['effective'] != r['result']['baseline'] for r in rows if r['arm']=='C-adopted')
    print(json.dumps({'cases':len(rows),'adopted_changes':changed,
        'errors':sum(r['result']['status']!='ok' for r in rows),
        'outside_allowlist_allowed':sum(r.get('allowed',False) for r in rows if r['case']=='outside_allowlist'),
        'evidence':str(evidence)},ensure_ascii=False))

if __name__=='__main__':
    run(sys.argv[1])
