import tempfile
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from consultation_read_tools import BLOCKED, safe_path, screen_model_read
from team_attachments import provider_input
from team_egress_guard import (BLOCK_MARKER, batch_block_reason, findings,
                               redact_loopback_urls, require_safe_egress, sanitize_tool_output)
from egress_batch_hook import decision as batch_decision


class EgressGuardTests(unittest.TestCase):
    def test_personal_windows_profile_path_is_blocked_at_provider_input(self):
        payload = 'Read the file at C:\\Users\\fictional.user\\AppData\\Local\\probe.py'
        ctx = SimpleNamespace(agent_system_instructions='', agent_definition=SimpleNamespace(description=''))
        with self.assertRaises(ValueError) as caught:
            provider_input(ctx, payload, 'claude')
        self.assertIn('user_profile_path', str(caught.exception))
        self.assertNotIn('fictional.user', str(caught.exception))

    def test_other_absolute_project_paths_are_not_rejected(self):
        self.assertEqual(findings(r'Inspect C:\Projects\demo\src\main.py'), [])
        require_safe_egress(r'Inspect C:\Projects\demo\src\main.py')

    def test_local_launch_url_is_redacted_before_model_input(self):
        source = 'Launch http://127.0.0.1:8000/?token=fictional and https://localhost:8877/api.'
        clean = redact_loopback_urls(source)
        self.assertNotIn('127.0.0.1', clean)
        self.assertNotIn('localhost', clean)
        self.assertNotIn('fictional', clean)
        self.assertEqual(findings(clean), [])
        self.assertIn('[LOCAL_LOOPBACK_URL]', clean)
        self.assertIn('loopback_url', findings(source))

    def test_sensitive_categories_are_blocked_without_echoing_values(self):
        cases = [
            ('api_key=synthetic_secret_value_123', 'secret'),
            ('access_token=synthetic_token_value_123', 'secret'),
            ('contact test@example.com', 'email'),
            ('connect to 10.22.33.44', 'private_ip'),
            ('service https://localhost:8877/api', 'loopback_url'),
        ]
        for value, label in cases:
            with self.subTest(label=label):
                with self.assertRaises(ValueError) as caught:
                    require_safe_egress(value)
                self.assertIn(label, str(caught.exception))
                self.assertNotIn(value, str(caught.exception))

    def test_tool_output_redacts_user_profile_path_before_provider_return(self):
        value = {'stdout': 'warning in C:\\Users\\fictional\\AppData\\Local\\Python\\Lib\\file.py',
                 'stderr': '', 'interrupted': False}
        clean, labels, changed = sanitize_tool_output(value)
        self.assertTrue(changed)
        self.assertIn('user_profile_path', labels)
        self.assertNotIn('fictional', clean['stdout'])
        self.assertNotIn('C:\\Users\\', clean['stdout'])
        self.assertEqual(findings(clean), [])

    def test_loopback_url_in_tool_output_is_not_withheld_and_batch_continues(self):
        value = {'file': {'content': 'Open http://127.0.0.1:8000/ or https://localhost:8877/api after starting.'}}
        clean, labels, changed = sanitize_tool_output(value)
        self.assertFalse(changed)
        self.assertNotIn('loopback_url', labels)
        self.assertEqual(clean, value)
        self.assertEqual(batch_block_reason({'tool_calls': [{'tool_name': 'Read', 'tool_response': clean}]}), '')

    def test_loopback_url_does_not_hide_a_real_secret_in_tool_output(self):
        value = {'file': {'content': 'http://127.0.0.1:8000/ api_key=synthetic_secret_value_123'}}
        clean, labels, _changed = sanitize_tool_output(value)
        self.assertIn('secret', labels)
        self.assertNotIn('synthetic_secret_value_123', str(clean))
        self.assertIn('protected_output_withheld',
                      batch_block_reason({'tool_calls': [{'tool_name': 'Read', 'tool_response': clean}]}))

    def test_unredactable_tool_output_is_withheld_and_batch_stops(self):
        value = {'stdout': 'api_key=synthetic_secret_value_123', 'stderr': '', 'interrupted': False}
        clean, labels, changed = sanitize_tool_output(value)
        self.assertTrue(changed)
        self.assertIn('secret', labels)
        self.assertNotIn('synthetic_secret_value_123', str(clean))
        self.assertIn(BLOCK_MARKER, clean['stdout'])
        payload = {'tool_calls': [{'tool_name': 'Bash', 'tool_response': clean}]}
        reason = batch_block_reason(payload)
        self.assertIn('protected_output_withheld', reason)
        result = batch_decision(payload)
        self.assertEqual(result['decision'], 'block')
        self.assertIn('reason', result)
        self.assertNotIn('hookSpecificOutput', result)
        self.assertNotIn('synthetic_secret_value_123', result['reason'])

    def test_batch_stops_when_unredacted_profile_path_remains(self):
        payload = {'tool_calls': [{'tool_name': 'Bash',
                                   'tool_response': 'warning C:\\Users\\fictional\\AppData\\Local\\Python\\x.py'}]}
        self.assertIn('user_profile_path', batch_block_reason(payload))
        result = batch_decision(payload)
        self.assertEqual(result['decision'], 'block')
        self.assertNotIn('hookSpecificOutput', result)

    def test_result_hook_emits_rewritten_output_without_network_call(self):
        hook = Path(__file__).resolve().parents[1] / 'result_hook.py'
        payload = {'hook_event_name': 'PostToolUse', 'tool_name': 'Read',
                   'tool_response': {'content': 'warning C:\\Users\\fictional\\AppData\\Local\\Python\\x.py'}}
        env = os.environ.copy()
        env['PYTHONPATH'] = str(hook.parent)
        result = subprocess.run([sys.executable, str(hook)], input=json.dumps(payload), text=True,
                                capture_output=True, cwd=hook.parent, env=env, timeout=10,
                                encoding='utf-8', errors='replace')
        self.assertEqual(result.returncode, 0)
        output = json.loads(result.stdout)
        clean = output['hookSpecificOutput']['updatedToolOutput']['content']
        self.assertNotIn('fictional', clean)
        self.assertNotIn('C:\\Users\\', clean)

    def test_batch_fails_closed_on_missing_payload_shape(self):
        result = batch_decision({})
        self.assertEqual(result['decision'], 'block')
        self.assertEqual(batch_block_reason({}), 'tool_batch_unavailable')

    def test_batch_fails_closed_on_malformed_call(self):
        payload = {'tool_calls': [{'tool_name': 'Bash'}]}
        result = batch_decision(payload)
        self.assertEqual(result['decision'], 'block')
        self.assertIn('tool_batch_unavailable', result['reason'])

    def test_batch_allows_normal_result_without_stop_output(self):
        payload = {'tool_calls': [{'tool_name': 'Read', 'tool_response': 'fictional sample data'}]}
        self.assertIsNone(batch_decision(payload))

    def test_batch_hook_subprocess_outputs_top_level_contract(self):
        hook = Path(__file__).resolve().parents[1] / 'egress_batch_hook.py'
        env = os.environ.copy()
        env['PYTHONPATH'] = str(hook.parent)
        payload = {'tool_calls': [{'tool_name': 'Bash',
                                  'tool_response': 'api_key=synthetic_secret_value_123'}]}
        result = subprocess.run([sys.executable, str(hook)], input=json.dumps(payload), text=True,
                                capture_output=True, cwd=hook.parent, env=env, timeout=10,
                                encoding='utf-8', errors='replace')
        self.assertEqual(result.returncode, 0)
        output = json.loads(result.stdout)
        self.assertEqual(output['decision'], 'block')
        self.assertIn('reason', output)
        self.assertNotIn('hookSpecificOutput', output)
        self.assertNotIn('synthetic_secret_value_123', result.stdout)

    def test_batch_hook_invalid_json_exits_nonzero_without_echo(self):
        hook = Path(__file__).resolve().parents[1] / 'egress_batch_hook.py'
        env = os.environ.copy()
        env['PYTHONPATH'] = str(hook.parent)
        result = subprocess.run([sys.executable, str(hook)], input='{bad json', text=True,
                                capture_output=True, cwd=hook.parent, env=env, timeout=10,
                                encoding='utf-8', errors='replace')
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('{bad json', result.stdout + result.stderr)

    def test_project_read_sanitizes_profile_paths_and_rejects_secrets(self):
        clean = screen_model_read({'path': 'src/module.py',
                                   'content': 'trace C:\\Users\\fictional\\AppData\\Local\\Python\\Lib\\x.py'})
        self.assertNotIn('fictional', clean['content'])
        with self.assertRaises(ValueError):
            screen_model_read({'path': 'src/config.py', 'content': 'api_key=synthetic_secret_value_123'})

    def test_dot_local_is_not_exposed_by_project_read_broker(self):
        self.assertIn('.local', BLOCKED)
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            private = root / '.local'
            private.mkdir()
            target = private / 'probe.py'
            target.write_text('synthetic', encoding='utf-8')
            with self.assertRaises(ValueError):
                safe_path(root, '.local/probe.py')

    def test_safe_relative_text_and_simple_task_are_not_blocked(self):
        require_safe_egress({'instruction': 'Run the approved tests and summarize pass/fail counts.',
                             'path': 'tests/test_search.py'})


if __name__ == '__main__':
    unittest.main()
