"""Retention helpers for the supervisor-owned server output logs."""
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path


ROTATION_SECONDS = 7 * 24 * 60 * 60
LOG_NAMES = ('server.stdout.log', 'server.stderr.log')
ACTIVE_JOB_STATUSES = ('planning', 'running', 'awaiting_approval', 'awaiting_acceptance')
ACTIVE_TASK_STATUSES = ('queued', 'running', 'awaiting_approval')


def rotation_state(data_dir, now=None):
    """Read or initialize the weekly rotation clock without touching existing logs."""
    data_dir = Path(data_dir)
    state_path = data_dir / 'log-rotation-state.json'
    if not state_path.exists():
        last_rotated = time.time() if now is None else now
        _write_state(state_path, last_rotated)
        return last_rotated
    value = json.loads(state_path.read_text(encoding='utf-8'))
    last_rotated = value.get('last_rotated_at') if isinstance(value, dict) else None
    if type(last_rotated) not in (int, float) or last_rotated < 0:
        raise ValueError('ログ整理の基準時刻が不正です。')
    return float(last_rotated)


def rotation_due(last_rotated, now=None):
    now = time.time() if now is None else now
    return now - last_rotated >= ROTATION_SECONDS


def has_pending_work(database_path):
    """Conservatively detect work or approvals that must not be interrupted."""
    database_path = Path(database_path)
    if not database_path.exists():
        return False
    uri = database_path.resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
        row = db.execute(
            "SELECT 1 FROM objects WHERE "
            "(kind='job' AND json_extract(body,'$.status') IN (?, ?, ?, ?)) OR "
            "(kind='task' AND json_extract(body,'$.status') IN (?, ?, ?)) OR "
            "(kind='approval' AND json_extract(body,'$.status')='pending') LIMIT 1",
            (*ACTIVE_JOB_STATUSES, *ACTIVE_TASK_STATUSES)).fetchone()
    return row is not None


def rotate_logs(data_dir, now=None):
    """Remove only the two closed supervisor log files and advance the clock."""
    data_dir = Path(data_dir)
    timestamp = time.time() if now is None else now
    for name in LOG_NAMES:
        (data_dir / name).unlink(missing_ok=True)
    _write_state(data_dir / 'log-rotation-state.json', timestamp)
    return timestamp


def _write_state(path, timestamp):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps({'last_rotated_at': timestamp}), encoding='utf-8')
    temporary.replace(path)
