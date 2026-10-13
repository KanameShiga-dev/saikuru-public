import copy
import tempfile
import unittest
from pathlib import Path

from eval.development_readiness.reacceptance_fixture import make_fixture, close


class ReacceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server = make_fixture(Path(self.temp.name))
        self.addCleanup(lambda: close(self.server))
        self.store = self.server.app.store
        self.engine = self.server.app.engine
        self.original = self.store.all('approval')[0]

    def test_conditional_evidence_requires_new_acceptance_and_preserves_history(self):
        self.engine.decide(self.original['id'], True, 'Synthetic proxy decision')
        self.assertEqual(self.store.get('synthetic-reacceptance')['status'], 'accepted_with_pending_checks')
        original = copy.deepcopy(self.store.get(self.original['id']))
        new_id = self.engine.record_completion_evidence(self.original['id'], 0, 0, 'Synthetic display observed', True)['approval_id']
        self.assertEqual(self.store.get('synthetic-reacceptance')['status'], 'awaiting_acceptance')
        self.assertEqual(self.store.get(self.original['id']), original)
        updated = self.store.get(new_id)
        self.assertEqual(updated['status'], 'pending')
        self.assertEqual(updated['payload']['pending_items'], [])
        self.assertEqual(updated['payload']['resolved_checks'][0]['source_approval_id'], self.original['id'])
        with self.assertRaises(ValueError): self.engine.decide(self.original['id'], True, '')
        self.engine.decide(new_id, True, 'Synthetic proxy reacceptance')
        self.assertEqual(self.store.get('synthetic-reacceptance')['status'], 'accepted')
        self.assertEqual(len(self.store.all('approval')), 2)
        self.assertEqual(self.engine.commands, {})
        self.assertEqual(self.engine.active, {})
        self.assertTrue(self.engine.paused)

    def test_unconfirmed_or_invalid_evidence_does_not_change_state(self):
        for item, check, text, confirmed in ((0,0,'note',False),(99,0,'note',True),(0,99,'note',True),(0,0,'',True)):
            before = self.store.all('approval')
            with self.assertRaises(ValueError):
                self.engine.record_completion_evidence(self.original['id'], item, check, text, confirmed)
            self.assertEqual(self.store.all('approval'), before)

    def test_reopening_preserves_pending_human_decision(self):
        self.engine.decide(self.original['id'], True, 'Synthetic proxy decision')
        new_id = self.engine.record_completion_evidence(self.original['id'], 0, 0, 'Synthetic note', True)['approval_id']
        port = self.server.server_port
        close(self.server)
        self.server = make_fixture(Path(self.temp.name), port)
        self.assertEqual(self.server.app.store.get('synthetic-reacceptance')['status'], 'awaiting_acceptance')
        self.assertEqual(self.server.app.store.get(new_id)['status'], 'pending')
        self.assertEqual(len(self.server.app.store.all('approval')), 2)
