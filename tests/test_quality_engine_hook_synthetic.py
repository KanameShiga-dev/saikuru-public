"""SQAH recording hook in the engine, with fixture adapters only: no AI, provider or normal DB."""
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from team_adapters import ADAPTERS
from team_config import default_config
from team_engine import Engine
from team_store import Store

PLAN = {'summary': '計画', 'tasks': [{'title': '確認', 'instruction': '確認する', 'role': 'researcher'}],
        'security_review_required': False}
DONE = {'status': 'done', 'summary': '完了', 'checks': ['確認：本文'], 'pending_checks': [], 'question': ''}
FINDING = {'status': 'needs_changes', 'summary': '重大度：低。表現を分けて書くとよい。', 'checks': ['確認：本文'],
           'pending_checks': [], 'question': ''}


class HookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.temp.name)
        config = default_config()
        config['approved_roots'] = [self.temp.name]
        self.engine = Engine(self.store, config, {'codex': ['fixture'], 'claude': ['fixture']}, 'http://127.0.0.1:1')
        self.saved = ADAPTERS.copy()
        self.addCleanup(self.close)
        self.reviewer_result = DONE
        test = self

        class Fixture:
            def run(s, ctx, prompt, schema):
                if ctx.task['role'] == 'planner':
                    return PLAN
                return test.reviewer_result if ctx.task['role'] == 'reviewer' else DONE
        ADAPTERS.update(codex=Fixture, claude=Fixture)

    def close(self):
        self.engine.shutdown.set()
        deadline = time.time() + 3
        while self.engine.active and time.time() < deadline:
            time.sleep(.01)
        ADAPTERS.update(self.saved)
        self.engine.handoff.close()
        self.store.db.close()

    def document_job(self, owned=True):
        job = self.engine.create_job('資料', '内部資料を作る', self.temp.name, True,
                                     document_source=self.temp.name, document_format='md')
        if owned:
            document = self.root / 'guide.md'
            document.write_text('# 案内\nok\n', encoding='utf-8')
            (self.root / ('.saikuru-owned-' + job['id'] + '.json')).write_text(
                json.dumps({'guide.md': hashlib.sha256(document.read_bytes()).hexdigest()}), encoding='utf-8')
        return job

    def run_until(self, job):
        deadline = time.time() + 30
        while self.store.get(job['id'])['status'] not in ('awaiting_acceptance', 'blocked', 'failed') \
                and time.time() < deadline:
            self.engine.tick()
            time.sleep(.01)
        deadline = time.time() + 3
        while self.engine.active and time.time() < deadline:
            time.sleep(.01)
        return self.store.get(job['id'])['status']

    def lines(self):
        path = self.store.directory / 'quality' / 'document-qa.jsonl'
        return [json.loads(l) for l in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []

    def test_reaching_acceptance_records_the_owned_deliverable(self):
        job = self.document_job()
        self.assertEqual(self.run_until(job), 'awaiting_acceptance')
        records = self.lines()
        self.assertEqual([(r['job_id'], r['file']) for r in records], [(job['id'], 'guide.md')])
        self.assertEqual(records[0]['overall'], 'passed')

    def test_a_failing_recorder_never_holds_up_acceptance(self):
        job = self.document_job()
        with mock.patch('team_quality_document.record_job_deliverables', side_effect=RuntimeError('boom')):
            self.assertEqual(self.run_until(job), 'awaiting_acceptance')
        self.assertEqual(self.lines(), [])
        self.assertTrue(any(a['kind'] == 'completion' for a in self.store.all('approval')))

    def test_nothing_is_recorded_without_a_document_source_or_a_manifest(self):
        self.engine._record_document_quality({'id': 'job-x', 'project': self.temp.name})
        self.engine._record_document_quality({'id': 'job-x', 'project': self.temp.name, 'document_source': self.temp.name})
        self.engine._record_document_quality({})
        self.assertEqual(self.lines(), [])

    def test_accepting_a_stopped_job_without_a_fix_also_records(self):
        # This board action exists only when an administrator enables the relaxation (off by default).
        self.engine.config['document_review_relaxed'] = True
        self.reviewer_result = FINDING
        job = self.document_job()
        self.assertEqual(self.run_until(job), 'blocked')
        self.assertEqual(self.lines(), [])           # stopped at the repair limit: nothing is offered yet
        self.engine.accept_without_fix(job['id'])
        self.assertEqual(self.store.get(job['id'])['status'], 'awaiting_acceptance')
        self.assertEqual([r['file'] for r in self.lines()], ['guide.md'])

    def test_recording_leaves_the_deliverable_and_the_approval_flow_alone(self):
        job = self.document_job()
        document = self.root / 'guide.md'
        before = document.read_bytes()
        self.assertEqual(self.run_until(job), 'awaiting_acceptance')
        self.assertEqual(document.read_bytes(), before)
        completions = [a for a in self.store.all('approval') if a['kind'] == 'completion' and a['status'] == 'pending']
        self.assertEqual(len(completions), 1)


if __name__ == '__main__':
    unittest.main()
