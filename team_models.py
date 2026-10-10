"""CLI model discovery without starting an inference turn."""
import re
import subprocess
import threading
import time
from team_adapters import Process
from team_config import COPILOT_MODELS, PROVIDERS, ROOT
from team_usage import Deadline


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
    process = Process(command + ['-p', '--restricted', '--input-format', 'stream-json',
        '--output-format', 'stream-json', '--verbose', '--no-session-persistence',
        '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--tools', '',
        '--permission-prompts', 'none'], ROOT / 'sample-project')
    deadline = Deadline()
    try:
        process.send({'type': 'control_request', 'request_id': 'catalog',
                      'request': {'subtype': 'initialize', 'hooks': {}}})
        while True:
            msg = process.receive(deadline)
            if msg.get('type') == 'control_response':
                response = msg.get('response', {})
                if response.get('request_id') != 'catalog':
                    continue
                if response.get('subtype') == 'error':
                    raise ValueError('Claudeモデル一覧を取得できません。')
                models = []
                for row in response.get('response', {}).get('models', []):
                    model = row.get('value')
                    if isinstance(model, str) and model:
                        models.append({'id': model, 'label': row.get('displayName') or model,
                                       'efforts': ['low', 'medium']})
                return models
            if msg.get('type') == 'control_request':
                process.send({'type': 'control_response', 'response': {'subtype': 'error',
                    'request_id': msg.get('request_id'), 'error': 'Catalog only'}})
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
            lifetime = 10 if force else (300 if cached and cached['models'] else 15)
            if cached and time.time() - cached['fetched_at'] < lifetime:
                return cached
            try:
                command = self.commands.get(provider)
                if not command:
                    raise ValueError('CLIが見つかりません。')
                rows = {'codex': codex_models, 'claude': claude_models, 'copilot': copilot_models}[provider](command)
                rows = list({row['id']: row for row in rows}.values())
                note = 'CLIから取得したモデル候補です。利用枠・権限によって実行時に拒否される場合があります。'
                if not rows:
                    note = '選択可能なモデルを取得できませんでした。CLIのログイン状態を確認してください。'
            except Exception:
                rows, note = [], 'モデル一覧を取得できませんでした。ログイン状態を確認して再取得してください。'
            value = {'models': rows, 'note': note, 'fetched_at': time.time()}
            self.cache[provider] = value
            return value

    def validate(self, item):
        data = self.get(item.get('adapter'))
        found = next((m for m in data['models'] if m['id'] == item.get('model')), None)
        if not found:
            raise ValueError('現在の候補にないモデルです。一覧を再取得して選択してください。')
        if item.get('effort') not in found['efforts']:
            raise ValueError('選択モデルが対応する推論設定を選択してください。')
