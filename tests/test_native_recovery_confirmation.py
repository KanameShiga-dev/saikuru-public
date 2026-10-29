"""Fictional maintenance confirmation; never a Provider launch or user receipt."""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch


class NativeIdentityPredicateTests(unittest.TestCase):
    def test_current_and_archived_native_identities_share_conservative_predicate(self):
        from types import SimpleNamespace
        from team_store import Store
        task = {'id': 'SYNTHETIC-task', 'job_id': 'SYNTHETIC-job', 'current_attempt': {'id': 'SYNTHETIC-attempt'}}
        fake = SimpleNamespace(for_job=lambda kind, job: [])
        def started(value, attempt='SYNTHETIC-attempt'):
            return Store.native_attempt_started(fake, value, attempt)
        self.assertFalse(started(task))
        self.assertTrue(started(dict(task, agent_run={'id': 'SYNTHETIC-run'})))
        self.assertTrue(started(dict(task, agent_run_id='SYNTHETIC-run')))
        fake.for_job = lambda kind, job: [{'task_id': 'SYNTHETIC-task', 'attempt_id': 'different'}]
        self.assertTrue(started(task))
        fake.for_job = lambda kind, job: [{'task_id': 'different', 'attempt_id': 'SYNTHETIC-attempt'}]
        self.assertTrue(started(task))
        fake.for_job = lambda kind, job: [{'task_id': 'different', 'attempt_id': 'different'}]
        self.assertFalse(started(task))
        fake.for_job = lambda kind, job: [{'task_id': 'different'}]
        self.assertTrue(started(task, None))  # malformed historical identity fails closed


class LegacyConfirmationWorkerGuardTests(unittest.TestCase):
    def test_non_native_worker_cannot_use_legacy_store_confirmation(self):
        from tests.test_recovery_controls import RecoveryControlTests
        fixture = RecoveryControlTests(); self.addCleanup(fixture.doCleanups); fixture.setUp()
        store = fixture.store; task = store.get(fixture.task['id']); job = store.get(fixture.job['id'])
        attempt_id = task['current_attempt']['id']
        self.assertFalse(task.get('agent_run')); self.assertFalse(task.get('agent_run_id'))
        self.assertEqual(store.for_job('agent_run', job['id']), [])
        before = store.get(task['id']); events = store.job_events(job['id'], 'recovery_confirmed')
        with patch.object(store._worker, 'task', task, create=True):
            with self.assertRaisesRegex(ValueError, '^worker_cannot_confirm_recovery$'):
                store.confirm_recovery(task['id'], task['state_revision'], job['state_revision'], attempt_id)
        self.assertEqual(store.get(task['id']), before)
        self.assertEqual(store.job_events(job['id'], 'recovery_confirmed'), events)
        # The same valid arguments succeed outside the worker scope, proving
        # refusal above was not caused by an unrelated fixture precondition.
        confirmed = store.confirm_recovery(task['id'], task['state_revision'], job['state_revision'], attempt_id)
        self.assertFalse(confirmed['recovery_check_required'])
        self.assertEqual(confirmed['state_revision'], task['state_revision'] + 1)


