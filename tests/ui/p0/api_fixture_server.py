"""Serve the integrated UI through real P0 APIs with disposable fictional data.

Providers and the work engine stay disabled. POST is denied by default; the
explicit action mode permits selected user-decision APIs against the temp DB.
"""

import argparse
import hashlib
import json
import secrets
import sys
import tempfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from server import App, DataLock, Handler
from team_attachments import Attachments
from team_config import default_config
from team_engine import Engine
from team_ledger import ROOT as LEDGER_ROOT
from team_p0_contract import artifact_records
from team_store import Store


def prepare_action_fixture(app, project):
    """Turn display-only examples into valid, isolated decision paths."""
    store, engine = app.store, app.engine

    # The real G2 gate binds approval, version and fingerprint to a readable scope.
    (project / 'SCOPE.md').write_text('Fictional project for isolated UI checks only.\n', encoding='utf-8')
    store.update('synthetic-g2-task', status='queued')
    engine._request_g2('synthetic-g2-job', 'synthetic-g2-task', '架空の操作確認用')

    # A G3 decision must have a completed planner result with concrete work.
    plan = {
        'summary': '架空の検索画面を作る計画',
        'security_review_required': False,
        'goal_contract': {
            'primary_goal': '架空の検索画面を作る',
            'deliverables': ['app/index.html'],
            'acceptance_criteria': ['架空のIDを入力して結果を確認できる'],
            'non_goals': [], 'constraints': ['架空データのみ'],
        },
        'tasks': [{'title': '架空の検索画面を作る',
                   'instruction': '架空データだけを使って検索画面を作る',
                   'role': 'builder',
                   'goal_links': ['app/index.html', '架空のIDを入力して結果を確認できる']}],
        'code_test_plan': {'required': True, 'reason': '画面の作成',
                           'cases': ['未知のIDは結果なし'],
                           'command': 'python -B -X utf8 -m unittest test_lookup',
                           'existing_tests': []},
    }
    store.update('synthetic-g3-task', status='succeeded', result=plan)
    store.update('synthetic-g3-approval', status='superseded')
    engine.new_approval(store.get('synthetic-g3-task', 'task'), 'plan', plan)

    # A question pauses the task; its approval belongs to that exact attempt.
    question_task = store.update('synthetic-question-task', status='blocked')
    question = store.get('synthetic-question-approval', 'approval')
    question_payload = json.dumps(question['payload'], ensure_ascii=False, sort_keys=True)
    store.update(question['id'], attempt=question_task['attempt'],
                 digest=hashlib.sha256(question_payload.encode('utf-8')).hexdigest())

    # Receipt requires completed normal and security reviews plus a signed payload.
    receipt_job = store.get('synthetic-receipt-job', 'job')
    for name, agent in [('通常レビュー', None), ('安全確認', 'common-security-reviewer')]:
        reviewer = engine.new_task(receipt_job, name, '架空の成果物を確認する',
                                   'reviewer', 'synthetic-receipt-task', agent_name=agent)
        store.update(reviewer['id'], status='succeeded',
                     result={'status': 'done', 'summary': '架空の確認用レビューが完了',
                             'checks': [], 'pending_checks': []})
    receipt_task = store.get('synthetic-receipt-task', 'task')
    receipt = store.get('synthetic-receipt-approval', 'approval')
    payload_text = json.dumps(receipt['payload'], ensure_ascii=False, sort_keys=True)
    store.update(receipt['id'], attempt=receipt_task['attempt'],
                 digest=hashlib.sha256(payload_text.encode('utf-8')).hexdigest())


