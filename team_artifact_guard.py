"""Claude Artifact (Design / Design System / Slides) gate for document jobs.

Current operation (user decision 2026-10-07): the CLI started by saikuru does not offer the Artifact tool
(only the desktop app's entrypoint does), so the builder only writes data files into the staging folder
(prepare_staging / staging_edit / check_staging). Sending to claude.ai is done afterwards from the desktop
session with the user's approval, and the URL is recorded via /api/jobs/artifact-url.
review() is kept for a future officially supported Artifact tool in saikuru; it is not reachable now.

review() runs locally before every Artifact tool call (via approval_hook -> Engine.tool_request).
- Only the builder of a job whose deliverable is a Claude Artifact format may publish.
- Creating the artifact (the moment content leaves this PC) always waits for the user's approval,
  even when automatic_operations is on. Later publishes may only target the artifact created in this job.
- Every local file sent must sit in the job's staging folder and pass the content check.
- delete / pin / force / capabilities and the comment/data tools are never allowed.
Fail-closed: any error blocks the call.
"""
import os
import re
from pathlib import Path

from team_web_guard import (SECRET, EMAIL, PHONE, PRIVATE_IP, INTERNAL_HOST, LOCAL_PATH, MY_NUMBER,
                            load_policy, _environment_terms)

# format -> (type name, type_url). Type URLs from `Artifact list scope=types` (core types).
FORMATS = {
    'claude_design': ('Design', 'https://claude.ai/artifact/QKN21svewxgyPb6SYRqWnd'),
    'claude_design_system': ('Design System', 'https://claude.ai/artifact/5M7UeXXcx16TP3vzVFNDzd'),
    'claude_slides': ('Slides', 'https://claude.ai/artifact/8jTsAFQMFDb2oA8MsPJ2eL'),
}
STAGING = 'claude-artifacts'
URL = re.compile(r'https://claude\.ai/(?:code/)?artifact/[A-Za-z0-9_-]+')
ASSET_EXT = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.woff2', '.woff', '.ttf', '.otf'}
TEXT_EXT = {'.json', '.html', '.css', '.js', '.md', '.txt', '.svg'}
READ_ACTIONS = {'read', 'list', 'quickstart'}
FORBIDDEN_KEYS = ('force', 'capabilities', 'contract', 'pin', 'overwrite_unread', 'favicon')
MAX_BYTES = 16_000_000


def is_artifact_format(name):
    return name in FORMATS


def staging_root(project, job_id):
    return Path(project).resolve() / STAGING / str(job_id)


def normalize(url):
    match = URL.search(str(url or ''))
    return match.group(0) if match else ''


def type_urls():
    return {normalize(v[1]) for v in FORMATS.values()}


def check_text(text):
    """Return (allowed, reason). Reason is a category label safe to show/log."""
    try:
        policy = load_policy()
        checks = [(SECRET, '認証情報・秘密情報らしき文字列'), (MY_NUMBER, '12桁の番号（個人番号等の可能性）'),
                  (EMAIL, 'メールアドレス'), (PHONE, '電話番号'), (PRIVATE_IP, '社内IPアドレス'),
                  (INTERNAL_HOST, '社内ホスト名・ドメイン'), (LOCAL_PATH, 'PC内・社内共有のパス')]
        for pattern, label in checks:
            if pattern.search(text):
                return False, label
        lowered = text.casefold()
        for domain in policy['blocked_domains']:
            d = str(domain).strip().casefold().lstrip('.')
            if d and re.search(r'(?:^|[^a-z0-9-])' + re.escape(d) + r'(?:$|[^a-z0-9-])', lowered):
                return False, '社内ドメイン（ポリシー指定）'
        for term in policy['blocked_terms']:
            t = str(term).strip().casefold()
            if t and t in lowered:
                return False, '社内の固有名詞（ポリシー指定）'
        for term in _environment_terms(None):
            if term.casefold() in lowered:
                return False, 'このPC・アカウントの固有名'
        return True, ''
    except Exception:
        return False, '公開前チェックを実行できなかったため停止しました。'


def staging_path(staging, project, value, base=None):
    """Resolve a local path the agent named; it must be a file inside the staging folder."""
    candidate = Path(str(value))
    if not candidate.is_absolute():
        candidate = Path(base or project) / candidate
    if candidate.is_symlink():
        raise ValueError('リンクは送れません。')
    resolved = candidate.resolve()
    if not resolved.is_relative_to(staging.resolve()):
        raise ValueError('作業フォルダ（' + STAGING + '）の外のファイルは送れません。')
    return resolved


