"""Offline checks for the lightweight history list and its existing detail view."""
import sqlite3
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from team_history import snapshot
from team_store import Store


class HistoryListCountsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.db.close)
        self.sessions = sqlite3.connect(':memory:')
        self.addCleanup(self.sessions.close)
        self.sessions.execute('CREATE TABLE sessions(body TEXT)')
        self.app = SimpleNamespace(
            store=self.store, config={}, usage=SimpleNamespace(snapshot=lambda: {}),
            consultations=SimpleNamespace(db=self.sessions, lock=threading.RLock()))
        for name in ('first', 'second', 'empty'):
            self.store.put('job', {'id': name, 'title': name, 'status': 'accepted', 'project': 'synthetic'})
        for name, job in [('one', 'first'), ('two', 'first'), ('three', 'second')]:
            self.store.put('task', {'id': name, 'job_id': job, 'status': 'succeeded', 'role': 'builder',
                                    'attempt': 1, 'summary': 'Synthetic completed work'})

    def test_list_counts_tasks_without_decoding_all_task_rows(self):
        original = self.store.all
        def read(kind):
            self.assertNotEqual(kind, 'task', 'The list must not load every task body')
            return original(kind)
        with patch.object(self.store, 'all', side_effect=read):
            value = snapshot(self.app)
        self.assertEqual({j['id']: j['task_count'] for j in value['jobs']},
                         {'first': 2, 'second': 1, 'empty': 0})
        self.assertEqual(value['tasks'], [])
        self.assertEqual(value['events'], [])

    def test_detail_keeps_task_records_and_consumption(self):
        value = snapshot(self.app, 'first')
        self.assertEqual({task['id'] for task in value['tasks']}, {'one', 'two'})
        self.assertTrue(all('consumption' in task for task in value['tasks']))
        self.assertNotIn('three', [task['id'] for task in value['tasks']])


if __name__ == '__main__':
    unittest.main()
