"""Isolated checks for goal-first completion and pending-check handling."""
import tempfile
import time
import unittest
from pathlib import Path

from team_adapters import ADAPTERS, schema_errors
from team_config import default_config
from team_engine import Engine, RESULT_SCHEMA, STAGE_COMPLETION_RULE, REVIEW_STATUS_RULE
from team_store import Store


PLAN = {
    'summary': '実装を計画します。',
    'tasks': [{'title': '実装する', 'instruction': '仕様に沿って実装する。', 'role': 'builder'}],
    'security_review_required': False,
    'questions': [],
    'context_updates': [],
}


class GoalFirstHarnessTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.temp.name)
        config = default_config()
        config['approved_roots'] = [self.temp.name]
        config['security_review'] = 'planner'
        config['document_review_relaxed'] = True
        self.engine = Engine(self.store, config, {'codex': ['fixture'], 'claude': ['fixture']}, 'http://127.0.0.1:1')
        self.saved_adapters = ADAPTERS.copy()
        self.addCleanup(self.restore)

    def restore(self):
        self.engine.shutdown.set()
        deadline = time.time() + 3
        while self.engine.active and time.time() < deadline:
            time.sleep(.01)
        ADAPTERS.update(self.saved_adapters)
        self.engine.handoff.close()
        self.store.db.close()

    def run_until_acceptance(self, job, timeout=30):
        deadline = time.time() + timeout
        while self.store.get(job['id'])['status'] not in ('awaiting_acceptance', 'blocked', 'failed') and time.time() < deadline:
            self.engine.tick()
            time.sleep(.01)
        return self.store.get(job['id'])

    def test_later_assigned_check_allows_stage_completion_but_stays_pending_at_acceptance(self):
        later_check = '独立計測側で境界条件を確認する。'

        class Fixture:
            def run(inner, ctx, prompt, schema):
                if ctx.task['role'] == 'planner':
                    return PLAN
                if ctx.task['role'] == 'builder':
                    return {'status': 'done', 'summary': '実装工程は完了。', 'checks': ['実装を確認。'],
                            'pending_checks': [later_check], 'question': '', 'questions': [], 'context_updates': []}
                return {'status': 'done', 'summary': '独立計測は未実施のまま残す。', 'checks': ['現行成果物を確認。'],
                        'pending_checks': [later_check], 'question': '', 'questions': [], 'context_updates': []}

        ADAPTERS.update(codex=Fixture, claude=Fixture)
        job = self.engine.create_job('検証課題', '実装し、独立計測は後続に割り当てる。', self.temp.name, True)
        self.assertEqual(self.run_until_acceptance(job)['status'], 'awaiting_acceptance')
        approval = next(a for a in self.store.all('approval') if a['job_id'] == job['id'] and a['kind'] == 'completion')
        self.assertEqual(approval['payload']['pending_items'][0]['checks'], [later_check])
        builder = next(t for t in self.store.all('task') if t['job_id'] == job['id'] and t['role'] == 'builder')
        self.assertEqual(builder['status'], 'succeeded')
        self.engine.decide(approval['id'], True, '')
        self.assertEqual(self.store.get(job['id'])['status'], 'accepted_with_pending_checks')

    def test_later_human_evidence_requires_fresh_acceptance(self):
        job = self.engine.create_job('架空検証', '二つの検証を確認する。', self.temp.name, True)
        for task in self.store.all('task'):
            if task['job_id'] == job['id']:
                self.store.update(task['id'], status='cancelled')
        review = self.engine.new_task(job, '確認', '架空の結果を確認', 'reviewer')
        self.store.update(review['id'], status='succeeded', result={'status': 'done', 'summary': '確認待ち', 'checks': []})
        self.store.update(job['id'], status='awaiting_acceptance')
        initial = self.engine.new_approval(review, 'completion', {
            'summary': '確認待ち', 'review_findings': [],
            'pending_items': [{'title': '境界確認', 'note': '人の確認待ち', 'checks': ['空入力', '負数入力']}],
        })
        with self.assertRaises(ValueError):
            self.engine.record_completion_evidence(initial['id'], 0, 0, '確認した', False)
        with self.assertRaises(ValueError):
            self.engine.record_completion_evidence(initial['id'], 0, 0, 'password=secret', True)
        first = self.engine.record_completion_evidence(initial['id'], 0, 0, '架空入力で空入力の結果を確認', True)
        self.assertEqual(self.store.get(initial['id'])['status'], 'superseded')
        self.assertEqual(self.store.get(job['id'])['status'], 'awaiting_acceptance')
        current = self.store.get(first['approval_id'])
        self.assertEqual(current['payload']['pending_items'][0]['checks'], ['負数入力'])
        self.assertEqual(current['payload']['resolved_checks'][0]['authority'], 'user_confirmed')
        with self.assertRaises(ValueError):
            self.engine.record_completion_evidence(initial['id'], 0, 0, '古い判断', True)
        self.engine.decide(current['id'], True, '')
        self.assertEqual(self.store.get(job['id'])['status'], 'accepted_with_pending_checks')
        second = self.engine.record_completion_evidence(current['id'], 0, 0, '架空入力で負数の結果を確認', True)
        self.assertEqual(self.store.get(job['id'])['status'], 'awaiting_acceptance')
        self.assertEqual(self.store.get(second['approval_id'])['payload']['pending_items'], [])
        self.engine.decide(second['approval_id'], True, '')
        self.assertEqual(self.store.get(job['id'])['status'], 'accepted')

    def test_required_checks_are_not_deferred_by_an_ai_plan_alone(self):
        self.assertIn('AIが作った計画だけで利用者の必須条件を省略・後回しにしない', STAGE_COMPLETION_RULE)
        self.assertIn('利用者が後続や独立計測側へ明示的に割り当てた検証', STAGE_COMPLETION_RULE)
        self.assertIn('解消した項目は、根拠をchecksに記録', REVIEW_STATUS_RULE)

    def test_code_defect_is_repaired_and_reviewed_before_acceptance(self):
        source = self.root / 'balance.py'

        class Fixture:
            def run(inner, ctx, prompt, schema):
                if ctx.task['role'] == 'planner':
                    return PLAN
                if ctx.task['role'] == 'builder':
                    text = 'def balance(amount, paid):\n    return max(amount - paid, 0)\n' if ctx.task['repair'] else 'def balance(amount, paid):\n    return amount - paid\n'
                    source.write_text(text, encoding='utf-8')
                    return {'status': 'done', 'summary': '実装しました。', 'checks': ['ファイルを作成。'],
                            'pending_checks': [], 'question': '', 'questions': [], 'context_updates': []}
                correct = 'return max(amount - paid, 0)' in source.read_text(encoding='utf-8')
                if correct:
                    return {'status': 'done', 'summary': '負の残高を0にする仕様を確認。', 'checks': ['balance.pyの境界条件を確認。'],
                            'pending_checks': [], 'question': '', 'questions': [], 'context_updates': []}
                return {'status': 'needs_changes', 'summary': '過払い時に負の残高を返す不具合。',
                        'checks': ['balance.py:返金・過払い境界を確認。'], 'pending_checks': [],
                        'question': '', 'questions': [], 'context_updates': []}

        ADAPTERS.update(codex=Fixture, claude=Fixture)
        job = self.engine.create_job('残高計算', '過払い時の残高は0とする計算を実装する。', self.temp.name, True)
        self.assertEqual(self.run_until_acceptance(job)['status'], 'awaiting_acceptance')
        self.assertIn('return max(amount - paid, 0)', source.read_text(encoding='utf-8'))
        tasks = [t for t in self.store.all('task') if t['job_id'] == job['id']]
        self.assertTrue(any(t['title'] == 'レビュー指摘を修正する' and t['status'] == 'succeeded' for t in tasks))
        self.assertGreaterEqual(sum(t['role'] == 'reviewer' and t['status'] == 'succeeded' for t in tasks), 2)

    def test_current_review_evidence_can_clear_an_older_stage_pending_check(self):
        job = self.engine.create_job('資料', '資料を作る。', self.temp.name, True,
                                     document_source=self.temp.name, document_format='md')
        stage = self.engine.new_task(job, '旧工程', '確認', 'builder')
        self.store.update(stage['id'], status='succeeded', result={
            'status': 'done', 'summary': '未確認項目を報告。', 'checks': [], 'pending_checks': ['検証X'],
        })
        review = self.engine.new_task(job, '最新レビュー', '証拠を確認', 'reviewer')
        self.store.update(review['id'], status='succeeded', result={
            'status': 'done', 'summary': '現行成果物で検証Xを確認。', 'checks': ['検証X: 現行成果物の証拠を確認。'],
            'pending_checks': [],
        })
        self.assertEqual(self.engine._pending_acceptance_items(job['id'], [self.store.get(review['id'])]), [])

    def test_schema_requires_new_reports_but_legacy_handoff_reports_remain_readable(self):
        report = {'status': 'done', 'summary': '完了', 'checks': [], 'question': '', 'questions': [], 'context_updates': []}
        self.assertTrue(any('pending_checks' in error for error in schema_errors(report, RESULT_SCHEMA)))
        report['pending_checks'] = []
        self.assertEqual(schema_errors(report, RESULT_SCHEMA), [])
        job = self.engine.create_job('資料', '資料を作る。', self.temp.name, True,
                                     document_source=self.temp.name, document_format='md')
        handed = self.engine.new_task(job, '旧形式の引継ぎ', '確認', 'builder')
        self.store.update(handed['id'], status='handed_off', result={
            'status': 'handoff', 'summary': '未確認を引き継ぐ。', 'checks': ['未実施：旧形式チェック'],
        })
        items = self.engine._pending_acceptance_items(job['id'], [])
        self.assertEqual(items[0]['checks'], ['未実施：旧形式チェック'])

    def test_stopped_stage_pending_check_survives_accept_without_fix_and_marks_receipt_pending(self):
        job = self.engine.create_job('資料', '資料を作る。', self.temp.name, True,
                                     document_source=self.temp.name, document_format='md')
        self.store.update(job['id'], status='blocked')
        builder = self.engine.new_task(job, '停止した制作', '作る', 'builder')
        self.store.update(builder['id'], status='blocked', result={
            'status': 'blocked', 'summary': '検証前に停止。', 'checks': [], 'pending_checks': ['必要な境界検証'],
        })
        reviewer = self.engine.new_task(job, 'レビュー', '確認', 'reviewer')
        self.store.update(reviewer['id'], status='succeeded', result={
            'status': 'done', 'summary': '資料内容を確認。', 'checks': ['内容確認済み。'], 'pending_checks': [],
        })

        self.engine.accept_without_fix(job['id'])
        approval = next(a for a in self.store.all('approval') if a['job_id'] == job['id'] and a['status'] == 'pending')
        self.assertEqual(approval['payload']['pending_items'][0]['checks'], ['必要な境界検証'])
        self.engine.decide(approval['id'], True, '')
        self.assertEqual(self.store.get(job['id'])['status'], 'accepted_with_pending_checks')


if __name__ == '__main__':
    unittest.main()
