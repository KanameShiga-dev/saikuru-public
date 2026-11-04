"""Contract for tests/known_failures.json: no silent removal, no double counting, no missing classification."""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'tests' / 'known_failures.json'
REQUIRED = ('id', 'title', 'test_ids', 'classification', 'current_result', 'evidence', 'next_action', 'owner')
RUN_FIELDS = ('base_commit', 'python', 'runner', 'command', 'discovered', 'executed',
              'passed', 'failed', 'errors', 'skipped', 'known_failure_ids')
SKIP_MARKS = re.compile(r'^\s*@(unittest\.)?(skip\w*|expectedFailure)\b')


def validate(data):
    """Return a list of problems; empty means the tracker is consistent."""
    problems = []
    seen_ids, seen_tests = set(), {}
    for e in data['entries']:
        eid = e.get('id', '?')
        for key in REQUIRED:
            if key not in e or e[key] in (None, ''):
                if key == 'test_ids' and e.get(key) == []:
                    continue
                problems.append('%s: missing %s' % (eid, key))
        if eid in seen_ids:
            problems.append('%s: duplicate id' % eid)
        seen_ids.add(eid)
        if e.get('classification') not in data['classifications']:
            problems.append('%s: bad classification' % eid)
        if e.get('current_result') not in data['results']:
            problems.append('%s: bad current_result' % eid)
        if e.get('classification') != 'unclassified' and e.get('current_result') == 'not_run':
            problems.append('%s: classified without a recorded run' % eid)
        for tid in e.get('test_ids', []):
            if tid in seen_tests:
                problems.append('%s: test %s already counted by %s' % (eid, tid, seen_tests[tid]))
            seen_tests[tid] = eid
    for c in data.get('collection_errors', []):
        if c.get('kf') not in seen_ids:
            problems.append('collection error %s: unknown tracker id %s' % (c.get('file'), c.get('kf')))
    return problems


def id_problems(tid):
    """tid is dotted module path plus Class.test_name, e.g. tests.test_x.Case.test_y."""
    parts = tid.split('.')
    for i in range(len(parts) - 2, 0, -1):
        path = ROOT.joinpath(*parts[:i]).with_suffix('.py')
        if path.is_file():
            name = parts[-1]
            lines = path.read_text(encoding='utf-8').splitlines()
            idx = [n for n, l in enumerate(lines) if re.match(r'\s*def %s\(' % re.escape(name), l)]
            if not idx:
                return ['test not found in ' + path.name]
            if any(SKIP_MARKS.match(lines[n - 1]) for n in idx if n):
                return ['test is disabled by a skip/expectedFailure mark']
            return []
    return ['module file not found']


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(DATA.read_text(encoding='utf-8'))

    def test_current_tracker_is_consistent(self):
        self.assertEqual(validate(self.data), [])

    def test_referenced_tests_exist_and_are_enabled(self):
        for e in self.data['entries']:
            for tid in e['test_ids']:
                self.assertEqual(id_problems(tid), [], e['id'] + ' ' + tid)

    def test_missing_classification_is_detected(self):
        self.data['entries'][0]['classification'] = ''
        self.assertTrue(any('classification' in p for p in validate(self.data)))

    def test_missing_evidence_is_detected(self):
        del self.data['entries'][0]['evidence']
        self.assertTrue(any('evidence' in p for p in validate(self.data)))

    def test_duplicate_count_is_detected(self):
        a, b = self.data['entries'][:2]
        a['test_ids'] = b['test_ids'] = ['tests.test_x.Case.test_y']
        self.assertTrue(any('already counted' in p for p in validate(self.data)))

    def test_duplicate_id_is_detected(self):
        self.data['entries'][1]['id'] = self.data['entries'][0]['id']
        self.assertTrue(any('duplicate id' in p for p in validate(self.data)))

    def test_classification_without_run_is_detected(self):
        self.data['entries'][0]['classification'] = 'fixture'
        self.assertTrue(any('without a recorded run' in p for p in validate(self.data)))

    def test_collection_error_without_entry_is_detected(self):
        self.data['collection_errors'][0]['kf'] = 'KF-999'
        self.assertTrue(any('unknown tracker id' in p for p in validate(self.data)))

    def test_disabled_test_is_detected(self):
        self.assertEqual(id_problems('tests.test_known_failures.SchemaTests.no_such_test'),
                         ['test not found in test_known_failures.py'])
        self.assertEqual(id_problems('tests.test_nothing.Case.test_y'), ['module file not found'])


class FinalRunRecordTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(DATA.read_text(encoding='utf-8'))

    def test_final_run_is_absent_or_complete(self):
        run = self.data['final_run']
        if run is None:
            self.skipTest('final run not recorded yet; overall result is NOT established')
        for key in RUN_FIELDS:
            self.assertIn(key, run)
        self.assertEqual(run['executed'], run['passed'] + run['failed'] + run['errors'] + run['skipped'])
        self.assertLessEqual(run['executed'], run['discovered'])
        ids = {e['id'] for e in self.data['entries']}
        self.assertLessEqual(set(run['known_failure_ids']), ids)
        if run['discovered'] > run['executed']:
            self.assertTrue(run.get('not_executed_reason'), 'unexecuted tests must be stated')


if __name__ == '__main__':
    unittest.main()
