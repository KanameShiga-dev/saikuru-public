"""Bounded Sairai operation tests: synthetic data only, durable evidence, no OS input."""
import json
import secrets
import threading
import time
from pathlib import Path
from team_ui_actions import decide

GOALS = {
    'search': 'Show only alpha tasks whose status is todo: alpha21, alpha22, alpha31.',
    'page': 'Display page 2, with alpha21 as the first visible task.',
    'update': 'Find task 21 and change its status to done, confirming the saved screen state.',
    'empty': 'Search for zzznomatch and confirm the no-results message.'}


def validate(s):
    if not isinstance(s, dict) or set(s) != {'query', 'status', 'page', 'message', 'rows', 'nextEnabled'}:
        raise ValueError('操作テストの画面状態が不正です。')
    if s['query'] not in ('', 'alpha', 'zzznomatch') or s['status'] not in ('', 'todo'):
        raise ValueError('許可されていない入力です。')
    if s['page'] not in range(1, 4) or type(s['page']) is not int or type(s['nextEnabled']) is not bool:
        raise ValueError('ページ状態が不正です。')
    if s['message'] not in ('', '該当するタスクはありません。') or not isinstance(s['rows'], list) or len(s['rows']) > 20:
        raise ValueError('画面範囲が不正です。')
    ids = []
    for r in s['rows']:
        if set(r) != {'id', 'title', 'status'} or type(r['id']) is not int or r['id'] not in range(1, 42):
            raise ValueError('テスト以外のデータは送信できません。')
        expected = ('alpha' if r['id'] in (21, 22, 23, 31) else 'task') + str(r['id'])
        if r['title'] != expected or r['status'] not in ('todo', 'done'):
            raise ValueError('テスト以外のデータは送信できません。')
        ids.append(r['id'])
    if len(set(ids)) != len(ids): raise ValueError('重複した行です。')
    return json.loads(json.dumps(s))


def achieved(goal, s):
    if goal == 'search':
        return s['query'] == 'alpha' and s['status'] == 'todo' and [r['id'] for r in s['rows']] == [21, 22, 31]
    if goal == 'page': return s['page'] == 2 and bool(s['rows']) and s['rows'][0]['id'] == 21
    if goal == 'update': return any(r['id'] == 21 and r['status'] == 'done' for r in s['rows'])
    return s['query'] == 'zzznomatch' and not s['rows'] and s['message'] == '該当するタスクはありません。'


