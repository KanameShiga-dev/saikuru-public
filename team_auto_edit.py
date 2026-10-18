"""Backed-up source/document edits for jobs explicitly set to auto-execute.

Never infer file paths from a model explanation or from the approval's grantRoot.
Only changes correlated with the native fileChange item are eligible.
"""
import hashlib
import json
from pathlib import Path, PureWindowsPath
from team_store import uid, now


SOURCE_SUFFIXES = {'.cs', '.py', '.js', '.mjs', '.ts', '.tsx', '.jsx', '.css', '.scss',
                   '.html', '.htm', '.md', '.txt'}
PROTECTED = {'.git', '.codex', '.claude', '.ssh', 'node_modules', '.venv', 'venv',
             'runtime', 'saves', 'savedata', 'backups', 'agents.md', 'claude.md',
             'auth.json', '.credentials.json', 'settings.py', 'config.py', 'config.js',
             'config.ts', 'conftest.py'}


def approved_scope_edit(ctx, target):
    """Recognize the explicit scope contract; unknown formats remain manual.

    Approval is bound to the actual ancestor planner, never any older plan.
    This grants ordinary source/document saves only, not commands or deletion.
    """
    if not ctx.writable or ctx.task.get('role') != 'builder':
        return False
    store = ctx.engine.store
    job = store.get(ctx.task['job_id'], 'job')
    if job.get('status') in ('cancelled', 'interrupted', 'failed'):
        return False
    task, seen = ctx.task, set()
    while task.get('role') != 'planner':
        if task['id'] in seen or not task.get('after'):
            return False
        seen.add(task['id'])
        task = store.get(task['after'], 'task')
        if task.get('job_id') != job['id']:
            return False
    plans = [a for a in store.all('approval') if a.get('job_id') == job['id']
             and a.get('task_id') == task['id'] and a.get('kind') == 'plan']
    if len(plans) != 1 or plans[0].get('status') != 'approved':
        return False
    root = Path(ctx.project).resolve()
    try:
        text = (root / 'SCOPE.md').read_text(encoding='utf-8-sig')
        roots = [line[6:].strip() for line in text.splitlines() if line.startswith('Root: ')]
        allowed = [line.split(':', 1)[1].strip() for line in text.splitlines()
                   if line.startswith('Allowed after human G3 approval: ')]
        if len(roots) != 1 or Path(roots[0]).resolve() != root or len(allowed) != 1:
            return False
        relative = target.resolve().relative_to(root)
        if target.is_symlink() or any((root.joinpath(*relative.parts[:n])).is_symlink()
                                     for n in range(1, len(relative.parts) + 1)):
            return False
        protected = PROTECTED | {'scope.md', 'command_allowlist.md', 'security_policy.md',
                                 'data_classification.md', 'tool_policy.yaml', 'audit'}
        if any(p.casefold() in protected or p.casefold().startswith('.env') for p in relative.parts):
            return False
        if target.suffix.lower() not in SOURCE_SUFFIXES:
            return False
        # Only explicit conventional directories, no prose-based wildcard grants.
        grants = {value.strip() for value in allowed[0].split(',')}
        rel = relative.as_posix()
        return any(grant in grants and rel.startswith(grant[:-2])
                   for grant in ('app/**', 'tests/**', 'docs/**', 'data/**', 'evidence/**', '.local/test-tmp/**')) or (
                       rel == 'README.md' and 'README.md' in grants)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def auto_edit_reason(ctx, payload):
    if (payload.get('source') != 'codex'
            or payload.get('operation') != 'item/fileChange/requestApproval'
            or not ctx.writable):
        return None
    job = ctx.engine.store.get(ctx.task['job_id'], 'job')
    if not job.get('auto_execute'):
        return None
    details = payload.get('details') or {}
    if details.get('grantRoot') or details.get('reason'):
        return None
    changes = payload.get('fileChanges')
    if not isinstance(changes, list) or not 1 <= len(changes) <= 50:
        return None
    root = Path(ctx.project).resolve()
    targets, seen, total = [], set(), 0
    try:
        for change in changes:
            if not isinstance(change, dict):
                return None
            kind = change.get('kind') or {}
            if kind.get('type') not in ('add', 'update') or kind.get('move_path'):
                return None
            raw = change.get('path')
            if not isinstance(raw, str) or not raw or raw.startswith(('\\\\', '//')):
                return None
            if ':' in raw[2:] or any(c in raw for c in '*?') or PureWindowsPath(raw).is_reserved():
                return None
            path = Path(raw)
            if any(p == '..' or p.lower() in PROTECTED or p.lower().startswith('.env') for p in path.parts):
                return None
            path = path if path.is_absolute() else root / path
            resolved = path.resolve()
            if not resolved.is_relative_to(root) or resolved == root or path.is_symlink():
                return None
            relative = resolved.relative_to(root)
            if any(p.lower() in PROTECTED or p.lower().startswith('.env') for p in relative.parts):
                return None
            if resolved.suffix.lower() not in SOURCE_SUFFIXES or str(resolved).casefold() in seen:
                return None
            if resolved.exists() and (not resolved.is_file() or resolved.stat().st_size > 10_000_000):
                return None
            if not resolved.exists() and kind['type'] == 'update':
                return None
            diff = change.get('diff')
            if not isinstance(diff, str) or not diff or len(diff) > 2_000_000:
                return None
            before = resolved.read_bytes() if resolved.exists() else None
            total += len(before or b'')
            if total > 30_000_000:
                return None
            seen.add(str(resolved).casefold())
            targets.append((resolved, relative, before, kind['type'], hashlib.sha256(diff.encode()).hexdigest()))
        ctx.check()
        folder = ctx.engine.store.directory / 'file-backups' / ctx.task['id'] / uid()
        folder.mkdir(parents=True, exist_ok=False)
        manifest = []
        for i, (path, relative, before, kind, digest) in enumerate(targets):
            # If another process changed this file during inspection, keep approval manual.
            if path.resolve() != path or (path.read_bytes() if path.exists() else None) != before:
                return None
            backup = f'{i}.before.bin' if before is not None else None
            if backup:
                (folder / backup).write_bytes(before)
            manifest.append({'target': str(path), 'existed': before is not None, 'backup': backup,
                             'before_sha256': hashlib.sha256(before).hexdigest() if before is not None else None,
                             'operation': kind, 'diff_sha256': digest})
        (folder / 'manifest.json').write_text(json.dumps({'at': now(), 'source': 'codex',
            'item_id': details.get('itemId'), 'files': manifest}, ensure_ascii=False, indent=2), encoding='utf-8')
        ctx.check()
        return ('依頼内のソース・文書編集を自動承認（変更前を退避）: '
                + ', '.join(str(t[1]) for t in targets)[:1800])
    except (OSError, ValueError, TypeError, AttributeError):
        return None
