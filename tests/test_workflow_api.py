"""Authenticated loopback workflow controls with fictional, offline work packages."""
import copy
import json
import threading
import types
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from tests import test_wbs_rework as fixtures
from server import App, Handler


class WorkflowApiTests(unittest.TestCase):
    def setUp(self):
        f = self.f = fixtures.ReworkTests()
        self.addCleanup(f.doCleanups); f.setUp()
        self.engine, self.store = f.engine, f.store
        self.engine.paused = True
        app = self.app = types.SimpleNamespace(engine=self.engine,store=self.store,config=self.engine.config,
            cookie='fictional-session',maintenance_token='fictional-maintenance',health={})
        for name in ('history_days','state','quality','public_state'):
            setattr(app,name,types.MethodType(getattr(App,name),app))
        app.redact_state = App.redact_state; app.slim_task = App.slim_task
        app.ENDED_JOB_STATUSES = App.ENDED_JOB_STATUSES; app.MIN_RECENT_JOBS = App.MIN_RECENT_JOBS
        for name in ('usage','cli_update','claude_update','copilot_update','notifications'):
            setattr(app,name,types.SimpleNamespace(snapshot=lambda:{}))
        self.http = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.http.app = app
        app.port = self.http.server_port; app.origin = 'http://127.0.0.1:'+str(app.port)
        self.thread = threading.Thread(target=self.http.serve_forever,daemon=True); self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.http.shutdown(); self.thread.join(5); self.http.server_close()

    def request(self,path,body=None,*,cookie=True,ui=True,origin=None,maintenance=False,headers=None):
        h = {'Origin':origin or self.app.origin,'Content-Type':'application/json'}
        if cookie: h['Cookie']='agent_team='+self.app.cookie
        if ui: h['X-Agent-Team-UI']='1'
        if maintenance: h['X-Saikuru-Maintenance']=self.app.maintenance_token
        h.update(headers or {})
        req = Request(self.app.origin+path,data=json.dumps(body).encode() if body is not None else None,headers=h)
        try:
            with urlopen(req,timeout=30) as response: return response.status,json.load(response)
        except HTTPError as response: return response.code,json.load(response)

    def prepare_body(self):
        wp=self.f.wp()
        return {'job_id':self.f.job_id,'work_package_id':wp['id'],'work_package_revision':wp['state_revision'],
                'action':'request_feature_review'}

    def decision_body(self,review,allow=True):
        return {'approval_id':review['approval_id'],'allow':allow,'confirmation':self.f.confirmation(review,allow),
                'note':'Fictional inspected feature','action':'decide_feature_review'}

    def view(self,maintenance=False):
        return self.request(('/api/maintenance/workflow' if maintenance else '/api/workflow/job')+'?id='+self.f.job_id,
                            maintenance=maintenance)

    def test_cookie_ui_origin_and_fetch_guards_before_any_write(self):
        body=self.prepare_body(); before=self.f.state()
        for options in ({'cookie':False},{'ui':False},{'origin':'https://example.invalid'},
                        {'headers':{'Sec-Fetch-Site':'cross-site'}},
                        {'cookie':False,'headers':{'Authorization':'Bearer fictional-worker'}}):
            self.assertEqual(self.request('/api/workflow/feature-review/prepare',body,**options)[0],403)
        self.assertEqual(self.request('/api/workflow/job?id='+self.f.job_id,cookie=False)[0],403)
        self.assertEqual(self.f.state(),before)

    def test_exact_action_envelopes_refuse_role_or_actor_injection(self):
        body=self.prepare_body(); before=self.f.state()
        for altered in (dict(body,actor='authenticated_local_app_session'),dict(body,intended_role='end_user'),
                        dict(body,action='approve'),dict(body,maintenance=True)):
            code,value=self.request('/api/workflow/feature-review/prepare',altered)
            self.assertEqual((code,value['code']),(400,'workflow_request_invalid'))
        self.assertEqual(self.f.state(),before)

    def test_authenticated_prepare_decide_and_duplicate_refusal(self):
        for _ in range(3): self.f.finish_next()
        status,review=self.request('/api/workflow/feature-review/prepare',self.prepare_body())
        self.assertEqual(status,200,review)
        status,view=self.view(); self.assertEqual(status,200,view)
        self.assertEqual(view['wait_owner'],'user')
        self.assertIn('decide_feature_review',view['available_actions'])
        self.assertEqual(view['feature_confirmation_count'],1)
        status,result=self.request('/api/workflow/feature-review/decide',self.decision_body(review))
        self.assertEqual(status,200,result); self.assertFalse(result['formal_receipt'])
        self.assertEqual(self.f.job()['status'],'running')
        before=self.f.state()
        self.assertEqual(self.request('/api/workflow/feature-review/decide',self.decision_body(review))[0],400)
        self.assertEqual(self.f.state(),before)
        self.assertEqual(len(self.store.job_events(self.f.job_id,'feature_review_decided')),1)

    def test_target_drift_or_spoofed_actor_rejected_without_mutation(self):
        review=self.f.ready_feature(); body=self.decision_body(review); before=self.f.state()
        bad=copy.deepcopy(body); bad['confirmation']['target_digest']='fictional-stale'
        for value in (bad,dict(body,actor='authenticated_local_app_session'),dict(body,role='end_user')):
            self.assertEqual(self.request('/api/workflow/feature-review/decide',value)[0],400)
        (self.f.workspace.project/'app.py').write_text('Fictional changed after preview\n',encoding='utf-8')
        self.assertEqual(self.request('/api/workflow/feature-review/decide',body)[0],400)
        self.assertEqual(self.f.state(),before)

    def test_cancelled_received_and_unknown_jobs_cannot_use_pending_feature(self):
        review=self.f.ready_feature()
        for state in ('cancelled','accepted','accepted_with_pending_checks','fictional-unknown'):
            self.store.update(self.f.job_id,status=state); before=self.f.state()
            code,result=self.request('/api/workflow/feature-review/decide',self.decision_body(review))
            self.assertEqual((code,result['code']),(400,'workflow_feature_job_not_running'))
            self.assertEqual(self.f.state(),before)

    def test_stored_maintenance_role_does_not_become_feature_consent(self):
        review=self.f.ready_feature(); self.store.update(review['approval_id'],intended_role='maintainer',category='MAINTENANCE')
        before=self.f.state()
        self.assertEqual(self.request('/api/workflow/feature-review/decide',self.decision_body(review),maintenance=True)[0],400)
        self.assertEqual(self.f.state(),before)
        status,view=self.view(); self.assertEqual(status,200,view)
        self.assertFalse(next(p for p in view['pending_decisions'] if p['id']==review['approval_id'])['actionable'])

    def test_expired_target_requires_new_confirmation(self):
        review=self.f.ready_feature(); self.store.update(review['approval_id'],expires_at=1)
        status,view=self.view(); self.assertEqual(status,200,view)
        self.assertIn('request_feature_review',next(w for w in view['work_packages'] if w['id']==self.f.wp()['id'])['available_actions'])
        self.assertEqual(self.request('/api/workflow/feature-review/decide',self.decision_body(review))[0],400)
        status,fresh=self.request('/api/workflow/feature-review/prepare',self.prepare_body())
        self.assertEqual(status,200,fresh); self.assertNotEqual(fresh['approval_id'],review['approval_id'])
        self.assertEqual(self.store.get(fresh['approval_id'])['status'],'pending')
        self.assertEqual(self.store.job_events(self.f.job_id,'feature_review_decided'),[])

    def test_metadata_views_have_canonical_packages_and_do_not_probe_git(self):
        before=self.f.state()
        with patch.object(self.engine,'workspace_view',side_effect=AssertionError('Unexpected filesystem probe')), \
             patch.object(self.engine,'_execution_workspace',side_effect=AssertionError('Unexpected execution probe')):
            status,view=self.view(); self.assertEqual(status,200,view)
            status,state=self.request('/api/state'); self.assertEqual(status,200,state)
            status,quality=self.request('/api/quality/job?id='+self.f.job_id); self.assertEqual(status,200,quality)
        self.assertEqual(len(view['work_packages']),2)
        self.assertEqual(view['execution_permission'],'none'); self.assertEqual(view['file_validation'],'not_performed')
        self.assertEqual(view['context']['plan_revision'],self.f.job()['plan_revision'])
        job=next(j for j in state['jobs'] if j['id']==self.f.job_id)
        self.assertIn('workflow',job); self.assertIn('workflow',quality)
        self.assertEqual(self.f.state(),before)

    def test_stopped_job_projection_is_read_only_and_legacy_rework_blocked(self):
        for state in ('blocked','failed','interrupted','cancelled'):
            self.store.update(self.f.job_id,status=state); before=self.f.state()
            code,view=self.view(); self.assertEqual(code,200,view)
            self.assertEqual(len(view['work_packages']),2,view)
            self.assertEqual(view['execution_permission'],'none')
            self.assertEqual(self.f.state(),before)
        self.assertEqual(self.request('/api/jobs/rework',{'id':self.f.job_id})[0],400)

    def test_maintenance_authentication_is_separate_from_normal_cookie(self):
        path='/api/maintenance/workflow?id='+self.f.job_id
        self.assertEqual(self.request(path)[0],403)
        self.assertEqual(self.request(path,headers={'X-Saikuru-Maintenance':'fictional-wrong'})[0],403)
        code,view=self.view(maintenance=True); self.assertEqual(code,200,view)
        self.assertIn('maintenance_guidance',view)
        self.assertNotIn('maintenance_guidance',self.view()[1])
        self.assertEqual(self.request('/api/workflow/job?id='+self.f.job_id+'&role=maintainer',maintenance=True)[0],400)
        self.app.maintenance_token=''
        self.assertEqual(self.request(path,headers={'X-Saikuru-Maintenance':'fictional-maintenance'})[0],403)

    def test_recovery_confirmation_does_not_queue_and_retry_rejects_wrong_action(self):
        task=self.f.first
        body={'id':task['id'],'task_revision':task['state_revision'],'job_revision':self.f.job()['state_revision'],
              'attempt_id':'fictional-attempt','writer_stopped':True,'checked_changes':True}
        with patch.object(self.engine,'confirm_recovery',return_value={'confirmed':True,'queued':False}) as confirm:
            self.assertEqual(self.request('/api/maintenance/recovery/confirm',body)[0],403)
            confirm.assert_not_called()
            code,result=self.request('/api/maintenance/recovery/confirm',body,maintenance=True)
            self.assertEqual(code,200,result); self.assertFalse(result['queued']); confirm.assert_called_once()
        with patch.object(self.engine,'retry') as retry:
            self.assertEqual(self.request('/api/retry',{'id':task['id'],'checked_changes':True,'action':'approve'})[0],400)
            retry.assert_not_called()

    def test_feature_errors_do_not_echo_private_diagnostics(self):
        body={'approval_id':'fictional','allow':True,'confirmation':{},'note':'','action':'decide_feature_review'}
        for message,expected in (('fictional-sensitive-placeholder','workflow_input_or_maintenance_check_required'),
                                 ('[INPUT_GUARD_CONFIRM] fictional-sensitive-placeholder','workflow_sensitive_input_blocked')):
            with patch.object(self.engine,'decide_feature_review',side_effect=ValueError(message)):
                code,result=self.request('/api/workflow/feature-review/decide',body)
            self.assertEqual((code,result['code']),(400,expected))
            self.assertNotIn('fictional-sensitive-placeholder',json.dumps(result))

    def test_job_becoming_received_during_observation_cannot_be_confirmed(self):
        review=self.f.ready_feature(); original=self.engine._feature_observation
        def changed(*args):
            self.store.update(self.f.job_id,status='accepted')
            return original(*args)
        with patch.object(self.engine,'_feature_observation',side_effect=changed):
            code,result=self.request('/api/workflow/feature-review/decide',self.decision_body(review))
        self.assertEqual(code,400,result)
        self.assertEqual(self.store.get(review['approval_id'])['status'],'pending')
        self.assertEqual(self.store.job_events(self.f.job_id,'feature_review_decided'),[])

    def test_concurrent_duplicate_decisions_have_one_winner(self):
        review=self.f.ready_feature(); original=self.engine._feature_observation
        barrier=threading.Barrier(2)
        def observed(*args):
            value=original(*args); barrier.wait(timeout=20); return value
        results=[]; errors=[]
        def call():
            try: results.append(self.request('/api/workflow/feature-review/decide',self.decision_body(review)))
            except Exception as error: errors.append(type(error).__name__)
        with patch.object(self.engine,'_feature_observation',side_effect=observed):
            threads=[threading.Thread(target=call) for _ in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(35)
        self.assertFalse(errors); self.assertEqual(sorted(r[0] for r in results),[200,400],results)
        self.assertEqual(len(self.store.job_events(self.f.job_id,'feature_review_decided')),1)

    def test_feature_rework_requires_current_revision_and_target(self):
        review=self.f.ready_feature(); self.f.approve(review)
        body={'job_id':self.f.job_id,'work_package_id':self.f.wp()['id'],
              'work_package_revision':self.f.wp()['state_revision'],
              'confirmation':{'action':'request_feature_rework','target_digest':review['target_digest']},
              'note':'Fictional bounded defect','action':'rework_job'}
        before=self.f.state()
        self.assertEqual(self.request('/api/workflow/feature-rework',dict(body,work_package_revision=0))[0],400)
        self.assertEqual(self.request('/api/workflow/feature-rework',dict(body,actor='end_user'))[0],400)
        self.assertEqual(self.f.state(),before)
        code,result=self.request('/api/workflow/feature-rework',body)
        self.assertEqual(code,200,result); self.assertEqual(self.f.job()['status'],'running')
        self.assertEqual(self.f.wp()['workflow_generation'],1)
        self.assertEqual(self.request('/api/workflow/feature-rework',body)[0],400)

    def test_expired_foreign_approval_pointer_cannot_be_superseded(self):
        review=self.f.ready_feature()
        self.store.update(review['approval_id'],status='superseded')
        foreign=copy.deepcopy(self.store.get(review['approval_id']))
        foreign['id']='fictional-foreign-approval'; foreign['job_id']='fictional-other-job'
        foreign['status']='pending'; foreign['expires_at']=1; foreign.pop('state_revision',None)
        self.store.put('approval',foreign)
        self.store.update(self.f.wp()['id'],feature_review_approval_id=foreign['id'])
        before=self.f.state()
        self.assertEqual(self.request('/api/workflow/feature-review/prepare',self.prepare_body())[0],400)
        self.assertEqual(self.f.state(),before)

    def test_terminal_state_projection_does_not_trust_slimmed_task_cache(self):
        self.f.ready_feature(); self.store.update(self.f.job_id,status='accepted')
        first=self.app.state()
        self.store.update(self.f.first['id'],result={'status':'failed','pending_checks':['Fictional unverified']})
        second=self.app.state()
        job=next(j for j in second['jobs'] if j['id']==self.f.job_id)
        self.assertFalse(job['workflow']['progress']['complete'])
        self.assertNotEqual(first['tasks'],second['tasks'])

    def test_unknown_effects_stop_marker_never_grants_standard_retry(self):
        self.store.update(self.f.first['id'],status='interrupted',recovery_check_required=True,
                          summary='fictional-private-maintenance-diagnostic')
        self.store.update(self.f.job_id,status='interrupted',p0_interruption={
            'recovery_status':'maintenance_required','interrupted_task_id':self.f.first['id']})
        code,view=self.view(); self.assertEqual(code,200,view)
        self.assertEqual(view['wait_owner'],'maintainer'); self.assertTrue(view['uncertain_operation'])
        self.assertIn('workflow_recovery_confirmation_required',view['stop_codes'])
        self.assertNotIn('retry_task',view['available_actions'])
        self.assertNotIn('fictional-private-maintenance-diagnostic',json.dumps(view))
        self.assertNotIn('fictional-private-maintenance-diagnostic',json.dumps(self.app.public_state()))

    def test_expired_and_maintenance_completion_never_actionable(self):
        review=self.f.ready_feature()
        self.store.update(review['approval_id'],kind='completion',intended_role='maintainer')
        # Even a positive progress hint cannot replace the personal role check.
        progress=self.engine.workflow_progress(self.f.job_id); progress['review_available']=True
        with patch.object(self.engine,'workflow_progress',return_value=progress):
            code,view=self.view(); self.assertEqual(code,200,view)
        self.assertFalse(next(p for p in view['pending_decisions'] if p['id']==review['approval_id'])['actionable'])
        self.store.update(review['approval_id'],kind='question',intended_role='end_user',expires_at=1)
        code,view=self.view(); self.assertEqual(code,200,view)
        self.assertFalse(next(p for p in view['pending_decisions'] if p['id']==review['approval_id'])['actionable'])


if __name__=='__main__': unittest.main()
