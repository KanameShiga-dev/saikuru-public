"""Restore verified project bytes and NTFS streams into a NEW local folder only."""
import argparse
import json
from pathlib import Path
import shutil
import zipfile

from team_archive import ARCHIVE_ROOT, digest
from team_ledger import Ledger, inside


def restore(archive_id, destination):
    ledger = Ledger(Path(__file__).parent / 'data')
    records = ledger.snapshot()['archives']
    record = next((r for r in records if r['id'] == archive_id), None)
    if not record or not record.get('zip_sha256') or not record.get('manifest_sha256'):
        raise ValueError('ZIP・照合記録の検証済みSHA-256がありません。')
    archive, manifest_path = Path(record['archive']).resolve(), Path(record['manifest']).resolve()
    if not archive.is_relative_to(ARCHIVE_ROOT) or not manifest_path.is_relative_to(ARCHIVE_ROOT):
        raise ValueError('アーカイブの場所が不正です。')
    with archive.open('rb') as src:
        if digest(src) != record['zip_sha256']:
            raise ValueError('ZIPのSHA-256が一致しません。')
    if record.get('manifest_entry'):
        import hashlib
        with zipfile.ZipFile(archive) as src:
            manifest_bytes = src.read(record['manifest_entry'])
        if hashlib.sha256(manifest_bytes).hexdigest() != record['manifest_sha256']:
            raise ValueError('ZIP内の照合記録のSHA-256が一致しません。')
        manifest = json.loads(manifest_bytes)
    else:
        with manifest_path.open('rb') as src:
            if digest(src) != record['manifest_sha256']:
                raise ValueError('照合記録のSHA-256が一致しません。')
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    dest = inside(destination)
    app = Path(__file__).parent.resolve()
    if dest.exists() or dest == inside(r'C:\Projects') or dest.is_relative_to(app) or app.is_relative_to(dest) or dest.is_relative_to(ARCHIVE_ROOT):
        raise ValueError('新しい空の復元先を C:\\Projects 以下に指定してください。既存フォルダには上書きしません。')
    if not dest.parent.is_dir():
        raise ValueError('復元先の親フォルダを先に作成してください。')
    name = Path(record['source']).name
    def target(rel):
        if Path(rel).is_absolute() or ':' in rel or '\\' in rel or '..' in Path(rel).parts:
            raise ValueError('照合記録の相対パスが不正です。')
        result = (dest / rel).resolve()
        if not result.is_relative_to(dest):
            raise ValueError('復元先のパスが不正です。')
        return result
    dest.mkdir()
    try:
        with zipfile.ZipFile(archive) as src:
            for rel in manifest['directories']:
                target(rel).mkdir(parents=True, exist_ok=True)
            for rel, meta in manifest['files'].items():
                p = target(rel)
                with src.open(name + '/' + rel) as entry, p.open('xb') as out:
                    shutil.copyfileobj(entry, out)
                with p.open('rb') as entry:
                    if digest(entry) != meta['sha256']:
                        raise ValueError('復元ファイルの照合に失敗しました。復元先を保持します。')
                for extra in meta.get('streams', []):
                    stream_name = extra['name']
                    if not stream_name.startswith(':') or not stream_name.endswith(':$DATA') or '/' in stream_name or '\\' in stream_name or stream_name.count(':') != 2:
                        raise ValueError('追加ストリーム名が不正です。')
                    with src.open(extra['zip_entry']) as entry, open(str(p) + stream_name, 'xb') as out:
                        shutil.copyfileobj(entry, out)
                    with open(str(p) + stream_name, 'rb') as entry:
                        if digest(entry) != extra['sha256']:
                            raise ValueError('追加ストリームの復元照合に失敗しました。')
    except Exception:
        # No automatic recursive cleanup of partial restoration.
        raise
    return str(dest)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='検証済みアーカイブを新規フォルダに復元（上書き禁止）')
    parser.add_argument('archive_id')
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    print(restore(args.archive_id, args.destination))
