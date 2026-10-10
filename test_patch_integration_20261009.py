import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from team_config import default_config
from team_engine import Engine
from team_store import Store
from team_claude_login import login_url

class IntegrationTests(unittest.TestCase):
    def test_default_document_review_stays_required(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            config = default_config()
            config['approved_roots'] = [directory]
            engine = Engine(store, config, {'codex': ['fixture'], 'claude': ['fixture']}, 'http://127.0.0.1:1')
            try:
                job = engine.create_job('fixture', 'Make a document', directory, True, document_source=directory, document_format='md')
                engine._queue_reviews(job, store.all('task')[0]['id'])
                self.assertTrue(any(t.get('agent_name') == 'common-security-reviewer' for t in store.all('task')))
                store.update(job['id'], status='blocked')
                with self.assertRaises(ValueError):
                    engine.accept_without_fix(job['id'])
            finally:
                engine.shutdown.set()
                engine.handoff.close()
                store.db.close()

    def test_malformed_login_url_is_ignored(self):
        self.assertIsNone(login_url('https://claude.com:invalid/oauth'))
        self.assertIsNone(login_url('https://[invalid/oauth'))

    def test_broken_policy_stops_input(self):
        import team_dlp
        with tempfile.TemporaryDirectory() as directory:
            policy = Path(directory) / 'policy.json'
            policy.write_text('{broken', encoding='utf-8')
            with patch.object(team_dlp, 'POLICY', policy), self.assertRaises(ValueError):
                team_dlp.require_clean_input('ordinary request')

    def test_pdf_preview_uses_document_runtime(self):
        import team_preview
        from reportlab.pdfgen import canvas
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / 'sample.pdf'
            writer = canvas.Canvas(str(pdf))
            writer.drawString(40, 700, 'Preview fixture')
            writer.showPage()
            writer.save()
            with patch.object(team_preview, 'PREVIEWS', root / 'previews'):
                record = team_preview.render('a' * 32, pdf)
                self.assertEqual(record['status'], 'done', record.get('error'))
                self.assertEqual(record['pages'], 1)
                image = root / 'previews' / ('a' * 32) / record['key'] / 'p001.png'
                self.assertTrue(image.read_bytes().startswith(b'\x89PNG'))

if __name__ == '__main__':
    unittest.main()
