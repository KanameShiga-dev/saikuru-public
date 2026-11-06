import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from team_log_retention import (
    ROTATION_SECONDS,
    has_pending_work,
    rotation_due,
    rotation_state,
    rotate_logs,
)


class LogRetentionTests(unittest.TestCase):
    def test_initial_state_preserves_existing_logs_for_a_week(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            log = data / 'server.stdout.log'
            log.write_text('existing log', encoding='utf-8')

            last_rotated = rotation_state(data, now=1000)

            self.assertEqual(last_rotated, 1000)
            self.assertTrue(log.exists())
            self.assertFalse(rotation_due(last_rotated, now=1000 + ROTATION_SECONDS - 1))
            self.assertTrue(rotation_due(last_rotated, now=1000 + ROTATION_SECONDS))

    def test_pending_tasks_and_approvals_defer_rotation(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / 'team.sqlite3'
            with closing(sqlite3.connect(database)) as db, db:
                db.execute('CREATE TABLE objects (kind TEXT, id TEXT, body TEXT)')
                db.execute('INSERT INTO objects VALUES (?, ?, ?)',
                           ('task', 'task-1', json.dumps({'status': 'queued'})))

            self.assertTrue(has_pending_work(database))

            with closing(sqlite3.connect(database)) as db, db:
                db.execute('DELETE FROM objects')
                db.execute('INSERT INTO objects VALUES (?, ?, ?)',
                           ('job', 'job-1', json.dumps({'status': 'awaiting_acceptance'})))

            self.assertTrue(has_pending_work(database))

            with closing(sqlite3.connect(database)) as db, db:
                db.execute('DELETE FROM objects')
                db.execute('INSERT INTO objects VALUES (?, ?, ?)',
                           ('approval', 'approval-1', json.dumps({'status': 'pending'})))

            self.assertTrue(has_pending_work(database))

    def test_rotation_removes_only_supervisor_logs_and_keeps_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            database = data / 'team.sqlite3'
            with closing(sqlite3.connect(database)) as db, db:
                db.execute('CREATE TABLE events (seq INTEGER PRIMARY KEY, body TEXT)')
                db.execute('INSERT INTO events(body) VALUES (?)', ('audit record',))
            stdout = data / 'server.stdout.log'
            stderr = data / 'server.stderr.log'
            unrelated = data / 'other.log'
            stdout.write_text('stdout', encoding='utf-8')
            stderr.write_text('stderr', encoding='utf-8')
            unrelated.write_text('keep', encoding='utf-8')

            rotate_logs(data, now=1234)

            self.assertFalse(stdout.exists())
            self.assertFalse(stderr.exists())
            self.assertTrue(unrelated.exists())
            self.assertEqual(rotation_state(data), 1234)
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute('SELECT body FROM events').fetchone()[0], 'audit record')

    def test_failed_log_removal_does_not_advance_rotation_clock(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            rotation_state(data, now=1000)
            (data / 'server.stdout.log').mkdir()

            with self.assertRaises(OSError):
                rotate_logs(data, now=2000)

            self.assertEqual(rotation_state(data), 1000)

    def test_corrupt_rotation_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            (data / 'log-rotation-state.json').write_text('{}', encoding='utf-8')

            with self.assertRaises(ValueError):
                rotation_state(data)


if __name__ == '__main__':
    unittest.main()
