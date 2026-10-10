"""User initiated subscription login. Never store, log or display OAuth URLs or credentials.

2026-10-09 (user decision): started from the background task, the CLI cannot open the browser itself and its fallback
URL was discarded, so nothing appeared. 采来 now reads only the sign-in URL from the CLI output, in memory, and opens it
with this PC's default browser. Only https URLs of Anthropic's sign-in hosts are opened; nothing is shown on the
screen, written to a file or recorded in the history. Codes and tokens are never read from or sent to the CLI.
"""
import os
import re
import subprocess
import threading
import time
from urllib.parse import urlparse

from team_config import ROOT, claude_config_directory

LOGIN_HOSTS = ('claude.com', 'claude.ai', 'anthropic.com')
URL = re.compile(r'https://[^\s<>"\']+')


def login_url(line):
    """The sign-in URL in one line of CLI output, or None (https on an Anthropic sign-in host only)."""
    match = URL.search(line or '')
    if not match:
        return None
    try:
        parsed = urlparse(match.group(0))
        host = (parsed.hostname or '').lower()
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != 'https' or parsed.username or parsed.password or port not in (None, 443):
        return None
    if not any(host == h or host.endswith('.' + h) for h in LOGIN_HOSTS):
        return None
    return match.group(0)


def open_in_browser(url):
    """Open with this PC's default browser (ShellExecute). The URL is not kept anywhere."""
    if hasattr(os, 'startfile'):
        os.startfile(url)
    else:
        import webbrowser
        webbrowser.open(url)


class ClaudeLogin:
    def __init__(self, app, opener=open_in_browser):
        self.app = app
        self.opener = opener
        self.lock = threading.Lock()
        self.state = 'idle'
        self.browser = None  # None: not yet, True: opened by 采来, False: could not open

    def snapshot(self):
        with self.lock:
            return {'state': self.state, 'browser_opened': self.browser}

    def start(self):
        with self.lock:
            if self.state == 'running':
                return {'state': self.state, 'browser_opened': self.browser}
            if self.app.health.get('claude', {}).get('authenticated') is True:
                return {'state': 'authenticated'}
            command = self.app.commands.get('claude')
            if not command:
                raise ValueError('Claude Code CLIが見つかりません。')
            self.state, self.browser = 'running', None
            threading.Thread(target=self._run, args=(list(command),), daemon=True).start()
            return {'state': self.state, 'browser_opened': self.browser}

    def _watch_output(self, stream):
        """Read the CLI output until it ends; open the first sign-in URL once. Lines are not kept."""
        opened = False
        for raw in iter(stream.readline, b''):
            if opened:
                continue
            url = login_url(raw.decode('utf-8', 'replace'))
            if not url:
                continue
            opened = True
            try:
                self.opener(url)
                result = True
            except OSError:
                result = False
            with self.lock:
                self.browser = result
            url = None

    def _run(self, command):
        state = 'failed'
        process = None
        try:
            env = os.environ.copy()
            env['CLAUDE_CONFIG_DIR'] = str(claude_config_directory())
            process = subprocess.Popen(command + ['auth', 'login', '--claudeai'],
                cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            reader = threading.Thread(target=self._watch_output, args=(process.stdout,), daemon=True)
            reader.start()
            deadline = time.monotonic() + 300
            while process.poll() is None:
                if self.app.engine.shutdown.wait(1) or time.monotonic() >= deadline:
                    process.terminate()
                    process.wait(timeout=5)
                    break
            reader.join(timeout=5)
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
