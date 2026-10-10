"""Native agent launch, permission boundaries, and review/retry lifecycle regressions."""
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from team_adapters import ADAPTERS, ClaudeAdapter, CodexAdapter, ProviderError
from team_common_agents import load_agent
from team_config import default_config
from team_engine import Context, Engine
from team_store import Store

DONE = dict(status='done', summary='fixture verified', checks=['fixture'], question='', questions=[], context_updates=[])
PLAN = dict(summary='fixture plan', tasks=[dict(title='fixture build', instruction='fixture only', role='builder')],
            security_review_required=True, questions=[], context_updates=[])


class NativeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        for provider in ('codex', 'claude'):
            directory = self.home / ('.' + provider) / 'agents'
            directory.mkdir(parents=True)
            for name in ('common-explorer', 'common-implementer', 'common-reviewer', 'common-security-reviewer'):
                writable = name == 'common-implementer'
                tools = 'Read, Glob, Grep, Edit, Write, Bash' if writable else 'Read, Glob, Grep'
                if provider == 'codex':
                    (directory / (name + '.toml')).write_text(
                        f'name="{name}"\ndescription="fixture agent"\nsandbox_mode="'+('workspace-write' if writable else 'read-only')+
                        f'"\ndeveloper_instructions="native role {name}"\n', encoding='utf-8')
                else:
                    (directory / (name + '.md')).write_text(
                        f'---\nname: {name}\ndescription: fixture agent\ntools: {tools}\n---\nnative role {name}\n', encoding='utf-8')
        self.home_patch = patch('pathlib.Path.home', return_value=self.home)
        self.home_patch.start()
        self.store = Store(self.root / 'data')
        self.config = default_config()
        self.config['approved_roots'] = [str(self.root)]
        self.engine = Engine(self.store, self.config, {'codex': ['fixture'], 'claude': ['fixture']}, 'http://127.0.0.1:1')
        self.saved = ADAPTERS.copy()
        self.auth_patch = patch('team_adapters.subprocess.run', return_value=SimpleNamespace(stdout='{"loggedIn":true}', returncode=0))
        self.auth_patch.start()

    def tearDown(self):
        self.engine.shutdown.set()
        for context in list(self.engine.active.values()):
            context.cancel.set()
        deadline = time.time() + 3
        while self.engine.active and time.time() < deadline:
            time.sleep(.01)
        ADAPTERS.update(self.saved)
        self.engine.handoff.close()
        self.store.db.close()
        self.home_patch.stop()
        self.auth_patch.stop()
        self.temp.cleanup()

    def context(self, provider='codex', role='researcher', agent_name=None):
        job = self.engine.create_job('fixture', 'fixture goal', str(self.root), True)
        task = self.engine.new_task(job, 'fixture', 'user task only', role, agent_name=agent_name)
        task = self.store.update(task['id'], profile=dict(adapter=provider, model='fixture-model', effort='medium'), attempt=1)
        ctx = Context(self.engine, task)
        ctx.prepare_agent(load_agent(role, provider, agent_name))
        return ctx

    def mock_process(self, messages):
        process = MagicMock()
        process.receive.side_effect = messages
        process.proc.stdin = io.StringIO()
        return process

    def test_read_only_roles_can_read_documents(self):
        for role in ('planner', 'reviewer'):
            ctx = self.context('claude', role)
            self.assertIn('mcp__project_read__read_document', ctx.agent_definition.tools, role)
            self.engine.active[ctx.task['id']] = ctx
            try:
                decision = self.engine.tool_request(ctx.token, {'task_id': ctx.task['id'], 'tool': 'mcp__project_read__read_document',
                                                                'input': {'path': 'a.pdf'}})
            finally:
                self.engine.active.pop(ctx.task['id'], None)
            self.assertTrue(decision['allow'], role)

    def test_agents_md_is_the_shared_rule_source_for_every_provider(self):
        from team_harness import CLAUDE_ROUTER
        (self.root / 'AGENTS.md').write_text('# Rules\n\nPROJECT-RULE-CANARY\n', encoding='utf-8')
        (self.root / 'CLAUDE.md').write_text(CLAUDE_ROUTER, encoding='utf-8')
        self.engine.commands = dict(self.engine.commands, copilot=['copilot-fixture'])
        for provider in ('codex', 'claude', 'copilot'):
            text = self.engine._instructions(self.context(provider))
            self.assertIn('PROJECT-RULE-CANARY', text, provider)
            self.assertNotIn('@AGENTS.md', text, provider)
        # A CLAUDE.md with Claude-only notes adds them on top of AGENTS.md (without the import line).
        (self.root / 'CLAUDE.md').write_text('@AGENTS.md\n\nCLAUDE-ONLY-NOTE\n', encoding='utf-8')
        text = self.engine._instructions(self.context('claude'))
        self.assertIn('PROJECT-RULE-CANARY', text)
        self.assertIn('CLAUDE-ONLY-NOTE', text)
        self.assertNotIn('CLAUDE-ONLY-NOTE', self.engine._instructions(self.context('codex')))

    def test_codex_native_instructions_and_read_only_session(self):
        ctx = self.context()
        process = self.mock_process([
            {'id': 1, 'result': {}}, {'id': 2, 'result': {'thread': {'id': 'native-thread'}}},
            *([{'id': 3, 'result': {'exitCode': 0, 'stdout': 'AGENT_TEAM_NATIVE_OK\r\n'}}] if os.name == 'nt' else []),
            {'id': 4 if os.name == 'nt' else 3, 'result': {}},
            {'method': 'item/completed', 'params': {'item': {'type': 'agentMessage', 'text': json.dumps(DONE)}}},
            {'method': 'turn/completed', 'params': {'turn': {'status': 'completed'}}}])
        with patch('team_adapters.Process', return_value=process) as factory:
            self.assertEqual(CodexAdapter().run(ctx, 'user task only', {}), DONE)
        requests = [call.args[0] for call in process.send.call_args_list if call.args[0].get('method') == 'thread/start']
        self.assertEqual(requests[0]['params']['sandbox'], 'read-only')
        self.assertIn('native role common-explorer', requests[0]['params']['developerInstructions'])
        self.assertEqual(ctx.agent_run['session_id'], 'native-thread')
        self.assertIn('multi_agent', factory.call_args.args[0])
        probe_requests = [c.args[0] for c in process.send.call_args_list if c.args[0].get('method') == 'command/exec']
        if os.name == 'nt':
            self.assertNotIn('outputBytesCap', probe_requests[0]['params'])
        process.close.assert_called_once()

    @unittest.skipUnless(os.name == 'nt', 'Windows sandbox startup check')
    def test_codex_sandbox_failure_stops_before_model_turn(self):
        ctx = self.context()
        process = self.mock_process([
            {'id': 1, 'result': {}}, {'id': 2, 'result': {'thread': {'id': 'native-thread'}}},
            {'id': 3, 'error': {'message': 'setup refresh failed'}}])
        with patch('team_adapters.Process', return_value=process), self.assertRaises(ProviderError) as failure:
            CodexAdapter().run(ctx, 'fixture only', {})
        self.assertEqual(failure.exception.code, 'codex_sandbox')
        self.assertFalse(any(c.args[0].get('method') == 'turn/start' for c in process.send.call_args_list))
        process.close.assert_called_once()

    def test_claude_auth_failure_stops_before_agent_inference(self):
        ctx = self.context('claude')
        with patch('team_adapters.subprocess.run', return_value=SimpleNamespace(stdout='{"loggedIn":false}', returncode=1)), patch('team_adapters.Process') as factory:
            with self.assertRaises(ProviderError) as failure:
                ClaudeAdapter().run(ctx, 'fixture only', {})
        self.assertEqual(failure.exception.code, 'claude_auth')
        factory.assert_not_called()

    def test_claude_selects_exact_named_agent_and_cleans_definition(self):
        ctx = self.context('claude', 'builder')
        import uuid
        session_id = str(uuid.UUID(ctx.agent_run['id']))
        process = self.mock_process([
            dict(type='system', subtype='init', session_id=session_id, agents=['common-implementer']),
            dict(type='result', subtype='success', structured_output=DONE)])
        with patch('team_adapters.Process', return_value=process) as factory:
            self.assertEqual(ClaudeAdapter().run(ctx, 'fixture only', {}), DONE)
        command = factory.call_args.args[0]
        self.assertEqual(command[command.index('--agent')+1], 'common-implementer')
        self.assertIn('Agent,Task', command)
        self.assertIn('StructuredOutput', command[command.index('--tools')+1].split(','))
        self.assertFalse(Path(command[command.index('--agents')+1]).exists())
        self.assertEqual(ctx.agent_run['session_id'], session_id)

    def test_claude_rejects_wrong_session(self):
        ctx = self.context('claude')
        process = self.mock_process([dict(type='system', subtype='init', session_id='wrong', agents=['common-explorer'])])
        with patch('team_adapters.Process', return_value=process), self.assertRaises(ProviderError):
            ClaudeAdapter().run(ctx, 'fixture only', {})
        self.assertIsNone(ctx.agent_run['session_id'])
        process.close.assert_called_once()

    def test_loader_rejects_privilege_escalation_missing_and_wrong_role(self):
        with self.assertRaises(ProviderError):
            load_agent('researcher', 'codex', 'common-implementer')
        path = self.home / '.codex/agents/common-reviewer.toml'
        path.write_text(path.read_text().replace('read-only', 'danger-full-access'))
        with self.assertRaises(ProviderError):
            load_agent('reviewer', 'codex')
        path.unlink()
        with self.assertRaises(ProviderError):
            load_agent('reviewer', 'codex')

    def test_security_review_is_separate_and_acceptance_waits(self):
        job = self.engine.create_job('fixture', 'fixture goal', str(self.root), True)
        planner = self.store.all('task')[-1]
        self.engine._finish(planner, PLAN)
        tasks = self.store.all('task')
        review, security = tasks[-2:]
        self.assertEqual(review['agent_name'], 'common-reviewer')
        self.assertEqual(security['agent_name'], 'common-security-reviewer')
        self.assertEqual(security['after'], review['id'])
        self.engine._finish(review, DONE)
        self.assertEqual(self.store.get(job['id'])['status'], 'running')
        self.assertFalse(any(a['kind'] == 'completion' for a in self.store.all('approval')))
        self.engine._finish(security, DONE)
        self.assertEqual(self.store.get(job['id'])['status'], 'awaiting_acceptance')

    def test_repair_replaces_security_review_and_requeues_both(self):
        job = self.engine.create_job('fixture', 'fixture goal', str(self.root), True)
        self.engine._finish(self.store.all('task')[-1], PLAN)
        review, security = self.store.all('task')[-2:]
        self.engine._finish(review, dict(DONE, status='needs_changes'))
        self.assertEqual(self.store.get(security['id'])['status'], 'cancelled')
        tasks = self.store.all('task')[-3:]
        self.assertEqual([t['agent_name'] for t in tasks], ['common-implementer', 'common-reviewer', 'common-security-reviewer'])
        self.assertTrue(all(t['repair'] == 1 for t in tasks))

    def test_retry_preserves_previous_session_and_starts_fresh(self):
        ctx = self.context()
        ctx.agent_started('first-session', 'fixture native')
        ctx.finish_agent('failed')
        self.store.update(ctx.task['id'], status='failed')
        self.engine.retry(ctx.task['id'], 'resume fixture')
        second = Context(self.engine, self.store.get(ctx.task['id']))
        second.prepare_agent(load_agent('researcher', 'codex'))
        self.assertNotEqual(ctx.agent_run['id'], second.agent_run['id'])
        history = self.store.get(ctx.task['id'])['agent_run_history']
        self.assertEqual(history[-1]['session_id'], 'first-session')
        self.assertEqual(history[-1]['status'], 'failed')

    def test_cancel_marks_session_cancelled(self):
        ctx = self.context()
        ctx.agent_started('cancel-session', 'fixture native')
        self.store.update(ctx.task['id'], status='cancelled')
        ctx.finish_agent('completed')
        self.assertEqual(self.store.get(ctx.task['id'])['agent_run']['status'], 'cancelled')


if __name__ == '__main__':
    unittest.main()
