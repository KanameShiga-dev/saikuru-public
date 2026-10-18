"""CLI model discovery without starting an inference turn."""
import re
import subprocess
import threading
import time
import logging
from team_adapters import Process
from team_config import COPILOT_MODELS, PROVIDERS, ROOT
from team_usage import Deadline, UsageUnavailable
from team_claude_auth import claude_env, is_auth_error, stored_token_state


log = logging.getLogger(__name__)


class ModelDiscoveryError(Exception):
    """A safe, user-facing model-catalog failure. Never carries CLI stderr or credential values."""
    def __init__(self, code, note):
        super().__init__(note)
        self.code = code
        self.note = note


CLAUDE_CATALOG_NOTES = {
    'auth_unavailable': 'Claudeの長期認証情報を確認できません。認証設定を確認してください。',
    'auth_rejected': 'Claudeのモデル一覧取得で認証が拒否されました。長期認証状態を確認してください。',
    'cli_unavailable': 'Claude CLIが見つかりません。CLIの導入状態を確認してください。',
    'cli_start_failed': 'Claude CLIを起動できません。実行ファイルと起動条件を確認してください。',
    'workdir_invalid': 'Claude CLIの安全な作業場所を確認できません。隔離版の設置状態を確認してください。',
    'timeout': 'Claude CLIのモデル一覧取得が時間切れになりました。時間をおいて再取得してください。',
    'communication_error': 'Claude CLIとの通信に失敗しました。ネットワーク状態を確認してください。',
    'catalog_rejected': 'Claude CLIがモデル一覧要求を拒否しました。CLIと認証設定を確認してください。',
    'catalog_incompatible': 'Claude CLIのモデル一覧応答形式を認識できません。CLIのバージョンを確認してください。',
    'catalog_empty': 'Claude CLIからモデル候補が返りませんでした。アカウントで利用可能なモデルを確認してください。',
    'catalog_error': 'Claude CLIのモデル一覧取得に失敗しました。CLIと接続状態を確認してください。',
}


def _claude_error(code):
    return ModelDiscoveryError(code, CLAUDE_CATALOG_NOTES[code])


def codex_models(command):
    process = Process(command + ['app-server', '--listen', 'stdio://'], ROOT)
    deadline, counter = Deadline(), 0
    try:
        def request(method, params):
            nonlocal counter
            counter += 1
            process.send({'id': counter, 'method': method, 'params': params})
            while True:
                msg = process.receive(deadline)
                if msg.get('id') == counter and 'method' not in msg:
                    if 'error' in msg:
                        raise ValueError('モデル一覧を取得できません。')
                    return msg.get('result') or {}
                if 'id' in msg and 'method' in msg:
                    process.send({'id': msg['id'], 'error': {'code': -32601, 'message': 'Catalog only'}})
        request('initialize', {'clientInfo': {'name': 'agent_team_models', 'version': '0.1.0'}})
        process.send({'method': 'initialized', 'params': {}})
        models, cursor = [], None
        for _ in range(20):
            data = request('model/list', {'limit': 100, 'includeHidden': False, 'cursor': cursor})
            for row in data.get('data', []):
                model = row.get('model')
                if not isinstance(model, str) or row.get('hidden') or 'astra' in model.lower():
                    continue
                efforts = [e['reasoningEffort'] for e in row.get('supportedReasoningEfforts', [])
                           if e.get('reasoningEffort') in ('low', 'medium')]
                if efforts:
                    models.append({'id': model, 'label': row.get('displayName') or model, 'efforts': efforts})
            cursor = data.get('nextCursor')
            if not cursor:
                return models
        raise ValueError('モデル一覧のページ数が上限を超えました。')
    finally:
        process.close()


