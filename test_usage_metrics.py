import tempfile
import unittest
from pathlib import Path

from team_usage_metrics import allowlisted_extra, code_version, normalize


class NormalizeTests(unittest.TestCase):
    def test_claude_total(self):
        row = normalize('claude', {'input_tokens': 5, 'cache_read_input_tokens': 100, 'cache_creation_input_tokens': 20,
                                   'output_tokens': 7}, 'provider_total', {})
        self.assertEqual((row['measurement'], row['input_total'], row['output']), ('final', 125, 7))

    def test_codex_running_total(self):
        row = normalize('codex', {'inputTokens': 120, 'cachedInputTokens': 100, 'outputTokens': 9}, 'provider_running_total', {})
        self.assertEqual((row['input_uncached'], row['cache_read'], row['cache_creation'], row['input_total']), (20, 100, None, 120))

    def test_unknown_is_not_zero(self):
        row = normalize('claude', None, None, {})
        self.assertEqual(row['measurement'], 'unknown')
        self.assertIsNone(row['output'])
        self.assertIsNone(row['input_total'])

    def test_partial_from_stream(self):
        partial = {'m1': {'input_tokens': 1, 'cache_read_input_tokens': 10, 'cache_creation_input_tokens': 2, 'output_tokens': 3},
                   'm2': {'input_tokens': 1, 'cache_read_input_tokens': 20, 'cache_creation_input_tokens': 0, 'output_tokens': 4}}
        row = normalize('claude', None, None, partial)
        self.assertEqual((row['measurement'], row['input_total'], row['output'], row['source']), ('partial', 34, 7, 'stream_partial'))

    def test_extra_allowlist(self):
        extra = allowlisted_extra({'num_turns': 3, 'duration_ms': 10, 'total_cost_usd': 0.5, 'result': 'secret text',
                                   'modelUsage': {'opus': {'inputTokens': 1, 'note': 'x'}}})
        self.assertEqual(extra, {'num_turns': 3, 'duration_ms': 10, 'total_cost_usd': 0.5, 'model_usage': {'opus': {'inputTokens': 1}}})
        self.assertEqual(len(code_version()), 12)


class ContextMeasurementTests(unittest.TestCase):
    def setUp(self):
        from team_config import default_config
        from team_engine import Engine
        from team_store import Store
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        config = default_config()
        config['approved_roots'] = [self.temp.name]
        self.engine = Engine(self.store, config, {'codex': ['fixture'], 'claude': ['fixture']}, 'http://127.0.0.1:1')

    def tearDown(self):
        self.engine.shutdown.set()
        self.engine.handoff.close()
        self.store.db.close()
        self.temp.cleanup()

    def test_failed_run_keeps_partial_usage_and_tool_calls(self):
        from team_common_agents import load_agent
        from team_engine import Context
        from team_optimization_metrics import report
        job = self.engine.create_job('sample', 'local goal', self.temp.name, True)
        task = self.engine.new_task(job, 'build', 'build', 'builder')
        ctx = Context(self.engine, task)
        ctx.prepare_agent(load_agent('builder', 'claude', 'common-implementer'))
        usage = {'input_tokens': 2, 'cache_read_input_tokens': 50, 'cache_creation_input_tokens': 5, 'output_tokens': 8}
        message = {'id': 'msg1', 'usage': usage, 'content': [{'type': 'tool_use', 'name': 'Bash'}]}
        ctx.observe_message(message)
        ctx.observe_message(dict(message, content=[{'type': 'tool_use', 'name': 'Read'}]))  # same call, streamed twice
        ctx.finish_agent('failed', 'execution')
        run = self.store.get(task['id'])['agent_run']
        self.assertEqual(run['usage_normalized']['measurement'], 'partial')
        self.assertEqual(run['usage_normalized']['input_total'], 57)
        self.assertEqual(run['tool_calls'], {'Bash': 1, 'Read': 1})
        self.assertEqual(run['optimization']['mode'], 'off')
        summary = report(Path(self.temp.name) / 'team.sqlite3')
        self.assertEqual(summary['overall']['partial_attempts'], 1)
        self.assertEqual(summary['per_job'][job['id']]['failed_or_cancelled']['output'], 8)


if __name__ == '__main__':
    unittest.main()
