import tempfile
import unittest
from pathlib import Path

from team_config import default_config
from team_document_capabilities import document_goal
from team_engine import Engine
from team_store import Store


class DocumentGoalTests(unittest.TestCase):
    def test_goal_text(self):
        goal = document_goal(Path('C:/x'), Path('C:/x'), 'pptx', '【目的】説明資料')
        self.assertTrue(goal.startswith('資料作成のみ。読み取り元: '))
        self.assertIn('必須成果物形式: pptx。', goal)
        self.assertIn('公開は禁止。', goal)
        self.assertTrue(goal.endswith('【目的】説明資料'))
        self.assertIn('claude.ai', document_goal('a', 'a', 'claude_slides', 't'))

    def test_new_request_can_be_a_document_job(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'docs'
            folder.mkdir()
            store = Store(str(Path(temp) / 'data'))
            config = default_config()
            config['approved_roots'] = [str(folder)]
            engine = Engine(store, config, {'codex': ['fixture'], 'claude': ['fixture']}, 'http://127.0.0.1:1')
            try:
                job = engine.create_job('資料', document_goal(folder, folder, 'pptx', '説明資料を作る'), str(folder), True,
                                        document_source=str(folder), document_format='pptx')
                self.assertEqual((job['document_source'], job['document_format']), (str(folder), 'pptx'))
                from team_document_capabilities import require_supported
                with self.assertRaises(ValueError):
                    require_supported('', 'x')
            finally:
                engine.shutdown.set()
                engine.handoff.close()
                store.db.close()


if __name__ == '__main__':
    unittest.main()
