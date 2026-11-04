"""M08: DB-pair check before any write, at every start entry. Fictional temp databases only."""
import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import background_runner
import manage
import server
import team_deployment as deploy
from team_handoff import HandoffDB
from team_store import Store

SOURCE = Path(__file__).resolve().parents[1]


def make_pair(directory):
    store = Store(directory)
    store.db.close()
    handoff = HandoffDB(directory)
    handoff.close()


def snapshot(directory):
    """Every file under the folder -> content hash. Equal snapshots prove nothing was written or created."""
    directory = Path(directory)
    if not directory.exists():
        return None
    return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(directory.rglob('*')) if p.is_file()}


def sql(path, statement):
    with closing(sqlite3.connect(path)) as db:
        db.execute(statement)
        db.commit()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / 'data'

    def bad_pairs(self):
        """name -> (folder, expected code); each folder is a separate broken pair."""
        base = Path(self.temp.name)
        cases = {}
        for name, code in (('partial', 'pair_partial'), ('corrupt', 'db_corrupt'), ('foreign', 'schema_unknown'),
                           ('mismatch', 'pair_mismatch'), ('newer', 'schema_newer')):
            folder = base / name
            make_pair(folder)
            cases[name] = (folder, code)
        (cases['partial'][0] / deploy.HANDOFF_DB).unlink()
        (cases['corrupt'][0] / deploy.HANDOFF_DB).write_bytes(b'not a database' * 100)
        foreign = cases['foreign'][0] / deploy.TEAM_DB
        foreign.unlink()
        sql(foreign, 'CREATE TABLE something_else (x)')
        other = base / 'other'
        make_pair(other)
        (cases['mismatch'][0] / deploy.HANDOFF_DB).write_bytes((other / deploy.HANDOFF_DB).read_bytes())
        sql(cases['newer'][0] / deploy.TEAM_DB, "UPDATE pair_identity SET value='99' WHERE key='schema_version'")
        return cases


class PairCheck(Fixture):
    def test_fresh_pair_is_allowed_and_check_writes_nothing(self):
        self.assertEqual(deploy.require_pair(self.data)['state'], 'fresh')
        self.assertFalse(self.data.exists())

    def test_a_database_with_unrelated_tables_is_not_blank(self):
        self.data.mkdir()
        sql(self.data / deploy.TEAM_DB, 'CREATE TABLE unrelated (x)')
        with self.assertRaises(deploy.DeploymentError) as caught:
            deploy.require_pair(self.data)
        self.assertEqual(caught.exception.code, 'schema_unknown')

    def test_empty_files_count_as_blank(self):
        self.data.mkdir()
        for name in deploy.PAIR_FILES:
            sqlite3.connect(self.data / name).close()
        self.assertEqual(deploy.require_pair(self.data)['state'], 'fresh')

    def test_compatible_pair_shares_one_id_and_is_not_modified_by_the_check(self):
        make_pair(self.data)
        before = snapshot(self.data)
        state = deploy.require_pair(self.data)
        self.assertEqual(state['state'], 'ready')
        self.assertTrue(state['pair_id'])
        self.assertEqual(snapshot(self.data), before)

    def test_pair_id_is_adopted_in_either_creation_order(self):
        first = Path(self.temp.name) / 'handoff-first'
        first.mkdir()
        HandoffDB(first).close()
        Store(first).db.close()
        second = Path(self.temp.name) / 'store-first'
        make_pair(second)
        for folder in (first, second):
            state = deploy.require_pair(folder)
            self.assertTrue(state['pair_id'], folder)

    def test_legacy_pair_without_identity_is_compatible(self):
        make_pair(self.data)
        for name in deploy.PAIR_FILES:
            sql(self.data / name, 'DROP TABLE pair_identity')
        self.assertIsNone(deploy.require_pair(self.data)['pair_id'])
        make_pair(self.data)  # opening with the current code registers an identity; both sides agree
        self.assertTrue(deploy.require_pair(self.data)['pair_id'])

    def test_every_bad_pair_is_refused_with_its_code_and_nothing_is_written(self):
        for name, (folder, code) in self.bad_pairs().items():
            before = snapshot(folder)
            with self.assertRaises(deploy.DeploymentError, msg=name) as caught:
                deploy.require_pair(folder)
            self.assertEqual(caught.exception.code, code, name)
            self.assertIn(code, str(caught.exception))
            self.assertEqual(snapshot(folder), before, name)


