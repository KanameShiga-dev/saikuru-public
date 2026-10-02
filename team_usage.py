"""Read-only quota snapshots. Credentials never leave the provider request or enter state/logs."""
import copy
import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from team_adapters import Process
from team_config import ROOT, claude_config_directory


class UsageUnavailable(Exception):
    pass


def recovery_ready(snapshot, profile, minimum=10):
    """Require fresh measured common and applicable model windows, never a reset estimate."""
    provider = profile.get('adapter')
    value = snapshot.get(provider, {})
    if value.get('status') != 'ok' or value.get('stale', True):
        return False
    rows = value.get('rows', [])
    common_id = 'claude' if provider == 'claude' else 'codex'
    common = next((row for row in rows if row.get('id') == common_id), None)
    if not common or not common.get('short') or not common.get('weekly'):
        return False
    relevant = [common]
    model = profile.get('model', '').casefold()
    if provider == 'claude':
        family = next((name for name in ('opus', 'sonnet', 'haiku') if name in model), None)
        relevant += [row for row in rows if family and family in row.get('id', '').casefold() and row is not common]
    elif len(rows) > 1:
        # A different Codex bucket cannot safely be assumed to cover this model.
        return False
    for row in relevant:
        windows = [row.get('weekly')] if row.get('weekly_only') else [row.get('short'), row.get('weekly')]
        for item in windows:
            if not item or type(item.get('remaining_percent')) not in (int, float) or item['remaining_percent'] < minimum:
                return False
            if item.get('resets_at') and item['resets_at'] <= time.time():
                return False
    return True


def timestamp(value):
    if type(value) in (int, float) and math.isfinite(value):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except ValueError:
            pass
    return None


def window(raw, used_key, reset_key):
    if not isinstance(raw, dict):
        return None
    used = raw.get(used_key)
    if type(used) not in (int, float) or not math.isfinite(used):
        return None
    return {'remaining_percent': round(max(0, min(100, 100 - used)), 1),
            'resets_at': timestamp(raw.get(reset_key))}


class Deadline:
    def __init__(self):
        self.end = time.monotonic() + 25

    def check(self):
        if time.monotonic() > self.end:
            raise UsageUnavailable('利用枠の取得がタイムアウトしました。')


