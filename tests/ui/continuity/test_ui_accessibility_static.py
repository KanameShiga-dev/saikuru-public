"""W05.04 static accessibility contract of web/index.html and web/style.css.

Reads the shipped files only; no server, browser, Provider or user data. Browser checks
(keyboard order, focus after re-render, zoom, phone width, contrast) are recorded in
docs/p0/UI_ACCEPTANCE.md section 3.12 and are not replaced by these tests.
"""
from html.parser import HTMLParser
from pathlib import Path
import re
import unittest

WEB = Path(__file__).resolve().parents[3] / 'web'


class _Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items = []

    def handle_starttag(self, tag, attrs):
        self.items.append((tag, dict(attrs)))


def _rules(css):
    """(selector, body) pairs of the flat rules; @media wrappers are irrelevant here."""
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    return [(m.group(1).strip(), m.group(2)) for m in re.finditer(r'([^{}@]+)\{([^{}]*)\}', css)]


class StaticAccessibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.css = (WEB / 'style.css').read_text(encoding='utf-8')
        cls.rules = _rules(cls.css)
        parser = _Elements()
        parser.feed((WEB / 'index.html').read_text(encoding='utf-8'))
        cls.elements = parser.items

    def selectors(self):
        return [s.strip() for selector, _ in self.rules for s in selector.split(',')]

    def test_flex_summaries_keep_a_visible_open_closed_marker(self):
        # display:flex on <summary> removes the native disclosure marker; a text marker must replace it.
        selectors = self.selectors()
        flex = sorted({s.strip() for selector, body in self.rules if 'display:flex' in body.replace(' ', '')
                       for s in selector.split(',') if s.strip().endswith('>summary')})
        self.assertIn('.feature-rework>summary', flex)
        for summary in flex:
            with self.subTest(summary=summary):
                self.assertIn(summary + '::before', selectors)
                parent = summary[:-len('>summary')]
                self.assertIn(parent + '[open]>summary::before', selectors)

    def test_rework_summary_marker_is_text_and_changes_when_open(self):
        bodies = {s.strip(): body for selector, body in self.rules for s in selector.split(',')}
        closed = bodies['.feature-rework>summary::before']
        opened = bodies['.feature-rework[open]>summary::before']
        self.assertRegex(closed, r"content:'[^']+'")
        self.assertRegex(opened, r"content:'[^']+'")
        self.assertNotEqual(re.search(r"content:'([^']+)'", closed).group(1),
                            re.search(r"content:'([^']+)'", opened).group(1))

    def test_interactive_elements_have_a_visible_focus_style(self):
        selectors = self.selectors()
        for required in ('button:focus-visible', 'a:focus-visible', 'input:focus-visible',
                         'textarea:focus-visible', 'select:focus-visible', '.feature-rework>summary:focus-visible',
                         '.detail-fold>summary:focus-visible'):
            with self.subTest(selector=required):
                self.assertIn(required, selectors)
        for selector, body in self.rules:
            if ':focus-visible' in selector:
                with self.subTest(rule=selector):
                    self.assertRegex(body, r'outline:\s*\d')

    def test_status_messages_use_live_regions(self):
        notice = [attrs for tag, attrs in self.elements if attrs.get('id') == 'notice']
        self.assertEqual(len(notice), 1)
        self.assertEqual(notice[0].get('role'), 'status')
        self.assertEqual(notice[0].get('aria-live'), 'polite')
        errors = [attrs for tag, attrs in self.elements if attrs.get('id') == 'job-form-message']
        self.assertEqual(errors[0].get('role'), 'alert')

    def test_dialogs_are_named_by_an_existing_heading(self):
        ids = {attrs['id'] for _, attrs in self.elements if 'id' in attrs}
        dialogs = [attrs for tag, attrs in self.elements if tag == 'dialog']
        self.assertTrue(dialogs)
        for attrs in dialogs:
            with self.subTest(dialog=attrs.get('id')):
                label = attrs.get('aria-labelledby') or ''
                self.assertTrue(label or attrs.get('aria-label'))
                for part in label.split():
                    self.assertIn(part, ids)

    def test_reduced_motion_is_respected(self):
        self.assertIn('prefers-reduced-motion:reduce', self.css.replace(' ', ''))


if __name__ == '__main__':
    unittest.main()