class NativeConfirmationTests(unittest.TestCase):
    def setUp(self):
        from tests.test_p0_integration import NativeReconciliationTests
        NativeReconciliationTests.setUp(self)

    def assess(self):
        return self.f.request('/api/workflow/recovery/assess', self.body, maintenance=True)[1]

    def reconcile(self):
        from tests.test_p0_integration import NativeReconciliationTests
        body = NativeReconciliationTests.request_body(self)
        code, result = self.f.request('/api/workflow/recovery/reconcile', body, maintenance=True)
        self.assertEqual(code, 200, result)
        return body

    def confirmation_body(self):
        return {'action': 'confirm_recovery', 'job_id': self.job_id, 'task_id': self.task_id,
            'target': self.engine.workflow_control_target(self.job_id, 'confirm_recovery', self.task_id),
            'writer_stopped': True, 'checked_changes': True}

    def confirm(self, body=None, **options):
        return self.f.request('/api/workflow/control', body or self.confirmation_body(), maintenance=True, **options)

    def test_confirmation_retains_original_target_and_only_changes_protected_transition_fields(self):
        reconciliation = self.reconcile(); before = self.store.get(self.task_id); job = self.store.get(self.job_id)
        record = self.store.native_reconciliation(self.task_id, reconciliation['target'])
        code, result = self.confirm()
        self.assertEqual(code, 200, result); self.assertTrue(result['confirmed'])
        self.assertFalse(result['queued']); self.assertFalse(result['resume_allowed'])
        self.assertEqual(result['next_action'], 'native_checkpoint_binding_required')
        after = self.store.get(self.task_id); marker = after['recovery_confirmation']; proof = marker['native_reconciliation']
        self.assertFalse(after['recovery_check_required'])
        self.assertEqual(after['state_revision'], before['state_revision'] + 1)
        self.assertEqual(after['status'], before['status']); self.assertEqual(after['current_attempt'], before['current_attempt'])
        self.assertEqual(self.store.get(self.job_id), job)
        self.assertEqual(proof['record_id'], record['id']); self.assertEqual(proof['record_event_seq'], record['event_seq'])
        self.assertEqual(proof['record_digest'], record['record_digest'])
        self.assertEqual(proof['source_target'], reconciliation['target'])
        restored = copy.deepcopy(after)
        for key, source in proof['source_fields'].items():
            if source['present']: restored[key] = source['value']
            else: restored.pop(key, None)
        self.assertEqual(restored, before)
        invariants = {k:v for k,v in after.items() if k not in proof['source_fields']}
        self.assertEqual(proof['task_invariants_digest'], hashlib.sha256(json.dumps(invariants,
            sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest())
        row = self.store.db.execute('SELECT body FROM events WHERE seq=?', (marker['event_seq'],)).fetchone()
        event = json.loads(row[0]); self.assertEqual(event['type'], 'recovery_confirmed')
        self.assertEqual(event['details']['native_reconciliation'], proof)
        self.assertEqual(self.store.native_reconciliation(self.task_id, reconciliation['target']), record)
        assessment = self.assess()
        self.assertFalse(assessment['eligible_for_reconciliation'])
        self.assertIn('native_confirmation_binding_required', assessment['reasons'])
        self.assertIsNone(assessment['target'])
        with self.assertRaises(ValueError): self.engine.retry(self.task_id, 'SYNTHETIC inspected; binding still absent')
        self.assertEqual(self.store.get(self.task_id), after)

    def test_missing_reconciliation_does_not_clear_a_fence(self):
        before = self.f.f.state(); self.assertEqual(self.confirm()[0], 400)
        self.assertEqual(self.f.f.state(), before)

    def test_maintenance_browser_and_server_actor_are_required(self):
        self.reconcile(); body = self.confirmation_body(); before = self.f.f.state()
        self.assertEqual(self.f.request('/api/workflow/control', body)[0], 403)
        for options in ({'cookie': False}, {'ui': False}, {'origin': 'https://example.invalid'},
                        {'cookie': False, 'headers': {'Authorization': 'Bearer fictional-worker'}}):
            self.assertEqual(self.confirm(body, **options)[0], 403)
        self.assertEqual(self.confirm(dict(body, actor='authenticated_maintenance_session'))[0], 400)
        task = self.store.get(self.task_id); job = self.store.get(self.job_id)
        with self.assertRaises(ValueError):
            self.engine.confirm_recovery(self.task_id, task['state_revision'], job['state_revision'], self.run['attempt_id'],
                writer_stopped=True, checked_changes=True)
        self.assertEqual(self.f.f.state(), before)

    def test_changed_file_requires_a_new_explicit_reconciliation(self):
        self.reconcile(); body = self.confirmation_body()
        path = self.f.f.workspace.project / 'app.py'; path.write_bytes(path.read_bytes() + b'\n# SYNTHETIC later edit\n')
        before = self.f.f.state(); self.assertEqual(self.confirm(body)[0], 400)
        self.assertEqual(self.f.f.state(), before)

    def test_own_operation_invalidates_confirmation_even_after_a_judgment(self):
        self.reconcile()
        self.store.event('operation_result', 'SYNTHETIC unknown own effect', self.task_id, self.job_id)
        before = self.f.f.state(); self.assertEqual(self.confirm()[0], 400)
        self.assertEqual(self.f.f.state(), before)

    def test_stale_control_target_and_confirmation_flags_are_refused(self):
        self.reconcile(); body = self.confirmation_body()
        for flags in ({'writer_stopped': False}, {'checked_changes': 1}):
            before = self.f.f.state(); self.assertEqual(self.confirm(dict(body, **flags))[0], 400)
            self.assertEqual(self.f.f.state(), before)
        self.store.update(self.task_id, synthetic_metadata_changed=True)
        before = self.f.f.state(); self.assertEqual(self.confirm(body)[0], 400)
        self.assertEqual(self.f.f.state(), before)

    def test_worker_generic_store_and_direct_confirmation_cannot_forge_native_provenance(self):
        self.reconcile(); task = self.store.get(self.task_id); job = self.store.get(self.job_id); before = self.f.f.state()
        with self.assertRaises(ValueError):
            self.store.confirm_recovery(self.task_id, task['state_revision'], job['state_revision'], self.run['attempt_id'])
        with self.assertRaises(ValueError):
            self.store.update(self.task_id, recovery_confirmation={'native_reconciliation': {'record_id': 'fictional-forgery'}})
        with patch.object(self.store._worker, 'task', task, create=True):
            with self.assertRaises(ValueError):
                self.engine.confirm_recovery(self.task_id, task['state_revision'], job['state_revision'], self.run['attempt_id'],
                    writer_stopped=True, checked_changes=True, actor='authenticated_maintenance_session')
            with self.assertRaisesRegex(ValueError, 'worker_cannot'):
                self.store.confirm_native_recovery(self.task_id, 1, 1, self.run['attempt_id'], {}, (), lambda: None)
        self.assertEqual(self.f.f.state(), before)

    def test_change_before_durable_confirmation_cas_is_refused(self):
        self.reconcile(); body = self.confirmation_body(); confirm = self.store.confirm_native_recovery
        def changed(*args, **kwargs):
            self.store.update(self.task_id, synthetic_cas_change=True)
            return confirm(*args, **kwargs)
        with patch.object(self.store, 'confirm_native_recovery', side_effect=changed):
            self.assertEqual(self.confirm(body)[0], 400)
        self.assertTrue(self.store.get(self.task_id)['recovery_check_required'])
        self.assertIsNone(self.store.get(self.task_id).get('recovery_confirmation'))
        self.assertEqual(self.store.job_events(self.job_id, 'recovery_confirmed'), [])

    def test_transition_failure_rolls_back_event_and_restores_both_internal_guards(self):
        self.reconcile(); body = self.confirmation_body(); before = self.f.f.state(); update = self.store.update
        def fail(ident, **values):
            if 'recovery_confirmation' in values: raise RuntimeError('SYNTHETIC interrupted confirmation write')
            return update(ident, **values)
        with patch.object(self.store, 'update', side_effect=fail):
            with self.assertRaises(RuntimeError):
                self.engine.confirm_recovery(self.task_id, body['target']['task_revision'], body['target']['job_revision'],
                    self.run['attempt_id'], writer_stopped=True, checked_changes=True,
                    actor='authenticated_maintenance_session')
        self.assertEqual(self.f.f.state(), before)
        self.assertIsNone(getattr(self.store._worker, 'native_confirmation_commit', None))
        self.assertIsNone(getattr(self.store._worker, 'recovery_commit', None))

    def test_process_only_change_before_confirmation_cas_is_refused(self):
        from types import SimpleNamespace
        self.reconcile(); body = self.confirmation_body()
        confirm = self.store.confirm_native_recovery
        original_roots = list(self.engine.config['approved_roots'])
        for change in ('approved_roots', 'active'):
            with self.subTest(change=change):
                before = self.f.f.state(); seen = []
                def changed(*args, **kwargs):
                    stamp = (self.store.db.total_changes,
                        self.store.db.execute('PRAGMA data_version').fetchone()[0])
                    if change == 'approved_roots':
                        self.engine.config['approved_roots'] = []
                    else:
                        self.engine.active['SYNTHETIC-process-only'] = SimpleNamespace(project='SYNTHETIC-project')
                    self.assertEqual(stamp, (self.store.db.total_changes,
                        self.store.db.execute('PRAGMA data_version').fetchone()[0]))
                    seen.append(change)
                    return confirm(*args, **kwargs)
                try:
                    with patch.object(self.store, 'confirm_native_recovery', side_effect=changed):
                        code, result = self.confirm(body)
                    self.assertEqual(code, 400, result)
                    self.assertIn('workflow_native_confirmation_changed', str(result))
                    self.assertEqual(seen, [change])
                    self.assertEqual(self.f.f.state(), before)
                    self.assertTrue(self.store.get(self.task_id)['recovery_check_required'])
                    self.assertIsNone(self.store.get(self.task_id).get('recovery_confirmation'))
                    self.assertEqual(self.store.job_events(self.job_id, 'recovery_confirmed'), [])
                finally:
                    self.engine.config['approved_roots'] = list(original_roots)
                    self.engine.active.pop('SYNTHETIC-process-only', None)

    def test_replay_cannot_replace_the_confirmed_transition(self):
        self.reconcile(); body = self.confirmation_body(); self.assertEqual(self.confirm(body)[0], 200)
        before = self.f.f.state(); self.assertEqual(self.confirm(body)[0], 400)
        self.assertEqual(self.confirm()[0], 400); self.assertEqual(self.f.f.state(), before)
        self.assertEqual(len(self.store.job_events(self.job_id, 'recovery_confirmed')), 1)

    def test_concurrent_confirmation_creates_one_transition_only(self):
        from concurrent.futures import ThreadPoolExecutor
        self.reconcile(); body = self.confirmation_body(); before = self.store.get(self.task_id)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.confirm(body)[0], range(2)))
        self.assertEqual(sorted(results), [200, 400])
        self.assertEqual(self.store.get(self.task_id)['state_revision'], before['state_revision'] + 1)
        self.assertEqual(len(self.store.job_events(self.job_id, 'recovery_confirmed')), 1)

    def test_reconciliation_body_tamper_is_unresolved_before_confirmation(self):
        body = self.reconcile()
        record = self.store.native_reconciliation(self.task_id, body['target'])
        stored = self.store.get(record['id']); stored['note'] = 'SYNTHETIC changed without updating digest'
        self.store.db.execute('UPDATE objects SET body=? WHERE id=?', (json.dumps(stored), record['id']))
        self.store.db.commit()
        assessment = self.assess()
        self.assertFalse(assessment['eligible_for_reconciliation'])
        self.assertIn('native_reconciliation_record_unresolved', assessment['reasons'])
        before = self.f.f.state(); self.assertEqual(self.confirm()[0], 400)
        self.assertEqual(self.f.f.state(), before)

    def test_reconciliation_cas_change_has_an_actionable_error_code(self):
        from tests.test_p0_integration import NativeReconciliationTests
        body = NativeReconciliationTests.request_body(self); commit = self.store.commit_native_reconciliation
        def changed(*args, **kwargs):
            self.store.update(self.task_id, synthetic_reconciliation_race=True)
            return commit(*args, **kwargs)
        with patch.object(self.store, 'commit_native_reconciliation', side_effect=changed):
            code, result = self.f.request('/api/workflow/recovery/reconcile', body, maintenance=True)
        self.assertEqual(code, 400); self.assertEqual(result['code'], 'workflow_native_reconciliation_changed')


