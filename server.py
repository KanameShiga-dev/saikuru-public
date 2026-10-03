"""Loopback-only dashboard and worker bridge. Python 3.11+, standard library only."""
import argparse
import hmac
import json
import mimetypes
import os
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from team_config import ROOT, discover, environment_status, load_config, validate_config
from team_engine import Engine, canonical
from team_store import Store
from team_usage import UsageMonitor
from team_folders import listing, create as create_folder, project_folder
from team_models import ModelCatalog
from mobile_gateway import start_gateway, wifi_address
from team_ledger import Ledger
from team_archive import Archives
from team_move import Moves
from team_cli_update import CliUpdateMonitor
from team_harness import Harnesses
from team_ui_automation import UiAutomation
from team_consultation import Consultations


class DataLock:
    def __init__(self, directory):
        Path(directory).mkdir(parents=True, exist_ok=True)
        self.file = (Path(directory) / 'instance.lock').open('a+b')
        if self.file.tell() == 0:
            self.file.write(b'0'); self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('同じデータフォルダを使用する統括サービスが既に起動しています。')

    def close(self):
        self.file.close()


class App:
    def __init__(self, directory, port):
        self.data_lock = DataLock(directory)
        self.store = Store(directory)
        self.ledger = Ledger(directory)
        self.harnesses = Harnesses(self.ledger)
        self.ui_automation = UiAutomation(directory, settings=lambda: self.config.get('decision', {}))
        self.config = load_config(directory)
        self.commands = discover()
        self.models = ModelCatalog(self.commands)
        self.health = environment_status(self.commands)
        self.port = port
        self.origin = f'http://127.0.0.1:{port}'
        self.cookie = secrets.token_urlsafe(32)
        self.engine = Engine(self.store, self.config, self.commands, self.origin)
        self.consultations = Consultations(self)
        self.archives = Archives(self.ledger, self.engine)
        self.moves = Moves(self.ledger, self.engine, self.archives)
        self.engine.project_blocked = lambda path: self.archives.blocked(path) or self.moves.blocked(path)
        for job in self.store.all('job'):
            self.engine.sync_handoff(job['id'])
        self.usage = UsageMonitor(self.commands, self.engine.shutdown)
        self.engine.usage_snapshot = self.usage.snapshot
        self.cli_update = CliUpdateMonitor(self, 'codex')
        self.claude_update = CliUpdateMonitor(self, 'claude')
        self.mobile = None
        self.mobile_error = None
        from team_notifications import Notifications
        self.notifications = Notifications(self)

    def refresh_claude_auth(self):
        self.health.update(environment_status({'claude': self.commands.get('claude')}))
        if self.health.get('claude', {}).get('authenticated'):
            self.usage.refresh_now('claude')

    def auth_loop(self):
        while not self.engine.shutdown.wait(60):
            previous = self.health.get('claude', {}).get('authenticated')
            current = environment_status({'claude': self.commands.get('claude')})
            self.health.update(current)
            if current.get('claude', {}).get('authenticated') and previous is not True:
                self.usage.refresh_now('claude')

    def save_config(self, config):
        validate_config(config)
        with self.store.lock:
            path = self.store.directory / 'config.json'
            backup = self.store.directory / ('config-backup-' + str(time.time_ns()) + '.json')
            backup.write_bytes(path.read_bytes())
            temp = path.with_suffix('.tmp')
            temp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
            os.replace(temp, path)
            self.config = config
            self.engine.config = config
            self.store.event('config_changed', '統括サービスの設定を更新しました。実行中タスクは元のプロファイルを継続します。')

    def state(self):
        return {'jobs': self.store.all('job'), 'tasks': self.store.all('task'),
                'approvals': self.store.all('approval'), 'events': self.store.events(),
                'config': self.config, 'providers': self.health, 'paused': self.engine.paused,
                'sample_project': str(ROOT / 'sample-project'), 'server_time': time.time(),
                'usage': self.usage.snapshot(), 'cli_update': self.cli_update.snapshot(),
                'claude_update': self.claude_update.snapshot(), 'notifications': self.notifications.snapshot()}


