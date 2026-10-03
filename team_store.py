"""Durable task state. All state transitions are serialized in this process."""
import copy
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


def uid():
    return uuid.uuid4().hex


def now():
    return time.time()


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.depth = 0
        self.db = sqlite3.connect(self.directory / 'team.sqlite3', check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS objects (kind TEXT, id TEXT PRIMARY KEY, body TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS transitions (seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, id TEXT, job_id TEXT, from_status TEXT, to_status TEXT, at REAL, reason TEXT)')
        self.db.execute('CREATE INDEX IF NOT EXISTS transition_job ON transitions(job_id, seq)')
        self.db.execute('CREATE TABLE IF NOT EXISTS metrics_meta (key TEXT PRIMARY KEY, value REAL)')
        self.db.execute("INSERT OR IGNORE INTO metrics_meta VALUES ('started_at', ?)", (now(),))
        self.db.commit()

    def put(self, kind, obj):
        with self.lock:
            obj = copy.deepcopy(obj)
            obj['updated_at'] = now()
            body = json.dumps(obj, ensure_ascii=False)
            if kind in ('job', 'task'):
                previous = self.db.execute('SELECT body FROM objects WHERE id=?', (obj['id'],)).fetchone()
                old = json.loads(previous[0]).get('status') if previous else None
                status = obj.get('status')
                if old != status:
                    job_id = obj['id'] if kind == 'job' else obj['job_id']
                    reason = 'system' if status == 'interrupted' else 'other'
                    if kind == 'job' and status == 'failed':
                        reason = 'task_error'
                    if reason != 'system':
                        pending = self.db.execute("SELECT body FROM objects WHERE kind='approval' AND json_extract(body,'$.job_id')=? AND json_extract(body,'$.status')='pending' ORDER BY rowid DESC", (job_id,)).fetchall()
                        for (raw,) in pending:
                            approval = json.loads(raw)
                            if kind == 'job' or approval.get('task_id') == obj['id']:
                                reason = approval.get('kind', 'other'); break
                        if reason == 'other' and kind == 'job':
                            reason = {'awaiting_approval':'plan', 'awaiting_acceptance':'completion'}.get(status, 'other')
                        if reason == 'other':
                            event = self.db.execute("SELECT body FROM events WHERE json_extract(body,'$.job_id')=? ORDER BY seq DESC LIMIT 1", (job_id,)).fetchone()
                            if event:
                                reason = json.loads(event[0]).get('type', 'other')
                        if kind == 'job' and status == 'blocked' and reason not in {'repair_limit','task_error'}:
                            tasks = self.db.execute("SELECT body FROM objects WHERE kind='task' AND json_extract(body,'$.job_id')=? ORDER BY rowid DESC", (job_id,)).fetchall()
                            latest = max((json.loads(raw) for (raw,) in tasks), key=lambda t:t.get('updated_at',0), default={})
                            report = latest.get('result') or {}
                            if latest.get('status')=='blocked' and (report.get('questions') or report.get('question')):
                                reason = 'question'
                            elif report.get('status')=='needs_changes':
                                reason = 'repair_limit'
                            elif latest.get('status')=='failed':
                                reason = 'task_error'
                    self.db.execute('INSERT INTO transitions(kind,id,job_id,from_status,to_status,at,reason) VALUES (?,?,?,?,?,?,?)',
                                    (kind, obj['id'], job_id, old, status, obj['updated_at'], reason))
            self.db.execute('INSERT INTO objects VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',
                            (kind, obj['id'], body))
            if self.depth == 0:
                self.db.commit()
            return obj

    def get(self, object_id, kind=None):
        with self.lock:
            row = self.db.execute('SELECT kind, body FROM objects WHERE id=?', (object_id,)).fetchone()
            if not row or (kind and row[0] != kind):
                raise ValueError('対象が見つかりません。')
            return json.loads(row[1])

    def update(self, object_id, **values):
        with self.lock:
            row = self.db.execute('SELECT kind, body FROM objects WHERE id=?', (object_id,)).fetchone()
            if not row:
                raise ValueError('対象が見つかりません。')
            obj = json.loads(row[1])
            obj.update(values)
            return self.put(row[0], obj)

    def all(self, kind):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute('SELECT body FROM objects WHERE kind=? ORDER BY rowid', (kind,))]

    def event(self, event_type, message, task_id=None, job_id=None):
        # Persist summaries, not raw provider protocol messages or environment variables.
        record = dict(type=event_type, message=str(message)[:3000], task_id=task_id,
                      job_id=job_id, at=now())
        with self.lock:
            self.db.execute('INSERT INTO events(body) VALUES (?)', (json.dumps(record, ensure_ascii=False),))
            if self.depth == 0:
                self.db.commit()

    @contextmanager
    def atomic(self):
        with self.lock:
            outer = self.depth == 0
            if outer:
                self.db.execute('BEGIN IMMEDIATE')
            self.depth += 1
            try:
                yield
                if outer:
                    self.db.commit()
            except BaseException:
                if outer:
                    self.db.rollback()
                raise
            finally:
                self.depth -= 1

    def events(self, count=100):
        with self.lock:
            rows = self.db.execute('SELECT seq, body FROM events ORDER BY seq DESC LIMIT ?', (count,))
            return [dict(json.loads(row[1]), seq=row[0]) for row in rows]

    def recover(self):
        with self.lock:
            for task in self.all('task'):
                if task['status'] in ('running', 'awaiting_approval'):
                    self.update(task['id'], status='interrupted', summary='サービス再起動により中断。変更内容を確認して再試行してください。')
                    self.update(task['job_id'], status='interrupted')
            for approval in self.all('approval'):
                if approval['status'] == 'pending' and approval['kind'] == 'tool':
                    self.update(approval['id'], status='expired', note='再起動により元の要求は失効しました。')

    def backup(self):
        target = self.directory / ('backup-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uid()[:6] + '.sqlite3')
        with self.lock, sqlite3.connect(target) as dest:
            self.db.backup(dest)
        return target.name
