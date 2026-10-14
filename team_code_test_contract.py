"""Per-job code-test plan and reusable test-file evidence.

This module never executes a command. Execution remains with the worker and
the existing command-approval path; only current, recorded results count.
"""

import hashlib
from pathlib import Path

from team_validation_gate import findings, validate_commands


def validate_plan(value):
    if not isinstance(value, dict) or set(value) != {'required', 'reason', 'cases', 'command', 'existing_tests'}:
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
    if required:
        if not cases:
            raise ValueError('コード変更の計画にはテストする条件が必要です。')
        validate_commands([command])
        from team_dlp import require_clean_input
        require_clean_input(command, False, '計画のテストコマンド')
    elif not reason.strip() or cases or command or existing:
        raise ValueError('テスト対象外の計画には理由だけを指定してください。')
    return {'required': required, 'reason': reason.strip(),
            'cases': [case.strip() for case in cases], 'command': command.strip(),
            'existing_tests': existing}


def commands(job):
    result = list(job.get('validation_commands') or [])
    plan = job.get('code_test_plan') or {}
    command = plan.get('command') if plan.get('required') else None
    if command and command not in result:
        result.append(command)
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
    try:
        current = snapshot_files(project, list(snapshot))
    except ValueError:
        return False
    return current == snapshot


def builder_issues(job, task, result):
    plan = job.get('code_test_plan') or {}
    if not plan.get('required'):
        return [], None
    issues = findings(commands(job), task)
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
