"""Import reviewed skills (SKILL.md folders) into saikuru.

Flow (docs/SKILL_IMPORT.md):
  skills/_inbox/<name>/SKILL.md  --(preview: machine checks, file list with SHA256)-->
  user approves on the skills page  -->  skills/<name>/ (+ .saikuru-import.json manifest)
  -> registered in the handoff DB as an imported skill, active for the projects the user picked.
Scripts shipped with a skill may be run by development-job builders only while every referenced skill
file still matches the SHA256 recorded at import (Engine.tool_request -> script_check).
Nothing here executes skill content.
"""
import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path

from team_config import ROOT

SKILLS = ROOT / 'skills'
INBOX = SKILLS / '_inbox'
BACKUPS = ROOT / 'data' / 'backups' / 'skills'
MANIFEST = '.saikuru-import.json'
NAME = re.compile(r'^[a-z0-9][a-z0-9-]{0,63}$')
DOCS = {'.md', '.txt'}
SCRIPTS = {'.py', '.ps1', '.sh', '.js', '.mjs', '.cjs', '.bat', '.cmd'}
DATA = {'.json', '.yaml', '.yml', '.csv', '.html', '.css', '.svg', '.png', '.jpg', '.jpeg', '.gif', '.webp',
        '.xml', '.toml', '.ini', '.cfg', '.tsv', '.pdf', '.docx', '.xlsx', '.pptx', '.ttf', '.otf', '.woff', '.woff2'}
REFUSED = {'.exe', '.dll', '.msi', '.scr', '.com', '.vbs', '.vbe', '.jse', '.wsf', '.hta', '.lnk', '.reg', '.ps1xml', '.jar', '.sys'}
MAX_FILES = 200
MAX_BYTES = 20 * 1024 * 1024
TEXT_LIMIT = 150_000
SECRET = re.compile(r'(?i)(?:sk-[\w-]{20,}|gh[pousr]_[\w]{20,}|xox[abpr]-[\w-]{10,}|AKIA[0-9A-Z]{16}|bearer\s+[\w.-]{20,}'
                    r'|(?:password|passwd|api[_-]?key|access[_-]?token|refresh[_-]?token|secret)\s*[:=]\s*["\']?[\w-]{8,}|-----BEGIN .*PRIVATE KEY)')
USER_PATH = re.compile(r'(?i)[A-Z]:[\\/]Users[\\/][^\\/\s]+')


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


PLAIN_DOCS = {'LICENSE', 'NOTICE', 'COPYING', 'AUTHORS'}


# Unambiguous injection phrases (same list as attachment_guard.check): never downgraded to a person's review.
MARKERS = ('ignoreallpreviousinstructions', 'ignorepreviousinstructions', 'disregardallpreviousinstructions',
           'bypassapproval', 'revealthesystemprompt', 'youarenowunrestricted', 'actasanunrestrictedai',
           '上記の指示を無視', '以前の指示を無視', 'システムの指示を無視', '承認を迂回')


def has_injection_marker(text):
    from attachment_guard import normalized
    compact = re.sub(r'[^\w぀-ヿ㐀-鿿]', '', normalized(text)).casefold()
    return any(marker in compact for marker in MARKERS)


def injection_hits(text, width=50):
    """Passages that made attachment_guard suspect prompt injection, with surrounding text, so a person can
    judge them (e.g. ordinary prose such as '確認項目…省略'). Mirrors attachment_guard.check's matching."""
    from attachment_guard import PATTERNS, normalized
    hits = []
    value = normalized(text)
    for reason, pattern in PATTERNS:
        for match in re.finditer(pattern, value, flags=re.I | re.S):
            hits.append((reason, value[max(0, match.start() - width):match.end() + width].replace('\n', ' ⏎ ')))
    compact = re.sub(r'[^\w぀-ヿ㐀-鿿]', '', value).casefold()
    for reason, pattern in PATTERNS[:-1]:
        for match in re.finditer(pattern, compact, flags=re.I | re.S):
            hits.append((reason + '（記号・改行を除いてつなげた文）', compact[max(0, match.start() - width):match.end() + width]))
    seen, unique = set(), []
    for reason, excerpt in hits:
        if excerpt not in seen:
            seen.add(excerpt)
            unique.append((reason, excerpt))
    return unique[:20]


def frontmatter(text):
    """name / description from the SKILL.md front matter (simple 'key: value' lines only)."""
    match = re.match(r'^\ufeff?---\s*\n(.*?)\n---\s*\n', text, re.S)
    if not match:
        raise ValueError('SKILL.md の先頭に --- で囲んだ name と description が必要です。')
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(':')
        if key.strip() in ('name', 'description') and value.strip():
            fields[key.strip()] = value.strip().strip('"\'')
    if not NAME.match(fields.get('name', '')):
        raise ValueError('name は英小文字・数字・ハイフンの64文字以内にしてください。')
    if not 1 <= len(fields.get('description', '')) <= 1024:
        raise ValueError('description（1〜1024文字）が必要です。')
    return fields, text[match.end():]


