"""Check CLI releases and run an explicitly selected provider update."""
import os
import re
import shutil
import subprocess
import threading
import time

from team_config import discover, environment_status


VERSION = re.compile(r'^\d+\.\d+\.\d+$')
CHECK_INTERVAL = 24 * 60 * 60
FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def version_tuple(value):
    if not VERSION.fullmatch(value):
        raise ValueError('CLIの版数を確認できませんでした。')
    return tuple(map(int, value.split('.')))


class CliUpdateMonitor:
    def __init__(self, app, provider='codex'):
        if provider not in ('codex', 'claude'):
            raise ValueError('未対応のCLIです。')
        self.app = app
        self.provider = provider
        self.label = 'Codex' if provider == 'codex' else 'Claude Code'
        self.package = '@openai/codex' if provider == 'codex' else '@anthropic-ai/claude-code'
        self.lock = threading.Lock()
        self.info = {'state': 'checking', 'installed': None, 'latest': None,
                     'checked_at': None, 'message': '更新を確認しています。'}
        self.thread = None

    def snapshot(self):
        with self.lock:
            return dict(self.info)

    def start(self):
        threading.Thread(target=self._periodic, daemon=True, name=f'{self.provider}-update-check').start()

    def _periodic(self):
        self.check()
        while not self.app.engine.shutdown.wait(CHECK_INTERVAL):
            self.check()

    def _run(self, args, timeout):
        result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8',
                                errors='replace', timeout=timeout, creationflags=FLAGS)
        if result.returncode:
            raise RuntimeError('CLI更新コマンドに失敗しました。ネットワーク、更新設定、CLIの導入方法を確認してください。')
        return result.stdout.strip()

    def _installed(self):
        command = discover()[self.provider]
        if not command:
            raise RuntimeError(f'采来 — サイクル —が使用する{self.label} CLIが見つかりません。')
        output = self._run(command + ['--version'], 20)
        match = re.search(r'\b(\d+\.\d+\.\d+)\b', output)
        if not match:
            raise RuntimeError(f'{self.label} CLIの版数を読めませんでした。')
        return match.group(1)

    def _npm(self):
        command = shutil.which('npm.cmd' if os.name == 'nt' else 'npm')
        if not command:
            raise RuntimeError('npmが見つかりません。')
        return command

    def check(self):
        with self.lock:
            if self.info['state'] in ('checking', 'updating') and self.thread and self.thread.is_alive() and self.thread is not threading.current_thread():
                return self.snapshot_unlocked()
            self.info.update(state='checking', message='更新を確認しています。')
            self.thread = threading.current_thread()
        try:
            installed = self._installed()
            latest = self._run([self._npm(), 'view', self.package, 'version'], 30)
            version_tuple(installed)
            version_tuple(latest)
            available = version_tuple(latest) > version_tuple(installed)
            value = {'state': 'available' if available else 'current',
                     'installed': installed, 'latest': latest, 'checked_at': time.time(),
                     'message': '更新できます。' if available else '最新版です。'}
        except (OSError, subprocess.TimeoutExpired, RuntimeError, ValueError):
            value = {'state': 'error', 'checked_at': time.time(),
                     'message': '更新情報を取得できませんでした。手動で再確認できます。'}
        with self.lock:
            self.info.update(value)
            if self.thread is threading.current_thread():
                self.thread = None
            return self.snapshot_unlocked()

    def snapshot_unlocked(self):
        return dict(self.info)

    def request_check(self):
        with self.lock:
            if self.info['state'] == 'updating':
                raise ValueError('CLIを更新中です。')
            if self.thread and self.thread.is_alive():
                return self.snapshot_unlocked()
            self.info.update(state='checking', message='更新を確認しています。')
            self.thread = threading.Thread(target=self.check, daemon=True, name=f'{self.provider}-update-check')
            self.thread.start()
            return self.snapshot_unlocked()

    def request_update(self, expected):
        with self.app.store.lock:
            with self.lock:
                if self.info['state'] != 'available' or expected != self.info['latest']:
                    raise ValueError('先に更新情報を再確認してください。')
                if time.time() - self.info['checked_at'] > CHECK_INTERVAL:
                    raise ValueError('更新情報が古いため、再確認してください。')
                if self.provider != 'codex' and self.app.cli_update.snapshot()['state'] == 'updating':
                    raise ValueError('Codex CLIの更新が終わってから実行してください。')
                if self.provider != 'claude' and self.app.claude_update.snapshot()['state'] == 'updating':
                    raise ValueError('Claude Code CLIの更新が終わってから実行してください。')
                if self.app.engine.active or any(t['status'] == 'running' for t in self.app.store.all('task')):
                    raise ValueError('実行中の作業が終わってから更新してください。')
                if self.app.archives.thread and self.app.archives.thread.is_alive():
                    raise ValueError('アーカイブ処理が終わってから更新してください。')
                if self.app.moves.thread and self.app.moves.thread.is_alive():
                    raise ValueError('移動処理が終わってから更新してください。')
                was_paused = self.app.engine.paused
                self.app.engine.paused = True
                previous = self.info['installed']
                self.info.update(state='updating', message=f'{self.label} CLIを更新しています。新規着手は一時停止中です。')
                self.thread = threading.Thread(target=self._install, args=(expected, previous, was_paused),
                                               daemon=True, name=f'{self.provider}-cli-update')
                self.thread.start()
                return self.snapshot_unlocked()

    def _install(self, target, previous, was_paused):
        message = ''
        try:
            if self.provider == 'codex':
                self._run([self._npm(), 'install', '-g', f'{self.package}@{target}',
                           '--no-audit', '--no-fund'], 300)
                if self._installed() != target:
                    raise RuntimeError('更新後の版数が一致しません。')
            else:
                command = discover()['claude']
                self._run(command + ['update'], 300)
                if version_tuple(self._installed()) <= version_tuple(previous):
                    raise RuntimeError('更新後も采来 — サイクル —が使用するCLIの版数が変わりません。Claudeの起動用パスを確認してください。')
            self._refresh_runtime()
            installed = self._installed()
            quota_ok = self.app.usage.refresh_now(self.provider, mark_loading=True)
            with self.lock:
                self.info.update(state='current', installed=installed, latest=installed,
                                 checked_at=time.time(), message=('更新が完了し、利用枠も再取得しました。' if quota_ok else
                                     'CLIの更新は完了しました。利用枠の再取得に失敗したため、前回値は最新値として扱いません。') + ' モデル一覧を再取得してください。')
            self.app.store.event(f'{self.provider}_cli_updated', f'{self.label} CLIを{previous}から{installed}へ更新しました。')
            return
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
            message = str(exc)
            restored = self.provider == 'claude' and self._installed_safe() == previous
            if self.provider == 'codex':
                try:
                    if self._installed() != previous:
                        self._run([self._npm(), 'install', '-g', f'{self.package}@{previous}',
                                   '--no-audit', '--no-fund'], 300)
                    restored = self._installed() == previous
                except (OSError, subprocess.TimeoutExpired, RuntimeError):
                    restored = False
            self._refresh_runtime()
            installed = self._installed_safe()
            with self.lock:
                self.info.update(state='error', installed=installed, checked_at=time.time(),
                                 message=message + (' 以前の版は使用できます。' if restored and self.provider == 'claude' else
                                                    ' 元の版へ戻しました。' if restored else ' CLIの版数を確認してください。'))
            self.app.store.event(f'{self.provider}_cli_update_failed', f'{self.label} CLIの更新に失敗しました。画面で状態を確認してください。')
        finally:
            self.app.engine.paused = was_paused

    def _installed_safe(self):
        try:
            return self._installed()
        except (OSError, subprocess.TimeoutExpired, RuntimeError, ValueError):
            return None

    def _refresh_runtime(self):
        commands = discover()
        with self.app.models.locks[self.provider]:
            self.app.commands = commands
            self.app.engine.commands = commands
            self.app.usage.commands = commands
            self.app.models.commands = commands
            self.app.models.cache.pop(self.provider, None)
        self.app.health = environment_status(commands)
