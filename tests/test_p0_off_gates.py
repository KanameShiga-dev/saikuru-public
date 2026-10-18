"""The G1/G2/G3 gates with the P0 contract switched off (p0_contract_enabled = false).

2026-10-19: a production folder keeps the P0 contract off. The gates must not depend on it: the AI-start approval
(G2) is created and single-use, and the plan approval (G3) is issued after the plan, as for a P0 job.
This subclasses the existing gate suite, so every one of its tests also runs with the switch off, and adds
explicit checks for the records Codex listed as not yet covered. Synthetic temporary data; no provider starts.
"""
import unittest
from unittest.mock import patch

from tests import test_g1_g2_gate as gate_tests


class P0OffGateTests(gate_tests.G1G2GateTests):
    def setUp(self):
        super().setUp()
        self.engine.config['p0_contract_enabled'] = False

    def test_a_request_is_a_legacy_job_with_the_gates_in_place(self):
        job = self.create_job()
        saved = self.store.get(job['id'], 'job')
        self.assertNotIn('p0_contract_version', saved)
        self.assertNotIn('goal_contract', saved)
        self.assertEqual(saved['g2_gate_version'], 1)
        self.assertEqual(saved['g2_status'], 'pending')
        self.assertEqual(saved['status'], 'awaiting_approval')
        pending = [a for a in self.store.for_job('approval', job['id']) if a['status'] == 'pending']
        self.assertEqual([a['kind'] for a in pending], ['ai_start'])
        self.assertEqual(self.planner(job['id'])['status'], 'queued')

    def test_the_plan_approval_g3_is_issued_after_g2_with_the_switch_off(self):
        job = self.create_job()
        task = self.planner(job['id'])
        self.engine.decide(self.g2(job['id'])['id'], True, 'Synthetic explicit G2 approval')
        self.assertEqual(self.store.get(job['id'])['g2_status'], 'approved')
        self.store.update(task['id'], status='running', attempt=1)
        self.assertTrue(self.engine._claim_g2_for_run(task['id'], 1))
        self.store.update(job['id'], g2_status='consumed')
        with patch('team_plan_check.issues', return_value=[]), \
                patch('team_document_capabilities.require_plan'):
            self.engine._finish(task, {'status': 'done', 'summary': 'Synthetic plan only', 'tasks': []})
        pending = [a for a in self.store.for_job('approval', job['id']) if a['status'] == 'pending']
        self.assertEqual([a['kind'] for a in pending], ['plan'])
        self.assertEqual(self.store.get(job['id'])['status'], 'awaiting_approval')
        self.assertNotIn('p0_contract_version', self.store.get(job['id']))


if __name__ == '__main__':
    unittest.main()
