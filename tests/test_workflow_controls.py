"""Versioned loopback control targets with fictional interrupted jobs only."""
import copy
import http.client
import json
import threading
import unittest
from unittest.mock import patch

from tests import test_recovery_controls as fixtures


class WorkflowControlTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.RecoveryControlTests(); self.addCleanup(self.f.doCleanups); self.f.setUp()
        self.engine,self.store=self.f.engine,self.f.store
        self.job_id,self.task_id=self.f.job['id'],self.f.task['id']
        self.server=self.f.start_http()

    def request(self,path,body=None,*,maintenance=False,cookie=True,ui=True,origin=None):
        headers={'Content-Type':'application/json'}
        if cookie: headers['Cookie']='agent_team='+self.server.app.cookie
        if ui: headers['X-Agent-Team-UI']='1'
        if maintenance: headers['X-Saikuru-Maintenance']=self.server.app.maintenance_token
        if origin: headers['Origin']=origin
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=15)
        try:
            connection.request('POST' if body is not None else 'GET',path,
                               json.dumps(body) if body is not None else None,headers)
            response=connection.getresponse(); return response.status,json.loads(response.read())
        finally: connection.close()

    def target(self,action='cancel_job'):
        return self.engine.workflow_control_target(self.job_id,action,None if action=='cancel_job' else self.task_id)

    def body(self,action='cancel_job'):
        value={'job_id':self.job_id,'action':action,'target':self.target(action)}
        if action=='cancel_job': return dict(value,confirmed=True)
        if action=='confirm_recovery': return dict(value,task_id=self.task_id,writer_stopped=True,checked_changes=True)
        return dict(value,task_id=self.task_id,checked_changes=True,note='Fictional inspected retry',handoff_allowed=False)

    def state(self):
        return (self.store.db.execute('SELECT * FROM objects ORDER BY id').fetchall(),
                self.store.db.execute('SELECT * FROM events ORDER BY seq').fetchall())

    def test_read_targets_have_no_side_effects_or_project_probes(self):
        before=self.state()
        with patch.object(self.engine,'workspace_view',side_effect=AssertionError('No project probe')):
            code,result=self.request('/api/workflow/controls?id='+self.job_id)
        self.assertEqual(code,200,result); self.assertEqual(result['contract_version'],1)
        self.assertEqual(result['execution_permission'],'none'); self.assertTrue(result['decides_nothing'])
        self.assertEqual(result['cancel_target'],self.target())
        self.assertEqual(result['artifact_path_guidance']['fictional_data_component'],'fixtures')
        self.assertFalse(result['artifact_path_guidance']['changes_policy'])
        self.assertEqual(self.state(),before)

    def test_browser_identity_and_origin_are_required_before_cancel(self):
        body=self.body(); before=self.state()
        for options in ({'cookie':False},{'ui':False},{'origin':'https://example.invalid'}):
            self.assertEqual(self.request('/api/workflow/control',body,**options)[0],403)
        self.assertEqual(self.request('/api/workflow/controls?id='+self.job_id,cookie=False)[0],403)
        self.assertEqual(self.state(),before)

    def test_exact_fields_and_explicit_confirmation_required(self):
        body=self.body(); before=self.state()
        for value in (dict(body,actor='end_user'),dict(body,confirmed=1),dict(body,confirmed=False),
                      dict(body,action='resume_everything'),dict(body,target=None)):
            self.assertEqual(self.request('/api/workflow/control',value)[0],400)
        self.assertEqual(self.state(),before)

    def test_tampered_or_wrong_action_target_refused(self):
        body=self.body(); before=self.state()
        for key,value in (('contract_version',True),('job_revision',float(body['target']['job_revision'])),
                          ('task_id',self.task_id),('job_id','fictional-foreign'),('state_digest','fictional-stale')):
            altered=copy.deepcopy(body); altered['target'][key]=value
            self.assertEqual(self.request('/api/workflow/control',altered)[0],400)
        self.assertEqual(self.request('/api/workflow/control',dict(body,target=self.target('retry_task')))[0],400)
        self.assertEqual(self.state(),before)

    def test_task_change_invalidates_job_cancel_target(self):
        body=self.body(); self.store.update(self.task_id,summary='Fictional changed after preview'); before=self.state()
        self.assertEqual(self.request('/api/workflow/control',body)[0],400)
        self.assertEqual(self.state(),before)

    def test_cancel_fences_attempts_without_restoring_files_and_is_not_receipt(self):
        before=self.f.app.read_bytes(); body=self.body()
        code,result=self.request('/api/workflow/control',body)
        self.assertEqual(code,200,result); self.assertFalse(result['edits_restored']); self.assertFalse(result['formal_receipt'])
        self.assertEqual(self.store.get(self.job_id)['status'],'cancelled')
        self.assertEqual(self.f.app.read_bytes(),before)
        self.assertNotEqual(self.store.get(self.task_id)['current_attempt']['status'],'running')
        snapshot=self.state()
        self.assertEqual(self.request('/api/workflow/control',body)[0],400)
        self.assertEqual(self.state(),snapshot)

    def test_received_job_cannot_be_cancelled_even_with_fresh_target(self):
        self.store.update(self.job_id,status='accepted'); body=self.body(); before=self.state()
        self.assertEqual(self.request('/api/workflow/control',body)[0],400)
        self.assertEqual(self.state(),before)

    def test_old_cancel_route_cannot_skip_confirmation_or_target(self):
        before=self.state()
        for body in ({'id':self.job_id},{'id':self.job_id,'confirmed':True,'workflow_target':None}):
            self.assertEqual(self.request('/api/cancel',body)[0],400)
        self.assertEqual(self.state(),before)
        self.assertEqual(self.request('/api/cancel',{'id':self.job_id,'confirmed':True,'workflow_target':self.target()})[0],200)

    def test_confirm_requires_maintenance_capability_and_never_queues(self):
        body=self.body('confirm_recovery'); before=self.state()
        self.assertEqual(self.request('/api/workflow/control',body)[0],403); self.assertEqual(self.state(),before)
        code,result=self.request('/api/workflow/control',body,maintenance=True)
        self.assertEqual(code,200,result); self.assertFalse(result['queued']); self.assertFalse(result['resume_allowed'])
        self.assertEqual(self.store.get(self.task_id)['status'],'interrupted')

    def test_confirmation_and_checkpoint_require_new_target_before_separate_retry(self):
        stale=self.body('retry_task')
        self.assertEqual(self.request('/api/workflow/control',self.body('confirm_recovery'),maintenance=True)[0],200)
        self.f.checkpoint()
        before=self.state()
        self.assertEqual(self.request('/api/workflow/control',stale,maintenance=True)[0],400)
        self.assertEqual(self.state(),before)
        code,result=self.request('/api/workflow/control',self.body('retry_task'),maintenance=True)
        self.assertEqual(code,200,result); self.assertTrue(result['queued']); self.assertFalse(result['started'])
        self.assertEqual(self.store.get(self.task_id)['status'],'queued')

    def test_retry_needs_current_checkpoint_and_maintenance(self):
        self.f.confirm(); body=self.body('retry_task')
        self.assertEqual(self.request('/api/workflow/control',body)[0],403)
        self.assertEqual(self.request('/api/workflow/control',body,maintenance=True)[0],400)
        self.assertEqual(self.store.get(self.task_id)['status'],'interrupted')

    def test_fresh_target_does_not_reconcile_unknown_operation(self):
        self.store.event('operation_result','Fictional uncertain action',self.task_id,self.job_id)
        code,result=self.request('/api/workflow/control',self.body('confirm_recovery'),maintenance=True)
        self.assertEqual(code,400,result)
        self.assertTrue(self.store.get(self.task_id)['recovery_check_required'])

    def test_foreign_task_and_capability_fields_do_not_grant_authority(self):
        body=self.body('confirm_recovery'); before=self.state()
        self.assertEqual(self.request('/api/workflow/control',dict(body,job_id='fictional-other'),maintenance=True)[0],400)
        self.assertEqual(self.request('/api/workflow/control',dict(body,role='maintainer'),maintenance=True)[0],400)
        self.assertEqual(self.state(),before)

    def test_retry_rechecks_target_after_filesystem_assessment(self):
        self.f.confirm(); self.f.checkpoint(); body=self.body('retry_task')
        original=self.engine.assess_checkpoint_resume
        def changed(*args,**kwargs):
            result=original(*args,**kwargs); self.store.update(self.task_id,summary='Fictional race'); return result
        with patch.object(self.engine,'assess_checkpoint_resume',side_effect=changed):
            self.assertEqual(self.request('/api/workflow/control',body,maintenance=True)[0],400)
        self.assertEqual(self.store.get(self.task_id)['status'],'interrupted')

    def test_concurrent_cancel_has_one_winner(self):
        body=self.body(); results=[]
        def call(): results.append(self.request('/api/workflow/control',body)[0])
        threads=[threading.Thread(target=call) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(15)
        self.assertEqual(sorted(results),[200,400])
        self.assertEqual(len(self.store.job_events(self.job_id,'cancelled')),1)

    def test_unknown_contract_never_falls_back_to_legacy_cancel(self):
        before=self.state(); original=self.store.get
        def unknown(identifier,*args,**kwargs):
            value=original(identifier,*args,**kwargs)
            return dict(value,workflow_contract_version=2) if identifier==self.job_id else value
        # The real Store already refuses to persist an unsupported version.
        with patch.object(self.store,'get',side_effect=unknown):
            self.assertEqual(self.request('/api/workflow/controls?id='+self.job_id)[0],400)
            self.assertEqual(self.request('/api/cancel',{'id':self.job_id})[0],400)
        self.assertEqual(self.state(),before)

    def test_local_io_error_is_fixed_and_does_not_echo_diagnostics(self):
        body=self.body(); before=self.state()
        with patch.object(self.engine,'cancel_job',side_effect=OSError('fictional-private-path')):
            code,result=self.request('/api/workflow/control',body)
        self.assertEqual((code,result['code']),(400,'workflow_local_io_check_required'))
        self.assertNotIn('fictional-private-path',json.dumps(result))
        self.assertEqual(self.state(),before)

    def test_new_checkpoint_draft_invalidates_previous_control_target(self):
        self.f.confirm(); body=self.body()
        self.engine.collect_checkpoint(self.task_id,writer_stopped=True)
        before=self.state()
        self.assertEqual(self.request('/api/workflow/control',body)[0],400)
        self.assertEqual(self.state(),before)

    def test_superseded_work_package_generation_cannot_be_retried(self):
        from tests import test_wbs_rework as packages
        f=packages.ReworkTests(); self.addCleanup(f.doCleanups); f.setUp()
        f.store.update(f.wp()['id'],workflow_generation=1)
        target=f.engine.workflow_control_target(f.job_id,'retry_task',f.first['id'])
        before=f.state()
        with self.assertRaisesRegex(ValueError,'workflow_control_task_not_current'):
            f.engine.retry(f.first['id'],'Fictional explicit retry',workflow_target=target)
        self.assertEqual(f.state(),before)

    def test_preplan_retry_reports_fresh_decision_instead_of_execution(self):
        job=self.store.put('job',fixtures.fixtures.initial_workflow(dict(id='fictional-preplan-control',
            title='Fictional plan',goal='Fictional plan',goal_contract=fixtures.fixtures.initial('Fictional plan'),
            project=str(self.f.project),status='awaiting_approval',request_origin='new',route='full',document_source=None,
            p0_contract_version=1,auto_execute=False,created_at=1,g2_gate_version=1,g2_status='consumed'),enabled=True))
        job=self.store.update(job['id'],status='planning')
        task=self.engine.new_task(job,'Fictional planner','Fictional plan only','planner')
        self.store.reserve_attempt(task['id'],task['state_revision']); self.store.recover()
        target=self.engine.workflow_control_target(job['id'],'confirm_recovery',task['id'])
        body={'job_id':job['id'],'task_id':task['id'],'action':'confirm_recovery','target':target,
              'writer_stopped':True,'checked_changes':True}
        self.assertEqual(self.request('/api/workflow/control',body,maintenance=True)[0],200)
        target=self.engine.workflow_control_target(job['id'],'retry_task',task['id'])
        body={'job_id':job['id'],'task_id':task['id'],'action':'retry_task','target':target,
              'checked_changes':True,'note':'Fictional explicit retry','handoff_allowed':False}
        code,result=self.request('/api/workflow/control',body,maintenance=True)
        self.assertEqual(code,200,result); self.assertFalse(result['started'])
        self.assertTrue(result['decision_required']); self.assertEqual(result['next_action'],'review_pending_decision')
        self.assertTrue(any(a['kind']=='ai_start' and a['status']=='pending' for a in self.store.for_job('approval',job['id'])))


if __name__=='__main__': unittest.main()
