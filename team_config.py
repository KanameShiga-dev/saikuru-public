"""Provider discovery and role profiles. Never reads credential contents."""
import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROVIDERS = ('codex', 'claude', 'copilot')
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
    return {'codex': codex_command, 'claude': claude_command, 'copilot': copilot_command}


def default_provider_settings():
    return {'enabled': ['codex', 'claude'], 'copilot_monthly_credits': None}


def default_config():
    from team_worktime import DEFAULT
    return {
        'schema_version': 1,
        'automatic_operations': False,
        'computer_use_allowed': False,
        'decision': {'provider': 'ollama', 'model': 'tev1:0.8b', 'shadow': True},
        'max_parallel_projects': 2,
        'security_review': 'always',
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
    validate_config(config)
    return config


def validate_config(config):
    from team_worktime import validate, DEFAULT
    validate(config.get('business_hours', DEFAULT))
    notifications = config.get('notifications', {'windows_enabled':False,'business_hours_only':True})
    if not isinstance(notifications, dict) or set(notifications) != {'windows_enabled','business_hours_only'} or any(type(v) is not bool for v in notifications.values()):
        raise ValueError('通知設定が不正です。')
    decision = config.get('decision', {'provider':'ollama','model':'tev1:0.8b','shadow':True})
    if (not isinstance(decision, dict) or set(decision) != {'provider','model','shadow'}
            or decision['provider'] not in {'auto','ollama','decisions','luna','disabled'}
            or decision['model'] not in {'tev1:0.8b','tev1:4b'} or decision['shadow'] is not True):
        raise ValueError('判断設定が不正です。現在は安全評価中のためシャドーモードのみ対応します。')
    if config.get('schema_version') != 1:
        raise ValueError('未対応の設定形式です。')
    if type(config.get('automatic_operations', False)) is not bool:
        raise ValueError('操作自動承認の設定は真偽値で指定してください。')
    if type(config.get('document_review_relaxed', False)) is not bool:
        raise ValueError('document_review_relaxed は真偽値を指定してください。')
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
    for role in ('planner', 'builder', 'researcher', 'reviewer'):
        if config['roles'].get(role) not in config['profiles']:
            raise ValueError('役割のプロファイルが見つかりません。')
        adapter = config['profiles'][config['roles'][role]].get('adapter')
        if adapter not in enabled:
            raise ValueError('役割の担当に、使用しないプロバイダが設定されています。先に担当を変更してください。')
        if adapter == 'copilot' and role not in COPILOT_ROLES:
            raise ValueError('GitHub Copilotは計画・実装・レビュー担当で選択できます（調査担当は未対応）。')
    for profile in config['profiles'].values():
        if profile['adapter'] not in PROVIDERS:
            raise ValueError('未対応の接続部です。')
        if not isinstance(profile['model'], str) or not profile['model'].strip():
            raise ValueError('モデル名が必要です。')
        if profile.get('effort') not in ('low', 'medium'):
            raise ValueError('この版はlow/mediumのみ対応。Highの証拠ゲートを迂回しません。')
        if profile['adapter'] in ('codex', 'copilot') and 'astra' in profile['model'].lower():
            raise ValueError('現行の個人方針でAstraをワーカーに使うことは禁止されています。')


def environment_status(commands):
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
            if name == 'claude' and run.returncode == 0:
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
        except (OSError, subprocess.TimeoutExpired):
            result[name] = {'available': False, 'version': '', 'note': 'CLIを起動できません。'}
    return result
