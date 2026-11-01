"""Deterministic completion gate for commands explicitly supplied with a job.

The builder runs commands through the existing tool approval path. This module
only checks the recorded exit codes; it never launches a process.
"""

import time


def validate_commands(value):
    if value in (None, []):
        return []
    if not isinstance(value, list) or not 1 <= len(value) <= 5:
        raise ValueError('検証コマンドは1〜5件の配列で指定してください。')
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


def findings(commands, cycle_task, job=None, project=None):
    """Use this builder's result or a still-valid persisted result for the job.

    Legacy callers without a job keep their original task-local behavior.
    """
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
        persisted = (job or {}).get('validation_evidence') or {}
        evidence = persisted.get(str(index)) if isinstance(persisted, dict) else None
        if run is not None and (run.get('timed_out') or run.get('exit_code') != 0):
            code = '時間切れ' if run.get('timed_out') else str(run.get('exit_code'))
            issues.append(f'検証コマンド{index + 1}が成功していません（終了状態: {code}）。')
            continue
        if job is None:
            if run is None:
                issues.append(f'検証コマンド{index + 1}が未実行です。')
            continue
        if (not isinstance(evidence, dict) or evidence.get('version') != 1
                or evidence.get('command') != commands[index]
                or evidence.get('job_id') != job.get('id')
                or evidence.get('exit_code') != 0 or evidence.get('timed_out') is not False
                or not evidence.get('task_id') or type(evidence.get('observed_at_ns')) is not int):
            issues.append(f'検証コマンド{index + 1}の有効な実行記録がありません。')
            continue
        if run is not None and evidence['task_id'] != cycle_task.get('id'):
            issues.append(f'検証コマンド{index + 1}の実行記録と担当が一致しません。')
            continue
        from team_validation_evidence import validity
        valid, reason, _ = validity(project or job.get('project', ''), evidence.get('snapshot'), commands)
        if not valid:
            issues.append(f'検証コマンド{index + 1}の証跡が失効しました: {reason}')
    return issues


def review_context(commands, cycle_task, job):
    """Give reviewers the gate's computed status without making prose authoritative."""
    issues = findings(commands, cycle_task, job=job, project=job.get('project'))
    if issues:
        return ('采来の検証証跡判定: 要再検証。理由: ' + ' '.join(issues)[:800]
                + '。レビュー本文の「再テスト不要」という判断はこの判定を変更しません。')
    return ('采来の検証証跡判定: 現行版で有効。終了コードと対象版を確認済み。'
            'テスト範囲や成果物の正しさは別途レビューしてください。')
