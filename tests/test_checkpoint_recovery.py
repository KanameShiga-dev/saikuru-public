"""Read-only recovery assessment with fictional repositories/DBs only."""
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

if __package__:
    from . import test_checkpoint_collection as fixtures
else:
    import test_checkpoint_collection as fixtures
import team_checkpoint as checkpoint


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.EngineDraftTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        for name in ('engine', 'store', 'task', 'job', 'project', 'root', 'app', 'cache'):
            setattr(self, name, getattr(self.fixture, name))

    def publish(self):
        draft = self.engine.collect_checkpoint(self.task['id'], writer_stopped=True)
        result = self.engine.finalize_checkpoint(draft['id'], writer_stopped=True)
        self.assertTrue(result['handoff_synced'])
        return draft

    def assess(self):
        return self.engine.assess_checkpoint_resume(self.task['id'], writer_stopped=True)

    def database_state(self):
        return (self.store.db.execute('SELECT * FROM objects ORDER BY id').fetchall(),
                self.store.db.execute('SELECT * FROM events ORDER BY seq').fetchall(),
                self.engine.handoff.db.execute('SELECT * FROM records ORDER BY seq').fetchall())

    def other_tasks(self):
        independent = self.engine.new_task(self.job, 'Fictional independent', 'Fictional independent scope', 'builder')
        independent = self.store.update(independent['id'], status='succeeded')
        following = self.engine.new_task(self.job, 'Fictional following', 'Fictional dependent scope', 'builder', self.task['id'])
        return independent, following

    def add_test_asset(self):
        tests = self.project / 'tests'
        tests.mkdir()
        asset = tests / 'test_app.py'
        asset.write_text('assert 1 == 1\n', encoding='utf-8', newline='\n')
        approval = self.store.get(self.job['plan_approval_id'])
        payload = dict(approval['payload'], checkpoint_paths=['app.py', 'tests/test_app.py'])
        sha = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        self.store.update(approval['id'], payload=payload, digest=sha)
        self.store.update(self.task['id'], test_asset_snapshot={'tests/test_app.py': hashlib.sha256(asset.read_bytes()).hexdigest()})
        return asset

    def test_matching_checkpoint_assessment_is_read_only_and_never_execution_authority(self):
        draft = self.publish()
        before = self.database_state()
        with patch.object(self.store, 'reserve_attempt', side_effect=AssertionError('must not dispatch')):
            result = self.assess()
        self.assertEqual(result['status'], 'candidate')
        self.assertFalse(result['resume_allowed'])
        self.assertEqual(result['reasons'], ['authorization_and_fresh_attempt_required'])
        self.assertEqual(result['preserved_succeeded_task_ids'], [self.task['id']])
        self.assertEqual(result['candidate_task_ids'], [])
        self.assertEqual(before, self.database_state())
        self.assertEqual(result['checkpoint_id'], draft['id'])

    def test_administrative_revision_after_restart_can_be_assessed_without_rebasing(self):
        draft = self.publish()
        self.store.update(self.job['id'], status='interrupted', summary='Fictional restart notice')
        self.store.update(self.task['id'], summary='Fictional administrative notice')
        self.assertFalse(self.engine.checkpoint_persistence(draft['id'])['store_ready'])
        before = self.database_state()
        self.assertEqual(self.assess()['status'], 'candidate')
        self.assertEqual(before, self.database_state())
        self.assertEqual(self.store.get(draft['id'])['state'], 'ready')

    def test_next_queued_task_is_selected_without_reexecuting_success(self):
        independent, following = self.other_tasks()
        self.publish()
        before = self.database_state()
        result = self.assess()
        self.assertEqual(result['status'], 'candidate')
        self.assertEqual(result['candidate_task_ids'], [following['id']])
        self.assertEqual(set(result['preserved_succeeded_task_ids']), {self.task['id'], independent['id']})
        self.assertFalse(result['resume_allowed'])
        self.assertEqual(before, self.database_state())

    def test_failed_boundary_requires_new_authorization_and_attempt_without_retrying(self):
        self.store.update(self.task['id'], status='failed')
        self.publish()
        before = self.database_state()
        result = self.assess()
        self.assertEqual(result['candidate_task_ids'], [self.task['id']])
        self.assertEqual(result['reasons'], ['authorization_and_fresh_attempt_required'])
        self.assertFalse(result['resume_allowed'])
        self.assertEqual(before, self.database_state())

    def test_spec_change_rechecks_only_branch_and_preserves_unrelated_success(self):
        independent, following = self.other_tasks()
        self.publish()
        self.store.update(self.task['id'], instruction='Fictional revised instruction')
        before = self.database_state()
        result = self.assess()
        self.assertIn('task_evidence_changed', result['reasons'])
        self.assertEqual(set(result['revalidate_task_ids']), {self.task['id'], following['id']})
        self.assertEqual(result['preserved_succeeded_task_ids'], [independent['id']])
        self.assertEqual(result['candidate_task_ids'], [])
        self.assertEqual(before, self.database_state())

    def test_changed_test_asset_rechecks_owner_and_dependents_only(self):
        asset = self.add_test_asset()
        independent, following = self.other_tasks()
        self.publish()
        asset.write_text('assert 2 == 2\n', encoding='utf-8', newline='\n')
        result = self.assess()
        self.assertIn('test_assets_changed', result['reasons'])
        self.assertEqual(result['changed_paths'], ['tests/test_app.py'])
        self.assertEqual(set(result['revalidate_task_ids']), {self.task['id'], following['id']})
        self.assertEqual(result['preserved_succeeded_task_ids'], [independent['id']])

    def test_unmapped_source_change_stops_instead_of_guessing_scope_or_resetting(self):
        independent, following = self.other_tasks()
        self.publish()
        self.app.write_text('value = 999\n', encoding='utf-8', newline='\n')
        before = self.database_state()
        result = self.assess()
        self.assertIn('change_scope_unresolved', result['reasons'])
        self.assertEqual(result['preserved_succeeded_task_ids'], [])
        self.assertEqual(result['candidate_task_ids'], [])
        self.assertEqual(before, self.database_state())
        self.assertEqual(self.store.get(independent['id'])['status'], 'succeeded')

    def test_approval_or_handoff_authority_change_is_blocked(self):
        self.publish()
        self.engine.handoff.fact(str(self.project), self.job['id'], 'fictional_key', 'changed',
                                 'fictional evidence', 'user_confirmed', 'fictional-authority')
        self.assertIn('handoff_changed', self.assess()['reasons'])
        self.store.update(self.job['plan_approval_id'], status='rejected')
        self.assertEqual(self.assess()['reasons'], ['approval_or_authority_changed'])

    def test_unknown_external_effect_is_not_replayed(self):
        self.publish()
        self.store.event('operation_dispatching', 'Fictional unknown effect', self.task['id'], self.job['id'])
        before = self.database_state()
        result = self.assess()
        self.assertIn('operation_reconciliation_required', result['reasons'])
        self.assertFalse(result['resume_allowed'])
        self.assertEqual(before, self.database_state())

    def test_recovery_fence_is_not_cleared_by_matching_files(self):
        self.publish()
        self.store.update(self.task['id'], recovery_check_required=True)
        result = self.assess()
        self.assertIn('writer_reconciliation_required', result['reasons'])
        self.assertTrue(self.store.get(self.task['id'])['recovery_check_required'])
        self.assertFalse(result['candidate_task_ids'])

    def test_changed_job_validation_cannot_reuse_old_success_evidence(self):
        self.publish()
        self.store.update(self.job['id'], validation_evidence={'status': 'fictional changed evidence'})
        result = self.assess()
        self.assertIn('job_validation_changed', result['reasons'])
        self.assertFalse(result['preserved_succeeded_task_ids'])

    def test_changed_success_status_is_not_queued_again(self):
        self.publish()
        self.store.update(self.task['id'], status='failed')
        result = self.assess()
        self.assertIn('task_state_changed', result['reasons'])
        self.assertFalse(result['candidate_task_ids'])

    def test_missing_handoff_receipt_is_reported_without_automatic_repair(self):
        self.publish()
        self.engine.handoff.db.execute("DELETE FROM records WHERE kind='checkpoint_ready'")
        self.engine.handoff.db.commit()
        before = self.database_state()
        self.assertIn('handoff_sync_required', self.assess()['reasons'])
        self.assertEqual(before, self.database_state())

    def test_missing_stop_confirmation_does_not_read_git(self):
        self.publish()
        with patch.object(checkpoint, '_git', side_effect=AssertionError('must not read Git')):
            result = self.engine.assess_checkpoint_resume(self.task['id'])
        self.assertEqual(result['reasons'], ['worker_stop_unconfirmed'])

    def test_terminal_job_cannot_become_resume_candidate(self):
        self.publish()
        self.store.update(self.job['id'], status='cancelled')
        self.assertEqual(self.assess()['reasons'], ['job_terminal_or_unknown'])

    def test_new_attempt_cannot_reuse_old_checkpoint(self):
        self.publish()
        current = self.store.update(self.task['id'], status='queued')
        self.store.reserve_attempt(self.task['id'], current['state_revision'])
        self.store.close_attempt(self.task['id'], 'fenced', 'fictional stop')
        self.store.update(self.task['id'], status='interrupted')
        self.assertEqual(self.assess()['reasons'], ['attempt_changed'])

    def test_missing_current_pointer_never_falls_back_to_older_ready(self):
        self.publish()
        # Model a damaged fictional DB; normal Store writers prohibit this.
        task = self.store.get(self.task['id'])
        task.pop('resume_checkpoint_id')
        self.store.db.execute('UPDATE objects SET body=? WHERE id=?', (json.dumps(task), task['id']))
        self.store.db.commit()
        self.assertEqual(self.assess()['reasons'], ['checkpoint_missing'])

    def test_old_ready_without_recovery_context_is_not_silently_upgraded(self):
        with patch.object(checkpoint, 'recovery_context', return_value={'schema_version': 0, 'tasks': {}, 'job': None}):
            draft = self.publish()
        before = self.database_state()
        result = self.assess()
        self.assertIn('recovery_context_missing_or_changed', result['reasons'])
        self.assertFalse(result['candidate_task_ids'])
        self.assertEqual(before, self.database_state())

    def test_test_asset_outside_approval_is_not_read_or_reported_as_current(self):
        self.store.update(self.task['id'], test_asset_snapshot={'outside.py': '0' * 64})
        self.publish()
        result = self.assess()
        self.assertIn('test_assets_unverifiable', result['reasons'])
        self.assertNotIn('outside.py', json.dumps(result))

    def test_source_cannot_be_relabelled_as_test_to_guess_narrower_impact(self):
        independent, following = self.other_tasks()
        self.store.update(self.task['id'], test_asset_snapshot={'app.py': hashlib.sha256(self.app.read_bytes()).hexdigest()})
        self.publish()
        self.app.write_text('value = 999\n', encoding='utf-8', newline='\n')
        result = self.assess()
        self.assertIn('test_assets_unverifiable', result['reasons'])
        self.assertIn('change_scope_unresolved', result['reasons'])
        self.assertFalse(result['preserved_succeeded_task_ids'])

    def test_authority_race_during_filesystem_verification_is_blocked(self):
        self.publish()
        original = checkpoint.inspect_collection
        def race(*args, **kwargs):
            result = original(*args, **kwargs)
            self.store.update(self.task['id'], summary='Fictional concurrent administrative update')
            return result
        with patch.object(checkpoint, 'inspect_collection', side_effect=race):
            self.assertEqual(self.assess()['reasons'], ['references_changed_during_assessment'])

    def test_temporary_project_pause_preserves_valid_writing_draft(self):
        draft = self.engine.collect_checkpoint(self.task['id'], writer_stopped=True)
        self.engine.project_blocked = lambda project: True
        with self.assertRaises(checkpoint.CheckpointUnavailable):
            self.engine.finalize_checkpoint(draft['id'], writer_stopped=True)
        self.assertEqual(self.store.get(draft['id'])['state'], 'writing')
        self.engine.project_blocked = lambda project: False
        self.assertTrue(self.engine.finalize_checkpoint(draft['id'], writer_stopped=True)['handoff_synced'])


if __name__ == '__main__':
    unittest.main()