def _sources(data, staging, project):
    """Local files this publish sends, plus artifact-to-artifact copies."""
    root = data.get('root')
    base = None
    if root:
        base = staging_path(staging, project, root)
        if not base.is_dir():
            raise ValueError('root が作業フォルダ内のフォルダではありません。')
    local, copies = [], []
    if data.get('file_path'):
        local.append(staging_path(staging, project, data['file_path'], base))
    for item in data.get('file_paths') or []:
        local.append(staging_path(staging, project, item, base))
    files = data.get('files') or {}
    entries = files.items() if isinstance(files, dict) else ((f.get('path'), f.get('path')) for f in files)
    for published, source in entries:
        if source is None:
            continue
        if isinstance(source, dict) and source.get('artifact'):
            if not normalize(source['artifact']):
                raise ValueError('コピー元の成果物URLが不正です。')
            copies.append(normalize(source['artifact']) + ' ' + str(source.get('path', ''))[:120])
            continue
        if isinstance(source, dict):
            source = source.get('from')
        local.append(staging_path(staging, project, source, base))
    return local, copies


def review(data, role, fmt, project, job_id, owned, create_urls):
    """Decide one Artifact call.

    Returns dict: allow (bool), note (str), kind ('read'|'create'|'update'), manual (bool),
    url (owned target), files (relative names), copies (list).
    """
    try:
        if fmt not in FORMATS:
            return {'allow': False, 'note': 'この依頼の成果物はClaudeのArtifact形式ではありません。'}
        if not isinstance(data, dict):
            return {'allow': False, 'note': '入力が不正です。'}
        action = data.get('action') or 'publish'
        staging = staging_root(project, job_id)
        if action in READ_ACTIONS:
            if data.get('out_dir'):
                staging_path(staging, project, data['out_dir'])
            return {'allow': True, 'kind': 'read', 'manual': False, 'note': 'Artifactの読み取り'}
        if action != 'publish':
            return {'allow': False, 'note': '削除・ピン留め・表示操作は許可していません（' + str(action)[:20] + '）。'}
        if role != 'builder':
            return {'allow': False, 'note': '公開は制作担当（builder）だけが行えます。'}
        if any(data.get(k) for k in FORBIDDEN_KEYS):
            return {'allow': False, 'note': 'force・capabilities・contract・pin・overwrite_unread は使えません。'}
        name, type_url = FORMATS[fmt]
        if data.get('type_url'):
            if normalize(data['type_url']) != normalize(type_url):
                return {'allow': False, 'note': 'この依頼で作れるのは ' + name + ' だけです（type_url: ' + type_url + '）。'}
            if data.get('url') or data.get('file_path') or data.get('files') or data.get('file_paths'):
                return {'allow': False, 'note': '作成はtype_urlとtitleだけで行い、中身はその後に作成されたURLへ送ってください。'}
            if owned:
                return {'allow': False, 'note': 'この依頼の成果物は作成済みです（' + owned[0] + '）。新しく作らず、そのURLを更新してください。'}
            title = str(data.get('title') or '').strip()
            if not title:
                return {'allow': False, 'note': 'title（成果物の名前）を指定してください。'}
            ok, reason = check_text(title + '\n' + str(data.get('description') or ''))
            if not ok:
                return {'allow': False, 'note': '公開前チェックで停止しました（' + reason + '）。名前・説明を見直してください。'}
            return {'allow': True, 'kind': 'create', 'manual': True, 'note': name + ' の作成',
                    'title': title[:200], 'type': name}
        target = normalize(data.get('url'))
        if not target:
            return {'allow': False, 'note': '更新するArtifactのurlを指定してください。'}
        if target not in owned and target not in create_urls:
            return {'allow': False, 'note': 'この依頼で作成したArtifact以外へは送れません。'}
        if target in type_urls():
            return {'allow': False, 'note': '種類（type）そのものへは送れません。'}
        if data.get('from_url') or data.get('asset_ids'):
            if not normalize(data.get('from_url')):
                return {'allow': False, 'note': 'コピー元の成果物URLが不正です。'}
        local, copies = _sources(data, staging, project)
        asset = bool(data.get('asset'))
        names = []
        for path in local:
            if not path.is_file():
                return {'allow': False, 'note': 'ファイルがありません: ' + path.name}
            suffix = path.suffix.lower()
            allowed_ext = ASSET_EXT if asset else (TEXT_EXT | ASSET_EXT)
            if suffix not in allowed_ext:
                return {'allow': False, 'note': '送れない種類のファイルです: ' + path.name}
            if path.stat().st_size > MAX_BYTES:
                return {'allow': False, 'note': '16MBを超えるファイルは送れません: ' + path.name}
            if suffix in TEXT_EXT:
                ok, reason = check_text(path.read_text(encoding='utf-8', errors='replace'))
                if not ok:
                    return {'allow': False, 'note': '公開前チェックで停止しました（' + path.name + '：' + reason + '）。'
                            '該当箇所を取り除くか、プレースホルダーにしてください。'}
            names.append(str(path.relative_to(staging.resolve())))
        if not local and not copies and not data.get('from_url'):
            return {'allow': False, 'note': '送るファイルがありません。'}
        return {'allow': True, 'kind': 'update', 'manual': False, 'url': target, 'files': names,
                'copies': copies, 'note': 'この依頼のArtifactへの送信（公開前チェック済み）'}
    except Exception as exc:
        return {'allow': False, 'note': '公開前チェックで停止しました: ' + str(exc)[:200]}


