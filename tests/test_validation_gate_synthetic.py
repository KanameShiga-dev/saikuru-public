"""Offline checks of the real engine's explicit-command completion gate."""
import tempfile
import subprocess
import unittest
from pathlib import Path

from team_config import default_config
from document_tools import document_operation
from team_document_capabilities import require_artifacts
from team_engine import CODE_TEST_REUSE_RULE, Context, Engine
from team_store import Store
from team_validation_gate import findings, record_result, validate_commands


COMMAND = 'python -B -X utf8 -m unittest synthetic_case'
DONE = {'status': 'done', 'summary': '架空の確認結果', 'checks': [],
        'pending_checks': [], 'question': '', 'questions': [], 'context_updates': []}


class ValidationGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='saikuru-validation-synthetic-')
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.project_root = root
        self.store = Store(root / 'runtime')
        config = default_config()
        config['approved_roots'] = [str(root)]
        self.engine = Engine(self.store, config, {}, 'http://127.0.0.1:0')
        self.engine.paused = True
        self.addCleanup(self.engine.handoff.close)
        self.addCleanup(self.store.db.close)
        self.job = self.store.put('job', {'id': 'synthetic-job', 'title': '架空の検証',
            'goal': '架空のコード変更', 'project': str(root), 'status': 'running',
            'auto_execute': True, 'created_at': 1, 'validation_commands': [COMMAND]})

    def cycle(self, repair=0):
        builder = self.engine.new_task(self.job, '架空の作業', '架空の編集', 'builder', repair=repair)
        self.engine._queue_reviews(self.job, builder['id'], repair)
        reviewer = next(t for t in self.store.for_job('task', self.job['id'])
                        if t['role'] == 'reviewer' and t.get('review_cycle') == builder['id']
                        and t.get('agent_name') != 'common-security-reviewer')
        self.store.update(builder['id'], status='succeeded', result=dict(DONE))
        return builder, reviewer

    def test_input_and_latest_attempt(self):
        self.assertEqual(validate_commands([COMMAND]), [COMMAND])
        for bad in ([COMMAND, COMMAND], ['x\ny'], [''], ['x'] * 4):
            with self.assertRaises(ValueError):
                validate_commands(bad)
        task = {'role': 'builder', 'validation_runs': [
            {'index': 0, 'exit_code': 1, 'timed_out': False},
            {'index': 0, 'exit_code': 0, 'timed_out': False}]}
        self.assertEqual(findings([COMMAND], task), [])
        self.assertIsNone(record_result({'role': 'reviewer'}, [COMMAND], COMMAND, 0, False))

    def test_output_truncation_wrapper_does_not_count_as_exact_command(self):
        wrapped = COMMAND + ' 2>&1 | tail -40'
        self.assertIsNone(record_result({'role': 'builder'}, [COMMAND], wrapped, 0, False))
        self.assertEqual(findings([COMMAND], {'role': 'builder', 'validation_runs': []}),
                         ['検証コマンド1が未実行です。'])

    def test_new_job_persists_only_explicit_commands(self):
        created = self.engine.create_job('架空の追加依頼', '架空の計算を修正する',
                                         str(self.project_root), False,
                                         validation_commands=[COMMAND])
        self.assertEqual(self.store.get(created['id'])['validation_commands'], [COMMAND])
        self.assertEqual([t['role'] for t in self.store.for_job('task', created['id'])], ['planner'])

    def test_missing_run_returns_to_builder_instead_of_acceptance(self):
        builder, reviewer = self.cycle()
        self.engine._finish(reviewer, dict(DONE))
        self.assertEqual(self.store.get(self.job['id'])['status'], 'running')
        self.assertEqual(self.store.get(reviewer['id'])['result']['status'], 'needs_changes')
        repairs = [t for t in self.store.for_job('task', self.job['id'])
                   if t['role'] == 'builder' and t['repair'] == 1]
        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0]['after'], reviewer['id'])
        self.assertFalse([a for a in self.store.for_job('approval', self.job['id'])
                          if a['kind'] == 'completion'])

    def test_failed_run_returns_to_builder(self):
        builder, reviewer = self.cycle()
        ctx = Context.__new__(Context)
        ctx.engine, ctx.task = self.engine, builder
        ctx.command_result(COMMAND, 1, 'synthetic failure')
        self.engine._finish(reviewer, dict(DONE))
        self.assertEqual(self.store.get(reviewer['id'])['result']['status'], 'needs_changes')
        self.assertIn('終了状態: 1', self.store.get(reviewer['id'])['result']['summary'])

    def test_later_command_invalidates_earlier_success(self):
        builder, reviewer = self.cycle()
        ctx = Context.__new__(Context)
        ctx.engine, ctx.task = self.engine, builder
        ctx.command_result(COMMAND, 0, 'synthetic success')
        ctx.command_result('python -V', 0, 'synthetic version')
        self.engine._finish(reviewer, dict(DONE))
        self.assertEqual(self.store.get(reviewer['id'])['result']['status'], 'needs_changes')

    def test_builder_reports_failure_and_repair_is_queued(self):
        builder, reviewer = self.cycle()
        self.store.update(builder['id'], status='running')
        failed = dict(DONE, status='needs_changes', summary='架空の検証が失敗')
        self.engine._finish(builder, failed)
        self.assertEqual(self.store.get(self.job['id'])['status'], 'running')
        self.assertEqual(self.store.get(reviewer['id'])['status'], 'cancelled')
        repairs = [t for t in self.store.for_job('task', self.job['id'])
                   if t['role'] == 'builder' and t['repair'] == 1]
        self.assertEqual(len(repairs), 1)

    def test_repair_limit_blocks_without_acceptance(self):
        builder, reviewer = self.cycle(self.engine.config['max_repairs'])
        self.engine._finish(reviewer, dict(DONE))
        self.assertEqual(self.store.get(self.job['id'])['status'], 'blocked')
        self.assertFalse([a for a in self.store.for_job('approval', self.job['id'])
                          if a['kind'] == 'completion'])

    def test_successful_run_reaches_acceptance(self):
        builder, reviewer = self.cycle()
        ctx = Context.__new__(Context)
        ctx.engine, ctx.task = self.engine, builder
        ctx.command_result(COMMAND, 0, 'synthetic success')
        self.engine._finish(reviewer, dict(DONE))
        security = next(t for t in self.store.for_job('task', self.job['id'])
                        if t.get('agent_name') == 'common-security-reviewer'
                        and t.get('review_cycle') == builder['id'])
        self.engine._finish(security, dict(DONE))
        self.assertEqual(self.store.get(self.job['id'])['status'], 'awaiting_acceptance')
        self.assertEqual(self.store.get(reviewer['id'])['result']['status'], 'done')
        self.assertEqual(len([a for a in self.store.for_job('approval', self.job['id'])
                              if a['kind'] == 'completion' and a['status'] == 'pending']), 1)

    def test_real_synthetic_failure_then_fix_stays_in_one_job(self):
        (self.project_root / 'calc.py').write_text('def total(a, b): return a + b - 1\n', encoding='utf-8')
        (self.project_root / 'synthetic_case.py').write_text(
            'import unittest\nfrom calc import total\n'
            'class Check(unittest.TestCase):\n'
            '    def test_sum(self): self.assertEqual(total(2, 3), 5)\n', encoding='utf-8')
        original_test = (self.project_root / 'synthetic_case.py').read_bytes()
        builder, reviewer = self.cycle()

        def actual_check(task):
            result = subprocess.run(['python', '-B', '-X', 'utf8', '-m', 'unittest',
                                     'synthetic_case'], cwd=self.project_root,
                                    capture_output=True, text=True, encoding='utf-8', timeout=15)
            ctx = Context.__new__(Context)
            ctx.engine, ctx.task = self.engine, task
            ctx.command_result(COMMAND, result.returncode, result.stdout + result.stderr)
            return result.returncode

        self.assertNotEqual(actual_check(builder), 0)
        self.engine._finish(reviewer, dict(DONE))
        self.assertEqual(self.store.get(self.job['id'])['status'], 'running')
        repair = next(t for t in self.store.for_job('task', self.job['id'])
                      if t['role'] == 'builder' and t['repair'] == 1)
        (self.project_root / 'calc.py').write_text('def total(a, b): return a + b\n', encoding='utf-8')
        self.assertEqual((self.project_root / 'synthetic_case.py').read_bytes(), original_test)
        self.assertEqual(actual_check(repair), 0)
        self.store.update(repair['id'], status='succeeded', result=dict(DONE))
        repaired_review = next(t for t in self.store.for_job('task', self.job['id'])
                               if t['role'] == 'reviewer' and t.get('review_cycle') == repair['id']
                               and t.get('agent_name') != 'common-security-reviewer')
        self.engine._finish(repaired_review, dict(DONE))
        security = next(t for t in self.store.for_job('task', self.job['id'])
                        if t.get('agent_name') == 'common-security-reviewer'
                        and t.get('review_cycle') == repair['id'])
        self.engine._finish(security, dict(DONE))
        self.assertEqual(self.store.get(self.job['id'])['status'], 'awaiting_acceptance')
        self.assertEqual(self.store.get(self.job['id']).get('rework_count', 0), 0)

    def test_document_finding_is_repaired_before_acceptance(self):
        self.engine.config['document_review_relaxed'] = True
        self.store.update(self.job['id'], document_source=str(self.project_root),
                          document_format='md', validation_commands=[])
        job = self.store.get(self.job['id'])
        builder = self.engine.new_task(job, '架空の資料作成', '架空の文章', 'builder')
        self.engine._queue_reviews(job, builder['id'])
        reviewer = next(t for t in self.store.for_job('task', job['id'])
                        if t['role'] == 'reviewer')
        self.store.update(builder['id'], status='succeeded', result=dict(DONE))
        issue = dict(DONE, status='needs_changes', summary='架空の文章に誤りがある')
        self.engine._finish(reviewer, issue)
        self.assertEqual(self.store.get(job['id'])['status'], 'running')
        self.assertTrue([t for t in self.store.for_job('task', job['id'])
                         if t['role'] == 'builder' and t['repair'] == 1])
        self.assertFalse([a for a in self.store.for_job('approval', job['id'])
                          if a['kind'] == 'completion'])

    def test_text_document_must_belong_to_current_job_and_match_hash(self):
        other = {'id': 'another-synthetic-job', 'project': str(self.project_root),
                 'document_format': 'md', 'document_manifest_required': True}
        document_operation(self.project_root, 'write_document',
                           {'path': 'report.md', 'content': 'fictional report'}, True, other['id'])
        with self.assertRaises(ValueError):
            require_artifacts({'id': self.job['id'], 'project': str(self.project_root),
                               'document_format': 'md', 'document_manifest_required': True})
        require_artifacts(other)
        (self.project_root / 'report.md').write_text('changed report', encoding='utf-8')
        with self.assertRaises(ValueError):
            require_artifacts(other)

    def test_legacy_text_document_job_keeps_previous_gate(self):
        require_artifacts({'id': 'old-synthetic-job', 'project': str(self.project_root),
                           'document_format': 'md'})

    def test_code_test_instruction_preserves_and_reruns_tests(self):
        self.assertIn('初回の実装時', CODE_TEST_REUSE_RULE)
        self.assertIn('既存のテストコードを保持して再実行', CODE_TEST_REUSE_RULE)
        self.assertIn('単独で実行', CODE_TEST_REUSE_RULE)
        self.assertIn('パイプ、リダイレクト', CODE_TEST_REUSE_RULE)
        self.assertIn('既存の承認と安全確認', CODE_TEST_REUSE_RULE)


if __name__ == '__main__':
    unittest.main()
