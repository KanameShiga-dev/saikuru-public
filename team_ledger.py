"""Local project inventory. Discovery never executes project code or grants access."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import time

from team_workspace import root

SKIP = {'.git', '.venv', 'venv', 'node_modules', 'library', 'temp', 'obj', 'bin',
        'build', 'builds', 'dist', 'out', 'data', 'backups', 'gacha-backups',
        '_archive', '_migration', '__pycache__', 'assets', 'packages', 'projectsettings',
        'logs', 'cache', 'userdata', 'sample-project', 'target', '.next', '.gradle',
        'outputs', 'captures', 'certs', 'profiles', 'records', 'chrome-amazon-automation-profile',
        'diagnostics', 'tests', 'examples', 'docs', 'deploy', 'manual', 'archive', 'work', '_project_archives'}
CONTAINERS = {'Claude', 'Codex', 'tools', 'Projects', 'samples'}
# The work folder may hold the application itself; it is never a ledger project.
APP = Path(__file__).resolve().parent
MARKERS = {'.git', 'package.json', 'pyproject.toml', 'requirements.txt',
           'Cargo.toml', 'go.mod', 'platformio.ini', 'ProjectSettings', 'server.py'}
DOCS = ('AGENTS.md', 'README.md', 'README.txt', 'HANDOFF.md', 'PLAN.md',
        'docs/OPERATIONS.md', 'docs/VALIDATION.md', 'docs/LIMITS.md')
EDITABLE = {'name': 160, 'purpose': 1500, 'category': 80, 'status': 80,
            'owner': 120, 'build_method': 2000, 'verification_method': 2000,
            'completion_criteria': 2000, 'next_action': 2000, 'notes': 3000,
            'source_path': 1000, 'harness_proposal': 8000}


def stamp():
    return time.strftime('%Y-%m-%d %H:%M:%S')


def inside(path):
    if not isinstance(path, (str, Path)) or not Path(path).is_absolute():
        raise ValueError('絶対パスを指定してください。')
    path = Path(path).resolve()
    if not path.is_relative_to(root()):
        raise ValueError('台帳の調査・登録範囲は作業フォルダー（' + str(root()) + '）の中です。')
    return path


def directory_names(path):
    return {p.name for p in path.iterdir()}


def inspect(path):
    path = inside(path)
    family = path.relative_to(root()).parts[:1] == ('family',)
    names = directory_names(path)
    tech = []
    for marker, label in [('package.json', 'Node.js'), ('pyproject.toml', 'Python'),
                          ('requirements.txt', 'Python'), ('Cargo.toml', 'Rust'),
                          ('go.mod', 'Go'), ('platformio.ini', 'PlatformIO')]:
        if marker in names:
            tech.append(label)
    if any(n.endswith('.py') for n in names):
        tech.append('Python')
    if any(n.endswith(('.sln', '.csproj')) for n in names):
        tech.append('.NET / C#')
    if 'ProjectSettings' in names or (path / 'unity/ProjectSettings').is_dir():
        tech.append('Unity / C#')
    if any(n.endswith('.ps1') for n in names):
        tech.append('PowerShell')
    if any(n.endswith('.ino') for n in names):
        tech.append('Arduino')
    if any(n.endswith(('.html', '.htm')) for n in names):
        tech.append('Web / HTML / JavaScript')
    if names & {'build.gradle', 'build.gradle.kts', 'settings.gradle', 'settings.gradle.kts'}:
        tech.append('Android / Gradle')
    if (path / 'server/requirements.txt').is_file():
        tech.append('Python')
    readme_title = ''
    if not family:
        readme = path / 'README.md'
        if readme.is_file() and not readme.is_symlink():
            with readme.open(encoding='utf-8-sig', errors='replace') as stream:
                for _ in range(12):
                    line = stream.readline(500)
                    if line.startswith('# '):
                        readme_title = line[2:].strip()[:160]
                        break
    git = {'repository': '.git' in names, 'branch': '', 'head': '',
           'changes': None, 'staged': None, 'untracked': None, 'error': ''}
    git['parent_repository'] = next((str(parent) for parent in path.parents
        if parent != root() and parent.is_relative_to(root()) and (parent / '.git').exists()), '')
    if git['repository']:
        def run(args):
            p = subprocess.run(['git', '--no-optional-locks', '-C', str(path)] + args,
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if p.returncode:
                raise ValueError('Git情報を取得できません（未コミットの初期状態・権限等を確認）。')
            return p.stdout.rstrip()
        try:
            git['branch'] = run(['branch', '--show-current']) or '(detached)'
            git['head'] = run(['rev-parse', 'HEAD'])
            lines = run(['status', '--porcelain=v1', '-uno']).splitlines()
            git['changes'] = len(lines)
            git['staged'] = sum(bool(line and line[0] != ' ') for line in lines)
            # Only aggregate untracked paths; file names and contents are not persisted.
            git['untracked'] = len(run(['ls-files', '--others', '--exclude-standard']).splitlines())
        except (OSError, ValueError, subprocess.TimeoutExpired):
            git['error'] = 'Git状態は未確認（取得失敗）。'
    category = 'ソース' if git['repository'] or tech else '資料・候補（未確認）'
    if any(word in path.name.lower() for word in ('deploy', 'tvinstall', 'offline')):
        category = '配備・ビルド用コピー（関係未確認）'
    if family:
        category = '家庭関連（内容未調査）'
    from team_instruction_health import inspect_instructions
    instruction_health = inspect_instructions(path, root())
    return {'instruction_health': instruction_health, 'path': str(path), 'technology': sorted(set(tech)), 'git': git, 'readme_title': readme_title,
            'documents': [str(path / d) for d in DOCS if (path / d).is_file()],
            'suggested_category': category, 'exists': True, 'checked_at': stamp()}


class Ledger:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.directory / 'project_ledger.sqlite3', check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS scans(id INTEGER PRIMARY KEY, at TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS revisions(id INTEGER PRIMARY KEY, project_id TEXT NOT NULL, at TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS archives(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS moves(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, body TEXT NOT NULL);
        ''')
        self.db.commit()

    def backup(self):
        name = f'project-ledger-backup-{time.time_ns()}.sqlite3'
        with self.lock, sqlite3.connect(self.directory / name) as dest:
            self.db.backup(dest)
        return name

    def issues(self):
        with self.lock:
            path = self.directory / 'development-issues.json'
            if not path.exists():
                return []
            return json.loads(path.read_text(encoding='utf-8'))

    def save_issue(self, body):
        fields = {'project': 160, 'title': 240, 'impact': 2000,
                  'next_action': 2000, 'owner': 120}
        item = {}
        for key, limit in fields.items():
            value = body.get(key, '')
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError('課題の入力形式または文字数を確認してください。')
            item[key] = value.strip()
        if not item['title'] or not item['project']:
            raise ValueError('対象プロジェクトと課題名は必須です。')
        for key, choices, default in (
            ('severity', ('blocker', 'warning'), 'warning'),
            ('status', ('open', 'in_progress', 'resolved', 'deferred'), 'open'),
        ):
            item[key] = body.get(key, default)
            if item[key] not in choices:
                raise ValueError('課題の区分または状態が不正です。')
        with self.lock:
            items = self.issues()
            issue_id = body.get('id')
            old = next((i for i in items if i['id'] == issue_id), None)
            if issue_id and old is None:
                raise ValueError('課題が見つかりません。')
            if old and body.get('revision') != old['revision']:
                raise ValueError('課題が更新されています。再読込して確認してください。')
            at = stamp()
            item.update(id=old['id'] if old else hashlib.sha256(os.urandom(32)).hexdigest()[:24],
                        revision=old['revision'] + 1 if old else 1,
                        created_at=old['created_at'] if old else at, updated_at=at,
                        history=(old.get('history', []) + [{k: v for k, v in old.items() if k != 'history'}]) if old else [])
            items = [item if i['id'] == item['id'] else i for i in items] if old else items + [item]
            path = self.directory / 'development-issues.json'
            temp = self.directory / 'development-issues.json.tmp'
            try:
                with temp.open('w', encoding='utf-8') as stream:
                    json.dump(items, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, path)
            finally:
                if temp.exists():
                    temp.unlink()
            return item

    def snapshot(self, approved=None, instruction_checks=False):
        with self.lock:
            projects = [json.loads(row[0]) for row in self.db.execute('SELECT body FROM projects ORDER BY path COLLATE NOCASE')]
            scan = self.db.execute('SELECT body FROM scans ORDER BY id DESC LIMIT 1').fetchone()
            archives = [json.loads(row[0]) for row in self.db.execute('SELECT body FROM archives ORDER BY rowid DESC')]
            moves = [json.loads(row[0]) for row in self.db.execute('SELECT body FROM moves ORDER BY rowid DESC')]
        allowed = {os.path.normcase(str(Path(p).resolve())) for p in (approved or ())}
        from team_instruction_health import instruction_presence
        for item in projects:
            item['worker_allowed'] = None if approved is None else os.path.normcase(item['path']) in allowed
            if instruction_checks:
                item['instruction_presence'] = instruction_presence(item['path'], root())
        return {'projects': projects, 'archives': archives, 'moves': moves, 'last_scan': json.loads(scan[0]) if scan else None,
                'root': str(root()), 'computer_use_policy': '基本禁止', 'schema_version': 1}

    def put_observation(self, path):
        observation = inspect(path)
        key = os.path.normcase(str(path))
        object_id = hashlib.sha256(key.encode()).hexdigest()[:24]
        row = self.db.execute('SELECT body FROM projects WHERE path=? COLLATE NOCASE', (str(path),)).fetchone()
        if row:
            object_id = json.loads(row[0])['id']
        elif self.db.execute('SELECT 1 FROM projects WHERE id=?', (object_id,)).fetchone():
            object_id = hashlib.sha256((key + str(time.time_ns())).encode()).hexdigest()[:24]
        item = json.loads(row[0]) if row else {
            'id': object_id, 'path': str(path), 'name': path.name,
            'purpose': '未確認', 'category': observation['suggested_category'],
            'status': '未評価', 'owner': '未確認', 'build_method': '未確認',
            'verification_method': '未確認', 'completion_criteria': '未確認',
            'next_action': '目的・検証方法・完了条件を確認し、ハーネス化の範囲を決める。',
            'notes': '', 'source_path': '', 'first_seen': stamp(), 'manual_updated_at': None}
        item['observation'] = observation
        self.db.execute('INSERT INTO projects VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',
                        (object_id, str(path), json.dumps(item, ensure_ascii=False)))
        return item

    def update(self, body):
        object_id = body.get('id')
        fields = body.get('fields')
        if not isinstance(fields, dict) or set(fields) - EDITABLE.keys():
            raise ValueError('編集項目が不正です。')
        for key, value in fields.items():
            if not isinstance(value, str) or len(value) > EDITABLE[key]:
                raise ValueError(f'{key} の文字数または形式が不正です。')
        if fields.get('source_path'):
            fields['source_path'] = str(inside(fields['source_path']))
        with self.lock:
            row = self.db.execute('SELECT body FROM projects WHERE id=?', (object_id,)).fetchone()
            if not row:
                raise ValueError('プロジェクトが見つかりません。')
            item = json.loads(row[0])
            if body.get('expected_updated_at') != item['manual_updated_at']:
                raise ValueError('別の画面で更新されています。再読み込みしてから編集してください。')
            self.backup()
            with self.db:
                self.db.execute('INSERT INTO revisions(project_id,at,body) VALUES (?,?,?)',
                                (object_id, stamp(), row[0]))
                item.update(fields)
                item['manual_updated_at'] = str(time.time_ns())
                self.db.execute('UPDATE projects SET body=? WHERE id=?', (json.dumps(item, ensure_ascii=False), object_id))
            return {'ok': True}

    def add(self, body):
        path = inside(body.get('path', ''))
        if not path.is_dir() or path == root() or path.is_relative_to(APP) or APP.is_relative_to(path):
            raise ValueError('作業フォルダーの中の既存プロジェクトフォルダを指定してください。')
        with self.lock:
            self.backup()
            with self.db:
                return self.put_observation(path)

    def scan(self):
        found, errors, seen = set(), [], 0
        def walk(path, depth):
            nonlocal seen
            seen += 1
            if seen > 2500:
                errors.append('フォルダ数の上限2500に到達。残りは未調査。')
                return
            try:
                names = directory_names(path)
                marker = bool(names & MARKERS) or any(n.endswith(('.sln', '.csproj', '.ino')) for n in names)
                project_category = path.parent == base / 'Projects'
                categorized_project = depth == 3 and path.parent.parent == base / 'Projects'
                candidate = (depth == 1 and path.name not in CONTAINERS) or (
                    depth == 2 and path.parent.name in CONTAINERS and not project_category) or categorized_project or marker
                if candidate:
                    found.add(path)
                # Family records, dependencies and repository internals are never traversed.
                if depth >= 4 or path.name == 'family' or marker:
                    return
                for child in sorted(path.iterdir()):
                    if child.is_dir() and not child.is_symlink() and not getattr(child, 'is_junction', lambda: False)() and not child.name.startswith(('.', '_project_archive_pending_')) and child.name.lower() not in SKIP and not (depth >= 2 and child.name.lower() == 'tools') and child.resolve() != APP:
                        walk(child, depth + 1)
            except (OSError, ValueError):
                errors.append(str(path) + ': フォルダ情報を取得できません。')
        base = root()
        walk(base, 0)
        with self.lock:
            backup = self.backup()
            with self.db:
                for path in sorted(found):
                    try:
                        self.put_observation(path)
                    except (OSError, ValueError):
                        errors.append(str(path) + ': 登録時の情報取得に失敗。')
                # Disappeared folders stay in the ledger. No automatic deletion.
                for object_id, raw in self.db.execute('SELECT id,body FROM projects').fetchall():
                    item = json.loads(raw)
                    item['observation']['exists'] = Path(item['path']).is_dir()
                    self.db.execute('UPDATE projects SET body=? WHERE id=?', (json.dumps(item, ensure_ascii=False), object_id))
                report = {'at': stamp(), 'root': str(base), 'max_depth': 4,
                    'visited_directories': seen, 'discovered': len(found), 'errors': errors,
                    'excluded': sorted(SKIP), 'backup': backup,
                    'scope_note': '深さ4まで。Git/技術構成のあるフォルダは内部探索を停止。家庭関連は直下のメタ情報のみ。資料・候補も含む。除外範囲内や深い場所は手動登録可能。'}
                self.db.execute('INSERT INTO scans(at,body) VALUES (?,?)', (stamp(), json.dumps(report, ensure_ascii=False)))
        self.export()
        return report

    def export(self):
        snapshot = self.snapshot()
        target = self.directory / 'project-ledger.json'
        temp = target.with_suffix(f'.{time.time_ns()}.tmp')
        temp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temp, target)
        def cell(value):
            return str(value).replace('|', '/').replace('\n', ' ')
        lines = ['# 開発プロジェクト台帳', '', '正本：project_ledger.sqlite3。編集・再調査は作業ボードの「プロジェクト台帳」から行います。',
                 '', '調査対象：' + snapshot['root'] + '。実行・完了判定は未実施。資料・候補・配備コピーも含みます。', '',
                 '| 名称 | 場所 | 分類 | 技術構成 | 管理状況 |', '|---|---|---|---|---|']
        for p in snapshot['projects']:
            lines.append('| ' + ' | '.join(map(cell, [p['name'], p['path'], p['category'],
                ' / '.join(p['observation']['technology']) or '未確認', p['status']])) + ' |')
        report_path = target.with_suffix('.md')
        report_temp = report_path.with_suffix(f'.{time.time_ns()}.md.tmp')
        report_temp.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        os.replace(report_temp, report_path)
        return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='作業フォルダーの開発プロジェクト台帳')
    parser.add_argument('action', choices=['scan', 'list', 'export', 'backup'])
    parser.add_argument('--data', type=Path, default=Path(__file__).parent / 'data')
    args = parser.parse_args()
    ledger = Ledger(args.data)
    result = getattr(ledger, args.action)() if args.action != 'list' else ledger.snapshot()
    print(json.dumps(str(result) if isinstance(result, Path) else result, ensure_ascii=False, indent=2))
