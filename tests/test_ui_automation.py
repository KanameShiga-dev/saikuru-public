import json
import tempfile
import unittest
from pathlib import Path
from team_ui_automation import UiAutomation, validate
from team_ui_actions import Context, decide

def visible():
    return {'query':'','status':'','page':1,'message':'','nextEnabled':True,
            'rows':[{'id':1,'title':'task1','status':'todo'}]}

class OperationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.calls=[]
        def choose(root,provider,state):
            self.calls.append((provider,state))
            return {'action':'SEARCH','arm':provider,'usage':{'input_tokens':10,'output_tokens':1}}
        self.service=UiAutomation(self.temp.name,choose)
        self.s=self.service.start({'goal':'search','mode':'hybrid','consent':True})
    def step(self,revision,state=None):
        return self.service.next({'id':self.s['id'],'revision':revision,'visible':state or visible()})
    def test_two_stalls_transfer(self):
        self.step(0);self.step(1);r=self.step(2)
        self.assertEqual(r['provider'],'model');self.assertEqual(r['switches'],1)
        self.assertEqual(self.calls[-1][1]['handoff']['previous_actions'][0]['action'],'SEARCH')
    def test_replay_rejected(self):
        self.step(0)
        with self.assertRaises(ValueError): self.step(0)
    def test_invalid_choice_never_returned(self):
        self.service.selector=lambda *_:{'action':'DELETE_ALL','arm':'ollama'}
        self.assertEqual(self.step(0)['status'],'invalid_action')
        self.assertEqual(len(self.service.sessions[self.s['id']]['steps']),0)
    def test_exception_transfers(self):
        def fail(*_):raise TimeoutError()
        self.service.selector=fail
        self.assertEqual(self.step(0)['status'],'provider_error')
        self.assertEqual(self.step(0)['status'],'provider_error')
    def test_cancel_avoids_calls(self):
        self.service.cancel(self.s)
        self.assertEqual(self.step(0)['status'],'cancelled');self.assertFalse(self.calls)
    def test_consent_and_concurrency(self):
        with self.assertRaises(ValueError):self.service.start({'goal':'search','mode':'hybrid'})
        with self.assertRaises(ValueError):self.service.start({'goal':'search','mode':'hybrid','consent':True})
    def test_limit(self):
        for i in range(8):self.step(i)
        self.assertEqual(self.step(8)['status'],'action_limit');self.assertEqual(len(self.calls),8)
    def test_progress_resets_counter(self):
        self.step(0);self.step(1);s=visible();s['query']='alpha';self.step(2,s)
        self.assertEqual(self.calls[-1][0],'model')
    def test_private_data_rejected(self):
        s=visible();s['query']='private'
        with self.assertRaises(ValueError):validate(s)
        s=visible();s['rows'][0]['title']='private'
        with self.assertRaises(ValueError):validate(s)
    def test_completion_without_call(self):
        s=visible();s.update(query='alpha',status='todo',rows=[{'id':i,'title':f'alpha{i}','status':'todo'} for i in (21,22,31)])
        self.assertEqual(self.step(0,s)['status'],'passed');self.assertFalse(self.calls)
    def test_durable_history_and_restart(self):
        self.step(0)
        svc=UiAutomation(self.temp.name)
        self.assertEqual(svc.history()['records'][0]['status'],'interrupted')
        self.assertEqual(len(svc.history()['records'][0]['steps']),1)
    def test_cancel_while_provider_busy(self):
        def choose(*_):
            self.service.cancel(self.s)
            return {'action':'SEARCH','arm':'ollama'}
        self.service.selector=choose
        self.assertEqual(self.step(0)['status'],'cancelled')
        self.assertFalse(self.service.sessions[self.s['id']]['steps'])
    def test_context_absolute_and_tools_denied(self):
        ctx=Context(Path('.'))
        self.assertTrue(Path(ctx.project).is_absolute())
        self.assertFalse(ctx.approve()['allow'])
    def test_adapter_bounds(self):
        for state in ({'choices':{}},{'choices':{'STOP':'stop'},'visible':'x'*13000}):
            with self.assertRaises(ValueError):decide(Path('.'),'ollama',state)
    def test_mobile_paths_registered(self):
        from mobile_gateway import VALID_PATHS
        self.assertTrue({'/operation-tests','/operation-tests.js','/api/ui-automation/start',
                         '/api/ui-automation/next','/api/ui-automation/history','/api/ui-automation/cancel'} <= VALID_PATHS)


if __name__=='__main__':unittest.main()
