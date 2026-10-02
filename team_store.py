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
        self.db.commit()

    def put(self, kind, obj):
        with self.lock:
            obj = copy.deepcopy(obj)
            obj['updated_at'] = now()
            self.db.execute('INSERT INTO objects VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',
                            (kind, obj['id'], json.dumps(obj, ensure_ascii=False)))
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
