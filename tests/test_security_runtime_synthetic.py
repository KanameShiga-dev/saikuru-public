"""Local guard checks using harmless text and simulated classifications only."""
import base64
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from consultation_read_tools import bootstrap, execute, screen_model_read
from team_attachments import Attachments, screen_sensitive
from team_engine import Context
from team_outbound_guard import native_commands


class FakeStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.events = []
        self.task = {'status': 'running'}

    def get(self, ident, kind=None):
        if kind == 'job':
            return {'status': 'running', 'route': 'full'}
        if kind == 'approval':
            return {'status': 'denied', 'note': 'synthetic decision', 'native_answers': {}}
        return self.task

    def update(self, ident, **changes):
        self.task.update(changes)
        return self.task

    def event(self, *args):
        self.events.append(args)


class FakeEngine:
    def __init__(self):
        self.store = FakeStore()
        self.config = {'automatic_operations': True}
        self.approvals = []

    def new_approval(self, task, kind, payload):
        self.approvals.append(payload)
        return {'id': 'synthetic-approval'}


class FakeAttachmentStore:
    def __init__(self, root):
        self.directory = Path(root)
        self.lock = threading.RLock()
        self.items = {}

    def put(self, kind, item):
        self.items[item['id']] = item

    def get(self, ident, kind=None):
        return self.items[ident]

    def update(self, ident, **changes):
        self.items[ident].update(changes)
        return self.items[ident]


class GuardSyntheticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='saikuru-guard-synthetic-')
        self.addCleanup(self.temp.cleanup)
        self.engine = FakeEngine()
        self.ctx = Context.__new__(Context)
        self.ctx.engine = self.engine
        self.ctx.task = {'id': 'synthetic-task', 'job_id': 'synthetic-job'}
        self.ctx.project = self.temp.name
        self.ctx.cancel = threading.Event()
        self.ctx.check = lambda: None

    def test_codex_command_needs_human_even_with_auto_operations(self):
        payload = {'source': 'codex', 'operation': 'item/commandExecution/requestApproval',
                   'details': {'command': 'python -c "print(1)"'}}
        with patch('team_audit.record'):
            result = self.ctx.approve(payload)
        self.assertFalse(result['allow'])  # simulated person declined
        self.assertEqual(len(self.engine.approvals), 1)
        self.assertTrue(self.engine.approvals[0]['force_manual'])
        self.assertFalse(any(event[0] == 'auto_approved' for event in self.engine.store.events))

    def test_unknown_or_simulated_denied_native_command_stops(self):
        payload = {'source': 'codex', 'operation': 'item/commandExecution/requestApproval',
                   'details': {}}
        self.assertFalse(self.ctx.approve(payload)['allow'])
        payload['details'] = {'command': 'Write-Output ok'}
        with patch('team_outbound_guard.classify', return_value=('credential', 'synthetic', 'deny')):
            with patch('team_audit.record'):
                self.assertFalse(self.ctx.approve(payload)['allow'])
        self.assertEqual(self.engine.approvals, [])

    def test_codex_file_change_scope_and_manual_approval(self):
        payload = {'source': 'codex', 'operation': 'item/fileChange/requestApproval',
                   'details': {}, 'fileChanges': [{'path': '../outside.txt'}]}
        self.assertFalse(self.ctx.approve(payload)['allow'])
        self.assertEqual(self.engine.approvals, [])
        payload['fileChanges'] = [{'path': 'draft.txt'}]
        self.assertFalse(self.ctx.approve(payload)['allow'])
        self.assertTrue(self.engine.approvals[0]['force_manual'])

    def test_attachments_and_project_reads_reject_simulated_classification(self):
        screen_sensitive('draft.txt', 'ordinary notes')
        self.assertEqual(screen_model_read({'content': 'ordinary notes'}), {'content': 'ordinary notes'})
        with patch('team_dlp.scan_input', return_value={'block': [], 'confirm': ['synthetic category']}):
            with self.assertRaises(ValueError):
                screen_sensitive('draft.txt', 'ordinary notes')
            with self.assertRaises(ValueError):
                screen_model_read({'content': 'ordinary notes'})

    def test_native_command_shape_fails_closed(self):
        self.assertEqual(native_commands({'command': ['python', '-V']}), ['python -V'])
        self.assertEqual(native_commands({'commandActions': [{'unexpected': 'shape'}]}), [])

    def test_attachment_pipeline_uses_only_local_synthetic_note(self):
        attachments = Attachments(FakeAttachmentStore(self.temp.name))
        item = attachments.upload({'name': 'draft.txt',
            'data': base64.b64encode(b'ordinary notes for a sample task').decode('ascii')})
        self.assertEqual(len(attachments.select([item['id']])), 1)
        blocks = attachments.content([item['id']], 'codex')
        self.assertEqual(blocks[0]['type'], 'text')
        self.assertIn('ordinary notes for a sample task', blocks[0]['text'])

    def test_project_read_pipeline_uses_only_local_synthetic_note(self):
        root = Path(self.temp.name)
        (root / 'draft.txt').write_text('ordinary notes for a sample task', encoding='utf-8')
        value = execute(root, 'read_file', {'path': 'draft.txt'})
        self.assertIn('ordinary notes for a sample task', screen_model_read(value)['content'])
        report = bootstrap(root, 'sample task')
        self.assertEqual(report['status'], 'reviewed')


if __name__ == '__main__':
    unittest.main()
