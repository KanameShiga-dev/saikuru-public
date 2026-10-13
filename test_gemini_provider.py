"""Gemini provider (emergency reserve on the free tier): scope, Input Guard, model fallback, format repair, isolation."""
import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import team_adapters
from team_adapters import GeminiAdapter, ProviderError
from team_config import GEMINI_MODELS, default_config, validate_config
from team_engine import RESULT_SCHEMA

GEMINI = {'adapter': 'gemini', 'model': 'gemini-3.7-flash', 'effort': 'medium'}
FULL = {'status': 'done', 'summary': '指摘なし', 'checks': ['確認：テスト'], 'pending_checks': [], 'question': '',
        'questions': [], 'context_updates': []}


def config_with(role, profile):
    config = default_config()
    config['provider_settings'] = {'enabled': ['codex', 'claude', 'gemini'], 'copilot_monthly_credits': None}
    config['profiles']['gemini-x'] = profile
    config['roles'][role] = 'gemini-x'
    return config


class SettingsTest(unittest.TestCase):
    def test_planner_and_reviewer_are_allowed(self):
        for role in ('planner', 'reviewer'):
            validate_config(config_with(role, copy.deepcopy(GEMINI)))

    def test_builder_and_researcher_are_refused(self):
        for role in ('builder', 'researcher'):
            with self.assertRaisesRegex(ValueError, 'Gemini'):
                validate_config(config_with(role, copy.deepcopy(GEMINI)))


class AdapterTest(unittest.TestCase):
    def ctx(self, **values):
        definition = SimpleNamespace(sandbox='read-only', tools=())
        self.events = []
        base = dict(task={'role': 'reviewer', 'profile': dict(GEMINI)}, agent_definition=definition, text_only=True,
                    attachment_ids=[], command=['gemini-not-started'], agent_system_instructions='',
                    agent_started=lambda *a: None, event=self.events.append, check=lambda: None, agent_run={})
        base.update(values)
        return SimpleNamespace(**base)

    def run_with(self, answers, ctx=None, prompt='レビューしてください'):
        calls = []

        def fake(adapter, ctx, model, text, cwd, env, repair=False):
            calls.append((model, repair))
            answer = answers[len(calls) - 1]
            if isinstance(answer, ProviderError):
                raise answer
            return answer
        with mock.patch.object(GeminiAdapter, '_call', fake), \
                mock.patch('team_claude_auth.read_stored_token', return_value='test-key'):
            result = GeminiAdapter().run(ctx or self.ctx(), prompt, RESULT_SCHEMA)
        return result, calls

    def test_refuses_out_of_scope_before_starting(self):
        cases = [dict(attachment_ids=['a']), dict(reference_images=['i']), dict(artifact_format='x'),
                 dict(document_scope=True), dict(text_only=False),
                 dict(task={'role': 'builder', 'profile': dict(GEMINI)}),
                 dict(task={'role': 'researcher', 'profile': dict(GEMINI)})]
        for values in cases:
            with self.subTest(values=list(values)):
                with self.assertRaisesRegex(ProviderError, '切り替えてください'):
                    GeminiAdapter().run(self.ctx(**values), 'x', {})

    def test_input_guard_blocks_before_sending(self):
        with mock.patch('team_dlp.scan_input', return_value={'block': ['秘密鍵'], 'confirm': []}):
            with mock.patch.object(GeminiAdapter, '_call') as call:
                with self.assertRaisesRegex(ProviderError, '送りません'):
                    GeminiAdapter().run(self.ctx(), 'x', {})
                call.assert_not_called()

    def test_missing_key_is_refused(self):
        with mock.patch('team_claude_auth.read_stored_token', return_value=''):
            with self.assertRaisesRegex(ProviderError, 'APIキー'):
                GeminiAdapter().run(self.ctx(), 'x', {})

    def test_complete_report_needs_one_call(self):
        result, calls = self.run_with([json.dumps(FULL)])
        self.assertEqual(result, FULL)
        self.assertEqual(calls, [('gemini-3.7-flash', False)])

    def test_exhausted_model_falls_back_in_order(self):
        quota = ProviderError('x', 'gemini_quota')
        busy = ProviderError('x', 'gemini_busy')
        result, calls = self.run_with([quota, busy, json.dumps(FULL)])
        self.assertEqual(result, FULL)
        order = ['gemini-3.7-flash'] + [m for m in GEMINI_MODELS if m != 'gemini-3.7-flash']
        self.assertEqual([m for m, _ in calls], order[:3])

    def test_all_models_exhausted_gives_a_clear_error(self):
        errors = [ProviderError('x', 'gemini_quota')] * len(GEMINI_MODELS)
        with self.assertRaisesRegex(ProviderError, '今日は使えません'):
            self.run_with(errors)

    def test_other_errors_are_not_retried_on_another_model(self):
        with self.assertRaisesRegex(ProviderError, 'boom'):
            self.run_with([ProviderError('boom', 'gemini')])

    def test_missing_item_is_repaired_once_by_the_same_model(self):
        partial = {k: v for k, v in FULL.items() if k != 'checks'}
        result, calls = self.run_with([json.dumps(partial), json.dumps(FULL)])
        self.assertEqual(result, FULL)
        self.assertEqual(calls, [('gemini-3.7-flash', False), ('gemini-3.7-flash', True)])

    def test_repair_failure_is_an_error(self):
        partial = {k: v for k, v in FULL.items() if k != 'checks'}
        with self.assertRaisesRegex(ProviderError, '項目がそろいません'):
            self.run_with([json.dumps(partial), json.dumps(partial)])

    def test_isolation_config_denies_everything_but_the_read_broker(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            (home / '.gemini' / 'policies').mkdir(parents=True)
            GeminiAdapter()._configure(home, {'command': 'python', 'args': ['t.py']})
            settings = json.loads((home / '.gemini' / 'settings.json').read_text(encoding='utf-8'))
            policy = (home / '.gemini' / 'policies' / 'agent-team.toml').read_text(encoding='utf-8')
            self.assertEqual(list(settings['mcpServers']), ['project_read'])
            self.assertIn('run_shell_command', settings['tools']['exclude'])
            self.assertIn('toolName = "*"\ndecision = "deny"', policy)
            self.assertIn('mcpName = "project_read"', policy)
            GeminiAdapter()._configure(home, None)
            self.assertNotIn('mcpServers', json.loads((home / '.gemini' / 'settings.json').read_text(encoding='utf-8')))
            self.assertNotIn('allow', (home / '.gemini' / 'policies' / 'agent-team.toml').read_text(encoding='utf-8'))

    def test_adapter_is_registered(self):
        self.assertIs(team_adapters.ADAPTERS['gemini'], GeminiAdapter)


if __name__ == '__main__':
    unittest.main()
