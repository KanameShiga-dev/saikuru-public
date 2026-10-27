"""Fictional dedicated Worktree boundary; formal entry copied byte-for-byte."""
import copy
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from team_harness import execution_workspace, workspace_projection, workspace_identity
from team_adapters import ProviderError


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name); self.home = self.base/'Saikuru'; self.source = self.home/'Source'
        self.source.mkdir(parents=True); (self.base/'scripts').mkdir()
        real_home = Path(__file__).resolve().parents[3]
        shutil.copyfile(real_home/'new_worktree.py', self.home/'new_worktree.py')
        shutil.copyfile(real_home.parent/'scripts/new_workspace_worktree.py', self.base/'scripts/new_workspace_worktree.py')
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
        self.git(self.source, '-c', 'init.defaultBranch=main', 'init')
        self.git(self.source, 'config', 'user.name', 'Fictional Fixture')
        self.git(self.source, 'config', 'user.email', 'fixture@users.noreply.github.com')
        (self.source/'app.py').write_text('fictional = True\n', encoding='utf-8')
        self.git(self.source, 'add', '--', 'app.py'); self.git(self.source, 'commit', '-m', 'Fictional base')
        result = subprocess.run([sys.executable, str(self.home/'new_worktree.py'), '--name', 'dev',
            '--branch', 'codex/fictional', '--create-branch'], env=self.env, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, 'Formal fixture entry failed')
        self.project = self.home/'Worktrees/dev'

    def git(self, root, *args):
        result = subprocess.run(['git','-C',str(root),*args], env=self.env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, 'Fictional Git setup/query failed')
        return result.stdout

    def view(self): return execution_workspace(self.project, ['app.py'])

    def test_formal_entry_registered_mapping_is_readonly_and_not_creation_attestation(self):
        before = self.git(self.project, 'status', '--porcelain=v1')
        head = self.git(self.project, 'rev-parse', 'HEAD')
        result = self.view()
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['execution_root'], str(self.project.resolve()))
        self.assertEqual(result['integration_root'], str(self.source.resolve()))
        self.assertFalse(result['automatic_git_mutation'])
        self.assertEqual(result['creation_provenance'], 'not_attested_by_git_registration')
        self.assertEqual(self.git(self.project, 'status', '--porcelain=v1'), before)
        self.assertEqual(self.git(self.project, 'rev-parse', 'HEAD'), head)

    def test_dirty_source_and_allowed_dirty_worktree_are_preserved_without_commit(self):
        (self.source/'app.py').write_text('fictional = "user source edit"\n', encoding='utf-8')
        (self.project/'app.py').write_text('fictional = "developer edit"\n', encoding='utf-8')
        source_before = (self.source/'app.py').read_bytes(); project_before = (self.project/'app.py').read_bytes()
        head = self.git(self.project, 'rev-parse', 'HEAD')
        result = self.view()
        self.assertEqual(result['dirty_changes'][0]['path'], 'app.py')
        self.assertEqual(result['integration_dirty'], 'not_inspected')
        self.assertEqual((self.source/'app.py').read_bytes(), source_before)
        self.assertEqual((self.project/'app.py').read_bytes(), project_before)
        self.assertEqual(self.git(self.project, 'rev-parse', 'HEAD'), head)

    def test_outside_scope_dirty_is_blocked_without_removal(self):
        (self.project/'unrelated.py').write_text('fictional user work\n', encoding='utf-8')
        result = workspace_projection(self.project, ['app.py'])
        self.assertEqual(result['reason'], 'workspace_dirty_outside_approved_scope')
        self.assertTrue((self.project/'unrelated.py').exists())

    def test_primary_checkout_and_missing_formal_entry_are_not_execution_copies(self):
        self.assertEqual(workspace_projection(self.source, ['app.py'])['status'], 'blocked')
        (self.home/'new_worktree.py').unlink()
        self.assertEqual(workspace_projection(self.project, ['app.py'])['reason'], 'workspace_formal_entry_or_source_missing')

    def test_repository_pointer_mismatch_detached_and_locked_worktree_refused(self):
        unrelated = self.home/'Worktrees/bad'; unrelated.mkdir()
        (unrelated/'.git').write_text('gitdir: ../unrelated\n', encoding='utf-8')
        self.assertEqual(workspace_projection(unrelated, ['app.py'])['reason'], 'workspace_repository_mismatch')
        self.git(self.source, 'worktree', 'lock', str(self.project))
        self.assertEqual(workspace_projection(self.project, ['app.py'])['reason'], 'workspace_registration_unavailable')
        self.git(self.source, 'worktree', 'unlock', str(self.project))
        self.git(self.project, 'checkout', '--detach')
        self.assertEqual(workspace_projection(self.project, ['app.py'])['status'], 'blocked')

    def test_stable_identity_changes_on_git_movement_but_not_allowed_edits(self):
        before = self.view()
        (self.project/'app.py').write_text('fictional edit\n', encoding='utf-8')
        self.assertEqual(workspace_identity(before), workspace_identity(self.view()))
        self.git(self.project, 'add', '--', 'app.py'); self.git(self.project, 'commit', '-m', 'Fictional moved head')
        self.assertNotEqual(workspace_identity(before), workspace_identity(self.view()))

    def bind_engine(self):
        from tests.test_plan_revision import RevisionTests
        fixture = RevisionTests(); self.addCleanup(fixture.doCleanups); fixture.setUp()
        fixture.store.update(fixture.approval['id'], status='superseded')
        fixture.store.update(fixture.job_id, project=str(self.project))
        fixture.engine.config['approved_roots'].append(str(self.project))
        fixture.approval = fixture.engine._new_workflow_plan_approval(fixture.planner, fixture.payload)
        fixture.approve()
        task = next(t for t in fixture.store.for_job('task', fixture.job_id) if t.get('work_package_id'))
        return fixture, task

    def test_engine_binds_g3_and_rechecks_scope_head_and_approved_root(self):
        fixture, task = self.bind_engine()
        self.assertEqual(fixture.engine.workspace_view(fixture.job_id)['status'], 'ready')
        self.assertEqual(fixture.engine._execution_workspace(task)['approved_paths'], ['app.py'])
        self.git(self.project, 'checkout', '-b', 'codex/fictional-changed')
        self.assertEqual(fixture.engine.workspace_view(fixture.job_id)['reason'], 'workspace_identity_changed')
        with self.assertRaises(ProviderError): fixture.engine._execution_workspace(task)
        fixture.engine.config['approved_roots'] = []
        self.assertEqual(fixture.engine.workspace_view(fixture.job_id)['reason'], 'workspace_approved_root_required')

    def test_native_context_not_created_after_scope_changes(self):
        fixture, task = self.bind_engine()
        fixture.store.reserve_attempt(task['id'], task['state_revision'])
        task = fixture.store.get(task['id'])
        (self.project/'outside.py').write_text('fictional user edit\n', encoding='utf-8')
        with patch('team_engine.Context') as context:
            fixture.engine._run(task)
        context.assert_not_called()
        self.assertEqual(fixture.store.get(task['id'])['failure_code'], 'workspace_boundary')
        self.assertTrue((self.project/'outside.py').exists())

    def test_file_permission_scope_and_git_guard_remain_closed(self):
        fixture, task = self.bind_engine()
        ctx = SimpleNamespace(task=task)
        for path in ('outside.py', '.git/config', '../Source/app.py'):
            result = fixture.engine._tool_request_context(ctx, {'tool':'Write','input':{'file_path':path}})
            self.assertFalse(result['allow'])
        self.assertFalse((self.project/'outside.py').exists())

    def test_source_head_change_requires_reconfirmation(self):
        fixture, task = self.bind_engine()
        self.git(self.source, 'commit', '--allow-empty', '-m', 'Fictional integration change')
        self.assertEqual(fixture.engine.workspace_view(fixture.job_id)['reason'], 'workspace_identity_changed')

    def test_unbound_automatic_review_also_uses_current_workspace_binding(self):
        fixture, task = self.bind_engine()
        review = next(t for t in fixture.store.for_job('task', fixture.job_id) if t['role'] == 'reviewer')
        self.assertIsNone(review.get('work_package_id'))
        self.assertEqual(fixture.engine._execution_workspace(review)['status'], 'ready')
        self.git(self.project, 'checkout', '-b', 'codex/fictional-other')
        with self.assertRaises(ProviderError): fixture.engine._execution_workspace(review)

    def test_in_scope_file_still_reaches_existing_explicit_operation_gate(self):
        fixture, task = self.bind_engine()
        fixture.store.update(fixture.job_id, auto_execute=False)
        calls = []
        def approve(payload):
            calls.append(payload); return {'allow':False,'note':'Fictional explicit approval required'}
        ctx = SimpleNamespace(task=task, project=str(self.project), writable=True, context_files=[], approve=approve)
        before = (self.project/'app.py').read_bytes()
        with patch('team_auto_edit.approved_scope_edit', return_value=False):
            result = fixture.engine._tool_request_context(ctx, {'tool':'Write','input':{'file_path':'app.py'}})
        self.assertFalse(result['allow'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['operation'], 'Write')
        self.assertEqual((self.project/'app.py').read_bytes(), before)

    def test_formal_creation_does_not_copy_or_discard_uncommitted_source_changes(self):
        (self.source/'app.py').write_text('fictional = "user dirty source"\n', encoding='utf-8')
        before = (self.source/'app.py').read_bytes()
        result = subprocess.run([sys.executable, str(self.home/'new_worktree.py'), '--name', 'second',
            '--branch', 'codex/second', '--create-branch'], env=self.env, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0)
        self.assertEqual((self.source/'app.py').read_bytes(), before)
        self.assertEqual((self.home/'Worktrees/second/app.py').read_text(encoding='utf-8'), 'fictional = True\n')

    def test_submodule_cannot_hide_changes_outside_the_bound_repository(self):
        head = self.git(self.project, 'rev-parse', 'HEAD').decode('ascii').strip()
        self.git(self.project, 'update-index', '--add', '--cacheinfo', '160000,'+head+',vendor')
        self.assertEqual(workspace_projection(self.project, ['app.py'])['reason'], 'workspace_submodule_unsupported')


if __name__ == '__main__': unittest.main()
