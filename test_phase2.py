import json
import tempfile
import unittest
from pathlib import Path

from team_handoff import HandoffDB
from team_usage_metrics import context_budget

SKILL = {'name': 'sample-skill', 'description': '日本語の文章を推敲する', 'applicability': '日本語の文章を推敲する',
         'steps': '手順' * 600, 'tools': '手順書のみ。', 'validation': '照合', 'failure': '報告', 'imported': True,
         'skill_dir': 'C:/nowhere', 'manifest_sha': 'x'}


def experience(key, value, human=False):
    return {'key': key, 'value': value, 'evidence': 'e', 'verification': 'human_recorded' if human else 'unverified_ai',
            'job_id': 'j', 'recorded_at': 1, 'status': 'current', 'authority': 'user_decision' if human else 'model'}


class BudgetTests(unittest.TestCase):
    def test_selection(self):
        skills = [dict(SKILL, id='auto1'), dict(SKILL, id='added', added_at_request=True)]
        items = [experience('experience:lesson:資料作成:スライド', 'スライドの章扉には番号を付ける'),
                 experience('experience:lesson:資料作成:スライド構成', 'スライドの構成は表紙から始める', human=True),
                 experience('experience:lesson:ネットワーク:設定', 'ファイアウォールの規則を確認する')]
        kept_skills, kept_items, plan = HandoffDB.reuse_budget('説明資料のスライドを作る。スライドの構成と章扉', skills, items, 1500)
        self.assertEqual(kept_skills[0]['id'], 'added')               # added at request: always full
        self.assertNotIn('summary_only', kept_skills[0])
        self.assertTrue(kept_skills[1]['summary_only'])                # over budget: summary + path only
        self.assertEqual(plan['experience_unrelated'], 1)              # firewall lesson is unrelated
        self.assertLess(plan['chars_after'], plan['chars_before'])

    def test_setting(self):
        self.assertEqual(context_budget({}), {'mode': 'shadow', 'reuse_chars': 8000})
        self.assertEqual(context_budget({'context_budget': {'mode': 'on', 'reuse_chars': 3000}})['mode'], 'on')
        self.assertEqual(context_budget({'context_budget': {'mode': 'bad'}})['mode'], 'off')


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        from team_config import default_config
        from team_engine import Engine
        from team_store import Store
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        config = default_config()
        config['approved_roots'] = [self.temp.name]
        self.engine = Engine(self.store, config, {'codex': ['fixture'], 'claude': ['fixture']}, 'http://127.0.0.1:1')
        self.skill = self.engine.handoff.register_imported_skill(SKILL, [self.temp.name])
        import team_skill_import
        self.saved = team_skill_import.current
        team_skill_import.current = lambda name, sha: True

    def tearDown(self):
        import team_skill_import
        team_skill_import.current = self.saved
        self.engine.shutdown.set()
        self.engine.handoff.close()
        self.store.db.close()
        self.temp.cleanup()

    def job(self, mode, **extra):
        return self.engine.create_job('t', '日本語の文章を推敲する', self.temp.name, False, evaluation_mode=mode,
                                      evaluation_confirmed=True, **extra)

    def test_modes_switch_only_reuse_context(self):
        for mode, has_skill in (('A', False), ('B', False), ('C', True), ('D', True)):
            refs = {}
            text = self.engine.handoff.context(self.job(mode), None, refs, {'mode': 'shadow', 'reuse_chars': 8000})
            package = json.loads(text)
            self.assertEqual(bool(package['reviewed_project_skills']), has_skill, mode)
            self.assertIn('user_decisions_and_approvals', package)
            self.assertEqual(refs['evaluation_mode'], mode)
            self.assertEqual(refs['budget']['mode'], 'shadow')

    def test_shadow_does_not_change_context(self):
        job = self.engine.create_job('t', '日本語の文章を推敲する', self.temp.name, False)
        off = json.loads(self.engine.handoff.context(job, None, {}, {'mode': 'off'}))
        shadow = json.loads(self.engine.handoff.context(job, None, {}, {'mode': 'shadow', 'reuse_chars': 1000}))
        self.assertEqual(off['reviewed_project_skills'], shadow['reviewed_project_skills'])
        on = json.loads(self.engine.handoff.context(job, None, {}, {'mode': 'on', 'reuse_chars': 1000}))
        self.assertTrue(on['reviewed_project_skills'][0].get('summary_only'))

    def test_evaluation_rules(self):
        with self.assertRaises(ValueError):
            self.engine.create_job('t', 'g', self.temp.name, False, evaluation_mode='A')          # not confirmed
        with self.assertRaises(ValueError):
            self.job('E')
        with self.assertRaises(ValueError):
            self.job('C', skill_ids=[self.skill])                                                # no request-time skills
        from team_optimization_metrics import report
        self.job('A')
        summary = report(Path(self.temp.name) / 'team.sqlite3')
        self.assertEqual(summary['evaluation_modes']['A']['jobs'], 1)
        self.assertIsNone(summary['evaluation_modes']['A']['input_total_per_accepted'])


if __name__ == '__main__':
    unittest.main()
