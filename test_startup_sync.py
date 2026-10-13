"""Startup re-syncs every job's handoff records: per-job reads and no-op records must stay cheap and correct."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from team_handoff import HandoffDB
from team_store import Store


class StartupSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root)
        self.handoff = HandoffDB(self.root)

    def tearDown(self):
        self.handoff.close()
        self.store.db.close()
        self.tmp.cleanup()

    def test_for_job_returns_only_that_jobs_objects_in_order(self):
        for n, job in enumerate(['j1', 'j2', 'j1', 'j2', 'j1']):
            self.store.put('task', {'id': f't{n}', 'job_id': job, 'status': 'queued'})
        self.store.put('approval', {'id': 'a1', 'job_id': 'j2', 'kind': 'plan', 'status': 'approved'})
        self.assertEqual([t['id'] for t in self.store.for_job('task', 'j1')], ['t0', 't2', 't4'])
        self.assertEqual([a['id'] for a in self.store.for_job('approval', 'j2')], ['a1'])
        self.assertEqual(self.store.for_job('approval', 'j1'), [])
        expected = [t for t in self.store.all('task') if t['job_id'] == 'j2']
        self.assertEqual(self.store.for_job('task', 'j2'), expected)

    def test_repeated_record_does_not_commit_and_keeps_the_first(self):
        project = str(self.root / 'project')
        self.handoff.record(project, 'j1', None, 'goal', 'goal:j1', 1.0, {'title': 'first'})
        real = self.handoff.db
        spy = mock.MagicMock(wraps=real)
        self.handoff.db = spy
        try:
            self.handoff.record(project, 'j1', None, 'goal', 'goal:j1', 2.0, {'title': 'second'})
            spy.commit.assert_not_called()
            self.assertFalse(real.in_transaction)
            self.handoff.record(project, 'j1', None, 'goal', 'goal:j1b', 3.0, {'title': 'new'})
            spy.commit.assert_called_once()
        finally:
            self.handoff.db = real
        rows = real.execute("SELECT source_id, payload FROM records ORDER BY seq").fetchall()
        self.assertEqual([r[0] for r in rows], ['goal:j1', 'goal:j1b'])
        self.assertIn('first', rows[0][1])
        # Another connection sees the committed rows (nothing left pending on ours).
        import sqlite3
        other = sqlite3.connect(self.root / 'handoff.sqlite3')
        try:
            self.assertEqual(other.execute('SELECT count(*) FROM records').fetchone()[0], 2)
        finally:
            other.close()

    def test_sync_twice_is_idempotent(self):
        job = {'id': 'j1', 'project': str(self.root / 'project'), 'title': 'T', 'goal': 'G', 'created_at': 1.0}
        task = {'id': 't1', 'job_id': 'j1', 'title': 'impl', 'role': 'implementer', 'profile': 'claude:sonnet',
                'instruction': 'do it', 'result': {'status': 'done', 'summary': 'ok'}, 'finished_at': 2.0}
        self.handoff.sync(job, [task], [])
        count = self.handoff.db.execute('SELECT count(*) FROM records').fetchone()[0]
        self.handoff.sync(job, [task], [])
        self.assertEqual(self.handoff.db.execute('SELECT count(*) FROM records').fetchone()[0], count)
        self.assertGreaterEqual(count, 3)


if __name__ == '__main__':
    unittest.main()