class UiAutomation:
    def __init__(self, directory, selector=decide, settings=None):
        self.root = Path(directory).resolve() / 'ui-automation'
        self.root.mkdir(exist_ok=True)
        self.selector = selector
        self.settings = settings or (lambda: {'provider': 'ollama', 'model': 'tev1:0.8b'})
        self.lock = threading.RLock()
        self.sessions = {}
        # Restarted sessions cannot be resumed with stale browser state.
        for p in self.root.glob('*/result.json'):
            try:
                s = json.loads(p.read_text(encoding='utf-8'))
                if s['status'] == 'running':
                    s['status'] = 'interrupted'; s['busy'] = False
                    p.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding='utf-8')
            except (ValueError, KeyError, OSError): pass

    def save(self, s):
        path = self.root / s['id'] / 'result.json'
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(path)

    def start(self, body):
        if body.get('goal') not in GOALS or body.get('mode') not in ('model', 'hybrid') or body.get('consent') is not True:
            raise ValueError('対象・モード・利用枠使用の確認が必要です。')
        with self.lock:
            if any(s['status'] == 'running' or s['busy'] for s in self.sessions.values()):
                raise ValueError('操作テストは同時に1件だけ実行できます。')
            ident = secrets.token_hex(12); folder = self.root / ident; folder.mkdir()
            (folder / 'AGENTS.md').write_text('Decision only. No file access, commands, tools or questions.\n', encoding='utf-8')
            s = {'id': ident, 'name': body['goal'], 'arm': body['mode'],
                'activeProvider': 'ollama' if body['mode'] == 'hybrid' else 'model',
                'status': 'running', 'created_at': time.time(), 'revision': 0, 'busy': False,
                'steps': [], 'switches': [], 'decisionFailures': [], 'noProgress': 0}
            adapter = body.get('normal_adapter', 'claude')
            if adapter not in ('claude', 'codex'): raise ValueError('引き継ぎ先はClaudeまたはCodexを指定してください。')
            s['normal_adapter'] = adapter
            s['decision_calls'] = 0
            s['policy'] = 'rules_then_simple_local_then_normal'
            self.sessions[ident] = s; self.save(s)
            return {'id': ident, 'revision': 0, 'status': 'running'}

    def switch(self, s, reason, visible):
        if s['activeProvider'] == 'ollama':
            s['switches'].append({'from': 'ollama', 'to': 'model', 'reason': reason,
                'after_actions': len(s['steps']), 'state': visible})
            s['activeProvider'] = 'model'

    def next(self, body):
        visible = validate(body.get('visible'))
        with self.lock:
            s = self.sessions[body['id']]
            if s['status'] != 'running': return {'status': s['status']}
            if s['busy'] or body.get('revision') != s['revision']: raise ValueError('古い操作・重複操作を拒否しました。')
            if s['steps'] and 'after' not in s['steps'][-1]:
                previous = s['steps'][-1]
                previous['after'] = visible
                s['noProgress'] = s['noProgress'] + 1 if previous['before'] == visible else 0
            if achieved(s['name'], visible):
                s['status'] = 'passed'; self.save(s); return {'status': 'passed', 'result': s}
            if len(s['steps']) >= 8:
                s['status'] = 'action_limit'; self.save(s); return {'status': s['status'], 'result': s}
            if s.get('decision_calls', 0) >= 12:
                s['status'] = 'action_limit'; self.save(s); return {'status': s['status'], 'result': s}
            if s['noProgress'] >= 2: self.switch(s, 'two_consecutive_actions_without_visible_progress', visible)
            target = 'zzznomatch' if s['name'] == 'empty' else 'alpha'
            choices = {'SEARCH': 'Click Search to apply current search text and status filter.', 'STOP': 'Stop if no safe action can progress.'}
            if visible['query'] != target: choices['FILL_QUERY'] = f'Fill Search field with {target}; Search applies it afterward.'
            if visible['status'] != 'todo': choices['FILTER_TODO'] = 'Select todo in global status filter; then click Search.'
            if visible['nextEnabled']: choices['NEXT'] = 'Click enabled Next page button.'
            if any(r['id'] == 21 and r['status'] != 'done' for r in visible['rows']): choices['UPDATE_21'] = 'Set synthetic task 21 status to done.'
            if s['name'] == 'page':
                choices = {'STOP': 'Stop if no enabled next page exists.'}
                if visible['nextEnabled']: choices['NEXT'] = 'Go to the next page.'
            # Apply local semantic choice only to a small, read-only choice set.
            if s['name'] != 'page' or len(choices) > 3 or self.settings().get('provider') == 'disabled':
                self.switch(s, 'complex_goal_use_normal_model', visible)
            state = {'goal': GOALS[s['name']], 'visible': visible, 'choices': choices}
            state['normal_adapter'] = s.get('normal_adapter', 'claude')
            state['local_model'] = self.settings().get('model', 'tev1:0.8b')
            if s['switches']:
                state['handoff'] = {'reason': s['switches'][0]['reason'],
                    'previous_actions': [{'action': p['action'], 'before': {k:v for k,v in p['before'].items() if k != 'rows'},
                        'after': {k:v for k,v in p.get('after', {}).items() if k != 'rows'}} for p in s['steps']],
                    'instruction': 'Continue from current state. Avoid repeating actions that made no progress.'}
            s['busy'] = True; self.save(s)
            provider = s['activeProvider']
            s['decision_calls'] = s.get('decision_calls', 0) + 1
        try:
            try: result = self.selector(self.root / s['id'], provider, state)
            except Exception as exc: result = {'action': 'STOP', 'arm': provider, 'error': type(exc).__name__,
                'reason_code': getattr(exc, 'code', 'decision_failure'), 'usage': getattr(exc, 'usage', {})}
            with self.lock:
                s['busy'] = False
                if s['status'] != 'running':
                    s['decisionFailures'].append(result); self.save(s); return {'status': s['status']}
                action = result.get('action')
                if action not in choices or action == 'STOP':
                    if provider == 'ollama':
                        s['decisionFailures'].append(result)
                        self.switch(s, 'provider_error' if result.get('error') else 'provider_stop' if action == 'STOP' else 'invalid_action', visible)
                        self.save(s)
                        return {'status': 'retry', 'revision': s['revision'], 'provider': 'model'}
                    s['status'] = 'provider_error' if result.get('error') else 'provider_stop' if action == 'STOP' else 'invalid_action'
                    s['error'] = result; self.save(s); return {'status': s['status'], 'result': s}
                s['steps'].append({'before': visible, 'action': action, 'decision': result})
                s['revision'] += 1; self.save(s)
                return {'status': 'action', 'action': action, 'revision': s['revision'], 'provider': provider,
                    'switches': len(s['switches']), 'usage': result.get('usage', {})}
        finally:
            with self.lock:
                s['busy'] = False

    def cancel(self, body):
        with self.lock:
            s = self.sessions[body['id']]
            if s['status'] == 'running': s['status'] = 'cancelled'
            self.save(s); return {'status': s['status']}

    def history(self):
        values = []
        for p in sorted(self.root.glob('*/result.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:100]:
            try: values.append(json.loads(p.read_text(encoding='utf-8')))
            except (OSError, ValueError): pass
        return {'records': values}
