"""2026-10-08: a review that only lacks re-run evidence must not stop the job waiting for the user.
The meaning of "blocked" is defined once (BLOCKED_RULE); repairs always re-run the verification (REPAIR_RULE);
a review that stops without a question goes to the repair step, bounded by max_repairs."""
import re
import tempfile
import time
import unittest
from pathlib import Path

import team_engine
from team_adapters import ADAPTERS
from team_config import default_config
from team_engine import Engine, REPAIR_RULE, repair_instruction
from team_store import Store

PLAN = {'summary': '計画', 'tasks': [{'title': '作る', 'instruction': '作ってテストする', 'role': 'builder'}],
        'security_review_required': False}
DONE = {'status': 'done', 'summary': '完了', 'checks': ['実行：テスト合格'], 'question': ''}
NO_EVIDENCE = {'status': 'blocked', 'summary': '修正後のテスト結果がないため完了と判定できない。',
               'checks': ['後続確認：現行版でunittestを実行する'], 'question': ''}
QUESTION = {'id': 'q1', 'text': '出力先を変えてよいですか', 'options': [
    {'id': 'yes', 'label': '変えてよい', 'input_required': False, 'input_label': ''}]}


class InstructionTextTest(unittest.TestCase):
    def test_no_instruction_redefines_blocked_for_missing_evidence(self):
        found = []
        for path in Path(team_engine.__file__).parent.glob('*.py'):
            if path.name.startswith('test_'):
                continue
            for match in re.findall(r'(?:不足|足りない|未検証|未確認)[^。\n]{0,12}なら\s*blocked', path.read_text(encoding='utf-8')):
                found.append(path.name + ': ' + match)
        self.assertFalse(found, 'blocked の意味は BLOCKED_RULE だけで定義する')

    def test_repair_instruction_asks_to_rerun_verification(self):
        text = repair_instruction(NO_EVIDENCE)
        self.assertIn(NO_EVIDENCE['summary'], text)
        self.assertIn('現行版でunittestを実行する', text)
        self.assertTrue(text.endswith(REPAIR_RULE))


class ReviewFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        config = default_config()
        config['approved_roots'] = [self.temp.name]
        config['security_review'] = 'planner'
        self.engine = Engine(self.store, config, {'codex': ['fixture'], 'claude': ['fixture']}, 'http://127.0.0.1:1')
        self.saved = ADAPTERS.copy()

    def tearDown(self):
        self.engine.shutdown.set()
        deadline = time.time() + 3
        while self.engine.active and time.time() < deadline:
            time.sleep(.01)
        ADAPTERS.update(self.saved)
        self.engine.handoff.close()
        self.store.db.close()
        self.temp.cleanup()

    def run_job(self, review):
        class Fixture:
            def run(s, ctx, prompt, schema):
                return PLAN if ctx.task['role'] == 'planner' else review(ctx.task) if ctx.task['role'] == 'reviewer' else DONE
        ADAPTERS.update(codex=Fixture, claude=Fixture)
        job = self.engine.create_job('開発', 'ツールを作る', self.temp.name, True)
        deadline = time.time() + 8
        while self.store.get(job['id'])['status'] not in ('awaiting_acceptance', 'blocked', 'failed') and time.time() < deadline:
            self.engine.tick()
            time.sleep(.01)
        return self.store.get(job['id'])

    def test_review_without_question_goes_to_repair(self):
        job = self.run_job(lambda task: NO_EVIDENCE if task['repair'] == 0 else DONE)
        self.assertEqual(job['status'], 'awaiting_acceptance')
        fix = next(t for t in self.store.all('task') if t['title'] == 'レビュー指摘を修正する')
        self.assertIn('現行版に対してやり直し', fix['instruction'])
        self.assertIn('現行版でunittestを実行する', fix['instruction'])
        self.assertTrue(any(e['type'] == 'review_blocked_to_changes' for e in self.store.events(200)))

    def test_review_with_question_still_waits_for_the_user(self):
        asking = dict(NO_EVIDENCE, questions=[QUESTION])
        job = self.run_job(lambda task: asking)
        self.assertEqual(job['status'], 'blocked')
        self.assertFalse(any(t['title'] == 'レビュー指摘を修正する' for t in self.store.all('task')))
        self.assertTrue(any(a['kind'] == 'question' and a['status'] == 'pending' for a in self.store.all('approval')))

    def test_missing_empty_fields_are_filled_but_wrong_types_refused(self):
        # 2026-10-09: a Copilot review left out "question"; it means "no question" and must not fail the task.
        job = self.engine.create_job('開発', 'ツールを作る', self.temp.name, False)
        task = self.engine.new_task(job, '確認', '確認する', 'researcher')
        result = {'status': 'done', 'summary': '完了', 'checks': ['実行：テスト合格']}
        self.engine._validate(task, result)
        self.assertEqual((result['question'], result['questions']), ('', []))
        with self.assertRaises(Exception):
            self.engine._validate(task, {'status': 'done', 'summary': '完了', 'checks': [], 'question': ['x']})
        with self.assertRaises(Exception):  # checks carry information: never filled in
            self.engine._validate(task, {'status': 'done', 'summary': '完了', 'checks': None})

    def test_repeated_stops_end_at_the_repair_limit(self):
        job = self.run_job(lambda task: NO_EVIDENCE)
        self.assertEqual(job['status'], 'blocked')
        fixes = [t for t in self.store.all('task') if t['title'] == 'レビュー指摘を修正する']
        self.assertEqual(len(fixes), self.engine.config['max_repairs'])
        self.assertTrue(any(e['type'] == 'repair_limit' for e in self.store.events(500)))


if __name__ == '__main__':
    unittest.main()
