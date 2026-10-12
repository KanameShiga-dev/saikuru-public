"""Claude Code sign-in for 采来's workers (2026-10-12, user approved: plan A + C).

A — long-lived token: the user creates it with `claude setup-token` and stores it in the Windows Credential Manager
    (generic credential TARGET). 采来 reads it there and passes it only to the Claude CLI processes it starts, as
    CLAUDE_CODE_OAUTH_TOKEN. Nothing about it is written to the database, events, logs or the screen; only "configured"
    or not is shown. Without a stored token the regular login (CLAUDE_CONFIG_DIR) is used as before.
    Why: the regular login's access token lasts about 8 hours and its refresh token rotates. Parallel workers refreshing
    at the same moment invalidated it (10-09 21:14, 10-11 01:24 and 17:04).
C — when a Claude run fails on sign-in, Claude tasks are held (queued, not failed) and the user gets a Windows
    notification. The hold ends when the sign-in changes (the credentials file is rewritten by a new login, or the
    stored token changes), so a stale "logged in" status cannot start the same failing run again and again.
"""
import ctypes
import hashlib
import os
import re
import threading
import time
from ctypes import wintypes
from pathlib import Path

TARGET = 'saikuru/claude-oauth-token'
ENV = 'CLAUDE_CODE_OAUTH_TOKEN'
CACHE_SECONDS = 60
REMIND_SECONDS = 2 * 3600
AUTH_ERROR = re.compile(r'Failed to authenticate|OAuth session expired|could not be refreshed|未ログイン|認証期限切れ|'
                        r'Invalid API key|Please run /login|not logged in', re.I)


class _Credential(ctypes.Structure):
    _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD), ('TargetName', wintypes.LPWSTR),
                ('Comment', wintypes.LPWSTR), ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD),
                ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)), ('Persist', wintypes.DWORD),
                ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p), ('TargetAlias', wintypes.LPWSTR),
                ('UserName', wintypes.LPWSTR)]


def read_stored_token(target=TARGET):
    """The token stored by the user (cmdkey /generic:TARGET), or None. Windows only; never raises."""
    if os.name != 'nt':
        return None
    try:
        advapi = ctypes.WinDLL('advapi32', use_last_error=True)
        advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    ctypes.POINTER(ctypes.POINTER(_Credential))]
        advapi.CredReadW.restype = wintypes.BOOL
        advapi.CredFree.argtypes = [ctypes.c_void_p]
        advapi.CredFree.restype = None
        pointer = ctypes.POINTER(_Credential)()
        if not advapi.CredReadW(target, 1, 0, ctypes.byref(pointer)):   # 1 = CRED_TYPE_GENERIC
            return None
        try:
            blob = bytes(pointer.contents.CredentialBlob[:pointer.contents.CredentialBlobSize])
        finally:
            advapi.CredFree(pointer)
    except (OSError, AttributeError, ValueError):
        return None
    # cmdkey stores the password as UTF-16LE; a credential written by other tools may be UTF-8.
    for encoding in ('utf-16-le', 'utf-8'):
        try:
            text = blob.decode(encoding).strip().strip('\x00')
        except UnicodeDecodeError:
            continue
        if text and text.isprintable() and ' ' not in text:
            return text
    return None


def is_auth_error(message):
    return bool(AUTH_ERROR.search(str(message or '')))


def credentials_mtime(config_dir):
    try:
        return (Path(config_dir) / '.credentials.json').stat().st_mtime
    except OSError:
        return None


class ClaudeAuth:
    """Sign-in state for Claude workers: the stored token (A) and the hold after a sign-in failure (C)."""

    def __init__(self, config_dir, notify=None, reader=read_stored_token, clock=time.time):
        self.config_dir, self.notify, self.reader, self.clock = config_dir, notify, reader, clock
        self.lock = threading.Lock()
        self._token, self._read_at = None, None
        self.hold = None   # {'since', 'credentials_mtime', 'token_digest', 'reminded'}

    def token(self):
        with self.lock:
            now = self.clock()
            if self._read_at is None or now - self._read_at >= CACHE_SECONDS:
                self._token, self._read_at = self.reader(), now
            return self._token

    def configured(self):
        return bool(self.token())

    def env(self, env):
        """The environment for a Claude CLI process: the stored token added when there is one."""
        token = self.token()
        if token:
            env = dict(env)
            env[ENV] = token
        return env

    @staticmethod
    def _digest(token):
        return hashlib.sha256(token.encode('utf-8')).hexdigest() if token else None

    def available(self, logged_in):
        """May a Claude task start now? logged_in is the CLI's own status (used when no token is stored)."""
        with self.lock:
            holding = self.hold is not None
        if holding:
            return False
        return self.configured() or logged_in is True

    def on_failure(self, message):
        """A Claude run failed. True when it was a sign-in failure: Claude tasks are then held and the user told."""
        if not is_auth_error(message):
            return False
        digest = self._digest(self.token())
        with self.lock:
            first = self.hold is None
            if first:
                self._read_at = None
                self.hold = {'since': self.clock(), 'credentials_mtime': credentials_mtime(self.config_dir),
                             'token_digest': digest, 'reminded': self.clock()}
        if first:
            self._tell('Claude Code のログインが切れたため、Claude の担当の作業を止めています。'
                       'ログインし直すか、長期の鍵を入れ直すと自動で再開します。')
        return True

    def refresh(self):
        """Called about every minute: end the hold when the sign-in changed; remind while it lasts.
        Returns 'released', 'holding' or None."""
        with self.lock:
            hold = self.hold
            if hold is None:
                return None
            self._read_at = None
        current_digest = self._digest(self.token())
        token_changed = current_digest != hold['token_digest']
        login_changed = credentials_mtime(self.config_dir) not in (None, hold['credentials_mtime'])
        if token_changed or (current_digest is None and login_changed):
            with self.lock:
                if self.hold is not hold:
                    return 'holding' if self.hold else None
                self.hold = None
            return 'released'
        if self.clock() - hold['reminded'] >= REMIND_SECONDS:
            hold['reminded'] = self.clock()
            self._tell('Claude Code のログインが切れたままです。Claude の担当の作業は止まっています。')
        return 'holding'

    def release(self):
        """The user asked to try again (for example after fixing the sign-in another way)."""
        with self.lock:
            self.hold, self._read_at = None, None

    def snapshot(self):
        with self.lock:
            hold = dict(self.hold) if self.hold else None
        return {'long_lived_token': self.configured(), 'held': hold is not None,
                'held_since': hold['since'] if hold else None}

    def _tell(self, message):
        if self.notify:
            try:
                self.notify(message)
            except Exception:
                pass   # a notification problem never stops 采来
