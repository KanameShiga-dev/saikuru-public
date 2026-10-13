"""Synthetic-only checks; no App startup, provider discovery, or external calls."""
import importlib.util
import json
import sqlite3
import tempfile
import threading
import unittest
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from team_store import Store
from team_engine import Engine
from team_evaluation import record_effort, snapshot


class EffortTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.db.close)
        self.store.put('job', {'id': 'j', 'status': 'accepted_with_pending_checks'})

    def body(self, **changes):
        return dict({'job_id': 'j', 'revision': 0, 'minutes': 12, 'observer': 'real_user', 'confirmed': True}, **changes)

    def test_unknown_is_not_zero_and_record_does_not_accept_or_refresh_job(self):
        self.assertIsNone(snapshot(self.store, 'j')['record'])
        before = self.store.get('j')
        result = record_effort(self.store, self.body())
        self.assertEqual(result['record']['minutes'], 12)
        self.assertFalse(result['final_acceptance'])
        self.assertEqual(self.store.get('j'), before)

    def test_correction_history_and_stale_write(self):
        record_effort(self.store, self.body())
        with self.assertRaises(ValueError):
            record_effort(self.store, self.body())
        result = record_effort(self.store, self.body(revision=1, minutes=0))
        self.assertEqual(result['record']['history'][0]['minutes'], 12)
        self.assertEqual(result['record']['minutes'], 0)

    def test_reject_unknown_fields_unconfirmed_invalid_minutes(self):
        for changes in ({'minutes': True}, {'minutes': -1}, {'minutes': 1.5}, {'minutes': 60001},
                        {'observer': 'ai'}, {'confirmed': False}, {'revision': True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                record_effort(self.store, self.body(**changes))
        with self.assertRaises(ValueError):
            record_effort(self.store, dict(self.body(), note='ordinary memo'))

    def test_counts_keep_review_rework_and_decisions_separate(self):
        self.store.put('task', {'id': 't', 'job_id': 'j', 'status': 'succeeded', 'role': 'reviewer',
                               'attempt': 2, 'result': {'status': 'done'},
                               'review_result_history': [{'result': {'status': 'needs_changes'}}]})
        for n, status in enumerate(('approved', 'denied', 'pending')):
            self.store.put('approval', {'id': f'a{n}', 'job_id': 'j', 'status': status})
        result = snapshot(self.store, 'j')
        self.assertEqual([result[k] for k in ('approved_decisions', 'denied_decisions',
                                             'pending_decisions', 'review_needs_changes_reports', 'task_retry_count')], [1]*5)
        self.assertEqual(result['user_rework_count'], 0)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.db.close)
        self.store.put('job', {'id': 'j', 'status': 'running'})
        self.store.put('task', {'id': 't', 'job_id': 'j', 'status': 'awaiting_approval', 'role': 'builder',
                               'instruction': 'synthetic task', 'after': None, 'attempt': 1})
        self.store.put('approval', {'id': 'a', 'job_id': 'j', 'task_id': 't', 'status': 'pending', 'kind': 'tool'})

    def test_restart_expires_native_request_and_retry_is_explicit(self):
        self.store.recover()
        self.assertEqual(self.store.get('t')['status'], 'interrupted')
        self.assertEqual(self.store.get('a')['status'], 'expired')
        engine = SimpleNamespace(store=self.store, active={}, sync_handoff=lambda job: None)
        Engine.retry(engine, 't', 'Synthetic changes inspected')
        self.assertEqual(self.store.get('t')['status'], 'queued')
        self.assertEqual(self.store.get('a')['status'], 'expired')

    def test_cancel_signals_worker_preserves_file_and_expires_pending_work(self):
        artifact = Path(self.temp.name) / 'sample.txt'
        artifact.write_text('synthetic edit', encoding='utf-8')
        cancel = threading.Event()
        engine = SimpleNamespace(store=self.store, active={'t': SimpleNamespace(cancel=cancel)})
        Engine.cancel_job(engine, 'j')
        self.assertTrue(cancel.is_set())
        for ident in ('j', 't', 'a'):
            self.assertEqual(self.store.get(ident)['status'], 'cancelled')
        self.assertEqual(artifact.read_text(encoding='utf-8'), 'synthetic edit')

    def test_sqlite_backup_is_readable_independent_and_integral(self):
        backup = self.store.backup()
        self.store.update('j', status='cancelled')
        with closing(sqlite3.connect(Path(self.temp.name) / backup)) as restored:
            self.assertEqual(restored.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            row = restored.execute("SELECT body FROM objects WHERE id='j'").fetchone()[0]
            self.assertEqual(json.loads(row)['status'], 'running')

    def test_abrupt_process_exit_rolls_back_and_recovery_does_not_resume(self):
        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            program = ('import os,sys\nfrom team_store import Store\n'
                       's=Store(sys.argv[1])\n'
                       "s.put('job', {'id':'crash-job','status':'running'})\n"
                       "s.put('task', {'id':'crash-task','job_id':'crash-job','status':'running'})\n"
                       'with s.atomic():\n'
                       " s.update('crash-task',status='succeeded')\n"
                       ' os._exit(23)\n')
            result = subprocess.run([sys.executable, '-B', '-c', program, tmp], cwd=project_root, timeout=10,
                                    capture_output=True)
            self.assertEqual(result.returncode, 23)
            restored = Store(tmp)
            try:
                self.assertEqual(restored.get('crash-task')['status'], 'running')
                restored.recover()
                restored.recover()
                self.assertEqual(restored.get('crash-task')['status'], 'interrupted')
                self.assertEqual(restored.get('crash-job')['status'], 'interrupted')
                self.assertEqual(restored.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            finally:
                restored.db.close()


class ReviewScoringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / 'eval/development_readiness/evaluate.py'
        spec = importlib.util.spec_from_file_location('readiness_evaluate', path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_misses_false_positives_duplicates_and_empty_review(self):
        value = {'case_id': 'synthetic-stock-v1', 'completed': True, 'adjudicator': 'human',
                 'findings': [{'finding_id': 1, 'defect_id': 'D1'}, {'finding_id': 2, 'defect_id': 'D1'},
                              {'finding_id': 3, 'defect_id': None}]}
        result = self.module.score(value)
        self.assertEqual(result['false_negatives'], ['D2', 'D3'])
        self.assertEqual(result['false_positives'], 1)
        self.assertEqual(result['duplicate_findings'], 1)
        self.assertEqual(result['precision'], .5)
        value['findings'] = []
        self.assertEqual(self.module.score(value)['recall'], 0)
        self.assertIsNone(self.module.score(value)['precision'])
        value['completed'] = False
        with self.assertRaises(ValueError): self.module.score(value)

    def test_prepare_isolated_and_does_not_expose_answers_or_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'review'
            self.module.prepare(path)
            self.assertEqual({p.name for p in path.iterdir()}, {'inventory.py', 'REQUEST.md'})
            with self.assertRaises(FileExistsError): self.module.prepare(path)
