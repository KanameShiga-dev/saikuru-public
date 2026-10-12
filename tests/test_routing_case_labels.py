import tempfile
import unittest
from pathlib import Path

from eval.routing_comparison.case_labels import FILE, INITIAL, quality


class StringCaseTests(unittest.TestCase):
    def test_known_defect_and_fixed_version(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / FILE
            target.write_text(INITIAL, encoding='utf-8')
            self.assertFalse(quality(directory))
            target.write_text('def normalize_labels(values):\n'
                              '    result = []\n'
                              '    for value in values:\n'
                              '        normalized = value.strip().lower()\n'
                              '        if normalized:\n'
                              '            result.append(normalized)\n'
                              '    return result\n', encoding='utf-8')
            self.assertTrue(quality(directory))


if __name__ == '__main__':
    unittest.main()
