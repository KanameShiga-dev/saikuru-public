"""read_document for PDFs: page text, OCR for picture-only pages, and an honest note when OCR is unavailable."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import document_tools


def picture_pdf(path, pages=2):
    from PIL import Image
    images = [Image.new('RGB', (400, 300), 'white') for _ in range(pages)]
    images[0].save(path, save_all=True, append_images=images[1:])


class PdfReadTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        picture_pdf(self.root / 'guide.pdf')

    def read(self):
        return document_tools.document_operation(self.root, 'read_document', {'path': 'guide.pdf'}, False)

    def test_picture_pages_are_read_with_ocr(self):
        with mock.patch.object(document_tools, '_ocr_pdf_pages', return_value=['采来でできること', '依頼の出し方']) as ocr:
            value = self.read()
        ocr.assert_called_once()
        self.assertEqual(value['page_count'], 2)
        self.assertEqual([p['text'] for p in value['pages']], ['采来でできること', '依頼の出し方'])
        self.assertTrue(all(p.get('ocr') for p in value['pages']))
        self.assertIn('OCR', value['note'])
        self.assertIn('sha256', value)

    def test_ocr_failure_is_reported_not_hidden(self):
        with mock.patch.object(document_tools, '_ocr_pdf_pages', side_effect=RuntimeError('OCR unavailable')):
            value = self.read()
        self.assertEqual([p['text'] for p in value['pages']], ['', ''])
        self.assertIn('できませんでした', value['note'])

    def test_not_a_pdf_is_refused(self):
        (self.root / 'fake.pdf').write_bytes(b'not a pdf')
        with self.assertRaises(ValueError):
            document_tools.document_operation(self.root, 'read_document', {'path': 'fake.pdf'}, False)


if __name__ == '__main__':
    unittest.main()
