"""Real local engine/storage boundaries; no provider or native CLI is started."""
import shutil
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from eval.development_readiness.reacceptance_fixture import make_fixture, close


class RecoveryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / 'source'
        self.server = make_fixture(self.base)
        self.addCleanup(lambda: close(self.server))
        self.bind()
        self.task_id = self.store.all('task')[0]['id']
        self.approval_id = self.store.all('approval')[0]['id']

    def bind(self):
        self.engine = self.server.app.engine
        self.store = self.server.app.store

    def reopen(self):
        close(self.server)
        self.server = make_fixture(self.base)
        self.bind()

    def test_cancel_survives_restart_and_scheduler_does_not_launch(self):
        artifact = self.base / 'project' / 'synthetic.txt'
        artifact.write_text('synthetic edit', encoding='utf-8')
        self.store.update(self.task_id, status='running')
        self.store.update('synthetic-reacceptance', status='running')
        cancel = threading.Event()
        self.engine.active[self.task_id] = SimpleNamespace(cancel=cancel)
        self.engine.cancel_job('synthetic-reacceptance')
        self.assertTrue(cancel.is_set())
        self.engine.active.clear()
        self.reopen()
        launched = threading.Event()
        self.engine._run = lambda task: launched.set()
        self.engine.paused = False
        for _ in range(3): self.engine.tick()
        self.assertFalse(launched.is_set())
        for ident in ('synthetic-reacceptance', self.task_id, self.approval_id):
            self.assertEqual(self.store.get(ident)['status'], 'cancelled')
        self.assertEqual(artifact.read_text(encoding='utf-8'), 'synthetic edit')
        with self.assertRaises(ValueError): self.engine.decide(self.approval_id, True, '')

    def test_restart_requires_retry_and_dispatches_once(self):
        self.store.update(self.task_id, status='awaiting_approval')
        self.store.update('synthetic-reacceptance', status='running')
        self.store.update(self.approval_id, kind='tool')
        self.reopen()
        self.assertEqual(self.store.get(self.task_id)['status'], 'interrupted')
        self.assertEqual(self.store.get(self.approval_id)['status'], 'expired')
        launches = []
        launched = threading.Event()
        def synthetic_worker(task):
            launches.append(task['id'])
            launched.set()
        self.engine._run = synthetic_worker
        self.engine.paused = False
        self.engine.tick()
        self.assertEqual(launches, [])
        self.engine.retry(self.task_id, 'Synthetic changes inspected')
        with self.assertRaises(ValueError): self.engine.retry(self.task_id, 'duplicate')
        self.engine.tick()
        self.assertTrue(launched.wait(2))
        self.engine.tick()
        self.assertEqual(launches, [self.task_id])
        self.assertEqual(self.store.get(self.approval_id)['status'], 'expired')

    def test_restore_both_databases_and_preserve_acceptance(self):
        self.engine.decide(self.approval_id, True, '')
        new_id = self.engine.record_completion_evidence(self.approval_id, 0, 0, 'Synthetic observation', True)['approval_id']
        self.engine.decide(new_id, True, '')
        primary = self.base / 'runtime' / self.store.backup()
        handoff = self.base / 'runtime' / ('handoff-' + primary.name)
        self.engine.handoff.backup(handoff)
        target = Path(self.temp.name) / 'restored'
        (target / 'runtime').mkdir(parents=True)
        shutil.copy2(primary, target / 'runtime' / 'team.sqlite3')
        shutil.copy2(handoff, target / 'runtime' / 'handoff.sqlite3')
        restored = make_fixture(target)
        try:
            for name in ('team.sqlite3', 'handoff.sqlite3'):
                with closing(sqlite3.connect((target/'runtime'/name).as_uri()+'?mode=ro', uri=True)) as db:
                    self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                    original = self.store.db if name == 'team.sqlite3' else self.engine.handoff.db
                    for table in tables:
                        quoted = '"' + table.replace('"', '""') + '"'
                        self.assertEqual(db.execute('SELECT * FROM '+quoted).fetchall(), original.execute('SELECT * FROM '+quoted).fetchall())
            self.assertEqual(restored.app.store.get('synthetic-reacceptance')['status'], 'accepted')
            self.assertEqual(len(restored.app.store.all('approval')), 2)
            restored.app.store.update('synthetic-reacceptance', title='Synthetic restored copy')
            self.assertNotEqual(restored.app.store.get('synthetic-reacceptance')['title'], self.store.get('synthetic-reacceptance')['title'])
        finally:
            close(restored)
        # On Windows this also verifies that backup destinations are no longer held open.
        primary.rename(primary.with_suffix('.checked'))
        handoff.rename(handoff.with_suffix('.checked'))


if __name__ == '__main__': unittest.main()
