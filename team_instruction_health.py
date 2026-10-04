"""Bounded, read-only instruction size diagnostics; never return document bodies."""
import os
from pathlib import Path
import stat
import time
import tomllib

CODEX_DEFAULT = 32768
CLAUDE_MAX = 4 * 1024 * 1024
CLAUDE_LINES = 200
NAMES = ('AGENTS.override.md', 'AGENTS.md', 'CLAUDE.md', 'CLAUDE.local.md',
         '.claude/CLAUDE.md', '.claude/AGENTS.md')
SKIP = {'.git', '.codex', '.claude', '.venv', 'venv', 'node_modules', 'data',
        'backups', 'library', 'temp', 'obj', 'bin', 'build', 'dist', '__pycache__',
        'logs', 'cache', '_archive', '_migration', 'userdata', 'certs', 'profiles',
        'family', '.ssh', '.aws', '.azure', 'secrets', 'records', 'captures', 'diagnostics'}
HINT = '必須ルールを先頭に残し、詳細を別資料・必要時に読むSkill・対象を限定したルールへ移してください。自動短縮や上限変更は行いません。'


def instruction_presence(project, boundary):
    """Check local instruction names using metadata only, without following links."""
    project, boundary = Path(project), Path(boundary).resolve()
    result = {'agents': None, 'claude': None, 'status': 'unknown',
              'checked_at': time.strftime('%Y-%m-%d %H:%M:%S'),
              'agents_files': [], 'claude_files': [], 'message': '指示ファイルの有無は未確認です。'}
    def metadata(path):
        for candidate in (*reversed(path.parents), path):
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('reparse')
        return path.lstat()
    try:
        if not project.is_absolute() or not project.resolve().is_relative_to(boundary):
            return result
        if 'family' in [p.casefold() for p in project.relative_to(boundary).parts]:
            result['message'] = '家庭関連の指示ファイルは未確認です。'
            return result
        if not stat.S_ISDIR(metadata(project).st_mode):
            result['message'] = '対象フォルダがないため、指示ファイルの有無は未確認です。'
            return result
    except FileNotFoundError:
        result['message'] = '対象フォルダがないため、指示ファイルの有無は未確認です。'
        return result
    except (OSError, ValueError):
        result['message'] = 'リンク・権限・対象範囲等により、指示ファイルの有無は未確認です。'
        return result
    for role, names in [('agents', ('AGENTS.md', 'AGENTS.override.md')),
                        ('claude', ('CLAUDE.md', '.claude/CLAUDE.md', 'CLAUDE.local.md'))]:
        uncertain = False
        for name in names:
            try:
                info = metadata(project / name)
                if stat.S_ISREG(info.st_mode):
                    result[role + '_files'].append(name)
                else:
                    uncertain = True
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                uncertain = True
        result[role] = True if result[role + '_files'] else None if uncertain else False
    agents, claude = result['agents'], result['claude']
    if agents is None or claude is None:
        result['message'] = ' ／ '.join(label + ('あり' if value is True else 'なし' if value is False else '未確認')
                                      for label, value in [('AGENTS.md系：', agents), ('CLAUDE.md系：', claude)])
    elif agents and claude:
        result.update(status='both_present', message='AGENTS.md系・CLAUDE.md系の両方があります。')
    elif not agents and not claude:
        result.update(status='both_missing', message='AGENTS.md系・CLAUDE.md系の両方がありません。')
    elif not agents:
        result.update(status='agents_missing', message='AGENTS.md系がありません（CLAUDE.md系はあります）。')
    else:
        result.update(status='claude_missing', message='CLAUDE.md系がありません（AGENTS.md系はあります）。')
    return result