def claude_models(command):
    if not command:
        raise _claude_error('cli_unavailable')
    if stored_token_state().get('state') != 'ok':
        # Do not silently fall back to the expiring interactive OAuth profile for catalog requests.
        raise _claude_error('auth_unavailable')
    try:
        env = claude_env()
    except Exception as exc:
        raise _claude_error('auth_unavailable') from exc
    if not env.get('CLAUDE_CODE_OAUTH_TOKEN'):
        raise _claude_error('auth_unavailable')
    env.pop('ANTHROPIC_API_KEY', None)
    cwd = ROOT.resolve()
    if not cwd.is_dir():
        raise _claude_error('workdir_invalid')
    try:
        process = Process(command + ['-p', '--restricted', '--input-format', 'stream-json',
            '--output-format', 'stream-json', '--verbose', '--no-session-persistence',
            '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--tools', '',
            '--permission-prompts', 'none'], cwd, env=env)
    except OSError as exc:
        raise _claude_error('cli_start_failed') from exc
    deadline = Deadline()
    try:
        process.send({'type': 'control_request', 'request_id': 'catalog',
                      'request': {'subtype': 'initialize', 'hooks': {}}})
        while True:
            msg = process.receive(deadline)
            if not isinstance(msg, dict):
                raise _claude_error('catalog_incompatible')
            if msg.get('type') == 'control_response':
                response = msg.get('response', {})
                if response.get('request_id') != 'catalog':
                    continue
                if response.get('subtype') == 'error':
                    if is_auth_error(response.get('error')):
                        raise _claude_error('auth_rejected')
                    raise _claude_error('catalog_rejected')
                if response.get('subtype') != 'success':
                    raise _claude_error('catalog_incompatible')
                payload = response.get('response')
                if not isinstance(payload, dict) or not isinstance(payload.get('models'), list):
                    raise _claude_error('catalog_incompatible')
                models = []
                for row in payload['models']:
                    if not isinstance(row, dict):
                        continue
                    model = row.get('value')
                    if isinstance(model, str) and model:
                        label = row.get('displayName')
                        levels = row.get('supportedEffortLevels')
                        efforts = [level for level in levels if level in ('low', 'medium')] \
                            if isinstance(levels, list) else []
                        if efforts:
                            models.append({'id': model,
                                'label': label if isinstance(label, str) and label else model,
                                'efforts': efforts})
                if not models:
                    raise _claude_error('catalog_empty' if not payload['models'] else 'catalog_incompatible')
                return models
            if msg.get('type') == 'control_request':
                process.send({'type': 'control_response', 'response': {'subtype': 'error',
                    'request_id': msg.get('request_id'), 'error': 'Catalog only'}})
    except UsageUnavailable as exc:
        raise _claude_error('timeout') from exc
    except ModelDiscoveryError:
        raise
    except Exception as exc:
        stderr = ''.join(getattr(process, 'stderr', []))
        if is_auth_error(stderr):
            code = 'auth_rejected'
        elif re.search(r'timeout|timed out|econn|network|socket|fetch failed', stderr, re.I):
            code = 'communication_error'
        else:
            code = 'catalog_error'
        raise _claude_error(code) from exc
    finally:
        process.close()


def copilot_models(command):
    run = subprocess.run(command + ['help', 'config'], capture_output=True, text=True, encoding='utf-8',
                         errors='replace', timeout=20, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if run.returncode != 0:
        raise ValueError('Copilotのモデル一覧を取得できません。')
    listed, inside = set(), False
    for line in run.stdout.splitlines():
        if re.match(r'^\s+`model`:', line):
            inside = True
        elif inside:
            match = re.match(r'^\s+- "([A-Za-z0-9._-]+)"\s*$', line)
            if not match:
                break
            listed.add(match.group(1))
    return [{'id': model, 'label': f'{label}（約{credits:g}クレジット/回）', 'efforts': ['low', 'medium'] if effort else ['medium']}
            for model, (label, effort, credits) in COPILOT_MODELS.items() if model in listed and 'astra' not in model]


def gemini_models(command):
    from team_config import GEMINI_MODELS
    return [{'id': model, 'label': label + '（無料枠・1日の回数に上限）', 'efforts': ['medium']}
            for model, label in GEMINI_MODELS.items()]


class ModelCatalog:
    def __init__(self, commands):
        self.commands = commands
        self.cache = {}
        self.locks = {name: threading.Lock() for name in PROVIDERS}

    def get(self, provider, force=False):
        if provider not in self.locks:
            raise ValueError('未対応の担当です。')
        with self.locks[provider]:
            cached = self.cache.get(provider)
            lifetime = 300 if cached and cached['models'] else 15
            if not force and cached and time.time() - cached['fetched_at'] < lifetime:
                return cached
            error = None
            try:
                command = self.commands.get(provider)
                if not command:
                    if provider == 'claude':
                        raise _claude_error('cli_unavailable')
                    raise ValueError('CLIが見つかりません。')
                rows = {'codex': codex_models, 'claude': claude_models, 'copilot': copilot_models,
                        'gemini': gemini_models}[provider](command)
                rows = list({row['id']: row for row in rows}.values())
                note = 'CLIから取得したモデル候補です。利用枠・権限によって実行時に拒否される場合があります。'
                if not rows:
                    if provider == 'claude':
                        error = 'catalog_empty'
                        note = CLAUDE_CATALOG_NOTES[error]
                    else:
                        note = '選択可能なモデルを取得できませんでした。CLIのログイン状態を確認してください。'
            except ModelDiscoveryError as exc:
                rows, note, error = [], exc.note, exc.code
            except UsageUnavailable as exc:
                rows = []
                note = CLAUDE_CATALOG_NOTES['timeout'] if provider == 'claude' else str(exc)
                error = 'timeout' if provider == 'claude' else None
            except Exception as exc:
                rows, note = [], 'モデル一覧を取得できませんでした。ログイン状態を確認して再取得してください。'
                if provider == 'claude':
                    error = 'catalog_error'
                    note = CLAUDE_CATALOG_NOTES[error]
            if error:
                log.warning('model catalog failed provider=%s category=%s', provider, error)
            value = {'models': rows, 'note': note, 'fetched_at': time.time()}
            if error:
                value['error'] = error
            self.cache[provider] = value
            return value

    def validate(self, item):
        data = self.get(item.get('adapter'))
        found = next((m for m in data['models'] if m['id'] == item.get('model')), None)
        if not found:
            raise ValueError('現在の候補にないモデルです。一覧を再取得して選択してください。')
        if item.get('effort') not in found['efforts']:
            raise ValueError('選択モデルが対応する推論設定を選択してください。')
