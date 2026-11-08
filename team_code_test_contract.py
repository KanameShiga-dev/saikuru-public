"""Per-job code-test plan and reusable test-file evidence.

This module never executes a command. Execution remains with the worker and
the existing command-approval path; only current, recorded results count.
"""

import hashlib
import re
from pathlib import Path, PureWindowsPath

from team_validation_gate import findings, validate_commands


def process_bypass_command(command):
    """Offer only a process-local option for the same relative PowerShell script."""
    match = re.fullmatch(
        r'(?i)(powershell(?:\.exe)?\s+-NoProfile)(\s+-File\s+'
        r'("\.[\\/][^"\r\n;&|<>`$]+\.ps1"|\.[\\/][^\s;&|<>`$]+\.ps1))', command)
    if not match or '..' in PureWindowsPath(match[3].strip('"')).parts:
        return None
    return match[1] + ' -ExecutionPolicy Bypass' + match[2]


def validate_plan(value):
    required_keys = {'required', 'reason', 'cases', 'command', 'existing_tests'}
    optional_keys = {'test_methods', 'preconditions', 'setup_steps', 'synthetic_accounts',
                     'ui_test_required', 'ui_test_tool', 'ui_test_reason', 'ui_test_command', 'cleanup_steps'}
    if (not isinstance(value, dict) or not required_keys <= set(value)
            or set(value) - required_keys - optional_keys):
        raise ValueError('計画にコードのテスト方針を指定してください。')
    required, reason, cases, command, existing = (
        value[key] for key in ('required', 'reason', 'cases', 'command', 'existing_tests'))
    if type(required) is not bool or not isinstance(reason, str) or len(reason) > 500:
        raise ValueError('テスト方針の適用判断が不正です。')
    if not isinstance(cases, list) or len(cases) > 20 or any(
            not isinstance(case, str) or not case.strip() or len(case) > 300 for case in cases):
        raise ValueError('テストする条件は20件以内で指定してください。')
    if not isinstance(command, str):
        raise ValueError('テストコマンドの形式が不正です。')
    if (not isinstance(existing, list) or len(existing) > 20 or
            any(not isinstance(name, str) or not name.strip() for name in existing)):
        raise ValueError('既存テストファイルの一覧が不正です。')
    def text_list(key, maximum=20):
        items = value.get(key, [])
        if (not isinstance(items, list) or len(items) > maximum
                or any(not isinstance(item, str) or not item.strip() or len(item) > 500 for item in items)):
            raise ValueError('テスト環境の準備項目が不正です。')
        return [item.strip() for item in items]

    methods = value.get('test_methods', [])
    if (not isinstance(methods, list) or len(methods) > 5
            or any(item not in ('unit', 'integration', 'api', 'ui') for item in methods)
            or len(methods) != len(set(methods))):
        raise ValueError('テスト方式の指定が不正です。')
    preconditions = text_list('preconditions')
    setup_steps = text_list('setup_steps')
    cleanup_steps = text_list('cleanup_steps')
    accounts = value.get('synthetic_accounts', [])
    if (not isinstance(accounts, list) or len(accounts) > 10
            or any(not isinstance(row, dict) or set(row) != {'purpose', 'fixture_method', 'reset_method'}
                   or any(not isinstance(row[key], str) or not row[key].strip() or len(row[key]) > 500
                          for key in ('purpose', 'fixture_method', 'reset_method')) for row in accounts)):
        raise ValueError('架空テストアカウントの準備・初期化方法が不正です。')
    ui_required = value.get('ui_test_required', False)
    ui_tool = value.get('ui_test_tool', 'not_applicable')
    ui_reason = value.get('ui_test_reason', '')
    ui_command = value.get('ui_test_command', '')
    if (type(ui_required) is not bool or ui_tool not in ('playwright', 'manual_fallback', 'not_applicable')
            or not isinstance(ui_reason, str) or len(ui_reason) > 500
            or not isinstance(ui_command, str)):
        raise ValueError('画面テストの計画が不正です。')
    if ui_required:
        if ui_tool == 'not_applicable' or 'ui' not in methods:
            raise ValueError('画面が必要なテストには画面テスト方式を指定してください。')
        if ui_tool == 'playwright':
            if not ui_command.strip():
                raise ValueError('Playwrightの画面テストコマンドがありません。')
            validate_commands([ui_command])
        elif not ui_reason.strip():
            raise ValueError('手動画面テストへ切り替える理由を記録してください。')
    elif ui_tool != 'not_applicable' or ui_command.strip():
        raise ValueError('画面テストが不要な計画に画面テストのコマンドが指定されています。')
    if required:
        if not cases:
            raise ValueError('コード変更の計画にはテストする条件が必要です。')
        if 'test_methods' in value and not methods:
            raise ValueError('コード変更の計画には実施するテスト方式が必要です。')
        if 'preconditions' in value and not preconditions:
            raise ValueError('追加の実行前提がない場合も、その旨を明記してください。')
        if 'setup_steps' in value and not setup_steps:
            raise ValueError('追加準備がない場合も、その旨を明記してください。')
        validate_commands([command])
        from team_dlp import require_clean_input
        require_clean_input(command, False, '計画のテストコマンド')
        if ui_command.strip():
            require_clean_input(ui_command, False, '計画の画面テストコマンド')
        for account in accounts:
            for field in account.values():
                require_clean_input(field, False, '架空テストアカウントの説明')
    elif not reason.strip() or cases or command or existing or methods or preconditions or setup_steps \
            or accounts or ui_required or ui_tool != 'not_applicable' or ui_command or cleanup_steps:
        raise ValueError('テスト対象外の計画には理由だけを指定してください。')
    return {'required': required, 'reason': reason.strip(),
            'cases': [case.strip() for case in cases], 'command': command.strip(),
            'existing_tests': existing, 'test_methods': methods,
            'preconditions': preconditions, 'setup_steps': setup_steps,
            'synthetic_accounts': accounts, 'ui_test_required': ui_required,
            'ui_test_tool': ui_tool, 'ui_test_reason': ui_reason.strip(),
            'ui_test_command': ui_command.strip(), 'cleanup_steps': cleanup_steps}


