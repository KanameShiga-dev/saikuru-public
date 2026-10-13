"""Provider discovery and role profiles. Never reads credential contents."""
import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROVIDERS = ('codex', 'claude', 'copilot', 'gemini')
# Gemini (2026-10-13, user decision): an emergency reserve on the free API tier, not a regular worker. The free tier
# caps requests per model per day (about 8 were enough to exhaust gemini-3.8-flash), Pro models and Google Search are
# not available, and Google may use free-tier input for training. So it takes only planner/reviewer/consultation,
# only text that passes the Input Guard, and falls back through these models when one is exhausted.
GEMINI_ROLES = ('planner', 'reviewer')
GEMINI_KEY_TARGET = 'saikuru/gemini-api-key'
GEMINI_MODELS = {'gemini-3.7-flash': 'Gemini 3.7 Flash', 'gemini-3.5-flash-lite': 'Gemini 3.5 Flash Lite',
                 'gemini-3.1-flash-lite': 'Gemini 3.1 Flash Lite', 'gemini-3.8-flash': 'Gemini 3.8 Flash'}
# Copilot has no approval hook in prompt mode, so its own edit/shell tools stay denied. It works only through 采来's
# tools: the read-only broker (planner/reviewer), the document tools, and copilot_work_tools whose writes and
# commands 采来 decides first (builder, 2026-10-09). The researcher of a development job is not covered.
COPILOT_ROLES = ('planner', 'reviewer', 'builder')
# Models confirmed to answer under the current Copilot license (all 29 catalog entries probed 2026-10-08).
# Refused by plan/policy at run time: gpt-6.1-sol, claude-fable-5.1, claude-fable-5, claude-opus-4.8-fast,
# claude-sonnet-4.6, kimi-k3. gpt-6-astra is excluded by the personal policy. Models without reasoning-effort
# support get a single nominal 'medium' and the adapter omits the flag.
# (label, supports reasoning effort, AI credits measured for one short prompt — uncached, effort low; a guide only)
COPILOT_MODELS = {
    'gpt-5-mini': ('GPT-5 mini', True, 0.06), 'gpt-5.4-mini': ('GPT-5.4 mini', True, 0.06),
    'gpt-5.4': ('GPT-5.4', True, 0.33), 'gpt-5.5': ('GPT-5.5', True, 1.4), 'gpt-5.3-codex': ('GPT-5.3 Codex', True, 0.21),
    'gpt-5.6-luna': ('GPT-5.6 Luna', True, 0.05), 'gpt-5.6-terra': ('GPT-5.6 Terra', True, 0.46),
    'gpt-5.6-sol': ('GPT-5.6 Sol', True, 0.92), 'gpt-6-luna': ('GPT-6 Luna', True, 0.02), 'gpt-6-sol': ('GPT-6 Sol', True, 0.46),
    'claude-haiku-4.5': ('Claude Haiku 4.5', False, 0.23), 'claude-sonnet-5': ('Claude Sonnet 5', True, 0.47),
    'claude-sonnet-5.5': ('Claude Sonnet 5.5', True, 0.55), 'claude-opus-4.8': ('Claude Opus 4.8', True, 1.2),
    'claude-opus-5': ('Claude Opus 5', True, 1.2), 'claude-opus-5.5': ('Claude Opus 5.5', True, 0.95),
    'gemini-3.7-flash': ('Gemini 3.7 Flash', True, 0.21), 'gemini-3.8-flash': ('Gemini 3.8 Flash', True, 0.21),
    'grok-4.5': ('Grok 4.5', True, 0.66), 'grok-4.6': ('Grok 4.6', True, 0.66), 'grok-4.7': ('Grok 4.7', True, 0.67),
    'mai-code-1.1-flash': ('MAI Code 1.1 Flash', True, 0.01)}


def claude_config_directory():
    """Use a separate per-user login; never copy another application's credentials."""
    return Path.home() / '.claude-sairai'