class NativeConfirmedAssessmentTests(unittest.TestCase):
    setUp = NativeConfirmationTests.setUp
    assess = NativeConfirmationTests.assess
    reconcile = NativeConfirmationTests.reconcile
    confirmation_body = NativeConfirmationTests.confirmation_body
    confirm = NativeConfirmationTests.confirm

    def confirmed(self):
        body = self.reconcile(); source = self.store.get(self.task_id)
        code, result = self.confirm(); self.assertEqual(code, 200, result)
        return body['target'], source

    def inspect(self):
        return self.engine.native_confirmed_recovery_assessment(self.task_id,
            actor='authenticated_maintenance_session')

    def test_exact_source_projection_is_fresh_read_only_and_grants_no_permission(self):
        target, source = self.confirmed(); before = self.f.f.state()
        basis = self.store.native_confirmation_basis(self.task_id)
        self.assertEqual(basis['source_task'], source); self.assertEqual(basis['source_target'], target)
        result = self.inspect()
        self.assertTrue(result['fresh_for_checkpoint_binding'], result)
        self.assertEqual(result['confirmed_target'], target)
        self.assertEqual(result['execution_permission'], 'none'); self.assertFalse(result['resume_allowed'])
        self.assertEqual(result['reasons'], [])
        self.assertEqual(self.f.f.state(), before)
        with self.assertRaises(ValueError): self.engine.retry(self.task_id, 'SYNTHETIC no binding permission')
        self.assertEqual(self.f.f.state(), before)

    def test_confirmed_native_checkpoint_ordinary_publication_is_refused(self):
        self.confirmed(); confirmed = self.store.get(self.task_id)
        draft = self.engine.collect_checkpoint(self.task_id, writer_stopped=True)
        record, job, task, paths, refs = self.engine._checkpoint_draft_inputs(draft['id'], True)
        before = self.f.f.state()
        with self.assertRaisesRegex(ValueError, 'native_checkpoint_transition_required'):
            self.store.commit_checkpoint(record, refs)
        self.assertEqual(self.f.f.state(), before)
        with patch.object(self.store._worker, 'task', confirmed, create=True):
            # The pre-existing attempt fence may reject the closed worker first.
            with self.assertRaises(ValueError):
                self.store.commit_checkpoint(record, refs)
        self.assertEqual(self.f.f.state(), before)
        with self.assertRaisesRegex(ValueError, 'native_checkpoint_transition_required'):
            self.engine.finalize_checkpoint(draft['id'], writer_stopped=True)
        self.assertEqual(self.f.f.state(), before)
        self.assertEqual(self.store.get(self.task_id), confirmed)
        self.assertEqual(self.store.get(draft['id'])['state'], 'writing')
        self.assertTrue(self.inspect()['fresh_for_checkpoint_binding'])
        with self.assertRaises(ValueError): self.engine.retry(self.task_id, 'SYNTHETIC no same-task binding')
        self.assertEqual(self.f.f.state(), before)

    def test_pre_guard_native_ready_checkpoint_remains_non_authoritative(self):
        self.confirmed(); draft = self.engine.collect_checkpoint(self.task_id, writer_stopped=True)
        # Reproduce the historical pre-guard publication path in this process only.
        with patch.object(self.store, 'native_checkpoint_requires_transition', return_value=False):
            result = self.engine.finalize_checkpoint(draft['id'], writer_stopped=True)
        self.assertTrue(result['store_ready']); self.assertTrue(result['handoff_synced'])
        before = self.f.f.state(); assessment = self.inspect()
        self.assertFalse(assessment['fresh_for_checkpoint_binding'])
        self.assertIsNone(assessment['confirmed_target'])
        with self.assertRaises(ValueError): self.engine.retry(self.task_id, 'SYNTHETIC no transition')
        self.assertEqual(self.f.f.state(), before)

    def test_changed_file_and_new_history_cannot_be_projected_away(self):
        self.confirmed(); path = self.f.f.workspace.project / 'app.py'; original = path.read_bytes()
        path.write_bytes(original + b'\n# SYNTHETIC after confirmation\n')
        before = self.f.f.state(); result = self.inspect()
        self.assertFalse(result['fresh_for_checkpoint_binding'])
        self.assertIn('native_confirmation_source_changed', result['reasons'])
        self.assertEqual(self.f.f.state(), before)
        path.write_bytes(original)
        self.store.event('SYNTHETIC_extra_history', 'SYNTHETIC unprojected event', self.task_id, self.job_id)
        before = self.f.f.state(); result = self.inspect()
        self.assertFalse(result['fresh_for_checkpoint_binding'])
        self.assertIn('native_confirmation_source_changed', result['reasons'])
        self.assertEqual(self.f.f.state(), before)

    def test_direct_task_and_proof_corruption_is_rejected(self):
        self.confirmed(); original = self.store.get(self.task_id)
        changes = [lambda t: t.update(state_revision=t['state_revision'] + 1),
            lambda t: t.update(synthetic_unreviewed_field=True),
            lambda t: t.update(recovery_check_required=True),
            lambda t: t['recovery_confirmation'].update(event_seq=True),
            lambda t: t['recovery_confirmation']['native_reconciliation']['source_target'].update(contract_version=True),
            lambda t: t['recovery_confirmation']['native_reconciliation']['source_fields'].pop('updated_at'),
            lambda t: t['recovery_confirmation']['native_reconciliation']['source_fields']['updated_at'].update(value=0),
            lambda t: t['recovery_confirmation']['native_reconciliation'].update(record_digest='0' * 64)]
        for change in changes:
            with self.subTest(change=changes.index(change)):
                corrupted = copy.deepcopy(original); change(corrupted)
                self.store.db.execute('UPDATE objects SET body=? WHERE id=?', (json.dumps(corrupted), self.task_id))
                self.store.db.commit()
                before = self.f.f.state()
                with self.assertRaises(ValueError): self.store.native_confirmation_basis(self.task_id)
                self.assertEqual(self.f.f.state(), before)
                self.store.db.execute('UPDATE objects SET body=? WHERE id=?', (json.dumps(original), self.task_id))
                self.store.db.commit()
        self.assertTrue(self.inspect()['fresh_for_checkpoint_binding'])

    def test_canonical_confirmation_event_missing_or_duplicated_is_rejected(self):
        self.confirmed(); marker = self.store.get(self.task_id)['recovery_confirmation']
        seq = marker['event_seq']; body = self.store.db.execute('SELECT body FROM events WHERE seq=?', (seq,)).fetchone()[0]
        self.store.db.execute('DELETE FROM events WHERE seq=?', (seq,)); self.store.db.commit()
        with self.assertRaisesRegex(ValueError, 'event_invalid'): self.store.native_confirmation_basis(self.task_id)
        self.store.db.execute('INSERT INTO events(seq,body) VALUES (?,?)', (seq, body)); self.store.db.commit()
        self.store.db.execute('INSERT INTO events(body) VALUES (?)', (body,)); self.store.db.commit()
        before = self.f.f.state()
        with self.assertRaisesRegex(ValueError, 'event_invalid'): self.store.native_confirmation_basis(self.task_id)
        result = self.inspect(); self.assertFalse(result['fresh_for_checkpoint_binding'])
        self.assertIsNone(result['confirmed_target']); self.assertEqual(self.f.f.state(), before)

    def test_matched_marker_and_event_cannot_forge_reconciliation_reference(self):
        self.confirmed(); original = self.store.get(self.task_id)
        seq = original['recovery_confirmation']['event_seq']
        event_body = self.store.db.execute('SELECT body FROM events WHERE seq=?', (seq,)).fetchone()[0]
        event = json.loads(event_body)
        for field, value in (('record_id', 'native-reconciliation-synthetic-forgery'),
                             ('record_digest', '0' * 64), ('record_event_seq', 1)):
            with self.subTest(field=field):
                task = copy.deepcopy(original); changed_event = copy.deepcopy(event)
                task['recovery_confirmation']['native_reconciliation'][field] = value
                changed_event['details']['native_reconciliation'][field] = value
                # Fault injection in the isolated fictional SQLite only.
                self.store.db.execute('UPDATE objects SET body=? WHERE id=?', (json.dumps(task), self.task_id))
                self.store.db.execute('UPDATE events SET body=? WHERE seq=?', (json.dumps(changed_event), seq))
                self.store.db.commit()
                try:
                    before = self.f.f.state()
                    with self.assertRaisesRegex(ValueError, 'native_confirmation_reconciliation_invalid'):
                        self.store.native_confirmation_basis(self.task_id)
                    result = self.inspect()
                    self.assertFalse(result['fresh_for_checkpoint_binding'])
                    self.assertIsNone(result['confirmed_target'])
                    self.assertEqual(result['execution_permission'], 'none')
                    self.assertEqual(self.f.f.state(), before)
                finally:
                    self.store.db.execute('UPDATE objects SET body=? WHERE id=?', (json.dumps(original), self.task_id))
                    self.store.db.execute('UPDATE events SET body=? WHERE seq=?', (event_body, seq))
                    self.store.db.commit()
        self.assertTrue(self.inspect()['fresh_for_checkpoint_binding'])

    def test_worker_actor_and_outer_transaction_are_refused(self):
        self.confirmed(); before = self.f.f.state()
        with self.assertRaises(ValueError): self.engine.native_confirmed_recovery_assessment(self.task_id)
        with patch.object(self.store._worker, 'task', self.store.get(self.task_id), create=True):
            with self.assertRaises(ValueError): self.inspect()
            with self.assertRaises(ValueError): self.store.native_confirmation_basis(self.task_id)
        with self.store.atomic():
            with self.assertRaisesRegex(ValueError, 'unlocked_transaction'): self.inspect()
        self.assertEqual(self.f.f.state(), before)

    def test_mid_observation_db_or_process_change_invalidates_freshness(self):
        self.confirmed(); original = self.engine._native_recovery_observation
        for mutation in ('db', 'roots'):
            roots = list(self.engine.config['approved_roots'])
            def changed(*args, **kwargs):
                result = original(*args, **kwargs)
                if mutation == 'db': self.store.event('SYNTHETIC_unrelated', 'SYNTHETIC stamp change')
                else: self.engine.config['approved_roots'] = []
                return result
            try:
                with patch.object(self.engine, '_native_recovery_observation', side_effect=changed):
                    result = self.inspect()
                self.assertFalse(result['fresh_for_checkpoint_binding'])
                self.assertIsNone(result['confirmed_target'])
                self.assertIn('native_confirmation_observation_changed', result['reasons'])
            finally:
                self.engine.config['approved_roots'] = roots


