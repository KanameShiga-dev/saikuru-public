import tempfile
import unittest
from pathlib import Path

from team_artifact_guard import FORMATS, check_staging, prepare_staging, review, staging_edit, staging_root

DESIGN = FORMATS['claude_design'][1]
OWNED = 'https://claude.ai/artifact/AbCdEf123'


class ArtifactGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        self.staging = staging_root(self.project, 'job1')
        (self.staging / 'project').mkdir(parents=True)
        self.page = self.staging / 'project' / 'Main.dc.html'
        self.page.write_text('<div>Spring Menu [YOUR PRICE]</div>', encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()

    def run_review(self, data, role='builder', owned=(), created=()):
        return review(data, role, 'claude_design', self.project, 'job1', list(owned), set(created))

    def test_create_needs_manual_approval(self):
        verdict = self.run_review({'type_url': DESIGN, 'title': 'Poster'})
        self.assertTrue(verdict['allow'])
        self.assertEqual(verdict['kind'], 'create')
        self.assertTrue(verdict['manual'])

    def test_create_rejects_other_type_and_second_artifact(self):
        self.assertFalse(self.run_review({'type_url': FORMATS['claude_slides'][1], 'title': 'x'})['allow'])
        self.assertFalse(self.run_review({'type_url': DESIGN, 'title': 'x'}, owned=[OWNED])['allow'])
        self.assertFalse(self.run_review({'type_url': DESIGN})['allow'])

    def test_only_builder_publishes(self):
        self.assertFalse(self.run_review({'type_url': DESIGN, 'title': 'x'}, role='reviewer')['allow'])

    def test_forbidden_actions(self):
        self.assertFalse(self.run_review({'action': 'delete', 'url': OWNED})['allow'])
        self.assertFalse(self.run_review({'action': 'pin', 'url': OWNED})['allow'])
        self.assertFalse(self.run_review({'url': OWNED, 'file_path': str(self.page), 'force': True}, owned=[OWNED])['allow'])
        self.assertTrue(self.run_review({'action': 'list', 'type': 'Design System'})['allow'])

    def test_update_only_owned_artifact(self):
        data = {'url': 'https://claude.ai/artifact/Other999', 'file_path': str(self.page)}
        self.assertFalse(self.run_review(data, owned=[OWNED])['allow'])
        verdict = self.run_review({'url': OWNED, 'root': str(self.staging), 'file_path': str(self.page),
                                   'files': {'project/canvas.json': None}}, created=[OWNED])
        self.assertTrue(verdict['allow'], verdict)
        self.assertEqual(verdict['url'], OWNED)

    def test_files_outside_staging_rejected(self):
        outside = Path(self.project) / 'secret.html'
        outside.write_text('<p>x</p>', encoding='utf-8')
        self.assertFalse(self.run_review({'url': OWNED, 'file_path': str(outside)}, owned=[OWNED])['allow'])
        self.assertFalse(self.run_review({'url': OWNED, 'root': str(self.staging),
                                          'files': {'project/a.html': '../../secret.html'}}, owned=[OWNED])['allow'])

    def test_content_check(self):
        for text in ('連絡先 taro@example.co.jp', 'server 192.168.1.20', r'C:\Users\someone\doc.txt', 'api_key = abc'):
            self.page.write_text(text, encoding='utf-8')
            verdict = self.run_review({'url': OWNED, 'file_path': str(self.page)}, owned=[OWNED])
            self.assertFalse(verdict['allow'], text)

    def test_staging_edit(self):
        ok, _ = staging_edit('Write', {'file_path': str(self.staging / 'project' / 'deck.json'), 'content': '{}'},
                             'builder', self.project, 'job1')
        self.assertTrue(ok)
        ok, _ = staging_edit('Write', {'file_path': str(Path(self.project) / 'x.html'), 'content': ''},
                             'builder', self.project, 'job1')
        self.assertFalse(ok)
        ok, _ = staging_edit('Write', {'file_path': str(self.staging / 'run.py'), 'content': ''},
                             'builder', self.project, 'job1')
        self.assertFalse(ok)
        ok, _ = staging_edit('Write', {'file_path': str(self.staging / 'a.html'), 'content': ''},
                             'reviewer', self.project, 'job1')
        self.assertFalse(ok)
        ok, _ = staging_edit('Write', {'file_path': str(self.staging / '_reference' / 'x.md'), 'content': ''},
                             'builder', self.project, 'job1')
        self.assertFalse(ok)

    def test_prepare_and_check_staging(self):
        staging = prepare_staging(self.project, 'job1', 'claude_design')
        self.assertTrue((staging / '_reference' / 'claude_design.md').is_file())
        with self.assertRaises(ValueError):
            check_staging(self.project, 'job1', 'claude_design')
        (staging / 'project' / 'canvas.json').write_text('{"v":3}', encoding='utf-8')
        check_staging(self.project, 'job1', 'claude_design')
        self.page.write_text('連絡先 taro@example.co.jp', encoding='utf-8')
        with self.assertRaises(ValueError):
            check_staging(self.project, 'job1', 'claude_design')


if __name__ == '__main__':
    unittest.main()
