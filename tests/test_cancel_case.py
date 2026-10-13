"""Cancel evaluation preparation only: no App startup, provider discovery or AI calls.

Uses short-lived synthetic Python processes; each test stops what it starts.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from team_adapters import Process
from eval.development_readiness import process_tree, run_cancel, slow_task_seed

TREE = ('import subprocess, sys, time; '
        'subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]); time.sleep(60)')


def wait_for(predicate, seconds=10):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(.2)
    return predicate()


@unittest.skipUnless(os.name == 'nt', 'Windows process API')
class ProcessTreeTests(unittest.TestCase):
    def test_adapter_close_stops_child_and_grandchild(self):
        with tempfile.TemporaryDirectory() as tmp:
            process = Process([sys.executable, '-B', '-c', TREE], tmp)
            try:
                records = wait_for(lambda: [r for r in process_tree.descendants(os.getpid())
                                            if r['pid'] == process.proc.pid or r['parent'] == process.proc.pid]
                                   if len(process_tree.descendants(process.proc.pid)) >= 1 else None)
                self.assertGreaterEqual(len(records), 2, records)
                self.assertTrue(all(process_tree.alive(r) for r in records))
            finally:
                process.close()
            self.assertEqual(wait_for(lambda: not process_tree.survivors(records), 10), True)

    def test_reused_pid_is_not_treated_as_recorded_and_not_stopped(self):
        sleeper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            record = wait_for(lambda: next((r for r in process_tree.descendants(os.getpid())
                                            if r['pid'] == sleeper.pid), None))
            self.assertTrue(process_tree.alive(record))
            fake = dict(record, created=record['created'] - 1)
            self.assertFalse(process_tree.alive(fake))
            self.assertEqual(process_tree.kill_recorded([fake]), [])
            self.assertIsNone(sleeper.poll())
            self.assertEqual(process_tree.kill_recorded([record]), [sleeper.pid])
            sleeper.wait(timeout=10)
        finally:
            if sleeper.poll() is None:
                sleeper.kill()

    def test_offline_rehearsal_of_c1_judgement(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / 'slow_task.py').write_bytes(Path(slow_task_seed.__file__).read_bytes())
            child = subprocess.Popen([sys.executable, '-B', 'slow_task.py'], cwd=project)
            try:
                self.assertTrue(wait_for(lambda: run_cancel.heartbeats(project) >= 2, 10))
                records = [r for r in process_tree.descendants(os.getpid()) if r['pid'] == child.pid]
                subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'], capture_output=True)
                observation = run_cancel.watch_after_cancel(records, project, seconds=4)
            finally:
                if child.poll() is None:
                    child.kill()
            self.assertEqual(run_cancel.verdict(observation, 0), 'pass', observation)
            self.assertFalse(observation['heartbeat_grew_after_window'])


class PrepareAndVerdictTests(unittest.TestCase):
    def test_slow_task_writes_one_line_per_second(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'heartbeat.txt'
            slow_task_seed.run(2, target)
            self.assertEqual(len(target.read_text(encoding='utf-8').splitlines()), 2)

    def test_prepare_fixtures_and_conditions(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / 'cancel-eval-test'
            manifest = run_cancel.prepare(base)
            for name in run_cancel.SCENARIOS:
                project = base / 'projects' / name
                self.assertEqual(manifest['fixture_hashes'][name], run_cancel.fixture_hashes(project))
                self.assertFalse((project / 'heartbeat.txt').exists())
            self.assertEqual((manifest['budget'], manifest['stop_new_at'], manifest['cancel_at']),
                             (150000, 100000, 130000))
            config = json.loads((base / 'runtime' / 'config.json').read_text(encoding='utf-8'))
            self.assertIs(config['automatic_operations'], False)
            self.assertEqual(config['provider_settings']['enabled'], ['claude'])
            with self.assertRaises(FileExistsError):
                run_cancel.prepare(base)
        for bad in (Path(tempfile.gettempdir()) / 'cancel-eval-x', run_cancel.ROOT / 'data' / 'other-x'):
            with self.assertRaises(ValueError):
                run_cancel.validate_base(bad)

    def test_verdicts(self):
        base = {'recorded_processes': [{'pid': 1, 'name': 'x'}], 'all_gone_after_seconds': 1.0,
                'survivors_after_window': [], 'heartbeat_grew_after_window': False}
        self.assertEqual(run_cancel.verdict(base, 0), 'pass')
        self.assertEqual(run_cancel.verdict(dict(base, all_gone_after_seconds=None), 0), 'fail_process_left')
        self.assertEqual(run_cancel.verdict(dict(base, survivors_after_window=[{'pid': 1}]), 0), 'fail_process_left')
        self.assertEqual(run_cancel.verdict(dict(base, heartbeat_grew_after_window=True), 0), 'fail_command_continued')
        self.assertEqual(run_cancel.verdict(base, 1), 'fail_operation_after_cancel')
        self.assertEqual(run_cancel.verdict(dict(base, recorded_processes=[]), 0), 'incomplete_nothing_recorded')


if __name__ == '__main__':
    unittest.main()
