"""Audit Log: guard decisions are appended to <project>/audit/YYYY-MM.jsonl by 采来 (agents cannot edit audit/).

Entries hold categories and outcomes only, never the checked text, so the log itself does not leak.
Writing never stops the work: a failure is recorded in 采来's own event log instead.
"""
import json
import time
from pathlib import Path


def record(project, entry):
    try:
        folder = Path(project).resolve() / 'audit'
        if folder.is_symlink() or getattr(folder, 'is_junction', lambda: False)() or not folder.resolve().is_relative_to(Path(project).resolve()):
            return False
        folder.mkdir(exist_ok=True)
        target = folder / (time.strftime('%Y-%m') + '.jsonl')
        if target.is_symlink() or not target.resolve().is_relative_to(folder.resolve()):
            return False
        line = json.dumps(dict(entry, at=time.strftime('%Y-%m-%dT%H:%M:%S%z')), ensure_ascii=False)
        with open(target, 'a', encoding='utf-8', newline='\n') as handle:
            handle.write(line + '\n')
        return True
    except OSError:
        return False
