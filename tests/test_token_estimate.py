"""Token estimator logic on synthetic runs only: no databases, App or AI calls."""
import unittest
from collections import Counter

from eval.token_estimate import estimate as e

SONNET = ('claude', 'sonnet')
SOL = ('codex', 'gpt-6-sol')


def synthetic(jobs_spec):
    """jobs_spec: {job_name: (kind, [(provider, model, role, tokens), ...])} -> (runs, jobs) like collect()."""
    runs, jobs = [], {}
    for name, (kind, rows) in jobs_spec.items():
        job = ('db', name)
        entry = {'kind': kind, 'runs': [], 'complete': True}
        for provider, model, role, tokens in rows:
            run = {'provider': provider, 'model': model, 'role': role, 'tokens': tokens, 'job': job}
            runs.append(run)
            entry['runs'].append(run)
        jobs[job] = entry
    return runs, jobs


def four_call(model, scale=1.0, kind=('development', 'full')):
    p, m = model
    return kind, [(p, m, 'planner', int(30000 * scale)), (p, m, 'builder', int(50000 * scale)),
                  (p, m, 'reviewer', int(40000 * scale)), (p, m, 'security', int(30000 * scale))]


class QuantileAndBandTests(unittest.TestCase):
    def test_quantile_interpolates(self):
        self.assertEqual(e.quantile([1, 2, 3, 4, 5], .5), 3)
        self.assertEqual(e.quantile([10, 20], .25), 12.5)
        self.assertIsNone(e.quantile([], .5))

    def test_narrowest_band_that_reaches_target(self):
        values = list(range(100, 201))
        inside = [(values, 150)] * 10
        q, widen, coverage, reached = e.choose_quantile(inside, .8)
        self.assertEqual((q, widen, reached), (0.25, 1.0, True))
        self.assertEqual(coverage, 1.0)

    def test_widens_beyond_full_range_and_reports_failure(self):
        values = [100, 110, 120]
        # Actuals 30% above the max: only widening by 1.5 covers them.
        q, widen, _, reached = e.choose_quantile([(values, 156)] * 5, .8)
        self.assertEqual((q, widen, reached), (0.0, 1.5, True))
        q, widen, coverage, reached = e.choose_quantile([(values, 1000)] * 5, .8)
        self.assertFalse(reached)
        self.assertEqual(coverage, 0.0)

    def test_no_usable_samples(self):
        self.assertEqual(e.choose_quantile([([5], 5)], .8), (None, None, None, False))


class PricingTests(unittest.TestCase):
    def test_security_falls_back_to_reviewer_model_and_unknown_role_is_skipped(self):
        runs, _ = synthetic({f'j{i}': four_call(SOL) for i in range(4)})
        stats = e.table(runs)
        counts = Counter({'planner': 1, 'builder': 2, 'reviewer': 1, 'security': 1})
        assignment = {'planner': SOL, 'builder': SOL, 'reviewer': SOL}
        self.assertEqual(e.priced(counts, assignment, stats), 30000 + 2 * 50000 + 40000 + 30000)
        self.assertIsNone(e.priced(Counter({'researcher': 1}), assignment, stats))

    def test_too_few_samples_gives_no_point_estimate(self):
        runs, _ = synthetic({'only': four_call(SONNET)})
        self.assertIsNone(e.point(Counter({(*SONNET, 'planner'): 1}), e.table(runs)))


class BacktestTests(unittest.TestCase):
    def setUp(self):
        spec = {f'dev{i}': four_call(SONNET, scale=1 + i / 20) for i in range(8)}
        spec.update({f'doc{i}': four_call(SONNET, scale=2 + i / 20, kind=('document', 'full')) for i in range(6)})
        self.runs, self.jobs = synthetic(spec)

    def test_after_planning_is_leave_one_out_and_reaches_target(self):
        result = e.after_planning(self.runs, self.jobs, .8)
        self.assertEqual(result['jobs'], 14)
        self.assertTrue(result['target_reached'])
        self.assertGreaterEqual(result['coverage'], .8)

    def test_at_submission_uses_same_kind_peers_only(self):
        result = e.at_submission(self.runs, self.jobs, .8)
        self.assertEqual(result['jobs'] + result['skipped_too_few_peers'], 14)
        assignment = {r: SONNET for r in ('planner', 'builder', 'reviewer', 'security')}
        dev = e.estimate_for(('development', 'full'), assignment, self.runs, self.jobs, 0.0)
        doc = e.estimate_for(('document', 'full'), assignment, self.runs, self.jobs, 0.0)
        self.assertEqual(dev[3], 8)
        self.assertEqual(doc[3], 6)
        self.assertLessEqual(dev[0], dev[1])
        self.assertLessEqual(dev[1], dev[2])

    def test_too_few_peers_gives_none(self):
        assignment = {r: SONNET for r in ('planner', 'builder', 'reviewer', 'security')}
        self.assertIsNone(e.estimate_for(('development', 'light_eval'), assignment, self.runs, self.jobs, 0.1))


class DatabaseSelectionTests(unittest.TestCase):
    def test_copies_are_skipped(self):
        for path in e.databases():
            parts = set(path.relative_to(e.ROOT).parts)
            self.assertFalse(parts & e.SKIP_PARTS, path)


if __name__ == '__main__':
    unittest.main()
