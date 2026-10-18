"""No.005 compatibility: no-fix document reviews can be inspected but findings stay blocking."""
import threading
import tempfile
import unittest
from pathlib import Path
from eval.development_readiness.reacceptance_fixture import make_fixture, close

class AcceptWithoutFixCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server = make_fixture(Path(self.temp.name))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.store = self.server.app.store
        self.engine = self.server.app.engine
        self.job = self.store.get('synthetic-reacceptance', 'job')

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        close(self.server)

    def test_no_fix_reaches_review_screen_but_does_not_bypass_findings(self):
        self.engine.config['document_review_relaxed'] = True
        self.store.update(self.job['id'], status='blocked', document_source=str(Path(self.temp.name)), document_format='md')
        reviewer = next(t for t in self.store.for_job('task', self.job['id']) if t.get('agent_name') == 'reviewer')
        self.store.update(reviewer['id'], result={'status': 'needs_changes', 'summary': '架空の指摘',
            'checks': ['架空の指摘を確認'], 'pending_checks': []})
        security = next(t for t in self.store.for_job('task', self.job['id']) if t.get('agent_name') == 'common-security-reviewer')
        self.store.update(security['id'], result={'status': 'done', 'summary': '架空の確認', 'checks': []})
        failed = self.engine.new_task(self.job, '架空の修正', '実行しない', 'builder')
        self.store.update(failed['id'], status='failed', result={'status': 'blocked', 'summary': '架空停止'})
        self.engine.accept_without_fix(self.job['id'])
        approval = next(a for a in self.store.for_job('approval', self.job['id'])
                        if a.get('kind') == 'completion' and a.get('status') == 'pending')
        self.assertTrue(approval['payload'].get('accept_without_fix'))
        self.assertEqual(self.engine.acknowledge_completion_review(approval['id'], True)['status'],
                         'awaiting_acceptance')
        with self.assertRaises(ValueError):
            self.engine.accept_completion(approval['id'], '正式受領')
        self.assertEqual(self.store.get(self.job['id'])['status'], 'awaiting_acceptance')
        self.assertEqual(self.store.get(approval['id'])['status'], 'pending')

if __name__ == '__main__': unittest.main()
