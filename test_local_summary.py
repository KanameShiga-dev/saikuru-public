"""Isolated compression/coverage checks; fake local transport, no cloud calls."""
import unittest
from unittest.mock import patch
from team_local_summary import analyze, review_plan, deduplicate, invariant_errors, settings, _CACHE


OK = {'equivalent': True, 'missing': [], 'added': [], 'changed': [], 'uncertain': []}


class LocalSummaryTests(unittest.TestCase):
    def setUp(self):
        _CACHE.clear()

    def test_adjacent_duplicates_only(self):
        self.assertEqual(deduplicate('説明です。\n\n説明です。\n\n次の話。'), '説明です。\n\n次の話。')
        for text in ('開発:\n確認。\n\n資料:\n確認。', '```\na\n\na\n```', '|値|\n|0|\n|0|', '- A\n\n- A'):
            self.assertEqual(deduplicate(text), text)

    def test_fixed_checks_reject_dropped_constraints_and_numbers(self):
        self.assertIn('critical_line_missing', invariant_errors('公開は禁止。\n説明を整理します。', '説明を整理。'))
        self.assertIn('identifier_or_number_changed', invariant_errors('期限は15日。\n長い説明です。', '期限は16日。'))

    def test_short_summary_never_calls_model(self):
        original = '請求書を作る。公開は禁止。'
        sent, meta = analyze(original, transport=lambda *a: self.fail('unexpected call'))
        self.assertEqual(sent, original)
        self.assertEqual(meta['reason'], 'short_input')

    def test_checked_summary_remains_shadow(self):
        source = '公開は禁止。\n' + '背景の説明です。' * 90
        candidate = '公開は禁止。\n背景を説明。'
        answers = iter([({'summary': candidate}, {'prompt_eval_count': 20, 'eval_count': 10}),
                        (OK, {'prompt_eval_count': 30, 'eval_count': 8})])
        sent, meta = analyze(source, {'local_summary': {'min_chars': 500}}, transport=lambda *a: next(answers))
        self.assertEqual(sent, source)
        self.assertEqual(meta['semantic_check'], 'passed')
        self.assertEqual(meta['local_input_tokens'], 50)
        self.assertFalse(meta['applied'])

    def test_missing_or_uncertain_rejected(self):
        for field in ('missing', 'added', 'changed', 'uncertain'):
            answer = dict(OK, **{field: ['synthetic finding']})
            meta = review_plan('PDFを作成。公開は禁止。', 'PDFを作成。',
                               transport=lambda *a: (answer, {}))
            self.assertEqual(meta['semantic_check'], 'rejected')
            self.assertNotIn('synthetic finding', str(meta))
            _CACHE.clear()

    def test_number_guard_even_if_model_claims_pass(self):
        meta = review_plan('15分の動画。', '10分の動画。', transport=lambda *a: (OK, {}))
        self.assertEqual(meta['semantic_check'], 'rejected')

    def test_timeout_falls_back_with_unknown_usage(self):
        def fail(*a):
            raise TimeoutError()
        meta = review_plan('PDFを作成。', 'PDFを作成。', transport=fail)
        self.assertEqual(meta['reason'], 'local_unavailable_or_timeout')
        self.assertIsNone(meta['local_input_tokens'])
        self.assertFalse(meta['applied'])

    def test_cache_avoids_repeat_call(self):
        first = review_plan('PDFを作成。', 'PDFを作成。', transport=lambda *a: (OK, {'prompt_eval_count': 9, 'eval_count': 3}))
        second = review_plan('PDFを作成。', 'PDFを作成。', transport=lambda *a: self.fail('cache miss'))
        self.assertEqual(first['local_calls'], 1)
        self.assertEqual(second['local_calls'], 0)
        self.assertTrue(second['cache_hit'])

    def test_malformed_judge_rejected(self):
        meta = review_plan('PDFを作成。', 'PDFを作成。', transport=lambda *a: ({'equivalent': True}, {}))
        self.assertEqual(meta['reason'], 'invalid_review')

    def test_no_active_or_remote_model_setting(self):
        self.assertEqual(settings({})['model'], 'gemma4:latest')
        for value in ({'mode': 'on'}, {'model': 'remote-model'}, {'timeout_seconds': 100}):
            with self.assertRaises(ValueError):
                settings({'local_summary': value})

    def test_context_limit_before_http(self):
        from team_local_summary import _chat
        with patch('urllib.request.build_opener', side_effect=AssertionError('unexpected network')):
            with self.assertRaisesRegex(ValueError, 'local_context_limit'):
                _chat('qwen2.5:7b', '点検する', {'original': '自然な日本語の長文。' * 1000, 'plan': '短い案。'}, 1)


if __name__ == '__main__':
    unittest.main()
