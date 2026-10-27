"""Fictional repositories and databases only; no Provider or operational files."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from team_checkpoint import (collect_project, digest, load_collection, scope_paths,
                             verify_collection, checkpoint_digest, MAX_FILE_BYTES)
from team_config import default_config
from team_engine import Engine
from team_p0_contract import initial, initial_workflow
from team_store import Store


class ProjectFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='fictional-checkpoint-')
        self.root = Path(temp.name).resolve()
        assert self.root.parent == Path(tempfile.gettempdir()).resolve()
        assert self.root.name.startswith('fictional-checkpoint-')
        self.addCleanup(temp.cleanup)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.cache = self.root / 'runtime' / 'checkpoints'
        self.app = self.project / 'app.py'
        self.app.write_text('value = 1\n', encoding='utf-8', newline='\n')
        (self.project / 'README.md').write_text('Fictional lookup only.\n', encoding='utf-8', newline='\n')
        (self.project / '.gitignore').write_text('ignored.py\n', encoding='utf-8', newline='\n')
        self.git('init', '-q')
        self.git('add', '--', 'app.py', 'README.md', '.gitignore')
        self.git('-c', 'user.name=Fictional Fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '-qm', 'Fictional baseline')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.project), *args], stderr=subprocess.DEVNULL)


class CollectionTests(ProjectFixture):
    def test_staged_and_unstaged_contents_preserved_without_git_mutation(self):
        self.app.write_text('value = 2\n', encoding='utf-8', newline='\n')
        self.git('add', '--', 'app.py')
        self.app.write_text('value = 3\n', encoding='utf-8', newline='\n')
        (self.project / 'new.py').write_text('fictional = True\n', encoding='utf-8', newline='\n')
        paths = ['app.py', 'new.py']
        head, staged = self.git('rev-parse', 'HEAD'), self.git('diff', '--cached', '--binary')
        manifest = collect_project(self.project, paths, self.cache)
        self.assertTrue(verify_collection(self.project, self.cache, manifest, paths))
        target = self.cache / manifest['collection_id']
        states = manifest['git_snapshot']['files']['app.py']
        self.assertEqual((target / states['index']['blob']).read_bytes(), b'value = 2\n')
        self.assertEqual((target / states['worktree']['blob']).read_bytes(), b'value = 3\n')
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)
        self.assertEqual(self.git('diff', '--cached', '--binary'), staged)
        self.assertEqual(self.app.read_bytes(), b'value = 3\n')
        body = (target / 'manifest.json').read_text(encoding='utf-8')
        self.assertNotIn('value = 3', body)
        self.assertNotIn(str(self.project), body)
        self.assertEqual(load_collection(self.project, self.cache, manifest['collection_id']), manifest)

    def test_scope_rejects_unsafe_names_and_case_aliases(self):
        for name in ('../outside.py', '/outside.py', 'C:/outside.py', 'dir\\app.py', './app.py',
                     'dir//app.py', '.env', 'data/records.json', 'transcripts/chat.txt', 'file.png',
                     'name\n.py', 'credentials.py'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                scope_paths([name])
        with self.assertRaises(ValueError):
            scope_paths(['app.py', 'APP.py'])

    def test_secret_or_conversation_content_is_not_copied(self):
        for text in ('password = "fictional-only"\n', 'user: fictional\nassistant: fictional\n',
                     '[{"role":"user","content":"fictional"}]',
                     '{"messages":[{"speaker":"human","text":"fictional"}]}'):
            self.app.write_text(text, encoding='utf-8', newline='\n')
            with self.subTest(kind=text.splitlines()[0][:8]), self.assertRaises(ValueError):
                collect_project(self.project, ['app.py'], self.cache)
            self.assertFalse(self.cache.exists())

    def test_staged_secret_is_rejected_even_when_worktree_is_clean_text(self):
        self.app.write_text('password = "fictional-only"\n', encoding='utf-8', newline='\n')
        self.git('add', '--', 'app.py')
        self.app.write_text('value = 5\n', encoding='utf-8', newline='\n')
        with self.assertRaises(ValueError):
            collect_project(self.project, ['app.py'], self.cache)
        self.assertFalse(self.cache.exists())

    def test_unscoped_or_ignored_file_does_not_expand_scope(self):
        readme = self.project / 'README.md'
        readme.write_text('Fictional unscoped change\n', encoding='utf-8', newline='\n')
        with self.assertRaises(ValueError):
            collect_project(self.project, ['app.py'], self.cache)
        readme.write_text('Fictional lookup only.\n', encoding='utf-8', newline='\n')
        (self.project / 'ignored.py').write_text('fictional = True\n', encoding='utf-8', newline='\n')
        with self.assertRaises(ValueError):
            collect_project(self.project, ['app.py', 'ignored.py'], self.cache)
        self.assertFalse(self.cache.exists())

    def test_cache_must_be_outside_project(self):
        with self.assertRaises(ValueError):
            collect_project(self.project, ['app.py'], self.project / 'cache')
        self.assertFalse((self.project / 'cache').exists())

    def test_blob_tampering_and_current_file_change_invalidate_references(self):
        manifest = collect_project(self.project, ['app.py'], self.cache)
        self.app.write_text('value = 99\n', encoding='utf-8', newline='\n')
        with self.assertRaises(ValueError):
            verify_collection(self.project, self.cache, manifest, ['app.py'])
        self.assertEqual(self.app.read_text(encoding='utf-8'), 'value = 99\n')
        self.app.write_text('value = 1\n', encoding='utf-8', newline='\n')
        reference = manifest['git_snapshot']['files']['app.py']['worktree']
        (self.cache / manifest['collection_id'] / reference['blob']).write_bytes(b'fictional tamper\n')
        with self.assertRaises(ValueError):
            verify_collection(self.project, self.cache, manifest, ['app.py'])

    def test_self_rehashed_manifest_cannot_escape_scope_or_blob_directory(self):
        original = collect_project(self.project, ['app.py'], self.cache)
        for kind in ('scope', 'blob', 'version', 'unknown'):
            manifest = copy.deepcopy(original)
            if kind == 'scope':
                manifest['scope'] = ['README.md']
            elif kind == 'blob':
                manifest['git_snapshot']['files']['app.py']['worktree']['blob'] = '../../outside.py'
            elif kind == 'version':
                manifest['schema_version'] = True
            else:
                manifest['extra_body'] = 'fictional unrecognized body'
            manifest['manifest_digest'] = digest({k: v for k, v in manifest.items() if k != 'manifest_digest'})
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                verify_collection(self.project, self.cache, manifest, ['app.py'])
        path = self.cache / original['collection_id'] / 'manifest.json'
        path.write_text('{"schema_version":1,' + json.dumps(original)[1:], encoding='utf-8', newline='\n')
        with self.assertRaises(ValueError):
            load_collection(self.project, self.cache, original['collection_id'])

    def test_rename_and_deleted_file_tombstones(self):
        self.git('mv', '--', 'app.py', 'renamed.py')
        manifest = collect_project(self.project, ['app.py', 'renamed.py'], self.cache)
        self.assertIsNone(manifest['git_snapshot']['files']['app.py']['worktree'])
        self.assertEqual(manifest['git_snapshot']['dirty'][0]['from'], 'app.py')
        self.assertTrue(verify_collection(self.project, self.cache, manifest, ['app.py', 'renamed.py']))

    def test_binary_oversized_and_gitlink_are_rejected(self):
        for data in (b'\0fictional binary', b'x' * (MAX_FILE_BYTES + 1)):
            self.app.write_bytes(data)
            with self.assertRaises(ValueError):
                collect_project(self.project, ['app.py'], self.cache)
        self.app.write_text('value = 1\n', encoding='utf-8', newline='\n')
        oid = self.git('rev-parse', 'HEAD').decode().strip()
        self.git('update-index', '--add', '--cacheinfo', '160000,' + oid + ',vendor')
        with self.assertRaises(ValueError):
            collect_project(self.project, ['app.py'], self.cache)

    def test_git_clean_filter_is_never_executed(self):
        (self.project / '.git' / 'info' / 'attributes').write_text('app.py filter=fixture\n',
                                                               encoding='utf-8', newline='\n')
        command = 'python -c "from pathlib import Path; Path(\'FILTER_EXECUTED\').write_text(\'fictional\')"'
        self.git('config', 'filter.fixture.clean', command)
        self.git('config', 'filter.fixture.required', 'true')
        self.app.write_text('value = 2\n', encoding='utf-8', newline='\n')
        manifest = collect_project(self.project, ['app.py'], self.cache)
        self.assertTrue(verify_collection(self.project, self.cache, manifest, ['app.py']))
        self.assertFalse((self.project / 'FILTER_EXECUTED').exists())

    def test_symlink_never_reads_outside_project(self):
        outside = self.root / 'outside.py'
        outside.write_text('value = 7\n', encoding='utf-8', newline='\n')
        assert self.app.resolve().is_relative_to(self.project)
        self.app.unlink()
        try:
            self.app.symlink_to(outside)
        except OSError:
            self.skipTest('Symlink privilege unavailable')
        with self.assertRaises(ValueError):
            collect_project(self.project, ['app.py'], self.cache)
        self.assertFalse(self.cache.exists())

    def test_windows_junction_parent_is_rejected_before_collection(self):
        try:
            import _winapi
        except ImportError:
            self.skipTest('Windows junction API unavailable')
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'app.py').write_text('value = 7\n', encoding='utf-8', newline='\n')
        link = self.project / 'linked'
        assert outside.resolve().is_relative_to(self.root)
        assert link.absolute().is_relative_to(self.root)
        _winapi.CreateJunction(str(outside), str(link))
        self.assertTrue(link.is_junction())
        with self.assertRaises(ValueError):
            collect_project(self.project, ['linked/app.py'], self.cache)
        self.assertFalse(self.cache.exists())


class EngineDraftTests(ProjectFixture):
    def setUp(self):
        super().setUp()
        self.store = Store(self.root / 'runtime')
        self.addCleanup(self.store.db.close)
        config = default_config()
        config['approved_roots'] = [str(self.project)]
        self.engine = Engine(self.store, config, {}, 'http://127.0.0.1:0')
        self.engine.paused = True
        self.addCleanup(self.engine.handoff.close)
        job = self.store.put('job', initial_workflow(dict(id='fictional-job', title='Fictional lookup',
            goal='Fictional lookup', goal_contract=initial('Fictional lookup'), project=str(self.project),
            status='awaiting_approval', request_origin='new', route='full', document_source=None,
            p0_contract_version=1, auto_execute=False, created_at=1), enabled=True))
        self.task = self.engine.new_task(job, 'Fictional build', 'Fictional only', 'builder')
        approval = self.engine.new_approval(self.task, 'plan', {'summary': 'Fictional plan', 'checkpoint_paths': ['app.py']})
        self.approval = self.store.update(approval['id'], status='approved')
        self.job = self.store.update(job['id'], status='running', plan_revision=1, requirement_revision=1,
                                    plan_approval_id=approval['id'], requirements_approval_id=approval['id'])
        self.task = self.store.reserve_attempt(self.task['id'], self.task['state_revision'])
        self.store.close_attempt(self.task['id'], 'closed', 'fictional-completion')
        self.task = self.store.update(self.task['id'], status='succeeded')

    def collect(self):
        return self.engine.collect_checkpoint(self.task['id'], writer_stopped=True)

    def test_draft_contains_references_not_source_or_decision_text(self):
        draft = self.collect()
        self.assertEqual(draft['state'], 'writing')
        result = self.engine.verify_checkpoint_draft(draft['id'], writer_stopped=True)
        self.assertTrue(result['valid'])
        self.assertFalse(result['ready'])
        self.assertFalse(result['resume_allowed'])
        self.assertNotIn('resume_checkpoint_id', self.store.get(self.task['id']))
        text = json.dumps(draft, ensure_ascii=False)
        self.assertNotIn('value = 1', text)
        self.assertNotIn('Fictional plan', text)
        self.assertNotIn(str(self.project), text)
        with self.assertRaises(ValueError):
            self.store.update(draft['id'], state='ready')

    def test_writer_stop_and_restart_reconciliation_are_required(self):
        with self.assertRaises(ValueError):
            self.engine.collect_checkpoint(self.task['id'])
        self.store.update(self.task['id'], recovery_check_required=True)
        with self.assertRaises(ValueError):
            self.collect()
        self.assertFalse(self.cache.exists())

    def test_revoked_project_registration_prevents_file_collection(self):
        self.engine.config['approved_roots'] = []
        with self.assertRaises(ValueError):
            self.collect()
        self.assertFalse(self.cache.exists())

    def test_changed_task_handoff_or_collection_fails_verification(self):
        draft = self.collect()
        self.engine.handoff.fact(str(self.project), self.job['id'], 'fictional_key', 'fictional value',
                                 'fictional evidence', 'user_confirmed', 'fictional-source')
        self.assertFalse(self.engine.verify_checkpoint_draft(draft['id'], writer_stopped=True)['valid'])
        fresh = self.collect()
        self.store.update(self.task['id'], summary='fictional changed stage')
        self.assertFalse(self.engine.verify_checkpoint_draft(fresh['id'], writer_stopped=True)['valid'])

    def test_tampered_approval_and_start_reference_are_rejected(self):
        self.store.update(self.approval['id'], payload={'checkpoint_paths': ['README.md']})
        with self.assertRaises(ValueError):
            self.collect()
        self.store.put('approval', self.approval)
        attempt = self.task['current_attempt']
        self.store.update(self.task['id'], current_attempt=dict(attempt, start_event_seq=999999))
        with self.assertRaises(ValueError):
            self.collect()

    def test_incomplete_prior_draft_is_not_guessed_into_a_generation(self):
        self.store.put('checkpoint', dict(id='fictional-incomplete', job_id=self.job['id'],
            task_id=self.task['id'], schema_version=1, state='writing'))
        with self.assertRaises(ValueError):
            self.collect()

    def test_rehashed_database_binding_cannot_claim_another_version(self):
        original = self.collect()
        for field, value in [('attempt_ordinal', 999), ('attempt_start_event_seq', 999),
                             ('plan_revision', 999), ('requirement_revision', 999),
                             ('plan_approval_id', 'fictional-other'), ('attempt_ordinal', True),
                             ('plan_revision', True)]:
            record = dict(original, state_revision=self.store.get(original['id'])['state_revision'], **{field: value})
            record['manifest_digest'] = checkpoint_digest(record)
            self.store.put('checkpoint', record)
            with self.subTest(field=field):
                self.assertFalse(self.engine.verify_checkpoint_draft(original['id'], writer_stopped=True)['valid'])

    def test_unknown_effect_condition_cannot_be_removed_by_rehashing(self):
        self.store.event('operation_result', 'Fictional result', self.task['id'], self.job['id'])
        record = self.collect()
        self.assertIn('operation_reconciliation_required', record['resume_preconditions'])
        record['resume_preconditions'] = ['checkpoint_not_ready']
        record['manifest_digest'] = checkpoint_digest(record)
        self.store.put('checkpoint', record)
        self.assertFalse(self.engine.verify_checkpoint_draft(record['id'], writer_stopped=True)['valid'])


if __name__ == '__main__':
    unittest.main()
