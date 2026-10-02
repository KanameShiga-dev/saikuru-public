"""Local intake classification only. Never selects permissions or bypasses planning."""
import re
import time
from team_ollama_decisions import OllamaSystemOneProvider

LABELS = {'research': '調査', 'implementation': '実装', 'review': 'レビュー',
          'normal': '通常モデルで整理', 'human': '人の確認が必要'}

def classify_intake(title, goal, settings):
    started = time.monotonic()
    text = (title + '\n' + goal).casefold()
    groups = {
        'research': ('調査', '調べ', '確認', 'research', 'investigate'),
        'implementation': ('実装', '修正', '作成', '追加', 'implement', 'fix'),
        'review': ('レビュー', '監査', 'review', 'audit'),
    }
    hints = [k for k, terms in groups.items() if any(t in text for t in terms)]
    risk = any(t in text for t in ('削除', '公開', '権限変更', 'delete', 'publish', 'deploy', '認証情報'))
    result = {'classification': 'normal', 'label': LABELS['normal'], 'provider': 'rules',
              'reason': 'complex_or_ambiguous', 'scope': 'intake_only', 'permission_granted': False}
    if risk:
        result.update(classification='human', reason='important_approval_remains_human')
    elif settings.get('provider', 'ollama') == 'disabled':
        result['reason'] = 'provider_disabled'
    elif len(hints) == 1 and len(text) <= 500:
        try:
            choices = {'research':'Choose when task_hints contains research: source investigation.',
                       'implementation':'Choose when task_hints contains implementation: scoped implementation.',
                       'review':'Choose when task_hints contains review: inspect existing changes.',
                       'normal':'Choose if no single task_hint identifies a category.'}
            # Send enum metadata only. No source, path, request text or credentials.
            data = OllamaSystemOneProvider(settings.get('model', 'tev1:0.8b'))._post({
                'state': {'task_hints': hints, 'complexity': 'simple'},
                'questions': {'route': {'type': 'choice', 'instructions':
                    'Classify the supplied intake metadata. Select normal if ambiguous. Never authorize execution.',
                    'criteria': choices}}}, 5)
            candidate = data.get('answers', {}).get('route', {}).get('choice')
            if candidate not in choices: raise ValueError('invalid_choice')
            # A conflicting label is uncertain; retain the normal planner path.
            result.update(classification=candidate if candidate in (hints[0], 'normal') else 'normal',
                          provider='ollama', reason='local_intake_classification',
                          usage={k:v for k,v in data.get('usage', {}).items()
                                 if k in ('input_tokens','output_tokens') and type(v) is int and v >= 0})
        except Exception:
            result['reason'] = 'local_failure_use_normal_planner'
    result.update(label=LABELS[result['classification']], latency_ms=round((time.monotonic()-started)*1000))
    return result

def fixed_agent(baseline):
    return {'decision':baseline, 'effective':baseline, 'baseline':baseline, 'provider':'rules',
            'reason_code':'development_uses_configured_agent', 'kind':'agent_route',
            'status':'ok', 'shadow':False, 'differs':False, 'latency_ms':0}
