"""Isolated outcome accounting; no production database or model calls."""
import unittest

from team_optimization_metrics import _outcomes


def row(job_id, measurement, input_total, output):
    return {'job_id': job_id, 'measurement': measurement, 'input_total': input_total,
            'output': output, 'weighted': input_total + output if input_total is not None and output is not None else None}


class AcceptanceMetricsTests(unittest.TestCase):
    def test_conditional_and_failed_costs_are_not_successes(self):
        jobs = {'ordinary': {'status': 'accepted'},
                'conditional': {'status': 'accepted_with_pending_checks'},
                'failed': {'status': 'failed'}}
        rows = [row('ordinary', 'final', 10, 5), row('conditional', 'final', 20, 10),
                row('failed', 'final', 4, 1)]
        report = _outcomes(jobs, jobs, rows)
        self.assertEqual(report['accepted_jobs'], 1)
        self.assertEqual(report['conditional_accepted_jobs'], 1)
        self.assertEqual(report['job_statuses']['failed'], 1)
        self.assertEqual(report['input_total_per_accepted'], 34)
        self.assertEqual(report['output_per_accepted'], 16)

    def test_unknown_or_partial_usage_hides_cost_ratio(self):
        jobs = {'ordinary': {'status': 'accepted'}, 'failed': {'status': 'failed'}}
        for measurement, tokens in [('unknown', None), ('partial', 3)]:
            with self.subTest(measurement=measurement):
                report = _outcomes(jobs, jobs, [row('ordinary', 'final', 10, 5),
                                                row('failed', measurement, tokens, 1)])
                self.assertIsNone(report['input_total_per_accepted'])
                self.assertFalse(report['complete_usage'])


if __name__ == '__main__':
    unittest.main()
