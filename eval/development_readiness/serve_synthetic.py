"""No AI: real HTTP/UI routes with disposable synthetic records and inert workers."""
import json
import secrets
import sys
import tempfile
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from server import App, Handler
from team_config import default_config
from team_store import Store


class SafeHandler(Handler):
    def log_message(self, *args): pass

    def do_POST(self):
        if urlparse(self.path).path != '/api/jobs/evaluation':
            return self.send(403, {'error': 'Synthetic fixture: only evaluation records can be changed.'})
        return super().do_POST()


def make_fixture(directory, port=0):
    server = ThreadingHTTPServer(('127.0.0.1', port), SafeHandler)
    app = App.__new__(App)
    app.store = Store(directory)
    app.config = default_config()
    app.config['approved_roots'] = []
    app.config['provider_settings']['enabled'] = []
    app.health = {}
    app.cookie = secrets.token_urlsafe(32)
    app.port = server.server_port
    app.origin = f'http://127.0.0.1:{app.port}'
    app.engine = SimpleNamespace(paused=True)
    app.mobile = None
    app.mobile_error = None
    for name in ('usage', 'cli_update', 'claude_update', 'copilot_update', 'notifications'):
        setattr(app, name, SimpleNamespace(snapshot=lambda: {}))
    for n in range(53):
        job = app.store.put('job', {'id': f'fixture-{n}', 'title': f'架空在庫 {n:02}', 'goal': '架空の在庫集計',
                                   'project': 'synthetic', 'status': 'accepted', 'created_at': time.time()-40*86400})
        job['updated_at'] = time.time()-30*86400-n
        app.store.db.execute('UPDATE objects SET body=? WHERE id=?', (json.dumps(job), job['id']))
        app.store.put('task', {'id': f't-{n}', 'job_id': job['id'], 'title': '架空の確認', 'role': 'reviewer',
                              'status': 'succeeded', 'attempt': 1, 'instruction': '架空の入力だけ確認',
                              'result': {'status': 'done', 'summary': '架空の結果'}})
    app.store.db.commit()
    server.app = app
    return server


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='sairai-synthetic-ui-') as directory:
        server = make_fixture(directory, 8814)
        print(server.app.origin, flush=True)
        try: server.serve_forever()
        finally:
            server.server_close()
            server.app.store.db.close()
