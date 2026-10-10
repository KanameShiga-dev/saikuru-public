import tempfile
import unittest
from pathlib import Path

from team_config import default_config
from team_engine import Engine
from team_store import Store


FIELDS = {'name': 'sample-skill', 'description': '日本語の文章を推敲する', 'applicability': '日本語の文章を推敲する',
          'steps': '手順', 'tools': '手順書のみ。', 'validation': '照合', 'failure': '報告', 'imported': True,
          'skill_dir': 'C:/nowhere', 'manifest_sha': 'x'}


class SkillSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.source, self.target = root / 'source', root / 'target'
        self.source.mkdir()
        self.target.mkdir()
        self.store = Store(str(root / 'data'))
        config = default_config()
        config['approved_roots'] = [str(self.source), str(self.target)]
        self.engine = Engine(self.store, config, {'codex': ['fixture'], 'claude': ['fixture']}, 'http://127.0.0.1:1')
        self.skill_id = self.engine.handoff.register_imported_skill(FIELDS, [str(self.source)])
        import team_skill_import
        self.saved_current = team_skill_import.current
        team_skill_import.current = lambda name, sha: True

    def tearDown(self):
        import team_skill_import
        team_skill_import.current = self.saved_current
        self.engine.shutdown.set()
        self.engine.handoff.close()
        self.store.db.close()
        self.temp.cleanup()

    def test_selectable_lists_each_version_once(self):
        self.engine.handoff.apply_skill(self.skill_id, str(self.target))
        items = self.engine.handoff.selectable_skills()
        self.assertEqual([i['id'] for i in items], [self.skill_id])

    def test_added_skill_is_applied_and_a_candidate(self):
        job = self.engine.create_job('t', 'まったく関係のない依頼', str(self.target), False, skill_ids=[self.skill_id])
        self.assertEqual(job['skill_selection'], {'mode': 'auto', 'ids': [self.skill_id], 'names': ['sample-skill']})
        handed = self.engine.handoff.job_skills(job)
        self.assertEqual([(i['id'], i['added_at_request']) for i in handed], [(self.skill_id, True)])
        self.assertIn('sample-skill', self.engine.handoff.active_skill_names(str(self.target)))

    def test_auto_matching_still_works_with_added_skills(self):
        second = self.engine.handoff.register_imported_skill(dict(FIELDS, name='other-skill', description='表を集計する',
                                                                  applicability='表を集計する'), [str(self.source)])
        job = self.engine.create_job('t', '日本語の文章を推敲する', str(self.source), False, skill_ids=[second])
        ids = [i['id'] for i in self.engine.handoff.job_skills(job)]
        self.assertEqual(ids, [second, self.skill_id])

    def test_none_and_auto(self):
        job = self.engine.create_job('t', '日本語の文章を推敲する', str(self.source), False, skill_mode='none',
                                     skill_ids=[self.skill_id])
        self.assertEqual(self.engine.handoff.job_skills(job), [])
        job = self.engine.create_job('t', 'まったく関係のない依頼', str(self.source), False)
        self.assertEqual(job['skill_selection'], {'mode': 'auto', 'ids': [], 'names': []})

    def test_rename_changes_display_name_only(self):
        handoff = self.engine.handoff
        self.assertEqual(handoff.selectable_skills()[0]['display_name'], 'sample-skill')
        handoff.rename_skill(self.skill_id, '  文章の推敲  ')
        item = handoff.selectable_skills()[0]
        self.assertEqual((item['name'], item['display_name']), ('sample-skill', '文章の推敲'))
        self.assertIn('sample-skill', handoff.active_skill_names(str(self.source)))
        job = self.engine.create_job('t', 'goal', str(self.target), False, skill_ids=[self.skill_id])
        self.assertEqual(job['skill_selection']['names'], ['文章の推敲'])
        handoff.rename_skill(self.skill_id, '')
        self.assertEqual(handoff.selectable_skills()[0]['display_name'], 'sample-skill')
        for bad in ('x' * 61, 'a<b>', 'a`b'):
            with self.assertRaises(ValueError):
                handoff.rename_skill(self.skill_id, bad)
        with self.assertRaises(ValueError):
            handoff.rename_skill('missing', 'name')

    def test_rejects_unknown_or_empty_selection(self):
        with self.assertRaises(ValueError):
            self.engine.create_job('t', 'goal', str(self.target), False, skill_ids=['nope'])
        with self.assertRaises(ValueError):
            self.engine.create_job('t', 'goal', str(self.target), False, skill_mode='selected')
        with self.assertRaises(ValueError):
            self.engine.create_job('t', 'goal', str(self.target), False, skill_mode='other')


if __name__ == '__main__':
    unittest.main()
