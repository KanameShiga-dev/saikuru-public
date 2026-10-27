"""Fault injection across restart, confirmation, Checkpoint, retry and stale results.

Fictional repositories, Store/Handoff DBs and child processes only. No App,
operational data, Provider, network or native worker is used. A "restart" is a
new Store connection plus a new Engine, whose constructor runs Store.recover().
Every scenario checks that the worker's edit stays in the project and that each
task has at most one live attempt with unique, increasing ordinals.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from team_config import default_config
from team_engine import Context, Engine
from team_p0_contract import initial, initial_workflow
from team_store import Store, StaleAttempt

SOURCE_ROOT = Path(__file__).resolve().parents[2]
WORKER_EDIT = 'value = 2\n'

# Child process: open the same fictional runtime, finalize one draft and die
# abruptly at the chosen boundary. os._exit skips cleanup like a killed process.
CRASH_PROGRAM = r'''
import os, sys
from team_config import default_config
from team_engine import Engine
from team_store import Store
runtime, project, checkpoint_id, boundary = sys.argv[1:5]
store = Store(runtime)
config = default_config()
config['approved_roots'] = [project]
engine = Engine(store, config, {}, 'http://127.0.0.1:0')
engine.paused = True
if boundary == 'store_transaction':
    original = store.event
    def event(kind, *args, **kwargs):
        if kind == 'checkpoint_ready':
            os._exit(31)
        return original(kind, *args, **kwargs)
    store.event = event
else:
    original = engine.handoff.checkpoint_receipt
    def receipt(*args, write=False, **kwargs):
        if write:
            os._exit(32)
        return original(*args, write=write, **kwargs)
    engine.handoff.checkpoint_receipt = receipt
engine.finalize_checkpoint(checkpoint_id, writer_stopped=True)
os._exit(0)
'''


class RecoveryFaultInjectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='fictional-continuity-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        assert self.root.name.startswith('fictional-continuity-')
        self.project = self.root / 'project'
        self.project.mkdir()
        self.runtime = self.root / 'runtime'
        self.app = self.project / 'app.py'
        self.app.write_text('value = 1\n', encoding='utf-8', newline='\n')
        self.git('init', '-q')
        self.git('add', '--', 'app.py')
        self.git('-c', 'user.name=Fictional Fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '-qm', 'Fictional baseline')
        self.config = default_config()
        self.config['approved_roots'] = [str(self.project)]
        self.store, self.engine = self.open_runtime()
        job = self.store.put('job', initial_workflow(dict(id='fictional-job', title='Fictional lookup',
            goal='Fictional lookup', goal_contract=initial('Fictional lookup'), project=str(self.project),
            status='awaiting_approval', request_origin='new', route='full', document_source=None,
            p0_contract_version=1, auto_execute=False, created_at=1), enabled=True))
        task = self.engine.new_task(job, 'Fictional build', 'Fictional only', 'builder')
        approval = self.engine.new_approval(task, 'plan', {'summary': 'Fictional plan', 'checkpoint_paths': ['app.py']})
        self.store.update(approval['id'], status='approved')
        self.job_id = job['id']
        self.task_id = task['id']
        self.plan_id = approval['id']
        self.store.update(job['id'], status='running', plan_revision=1, requirement_revision=1,
                          plan_approval_id=approval['id'], requirements_approval_id=approval['id'])
        self.first = self.store.reserve_attempt(task['id'], task['state_revision'])
        # The interrupted worker's uncommitted edit; it must survive every fault.
        self.app.write_text(WORKER_EDIT, encoding='utf-8', newline='\n')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.project), *args], stderr=subprocess.DEVNULL)

    def open_runtime(self):
        store = Store(self.runtime)
        self.addCleanup(store.db.close)
        engine = Engine(store, self.config, {}, 'http://127.0.0.1:0')
        engine.paused = True
        self.addCleanup(engine.handoff.close)
        return store, engine

    def restart(self):
        """New connection and Engine; the old objects stay usable as a late writer."""
        self.store, self.engine = self.open_runtime()

    def old_context(self, task):
        # Callback fields only; no Context preparation, Provider or credential access.
        ctx = Context.__new__(Context)
        ctx.engine, ctx.task, ctx.token = self.engine, task, 'fictional-worker-token'
        ctx.agent_run = {'id': 'fictional-run', 'provider': 'codex'}
        ctx._partial_usage = {}
        return ctx

    def task(self):
        return self.store.get(self.task_id)

    def confirm(self):
        task, job = self.task(), self.store.get(self.job_id)
        return self.engine.confirm_recovery(task['id'], task['state_revision'], job['state_revision'],
                                            task['current_attempt']['id'], writer_stopped=True, checked_changes=True)

    def draft(self):
        return self.engine.collect_checkpoint(self.task_id, writer_stopped=True)

    def publish(self):
        result = self.engine.finalize_checkpoint(self.draft()['id'], writer_stopped=True)
        self.assertTrue(result['handoff_synced'])
        return self.task()['resume_checkpoint_id']

    def stopped_with_checkpoint(self):
        self.restart()
        self.confirm()
        return self.publish()

    def crash_child(self, checkpoint_id, boundary):
        return subprocess.run([sys.executable, '-B', '-c', CRASH_PROGRAM, str(self.runtime), str(self.project),
                               checkpoint_id, boundary], cwd=SOURCE_ROOT, timeout=60, capture_output=True)

    def assert_edit_preserved(self, expected=WORKER_EDIT):
        self.assertEqual(self.app.read_text(encoding='utf-8'), expected)

    def assert_unique_attempts(self, expected_count):
        starts = [e for e in self.store.job_events(self.job_id, 'attempt_started') if e['task_id'] == self.task_id]
        ids = [e['details']['attempt_id'] for e in starts]
        ordinals = [e['details'].get('ordinal') for e in starts]
        self.assertEqual(len(starts), expected_count)
        self.assertEqual(len(set(ids)), len(ids))
        self.assertEqual(ordinals, sorted(set(ordinals)))
        task = self.task()
        self.assertEqual(task['current_attempt']['id'], ids[-1])
        running = [t for t in self.store.for_job('task', self.job_id)
                   if (t.get('current_attempt') or {}).get('status') == 'running']
        self.assertLessEqual(len(running), 1)

    def assert_not_resumable(self, reason):
        result = self.engine.assess_checkpoint_resume(self.task_id, writer_stopped=True)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn(reason, result['reasons'])
        self.assertEqual(result['candidate_task_ids'], [])
        self.assertFalse(result['resume_allowed'])
        with self.assertRaises(ValueError):
            self.engine.retry(self.task_id, 'Fictional changes checked')
        self.assertEqual(self.task()['status'], 'interrupted')
        return result

    # 1. Abrupt exit while saving -------------------------------------------------

    def test_abrupt_exit_inside_store_publication_leaves_no_half_checkpoint(self):
        self.restart()
        self.confirm()
        draft = self.draft()
        result = self.crash_child(draft['id'], 'store_transaction')
        self.assertEqual(result.returncode, 31, result.stderr[-400:])
        self.restart()
        self.assertEqual(self.store.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(self.store.get(draft['id'])['state'], 'writing')
        self.assertIsNone(self.task().get('resume_checkpoint_id'))
        self.assertFalse(self.store.job_events(self.job_id, 'checkpoint_ready'))
        self.assert_not_resumable('checkpoint_missing')
        self.assert_edit_preserved()
        # The untouched draft can still be published once the fault is gone.
        self.assertTrue(self.engine.finalize_checkpoint(draft['id'], writer_stopped=True)['handoff_synced'])
        self.assertEqual(self.task()['resume_checkpoint_id'], draft['id'])
        self.assert_unique_attempts(1)

    def test_abrupt_exit_between_store_commit_and_handoff_receipt_is_detected_and_resynced(self):
        self.restart()
        self.confirm()
        draft = self.draft()
        result = self.crash_child(draft['id'], 'handoff_receipt')
        self.assertEqual(result.returncode, 32, result.stderr[-400:])
        self.restart()
        self.assertEqual(self.task()['resume_checkpoint_id'], draft['id'])
        self.assertEqual(self.engine.checkpoint_persistence(draft['id'])['reasons'], ['handoff_sync_required'])
        self.assert_not_resumable('handoff_sync_required')
        self.assertTrue(self.engine.resync_checkpoint(draft['id'])['handoff_synced'])
        self.assertEqual(self.engine.assess_checkpoint_resume(self.task_id, writer_stopped=True)['status'], 'candidate')
        self.assert_edit_preserved()
        self.assert_unique_attempts(1)

    # 2. Derived (Handoff) DB unavailable -----------------------------------------

    def test_handoff_outage_keeps_store_durable_and_blocks_retry_until_resync(self):
        self.restart()
        self.confirm()
        draft = self.draft()
        def unavailable(*args, **kwargs):
            raise sqlite3.OperationalError('fictional derived database is locked')
        with patch.object(self.engine.handoff, 'checkpoint_receipt', side_effect=unavailable):
            result = self.engine.finalize_checkpoint(draft['id'], writer_stopped=True)
        self.assertEqual((result['store_ready'], result['handoff_synced']), (True, False))
        self.assertEqual(self.store.get(draft['id'])['state'], 'ready')
        self.assertEqual(len(self.store.job_events(self.job_id, 'checkpoint_handoff_pending')), 1)
        self.assert_not_resumable('handoff_sync_required')
        self.assertTrue(self.engine.resync_checkpoint(draft['id'])['handoff_synced'])
        self.engine.retry(self.task_id, 'Fictional changes checked')
        self.assertEqual(self.task()['status'], 'queued')
        self.assertEqual(len(self.store.job_events(self.job_id, 'retry')), 1)
        self.assert_edit_preserved()

    # 3. Corrupted references ------------------------------------------------------

    def collection_dir(self, checkpoint_id):
        return self.runtime / 'checkpoints' / checkpoint_id

    def test_tampered_blob_blocks_resume_without_restoring_or_repairing(self):
        checkpoint_id = self.stopped_with_checkpoint()
        blob = next((self.collection_dir(checkpoint_id) / 'blobs').iterdir())
        blob.write_bytes(b'value = 666\n')
        self.assert_not_resumable('checkpoint_files_or_scope_invalid')
        self.assertEqual(blob.read_bytes(), b'value = 666\n')
        self.assert_edit_preserved()
        self.assert_unique_attempts(1)

    def test_missing_manifest_blocks_resume(self):
        checkpoint_id = self.stopped_with_checkpoint()
        (self.collection_dir(checkpoint_id) / 'manifest.json').unlink()
        self.assert_not_resumable('checkpoint_files_or_scope_invalid')
        self.assert_edit_preserved()
        self.assert_unique_attempts(1)

    def test_deleted_checkpoint_record_behind_pointer_blocks_without_fallback(self):
        checkpoint_id = self.stopped_with_checkpoint()
        with self.store.atomic():
            self.store.db.execute('DELETE FROM objects WHERE id=?', (checkpoint_id,))
        self.restart()
        self.assertEqual(self.engine.checkpoint_persistence(checkpoint_id)['reasons'], ['checkpoint_binding_invalid'])
        self.assert_not_resumable('checkpoint_or_authority_invalid')
        self.assert_edit_preserved()
        self.assert_unique_attempts(1)

    # 4. External change after the Checkpoint ---------------------------------------

    def test_external_edit_after_checkpoint_is_kept_and_blocks_retry(self):
        self.stopped_with_checkpoint()
        self.app.write_text('value = 3\n', encoding='utf-8', newline='\n')
        result = self.assert_not_resumable('workspace_changed')
        self.assertEqual(result['changed_paths'], ['app.py'])
        self.assert_edit_preserved('value = 3\n')
        self.assert_unique_attempts(1)

    def test_external_commit_after_checkpoint_blocks_retry(self):
        self.stopped_with_checkpoint()
        self.git('add', '--', 'app.py')
        self.git('-c', 'user.name=Fictional Fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '-qm', 'Fictional external commit')
        self.assert_not_resumable('git_head_changed')
        self.assert_edit_preserved()
        self.assert_unique_attempts(1)

    # 5. Approval expiry -------------------------------------------------------------

    def test_restart_expires_pending_tool_approval_and_late_decision_cannot_revive_it(self):
        self.store.update(self.task_id, status='awaiting_approval')
        approval = self.engine.new_approval(self.task(), 'tool', {'summary': 'Fictional tool'})
        self.restart()
        self.assertEqual(self.store.get(approval['id'])['status'], 'expired')
        with self.assertRaises(ValueError):
            self.engine.decide(approval['id'], True, 'Fictional late approval')
        self.assertEqual(self.store.get(approval['id'])['status'], 'expired')
        self.assertEqual(self.task()['status'], 'interrupted')
        self.assertTrue(self.task()['recovery_check_required'])
        self.assert_edit_preserved()
        self.assert_unique_attempts(1)

    def test_plan_expired_after_checkpoint_refuses_retry(self):
        self.stopped_with_checkpoint()
        self.store.update(self.plan_id, status='expired')
        self.assert_not_resumable('approval_or_authority_changed')
        self.assert_edit_preserved()
        self.assert_unique_attempts(1)

    # 6. Double retry ----------------------------------------------------------------

    def test_concurrent_retries_from_two_runtimes_queue_once_and_reserve_one_attempt(self):
        self.stopped_with_checkpoint()
        other_store, other_engine = self.open_runtime()
        barrier = threading.Barrier(2)

        def retry(engine):
            barrier.wait(timeout=10)
            try:
                engine.retry(self.task_id, 'Fictional changes checked')
                return 'queued'
            except ValueError:
                return 'refused'

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(retry, (self.engine, other_engine)))
        self.assertCountEqual(results, ['queued', 'refused'])
        self.assertEqual(len(self.store.job_events(self.job_id, 'retry')), 1)
        with self.assertRaises(ValueError):
            self.engine.retry(self.task_id, 'Fictional duplicate')
        revision = self.task()['state_revision']
        barrier = threading.Barrier(2)

        def reserve(store):
            barrier.wait(timeout=10)
            try:
                store.reserve_attempt(self.task_id, revision)
                return 'reserved'
            except StaleAttempt:
                return 'refused'

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, (self.store, other_store)))
        self.assertCountEqual(results, ['reserved', 'refused'])
        self.assertEqual(self.task()['current_attempt']['ordinal'], self.first['current_attempt']['ordinal'] + 1)
        self.assert_edit_preserved()
        self.assert_unique_attempts(2)

    # 7. Old result arrives late -------------------------------------------------------

    def test_old_result_while_stopped_cannot_finish_or_clear_the_fence(self):
        old = self.old_context(self.first)
        self.restart()
        before = self.task()
        for call in (lambda: old.record_usage({'inputTokens': 999}),
                     lambda: old.command_result('fictional-command', 0, 'fictional output'),
                     lambda: self.engine._finish(self.first, {'status': 'done', 'summary': 'stale'})):
            with self.assertRaises(StaleAttempt):
                call()
        self.assertEqual(self.task(), before)
        self.assertTrue(before['recovery_check_required'])
        self.assert_edit_preserved()
        self.assert_unique_attempts(1)

    def test_old_result_after_new_attempt_cannot_touch_new_generation(self):
        old = self.old_context(self.first)
        self.stopped_with_checkpoint()
        self.engine.retry(self.task_id, 'Fictional changes checked')
        task = self.task()
        new = self.store.reserve_attempt(task['id'], task['state_revision'])
        before, events = self.task(), self.store.events()
        for call in (lambda: old.record_usage({'inputTokens': 999}),
                     lambda: old.command_result('fictional-command', 0, 'fictional output'),
                     lambda: old.approve({'operation': 'fictional'}),
                     lambda: self.engine._finish(self.first, {'status': 'done', 'summary': 'stale'})):
            with self.assertRaises(StaleAttempt):
                call()
        self.assertEqual(self.task(), before)
        self.assertEqual(self.store.events(), events)
        self.assertNotEqual(new['current_attempt']['id'], self.first['current_attempt']['id'])
        self.assert_edit_preserved()
        self.assert_unique_attempts(2)


if __name__ == '__main__':
    unittest.main()
