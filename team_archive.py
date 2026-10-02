"""Explicit local ZIP archiving. Originals are removed only after byte verification."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import threading
import time
import zipfile
from contextlib import ExitStack

from team_ledger import ROOT, inside, stamp
from team_github_retirement import plan as github_plan, retire as github_retire

ARCHIVE_ROOT = ROOT / '_project_archives'
LIVE = {'preparing', 'github_retiring', 'compressing', 'verifying', 'verified', 'deleting'}


def overlaps(a, b):
    a, b = Path(a).resolve(), Path(b).resolve()
    return a.is_relative_to(b) or b.is_relative_to(a)


def validate_path(path):
    raw = Path(path)
    resolved = inside(raw)
    if resolved == ROOT or resolved.is_relative_to(ARCHIVE_ROOT):
        raise ValueError('作業ルート・アーカイブ保管場所は対象にできません。')
    for part in [raw] + list(raw.parents):
        if part == ROOT.parent:
            break
        if part.exists() and part.lstat().st_file_attributes & 0x400:
            raise ValueError('リンク・ジャンクションを含むパスは対象にできません。')
    return resolved


def alternate_streams(path):
    # Named streams are preserved in separate ZIP entries plus the manifest.
    import ctypes
    from ctypes import wintypes
    class StreamData(ctypes.Structure):
        _fields_ = [('size', ctypes.c_longlong), ('name', wintypes.WCHAR * 296)]
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    first = api.FindFirstStreamW
    first.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(StreamData), wintypes.DWORD]
    first.restype = wintypes.HANDLE
    next_stream = api.FindNextStreamW
    next_stream.argtypes = [wintypes.HANDLE, ctypes.POINTER(StreamData)]
    next_stream.restype = wintypes.BOOL
    close = api.FindClose
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    data = StreamData()
    handle = first(str(path), 0, ctypes.byref(data), 0)
    if handle == wintypes.HANDLE(-1).value:
        error = ctypes.get_last_error()
        if error == 38:  # No streams (empty directory or zero-byte file).
            return []
        raise ValueError('NTFSストリームを確認できません。削除は実行しません。')
    streams = []
    try:
        while True:
            if data.name not in ('::$DATA', ''):
                streams.append({'name': data.name, 'size': data.size})
            if not next_stream(handle, ctypes.byref(data)):
                if ctypes.get_last_error() not in (38, 18):
                    raise ValueError('NTFSストリームの確認に失敗しました。')
                return sorted(streams, key=lambda s:s['name'])
    finally:
        close(handle)


def inventory(root, check=None):
    files, dirs = {}, []
    stack = [root]
    while stack:
        directory = stack.pop()
        if check:
            check()
        st = directory.lstat()
        if st.st_file_attributes & (0x400 | 0x4000) or alternate_streams(directory):
            raise ValueError('リンク・暗号化・追加ストリームのあるフォルダはZIPで安全に保存できません。')
        dirs.append(directory.relative_to(root).as_posix())
        for p in sorted(directory.iterdir()):
            st = p.lstat()
            if st.st_file_attributes & (0x400 | 0x4000):
                raise ValueError('リンク・暗号化ファイルがあるため中止しました。')
            if stat.S_ISDIR(st.st_mode):
                stack.append(p)
            elif stat.S_ISREG(st.st_mode):
                if p.name == '.git' or (p.name == 'commondir' and '.git' in p.parts):
                    raise ValueError('外部Git管理領域を参照する作業ツリー・サブモジュールは対象にできません。')
                files[p.relative_to(root).as_posix()] = {'size': st.st_size, 'mtime_ns': st.st_mtime_ns,
                    'mode': stat.S_IMODE(st.st_mode), 'streams': alternate_streams(p)}
            else:
                raise ValueError('通常のファイル以外があるため中止しました。')
    return {'files': files, 'directories': sorted(dirs)}


def signature(items):
    return hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()


def digest(stream, check=None):
    sha = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        if check:
            check()
        sha.update(chunk)
    return sha.hexdigest()


def exclusive_read(path, delete_access=False, share_delete=False):
    """Keep Windows writers away; deletion uses the very handle whose bytes we check."""
    import ctypes
    from ctypes import wintypes
    import msvcrt
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    api.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    api.SetFileInformationByHandle.restype = wintypes.BOOL
    handle = api.CreateFileW(str(path), 0x80000000 | (0x10000 if delete_access else 0),
                            1 | (4 if share_delete else 0), None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ValueError('書き込み中・使用中・権限不足のファイルがあります。残りのフォルダとZIPを保持しました。')
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        api.CloseHandle(handle)
        raise
    return os.fdopen(fd, 'rb'), handle, api


def remove_verified(path, meta, check):
    import ctypes
    if path.lstat().st_file_attributes & 1:
        path.chmod(stat.S_IWRITE | stat.S_IREAD)
    stream, handle, api = exclusive_read(path, True)
    with ExitStack() as handles:
        handles.enter_context(stream)
        if digest(stream, check) != meta['sha256']:
            raise ValueError('削除直前の内容が変わりました。残りは保持します。')
        for extra in meta['streams']:
            named, _, _ = exclusive_read(str(path) + extra['name'], share_delete=True)
            handles.enter_context(named)
            if digest(named, check) != extra['sha256']:
                raise ValueError('削除直前に追加ストリームが変わりました。削除を止めました。')
        check()
        disposition = ctypes.c_ubyte(1)  # FILE_DISPOSITION_INFO.DeleteFile
        if not api.SetFileInformationByHandle(handle, 4, ctypes.byref(disposition), 1):
            raise ValueError('使用中・権限などにより削除できません。ZIPと残りのフォルダを保持しました。')


class Archives:
    def __init__(self, ledger, engine):
        self.ledger, self.engine = ledger, engine
        self.previews = {}
        self.preview_requests = {}
        self.thread = None
        with ledger.lock, ledger.db:
            ledger.db.execute('CREATE TABLE IF NOT EXISTS archives(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, body TEXT NOT NULL)')
            for object_id, raw in ledger.db.execute('SELECT id,body FROM archives').fetchall():
                item = json.loads(raw)
                if item['state'] in LIVE:
                    item.update(state='interrupted', message='サービス停止で中断しました。ZIP・元フォルダ・退避場所を確認して復旧してください。', updated_at=stamp())
                    ledger.db.execute('UPDATE archives SET body=? WHERE id=?', (json.dumps(item, ensure_ascii=False), object_id))

    def records(self):
        with self.ledger.lock:
            return [json.loads(row[0]) for row in self.ledger.db.execute('SELECT body FROM archives ORDER BY rowid DESC')]

    def save(self, record, **fields):
        record.update(fields, updated_at=stamp())
        with self.ledger.lock, self.ledger.db:
            self.ledger.db.execute('INSERT INTO archives VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',
                (record['id'], record['project_id'], json.dumps(record, ensure_ascii=False)))

    def blocked(self, path):
        return any(overlaps(path, r['source']) and (r['state'] in LIVE or
            (r['state'] in ('completed', 'interrupted', 'needs_recovery') and not Path(r['source']).is_dir())) for r in self.records())

    def project(self, project_id):
        if not isinstance(project_id, str) or len(project_id) > 100:
            raise ValueError('プロジェクトIDが不正です。')
        with self.ledger.lock:
            row = self.ledger.db.execute('SELECT body FROM projects WHERE id=?', (project_id,)).fetchone()
        if not row:
            raise ValueError('プロジェクトが見つかりません。')
        return json.loads(row[0])

    def eligible(self, project):
        path = validate_path(project['path'])
        if any(json.loads(row[0])['state'] in ('preparing', 'moving', 'needs_recovery') for row in self.ledger.db.execute('SELECT body FROM moves').fetchall()):
            raise ValueError('フォルダ移動が処理中、または復旧確認待ちです。移動の状態を確認してください。')
        if not path.is_dir():
            raise ValueError('元フォルダがありません。台帳・アーカイブ履歴を確認してください。')
        if overlaps(path, Path(__file__).parent) or overlaps(path, self.ledger.directory):
            raise ValueError('稼働中の采来 — サイクル —・台帳DBを含むフォルダは対象にできません。')
        if (path / '.git').is_file() or any((p / '.git').exists() for p in path.parents if p != ROOT and p.is_relative_to(ROOT)):
            raise ValueError('親Gitプロジェクトの一部分・外部Git作業ツリーは対象にできません。')
        others = self.ledger.snapshot()['projects']
        if any(p['id'] != project['id'] and Path(p['path']).is_dir() and Path(p['path']).resolve().is_relative_to(path) for p in others):
            raise ValueError('他の台帳プロジェクトが入っています。内側のプロジェクトを個別にアーカイブしてください。')
        for job in self.engine.store.all('job'):
            if overlaps(path, job['project']) and job['status'] in ('planning', 'running', 'awaiting_approval'):
                raise ValueError('この範囲に未完了の実行・待機依頼があります。依頼を終えるか中止してから実施してください。')
        for task in self.engine.store.all('task'):
            if task['id'] in self.engine.active or task['status'] in ('running', 'awaiting_approval'):
                job = self.engine.store.get(task['job_id'])
                if overlaps(path, job['project']):
                    raise ValueError('対象プロジェクトの作業が実行中です。')
        if any(r['state'] in LIVE for r in self.records()):
            raise ValueError('別のアーカイブを実行中です。完了後に操作してください。')
        if ARCHIVE_ROOT.exists():
            inside(ARCHIVE_ROOT)
            if ARCHIVE_ROOT.lstat().st_file_attributes & 0x400:
                raise ValueError('保管フォルダがリンクになっています。処理できません。')
        return path

    def preview(self, body):
        operation = body.get('operation', 'archive')
        if operation not in ('archive', 'delete'):
            raise ValueError('操作の種類が不正です。')
        with self.engine.store.lock:
            project = self.project(body.get('id'))
            path = self.eligible(project)
        def check():
            if self.engine.shutdown.is_set():
                raise ValueError('サービスが停止しました。再読み込みして確認をやり直してください。')
        items = inventory(path, check)
        total = sum(f['size'] + sum(s['size'] for s in f['streams']) for f in items['files'].values())
        github = github_plan(path, body.get('github_repositories', []))
        free = shutil.disk_usage(ROOT).free
        required = total + (len(items['files']) + len(items['directories']) + sum(len(f['streams']) for f in items['files'].values())) * 4096 + 100 * 1024**2
        if free < required:
            raise ValueError('圧縮用の空き容量が不足しています。元フォルダは削除しません。')
        token, object_id = secrets.token_urlsafe(32), secrets.token_hex(12)
        preview = {'token': token, 'id': object_id, 'project_id': project['id'], 'source': str(path), 'operation': operation,
            'github_plan': github, 'stream_count': sum(len(f['streams']) for f in items['files'].values()),
            'archive': str(ARCHIVE_ROOT / object_id / 'project.zip'), 'quarantine': str(path.parent / ('_project_archive_pending_' + object_id)),
            'files': len(items['files']), 'bytes': total, 'directories': len(items['directories']),
            'fingerprint': signature(items), 'expires': time.time() + 900, 'free_bytes': free}
        with self.engine.store.lock:
            self.previews = {k: v for k, v in self.previews.items() if v['expires'] > time.time()}
            self.previews[token] = preview
        return {k: v for k, v in preview.items() if k not in ('fingerprint',)}

    def request_preview(self, body):
        self.project(body.get('id'))
        request_id = secrets.token_hex(12)
        with self.engine.store.lock:
            self.preview_requests = {k:v for k,v in self.preview_requests.items() if v['created'] > time.time() - 3600}
            if sum(v['status'] == 'working' for v in self.preview_requests.values()) >= 2:
                raise ValueError('対象確認が実行中です。完了後に操作してください。')
            self.preview_requests[request_id] = {'status': 'working', 'created': time.time(), 'message': 'ファイルとGitHub公開先を確認しています。'}
        def run_preview():
            try:
                result = self.preview(body)
                value = {'status': 'ready', 'preview': result}
            except Exception as exc:
                value = {'status': 'failed', 'error': str(exc) if isinstance(exc, ValueError) else '対象確認に失敗しました。使用中・権限・接続を確認してください。'}
            with self.engine.store.lock:
                self.preview_requests[request_id].update(value)
        threading.Thread(target=run_preview, daemon=True).start()
        return {'request_id': request_id, 'status': 'working'}

    def preview_status(self, request_id):
        with self.engine.store.lock:
            if request_id not in self.preview_requests:
                return {'status': 'failed', 'error': '対象確認が見つかりません。確認ボタンを押し直してください。'}
            return dict(self.preview_requests[request_id])

    def start(self, body):
        if not isinstance(body.get('token'), str) or len(body['token']) > 200:
            raise ValueError('確認トークンが不正です。')
        with self.engine.store.lock:
            preview = self.previews.get(body.get('token'))
            if not preview or preview['expires'] < time.time():
                raise ValueError('確認の有効期限が切れました。もう一度対象を確認してください。')
            if body.get('confirmed_delete') is not True or body.get('closed_apps') is not True or body.get('github_reviewed') is not True or body.get('confirmed_path') != preview['source']:
                raise ValueError('対象パス・アプリ停止・元フォルダ削除の確認が必要です。')
            self.eligible(self.project(preview['project_id']))
            del self.previews[body['token']]
            self.ledger.backup()
            record = {k: v for k, v in preview.items() if k not in ('token', 'expires', 'free_bytes')}
            record.update(state='preparing', created_at=stamp(), completed_files=0,
                          message='対象の再確認を行っています。', zip_sha256='', manifest='')
            self.save(record)
            self.thread = threading.Thread(target=self.run, args=(record,), daemon=True)
            self.thread.start()
        return {'ok': True, 'archive_id': record['id']}

    def run(self, record):
        source, quarantine = Path(record['source']), Path(record['quarantine'])
        moved, deleting = False, False
        archive_lock = None
        def check():
            if self.engine.shutdown.is_set():
                raise ValueError('サービス停止により中断しました。保存したZIPとフォルダを確認してください。')
        try:
            validate_path(source)
            items = inventory(source, check)
            if signature(items) != record['fingerprint']:
                raise ValueError('確認画面の後に内容が変わりました。元フォルダを保持し、中止しました。')
            self.save(record, state='github_retiring', message='GitHubを非公開化・確認し、接続を解除しています。')
            def github_progress(changes, removed):
                self.save(record, github_changes=changes, removed_remotes=removed)
            github_retire(source, record['github_plan'], github_progress, check)
            # Git remote removal intentionally changes .git/config; inventory the confirmed result.
            after_github = inventory(source, check)
            def non_git(snapshot):
                return {'files': {k:v for k,v in snapshot['files'].items() if k != '.git' and not k.startswith('.git/')},
                        'directories': [k for k in snapshot['directories'] if k != '.git' and not k.startswith('.git/')]}
            if signature(non_git(items)) != signature(non_git(after_github)):
                raise ValueError('GitHub処理中にプロジェクトのファイルが変わりました。ローカル削除を止めました。')
            items = after_github
            self.save(record, fingerprint=signature(items), files=len(items['files']))
            archive = Path(record['archive'])
            inside(archive)
            archive.parent.mkdir(parents=True, exist_ok=False)
            partial = archive.with_suffix('.partial')
            self.save(record, state='compressing', partial_archive=str(partial), message='全ファイルを圧縮しています。')
            with zipfile.ZipFile(partial, 'x', zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as output:
                for rel in items['directories']:
                    output.writestr(source.name + ('/' + rel if rel != '.' else '') + '/', b'')
                for index, (rel, meta) in enumerate(items['files'].items(), 1):
                    check()
                    p = source / rel
                    info = zipfile.ZipInfo.from_file(p, source.name + '/' + rel, strict_timestamps=False)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    sha = hashlib.sha256()
                    with p.open('rb') as src, output.open(info, 'w', force_zip64=True) as dst:
                        while chunk := src.read(1024 * 1024):
                            check()
                            sha.update(chunk)
                            dst.write(chunk)
                    meta['sha256'] = sha.hexdigest()
                    for stream_index, extra in enumerate(meta['streams']):
                        extra['zip_entry'] = '_agent_team_ntfs_' + record['id'] + '/' + hashlib.sha256(rel.encode()).hexdigest() + '/' + str(stream_index)
                        sha = hashlib.sha256()
                        with open(str(p) + extra['name'], 'rb') as src, output.open(extra['zip_entry'], 'w', force_zip64=True) as dst:
                            while chunk := src.read(1024 * 1024):
                                check()
                                sha.update(chunk)
                                dst.write(chunk)
                        extra['sha256'] = sha.hexdigest()
                    if index % 50 == 0 or index == record['files']:
                        self.save(record, completed_files=index)
                manifest_bytes = json.dumps({'source': str(source), 'created_at': stamp(), **items}, ensure_ascii=False, indent=2).encode('utf-8')
                manifest_entry = '_agent_team_manifest_' + record['id'] + '.json'
                output.writestr(manifest_entry, manifest_bytes)
            with partial.open('r+b') as stream:
                os.fsync(stream.fileno())
            self.save(record, state='verifying', message='ZIPを読み戻し、全ファイルのSHA-256を照合しています。')
            with zipfile.ZipFile(partial) as output:
                if len(output.infolist()) != len(items['files']) + len(items['directories']) + sum(len(f['streams']) for f in items['files'].values()) + 1:
                    raise ValueError('ZIPの格納件数が一致しません。')
                for rel, meta in items['files'].items():
                    with output.open(source.name + '/' + rel) as src:
                        if digest(src, check) != meta['sha256']:
                            raise ValueError('ZIPの内容照合に失敗しました。')
                    for extra in meta['streams']:
                        with output.open(extra['zip_entry']) as src:
                            if digest(src, check) != extra['sha256']:
                                raise ValueError('追加ストリームの照合に失敗しました。削除しません。')
                if output.read(manifest_entry) != manifest_bytes:
                    raise ValueError('ZIP内の照合記録が一致しません。削除しません。')
            check()
            # Metadata and bytes are checked again immediately before any move/delete.
            current = inventory(source, check)
            if signature(current) != record['fingerprint']:
                raise ValueError('圧縮中に元フォルダが変更されました。元フォルダを保持します。')
            for rel, meta in items['files'].items():
                with (source / rel).open('rb') as stream:
                    if digest(stream, check) != meta['sha256']:
                        raise ValueError('元ファイルが圧縮後に変わりました。削除しません。')
                for extra in meta['streams']:
                    with open(str(source / rel) + extra['name'], 'rb') as stream:
                        if digest(stream, check) != extra['sha256']:
                            raise ValueError('追加ストリームが圧縮後に変わりました。削除しません。')
            manifest = archive.parent / 'manifest.json'
            manifest.write_bytes(manifest_bytes)
            with manifest.open('r+b') as stream:
                os.fsync(stream.fileno())
            with partial.open('rb') as stream:
                zip_sha = digest(stream, check)
            with manifest.open('rb') as stream:
                manifest_sha = digest(stream, check)
            partial.rename(archive)
            self.save(record, state='verified', zip_sha256=zip_sha, manifest=str(manifest), manifest_sha256=manifest_sha, manifest_entry=manifest_entry,
                      message='ZIPの内容照合済み。元フォルダを削除する準備をしています。')
            check()
            archive_lock, _, _ = exclusive_read(archive)
            if digest(archive_lock, check) != zip_sha:
                raise ValueError('検証したZIPが変更されました。元フォルダは削除しません。')
            # Checked exact absolute paths, same-volume rename; never overwrite a destination.
            validate_path(source)
            inside(quarantine)
            if quarantine.exists() or quarantine.is_symlink():
                raise ValueError('退避先が既に存在します。元フォルダを保持します。')
            source.rename(quarantine)
            moved = True
            current = inventory(quarantine, check)
            if signature(current) != record['fingerprint']:
                raise ValueError('削除直前の内容が変わりました。削除を中止します。')
            self.save(record, state='deleting', message='照合済みのファイルだけを削除しています。')
            deleting = True
            # Remove only the exact manifest entries. Newly created files are never deleted.
            for rel, meta in items['files'].items():
                check()
                p = quarantine / rel
                validate_path(p)
                if not p.resolve().is_relative_to(quarantine.resolve()) or p.lstat().st_file_attributes & 0x400:
                    raise ValueError('削除対象のパスが変わりました。処理を止めました。')
                remove_verified(p, meta, check)
            for rel in sorted(items['directories'], key=lambda s: -1 if s == '.' else s.count('/') + 1, reverse=True):
                check()
                p = quarantine if rel == '.' else quarantine / rel
                validate_path(p)
                if not p.resolve().is_relative_to(quarantine.resolve()) or p.lstat().st_file_attributes & 0x400:
                    raise ValueError('削除対象のフォルダが変わりました。処理を止めました。')
                p.rmdir()  # Fails rather than deleting unexpected new files.
            with self.ledger.lock, self.ledger.db:
                project = self.project(record['project_id'])
                self.ledger.db.execute('INSERT INTO revisions(project_id,at,body) VALUES (?,?,?)',
                    (project['id'], stamp(), json.dumps(project, ensure_ascii=False)))
                project.update(status='削除済み' if record.get('operation') == 'delete' else 'アーカイブ済み', manual_updated_at=str(time.time_ns()))
                project['observation']['exists'] = False
                self.ledger.db.execute('UPDATE projects SET body=? WHERE id=?', (json.dumps(project, ensure_ascii=False), project['id']))
            self.save(record, state='completed', message='GitHub非公開化・接続解除・圧縮・全ファイル照合・元フォルダ削除が完了しました。')
        except Exception as exc:
            if moved and not deleting and quarantine.exists() and not source.exists():
                try:
                    quarantine.rename(source)
                    moved = False
                except OSError:
                    pass
            safe = str(exc) if isinstance(exc, ValueError) else 'ファイル操作に失敗しました。使用中・権限・空き容量を確認してください。'
            self.save(record, state='needs_recovery' if moved else 'failed', message=safe,
                      remaining_folder=str(quarantine) if moved else str(source))
        finally:
            if archive_lock:
                archive_lock.close()
