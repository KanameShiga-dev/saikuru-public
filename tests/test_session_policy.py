"""Pure fictional checkpoint/session binding tests; never starts a Provider."""
import copy
import json
import unittest

import team_session_policy as policy
from team_context_pages import CONTEXT_FORMAT


class SessionBindingTests(unittest.TestCase):
    def setUp(self):
        self.job = {'id': 'fictional-job', 'workflow_contract_version': 1, 'status': 'running',
                    'state_revision': 8, 'requirement_revision': 1, 'requirements_approval_id': 'req-approved',
                    'plan_revision': 2, 'plan_approval_id': 'plan-approved'}
        self.source = {'id': 'source-task', 'job_id': self.job['id'], 'status': 'succeeded',
                       'resume_checkpoint_id': 'checkpoint-one',
                       'current_attempt': {'id': 'old-attempt', 'status': 'closed'}}
        self.target = {'id': 'target-task', 'job_id': self.job['id'], 'title': 'Fictional next task',
                       'instruction': 'Continue fictional scoped work', 'role': 'builder',
                       'status': 'queued', 'attempt': 0, 'state_revision': 2, 'after': self.source['id'],
                       'profile': {'adapter': 'codex', 'model': 'fictional-model', 'effort': 'medium'}}
        self.checkpoint = dict({key: self.job[key] for key in policy.VERSION_KEYS},
            id='checkpoint-one', job_id=self.job['id'], task_id=self.source['id'], state='ready',
            attempt_id='old-attempt', manifest_digest='a' * 64)
        self.assessment = {'status': 'candidate', 'assessment_only': True, 'resume_allowed': False,
                           'checkpoint_id': self.checkpoint['id'], 'candidate_task_ids': [self.target['id']],
                           'revalidate_task_ids': [], 'reasons': ['authorization_and_fresh_attempt_required']}

    def build(self):
        return policy.checkpoint_session_binding(self.job, self.source, self.target,
                                                  self.checkpoint, self.assessment)

    def start(self):
        binding = self.build()
        self.target.update(status='running', attempt=1, state_revision=3,
                           current_attempt={'id': 'new-attempt', 'status': 'running', 'ordinal': 1,
                            'approved_versions': {key: self.job[key] for key in policy.VERSION_KEYS}})
        return binding

    def manifest(self):
        manifest = {'format': CONTEXT_FORMAT, 'job_id': self.job['id'], 'task_id': self.target['id'],
                    'attempt_id': self.target['current_attempt']['id'], 'execution_permission': 'none',
                    'state_revisions': {'job': self.job['state_revision'], 'task': self.target['state_revision']}}
        return dict(manifest, manifest_digest=policy._digest(manifest))

    def validate(self, binding, manifest=None):
        return policy.validate_fresh_session(binding, self.job, self.target, manifest or self.manifest())

    def test_binding_is_deterministic_and_grants_no_execution_or_native_resume(self):
        before = copy.deepcopy((self.job, self.source, self.target, self.checkpoint, self.assessment))
        binding = self.build()
        self.assertEqual(binding, self.build())
        self.assertEqual(binding['execution_permission'], 'none')
        self.assertIs(binding['native_resume'], False)
        self.assertEqual(before, (self.job, self.source, self.target, self.checkpoint, self.assessment))
        self.assertNotIn('Continue fictional', json.dumps(binding))
        self.assertNotIn('fictional-model', json.dumps(binding))

    def test_fresh_attempt_keeps_selected_profile_and_approvals(self):
        binding = self.start()
        original = copy.deepcopy(self.target['profile'])
        result = self.validate(binding)
        self.assertEqual(result['attempt_id'], 'new-attempt')
        self.assertIs(result['native_resume'], False)
        self.assertEqual(self.target['profile'], original)

    def test_each_supported_provider_uses_same_domain_policy(self):
        for adapter in ('codex', 'claude', 'copilot'):
            with self.subTest(adapter=adapter):
                self.setUp()
                self.target['profile']['adapter'] = adapter
                self.assertFalse(self.validate(self.start())['native_resume'])

    def test_missing_or_unsafe_profile_cannot_silently_default(self):
        for value in ({}, {'adapter': 'codex', 'model': 'fictional-model', 'effort': 'high'},
                      {'adapter': 'unknown', 'model': 'fictional-model', 'effort': 'medium'},
                      {'adapter': 'codex', 'model': 'fictional\nmodel', 'effort': 'medium'}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.target['profile'] = value
                self.build()

    def test_reservation_is_not_ready_checkpoint(self):
        self.checkpoint['state'] = 'writing'
        with self.assertRaises(ValueError): self.build()

    def test_source_pointer_attempt_job_and_writers_must_match(self):
        for field, value in (('resume_checkpoint_id', 'other'), ('job_id', 'other'),
                              ('recovery_check_required', True), ('status', 'running')):
            with self.subTest(field=field):
                self.setUp(); self.source[field] = value
                with self.assertRaises(ValueError): self.build()

    def test_succeeded_target_is_never_reexecuted(self):
        self.target['status'] = 'succeeded'
        with self.assertRaises(ValueError): self.build()

    def test_changed_or_incomplete_assessment_is_not_authority(self):
        for key, value in (('resume_allowed', True), ('assessment_only', False),
                           ('reasons', []), ('candidate_task_ids', []),
                           ('revalidate_task_ids', ['target-task']), ('status', 'blocked')):
            with self.subTest(key=key):
                self.setUp(); self.assessment[key] = value
                with self.assertRaises(ValueError): self.build()

    def test_work_package_dispatch_stays_separately_gated(self):
        self.target['work_package_id'] = 'fictional-package'
        with self.assertRaises(ValueError): self.build()

    def test_changed_provider_model_or_effort_refuses_start(self):
        for field, value in (('adapter', 'claude'), ('model', 'other-model'), ('effort', 'low')):
            with self.subTest(field=field):
                self.setUp(); binding = self.start(); self.target['profile'][field] = value
                with self.assertRaises(ValueError): self.validate(binding)

    def test_changed_approvals_or_task_scope_refuse_start(self):
        for field, value in (('instruction', 'Different task'), ('role', 'reviewer'), ('after', None)):
            with self.subTest(field=field):
                self.setUp(); binding = self.start(); self.target[field] = value
                with self.assertRaises(ValueError): self.validate(binding)
        self.setUp(); binding = self.start(); self.job['plan_revision'] += 1
        with self.assertRaises(ValueError): self.validate(binding)

    def test_same_or_wrong_generation_cannot_resume(self):
        for change in ('same-source', 'same-target', 'old-ordinal', 'closed', 'bool'):
            with self.subTest(change=change):
                self.setUp(); binding = self.start()
                if change == 'same-source': self.target['current_attempt']['id'] = 'old-attempt'
                if change == 'same-target':
                    binding['target_attempt_before'] = 'new-attempt'
                    binding['binding_digest'] = policy._digest({k: v for k, v in binding.items() if k != 'binding_digest'})
                if change == 'old-ordinal': self.target['attempt'] = 0
                if change == 'closed': self.target['current_attempt']['status'] = 'closed'
                if change == 'bool': self.target['attempt'] = True
                with self.assertRaises(ValueError): self.validate(binding)

    def test_context_from_old_attempt_revision_or_other_task_is_rejected(self):
        binding = self.start()
        for field, value in (('attempt_id', 'old-attempt'), ('task_id', 'other-task'),
                              ('state_revisions', {'job': 7, 'task': 3}), ('execution_permission', 'execute')):
            with self.subTest(field=field):
                manifest = self.manifest(); manifest[field] = value
                manifest['manifest_digest'] = policy._digest({k: v for k, v in manifest.items() if k != 'manifest_digest'})
                with self.assertRaises(ValueError): self.validate(binding, manifest)

    def test_binding_and_manifest_tampering_detected(self):
        binding = self.start(); changed = dict(binding, checkpoint_id='another-checkpoint')
        with self.assertRaises(ValueError): self.validate(changed)
        manifest = self.manifest(); manifest['unknown'] = 'tampered'
        with self.assertRaises(ValueError): self.validate(binding, manifest)


class SessionPreparationTests(unittest.TestCase):
    def setUp(self):
        from tests.test_versioned_context import VersionedContextTests
        self.fixture = VersionedContextTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.engine, self.store = self.fixture.engine, self.fixture.store
        self.source = self.fixture.task
        self.target = self.engine.new_task(self.fixture.job, 'Fictional next',
            'Continue approved fictional work', 'builder', self.source['id'])
        self.target = self.store.update(self.target['id'], profile={
            'adapter': 'codex', 'model': 'fictional-model', 'effort': 'medium'})
        self.checkpoint = self.engine.collect_checkpoint(self.source['id'], writer_stopped=True)
        self.engine.finalize_checkpoint(self.checkpoint['id'], writer_stopped=True)

    def prepare(self):
        return self.engine.prepare_checkpoint_session(self.source['id'], self.target['id'], writer_stopped=True)

    def state(self):
        return (self.store.db.execute('SELECT * FROM objects ORDER BY id').fetchall(),
                self.store.db.execute('SELECT * FROM events ORDER BY seq').fetchall(),
                self.engine.handoff.db.execute('SELECT * FROM records ORDER BY seq').fetchall())

    def test_preparation_revalidates_real_checkpoint_and_does_not_dispatch(self):
        from unittest.mock import patch
        before = self.state()
        with patch.object(self.store, 'reserve_attempt', side_effect=AssertionError('must not dispatch')):
            binding = self.prepare()
        self.assertEqual(binding['checkpoint_id'], self.checkpoint['id'])
        self.assertEqual(binding['target_task_id'], self.target['id'])
        self.assertFalse(binding['native_resume'])
        self.assertEqual(before, self.state())

    def test_mutated_file_and_unknown_operation_refuse_preparation(self):
        self.fixture.app.write_text('fictional changed content\n', encoding='utf-8')
        with self.assertRaises(ValueError): self.prepare()

    def test_stop_confirmation_is_required(self):
        with self.assertRaises(ValueError):
            self.engine.prepare_checkpoint_session(self.source['id'], self.target['id'])

    def test_inputs_changing_during_assessment_are_rejected(self):
        from unittest.mock import patch
        original = self.engine.assess_checkpoint_resume
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            self.store.update(self.target['id'], instruction='Different fictional scope')
            return result
        with patch.object(self.engine, 'assess_checkpoint_resume', side_effect=changed):
            with self.assertRaises(ValueError): self.prepare()

    def request(self):
        return self.engine.request_checkpoint_session(self.source['id'], self.target['id'], writer_stopped=True)

    def running_context(self):
        from types import SimpleNamespace
        self.request()
        current = self.store.get(self.target['id'], 'task')
        task = self.store.reserve_attempt(current['id'], current['state_revision'])
        return SimpleNamespace(task=task, agent_run={})

    def test_request_is_idempotent_and_does_not_start_or_retry(self):
        first = self.request(); before = self.state()
        self.assertEqual(first, self.request())
        self.assertEqual(before, self.state())
        self.assertEqual(self.store.get(self.target['id'])['status'], 'queued')
        self.assertIsNone(self.store.get(self.target['id']).get('current_attempt'))

    def test_new_attempt_context_uses_current_domain_without_native_resume(self):
        ctx = self.running_context()
        self.engine.sync_handoff(self.fixture.job['id'])
        result = json.loads(self.engine._checkpoint_session_context(ctx))
        self.assertEqual(result['current_domain_context']['attempt_id'], ctx.task['current_attempt']['id'])
        self.assertFalse(result['checkpoint_reference']['native_resume'])
        self.assertEqual(ctx.agent_run['checkpoint_session']['checkpoint_id'], self.checkpoint['id'])
        self.assertNotIn('session_id', json.dumps(result))

    def test_changed_files_after_new_attempt_stop_before_provider(self):
        ctx = self.running_context()
        self.fixture.app.write_text('fictional later change\n', encoding='utf-8')
        with self.assertRaises(ValueError): self.engine._checkpoint_session_context(ctx)

    def test_changed_decision_or_profile_after_reservation_is_rejected(self):
        ctx = self.running_context()
        profile = dict(ctx.task['profile'], model='different-fictional-model')
        self.store.update(ctx.task['id'], profile=profile)
        with self.assertRaises(ValueError): self.engine._checkpoint_session_context(ctx)

    def test_request_does_not_reopen_failed_task(self):
        self.store.update(self.target['id'], status='failed')
        before = self.state()
        with self.assertRaises(ValueError): self.request()
        self.assertEqual(before, self.state())

    def test_failover_cannot_change_continuation_provider(self):
        self.request()
        task = self.store.get(self.target['id']); before = self.state()
        self.assertFalse(self.engine._failover(task, self.fixture.job, 'usage_limit', before_start=True))
        self.assertEqual(before, self.state())

    def test_scheduler_uses_normal_fresh_attempt_and_preserves_profile(self):
        binding = self.request()
        self.engine.paused = False
        workers = self.tick_without_workers()
        self.assertEqual(len(workers), 1)
        task = workers[0]['args'][0]
        self.assertEqual(task['status'], 'running')
        self.assertEqual(task['attempt'], 1)
        self.assertEqual(task['checkpoint_session_binding'], binding)
        self.assertEqual(task['profile'], self.target['profile'])
        self.assertEqual(self.store.get(self.source['id'])['status'], 'succeeded')

    def test_scheduler_changed_binding_never_starts_a_worker(self):
        self.request()
        self.store.update(self.target['id'], profile=dict(self.target['profile'], model='other-fictional'))
        self.engine.paused = False
        self.assertEqual(self.tick_without_workers(), [])
        self.assertEqual(self.store.get(self.target['id'])['status'], 'blocked')
        self.assertIsNone(self.store.get(self.target['id']).get('current_attempt'))

    def tick_without_workers(self):
        import threading
        from types import SimpleNamespace
        from unittest.mock import patch
        original = threading.Thread
        workers = []
        def thread(*args, **kwargs):
            if kwargs.get('target') == self.engine._run:
                workers.append(kwargs)
                return SimpleNamespace(start=lambda: None)
            # Git's subprocess pipe-reader threads must run normally on Windows.
            return original(*args, **kwargs)
        with patch.object(self.engine, '_restore_recovered_models'), patch('team_engine.threading.Thread', side_effect=thread):
            self.engine.tick()
        return workers

    def assert_waiting(self):
        self.assertEqual(self.store.get(self.target['id'])['status'], 'queued')
        self.assertEqual(self.store.get(self.fixture.job['id'])['status'], 'running')
        self.assertIsNone(self.store.get(self.target['id']).get('current_attempt'))

    def test_pending_decision_defers_without_inspection_or_state_change(self):
        from unittest.mock import patch
        self.request()
        self.store.put('approval', {'id': 'fictional-wait', 'job_id': self.fixture.job['id'],
            'task_id': self.source['id'], 'kind': 'question', 'status': 'pending'})
        before = self.state(); self.engine.paused = False
        with patch.object(self.engine, 'prepare_checkpoint_session') as prepare:
            self.assertEqual(self.tick_without_workers(), [])
        prepare.assert_not_called()
        self.assert_waiting(); self.assertEqual(before, self.state())

    def test_project_gate_defers_without_heavy_inspection(self):
        from unittest.mock import patch
        from team_handoff import canonical
        self.request(); self.engine.paused = False
        self.engine._checkpoint_projects.add(canonical(str(self.fixture.project)))
        with patch.object(self.engine, 'prepare_checkpoint_session') as prepare:
            self.assertEqual(self.tick_without_workers(), [])
        prepare.assert_not_called(); self.assert_waiting()

    def test_busy_writer_defers_without_heavy_inspection(self):
        from unittest.mock import patch
        self.request(); self.engine.paused = False
        other = self.engine.new_task(self.fixture.job, 'Fictional active', 'Fictional scope', 'builder')
        self.store.update(other['id'], status='running')
        with patch.object(self.engine, 'prepare_checkpoint_session') as prepare:
            self.assertEqual(self.tick_without_workers(), [])
        prepare.assert_not_called(); self.assert_waiting()

    def test_unfinished_predecessor_or_full_capacity_skip_inspection(self):
        from unittest.mock import patch
        self.request(); self.engine.paused = False
        self.store.update(self.source['id'], status='blocked')
        with patch.object(self.engine, 'prepare_checkpoint_session') as prepare:
            self.assertEqual(self.tick_without_workers(), [])
        prepare.assert_not_called(); self.assert_waiting()
        self.store.update(self.source['id'], status='succeeded')
        self.engine.config['max_parallel_projects'] = 0
        with patch.object(self.engine, 'prepare_checkpoint_session') as prepare:
            self.assertEqual(self.tick_without_workers(), [])
        prepare.assert_not_called(); self.assert_waiting()

    def test_temporary_preparation_exception_keeps_job_and_task_queued(self):
        from unittest.mock import patch
        self.request(); self.engine.paused = False
        with patch.object(self.engine, 'prepare_checkpoint_session', side_effect=policy.SessionPreparationDeferred('worker_busy')):
            self.assertEqual(self.tick_without_workers(), [])
        self.assert_waiting()

    def test_revision_race_defers_then_next_tick_can_dispatch(self):
        from unittest.mock import patch
        self.request(); self.engine.paused = False
        original = self.engine.prepare_checkpoint_session
        def advance(*args, **kwargs):
            result = original(*args, **kwargs)
            self.store.update(self.target['id'], summary='Fictional administrative update')
            return result
        with patch.object(self.engine, 'prepare_checkpoint_session', side_effect=advance):
            self.assertEqual(self.tick_without_workers(), [])
        self.assert_waiting()
        self.assertEqual(len(self.tick_without_workers()), 1)

    def test_question_arriving_after_preflight_is_waiting_not_failure(self):
        from unittest.mock import patch
        self.request(); self.engine.paused = False
        original = self.engine.prepare_checkpoint_session
        def question(*args, **kwargs):
            result = original(*args, **kwargs)
            self.store.put('approval', {'id': 'fictional-late-question', 'job_id': self.fixture.job['id'],
                'task_id': self.source['id'], 'kind': 'question', 'status': 'pending'})
            return result
        with patch.object(self.engine, 'prepare_checkpoint_session', side_effect=question):
            self.assertEqual(self.tick_without_workers(), [])
        self.assert_waiting()

    def test_changed_file_is_permanent_binding_failure_not_wait(self):
        self.request(); self.engine.paused = False
        self.fixture.app.write_text('fictional changed file\n', encoding='utf-8')
        self.assertEqual(self.tick_without_workers(), [])
        self.assertEqual(self.store.get(self.target['id'])['status'], 'blocked')
        self.assertEqual(self.store.get(self.fixture.job['id'])['status'], 'blocked')

    def test_waiting_queue_load_has_zero_heavy_calls_across_repeated_ticks(self):
        from unittest.mock import patch
        binding = self.request(); self.engine.paused = False
        for index in range(8):
            other = self.engine.new_task(self.fixture.job, 'Fictional waiting', 'Fictional scope', 'builder')
            self.store.update(other['id'], checkpoint_session_binding=binding)
        self.store.put('approval', {'id': 'fictional-queue-wait', 'job_id': self.fixture.job['id'],
            'task_id': self.source['id'], 'kind': 'question', 'status': 'pending'})
        before = self.state()
        with patch.object(self.engine, 'prepare_checkpoint_session') as prepare:
            for _ in range(5): self.assertEqual(self.tick_without_workers(), [])
        prepare.assert_not_called(); self.assert_waiting()
        self.assertEqual(before, self.state())

    def test_unknown_operation_after_reservation_blocks_context(self):
        ctx = self.running_context()
        self.store.event('operation_started', 'Fictional unknown operation', self.source['id'], self.fixture.job['id'])
        with self.assertRaises(ValueError): self.engine._checkpoint_session_context(ctx)

    def test_actual_engine_path_delivers_fresh_context_to_fake_adapter(self):
        from unittest.mock import patch
        from team_engine import ProviderError
        self.request()
        current = self.store.get(self.target['id'], 'task')
        task = self.store.reserve_attempt(current['id'], current['state_revision'])
        self.engine.commands['codex'] = ['fictional-never-executed-cli']
        seen = []
        class FakeAdapter:
            def run(adapter, ctx, prompt, schema):
                seen.append((prompt, ctx.task['profile'], ctx.task['current_attempt']['id']))
                raise ProviderError('Fictional adapter stops before external execution', 'fictional_stop')
        with patch.dict('team_engine.ADAPTERS', {'codex': FakeAdapter}):
            self.engine._run(task)
        self.assertEqual(len(seen), 1)
        self.assertIn('current_domain_context', seen[0][0])
        self.assertNotIn('__SAIKURU_FRESH_CHECKPOINT_CONTEXT__', seen[0][0])
        self.assertIn(task['current_attempt']['id'], seen[0][0])
        self.assertEqual(seen[0][1], self.target['profile'])
        self.assertEqual(self.store.get(task['id'])['current_attempt']['status'], 'closed')


if __name__ == '__main__':
    unittest.main()