class ReadOnlyHandler(Handler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.send(405, {'error': '架空データの画面確認用です。変更は受け付けません。'})

    def do_GET(self):
        allowed = {'/', '/health', '/app.js', '/style.css', '/theme.css',
                   '/shell.js', '/attachments.js', '/skill-picker.js',
                   '/api/state', '/api/notifications', '/api/tasks/detail',
                   '/api/job/p0-artifact', '/api/instruction-health', '/api/skills'}
        if urlparse(self.path).path not in allowed:
            return self.send(403, {'error': '架空データの画面確認用です。'})
        return super().do_GET()


class FixtureActionHandler(ReadOnlyHandler):
    """Exercise existing user-decision APIs against the disposable fictional DB only."""

    ALLOWED_POSTS = {'/api/decide', '/api/completion/evidence',
                     '/api/jobs/review-confirmation', '/api/jobs/formal-acceptance',
                     '/api/jobs/rework', '/api/cancel', '/api/jobs'}

    def do_POST(self):
        if urlparse(self.path).path not in self.ALLOWED_POSTS:
            return self.send(405, {'error': '架空データの確認用です。この操作は受け付けません。'})
        return Handler.do_POST(self)


def make_server(port, base, allow_fixture_actions=False):
    project = base / 'project'
    project.mkdir()
    handler = FixtureActionHandler if allow_fixture_actions else ReadOnlyHandler
    server = ThreadingHTTPServer(('127.0.0.1', port), handler)
    app = App.__new__(App)
    try:
        app.data_lock = DataLock(base / 'runtime')
        app.store = Store(base / 'runtime')
        app.config = default_config()
        app.config['approved_roots'] = [str(project.resolve())]
        app.config['provider_settings']['enabled'] = []
        app.config['decision']['provider'] = 'disabled'
        app.port = server.server_port
        app.origin = f'http://127.0.0.1:{app.port}'
        app.cookie = secrets.token_urlsafe(32)
        app.health = {}
        app.mobile = app.mobile_error = None
        app.engine = Engine(app.store, app.config, {}, app.origin)
        app.engine.paused = True
        app.attachments = Attachments(app.store)
        for name in ('usage', 'cli_update', 'claude_update', 'copilot_update', 'notifications'):
            setattr(app, name, SimpleNamespace(snapshot=lambda: {}))
        fixture = json.loads((ROOT / 'tests/fixtures/p0/state.json').read_text(encoding='utf-8'))
        for job in fixture['jobs']:
            row = {key: value for key, value in job.items() if key != 'p0_progress'}
            row['project'] = str(project.resolve())
            if row.get('p0_artifacts'):
                paths = [item['path'] for item in row['p0_artifacts']]
                for name in paths:
                    target = project / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not target.exists():
                        target.write_bytes(b'<!doctype html><title>Fictional artifact</title>\n'
                                           b'<p>Fictional lookup preview only.</p>\n')
                row['p0_artifacts'] = artifact_records(project, paths)
            app.store.put('job', row)
        for task in fixture['tasks']:
            row = dict(task)
            row.setdefault('summary', '')
            row.setdefault('result', None)
            row.setdefault('instruction', '（架空の確認用作業）')
            row.setdefault('profile', {'adapter': 'codex', 'model': 'synthetic-model',
                                       'effort': 'medium'})
            app.store.put('task', row)
        for approval in fixture['approvals']:
            app.store.put('approval', approval)
        if allow_fixture_actions:
            prepare_action_fixture(app, project)
        server.app = app
        return server
    except BaseException:
        server.server_close()
        if hasattr(app, 'engine'):
            app.engine.handoff.close()
        if hasattr(app, 'store'):
            app.store.db.close()
        if hasattr(app, 'data_lock'):
            app.data_lock.close()
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--allow-fixture-actions', action='store_true',
                        help='Allow selected real job and decision APIs against disposable fictional data only')
    args = parser.parse_args()
    temp_parent = None
    if args.allow_fixture_actions:
        temp_parent = ROOT.parent.resolve()
        if not temp_parent.is_relative_to(LEDGER_ROOT.resolve()):
            raise ValueError('操作確認モードは C:/AI_Work 内の統合作業領域から起動してください。')
    with tempfile.TemporaryDirectory(prefix='saikuru-p0-ui-api-', dir=temp_parent) as folder:
        server = make_server(args.port, Path(folder), args.allow_fixture_actions)
        print(server.app.origin, flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()
            server.app.engine.shutdown.set()
            server.app.engine.handoff.close()
            server.app.store.db.close()
            server.app.data_lock.close()


if __name__ == '__main__':
    main()