REFERENCE_DIR = Path(__file__).resolve().parent / 'artifact_types'
# Index file and at least one content file pattern each format needs under staging/project.
REQUIRED = {
    'claude_design': ('canvas.json', '*.dc.html'),
    'claude_slides': ('deck.json', 'slides/*.html'),
    'claude_design_system': ('design-system.json', 'README.md'),
}


def prepare_staging(project, job_id, fmt):
    """Create the staging folder and copy the local format reference into it (for the builder to read)."""
    staging = staging_root(project, job_id)
    (staging / 'project').mkdir(parents=True, exist_ok=True)
    source = REFERENCE_DIR / (fmt + '.md')
    target = staging / '_reference' / (fmt + '.md')
    target.parent.mkdir(exist_ok=True)
    if source.is_file():
        target.write_bytes(source.read_bytes())
    return staging


def check_staging(project, job_id, fmt):
    """Deliverable gate: required files exist under staging/project and every text file passes the content check."""
    if fmt not in REQUIRED:
        raise ValueError('ClaudeのArtifact形式ではありません。')
    root = staging_root(project, job_id) / 'project'
    index, content = REQUIRED[fmt]
    if not (root / index).is_file() or not any(p.is_file() for p in root.glob(content)):
        raise ValueError('Artifactのデータファイルがそろっていません（' + STAGING + '\\' + str(job_id)
                         + '\\project に ' + index + ' と ' + content + ' が必要）。完了扱いにできません。')
    problems = []
    for path in root.rglob('*'):
        if path.is_symlink():
            problems.append(path.name + '：リンク')
            continue
        if not path.is_file():
            continue
        if path.suffix.lower() not in TEXT_EXT:
            problems.append(path.name + '：送れない種類のファイル')
            continue
        ok, reason = check_text(path.read_text(encoding='utf-8', errors='replace'))
        if not ok:
            problems.append(str(path.relative_to(root)) + '：' + reason)
    if problems:
        raise ValueError('Artifactのデータファイルが公開前チェックに該当しました（' + '、'.join(problems[:5])
                         + '）。該当箇所をプレースホルダーに置き換えてください。')


def staging_edit(tool, data, role, project, job_id):
    """Write/Edit/Read/Glob/Grep for artifact jobs: only inside the staging folder."""
    try:
        staging = staging_root(project, job_id)
        if tool in ('Read', 'Glob', 'Grep'):
            target = data.get('file_path') or data.get('path') or str(staging)
            resolved = (Path(str(target)) if Path(str(target)).is_absolute() else Path(project) / str(target)).resolve()
            if not resolved.is_relative_to(Path(project).resolve()):
                return False, '対象プロジェクト外の読み取りです。'
            if any(p.lower() in ('.env', '.ssh', '.credentials.json', 'auth.json', '.git') for p in resolved.parts):
                return False, '機密情報・Git内部へのアクセスは許可しません。'
            return True, 'プロジェクト内の読み取り'
        if role != 'builder':
            return False, '作業フォルダへの書き込みは制作担当だけです。'
        path = staging_path(staging, project, data.get('file_path') or '')
        if not path.is_relative_to((staging / 'project').resolve()):
            return False, '書き込めるのは作業フォルダの project フォルダの中だけです（_reference は読み取り専用）。'
        if path.suffix.lower() not in TEXT_EXT:
            return False, '作業フォルダに書けるのは json/html/css/js/md/txt/svg だけです。'
        if tool == 'Write' and len(str(data.get('content') or '').encode('utf-8')) > MAX_BYTES:
            return False, '16MBを超えるファイルは書けません。'
        os.makedirs(path.parent, exist_ok=True)
        return True, '作業フォルダへの書き込み: ' + str(path.relative_to(staging.resolve()))
    except Exception as exc:
        return False, str(exc)[:200]
