"""synthetic-due-date-v1 preparation only: no App startup, provider discovery or AI calls."""
import json
import tempfile
import unittest
from pathlib import Path

from team_store import Store
from eval.development_readiness import check_due_date, resume_due_date, run_due_date, summarize_due_date
from eval.development_readiness.accept_due_date import ensure_single_job

HERE = Path(__file__).resolve().parents[1] / 'eval' / 'development_readiness'


class GraderTests(unittest.TestCase):
    def results(self, path, **kwargs):
        result = check_due_date.check(path, **kwargs)
        return {c['case']: c['passed'] for c in result['checks']}, result

    def test_reference_passes_every_case(self):
        passed, result = self.results(HERE / 'due_date_reference.py')
        self.assertTrue(result['all_passed'], passed)
        self.assertIsNone(result['error'])

    def test_seed_fails_only_the_rolling_cases(self):
        passed, result = self.results(HERE / 'due_date_seed.py')
        self.assertFalse(result['all_passed'])
        self.assertEqual(sorted(k for k, v in passed.items() if not v),
                         ['closed_rolls_forward', 'consecutive_closed', 'month_year_boundary', 'zero_days'])

    def write(self, directory, source):
        path = Path(directory) / 'due_date.py'
        path.write_text(source, encoding='utf-8')
        return path

    def test_endless_loop_times_out_and_broken_module_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            loop = self.write(tmp, 'def due_date(start, days, closed):\n    while True: pass\n')
            passed, result = self.results(loop, timeout=3)
            self.assertEqual(result['error'], 'timeout')
            self.assertFalse(any(passed.values()))
            broken = self.write(tmp, 'def due_date(:\n')
            self.assertEqual(self.results(broken)[1]['error'], 'module_failed')

    def test_mutating_the_closed_days_fails_input_check(self):
        source = (HERE / 'due_date_reference.py').read_text(encoding='utf-8').replace(
            '    closed = set(closed_weekdays)\n', '    closed = set(closed_weekdays)\n'
            '    if isinstance(closed_weekdays, list): closed_weekdays.append(9)\n')
        with tempfile.TemporaryDirectory() as tmp:
            passed, _ = self.results(self.write(tmp, source))
            self.assertFalse(passed['input_unchanged'])
            self.assertTrue(passed['closed_rolls_forward'])

    def test_readme_presence_is_not_a_correctness_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'README.md').write_bytes((HERE / 'due_date_readme_seed.md').read_bytes())
            status = check_due_date.readme_status(tmp)
            self.assertFalse(status['readme_changed'])
            self.assertEqual(status['readme_example_verified'], 'human_check_required')


