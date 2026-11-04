"""Fictional approved-plan work packages; no Provider, runtime or user data."""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

import team_wbs as wbs
from team_p0_contract import validate_plan


def proposal(two=False):
    links = ['Fictional lookup app', 'Fictional lookup tested']
    result = {'summary': 'Fictional plan', 'security_review_required': False,
        'questions': [], 'context_updates': [], 'warnings': [],
        'goal_contract': {'primary_goal': 'Fictional lookup', 'deliverables': links[:1],
            'acceptance_criteria': links[1:], 'non_goals': [], 'constraints': ['Fictional data only']},
        'code_test_plan': {'required': True, 'reason': 'Fictional test', 'cases': ['Fictional lookup'],
            'command': 'python -B -m unittest', 'existing_tests': []},
        'checkpoint_paths': ['app.py'],
        'work_packages': [{'wbs_id': 'W1.01', 'title': 'Fictional feature', 'goal_links': links,
            'done_when': ['Fictional lookup tested'], 'depends_on': [], 'input_artifacts': []}],
        'tasks': [{'title': 'Fictional build', 'instruction': 'Build fictional lookup', 'role': 'builder',
            'goal_links': links, 'wbs_id': 'W1.01'}]}
    if two:
        result['work_packages'].append(dict(result['work_packages'][0], wbs_id='W1.02',
                                           title='Fictional followup', depends_on=['W1.01']))
        result['tasks'].append(dict(result['tasks'][0], wbs_id='W1.02', title='Fictional second build'))
    return result


class WorkPackageValidationTests(unittest.TestCase):
    def test_complete_proposal_does_not_mutate_input(self):
        value = proposal(two=True); before = copy.deepcopy(value)
        self.assertEqual(wbs.validate_work_packages(value), value['work_packages'])
        self.assertEqual(value, before)

    def test_duplicate_and_runtime_identifiers_are_not_implicit_membership(self):
        value = proposal(two=True); value['work_packages'][1]['wbs_id'] = 'w1.01'
        with self.assertRaises(ValueError): wbs.validate_work_packages(value)
        value = proposal(); value['tasks'][0]['wbs_id'] = 'fictional-native-session'
        with self.assertRaises(ValueError): wbs.validate_work_packages(value)

    def test_task_and_completion_links_are_required_and_bounded(self):
        for field, replacement in (('goal_links', []), ('done_when', []), ('goal_links', ['Unapproved goal'])):
            value = proposal(); value['work_packages'][0][field] = replacement
            with self.assertRaises(ValueError): wbs.validate_work_packages(value)
        value = proposal(); value['tasks'][0]['goal_links'] = ['Unapproved task goal']
        with self.assertRaises(ValueError): wbs.validate_work_packages(value)

    def test_dependency_undefined_self_and_cycles_are_rejected(self):
        for deps in (['missing'], ['W1.01']):
            value = proposal(); value['work_packages'][0]['depends_on'] = deps
            with self.assertRaises(ValueError): wbs.validate_work_packages(value)
        value = proposal(two=True); value['work_packages'][0]['depends_on'] = ['W1.02']
        with self.assertRaises(ValueError): wbs.validate_work_packages(value)
        value = proposal(two=True); value['tasks'].reverse()
        with self.assertRaises(ValueError): wbs.validate_work_packages(value)

    def test_worker_cannot_supply_execution_or_review_state(self):
        for key in ('execution_state', 'review_status', 'task_ids', 'profile'):
            value = proposal(); value['work_packages'][0][key] = 'approved'
            with self.assertRaises(ValueError): wbs.validate_work_packages(value)

    def test_every_package_needs_a_planned_task_and_safe_checkpoint_scope(self):
        value = proposal(two=True); value['tasks'].pop()
        with self.assertRaises(ValueError): wbs.validate_work_packages(value)
        for paths in ([], ['../outside.py'], ['.env']):
            value = proposal(); value['checkpoint_paths'] = paths
            with self.assertRaises(ValueError): wbs.validate_work_packages(value)

    def test_schema_is_opt_in_and_base_is_unchanged(self):
        from team_engine import PLAN_SCHEMA_P0_TESTS
        from team_adapters import schema_errors
        before = copy.deepcopy(PLAN_SCHEMA_P0_TESTS)
        extended = wbs.plan_schema(PLAN_SCHEMA_P0_TESTS)
        self.assertEqual(schema_errors(proposal(), extended), [])
        self.assertNotIn('work_packages', PLAN_SCHEMA_P0_TESTS['properties'])
        self.assertEqual(PLAN_SCHEMA_P0_TESTS, before)
        self.assertIn('goal_contract', extended['required'])
        self.assertIn('code_test_plan', extended['required'])
        from team_checkpoint import MAX_FILES
        self.assertEqual(extended['properties']['checkpoint_paths']['maxItems'], MAX_FILES)

    def test_legacy_plan_stays_valid_without_new_fields(self):
        value = proposal(); value.pop('work_packages'); value.pop('checkpoint_paths')
        value['tasks'][0].pop('wbs_id')
        self.assertEqual(validate_plan({'goal': 'Fictional lookup'}, value)['primary_goal'], 'Fictional lookup')
        selected = {'goal': 'Fictional lookup', 'workflow_contract_version': 1,
                    'request_origin': 'new', 'route': 'full', 'document_source': None, 'p0_contract_version': 1}
        with self.assertRaises(ValueError): validate_plan(selected, value)


