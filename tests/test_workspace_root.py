"""The work folder comes from config.json (workspace_root), not from a fixed C: path (change C, 2026-11-08)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import team_folders
import team_ledger
import team_workspace
from team_config import ROOT


class WorkspaceRootTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.work = self.base / 'work'; (self.work / 'memo').mkdir(parents=True)
        (self.base / 'outside').mkdir()
        saved = team_workspace._root
        self.addCleanup(setattr, team_workspace, '_root', saved)

    def test_a_set_folder_is_used_and_checked_at_the_entry(self):
        self.assertEqual(team_workspace.configure({'workspace_root': str(self.work)}), self.work)
        self.assertEqual(team_workspace.root(), self.work)
        for bad in ('relative/path', str(self.base / 'missing'), self.work.anchor, str(ROOT / 'data'), 5):
            with self.assertRaises(ValueError, msg=bad):
                team_workspace.resolve({'workspace_root': bad})

    def test_without_the_key_the_default_folder_is_used_and_made_only_at_service_start(self):
        default = self.base / 'SaikuruProjects'
        with mock.patch.object(team_workspace, 'DEFAULT', default):
            self.assertEqual(team_workspace.configure({}), default)
            self.assertFalse(default.exists())
            team_workspace.prepare_default()
            self.assertTrue(default.is_dir())

    def test_tools_outside_the_server_read_the_app_config_file(self):
        app = self.base / 'app'; (app / 'data').mkdir(parents=True)
        (app / 'data' / 'config.json').write_text(json.dumps({'workspace_root': str(self.work)}), encoding='utf-8')
        with mock.patch.object(team_workspace, 'ROOT', app):
            team_workspace._root = None
            self.assertEqual(team_workspace.root(), self.work)

    def test_folder_choice_and_ledger_follow_the_work_folder(self):
        team_workspace.configure({'workspace_root': str(self.work)})
        listed = team_folders.listing('')
        self.assertEqual((listed['path'], listed['parent']), (str(self.work), None))
        self.assertEqual([e['name'] for e in listed['entries']], ['memo'])
        self.assertEqual(team_folders.project_folder(str(self.work / 'memo')), self.work / 'memo')
        self.assertEqual(team_folders.create(str(self.work), 'new'), str(self.work / 'new'))
        self.assertEqual(team_ledger.inside(self.work / 'memo'), self.work / 'memo')
        for refused in (str(self.work), str(self.base / 'outside')):
            with self.assertRaises(ValueError, msg=refused):
                team_folders.project_folder(refused)
        with self.assertRaises(ValueError):
            team_ledger.inside(self.base / 'outside')

    def test_family_is_judged_from_the_work_folder_at_any_depth(self):
        team_workspace.configure({'workspace_root': str(self.work)})
        for name in ('family', 'memo'):
            (self.work / name).mkdir(exist_ok=True)
            (self.work / name / 'README.md').write_text('# 見出し\n', encoding='utf-8')
        family, memo = (team_ledger.inspect(self.work / n) for n in ('family', 'memo'))
        self.assertEqual((family['readme_title'], family['suggested_category']),('', '家庭関連（内容未調査）'))
        self.assertEqual(memo['readme_title'], '見出し')

    def test_the_bundled_sample_project_stays_selectable_outside_the_work_folder(self):
        team_workspace.configure({'workspace_root': str(self.work)})
        if not team_folders.SAMPLE.is_dir():
            self.skipTest('sample-project is not present in this checkout')
        self.assertEqual(team_folders.project_folder(str(team_folders.SAMPLE)), team_folders.SAMPLE)
        self.assertIsNone(team_folders.listing(str(team_folders.SAMPLE))['parent'])
        with self.assertRaises(ValueError):
            team_folders.folder(str(ROOT / 'web'))


if __name__ == '__main__':
    unittest.main()
