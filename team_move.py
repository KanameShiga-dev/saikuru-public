"""Confirmed same-volume folder relocation with durable ledger history."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import threading
import time

from team_archive import LIVE as ARCHIVE_LIVE, overlaps, validate_path
from team_ledger import ROOT, inspect, stamp

LIVE = {'preparing', 'moving'}


def signature(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def git(path, *args):
    operation = {'rev-parse': 'Gitの所属情報取得', 'status': 'Git差分取得', 'ls-files': 'Git追跡ファイル取得', 'worktree': 'Git作業ツリー処理'}.get(args[0] if args else '', 'Git処理')
    try:
        result = subprocess.run(['git', '--no-optional-locks', '-C', str(path), *args],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=45,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        raise ValueError(f'{operation}が45秒以内に完了しませんでした。対象：{path}。使用中のアプリ・Gitロックを確認してください。') from None
    if result.returncode:
        if 'not a git repository' in result.stderr.lower():
            reason = '.git は存在しますがGitリポジトリとして認識できません。中のGit情報を自動削除・修復せず停止しました。'
        elif 'dubious ownership' in result.stderr.lower():
            reason = 'Gitがフォルダ所有者の不一致を検出しました。安全設定を自動変更せず停止しました。'
        else:
            reason = '権限、Gitロック、サブモジュール、使用中のアプリを確認してください。'
        raise ValueError(f'{operation}に失敗しました（終了コード {result.returncode}）。対象：{path}。{reason}')
    return result.stdout.strip()


def empty_git_directory(path):
    marker = Path(path) / '.git'
    return marker.is_dir() and not any(marker.iterdir())


def repository(path):
    for candidate in [path, *path.parents]:
        if not candidate.is_relative_to(ROOT):
            break
        if (candidate / '.git').exists():
            if empty_git_directory(candidate):
                continue
            return Path(git(candidate, 'rev-parse', '--show-toplevel')).resolve()
    return None


def mapping(path, source, destination):
    p = Path(path).resolve()
    return str(destination / p.relative_to(source)) if p.is_relative_to(source) else str(p)


def file_attributes(path):
    # Unity caches can exceed the traditional Windows path limit. Preserve the
    # link check instead of skipping those files during a whole-folder move.
    value = str(path)
    if os.name == 'nt' and not value.startswith('\\\\?\\'):
        value = '\\\\?\\' + value
    return os.lstat(value).st_file_attributes


class Moves:
    def __init__(self, ledger, engine, archives):
        self.ledger, self.engine, self.archives = ledger, engine, archives
        self.requests, self.previews, self.thread = {}, {}, None
        with ledger.lock, ledger.db:
            for object_id, raw in ledger.db.execute('SELECT id,body FROM moves').fetchall():
                record = json.loads(raw)
                if record['state'] in LIVE:
                    record.update(state='needs_recovery', message='移動中にサービスが停止しました。移動元・移動先と台帳を確認してください。自動で再移動しません。', updated_at=stamp())
                    ledger.db.execute('UPDATE moves SET body=? WHERE id=?', (json.dumps(record, ensure_ascii=False), object_id))

    def records(self):
        with self.ledger.lock:
            return [json.loads(row[0]) for row in self.ledger.db.execute('SELECT body FROM moves ORDER BY rowid DESC')]

    def blocked(self, path):
        return any((overlaps(path, r['source']) or overlaps(path, r['destination'])) and r['state'] in LIVE | {'needs_recovery'} for r in self.records())

    def save(self, record, **fields):
        record.update(fields, updated_at=stamp())
        with self.ledger.lock, self.ledger.db:
            self.ledger.db.execute('INSERT INTO moves VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',
                (record['id'], record['project_id'], json.dumps(record, ensure_ascii=False)))

    def eligible(self, source, destination, own_id=None):
        source, destination = validate_path(source), validate_path(destination)
        if not source.is_dir():
            raise ValueError(f'移動元フォルダが見つかりません：{source}。台帳を再調査して現在の場所を確認してください。')
        if destination.exists():
            candidate = destination.with_name(destination.name + '-moved')
            for index in range(2, 102):
                if not candidate.exists():
                    break
                candidate = destination.with_name(destination.name + '-moved-' + str(index))
            suggestion = f'未使用の例：{candidate}。' if not candidate.exists() else '別の未使用の名前を指定してください。'
            raise ValueError(f'移動先が既に存在します：{destination}。空フォルダでも上書きしません。移動先フォルダ自体は作成せず、その親フォルダだけを用意してください。{suggestion}')
        if not destination.parent.is_dir():
            raise ValueError(f'移動先の親フォルダがありません：{destination.parent}。この親フォルダを作成してから確認してください。移動先の {destination.name} はまだ作成しないでください。')
        if overlaps(source, destination) or source.stat().st_dev != destination.parent.stat().st_dev:
            raise ValueError('移動元の内側・外側への重ね移動や、別ドライブへの移動は対象外です。')
        app = Path(__file__).parent
        if any(overlaps(p, app) or overlaps(p, self.ledger.directory) for p in (source, destination)):
            raise ValueError('稼働中の采来 — サイクル —・台帳DBを含む場所は移動できません。')
        if any(r['state'] in ARCHIVE_LIVE for r in self.archives.records()):
            raise ValueError('アーカイブ・削除が処理中です。完了後に移動してください。')
        if any(r['id'] != own_id and r['state'] in LIVE | {'needs_recovery'} for r in self.records()):
            raise ValueError('別の移動が処理中、または復旧確認待ちです。')
        scope = [source, destination]
        for job in self.engine.store.all('job'):
            if job['status'] in ('planning', 'running', 'awaiting_approval') and any(overlaps(p, job['project']) for p in scope):
                raise ValueError('この範囲に未完了の依頼があります。依頼を完了または中止してから移動してください。')
        for task in self.engine.store.all('task'):
            if task['id'] in self.engine.active or task['status'] in ('running', 'awaiting_approval'):
                job = self.engine.store.get(task['job_id'])
                if any(overlaps(p, job['project']) for p in scope):
                    raise ValueError('この範囲でAIが作業中です。停止してから移動してください。')
        return source, destination

    def plan(self, body):
        project = self.archives.project(body.get('id'))
        source, destination = self.eligible(project['path'], body.get('destination', ''), body.get('_own_id'))
        root, target_root = repository(source), repository(destination.parent)
        kind = 'folder'
        git_roots = []
        empty_markers = set()
        for base in (source, destination.parent):
            for ancestor in [base, *base.parents]:
                if not ancestor.is_relative_to(ROOT):
                    break
                if empty_git_directory(ancestor):
                    empty_markers.add(str(ancestor / '.git'))
        if root == source and (source / '.git').is_file():
            if git(source, 'rev-parse', '--show-superproject-working-tree'):
                raise ValueError('Gitサブモジュールは親の設定変更が必要なため、この移動機能では対象外です。')
            common = Path(git(source, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
            validate_path(common)
            kind = 'worktree'
            git_roots.append(str(source))
        # A rename preserves bytes, NTFS streams and attributes; reject linked paths and
        # embedded external Git metadata that would become invalid after relocation.
        for current, directories, files in os.walk(source, followlinks=False, onerror=lambda e: (_ for _ in ()).throw(e)):
            here = Path(current)
            for name in directories + files:
                p = here / name
                if file_attributes(p) & 0x400:
                    raise ValueError('内部にリンク・ジャンクションがあります。リンク先への影響を確認してから別途移動してください。')
            if '.git' in files and not (here == source and kind == 'worktree'):
                raise ValueError('内部にサブモジュール・外部Git作業ツリーがあります。このフォルダ全体の移動は対象外です。')
            if '.git' in directories:
                if empty_git_directory(here):
                    empty_markers.add(str(here / '.git'))
                    directories.remove('.git')
                    continue
                if Path(git(here, 'rev-parse', '--show-toplevel')).resolve() != here.resolve():
                    raise ValueError('Gitの作業場所が絶対パス等で別の場所に設定されています。Git設定を確認してから移動してください。')
                git_roots.append(str(here))
                if sum(line.startswith('worktree ') for line in git(here, 'worktree', 'list', '--porcelain').splitlines()) > 1:
                    raise ValueError('他のGit作業ツリーを管理するリポジトリがあります。関連作業ツリーを整理してから移動してください。')
                directories.remove('.git')
        projects = self.ledger.snapshot()['projects']
        affected = [p for p in projects if Path(p['path']).resolve().is_relative_to(source)]
        references = [p for p in projects if p.get('source_path') and Path(p['source_path']).resolve().is_relative_to(source)]
        for p in affected:
            target = mapping(p['path'], source, destination)
            if any(os.path.normcase(o['path']) == os.path.normcase(target) and o['id'] not in {a['id'] for a in affected} for o in projects):
                raise ValueError('移動先のパスは台帳に既に登録されています。別の未使用パスを指定してください。')
        warnings = ['GitHubの公開設定・Git remoteは変更しません。コミット・pushは行いません。',
            'ソース内の絶対パス・IDE設定・外部スクリプト・既存依頼と引き継ぎ履歴のパスは自動変更しません。',
            '移動先へのAI作業許可は自動で付与しません。移動後に起動方法と作業対象設定を確認してください。']
        for marker in sorted(empty_markers):
            warnings.append(f'空のGitフォルダを検出：{marker}。Git接続・履歴ありとは判定せず、そのまま保持します。')
        if root and root != source:
            warnings.insert(0, '親Gitでは、このフォルダの追跡対象が削除された変更として表示されます。親のGit履歴は移動先に引き継がれません。')
        if target_root:
            warnings.insert(0, '移動先は別のGit管理範囲です。追加ファイルまたは入れ子のGitリポジトリとして扱われます。')
        if root == source:
            warnings.insert(0, 'プロジェクト自身のGit履歴と接続設定を保ったまま移動します。')
        status = git(root, 'status', '--porcelain=v1', '--untracked-files=normal', '--', str(source)) if root else ''
        tracked = git(root, 'ls-files', '--', str(source)).splitlines() if root else []
        return {'id': secrets.token_hex(12), 'token': secrets.token_urlsafe(32), 'expires': time.time()+900,
            'project_id': project['id'], 'source': str(source), 'destination': str(destination), 'kind': kind,
            'source_identity': [source.stat().st_dev, source.stat().st_ino], 'git_root': str(root or ''),
            'destination_git_root': str(target_root or ''), 'git_roots': git_roots, 'tracked_files': len(tracked), 'git_changes': len(status.splitlines()),
            'git_signature': signature(status), 'ledger_signature': signature(affected + references),
            'affected': [{'id': p['id'], 'name': p['name'], 'source': p['path'], 'destination': mapping(p['path'], source, destination)} for p in affected],
            'references': [{'id': p['id'], 'name': p['name']} for p in references], 'warnings': warnings}

    def request_preview(self, body):
        with self.engine.store.lock:
            self.requests = {k:v for k,v in self.requests.items() if v['created'] > time.time()-3600}
            if sum(r['status'] == 'working' for r in self.requests.values()) >= 2:
                raise ValueError('対象確認が処理中です。完了を待ってください。')
            request_id = secrets.token_urlsafe(24)
            request = {'status': 'working', 'created': time.time()}
            self.requests[request_id] = request
        def run():
            try:
                preview = self.plan(body)
                with self.engine.store.lock:
                    self.previews = {k:v for k,v in self.previews.items() if v['expires'] > time.time()}
                    self.previews[preview['token']] = preview
                    request.update(status='ready', preview=preview)
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                message = str(exc) if isinstance(exc, ValueError) else '対象確認に失敗しました。アクセス権・Git・使用中のアプリを確認してください。'
                with self.engine.store.lock:
                    request.update(status='failed', error=message)
        threading.Thread(target=run, daemon=True).start()
        return {'request_id': request_id}

    def preview_status(self, request_id):
        with self.engine.store.lock:
            if request_id not in self.requests:
                raise ValueError('確認情報がありません。もう一度移動対象を確認してください。')
            return dict(self.requests[request_id])

    def start(self, body):
        with self.engine.store.lock:
            preview = self.previews.get(body.get('token', ''))
            if not preview or preview['expires'] < time.time():
                raise ValueError('確認期限が切れました。移動対象を確認し直してください。')
            if body.get('confirmed_source') != preview['source'] or body.get('confirmed_destination') != preview['destination'] or any(body.get(k) is not True for k in ('closed_apps', 'confirmed_move', 'git_reviewed')):
                raise ValueError('移動元・移動先・Gitへの影響・アプリ停止の確認が必要です。')
            self.eligible(preview['source'], preview['destination'])
            backup = self.ledger.backup()
            del self.previews[body['token']]
            record = {k:v for k,v in preview.items() if k not in ('token', 'expires')}
            record.update(created_at=stamp(), backup=backup)
            self.save(record, state='preparing', message='移動直前の状態を再確認しています。')
            self.thread = threading.Thread(target=self.run, args=(record,), daemon=True)
            self.thread.start()
            return {'ok': True, 'move_id': record['id']}

    def relocate(self, source, destination, kind):
        validate_path(source); validate_path(destination)
        if destination.exists():
            raise ValueError('移動先が存在します。上書きしません。')
        if kind == 'worktree':
            git(source, 'worktree', 'move', str(source), str(destination))
        else:
            source.rename(destination)

    def run(self, record):
        source, destination, moved = Path(record['source']), Path(record['destination']), False
        try:
            # The potentially large folder traversal runs without dashboard locks.
            # The durable preparing record already blocks workers in both scopes.
            current = self.plan_for_record(record)
            # Shares the worker/archiver start lock. No new task can start in this scope
            # between revalidation, rename and the committed ledger update.
            with self.engine.store.lock, self.ledger.lock:
                self.eligible(source, destination, record['id'])
                projects = self.ledger.snapshot()['projects']
                affected = [p for p in projects if Path(p['path']).resolve().is_relative_to(source)]
                references = [p for p in projects if p.get('source_path') and Path(p['source_path']).resolve().is_relative_to(source)]
                current['ledger_signature'] = signature(affected + references)
                current['source_identity'] = [source.stat().st_dev, source.stat().st_ino]
                root = repository(source)
                current['git_root'] = str(root or '')
                current['destination_git_root'] = str(repository(destination.parent) or '')
                current['git_signature'] = signature(git(root, 'status', '--porcelain=v1', '--untracked-files=normal', '--', str(source)) if root else '')
                for key in ('source_identity', 'git_root', 'destination_git_root', 'git_roots', 'kind', 'git_signature', 'ledger_signature'):
                    if current[key] != record[key]:
                        raise ValueError('確認後にフォルダ・Git・台帳が変わりました。移動を止めました。対象を確認し直してください。')
                if self.engine.shutdown.is_set():
                    raise ValueError('サービス停止のため移動しませんでした。')
                self.save(record, state='moving', message='フォルダと台帳の参照先を移動しています。')
                with self.ledger.lock:
                    projects = self.ledger.snapshot()['projects']
                    # Lock out scans and edits while the directory and ledger change.
                    self.relocate(source, destination, record['kind']); moved = True
                    if [destination.stat().st_dev, destination.stat().st_ino] != record['source_identity']:
                        raise ValueError('移動先のフォルダ識別情報が一致しません。')
                    for old_root in record['git_roots']:
                        new_root = Path(mapping(old_root, source, destination))
                        if Path(git(new_root, 'rev-parse', '--show-toplevel')).resolve() != new_root:
                            raise ValueError('移動後のGit作業場所が一致しません。移動元へ戻します。')
                    updates = []
                    for p in projects:
                        before = json.dumps(p, ensure_ascii=False)
                        changed = False
                        if Path(p['path']).resolve().is_relative_to(source):
                            p['path'] = mapping(p['path'], source, destination)
                            p['observation'] = inspect(Path(p['path']))
                            changed = True
                        elif overlaps(p['path'], source) or overlaps(p['path'], destination):
                            if Path(p['path']).is_dir():
                                p['observation'] = inspect(Path(p['path'])); changed = True
                        if p.get('source_path') and Path(p['source_path']).resolve().is_relative_to(source):
                            p['source_path'] = mapping(p['source_path'], source, destination); changed = True
                        if changed:
                            p.pop('worker_allowed', None)
                            p['manual_updated_at'] = str(time.time_ns())
                            updates.append((p, before))
                    with self.ledger.db:
                        for p, before in updates:
                            self.ledger.db.execute('INSERT INTO revisions(project_id,at,body) VALUES (?,?,?)', (p['id'], stamp(), before))
                            self.ledger.db.execute('UPDATE projects SET path=?,body=? WHERE id=?', (p['path'], json.dumps(p, ensure_ascii=False), p['id']))
                        record.update(state='completed', message='フォルダ移動と台帳更新が完了しました。起動方法・絶対パス参照・AI作業対象を確認してください。', updated_at=stamp())
                        self.ledger.db.execute('UPDATE moves SET body=? WHERE id=?', (json.dumps(record, ensure_ascii=False), record['id']))
        except Exception as exc:
            state, message = 'failed', '移動を中止しました。元フォルダは保持しています。'
            if moved:
                try:
                    self.relocate(destination, source, record['kind'])
                    message = '台帳更新・確認に失敗したため、フォルダを移動元へ戻しました。'
                except Exception:
                    state, message = 'needs_recovery', '移動後の復旧確認が必要です。履歴の移動元・移動先・DBバックアップを確認してください。自動で再試行しません。'
            elif destination.exists():
                state, message = 'needs_recovery', '移動先の状態確認が必要です。上書き・再移動は行っていません。移動元と移動先を確認してください。'
            if isinstance(exc, (ValueError, OSError)):
                message += ' ' + str(exc)
            self.save(record, state=state, message=message)

    def plan_for_record(self, record):
        # Our own durable intent is ignored; all other move/archive guards still apply.
        return self.plan({'id': record['project_id'], 'destination': record['destination'], '_own_id': record['id']})
