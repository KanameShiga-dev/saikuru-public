"""Fixes from the 2026-10-09 review: command timeout, Copilot session cleanup, request commands, approval source."""
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import copilot_work_tools as work
from team_adapters import forget_copilot_session


class TimeoutTest(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows process tree')
    def test_timeout_returns_promptly_and_stops_the_child(self):
        folder = tempfile.mkdtemp()
        marker = 'timeout_probe_%d' % time.time_ns()
        began = time.time()
        value = work.run_command(folder, f'python -c "import time; time.sleep(60)  # {marker}"', 3)
        self.assertTrue(value['timed_out'])
        self.assertLess(time.time() - began, 25)
        found = subprocess.run(['powershell', '-NoProfile', '-Command',
                                f"(Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*{marker}*' -and $_.Name -eq 'python.exe' }}).ProcessId"],
                               capture_output=True, text=True).stdout.strip()
        self.assertEqual(found, '')


class SessionCleanupTest(unittest.TestCase):
    def test_only_this_runs_session_is_removed(self):
        with tempfile.TemporaryDirectory() as home:
            state = Path(home, 'session-state')
            mine, other = '11111111-2222-3333-4444-555555555555', '66666666-7777-8888-9999-000000000000'
            for sid in (mine, other):
                (state / sid).mkdir(parents=True)
                (state / sid / 'events.jsonl').write_text('{}', encoding='utf-8')
                (state / '.session-operation-locks').mkdir(exist_ok=True)
                (state / '.session-operation-locks' / (sid + '.lock')).write_text('', encoding='utf-8')
            with mock.patch.dict(os.environ, {'COPILOT_HOME': home}):
                self.assertTrue(forget_copilot_session(mine))
                self.assertFalse(forget_copilot_session('../session-state'))  # not a session id: nothing removed
            self.assertFalse((state / mine).exists())
            self.assertFalse((state / '.session-operation-locks' / (mine + '.lock')).exists())
            self.assertTrue((state / other / 'events.jsonl').exists())
            self.assertTrue((state / '.session-operation-locks' / (other + '.lock')).exists())


class RequestCommandsTest(unittest.TestCase):
    def test_rule_reaches_planner_builder_reviewer_and_the_template(self):
        from team_engine import REQUEST_COMMANDS_RULE
        source = Path(__file__).with_name('team_engine.py').read_text(encoding='utf-8')
        self.assertIn("(REQUEST_COMMANDS_RULE if not getattr(self, 'document_scope', False) and self.task['role'] in ('planner', 'builder', 'reviewer')",
                      source)
        self.assertIn('質問しない', REQUEST_COMMANDS_RULE)
        self.assertIn('Requested by the user', REQUEST_COMMANDS_RULE)
        harness = Path(__file__).with_name('team_harness.py').read_text(encoding='utf-8')
        self.assertIn('## Requested by the user', harness)
        self.assertIn('A command the user wrote in the request itself', harness)


class ReviewScopeAndEnvironmentTest(unittest.TestCase):
    """2026-10-09 comparison: review variance, impossible checks, command form, __pycache__ parity."""

    def test_rules_reach_every_role(self):
        from team_config import default_config
        from team_engine import Context, Engine, ENVIRONMENT_RULE, REVIEW_STATUS_RULE, REQUEST_COMMANDS_RULE
        from team_store import Store
        self.assertIn('「参考：」', REVIEW_STATUS_RULE)
        self.assertIn('この環境で実行できない検証を求めない', REVIEW_STATUS_RULE)
        self.assertIn('シンボリックリンクの作成はできない', ENVIRONMENT_RULE)
        self.assertIn('1つずつ、書かれた形のまま', REQUEST_COMMANDS_RULE)
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp, 'p')
            project.mkdir()
            store = Store(temp)
            config = default_config()
            config['approved_roots'] = [str(project)]
            engine = Engine(store, config, {'codex': ['fixture'], 'claude': ['fixture']}, 'http://127.0.0.1:1')
            try:
                job = engine.create_job('開発', '作る', str(project), True)
                for role in ('planner', 'builder', 'researcher', 'reviewer'):
                    with self.subTest(role=role):
                        ctx = Context(engine, engine.new_task(job, 'x', 'x', role))
                        from team_common_agents import load_agent
                        ctx.prepare_agent(load_agent(role, 'claude'))
                        self.assertIn(ENVIRONMENT_RULE.strip()[:40], ctx.agent_system_instructions)
            finally:
                engine.shutdown.set()
                engine.handoff.close()
                store.db.close()

    def test_claude_runs_without_bytecode_files(self):
        source = Path(__file__).with_name('team_adapters.py').read_text(encoding='utf-8')
        self.assertIn("env['PYTHONDONTWRITEBYTECODE'] = '1'", source)


class RoleProfilesTest(unittest.TestCase):
    def test_per_request_assignees(self):
        from team_config import default_config
        from team_engine import Engine
        from team_store import Store
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp, 'p')
            project.mkdir()
            store = Store(temp)
            config = default_config()
            config['approved_roots'] = [str(project)]
            config['provider_settings'] = {'enabled': ['claude', 'copilot'], 'copilot_monthly_credits': 1000}
            engine = Engine(store, config, {'claude': ['fixture'], 'copilot': ['fixture']}, 'http://127.0.0.1:1')
            try:
                luna = {'adapter': 'copilot', 'model': 'gpt-6-luna', 'effort': 'medium'}
                job = engine.create_job('開発', '作る', str(project), True, role_profiles={'builder': luna})
                self.assertEqual(store.get(job['id'])['role_overrides'], {'builder': luna})
                self.assertEqual(engine.new_task(store.get(job['id']), '実装', 'x', 'builder')['profile'], luna)
                for bad in ({'researcher': luna}, {'builder': dict(luna, model='gpt-6-astra')}, {'builder': dict(luna, effort='high')},
                            {'boss': luna}, {'builder': {'adapter': 'codex', 'model': 'x', 'effort': 'low'}}):
                    with self.subTest(bad=bad):
                        with self.assertRaises(ValueError):
                            engine.create_job('開発', '作る', str(project), True, role_profiles=bad)
            finally:
                engine.shutdown.set()
                engine.handoff.close()
                store.db.close()


class ApprovalSourceTest(unittest.TestCase):
    def test_copilot_requests_are_recorded_as_copilot(self):
        from team_config import default_config
        from team_engine import Context, Engine
        from team_store import Store
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp, 'p')
            project.mkdir()
            store = Store(temp)
            config = default_config()
            config['approved_roots'] = [str(project)]
            config['automatic_operations'] = False
            engine = Engine(store, config, {'codex': ['fixture'], 'claude': ['fixture']}, 'http://127.0.0.1:1')
            try:
                job = engine.create_job('開発', '作る', str(project), False)
                ctx = Context(engine, engine.new_task(job, '実装', '実装する', 'builder'))
                engine.active[ctx.task['id']] = ctx
                seen = []
                ctx.approve = lambda payload: seen.append(payload) or {'allow': False, 'note': ''}
                engine.tool_request(ctx.token, {'task_id': ctx.task['id'], 'tool': 'Bash', 'input': {'command': 'python x.py'},
                                                'source': 'copilot'})
                engine.tool_request(ctx.token, {'task_id': ctx.task['id'], 'tool': 'Bash', 'input': {'command': 'python x.py'}})
                self.assertEqual([p['source'] for p in seen], ['copilot', 'claude'])
            finally:
                engine.active.clear()
                engine.shutdown.set()
                engine.handoff.close()
                store.db.close()


if __name__ == '__main__':
    unittest.main()
