"""Live run 2026-11-10: a restart expired the evidence of tests that start another process (fictional files only)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import team_validation_evidence as evidence

COMMANDS = ['python -m unittest']


class RestartEvidenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='evidence-restart-')
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name) / 'project'
        self.data = Path(temp.name) / 'data'
        self.project.mkdir(); self.data.mkdir()
        # subprocess puts every environment value in scope ('all'), so the fingerprint uses the key.
        (self.project / 'tool.py').write_text('import subprocess\n', encoding='utf-8')
        key = patch.object(evidence, '_ENVIRONMENT_HMAC_KEY', evidence._ENVIRONMENT_HMAC_KEY)
        key.start(); self.addCleanup(key.stop)

    def restart(self, data_directory=True):
        """What a new server process does: a fresh in-memory key, then the data folder's key."""
        evidence._ENVIRONMENT_HMAC_KEY = os.urandom(32)
        if data_directory:
            evidence.use_data_directory_key(self.data)

    def test_evidence_survives_a_restart_with_the_data_folder_key(self):
        self.restart()
        recorded = evidence.snapshot(self.project, None, COMMANDS)
        self.assertEqual(recorded['environment_scope'], 'all')
        self.restart()
        self.assertEqual(evidence.validity(self.project, recorded, COMMANDS)[:1], (True,))

    def test_without_the_data_folder_key_a_restart_expires_it(self):
        self.restart(data_directory=False)
        recorded = evidence.snapshot(self.project, None, COMMANDS)
        self.restart(data_directory=False)
        self.assertFalse(evidence.validity(self.project, recorded, COMMANDS)[0])

    def test_a_changed_environment_value_still_expires_it_after_a_restart(self):
        self.restart()
        recorded = evidence.snapshot(self.project, None, COMMANDS)
        self.restart()
        with patch.dict(os.environ, {'FICTIONAL_SETTING': 'changed'}):
            self.assertFalse(evidence.validity(self.project, recorded, COMMANDS)[0])

    def test_starting_from_another_folder_or_terminal_keeps_it_valid(self):
        with patch.dict(os.environ, {'PWD': '/fictional/first', 'OLDPWD': '/fictional/before', '_': 'python', 'SHLVL': '1'}):
            self.restart()
            recorded = evidence.snapshot(self.project, None, COMMANDS)
        with patch.dict(os.environ, {'PWD': '/fictional/second', 'OLDPWD': '/fictional/first', '_': 'pythonw', 'SHLVL': '3'}):
            self.restart()
            self.assertEqual(evidence.validity(self.project, recorded, COMMANDS)[:1], (True,))

    def test_a_shell_variable_the_project_reads_by_name_still_counts(self):
        (self.project / 'tool.py').write_text('import os\nFOLDER = os.environ.get("PWD")\n', encoding='utf-8')
        with patch.dict(os.environ, {'PWD': '/fictional/first'}):
            self.restart()
            recorded = evidence.snapshot(self.project, None, COMMANDS)
            self.assertEqual(recorded['environment_scope'], 'selected')
        with patch.dict(os.environ, {'PWD': '/fictional/second'}):
            self.restart()
            self.assertFalse(evidence.validity(self.project, recorded, COMMANDS)[0])

    def test_another_data_folder_has_another_key_and_no_value_is_stored(self):
        with patch.dict(os.environ, {'FICTIONAL_SETTING': 'fictional-value-not-to-store'}):
            self.restart()
            recorded = evidence.snapshot(self.project, None, COMMANDS)
            other = self.data.parent / 'other-data'; other.mkdir()
            evidence.use_data_directory_key(other)
            self.assertFalse(evidence.validity(self.project, recorded, COMMANDS)[0])
        self.assertNotIn('fictional-value-not-to-store', str(recorded))
        self.assertEqual(len((self.data / evidence.ENVIRONMENT_KEY_FILE).read_bytes()), 32)
        self.assertNotEqual((self.data / evidence.ENVIRONMENT_KEY_FILE).read_bytes(),
                            (other / evidence.ENVIRONMENT_KEY_FILE).read_bytes())

    def test_a_damaged_key_file_is_refused(self):
        (self.data / evidence.ENVIRONMENT_KEY_FILE).write_bytes(b'short')
        with self.assertRaises(ValueError):
            evidence.use_data_directory_key(self.data)


if __name__ == '__main__':
    unittest.main()