class EveryEntry(Fixture):
    def test_make_server_refuses_before_data_lock_stores_or_socket(self):
        with mock.patch.object(server, 'ThreadingHTTPServer') as listener, \
                mock.patch.object(server, 'App') as app:
            for name, (folder, code) in self.bad_pairs().items():
                before = snapshot(folder)
                with self.assertRaises(deploy.DeploymentError, msg=name) as caught:
                    server.make_server(folder, 0)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(snapshot(folder), before, name)
            listener.assert_not_called()
            app.assert_not_called()

    def test_server_main_exits_with_a_message_instead_of_a_traceback(self):
        folder, code = self.bad_pairs()['partial']
        with mock.patch.object(sys, 'argv', ['server.py', '--data', str(folder), '--port', '0']):
            with self.assertRaises(SystemExit) as caught:
                server.main()
        self.assertIn(code, str(caught.exception))

    def test_server_script_run_directly_stops_with_a_nonzero_exit(self):
        folder, code = self.bad_pairs()['corrupt']
        before = snapshot(folder)
        done = subprocess.run([sys.executable, '-X', 'utf8', str(SOURCE / 'server.py'),
                               '--data', str(folder), '--port', '0'],
                              cwd=SOURCE, capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn(code, done.stderr)
        self.assertEqual(snapshot(folder), before)

    def test_check_command_used_by_the_powershell_entries(self):
        make_pair(self.data)
        script = str(SOURCE / 'team_deployment.py')
        good = subprocess.run([sys.executable, '-I', '-X', 'utf8', script, 'check', '--data', str(self.data)],
                              capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual((good.returncode, good.stdout.strip()), (0, 'ready'))
        folder, code = self.bad_pairs()['mismatch']
        bad = subprocess.run([sys.executable, '-I', '-X', 'utf8', script, 'check', '--data', str(folder)],
                             capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(bad.returncode, 2)
        self.assertIn(code, bad.stderr)

    def test_manage_start_refuses_before_scheduler_or_child_process(self):
        root = Path(self.temp.name) / 'root'
        folder, code = self.bad_pairs()['partial']
        (root / 'data').mkdir(parents=True)
        (root / 'data' / deploy.TEAM_DB).write_bytes((folder / deploy.TEAM_DB).read_bytes())
        (root / 'data' / 'background.disabled').touch()
        before = snapshot(root / 'data')
        with mock.patch.object(manage, 'ROOT', root), \
                mock.patch.object(manage, 'health', return_value=False), \
                mock.patch.object(sys, 'argv', ['manage.py', 'start']), \
                mock.patch.object(manage.subprocess, 'run') as run, \
                mock.patch.object(manage.subprocess, 'Popen') as popen:
            with self.assertRaises(SystemExit) as caught:
                manage.main()
        self.assertIn(code, str(caught.exception))
        run.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(snapshot(root / 'data'), before)  # marker kept, no logs created

    @unittest.skipUnless(os.name == 'nt', 'background_runner uses msvcrt')
    def test_background_runner_does_not_spawn_on_a_bad_pair_but_does_on_a_good_one(self):
        class Stop(Exception):
            pass

        def run_once(root):
            with mock.patch.object(background_runner, 'ROOT', root), \
                    mock.patch.object(background_runner, 'health', return_value=False), \
                    mock.patch.object(background_runner.time, 'sleep', side_effect=Stop), \
                    mock.patch.object(background_runner.subprocess, 'Popen') as popen:
                with self.assertRaises(Stop):
                    background_runner.main()
            return popen

        bad_root = Path(self.temp.name) / 'bad'
        (bad_root / 'data').mkdir(parents=True)
        (bad_root / 'data' / deploy.TEAM_DB).write_bytes((self.bad_pairs()['partial'][0] / deploy.TEAM_DB).read_bytes())
        run_once(bad_root).assert_not_called()
        self.assertIn('pair_partial', (bad_root / 'data' / 'server.stderr.log').read_text(encoding='utf-8'))

        good_root = Path(self.temp.name) / 'good'
        make_pair(good_root / 'data')
        run_once(good_root).assert_called_once()

    def test_powershell_entries_run_the_check_before_their_first_action(self):
        start = (SOURCE / 'Start-AgentTeam.ps1').read_text(encoding='utf-8-sig')
        self.assertLess(start.index('team_deployment.py'), start.index('New-Item'))
        self.assertLess(start.index('team_deployment.py'), start.index('Start-Process'))
        install = (SOURCE / 'install_background.ps1').read_text(encoding='utf-8-sig')
        self.assertLess(install.index('team_deployment.py'), install.index('Register-ScheduledTask'))
        self.assertLess(install.index('team_deployment.py'), install.index('Add-Type'))


if __name__ == '__main__':
    unittest.main()
