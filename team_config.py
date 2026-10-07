"""Provider discovery and role profiles. Never reads credential contents."""
import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent


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
    return {'codex': codex_command, 'claude': claude_command}


def default_config():
    from team_worktime import DEFAULT
    return {
        'schema_version': 1,
        'automatic_operations': False,
        'computer_use_allowed': False,
        'decision': {'provider': 'ollama', 'model': 'tev1:0.8b', 'shadow': True},
        'max_parallel_projects': 2,
        'security_review': 'always',
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
        'approved_roots': [str(ROOT / 'sample-project')]
    }


def load_config(data):
    path = Path(data) / 'config.json'
    if not path.exists():
        path.write_text(json.dumps(default_config(), ensure_ascii=False, indent=2), encoding='utf-8')
    config = json.loads(path.read_text(encoding='utf-8'))
    config.setdefault('business_hours', default_config()['business_hours'])
    config.setdefault('notifications', default_config()['notifications'])
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
    if config.get('security_review', 'always') not in ('always', 'planner'):
        raise ValueError('security_review は always または planner を指定してください。')
    if type(config.get('computer_use_allowed', False)) is not bool:
        raise ValueError('Computer Useの設定は真偽値で指定してください。')
    for role in ('planner', 'builder', 'researcher', 'reviewer'):
        if config['roles'].get(role) not in config['profiles']:
            raise ValueError('役割のプロファイルが見つかりません。')
    for profile in config['profiles'].values():
        if profile['adapter'] not in ('codex', 'claude'):
            raise ValueError('未対応の接続部です。')
        if not isinstance(profile['model'], str) or not profile['model'].strip():
            raise ValueError('モデル名が必要です。')
        if profile.get('effort') not in ('low', 'medium'):
            raise ValueError('この版はlow/mediumのみ対応。Highの証拠ゲートを迂回しません。')
        if profile['adapter'] == 'codex' and 'astra' in profile['model'].lower():
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
                auth = subprocess.run(command + ['auth', 'status', '--json'], capture_output=True,
                    text=True, encoding='utf-8', errors='replace', timeout=15,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                try:
                    logged_in = json.loads(auth.stdout).get('loggedIn') is True
                    result[name]['authenticated'] = logged_in
                    result[name]['note'] = ('CLIログイン済み。モデル接続は実行時に確認。' if logged_in else
                        '未ログイン。単独起動するClaude Codeでログインが必要です。')
                except (ValueError, AttributeError):
                    result[name]['note'] = 'CLI起動済み。ログイン状態を判定できません。'
        except (OSError, subprocess.TimeoutExpired):
            result[name] = {'available': False, 'version': '', 'note': 'CLIを起動できません。'}
    return result
