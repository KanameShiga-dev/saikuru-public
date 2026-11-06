"""User-approved local Web trials, independent of the model worker lifetime."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import threading
import time
import urllib.request

from consultation_read_tools import safe_path, screen_model_read
from team_p0_contract import artifact_records


class WebTrials:
    def __init__(self, app):
        self.app = app
        self.lock = threading.RLock()
        self.pending = {}
        self.running = {}

    def context(self, job_id):
        job = self.app.store.get(job_id, 'job')
        root = Path(job['project']).resolve(strict=True)
        if (job.get('p0_contract_version') != 1
                or root not in [Path(p).resolve() for p in self.app.config['approved_roots']]
                or self.app.engine.project_blocked(str(root))):
            raise ValueError('承認済みの開発プロジェクトを選択してください。')
        if any(Path(t['project']).resolve() == root for t in self.app.store.all('job')
               if t.get('status') in ('running', 'planning', 'awaiting_approval')):
            raise ValueError('同じプロジェクトの実行・承認待ちが終わってから試行してください。')
        return job, root

    def fingerprint(self, job, root, start, stop):
        records = artifact_records(str(root), [a['path'] for a in job.get('p0_artifacts') or []])
        if not records or records != job.get('p0_artifacts'):
            raise ValueError('登録済み成果物と現在のファイルが一致しません。再検証が必要です。')
        for name in (start, stop):
            path = safe_path(root, name, allow_data=True)
            if Path(name).is_absolute() or path.suffix.casefold() != '.ps1':
                raise ValueError('起動・停止にはプロジェクト相対のPowerShellスクリプトを指定してください。')
            safe_path(root, name)
            records.append({'path': name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        return hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def command(root, script, port):
        command = ['powershell', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                   '-File', str(safe_path(root, script, allow_data=True)), '-Port', str(port)]
        from team_outbound_guard import classify
        from team_dlp import require_clean_input
        text = subprocess.list2cmdline(command)
        require_clean_input(text, False, '確認用サーバーのコマンド')
        verdict = classify(text)
        if verdict and verdict[2] == 'deny':
            raise ValueError('既存の操作ガードにより起動・停止コマンドを実行できません。')
        return command

    @staticmethod
    def occupied(port):
        with socket.socket() as sock:
            if os.name == 'nt':
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                sock.bind(('127.0.0.1', port))
            except OSError:
                return True
            return False

    def settings(self, job_id):
        """Suggest existing conventional scripts and an unused local port, without executing."""
        with self.lock:
            job, root = self.context(job_id)
            names = {path.name.casefold(): path.name for path in root.iterdir() if path.is_file()}
            pairs = [(names[start], names[stop]) for start, stop in (
                ('start-server.ps1', 'stop-server.ps1'), ('start.ps1', 'stop.ps1'))
                if start in names and stop in names]
            if len(pairs) != 1:
                return {'configured': False,
                        'message': '起動・停止の設定を一意に確認できません。詳細設定で指定してください。'}
            start, stop = pairs[0]
            self.fingerprint(job, root, start, stop)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            if port == self.app.port:
                raise ValueError('空いている確認用ポートを選べませんでした。もう一度準備してください。')
            return {'configured': True, 'start_script': start, 'stop_script': stop, 'port': port}

    def prepare(self, body):
        with self.lock:
            job, root = self.context(body.get('job_id'))
            port = body.get('port')
            if type(port) is not int or not 1024 <= port <= 65535 or port == self.app.port:
                raise ValueError('采来とは別の1024〜65535番のポートを指定してください。')
            if self.occupied(port):
                raise ValueError('このポートは使用中です。別のポートを選ぶか、既存サーバーを通常の手順で停止してください。采来は自動停止しません。')
            start, stop = body.get('start_script'), body.get('stop_script')
            if any(not isinstance(n, str) or not n or len(n) > 240 for n in (start, stop)):
                raise ValueError('起動・停止スクリプトを指定してください。')
            digest = self.fingerprint(job, root, start, stop)
            scripts = [{'path': name, 'content': screen_model_read(
                safe_path(root, name, allow_data=True).read_text(encoding='utf-8-sig'))}
                for name in (start, stop)]
            token = secrets.token_urlsafe(24)
            entry = {'job_id': job['id'], 'root': str(root), 'port': port,
                     'start_script': start, 'stop_script': stop, 'fingerprint': digest,
                     'stop_sha256': self.fingerprint_for_script(root, stop),
                     'expires': time.monotonic() + 300}
            self.pending = {k: v for k, v in self.pending.items() if v['expires'] > time.monotonic()}
            self.pending[token] = entry
            return {'token': token, 'url': f'http://127.0.0.1:{port}/',
                    'start_command': subprocess.list2cmdline(self.command(root, start, port)),
                    'stop_command': subprocess.list2cmdline(self.command(root, stop, port)),
                    'fingerprint': digest, 'scripts': scripts,
                    'notice': '両コマンドとスクリプトの内容を確認してください。プロセス限定Bypassで実行し、OS設定は変更しません。既存サーバーは停止しません。'}

    def start(self, body):
        with self.lock:
            entry = self.pending.get(body.get('token'))
            if (not entry or entry['expires'] < time.monotonic()
                    or body.get('approve_start') is not True or body.get('approve_stop') is not True):
                raise ValueError('最新の起動・停止コマンドをそれぞれ承認してください。')
            job, root = self.context(entry['job_id'])
            if self.fingerprint(job, root, entry['start_script'], entry['stop_script']) != entry['fingerprint']:
                raise ValueError('確認後に成果物またはスクリプトが変わりました。準備し直してください。')
            key = os.path.normcase(str(root))
            if key in self.running and self.running[key]['process'].poll() is not None:
                del self.running[key]
            if key in self.running or self.occupied(entry['port']):
                raise ValueError('このプロジェクトまたはポートは使用中です。既存サーバーは自動停止しません。')
            del self.pending[body['token']]
            directory = self.app.store.directory / 'web-trials'
            directory.mkdir(exist_ok=True)
            output = (directory / (job['id'] + '.log')).open('wb')
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith(('AGENT_TEAM_', 'SAIKURU_'))}
            try:
                process = subprocess.Popen(self.command(root, entry['start_script'], entry['port']),
                                           cwd=root, env=env, stdin=subprocess.DEVNULL,
                                           stdout=output, stderr=subprocess.STDOUT,
                                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            finally:
                output.close()
            self.running[key] = dict(entry, process=process, status='starting', message='起動を確認しています。')
            self.app.store.event('web_trial_start', '利用者が起動・停止コマンドを承認し、確認用Webサーバーを起動しました。',
                                 job_id=job['id'], details={
                                     'port': entry['port'], 'fingerprint': entry['fingerprint'],
                                     'start_script': entry['start_script'], 'stop_script': entry['stop_script'],
                                     'approve_start': True, 'approve_stop': True})
            threading.Thread(target=self.wait_ready, args=(key,), daemon=True).start()
            return {'status': 'starting'}

    def wait_ready(self, key):
        try:
            for _ in range(30):
                with self.lock:
                    entry = self.running.get(key)
                    if not entry or entry['status'] != 'starting':
                        return
                    if entry['process'].poll() is not None:
                        raise ValueError('確認用サーバーが終了しました。起動スクリプトとログを確認してください。')
                    port = entry['port']
                if self.occupied(port):
                    self.probe(entry)
                    with self.lock:
                        if self.running.get(key) is entry and entry['status'] == 'starting':
                            entry.update(status='ready', message='現行成果物で起動し、確認URLのHTTP応答を確認しました。画面の合格は未確認です。')
                    return
                time.sleep(.5)
            raise ValueError('確認用サーバーが起動時間内に応答しませんでした。')
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            with self.lock:
                entry = self.running.get(key)
                if entry:
                    from copilot_work_tools import stop_tree
                    if entry['process'].poll() is None:
                        stop_tree(entry['process'])
                        entry['process'].wait(timeout=10)
                    entry.update(status='failed', message=str(exc)[:300])
                    self.app.store.event('web_trial_failed', entry['message'], job_id=entry['job_id'])

    @staticmethod
    def check_listener(entry):
        if os.name != 'nt':
            raise ValueError('この確認用サーバー管理はWindows専用です。')
        command = f'Get-NetTCPConnection -State Listen -LocalPort {entry["port"]} | Select-Object LocalAddress,OwningProcess | ConvertTo-Json -Compress'
        result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', command],
                                capture_output=True, text=True, timeout=10,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        rows = json.loads(result.stdout or '[]')
        if isinstance(rows, dict):
            rows = [rows]
        if result.returncode or not rows or any(
                r.get('LocalAddress') != '127.0.0.1' or r.get('OwningProcess') != entry['process'].pid for r in rows):
            raise ValueError('この起動プロセスによる127.0.0.1限定の待ち受けを確認できません。')

    def probe(self, entry):
        self.check_listener(entry)
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        client = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with client.open(f'http://127.0.0.1:{entry["port"]}/', timeout=2) as response:
            if response.status != 200:
                raise ValueError('確認URLがHTTP 200を返していません。')

    def status(self, job_id):
        job = self.app.store.get(job_id, 'job')
        key = os.path.normcase(str(Path(job['project']).resolve()))
        with self.lock:
            entry = self.running.get(key)
            if not entry:
                return {'status': 'stopped', 'message': '采来が管理する確認用サーバーは起動していません。'}
            status, message = entry['status'], entry['message']
            if entry['process'].poll() is not None and status != 'failed':
                status, message = 'stopped', '確認用サーバーは終了しています。'
            if status == 'ready':
                try:
                    digest = self.fingerprint(job, Path(entry['root']), entry['start_script'], entry['stop_script'])
                    if digest != entry['fingerprint']:
                        status, message = 'stale', '起動後に成果物またはスクリプトが更新されました。停止後、起動し直してください。'
                except (OSError, ValueError):
                    status, message = 'stale', '現在の成果物を確認できません。再検証と起動し直しが必要です。'
            if status == 'ready':
                try:
                    self.probe(entry)
                except (OSError, ValueError, subprocess.SubprocessError):
                    status, message = 'unavailable', '管理対象の待ち受けとHTTP応答を確認できません。停止して確認してください。'
            return {'status': status, 'message': message, 'url': f'http://127.0.0.1:{entry["port"]}/',
                    'job_id': entry['job_id'], 'fingerprint': entry['fingerprint']}

    def stop(self, body):
        with self.lock:
            job, root = self.context(body.get('job_id'))
            key = os.path.normcase(str(root))
            entry = self.running.get(key)
            if not entry or body.get('confirmed') is not True:
                raise ValueError('采来が起動した確認用サーバーの停止を確認してください。')
            process = entry['process']
            if process.poll() is None:
                if body.get('force_owned_process') is True:
                    from copilot_work_tools import stop_tree
                    stop_tree(process)
                    process.wait(timeout=10)
                    del self.running[key]
                    self.app.store.event('web_trial_stop', '利用者の明示確認で管理対象の確認用プロセスだけを停止しました。',
                                         job_id=job['id'])
                    return {'status': 'stopped'}
                # Never execute a changed stop script using the old consent.
                digest = self.fingerprint_for_script(root, entry['stop_script'])
                if digest != entry['stop_sha256']:
                    raise ValueError('停止スクリプトが変更されています。采来の終了時に管理対象プロセスだけを停止します。')
                stopping = subprocess.Popen(self.command(root, entry['stop_script'], entry['port']), cwd=root,
                                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL,
                                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                try:
                    exit_code = stopping.wait(timeout=15)
                except subprocess.TimeoutExpired as exc:
                    from copilot_work_tools import stop_tree
                    stop_tree(stopping)
                    stopping.wait(timeout=10)
                    raise ValueError('停止コマンドが時間切れになったため、そのコマンドのプロセスツリーを停止しました。') from exc
                if exit_code:
                    raise ValueError('停止コマンドが失敗しました。確認用サーバーは停止済みと扱いません。')
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired as exc:
                    raise ValueError('停止後も管理対象プロセスが残っています。') from exc
            del self.running[key]
            self.app.store.event('web_trial_stop', '利用者の操作で確認用Webサーバーを停止しました。', job_id=job['id'])
            return {'status': 'stopped'}

    @staticmethod
    def fingerprint_for_script(root, script):
        return hashlib.sha256(safe_path(root, script, allow_data=True).read_bytes()).hexdigest()

    def close(self):
        from copilot_work_tools import stop_tree
        with self.lock:
            for entry in self.running.values():
                if entry['process'].poll() is None:
                    stop_tree(entry['process'])
                    entry['process'].wait(timeout=10)
            self.running.clear()
            self.pending.clear()
