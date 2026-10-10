import json
import tempfile
import unittest
from pathlib import Path

import design_profile as dp


def make_pdf(path):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path), pagesize=A4)
    width, height = A4
    for _ in range(2):
        c.setFillColorRGB(0x1f / 255, 0x4e / 255, 0x79 / 255)
        c.rect(0, height - 18 * mm, width, 18 * mm, fill=1, stroke=0)
        c.setFont('Helvetica-Bold', 24)
        c.drawString(25 * mm, height - 45 * mm, 'Quality report')
        c.setFillColorRGB(0x33 / 255, 0x33 / 255, 0x33 / 255)
        c.setFont('Helvetica', 10.5)
        y = height - 70 * mm
        for _ in range(20):
            c.drawString(25 * mm, y, 'Body text sample for the design profile test, repeated on every line.')
            y -= 18
        c.showPage()
    c.save()


class DesignProfileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pdf = Path(cls.tmp.name) / 'ref.pdf'
        make_pdf(cls.pdf)
        cls.profile = dp.learn(cls.pdf, 'test')

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_learns_colours_fonts_sizes(self):
        p = self.profile
        self.assertEqual(p['colors']['text'], '#333333')
        self.assertEqual(p['colors']['primary'], '#1f4e79')
        self.assertEqual(p['sizes_pt']['body'], 10.5)
        self.assertEqual(p['sizes_pt']['title'], 24)
        self.assertEqual(p['fonts']['body']['family'], 'Arial')
        self.assertEqual(p['page']['orientation'], 'portrait')

    def test_validate_rejects_bad_values(self):
        bad = json.loads(json.dumps(self.profile))
        bad['colors']['primary'] = 'red;}</style><script>'
        with self.assertRaises(ValueError):
            dp.validate(bad)
        bad = json.loads(json.dumps(self.profile))
        bad['fonts']['body']['family'] = "x';}body{"
        with self.assertRaises(ValueError):
            dp.validate(bad)

    def test_css_and_docx(self):
        css = dp.css(self.profile)
        self.assertIn('--color-primary:#1f4e79', css)
        self.assertNotIn('<', css)
        from docx import Document
        doc = Document()
        dp.apply_docx(doc, self.profile)
        self.assertEqual(doc.styles['Normal'].font.name, 'Arial')
        table = doc.add_table(rows=2, cols=2)
        dp.style_docx_table(table, self.profile)

    def test_spot_colour_only_when_distinct(self):
        p = json.loads(json.dumps(self.profile))
        p['colors']['secondary'] = '#e47c24'
        self.assertEqual(dp.slide_palette(p)['secondary'], '#e47c24')
        p['colors']['secondary'] = '#6d8ba8'   # tint of the primary navy: not a colour of its own
        self.assertNotIn('secondary', dp.slide_palette(p))

    def test_rejects_non_pdf(self):
        other = Path(self.tmp.name) / 'x.pdf'
        other.write_bytes(b'not a pdf')
        with self.assertRaises(ValueError):
            dp.learn(other)


if __name__ == '__main__':
    unittest.main()