def _auth_test_required(text):
    term = r'(?:認証|ログイン|権限|テストアカウント)'
    feature = term + r'(?:機能|処理|システム)?'
    prohibited_action = r'(?:実装|削除|公開|権限変更|外部アクセス|コマンド実行|テスト実行)'
    # Remove explicit exclusions only; a positive requirement elsewhere still counts.
    exclusions = (
        feature + r'(?:[・/／、](?:' + feature + r'|DB|外部API|外部公開))*'
        r'(?:は|が|を)?(?:存在しない|ない|なく|ありません|がない|がなく|'
        r'確認した範囲にない|確認した範囲にはない)',
        r'架空(?:の)?(?:テスト)?アカウント(?:は|を)(?:作らない|作成しない|不要|対象外)',
        prohibited_action + r'(?:[・/／、]' + prohibited_action + r')*'
        r'(?:は|を)(?:許可しない|禁止する)',
        feature + r'(?:[・/／、](?:' + feature + r'|DB|外部API|外部公開))*'
        r'(?:は|を)?(?:設けない|実装しない|追加しない|不要|対象外|依頼対象外|なし|無し|無い|はない)',
        r'(?i)\b(?:no|without)\s+(?:authentication|authorization|login|sign.?in|accounts?)\b',
    )
    for pattern in exclusions:
        text = re.sub(pattern, '', text)
    return bool(re.search(r'(?i)\b(authentication|authorization|login|sign.?in|account|role-based)\b'
                          r'|認証|ログイン|権限|テストアカウント', text))


def _ui_test_required(text):
    """Whether what is to be built has a screen. Explicit exclusions are removed first, as for authentication."""
    screen = r'(?:Web画面|web画面|画面操作テスト|画面テスト|画面操作|画面|ブラウザ|UI|ユーザーインターフェース)'
    exclusions = (
        screen + r'(?:は|を|が|も)?(?:不要|なし|無し|ない|作らない|使わない|対象外|含めない|設けない|ありません)',
        r'(?i)\b(?:no|without)\s+(?:ui|gui|browser|screen|web\s+(?:ui|page|screen))\b',
    )
    for pattern in exclusions:
        text = re.sub(pattern, '', text)
    return bool(re.search(r'(?i)\b(web|website|browser|html|frontend|front-end|user interface|ui)\b'
                          r'|webシステム|webアプリ|ブラウザ|画面|画面操作|ユーザーインターフェース', text))


