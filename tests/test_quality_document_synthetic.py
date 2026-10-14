"""SQAH Phase 2 on synthetic documents: no AI, process, network or conversion tool."""
import json
import tempfile
import unittest
from pathlib import Path

from team_quality_document import inspect_file, inspect_text, overall, record

GOOD_MD = """# 利用案内

## 目的
この資料は架空の図書室の案内です。

## 手順
詳細は [別紙](annex.md) と [目的](#目的) を参照。

```python
TODO = 1  # inside a fence: not a placeholder
```
"""

GOOD_HTML = """<!doctype html><html><body>
<h1>利用案内</h1><h2 id="purpose">目的</h2><p>架空の図書室です。<br/></p>
<p><a href="annex.html">別紙</a> <a href="#purpose">目的</a> <a href="https://example.com/x">外部</a></p>
<script>var TODO = 1;</script>
</body></html>"""


def states(result):
    return {c['id']: c['state'] for c in result['checks']}


class MarkdownTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base / 'annex.md').write_text('# annex\n', encoding='utf-8')
        (self.base / 'annex.html').write_text('<h1>a</h1>', encoding='utf-8')

    def test_clean_document_passes_every_check(self):
        result = inspect_text('guide.md', GOOD_MD, self.base, required_headings=['目的', '手順'])
        self.assertEqual(overall(result['checks']), 'passed', result)

    def test_embedded_defects_are_found_with_line_numbers_only(self):
        text = '# 案内\n\n## \n未記入です TODO\n[壊れ](missing.md) [内部](#nowhere)\n```\nopen fence\n'
        result = inspect_text('guide.md', text, self.base, required_headings=['手順'])
        s = states(result)
        self.assertEqual(s['DQA-01a'], 'failed')   # unclosed fence
        self.assertEqual(s['DQA-01b'], 'failed')   # empty heading
        self.assertEqual(s['DQA-01c'], 'failed')   # required heading missing
        self.assertEqual(s['DQA-01d'], 'failed')   # broken file link and anchor
        self.assertEqual(s['DQA-01e'], 'failed')   # placeholder
        lines = {c['id']: c.get('lines') for c in result['checks']}
        self.assertEqual(lines['DQA-01d'], [5])
        self.assertEqual(lines['DQA-01e'], [4])

    def test_link_checks_stay_inside_the_project_and_skip_remote_urls(self):
        text = '# t\n[a](https://example.com) [b](../outside.md) [c](/etc/passwd) [d](mailto:x@example.com)\n'
        result = inspect_text('t.md', text, self.base)
        self.assertEqual(states(result)['DQA-01d'], 'failed')
        ok = inspect_text('t.md', '# t\n[a](https://example.com) [d](mailto:x@example.com)\n', self.base)
        self.assertEqual(states(ok)['DQA-01d'], 'passed')


class HtmlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base / 'annex.html').write_text('<h1>a</h1>', encoding='utf-8')

    def test_clean_html_passes_and_script_text_is_not_a_placeholder(self):
        result = inspect_text('guide.html', GOOD_HTML, self.base)
        self.assertEqual(overall(result['checks']), 'passed', result)

    def test_unclosed_tags_empty_heading_broken_link_placeholder(self):
        html = '<h1>案内</h1>\n<h2></h2>\n<div><p>TBD\n<a href="missing.html">x</a>\n'
        result = inspect_text('guide.html', html, self.base)
        s = states(result)
        self.assertEqual((s['DQA-01a'], s['DQA-01b'], s['DQA-01d'], s['DQA-01e']), ('failed',) * 4)


