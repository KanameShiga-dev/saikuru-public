"""Offline code-test planning, reuse, and pre-review gate checks."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from team_code_test_contract import commands, snapshot_files, validate_plan, verify_snapshot, _auth_test_required, process_bypass_command
from team_adapters import schema_errors
from team_config import default_config
from team_engine import Context, Engine, PLAN_SCHEMA_TESTS, RESULT_SCHEMA_TESTS
from team_store import Store


COMMAND = 'python -B -X utf8 -m unittest test_calc'
PLAN = {'required': True, 'reason': '架空の計算コードを変更する',
        'cases': ['二つの数量を正しく足す'], 'command': COMMAND, 'existing_tests': []}
DONE = {'status': 'done', 'summary': '架空の作業が完了', 'checks': [],
        'pending_checks': [], 'pending_check_details': [], 'question': '', 'questions': [], 'context_updates': [],
        'warnings': [], 'stage_complete': True, 'blocking_reasons': [],
        'test_files': ['test_calc.py'], 'test_change_reason': ''}
REVIEW = {key: value for key, value in DONE.items() if key not in ('test_files', 'test_change_reason')}


class CodeTestContractTests(unittest.TestCase):
    def test_process_bypass_proposal_is_limited_to_same_relative_script(self):
        command = r'powershell -NoProfile -File .\tests\Run-Tests.ps1'
        self.assertEqual(process_bypass_command(command),
                         r'powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\Run-Tests.ps1')
        for command in (command + '; echo x', 'Set-ExecutionPolicy RemoteSigned',
                        r'powershell -NoProfile -File ..\outside.ps1',
                        r'powershell -NoProfile -File .\..\outside.ps1',
                        r'powershell -NoProfile -File C:\outside.ps1',
                        r'powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\Run-Tests.ps1'):
            with self.subTest(command=command):
                self.assertIsNone(process_bypass_command(command))

    def test_auth_exclusions_do_not_require_accounts(self):
        for text in ('固定データと認証なしの構成にする。',
                     '認証・DB・外部API・外部公開は設けない。',
                     '認証機能は不要。', 'ログイン機能は依頼対象外。',
                     '認証・権限機能は対象外。',
                     'この下書きは実装・削除・公開・権限変更・外部アクセス・コマンド実行・テスト実行を許可しない。',
                     '権限変更は禁止する。',
                     'A server without authentication', 'No login is required'):
            with self.subTest(text=text):
                self.assertFalse(_auth_test_required(text))

    def test_positive_auth_requirement_survives_other_exclusions(self):
        for text in ('認証機能を実装する', 'ログイン画面を確認する', '権限を確認する',
                     '認証なしの公開画面とログイン機能を実装する',
                     '認証は不要。管理画面の権限をテストする',
                     '権限変更を許可しない。ログイン画面を確認する。',
                     'ログイン画面を確認する。公開・権限変更を許可しない。',
                     'No authentication on public pages. Test login on admin pages.'):
            with self.subTest(text=text):
                self.assertTrue(_auth_test_required(text))

    def test_observed_auth_absence_does_not_require_fictional_accounts(self):
        for text in ('認証機能がないため架空アカウントは作らず、架空の検索入力を使います。',
                     '認証機能は確認した範囲にないため、架空アカウントは作らない。',
                     '認証・権限機能がなく、検索を確認する。',
                     '認証機能はありません。検索を確認する。'):
            with self.subTest(text=text):
                self.assertFalse(_auth_test_required(text))
                self.assertTrue(_auth_test_required(text + '管理画面のログイン機能をテストする。'))

    def test_saved_authless_plan_returns_to_approval_without_ai_or_execution(self):
        self.check_saved_plan_recovery({'recovery_status': 'retrying'})

    def test_permission_prohibition_plan_recovers_without_prior_interruption(self):
        self.check_saved_plan_recovery(None)

    def check_saved_plan_recovery(self, interruption):
        from team_p0_contract import initial
        job = self.store.update(self.job['id'], status='blocked', p0_contract_version=1,
                                goal_contract=initial(self.job['goal']),
                                p0_interruption=interruption)
        planner = self.engine.new_task(job, '架空の計画', '架空の計画を作る', 'planner')
        plan = dict(PLAN, test_methods=['unit'],
                    preconditions=['Pythonと.local\\test-tmpを準備済み'],
                    setup_steps=['固定の架空入力を準備する'], synthetic_accounts=[],
                    ui_test_required=False, ui_test_tool='not_applicable', ui_test_reason='画面なし',
                    ui_test_command='', cleanup_steps=['今回の架空入力だけを片付ける'])
        result = {'summary': '計算機能を実装する。実装・削除・公開・権限変更・外部アクセス・コマンド実行・テスト実行を許可しない。',
                  'tasks': [{'title': '計算を実装する', 'instruction': '計算を実装してテストする',
                             'role': 'builder', 'goal_links': ['計算機能']}],
                  'security_review_required': False, 'questions': [], 'context_updates': [],
                  'warnings': [], 'code_test_plan': plan,
                  'goal_contract': {'primary_goal': job['goal'], 'deliverables': ['計算機能'],
                                    'acceptance_criteria': ['計算が正しい'], 'non_goals': [],
                                    'constraints': []}}
        task = self.store.update(planner['id'], status='blocked', result=result,
                                 summary='・認証/権限を試す架空アカウントの役割、作成方法、初期化方法がありません。')
        self.store.update(job['id'], p0_interruption={'recovery_status': 'keep_paused'})
        self.engine._restore_auth_exclusion_plans()
        self.assertEqual(self.store.get(task['id'])['status'], 'blocked')
        self.store.update(job['id'], p0_interruption=interruption)
        # A remaining real omission must not be bypassed.
        bad = dict(result, code_test_plan=dict(plan, cleanup_steps=[]))
        self.store.update(task['id'], result=bad)
        self.engine._restore_auth_exclusion_plans()
        self.assertEqual(self.store.get(task['id'])['status'], 'blocked')
        self.store.update(task['id'], result=result)
        self.engine._restore_auth_exclusion_plans()
        self.assertEqual(self.store.get(job['id'])['status'], 'awaiting_approval')
        self.assertEqual(self.store.get(task['id'])['attempt'], task['attempt'])
        approvals = self.store.for_job('approval', job['id'])
        self.assertEqual([(a['kind'], a['status']) for a in approvals], [('plan', 'pending')])
        self.assertEqual(approvals[0]['payload'], dict(result, question=''))
        self.assertEqual(len(self.store.for_job('task', job['id'])), 1)
        self.assertFalse(self.engine.active)
        self.engine._restore_auth_exclusion_plans()
        self.assertEqual(len(self.store.for_job('approval', job['id'])), 1)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='saikuru-code-tests-synthetic-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'runtime')
        config = default_config()
        config['approved_roots'] = [str(self.root)]
        self.engine = Engine(self.store, config, {}, 'http://127.0.0.1:0')
        self.engine.paused = True
        self.addCleanup(self.engine.handoff.close)
        self.addCleanup(self.store.db.close)
        self.job = self.store.put('job', {'id': 'synthetic-code-job', 'title': '架空の計算',
            'goal': '架空の計算コードを修正する', 'project': str(self.root), 'status': 'running',
            'auto_execute': True, 'created_at': 1, 'validation_commands': [],
            'code_test_contract_version': 1, 'code_test_plan': dict(PLAN)})

    def cycle(self, repair=0):
        builder = self.engine.new_task(self.job, '架空の実装', '架空の計算を直す', 'builder', repair=repair)
        self.engine._queue_reviews(self.job, builder['id'], repair)
        reviews = [t for t in self.store.for_job('task', self.job['id'])
                   if t.get('review_cycle') == builder['id'] and t['role'] == 'reviewer']
        return builder, next(t for t in reviews if t.get('agent_name') != 'common-security-reviewer'), \
            next(t for t in reviews if t.get('agent_name') == 'common-security-reviewer')

    def run_check(self, task):
        completed = subprocess.run(['python', '-B', '-X', 'utf8', '-m', 'unittest', 'test_calc'],
                                   cwd=self.root, capture_output=True, text=True, timeout=15)
        ctx = Context.__new__(Context)
        ctx.engine, ctx.task = self.engine, task
        ctx.command_result(COMMAND, completed.returncode, completed.stdout + completed.stderr)
        return completed.returncode

    def test_new_job_requires_structured_plan_but_legacy_does_not(self):
        created = self.engine.create_job('架空の新規依頼', '架空の計算コードを修正する',
                                         str(self.root), False)
        self.assertEqual(self.store.get(created['id'])['code_test_contract_version'], 1)
        self.assertIn('code_test_plan', PLAN_SCHEMA_TESTS['required'])
        self.assertIn('test_files', RESULT_SCHEMA_TESTS['required'])
        self.assertEqual(schema_errors(dict(REVIEW, test_files=['test_calc.py'],
                                            test_change_reason=''), RESULT_SCHEMA_TESTS), [])
        self.assertEqual(validate_plan(PLAN)['command'], COMMAND)
        self.assertEqual(validate_plan({'required': False, 'reason': '調査のみ',
                                        'cases': [], 'command': '', 'existing_tests': []})['required'], False)
        with self.assertRaises(ValueError):
            validate_plan({'required': True, 'reason': '', 'cases': [], 'command': COMMAND})
        self.assertEqual(commands({'validation_commands': [COMMAND], 'code_test_plan': PLAN}), [COMMAND])

    def test_plan_expansion_saves_test_policy_before_builder_and_review(self):
        existing_file = self.root / 'tests' / 'test_old.py'
        existing_file.parent.mkdir()
        existing_file.write_text('def test_old(): assert True\n', encoding='utf-8')
        plan = dict(PLAN, existing_tests=['tests/test_old.py'])
        planner = self.engine.new_task(self.job, '架空の計画', '架空の計画を作る', 'planner')
        report = {'summary': '既存テストを調べ、不足分を追加する',
                  'tasks': [{'title': '計算を実装しテストする',
                             'instruction': '既存テストを再利用し、数量追加のケースを加えて実行する',
                             'role': 'builder'}],
                  'security_review_required': False, 'questions': [], 'context_updates': [],
                  'code_test_plan': plan}
        self.engine._validate(planner, report)
        self.engine._expand_plan(planner, report)
        self.assertEqual(self.store.get(self.job['id'])['code_test_plan']['cases'], PLAN['cases'])
        self.assertIn('tests/test_old.py', self.store.get(self.job['id'])['test_assets'])
        self.assertEqual([t['role'] for t in self.store.for_job('task', self.job['id'])],
                         ['planner', 'builder', 'reviewer', 'reviewer'])

    def test_test_files_are_inside_project_retained_and_changed_with_reason(self):
        target = self.root / 'tests' / 'test_calc.py'
        target.parent.mkdir()
        target.write_text('def test_one(): assert 1 == 1\n', encoding='utf-8')
        original = snapshot_files(self.root, ['tests/test_calc.py'])
        self.assertEqual(snapshot_files(self.root, ['tests\\test_calc.py']), original)
        self.assertTrue(verify_snapshot(self.root, original))
        target.write_text('def test_two(): assert 2 == 2\n', encoding='utf-8')
        self.assertFalse(verify_snapshot(self.root, original))
        with self.assertRaises(ValueError):
            snapshot_files(self.root, ['tests/test_calc.py'], original)
        self.assertNotEqual(snapshot_files(self.root, ['tests/test_calc.py'], original,
                                           '追加された仕様を確認する'), original)
        with self.assertRaises(ValueError):
            snapshot_files(self.root, ['../test_calc.py'])
        with self.assertRaises(ValueError):
            snapshot_files(self.root, [['test_calc.py']])
        other = self.root / 'tests' / 'test_other.py'
        other.write_text('def test_other(): assert True\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            snapshot_files(self.root, ['tests/test_other.py'], original,
                           '前回のテストを外してはいけない')

    def test_missing_test_or_run_returns_to_repair_before_review(self):
        builder, reviewer, _ = self.cycle()
        self.engine._finish(builder, dict(DONE))
        self.assertEqual(self.store.get(builder['id'])['result']['status'], 'needs_changes')
        self.assertEqual(self.store.get(reviewer['id'])['status'], 'cancelled')
        self.assertTrue([t for t in self.store.for_job('task', self.job['id'])
                         if t['role'] == 'builder' and t['repair'] == 1])

    def test_code_test_repair_limit_stops_without_acceptance(self):
        builder, reviewer, _ = self.cycle(self.engine.config['max_repairs'])
        self.engine._finish(builder, dict(DONE))
        self.assertEqual(self.store.get(self.job['id'])['status'], 'blocked')
        self.assertEqual(self.store.get(reviewer['id'])['status'], 'cancelled')
        self.assertFalse([a for a in self.store.for_job('approval', self.job['id'])
                          if a['kind'] == 'completion'])

    def test_initial_test_is_reused_after_fix_and_added_case_has_reason(self):
        (self.root / 'calc.py').write_text('def total(a, b): return a + b - 1\n', encoding='utf-8')
        test_file = self.root / 'test_calc.py'
        test_file.write_text('import unittest\nfrom calc import total\n'
                             'class Check(unittest.TestCase):\n'
                             '    def test_add(self): self.assertEqual(total(2, 3), 5)\n', encoding='utf-8')
        builder, reviewer, _ = self.cycle()
        self.assertNotEqual(self.run_check(builder), 0)
        self.engine._finish(builder, dict(DONE))
        self.assertEqual(self.store.get(reviewer['id'])['status'], 'cancelled')
        self.assertEqual(self.store.get(builder['id'])['result']['status'], 'needs_changes')
        first_hash = self.store.get(self.job['id'])['test_assets']['test_calc.py']

        repair = next(t for t in self.store.for_job('task', self.job['id'])
                      if t['role'] == 'builder' and t['repair'] == 1)
        (self.root / 'calc.py').write_text('def total(a, b): return a + b\n', encoding='utf-8')
        self.assertEqual(self.run_check(repair), 0)
        self.engine._finish(repair, dict(DONE))
        self.assertEqual(self.store.get(repair['id'])['result']['status'], 'done')
        self.assertEqual(self.store.get(self.job['id'])['test_assets']['test_calc.py'], first_hash)

        review = next(t for t in self.store.for_job('task', self.job['id'])
                      if t['role'] == 'reviewer' and t.get('review_cycle') == repair['id']
                      and t.get('agent_name') != 'common-security-reviewer')
        self.engine._finish(review, dict(REVIEW))
        self.assertEqual(self.store.get(review['id'])['result']['status'], 'done')

        # A later specification adds a new case; the prior test remains and the
        # file change needs a stated specification reason before review.
        test_file.write_text(test_file.read_text(encoding='utf-8') +
                             '    def test_zero(self): self.assertEqual(total(0, 2), 2)\n', encoding='utf-8')
        next_builder, _, _ = self.cycle(repair=0)
        self.assertEqual(self.run_check(next_builder), 0)
        self.engine._finish(next_builder, dict(DONE))
        self.assertEqual(self.store.get(next_builder['id'])['result']['status'], 'needs_changes')
        reasoned_builder = next(t for t in self.store.for_job('task', self.job['id'])
                                if t['role'] == 'builder' and t['repair'] == 1
                                and t['id'] != repair['id'])
        self.assertEqual(self.run_check(reasoned_builder), 0)
        self.engine._finish(reasoned_builder, dict(DONE, test_change_reason='追加条件: 0と数量の合計'))
        self.assertEqual(self.store.get(reasoned_builder['id'])['result']['status'], 'done')
        self.assertNotEqual(self.store.get(self.job['id'])['test_assets']['test_calc.py'], first_hash)

    def test_post_run_test_tampering_fails_review(self):
        (self.root / 'calc.py').write_text('def total(a, b): return a + b\n', encoding='utf-8')
        test_file = self.root / 'test_calc.py'
        test_file.write_text('import unittest\nfrom calc import total\n'
                             'class Check(unittest.TestCase):\n'
                             '    def test_add(self): self.assertEqual(total(2, 3), 5)\n', encoding='utf-8')
        builder, reviewer, _ = self.cycle()
        self.assertEqual(self.run_check(builder), 0)
        self.engine._finish(builder, dict(DONE))
        test_file.write_text('import unittest\n', encoding='utf-8')
        self.engine._finish(reviewer, dict(REVIEW))
        self.assertEqual(self.store.get(reviewer['id'])['result']['status'], 'needs_changes')

    def test_test_file_changed_after_successful_run_fails_before_review(self):
        (self.root / 'calc.py').write_text('def total(a, b): return a + b\n', encoding='utf-8')
        test_file = self.root / 'test_calc.py'
        test_file.write_text('import unittest\nfrom calc import total\n'
                             'class Check(unittest.TestCase):\n'
                             '    def test_add(self): self.assertEqual(total(2, 3), 5)\n', encoding='utf-8')
        builder, reviewer, _ = self.cycle()
        self.assertEqual(self.run_check(builder), 0)
        observed = self.store.get(builder['id'])['validation_runs'][-1]['observed_at_ns']
        test_file.write_text('import unittest\n', encoding='utf-8')
        os.utime(test_file, ns=(observed + 1_000_000_000, observed + 1_000_000_000))
        self.engine._finish(builder, dict(DONE))
        self.assertEqual(self.store.get(builder['id'])['result']['status'], 'needs_changes')
        self.assertEqual(self.store.get(reviewer['id'])['status'], 'cancelled')


if __name__ == '__main__':
    unittest.main()