def discover():
    os.environ['CLAUDE_CONFIG_DIR'] = str(claude_config_directory())
    home = Path.home()
    codex = home / 'AppData/Roaming/npm/node_modules/@openai/codex/bin/codex.js'
    node = shutil.which('node')
    codex_command = [node, str(codex)] if node and codex.is_file() else []
    claude_command = []
    executable = shutil.which('claude.exe')
    if executable and 'WindowsApps' not in executable:
        claude_command = [executable]
    if not claude_command:
        roots = [home / '.local/bin', home / 'AppData/Roaming/Claude/claude-code']
        packages = home / 'AppData/Local/Packages'
        if packages.is_dir():
            roots += [p / 'LocalCache/Roaming/Claude/claude-code' for p in packages.glob('Claude_*')]
        candidates = []
        for root in roots:
            if (root / 'claude.exe').is_file():
                candidates.append(root / 'claude.exe')
            if root.is_dir():
                candidates.extend(root.glob('*/claude.exe'))
        if candidates:
            newest = max(candidates, key=lambda p: p.stat().st_mtime)
            claude_command = [str(newest)]
    copilot_command = []
    executable = shutil.which('copilot.exe')
    if executable and 'WindowsApps' not in executable:
        copilot_command = [executable]
    else:
        # WinGet installs do not reach the PATH of already-running processes.
        packages = home / 'AppData/Local/Microsoft/WinGet/Packages'
        candidates = list(packages.glob('GitHub.Copilot_*/copilot.exe')) if packages.is_dir() else []
        if candidates:
            copilot_command = [str(max(candidates, key=lambda p: p.stat().st_mtime))]
    gemini_command = []
    executable = shutil.which('gemini.cmd')
    if not executable:
        # WinGet's Node.js reaches the PATH of new processes only; look in its fixed package folder.
        packages = home / 'AppData/Local/Microsoft/WinGet/Packages'
        candidates = list(packages.glob('OpenJS.NodeJS*/node-*/gemini.cmd')) if packages.is_dir() else []
        executable = str(max(candidates, key=lambda p: p.stat().st_mtime)) if candidates else None
    if executable:
        gemini_command = [executable]
    return {'codex': codex_command, 'claude': claude_command, 'copilot': copilot_command, 'gemini': gemini_command}


def default_provider_settings():
    return {'enabled': ['codex', 'claude'], 'copilot_monthly_credits': None}


def default_config():
    from team_worktime import DEFAULT
    return {
        'schema_version': 1,
        'automatic_operations': False,
        'computer_use_allowed': False,
        'decision': {'provider': 'ollama', 'model': 'tev1:0.8b', 'shadow': True},
        'local_summary': {'mode': 'shadow', 'model': 'gemma4:latest',
                          'min_chars': 2400, 'max_chars': 16000, 'timeout_seconds': 30},
        'max_parallel_projects': 2,
        'security_review': 'always',
        'review_context_mode': 'off',
        'document_review_relaxed': False,
        'task_timeout_seconds': 1800,
        'approval_timeout_seconds': 900,
        'max_repairs': 2,
        'notifications': {'windows_enabled': False, 'business_hours_only': True},
        'business_hours': json.loads(json.dumps(DEFAULT)),
        'roles': {'planner': 'codex-standard', 'builder': 'claude-standard',
                  'researcher': 'codex-standard', 'reviewer': 'codex-standard'},
        'profiles': {
            'codex-standard': {'adapter': 'codex', 'model': 'gpt-6-sol', 'effort': 'medium'},
            'claude-standard': {'adapter': 'claude', 'model': 'sonnet', 'effort': 'medium'}
        },
        'approved_roots': [str(ROOT / 'sample-project')],
        'provider_settings': default_provider_settings()
    }


