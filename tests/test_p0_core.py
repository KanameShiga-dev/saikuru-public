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
from team_p0_contract import artifact_records, current_artifacts, initial, validate_plan
from team_recovery import p0_interruption, retry_local_read
from team_store import Store


class P0ContractTests(unittest.TestCase):
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

    def test_maintenance_question_is_not_a_user_approval(self):
        job = self.server.app.store.all('job')[0]
        self.server.app.store.update(job['id'], p0_contract_version=1,
                                     goal_contract=initial(job['goal']))
        task = self.server.app.store.for_job('task', job['id'])[0]
        approval = self.server.app.engine.new_approval(task, 'question', {
            'summary': 'サンドボックスの起動設定を直す必要があります。',
            'questions': [{'id': 'technical', 'text': 'TEMP/TMPをどう変更しますか？',
                           'options': [{'id': 'a', 'label': '変更する',
                                        'input_required': False, 'input_label': ''}]}]})
        self.assertEqual(approval['intended_role'], 'maintainer')
        status, public = self.request('/api/state')
        self.assertEqual(status, 200)
        public_approval = next(item for item in public['approvals'] if item['id'] == approval['id'])
        self.assertNotIn('TEMP/TMP', json.dumps(public_approval, ensure_ascii=False))
        self.assertNotIn('TEMP/TMP', json.dumps(public['tasks'], ensure_ascii=False))
        status, detail = self.request('/api/tasks/detail?id=' + task['id'])
        self.assertEqual(status, 200)
        self.assertNotIn('TEMP/TMP', json.dumps(detail, ensure_ascii=False))
        self.assertEqual(self.request('/api/decide', {'id': approval['id'], 'allow': False})[0], 403)
        self.assertEqual(self.server.app.store.get(approval['id'])['status'], 'pending')
        self.assertEqual(self.request('/api/decide', {'id': approval['id'], 'allow': False},
                                      maintainer=True)[0], 200)

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
