"""User initiated subscription login. Never capture OAuth URLs or credentials."""
import os
import subprocess
import threading
import time

from team_config import ROOT, claude_config_directory


class ClaudeLogin:
    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.state = 'idle'

    def snapshot(self):
        with self.lock:
            return {'state': self.state}

    def start(self):
        with self.lock:
            if self.state == 'running':
                return {'state': self.state}
            if self.app.health.get('claude', {}).get('authenticated') is True:
                return {'state': 'authenticated'}
            command = self.app.commands.get('claude')
            if not command:
                raise ValueError('Claude Code CLIが見つかりません。')
            self.state = 'running'
            threading.Thread(target=self._run, args=(list(command),), daemon=True).start()
            return {'state': self.state}

    def _run(self, command):
        state = 'failed'
        process = None
        try:
            env = os.environ.copy()
            env['CLAUDE_CONFIG_DIR'] = str(claude_config_directory())
            process = subprocess.Popen(command + ['auth', 'login', '--claudeai'],
                cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            deadline = time.monotonic() + 300
            while process.poll() is None:
                if self.app.engine.shutdown.wait(1) or time.monotonic() >= deadline:
                    process.terminate()
                    process.wait(timeout=5)
                    break
            self.app.refresh_claude_auth()
            if self.app.health.get('claude', {}).get('authenticated') is True:
                state = 'authenticated'
        except (OSError, subprocess.SubprocessError):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
        finally:
            with self.lock:
                self.state = state
