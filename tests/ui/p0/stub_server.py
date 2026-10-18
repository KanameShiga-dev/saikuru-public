"""Read-only local stub that serves web/ and a synthetic /api/state for the P0 UI checks.

No real data, provider, token or database is used. It listens on 127.0.0.1 only and rejects every mutation.
Fields that the real API always provides but the minimal fixture omits are filled with obvious synthetic
defaults (see FILL_*); each fill is a gap to report to the contract owner, not a contract.

    python -X utf8 tests/ui/p0/stub_server.py [--port 8891] [--state tests/fixtures/p0/state.json]
"""
import argparse
import copy
import json
import mimetypes
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WEB = ROOT / 'web'
DEFAULT_STATE = ROOT / 'tests' / 'fixtures' / 'p0' / 'state.json'

FILL_TASK = {'attempt': 0, 'summary': '', 'result': None, 'after': None, 'instruction': '（架空）',
             'profile': {'adapter': 'codex', 'model': 'synthetic-model', 'effort': 'medium'}}
FILL_JOB = {'project': 'C:/synthetic/project', 'warning_issues': [], 'artifacts': []}


def complete(state):
    state = copy.deepcopy(state)
    now = time.time()
    for job in state.get('jobs', []):
        for key, value in FILL_JOB.items():
            job.setdefault(key, copy.deepcopy(value))
        job.setdefault('created_at', now - 600)
        job.setdefault('updated_at', now - 60)
    for task in state.get('tasks', []):
        for key, value in FILL_TASK.items():
            task.setdefault(key, copy.deepcopy(value))
        task.setdefault('updated_at', now - 60)
    state.setdefault('approvals', [])
    state.update({'paused': False, 'events': [], 'providers': {}, 'usage': {},
                  'history': {'days': 7, 'hidden_jobs': 0}, 'cli_update': {}, 'claude_update': {}, 'copilot_update': {},
                  'config': {'roles': {}, 'profiles': {}, 'provider_settings': {'enabled': ['codex', 'claude']},
                             'max_repairs': 2, 'approved_roots': ['C:/synthetic']}})
    return state


class Handler(BaseHTTPRequestHandler):
    state = {}

    def log_message(self, *args):
        pass

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path == '/api/state':
            return self.send_json(self.state)
        if path == '/api/jobs/previews':
            return self.send_json({'files': []})
        if path.startswith('/api/'):
            return self.send_json({})
        name = 'index.html' if path == '/' else path.lstrip('/')
        target = (WEB / name).resolve()
        if WEB.resolve() not in target.parents or not target.is_file():
            return self.send_json({'error': 'not found'}, 404)
        body = target.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', (mimetypes.guess_type(target.name)[0] or 'text/plain') + '; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.send_json({'error': '確認用スタブのため、変更は受け付けません。'}, 405)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8891)
    parser.add_argument('--state', default=str(DEFAULT_STATE))
    args = parser.parse_args(argv)
    Handler.state = complete(json.loads(Path(args.state).read_text(encoding='utf-8')))
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print('stub on http://127.0.0.1:%d' % args.port, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
