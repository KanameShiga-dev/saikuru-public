"""Synthetic P0 path from approved plan through an actual local test and acceptance."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from team_config import default_config
from team_engine import Context, Engine
from team_p0_contract import initial
from team_store import Store


COMMAND = 'python -B -X utf8 -m unittest test_lookup'


class P0IntegrationTests(unittest.TestCase):
    def test_plan_to_artifact_to_user_acceptance(self):
        temp = tempfile.TemporaryDirectory(prefix='saikuru-p0-synthetic-')
        self.addCleanup(temp.cleanup)
        folder = temp.name
        root = Path(folder)
        store = Store(root / 'runtime')
        config = default_config()
        config['approved_roots'] = [str(root)]
        engine = Engine(store, config, {}, 'http://127.0.0.1:0')
        engine.paused = True
        self.addCleanup(store.db.close)
        self.addCleanup(engine.handoff.close)
        goal = 'Create a fictional lookup and show no result for an unknown ID'
        job = store.put('job', {'id': 'fictional-p0-job', 'title': 'Fictional lookup',
            'goal': goal, 'goal_contract': initial(goal), 'p0_contract_version': 1,
            'project': str(root), 'status': 'awaiting_approval', 'auto_execute': False,
            'validation_commands': [], 'code_test_contract_version': 1})
        planner = engine.new_task(job, 'Plan', 'Plan fictional lookup', 'planner')
        plan = {'summary': 'Create a fictional lookup', 'security_review_required': False,
            'goal_contract': {'primary_goal': 'Search fictional items',
                              'deliverables': ['lookup.py'],
                              'acceptance_criteria': ['Unknown ID shows no result'],
                              'non_goals': [], 'constraints': ['Fictional data only']},
            'tasks': [{'title': 'Build fictional lookup', 'instruction': 'Build and test lookup',
                       'role': 'builder', 'goal_links': ['lookup.py', 'Unknown ID shows no result']}],
            'code_test_plan': {'required': True, 'reason': 'New code',
                               'cases': ['Unknown ID shows no result'],
                               'command': COMMAND, 'existing_tests': []}}
        store.update(planner['id'], status='succeeded', result=plan)
        g3 = engine.new_approval(planner, 'plan', plan)
        self.assertEqual(store.get(job['id'])['status'], 'awaiting_approval')
        engine.decide(g3['id'], True, 'Fictional plan approved')
        self.assertEqual(store.get(g3['id'], 'approval')['status'], 'approved')
        tasks = store.for_job('task', job['id'])
        builder = next(task for task in tasks if task['role'] == 'builder')
        reviews = [task for task in tasks if task['role'] == 'reviewer']
        self.assertEqual(store.get(job['id'])['goal_contract']['original_request'], goal)
        self.assertEqual(store.get(builder['id'])['goal_links'],
                         ['lookup.py', 'Unknown ID shows no result'])
        self.assertTrue(all(store.get(task['id'])['goal_links'] ==
                            ['lookup.py', 'Unknown ID shows no result'] for task in reviews))

        (root / 'lookup.py').write_text(
            'def lookup(item_id):\n    return "No result" if item_id != "A001" else "Fictional item"\n',
            encoding='utf-8')
        (root / 'test_lookup.py').write_text(
            'import unittest\nfrom lookup import lookup\n'
            'class Check(unittest.TestCase):\n'
            '    def test_unknown(self): self.assertEqual(lookup("X999"), "No result")\n',
            encoding='utf-8')
        run = subprocess.run(['python', '-B', '-X', 'utf8', '-m', 'unittest', 'test_lookup'],
                             cwd=root, capture_output=True, text=True, timeout=15)
        self.assertEqual(run.returncode, 0, run.stderr)
        context = Context.__new__(Context)
        context.engine, context.task = engine, builder
        context.command_result(COMMAND, run.returncode, run.stdout + run.stderr)
        done = {'status': 'done', 'summary': 'Fictional lookup exists', 'checks': [],
                'pending_checks': [], 'question': '', 'questions': [], 'context_updates': [],
                'test_files': ['test_lookup.py'], 'test_change_reason': '',
                'artifacts': ['lookup.py']}
        engine._finish(builder, done)
        self.assertEqual(store.get(builder['id'])['result']['status'], 'done')
        review = {'status': 'done', 'summary': 'Synthetic review passed', 'checks': [],
                  'pending_checks': [], 'question': '', 'questions': [],
                  'context_updates': []}
        for reviewer in reviews:
            engine._finish(reviewer, dict(review))
        job = store.get(job['id'])
        self.assertEqual(job['status'], 'awaiting_acceptance')
        approval = next(item for item in store.for_job('approval', job['id'])
                        if item['kind'] == 'completion')
        self.assertEqual(len(approval['payload']['pending_items']), 2)
        with self.assertRaises(ValueError):
            engine.accept_completion(approval['id'], '正式受領')
        current_id = approval['id']
        for _ in range(2):
            response = engine.record_completion_evidence(
                current_id, 0, 0, '合成成果物の操作を確認', True)
            current_id = response['approval_id']
        engine.acknowledge_completion_review(current_id, True)
        self.assertEqual(engine.accept_completion(current_id, '正式受領')['status'], 'accepted')
        self.assertEqual(store.get(job['id'])['goal_contract']['completion_status'], 'accepted')

class WorkflowApiIntegrationTests(unittest.TestCase):
    """Synthetic worker results and synthetic user actions, never real receipt."""

    def test_serial_features_to_global_review_and_explicit_synthetic_receipt(self):
        from tests import test_workflow_api as fixtures
        f = fixtures.WorkflowApiTests(); self.addCleanup(f.doCleanups); f.setUp()
        engine, store, job_id = f.engine, f.store, f.f.job_id
        # The fixture publishes its fictional plan through the real G3 engine.
        self.assertEqual(f.f.job()['plan_revision'], 1)
        self.assertEqual(f.f.job()['status'], 'running')
        decided = []
        for index in range(2):
            builder = next(t for t in f.f.tasks() if t.get('plan_task_index') == index)
            if index == 1:
                reserved = engine._reserve_workflow_attempt(builder, engine.workflow_dispatch_view(job_id))
                engine.store.recover()  # Fictional crash, actual persistence/fencing path.
                target = engine.workflow_control_target(job_id, 'confirm_recovery', builder['id'])
                confirm = {'action': 'confirm_recovery', 'job_id': job_id, 'task_id': builder['id'],
                           'target': target, 'writer_stopped': True, 'checked_changes': True}
                self.assertEqual(f.request('/api/workflow/control', confirm)[0], 403)
                code, result = f.request('/api/workflow/control', confirm, maintenance=True)
                if code != 200:
                    # Preserve the real refusal reason in the sanitized local
                    # test report, rather than weakening the operation fence.
                    engine._recovery_control_inputs(builder['id'])
                self.assertEqual(code, 200, result); self.assertFalse(result['queued'])
                draft = engine.collect_checkpoint(builder['id'], writer_stopped=True)
                self.assertTrue(engine.finalize_checkpoint(draft['id'], writer_stopped=True)['handoff_synced'])
                retry = {'action': 'retry_task', 'job_id': job_id, 'task_id': builder['id'],
                         'target': engine.workflow_control_target(job_id, 'retry_task', builder['id']),
                         'checked_changes': True, 'note': 'SYNTHETIC crash inspected', 'handoff_allowed': False}
                code, result = f.request('/api/workflow/control', retry, maintenance=True)
                self.assertEqual(code, 200, result); self.assertFalse(result['started'])
                self.assertNotEqual(store.get(builder['id'])['current_attempt']['status'], 'running')
            for _ in range(3):
                f.f.finish_next(summary='SYNTHETIC Provider result; no native model')
            wp = store.get(builder['work_package_id'], 'work_package')
            body = {'job_id': job_id, 'work_package_id': wp['id'],
                    'work_package_revision': wp['state_revision'], 'action': 'request_feature_review'}
            code, review = f.request('/api/workflow/feature-review/prepare', body)
            self.assertEqual(code, 200, review)
            self.assertNotEqual(engine.workflow_dispatch_view(job_id)['status'], 'ready')
            code, result = f.request('/api/workflow/feature-review/decide', f.decision_body(review))
            self.assertEqual(code, 200, result); self.assertFalse(result['formal_receipt'])
            decided.append(review['approval_id'])
            self.assertEqual(f.f.job()['status'], 'running')
        global_reviews = [t for t in f.f.tasks() if t.get('workflow_job_task')]
        self.assertTrue(global_reviews)
        for _ in global_reviews:
            f.f.finish_next(summary='SYNTHETIC independent integration review')
        self.assertEqual(f.f.job()['status'], 'awaiting_acceptance')
        completion = next(a for a in store.for_job('approval', job_id)
                          if a['kind'] == 'completion' and a['status'] == 'pending')
        body = {'approval_id': completion['id'], 'confirmation': '正式受領', 'note': 'SYNTHETIC ONLY'}
        self.assertEqual(f.request('/api/jobs/formal-acceptance', body)[0], 400)
        # Clear only the actual generated checks, via the real HTTP evidence route.
        for _ in range(12):
            pending = completion['payload'].get('pending_items') or []
            if not pending: break
            code, result = f.request('/api/completion/evidence', {'approval_id': completion['id'],
                'item_index': 0, 'check_index': 0, 'evidence': 'SYNTHETIC local inspection only', 'confirmed': True})
            self.assertEqual(code, 200, result)
            completion = store.get(result['approval_id'], 'approval')
        self.assertFalse(completion['payload'].get('pending_items'))
        self.assertEqual(f.request('/api/jobs/review-confirmation',
                                  {'approval_id': completion['id'], 'confirmed': True})[0], 200)
        body['approval_id'] = completion['id']
        code, result = f.request('/api/jobs/formal-acceptance', body)
        self.assertEqual(code, 200, result); self.assertEqual(result['status'], 'accepted')
        self.assertEqual(f.request('/api/jobs/formal-acceptance', body)[0], 400)
        self.assertEqual(len(store.job_events(job_id, 'formal_acceptance_decided')), 1)
        self.assertEqual(len(decided), 2)


if __name__ == '__main__':
    unittest.main()
