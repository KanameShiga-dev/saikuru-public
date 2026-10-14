"""Deterministic completion gate for commands explicitly supplied with a job.

The builder runs commands through the existing tool approval path. This module
only checks the recorded exit codes; it never launches a process.
"""

import time


def validate_commands(value):
    if value in (None, []):
        return []
    if not isinstance(value, list) or not 1 <= len(value) <= 3:
        raise ValueError('検証コマンドは1〜3件の配列で指定してください。')
    commands = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > 500 \
                or any(ord(char) < 32 for char in item):
            raise ValueError('検証コマンドは改行のない500文字以内の文字列で指定してください。')
        command = item.strip()
        if command in commands:
            raise ValueError('同じ検証コマンドを重複して指定できません。')
        commands.append(command)
    return commands


def record_result(task, commands, command, exit_code, timed_out):
    """Return updated evidence for an exact user-supplied command, or None."""
    if task.get('role') != 'builder' or str(command).strip() not in commands:
        return None
    index = commands.index(str(command).strip())
    runs = list(task.get('validation_runs') or [])
    runs.append({'index': index, 'exit_code': exit_code if type(exit_code) is int else None,
                 'timed_out': timed_out is True, 'observed_at_ns': time.time_ns()})
    return runs[-30:]


def findings(commands, cycle_task):
    """Only evidence from the builder immediately before this review counts."""
    if not commands:
        return []
    if not cycle_task or cycle_task.get('role') != 'builder':
        return ['今回のレビュー直前に修正・検証担当が検証コマンドを実行していません。']
    latest = {}
    for run in cycle_task.get('validation_runs') or []:
        if isinstance(run, dict) and type(run.get('index')) is int:
            latest[run['index']] = run
    issues = []
    for index in range(len(commands)):
        run = latest.get(index)
        if run is None:
            issues.append(f'検証コマンド{index + 1}が未実行です。')
        elif run.get('timed_out') or run.get('exit_code') != 0:
            code = '時間切れ' if run.get('timed_out') else str(run.get('exit_code'))
            issues.append(f'検証コマンド{index + 1}が成功していません（終了状態: {code}）。')
    return issues