def load_config(data):
    path = Path(data) / 'config.json'
    if not path.exists():
        path.write_text(json.dumps(default_config(), ensure_ascii=False, indent=2), encoding='utf-8')
    config = json.loads(path.read_text(encoding='utf-8'))
    config.setdefault('business_hours', default_config()['business_hours'])
    config.setdefault('notifications', default_config()['notifications'])
    config.setdefault('provider_settings', default_provider_settings())
    config.setdefault('local_summary', default_config()['local_summary'])
    validate_config(config)
    return config


def validate_config(config):
    from team_local_summary import settings
    settings(config)
    from team_worktime import validate, DEFAULT
    validate(config.get('business_hours', DEFAULT))
    notifications = config.get('notifications', {'windows_enabled':False,'business_hours_only':True})
    if not isinstance(notifications, dict) or set(notifications) != {'windows_enabled','business_hours_only'} or any(type(v) is not bool for v in notifications.values()):
        raise ValueError('通知設定が不正です。')
    decision = config.get('decision', {'provider':'ollama','model':'tev1:0.8b','shadow':True})
    days = config.get('history_days', 7)
    if type(days) is not int or not 1 <= days <= 3650:
        raise ValueError('履歴の表示期間は1〜3650日の整数で指定してください。')
    if (not isinstance(decision, dict) or set(decision) != {'provider','model','shadow'}
            or decision['provider'] not in {'auto','ollama','decisions','luna','disabled'}
            or decision['model'] not in {'tev1:0.8b','tev1:4b'} or decision['shadow'] is not True):
        raise ValueError('判断設定が不正です。現在は安全評価中のためシャドーモードのみ対応します。')
    if config.get('schema_version') != 1:
        raise ValueError('未対応の設定形式です。')
    route_eval = config.get('routing_evaluation')
    if route_eval is not None:
        if (not isinstance(route_eval, dict) or set(route_eval) != {'enabled', 'root'}
                or type(route_eval['enabled']) is not bool or not isinstance(route_eval['root'], str)):
            raise ValueError('軽い経路の評価設定が不正です。')
        if route_eval['enabled'] and (not Path(route_eval['root']).is_absolute()
                                      or not Path(route_eval['root']).is_dir()):
            raise ValueError('軽い経路の評価ルートは既存の絶対パスが必要です。')
        if route_eval['enabled']:
            root = Path(route_eval['root']).resolve()
            if ROOT.resolve().is_relative_to(root) or root == (ROOT / 'data').resolve():
                raise ValueError('軽い経路の評価ルートに通常サービスのフォルダは使えません。')
    if type(config.get('automatic_operations', False)) is not bool:
        raise ValueError('操作自動承認の設定は真偽値で指定してください。')
    if type(config.get('document_review_relaxed', False)) is not bool:
        raise ValueError('document_review_relaxed は真偽値を指定してください。')
    review_context_mode = config.get('review_context_mode', 'off')
    if review_context_mode not in ('off', 'shadow', 'on'):
        raise ValueError('レビュー文脈の評価モードが不正です。')
    if review_context_mode != 'off' and not (route_eval and route_eval['enabled']):
        raise ValueError('レビュー文脈の比較は隔離評価ルートでだけ有効にできます。')
    if config.get('security_review', 'always') not in ('always', 'planner'):
        raise ValueError('security_review は always または planner を指定してください。')
    if type(config.get('computer_use_allowed', False)) is not bool:
        raise ValueError('Computer Useの設定は真偽値で指定してください。')
    settings = config.get('provider_settings', default_provider_settings())
    if not isinstance(settings, dict) or set(settings) != {'enabled', 'copilot_monthly_credits'}:
        raise ValueError('プロバイダ設定が不正です。')
    enabled = settings['enabled']
    if (not isinstance(enabled, list) or not enabled or len(set(enabled)) != len(enabled)
            or any(name not in PROVIDERS for name in enabled)):
        raise ValueError('使用するプロバイダを1つ以上選択してください。')
    credits = settings['copilot_monthly_credits']
    if credits is not None and (type(credits) not in (int, float) or not 0 < credits <= 10_000_000):
        raise ValueError('GitHub Copilotの月間クレジット上限は正の数で入力してください。')
    if 'context_optimization' in config:
        from team_context_optimizer import validate as validate_context_optimization
        validate_context_optimization(config['context_optimization'])
    for role in ('planner', 'builder', 'researcher', 'reviewer'):
        if config['roles'].get(role) not in config['profiles']:
            raise ValueError('役割のプロファイルが見つかりません。')
        adapter = config['profiles'][config['roles'][role]].get('adapter')
        if adapter not in enabled:
            raise ValueError('役割の担当に、使用しないプロバイダが設定されています。先に担当を変更してください。')
        if adapter == 'copilot' and role not in COPILOT_ROLES:
            raise ValueError('GitHub Copilotは計画・実装・レビュー担当で選択できます（調査担当は未対応）。')
        if adapter == 'gemini' and role not in GEMINI_ROLES:
            raise ValueError('Geminiは計画・レビュー担当（非常用）でだけ選択できます。')
    for profile in config['profiles'].values():
        if profile['adapter'] not in PROVIDERS:
            raise ValueError('未対応の接続部です。')
        if not isinstance(profile['model'], str) or not profile['model'].strip():
            raise ValueError('モデル名が必要です。')
        if profile.get('effort') not in ('low', 'medium'):
            raise ValueError('この版はlow/mediumのみ対応。Highの証拠ゲートを迂回しません。')
        if profile['adapter'] in ('codex', 'copilot') and 'astra' in profile['model'].lower():
            raise ValueError('現行の個人方針でAstraをワーカーに使うことは禁止されています。')