class NativeCheckpointTransitionPrimitiveTests(unittest.TestCase):
    setUp = NativeConfirmedAssessmentTests.setUp
    assess = NativeConfirmedAssessmentTests.assess
    reconcile = NativeConfirmedAssessmentTests.reconcile
    confirmation_body = NativeConfirmedAssessmentTests.confirmation_body
    confirm = NativeConfirmedAssessmentTests.confirm
    confirmed = NativeConfirmedAssessmentTests.confirmed

    def draft(self):
        self.confirmed()
        draft = self.engine.collect_checkpoint(self.task_id, writer_stopped=True)
        record, _, _, _, refs = self.engine._checkpoint_draft_inputs(draft['id'], True)
        return record, refs

    def stamp(self):
        return (self.store.db.total_changes, self.store.db.execute('PRAGMA data_version').fetchone()[0])

    def test_atomic_native_transition_binds_original_pointer_and_unique_events(self):
        record, refs = self.draft(); before = self.store.get(self.task_id)
        marker = before['recovery_confirmation']; old_pointer = {'present': 'resume_checkpoint_id' in before,
            'value': before.get('resume_checkpoint_id')}
        result = self.store.commit_native_checkpoint(record, refs, self.stamp(), lambda: None)
        ready, transition = result['checkpoint'], result['transition']
        self.assertEqual(transition['previous_pointer'], old_pointer)
        self.assertEqual(transition['confirmation_event_seq'], marker['event_seq'])
        self.assertEqual(transition['source_target'], marker['native_reconciliation']['source_target'])
        self.assertEqual(transition['reconciliation_id'], marker['native_reconciliation']['record_id'])
        self.assertEqual(transition['checkpoint_id'], ready['id'])
        self.assertEqual(transition['checkpoint_digest'], ready['manifest_digest'])
        self.assertEqual(transition['before_task_revision'], before['state_revision'])
        self.assertEqual(transition['after_task_revision'], before['state_revision'] + 1)
        self.assertLess(marker['event_seq'], ready['ready_binding']['event_seq'])
        self.assertLess(ready['ready_binding']['event_seq'], result['transition_event_seq'])
        body = {k:v for k,v in transition.items() if k not in ('record_digest', 'updated_at')}
        self.assertEqual(transition['record_digest'], hashlib.sha256(json.dumps(body, sort_keys=True,
            ensure_ascii=False, separators=(',', ':')).encode()).hexdigest())
        self.assertEqual(self.store.checkpoint_binding(ready['id'])[0], ready)
        self.assertEqual(result['execution_permission'], 'none'); self.assertFalse(result['resume_allowed'])
        self.assertFalse(self.engine.checkpoint_persistence(ready['id'])['handoff_synced'])
        before_replay = self.f.f.state()
        with self.assertRaises(ValueError):
            self.store.commit_native_checkpoint(record, refs, self.stamp(), lambda: None)
        self.assertEqual(self.f.f.state(), before_replay)
        with self.assertRaises(ValueError): self.engine.retry(self.task_id, 'SYNTHETIC no binding')

    def test_transition_failure_rolls_back_checkpoint_pointer_events_and_guards(self):
        record, refs = self.draft(); before = self.f.f.state(); emit = self.store.event
        def fail(kind, *args, **kwargs):
            if kind == 'native_checkpoint_transition_committed': raise RuntimeError('SYNTHETIC event failure')
            return emit(kind, *args, **kwargs)
        with patch.object(self.store, 'event', side_effect=fail):
            with self.assertRaises(RuntimeError):
                self.store.commit_native_checkpoint(record, refs, self.stamp(), lambda: None)
        self.assertEqual(self.f.f.state(), before)
        self.assertEqual(self.store.get(record['id'])['state'], 'writing')
        self.assertIsNone(getattr(self.store._worker, 'native_checkpoint_commit', None))
        self.assertIsNone(getattr(self.store._worker, 'native_transition_commit', None))
        self.assertEqual(self.store.job_events(self.job_id, 'native_checkpoint_transition_committed'), [])

    def test_concurrent_commits_create_only_one_transition(self):
        from concurrent.futures import ThreadPoolExecutor
        record, refs = self.draft(); stamp = self.stamp()
        def commit(_):
            try:
                self.store.commit_native_checkpoint(record, refs, stamp, lambda: None)
                return 'committed'
            except ValueError:
                return 'refused'
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sorted(executor.map(commit, range(2))), ['committed', 'refused'])
        events = self.store.job_events(self.job_id, 'native_checkpoint_transition_committed')
        self.assertEqual(len(events), 1)
        self.assertEqual(len(self.store.for_job('native_checkpoint_transition', self.job_id)), 1)

    def test_stale_stamp_process_change_and_worker_cannot_publish(self):
        record, refs = self.draft(); stale = self.stamp()
        self.store.event('SYNTHETIC_unrelated', 'SYNTHETIC database change')
        before = self.f.f.state()
        with self.assertRaises(ValueError): self.store.commit_native_checkpoint(record, refs, stale, lambda: None)
        with self.assertRaisesRegex(ValueError, 'SYNTHETIC_process_changed'):
            self.store.commit_native_checkpoint(record, refs, self.stamp(),
                lambda: (_ for _ in ()).throw(ValueError('SYNTHETIC_process_changed')))
        with self.assertRaisesRegex(ValueError, 'native_checkpoint_transition_changed'):
            self.store.commit_native_checkpoint(record, refs, self.stamp(),
                lambda: self.store.event('SYNTHETIC_callback_write', 'SYNTHETIC should roll back'))
        with patch.object(self.store._worker, 'task', self.store.get(self.task_id), create=True):
            with self.assertRaisesRegex(ValueError, 'worker_cannot_commit_native_checkpoint'):
                self.store.commit_native_checkpoint(record, refs, self.stamp(), lambda: None)
        self.assertEqual(self.f.f.state(), before)

    def test_post_confirmation_pending_or_invalid_history_blocks_transition(self):
        record, refs = self.draft()
        for event_type in ('checkpoint_handoff_pending', 'checkpoint_invalid'):
            with self.subTest(event_type=event_type):
                self.store.event(event_type, 'SYNTHETIC retained failure history', self.task_id, self.job_id,
                    details={'checkpoint_id': record['id']})
                before = self.f.f.state()
                with self.assertRaisesRegex(ValueError, 'native_checkpoint_history_changed'):
                    self.store.commit_native_checkpoint(record, refs, self.stamp(), lambda: None)
                self.assertEqual(self.f.f.state(), before)
                self.assertEqual(self.store.get(record['id'])['state'], 'writing')

    def test_reserved_kind_and_existing_transition_cannot_be_forged(self):
        record, refs = self.draft(); ident = 'native-checkpoint-transition-SYNTHETIC'
        before = self.f.f.state()
        with self.assertRaisesRegex(ValueError, 'native_checkpoint_transition_id_reserved'):
            self.store.put('other', {'id': ident})
        with patch.object(self.store._worker, 'native_reconciliation_commit', ident, create=True):
            with self.assertRaisesRegex(ValueError, 'native_checkpoint_transition_id_reserved'):
                self.store.put('native_effect_reconciliation', {'id': ident})
        with self.assertRaisesRegex(ValueError, 'native_checkpoint_transition_requires_server_commit'):
            self.store.put('native_checkpoint_transition', {'id': ident})
        with self.assertRaisesRegex(ValueError, 'native_checkpoint_transition_event_requires_server_commit'):
            self.store.event('native_checkpoint_transition_committed', 'SYNTHETIC forgery',
                self.task_id, self.job_id, details={'transition_id': ident})
        self.assertEqual(self.f.f.state(), before)
        transition = self.store.commit_native_checkpoint(record, refs, self.stamp(), lambda: None)['transition']
        before = self.f.f.state()
        with self.assertRaisesRegex(ValueError, 'native_checkpoint_transition_requires_server_commit'):
            self.store.update(transition['id'], synthetic_forgery=True)
        self.assertEqual(self.f.f.state(), before)


