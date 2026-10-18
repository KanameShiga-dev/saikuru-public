"""P0 contracts, API role boundaries, and bounded local recovery with fictional data."""

import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from eval.development_readiness.reacceptance_fixture import close, make_fixture
from team_p0_contract import artifact_records, current_artifacts, initial, p0_artifact_text, progress, validate_plan
from team_recovery import p0_failure_category, p0_interruption, retry_local_read
from team_store import Store


class P0ContractTests(unittest.TestCase):
    def test_shared_fixture_matches_server_progress(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures' / 'p0' / 'state.json').read_text(encoding='utf-8'))
        for job in fixture['jobs']:
            if job.get('p0_contract_version') == 1:
                self.assertEqual(progress(job, fixture['tasks'], fixture['approvals']), job['p0_progress'])
            else:
                self.assertNotIn('p0_progress', job)
        self.assertEqual(len(fixture['jobs']), 11)

    def test_progress_actions_are_hints_from_current_receipt(self):
        job = {'id': 'fictional-job', 'status': 'awaiting_acceptance',
               'p0_artifacts': [{'path': 'app/index.html', 'sha256': 'fictional'}]}
        receipt = {'job_id': job['id'], 'kind': 'completion', 'status': 'pending',
                   'intended_role': 'end_user',
                   'payload': {'pending_items': [{'checks': ['Open the fictional app']}]}}
        projected = progress(job, [], [receipt])
        self.assertEqual(projected['completion_status'], 'awaiting_user_acceptance')
        self.assertEqual(projected['available_actions'],
                         ['cancel_job', 'review_completion', 'record_completion_evidence'])
        self.assertEqual(projected['deliverable_access'], [
            {'path': 'app/index.html', 'method': 'project_relative_file',
             'launch_status': 'unverified',
             'launch_hint': '起動手順は未登録です。成果物の内容を確認し、起動と操作は別途検証してください。'}])
        receipt['intended_role'] = 'maintainer'
        self.assertEqual(progress(job, [], [receipt])['available_actions'], ['cancel_job'])

    def test_p0_artifact_text_reads_only_current_registered_small_text(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'lookup.py'
            path.write_bytes(b'print("fictional item")\n')
            job = {'project': folder, 'p0_contract_version': 1,
                   'p0_artifacts': artifact_records(folder, ['lookup.py'])}
            self.assertEqual(p0_artifact_text(job, 'lookup.py')['content'],
                             'print("fictional item")\n')
            with self.assertRaisesRegex(ValueError, '登録済み'):
                p0_artifact_text(job, 'other.py')
            path.write_bytes(b'print("changed")\n')
            with self.assertRaisesRegex(ValueError, '変更'):
                p0_artifact_text(job, 'lookup.py')

    def test_original_request_cannot_change_after_registration(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            try:
                job = store.put('job', {'id': 'fictional', 'goal': 'Make a fictional lookup',
                                        'status': 'planning', 'p0_contract_version': 1,
                                        'goal_contract': initial('Make a fictional lookup')})
                with self.assertRaisesRegex(ValueError, '元の依頼'):
                    store.update(job['id'], goal='Make a different product')
                self.assertEqual(store.get(job['id'])['goal'], 'Make a fictional lookup')
                store.update(job['id'], status='running')
                with self.assertRaisesRegex(ValueError, '承認済みの達成条件'):
                    store.update(job['id'], goal_contract=dict(
                        initial('Make a fictional lookup'), primary_goal='Another goal'))
            finally:
                store.db.close()

    def test_plan_needs_observable_criteria_and_task_links(self):
        job = {'goal': 'Build a fictional lookup'}
        plan = {
            'goal_contract': {'primary_goal': 'Search fictional items',
                              'deliverables': ['Lookup page'],
                              'acceptance_criteria': ['Unknown IDs show no result'],
                              'non_goals': [], 'constraints': ['Fictional data']},
            'tasks': [{'title': 'Build page', 'instruction': 'Build a lookup page',
                       'role': 'builder', 'goal_links': ['Lookup page']}],
            'code_test_plan': {'required': True, 'cases': ['Unknown ID'],
                               'command': 'python -m unittest'}
        }
        self.assertEqual(validate_plan(job, plan)['acceptance_criteria'],
                         ['Unknown IDs show no result'])
        plan['tasks'][0]['goal_links'] = ['Unrelated optimization']
        with self.assertRaisesRegex(ValueError, '対応しない'):
            validate_plan(job, plan)

    def test_artifact_is_current_and_contained_in_project(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'lookup.py').write_text('print("fictional")\n', encoding='utf-8')
            job = {'project': folder, 'p0_artifacts': artifact_records(folder, ['lookup.py'])}
            self.assertEqual(current_artifacts(job), (True, ''))
            (root / 'lookup.py').write_text('print("changed")\n', encoding='utf-8')
            self.assertFalse(current_artifacts(job)[0])
            with self.assertRaisesRegex(ValueError, '相対パス'):
                artifact_records(folder, ['../outside.py'])
            (root / '.env').write_text('FICTIONAL_SETTING=placeholder\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '機密設定'):
                artifact_records(folder, ['.env'])

    def test_local_read_retries_only_transient_sqlite_lock(self):
        calls = []
        def read():
            calls.append(1)
            if len(calls) < 3:
                raise sqlite3.OperationalError('database is locked')
            return 'fictional result'
        self.assertEqual(retry_local_read(read), 'fictional result')
        self.assertEqual(len(calls), 3)
        with self.assertRaises(sqlite3.OperationalError):
            retry_local_read(lambda: (_ for _ in ()).throw(
                sqlite3.OperationalError('database is malformed')))

    def test_repeated_failure_keeps_same_resume_target_and_count(self):
        task = {'id': 'fictional-task', 'role': 'builder', 'profile': {'adapter': 'codex'}}
        first = p0_interruption(task, None, 'startup')
        second = p0_interruption(task, first, 'startup')
        self.assertEqual(second['attempt_count'], 2)
        self.assertEqual(second['resume_target'], task['id'])

    def test_pre_provider_secret_refusal_is_a_safety_stop(self):
        self.assertEqual(p0_failure_category('prompt_secret'), 'SAFETY_BLOCKED')
        self.assertEqual(p0_failure_category('reference_secret'), 'SAFETY_BLOCKED')
        self.assertEqual(p0_failure_category('startup'), 'MAINTENANCE_REQUIRED')

    def test_goal_and_recovery_count_survive_store_reopen(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            goal = 'Build fictional lookup'
            job = store.put('job', {'id': 'fictional', 'goal': goal, 'status': 'blocked',
                                    'p0_contract_version': 1, 'goal_contract': initial(goal)})
            task = store.put('task', {'id': 'fictional-task', 'job_id': job['id'],
                                      'role': 'builder', 'status': 'failed',
                                      'profile': {'adapter': 'codex'}})
            first = p0_interruption(task, None, 'startup')
            store.update(job['id'], p0_interruption=p0_interruption(task, first, 'startup'))
            store.db.close()
            reopened = Store(folder)
            try:
                restored = reopened.get(job['id'], 'job')
                self.assertEqual(restored['goal'], goal)
                self.assertEqual(restored['p0_interruption']['attempt_count'], 2)
                self.assertEqual(restored['p0_interruption']['resume_target'], task['id'])
                self.assertEqual(reopened.get(task['id'], 'task')['status'], 'failed')
            finally:
                reopened.db.close()


class P0ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server = make_fixture(Path(self.temp.name))
        self.server.app.maintenance_token = 'synthetic-maintenance-capability'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        close(self.server)

    def request(self, path, body=None, maintainer=False):
        headers = {'Cookie': 'agent_team=' + self.server.app.cookie,
                   'X-Agent-Team-UI': '1', 'Origin': self.server.app.origin,
                   'Content-Type': 'application/json'}
        if maintainer:
            headers['X-Saikuru-Maintenance'] = self.server.app.maintenance_token
        request = Request(self.server.app.origin + path,
                          data=json.dumps(body).encode() if body is not None else None,
                          headers=headers, method='POST' if body is not None else 'GET')
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def test_standard_api_reads_only_registered_current_p0_text(self):
        job = self.server.app.store.all('job')[0]
        path = Path(job['project']) / 'lookup.py'
        path.write_bytes(b'print("fictional item")\n')
        self.server.app.store.update(job['id'], p0_contract_version=1,
                                     goal_contract=initial(job['goal']),
                                     p0_artifacts=artifact_records(job['project'], ['lookup.py']))
        address = '/api/job/p0-artifact?job_id=' + job['id'] + '&path=lookup.py'
        status, payload = self.request(address)
        self.assertEqual(status, 200)
        self.assertEqual(payload['content'], 'print("fictional item")\n')
        self.assertFalse(payload['truncated'])
        self.assertEqual(self.request(address.replace('lookup.py', 'other.py'))[0], 400)
        path.write_bytes(b'print("changed")\n')
        self.assertEqual(self.request(address)[0], 400)

    def test_maintenance_question_is_not_a_user_approval(self):
        job = self.server.app.store.all('job')[0]
        self.server.app.store.update(job['id'], p0_contract_version=1,
                                     goal_contract=initial(job['goal']))
        task = self.server.app.store.for_job('task', job['id'])[0]
        self.server.app.store.update(task['id'], title='TEMP/TMP synthetic technical task')
        approval = self.server.app.engine.new_approval(task, 'question', {
            'summary': 'サンドボックスの起動設定を直す必要があります。',
            'questions': [{'id': 'technical', 'text': 'TEMP/TMPをどう変更しますか？',
                           'options': [{'id': 'a', 'label': '変更する',
                                        'input_required': False, 'input_label': ''}]}]})
        self.assertEqual(approval['intended_role'], 'maintainer')
        status, public = self.request('/api/state')
        self.assertEqual(status, 200)
        public_approval = next(item for item in public['approvals'] if item['id'] == approval['id'])
        self.assertEqual((public_approval['category'], public_approval['intended_role']),
                         ('MAINTENANCE', 'maintainer'))
        self.assertNotIn('TEMP/TMP', json.dumps(public_approval, ensure_ascii=False))
        self.assertNotIn('TEMP/TMP', json.dumps(public['tasks'], ensure_ascii=False))
        status, detail = self.request('/api/tasks/detail?id=' + task['id'])
        self.assertEqual(status, 200)
        self.assertNotIn('TEMP/TMP', json.dumps(detail, ensure_ascii=False))
        self.assertEqual(self.request('/api/decide', {'id': approval['id'], 'allow': False})[0], 403)
        self.assertEqual(self.server.app.store.get(approval['id'])['status'], 'pending')
        self.assertEqual(self.request('/api/decide', {'id': approval['id'], 'allow': False},
                                      maintainer=True)[0], 200)
        status, after = self.request('/api/state')
        self.assertEqual(status, 200)
        self.assertNotIn('TEMP/TMP', json.dumps(after, ensure_ascii=False))

    def test_maintenance_failure_details_do_not_reach_standard_state(self):
        store = self.server.app.store
        job = store.all('job')[0]
        task = store.for_job('task', job['id'])[0]
        store.update(job['id'], p0_contract_version=1, goal_contract=initial(job['goal']),
                     p0_interruption={'interrupted_task_id': task['id'],
                                      'resume_target': task['id'],
                                      'recovery_status': 'maintenance_required',
                                      'interruption_reason': 'synthetic-technical-detail'})
        store.update(task['id'], title='synthetic-technical-detail')
        store.event('task_error', 'synthetic-technical-detail', task['id'], job['id'])
        status, public = self.request('/api/state')
        self.assertEqual(status, 200)
        self.assertNotIn('synthetic-technical-detail',
                         json.dumps(public, ensure_ascii=False))
        self.assertEqual(public['jobs'][0]['p0_progress']['current'], '保守対応待ち')
        masked = next(item for item in public['tasks'] if item['id'] == task['id'])
        self.assertEqual(masked['profile']['adapter'], '')

    def test_unrelated_optimization_becomes_nonblocking_backlog(self):
        store = self.server.app.store
        job = store.all('job')[0]
        store.update(job['id'], p0_contract_version=1,
                     goal_contract=initial(job['goal']))
        task = store.for_job('task', job['id'])[0]
        result = {'status': 'needs_changes', 'stage_complete': True,
                  'warnings': ['架空検索の完了と無関係な内部最適化案'],
                  'blocking_reasons': [], 'questions': [], 'pending_checks': [],
                  'question': ''}
        self.server.app.engine._record_progress_warnings(task, result)
        self.assertEqual(result['status'], 'done')
        updated = store.get(job['id'], 'job')
        self.assertEqual(updated['goal'], job['goal'])
        self.assertEqual(len(updated['warning_issues']), 1)
        self.assertFalse(updated['warning_issues'][0]['blocking'])

    def test_safety_hold_preserves_task_and_does_not_claim_completion(self):
        store = self.server.app.store
        job = store.all('job')[0]
        task = store.for_job('task', job['id'])[0]
        store.update(job['id'], p0_contract_version=1,
                     goal_contract=initial(job['goal']))
        store.update(task['id'], status='blocked')
        self.server.app.engine._hold_p0_task(task, 'reference_secret', 'SAFETY_BLOCKED')
        held = store.get(job['id'], 'job')
        self.assertEqual(held['status'], 'blocked')
        self.assertEqual(held['p0_interruption']['recovery_status'], 'safety_blocked')
        self.assertEqual(held['p0_interruption']['resume_target'], task['id'])
        self.assertEqual(store.get(task['id'], 'task')['status'], 'blocked')

    def test_reviewer_repair_keeps_requirement_link(self):
        store = self.server.app.store
        job = store.all('job')[0]
        contract = initial(job['goal'])
        contract['deliverables'] = ['lookup.py']
        contract['acceptance_criteria'] = ['Unknown ID shows no result']
        store.update(job['id'], p0_contract_version=1,
                     goal_contract=contract, status='running')
        reviewer = next(task for task in store.for_job('task', job['id'])
                        if task.get('agent_name') == 'reviewer')
        store.update(reviewer['id'], goal_links=['lookup.py'], goal_id=job['id'])
        result = {'status': 'needs_changes', 'summary': 'Unknown ID is not handled',
                  'checks': ['Unknown ID shows no result'], 'pending_checks': [],
                  'blocking_reasons': [{'requirement': 'Unknown ID shows no result',
                                        'impact': 'The required response is missing'}],
                  'questions': [], 'question': ''}
        self.server.app.engine._finish(reviewer, result)
        repair = next(task for task in store.for_job('task', job['id'])
                      if task['role'] == 'builder' and task.get('after') == reviewer['id'])
        self.assertEqual(repair['goal_links'], ['Unknown ID shows no result'])

    def test_pending_acceptance_cannot_be_formally_accepted(self):
        job = self.server.app.store.all('job')[0]
        project = Path(job['project'])
        (project / 'lookup.py').write_text('print("fictional")\n', encoding='utf-8')
        self.server.app.store.update(job['id'], p0_contract_version=1,
                                     goal_contract=initial(job['goal']),
                                     p0_artifacts=artifact_records(project, ['lookup.py']))
        approval = next(a for a in self.server.app.store.all('approval') if a['kind'] == 'completion')
        self.server.app.engine.acknowledge_completion_review(approval['id'], True)
        with self.assertRaisesRegex(ValueError, '未完了のまま正式受領'):
            self.server.app.engine.accept_completion(approval['id'], '正式受領')

    def test_user_evidence_and_current_artifact_are_required_for_acceptance(self):
        store = self.server.app.store
        job = store.all('job')[0]
        project = Path(job['project'])
        artifact = project / 'lookup.py'
        artifact.write_text('print("fictional lookup")\n', encoding='utf-8')
        store.update(job['id'], p0_contract_version=1,
                     goal_contract=initial(job['goal']),
                     p0_artifacts=artifact_records(project, ['lookup.py']))
        approval = next(a for a in store.all('approval') if a['kind'] == 'completion')
        replacement = self.server.app.engine.record_completion_evidence(
            approval['id'], 0, 0, '合成画面で表示を確認', True)
        current_id = replacement['approval_id']
        self.server.app.engine.acknowledge_completion_review(current_id, True)
        artifact.write_text('print("changed after review")\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.server.app.engine.accept_completion(current_id, '正式受領')
        artifact.write_text('print("fictional lookup")\n', encoding='utf-8')
        self.assertEqual(self.server.app.engine.accept_completion(
            current_id, '正式受領')['status'], 'accepted')
