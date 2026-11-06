import tempfile
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from eval.development_readiness.reacceptance_fixture import close, make_fixture
from team_p0_contract import initial


class UserRecoveryTests(unittest.TestCase):
    def command_change_question(self):
        old = r'powershell -NoProfile -File .\tests\Run-Tests.ps1'
        new = r'powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\Run-Tests.ps1'
        self.store.update(self.job_id, code_test_plan={'required': True, 'command': old},
                          validation_commands=[old], validation_evidence={'0': {'command': old}})
        approval = self.maintenance_question()
        self.store.update(self.task_id, validation_runs=[{'index': 0, 'exit_code': 1}])
        return approval, {'before': old, 'after': new}

    def test_explicit_command_change_updates_contract_and_invalidates_old_evidence(self):
        from team_code_test_contract import commands
        approval, change = self.command_change_question()
        target = self.target()
        answers = {'q': {'option_id': 'yes', 'text': ''}}
        self.assertEqual(self.engine.user_recovery_question(self.job_id, target)['command_changes'], [change])
        self.engine.user_recovery_answer(self.job_id, target, answers, True, change)
        job = self.store.get(self.job_id)
        self.assertEqual(commands(job), [change['after']])
        self.assertEqual(job['code_test_plan']['command'], change['after'])
        self.assertEqual(job['validation_evidence'], {})
        self.assertEqual(job['validation_command_history'][-1]['before'], change['before'])
        self.assertEqual(job['validation_command_history'][-1]['previous_runs'][0]['exit_code'], 1)
        self.assertEqual(self.store.get(approval['id'])['command_change'], change)
        task = self.store.get(self.task_id)
        self.assertEqual(task['validation_runs'], [])
        self.assertTrue(task['requires_validation_run'])
        self.assertEqual(task['status'], 'queued')
        with self.assertRaises(ValueError):
            self.engine.user_recovery_answer(self.job_id, target, answers, True, change)

    def test_answer_text_alone_does_not_change_verification_command(self):
        approval, change = self.command_change_question()
        self.engine.user_recovery_answer(self.job_id, self.target(),
            {'q': {'option_id': 'yes', 'text': change['after']}}, True)
        job = self.store.get(self.job_id)
        self.assertEqual(job['code_test_plan']['command'], change['before'])
        self.assertNotIn('validation_command_history', job)
        self.assertNotIn('command_change', self.store.get(approval['id']))

    def test_command_change_cannot_replace_script_or_skip_confirmation(self):
        approval, change = self.command_change_question()
        target = self.target()
        answers = {'q': {'option_id': 'yes', 'text': ''}}
        for bad in (dict(change, after=change['after'] + '; Remove-Item x'),
                    dict(change, before='powershell -NoProfile -File .\\other.ps1'),
                    dict(change, after=change['after'].replace('Run-Tests.ps1', 'other.ps1'))):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.engine.user_recovery_answer(self.job_id, target, answers, True, bad)
        with self.assertRaises(ValueError):
            self.engine.user_recovery_answer(self.job_id, target, answers, False, change)
        with self.assertRaises(ValueError):
            self.engine.user_recovery_answer(self.job_id, target, {}, True, change)
        job = self.store.get(self.job_id)
        self.assertEqual(job['code_test_plan']['command'], change['before'])
        self.assertNotIn('validation_command_history', job)
        self.assertEqual(self.store.get(approval['id'])['status'], 'pending')

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.server = make_fixture(Path(temp.name))
        self.addCleanup(self.finish)
        self.engine = self.server.app.engine
        self.store = self.server.app.store
        job = self.store.all('job')[0]
        self.job_id = job['id']
        task = next(t for t in self.store.for_job('task', self.job_id) if t['role'] == 'builder')
        self.task_id = task['id']
        for approval in self.store.for_job('approval', self.job_id):
            self.store.update(approval['id'], status='superseded')
        self.store.update(self.task_id, status='failed', result=None, attempt=1, failure_code='fictional')
        self.store.update(self.job_id, status='blocked', p0_contract_version=1,
                          goal_contract=initial(job['goal']),
                          p0_interruption={'interrupted_task_id': self.task_id, 'resume_target': self.task_id,
                                           'recovery_status': 'maintenance_required',
                                           'failure_signature': 'fictional-signature'})
        planner = self.engine.new_task(self.store.get(self.job_id), '架空の計画', '架空の計画', 'planner')
        planner = self.store.update(planner['id'], status='succeeded',
                                    profile={'adapter': 'codex', 'model': 'gpt-6-sol', 'effort': 'low'})
        self.engine.config['provider_settings']['enabled'] = ['codex', 'claude']
        approval = self.engine.new_approval(planner, 'plan', {'summary': '架空の計画'})
        self.store.update(approval['id'], status='approved')

    def finish(self):
        close(self.server)

    def target(self):
        result = self.engine.user_recovery_target(self.job_id)
        return {'task_id': result['task_id'], 'binding': result['binding']}

    def maintenance_question(self, text='実行環境を確認しますか？'):
        stop = self.store.get(self.job_id)['p0_interruption']
        self.store.update(self.job_id, p0_interruption=dict(stop, recovery_status='retrying',
                                                          interruption_reason='test_temp_scope'))
        task = self.store.update(self.task_id, status='blocked')
        return self.engine.new_approval(task, 'question', {
            'summary': '架空の実行環境で承認の期限が切れました。',
            'questions': [{'id': 'q', 'text': text, 'options': [
                {'id': 'yes', 'label': '確認する', 'input_required': False, 'input_label': ''}]}]})

    def test_explicit_confirmation_retries_once_and_preserves_history(self):
        target = self.target()
        with self.assertRaises(ValueError):
            self.engine.user_recovery_retry(self.job_id, target, False)
        self.assertEqual(self.store.get(self.task_id)['status'], 'failed')
        self.assertEqual(self.engine.user_recovery_retry(self.job_id, target, True), {'ok': True})
        task = self.store.get(self.task_id)
        self.assertEqual(task['status'], 'queued')
        self.assertEqual(task['recovery_history'][-1]['failure_code'], 'fictional')
        self.assertEqual(self.store.get(self.job_id)['p0_interruption']['recovery_status'], 'retrying')
        with self.assertRaises(ValueError):
            self.engine.user_recovery_retry(self.job_id, target, True)

    def test_changed_attempt_invalidates_confirmation(self):
        target = self.target()
        self.store.update(self.task_id, attempt=2)
        with self.assertRaises(ValueError):
            self.engine.user_recovery_retry(self.job_id, target, True)
        self.assertEqual(self.store.get(self.task_id)['status'], 'failed')

    def test_new_operation_invalidates_confirmation(self):
        target = self.target()
        self.store.event('operation_bash', '架空の操作', self.task_id, self.job_id)
        with self.assertRaises(ValueError):
            self.engine.user_recovery_retry(self.job_id, target, True)
        self.assertEqual(self.store.get(self.task_id)['status'], 'failed')

    def test_changed_plan_invalidates_confirmation(self):
        target = self.target()
        plan = next(a for a in self.store.for_job('approval', self.job_id)
                    if a.get('kind') == 'plan')
        self.store.update(plan['id'], note='架空の確認内容変更')
        with self.assertRaises(ValueError):
            self.engine.user_recovery_retry(self.job_id, target, True)
        self.assertEqual(self.store.get(self.task_id)['status'], 'failed')

    def test_recovery_investigation_is_linked_deduplicated_and_requires_ai_approval(self):
        self.store.update(self.task_id, summary='fictional-private-diagnostic-marker')
        target = self.target()
        with self.assertRaises(ValueError):
            self.engine.user_recovery_request(self.job_id, target, False)
        result = self.engine.user_recovery_request(self.job_id, target, True)
        child = self.store.get(result['job_id'], 'job')
        self.assertEqual(child['project'], self.store.get(self.job_id)['project'])
        self.assertIn('采来本体やその運用データではない', child['goal'])
        self.assertEqual(child['status'], 'awaiting_approval')
        self.assertEqual(child['g2_status'], 'pending')
        self.assertTrue(child['recovery_model_locked'])
        expected = {'adapter': 'codex', 'model': 'gpt-6-sol', 'effort': 'low'}
        self.assertEqual(child['role_overrides']['planner'], expected)
        self.assertEqual(child['role_overrides']['researcher'], expected)
        self.assertEqual(child['role_overrides']['builder'], expected)
        child_planner = next(t for t in self.store.for_job('task', child['id']) if t['role'] == 'planner')
        self.assertEqual(child_planner['profile'], expected)
        g2 = self.store.get(child['g2_approval_id'], 'approval')
        self.assertEqual(g2['payload']['fingerprint'], self.engine._g2_fingerprint(child, child_planner)[0])
        self.assertNotIn('fictional-private-diagnostic-marker', child['goal'])
        self.assertEqual(self.store.get(self.task_id)['status'], 'failed')
        current = self.target()
        again = self.engine.user_recovery_request(self.job_id, current, True)
        self.assertEqual(again, {'job_id': child['id'], 'existing': True})
        self.assertEqual(len(self.store.for_job('recovery_case', self.job_id)), 1)
        with self.assertRaises(ValueError):
            self.engine.user_recovery_retry(self.job_id, current, True)
        with self.assertRaises(ValueError):
            self.engine.retry(self.task_id, '')
        self.store.event('fictional_progress', '架空の進捗', job_id=child['id'])
        with self.assertRaises(ValueError):
            self.engine.user_recovery_retry(self.job_id, current, True)

    def test_recovery_never_automatically_changes_the_selected_model(self):
        self.engine.config['p0_contract_enabled'] = False
        result = self.engine.user_recovery_request(self.job_id, self.target(), True)
        child = self.store.get(result['job_id'], 'job')
        task = next(t for t in self.store.for_job('task', child['id']) if t['role'] == 'planner')
        self.assertFalse(self.engine._failover(task, child, 'usage_limit', before_start=True))
        self.assertEqual(self.store.get(task['id'])['profile']['model'], 'gpt-6-sol')

    def test_unknown_failure_investigation_cannot_plan_or_invoke_writes(self):
        from team_adapters import ProviderError
        child_id = self.engine.user_recovery_request(self.job_id, self.target(), True)['job_id']
        child = self.store.get(child_id, 'job')
        self.assertEqual(child['project'], self.store.get(self.job_id)['project'])
        self.assertIn('采来本体やその運用データではない', child['goal'])
        self.assertTrue(child['recovery_readonly'])
        self.assertIn('読み取り専用', child['goal'])
        planner = next(t for t in self.store.for_job('task', child_id) if t['role'] == 'planner')
        plan = {'summary': '架空の計画', 'tasks': [{'role': 'builder', 'title': '修復', 'instruction': '修復'}]}
        with self.assertRaises(ProviderError):
            self.engine._validate(planner, plan)
        with self.assertRaises(ValueError):
            self.engine._expand_plan_body(planner, plan)
        ctx = SimpleNamespace(task=planner)
        for tool in ('Write', 'Edit', 'Bash', 'WebSearch', 'unknown_tool'):
            self.assertFalse(self.engine._tool_request_context(ctx, {'tool': tool})['allow'])
        old_fingerprint = self.engine._g2_fingerprint(child, planner)[0]
        altered = dict(child, recovery_readonly=False)
        self.assertNotEqual(old_fingerprint, self.engine._g2_fingerprint(altered, planner)[0])

    def test_unfinished_recovery_checks_block_original_retry(self):
        result = self.engine.user_recovery_request(self.job_id, self.target(), True)
        self.store.update(result['job_id'], status='accepted_with_pending_checks')
        current = self.engine.user_recovery_target(self.job_id)
        self.assertTrue(current['recovery_in_progress'])
        with self.assertRaises(ValueError):
            self.engine.user_recovery_retry(self.job_id,
                {'task_id': current['task_id'], 'binding': current['binding']}, True)
        self.assertEqual(self.store.get(self.task_id)['status'], 'failed')

    def accepted_investigation(self, summary='架空の一時領域が不足。作成後に確認する。'):
        child_id = self.engine.user_recovery_request(self.job_id, self.target(), True)['job_id']
        child = self.store.get(child_id, 'job')
        researcher = self.engine.new_task(child, '架空の調査', '架空の調査', 'researcher')
        self.store.update(researcher['id'], status='succeeded', summary=summary,
                          result={'status': 'done', 'summary': summary})
        self.store.update(child_id, status='accepted')
        return child_id, researcher['id']

    def test_reviewed_investigation_creates_linked_repair_with_normal_approvals(self):
        investigation, _ = self.accepted_investigation()
        current = self.engine.user_recovery_target(self.job_id)
        self.assertIn('一時領域', current['repair_preview'])
        target = {'task_id': current['task_id'], 'binding': current['binding']}
        with self.assertRaises(ValueError):
            self.engine.user_recovery_repair(self.job_id, target, False)
        child_id = self.engine.user_recovery_repair(self.job_id, target, True)['job_id']
        child = self.store.get(child_id, 'job')
        self.assertEqual(child['project'], self.store.get(self.job_id)['project'])
        self.assertIn('采来本体やその運用データではない', child['goal'])
        self.assertEqual(child['g2_status'], 'pending')
        self.assertFalse(child['recovery_readonly'])
        self.assertIn(current['repair_preview'], child['goal'])
        self.assertIn('未検証', child['goal'])
        self.assertEqual(child['role_overrides']['builder']['model'], 'gpt-6-sol')
        case = next(c for c in self.store.for_job('recovery_case', self.job_id)
                    if c['recovery_job_id'] == child_id)
        self.assertEqual(case['investigation_job_id'], investigation)
        with self.assertRaises(ValueError):
            self.engine.user_recovery_repair(self.job_id, target, True)
        with self.assertRaises(ValueError):
            self.engine.user_recovery_retry(self.job_id, self.target(), True)

    def test_changed_or_sensitive_investigation_cannot_be_used_for_repair(self):
        _, researcher_id = self.accepted_investigation()
        target = self.target()
        self.store.update(researcher_id, summary='架空の報告を変更')
        with self.assertRaises(ValueError):
            self.engine.user_recovery_repair(self.job_id, target, True)
        self.store.update(researcher_id, summary='password=fictional-secret-value')
        current = self.engine.user_recovery_target(self.job_id)
        self.assertIsNone(current['repair_preview'])
        with self.assertRaises(ValueError):
            self.engine.user_recovery_repair(self.job_id, self.target(), True)

    def test_accepted_repair_allows_explicit_retry_without_auto_resuming(self):
        self.accepted_investigation()
        child_id = self.engine.user_recovery_repair(self.job_id, self.target(), True)['job_id']
        self.store.update(child_id, status='accepted')
        self.assertEqual(self.store.get(self.task_id)['status'], 'failed')
        self.engine.user_recovery_retry(self.job_id, self.target(), True)
        self.assertEqual(self.store.get(self.task_id)['status'], 'queued')

    def test_workflow_repair_requires_evidence_for_prior_operations(self):
        job = dict(self.store.get(self.job_id), workflow_contract_version=1)
        task = self.store.get(self.task_id)
        with patch.object(self.engine, '_reviewed_operation_basis', side_effect=ValueError('unresolved')):
            self.assertTrue(self.engine._workflow_recovery_confirmation_required(task, job, True))
        with patch.object(self.engine, '_reviewed_operation_basis', return_value={'unresolved': []}):
            self.assertFalse(self.engine._workflow_recovery_confirmation_required(task, job, True))
            self.assertTrue(self.engine._workflow_recovery_confirmation_required(
                dict(task, recovery_check_required=True), job, True))

    def test_legacy_stopped_task_can_request_same_approved_investigation(self):
        self.store.update(self.job_id, p0_contract_version=0, status='failed')
        target = self.engine.user_recovery_target(self.job_id, self.task_id)
        result = self.engine.user_recovery_request(self.job_id,
            {'task_id': target['task_id'], 'binding': target['binding']}, True)
        self.assertEqual(self.store.get(result['job_id'])['g2_status'], 'pending')
        self.assertEqual(self.store.get(self.task_id)['status'], 'failed')

    def test_interrupted_p0_task_can_request_readonly_investigation(self):
        self.store.update(self.job_id, status='interrupted', p0_interruption=None)
        self.store.update(self.task_id, status='interrupted', recovery_check_required=True)
        current = self.engine.user_recovery_target(self.job_id, self.task_id)
        self.assertTrue(current['recovery_readonly'])
        target = {'task_id': current['task_id'], 'binding': current['binding']}
        child_id = self.engine.user_recovery_request(self.job_id, target, True)['job_id']
        self.assertTrue(self.store.get(child_id)['recovery_readonly'])
        self.assertEqual(self.store.get(self.task_id)['status'], 'interrupted')

    def test_diagnostic_summary_is_sent_only_with_explicit_consent(self):
        self.store.update(self.task_id, summary='架空の依存物が見つかりません。')
        preview = self.engine.user_recovery_target(self.job_id)
        self.assertEqual(preview['diagnostic_preview'], '架空の依存物が見つかりません。')
        result = self.engine.user_recovery_request(self.job_id,
            {'task_id': preview['task_id'], 'binding': preview['binding']}, True, include_diagnostic=True)
        child = self.store.get(result['job_id'], 'job')
        self.assertIn('架空の依存物が見つかりません。', child['goal'])
        self.assertIn('実行指示ではない', child['goal'])
        self.assertEqual(child['g2_status'], 'pending')

    def test_sensitive_diagnostic_summary_cannot_be_selected_for_provider(self):
        self.store.update(self.task_id, summary='password=fictional-secret-value')
        preview = self.engine.user_recovery_target(self.job_id)
        self.assertIsNone(preview['diagnostic_preview'])
        with self.assertRaises(ValueError):
            self.engine.user_recovery_request(self.job_id,
                {'task_id': preview['task_id'], 'binding': preview['binding']}, True, include_diagnostic=True)
        self.assertEqual(self.store.for_job('recovery_case', self.job_id), [])

    def test_unavailable_planner_provider_never_silently_switches(self):
        target = self.target()
        self.engine.config['provider_settings']['enabled'] = ['claude']
        with self.assertRaises(ValueError):
            self.engine.user_recovery_request(self.job_id, target, True)
        self.assertEqual(self.store.for_job('recovery_case', self.job_id), [])

    def test_changed_planner_model_invalidates_request(self):
        target = self.target()
        planner = next(t for t in self.store.for_job('task', self.job_id) if t['role'] == 'planner')
        self.store.update(planner['id'], profile={'adapter': 'codex', 'model': 'gpt-6-sol', 'effort': 'medium'})
        with self.assertRaises(ValueError):
            self.engine.user_recovery_request(self.job_id, target, True)
        self.assertEqual(self.store.for_job('recovery_case', self.job_id), [])

    def test_invalid_plan_planner_binding_does_not_guess_a_model(self):
        plan = next(a for a in self.store.for_job('approval', self.job_id) if a.get('kind') == 'plan')
        self.store.update(plan['id'], task_id=self.task_id)
        target = self.engine.user_recovery_target(self.job_id)
        self.assertIsNone(target['recovery_planner_profile'])
        with self.assertRaises(ValueError):
            self.engine.user_recovery_request(self.job_id,
                {'task_id': target['task_id'], 'binding': target['binding']}, True)
        self.assertEqual(self.store.for_job('recovery_case', self.job_id), [])

    def test_security_failure_cannot_be_retried_or_assigned_to_recovery(self):
        self.store.update(self.task_id, failure_code='egress_blocked')
        with self.assertRaises(ValueError):
            self.target()

    def test_other_running_task_blocks_recovery(self):
        other = next(t for t in self.store.for_job('task', self.job_id) if t['id'] != self.task_id)
        self.store.update(other['id'], status='running')
        with self.assertRaises(ValueError):
            self.target()

    def test_safety_hold_and_pending_approval_cannot_be_retried(self):
        stop = self.store.get(self.job_id)['p0_interruption']
        self.store.update(self.job_id, p0_interruption=dict(stop, recovery_status='safety_blocked'))
        with self.assertRaises(ValueError):
            self.target()
        self.store.update(self.job_id, p0_interruption=stop)
        task = self.store.get(self.task_id)
        self.engine.new_approval(task, 'question', {'summary': '架空の確認', 'questions': []})
        with self.assertRaises(ValueError):
            self.target()

    def test_maintenance_question_can_be_investigated_but_not_bypassed(self):
        task = self.store.get(self.task_id)
        tool = self.engine.new_approval(task, 'tool', {'operation': 'Write', 'details': {}})
        self.store.update(tool['id'], status='expired')
        approval = self.maintenance_question()
        self.assertEqual(approval['intended_role'], 'maintainer')
        target = self.engine.user_recovery_target(self.job_id)
        self.assertTrue(target['pending_maintenance_question'])
        short = {'task_id': target['task_id'], 'binding': target['binding']}
        with self.assertRaises(ValueError):
            self.engine.user_recovery_retry(self.job_id, short, True)
        child_id = self.engine.user_recovery_request(self.job_id, short, True)['job_id']
        goal = self.store.get(child_id, 'job')['goal']
        self.assertIn('追加の確認事項', goal)
        self.assertIn('期限切れ1件', goal)
        self.assertNotIn('テスト用一時領域を使用できなかった', goal)
        self.assertNotIn('承認の期限が切れました', goal)
        self.assertEqual(self.store.get(approval['id'])['status'], 'pending')
        with self.assertRaises(ValueError):
            self.engine.decide(approval['id'], True, '', {'q': {'option_id': 'yes', 'text': ''}})
        self.assertEqual(self.store.get(approval['id'])['status'], 'pending')

    def test_user_can_review_and_answer_safe_maintenance_question(self):
        approval = self.maintenance_question()
        target = self.target()
        question = self.engine.user_recovery_question(self.job_id, target)
        self.assertTrue(question['can_answer'])
        self.assertEqual(question['approval_id'], approval['id'])
        answers = {'q': {'option_id': 'yes', 'text': ''}}
        with self.assertRaises(ValueError):
            self.engine.user_recovery_answer(self.job_id, target, answers, False)
        self.assertEqual(self.store.get(approval['id'])['status'], 'pending')
        self.assertEqual(self.engine.user_recovery_answer(self.job_id, target, answers, True), {'ok': True})
        self.assertEqual(self.store.get(approval['id'])['status'], 'approved')
        self.assertEqual(self.store.get(self.task_id)['status'], 'queued')
        public_task = next(t for t in self.server.app.public_state()['tasks'] if t['id'] == self.task_id)
        self.assertEqual(public_task['title'], '担当工程')
        self.assertEqual(public_task['summary'], '')
        with self.assertRaises(ValueError):
            self.engine.user_recovery_answer(self.job_id, target, answers, True)

    def test_cancelled_investigation_allows_user_to_answer_original_question(self):
        self.maintenance_question()
        child_id = self.engine.user_recovery_request(self.job_id, self.target(), True)['job_id']
        pending = self.target()
        self.assertFalse(self.engine.user_recovery_question(self.job_id, pending)['can_answer'])
        with self.assertRaises(ValueError):
            self.engine.user_recovery_answer(self.job_id, pending, {'q': {'option_id': 'yes', 'text': ''}}, True)
        self.engine.cancel_job(child_id)
        current = self.target()
        self.assertTrue(self.engine.user_recovery_question(self.job_id, current)['can_answer'])
        self.engine.user_recovery_answer(self.job_id, current, {'q': {'option_id': 'yes', 'text': ''}}, True)
        self.assertEqual(self.store.get(self.task_id)['status'], 'queued')

    def test_cancelled_investigation_can_be_requested_again(self):
        first = self.engine.user_recovery_request(self.job_id, self.target(), True)['job_id']
        self.engine.cancel_job(first)
        second = self.engine.user_recovery_request(self.job_id, self.target(), True)['job_id']
        self.assertNotEqual(first, second)
        self.assertEqual(self.store.get(second)['g2_status'], 'pending')

    def test_cancelled_repair_does_not_disable_readonly_on_new_investigation(self):
        self.accepted_investigation()
        repair = self.engine.user_recovery_repair(self.job_id, self.target(), True)['job_id']
        self.engine.cancel_job(repair)
        new_id = self.engine.user_recovery_request(self.job_id, self.target(), True)['job_id']
        self.assertTrue(self.store.get(new_id)['recovery_readonly'])

    def test_sensitive_maintenance_question_is_not_exposed(self):
        self.maintenance_question('サンドボックスのパスワードを入力してください。')
        target = self.target()
        with self.assertRaises(ValueError):
            self.engine.user_recovery_question(self.job_id, target)
        self.assertEqual(self.store.get(self.task_id)['status'], 'blocked')

    def test_sensitive_answer_is_not_stored_or_sent(self):
        approval = self.maintenance_question()
        target = self.target()
        answers = {'q': {'option_id': 'yes', 'text': 'password=fictional-secret-value'}}
        with self.assertRaises(ValueError):
            self.engine.user_recovery_answer(self.job_id, target, answers, True)
        saved = self.store.get(approval['id'])
        self.assertEqual(saved['status'], 'pending')
        self.assertNotIn('fictional-secret-value', json.dumps(saved))

    def test_recovery_question_cannot_transfer_scope(self):
        stop = self.store.get(self.job_id)['p0_interruption']
        self.store.update(self.job_id, p0_interruption=dict(stop, recovery_status='retrying'))
        task = self.store.update(self.task_id, status='blocked')
        self.engine.new_approval(task, 'question', {'summary': '架空の実行環境の確認',
            'questions': [{'id': 'handoff_scope_decision', 'text': '後続に移しますか？', 'options': [
                {'id': 'leave', 'label': '移す', 'input_required': False, 'input_label': ''}]}]})
        target = self.target()
        with self.assertRaises(ValueError):
            self.engine.user_recovery_question(self.job_id, target)

    def test_question_change_invalidates_user_confirmation(self):
        approval = self.maintenance_question()
        target = self.target()
        self.store.update(approval['id'], note='架空の変更')
        with self.assertRaises(ValueError):
            self.engine.user_recovery_answer(self.job_id, target, {'q': {'option_id': 'yes', 'text': ''}}, True)
        self.assertEqual(self.store.get(approval['id'])['status'], 'pending')

    def test_browser_session_required_and_existing_maintenance_route_remains_protected(self):
        from server import Handler
        self.server.RequestHandlerClass = Handler
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), thread.join(timeout=3)))
        origin = self.server.app.origin
        cookie = 'agent_team=' + self.server.app.cookie

        def call(path, body=None, authenticated=True):
            headers = {'Content-Type': 'application/json', 'X-Agent-Team-UI': '1', 'Origin': origin}
            if authenticated:
                headers['Cookie'] = cookie
            request = Request(origin + path, data=json.dumps(body).encode() if body is not None else None,
                              headers=headers, method='POST' if body is not None else 'GET')
            try:
                with urlopen(request, timeout=5) as response:
                    return response.status, json.load(response)
            except HTTPError as error:
                with error:
                    return error.code, json.load(error)

        url = '/api/recovery/user-target?job_id=' + self.job_id
        self.assertEqual(call(url, authenticated=False)[0], 403)
        status, target = call(url)
        self.assertEqual(status, 200)
        self.assertNotIn('failure_signature', target)
        self.assertEqual(call('/api/retry', {'id': self.task_id, 'checked_changes': True})[0], 403)
        payload = {'job_id': self.job_id,
                   'target': {'task_id': target['task_id'], 'binding': target['binding']}, 'confirmed': False}
        self.assertEqual(call('/api/recovery/user-repair', payload, authenticated=False)[0], 403)
        self.assertEqual(call('/api/recovery/user-repair', payload)[0], 400)
        self.assertEqual(call('/api/recovery/user-retry', payload)[0], 400)
        self.assertEqual(self.store.get(self.task_id)['status'], 'failed')
        payload['confirmed'] = True
        self.assertEqual(call('/api/recovery/user-retry', payload)[0], 200)
        self.assertEqual(self.store.get(self.task_id)['status'], 'queued')


if __name__ == '__main__':
    unittest.main()
