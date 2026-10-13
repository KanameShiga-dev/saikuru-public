"""Persistent synthetic human reacceptance UI; never discovers or runs a provider."""
import argparse
import json
import secrets
import sys
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from server import App, DataLock, Handler
from team_config import default_config
from team_engine import Engine
from team_store import Store


class FixtureHandler(Handler):
    def log_message(self, *args): pass

    def do_GET(self):
        allowed = {'/', '/health', '/app.js', '/style.css', '/theme.css', '/shell.js',
                   '/attachments.js', '/skill-picker.js', '/api/state', '/api/mobile',
                   '/api/tasks/detail', '/api/handoff', '/api/jobs/evaluation', '/api/jobs/archive'}
        if urlparse(self.path).path not in allowed:
            return self.send(403, {'error': 'この隔離画面では受領確認だけを行えます。'})
        return super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path not in {'/api/decide', '/api/completion/evidence', '/api/jobs/evaluation'}:
            return self.send(403, {'error': 'この隔離画面では受領確認だけを行えます。'})
        return super().do_POST()


def make_fixture(base, port=0, project_dir='project'):
    base = Path(base)
    runtime, project = base/'runtime', base/project_dir
    project.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', port), FixtureHandler)
    app = App.__new__(App)
    try:
        app.data_lock = DataLock(runtime)
        app.store = Store(runtime)
        app.config = default_config()
        app.config['approved_roots'] = [str(project.resolve())]
        app.config['provider_settings']['enabled'] = []
        app.config['local_summary']['mode'] = 'off'
        app.port = server.server_port
        app.origin = f'http://127.0.0.1:{app.port}'
        app.cookie = secrets.token_urlsafe(32)
        app.health = {}
        app.mobile = app.mobile_error = None
        app.engine = Engine(app.store, app.config, {}, app.origin)
        app.engine.paused = True
        for name in ('usage', 'cli_update', 'claude_update', 'copilot_update', 'notifications'):
            setattr(app, name, SimpleNamespace(snapshot=lambda: {}))
        if not app.store.all('job'):
            job = app.store.put('job', {'id': 'synthetic-reacceptance', 'title': '架空案件：証拠追加と再受領',
                'goal': '架空の受領経路だけを確認します。AIによる開発や採点は実施していません。',
                'project': str(project.resolve()), 'status': 'awaiting_acceptance',
                'created_at': time.time(), 'auto_execute': False})
            task = app.engine.new_task(job, '架空メモの表示確認', '受領経路を確認する合成カード。AI実行はありません。', 'reviewer')
            task = app.store.update(task['id'], status='succeeded', result={
                'status': 'done', 'summary': '架空メモ：確認用。これは表示確認用の合成結果です。AIレビューや採点の合格ではありません。',
                'checks': [], 'pending_checks': ['カードに「架空メモ：確認用」と表示されることを本人が確認する']})
            approval = app.engine.new_approval(task, 'completion', {
                'summary': '架空メモ：確認用。最初は未確認項目を残して受領し、その後で根拠を追加してください。',
                'checks': [], 'review_findings': [],
                'pending_items': [{'title': '本人による表示確認', 'note': '架空の表示だけを確認します。',
                                   'checks': ['カードに「架空メモ：確認用」と表示されることを本人が確認する']}],
                'note': '独立した一時評価です。通常サービスの記録には影響しません。'})
            (base/'manifest.json').write_text(json.dumps({'purpose': 'synthetic-reacceptance-human',
                'job_id': job['id'], 'task_id': task['id'], 'initial_approval_id': approval['id'],
                'ai_calls': 0, 'initial_state': 'awaiting_acceptance', 'fixture_seed_is_human_evidence': False},
                ensure_ascii=False, indent=2), encoding='utf-8')
        server.app = app
        return server
    except BaseException:
        server.server_close()
        if hasattr(app, 'engine'): app.engine.handoff.close()
        if hasattr(app, 'store'): app.store.db.close()
        if hasattr(app, 'data_lock'): app.data_lock.close()
        raise


def close(server):
    server.server_close()
    server.app.engine.shutdown.set()
    server.app.engine.handoff.close()
    server.app.store.db.close()
    server.app.data_lock.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8816)
    args = parser.parse_args()
    base = args.base.resolve()
    if base.parent != (ROOT/'data').resolve() or not base.name.startswith('reacceptance-human-'):
        raise ValueError('Use a dedicated reacceptance-human-* directory directly under data')
    if base.exists() and not (base/'manifest.json').exists():
        raise ValueError('Existing directory is not a resumable fixture')
    if (base/'manifest.json').exists():
        manifest = json.loads((base/'manifest.json').read_text(encoding='utf-8'))
        if manifest.get('purpose') != 'synthetic-reacceptance-human':
            raise ValueError('Fixture purpose mismatch')
    server = make_fixture(base, args.port)
    print(server.app.origin, flush=True)
    try: server.serve_forever()
    finally: close(server)