class GoalLinkNormalizationTests(unittest.TestCase):
    """M02-I: D<n>/A<n> labels expand to exact contract text; everything else stays untouched or is refused."""
    def labelled(self):
        value = proposal()
        value['work_packages'][0]['goal_links'] = ['D1', 'A1']
        value['tasks'][0]['goal_links'] = ['A1']
        return value

    def test_labels_expand_by_contract_order_in_tasks_and_packages_without_mutating_input(self):
        value = self.labelled(); before = copy.deepcopy(value)
        result = wbs.normalize_goal_links(value)
        self.assertEqual(value, before)
        self.assertEqual(result['work_packages'][0]['goal_links'], ['Fictional lookup app', 'Fictional lookup tested'])
        self.assertEqual(result['tasks'][0]['goal_links'], ['Fictional lookup tested'])
        self.assertEqual(wbs.validate_work_packages(result), result['work_packages'])

    def test_normalizing_is_idempotent_and_leaves_exact_text_alone(self):
        value = proposal()
        self.assertEqual(wbs.normalize_goal_links(value), value)
        once = wbs.normalize_goal_links(self.labelled())
        self.assertEqual(wbs.normalize_goal_links(once), once)

    def test_unknown_number_is_refused_and_inexact_forms_are_not_guessed(self):
        for label in ('D2', 'A2', 'D10'):
            value = self.labelled(); value['tasks'][0]['goal_links'] = [label]
            with self.assertRaises(ValueError): wbs.normalize_goal_links(value)
        for label in ('d1', 'D 1', 'D1 ', 'D01', 'A0', 'Fictional lookup'):
            value = self.labelled(); value['tasks'][0]['goal_links'] = [label]
            result = wbs.normalize_goal_links(value)
            self.assertEqual(result['tasks'][0]['goal_links'], [label])
            with self.assertRaises(ValueError): wbs.validate_work_packages(result)

    def test_label_plus_its_own_text_is_a_duplicate_and_partial_coverage_still_fails(self):
        value = self.labelled(); value['work_packages'][0]['goal_links'] = ['D1', 'Fictional lookup app', 'A1']
        with self.assertRaises(ValueError): wbs.validate_work_packages(wbs.normalize_goal_links(value))
        value = self.labelled(); value['work_packages'][0]['goal_links'] = ['D1']
        with self.assertRaises(ValueError): wbs.validate_work_packages(wbs.normalize_goal_links(value))

    def test_guide_documents_the_label_rule(self):
        self.assertIn('D1', wbs.GUIDE); self.assertIn('A1', wbs.GUIDE); self.assertIn('1始まり', wbs.GUIDE)


