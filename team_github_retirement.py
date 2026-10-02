"""Only metadata is read remotely. Privacy and disconnection require UI confirmation."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlsplit


def command(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=45, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError('GitHub/Gitへの接続に失敗しました。ログイン・ネットワークを確認してください。')
    if result.returncode:
        # Never persist CLI stderr or credential-bearing URLs.
        error = result.stderr
        if 'HTTP 404' in error:
            raise ValueError('GitHub公開先が見つからない、またはアクセス権がありません。対象URLとログイン先を確認してください。')
        if 'HTTP 422' in error:
            raise ValueError('GitHubの非公開化が拒否されました。公開フォーク・組織の設定などをGitHubで確認してください。ローカル削除は止めました。')
        if 'HTTP 429' in error or 'rate limit' in error.lower():
            raise ValueError('GitHubのAPI利用制限に達しました。時間を置いてやり直してください。ローカル削除は止めました。')
        raise ValueError('GitHub/Gitの操作が拒否されました。GitHub CLIのログイン・リポジトリの管理権限を確認してください。')
    return result.stdout.strip()


def repository(value, allow_short=True):
    value = value.strip()
    ssh = re.fullmatch(r'(?:git@)?github\.com:([^/]+/[^/]+)', value)
    if ssh:
        value = ssh.group(1)
    elif '://' in value:
        parsed = urlsplit(value)
        if parsed.hostname not in ('github.com', 'ssh.github.com') or parsed.query or parsed.fragment:
            return None
        value = parsed.path.strip('/')
    elif not allow_short or ':' in value or '/' not in value:
        return None
    value = value.removesuffix('.git')
    return value if re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value) and '..' not in value else None


def remotes(path):
    if not (Path(path) / '.git').is_dir():
        return []
    names = command(['git', '-C', str(path), 'remote']).splitlines()
    refs = []
    for name in names:
        # Fetch and push URLs may differ. Consider both, including multiple URLs.
        urls = command(['git', '-C', str(path), 'remote', 'get-url', '--all', name]).splitlines()
        urls += command(['git', '-C', str(path), 'remote', 'get-url', '--push', '--all', name]).splitlines()
        for url in urls:
            repo = repository(url, allow_short=False)
            if repo:
                refs.append({'remote': name, 'repository': repo})
    return sorted([dict(pair) for pair in {tuple(sorted(r.items())) for r in refs}], key=lambda r:(r['remote'], r['repository']))


def api(repo, method='GET'):
    gh = shutil.which('gh')
    if not gh:
        raise ValueError('GitHub CLIが見つかりません。非公開化を確認できないため削除しません。')
    args = [gh, 'api', '--hostname', 'github.com', 'repos/' + repo, '--method', method]
    if method == 'PATCH':
        args += ['-F', 'private=true']
    result = json.loads(command(args))
    return {'id': result['id'], 'repository': result['full_name'], 'private': result['private'],
            'admin': result.get('permissions', {}).get('admin', False), 'fork': result.get('fork', False)}


def plan(path, manual):
    if not isinstance(manual, list) or len(manual) > 10 or any(not isinstance(v, str) or len(v)>300 for v in manual):
        raise ValueError('追加のGitHub公開先は10件以内で入力してください。')
    refs = remotes(path)
    repositories = {r['repository'] for r in refs}
    normalized_manual = []
    for value in manual:
        repo = repository(value)
        if not repo:
            raise ValueError('GitHub公開先は owner/repository または github.com のリポジトリURLで入力してください。')
        repositories.add(repo)
        normalized_manual.append(repo)
    targets = [api(repo) for repo in sorted(repositories)]
    if any(not r['private'] and not r['admin'] for r in targets):
        raise ValueError('非公開化に必要なGitHub管理権限がありません。削除を開始できません。')
    if any(not r['private'] and r['fork'] for r in targets):
        raise ValueError('公開フォークをそのまま非公開化することはできません。GitHubで保管方法を確認してから実施してください。')
    return {'refs': refs, 'repositories': targets, 'manual': normalized_manual}


def retire(path, expected, record_progress, check):
    if remotes(path) != expected['refs']:
        raise ValueError('確認画面の後にGitHub接続先が変わりました。処理を止めました。')
    changes = []
    for target in expected['repositories']:
        check()
        current = api(target['repository'])
        if current['id'] != target['id']:
            raise ValueError('GitHubリポジトリのIDが変わりました。削除しません。')
        changes.append({'repository': current['repository'], 'previous_private': current['private'],
                        'private_confirmed': False})
        record_progress(changes, [])  # Durable intent before a remote mutation.
        if not current['private']:
            if not current['admin']:
                raise ValueError('GitHubの管理権限がなく、非公開化できません。削除しません。')
            changed = api(current['repository'], 'PATCH')
            if changed['id'] != target['id']:
                raise ValueError('変更対象が一致しません。削除しません。')
        check()
        verified = api(current['repository'])
        if verified['id'] != target['id'] or not verified['private']:
            raise ValueError('GitHubの非公開化を確認できません。削除しません。')
        changes[-1]['private_confirmed'] = True
        record_progress(changes, [])
    # Remove only the GitHub remotes listed on the confirmation screen.
    if remotes(path) != expected['refs']:
        raise ValueError('処理中にGitHub接続先が変わりました。接続解除・削除を止めました。')
    removed = []
    for name in sorted({r['remote'] for r in expected['refs']}):
        check()
        command(['git', '-C', str(path), 'remote', 'remove', name])
        removed.append(name)
        record_progress(changes, removed)
    if remotes(path):
        raise ValueError('GitHub接続が残っています。削除しません。')
    return changes, removed
