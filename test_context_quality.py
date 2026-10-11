"""Quality regressions, with isolated DBs and no provider/network calls."""
import copy
import json
import tempfile
import unittest

from team_handoff import HandoffDB
from team_instructions import MARKER, compact_instruction, instruction_revision


class InstructionQualityTests(unittest.TestCase):
    def test_separate_code_examples_keep_repeated_lines(self):
        text = '関数A:\n```python\ndef a():\n    return 0\n```\n関数B:\n```python\ndef b():\n    return 0\n```'
        self.assertEqual(compact_instruction(text), text)

    def test_scoped_repetition_table_and_whitespace_are_preserved(self):
        text = '開発:\r\n確認してください。\r\n資料:\r\n確認してください。\r\n|値|\r\n|0|\r\n|0|\r\n  末尾  '
        self.assertEqual(compact_instruction(text), text)

    def test_only_exact_latest_supplement_is_suppressed(self):
        text = compact_instruction('元の依頼', 'Aを使う。')
        self.assertEqual(compact_instruction(text, 'Aを使う。'), text)
        text = compact_instruction(text, 'Bを使う。')
        self.assertEqual(compact_instruction(text, 'Aを使う。'), text + MARKER + 'Aを使う。')

    def test_whitespace_change_is_not_equivalent(self):
        text = compact_instruction('元の依頼', '    return 0')
        self.assertEqual(compact_instruction(text, 'return 0'), text + MARKER + 'return 0')

    def test_existing_supplements_are_not_rewritten(self):
        text = '元の依頼' + MARKER + '同じ補足' + MARKER + '同じ補足'
        self.assertEqual(compact_instruction(text), text)

    def test_history_keeps_original_and_submission(self):
        task = {'instruction': '原文\r\n  code', 'attempt': 2}
        result = instruction_revision(task, compact_instruction(task['instruction'], '補足'), 1, 'retry', '補足')
        self.assertEqual(result['instruction_history'][0]['instruction'], task['instruction'])
        self.assertEqual(result['instruction_history'][0]['submitted_note'], '補足')

    def test_engine_startup_does_not_rewrite_persisted_instructions(self):
        from team_config import default_config
        from team_engine import Engine
        from team_store import Store
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            text = 'A:\ndef a():\n    return 0\nB:\ndef b():\n    return 0'
            store.put('task', {'id': 'task', 'job_id': 'job', 'status': 'queued', 'instruction': text})
            engine = None
            try:
                engine = Engine(store, default_config(), {}, 'http://127.0.0.1:1')
                task = store.get('task')
                self.assertEqual(task['instruction'], text)
                self.assertNotIn('instruction_history', task)
            finally:
                if engine:
                    engine.shutdown.set()
                    engine.handoff.close()
                store.db.close()


class ContextQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = HandoffDB(self.temp.name)
        self.job = {'id': 'quality-job', 'project': self.temp.name, 'goal': '説明動画を作成する'}
        self.skills = [{'id': 's', 'name': 'production', 'description': '説明動画',
                        'steps': '条件を確認。' * 400, 'validation': '音声と映像を照合',
                        'failure': '素材不備なら制作を停止', 'skill_dir': 'fixture-only'}]
        self.items = [
            {'key': 'experience:lesson:export', 'value': '例外条件: 顧客向けは音声なし。' * 120,
             'verification': 'human_recorded', 'evidence': 'synthetic decision'},
            {'key': 'experience:lesson:rollback', 'value': '失敗時は元ファイルを保持する。',
             'verification': 'unverified_ai', 'evidence': 'synthetic lesson'}]
        self.db.job_skills = lambda job: copy.deepcopy(self.skills)
        self.db.experience = lambda project, query: {'items': copy.deepcopy(self.items), 'note': '参考データ'}

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def context(self, mode, chars=1000):
        refs = {}
        package = json.loads(self.db.context(self.job, refs=refs, budget={'mode': mode, 'reuse_chars': chars}))
        return package, refs

    def test_on_preserves_long_exception_and_skill_validation(self):
        off, _ = self.context('off')
        on, refs = self.context('on')
        self.assertEqual(on, off)
        self.assertTrue(refs['budget']['quality_fallback'])
        self.assertGreater(refs['budget']['candidate_experience_dropped'], 0)
        self.assertGreater(refs['budget']['candidate_skills_summarized'], 0)
        self.assertEqual(refs['budget']['experience_dropped'], 0)
        self.assertEqual(refs['budget']['skills_summarized'], 0)
        self.assertEqual(refs['budget']['chars_before'], refs['budget']['chars_after'])
        from team_optimization_metrics import _attempt
        row = _attempt({'id': 't', 'job_id': 'j'}, {'context_refs': refs})
        self.assertTrue(row['quality_fallback'])
        self.assertEqual(row['reuse_chars_before'], row['reuse_chars_after'])
        self.assertLess(row['candidate_reuse_chars_after'], row['reuse_chars_after'])

    def test_lexical_mismatch_does_not_remove_experience(self):
        self.skills = []
        off, _ = self.context('off', 100000)
        on, refs = self.context('on', 100000)
        self.assertEqual(on, off)
        self.assertTrue(refs['budget']['quality_fallback'])

    def test_shadow_keeps_original_with_candidate_metrics(self):
        off, _ = self.context('off')
        shadow, refs = self.context('shadow')
        self.assertEqual(shadow, off)
        self.assertGreater(refs['budget']['experience_dropped'], 0)

    def test_current_facts_and_evaluation_boundaries(self):
        self.db.fact(self.temp.name, self.job['id'], 'constraint', '外部送信禁止', 'synthetic user decision',
                     'user_decision', 'quality-constraint')
        for mode, skills, experience in [('A', False, False), ('B', False, True), ('C', True, False), ('D', True, True)]:
            self.job['evaluation'] = {'mode': mode}
            off, _ = self.context('off')
            on, _ = self.context('on')
            self.assertEqual(on, off, mode)
            self.assertEqual(bool(on['reviewed_project_skills']), skills)
            self.assertEqual(bool(on['enterprise_experience']['items']), experience)
            self.assertEqual(on['current_facts'][0]['value'], '外部送信禁止')

    def test_no_loss_needs_no_fallback(self):
        self.skills, self.items = [], []
        _, refs = self.context('on')
        self.assertFalse(refs['budget']['quality_fallback'])


if __name__ == '__main__':
    unittest.main()