def planning_issues(result, job):
    """Catch test setup omissions before presenting a plan for approval."""
    # This contract was added after existing jobs had already been stored.
    # Do not retroactively impose its planner schema on those jobs.
    if not job.get('code_test_contract_version'):
        return []
    plan = result.get('code_test_plan') or {}
    # What is built is said by the request, the deliverables and the acceptance criteria. The plan's summary and task
    # instructions also discuss the test approach ("画面操作テストは対象外", "認証・権限機能は元の依頼にない"), which
    # must not turn into a screen or a login; the planner repeats such words when answering this very check.
    contract = result.get('goal_contract') or job.get('goal_contract') or {}
    built = ' '.join([str(job.get('goal') or '')]
                     + [str(x) for key in ('deliverables', 'acceptance_criteria') for x in contract.get(key, [])])
    ui_required = _ui_test_required(built)
    auth_required = _auth_test_required(built)
    if not plan.get('required'):
        if (job.get('code_test_contract_version') and ui_required
                and any(isinstance(t, dict) and t.get('role') == 'builder' for t in result.get('tasks') or [])):
            return ['画面を持つシステムを変更する計画ですが、テスト対象外になっています。コード/APIと画面操作の検証を計画してください。']
        return []
    issues = []
    required_fields = ('test_methods', 'preconditions', 'setup_steps', 'synthetic_accounts',
                       'ui_test_required', 'ui_test_tool', 'ui_test_reason', 'ui_test_command', 'cleanup_steps')
    missing = [key for key in required_fields if key not in plan]
    if missing:
        return ['テスト計画に実行方式・環境前提・準備/後片付け・架空アカウント・画面確認の判断がありません。']
    if not plan['test_methods'] or not any(mode in plan['test_methods'] for mode in ('unit', 'integration', 'api')):
        issues.append('コード機能のテストコードを含む unit / integration / api の方式がありません。')
    if not plan['preconditions']:
        issues.append('ランタイム、依存物、権限、必要ファイルなどの実行前提が明記されていません。')
    if not any('.local/test-tmp' in value.replace('\\', '/').casefold()
               for value in plan['preconditions']):
        issues.append('采来が準備するプロジェクト内の .local/test-tmp が実行前提に記録されていません。')
    if not plan['setup_steps']:
        issues.append('テスト環境、分離DB/サーバー、テストデータをどう準備するかが明記されていません。')
    if not plan['cleanup_steps']:
        issues.append('テスト環境や架空データをどう初期化・後片付けするかが明記されていません。')
    if auth_required and not plan['synthetic_accounts']:
        issues.append('認証/権限を試す架空アカウントの役割、作成方法、初期化方法がありません。')
    if ui_required and not plan['ui_test_required']:
        issues.append('画面を持つシステムですが、画面操作テストを計画していません。')
    if ui_required and plan['ui_test_required']:
        if 'ui' not in plan['test_methods']:
            issues.append('画面操作テストの方式uiがtest_methodsにありません。')
        if plan['ui_test_tool'] == 'playwright':
            from team_test_environment import playwright_status
            if not plan['ui_test_command']:
                issues.append('Playwrightの実行コマンドがありません。')
            elif not playwright_status(job['project'])['available']:
                issues.append('Playwrightの実行環境が未準備です。手動へ黙って切り替えず、理由を明示してください。')
        elif plan['ui_test_tool'] != 'manual_fallback' or not plan['ui_test_reason'].strip():
            issues.append('Playwrightが使えない場合の代替方法と理由が明記されていません。')
        else:
            from team_test_environment import playwright_status
            if playwright_status(job['project'])['available']:
                issues.append('Playwrightが利用可能です。手動代替ではなく、Playwrightによる画面操作テストを計画してください。')
    elif plan['ui_test_required']:
        if plan['ui_test_tool'] == 'playwright' and not plan['ui_test_command']:
            issues.append('Playwrightの実行コマンドがありません。')
        if plan['ui_test_tool'] == 'playwright':
            from team_test_environment import playwright_status
            if not playwright_status(job['project'])['available']:
                issues.append('Playwrightの実行環境が未準備です。')
    return issues


def commands(job):
    result = list(job.get('validation_commands') or [])
    plan = job.get('code_test_plan') or {}
    command = plan.get('command') if plan.get('required') else None
    if command and command not in result:
        result.append(command)
    ui_command = plan.get('ui_test_command') if plan.get('ui_test_required') else None
    if ui_command and ui_command not in result:
        result.append(ui_command)
    return result


