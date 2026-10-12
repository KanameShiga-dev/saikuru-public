import tempfile
import unittest
from pathlib import Path

from eval.routing_comparison.case_document import INITIAL, quality


class DocumentCaseTests(unittest.TestCase):
    def test_stale_facts_fail_and_correct_facts_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'guide-old.md').write_text(INITIAL, encoding='utf-8')
            target = Path(directory) / 'guide.md'
            self.assertFalse(quality(directory))
            target.write_text('# 架空図書室の利用案内\n## 開館\n平日 9:00〜17:00。土日は休館。\n'
                              '## 貸出\n3冊まで14日。\n## 返却と予約\n返却は窓口。予約不可。\n', encoding='utf-8')
            self.assertTrue(quality(directory))
            target.write_text('# 架空図書室の利用案内\n## 開館\n平日 9:00〜17:00。土日は休館。\n'
                              '## 貸出\n3冊まで14日。\n## 返却と予約\n返却は窓口。予約は不可。\n', encoding='utf-8')
            self.assertTrue(quality(directory))
            target.write_text(target.read_text(encoding='utf-8').replace('予約は不可', '予約：不可'), encoding='utf-8')
            self.assertTrue(quality(directory))
            target.write_text(target.read_text(encoding='utf-8').replace('予約：不可', '予約できます'), encoding='utf-8')
            self.assertFalse(quality(directory))


if __name__ == '__main__':
    unittest.main()
