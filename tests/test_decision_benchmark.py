import tempfile
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from decision_benchmark import ARMS, SEED, automatic_benchmark_approval, allowed_command, benchmark_context
from team_decisions import DecisionInput, DecisionRouter, route_agent


class Candidate:
    def is_available(self):
        return True
    def decide(self, request, timeout):
        return {'decision': 'common-harness-auditor'}


class BenchmarkBoundaryTests(unittest.TestCase):
    def test_shadow_and_live_have_distinct_effective_values(self):
        request = DecisionInput('agent_route', ('common-explorer','common-harness-auditor'),
            'common-explorer', {'role':'researcher'}, 'low')
        for shadow, expected in ((True,'common-explorer'), (False,'common-harness-auditor')):
            result = DecisionRouter({'ollama':Candidate()}, {'provider':'ollama','shadow':shadow}).route(request)
            self.assertEqual(result['effective'], expected)
    def test_live_failure_stops(self):
        result = DecisionRouter({}, {'provider':'ollama','shadow':False}).route(
            DecisionInput('agent_route', ('common-explorer',), 'common-explorer', {}, 'low'))
        self.assertEqual(result['effective'], 'HUMAN')
    def test_production_default_context_is_unchanged(self):
        self.assertEqual(route_agent('builder','common-implementer',{'provider':'disabled','shadow':True})['effective'], 'common-implementer')
    def test_context_rejects_extra_fields(self):
        with self.assertRaises(ValueError):
            route_agent('researcher','common-explorer',context={'permissions':'all'})
    def test_initial_seeds_are_shared(self):
        self.assertGreaterEqual(len(SEED), 8)
        self.assertEqual(set(ARMS), {'A-disabled','B-shadow','C-adopted'})
    def test_questions_are_declined_without_wait(self):
        events=[]
        ctx=SimpleNamespace(task={'id':'t','job_id':'j'},engine=SimpleNamespace(
            store=SimpleNamespace(event=lambda *args: events.append(args))))
        self.assertFalse(automatic_benchmark_approval(ctx,{'operation':'question'})['allow'])
        self.assertEqual(len(events),2)
    def test_provider_allow_cannot_expand_fixed_boundary(self):
        ctx=SimpleNamespace(task={'id':'t','job_id':'j'},engine=SimpleNamespace(
            config={'decision':{'provider':'disabled'}},store=SimpleNamespace(event=lambda *args:None)))
        with patch('decision_benchmark.DecisionRouter') as router:
            router.return_value.route.return_value={'effective':'ALLOW'}
            self.assertFalse(automatic_benchmark_approval(ctx,{'operation':'question'})['allow'])
    def test_provider_deny_is_advice_for_preapproved_command(self):
        ctx=SimpleNamespace(project='.',task={'id':'t','job_id':'j'},engine=SimpleNamespace(
            config={'decision':{'provider':'disabled'}},store=SimpleNamespace(event=lambda *args:None)))
        with patch('decision_benchmark.DecisionRouter') as router, patch('decision_benchmark.auto_read_reason',return_value='safe'):
            router.return_value.route.return_value={'effective':'DENY'}
            result=automatic_benchmark_approval(ctx,{'operation':'item/commandExecution/requestApproval'})
            self.assertTrue(result['allow'])
            import json
            self.assertEqual(json.loads(result['note'])['recommendation'],'DENY')
    def test_invalid_task_hint_rejected(self):
        with self.assertRaises(Exception):
            DecisionInput('benchmark_permission',('ALLOW','DENY'),'DENY',{'task_hints':['grant_admin']},'low').validate()
    def test_unrecognized_commands_are_denied(self):
        self.assertFalse(allowed_command(SimpleNamespace(project='.'), {'operation':'shell','details':{}}))
    def test_command_approval_does_not_swap_context_and_payload(self):
        events=[]
        ctx=SimpleNamespace(project='.',task={'id':'t','job_id':'j'},engine=SimpleNamespace(
            store=SimpleNamespace(event=lambda *args: events.append(args))))
        self.assertFalse(automatic_benchmark_approval(ctx, {'source':'codex',
            'operation':'item/commandExecution/requestApproval','details':{}})['allow'])
    def test_fixed_test_command_checks_shell_cwd_and_escalation(self):
        root=Path('.').resolve()
        shell=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
        script='python -m unittest discover -s tests -v'
        details={'cwd':str(root),'command':f'"{shell}" -Command "{script}"',
                 'commandActions':[{'command':script}]}
        ctx=SimpleNamespace(project=str(root))
        payload={'operation':'item/commandExecution/requestApproval','details':details}
        self.assertTrue(allowed_command(ctx,payload))
        details['additionalPermissions']={'network':True}
        self.assertFalse(allowed_command(ctx,payload))
        details.pop('additionalPermissions')
        details['command']=f'"{root / "untrusted.exe"}" -Command "{script}"'
        self.assertFalse(allowed_command(ctx,payload))
    def test_benchmark_context_keeps_facts_and_evidence_without_duplicate_source(self):
        import json
        task={'id':'t','job_id':'j','role':'builder','status':'succeeded',
              'instruction':'SOURCE '*10000,'summary':'saved','result':{'checks':['unit passed']}}
        facts=[{'key':'requirement','value':'keep','evidence':'SPEC'}]
        app=SimpleNamespace(store=SimpleNamespace(all=lambda kind:[task]),
            engine=SimpleNamespace(handoff=SimpleNamespace(current_facts=lambda job:facts)))
        data=json.loads(benchmark_context(app,{'id':'j'}))
        self.assertEqual(data['current_facts'],facts)
        self.assertEqual(data['task_reports'][0]['checks'],['unit passed'])
        self.assertEqual(len(data['task_reports'][0]['instruction_sha256']),64)
        self.assertNotIn('SOURCE',json.dumps(data))


if __name__ == '__main__':
    unittest.main()
