"""Fictional persisted workflow records; no Engine/provider or operational DB."""
import copy
from pathlib import Path
import tempfile
import unittest

from team_config import default_config, validate_config
from team_p0_contract import initial, initial_workflow, workflow_version
from team_store import Store


class WorkflowStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fictional-workflow-storage-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.db.close())

    def request(self, identifier='fictional-job'):
        return dict(id=identifier, goal='Fictional product lookup', status='awaiting_approval',
                    request_origin='new', route='full', document_source=None,
                    p0_contract_version=1, goal_contract=initial('Fictional product lookup'))

    def workflow(self, identifier='fictional-job'):
        return self.store.put('job', initial_workflow(self.request(identifier), enabled=True))

    def approved(self):
        job = self.workflow()
        self.store.put('approval', dict(id='fictional-plan', job_id=job['id'], kind='plan', status='approved'))
        return self.store.update(job['id'], requirement_revision=1, requirements_approval_id='fictional-plan',
                                 plan_revision=1, plan_approval_id='fictional-plan')

    def wp(self):
        job = self.approved()
        return self.store.put('work_package', dict(id='fictional-wp', job_id=job['id'], schema_version=1,
            plan_revision=1, spec_source_approval_id='fictional-plan',
            execution_state='planned', review_status='not_requested'))

    def test_default_off_and_explicit_selection(self):
        config = default_config()
        self.assertIs(config['workflow_contract_enabled'], False)
        validate_config(config)
        for invalid in (None, 'true', 1):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_config(dict(config, workflow_contract_enabled=invalid))
        original = self.request()
        with self.assertRaises(ValueError):
            initial_workflow(original)
        selected = initial_workflow(original, enabled=True)
        self.assertNotIn('workflow_contract_version', original)
        self.assertEqual(workflow_version(selected), 1)
        self.assertEqual(selected['plan_revision'], 0)

    def test_selection_rejects_non_new_full_p0_and_unrecognized_versions(self):
        for key, value in [('request_origin', 'legacy'), ('route', 'light_eval'),
                           ('document_source', 'fictional.md'), ('p0_contract_version', False),
                           ('p0_contract_version', 0), ('status', 'accepted')]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                initial_workflow(dict(self.request(), **{key: value}), enabled=True)
        for version in (None, True, '1', 0, 2):
            with self.subTest(version=version), self.assertRaises(ValueError):
                workflow_version(dict(self.request(), workflow_contract_version=version))

    def test_legacy_objects_remain_unstamped_and_reload_unchanged(self):
        job = self.store.put('job', dict(id='legacy', goal='Fictional legacy request', status='blocked'))
        task = self.store.put('task', dict(id='legacy-task', job_id='legacy', status='blocked'))
        before = {kind: self.store.all(kind) for kind in ('job', 'task')}
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual(before, {kind: self.store.all(kind) for kind in before})
        self.assertNotIn('workflow_contract_version', job)
        self.assertNotIn('state_revision', task)

    def test_existing_jobs_cannot_be_enrolled_and_workflow_cannot_be_removed(self):
        job = self.store.put('job', self.request())
        with self.assertRaises(ValueError):
            self.store.put('job', initial_workflow(job, enabled=True))
        selected = self.workflow('selected')
        del selected['workflow_contract_version']
        with self.assertRaises(ValueError):
            self.store.put('job', selected)

    def test_revision_persists_and_stale_snapshot_is_rejected(self):
        job = self.workflow()
        stale = copy.deepcopy(job)
        updated = self.store.update(job['id'], status='planning')
        self.assertEqual(updated['state_revision'], 2)
        with self.assertRaises(ValueError):
            self.store.put('job', stale)
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.get(job['id'])['state_revision'], 2)

    def test_legacy_orphan_task_can_be_created_updated_and_reloaded(self):
        task = self.store.put('task', dict(id='orphan', job_id='missing-legacy-job', status='blocked'))
        self.assertNotIn('state_revision', task)
        updated = self.store.update(task['id'], summary='Fictional preserved legacy update')
        self.assertNotIn('state_revision', updated)
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.get(task['id']), updated)

    def test_missing_identifiers_rejected_with_value_error(self):
        for kind, record in [('task', {'id': 'bad-task'}), ('work_package', {'id': 'bad-wp'}),
                              ('checkpoint', {'id': 'bad-checkpoint'})]:
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.store.put(kind, record)
        job = self.workflow()
        with self.assertRaises(ValueError):
            self.store.put('checkpoint', dict(id='missing-task', job_id=job['id'], schema_version=1,
                                              state='writing'))
        with self.assertRaises(ValueError):
            self.store.put('task', dict(id='new-orphan', job_id='missing', state_revision=1, status='queued'))

    def test_new_object_requires_eligible_parent_and_supported_schema(self):
        wp = self.wp()
        self.assertEqual(wp['state_revision'], 1)
        for field, value in [('schema_version', 2), ('schema_version', True), ('plan_revision', 0),
                             ('plan_revision', True), ('spec_source_approval_id', 'other'),
                             ('execution_state', 'done'), ('review_status', 'accepted'),
                             ('task_ids', []), ('depends_on', [])]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.store.put('work_package', dict(wp, **{field: value}))
        self.store.put('job', dict(id='legacy', goal='Fictional', status='blocked'))
        with self.assertRaises(ValueError):
            self.store.put('work_package', dict(wp, id='legacy-wp', job_id='legacy'))

    def test_approval_references_are_same_job_and_approved(self):
        job = self.workflow()
        for status, parent in [('pending', job['id']), ('denied', job['id']), ('approved', 'other')]:
            self.store.put('approval', dict(id='fictional-plan', job_id=parent, kind='plan', status=status))
            with self.subTest(status=status, parent=parent), self.assertRaises(ValueError):
                self.store.update(job['id'], plan_revision=1, plan_approval_id='fictional-plan')
        self.assertEqual(self.store.get(job['id'])['plan_revision'], 0)

    def test_parent_membership_and_checkpoint_drafts(self):
        wp = self.wp()
        task = self.store.put('task', dict(id='fictional-task', job_id=wp['job_id'],
                                          work_package_id=wp['id'], status='queued'))
        checkpoint = self.store.put('checkpoint', dict(id='fictional-checkpoint', schema_version=1,
            job_id=wp['job_id'], work_package_id=wp['id'], task_id=task['id'], state='writing'))
        self.assertEqual(checkpoint['state_revision'], 1)
        with self.assertRaises(ValueError):
            self.store.update(checkpoint['id'], state='ready')
        self.workflow('other')
        with self.assertRaises(ValueError):
            self.store.put('task', dict(id='foreign-task', job_id='other', work_package_id=wp['id'], status='queued'))
        with self.assertRaises(ValueError):
            self.store.update(task['id'], work_package_id=None)
        self.assertEqual(self.store.get(checkpoint['id'])['state'], 'writing')
        self.store.update(checkpoint['id'], state='invalid')
        with self.assertRaises(ValueError):
            self.store.update(checkpoint['id'], state='writing')
        self.store.put('job', dict(id='legacy', goal='Fictional legacy', status='blocked'))
        with self.assertRaises(ValueError):
            self.store.update(task['id'], job_id='legacy', work_package_id=None)

    def test_object_kind_collision_cannot_overwrite_existing_record(self):
        job = self.workflow()
        with self.assertRaises(ValueError):
            self.store.put('approval', dict(id=job['id'], job_id=job['id'], status='pending'))
        self.assertEqual(self.store.get(job['id'], 'job'), job)


if __name__ == '__main__':
    unittest.main()
