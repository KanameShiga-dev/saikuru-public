import unittest

from team_handoff import skill_matches

SKILL = ('参考PDFのデザイン学習と統一デザインの反映（PPTX・PDF・MP4・Word・HTML） '
         '参考PDFのデザイン（配色・書体・文字サイズ・余白）に合わせる、デザインを統一する、同じ見た目で資料を作る依頼。')


class SkillMatchTest(unittest.TestCase):
    def test_japanese_requests_without_spaces(self):
        self.assertTrue(skill_matches('参考PDFのデザインに合わせてスライドを作ってください', SKILL))
        self.assertTrue(skill_matches('デザインを統一して報告書を作る', SKILL))

    def test_unrelated_requests(self):
        self.assertFalse(skill_matches('CSVを出力するスクリプトを直してください', SKILL))
        self.assertFalse(skill_matches('ログイン画面のバグを修正する', SKILL))
        # Hiragana-only pairs (する・して…) alone never match.
        self.assertFalse(skill_matches('それをしてください', 'これをしてください'))

    def test_latin_words_still_match(self):
        self.assertTrue(skill_matches('make a pptx deck', SKILL))


if __name__ == '__main__':
    unittest.main()
