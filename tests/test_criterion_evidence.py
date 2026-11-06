"""Requirement coverage and two-stage review flow, without paid model calls."""
import copy
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from team_adapters import schema_errors
from team_config import default_config
from team_consultation import Consultations
from team_engine import Engine, RESULT_SCHEMA_CRITERIA, REQUIREMENT_FIDELITY_RULE
from team_p0_contract import artifact_records, criterion_review, criterion_review_findings, validate_plan
from team_store import Store


CAPITAL = '都道府県を検索すると県庁所在地を表示する'
MAP = '検索結果の下に日本地図を表示し対象の都道府県だけ色を変える'
PRODUCT = '対象の県の特産品を1つ吹き出しに表示する'
REPORT = {
    'status': 'done', 'summary': '条件別の確認結果です。',
    'checks': ['現行版を確認。'], 'question': '', 'questions': [],
    'context_updates': [], 'pending_checks': [], 'pending_check_details': [],
    'warnings': [], 'stage_complete': True, 'blocking_reasons': [],
}


def report(criteria):
    return dict(copy.deepcopy(REPORT), acceptance_results=[
        {'criterion': criterion, 'outcome': 'passed',
         'evidence': 'test_lookup: 対象入力に対する現行版の期待値と観測結果を照合。'}
        for criterion in criteria])


class CriterionEvidenceTests(unittest.TestCase):
    def test_plan_must_assign_every_condition_without_changing_legacy_plans(self):
        plan = {
            'goal_contract': {'primary_goal': '検索Webアプリ', 'deliverables': ['Webページ'],
                              'acceptance_criteria': [CAPITAL, MAP, PRODUCT],
                              'non_goals': [], 'constraints': []},
            'tasks': [{'title': '実装', 'instruction': '実装する', 'role': 'builder',
                       'goal_links': ['Webページ', CAPITAL]}],
            'code_test_plan': {'required': True, 'reason': '変更', 'cases': ['検索'],
                               'command': 'python -m unittest', 'existing_tests': []},
        }
        job = {'goal': '検索と地図'}
        self.assertEqual(validate_plan(job, plan)['acceptance_criteria'], [CAPITAL, MAP, PRODUCT])
        job['criterion_evidence_version'] = 1
        with self.assertRaisesRegex(ValueError, '受入条件を担当する作業'):
            validate_plan(job, plan)
        plan['tasks'][0]['goal_links'] += [MAP, PRODUCT]
        self.assertEqual(validate_plan(job, plan)['acceptance_criteria'], [CAPITAL, MAP, PRODUCT])

    def test_empty_general_test_claim_does_not_cover_any_condition(self):
        issues = criterion_review_findings([CAPITAL, MAP, PRODUCT], REPORT)
        self.assertEqual([x['requirement'] for x in issues], [CAPITAL, MAP, PRODUCT])

    def test_failed_map_is_not_passed_despite_other_successes(self):
        result = report([CAPITAL, MAP, PRODUCT])
        result['acceptance_results'][1].update(
            outcome='failed', evidence='地図が県境のある形ではなく、四角いタイル一覧になっている。')
        self.assertEqual(criterion_review_findings([CAPITAL, MAP, PRODUCT], result), [
            {'requirement': MAP, 'impact': '地図が県境のある形ではなく、四角いタイル一覧になっている。'}])

    def test_duplicate_unknown_empty_and_invalid_results_are_rejected(self):
        valid = report([CAPITAL])['acceptance_results'][0]
        for rows in ([valid, valid], [dict(valid, criterion=MAP)],
                     [dict(valid, evidence=' ')], [dict(valid, outcome='unknown')],
                     [dict(valid, criterion=[])], [dict(valid, extra=True)],
                     'not a list'):
            with self.subTest(rows=rows):
                self.assertTrue(criterion_review_findings([CAPITAL], dict(REPORT, acceptance_results=rows)))

    def test_human_check_requires_cited_pending_details_and_is_not_passed(self):
        result = report([MAP])
        result['acceptance_results'][0].update(outcome='user_pending', evidence='利用者の実画面確認待ち。')
        self.assertTrue(criterion_review_findings([MAP], result))
        result.update(pending_checks=[MAP], pending_check_details=[{
            'check': MAP, 'source_type': 'approved_plan', 'source_excerpt': MAP,
            'owner': 'user', 'required': True, 'feasibility': 'feasible'}])
        self.assertEqual(criterion_review_findings([MAP], result), [])
        result['pending_check_details'][0]['source_excerpt'] = '計画にない条件'
        self.assertTrue(criterion_review_findings([MAP], result))
        result['pending_check_details'][0].update(source_type='request', source_excerpt=MAP)
        self.assertEqual(criterion_review_findings([MAP], result, request=MAP), [])
        result['pending_check_details'][0]['owner'] = 'ai'
        self.assertTrue(criterion_review_findings([MAP], result, request=MAP))

    def test_same_condition_cannot_be_passed_and_pending(self):
        self.assertTrue(criterion_review_findings([CAPITAL], dict(report([CAPITAL]), pending_checks=[CAPITAL])))

    def test_pending_instructions_link_through_exact_approved_criterion(self):
        result = report([CAPITAL])
        result['acceptance_results'][0]['outcome'] = 'user_pending'
        detail = {'check': '利用者がページを起動して検索し、表示された所在地を確認する。',
                  'source_type': 'approved_plan', 'source_excerpt': CAPITAL,
                  'required': True, 'owner': 'user', 'feasibility': 'feasible'}
        result.update(pending_checks=[CAPITAL], pending_check_details=[detail])
        self.assertEqual(criterion_review_findings([CAPITAL], result), [])
        for changes in ({'source_excerpt': '検索'}, {'source_type': 'request'},
                        {'owner': 'ai'}, {'required': False}, {'feasibility': 'unavailable'},
                        {'check': ''}):
            with self.subTest(changes=changes):
                result['pending_check_details'] = [dict(detail, **changes)]
                self.assertTrue(criterion_review_findings([CAPITAL], result, request=CAPITAL))
        result['pending_check_details'] = [detail]
        result['pending_checks'] = []
        self.assertTrue(criterion_review_findings([CAPITAL], result))

    def test_new_review_schema_is_strict_without_backfilling_evidence(self):
        self.assertTrue(schema_errors(REPORT, RESULT_SCHEMA_CRITERIA))
        self.assertEqual(schema_errors(report([CAPITAL]), RESULT_SCHEMA_CRITERIA), [])
        self.assertFalse(criterion_review({}, {'role': 'reviewer'}))
        self.assertFalse(criterion_review({'criterion_evidence_version': 1},
                                          {'role': 'reviewer', 'agent_name': 'common-security-reviewer'}))
        self.assertIn('タイル一覧', REQUIREMENT_FIDELITY_RULE)


class ReviewFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'runtime')
        config = default_config()
        config.update(approved_roots=[str(self.root)], security_review='planner')
        self.engine = Engine(self.store, config, {}, 'http://127.0.0.1:0')
        self.engine.paused = True
        self.addCleanup(self.engine.handoff.close)
        self.addCleanup(self.store.db.close)
        (self.root / 'lookup.py').write_text('# fictional fixture\n', encoding='utf-8')

    def fixture(self, ident, criteria):
        job = self.store.put('job', {
            'id': ident, 'title': '検索Webアプリ', 'goal': '。'.join(criteria),
            'project': str(self.root), 'status': 'running', 'auto_execute': False,
            'created_at': 1, 'p0_contract_version': 1, 'criterion_evidence_version': 1,
            'goal_contract': {'original_request': '。'.join(criteria), 'primary_goal': '検索',
                              'deliverables': ['Webページ'], 'acceptance_criteria': criteria,
                              'non_goals': [], 'constraints': [], 'completion_status': 'in_progress'},
            'p0_artifacts': artifact_records(str(self.root), ['lookup.py']),
        })
        builder = self.engine.new_task(job, '実装', '検索を実装', 'builder')
        self.store.update(builder['id'], status='succeeded', result=copy.deepcopy(REPORT))
        self.engine._queue_reviews(job, builder['id'])
        reviewer = next(t for t in self.store.for_job('task', job['id']) if t['role'] == 'reviewer')
        return job, reviewer

    def test_new_requests_enable_guard_and_old_jobs_are_not_migrated(self):
        created = self.engine.create_job('新規検索', CAPITAL, str(self.root), False)
        self.assertEqual(created['criterion_evidence_version'], 1)
        self.assertEqual(created['status'], 'awaiting_approval')
        self.assertFalse(self.engine.active)
        self.assertTrue(all(t['role'] == 'planner' and t['status'] == 'queued'
                            for t in self.store.for_job('task', created['id'])))

    def test_two_stage_request_requires_map_fix_before_acceptance(self):
        first, review = self.fixture('first', [CAPITAL])
        self.engine._finish(review, report([CAPITAL]))
        self.assertEqual(self.store.get(first['id'])['status'], 'awaiting_acceptance')
        second, review = self.fixture('second', [CAPITAL, MAP, PRODUCT])
        result = report([CAPITAL, MAP, PRODUCT])
        result['acceptance_results'][1].update(
            outcome='failed', evidence='四角いタイルのみ。日本地図と対象県の連動表示へ修正する。')
        self.engine._validate(review, result)
        self.engine._finish(review, result)
        self.assertFalse([a for a in self.store.for_job('approval', second['id']) if a['kind'] == 'completion'])
        fix = next(t for t in self.store.for_job('task', second['id']) if t['role'] == 'builder' and t['repair'] == 1)
        self.assertIn(MAP, fix['instruction'])
        self.assertIn('やり直し', fix['instruction'])
        fixed_review = next(t for t in self.store.for_job('task', second['id']) if t.get('review_cycle') == fix['id'])
        self.store.update(fix['id'], status='succeeded', result=copy.deepcopy(REPORT))
        self.engine._finish(fixed_review, report([CAPITAL, MAP, PRODUCT]))
        self.assertEqual(self.store.get(second['id'])['status'], 'awaiting_acceptance')
        self.assertEqual(self.store.get(first['id'])['status'], 'awaiting_acceptance')
        approval = next(a for a in self.store.for_job('approval', second['id']) if a['kind'] == 'completion')
        self.assertTrue(any(MAP in check for check in approval['payload']['checks']))
        self.assertEqual(approval['status'], 'pending')

    def test_missing_criterion_report_enters_existing_bounded_repair(self):
        job, reviewer = self.fixture('missing', [CAPITAL, MAP])
        self.engine._finish(reviewer, report([CAPITAL]))
        self.assertEqual(self.store.get(reviewer['id'])['result']['status'], 'needs_changes')
        self.assertTrue(any(t['role'] == 'builder' and t['repair'] == 1
                            for t in self.store.for_job('task', job['id'])))

    def test_prose_pending_check_does_not_create_implementation_repair(self):
        job, reviewer = self.fixture('prose-pending', [CAPITAL])
        planner = self.engine.new_task(job, '計画', '計画', 'planner')
        self.store.update(planner['id'], status='succeeded', result={
            'goal_contract': job['goal_contract'], 'tasks': []})
        approval = self.engine.new_approval(planner, 'plan', {'summary': '承認済み計画'})
        self.store.update(approval['id'], status='approved')
        result = report([CAPITAL])
        result['acceptance_results'][0]['outcome'] = 'user_pending'
        result.update(pending_checks=[CAPITAL], pending_check_details=[{
            'check': 'ブラウザで検索操作をして結果を確認する。',
            'source_type': 'approved_plan', 'source_excerpt': CAPITAL,
            'owner': 'user', 'required': True, 'feasibility': 'feasible'}])
        self.engine._finish(reviewer, result)
        self.assertEqual(self.store.get(job['id'])['status'], 'awaiting_acceptance')
        self.assertFalse(any(t['repair'] for t in self.store.for_job('task', job['id'])))
        self.assertEqual(self.store.get(reviewer['id'])['result']['status'], 'done')
        completion = next(a for a in self.store.for_job('approval', job['id']) if a['kind'] == 'completion')
        self.assertEqual(completion['status'], 'pending')
        self.assertTrue(any(CAPITAL in item['checks'] and item.get('source_type') == 'approved_plan'
                            for item in completion['payload']['pending_items']))
        self.assertFalse(completion['payload']['reference_items'])

    def test_gate_only_saved_rejection_is_rechecked_without_ai_or_acceptance(self):
        job, reviewer = self.fixture('saved-rejection', [CAPITAL])
        result = report([CAPITAL])
        result['acceptance_results'][0]['outcome'] = 'user_pending'
        result.update(pending_checks=[CAPITAL], pending_check_details=[{
            'check': 'ブラウザで検索操作をして結果を確認する。',
            'source_type': 'approved_plan', 'source_excerpt': CAPITAL,
            'owner': 'user', 'required': True, 'feasibility': 'feasible'}])
        impact = '利用者による確認待ちの出典・担当・未確認項目がありません。'
        rejected = dict(result, status='needs_changes',
                        summary='（采来による受入条件確認）' + CAPITAL + ': ' + impact + '\n元のレビュー: ' + result['summary'],
                        checks=result['checks'] + [impact],
                        blocking_reasons=[{'requirement': CAPITAL, 'impact': impact}])
        self.store.update(reviewer['id'], status='succeeded', result=rejected, summary=rejected['summary'])
        self.store.update(job['id'], status='blocked')
        self.store.event('criterion_evidence_failed', '保存済み判定', reviewer['id'], job['id'])
        with self.assertRaises(ValueError):
            self.engine.recheck_saved_criterion_report(reviewer['id'], False)
        with patch.object(self.engine, '_run', side_effect=AssertionError('No AI dispatch')):
            recovered = self.engine.recheck_saved_criterion_report(reviewer['id'], True)
        self.assertEqual(recovered['status'], 'awaiting_acceptance')
        self.assertEqual(self.store.get(reviewer['id'])['review_result_history'][-1]['result'], rejected)
        self.assertEqual(self.store.get(reviewer['id'])['attempt'], reviewer['attempt'])
        with self.assertRaises(ValueError):
            self.engine.recheck_saved_criterion_report(reviewer['id'], True)

    def test_worker_dispatch_requests_condition_schema_and_preserves_receipt_boundary(self):
        job, reviewer = self.fixture('dispatch', [CAPITAL, MAP, PRODUCT])
        seen = []

        class Fixture:
            def run(inner, ctx, prompt, schema):
                seen.append((prompt, schema))
                return report([CAPITAL, MAP, PRODUCT])

        self.engine.commands = {reviewer['profile']['adapter']: ['fixture-never-executed']}
        with patch.dict('team_engine.ADAPTERS', {reviewer['profile']['adapter']: Fixture}):
            self.engine._run(reviewer)
        self.assertEqual(len(seen), 1)
        self.assertIn('acceptance_results', seen[0][1]['required'])
        self.assertIn('【条件別レビュー】', seen[0][0])
        self.assertIn(MAP, seen[0][0])
        self.assertEqual(self.store.get(reviewer['id'])['status'], 'succeeded')
        self.assertEqual(self.store.get(job['id'])['status'], 'awaiting_acceptance')

    def test_guard_does_not_consume_user_questions_as_repair(self):
        job, reviewer = self.fixture('question', [CAPITAL])
        result = dict(report([CAPITAL]), status='blocked', questions=[{
            'id': 'choice', 'text': '表示方法を選んでください。', 'options': [
                {'id': 'a', 'label': '方式A', 'input_required': False, 'input_label': ''}]}])
        self.engine._finish(reviewer, result)
        self.assertEqual(self.store.get(job['id'])['status'], 'blocked')
        self.assertTrue(any(a['kind'] == 'question' for a in self.store.for_job('approval', job['id'])))

    def test_existing_request_is_not_retroactively_required_to_use_new_report(self):
        job, reviewer = self.fixture('legacy', [CAPITAL])
        self.store.update(job['id'], criterion_evidence_version=None)
        self.engine._finish(reviewer, copy.deepcopy(REPORT))
        self.assertEqual(self.store.get(job['id'])['status'], 'awaiting_acceptance')

    def test_package_reviews_use_package_conditions_not_unfinished_job_features(self):
        job, reviewer = self.fixture('package', [CAPITAL, MAP, PRODUCT])
        scoped = dict(reviewer, work_package_id='feature')
        with patch.object(self.store, 'get', return_value={'done_when': [CAPITAL]}) as read:
            self.assertEqual(self.engine._review_criteria(job, scoped), [CAPITAL])
            read.assert_called_once_with('feature', 'work_package')
        self.assertEqual(self.engine._review_criteria(job, reviewer), [CAPITAL, MAP, PRODUCT])

    def test_registered_artifact_revision_requires_valid_current_builder_proof(self):
        job, reviewer = self.fixture('artifact-version', [CAPITAL])
        builder = self.store.get(reviewer['review_cycle'], 'task')
        old = copy.deepcopy(job['p0_artifacts'])
        (self.root / 'lookup.py').write_text('# fictional repaired version\n', encoding='utf-8')
        actual = artifact_records(str(self.root), ['lookup.py'])
        self.store.update(job['id'], validation_commands=['python -m unittest'],
                          validation_evidence={'0': {'task_id': builder['id'], 'snapshot': {
                              'files': {'lookup.py': actual[0]['sha256']}}}})
        current = self.store.get(job['id'])
        with patch('team_validation_gate.findings', return_value=['invalidated evidence']):
            self.assertEqual(self.engine._refresh_validated_artifacts(current, reviewer)['p0_artifacts'], old)
        with patch('team_validation_gate.findings', return_value=[]):
            updated = self.engine._refresh_validated_artifacts(current, reviewer)
        self.assertEqual(updated['p0_artifacts'], actual)
        self.assertEqual(updated['artifact_revision_history'][-1]['previous'], old)
        self.assertEqual(updated['artifact_revision_history'][-1]['validation_task_id'], builder['id'])
        self.assertEqual(updated['status'], 'running')

    def test_artifact_revision_never_refreshes_unproven_or_external_changes(self):
        job, reviewer = self.fixture('artifact-external', [CAPITAL])
        old = copy.deepcopy(job['p0_artifacts'])
        (self.root / 'lookup.py').write_text('# fictional external edit\n', encoding='utf-8')
        self.store.update(job['id'], validation_commands=['python -m unittest'],
                          validation_evidence={'0': {'task_id': reviewer['review_cycle'],
                                                   'snapshot': {'files': {'lookup.py': old[0]['sha256']}}}})
        with patch('team_validation_gate.findings', return_value=[]):
            updated = self.engine._refresh_validated_artifacts(self.store.get(job['id']), reviewer)
        self.assertEqual(updated['p0_artifacts'], old)
        self.assertNotIn('artifact_revision_history', updated)

    def test_saved_artifact_rejection_requires_confirmation_and_preserves_report(self):
        job, reviewer = self.fixture('artifact-saved', [CAPITAL])
        preceding = self.store.put('task', {
            'id': 'artifact-saved-prior-review', 'job_id': job['id'], 'role': 'reviewer',
            'status': 'succeeded', 'result': report([CAPITAL]),
            'review_cycle': reviewer['review_cycle'], 'after': reviewer['review_cycle'],
            'created_at': reviewer['created_at'] + 100})
        self.store.update(reviewer['id'], after=preceding['id'])
        impact = '完成物が確認後に変更されています。'
        rejected = dict(report([CAPITAL]), status='needs_changes',
                        summary='保存済みレビュー。\n' + impact,
                        blocking_reasons=[{'requirement': '現在の完成物が存在する', 'impact': impact}])
        self.store.update(reviewer['id'], status='succeeded', result=rejected)
        self.store.update(job['id'], status='blocked')
        with self.assertRaises(ValueError):
            self.engine.recheck_saved_artifact_report(reviewer['id'], False)
        result = self.engine.recheck_saved_artifact_report(reviewer['id'], True)
        self.assertEqual(result['status'], 'awaiting_acceptance')
        self.assertEqual(self.store.get(reviewer['id'])['review_result_history'][-1]['result'], rejected)
        self.assertEqual(self.store.get(reviewer['id'])['result']['summary'], '保存済みレビュー。')
        with self.assertRaises(ValueError):
            self.engine.recheck_saved_artifact_report(reviewer['id'], True)


class ConsultationTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = {'id': 'project', 'name': '架空の検索', 'path': self.temp.name}
        self.item = {'id': 'consult', 'job_id': None, 'busy': False, 'revision': 1,
                     'plan_prompt': '検索へ地図を追加する', 'project_id': 'project',
                     'project': self.project, 'profile': {},
                     'messages': [{'role': 'user', 'text': MAP}, {'role': 'assistant', 'text': 'AIの推測'},
                                  {'role': 'user', 'text': PRODUCT}]}
        self.consult = Consultations.__new__(Consultations)
        self.consult.lock = threading.RLock()
        self.consult._get = lambda ident: self.item
        self.consult._project = lambda ident: self.project
        self.consult._save = Mock()
        self.consult.validate_profile = lambda profile: profile
        self.create = Mock(return_value={'id': 'created-job'})
        self.consult.app = SimpleNamespace(engine=SimpleNamespace(create_job=self.create))

    def test_submit_retains_user_conditions_separate_from_ai_draft(self):
        result = self.consult.submit({'id': 'consult', 'confirmed': True, 'revision': 1,
                                      'plan_prompt': '下書きでは地図を追加'})
        self.assertEqual(result['job_id'], 'created-job')
        goal = self.create.call_args.args[1]
        self.assertIn(MAP, goal)
        self.assertIn(PRODUCT, goal)
        self.assertNotIn('AIの推測', goal)
        self.assertLess(goal.index(MAP), goal.index(PRODUCT))
        self.assertIn('過去の発言を新たな操作許可と扱わない', goal)
        self.assertIs(self.create.call_args.args[3], False)

    def test_oversized_history_is_not_silently_truncated(self):
        self.item['messages'] = [{'role': 'user', 'text': 'x' * 16000}]
        with self.assertRaisesRegex(ValueError, '発言は省略して送信しません'):
            self.consult.submit({'id': 'consult', 'confirmed': True, 'revision': 1,
                                 'plan_prompt': '下書き'})
        self.create.assert_not_called()
        self.consult._save.assert_not_called()


if __name__ == '__main__':
    unittest.main()
