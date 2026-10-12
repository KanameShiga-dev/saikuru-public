"""Correct one impossible synthetic UI check, preserving an explicit audit event."""
import copy
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from team_store import Store

OLD = '再受領ボタンを押す前後の状態を確認'
NEW = '条件付き受領後も残る未確認項目が表示されていることを確認'


def main():
    base = Path(sys.argv[1]).resolve()
    if base.parent != (ROOT / 'data').resolve() or not base.name.startswith('routing-human-'):
        raise ValueError('Synthetic human fixture only')
    manifest = json.loads((base / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('purpose') != 'synthetic human acceptance UI':
        raise ValueError('Not the human acceptance fixture')
    store = Store(base / 'runtime')
    try:
        with store.atomic():
            job = store.get(manifest['job_id'], 'job')
            if job['status'] != 'accepted_with_pending_checks':
                raise ValueError('Expected conditional acceptance')
            approvals = [a for a in store.all('approval')
                         if a['job_id'] == job['id'] and a['kind'] == 'completion']
            approval = approvals[-1]
            if approval['status'] != 'approved':
                raise ValueError('Expected most recent approved completion')
            payload = copy.deepcopy(approval['payload'])
            items = payload.get('pending_items') or []
            if len(items) != 1 or items[0].get('checks') != [OLD]:
                raise ValueError('The original check no longer matches')
            items[0]['checks'] = [NEW]
            digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            previous_digest = approval['digest']
            store.update(approval['id'], payload=payload, digest=digest)
            store.event('synthetic_fixture_check_corrected',
                        '隔離評価の循環した確認項目を、条件付き受領後に観察可能な項目へ修正しました。'
                        '利用者の根拠と受領状態は変更していません。', approval['task_id'], job['id'])
        (base / 'fixture-correction.json').write_text(json.dumps({
            'old_check': OLD, 'new_check': NEW, 'previous_digest': previous_digest,
            'updated_digest': digest, 'approval_id': approval['id']}, ensure_ascii=False, indent=2), encoding='utf-8')
        print('CORRECTED_SYNTHETIC_CHECK', flush=True)
    finally:
        store.db.close()


if __name__ == '__main__':
    main()
