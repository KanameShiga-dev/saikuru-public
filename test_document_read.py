import tempfile
import unittest
from pathlib import Path

import document_tools as dt
from document_media_worker import build_pptx


class PptxReadTest(unittest.TestCase):
    def test_reads_text_table_boxes_and_design(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            design = {'accent': '#1f4e79', 'dark': '#1f4e79', 'text': '#333333', 'bg': '#f4f6f8', 'light': '#e3e6e8',
                      'muted': '#6a6b6c', 'on_accent': '#ffffff', 'heading_font': 'Yu Gothic', 'body_font': 'Yu Gothic'}
            slides = [{'title': '表紙', 'body': '[部署名]', 'layout': 'cover'},
                      {'title': '流れ', 'body': '', 'diagram': {'nodes': ['集める', '分ける']}},
                      {'title': '分類', 'body': '', 'table': [['分類', '例'], ['誤解', '読み違い']]}]
            build_pptx({'design': design}, slides, root / 'deck.pptx')
            result = dt.document_operation(root, 'read_document', {'path': 'deck.pptx'}, False)
            self.assertEqual(result['slide_count'], 3)
            self.assertIn('[部署名]', result['content'])
            self.assertIn('[図形] 集める', result['content'])
            self.assertIn('[表 2行×2列]', result['content'])
            self.assertIn('誤解 | 読み違い', result['content'])
            self.assertEqual(result['slides'][1]['boxes'], 2)
            self.assertEqual(list(result['design']['fonts']), ['Yu Gothic'])
            self.assertIn('#333333', result['design']['text_colors'])
            self.assertIn('#1f4e79', result['design']['fills'])


class SparseLayoutTest(unittest.TestCase):
    def test_sparse_detection(self):
        from document_media_worker import _sparse_lines
        self.assertEqual(_sparse_lines({'title': 't', 'body': '・一つ目\n・二つ目'}), ['一つ目', '二つ目'])
        self.assertEqual(_sparse_lines({'title': 't', 'body': '一文だけ'}), ['一文だけ'])
        self.assertIsNone(_sparse_lines({'title': 't', 'body': 'a\nb\nc\nd\ne'}))
        self.assertIsNone(_sparse_lines({'title': 't', 'body': '短い', 'table': [['a']]}))
        self.assertIsNone(_sparse_lines({'title': 't', 'body': 'x' * 41}))
        self.assertIsNone(_sparse_lines({'title': 't', 'body': '短い', 'layout': 'section'}))

    def test_card_size_avoids_orphans(self):
        from document_media_worker import _card_size
        size = _card_size(['記録の書き方がばらばら', '手戻りの多い工程が分かっていない'], 250)
        per = int(250 // (size * 1.05))
        for point in ('記録の書き方がばらばら', '手戻りの多い工程が分かっていない'):
            self.assertTrue(len(point) <= per or len(point) - per >= 3)

    def test_cards_render_in_pptx(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            build_pptx({}, [{'title': '表紙', 'body': '', 'layout': 'cover'},
                            {'title': '課題', 'body': '一つ目\n二つ目\n三つ目'}], root / 'deck.pptx')
            result = dt.document_operation(root, 'read_document', {'path': 'deck.pptx'}, False)
            for text in ('01', '02', '03', '一つ目', '三つ目'):
                self.assertIn(text, result['content'])


if __name__ == '__main__':
    unittest.main()
