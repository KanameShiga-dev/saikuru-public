"""Session continuation across a restart into a fresh Provider session.

Fictional repository, Store/Handoff DBs, DLP policy and adapters only. A
"restart" is a new Store connection plus a new Engine on the same runtime
directory. Fake adapters record the prompt and stop before any external call.
Checks: requirements, approvals and unfinished work reach the new session; no
loss, no duplicate dispatch, no old native session reuse, no resend of
confidential text; unsupported providers are refused as unsupported.
"""
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from team_engine import Engine, ProviderError
from team_store import Store

OLD_NATIVE_SESSION = 'fictional-old-native-session-0001'
USER_ANSWER = 'Fictional answer: show prices in yen'
CONFIDENTIAL = 'fictional-internal-tag'  # registered by the fixture's fictional DLP policy


class SessionContinuityTests(unittest.TestCase):
    def setUp(self):
        from tests.test_versioned_context import VersionedContextTests
        self.base = VersionedContextTests()
        self.addCleanup(self.base.doCleanups)
        self.base.setUp()
        self.engine, self.store = self.base.engine, self.base.store
        self.job_id = self.base.job['id']
        self.source = self.store.get(self.base.task['id'])

    # Fixture building ---------------------------------------------------------

    def prepare(self, *, adapter='codex', source_summary=None):
        """Decisions, history and later work exist BEFORE the Checkpoint."""
        self.store.put('approval', {'id': 'fictional-answered-question', 'job_id': self.job_id,
            'task_id': self.source['id'], 'kind': 'question', 'status': 'approved',
            'payload': {'questions': [{'id': 'q1', 'text': 'Fictional currency?'}]},
            'answers': {'q1': USER_ANSWER}, 'note': ''})
        run = dict(self.source.get('agent_run') or {}, id='fictional-old-run', provider='codex',
                   session_id=OLD_NATIVE_SESSION, status='succeeded')
        changes = {'agent_run': run}
        if source_summary is not None:
            changes['summary'] = source_summary
        self.source = self.store.update(self.source['id'], **changes)
        job = self.store.get(self.job_id)
        self.target = self.engine.new_task(job, 'Fictional next', 'Continue approved fictional work',
                                           'builder', self.source['id'])
        self.target = self.store.update(self.target['id'], profile={
            'adapter': adapter, 'model': 'fictional-model', 'effort': 'medium'})
        self.later = self.engine.new_task(job, 'Fictional later', 'Fictional unfinished follow-up',
                                          'builder', self.target['id'])
        checkpoint = self.engine.collect_checkpoint(self.source['id'], writer_stopped=True)
        self.engine.finalize_checkpoint(checkpoint['id'], writer_stopped=True)
        self.checkpoint_id = checkpoint['id']

    def restart(self):
        store = Store(self.store.directory)
        self.addCleanup(store.db.close)
        engine = Engine(store, self.engine.config, {}, 'http://127.0.0.1:0')
        engine.paused = True
        self.addCleanup(engine.handoff.close)
        engine.project_blocked = self.engine.project_blocked
        return store, engine

    def dispatch(self, engine):
        """One scheduler tick; capture workers instead of starting them."""
        original = threading.Thread
        workers = []
        def thread(*args, **kwargs):
            if kwargs.get('target') == engine._run:
                workers.append(kwargs['args'][0])
                return SimpleNamespace(start=lambda: None)
            return original(*args, **kwargs)  # Git pipe readers must still run
        engine.paused = False
        with patch.object(engine, '_restore_recovered_models'), \
                patch('team_engine.threading.Thread', side_effect=thread):
            engine.tick()
        engine.paused = True
        return workers

    def run_with_fake(self, engine, task, adapter='codex'):
        seen = []
        class FakeAdapter:
            def run(fake, ctx, prompt, schema):
                seen.append({'prompt': prompt, 'profile': dict(ctx.task['profile']),
                             'attempt_id': ctx.task['current_attempt']['id'],
                             'agent_run': dict(ctx.agent_run or {})})
                raise ProviderError('Fictional adapter stops before external execution', 'fictional_stop')
        engine.commands[adapter] = ['fictional-never-executed-cli']
        with patch.dict('team_engine.ADAPTERS', {adapter: FakeAdapter}):
            engine._run(task)
        return seen

    def continue_after_restart(self, adapter='codex'):
        self.engine.request_checkpoint_session(self.source['id'], self.target['id'], writer_stopped=True)
        store, engine = self.restart()
        workers = self.dispatch(engine)
        self.assertEqual([w['id'] for w in workers], [self.target['id']])
        return store, engine, workers[0], self.run_with_fake(engine, workers[0], adapter)

    def attempt_starts(self, store, task_id):
        return [e for e in store.job_events(self.job_id, 'attempt_started') if e['task_id'] == task_id]

    # Retention ----------------------------------------------------------------

    def test_requirements_decisions_and_unfinished_work_reach_the_new_session(self):
        self.prepare()
        store, engine, task, seen = self.continue_after_restart()
        self.assertEqual(len(seen), 1)
        prompt = seen[0]['prompt']
        job = store.get(self.job_id)
        self.assertIn(job['goal'], prompt)                       # requirement
        self.assertIn(job['plan_approval_id'], prompt)           # approved plan
        self.assertIn(USER_ANSWER, prompt)                       # the user's own answer
        self.assertIn('Continue approved fictional work', prompt)  # current instruction
        self.assertIn(self.later['id'], prompt)                  # unfinished follow-up stays listed
        self.assertIn(self.checkpoint_id, prompt)                # provenance
        self.assertEqual(seen[0]['profile'], self.target['profile'])
        self.assertEqual(store.get(self.later['id'])['status'], 'queued')

    # No duplicate work -----------------------------------------------------------

    def test_continuation_runs_once_and_never_reruns_or_resumes_the_old_session(self):
        self.prepare()
        store, engine, task, seen = self.continue_after_restart()
        self.assertEqual(self.dispatch(engine), [])
        other_store, other_engine = self.restart()
        self.assertEqual(self.dispatch(other_engine), [])
        self.assertEqual(len(self.attempt_starts(store, self.target['id'])), 1)
        self.assertEqual(len(self.attempt_starts(store, self.source['id'])), 1)  # original run only
        self.assertEqual(store.get(self.source['id'])['status'], 'succeeded')
        self.assertNotIn(OLD_NATIVE_SESSION, seen[0]['prompt'])
        self.assertNotEqual(seen[0]['agent_run'].get('session_id'), OLD_NATIVE_SESSION)
        self.assertNotEqual(seen[0]['attempt_id'], self.source['current_attempt']['id'])
        self.assertFalse(seen[0]['agent_run']['checkpoint_session']['native_resume'])

    # No resend of confidential text ------------------------------------------------

    def test_confidential_text_in_saved_report_stops_before_any_provider_call(self):
        self.prepare(source_summary='Fictional report mentioning ' + CONFIDENTIAL)
        self.engine.request_checkpoint_session(self.source['id'], self.target['id'], writer_stopped=True)
        store, engine = self.restart()
        workers = self.dispatch(engine)
        self.assertEqual(len(workers), 1)
        self.assertEqual(self.run_with_fake(engine, workers[0]), [])   # adapter never called
        task = store.get(self.target['id'])
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(task['summary'], '文脈の安全な外部提示を確認できません。')  # DLP stop, fixed text
        self.assertNotIn(CONFIDENTIAL, task['summary'])
        self.assertEqual(task['current_attempt']['status'], 'closed')

    def test_confidential_fact_added_after_checkpoint_is_not_sent(self):
        self.prepare()
        self.engine.request_checkpoint_session(self.source['id'], self.target['id'], writer_stopped=True)
        self.engine.handoff.fact(str(self.base.project), self.job_id, 'fictional-key', CONFIDENTIAL,
                                 'Fictional evidence', 'model_reported', 'fictional-later-source')
        store, engine = self.restart()
        self.assertEqual(self.dispatch(engine), [])   # changed authority stops before any attempt
        task = store.get(self.target['id'])
        self.assertEqual((task['status'], task['failure_code']), ('blocked', 'checkpoint_session_changed'))
        self.assertIsNone(task.get('current_attempt'))
        self.assertFalse(self.attempt_starts(store, self.target['id']))

    # Supported and unsupported providers ------------------------------------------

    def test_each_supported_provider_gets_a_fresh_session_with_the_same_context(self):
        for adapter in ('codex', 'claude', 'copilot'):
            with self.subTest(adapter=adapter):
                self.setUp()
                self.prepare(adapter=adapter)
                store, engine, task, seen = self.continue_after_restart(adapter)
                self.assertEqual(len(seen), 1)
                self.assertIn('current_domain_context', seen[0]['prompt'])
                self.assertIn(USER_ANSWER, seen[0]['prompt'])
                self.assertNotIn(OLD_NATIVE_SESSION, seen[0]['prompt'])
                self.assertEqual(seen[0]['profile']['adapter'], adapter)

    def test_unsupported_provider_is_refused_as_unsupported_without_state_change(self):
        for adapter in ('gemini', 'fictional-unknown'):
            with self.subTest(adapter=adapter):
                self.setUp()
                self.prepare(adapter=adapter)
                before = self.store.get(self.target['id'])
                with self.assertRaisesRegex(ValueError, 'session_profile_invalid'):
                    self.engine.request_checkpoint_session(self.source['id'], self.target['id'], writer_stopped=True)
                self.assertEqual(self.store.get(self.target['id']), before)
                self.assertFalse(self.store.job_events(self.job_id, 'checkpoint_session_requested'))


if __name__ == '__main__':
    unittest.main()
