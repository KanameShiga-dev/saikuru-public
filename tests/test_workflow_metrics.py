"""Invented measurement metadata only; no prices, Provider, App or operational DB."""
import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from team_usage_metrics import normalize, weighted, workflow_measurements, copilot_normalized


class WorkflowMetricsTests(unittest.TestCase):
    def setUp(self):
        self.job={'id':'fictional-job','status':'running'}
        self.run={'id':'fictional-run','provider':'claude','model':'fictional-model','effort':'medium',
            'attempt':1,'attempt_id':'fictional-attempt','session_id':'fictional-native-session','status':'succeeded',
            'started_at':10,'finished_at':20,'usage':{'input_tokens':100,'cache_read_input_tokens':20,
                'cache_creation_input_tokens':5,'output_tokens':10},'usage_source':'provider_total',
            'usage_raw':{'total_cost_usd':0.1},'measurement_ids':{'checkpoint_id':'fictional-checkpoint'},
            'prompt_measurements':[{'stage':'engine_ready','characters':50,'kind':'initial'},
                {'stage':'adapter_payload','characters':80,'includes_system':False,'kind':'initial'}],
            'inherited_context':{'characters':30}}
        self.task={'id':'fictional-task','job_id':self.job['id'],'agent_run':self.run,'attempt':1}

    def report(self,**kwargs):
        return workflow_measurements([self.job],[self.task],**kwargs)

    def test_links_identifiers_but_hashes_native_session(self):
        projection=self.report(); row=projection['rows'][0]
        self.assertEqual(row['run_id'],self.run['id']); self.assertEqual(row['attempt_id'],self.run['attempt_id'])
        self.assertEqual(len(row['session_fingerprint']),64)
        self.assertEqual(projection['by_task']['groups'][self.task['id']]['tokens']['input_total']['total'],125)
        self.assertEqual(projection['by_session']['groups'][row['session_fingerprint']]['runs'],1)
        self.assertNotIn(self.run['session_id'],json.dumps(row))
        self.assertEqual(row['initial_payload_characters'],80); self.assertEqual(row['inherited_context_characters'],30)
        self.run['prompt_measurements_truncated']=True
        self.assertTrue(self.report()['rows'][0]['prepared_payloads_lower_bound'])

    def test_duplicate_snapshots_use_latest_total_once(self):
        old=copy.deepcopy(self.run); old['usage']['input_tokens']=20
        self.task['agent_run_history']=[old,old]
        report=self.report(); self.assertEqual(len(report['rows']),1); self.assertEqual(report['deduplicated_snapshots'],2)
        self.assertEqual(report['jobs'][self.job['id']]['tokens']['input_total']['total'],125)

    def test_conflicting_run_owner_is_unknown_not_double_billed(self):
        other=dict(self.task,id='fictional-other-task')
        report=workflow_measurements([self.job],[self.task,other])
        self.assertEqual(report['conflicting_run_ids'],1)
        self.assertIsNone(report['jobs'][self.job['id']]['tokens']['input_total']['total'])
        self.assertIsNone(report['rows'][0]['cost_usd_reported'])
        self.run['measurement_ids']['job_id']='fictional-foreign-job'
        self.assertIsNone(self.report()['rows'][0]['input_total'])

    def test_missing_run_identity_is_not_collapsed_into_one_known_run(self):
        self.run.pop('id'); self.task['agent_run_history']=[copy.deepcopy(self.run)]
        report=self.report(); self.assertEqual(len(report['rows']),2)
        self.assertTrue(all(r['measurement']=='unknown' for r in report['rows']))

    def test_partial_measurement_is_a_labelled_lower_bound(self):
        self.run.pop('usage'); self.run['usage_normalized']={'measurement':'partial','input_total':10,'output':1}
        report=self.report()['jobs'][self.job['id']]['tokens']['input_total']
        self.assertIsNone(report['total']); self.assertEqual(report['known_sum'],10); self.assertTrue(report['lower_bound'])

    def test_missing_usage_and_price_do_not_become_zero(self):
        self.task['agent_run_history']=None
        self.run.pop('usage'); self.run.pop('usage_raw')
        row=self.report()['rows'][0]
        self.assertIsNone(row['input_total']); self.assertIsNone(row['cost_usd_reported'])
        self.assertIn('price_unknown',row['unknown_reasons'])

    def test_explicit_provider_zero_remains_known_zero(self):
        self.run['usage']={k:0 for k in self.run['usage']}; self.run['usage_raw']['total_cost_usd']=0
        report=self.report()['jobs'][self.job['id']]
        self.assertEqual(report['tokens']['input_total']['total'],0); self.assertEqual(report['cost_usd']['total'],0)

    def test_codex_writes_are_retained_without_guessing_overlap(self):
        row=normalize('codex',{'inputTokens':120,'cachedInputTokens':20,'cacheWriteInputTokens':9,'outputTokens':10},'reported',{})
        self.assertEqual(row['input_total'],120); self.assertEqual(row['cache_creation'],9)
        self.assertIsNone(row['input_uncached']); self.assertIsNone(weighted(row))

    def test_codex_missing_write_is_unknown_and_zero_is_distinct(self):
        raw={'inputTokens':120,'cachedInputTokens':20,'outputTokens':10}
        missing=normalize('codex',raw,'reported',{}); zero=normalize('codex',dict(raw,cacheWriteInputTokens=0),'reported',{})
        self.assertIsNone(missing['cache_creation']); self.assertIsNone(weighted(missing))
        self.assertEqual(zero['cache_creation'],0); self.assertIsInstance(weighted(zero),int)

    def test_invalid_token_types_do_not_count_as_numbers(self):
        row=normalize('claude',{'input_tokens':True,'cache_read_input_tokens':-1,'cache_creation_input_tokens':0,'output_tokens':2.5},'reported',{})
        self.assertIsNone(row['input_total']); self.assertIsNone(weighted(row))
        self.assertEqual(copilot_normalized({'inputTokens':True})['measurement'],'unknown')

    def test_missing_per_call_component_does_not_silently_add_zero(self):
        row=normalize('claude',None,None,{'a':{'input_tokens':10},'b':{'output_tokens':2}})
        self.assertIsNone(row['input_uncached']); self.assertIsNone(row['output']); self.assertIsNone(row['input_total'])

    def test_nonfinite_cost_is_unknown(self):
        for value in (float('nan'),float('inf'),True,-1):
            self.run['usage_raw']['total_cost_usd']=value
            self.assertIsNone(self.report()['rows'][0]['cost_usd_reported'])

    def test_overlapping_human_waits_are_unioned(self):
        approvals=[{'job_id':self.job['id'],'intended_role':'end_user','status':'approved','created_at':a,'decided_at':b}
                   for a,b in ((10,20),(15,30))]
        wait=self.report(approvals=approvals)['jobs'][self.job['id']]['human_decision_wait']
        self.assertEqual(wait['seconds'],20); self.assertIsNone(wait['actual_human_work_seconds'])

    def test_pending_and_missing_decision_time_are_distinct(self):
        approval={'job_id':self.job['id'],'intended_role':'end_user','status':'pending','created_at':10}
        wait=self.report(approvals=[approval],at=30)['jobs'][self.job['id']]['human_decision_wait']
        self.assertIsNone(wait['seconds']); self.assertEqual(wait['observed_seconds'],20); self.assertEqual(wait['pending_intervals'],1)
        approval['status']='approved'
        wait=self.report(approvals=[approval],at=30)['jobs'][self.job['id']]['human_decision_wait']
        self.assertIsNone(wait['observed_seconds']); self.assertEqual(wait['unknown_intervals'],1)

    def test_run_wall_time_removes_overlap_but_does_not_claim_compute_time(self):
        run=dict(self.run,id='fictional-run-2',started_at=15,finished_at=25,attempt=2,attempt_id='fictional-attempt-2')
        self.task['agent_run_history']=[self.run]; self.task['agent_run']=run
        row=self.report()['jobs'][self.job['id']]
        self.assertEqual(row['run_wall_seconds_union']['seconds'],15); self.assertEqual(row['retries_known'],1)
        self.assertIsNone(row['actual_human_work_seconds'])

    def test_missing_and_backwards_timestamps_stay_unknown(self):
        self.run['finished_at']=9
        self.assertIsNone(self.report()['jobs'][self.job['id']]['run_wall_seconds_union']['seconds'])

    def test_truncated_history_invalidates_complete_totals(self):
        self.task['attempt']=3
        self.assertTrue(self.report()['jobs'][self.job['id']]['history_incomplete'])
        self.assertIsNone(self.report()['jobs'][self.job['id']]['tokens']['input_total']['total'])
        self.task['attempt']=1
        self.task['agent_run_history_truncated']=True
        summary=self.report()['jobs'][self.job['id']]
        self.assertTrue(summary['history_truncated']); self.assertIsNone(summary['tokens']['input_total']['total'])
        self.assertTrue(summary['cost_usd']['lower_bound'])

    def test_foreign_checkpoint_is_not_a_verified_relation(self):
        row=self.report(checkpoints=[{'id':'fictional-checkpoint','job_id':'fictional-other-job'}])['rows'][0]
        self.assertFalse(row['checkpoint_relation_verified']); self.assertIn('checkpoint_relation_unverified',row['unknown_reasons'])

    def test_text_and_unknown_raw_fields_are_not_exported(self):
        self.task['instruction']='fictional-private-note'; self.run['raw_prompt']='fictional-private-note'
        self.run['usage_raw']['raw_output']='fictional-private-note'
        self.assertNotIn('fictional-private-note',json.dumps(self.report()))

    def test_projection_does_not_mutate_input(self):
        before=copy.deepcopy((self.job,self.task)); self.report(); self.assertEqual((self.job,self.task),before)

    def test_attempt_without_agent_run_is_explicitly_unmeasured(self):
        self.task.pop('agent_run'); self.task['current_attempt']={'id':'fictional-attempt'}
        report=self.report(); self.assertEqual(len(report['rows']),1)
        self.assertIsNone(report['rows'][0]['run_id']); self.assertIsNone(report['rows'][0]['input_total'])

    def test_held_run_is_not_billed_as_an_invocation(self):
        self.run['status']='held'; report=self.report()
        self.assertEqual(report['rows'],[]); self.assertIsNone(report['jobs'][self.job['id']]['cost_usd']['total'])

    def test_existing_report_reads_fictional_db_without_writes_or_text_export(self):
        from team_optimization_metrics import attempts, workflow_report, ATTEMPT_FIELDS
        with tempfile.TemporaryDirectory(prefix='fictional-metrics-db-') as root:
            path=Path(root)/'fixture.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE objects (id TEXT,kind TEXT,body TEXT)')
                for kind,value in (('job',self.job),('task',self.task)):
                    db.execute('INSERT INTO objects VALUES (?,?,?)',(value['id'],kind,json.dumps(value)))
                db.commit()
            before=path.read_bytes(); rows,_,_=attempts(path); report=workflow_report(path)
            self.assertEqual(path.read_bytes(),before); self.assertEqual(len(rows),1)
            self.assertEqual(set(rows[0]),set(ATTEMPT_FIELDS)); self.assertEqual(len(report['rows']),1)
            import team_optimization_metrics as metrics
            with patch.object(metrics,'_measurement_snapshot',wraps=metrics._measurement_snapshot) as read:
                combined=metrics.report(path)
                self.assertEqual(read.call_count,1)
                self.assertEqual(combined['overall']['input_total'],combined['workflow_measurements']['jobs'][self.job['id']]['tokens']['input_total']['total'])


if __name__=='__main__': unittest.main()