def snapshot_files(project, files, previous=None, change_reason=''):
    """Return {relative path: SHA256}; reject missing or dropped prior tests."""
    if (not isinstance(files, list) or not 1 <= len(files) <= 20
            or any(not isinstance(name, str) for name in files)
            or len(files) != len(set(files))):
        raise ValueError('使用したテストファイルを重複なしで1〜20件報告してください。')
    if not isinstance(change_reason, str) or len(change_reason) > 1000:
        raise ValueError('テスト変更理由の形式が不正です。')
    root = Path(project).resolve()
    snapshot = {}
    for name in files:
        if (not isinstance(name, str) or not name or len(name) > 300
                or any(ord(char) < 32 for char in name)):
            raise ValueError('テストファイルは対象プロジェクトからの相対パスで指定してください。')
        relative = Path(name)
        if relative.is_absolute() or relative.drive or '..' in relative.parts or not relative.parts:
            raise ValueError('テストファイルは対象プロジェクト内に限定してください。')
        basename = relative.name.casefold()
        if not (any(part.casefold() in ('test', 'tests', '__tests__', 'spec', 'specs')
                    for part in relative.parts[:-1])
                or 'test' in basename or 'spec' in basename):
            raise ValueError('テストファイルとして識別できる名前またはテスト用フォルダを使ってください。')
        candidate = root / relative
        path = candidate.resolve()
        if (not path.is_relative_to(root) or not path.is_file() or candidate.is_symlink()
                or not 0 < path.stat().st_size <= 2_000_000):
            raise ValueError('テストファイルが存在しない、対象外、または大きすぎます。')
        key = relative.as_posix()
        if key in snapshot:
            raise ValueError('同じテストファイルを重複して報告できません。')
        snapshot[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    previous = previous or {}
    if set(previous) - set(snapshot):
        raise ValueError('前回のテストファイルが報告から外れています。既存テストを保持してください。')
    if any(previous[name] != snapshot[name] for name in previous) and not change_reason.strip():
        raise ValueError('既存テストの内容を変更した理由と仕様上の根拠を報告してください。')
    return snapshot


def verify_snapshot(project, snapshot):
    if not isinstance(snapshot, dict) or not snapshot:
        return False
    try:
        current = snapshot_files(project, list(snapshot))
    except ValueError:
        return False
    return current == snapshot


def checkpoint_test_assets(project, snapshot, allowed_paths):
    """Read only already-approved checkpoint paths, without running tests."""
    from team_checkpoint import HEX, _root, _path, _read, scope_paths
    allowed = scope_paths(allowed_paths)
    if (not isinstance(snapshot, dict) or not 1 <= len(snapshot) <= 20
            or any(name not in allowed or not isinstance(sha, str) or not HEX.fullmatch(sha)
                   for name, sha in snapshot.items())):
        return {'valid': False, 'changed_paths': [], 'tracked_paths': [], 'reason': 'test_assets_unverifiable'}
    for name in snapshot:
        relative = Path(name)
        if not (any(part.casefold() in ('test', 'tests', '__tests__', 'spec', 'specs') for part in relative.parts[:-1])
                or 'test' in relative.name.casefold() or 'spec' in relative.name.casefold()):
            return {'valid': False, 'changed_paths': [], 'tracked_paths': [], 'reason': 'test_assets_unverifiable'}
    root = _root(project)
    changed = []
    for name, sha in snapshot.items():
        try:
            current = hashlib.sha256(_read(_path(root, name))).hexdigest()
        except (ValueError, OSError):
            changed.append(name)
        else:
            if current != sha:
                changed.append(name)
    return {'valid': not changed, 'changed_paths': sorted(changed), 'tracked_paths': sorted(snapshot),
            'reason': 'test_assets_changed' if changed else None}


def validation_job(job, task):
    """Keep per-builder proof distinct from another WP's latest Job summary."""
    if job.get('workflow_contract_version') == 1 and task and task.get('workflow_validation_evidence') is not None:
        return dict(job,validation_evidence=task['workflow_validation_evidence'])
    return job


def builder_issues(job, task, result):
    job = validation_job(job,task)
    plan = job.get('code_test_plan') or {}
    if not plan.get('required'):
        return [], None
    issues = findings(commands(job), task, job=job, project=job['project'])
    try:
        snapshot = snapshot_files(job['project'], result.get('test_files'),
                                  job.get('test_assets'), result.get('test_change_reason', ''))
    except ValueError as exc:
        issues.append(str(exc))
        snapshot = None
    if snapshot is not None:
        test_index = commands(job).index(plan['command'])
        matching_runs = [run for run in task.get('validation_runs') or []
                         if isinstance(run, dict) and run.get('index') == test_index]
        last_run = matching_runs[-1] if matching_runs else {}
        observed_at_ns = last_run.get('observed_at_ns')
        if (last_run.get('exit_code') == 0 and not last_run.get('timed_out')
                and (type(observed_at_ns) is not int or observed_at_ns <= 0)):
            issues.append('テスト実行時点の記録がありません。現行のテストファイルで再実行してください。')
        elif type(observed_at_ns) is int and observed_at_ns > 0:
            root = Path(job['project']).resolve()
            try:
                changed_after_run = any((root / name).stat().st_mtime_ns > observed_at_ns
                                        for name in snapshot)
            except OSError:
                changed_after_run = True
            if changed_after_run:
                issues.append('テスト実行後にテストファイルが変更または削除されています。現行版で再実行してください。')
    return issues, snapshot
