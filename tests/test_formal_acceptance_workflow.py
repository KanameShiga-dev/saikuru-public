"""Synthetic approval workflow tests; all records and projects are fictional."""
import hashlib
import json
import threading
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from eval.development_readiness.reacceptance_fixture import make_fixture, close


class FormalAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server = make_fixture(Path(self.temp.name))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.store = self.server.app.store
        self.engine = self.server.app.engine
        self.job = self.store.get('synthetic-reacceptance', 'job')
        self.approval = self.store.all('approval')[0]

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        close(self.server)

    def api(self, path, body, authenticated=True):
        headers = {'Content-Type': 'application/json', 'X-Agent-Team-UI': '1',
                   'Origin': self.server.app.origin}
        if authenticated:
            headers['Cookie'] = 'agent_team=' + self.server.app.cookie
        request = Request(self.server.app.origin + path,
                          data=json.dumps(body, ensure_ascii=False).encode(), headers=headers, method='POST')
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def acknowledge(self, approval_id=None):
        return self.engine.acknowledge_completion_review(approval_id or self.approval['id'], True)

    def test_ACC01_plan_approval_does_not_accept_job(self):
        plan = self.engine.new_approval(self.store.all('task')[0], 'plan', {'tasks': []})
        self.store.update(self.job['id'], status='awaiting_approval')
        with patch.object(self.engine, '_expand_plan'):
            self.engine.decide(plan['id'], True, 'synthetic plan approval')
        self.assertEqual(self.store.get(self.job['id'])['status'], 'awaiting_approval')

    def test_ACC02_scope_approval_does_not_accept_job(self):
        approval = self.engine.new_approval(self.store.all('task')[0], 'scope_transfer', {'scope': 'fictional'})
        self.engine.decide(approval['id'], True, 'synthetic scope approval')
        self.assertEqual(self.store.get(self.job['id'])['status'], 'awaiting_acceptance')

    def test_ACC03_review_result_confirmation_does_not_accept_job(self):
        status, _ = self.api('/api/jobs/review-confirmation',
                             {'approval_id': self.approval['id'], 'confirmed': True})
        self.assertEqual(status, 200)
        self.assertEqual(self.store.get(self.job['id'])['status'], 'awaiting_acceptance')
        self.assertEqual(self.store.get(self.approval['id'])['status'], 'pending')
        self.assertEqual(self.store.get(self.approval['id'])['review_acknowledgement']['action'],
                         'review_result_confirmation')

    def test_ACC22_cancelled_prior_review_cycles_do_not_block_current_review(self):
        previous_builder = self.engine.new_task(
            self.job, '架空の過去工程', '古いレビュー周期を再現する合成タスク。', 'builder')
        self.store.update(previous_builder['id'], status='succeeded')
        previous_reviewer = self.engine.new_task(
            self.job, '架空の旧レビュー', '置き換えられたレビュー工程を再現する。', 'reviewer')
        self.store.update(previous_reviewer['id'], status='cancelled',
                          review_cycle=previous_builder['id'], agent_name='reviewer')

        self.acknowledge()
        self.assertEqual(self.store.get(self.approval['id'])['status'], 'pending')
        result = self.engine.accept_completion(self.approval['id'], '正式受領')
        self.assertEqual(result['status'], 'accepted_with_pending_checks')

    def test_ACC04_security_review_confirmation_does_not_accept_job(self):
        approval = self.engine.new_approval(self.store.all('task')[0], 'security_review_result', {'result': 'passed'})
        self.engine.decide(approval['id'], True, 'synthetic security review confirmation')
        self.assertEqual(self.store.get(self.job['id'])['status'], 'awaiting_acceptance')

    def test_ACC05_explicit_formal_acceptance_transitions_only_after_confirmation(self):
        with self.assertRaises(ValueError):
            self.engine.accept_completion(self.approval['id'], '受領')
        self.assertEqual(self.store.get(self.job['id'])['status'], 'awaiting_acceptance')
        self.acknowledge()
        result = self.engine.accept_completion(self.approval['id'], '正式受領')
        self.assertEqual(result['status'], 'accepted_with_pending_checks')

    def test_acceptance_preflight_checks_same_guards_without_accepting(self):
        status, body = self.api('/api/jobs/acceptance-check', {'approval_id': self.approval['id']})
        self.assertEqual(status, 400)
        self.assertIn('レビュー結果', body['error'])
        self.acknowledge()
        before = self.store.get(self.approval['id'])
        status, body = self.api('/api/jobs/acceptance-check', {'approval_id': self.approval['id']})
        self.assertEqual(status, 200)
        self.assertTrue(body['ready'])
        self.assertEqual(self.store.get(self.approval['id']), before)
        self.assertEqual(self.store.get(self.job['id'])['status'], 'awaiting_acceptance')
        self.assertFalse(any(e['type'] == 'formal_acceptance_decided' for e in self.store.events(100)))
        status, _ = self.api('/api/jobs/acceptance-check', {'approval_id': self.approval['id']}, authenticated=False)
        self.assertEqual(status, 403)

    def test_acceptance_preflight_exposes_stale_validation_and_rejects_extra_fields(self):
        self.store.update(self.job['id'], validation_commands=['python -m unittest'])
        self.acknowledge()
        status, body = self.api('/api/jobs/acceptance-check', {'approval_id': self.approval['id']})
        self.assertEqual(status, 400)
        self.assertIn('検証証跡', body['error'])
        status, _ = self.api('/api/jobs/acceptance-check', {'approval_id': self.approval['id'], 'allow': True})
        self.assertEqual(status, 400)
        self.assertEqual(self.store.get(self.job['id'])['status'], 'awaiting_acceptance')

    def test_ACC06_missing_required_test_evidence_blocks_acceptance(self):
        command = 'python -m unittest discover -s tests'
        self.store.update(self.job['id'], validation_commands=[command],
                          code_test_plan={'required': True, 'command': command})
        self.acknowledge()
        with self.assertRaisesRegex(ValueError, '検証証跡'):
            self.engine.accept_completion(self.approval['id'], '正式受領')
        self.assertEqual(self.store.get(self.job['id'])['status'], 'awaiting_acceptance')

    def test_ACC23_stale_validation_can_restart_without_rejecting_the_deliverable(self):
        command = 'python -m unittest discover -s tests'
        self.store.update(self.job['id'], validation_commands=[command])
        self.acknowledge()
        result = self.engine.revalidate_completion(self.approval['id'])
        self.assertEqual(self.store.get(self.job['id'])['status'], 'running')
        self.assertEqual(self.store.get(self.approval['id'])['status'], 'superseded')
        checker = self.store.get(result['task_id'], 'task')
        self.assertEqual(checker['role'], 'builder')
        self.assertTrue(checker['requires_validation_run'])
        self.assertIn('変更しないでください', checker['instruction'])
        reviewers = [t for t in self.store.for_job('task', self.job['id'])
                     if t.get('review_cycle') == checker['id'] and t['role'] == 'reviewer']
        self.assertEqual(len(reviewers), 2)

    def test_ACC07_unresolved_review_findings_block_acceptance(self):
        payload = dict(self.approval['payload'], review_findings=[{'reviewer': 'synthetic', 'summary': 'P0 finding'}])
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        self.store.update(self.approval['id'], payload=payload, digest=digest)
        self.acknowledge()
        with self.assertRaisesRegex(ValueError, '未解決のレビュー指摘'):
            self.engine.accept_completion(self.approval['id'], '正式受領')
        self.assertEqual(self.store.get(self.job['id'])['status'], 'awaiting_acceptance')

    def test_ACC17_tampered_approval_payload_fails_closed(self):
        payload = dict(self.approval['payload'], summary='tampered synthetic content')
        self.store.update(self.approval['id'], payload=payload)
        with self.assertRaisesRegex(ValueError, '不整合'):
            self.engine.accept_completion(self.approval['id'], '正式受領')
        self.assertEqual(self.store.get(self.job['id'])['status'], 'awaiting_acceptance')

    def test_ACC18_concurrent_duplicate_submissions_commit_only_once(self):
        self.acknowledge()
        barrier = threading.Barrier(2)
        outcomes = []

        def submit():
            barrier.wait(timeout=3)
            try:
                outcomes.append(self.engine.accept_completion(self.approval['id'], '正式受領')['status'])
            except ValueError:
                outcomes.append('rejected')

        workers = [threading.Thread(target=submit) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)
        self.assertCountEqual(outcomes, ['accepted_with_pending_checks', 'rejected'])
        events = [e for e in self.store.events(100) if e['type'] == 'formal_acceptance_decided']
        self.assertEqual(len(events), 1)
        self.assertEqual(self.store.get(self.job['id'])['status'], 'accepted_with_pending_checks')

    def test_ACC19_review_confirmation_is_bound_to_the_reviewed_content_version(self):
        self.acknowledge()
        payload = dict(self.approval['payload'], summary='new synthetic review version')
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        self.store.update(self.approval['id'], payload=payload, digest=digest)
        with self.assertRaisesRegex(ValueError, 'レビュー結果を確認してください'):
            self.engine.accept_completion(self.approval['id'], '正式受領')
        self.assertEqual(self.store.get(self.job['id'])['status'], 'awaiting_acceptance')

    def test_ACC08_unauthenticated_api_and_forged_engine_actor_are_rejected(self):
        with self.assertRaisesRegex(ValueError, '認証済み'):
            self.engine.accept_completion(self.approval['id'], '正式受領', actor='ai_agent')
        status, _ = self.api('/api/jobs/formal-acceptance',
                             {'approval_id': self.approval['id'], 'confirmation': '正式受領'},
                             authenticated=False)
        self.assertEqual(status, 403)
        self.assertEqual(self.store.get(self.job['id'])['status'], 'awaiting_acceptance')

    def test_ACC09_duplicate_submission_is_rejected_without_second_transition(self):
        self.acknowledge()
        self.engine.accept_completion(self.approval['id'], '正式受領')
        events_before = len(self.store.events(1000))
        with self.assertRaisesRegex(ValueError, '処理済み'):
            self.engine.accept_completion(self.approval['id'], '正式受領')
        self.assertEqual(self.store.get(self.job['id'])['status'], 'accepted_with_pending_checks')
        self.assertEqual(len(self.store.events(1000)), events_before)

    def test_ACC10_stale_job_state_rejects_acceptance(self):
        self.store.update(self.job['id'], status='running')
        with self.assertRaisesRegex(ValueError, '受領待ち'):
            self.engine.accept_completion(self.approval['id'], '正式受領')
        self.assertEqual(self.store.get(self.job['id'])['status'], 'running')

    def test_ACC11_legacy_decide_api_cannot_formally_accept(self):
        status, body = self.api('/api/decide', {'id': self.approval['id'], 'allow': True,
                                                'note': 'review result only, not formal acceptance'})
        self.assertNotEqual(status, 200)
        self.assertIn('正式受領', body['error'])
        self.assertEqual(self.store.get(self.job['id'])['status'], 'awaiting_acceptance')
        self.assertEqual(self.store.get(self.approval['id'])['status'], 'pending')

    def test_ACC12_ui_explains_job_target_and_explicit_receipt_action(self):
        request = Request(self.server.app.origin + '/app.js', headers={
            'Cookie': 'agent_team=' + self.server.app.cookie})
        with urlopen(request, timeout=3) as response:
            script = response.read().decode('utf-8')
        self.assertIn('依頼全体の正式受領', script)
        self.assertIn('対象依頼：', script)
        self.assertIn('確認欄に「正式受領」と入力', script)
        self.assertIn('/api/jobs/formal-acceptance', script)
        self.assertIn('/api/jobs/review-confirmation', script)
        self.assertIn('検証だけを再実行してレビューし直す', script)
        self.assertIn('/api/jobs/revalidate', script)
        self.assertIn('自動修正せず、判断待ちで停止します', script)

    def test_ACC13_conditional_acceptance_records_remaining_checks(self):
        self.acknowledge()
        result = self.engine.accept_completion(self.approval['id'], '正式受領')
        self.assertEqual(result['status'], 'accepted_with_pending_checks')
        self.assertTrue(result['audit']['conditional'])
        self.assertGreater(result['audit']['unresolved_items'], 0)

    def test_ACC14_cancelled_or_superseded_request_cannot_accept(self):
        replacement = self.engine.new_approval(self.store.all('task')[1], 'completion',
                                               dict(self.approval['payload']))
        with self.assertRaisesRegex(ValueError, '古くなりました'):
            self.engine.accept_completion(self.approval['id'], '正式受領')
        self.store.update(self.job['id'], status='cancelled')
        with self.assertRaisesRegex(ValueError, '受領待ち'):
            self.engine.accept_completion(replacement['id'], '正式受領')
        self.assertEqual(self.store.get(self.job['id'])['status'], 'cancelled')

    def test_ACC15_audit_records_actor_target_action_and_transition(self):
        status, _ = self.api('/api/jobs/review-confirmation',
                             {'approval_id': self.approval['id'], 'confirmed': True})
        self.assertEqual(status, 200)
        status, body = self.api('/api/jobs/formal-acceptance', {
            'approval_id': self.approval['id'], 'confirmation': '正式受領', 'actor': 'forged-ai-actor'})
        self.assertEqual(status, 200)
        saved = self.store.get(self.approval['id'])['decision']
        event = next(e for e in self.store.events(100) if e['type'] == 'formal_acceptance_decided')
        transition = [row for row in self.store.db.execute(
            'SELECT from_status,to_status,reason FROM transitions WHERE job_id=? ORDER BY seq DESC',
            (self.job['id'],)).fetchall()][0]
        self.assertEqual(transition, ('awaiting_acceptance', body['status'], 'formal_acceptance'))
        for record in (saved, event['details']):
            self.assertEqual(record['actor'], 'authenticated_local_app_session')
            self.assertEqual(record['action'], 'formal_acceptance')
            self.assertEqual(record['target_job_id'], self.job['id'])
            self.assertEqual(record['from_status'], 'awaiting_acceptance')
            self.assertEqual(record['to_status'], body['status'])

    def test_ACC16_no004_operation_sequence_no_longer_accepts_implicitly(self):
        before = self.store.get(self.job['id'])['status']
        with self.assertRaisesRegex(ValueError, '正式受領は承認操作と分離'):
            self.engine.decide(self.approval['id'], True,
                               'review result confirmed; this is not formal acceptance')
        self.assertEqual(self.store.get(self.job['id'])['status'], before)
        self.assertEqual(self.store.get(self.approval['id'])['status'], 'pending')


    def test_ACC20_workspace_change_after_review_confirmation_rejects_old_receipt(self):
        self.acknowledge()
        (Path(self.job['project']) / 'deliverable.txt').write_text('changed after review', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '対象版が変化'):
            self.engine.accept_completion(self.approval['id'], '正式受領')
        self.assertEqual(self.store.get(self.job['id'])['status'], 'awaiting_acceptance')
        self.assertEqual(self.store.get(self.approval['id'])['status'], 'pending')

    def test_ACC21_process_restart_key_rotation_requires_a_fresh_review(self):
        import team_engine
        self.acknowledge()
        previous = team_engine._ACCEPTANCE_TARGET_KEY
        try:
            team_engine._ACCEPTANCE_TARGET_KEY = __import__('secrets').token_bytes(32)
            with self.assertRaisesRegex(ValueError, '対象版が変化'):
                self.engine.accept_completion(self.approval['id'], '正式受領')
            self.assertEqual(self.store.get(self.job['id'])['status'], 'awaiting_acceptance')
        finally:
            team_engine._ACCEPTANCE_TARGET_KEY = previous

    def test_REC02_to_REC04_restart_reissues_confirmation_only_for_unchanged_target(self):
        import secrets
        import team_engine
        self.acknowledge()
        previous = team_engine._ACCEPTANCE_TARGET_KEY
        try:
            team_engine._ACCEPTANCE_TARGET_KEY = secrets.token_bytes(32)
            status, result = self.api('/api/jobs/reconfirm-review',
                                      {'approval_id': self.approval['id'], 'confirmed': True})
            self.assertEqual(status, 200)
            replacement = self.store.get(result['approval_id'], 'approval')
            self.assertEqual(replacement['status'], 'pending')
            self.assertEqual(replacement['supersedes_approval_id'], self.approval['id'])
            self.assertNotIn('review_acknowledgement', replacement)
            self.assertEqual(self.store.get(self.approval['id'])['status'], 'superseded')
            with self.assertRaisesRegex(ValueError, '処理済み'):
                self.engine.accept_completion(self.approval['id'], '正式受領')
            # A second restart before the new acknowledgement remains recoverable.
            team_engine._ACCEPTANCE_TARGET_KEY = secrets.token_bytes(32)
            self.engine.acknowledge_completion_review(result['approval_id'], True)
            accepted = self.engine.accept_completion(result['approval_id'], '正式受領')
            self.assertEqual(accepted['status'], 'accepted_with_pending_checks')
            events = [e for e in self.store.events(100) if e['type'] == 'completion_review_reissued']
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]['details']['previous_approval_id'], self.approval['id'])
            self.assertEqual(events[0]['details']['replacement_approval_id'], result['approval_id'])
        finally:
            team_engine._ACCEPTANCE_TARGET_KEY = previous

    def test_REC05_changed_target_cannot_use_reconfirmation_recovery(self):
        import secrets
        import team_engine
        self.acknowledge()
        (Path(self.job['project']) / 'deliverable.txt').write_text('changed after review', encoding='utf-8')
        previous = team_engine._ACCEPTANCE_TARGET_KEY
        try:
            team_engine._ACCEPTANCE_TARGET_KEY = secrets.token_bytes(32)
            with self.assertRaisesRegex(ValueError, '再テストと再レビュー'):
                self.engine.reissue_completion_review(self.approval['id'], True)
        finally:
            team_engine._ACCEPTANCE_TARGET_KEY = previous
        self.assertEqual(self.store.get(self.job['id'])['status'], 'awaiting_acceptance')
        self.assertEqual(len([a for a in self.store.for_job('approval', self.job['id'])
                              if a.get('kind') == 'completion']), 1)

    def test_REC06_changed_review_result_cannot_use_reconfirmation_recovery(self):
        import secrets
        import team_engine
        self.acknowledge()
        reviewer = next(t for t in self.store.for_job('task', self.job['id']) if t.get('role') == 'reviewer')
        self.store.update(reviewer['id'], result={'status': 'done', 'summary': 'changed synthetic review'})
        previous = team_engine._ACCEPTANCE_TARGET_KEY
        try:
            team_engine._ACCEPTANCE_TARGET_KEY = secrets.token_bytes(32)
            with self.assertRaisesRegex(ValueError, 'レビュー結果または担当状態'):
                self.engine.reissue_completion_review(self.approval['id'], True)
        finally:
            team_engine._ACCEPTANCE_TARGET_KEY = previous

    def test_REC07_changed_unresolved_items_cannot_use_reconfirmation_recovery(self):
        import secrets
        import team_engine
        self.acknowledge()
        payload = dict(self.approval['payload'],
                       pending_items=[{'title': 'new unresolved synthetic item', 'checks': ['recheck'] }])
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        self.store.update(self.approval['id'], payload=payload, digest=digest)
        previous = team_engine._ACCEPTANCE_TARGET_KEY
        try:
            team_engine._ACCEPTANCE_TARGET_KEY = secrets.token_bytes(32)
            with self.assertRaisesRegex(ValueError, '以前の確認証跡'):
                self.engine.reissue_completion_review(self.approval['id'], True)
        finally:
            team_engine._ACCEPTANCE_TARGET_KEY = previous

    def test_REC09_reconfirmation_requires_authenticated_api_session(self):
        self.acknowledge()
        status, _ = self.api('/api/jobs/reconfirm-review',
                             {'approval_id': self.approval['id'], 'confirmed': True}, authenticated=False)
        self.assertEqual(status, 403)
        self.assertEqual(len([a for a in self.store.for_job('approval', self.job['id'])
                              if a.get('kind') == 'completion']), 1)

    def test_REC10_missing_recovery_metadata_fails_closed(self):
        import secrets
        import team_engine
        self.acknowledge()
        ack = self.store.get(self.approval['id'])['review_acknowledgement']
        ack.pop('target_state', None)
        self.store.update(self.approval['id'], review_acknowledgement=ack)
        previous = team_engine._ACCEPTANCE_TARGET_KEY
        try:
            team_engine._ACCEPTANCE_TARGET_KEY = secrets.token_bytes(32)
            with self.assertRaisesRegex(ValueError, '安全な復旧情報'):
                self.engine.reissue_completion_review(self.approval['id'], True)
        finally:
            team_engine._ACCEPTANCE_TARGET_KEY = previous

    def test_REC02_environment_dependent_target_requires_full_rerun(self):
        import secrets
        import team_engine
        from unittest.mock import patch
        with patch('team_validation_evidence._environment_dependencies',
                   return_value=('selected', ('NO007_SYNTHETIC_SETTING',))):
            self.acknowledge()
            previous = team_engine._ACCEPTANCE_TARGET_KEY
            try:
                team_engine._ACCEPTANCE_TARGET_KEY = secrets.token_bytes(32)
                with self.assertRaisesRegex(ValueError, '環境変数依存'):
                    self.engine.reissue_completion_review(self.approval['id'], True)
            finally:
                team_engine._ACCEPTANCE_TARGET_KEY = previous
        self.assertEqual(len([a for a in self.store.for_job('approval', self.job['id'])
                              if a.get('kind') == 'completion']), 1)

if __name__ == '__main__':
    unittest.main()
