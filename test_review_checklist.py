"""The user's approval of a reviewer's reported checks is passed on with the answer."""
import unittest

from team_questions import review_checklist_note

CHECKS = ['確認：操作記録を確認した', '確認：PDFの本文に個人名は無い', '未確認：画像のメタデータ', '推奨：機械チェックを追加']


class ReviewChecklistTest(unittest.TestCase):
    def test_approved_rejected_and_instruction(self):
        text = review_checklist_note(CHECKS, {'approved': [1], 'instruction': '操作記録が無いのは資料作成のため'})
        self.assertIn('利用者が承認した確認：\n- 確認：PDFの本文に個人名は無い', text)
        self.assertIn('利用者が承認しなかった確認（根拠・範囲を見直すこと）：\n- 確認：操作記録を確認した', text)
        self.assertNotIn('未確認：画像のメタデータ', text)  # only reported checks can be approved or rejected
        self.assertIn('操作記録が無いのは資料作成のため', text)

    def test_invalid_values_are_refused(self):
        for value in ({'approved': [9]}, {'approved': [0, 0]}, {'approved': ['0']}, {'approved': [], 'instruction': 'x' * 1001},
                      {'approved': [], 'other': 1}, []):
            with self.assertRaises(ValueError):
                review_checklist_note(CHECKS, value)


if __name__ == '__main__':
    unittest.main()