def inspect_instructions(project, boundary=None, deep=False):
    original = Path(project)
    project = original.resolve()
    boundary = Path(boundary).resolve() if boundary else project
    if not project.is_relative_to(boundary) or not project.is_dir():
        raise ValueError('指示ファイルを確認できる既存の対象フォルダを選択してください。')
    report = {'checked_at': time.strftime('%Y-%m-%d %H:%M:%S'), 'files': [],
              'warnings': [], 'partial': False, 'codex_limit_bytes': CODEX_DEFAULT,
              'limit_basis': '公式既定値（実行時の上書きは未確認）',
              'claude_recommended_lines': CLAUDE_LINES, 'claude_max_bytes': CLAUDE_MAX,
              'claude_warning_bytes': CODEX_DEFAULT,
              'scope': '階層と子フォルダの候補' if deep else '開始フォルダまでの候補',
              'hint': HINT}
    def warn(code, message):
        if not any(w['code'] == code and w['message'] == message for w in report['warnings']):
            report['warnings'].append({'code': code, 'message': message})
    report['presence'] = instruction_presence(original, boundary)
    if report['presence']['status'] != 'both_present':
        warn('missing_instructions' if report['presence']['status'] != 'unknown' else 'presence_unknown',
             report['presence']['message'])
    def linked(path):
        return any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction())
                   for p in (path, *path.parents))
    if linked(original) or 'family' in project.relative_to(boundary).parts:
        report['partial'] = True
        warn('unreadable', 'リンク経由・家庭関連の指示ファイルは検査していません。')
        return report
    home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    fallbacks = []
    for cfg, label in [(home / 'config.toml', '利用者設定'),
                       (project / '.codex/config.toml', 'プロジェクト設定候補（信頼・優先順位は未確認）')]:
        try:
            if cfg.is_file() and not linked(cfg):
                if cfg.stat().st_size > 65536:
                    raise ValueError('large config')
                data = tomllib.loads(cfg.read_text(encoding='utf-8-sig'))
                limit = data.get('project_doc_max_bytes')
                if type(limit) is int and 0 <= limit <= 64 * 1024 * 1024:
                    if cfg == project / '.codex/config.toml':
                        report['project_config_limit_bytes'] = limit
                        report['codex_limit_bytes'] = min(report['codex_limit_bytes'], limit)
                        report['limit_basis'] = '利用者/既定とプロジェクト設定候補の小さい上限で警告（信頼・実効値は未確認）'
                    else:
                        report['codex_limit_bytes'] = limit
                        report['limit_basis'] = label + 'の値を用いた見積もり（実効値は未確認）'
                names = data.get('project_doc_fallback_filenames')
                if isinstance(names, list):
                    candidates = [n for n in names[:16] if isinstance(n, str) and n
                                 and n not in ('.', '..') and not any(c in n for c in '/\\:')]
                    fallbacks = list(dict.fromkeys((*fallbacks, *candidates)))[:16]
        except (OSError, ValueError, UnicodeError):
            report['partial'] = True
            warn('config_unknown', 'Codex設定の確認が不完全です。上限は参考値として扱ってください。')
    # Nearest Git root within the authorized boundary; custom root markers are not inferred.
    chain = [project]
    for parent in project.parents:
        if (chain[-1] / '.git').exists() or not parent.is_relative_to(boundary):
            break
        chain.append(parent)
        if parent == boundary:
            break
    if not any((p / '.git').exists() for p in chain):
        chain = [project]
    chain.reverse()
    all_names = tuple(dict.fromkeys((*NAMES, *fallbacks)))
    files, measured, scanned, bytes_read = {}, {}, 0, 0
    deadline = time.monotonic() + 4
    def measure(folder, name, scope='project'):
        nonlocal bytes_read
        target = folder / name
        key = str(target)
        if key in measured:
            return measured[key]
        if len(report['files']) >= 128 or time.monotonic() >= deadline:
            report['partial'] = True
            return None
        try:
            if not target.exists():
                return None
            if linked(target) or not target.is_file():
                raise ValueError('link or not file')
            size = target.stat().st_size
            lines = None
            if size <= CLAUDE_MAX and bytes_read + size <= 16 * 1024 * 1024:
                with target.open('rb') as stream:
                    raw = stream.read(CLAUDE_MAX + 1)
                if len(raw) != size:
                    raise ValueError('changed')
                bytes_read += size
                try:
                    text = raw.decode('utf-8-sig')
                    lines = len(text.splitlines())
                except UnicodeError:
                    report['partial'] = True
                    warn('encoding_unknown', 'UTF-8以外の指示ファイルは行数未確認です。容量だけを確認しています。')
            else:
                report['partial'] = True
            label = target.relative_to(boundary).as_posix() if scope == 'project' else '利用者共通/' + name
            row = {'path': label, 'bytes': size, 'lines': lines, 'scope': scope}
            measured[key] = row
            report['files'].append(row)
            if name in ('AGENTS.md', 'AGENTS.override.md', *fallbacks) and size > report['codex_limit_bytes']:
                warn('codex_file_limit', label + ' はCodexの読み込み上限候補を超えています。末尾が省略される可能性があります。')
            if name in ('CLAUDE.md', 'CLAUDE.local.md', '.claude/CLAUDE.md') and size > CLAUDE_MAX:
                warn('claude_file_limit', label + ' は4 MiBを超え、現在の公式仕様では読み込み対象外です。')
            elif name in ('CLAUDE.md', 'CLAUDE.local.md', '.claude/CLAUDE.md') and size >= CODEX_DEFAULT:
                warn('claude_large_bytes', label + ' は采来の肥大化注意目安32 KiB以上です。これはClaude Codeの公式読み込み上限ではありません。')
            if name not in ('AGENTS.override.md',) and lines is not None and lines >= CLAUDE_LINES:
                warn('recommended_size', label + ' は200行以上です。Claude Code向けの推奨サイズを超えています（打ち切り上限ではありません）。')
            return row
        except (OSError, ValueError):
            report['partial'] = True
            warn('unreadable', '指示ファイルの一部を読み取れません。サイズ確認は未完了です。')
            return None
    global_row = None
    for name in ('AGENTS.override.md', 'AGENTS.md'):
        row = measure(home, name, 'global')
        if row and row['bytes']:
            global_row = row
            break
    claude_home = Path(os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home() / '.claude')))
    measure(claude_home, 'CLAUDE.md', 'global')
    def visit(folder, depth, inherited):
        nonlocal scanned
        scanned += 1
        if scanned > 400 or len(report['files']) >= 128 or time.monotonic() >= deadline:
            report['partial'] = True
            return
        rows = {name: measure(folder, name) for name in all_names}
        chosen = next((rows[n] for n in ('AGENTS.override.md', 'AGENTS.md', *fallbacks)
                       if rows.get(n) and rows[n]['bytes']), None)
        total = inherited + (chosen['bytes'] if chosen else 0)
        files[str(folder)] = total
        if total >= report['codex_limit_bytes'] and total:
            rel = folder.relative_to(boundary).as_posix()
            warn('codex_chain_limit', rel + ' までの指示候補の合計がCodex上限に到達しています。後続の指示が省略される可能性があります。')
        elif total >= report['codex_limit_bytes'] * .8 and total:
            warn('codex_near_limit', folder.relative_to(boundary).as_posix() + ' までの指示候補がCodex上限の80%以上です。')
        if not deep or folder != project and not folder.is_relative_to(project):
            return
        if depth >= 6:
            report['partial'] = True
            return
        try:
            for index, child in enumerate(folder.iterdir()):
                if (index >= 2000 or scanned >= 400 or len(report['files']) >= 128
                        or time.monotonic() >= deadline):
                    report['partial'] = True
                    break
                if child.name.casefold() in SKIP:
                    continue
                if linked(child):
                    report['partial'] = True
                    continue
                if child.is_dir():
                    visit(child, depth + 1, total)
        except OSError:
            report['partial'] = True
    total = global_row['bytes'] if global_row else 0
    for folder in chain:
        visit(folder, 0, total)
        total = files.get(str(folder), total)
    report['codex_candidate_max_bytes'] = max(files.values(), default=total)
    if report['partial']:
        warn('partial', '検査の上限・リンク・読取失敗等により未確認の範囲があります。警告なしでも全指示の読み込み保証にはなりません。')
    report['note'] = '候補のサイズ診断です。Codexの実効設定・信頼状態、Claudeの版・設定・@import・管理者/利用者ルール、実際のAI読み込みは未確認です。'
    return report
