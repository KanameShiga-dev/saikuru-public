"""Serve the integrated UI through the real P0 state API with fictional data.

All providers are disabled. The runtime is temporary, and every POST is denied.
Use this for display and navigation checks, not for approval or execution tests.
"""

import argparse
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
from team_config import default_config
from team_engine import Engine
from team_store import Store


class ReadOnlyHandler(Handler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.send(405, {'error': '架空データの画面確認用です。変更は受け付けません。'})

    def do_GET(self):
        allowed = {'/', '/health', '/app.js', '/style.css', '/theme.css',
                   '/shell.js', '/attachments.js', '/skill-picker.js',
                   '/api/state', '/api/notifications', '/api/tasks/detail'}
        if urlparse(self.path).path not in allowed:
            return self.send(403, {'error': '架空データの画面確認用です。'})
        return super().do_GET()


def make_server(port, base):
    project = base / 'project'
    project.mkdir()
    server = ThreadingHTTPServer(('127.0.0.1', port), ReadOnlyHandler)
    app = App.__new__(App)
    try:
        app.data_lock = DataLock(base / 'runtime')
        app.store = Store(base / 'runtime')
        app.config = default_config()
        app.config['approved_roots'] = [str(project.resolve())]
        app.config['provider_settings']['enabled'] = []
        app.port = server.server_port
        app.origin = f'http://127.0.0.1:{app.port}'
        app.cookie = secrets.token_urlsafe(32)
        app.health = {}
        app.mobile = app.mobile_error = None
        app.engine = Engine(app.store, app.config, {}, app.origin)
        app.engine.paused = True
        for name in ('usage', 'cli_update', 'claude_update', 'copilot_update', 'notifications'):
            setattr(app, name, SimpleNamespace(snapshot=lambda: {}))
        fixture = json.loads((ROOT / 'tests/fixtures/p0/state.json').read_text(encoding='utf-8'))
        for job in fixture['jobs']:
            row = {key: value for key, value in job.items() if key != 'p0_progress'}
            row['project'] = str(project.resolve())
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
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='saikuru-p0-ui-api-') as folder:
        server = make_server(args.port, Path(folder))
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