def codex_usage(command):
    if not command:
        raise UsageUnavailable('Codex CLIが見つかりません。')
    process = Process(command + ['app-server', '--listen', 'stdio://'], ROOT)
    deadline = Deadline()
    try:
        def request(request_id, method, params):
            process.send({'id': request_id, 'method': method, 'params': params})
            while True:
                msg = process.receive(deadline)
                if msg.get('id') == request_id and 'method' not in msg:
                    if 'error' in msg:
                        raise UsageUnavailable('Codexの利用枠を取得できません。ログイン状態を確認してください。')
                    return msg.get('result') or {}
                if 'id' in msg and 'method' in msg:
                    process.send({'id': msg['id'], 'error': {'code': -32601, 'message': 'Read-only quota client'}})
        request(1, 'initialize', {'clientInfo': {'name': 'agent_team_usage', 'version': '0.1.0'}})
        process.send({'method': 'initialized', 'params': {}})
        data = request(2, 'account/rateLimits/read', {})
    finally:
        process.close()
    buckets = data.get('rateLimitsByLimitId')
    if not isinstance(buckets, dict) or not buckets:
        single = data.get('rateLimits')
        buckets = {single.get('limitId') or 'codex': single} if isinstance(single, dict) else {}
    rows = []
    for key, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        short = weekly = None
        minutes = None
        for slot in ('primary', 'secondary'):
            item = bucket.get(slot)
            if not isinstance(item, dict):
                continue
            duration = item.get('windowDurationMins')
            if duration == 10080:
                weekly = window(item, 'usedPercent', 'resetsAt')
            elif type(duration) in (int, float) and 0 < duration < 10080:
                if minutes is None or duration < minutes:
                    short, minutes = window(item, 'usedPercent', 'resetsAt'), duration
        label = bucket.get('limitName') or ('共通枠（Codex）' if key == 'codex' else key)
        rows.append({'id': str(key)[:100], 'label': str(label)[:120],
                     'short': short, 'weekly': weekly, 'minutes': minutes})
    return rows


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def claude_usage(_command):
    # The local CLI's OAuth usage endpoint is not a stable public API.
    # Use its existing login only in memory; never copy/refresh credentials or log responses.
    directory = claude_config_directory()
    try:
        credentials = json.loads((directory / '.credentials.json').read_text(encoding='utf-8'))
        token = credentials.get('claudeAiOauth', {}).get('accessToken')
    except (OSError, ValueError, AttributeError):
        token = None
    if not isinstance(token, str) or not token:
        raise UsageUnavailable('認証情報がありません。采来専用のClaudeログインが必要です。')
    request = urllib.request.Request('https://api.anthropic.com/api/oauth/usage',
        headers={'Authorization': 'Bearer ' + token, 'anthropic-beta': 'oauth-2025-04-20',
                 'Accept': 'application/json'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        if code == 429:
            raise UsageUnavailable('取得回数の制限中です。15分後以降に再取得します。') from None
        if code in (401, 403):
            raise UsageUnavailable('残量取得側の認証・権限エラーです。CLIのログイン状態とは別に確認してください。') from None
        raise UsageUnavailable('Claudeの利用枠を取得できません。') from None
    short = window(data.get('five_hour'), 'utilization', 'resets_at')
    rows = [{'id': 'claude', 'label': '全モデル共通', 'short': short,
             'weekly': window(data.get('seven_day'), 'utilization', 'resets_at'), 'minutes': 300}]
    for key, value in data.items():
        if key.startswith('seven_day_') and isinstance(value, dict):
            weekly = window(value, 'utilization', 'resets_at')
            if weekly:
                rows.append({'id': key, 'label': key.removeprefix('seven_day_')[:100] + ' 専用枠',
                             'short': None, 'weekly': weekly, 'minutes': None, 'weekly_only': True})
    return rows


class UsageMonitor:
    def __init__(self, commands, shutdown):
        self.commands, self.shutdown = commands, shutdown
        self.lock = threading.Lock()
        self.reader_locks = {name: threading.Lock() for name in ('codex', 'claude')}
        self.values = {name: {'status': 'loading', 'rows': [], 'updated_at': None,
                            'note': '利用枠を取得中…'} for name in ('codex', 'claude')}

    def start(self):
        for name in ('codex', 'claude'):
            threading.Thread(target=self._loop, args=(name,), daemon=True).start()

    def _loop(self, name):
        delay = 300
        while not self.shutdown.is_set():
            success = self.refresh_now(name)
            delay = 300 if success else min(3600, max(900, delay * 2))
            with self.lock:
                self.values[name]['next_update_at'] = time.time() + delay
            if self.shutdown.wait(delay):
                return

    def refresh_now(self, name, mark_loading=False):
        """Re-read one provider after its CLI changes; keep failed values visibly stale."""
        readers = {'codex': codex_usage, 'claude': claude_usage}
        if name not in readers:
            raise ValueError('未対応の利用枠です。')
        with self.reader_locks[name]:
            if mark_loading:
                with self.lock:
                    self.values[name].update(status='loading', note='CLI更新後の利用枠を再取得中…')
            try:
                rows = readers[name](self.commands.get(name))
                if not any(r.get('short') or r.get('weekly') for r in rows):
                    raise UsageUnavailable('利用枠の割合が提供されていません。')
                value = {'status': 'ok', 'rows': rows, 'updated_at': time.time(),
                         'note': '5分ごとに自動取得'}
                with self.lock:
                    self.values[name] = value
                return True
            except Exception as exc:
                # Do not return raw provider errors: they may include private request data.
                note = str(exc) if isinstance(exc, UsageUnavailable) else '利用枠の取得に失敗しました。'
                with self.lock:
                    self.values[name].update(status='unavailable', note=note)
                return False

    def snapshot(self):
        with self.lock:
            result = copy.deepcopy(self.values)
        now = time.time()
        for value in result.values():
            value['stale'] = value['status'] != 'ok' or now - (value['updated_at'] or 0) > 600
            for row in value['rows']:
                for key in ('short', 'weekly'):
                    item = row.get(key)
                    if item and item['resets_at'] and item['resets_at'] <= now:
                        row[key] = None  # Reset has passed; never invent 100% remaining.
                        value['stale'] = True
        return result
