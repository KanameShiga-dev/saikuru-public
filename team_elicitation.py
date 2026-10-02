"""Bridge bounded MCP confirmation forms; never fabricate authentication proofs."""
from pathlib import Path
import re
from team_adapters import ProviderError


def reply_to_elicitation(ctx, params):
    request = params.get('request') or params
    if not isinstance(request, dict):
        raise ProviderError('MCP承認要求の形式が不正です。')
    mode = request.get('mode')
    message = str(request.get('message') or '').casefold()
    server = str(params.get('serverName') or '').casefold()
    computer_request = (server in ('computer-use', 'computer_use')
                        or 'computer use' in message or 'computer-use' in message
                        or (server == 'node_repl' and re.fullmatch(r'allow codex to use .+\?', message.strip())))
    if computer_request and not ctx.engine.config.get('computer_use_allowed', False):
        ctx.event('利用者方針によりComputer Useは基本禁止です。画面操作を自動承認しません。プログラムによる検証へ切り替えてください。')
        return {'action': 'decline', 'content': None}
    if mode not in ('form', 'openai/form', 'openaiForm'):
        raise ProviderError('このMCP要求には専用の認証・確認画面が必要です。URLや本人確認を自動承認できません。')
    schema = request.get('requestedSchema') or {}
    properties = schema.get('properties') or {}
    required = schema.get('required') or []
    import json
    ctx.event('MCP確認の項目構造: ' + json.dumps({'server': params.get('serverName'), 'mode': mode,
        'schema_keys': list(schema), 'fields': {k: v.get('type') for k, v in properties.items() if isinstance(v, dict)},
        'required': required}, ensure_ascii=False)[:1800])
    if schema.get('type') != 'object' or len(properties) > 1 or (not properties and required):
        raise ProviderError('MCP確認フォームが対応範囲外です。操作確認以外の入力を推測しません。')
    name, field = next(iter(properties.items())) if properties else (None, {})
    if name is not None and (name not in ('approve', 'approved', 'allow', 'allowed', 'confirm', 'consent', 'action', 'decision') or required not in ([], [name])):
        raise ProviderError('MCP確認項目を操作承認と確認できませんでした。')
    if name is None:
        # Computer Use uses an empty-object form: the action is the approval.
        content = {}
    elif field.get('type') == 'boolean':
        value = True
        content = {name: value}
    elif field.get('type') == 'string':
        accepted = [v for v in field.get('enum', []) if v in ('approve', 'approved', 'accept', 'allow', 'yes')]
        if len(accepted) != 1:
            raise ProviderError('MCP承認の選択値を一意に確認できませんでした。')
        value = accepted[0]
        content = {name: value}
    else:
        raise ProviderError('MCP承認の入力形式に対応していません。')
    message = request.get('message')
    if not isinstance(message, str) or not message.strip() or len(message) > 3000:
        raise ProviderError('承認対象の説明が不足しています。')
    # A session-local app confirmation can reuse this user's explicit scope.
    # Every other request reaches the board even with automatic_operations enabled.
    project_ok = False  # Distribution builds do not inherit personal GUI approvals.
    target = ''  # No personal application target in distribution.
    text = message.casefold()
    scoped = (project_ok and ctx.task['role'] == 'builder'
              and params.get('serverName') in ('node_repl', 'computer-use', 'computer_use')
              and re.search(r'(?<![a-z0-9_])' + target + r'(?![a-z0-9_])', text)
              and ('computer use' in text or 'computer-use' in text
                   or text.strip() == 'allow codex to use bravestrategypcruntimesmoke?')
              and not any(word in text for word in ('always', 'permanent', 'all apps', '永久', 'delete', 'upload', 'payment', 'password')))
    answer = ctx.approve({'source': 'codex', 'operation': 'mcpServer/elicitation/request',
        'force_manual': not scoped, 'details': {'server': params.get('serverName'),
            'mode': mode, 'message': message, 'field': name}})
    ctx.event('MCP承認要求へ回答しました（対象ゲームの既存承認）' if scoped else 'MCP承認要求へボードの判断を返しました。')
    return {'action': 'accept' if answer['allow'] else 'decline',
            'content': content if answer['allow'] else None}
