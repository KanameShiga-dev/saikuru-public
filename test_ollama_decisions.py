import os
import unittest
from unittest.mock import patch
from team_decisions import DecisionError, DecisionInput, DecisionRouter
from team_ollama_decisions import OllamaSystemOneProvider


class OllamaTests(unittest.TestCase):
    def request(self):
        return DecisionInput('agent_route', ('A', 'B'), 'A', {'role':'researcher'}, 'low')

    def test_batch_and_telemetry(self):
        data = {'answers': {str(i): {'choice':'B','confidence':.8,
                'probabilities':{'A':.1,'B':.8,'HUMAN':.1}} for i in range(2)},
                'usage':{'input_tokens':10,'output_tokens':2}}
        provider = OllamaSystemOneProvider()
        with patch.object(provider, '_post', return_value=data) as post:
            results = provider.decide_batch([self.request(),self.request()],1)
        self.assertEqual(len(results),2)
        self.assertEqual(results[0]['confidence'],.8)
        self.assertNotIn('usage', results[1])
        self.assertEqual(len(post.call_args.args[0]['questions']),2)

    def test_high_risk_never_sent(self):
        provider = OllamaSystemOneProvider()
        request = DecisionInput('loop', ('A','B'),'A',{},'high')
        with patch.object(provider, '_post') as post, self.assertRaises(DecisionError):
            provider.decide(request,1)
        post.assert_not_called()

    def test_nonlocal_rejected_before_network(self):
        with patch.dict(os.environ, {'OLLAMA_BASE_URL':'http://example.com:11434'}), \
             self.assertRaises(DecisionError):
            OllamaSystemOneProvider().decide(self.request(),1)

    def test_unknown_choice_rejected(self):
        with patch.object(OllamaSystemOneProvider,'_post',return_value={'answers':{'0':{'choice':'DELETE'}}}), \
             self.assertRaises(DecisionError):
            OllamaSystemOneProvider().decide(self.request(),1)

    def test_auto_local_first(self):
        local=OllamaSystemOneProvider()
        with patch.dict(os.environ, {'DECISION_PROVIDER':'auto'},clear=True), \
             patch.object(local,'decide',return_value={'decision':'B','confidence':.8}), \
             patch.object(local,'is_available',return_value=True):
            result=DecisionRouter({'ollama':local}).route(self.request())
        self.assertEqual(result['provider'],'ollama')
        self.assertEqual(result['effective'],'A')

    def test_stopped_local_keeps_shadow_baseline(self):
        with patch.dict(os.environ, {'DECISION_PROVIDER':'ollama'},clear=True), \
             patch.object(OllamaSystemOneProvider,'_post',side_effect=DecisionError('ollama_request_failed')):
            result=DecisionRouter().route(self.request())
        self.assertEqual(result['status'],'error')
        self.assertEqual(result['effective'],'A')


if __name__=='__main__':
    unittest.main()
