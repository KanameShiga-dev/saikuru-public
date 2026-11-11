"""Documents are saved only when their text is Japanese: no kanji that Japanese lacks, no lost characters."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from team_document_capabilities import allowed_kanji_argument, non_japanese_kanji, require_japanese_text

ROOT = Path(__file__).resolve().parents[1]
SIMPLIFIED = '这是一个问题'  # Fictional sample of simplified Chinese.


class JapaneseTextTests(unittest.TestCase):
    def test_simplified_forms_are_found_and_japanese_kanji_are_not(self):
        self.assertEqual(non_japanese_kanji(SIMPLIFIED + 'と課題'), '这问题')
        # Old forms, name kanji, kana, Latin text, symbols and emoji are all left alone.
        self.assertEqual(non_japanese_kanji('直骨角 海 鷗﨑髙𠮷 社會 かな カナ ABC ①〜☆😀'), '')

    def test_every_string_of_a_tool_call_is_checked(self):
        require_japanese_text({'path': '架空.md', 'content': '架空の資料です。'})
        for arguments in ({'path': 'a.md', 'content': SIMPLIFIED},
                          {'path': 'a.pptx', 'slides': [{'title': '計画', 'bullets': ['时间の管理']}]},
                          {'path': 'a.xlsx', 'sheets': [{'name': '表', 'rows': [['品名', 1], ['软件', 2]]}]}):
            with self.assertRaisesRegex(ValueError, '日本語にない漢字'):
                require_japanese_text(arguments)
        with self.assertRaisesRegex(ValueError, '「这」「问」「题」'):
            require_japanese_text({'path': 'a.md', 'content': SIMPLIFIED})

    def test_what_the_person_wrote_in_the_request_passes(self):
        allowed = allowed_kanji_argument({'title': '架空の資料', 'goal': '「习」という字の成り立ちを説明する'})
        self.assertEqual(allowed, '4e60')
        self.assertTrue(allowed.isascii())
        require_japanese_text({'path': 'a.md', 'content': '「习」は「習」の簡体字です。'}, allowed)
        with self.assertRaisesRegex(ValueError, '「这」'):
            require_japanese_text({'path': 'a.md', 'content': '习と这'}, allowed)
        self.assertEqual(allowed_kanji_argument({'title': '架空', 'goal': '日本語だけの依頼'}), '')

    def test_lost_characters_and_html_without_the_japanese_mark_are_refused(self):
        with self.assertRaisesRegex(ValueError, '文字化け'):
            require_japanese_text({'path': 'a.md', 'content': '壊れた�字'})
        with self.assertRaisesRegex(ValueError, 'lang="ja"'):
            require_japanese_text({'path': 'a.html', 'content': '<html><body>本文</body></html>'})
        with self.assertRaisesRegex(ValueError, 'lang="ja"'):
            require_japanese_text({'path': 'a.html', 'content': '<p>本文だけの断片</p>'})
        for opening in ('<html lang="ja">', "<html lang='ja-JP' class=\"x\">", '<HTML LANG=ja>'):
            require_japanese_text({'path': 'a.html', 'content': '<!doctype html>' + opening + '<body>本文</body></html>'})
        require_japanese_text({'path': 'a.md', 'content': '<html> を説明する文'})  # Only .html files carry the mark.


class DocumentToolTests(unittest.TestCase):
    def call(self, folder, arguments, *extra):
        request = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                              'params': {'name': 'write_document', 'arguments': arguments}}, ensure_ascii=False)
        ran = subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'document_tools.py'), str(folder), str(folder),
                              '--job-id', 'a' * 32, '--write', *extra],
                             input=request + '\n', capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        return json.loads(ran.stdout.strip().splitlines()[-1])

    def test_the_tool_saves_japanese_and_refuses_the_rest_before_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            refused = self.call(folder, {'path': 'bad.md', 'content': SIMPLIFIED})
            self.assertIn('日本語にない漢字', refused['error']['message'])
            self.assertFalse((folder / 'bad.md').exists())
            self.assertIn('result', self.call(folder, {'path': 'good.md', 'content': '架空の資料です。'}))
            self.assertEqual((folder / 'good.md').read_text(encoding='utf-8'), '架空の資料です。')
            self.assertIn('result', self.call(folder, {'path': 'asked.md', 'content': '「习」の説明'}, '--allow-kanji', '4e60'))
            self.assertIn('lang="ja"', self.call(folder, {'path': 'page.html', 'content': '<p>断片</p>'})['error']['message'])
            self.assertFalse((folder / 'page.html').exists())


if __name__ == '__main__':
    unittest.main()
