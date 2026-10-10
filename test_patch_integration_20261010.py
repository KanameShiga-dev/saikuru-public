import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import team_dlp
from team_security_audit import result_message
from team_outbound_guard import classify, excepted

class IntegrationTests(unittest.TestCase):
    def test_secret_assignment_is_redacted_before_tail(self):
        value = 'SENSITIVE_VALUE_' + 'x' * 24000
        result = result_message('python check.py', 0, 'password=' + value + '\nPASS')
        self.assertNotIn('SENSITIVE_VALUE', result)
        self.assertNotIn('x' * 100, result)
        self.assertIn('PASS', result)
        self.assertIn('伏せ字', result)

    def test_token_assignment_in_command_is_not_recorded(self):
        result = result_message('python script.py --token=synthetic-private-value', 0, 'OK')
        self.assertNotIn('synthetic-private-value', result)

    def test_broken_masking_policy_does_not_log_original(self):
        with patch('team_security_audit._patterns', side_effect=ValueError('broken policy')):
            result = result_message('private command', 1, 'private output')
        self.assertNotIn('private command', result)
        self.assertNotIn('private output', result)
        self.assertIn('終了コード 1', result)

    def test_permission_exception_cannot_append_command(self):
        allowed = 'python -m pip install sample-package'
        policy = {'outbound_exceptions': [{'category': 'install', 'contains': allowed}]}
        self.assertIsNone(excepted(allowed + '; Set-ExecutionPolicy Unrestricted', 'install', policy))
        self.assertEqual(classify('Set-ExecutionPolicy Unrestricted')[0], 'system')

    def test_security_policy_terms_are_masked(self):
        with tempfile.TemporaryDirectory() as folder:
            policy = Path(folder) / 'security-policy.json'
            policy.write_text(json.dumps({'confidential_terms': ['internal-client-fixture'], 'outbound_exceptions': []}))
            with patch.object(team_dlp, 'POLICY', policy):
                result = result_message('echo internal-client-fixture', 0, 'internal-client-fixture PASS')
            self.assertNotIn('internal-client-fixture', result)
            self.assertIn('PASS', result)

if __name__ == '__main__':
    unittest.main()
