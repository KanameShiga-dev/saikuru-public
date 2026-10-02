"""Lossless, immutable handoff snapshots for bounded worker reads."""
import hashlib
import json
import uuid
from pathlib import Path


def write_pages(package, directory, job_id, page_chars=24000):
    folder = Path(directory) / 'context-pages' / job_id / uuid.uuid4().hex
    folder.mkdir(parents=True, exist_ok=False)
    index = []
    files = []
    for section, value in package.items():
        text = json.dumps(value, ensure_ascii=False)
        parts = max(1, (len(text) + page_chars - 1) // page_chars)
        entries = []
        for part in range(parts):
            content = text[part * page_chars:(part + 1) * page_chars]
            path = folder / f'{len(index):02d}-{part + 1:03d}.txt'
            raw = content.encode('utf-8')
            path.write_bytes(raw)
            entries.append({'part': part + 1, 'path': str(path.resolve()),
                            'chars': len(content), 'sha256': hashlib.sha256(raw).hexdigest()})
            files.append(str(path.resolve()))
        index.append({'section': section, 'parts': parts, 'pages': entries})
    manifest = {'format': 'handoff-pages-v1', 'job_id': job_id,
                'instructions': '同じsectionの全partを番号順に連結すると元のJSON値になります。途中の断片だけで判断しない。最新の利用者判断・現在の事実は作業前に全partを読む。他の工程の報告は関係するsectionを読む。古い判断は時点と現在の方針を照合する。省略・要約は行っていません。',
                'required_before_work': ['current_facts', 'user_decisions_and_approvals'],
                'index': index}
    path = folder / 'index.json'
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    files.append(str(path.resolve()))
    return json.dumps(manifest, ensure_ascii=False), files
