"""Fictional WBS graph/size/input validation, without provider calls."""
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import team_wbs as wbs
from team_p0_contract import validate_plan
from tests.test_work_packages import proposal


class GraphTests(unittest.TestCase):
    def rejected(self, payload):
        with self.assertRaises(ValueError): wbs.validate_work_packages(payload)

    def test_longer_cycle_and_transitive_order_conflict(self):
        value = proposal(two=True)
        value['work_packages'].append(dict(value['work_packages'][1], wbs_id='W1.03', depends_on=['W1.02']))
        value['tasks'].append(dict(value['tasks'][1], wbs_id='W1.03'))
        self.assertEqual(len(wbs.validate_work_packages(value)), 3)
        cycle = copy.deepcopy(value); cycle['work_packages'][0]['depends_on'] = ['W1.03']
        self.rejected(cycle)
        value['tasks'][0], value['tasks'][2] = value['tasks'][2], value['tasks'][0]
        self.rejected(value)

    def test_missing_duplicate_self_and_foreign_job_labels(self):
        for deps in (['missing'], ['W1.01'], ['W1.02', 'W1.02'], ['foreign-job/W1.02']):
            value = proposal(two=True); value['work_packages'][0]['depends_on'] = deps
            self.rejected(value)

    def test_each_package_needs_deliverable_and_acceptance_link(self):
        for links in (['Fictional lookup app'], ['Fictional lookup tested']):
            value = proposal(); value['work_packages'][0]['goal_links'] = links
            value['tasks'][0]['goal_links'] = links
            self.rejected(value)

    def test_full_goal_coverage_without_forcing_one_package_per_condition(self):
        value = proposal(); value['goal_contract']['acceptance_criteria'].append('Fictional error tested')
        self.rejected(value)
        value['work_packages'][0]['goal_links'].append('Fictional error tested')
        self.assertEqual(len(wbs.validate_work_packages(value)), 1)

    def test_size_bounds_reject_instead_of_automatically_splitting(self):
        for field in ('work_packages', 'tasks'):
            value = proposal(); value[field] *= 7
            self.rejected(value)
        value = proposal(); value['work_packages'][0]['done_when'] = ['condition'] * 21
        self.rejected(value)
        value = proposal(); before = copy.deepcopy(value)
        wbs.validate_work_packages(value)
        self.assertEqual(value, before)  # No rewrite, token heuristic or Task split.

    def test_one_and_six_packages_valid_seven_refused_with_split_guidance(self):
        def sized(count):
            value = proposal(); package, task = value['work_packages'][0], value['tasks'][0]
            value['work_packages'] = [dict(copy.deepcopy(package), wbs_id=f'W1.{n:02d}') for n in range(1, count + 1)]
            value['tasks'] = [dict(copy.deepcopy(task), wbs_id=f'W1.{n:02d}') for n in range(1, count + 1)]
            return value
        for count in (1, 6):
            self.assertEqual(len(wbs.validate_work_packages(sized(count))), count)
        for field in ('work_packages', 'tasks'):
            value = sized(6); value[field].append(copy.deepcopy(value[field][0]))
            before = copy.deepcopy(value)
            with self.assertRaises(ValueError) as caught: wbs.validate_work_packages(value)
            self.assertEqual(str(caught.exception), 'work_package_count_over_limit')
            self.assertEqual(value, before)  # Nothing is truncated or rewritten.
        with self.assertRaises(ValueError) as caught: wbs.validate_work_packages(dict(proposal(), work_packages=[]))
        self.assertEqual(str(caught.exception), 'work_package_count_invalid')
        self.assertEqual(wbs.SIZE_ADVICE['limit'], 6)
        self.assertIn('7件以上', wbs.GUIDE); self.assertIn('段階', wbs.GUIDE)

    def test_wait_advice_distinguishes_kinds_and_owners_without_dispatching(self):
        expected = {'workspace_or_series_busy': ('capacity', 'system'), 'retry_wait': ('retry', 'system'),
                    'pending_decision': ('decision', 'user'), 'dependency_feature_review_required': ('dependency', 'system'),
                    'recovery_confirmation_required': ('maintenance', 'maintainer'),
                    'task_plan_not_current': ('consistency', 'maintainer'), 'never_seen_code': ('unknown', 'maintainer')}
        for code, pair in expected.items():
            advice = wbs.wait_advice(code)
            self.assertEqual((advice['kind'], advice['owner']), pair, code)

    def test_wait_owner_follows_who_holds_the_pending_approval(self):
        for approver in ('user', 'authorized_person'):
            for code in ('pending_decision', 'dependency_feature_review_required'):
                self.assertEqual(wbs.wait_advice(code, approver)['owner'], approver, (code, approver))
        # A finished feature whose review is not started yet waits for the user to start it.
        self.assertEqual(wbs.wait_advice('dependency_feature_review_required', None, True)['owner'], 'user')
        self.assertEqual(wbs.wait_advice('dependency_feature_review_required')['owner'], 'system')
        self.assertEqual(wbs.wait_advice('retry_wait', None, True)['owner'], 'system')
        # A broken review record is never fixed by a person approving it.
        self.assertEqual(wbs.wait_advice('dependency_feature_review_invalid', 'user')['owner'], 'maintainer')
        self.assertEqual(wbs.wait_advice('retry_wait', 'user')['owner'], 'system')

    def test_old_approved_payload_without_inputs_remains_valid(self):
        value = proposal(); del value['work_packages'][0]['input_artifacts']
        self.assertEqual(wbs.validate_work_packages(value), value['work_packages'])

    def test_new_schema_exposes_exact_bounded_input_refs(self):
        from team_engine import PLAN_SCHEMA_P0_TESTS
        schema = wbs.plan_schema(PLAN_SCHEMA_P0_TESTS)
        item = schema['properties']['work_packages']['items']
        self.assertIn('input_artifacts', item['required'])
        refs = item['properties']['input_artifacts']
        self.assertEqual(refs['maxItems'], 20)
        self.assertFalse(refs['items']['additionalProperties'])
        self.assertNotIn('work_packages', PLAN_SCHEMA_P0_TESTS['properties'])


class InputArtifactTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root/'existing.py').write_text('fictional = True\n', encoding='utf-8')
        self.record = {'path': 'existing.py', 'sha256': hashlib.sha256((self.root/'existing.py').read_bytes()).hexdigest()}
        self.job = {'id': 'fictional-job', 'project': str(self.root), 'p0_artifacts': [self.record]}
        self.value = proposal()
        self.value['work_packages'][0]['input_artifacts'] = [dict(self.record, job_id=self.job['id'])]

    def validate(self):
        return wbs.validate_work_packages(self.value, self.job)

    def test_current_registered_same_job_input_is_read_without_mutation(self):
        before = copy.deepcopy((self.value, self.job))
        self.assertEqual(self.validate(), self.value['work_packages'])
        self.assertEqual((self.value, self.job), before)

    def test_other_job_and_unknown_input_rejected_before_file_read(self):
        for edit in ({'job_id': 'another-fictional-job'}, {'path': 'unknown.py'}):
            self.value = proposal()
            ref = dict(self.record, job_id=self.job['id']); ref.update(edit)
            self.value['work_packages'][0]['input_artifacts'] = [ref]
            with patch('team_p0_contract.artifact_records') as read:
                with self.assertRaises(ValueError): self.validate()
                read.assert_not_called()

    def test_missing_or_changed_file_and_stale_registry_are_rejected(self):
        (self.root/'existing.py').write_text('fictional = False\n', encoding='utf-8')
        with self.assertRaises(ValueError): self.validate()
        (self.root/'existing.py').unlink()
        with self.assertRaises(ValueError): self.validate()
        self.job['p0_artifacts'] = []
        with self.assertRaises(ValueError): self.validate()

    def test_unsafe_registered_path_is_never_accepted(self):
        for path in ('../outside.py', '.env', '/absolute.py'):
            self.job['p0_artifacts'] = [dict(self.record, path=path)]
            self.value['work_packages'][0]['input_artifacts'] = [dict(self.job['p0_artifacts'][0], job_id=self.job['id'])]
            with self.assertRaises(ValueError): self.validate()

    def test_ambiguous_registry_duplicate_refs_and_invalid_types(self):
        self.job['p0_artifacts'].append(dict(self.record))
        with self.assertRaises(ValueError): self.validate()
        self.job['p0_artifacts'].pop()
        refs = self.value['work_packages'][0]['input_artifacts']
        refs.append(dict(refs[0]))
        with self.assertRaises(ValueError): self.validate()
        for invalid in (None, {}, [dict(refs[0], sha256='not-a-hash')], [dict(refs[0], approved=True)], refs * 11):
            self.value['work_packages'][0]['input_artifacts'] = invalid
            with self.assertRaises(ValueError): self.validate()

    def test_missing_job_context_is_fail_closed(self):
        with self.assertRaises(ValueError): wbs.validate_work_packages(self.value)

    def approved_fixture(self):
        from tests.test_work_packages import WorkPackageStorageTests
        fixture = WorkPackageStorageTests(); self.addCleanup(fixture.doCleanups); fixture.setUp()
        job = fixture.store.update(fixture.job['id'], project=str(self.root), p0_artifacts=[self.record])
        ref = dict(self.record, job_id=job['id'])
        payload = copy.deepcopy(fixture.payload); payload['work_packages'][0]['input_artifacts'] = [ref]
        self.assertEqual(validate_plan(job, payload)['primary_goal'], 'Fictional lookup')
        approval = fixture.engine.new_approval(fixture.planner, 'plan', payload)
        approval = fixture.store.update(approval['id'], status='approved')
        job = fixture.store.update(job['id'], plan_approval_id=approval['id'], requirements_approval_id=approval['id'])
        return fixture, job, approval, payload

    def test_proposal_checks_current_inputs_but_approved_source_retains_approved_version(self):
        fixture, job, approval, payload = self.approved_fixture()
        self.assertEqual(len(wbs.approved_source(job, approval, approval)), 2)
        (self.root/'existing.py').unlink()
        with self.assertRaises(ValueError): validate_plan(job, payload)
        self.assertEqual(len(wbs.approved_source(job, approval, approval)), 2)
        self.assertEqual(validate_plan(job, payload, check_current_inputs=False)['primary_goal'], 'Fictional lookup')

    def test_legitimate_input_edit_and_registry_update_does_not_break_view_or_idempotent_install(self):
        fixture, job, approval, payload = self.approved_fixture()
        mapping = fixture.engine.install_work_packages(job['id'], expected_job_revision=job['state_revision'])
        before = fixture.engine.work_package_view(job['id'])
        (self.root/'existing.py').write_text('fictional = "updated"\n', encoding='utf-8')
        changed = {'path': 'existing.py', 'sha256': hashlib.sha256((self.root/'existing.py').read_bytes()).hexdigest()}
        current = fixture.store.update(job['id'], p0_artifacts=[changed])
        with patch('team_p0_contract.artifact_records', side_effect=AssertionError('Read-only view touched current file')):
            self.assertEqual(fixture.engine.work_package_view(job['id']), before)
            self.assertEqual(fixture.engine.install_work_packages(job['id'], expected_job_revision=current['state_revision']), mapping)
        self.assertEqual(fixture.store.get(approval['id'], 'approval')['payload'], payload)
        with self.assertRaises(ValueError): validate_plan(current, payload)
        with self.assertRaises(ValueError): wbs.validate_input_artifacts(payload['work_packages'][0]['input_artifacts'], current)

    def test_unregistered_or_deleted_current_input_does_not_hide_approved_view(self):
        fixture, job, approval, payload = self.approved_fixture()
        fixture.engine.install_work_packages(job['id'], expected_job_revision=job['state_revision'])
        before = fixture.engine.work_package_view(job['id'])
        (self.root/'existing.py').unlink()
        fixture.store.update(job['id'], p0_artifacts=[])
        self.assertEqual(fixture.engine.work_package_view(job['id']), before)
        with self.assertRaises(ValueError): validate_plan(fixture.store.get(job['id']), payload)

    def test_historical_input_mode_still_rejects_foreign_job_unsafe_paths_and_malformed_refs(self):
        for change in ({'job_id': 'foreign-job'}, {'path': '../outside.py'}, {'path': '/absolute.py'},
                       {'path': 'C:existing.py'}, {'path': './existing.py'}, {'sha256': 'wrong'}, {'extra': True}):
            value = copy.deepcopy(self.value)
            value['work_packages'][0]['input_artifacts'][0].update(change)
            with self.assertRaises(ValueError): wbs.validate_work_packages(value, self.job, check_current_inputs=False)

    def test_readonly_source_still_checks_approval_digest(self):
        fixture, job, approval, payload = self.approved_fixture()
        fixture.engine.install_work_packages(job['id'], expected_job_revision=job['state_revision'])
        changed = copy.deepcopy(payload); changed['work_packages'][0]['input_artifacts'][0]['sha256'] = '0' * 64
        fixture.store.update(approval['id'], payload=changed)
        with self.assertRaises(ValueError): fixture.engine.work_package_view(job['id'])


if __name__ == '__main__': unittest.main()
