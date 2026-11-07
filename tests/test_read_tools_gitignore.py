"""Planning and review can read .gitignore to confirm what is kept out of publication (trial 5, 2026-11-07)."""
import tempfile
import unittest
from pathlib import Path

from consultation_read_tools import safe_path


class GitignoreReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / '.gitignore').write_text('.local/\n*.sqlite3\n', encoding='utf-8')
        (self.root / 'sub').mkdir()
        (self.root / 'sub' / '.gitignore').write_text('build/\n', encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_gitignore_can_be_read(self):
        self.assertEqual(safe_path(self.root, '.gitignore'), self.root / '.gitignore')
        self.assertEqual(safe_path(self.root, 'sub/.gitignore'), self.root / 'sub' / '.gitignore')

    def test_other_files_without_a_listed_suffix_are_still_refused(self):
        for name, text in (('.npmrc', 'x'), ('.gitconfig', 'x'), ('notes', 'x'), ('.gitignore.bak', 'x')):
            (self.root / name).write_text(text, encoding='utf-8')
            with self.assertRaises(ValueError, msg=name):
                safe_path(self.root, name)

    def test_protected_places_and_names_stay_refused(self):
        for folder in ('.git', '.local', 'secrets'):
            (self.root / folder).mkdir()
            (self.root / folder / '.gitignore').write_text('x', encoding='utf-8')
            with self.assertRaises(ValueError, msg=folder):
                safe_path(self.root, folder + '/.gitignore')
        (self.root / '.env').write_text('x', encoding='utf-8')
        with self.assertRaises(ValueError):
            safe_path(self.root, '.env')


if __name__ == '__main__':
    unittest.main()
