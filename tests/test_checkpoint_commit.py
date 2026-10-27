"""Fictional SQLite/Git fault boundaries; never production data or Providers."""
import copy
import json
import sqlite3
import threading
import unittest
from unittest.mock import patch

import test_checkpoint_collection as fixtures
import team_checkpoint as checkpoint
from team_store import Store


class CheckpointCommitTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.EngineDraftTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        for name in ('engine', 'store', 'task', 'job', 'root', 'project', 'app', 'cache'):
            setattr(self, name, getattr(self.fixture, name))

    def collect(self):
        return self.engine.collect_checkpoint(self.task['id'], writer_stopped=True)

    def publish(self, draft):
        return self.engine.finalize_checkpoint(draft['id'], writer_stopped=True)

    def test_ready_pointer_event_and_post_pointer_revision_are_one_binding(self):
        draft = self.collect()
        result = self.publish(draft)
        task = self.store.get(self.task['id'])
        ready, event = self.store.checkpoint_binding(draft['id'])
        self.assertTrue(result['store_ready'])
        self.assertTrue(result['handoff_synced'])
        self.assertFalse(result['resume_allowed'])
        self.assertEqual(task['resume_checkpoint_id'], ready['id'])
        self.assertEqual(ready['snapshot_revisions']['task'] + 1, ready['ready_binding']['task'])
        self.assertEqual(ready['ready_binding']['task'], task['state_revision'])
        self.assertEqual(event['details']['manifest_digest'], draft['manifest_digest'])
        self.assertEqual(ready['resume_preconditions'], ['checkpoint_not_ready'])  # immutable observation
        self.assertTrue(self.engine.checkpoint_persistence(ready['id'])['handoff_synced'])

    def test_generic_store_writes_cannot_publish_or_repoint_or_clear(self):
        draft = self.collect()
        with self.assertRaises(ValueError):
            self.store.update(self.task['id'], resume_checkpoint_id=draft['id'])
        with self.assertRaises(ValueError):
            self.store.update(draft['id'], state='ready')
        self.publish(draft)
        with self.assertRaises(ValueError):
            self.store.update(self.task['id'], resume_checkpoint_id=None)
        with self.assertRaises(ValueError):
            self.store.update(draft['id'], state='invalid')
        self.assertTrue(self.engine.checkpoint_persistence(draft['id'])['store_ready'])

    def test_failure_before_ready_event_rolls_back_pointer_and_all_store_changes(self):
        draft = self.collect()
        original = self.store.event
        def fail(kind, *args, **kwargs):
            if kind == 'checkpoint_ready':
                raise sqlite3.OperationalError('fictional event fault')
            return original(kind, *args, **kwargs)
        with patch.object(self.store, 'event', side_effect=fail), self.assertRaises(sqlite3.Error):
            self.publish(draft)
        self.assertEqual(self.store.get(draft['id'])['state'], 'writing')
        self.assertNotIn('resume_checkpoint_id', self.store.get(self.task['id']))
        self.assertFalse(self.store.job_events(self.job['id'], 'checkpoint_ready'))
        self.assertFalse(self.engine.checkpoint_persistence(draft['id'])['store_ready'])
        self.assertTrue(self.publish(draft)['handoff_synced'])

    def test_failure_at_ready_row_rolls_back_already_written_pointer_and_event(self):
        draft = self.collect()
        original = self.store.put
        def fail(kind, obj, *args, **kwargs):
            if kind == 'checkpoint' and obj.get('state') == 'ready':
                raise sqlite3.OperationalError('fictional row fault')
            return original(kind, obj, *args, **kwargs)
        with patch.object(self.store, 'put', side_effect=fail), self.assertRaises(sqlite3.Error):
            self.publish(draft)
        self.assertEqual(self.store.get(draft['id'])['state'], 'writing')
        self.assertNotIn('resume_checkpoint_id', self.store.get(self.task['id']))
        self.assertFalse(self.store.job_events(self.job['id'], 'checkpoint_ready'))

    def test_sqlite_abort_preserves_previous_ready_pointer_across_reopen(self):
        first = self.collect()
        self.publish(first)
        second = self.collect()
        self.store.db.executescript("""CREATE TRIGGER fictional_ready_abort BEFORE UPDATE ON objects
            WHEN NEW.kind='checkpoint' AND json_extract(NEW.body,'$.state')='ready'
            BEGIN SELECT RAISE(ABORT,'fictional publication fault'); END;""")
        with self.assertRaises(sqlite3.Error):
            self.publish(second)
        reopened = Store(self.root / 'runtime')
        self.addCleanup(reopened.db.close)
        self.assertEqual(reopened.get(self.task['id'])['resume_checkpoint_id'], first['id'])
        self.assertEqual(reopened.get(second['id'])['state'], 'writing')
        self.assertEqual(len(reopened.job_events(self.job['id'], 'checkpoint_ready')), 1)

    def test_other_connection_never_observes_half_publication(self):
        draft = self.collect()
        observer = Store(self.root / 'runtime')
        self.addCleanup(observer.db.close)
        original = self.store.put
        observations = []
        def observe(kind, obj, *args, **kwargs):
            if kind == 'checkpoint' and obj.get('state') == 'ready':
                observations.append((observer.get(draft['id'])['state'],
                                     observer.get(self.task['id']).get('resume_checkpoint_id')))
            return original(kind, obj, *args, **kwargs)
        with patch.object(self.store, 'put', side_effect=observe):
            self.publish(draft)
        self.assertEqual(observations, [('writing', None)])
        self.assertEqual(observer.get(draft['id'])['state'], 'ready')
        self.assertEqual(observer.get(self.task['id'])['resume_checkpoint_id'], draft['id'])

    def test_manifest_write_failure_has_no_ready_or_pointer(self):
        original = checkpoint._atomic_write
        def fail(path, data):
            if path.name == 'manifest.json':
                raise OSError('fictional manifest replace fault')
            return original(path, data)
        with patch.object(checkpoint, '_atomic_write', side_effect=fail):
            with self.assertRaises(OSError):
                self.collect()
        self.assertFalse(self.store.for_job('checkpoint', self.job['id']))
        self.assertNotIn('resume_checkpoint_id', self.store.get(self.task['id']))

    def test_interruption_after_store_commit_leaves_detectable_pending_projection(self):
        draft = self.collect()
        with patch.object(self.engine, 'resync_checkpoint', side_effect=SystemExit('fictional interruption')):
            with self.assertRaises(SystemExit):
                self.publish(draft)
        reopened = Store(self.root / 'runtime')
        self.addCleanup(reopened.db.close)
        ready, event = reopened.checkpoint_binding(draft['id'])
        self.assertEqual(ready['state'], 'ready')
        self.assertFalse(self.engine.checkpoint_persistence(draft['id'])['handoff_synced'])
        self.assertTrue(self.engine.resync_checkpoint(draft['id'])['handoff_synced'])

    def test_failure_after_handoff_commit_does_not_hide_already_written_receipt(self):
        draft = self.collect()
        original = self.store.event
        def fail(kind, *args, **kwargs):
            if kind == 'checkpoint_handoff_synced':
                raise sqlite3.OperationalError('fictional Store receipt-event fault')
            return original(kind, *args, **kwargs)
        with patch.object(self.store, 'event', side_effect=fail), self.assertRaises(sqlite3.Error):
            self.publish(draft)
        self.assertTrue(self.engine.checkpoint_persistence(draft['id'])['handoff_synced'])
        self.assertTrue(self.engine.resync_checkpoint(draft['id'])['handoff_synced'])

    def test_handoff_failure_keeps_durable_ready_and_resync_preserves_independent_state(self):
        handoff = self.engine.handoff
        handoff.fact(str(self.project), self.job['id'], 'fictional_key', 'fictional_value',
                     'fictional_evidence', 'user_confirmed', 'fictional-source')
        with handoff.lock:
            handoff.db.execute('INSERT INTO skill_active VALUES (?,?,?,?)', (str(self.project.resolve()).lower(), 'fictional_skill', 'fictional_version', 1))
            handoff.db.commit()
            before = {t: handoff.db.execute('SELECT * FROM ' + t).fetchall() for t in ('facts', 'conflicts', 'skill_active', 'skill_versions')}
        draft = self.collect()
        original = handoff.checkpoint_receipt
        def fail(*args, **kwargs):
            if kwargs.get('write'):
                raise sqlite3.OperationalError('fictional handoff disk fault')
            return original(*args, **kwargs)
        with patch.object(handoff, 'checkpoint_receipt', side_effect=fail):
            pending = self.publish(draft)
        self.assertEqual(pending['reasons'], ['handoff_sync_required'])
        self.assertTrue(pending['store_ready'])
        self.assertFalse(self.engine.checkpoint_persistence(draft['id'])['handoff_synced'])
        self.assertTrue(self.engine.resync_checkpoint(draft['id'])['handoff_synced'])
        self.assertTrue(self.engine.resync_checkpoint(draft['id'])['handoff_synced'])
        with handoff.lock:
            after = {t: handoff.db.execute('SELECT * FROM ' + t).fetchall() for t in before}
            count = handoff.db.execute("SELECT count(*) FROM records WHERE kind='checkpoint_ready'").fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(count, 1)
        self.assertEqual(len(self.store.job_events(self.job['id'], 'checkpoint_handoff_pending')), 1)

    def test_lost_derived_record_is_detected_and_reconstructed_idempotently(self):
        draft = self.collect()
        self.publish(draft)
        handoff = self.engine.handoff
        with handoff.lock:
            handoff.db.execute("DELETE FROM records WHERE kind='checkpoint_ready'")
            handoff.db.commit()
        self.assertFalse(self.engine.checkpoint_persistence(draft['id'])['handoff_synced'])
        self.assertTrue(self.engine.resync_checkpoint(draft['id'])['handoff_synced'])
        self.assertEqual(len(self.store.job_events(self.job['id'], 'checkpoint_ready')), 1)

    def test_conflicting_derived_record_is_never_silently_overwritten(self):
        draft = self.collect()
        self.publish(draft)
        handoff = self.engine.handoff
        with handoff.lock:
            handoff.db.execute("UPDATE records SET payload='{}' WHERE kind='checkpoint_ready'")
            handoff.db.commit()
        self.assertFalse(self.engine.checkpoint_persistence(draft['id'])['handoff_synced'])
        result = self.engine.resync_checkpoint(draft['id'])
        self.assertTrue(result['store_ready'])
        self.assertFalse(result['handoff_synced'])
        self.assertEqual(handoff.db.execute("SELECT payload FROM records WHERE kind='checkpoint_ready'").fetchone()[0], '{}')

    def test_task_race_invalidates_draft_but_preserves_old_pointer(self):
        old = self.collect()
        self.publish(old)
        draft = self.collect()
        original = checkpoint.verify_draft_files
        def race(*args, **kwargs):
            result = original(*args, **kwargs)
            self.store.update(self.task['id'], summary='fictional concurrent update')
            return result
        with patch.object(checkpoint, 'verify_draft_files', side_effect=race), self.assertRaises(ValueError):
            self.publish(draft)
        self.assertEqual(self.store.get(draft['id'])['state'], 'invalid')
        self.assertEqual(self.store.get(self.task['id'])['resume_checkpoint_id'], old['id'])

    def test_handoff_authority_race_invalidates_draft(self):
        draft = self.collect()
        original = checkpoint.verify_draft_files
        def race(*args, **kwargs):
            result = original(*args, **kwargs)
            self.engine.handoff.fact(str(self.project), self.job['id'], 'fictional_key', 'changed',
                                     'fictional_evidence', 'user_confirmed', 'fictional-race')
            return result
        with patch.object(checkpoint, 'verify_draft_files', side_effect=race), self.assertRaises(ValueError):
            self.publish(draft)
        self.assertEqual(self.store.get(draft['id'])['state'], 'invalid')
        self.assertNotIn('resume_checkpoint_id', self.store.get(self.task['id']))

    def test_changed_blob_or_unresolved_operation_refuses_ready(self):
        draft = self.collect()
        self.app.write_text('value = 99\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.publish(draft)
        self.assertEqual(self.store.get(draft['id'])['state'], 'invalid')
        self.store.event('operation_dispatching', 'Fictional unknown effect', self.task['id'], self.job['id'])
        pending = self.collect()
        with self.assertRaises(ValueError):
            self.publish(pending)
        self.assertEqual(self.store.get(pending['id'])['state'], 'invalid')

    def test_current_task_change_after_ready_is_not_a_valid_candidate(self):
        draft = self.collect()
        self.publish(draft)
        self.store.update(self.task['id'], summary='fictional new progress')
        self.assertFalse(self.engine.checkpoint_persistence(draft['id'])['store_ready'])
        with self.assertRaises(ValueError):
            self.engine.resync_checkpoint(draft['id'])

    def test_store_publisher_also_refuses_unresolved_preconditions(self):
        self.store.event('operation_dispatching', 'Fictional unknown effect', self.task['id'], self.job['id'])
        draft = self.collect()
        with self.store.lock:
            job, task, paths, refs = self.engine._checkpoint_inputs(self.task['id'], True)
        with self.assertRaises(ValueError):
            self.store.commit_checkpoint(draft, refs)
        self.assertEqual(self.store.get(draft['id'])['state'], 'writing')
        self.assertNotIn('resume_checkpoint_id', self.store.get(self.task['id']))

    def test_missing_stop_confirmation_does_not_destroy_draft(self):
        draft = self.collect()
        with self.assertRaises(ValueError):
            self.engine.finalize_checkpoint(draft['id'])
        self.assertEqual(self.store.get(draft['id'])['state'], 'writing')

    def test_git_collection_and_verification_release_store_lock_but_keep_project_gate(self):
        original = checkpoint._git
        observed = []
        def check(*args, **kwargs):
            acquired = threading.Event()
            def other_thread():
                with self.store.lock:
                    acquired.set()
            worker = threading.Thread(target=other_thread)
            worker.start()
            worker.join(2)
            observed.append(acquired.is_set())
            self.assertTrue(acquired.is_set(), 'Git IO must not hold Store lock')
            self.assertTrue(self.engine._checkpoint_projects)
            self.assertFalse(self.store.db.in_transaction)
            return original(*args, **kwargs)
        with patch.object(checkpoint, '_git', side_effect=check):
            draft = self.collect()
            self.assertTrue(self.engine.verify_checkpoint_draft(draft['id'], writer_stopped=True)['valid'])
            self.publish(draft)
        self.assertTrue(observed)
        self.assertFalse(self.engine._checkpoint_projects)

    def test_concurrent_checkpoint_for_same_project_is_refused(self):
        with self.engine._checkpoint_project(self.task['id']):
            with self.assertRaises(ValueError):
                self.collect()
        self.assertFalse(self.engine._checkpoint_projects)
        self.assertEqual(self.collect()['state'], 'writing')

    def test_case_variant_of_registered_windows_path_is_checked_without_scope_expansion(self):
        if checkpoint.os.name != 'nt':
            self.skipTest('Windows case-insensitive path check')
        self.engine.config['approved_roots'] = [str(self.project).upper()]
        self.assertEqual(self.collect()['state'], 'writing')


if __name__ == '__main__':
    unittest.main()
