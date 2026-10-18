import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import team_models
from team_models import ModelCatalog, ModelDiscoveryError
from team_usage import UsageUnavailable


SECRET_SENTINEL = 'not-a-real-secret-value-for-test-only'


def catalog_response(rows, subtype='success'):
    return {'type': 'control_response', 'response': {
        'request_id': 'catalog', 'subtype': subtype,
        'response': {'models': rows} if subtype == 'success' else None,
        'error': None if subtype == 'success' else 'OAuth session expired'}}


class FakeProcess:
    instances = []
    next_message = None
    init_error = None

    def __init__(self, command, cwd, env=None):
        if type(self).init_error:
            raise type(self).init_error
        self.command = command
        self.cwd = Path(cwd)
        self.env = dict(env or {})
        self.stderr = []
        self.closed = False
        type(self).instances.append(self)

    def send(self, value):
        self.sent = value

    def receive(self, _deadline):
        value = type(self).next_message
        if isinstance(value, BaseException):
            raise value
        return value

    def close(self):
        self.closed = True


class ClaudeModelCatalogTests(unittest.TestCase):
    def setUp(self):
        FakeProcess.instances = []
        FakeProcess.next_message = catalog_response([
            {'value': 'sonnet', 'displayName': 'Sonnet 5.5',
             'supportedEffortLevels': ['low', 'medium', 'high']},
            {'value': 'haiku', 'displayName': 'Haiku 5.5',
             'supportedEffortLevels': ['medium']},
        ])
        FakeProcess.init_error = None
        self.patchers = [
            patch('team_models.Process', FakeProcess),
            patch('team_models.stored_token_state', return_value={'state': 'ok'}),
            patch('team_models.claude_env', return_value={
                'CLAUDE_CONFIG_DIR': 'isolated-profile',
                'CLAUDE_CODE_OAUTH_TOKEN': SECRET_SENTINEL,
                'ANTHROPIC_API_KEY': SECRET_SENTINEL,
            }),
        ]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_uses_existing_root_and_long_lived_auth_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(team_models, 'ROOT', Path(directory)):
                rows = team_models.claude_models(['claude.exe'])
            process = FakeProcess.instances[-1]
            self.assertEqual(str(process.cwd), directory)
            self.assertTrue(process.cwd.is_dir())
            self.assertFalse((Path(directory) / 'sample-project').exists())
        self.assertEqual(process.env['CLAUDE_CODE_OAUTH_TOKEN'], SECRET_SENTINEL)
        self.assertNotIn('ANTHROPIC_API_KEY', process.env)
        self.assertEqual(rows, [
            {'id': 'sonnet', 'label': 'Sonnet 5.5', 'efforts': ['low', 'medium']},
            {'id': 'haiku', 'label': 'Haiku 5.5', 'efforts': ['medium']},
        ])
        self.assertTrue(process.closed)

    def test_missing_long_lived_auth_does_not_start_cli_or_fall_back(self):
        with patch('team_models.stored_token_state', return_value={'state': 'missing'}):
            with self.assertRaises(ModelDiscoveryError) as caught:
                team_models.claude_models(['claude.exe'])
        self.assertEqual(caught.exception.code, 'auth_unavailable')
        self.assertEqual(FakeProcess.instances, [])

    def test_missing_cli_has_specific_category(self):
        with self.assertRaises(ModelDiscoveryError) as caught:
            team_models.claude_models([])
        self.assertEqual(caught.exception.code, 'cli_unavailable')

    def test_invalid_root_is_rejected_without_creating_a_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / 'missing'
            with patch.object(team_models, 'ROOT', missing):
                with self.assertRaises(ModelDiscoveryError) as caught:
                    team_models.claude_models(['claude.exe'])
        self.assertEqual(caught.exception.code, 'workdir_invalid')
        self.assertFalse(missing.exists())
        self.assertEqual(FakeProcess.instances, [])

    def test_process_start_failure_is_categorized(self):
        FakeProcess.init_error = OSError('private path detail')
        with self.assertRaises(ModelDiscoveryError) as caught:
            team_models.claude_models(['claude.exe'])
        self.assertEqual(caught.exception.code, 'cli_start_failed')
        self.assertNotIn('private path detail', str(caught.exception))

    def test_timeout_is_categorized(self):
        FakeProcess.next_message = UsageUnavailable('timeout')
        with self.assertRaises(ModelDiscoveryError) as caught:
            team_models.claude_models(['claude.exe'])
        self.assertEqual(caught.exception.code, 'timeout')

    def test_auth_rejection_is_distinguished_from_protocol_error(self):
        FakeProcess.next_message = catalog_response([], subtype='error')
        with self.assertRaises(ModelDiscoveryError) as caught:
            team_models.claude_models(['claude.exe'])
        self.assertEqual(caught.exception.code, 'auth_rejected')

    def test_invalid_catalog_shape_does_not_create_candidates(self):
        FakeProcess.next_message = catalog_response(None)
        with self.assertRaises(ModelDiscoveryError) as caught:
            team_models.claude_models(['claude.exe'])
        self.assertEqual(caught.exception.code, 'catalog_incompatible')

    def test_empty_catalog_is_distinguished(self):
        FakeProcess.next_message = catalog_response([])
        catalog = ModelCatalog({'claude': ['claude.exe']})
        result = catalog.get('claude', force=True)
        self.assertEqual(result['models'], [])
        self.assertEqual(result['error'], 'catalog_empty')
        self.assertIn('候補', result['note'])

    def test_auth_failure_log_and_api_result_do_not_contain_secret(self):
        FakeProcess.next_message = catalog_response([], subtype='error')
        catalog = ModelCatalog({'claude': ['claude.exe']})
        with self.assertLogs(team_models.log, level='WARNING') as captured:
            result = catalog.get('claude', force=True)
        combined = '\n'.join(captured.output) + result['note']
        self.assertEqual(result['error'], 'auth_rejected')
        self.assertNotIn(SECRET_SENTINEL, combined)

    def test_cache_and_force_refresh(self):
        calls = []

        def models(_command):
            calls.append(len(calls) + 1)
            return [{'id': 'sonnet', 'label': 'Sonnet', 'efforts': ['medium']}]

        with patch('team_models.claude_models', side_effect=models):
            catalog = ModelCatalog({'claude': ['claude.exe']})
            first = catalog.get('claude')
            second = catalog.get('claude')
            third = catalog.get('claude', force=True)
        self.assertEqual(len(calls), 2)
        self.assertIs(first, second)
        self.assertIsNot(first, third)

    def test_other_provider_catalog_remains_available(self):
        result = ModelCatalog({'gemini': ['gemini']}).get('gemini', force=True)
        self.assertTrue(result['models'])
        self.assertNotIn('error', result)

    def test_codex_and_copilot_catalogs_and_forced_refresh_remain_available(self):
        row = {'id': 'model-one', 'label': 'Model One', 'efforts': ['medium']}
        with patch('team_models.codex_models', return_value=[row]) as codex, \
                patch('team_models.copilot_models', return_value=[row]) as copilot:
            catalog = ModelCatalog({'codex': ['codex'], 'copilot': ['copilot']})
            self.assertEqual(catalog.get('codex')['models'], [row])
            self.assertEqual(catalog.get('codex', force=True)['models'], [row])
            self.assertEqual(catalog.get('copilot')['models'], [row])
            self.assertEqual(catalog.get('copilot', force=True)['models'], [row])
        self.assertEqual(codex.call_count, 2)
        self.assertEqual(copilot.call_count, 2)

    def test_validation_allows_only_returned_model_and_effort(self):
        with patch('team_models.claude_models', return_value=[
            {'id': 'sonnet', 'label': 'Sonnet', 'efforts': ['low', 'medium']}]):
            catalog = ModelCatalog({'claude': ['claude.exe']})
            catalog.validate({'adapter': 'claude', 'model': 'sonnet', 'effort': 'medium'})
            with self.assertRaises(ValueError):
                catalog.validate({'adapter': 'claude', 'model': 'imaginary', 'effort': 'medium'})
            with self.assertRaises(ValueError):
                catalog.validate({'adapter': 'claude', 'model': 'sonnet', 'effort': 'high'})


if __name__ == '__main__':
    unittest.main()