class PrepareTests(unittest.TestCase):
    def test_prepare_isolates_project_and_records_fixed_conditions(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / 'due-date-eval-test'
            manifest = run_due_date.prepare(base)
            project = base / 'development'
            self.assertEqual({p.name for p in project.iterdir()},
                             {'due_date.py', 'README.md', 'AGENTS.md', 'CLAUDE.md', 'SCOPE.md'})
            self.assertEqual((project / 'due_date.py').read_bytes(), (HERE / 'due_date_seed.py').read_bytes())
            config = json.loads((base / 'runtime' / 'config.json').read_text(encoding='utf-8'))
            self.assertEqual(config['max_repairs'], 1)
            self.assertEqual(config['provider_settings']['enabled'], ['claude'])
            self.assertEqual(config['approved_roots'], [str(project)])
            self.assertEqual(config['review_context_mode'], 'off')
            self.assertEqual((manifest['budget'], manifest['stop_new_at'], manifest['cancel_at']),
                             (300000, 195000, 240000))
            self.assertEqual(set(manifest['source_hashes']), set(run_due_date.HASHED))
            sheet = json.loads((base / 'human_time.json').read_text(encoding='utf-8'))
            self.assertEqual(set(sheet['phases'].values()), {None})
            with self.assertRaises(FileExistsError):
                run_due_date.prepare(base)

    def test_base_must_be_a_new_style_directory_under_data(self):
        for bad in (Path(tempfile.gettempdir()) / 'due-date-eval-x', run_due_date.ROOT / 'data' / 'other-x'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                run_due_date.validate_base(bad)


class SummaryTests(unittest.TestCase):
    def sheet(self, directory, phases, observer='real_user'):
        path = Path(directory) / 'human_time.json'
        path.write_text(json.dumps({'observer': observer, 'unit': 'minutes', 'phases': phases}), encoding='utf-8')
        return path

    def test_unmeasured_is_not_zero(self):
        phases = dict.fromkeys(run_due_date.PHASES, None)
        with tempfile.TemporaryDirectory() as tmp:
            result = summarize_due_date.human_time(self.sheet(tmp, dict(phases, preparation=3, recovery=0)))
            self.assertIsNone(result['total_minutes'])
            self.assertEqual(result['measured_minutes_lower_bound'], 3)
            self.assertEqual(result['unmeasured_phases'], ['confirmation', 'correction', 'operation'])
            full = summarize_due_date.human_time(self.sheet(tmp, dict.fromkeys(run_due_date.PHASES, 2)))
            self.assertEqual(full['total_minutes'], 10)

    def test_invalid_sheets_are_rejected(self):
        phases = dict.fromkeys(run_due_date.PHASES, None)
        with tempfile.TemporaryDirectory() as tmp:
            for bad in (dict(phases, operation=-1), dict(phases, operation=1.5), dict(phases, operation=True),
                        {k: v for k, v in phases.items() if k != 'recovery'}, dict(phases, waiting=1)):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    summarize_due_date.human_time(self.sheet(tmp, bad))
            with self.assertRaises(ValueError):
                summarize_due_date.human_time(self.sheet(tmp, phases, observer='evaluation_runner'))

    def test_review_reports_split_by_agent(self):
        tasks = [{'role': 'reviewer', 'agent_name': 'common-reviewer', 'result': {'status': 'done'},
                  'review_result_history': [{'result': {'status': 'needs_changes'}}]},
                 {'role': 'reviewer', 'agent_name': 'common-security-reviewer', 'result': {'status': 'needs_changes'}},
                 {'role': 'builder', 'result': {'status': 'needs_changes'}}]
        self.assertEqual(summarize_due_date.review_split(tasks), {'normal': 1, 'security': 1})


class ResumePreflightTests(unittest.TestCase):
    """Synthetic budget-stopped state. Does not depend on local evaluation records, which change once resumed."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / 'due-date-eval-synthetic'
        manifest = run_due_date.prepare(self.base)
        store = Store(self.base / 'runtime')
        try:
            store.put('job', {'id': 'j', 'status': 'cancelled'})
            for n, (role, agent) in enumerate((('planner', 'common-explorer'), ('builder', 'common-implementer'),
                                               ('builder', 'common-implementer'), ('reviewer', 'common-reviewer'))):
                store.put('task', {'id': f't{n}', 'job_id': 'j', 'role': role, 'agent_name': agent,
                                   'status': 'succeeded', 'after': f't{n-1}' if n else None})
            store.put('task', {'id': 'sec', 'job_id': 'j', 'role': 'reviewer', 'status': 'cancelled',
                               'agent_name': 'common-security-reviewer', 'after': 't3'})
        finally:
            store.db.close()
        manifest.update(job_id='j', results=[{'stop_reason': 'unknown_usage_or_budget_stop'}])
        (self.base / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        (self.base / 'checks').mkdir()
        (self.base / 'checks' / 'round-2.json').write_text(json.dumps({'all_passed': True}), encoding='utf-8')

    def test_original_state_passes_and_points_at_the_security_review(self):
        job_id, security_id, total = resume_due_date.preflight(self.base)
        store = Store(self.base / 'runtime')
        try:
            self.assertEqual(store.get(security_id)['agent_name'], 'common-security-reviewer')
            self.assertEqual(store.get(job_id)['status'], 'cancelled')
        finally:
            store.db.close()
        self.assertLess(total, resume_due_date.RESUME_REFUSE_AT)

    def test_refuses_when_any_precondition_breaks(self):
        manifest_path = self.base / 'manifest.json'
        original = manifest_path.read_text(encoding='utf-8')
        manifest = json.loads(original)
        manifest_path.write_text(json.dumps(dict(manifest, resume={})), encoding='utf-8')
        with self.assertRaises(ValueError):
            resume_due_date.preflight(self.base)
        manifest_path.write_text(original, encoding='utf-8')
        check = self.base / 'checks' / 'round-2.json'
        saved = check.read_text(encoding='utf-8')
        check.write_text(json.dumps(dict(json.loads(saved), all_passed=False)), encoding='utf-8')
        with self.assertRaises(ValueError):
            resume_due_date.preflight(self.base)
        check.write_text(saved, encoding='utf-8')
        store = Store(self.base / 'runtime')
        try:
            security = next(t for t in store.all('task') if t.get('agent_name') == 'common-security-reviewer')
            store.put('approval', {'id': 'synthetic-pending', 'job_id': security['job_id'], 'status': 'pending',
                                   'kind': 'tool'})
        finally:
            store.db.close()
        with self.assertRaises(ValueError):
            resume_due_date.preflight(self.base)


class AcceptanceGuardTests(unittest.TestCase):
    def test_refuses_empty_or_multi_job_db_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            try:
                database = Path(tmp) / 'team.sqlite3'
                with self.assertRaises(ValueError):
                    ensure_single_job(database)
                store.put('job', {'id': 'a', 'status': 'awaiting_acceptance'})
                ensure_single_job(database)
                store.put('job', {'id': 'b', 'status': 'accepted'})
                with self.assertRaises(ValueError):
                    ensure_single_job(database)
                self.assertEqual(len(store.all('job')), 2)
            finally:
                store.db.close()


if __name__ == '__main__':
    unittest.main()