def environment_status(commands, check_auth=True):
    result = {}
    for name, command in commands.items():
        if not command:
            result[name] = {'available': False, 'version': '', 'note': 'CLIが見つかりません。'}
            continue
        try:
            run = subprocess.run(command + ['--version'], capture_output=True, text=True,
                                 encoding='utf-8', errors='replace', timeout=15,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            result[name] = {'available': run.returncode == 0, 'version': run.stdout.strip()[:200],
                            'note': 'CLI起動を確認。認証・モデル接続はタスク実行時に確認。', 'command': command}
            if name == 'claude' and run.returncode == 0 and check_auth:
                try:
                    auth = subprocess.run(command + ['auth', 'status', '--json'], capture_output=True,
                        text=True, encoding='utf-8', errors='replace', timeout=15,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    logged_in = json.loads(auth.stdout).get('loggedIn') is True
                    result[name]['authenticated'] = logged_in
                    result[name]['note'] = ('CLIログイン済み。モデル接続は実行時に確認。' if logged_in else
                        '未ログイン。単独起動するClaude Codeでログインが必要です。')
                except subprocess.TimeoutExpired:
                    # The CLI itself started; a slow login check is not "cannot start" (seen right after a restart).
                    result[name]['note'] = 'CLI起動済み。ログイン状態の確認が時間切れのため、まもなく再確認します。'
                except (ValueError, AttributeError):
                    result[name]['note'] = 'CLI起動済み。ログイン状態を判定できません。'
            if name == 'copilot' and run.returncode == 0:
                result[name]['note'] = ('CLI起動を確認。計画・実装・レビュー担当（道具は采来の確認付きのものだけ）。'
                                        '未ログインの場合は端末で copilot login を実行してください。')
            if name == 'gemini' and run.returncode == 0:
                result[name]['note'] = ('CLI起動を確認。無料枠の非常用（計画・レビューのみ、1日の回数に上限あり、'
                                        '入力が学習に使われる可能性）。APIキーは資格情報マネージャーに保管。')
        except (OSError, subprocess.TimeoutExpired):
            result[name] = {'available': False, 'version': '', 'note': 'CLIを起動できません。'}
    return result
