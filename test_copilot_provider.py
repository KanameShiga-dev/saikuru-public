"""GitHub Copilot provider: settings validation, monthly credit view and fail-closed adapter scope."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import team_usage
from team_adapters import CopilotAdapter, ProviderError, copilot_answer
from team_config import default_config, validate_config
from team_usage import copilot_usage, record_copilot_usage, recovery_ready


def config_with(planner=None, builder=None, enabled=None, credits=None):
    config = default_config()
    config['provider_settings'] = {'enabled': enabled or ['codex', 'claude', 'copilot'],
                                   'copilot_monthly_credits': credits}
    if planner:
        config['profiles']['copilot-plan'] = planner
        config['roles']['planner'] = 'copilot-plan'
    if builder:
        config['profiles']['copilot-build'] = builder
        config['roles']['builder'] = 'copilot-build'
    return config


COPILOT = {'adapter': 'copilot', 'model': 'gpt-5-mini', 'effort': 'low'}


class SettingsTest(unittest.TestCase):
    def test_copilot_planner_is_allowed(self):
        validate_config(config_with(planner=copy.deepcopy(COPILOT), credits=1000))

    def test_copilot_builder_is_allowed_researcher_refused(self):
        # 2026-10-09: the builder works through 采来's checked tools (copilot_work_tools / document tools).
        validate_config(config_with(builder=copy.deepcopy(COPILOT)))
        config = config_with()
        config['profiles']['copilot-research'] = copy.deepcopy(COPILOT)
        config['roles']['researcher'] = 'copilot-research'
        with self.assertRaisesRegex(ValueError, '調査担当'):
            validate_config(config)

    def test_role_on_disabled_provider_is_refused(self):
        with self.assertRaisesRegex(ValueError, '使用しないプロバイダ'):
            validate_config(config_with(enabled=['codex']))  # builder default is Claude

    def test_empty_or_unknown_provider_list_is_refused(self):
        for enabled in ([], ['codex', 'gemini'], ['codex', 'codex']):
            config = config_with()
            config['provider_settings']['enabled'] = enabled
            with self.assertRaises(ValueError):
                validate_config(config)

    def test_credit_limit_must_be_positive(self):
        for credits in (0, -5, '100', True):
            with self.assertRaises(ValueError):
                validate_config(config_with(credits=credits))

    def test_astra_is_refused_for_copilot(self):
        with self.assertRaises(ValueError):
            validate_config(config_with(planner=dict(COPILOT, model='gpt-6-astra')))

    def test_old_config_without_provider_settings_still_validates(self):
        config = default_config()
        del config['provider_settings']
        validate_config(config)


class CreditTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(team_usage, 'COPILOT_LEDGER', Path(self.folder.name) / 'copilot-credits.json')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.folder.cleanup)

    def test_percent_from_monthly_limit_and_recorded_use(self):
        record_copilot_usage(150.0, 1)
        record_copilot_usage(100.0, 0)
        value = copilot_usage(1000)
        monthly = value['rows'][0]['monthly']
        self.assertEqual(value['status'], 'ok')
        self.assertEqual((monthly['used'], monthly['limit'], monthly['remaining_percent']), (250.0, 1000, 75.0))
        self.assertEqual(monthly['premium_requests'], 1)
        self.assertTrue(recovery_ready({'copilot': value}, COPILOT))

    def test_no_limit_means_unknown_not_full(self):
        record_copilot_usage(10.0, 0)
        value = copilot_usage(None)
        self.assertEqual(value['status'], 'unavailable')
        self.assertIsNone(value['rows'][0]['monthly']['remaining_percent'])
        self.assertFalse(recovery_ready({'copilot': value}, COPILOT))

    def test_overuse_is_zero_and_new_month_starts_empty(self):
        record_copilot_usage(1500.0, 0)
        self.assertEqual(copilot_usage(1000)['rows'][0]['monthly']['remaining_percent'], 0)
        next_month = datetime(2099, 1, 15, tzinfo=timezone.utc).timestamp()
        self.assertEqual(copilot_usage(1000, next_month)['rows'][0]['monthly']['used'], 0.0)

    def test_reset_is_first_of_next_month_utc(self):
        moment = datetime(2026, 12, 20, tzinfo=timezone.utc).timestamp()
        resets = copilot_usage(1000, moment)['rows'][0]['monthly']['resets_at']
        self.assertEqual(datetime.fromtimestamp(resets, timezone.utc), datetime(2027, 1, 1, tzinfo=timezone.utc))


class AdapterScopeTest(unittest.TestCase):
    def ctx(self, **values):
        definition = SimpleNamespace(sandbox='read-only', tools=('WebSearch', 'mcp__project_read__read_file'))
        base = dict(task={'profile': dict(COPILOT)}, agent_definition=definition, text_only=True,
                    attachment_ids=[], command=['copilot-not-started'])
        base.update(values)
        return SimpleNamespace(**base)

    def test_refuses_non_text_tasks_before_starting(self):
        with self.assertRaisesRegex(ProviderError, '切り替えてください'):
            CopilotAdapter().run(self.ctx(text_only=False), 'x', {})

    def test_refuses_write_tools_outside_a_document_builder(self):
        definition = SimpleNamespace(sandbox='read-only', tools=('mcp__project_read__write_document',))
        with self.assertRaisesRegex(ProviderError, '渡せません'):
            CopilotAdapter().run(self.ctx(agent_definition=definition), 'x', {})

    def test_document_builder_gets_the_document_tools(self):
        from team_engine import RESULT_SCHEMA
        definition = SimpleNamespace(sandbox='read-only', tools=('mcp__project_read__read_file', 'mcp__project_read__write_document',
                                                                  'mcp__project_read__generate_media'))
        ctx = self.ctx(agent_definition=definition, task={'role': 'builder', 'profile': dict(COPILOT)}, document_scope=True,
                       consultation_research=True, read_mcp={'command': 'python', 'args': ['document_tools.py', '--write']},
                       agent_system_instructions='', agent_started=lambda *a: None, event=lambda *a: None)
        seen = {}
        def fake(adapter, ctx, command, text, cwd, env, allowed, baseline=None):
            seen['allowed'] = allowed
            return json.dumps({'status': 'done', 'summary': 's', 'checks': ['c'], 'question': ''})
        with mock.patch.object(CopilotAdapter, '_call', fake):
            CopilotAdapter().run(ctx, 'x', RESULT_SCHEMA)
        self.assertEqual(seen['allowed'], ['project_read-read_file', 'project_read-write_document', 'project_read-generate_media'])

    def test_refuses_attachments(self):
        with self.assertRaisesRegex(ProviderError, '添付'):
            CopilotAdapter().run(self.ctx(attachment_ids=['a']), 'x', {})

    def test_answer_accepts_plain_or_fenced_json(self):
        self.assertEqual(copilot_answer('{"a": 1}'), {'a': 1})
        self.assertEqual(copilot_answer('```json\n{"a": 1}\n```'), {'a': 1})
        with self.assertRaises(ProviderError):
            copilot_answer('了解しました。')

    def test_answer_with_preface_or_closing_text(self):
        # 2026-10-09: Copilot has no output-schema option; a preface around the JSON must not fail the review.
        from team_engine import RESULT_SCHEMA
        result = {'status': 'done', 'summary': '指摘なし', 'checks': ['確認：{x} の扱い'], 'question': ''}
        body = json.dumps(result, ensure_ascii=False)
        for text in ('レビュー結果です。\n' + body, body + '\n以上です。', '結果：\n```json\n' + body + '\n```\n補足なし',
                     '例 {"note": 1} を参照。\n' + body):
            with self.subTest(text=text[:20]):
                self.assertEqual(copilot_answer(text, RESULT_SCHEMA), result)
        with self.assertRaises(ProviderError):
            copilot_answer('結果は {"other": 1} です。', RESULT_SCHEMA)


class ReportRepairTest(unittest.TestCase):
    """2026-10-09: the report must carry the same items whatever the model. A Copilot report with a missing or
    malformed item is re-output once by the same model in the same session (no tools); 采来 does not fill it in."""

    def setUp(self):
        from team_engine import RESULT_SCHEMA
        self.schema = RESULT_SCHEMA
        self.full = {'status': 'done', 'summary': '指摘なし', 'checks': ['確認：テスト'], 'question': '',
                     'questions': [], 'context_updates': []}
        self.events = []
        definition = SimpleNamespace(sandbox='read-only', tools=())
        self.ctx = SimpleNamespace(task={'profile': dict(COPILOT)}, agent_definition=definition, text_only=True,
                                   attachment_ids=[], command=['copilot'], agent_system_instructions='',
                                   agent_started=lambda *a: None, event=self.events.append)

    def run_with(self, answers):
        calls = []
        def fake(adapter, ctx, command, text, cwd, env, allowed, baseline=None):
            calls.append(command)
            return answers[len(calls) - 1]
        with mock.patch.object(CopilotAdapter, '_call', fake):
            result = CopilotAdapter().run(self.ctx, 'x', self.schema)
        return result, calls

    def test_complete_report_needs_one_call(self):
        result, calls = self.run_with([json.dumps(self.full)])
        self.assertEqual(result, self.full)
        self.assertEqual(len(calls), 1)

    def test_missing_item_is_reoutput_in_the_same_session(self):
        partial = {k: v for k, v in self.full.items() if k not in ('checks', 'question')}
        result, calls = self.run_with([json.dumps(partial), json.dumps(self.full)])
        self.assertEqual(result, self.full)
        session = calls[0][calls[0].index('--session-id') + 1]
        self.assertIn('--resume=' + session, calls[1])
        self.assertIn('--available-tools=__none__', calls[1])
        self.assertTrue(any('checks（欠けている）' in e for e in self.events))

    def test_still_missing_after_one_repair_stops_with_the_items(self):
        partial = {k: v for k, v in self.full.items() if k != 'checks'}
        with self.assertRaisesRegex(ProviderError, 'checks'):
            self.run_with([json.dumps(partial), '了解しました。'])

    def test_schema_errors(self):
        from team_adapters import schema_errors
        self.assertEqual(schema_errors(self.full, self.schema), [])
        wrong = dict(self.full, status='finished', checks='確認', question=['x'])
        errors = schema_errors(wrong, self.schema)
        self.assertTrue(any(e.startswith('status') for e in errors))
        self.assertTrue(any(e.startswith('checks') for e in errors))
        self.assertTrue(any(e.startswith('question') for e in errors))
        # "no question / no update" fields may be absent or null; information fields may not.
        lean = {k: v for k, v in self.full.items() if k not in ('question', 'questions', 'context_updates')}
        self.assertEqual(schema_errors(dict(lean, question=None), self.schema), [])
        self.assertEqual(schema_errors({k: v for k, v in lean.items() if k != 'summary'}, self.schema), ['summary（欠けている）'])


if __name__ == '__main__':
    unittest.main()