class Handler(BaseHTTPRequestHandler):
    server_version = 'AgentTeam/0.1'

    def log_message(self, *_):
        pass  # Requests can contain personal task details; don't write access logs.

    @property
    def app(self):
        return self.server.app

    def common(self):
        if self.headers.get('Host') != f'127.0.0.1:{self.app.port}':
            raise PermissionError('無効なホストです。127.0.0.1のURLを使用してください。')
        origin = self.headers.get('Origin')
        if origin and origin != self.app.origin:
            raise PermissionError('他のサイトからの要求は拒否しました。')
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise PermissionError('他のサイトからの要求は拒否しました。')

    def authorized(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
            actual = cookie['agent_team'].value
        except (KeyError, ValueError):
            return False
        return hmac.compare_digest(actual, self.app.cookie)

    def send(self, status, value, content_type='application/json; charset=utf-8', set_cookie=False):
        body = json.dumps(value, ensure_ascii=False).encode() if not isinstance(value, bytes) else value
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        if set_cookie:
            self.send_header('Set-Cookie', f'agent_team={self.app.cookie}; HttpOnly; SameSite=Strict; Path=/')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            self.common()
            path = urlparse(self.path).path
            if path == '/health':
                return self.send(200, {'status': 'ok', 'app': 'agent-team', 'version': '0.1.0'})
            if path == '/api/project-history':
                if not self.authorized():
                    raise PermissionError('画面を再読み込みしてください。')
                from team_history import snapshot
                query = parse_qs(urlparse(self.path).query)
                return self.send(200, snapshot(self.app, (query.get('job_id') or [''])[0],
                    max(0, int((query.get('before') or ['0'])[0]))))
            if path == '/api/decision-wait-stats':
                if not self.authorized():
                    raise PermissionError('画面を再読み込みしてください。')
                from team_waits import statistics
                query = parse_qs(urlparse(self.path).query)
                try:
                    start = float(query['from'][0]) if query.get('from') else None
                    end = float(query['to'][0]) if query.get('to') else None
                    result = statistics(self.app.store, self.app.config.get('business_hours'), start, end)
                except ValueError as exc:
                    return self.send(400, {'error':str(exc)})
                return self.send(200, result)
            if path == '/api/state':
                if not self.authorized():
                    raise PermissionError('画面を再読み込みしてください。')
                return self.send(200, self.app.state())
            if path == '/api/notifications':
                if not self.authorized():
                    raise PermissionError('画面を再読み込みしてください。')
                return self.send(200, self.app.notifications.snapshot())
            if path == '/api/job/artifact':
                if not self.authorized():
                    raise PermissionError('画面を再読み込みしてください。')
                query=parse_qs(urlparse(self.path).query)
                job=self.app.store.get((query.get('job_id') or [''])[0],'job')
                relative=(query.get('path') or [''])[0]
                if not any(a.get('path')==relative for a in job.get('artifacts',[])):
                    raise ValueError('登録された成果物を選択してください。')
                from consultation_read_tools import safe_path
                artifact=safe_path(Path(job['project']).resolve(),relative)
                return self.send(200,{'path':relative,'content':artifact.read_text(encoding='utf-8')})
            if path == '/api/ledger/consultation/models':
                if not self.authorized():
                    raise PermissionError('画面を再読み込みしてください。')
                query = parse_qs(urlparse(self.path).query)
                return self.send(200, self.app.consultations.choices(
                    query.get('adapter', ['codex'])[0], query.get('refresh', ['0'])[0] == '1'))
            if path == '/api/ledger/consultation':
                if not self.authorized():
                    raise PermissionError('画面を再読み込みしてください。')
                ident = parse_qs(urlparse(self.path).query).get('id', [''])[0]
                return self.send(200, self.app.consultations.get(ident))
            if path == '/api/ui-automation/history':
                if not self.authorized():
                    raise PermissionError('画面を再読み込みしてください。')
                return self.send(200, self.app.ui_automation.history())
            if path in ('/api/ledger', '/api/ledger/export'):
                if not self.authorized():
                    raise PermissionError('画面を再読み込みしてください。')
                return self.send(200, self.app.ledger.snapshot(self.app.config['approved_roots']))
            if path == '/api/ledger/preview-status':
                if not self.authorized():
                    raise PermissionError('画面を再読み込みしてください。')
                query = parse_qs(urlparse(self.path).query)
                return self.send(200, self.app.archives.preview_status((query.get('id') or [''])[0]))
            if path == '/api/ledger/move/preview-status':
                if not self.authorized():
                    raise PermissionError('画面を再読み込みしてください。')
                query = parse_qs(urlparse(self.path).query)
                return self.send(200, self.app.moves.preview_status((query.get('id') or [''])[0]))
            if path == '/api/handoff':
                if not self.authorized():
                    raise PermissionError('画面を再読み込みしてください。')
                query = parse_qs(urlparse(self.path).query)
                job_id = (query.get('job') or [''])[0]
                job = self.app.store.get(job_id, 'job')
                self.app.engine.sync_handoff(job_id)
                before = (query.get('before') or [''])[0]
                before = int(before) if before.isdigit() else None
                return self.send(200, {'records':self.app.engine.handoff.history(job_id,job['project'],before),
                    'conflicts':self.app.engine.handoff.pending_conflicts(job_id),
                    'facts':self.app.engine.handoff.current_facts(job_id)})
            if path == '/api/mobile':
                if not self.authorized():
                    raise PermissionError('画面を再読み込みしてください。')
                gateway = self.app.mobile
                return self.send(200, {'available': gateway is not None,
                    'url': gateway.origin if gateway else None,
                    'code': gateway.secret['code'] if gateway else None,
                    'fingerprint': gateway.fingerprint if gateway else None,
                    'error': self.app.mobile_error})
            files = {'/history': 'history.html', '/history.js': 'history.js', '/history.css': 'history.css', '/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css', '/theme.css': 'theme.css', '/shell.js': 'shell.js',
                     '/operation-tests': 'operation-tests.html', '/operation-tests.js': 'operation-tests.js',
                     '/ledger': 'ledger.html', '/ledger.js': 'ledger.js', '/ledger-consultation.js': 'ledger-consultation.js', '/ledger.css': 'ledger.css'}
            if path not in files:
                return self.send(404, {'error': '見つかりません。'})
            file = ROOT / 'web' / files[path]
            return self.send(200, file.read_bytes(), (mimetypes.guess_type(file)[0] or 'text/plain') + '; charset=utf-8', path in ('/', '/ledger', '/operation-tests', '/history'))
        except PermissionError as exc:
            self.send(403, {'error': str(exc)})
        except (OSError, ValueError):
            self.send(500, {'error': 'ファイルを読み込めませんでした。'})

    def do_POST(self):
        try:
            self.common()
            path = urlparse(self.path).path
            size = int(self.headers.get('Content-Length', '0'))
            max_size = 300000 if path in ('/api/harness/apply', '/api/harness/structure-preview') else 100000
            if not 0 < size <= max_size or self.headers.get_content_type() != 'application/json':
                raise ValueError('JSONリクエストのサイズまたは形式が不正です。')
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError('JSONオブジェクトが必要です。')
            if path == '/worker/tool':
                token = self.headers.get('Authorization', '').removeprefix('Bearer ')
                consultation = self.app.consultations.tool_request(token, body)
                if consultation is not None:
                    return self.send(200, consultation)
                return self.send(200, self.app.engine.tool_request(token, body))
            if not self.authorized() or self.headers.get('X-Agent-Team-UI') != '1':
                raise PermissionError('ブラウザからの認証済み操作が必要です。')
            engine = self.app.engine
            if path == '/api/ledger/consultation/open':
                result = self.app.consultations.open(body)
            elif path == '/api/ledger/consultation/send':
                result = self.app.consultations.send(body)
            elif path == '/api/ledger/consultation/cancel':
                result = self.app.consultations.cancel(body)
            elif path == '/api/ledger/consultation/submit':
                result = self.app.consultations.submit(body)
            elif path == '/api/ui-automation/start':
                result = self.app.ui_automation.start(body)
            elif path == '/api/ui-automation/next':
                result = self.app.ui_automation.next(body)
            elif path == '/api/ui-automation/cancel':
                result = self.app.ui_automation.cancel(body)
            elif path == '/api/harness/preview':
                result = self.app.harnesses.preview(body)
            elif path == '/api/harness/fill-preview':
                result = self.app.harnesses.fill_preview(body)
            elif path == '/api/harness/fill-apply':
                result = self.app.harnesses.fill_apply(body)
            elif path == '/api/harness/structure-preview':
                result = self.app.harnesses.structure_preview(body)
            elif path == '/api/harness/apply':
                result = self.app.harnesses.apply(body)
            elif path == '/api/codex-usage/refresh':
                success = self.app.usage.refresh_now('codex')
                result = {'success': success, 'usage': self.app.usage.snapshot()['codex']}
            elif path == '/api/notifications':
                if set(body) != {'windows_enabled'} or type(body['windows_enabled']) is not bool:
                    raise ValueError('通知の有効・無効を指定してください。')
                config = dict(self.app.config)
                config['notifications'] = {'windows_enabled': body['windows_enabled'], 'business_hours_only': True}
                self.app.save_config(config)
                result = self.app.notifications.snapshot()
            elif path == '/api/notifications/test':
                from team_notifications import windows_summary
                windows_summary({'decision':0,'completion':0,'accepted':0}, preview=True)
                result = {'ok':True,'note':'Windowsへテスト通知を送信しました。実際の表示は端末で確認してください。'}
            elif path == '/api/claude-auth/refresh':
                self.app.refresh_claude_auth()
                result = {'authenticated': self.app.health.get('claude', {}).get('authenticated')}
            elif path == '/api/ledger/update':
                result = self.app.ledger.update(body)
            elif path == '/api/ledger/add':
                result = self.app.ledger.add(body)
            elif path == '/api/ledger/scan':
                result = self.app.ledger.scan()
            elif path == '/api/ledger/backup':
                result = {'backup': self.app.ledger.backup()}
            elif path == '/api/ledger/archive/preview':
                result = self.app.archives.request_preview(dict(body, operation='archive'))
            elif path == '/api/ledger/archive/start':
                result = self.app.archives.start(body)
            elif path == '/api/ledger/delete/preview':
                result = self.app.archives.request_preview(dict(body, operation='delete'))
            elif path == '/api/ledger/delete/start':
                result = self.app.archives.start(body)
            elif path == '/api/ledger/move/preview':
                result = self.app.moves.request_preview(body)
            elif path == '/api/ledger/move/start':
                result = self.app.moves.start(body)
            elif path == '/api/jobs':
                if body.get('consent') is not True:
                    raise ValueError('AIへの送信と作業範囲の確認が必要です。')
                result = engine.create_job(str(body.get('title', '')), str(body.get('goal', '')),
                                           str(body.get('project', '')), body.get('auto_execute') is True)
            elif path == '/api/decide':
                if type(body.get('allow')) is not bool:
                    raise ValueError('承認または拒否を選んでください。')
                engine.decide(body['id'], body['allow'], body.get('note', ''), body.get('answers'))
                result = {'ok': True}
            elif path == '/api/cancel':
                engine.cancel_job(body['id'])
                result = {'ok': True}
            elif path == '/api/retry':
                if body.get('checked_changes') is not True:
                    raise ValueError('既に行われた変更を確認してください。')
                engine.retry(body['id'], body.get('note', ''))
                result = {'ok': True}
            elif path == '/api/tasks/repair-review':
                if body.get('checked_changes') is not True:
                    raise ValueError('修正内容と作業範囲の確認が必要です。')
                result = engine.repair_review(body['id'],body.get('note',''),body.get('artifact_path',''))
            elif path == '/api/tasks/handoff':
                engine.handoff_task(body['id'], body.get('note', ''))
                result = {'ok': True}
            elif path == '/api/tasks/transfer':
                result = engine.transfer_scope(body['id'], body['target_id'], body.get('scope', ''),
                                               body.get('checked_changes') is True, body.get('resume') is True)
            elif path == '/api/tasks/advice':
                result = engine.diagnose_task(body['id'])
            elif path == '/api/conflicts/investigate':
                result = engine.investigate_conflicts(body['id'])
            elif path == '/api/tasks/model':
                profile = body.get('profile')
                if not isinstance(profile, dict):
                    raise ValueError('切替先のモデルを選択してください。')
                profile = {k: str(profile.get(k, '')) for k in ('adapter', 'model', 'effort')}
                self.app.models.validate(profile)
                if body.get('resume') is True and body.get('checked_changes') is not True:
                    raise ValueError('実施済みの変更を確認してください。')
                result = engine.switch_model(body['id'], profile, body.get('include_waiting') is True, body.get('resume') is True,
                                             body.get('auto_return') is True)
            elif path == '/api/tasks/restore':
                result = engine.restore_team_model(body['id'], body.get('include_waiting') is True)
            elif path == '/api/pause':
                if self.app.cli_update.snapshot()['state'] == 'updating' or self.app.claude_update.snapshot()['state'] == 'updating':
                    raise ValueError('CLI更新中は一時停止の状態を変更できません。')
                engine.paused = body.get('paused') is True
                result = {'paused': engine.paused}
            elif path == '/api/stop':
                if self.app.cli_update.snapshot()['state'] == 'updating' or self.app.claude_update.snapshot()['state'] == 'updating':
                    raise ValueError('CLI更新中は完了を待ってから全作業を中止してください。')
                engine.paused = True
                for job in self.app.store.all('job'):
                    if job['status'] in ('running', 'planning', 'awaiting_approval'):
                        engine.cancel_job(job['id'])
                result = {'ok': True}
            elif path == '/api/shutdown':
                engine.paused = True
                engine.shutdown.set()
                result = {'ok': True}
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            elif path == '/api/backup':
                primary = self.app.store.backup()
                handoff = 'handoff-' + primary
                self.app.engine.handoff.backup(self.app.store.directory / handoff)
                result = {'file':primary,'handoff_file':handoff}
            elif path == '/api/handoff/fact':
                job = self.app.store.get(body.get('job_id',''), 'job')
                self.app.engine.handoff.set_user_fact(job['project'],job['id'],body.get('key',''),
                    body.get('value',''),body.get('evidence',''))
                self.app.store.event('handoff_fact', '引き継ぎ情報を利用者が登録・訂正しました。',job_id=job['id'])
                result = {'ok':True}
            elif path == '/api/folders/list':
                result = listing(body.get('path'))
            elif path == '/api/folders/create':
                result = {'path': create_folder(body.get('parent'), body.get('name'))}
            elif path == '/api/projects':
                if body.get('consent') is not True:
                    raise ValueError('プロジェクト登録の確認が必要です。')
                project = project_folder(str(body.get('path', '')))
                with self.app.store.lock:
                    config = json.loads(json.dumps(self.app.config))
                    if canonical(project) not in [canonical(p) for p in config['approved_roots']]:
                        config['approved_roots'].append(str(project))
                        self.app.save_config(config)
                result = {'ok': True, 'path': str(project)}
            elif path == '/api/models':
                result = self.app.models.get(body.get('adapter'), body.get('refresh') is True)
            elif path == '/api/cli-update/check':
                result = self.app.cli_update.request_check()
            elif path == '/api/cli-update/install':
                result = self.app.cli_update.request_update(body.get('version'))
            elif path == '/api/claude-update/check':
                result = self.app.claude_update.request_check()
            elif path == '/api/claude-update/install':
                result = self.app.claude_update.request_update(body.get('version'))
            elif path == '/api/decision-settings':
                config = json.loads(json.dumps(self.app.config))
                config['decision'] = body
                self.app.save_config(config)
                result = {'ok': True}
            elif path == '/api/decision-check':
                from team_decisions import route_agent
                settings = self.app.config.get('decision', {'provider':'ollama','model':'tev1:0.8b','shadow':True})
                if settings['provider'] not in ('ollama','disabled'):
                    raise ValueError('接続確認はローカルOllamaまたは停止モードで実施してください。クラウドへの試験送信は行いません。')
                result = route_agent('researcher','common-explorer',settings)
            elif path == '/api/profiles':
                config = json.loads(json.dumps(self.app.config))
                for role in ('planner', 'builder', 'researcher', 'reviewer'):
                    item = body.get(role)
                    if not isinstance(item, dict):
                        raise ValueError('全役割のプロファイルが必要です。')
                    self.app.models.validate(item)
                    name = role + '-' + str(time.time_ns())
                    config['profiles'][name] = {k: str(item.get(k, '')) for k in ('adapter', 'model', 'effort')}
                    config['roles'][role] = name
                self.app.save_config(config)
                result = {'ok': True}
            else:
                return self.send(404, {'error': '未対応の操作です。'})
            self.send(200, result)
        except PermissionError as exc:
            self.send(403, {'error': str(exc)})
        except (ValueError, KeyError, TypeError) as exc:
            self.send(400, {'error': str(exc)[:1000]})
        except Exception:
            self.send(500, {'error': '処理に失敗しました。再読み込みして状態を確認してください。'})


def make_server(directory, port=8790):
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    try:
        server.app = App(directory, server.server_port)
    except Exception:
        server.server_close()
        raise
    return server


def mobile_loop(app):
    while not app.engine.shutdown.is_set():
        try:
            address = wifi_address()
            if app.mobile is not None and app.mobile.ip != address:
                app.mobile.shutdown()
                app.mobile.server_close()
                app.mobile = None
            if app.mobile is None:
                app.mobile = start_gateway(app.store.directory)
            app.mobile_error = None
        except Exception as exc:
            app.mobile_error = str(exc)[:300]
        app.engine.shutdown.wait(30)
    if app.mobile is not None:
        app.mobile.shutdown()
        app.mobile.server_close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8790)
    parser.add_argument('--data', type=Path, default=ROOT / 'data')
    args = parser.parse_args()
    server = make_server(args.data, args.port)
    if args.port == 8790:
        threading.Thread(target=mobile_loop, args=(server.app,), daemon=True).start()
    server.app.usage.start()
    server.app.notifications.start()
    server.app.cli_update.start()
    server.app.claude_update.start()
    threading.Thread(target=server.app.auth_loop, daemon=True).start()
    threading.Thread(target=server.app.engine.loop, daemon=True).start()
    print(f'采来 — サイクル —: {server.app.origin}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.app.engine.shutdown.set()
        server.app.consultations.shutdown()
        for ctx in list(server.app.engine.active.values()):
            ctx.cancel.set()
        deadline = time.time() + 15
        while server.app.engine.active and time.time() < deadline:
            time.sleep(.1)
        if server.app.archives.thread:
            server.app.archives.thread.join(timeout=10)
        if server.app.moves.thread:
            server.app.moves.thread.join(timeout=10)
        if server.app.cli_update.snapshot()['state'] == 'updating' and server.app.cli_update.thread:
            server.app.cli_update.thread.join(timeout=650)
        if server.app.claude_update.snapshot()['state'] == 'updating' and server.app.claude_update.thread:
            server.app.claude_update.thread.join(timeout=310)
        server.server_close()
        server.app.notifications.stop()
        server.app.store.db.close()
        server.app.engine.handoff.close()
        server.app.data_lock.close()


if __name__ == '__main__':
    main()
