"""Startup DB-pair check, paired backup/restore and release-candidate manifest.

Standalone on purpose (standard library only): PowerShell entry points run it with `python -I`.
Every check here is read-only. Nothing in this module overwrites an existing database;
backup and restore only ever write into a directory that does not exist yet.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

TEAM_DB = 'team.sqlite3'
HANDOFF_DB = 'handoff.sqlite3'
PAIR_FILES = (TEAM_DB, HANDOFF_DB)
REQUIRED_TABLES = {
    TEAM_DB: {'objects', 'events', 'transitions', 'metrics_meta'},
    HANDOFF_DB: {'records', 'facts', 'conflicts', 'skill_candidates', 'skill_versions',
                 'skill_active', 'skill_titles'},
}
SCHEMA_VERSION = 1
MANIFEST_NAME = 'pair-manifest.json'
IDENTITY_SQL = 'CREATE TABLE IF NOT EXISTS pair_identity (key TEXT PRIMARY KEY, value TEXT NOT NULL)'


class DeploymentError(Exception):
    """A refusal with a stable code. Nothing was written when this is raised by a check."""

    def __init__(self, code, message, db=None):
        super().__init__(message)
        self.code = code
        self.db = db

    def __str__(self):
        where = f' db={self.db}' if self.db else ''
        return f'[{self.code}]{where} {super().__str__()}'


GUIDANCE = ('書込みを行わずに停止しました。DB を作り直さず、バックアップから別フォルダへ復元して'
            '内容を確認してください: python -I team_deployment.py restore')


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def _open_readonly(path):
    """Read-only open that creates no -wal/-shm. Without side files the main file is the whole database,
    so `immutable=1` is exact; if side files exist they may hold data, so they are read via `mode=ro`."""
    path = Path(path).resolve()
    side_files = any(Path(str(path) + suffix).exists() for suffix in ('-wal', '-shm'))
    return sqlite3.connect(path.as_uri() + ('?mode=ro' if side_files else '?immutable=1'), uri=True)


def _counts(db):
    names = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {name: db.execute('SELECT count(*) FROM "%s"' % name.replace('"', '""')).fetchone()[0]
            for name in names}


def inspect_db(path, name):
    """Read-only state of one DB: missing | empty | ready | foreign | corrupt | newer."""
    path = Path(path)
    if not path.exists():
        return {'state': 'missing'}
    try:
        with closing(_open_readonly(path)) as db:
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                return {'state': 'corrupt'}
            counts = _counts(db)
            if not counts:
                return {'state': 'empty'}
            if not REQUIRED_TABLES[name] <= set(counts):
                return {'state': 'foreign'}
            identity = {}
            if 'pair_identity' in counts:
                identity = dict(db.execute('SELECT key, value FROM pair_identity').fetchall())
            if int(identity.get('schema_version', SCHEMA_VERSION)) > SCHEMA_VERSION:
                return {'state': 'newer'}
            return {'state': 'ready', 'pair_id': identity.get('pair_id'), 'counts': counts}
    except sqlite3.DatabaseError:
        return {'state': 'corrupt'}


def inspect_pair(directory):
    """Classify the pair in `directory` without writing. Returns {'state': 'fresh'|'ready', ...} or raises."""
    directory = Path(directory)
    found = {name: inspect_db(directory / name, name) for name in PAIR_FILES}
    for name, info in found.items():
        if info['state'] == 'corrupt':
            raise DeploymentError('db_corrupt', '読み取れない、または整合性検査に失敗したデータベースです。' + GUIDANCE, name)
        if info['state'] == 'foreign':
            raise DeploymentError('schema_unknown', '期待するテーブルを持たないデータベースです。' + GUIDANCE, name)
        if info['state'] == 'newer':
            raise DeploymentError('schema_newer', 'この版より新しいスキーマです。古い版で起動しないでください。' + GUIDANCE, name)
    blank = {'missing', 'empty'}
    states = [found[name]['state'] for name in PAIR_FILES]
    if all(state in blank for state in states):
        return {'state': 'fresh'}
    for name in PAIR_FILES:
        if found[name]['state'] in blank:
            raise DeploymentError('pair_partial', '片方のデータベースだけが存在します。' + GUIDANCE, name)
    ids = {found[name].get('pair_id') for name in PAIR_FILES} - {None}
    if len(ids) > 1:
        raise DeploymentError('pair_mismatch', 'team と handoff が別のペアのものです。' + GUIDANCE)
    return {'state': 'ready', 'pair_id': ids.pop() if ids else None,
            'counts': {name: found[name]['counts'] for name in PAIR_FILES}}


def require_pair(directory):
    """Startup gate shared by every entry point. Raises DeploymentError before any write."""
    return inspect_pair(directory)


def register_identity(db, directory, peer_name):
    """Called by Store/HandoffDB on their own writable connection: share one pair id, record the schema version."""
    db.execute(IDENTITY_SQL)
    if db.execute("SELECT 1 FROM pair_identity WHERE key='pair_id'").fetchone() is None:
        peer = inspect_db(Path(directory) / peer_name, peer_name)
        db.execute("INSERT INTO pair_identity VALUES ('pair_id', ?)", (peer.get('pair_id') or uuid.uuid4().hex,))
    db.execute("INSERT OR IGNORE INTO pair_identity VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))


@contextmanager
def _instance_lock(directory):
    """Same lock file and byte as server.DataLock: a running server makes this refuse."""
    with open(Path(directory) / 'instance.lock', 'a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise DeploymentError('in_use', '統括サービスが稼働中です。停止して書込みが止まった状態で実行してください。') from None
        yield


def backup_pair(source, dest):
    """Copy a quiesced, consistent pair into a new directory and write a hash manifest."""
    source, dest = Path(source), Path(dest)
    if inspect_pair(source)['state'] != 'ready':
        raise DeploymentError('nothing_to_backup', 'バックアップ対象のデータベースペアがありません。')
    if dest.exists():
        raise DeploymentError('dest_exists', '出力先が既に存在します。新しいフォルダを指定してください。')
    with _instance_lock(source):
        dest.mkdir(parents=True)
        try:
            for name in PAIR_FILES:
                with closing(_open_readonly(source / name)) as src, closing(sqlite3.connect(dest / name)) as out:
                    src.backup(out)
                    # Self-contained file: no -wal/-shm needed to read the copy.
                    out.execute('PRAGMA journal_mode=DELETE')
            state = inspect_pair(dest)
            manifest = {'format': 1, 'pair_id': state['pair_id'], 'schema_version': SCHEMA_VERSION,
                        'files': {name: {'size': (dest / name).stat().st_size, 'sha256': _sha256(dest / name),
                                         'counts': state['counts'][name]} for name in PAIR_FILES}}
            (dest / MANIFEST_NAME).write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding='utf-8')
        except BaseException:
            shutil.rmtree(dest, ignore_errors=True)
            raise
    return manifest


def restore_pair(backup, target):
    """Restore a verified backup into a directory that does not exist yet, then re-inspect it."""
    backup, target = Path(backup), Path(target)
    manifest = json.loads((backup / MANIFEST_NAME).read_text(encoding='utf-8'))
    for name in PAIR_FILES:
        entry = manifest['files'][name]
        file = backup / name
        if not file.is_file() or file.stat().st_size != entry['size'] or _sha256(file) != entry['sha256']:
            raise DeploymentError('manifest_mismatch', 'バックアップがマニフェストと一致しません。', name)
    if target.exists():
        raise DeploymentError('dest_exists', '復元先が既に存在します。新しいフォルダを指定してください。')
    staging = target.with_name(target.name + '.restoring')
    if staging.exists():
        raise DeploymentError('dest_exists', '復元の作業フォルダが残っています: ' + staging.name)
    staging.mkdir(parents=True)
    try:
        for name in PAIR_FILES:
            shutil.copy2(backup / name, staging / name)
        state = inspect_pair(staging)
        if state['state'] != 'ready' or state['pair_id'] != manifest['pair_id']:
            raise DeploymentError('restore_mismatch', '復元したペアの識別子が一致しません。')
        for name in PAIR_FILES:
            if state['counts'][name] != manifest['files'][name]['counts']:
                raise DeploymentError('restore_mismatch', '復元したデータの件数が一致しません。', name)
        staging.rename(target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return state


EXCLUDED_PARTS = {'data', '.git', '__pycache__', '.test-output', '.local'}
EXCLUDED_SUFFIXES = {'.sqlite3', '.sqlite3-wal', '.sqlite3-shm', '.db', '.log', '.key', '.pem', '.pfx', '.lock'}
EXCLUDED_NAME_WORDS = ('secret', 'credential', 'password')
COMMIT = re.compile(r'[0-9a-f]{40}')


def excluded_reason(relative):
    parts = relative.split('/')
    name = parts[-1].lower()
    if EXCLUDED_PARTS & set(parts[:-1]) or name in EXCLUDED_PARTS:
        return 'excluded folder'
    if name.startswith('.env') or Path(name).suffix in EXCLUDED_SUFFIXES or any(w in name for w in EXCLUDED_NAME_WORDS):
        return 'operational or secret file type'
    return None


def build_release_manifest(root, allowlist, commit):
    """Deterministic manifest from an explicit allowlist. An excluded or missing path is an error, never skipped."""
    root = Path(root).resolve()
    if not COMMIT.fullmatch(commit):
        raise DeploymentError('bad_commit', '対象版は 40 桁の小文字 16 進のコミット ID で指定してください。')
    files = []
    for relative in sorted(set(allowlist)):
        pure = Path(relative)
        if pure.is_absolute() or '..' in pure.parts or '\\' in relative or relative.startswith('/'):
            raise DeploymentError('bad_path', '相対パス（/ 区切り、.. なし）だけを指定できます: ' + relative)
        reason = excluded_reason(relative)
        if reason:
            raise DeploymentError('excluded_path', f'{relative}: {reason}のため配布候補にできません。')
        file = (root / relative).resolve()
        if not file.is_relative_to(root) or not file.is_file():
            raise DeploymentError('missing_path', '配布候補のファイルが見つかりません: ' + relative)
        files.append({'path': relative, 'size': file.stat().st_size, 'sha256': _sha256(file),
                      'kind': file.suffix.lstrip('.').lower() or 'none'})
    return {'format': 1, 'commit': commit, 'files': files}


def verify_release_manifest(root, manifest):
    """Return a list of problems; an empty list means every listed file matches and none is excluded."""
    root = Path(root).resolve()
    problems = []
    seen = set()
    for entry in manifest['files']:
        relative = entry['path']
        if relative in seen:
            problems.append('duplicate: ' + relative)
        seen.add(relative)
        if Path(relative).is_absolute() or '..' in Path(relative).parts or excluded_reason(relative):
            problems.append('not allowed: ' + relative)
            continue
        file = (root / relative).resolve()
        if not file.is_relative_to(root) or not file.is_file():
            problems.append('missing: ' + relative)
        elif file.stat().st_size != entry['size'] or _sha256(file) != entry['sha256']:
            problems.append('changed: ' + relative)
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    check = sub.add_parser('check')
    check.add_argument('--data', type=Path, default=Path(__file__).resolve().parent / 'data')
    backup = sub.add_parser('backup')
    backup.add_argument('--data', type=Path, default=Path(__file__).resolve().parent / 'data')
    backup.add_argument('--to', type=Path, required=True)
    restore = sub.add_parser('restore')
    restore.add_argument('--from', dest='source', type=Path, required=True)
    restore.add_argument('--to', type=Path, required=True)
    manifest = sub.add_parser('manifest')
    manifest.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    manifest.add_argument('--commit', required=True)
    manifest.add_argument('--out', type=Path, required=True)
    manifest.add_argument('paths', nargs='+')
    verify = sub.add_parser('verify-manifest')
    verify.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    verify.add_argument('manifest', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == 'check':
            print(require_pair(args.data)['state'])
        elif args.action == 'backup':
            print(json.dumps(backup_pair(args.data, args.to)['files'], sort_keys=True))
        elif args.action == 'restore':
            print(restore_pair(args.source, args.to)['state'])
        elif args.action == 'manifest':
            result = build_release_manifest(args.root, args.paths, args.commit)
            with open(args.out, 'x', encoding='utf-8') as handle:
                json.dump(result, handle, indent=1)
        else:
            problems = verify_release_manifest(args.root, json.loads(args.manifest.read_text(encoding='utf-8')))
            print('\n'.join(problems) or 'ok')
            return 2 if problems else 0
    except DeploymentError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
