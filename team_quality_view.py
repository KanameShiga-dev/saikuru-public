"""Read-only QA view of a job, derived from records the engine already keeps (SQAH Phase 1).

It decides nothing. Acceptance, repair and blocking stay with the validation gate, the code-test
contract, the deliverable check, the reviews and the user. This module only recomputes what those
recorded, with the existing functions, and lays it out in four separate groups:

  facts      what a program established (exit codes, file hashes, deliverable presence)
  findings   what AI reviewers reported (never counted as evidence)
  unresolved what is still unconfirmed
  approval   what the user has decided

States: passed / failed / inconclusive / invalidated. It never starts a process or writes a file.
"""
import hashlib
from pathlib import Path

from team_code_test_contract import commands, verify_snapshot
from team_validation_gate import findings as gate_findings

PASSED, FAILED, INCONCLUSIVE, INVALIDATED = 'passed', 'failed', 'inconclusive', 'invalidated'


def _reviews(tasks):
    return [t for t in tasks if t.get('role') == 'reviewer']


def _latest_builder(tasks):
    builders = [t for t in tasks if t.get('role') == 'builder' and t.get('status') in ('succeeded', 'handed_off')]
    return builders[-1] if builders else None


def _verification(job, builder):
    """Validation commands and the code-test plan, via the engine's own checks."""
    listed = commands(job)
    plan = job.get('code_test_plan') or {}
    if not listed:
        reason = 'コード変更なし、またはテスト対象外（計画の理由を参照）' if plan and not plan.get('required') \
            else '検証コマンドが指定されていません'
        return {'id': 'CQA-03', 'state': INCONCLUSIVE, 'detail': reason, 'commands': 0}
    issues = gate_findings(listed, builder)
    state = FAILED if issues else PASSED
    return {'id': 'CQA-03', 'state': state, 'commands': len(listed),
            'detail': ' '.join(issues) if issues else '指定された検証コマンドがすべて終了コード0（直前の制作工程）'}


def _tests(job, builder):
    plan = job.get('code_test_plan') or {}
    if not plan.get('required'):
        return None
    snapshot = (builder or {}).get('test_asset_snapshot') or {}
    if not snapshot:
        return {'id': 'CQA-06', 'state': INCONCLUSIVE, 'detail': 'テストファイルの記録がありません', 'files': 0}
    if not verify_snapshot(job.get('project', ''), snapshot):
        return {'id': 'CQA-06', 'state': INVALIDATED, 'files': len(snapshot),
                'detail': 'テストファイルが記録後に変更・削除されています（再実行が必要）'}
    return {'id': 'CQA-06', 'state': PASSED, 'files': len(snapshot),
            'detail': '記録したテストファイルのハッシュが現在と一致'}


def _deliverable(job, events, recorded_hashes):
    """Presence from the engine's own event; hash drift against a hash recorded at QA time."""
    missing = [e for e in events if e.get('type') == 'deliverable_missing']
    if missing:
        return {'id': 'DQA-01', 'state': FAILED, 'detail': '成果物の存在確認で不足が記録されています'}
    if not recorded_hashes:
        return None
    drifted = []
    for name, digest in recorded_hashes.items():
        path = Path(job.get('project', '')) / name
        try:
            current = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            drifted.append(name)
            continue
        if current != digest:
            drifted.append(name)
    if drifted:
        return {'id': 'DQA-01', 'state': INVALIDATED, 'files': len(recorded_hashes),
                'detail': '検査後に成果物が変更されています: ' + ', '.join(sorted(drifted)[:5])}
    return {'id': 'DQA-01', 'state': PASSED, 'files': len(recorded_hashes), 'detail': '検査時のハッシュと現在が一致'}


def quality_view(job, tasks, approvals, events=(), recorded_hashes=None):
    """QA view of one job. All arguments are plain records; nothing is read from a database here."""
    builder = _latest_builder(tasks)
    facts = [item for item in (_verification(job, builder), _tests(job, builder),
                               _deliverable(job, list(events), recorded_hashes)) if item]
    reviews = [{'agent': t.get('agent_name') or 'reviewer', 'status': (t.get('result') or {}).get('status'),
                'summary_chars': len(str((t.get('result') or {}).get('summary') or ''))}
               for t in _reviews(tasks) if t.get('status') == 'succeeded']
    completion = [a for a in approvals if a.get('kind') == 'completion']
    latest = completion[-1] if completion else None
    pending = sum(len(item.get('checks') or []) for item in ((latest or {}).get('payload') or {}).get('pending_items') or [])
    states = [item['state'] for item in facts]
    if FAILED in states:
        overall = FAILED
    elif INVALIDATED in states:
        overall = INVALIDATED
    elif states and all(s == PASSED for s in states):
        overall = PASSED
    else:
        overall = INCONCLUSIVE
    return {
        'job_id': job.get('id'),
        'overall': overall,
        'facts': facts,
        'findings': {'source': 'ai_reviewers', 'counted_as_evidence': False, 'reviews': reviews},
        'unresolved': {'pending_items': pending, 'job_status': job.get('status')},
        'approval': {'status': (latest or {}).get('status'),
                     'accepted': job.get('status') == 'accepted',
                     'conditional': job.get('status') == 'accepted_with_pending_checks'},
        'decides_nothing': True,
    }
