"""M08: paired backup, restore into a separate new folder, release-candidate manifest. Fictional data only."""
import contextlib
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import team_deployment as deploy
from team_handoff import HandoffDB
from team_store import Store

COMMIT = 'a' * 40


def make_pair_with_records(directory):
    Store(directory).db.close()
    HandoffDB(directory).close()
    with closing(sqlite3.connect(Path(directory) / deploy.TEAM_DB)) as db:
        db.execute("INSERT INTO objects VALUES ('note', 'n1', ?)", (json.dumps({'text': '架空の記録A'}, ensure_ascii=False),))
        db.execute("INSERT INTO objects VALUES ('note', 'n2', '{}')")
        db.commit()
    with closing(sqlite3.connect(Path(directory) / deploy.HANDOFF_DB)) as db:
        db.execute("INSERT INTO records(project,job_id,task_id,kind,source_id,happened_at,payload) "
                   "VALUES ('demo','j1','t1','note','s1',1.0,'{}')")
        db.commit()


def snapshot(directory):
    """File name -> hash. instance.lock is the lock file the running server also creates; it is not data."""
    directory = Path(directory)
    return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(directory.rglob('*')) if p.is_file() and p.name != 'instance.lock'}


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.source = self.base / 'source'
        make_pair_with_records(self.source)


class BackupRestore(Base):
    def test_backup_then_restore_into_a_separate_new_folder(self):
        source_before = snapshot(self.source)
        manifest = deploy.backup_pair(self.source, self.base / 'backup')
        self.assertEqual(snapshot(self.source), source_before)  # the source pair is untouched
        self.assertEqual(manifest['files'][deploy.TEAM_DB]['counts']['objects'], 2)
        self.assertEqual(manifest['files'][deploy.HANDOFF_DB]['counts']['records'], 1)
        backup_before = snapshot(self.base / 'backup')

        state = deploy.restore_pair(self.base / 'backup', self.base / 'restored')
        self.assertEqual(snapshot(self.base / 'backup'), backup_before)
        self.assertEqual(snapshot(self.source), source_before)
        self.assertEqual(state['pair_id'], manifest['pair_id'])
        self.assertEqual(deploy.inspect_pair(self.base / 'restored')['counts'], deploy.inspect_pair(self.source)['counts'])
        with closing(sqlite3.connect(self.base / 'restored' / deploy.TEAM_DB)) as db:
            self.assertEqual(json.loads(db.execute("SELECT body FROM objects WHERE id='n1'").fetchone()[0])['text'], '架空の記録A')
        self.assertFalse((self.base / 'restored.restoring').exists())

    def test_restored_pair_passes_the_startup_check_and_opens_with_the_stores(self):
        deploy.backup_pair(self.source, self.base / 'backup')
        deploy.restore_pair(self.base / 'backup', self.base / 'restored')
        self.assertEqual(deploy.require_pair(self.base / 'restored')['state'], 'ready')
        store = Store(self.base / 'restored')
        self.assertEqual(len(store.all('note')), 2)
        store.db.close()
        HandoffDB(self.base / 'restored').close()

    def test_existing_destinations_are_never_overwritten(self):
        (self.base / 'taken').mkdir()
        (self.base / 'taken' / 'keep.txt').write_text('x')
        with self.assertRaises(deploy.DeploymentError) as caught:
            deploy.backup_pair(self.source, self.base / 'taken')
        self.assertEqual(caught.exception.code, 'dest_exists')
        deploy.backup_pair(self.source, self.base / 'backup')
        with self.assertRaises(deploy.DeploymentError) as caught:
            deploy.restore_pair(self.base / 'backup', self.base / 'taken')
        self.assertEqual(caught.exception.code, 'dest_exists')
        self.assertEqual(snapshot(self.base / 'taken'), {'keep.txt': hashlib.sha256(b'x').hexdigest()})

    def test_tampered_backup_is_refused_and_creates_no_restore_folder(self):
        deploy.backup_pair(self.source, self.base / 'backup')
        with open(self.base / 'backup' / deploy.HANDOFF_DB, 'ab') as handle:
            handle.write(b'\0')
        with self.assertRaises(deploy.DeploymentError) as caught:
            deploy.restore_pair(self.base / 'backup', self.base / 'restored')
        self.assertEqual(caught.exception.code, 'manifest_mismatch')
        self.assertFalse((self.base / 'restored').exists())
        self.assertFalse((self.base / 'restored.restoring').exists())

    def test_running_instance_blocks_backup_and_leaves_no_output(self):
        with deploy._instance_lock(self.source):
            with self.assertRaises(deploy.DeploymentError) as caught:
                deploy.backup_pair(self.source, self.base / 'backup')
        self.assertEqual(caught.exception.code, 'in_use')
        self.assertFalse((self.base / 'backup').exists())

    def test_unusable_sources_are_not_backed_up(self):
        (self.source / deploy.HANDOFF_DB).unlink()
        with self.assertRaises(deploy.DeploymentError) as caught:
            deploy.backup_pair(self.source, self.base / 'backup')
        self.assertEqual(caught.exception.code, 'pair_partial')
        with self.assertRaises(deploy.DeploymentError) as caught:
            deploy.backup_pair(self.base / 'nothing-here', self.base / 'backup')
        self.assertEqual(caught.exception.code, 'nothing_to_backup')
        self.assertFalse((self.base / 'backup').exists())

    def test_command_line_round_trip(self):
        def run(*argv):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = deploy.main(list(argv))
            return code, out.getvalue(), err.getvalue()

        self.assertEqual(run('check', '--data', str(self.source))[:2], (0, 'ready\n'))
        self.assertEqual(run('backup', '--data', str(self.source), '--to', str(self.base / 'b'))[0], 0)
        self.assertEqual(run('restore', '--from', str(self.base / 'b'), '--to', str(self.base / 'r'))[:2], (0, 'ready\n'))
        code, _, err = run('restore', '--from', str(self.base / 'b'), '--to', str(self.base / 'r'))
        self.assertEqual(code, 2)
        self.assertIn('dest_exists', err)