class NativeReconciliationStoreGuardTests(unittest.TestCase):
    setUp = NativeConfirmationTests.setUp
    assess = NativeConfirmationTests.assess

    def commit(self, target, summary, stamp=None):
        if stamp is None:
            with self.store.lock: stamp = self.engine._native_assessment_stamp()[:2]
        return self.store.commit_native_reconciliation(self.task_id, target, summary,
            'SYNTHETIC server-side component check', stamp, lambda: None)

    def test_false_target_summary_and_stale_database_stamp_are_refused_directly(self):
        assessment = self.assess(); target = assessment['target']; summary = assessment['summary']
        for changed, view in ((dict(target, task_id='fictional-other'), summary),
                              (dict(target, contract_version=True), summary),
                              (target, dict(summary, observation_stable=False))):
            before = self.f.f.state()
            with self.assertRaises(ValueError): self.commit(changed, view)
            self.assertEqual(self.f.f.state(), before)
        with self.store.lock: stamp = self.engine._native_assessment_stamp()[:2]
        self.store.put('fictional_metadata', {'id': 'fictional-concurrent-component-write'})
        before = self.f.f.state()
        with self.assertRaises(ValueError): self.commit(target, summary, stamp)
        self.assertEqual(self.f.f.state(), before)

    def test_running_task_or_running_attempt_cannot_be_certified_by_a_direct_writer(self):
        assessment = self.assess(); target = assessment['target']; summary = assessment['summary']
        original = self.store.get(self.task_id)
        self.store.update(self.task_id, status='running')
        target = dict(target, task_revision=self.store.get(self.task_id)['state_revision'])
        before = self.f.f.state()
        with self.assertRaises(ValueError): self.commit(target, summary)
        self.assertEqual(self.f.f.state(), before)
        self.store.update(self.task_id, status=original['status'],
            current_attempt=dict(original['current_attempt'], status='running'))
        target = dict(target, task_revision=self.store.get(self.task_id)['state_revision'])
        before = self.f.f.state()
        with self.assertRaises(ValueError): self.commit(target, summary)
        self.assertEqual(self.f.f.state(), before)

    def test_missing_baseline_cannot_be_certified_by_a_direct_writer(self):
        assessment = self.assess(); target = assessment['target']; summary = assessment['summary']
        record = self.store.native_scope_baseline(self.task_id, target['attempt_id'])
        # Fictional database fault injection only, never operational or coordination DBs.
        self.store.db.execute('DELETE FROM objects WHERE id=?', (record['id'],)); self.store.db.commit()
        before = self.f.f.state()
        with self.assertRaisesRegex(ValueError, 'baseline_required'): self.commit(target, summary)
        self.assertEqual(self.f.f.state(), before)


if __name__ == '__main__': unittest.main()
