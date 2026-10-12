"""Summarize a synthetic document stop without printing local paths or raw prompts."""
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

name = sys.argv[1] if len(sys.argv) > 1 else 'document1'
if name not in ('document1', 'document2', 'reviewctx-doc-on', 'reviewctx-doc-on-v2', 'reviewctx-doc-off'):
    raise SystemExit('Unknown synthetic document run')
database = Path(__file__).resolve().parents[2] / ('data/routing-comparison-20261012-' + name) / 'runtime/team.sqlite3'
with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as db:
    tasks = [json.loads(body) for (body,) in db.execute("SELECT body FROM objects WHERE kind='task'")]
    approvals = [json.loads(body) for (body,) in db.execute("SELECT body FROM objects WHERE kind='approval'")]
print(json.dumps({
    'task_statuses': {role + ':' + status: count for (role, status), count
                      in Counter((t['role'], t['status']) for t in tasks).items()},
    'task_results': [{'role': t['role'], 'status': t['status'],
                      'result_status': (t.get('result') or {}).get('status'),
                      'pending_checks': (t.get('result') or {}).get('pending_checks') if name.startswith('reviewctx-') else [],
                      'result_keys': sorted((t.get('result') or {}).keys()),
                      'factual_issue_keywords': [word for word in ('時刻', '冊', '返却', '予約', '期限', '見出し')
                                                if word in json.dumps(t.get('result') or {}, ensure_ascii=False)]
                      if t['role'] == 'reviewer' else [],
                      'question_ids': [q.get('id') for q in (t.get('result') or {}).get('questions', [])],
                      'question_keywords': [word for word in ('範囲', '上書き', 'ファイル', '許可', '資料', '確認')
                                            if word in str((t.get('result') or {}).get('questions', []))]}
                     for t in tasks],
    'approvals': [{'kind': a['kind'], 'status': a['status'],
                   'question_ids': [q.get('id') for q in a.get('payload', {}).get('questions', [])]}
                  for a in approvals],
}, ensure_ascii=False, indent=2))
