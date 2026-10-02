"""Offline contract/gate evaluation. These cases do not measure semantic accuracy."""
import json
import os
import unittest
from unittest.mock import patch
from team_decisions import DecisionInput, DecisionRouter, DecisionError, LunaProvider, DecisionsPreviewProvider


class Fake:
    def __init__(self, available=True, decision='B', error=None):
        self.available, self.decision, self.error = available, decision, error
        self.calls = 0

    def is_available(self):
        return self.available

    def decide(self, request, timeout):
        self.calls += 1
        if self.error:
            raise DecisionError(self.error)
        return {'decision': self.decision}


class DecisionTests(unittest.TestCase):
    def request(self, kind='agent_route', risk='low'):
        return DecisionInput(kind, ('A', 'B'), 'A', {}, risk)

    def test_120_contract_cases(self):
        for kind in ('task_route', 'agent_route', 'skill_route', 'model_route', 'loop'):
            for mode in ('auto', 'decisions', 'luna', 'disabled'):
                for shadow in (True, False):
                    for risk in ('low', 'normal', 'high'):
                        with self.subTest(kind=kind, mode=mode, shadow=shadow, risk=risk):
                            providers = {'decisions': Fake(), 'luna': Fake()}
                            with patch.dict(os.environ, {'DECISION_PROVIDER': mode,
                                  'DECISIONS_API_ENABLED': 'true',
                                  'DECISION_SHADOW_MODE': str(shadow).lower()}, clear=True):
                                result = DecisionRouter(providers).route(self.request(kind, risk))
                            expected = 'HUMAN' if risk == 'high' else 'A' if mode == 'disabled' else 'B'
                            self.assertEqual(result['decision'], expected)
                            self.assertEqual(result['effective'], 'A' if shadow else expected)
                            if risk == 'high' or mode == 'disabled':
                                self.assertEqual(sum(p.calls for p in providers.values()), 0)

    def test_unavailable_strict_does_not_switch(self):
        for mode in ('decisions', 'luna'):
            with self.subTest(mode=mode), patch.dict(os.environ, {
                    'DECISION_PROVIDER': mode, 'DECISIONS_API_ENABLED': 'true',
                    'DECISION_SHADOW_MODE': 'false'}, clear=True):
                providers = {'decisions': Fake(False), 'luna': Fake(False)}
                result = DecisionRouter(providers).route(self.request())
                self.assertEqual(result['status'], 'error')
                self.assertEqual(result['effective'], 'HUMAN')

    def test_invalid_choice_retry_bounded_and_safe(self):
        with patch.dict(os.environ, {'DECISION_PROVIDER': 'auto', 'DECISION_MAX_RETRIES': '1'}, clear=True):
            luna = Fake(decision='DELETE_ALL')
            result = DecisionRouter({'decisions': Fake(False), 'luna': luna}).route(self.request())
            self.assertEqual(luna.calls, 2)
            self.assertEqual(result['status'], 'fallback')
            self.assertEqual(result['effective'], 'A')

    def test_metadata_rejects_raw_prompt(self):
        with self.assertRaises(DecisionError):
            DecisionInput('loop', ('A',), 'A', {'prompt': 'private'}).validate()

    def test_preview_flag_does_not_enable_stub(self):
        with patch.dict(os.environ, {'DECISIONS_API_ENABLED': 'true'}):
            self.assertFalse(DecisionsPreviewProvider().is_available())

    def test_api_disabled_without_credentials(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(LunaProvider().is_available())

    def test_responses_adapter_mock(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            'status': 'completed', 'output': [{'type': 'message', 'content': [
                {'type': 'output_text', 'text': '{"decision":"B"}'}]}],
            'usage': {'input_tokens': 10, 'output_tokens': 2, 'total_tokens': 12}}).encode()
        opener = unittest.mock.MagicMock()
        opener.open.return_value = response
        with patch.dict(os.environ, {'DECISION_API_CALLS_APPROVED': 'true',
                                    'OPENAI_API_KEY': 'synthetic-fixture'}, clear=True), \
             patch('team_decisions.urllib.request.build_opener', return_value=opener):
            result = LunaProvider().decide(self.request(), 1.2)
        self.assertEqual(result['decision'], 'B')
        payload = json.loads(opener.open.call_args.args[0].data)
        self.assertFalse(payload['store'])
        self.assertEqual(payload['model'], 'gpt-6-luna')
        self.assertNotIn('tools', payload)

    def test_provider_exception_safe(self):
        provider = Fake()
        provider.decide = unittest.mock.Mock(side_effect=RuntimeError('synthetic provider failure'))
        with patch.dict(os.environ, {'DECISION_PROVIDER': 'auto'}, clear=True):
            result = DecisionRouter({'decisions': Fake(False), 'luna': provider}).route(self.request())
        self.assertEqual(result['effective'], 'A')

    def test_late_response_not_accepted(self):
        provider = Fake()
        with patch.dict(os.environ, {'DECISION_PROVIDER': 'luna', 'DECISION_TIMEOUT_MS': '100',
                                    'DECISION_SHADOW_MODE': 'false'}, clear=True), \
             patch('team_decisions.time.monotonic', side_effect=[0, 0, .2, .2, .2]):
            result = DecisionRouter({'decisions': Fake(False), 'luna': provider}).route(self.request())
        self.assertEqual(result['status'], 'error')


if __name__ == '__main__':
    unittest.main()