class SafetyTests(unittest.TestCase):
    def test_secret_and_personal_data_are_flagged_without_copying_values(self):
        secret = 'ghp_' + 'A' * 24
        mail = 'person@example.com'
        text = f'# 案内\n鍵 {secret}\n連絡先 {mail}\n'
        with tempfile.TemporaryDirectory() as tmp:
            result = inspect_text('x.md', text, tmp)
        self.assertEqual(states(result)['DQA-05'], 'failed')
        dumped = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(secret, dumped)
        self.assertNotIn(mail, dumped)

    def test_digit_runs_only_warn_while_secret_email_phone_still_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            def sensitive(text):
                return [c for c in inspect_text('x.md', '# t\n' + text + '\n', tmp)['checks'] if c['id'] == 'DQA-05'][0]
            folder = sensitive('評価フォルダ data/eval-20261013-2351 を使用')      # 8+4 digits: a date, not a person
            self.assertEqual(folder['state'], 'inconclusive')
            self.assertIn('12桁', folder['detail'])
            card = sensitive('番号 4111 1111 1111 1111 の例')
            self.assertEqual(card['state'], 'inconclusive')
            for text in ('鍵 ' + 'ghp_' + 'B' * 24, 'メール person@example.com', '電話 03-1234-5678'):
                with self.subTest(text=text[:6]):
                    self.assertEqual(sensitive(text)['state'], 'failed')
            both = sensitive('person@example.com と data/eval-20261013-2351')
            self.assertEqual(both['state'], 'failed')
            self.assertIn('メールアドレス', both['detail'])
            self.assertEqual(sensitive('数字なしの普通の文章')['state'], 'passed')

    def test_digit_runs_only_warn_while_secret_email_phone_still_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            def sensitive(text):
                return [c for c in inspect_text('x.md', '# t\n' + text + '\n', tmp)['checks'] if c['id'] == 'DQA-05'][0]
            folder = sensitive('評価フォルダ data/eval-20261013-2351 を使用')      # 8+4 digits: a date, not a person
            self.assertEqual(folder['state'], 'inconclusive')
            self.assertIn('12桁', folder['detail'])
            card = sensitive('番号 4111 1111 1111 1111 の例')
            self.assertEqual(card['state'], 'inconclusive')
            for text in ('鍵 ' + 'ghp_' + 'B' * 24, 'メール person@example.com', '電話 03-1234-5678'):
                with self.subTest(text=text[:6]):
                    self.assertEqual(sensitive(text)['state'], 'failed')
            both = sensitive('person@example.com と data/eval-20261013-2351')
            self.assertEqual(both['state'], 'failed')
            self.assertIn('メールアドレス', both['detail'])
            self.assertEqual(sensitive('数字なしの普通の文章')['state'], 'passed')

    def test_clean_text_says_what_the_detector_cannot_see(self):
        with tempfile.TemporaryDirectory() as tmp:
            detail = [c for c in inspect_text('x.md', '# t\nok\n', tmp)['checks'] if c['id'] == 'DQA-05'][0]['detail']
        self.assertIn('検出できません', detail)

    def test_other_formats_are_inconclusive_not_passed(self):
        result = inspect_text('deck.pptx', 'binary', '.')
        self.assertEqual(overall(result['checks']), 'inconclusive')


class FileAndRecordTests(unittest.TestCase):
    def test_inspect_file_hashes_what_it_read_and_rejects_escapes_and_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / 'doc.md').write_text('# 案内\nok\n', encoding='utf-8')
            result = inspect_file(project, 'doc.md')
            self.assertEqual(len(result['sha256']), 64)
            for bad in ('../x.md', 'nope.md', str(project / 'doc.md')):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    inspect_file(project, bad)

    def test_oversized_and_non_utf8_files_are_inconclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / 'bin.md').write_bytes(b'\xff\xfe\x00bad')
            self.assertEqual(overall(inspect_file(project, 'bin.md')['checks']), 'inconclusive')

    def test_record_appends_a_line_and_changes_nothing_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            project, store = Path(tmp) / 'p', Path(tmp) / 'qa'
            project.mkdir()
            doc = project / 'doc.md'
            doc.write_text('# 案内\nok\n', encoding='utf-8')
            before = doc.read_bytes()
            record(store, 'job-1', inspect_file(project, 'doc.md'))
            record(store, 'job-1', inspect_file(project, 'doc.md'))
            lines = (store / 'document-qa.jsonl').read_text(encoding='utf-8').splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])['overall'], 'passed')
            self.assertEqual(doc.read_bytes(), before)
            self.assertEqual(sorted(p.name for p in project.iterdir()), ['doc.md'])


if __name__ == '__main__':
    unittest.main()
