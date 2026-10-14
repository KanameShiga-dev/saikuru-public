"""SQAH Phase 1 on synthetic records: no database, App, process launch or AI call."""
import hashlib
import tempfile
import unittest
from pathlib import Path

from team_quality_view import FAILED, INCONCLUSIVE, INVALIDATED, PASSED, quality_view


def builder(runs=None, snapshot=None, status='succeeded'):
    return {'id': 'b1', 'role': 'builder', 'status': status, 'validation_runs': runs or [],
            'test_asset_snapshot': snapshot or {}}


def run(index=0, code=0, timed_out=False):
    return {'index': index, 'exit_code': code, 'timed_out': timed_out, 'observed_at_ns': 1}


def review(agent, status, summary='synthetic'):
    return {'id': 'r-' + agent, 'role': 'reviewer', 'status': 'succeeded', 'agent_name': agent,
            'result': {'status': status, 'summary': summary}}


class VerificationTests(unittest.TestCase):
    def facts(self, job, tasks, **kw):
        return quality_view(job, tasks, [], **kw)

    def test_commands_all_zero_passes(self):
        view = self.facts({'id': 'j', 'validation_commands': ['python -m unittest']}, [builder([run()])])
        self.assertEqual(view['facts'][0]['state'], PASSED)
        self.assertEqual(view['overall'], PASSED)

    def test_failure_timeout_and_missing_run_fail_using_the_engines_own_check(self):
        job = {'id': 'j', 'validation_commands': ['a', 'b']}
        for runs in ([run(0, 1), run(1, 0)], [run(0, 0), run(1, timed_out=True)], [run(0, 0)]):
            with self.subTest(runs=runs):
                view = self.facts(job, [builder(runs)])
                self.assertEqual(view['facts'][0]['state'], FAILED)
                self.assertEqual(view['overall'], FAILED)

    def test_no_commands_is_inconclusive_not_passed(self):
        view = self.facts({'id': 'j'}, [builder()])
        self.assertEqual(view['facts'][0]['state'], INCONCLUSIVE)
        self.assertEqual(view['overall'], INCONCLUSIVE)
        plan = {'required': False, 'reason': 'ドキュメントのみ', 'cases': [], 'command': '', 'existing_tests': []}
        view = self.facts({'id': 'j', 'code_test_plan': plan}, [builder()])
        self.assertIn('テスト対象外', view['facts'][0]['detail'])

    def test_no_builder_yet_is_not_a_pass(self):
        view = self.facts({'id': 'j', 'validation_commands': ['a']}, [])
        self.assertEqual(view['facts'][0]['state'], FAILED)


class TestFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        (self.project / 'tests').mkdir()
        self.test = self.project / 'tests' / 'test_x.py'
        self.test.write_text('assert True\n', encoding='utf-8')
        self.digest = hashlib.sha256(self.test.read_bytes()).hexdigest()
        self.plan = {'required': True, 'reason': '', 'cases': ['c'], 'command': 'python -m unittest',
                     'existing_tests': []}
        self.job = {'id': 'j', 'project': str(self.project), 'code_test_plan': self.plan}

    def view(self, snapshot):
        return quality_view(self.job, [builder([run()], snapshot)], [])

    def states(self, view):
        return {f['id']: f['state'] for f in view['facts']}

    def test_matching_hash_passes_changed_or_deleted_file_invalidates(self):
        snapshot = {'tests/test_x.py': self.digest}
        self.assertEqual(self.states(self.view(snapshot))['CQA-06'], PASSED)
        self.test.write_text('assert False\n', encoding='utf-8')
        view = self.view(snapshot)
        self.assertEqual(self.states(view)['CQA-06'], INVALIDATED)
        self.assertEqual(view['overall'], INVALIDATED)
        self.test.unlink()
        self.assertEqual(self.states(self.view(snapshot))['CQA-06'], INVALIDATED)

    def test_missing_snapshot_is_inconclusive(self):
        self.assertEqual(self.states(self.view({}))['CQA-06'], INCONCLUSIVE)

    def test_view_never_writes_or_touches_the_files(self):
        before = (self.test.stat().st_mtime_ns, self.digest)
        self.view({'tests/test_x.py': self.digest})
        self.assertEqual((self.test.stat().st_mtime_ns, hashlib.sha256(self.test.read_bytes()).hexdigest()), before)
        self.assertEqual(sorted(p.name for p in self.project.rglob('*') if p.is_file()), ['test_x.py'])


class DeliverableTests(unittest.TestCase):
    def test_missing_event_fails_and_hash_drift_invalidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / 'guide.md'
            doc.write_text('# synthetic\n', encoding='utf-8')
            digest = hashlib.sha256(doc.read_bytes()).hexdigest()
            job = {'id': 'j', 'project': tmp}
            ok = quality_view(job, [], [], recorded_hashes={'guide.md': digest})
            self.assertEqual({f['id']: f['state'] for f in ok['facts']}['DQA-01'], PASSED)
            doc.write_text('# changed\n', encoding='utf-8')
            drift = quality_view(job, [], [], recorded_hashes={'guide.md': digest})
            self.assertEqual({f['id']: f['state'] for f in drift['facts']}['DQA-01'], INVALIDATED)
            lost = quality_view(job, [], [], events=[{'type': 'deliverable_missing'}])
            self.assertEqual({f['id']: f['state'] for f in lost['facts']}['DQA-01'], FAILED)

    def test_no_deliverable_information_adds_no_fact(self):
        view = quality_view({'id': 'j', 'validation_commands': ['a']}, [builder([run()])], [])
        self.assertEqual([f['id'] for f in view['facts']], ['CQA-03'])


class SeparationTests(unittest.TestCase):
    def test_ai_reviews_are_findings_not_evidence(self):
        tasks = [builder(), review('common-reviewer', 'done'), review('common-security-reviewer', 'done')]
        view = quality_view({'id': 'j'}, tasks, [])
        self.assertFalse(view['findings']['counted_as_evidence'])
        self.assertEqual(len(view['findings']['reviews']), 2)
        # Both reviewers said done, yet nothing the program could establish: still inconclusive.
        self.assertEqual(view['overall'], INCONCLUSIVE)

    def test_failed_commands_win_over_done_reviews(self):
        tasks = [builder([run(0, 2)]), review('common-reviewer', 'done')]
        view = quality_view({'id': 'j', 'validation_commands': ['a']}, tasks, [])
        self.assertEqual(view['overall'], FAILED)

    def test_review_text_is_not_copied_into_the_view(self):
        tasks = [builder(), review('common-reviewer', 'done', summary='SECRET-LOOKING TEXT ' * 10)]
        text = repr(quality_view({'id': 'j'}, tasks, []))
        self.assertNotIn('SECRET-LOOKING', text)

    def test_approval_and_unresolved_counts(self):
        approvals = [{'kind': 'tool', 'status': 'approved'},
                     {'kind': 'completion', 'status': 'approved', 'payload': {'pending_items': [
                         {'title': 'a', 'checks': ['x', 'y']}, {'title': 'b', 'checks': ['z']}]}}]
        job = {'id': 'j', 'status': 'accepted_with_pending_checks'}
        view = quality_view(job, [], approvals)
        self.assertEqual(view['unresolved']['pending_items'], 3)
        self.assertEqual(view['approval'], {'status': 'approved', 'accepted': False, 'conditional': True})
        self.assertTrue(view['decides_nothing'])


if __name__ == '__main__':
    unittest.main()