def _bounded_paths(folder):
    count = 0
    root = folder.resolve()
    for directory, directories, filenames in os.walk(folder, followlinks=False):
        kept = []
        for name in sorted(directories):
            path = Path(directory) / name
            count += 1
            if count > 1000:
                raise ValueError('フォルダ内の項目が多すぎます。')
            if path.is_symlink() or getattr(path, 'is_junction', lambda: False)() or not path.resolve().is_relative_to(root):
                yield path
            else:
                kept.append(name)
        directories[:] = kept
        for name in sorted(filenames):
            count += 1
            if count > 1000:
                raise ValueError('フォルダ内の項目が多すぎます。')
            yield Path(directory) / name


def _bounded_target(path, root):
    if path.is_symlink() or getattr(path, 'is_junction', lambda: False)() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('リンク先や指定範囲外のフォルダは移動できません。')
    return path


def inspect(folder):
    """Machine checks and file inventory of one skill folder. Returns a preview dict (never raises for findings)."""
    folder = Path(folder)
    if folder.is_symlink() or getattr(folder,'is_junction',lambda:False)():
        raise ValueError('リンクされたスキルフォルダは取り込めません。')
    findings, reviews, files, total = [], [], [], 0
    skill_md = folder / 'SKILL.md'
    fields, body = {}, ''
    try:
        if not skill_md.is_file() or skill_md.is_symlink():
            raise ValueError('SKILL.md がありません。')
        if skill_md.stat().st_size > TEXT_LIMIT * 4:
            raise ValueError('SKILL.md が大きすぎます。')
        fields, body = frontmatter(skill_md.read_text(encoding='utf-8'))
        if fields['name'] != folder.name:
            findings.append('フォルダ名と name が違います（' + folder.name + ' / ' + fields['name'] + '）。フォルダ名を name に合わせてください。')
    except (ValueError, UnicodeError) as exc:
        findings.append(str(exc))
    for path in _bounded_paths(folder):
        relative = path.relative_to(folder).as_posix()
        if path.is_symlink() or getattr(path,'is_junction',lambda:False)() or not path.resolve().is_relative_to(folder.resolve()):
            findings.append('リンクは取り込めません: ' + relative)
            continue
        if path.is_dir():
            continue
        if any(part.startswith('.') for part in path.relative_to(folder).parts):
            findings.append('隠しファイルは取り込めません: ' + relative)
            continue
        suffix = path.suffix.lower()
        size = path.stat().st_size
        if size>MAX_BYTES or total+size>MAX_BYTES or len(files)>=MAX_FILES:
            findings.append('サイズ・ファイル数の上限を超えるため検査を停止しました。')
            break
        total += size
        kind = ('doc' if suffix in DOCS or (not suffix and path.name.upper() in PLAIN_DOCS) else 'script' if suffix in SCRIPTS
                else 'data' if suffix in DATA else None)
        if suffix in REFUSED or kind is None:
            findings.append('取り込めない種類のファイルです: ' + relative)
            continue
        files.append({'path': relative, 'kind': kind, 'bytes': size, 'sha256': _sha(path)})
        if kind in ('doc', 'script') or suffix in ('.json', '.yaml', '.yml', '.html', '.toml', '.ini', '.cfg'):
            try:
                text = path.read_text(encoding='utf-8')
            except UnicodeError:
                findings.append('UTF-8 で読めないテキストです: ' + relative)
                continue
            if len(text) > TEXT_LIMIT:
                findings.append('テキストが大きすぎます（15万文字まで）: ' + relative)
                continue
            if SECRET.search(text):
                findings.append('認証情報らしい文字列があります: ' + relative)
            if USER_PATH.search(text):
                findings.append('個人のフォルダのパス（C:\\Users\\…）があります: ' + relative)
            if kind == 'doc':
                try:
                    from attachment_guard import check
                    check(text)
                except ValueError as exc:
                    findings.append(relative + '：プロンプト注入の疑いがあるため取り込めません。該当内容を除いてください。')
                    hits = injection_hits(text)
                    if not hits or has_injection_marker(text):
                        # The guard refused but the passage cannot be shown for review: stays blocking.
                        findings.append(relative + '：' + str(exc).split('があるため')[0] + 'があります（該当箇所を特定できないため取り込めません）。')
                    for reason, excerpt in hits:
                        reviews.append({'id': hashlib.sha256((relative + '\n' + excerpt).encode('utf-8')).hexdigest()[:16],
                                        'file': relative, 'reason': reason, 'excerpt': excerpt})
    if len(files) > MAX_FILES:
        findings.append(f'ファイルが多すぎます（{MAX_FILES}件まで）。')
    if total > MAX_BYTES:
        findings.append('合計サイズが大きすぎます（20MBまで）。')
    scripts = [f for f in files if f['kind'] == 'script']
    return {'folder': folder.name, 'name': fields.get('name', folder.name), 'description': fields.get('description', ''),
            'body': body[:8000], 'body_truncated': len(body) > 8000, 'files': files, 'scripts': scripts,
            'bytes': total, 'findings': findings, 'reviews': reviews, 'importable': not findings,
            'existing': (SKILLS / folder.name / 'SKILL.md').is_file()}