class WorkPackageStorageTests(unittest.TestCase):
    def setUp(self):
        from tests.test_versioned_context import VersionedContextTests
        self.fixture = VersionedContextTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.engine, self.store = self.fixture.engine, self.fixture.store
        self.payload = proposal(two=True)
        self.job = self.store.update(self.fixture.job['id'], status='awaiting_approval', code_test_contract_version=1)
        self.planner = self.engine.new_task(self.job, 'Fictional plan', 'Plan fictional lookup', 'planner')
        approval = self.engine.new_approval(self.planner, 'plan', self.payload)
        self.approval = self.store.update(approval['id'], status='approved')
        self.job = self.store.update(self.job['id'], requirement_revision=2, plan_revision=2,
            requirements_approval_id=approval['id'], plan_approval_id=approval['id'])

    def install(self):
        return self.engine.install_work_packages(self.job['id'], expected_job_revision=self.store.get(self.job['id'])['state_revision'])

    def state(self):
        return (self.store.db.execute('SELECT * FROM objects ORDER BY id').fetchall(),
                self.store.db.execute('SELECT * FROM events ORDER BY seq').fetchall())

    def task(self, index=0, bound=False):
        spec = self.payload['tasks'][index]
        task = self.engine.new_task(self.job, spec['title'], spec['instruction'], spec['role'], self.planner['id'],
                                   **({'plan_task_index': index} if bound else {}))
        if bound: return task
        return self.store.update(task['id'], goal_links=list(spec['goal_links']), goal_id=self.job['id'])

    def test_materialization_is_idempotent_and_keeps_definitions_in_approval(self):
        original = self.store.get(self.approval['id'])
        ids = self.install(); before = self.state()
        self.assertEqual(self.install(), ids); self.assertEqual(self.state(), before)
        self.assertEqual(self.store.get(self.approval['id']), original)
        for row in self.store.for_job('work_package', self.job['id']):
            self.assertNotEqual(row['id'], row['wbs_id'])
            self.assertNotIn('depends_on', row); self.assertNotIn('task_ids', row)
            self.assertEqual(row['execution_state'], 'planned')
            self.assertEqual(row['review_status'], 'not_requested')

    def test_task_membership_is_single_source_and_does_not_change_after_or_profile(self):
        original = self.task()
        bound = self.task(bound=True)
        self.assertNotEqual(bound['work_package_id'], 'W1.01')
        self.assertEqual(bound['after'], original['after'])
        self.assertEqual(bound['profile'], original['profile'])
        self.assertEqual(bound['attempt'], 0); self.assertIsNone(bound.get('current_attempt'))
        self.assertEqual(bound['plan_task_index'], 0)
        with self.assertRaises(ValueError): self.store.update(original['id'], work_package_id=bound['work_package_id'])
        with self.assertRaises(ValueError): self.store.update(bound['id'], work_package_id=None)
        before = self.state()
        with self.assertRaises(ValueError): self.task(bound=True)
        self.assertEqual(before, self.state())

    def test_view_derives_dependencies_and_membership_without_writing(self):
        task = self.task(bound=True); before = self.state()
        view = self.engine.work_package_view(self.job['id'])
        self.assertEqual(before, self.state())
        self.assertEqual(view['execution_permission'], 'none')
        self.assertEqual(view['work_packages'][0]['task_ids'], [task['id']])
        self.assertEqual(view['work_packages'][1]['depends_on'], ['W1.01'])

    def test_view_does_not_materialize_missing_packages(self):
        before = self.state(); view = self.engine.work_package_view(self.job['id'])
        self.assertFalse(view['materialized']); self.assertEqual(before, self.state())

    def test_projection_tampering_is_not_new_authority(self):
        ids = self.install()
        self.store.update(ids['W1.01'], done_when=['Fictional changed condition'])
        with self.assertRaises(ValueError): self.install()
        with self.assertRaises(ValueError): self.engine.work_package_view(self.job['id'])

    def test_approval_digest_and_plan_state_are_rechecked(self):
        self.store.update(self.approval['id'], payload=dict(self.payload, summary='Fictional changed payload'))
        before = self.state()
        with self.assertRaises(ValueError): self.install()
        self.assertEqual(before, self.state())

    def test_denied_or_terminal_plan_cannot_materialize(self):
        self.store.update(self.approval['id'], status='denied')
        with self.assertRaises(ValueError): self.install()
        self.store.update(self.approval['id'], status='approved')
        self.store.update(self.job['id'], status='cancelled')
        with self.assertRaises(ValueError): self.install()

    def test_stale_revision_and_wrong_task_spec_are_refused(self):
        with self.assertRaises(ValueError):
            self.engine.install_work_packages(self.job['id'], expected_job_revision=True)
        with self.assertRaises(ValueError): self.engine.new_task(self.job, 'Fictional build',
            'Unapproved fictional instruction', 'builder', self.planner['id'], plan_task_index=0)
        with self.assertRaises(ValueError): self.engine.new_task(self.job, 'Fictional build',
            'Build fictional lookup', 'builder', 'fictional-native-session', plan_task_index=0)

    def test_existing_after_chain_is_preserved_during_atomic_expansion(self):
        original = self.store.get(self.approval['id'])
        self.engine._expand_plan(self.planner, copy.deepcopy(self.payload))
        tasks = [t for t in self.store.for_job('task', self.job['id']) if t.get('work_package_id')]
        self.assertEqual(len(tasks), 2)
        by_title = {t['title']: t for t in tasks}
        self.assertEqual(by_title['Fictional build']['after'], self.planner['id'])
        self.assertEqual(by_title['Fictional second build']['after'], by_title['Fictional build']['id'])
        self.assertEqual(self.store.get(self.approval['id']), original)

    def test_modified_result_cannot_replace_approved_payload(self):
        value = copy.deepcopy(self.payload); value['tasks'][0]['instruction'] = 'Different fictional scope'
        before = self.state()
        with self.assertRaises(ValueError): self.engine._expand_plan(self.planner, value)
        self.assertEqual(before, self.state())

    def test_fault_during_expansion_rolls_back_packages_tasks_and_events(self):
        original = self.engine.new_task
        def fail(job, title, *args, **kwargs):
            if title == 'Fictional second build': raise RuntimeError('Fictional expansion fault')
            return original(job, title, *args, **kwargs)
        before = self.state()
        with patch.object(self.engine, 'new_task', side_effect=fail):
            with self.assertRaises(RuntimeError): self.engine._expand_plan(self.planner, self.payload)
        self.assertEqual(before, self.state())

    def test_processed_g3_cannot_be_reapproved_to_duplicate_work(self):
        before = self.state()
        with self.assertRaises(ValueError): self.engine.decide(self.approval['id'], True, '')
        self.assertEqual(before, self.state())

    def test_missing_g3_version_is_not_self_registered(self):
        from team_p0_contract import initial, initial_workflow
        job = self.store.put('job', initial_workflow({'id': 'fictional-unapproved-job',
            'goal': 'Fictional fresh request', 'goal_contract': initial('Fictional fresh request'),
            'project': str(self.fixture.project), 'status': 'awaiting_approval',
            'request_origin': 'new', 'route': 'full', 'document_source': None, 'p0_contract_version': 1}, enabled=True))
        before = self.state()
        with self.assertRaises(ValueError):
            self.engine.install_work_packages(job['id'], expected_job_revision=job['state_revision'])
        self.assertEqual(before, self.state())
        self.assertEqual(self.store.get(job['id'])['plan_revision'], 0)

    def test_legacy_job_is_not_enrolled_or_modified(self):
        job = self.store.put('job', {'id': 'fictional-legacy-job', 'goal': 'Fictional legacy',
            'project': str(self.fixture.project), 'status': 'running'})
        before = self.state()
        with self.assertRaises(ValueError):
            self.engine.install_work_packages(job['id'], expected_job_revision=job.get('state_revision', 0))
        self.assertEqual(before, self.state())
        self.assertNotIn('workflow_contract_version', self.store.get(job['id']))


if __name__ == '__main__':
    unittest.main()
