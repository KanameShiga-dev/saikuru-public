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
