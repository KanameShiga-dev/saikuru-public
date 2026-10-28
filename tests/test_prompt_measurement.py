"""Attempt-fenced numeric callbacks; no native Context initialization or Provider."""
import json
from types import SimpleNamespace
import unittest

from tests import test_attempt_fencing as fixtures
from team_store import StaleAttempt
from team_usage_metrics import observe_prompt


class PromptMeasurementTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.AttemptFencingTests(); self.addCleanup(self.f.doCleanups); self.f.setUp()
        self.ctx=self.f.context(self.f.reserve())

    def test_numeric_metadata_has_stage_and_no_prompt_text(self):
        secret='fictional-private-prompt'
        self.ctx.record_prompt_measurement(secret,stage='engine_ready')
        self.ctx.record_prompt_measurement(secret+' fictional-format',stage='adapter_payload')
        record=self.f.store.get(self.f.task['id'])['agent_run']
        self.assertNotIn(secret,json.dumps(record)); self.assertEqual(len(record['prompt_measurements']),2)
        self.assertEqual(record['prompt_measurements'][-1]['characters'],len(secret+' fictional-format'))
        self.assertFalse(record['prompt_measurements'][-1]['model_invocation_proven'])

    def test_observer_keeps_system_and_format_repair_preparations_separate(self):
        observe_prompt(self.ctx,'Fictional first',includes_system=True)
        observe_prompt(self.ctx,'Fictional repair',includes_system=True,kind='format_repair')
        rows=self.f.store.get(self.f.task['id'])['agent_run']['prompt_measurements']
        self.assertEqual([r['kind'] for r in rows],['initial','format_repair'])
        self.assertTrue(all(r['includes_system'] for r in rows))

    def test_old_attempt_cannot_record_prompt_or_usage(self):
        self.f.next_attempt(); before=self.f.store.get(self.f.task['id'])
        with self.assertRaises(StaleAttempt): observe_prompt(self.ctx,'Fictional stale')
        with self.assertRaises(StaleAttempt): self.ctx.record_usage({'inputTokens':100})
        self.assertEqual(self.f.store.get(self.f.task['id']),before)

    def test_usage_total_replaces_previous_and_retains_codex_cache_writes(self):
        self.ctx.record_usage({'inputTokens':10,'cachedInputTokens':2,'cacheWriteInputTokens':0,'outputTokens':1})
        self.ctx.record_usage({'inputTokens':20,'cachedInputTokens':4,'cacheWriteInputTokens':3,'outputTokens':2,'untrusted_text':'fictional-private'})
        record=self.f.store.get(self.f.task['id'])['agent_run']
        self.assertEqual(record['usage']['inputTokens'],20); self.assertEqual(record['usage']['cacheWriteInputTokens'],3)
        self.assertEqual(record['usage_normalized']['cache_creation'],3)
        self.assertEqual(record['usage_normalized']['input_total'],20)
        self.assertNotIn('fictional-private',json.dumps(record))

    def test_boolean_counts_and_invalid_stages_are_not_recorded(self):
        self.ctx.record_usage({'inputTokens':True,'outputTokens':-1})
        self.assertEqual(self.ctx.agent_run['usage'],{})
        self.ctx.record_prompt_measurement('Fictional',stage='fictional-unknown')
        self.assertNotIn('prompt_measurements',self.ctx.agent_run)

    def test_preparation_history_is_bounded_and_truncation_is_persisted(self):
        for _ in range(65): observe_prompt(self.ctx,'Fictional')
        record=self.f.store.get(self.f.task['id'])['agent_run']
        self.assertEqual(len(record['prompt_measurements']),64)
        self.assertTrue(record['prompt_measurements_truncated'])

    def test_old_adapter_fixture_without_callback_still_means_unknown(self):
        observe_prompt(SimpleNamespace(),'Fictional metadata-only fixture')


if __name__=='__main__': unittest.main()
