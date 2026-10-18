import sys
import unittest

from claude_hook_settings import build_egress_hook_settings, validate_egress_hook_settings


class ClaudeHookSettingsTests(unittest.TestCase):
    def test_runtime_and_wrapped_e2e_share_event_contract(self):
        runtime = build_egress_hook_settings(
            sys.executable, ['runtime-result.py'], ['runtime-result.py'],
            ['runtime-batch.py'])
        e2e = build_egress_hook_settings(
            sys.executable, ['observer.py', 'PostToolUse', 'result.py'],
            ['observer.py', 'PostToolUseFailure', 'result.py'],
            ['observer.py', 'PostToolBatch', 'batch.py'])

        def contract(settings):
            hooks = settings['hooks']
            return {
                'PostToolUse': (hooks['PostToolUse'][0]['matcher'],
                                hooks['PostToolUse'][0]['hooks'][0]['timeout'],
                                hooks['PostToolUse'][0]['hooks'][0].get('onFailure')),
                'PostToolUseFailure': (hooks['PostToolUseFailure'][0]['matcher'],
                                       hooks['PostToolUseFailure'][0]['hooks'][0]['timeout'],
                                       hooks['PostToolUseFailure'][0]['hooks'][0].get('onFailure')),
                'PostToolBatch': (hooks['PostToolBatch'][0].get('matcher'),
                                  hooks['PostToolBatch'][0]['hooks'][0]['timeout'],
                                  hooks['PostToolBatch'][0]['hooks'][0].get('onFailure')),
            }

        self.assertEqual(contract(runtime), contract(e2e))

    def test_only_post_tool_batch_fails_closed(self):
        hooks = build_egress_hook_settings('python', ['result.py'], ['result.py'],
                                           ['batch.py'])['hooks']
        self.assertIsNone(hooks['PostToolUse'][0]['hooks'][0].get('onFailure'))
        self.assertIsNone(hooks['PostToolUseFailure'][0]['hooks'][0].get('onFailure'))
        self.assertEqual(hooks['PostToolBatch'][0]['hooks'][0]['onFailure'], 'block')
        self.assertNotIn('matcher', hooks['PostToolBatch'][0])

    def test_missing_egress_configuration_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'egress_hooks_missing'):
            validate_egress_hook_settings({'hooks': {}})
        settings = build_egress_hook_settings('python', ['result.py'], ['result.py'], ['batch.py'])
        settings['hooks'].pop('PostToolBatch')
        with self.assertRaisesRegex(ValueError, 'PostToolBatch'):
            validate_egress_hook_settings(settings)


if __name__ == '__main__':
    unittest.main()