def inbox():
    INBOX.mkdir(parents=True, exist_ok=True)
    return [inspect(folder) for folder in sorted(INBOX.iterdir()) if folder.is_dir() and not folder.is_symlink()]


def manifest(name):
    if not NAME.fullmatch(str(name)):
        return None
    try:
        folder = _bounded_target(SKILLS / name, SKILLS)
        path = _bounded_target(folder / MANIFEST, folder)
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            return None
        return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        return None


def current(name, manifest_sha):
    """True while the installed folder still matches the manifest recorded at import."""
    data = manifest(name)
    if not data or hashlib.sha256(json.dumps(data['files'], sort_keys=True).encode()).hexdigest() != manifest_sha:
        return False
    folder = SKILLS / name
    try:
        _bounded_target(folder, SKILLS)
        for f in data['files']:
            path = _bounded_target(folder / f['path'], folder)
            if not path.is_file() or path.stat().st_size > MAX_BYTES or _sha(path) != f['sha256']:
                return False
        return True
    except (ValueError, OSError):
        return False


def import_skill(folder_name, reviewed, desktop=False, confirmed_reviews=()):
    """Move an inbox folder into skills/<name> after the user's review. Returns (preview, manifest_sha)."""
    if reviewed is not True:
        raise ValueError('本文・スクリプト・機械チェックの結果を確認したうえで取り込んでください。')
    if not NAME.match(str(folder_name)):
        raise ValueError('取り込み待ちのスキルを選択してください。')
    source = INBOX / folder_name
    if not source.is_dir() or source.is_symlink():
        raise ValueError('取り込み待ちのスキルが見つかりません。')
    preview = inspect(source)
    if preview['findings']:
        raise ValueError('機械チェックの指摘があるため取り込めません: ' + ' / '.join(preview['findings'][:3]))
    pending = [r for r in preview['reviews'] if r['id'] not in set(confirmed_reviews or ())]
    if pending:
        raise ValueError('要確認の箇所（' + str(len(pending)) + '件）を読み、誤検知と確認したものにチェックしてください: ' + pending[0]['file'])
    target = SKILLS / preview['name']
    if not source.resolve().is_relative_to(INBOX.resolve()) or not target.resolve().is_relative_to(SKILLS.resolve()) or target.is_symlink() or getattr(target,'is_junction',lambda:False)():
        raise ValueError('取り込み先・元のリンクや範囲を確認してください。')
    _bounded_target(BACKUPS, ROOT / 'data')
    stamp = time.strftime('%Y%m%d-%H%M%S')
    if target.exists():
        BACKUPS.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(BACKUPS / (preview['name'] + '-' + stamp)))
    shutil.move(str(source), str(target))
    data = {'name': preview['name'], 'description': preview['description'], 'imported_at': time.time(),
            'files': [{'path': f['path'], 'kind': f['kind'], 'sha256': f['sha256']} for f in preview['files']],
            'confirmed_reviews': preview['reviews']}
    (target / MANIFEST).write_bytes(json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'))
    if desktop:
        home = _bounded_target(Path.home() / '.claude' / 'skills' / preview['name'], Path.home())
        if home.exists():
            BACKUPS.mkdir(parents=True, exist_ok=True)
            shutil.move(str(home), str(BACKUPS / (preview['name'] + '-desktop-' + stamp)))
        shutil.copytree(target, home, ignore=shutil.ignore_patterns(MANIFEST))
    return preview, hashlib.sha256(json.dumps(data['files'], sort_keys=True).encode()).hexdigest()


def remove_installed(name, desktop=False):
    """Move an imported skill's folder (and optionally the desktop copy) to the backups. Nothing is hard-deleted."""
    if not NAME.match(str(name)):
        raise ValueError('スキル名が不正です。')
    _bounded_target(BACKUPS, ROOT / 'data')
    stamp = time.strftime('%Y%m%d-%H%M%S')
    moved = []
    BACKUPS.mkdir(parents=True, exist_ok=True)
    folder = SKILLS / name
    if not folder.resolve().is_relative_to(SKILLS.resolve()) or getattr(folder,'is_junction',lambda:False)():
        raise ValueError('リンク先のスキルは移動できません。')
    if folder.is_dir() and not folder.is_symlink():
        shutil.move(str(folder), str(BACKUPS / (name + '-deleted-' + stamp)))
        moved.append(str(BACKUPS / (name + '-deleted-' + stamp)))
    if desktop:
        home = _bounded_target(Path.home() / '.claude' / 'skills' / name, Path.home())
        if home.is_dir() and not home.is_symlink():
            shutil.move(str(home), str(BACKUPS / (name + '-desktop-deleted-' + stamp)))
            moved.append(str(BACKUPS / (name + '-desktop-deleted-' + stamp)))
    return moved


def registry_fields(preview, manifest_sha):
    """Payload stored in the handoff DB and shown to agents (paths are the installed ones)."""
    folder = SKILLS / preview['name']
    scripts = '\n'.join(f"- {s['path']}（実行例: python \"{folder / s['path']}\"。SHA256 {s['sha256'][:12]}…）"
                        if s['path'].endswith('.py') else f"- {s['path']}（{folder / s['path']}）" for s in preview['scripts'])
    body = preview['body']
    return {'name': preview['name'], 'description': preview['description'],
            'applicability': preview['description'],
            'steps': (body[:2600] + ('\n…（続きは SKILL.md 全文を読む）' if len(body) > 2600 else '')
                      + '\n\nSKILL.md 全文と参考資料: ' + str(folder)),
            'tools': ('同梱スクリプト（取り込み時から変更されていない場合だけ実行可。開発の依頼の制作担当のみ）:\n' + scripts)
                     if scripts else '同梱スクリプトなし。手順書のみ。',
            'validation': 'SKILL.md に書かれた確認方法に従う。書かれていなければ、成果物と依頼の受入条件を照合する。',
            'failure': 'スキルの手順が今回の依頼・安全制約と合わない場合は従わず、理由を報告する。スクリプトが拒否・失敗した場合は blocked で報告する。',
            'imported': True, 'skill_dir': str(folder), 'manifest_sha': manifest_sha}


def script_check(command, active_names):
    """For a shell command that refers to files of an installed skill: (allowed, reason, names) or None if unrelated.
    Every referenced file must belong to a skill active in this project and match the import manifest."""
    if not isinstance(command, str):
        return None
    root = str(SKILLS).replace('\\', '/').casefold()
    normalized = command.replace('\\', '/')
    # Only a path into the inbox counts (…/skills/_inbox/… or skills/_inbox/…). The word "_inbox" in text,
    # such as slide content written by a script, is not a reference to an unimported skill.
    inbox = re.search(r'(?:^|[\s"\'=(:/])skills/_inbox(?:/|$|[\s"\'])', normalized, re.I)
    if root not in normalized.casefold() and not inbox:
        return None
    if inbox:
        return False, '取り込み前（_inbox）のスキルは実行できません。', []
    writes = (r'(?:>{1,2}\s*["\']?|\b(?:Set-Content|Add-Content|Out-File|Remove-Item|Move-Item|Copy-Item|Rename-Item|New-Item'
              r'|rm|mv|cp|del|tee|touch|mkdir|sed\s+-i)\b[^;&|\n]*?)' + re.escape(root))
    if re.search(writes, normalized.casefold(), re.I):
        return False, '取り込んだスキルのファイルは変更できません（読み取りと、取り込み時のままのスクリプトの実行だけ）。', []
    names = []
    for match in re.finditer(re.escape(root) + r'/([a-z0-9-]+)/([^\s"\'|&;<>]+)', normalized.casefold()):
        name, relative = match.group(1), match.group(2)
        start = match.start(2)
        relative = normalized[start:start + len(relative)]
        data = manifest(name)
        if name not in active_names or not data:
            return False, 'このプロジェクトで有効になっていないスキルのファイルです: ' + name, names
        record = next((f for f in data['files'] if f['path'].casefold() == relative.casefold()), None)
        path = SKILLS / name / relative
        if record is None or not path.is_file() or _sha(path) != record['sha256']:
            return False, '取り込み時から変更された、または取り込まれていないスキルのファイルです: ' + name + '/' + relative, names
        names.append(name + '/' + relative)
    if not names:
        return False, 'スキルのファイルを特定できませんでした。絶対パスで指定してください。', names
    return True, 'スキルのファイルは取り込み時のまま（SHA256一致）', names