class ReleaseManifest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name, body in {'app.py': 'print(1)', 'docs/guide.md': '# guide', 'data/team.sqlite3': 'ops',
                           '.env': 'K=v', 'server.log': 'log', 'client_secret.json': '{}', 'data/x.txt': 'x'}.items():
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text(body, encoding='utf-8')

    def test_manifest_is_deterministic_and_verifiable(self):
        first = deploy.build_release_manifest(self.root, ['docs/guide.md', 'app.py'], COMMIT)
        second = deploy.build_release_manifest(self.root, ['app.py', 'docs/guide.md', 'app.py'], COMMIT)
        self.assertEqual(first, second)
        self.assertEqual([f['path'] for f in first['files']], ['app.py', 'docs/guide.md'])
        self.assertEqual(first['files'][0]['sha256'], hashlib.sha256(b'print(1)').hexdigest())
        self.assertEqual(first['files'][0]['size'], 8)
        self.assertEqual(deploy.verify_release_manifest(self.root, first), [])

    def test_changed_or_removed_files_are_reported(self):
        manifest = deploy.build_release_manifest(self.root, ['app.py', 'docs/guide.md'], COMMIT)
        (self.root / 'app.py').write_text('print(2)', encoding='utf-8')
        (self.root / 'docs' / 'guide.md').unlink()
        self.assertEqual(deploy.verify_release_manifest(self.root, manifest), ['changed: app.py', 'missing: docs/guide.md'])

    def test_operational_secret_and_log_files_cannot_be_listed(self):
        for path in ('data/team.sqlite3', 'data/x.txt', '.env', 'server.log', 'client_secret.json'):
            with self.assertRaises(deploy.DeploymentError, msg=path) as caught:
                deploy.build_release_manifest(self.root, ['app.py', path], COMMIT)
            self.assertEqual(caught.exception.code, 'excluded_path', path)

    def test_bad_paths_and_commits_are_refused(self):
        for path, code in (('../outside.py', 'bad_path'), (str(self.root / 'app.py'), 'bad_path'),
                           ('docs\\guide.md', 'bad_path'), ('nope.py', 'missing_path')):
            with self.assertRaises(deploy.DeploymentError, msg=path) as caught:
                deploy.build_release_manifest(self.root, [path], COMMIT)
            self.assertEqual(caught.exception.code, code, path)
        with self.assertRaises(deploy.DeploymentError) as caught:
            deploy.build_release_manifest(self.root, ['app.py'], 'main')
        self.assertEqual(caught.exception.code, 'bad_commit')

    def test_a_hand_edited_manifest_listing_an_excluded_file_fails_verification(self):
        manifest = deploy.build_release_manifest(self.root, ['app.py'], COMMIT)
        manifest['files'].append({'path': 'data/team.sqlite3', 'size': 3, 'sha256': '0' * 64, 'kind': 'sqlite3'})
        self.assertEqual(deploy.verify_release_manifest(self.root, manifest), ['not allowed: data/team.sqlite3'])

    def test_command_line_writes_a_new_manifest_only(self):
        out = self.root / 'candidate.json'
        argv = ['manifest', '--root', str(self.root), '--commit', COMMIT, '--out', str(out), 'app.py']
        self.assertEqual(deploy.main(argv), 0)
        self.assertEqual(deploy.main(['verify-manifest', '--root', str(self.root), str(out)]), 0)
        with self.assertRaises(FileExistsError):
            deploy.main(argv)


if __name__ == '__main__':
    unittest.main()
