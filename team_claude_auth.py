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
2026-10-15 (login lost again after usage-limit waits): the stored credential had been written empty, so plan A was
    silently off, and every resume after a long wait started several Claude processes that refreshed the expired
    login at once. Now the regular login is judged from its file (no CLI start, which may refresh it), only one Claude
    task starts while the login is near its refresh, 采来's own tools use the stored token too, and an empty or old
    stored token is reported instead of being treated as "not configured".
"""
import ctypes
import hashlib
import json
import os
import re
import threading
import time
from ctypes import wintypes
from pathlib import Path

# 2026-10-15: a new name. The old 'saikuru/claude-oauth-token' was first written empty by `cmdkey` with enterprise
# (roaming) persistence; on this domain PC the roamed empty copy came back and replaced the token stored locally.
# The new name has never roamed, and store_claude_token.py writes it with local-machine persistence only.
TARGET = 'saikuru/claude-oauth-token-local'
LEGACY_TARGET = 'saikuru/claude-oauth-token'
ENV = 'CLAUDE_CODE_OAUTH_TOKEN'
CACHE_SECONDS = 60
REMIND_SECONDS = 2 * 3600
REFRESH_MARGIN = 10 * 60          # without a stored token: the access token expires within this, so a start may refresh
TOKEN_WARN_AGE = 335 * 86400      # the long-lived token lasts about a year; warn a month before
AUTH_ERROR = re.compile(r'Failed to authenticate|OAuth session expired|could not be refreshed|未ログイン|認証期限切れ|'
                        r'Invalid API key|Please run /login|not logged in', re.I)


class _Credential(ctypes.Structure):
    _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD), ('TargetName', wintypes.LPWSTR),
                ('Comment', wintypes.LPWSTR), ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD),
                ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)), ('Persist', wintypes.DWORD),
                ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p), ('TargetAlias', wintypes.LPWSTR),
                ('UserName', wintypes.LPWSTR)]


def _read_credential(target):
    """(found, blob, written_at, persist) of the generic credential; found is False when it does not exist.
    persist: 2 = this PC only, 3 = enterprise (roams with the domain profile). Never raises."""
    if os.name != 'nt':
        return False, b'', None, None
    try:
        advapi = ctypes.WinDLL('advapi32', use_last_error=True)
        advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    ctypes.POINTER(ctypes.POINTER(_Credential))]
        advapi.CredReadW.restype = wintypes.BOOL
        advapi.CredFree.argtypes = [ctypes.c_void_p]
        advapi.CredFree.restype = None
        pointer = ctypes.POINTER(_Credential)()
        if not advapi.CredReadW(target, 1, 0, ctypes.byref(pointer)):   # 1 = CRED_TYPE_GENERIC
            return False, b'', None, None
        try:
            blob = bytes(pointer.contents.CredentialBlob[:pointer.contents.CredentialBlobSize])
            written = pointer.contents.LastWritten
            filetime = (written.dwHighDateTime << 32) | written.dwLowDateTime
            written_at = filetime / 10_000_000 - 11_644_473_600 if filetime else None   # FILETIME -> Unix time
            persist = pointer.contents.Persist
        finally:
            advapi.CredFree(pointer)
    except (OSError, AttributeError, ValueError):
        return False, b'', None, None
    return True, blob, written_at, persist


def _decode(blob):
    # cmdkey stores the password as UTF-16LE; a credential written by other tools may be UTF-8.
    for encoding in ('utf-16-le', 'utf-8'):
        try:
            text = blob.decode(encoding).strip().strip('\x00')
        except UnicodeDecodeError:
            continue
        if text and text.isprintable() and ' ' not in text:
            return text
    return None


def read_stored_token(target=TARGET):
    """The token stored by the user (cmdkey /generic:TARGET), or None. Windows only; never raises."""
    found, blob, _, _ = _read_credential(target)
    return _decode(blob) if found else None


def stored_token_state(target=TARGET):
    """{'state': 'ok' | 'missing' | 'invalid', 'written_at'}. 'invalid' = the credential exists but holds no usable
    token (seen 2026-10-12: `cmdkey /pass` wrote it empty, and 采来 silently fell back to the regular login)."""
    found, blob, written_at, persist = _read_credential(target)
    if not found:
        legacy = target == TARGET and _read_credential(LEGACY_TARGET)[0]
        return {'state': 'missing', 'written_at': None, 'persist': None, 'legacy': legacy}
    token = _decode(blob)
    usable = bool(token) and token.startswith('sk-ant-') and len(token) >= 40
    return {'state': 'ok' if usable else 'invalid', 'written_at': written_at, 'persist': persist}


def login_file_state(config_dir, now=None):
    """The regular login judged from <config_dir>/.credentials.json without starting the CLI (a CLI start may refresh
    the login, and several refreshing at once lose it). Times are Unix seconds; logged_in is None when unreadable."""
    now = time.time() if now is None else now
    try:
        data = json.loads((Path(config_dir) / '.credentials.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {'logged_in': False, 'access_expires_at': None, 'refresh_expires_at': None}
    except (OSError, ValueError):
        return {'logged_in': None, 'access_expires_at': None, 'refresh_expires_at': None}
    oauth = data.get('claudeAiOauth') if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return {'logged_in': False, 'access_expires_at': None, 'refresh_expires_at': None}

    def seconds(value):
        return value / 1000 if isinstance(value, (int, float)) and value > 0 else None

    access_at, refresh_at = seconds(oauth.get('expiresAt')), seconds(oauth.get('refreshTokenExpiresAt'))
    access_ok = bool(oauth.get('accessToken')) and access_at is not None and access_at > now
    refresh_ok = bool(oauth.get('refreshToken')) and (refresh_at is None or refresh_at > now)
    return {'logged_in': access_ok or refresh_ok, 'access_expires_at': access_at, 'refresh_expires_at': refresh_at}


def claude_env(base=None):
    """Environment for a Claude CLI started by 采来's own tools (measurement, calibration): 采来's config folder, and
    the stored long-lived token when there is one, so these tools never refresh the shared regular login."""
    from team_config import claude_config_directory
    env = dict(os.environ if base is None else base)
    env['CLAUDE_CONFIG_DIR'] = str(claude_config_directory())
    token = read_stored_token()
    if token:
        env.pop('ANTHROPIC_API_KEY', None)   # an API key would take precedence over the token
        env[ENV] = token
    return env


def is_auth_error(message):
    return bool(AUTH_ERROR.search(str(message or '')))


def credentials_mtime(config_dir):
    try:
        return (Path(config_dir) / '.credentials.json').stat().st_mtime
    except OSError:
        return None


class ClaudeAuth:
    """Sign-in state for Claude workers: the stored token (A) and the hold after a sign-in failure (C)."""

    def __init__(self, config_dir, notify=None, reader=read_stored_token, clock=time.time, state_reader=stored_token_state):
        self.config_dir, self.notify, self.reader, self.clock = config_dir, notify, reader, clock
        self.state_reader = state_reader
        self.lock = threading.Lock()
        self._token, self._read_at = None, None
        self.hold = None   # {'since', 'credentials_mtime', 'token_digest', 'reminded'}
        self._warned = None   # the token problem last told to the user (told once per change)

    def login_state(self):
        """The regular login from its file (no CLI start); see login_file_state."""
        return login_file_state(self.config_dir, self.clock())

    def logged_in(self):
        """True when Claude tasks can sign in: a stored token, or a regular login whose file is still usable.
        Read live, so a hold released by a new login is not re-held from a status cached a minute ago."""
        return True if self.configured() else self.login_state()['logged_in']

    def refresh_window(self):
        """True without a stored token while the access token is expired or about to expire: the next Claude start
        will refresh the shared login, so only one Claude task may start until the refresh is done."""
        if self.configured():
            return False
        expires = self.login_state()['access_expires_at']
        return expires is None or expires - self.clock() <= REFRESH_MARGIN

    def token_problem(self):
        """A message when the stored token needs the user's attention (empty/invalid, or about a year old), else None."""
        try:
            state = self.state_reader()
        except Exception:
            return None
        if state['state'] == 'invalid':
            return ('保管庫の長期の鍵（' + TARGET + '）が空か、形が正しくありません。通常のログインで動いているため、'
                    'ログインが消えることがあります。claude setup-token で鍵を作り、store_claude_token.py で入れ直してください。')
        if state['state'] == 'missing' and state.get('legacy'):
            return ('長期の鍵の保存先を ' + TARGET + ' に変えました（古い ' + LEGACY_TARGET + ' は、会社のネットワークの同期で'
                    '空の値に戻るため使いません）。claude setup-token で鍵を作り、store_claude_token.py で入れ直してください。')
        if state['state'] == 'ok' and state.get('persist') == 3:
            return ('長期の鍵が、会社のネットワークで同期される形で保存されています。同期で上書きされることがあるため、'
                    'store_claude_token.py で入れ直してください（この PC だけの形で保存されます）。')
        written = state.get('written_at')
        if state['state'] == 'ok' and written and self.clock() - written >= TOKEN_WARN_AGE:
            return ('長期の鍵を保存してから約11か月たちました。期限（約1年）が切れる前に、claude setup-token で作り直し、'
                    'store_claude_token.py で入れ直してください。')
        return None

    def check_token(self):
        """Tell the user once when the token problem appears or changes. Returns the current problem (or None)."""
        problem = self.token_problem()
        with self.lock:
            changed, self._warned = problem != self._warned, problem
        if problem and changed:
            self._tell(problem)
        return problem

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

    def tell(self, message):
        """A Windows notification through the same channel as sign-in problems (used for automatic failover)."""
        self._tell(message)

    def _tell(self, message):
        if self.notify:
            try:
                self.notify(message)
            except Exception:
                pass   # a notification problem never stops 采来
