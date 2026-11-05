"""M09 live run: the Claude builder's test temp folder must not expire command evidence (fictional files only)."""
import tempfile
import unittest
from pathlib import Path

from team_validation_evidence import snapshot, validity

COMMANDS = ['python -m unittest']


class ScratchFolderEvidenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='evidence-scratch-')
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name)
        (self.project / 'catalog.py').write_text('ITEMS = {"1": "apple"}\n', encoding='utf-8')
        self.scratch = self.project / '.local' / 'test-tmp' / 'claude'
        self.scratch.mkdir(parents=True)
        (self.scratch / 'cache-break-state-a.json').write_text('{"n": 1}', encoding='utf-8')
        self.recorded = snapshot(self.project, None, COMMANDS)

    def test_scratch_folder_is_not_recorded(self):
        self.assertEqual(sorted(self.recorded['files']), ['catalog.py'])

    def test_cli_cache_written_after_the_run_keeps_the_evidence_valid(self):
        (self.scratch / 'cache-break-state-a.json').write_text('{"n": 2}', encoding='utf-8')
        (self.scratch / 'cache-break-state-b.json').write_text('{"n": 1}', encoding='utf-8')
        self.assertEqual(validity(self.project, self.recorded, COMMANDS)[:1], (True,))

    def test_code_change_still_expires_the_evidence(self):
        (self.project / 'catalog.py').write_text('ITEMS = {"1": "banana"}\n', encoding='utf-8')
        self.assertFalse(validity(self.project, self.recorded, COMMANDS)[0])

    def test_other_local_files_are_still_recorded(self):
        (self.project / '.local' / 'notes.txt').write_text('fictional', encoding='utf-8')
        self.assertFalse(validity(self.project, self.recorded, COMMANDS)[0])


if __name__ == '__main__':
    unittest.main()
