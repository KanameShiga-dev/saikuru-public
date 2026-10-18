"""Goal and acceptance contract for new development jobs.

The original request remains in job.goal. A proposed plan becomes authoritative
only through the existing G3 decision path. No model can change job.goal.
"""

from copy import deepcopy
import hashlib
from pathlib import Path


MAX_ITEMS = 20
MAX_TEXT = 500


def initial(goal):
    return {
        'original_request': goal,
        'primary_goal': goal,
        'deliverables': [],
        'acceptance_criteria': [],
        'non_goals': [],
        'constraints': [],
        'completion_status': 'in_progress',
    }


def _items(value, label, required=False):
    if (not isinstance(value, list) or len(value) > MAX_ITEMS
            or (required and not value)
            or any(not isinstance(item, str) or not item.strip() or len(item) > MAX_TEXT
                   for item in value)):
        raise ValueError(f'{label}の形式または件数が不正です。')
    clean = [item.strip() for item in value]
    if len(set(clean)) != len(clean):
        raise ValueError(f'{label}に重複があります。')
    return clean


def validate_plan(job, result):
    """Validate planner linkage before a plan is offered to the user."""
    proposed = result.get('goal_contract')
    if not isinstance(proposed, dict) or set(proposed) != {
            'primary_goal', 'deliverables', 'acceptance_criteria', 'non_goals', 'constraints'}:
        raise ValueError('計画に目的・成果物・受入条件がありません。')
    primary = proposed['primary_goal']
    if not isinstance(primary, str) or not primary.strip() or len(primary) > MAX_TEXT:
        raise ValueError('計画の目的が不正です。')
    contract = initial(job['goal'])
    contract['primary_goal'] = primary.strip()
    for key, label in (
            ('deliverables', '成果物'), ('acceptance_criteria', '受入条件'),
            ('non_goals', '対象外'), ('constraints', '制約')):
        contract[key] = _items(proposed[key], label, key in ('deliverables', 'acceptance_criteria'))
    allowed = set(contract['deliverables']) | set(contract['acceptance_criteria'])
    for index, spec in enumerate(result.get('tasks') or [], 1):
        links = _items(spec.get('goal_links'), f'作業{index}の目的との対応', required=True)
        if any(link not in allowed for link in links):
            raise ValueError(f'作業{index}に成果物・受入条件へ対応しない作業があります。')
    test_plan = result.get('code_test_plan') or {}
    if test_plan.get('required') is not True or not test_plan.get('cases') or not test_plan.get('command'):
        raise ValueError('開発計画には現行版で実行するテストと確認条件が必要です。')
    return contract


def approved_plan(job, result):
    contract = validate_plan(job, result)
    contract['completion_status'] = 'in_progress'
    return deepcopy(contract)


def artifact_records(project, paths):
    if not isinstance(paths, list) or len(paths) > MAX_ITEMS or any(
            not isinstance(name, str) or not name.strip() or len(name) > 260 for name in paths):
        raise ValueError('成果ファイルの報告形式が不正です。')
    root = Path(project).resolve()
    records = []
    for name in paths:
        relative = Path(name)
        if relative.is_absolute() or any(part in ('.', '..') for part in relative.parts):
            raise ValueError('成果ファイルは依頼フォルダ内の相対パスで指定してください。')
        forbidden = {'.git', 'data', '__pycache__', '.env', '.ssh', '.aws', '.azure',
                     'secrets', 'credentials', 'logs'}
        if any(part.casefold() in forbidden or part.startswith('.') or
               any(word in part.casefold() for word in ('secret', 'credential', 'private_key',
                                                       'access_token', 'api_key'))
               for part in relative.parts):
            raise ValueError('成果ファイルとして機密設定や内部データを指定できません。')
        target = (root / relative).resolve()
        if (not target.is_relative_to(root) or not target.is_file()
                or target.stat().st_size > 20_000_000):
            raise ValueError('成果ファイルを安全に確認できません。')
        records.append({'path': relative.as_posix(),
                        'sha256': hashlib.sha256(target.read_bytes()).hexdigest()})
    return records


def current_artifacts(job):
    records = job.get('p0_artifacts') or []
    if not isinstance(records, list) or not records:
        return False, '完成物のファイルが登録されていません。'
    try:
        actual = artifact_records(job['project'], [item['path'] for item in records])
    except (ValueError, KeyError, TypeError, OSError):
        return False, '完成物のファイルを確認できません。'
    if actual != records:
        return False, '完成物が確認後に変更されています。'
    return True, ''


def p0_artifact_text(job, relative):
    """Read only a current, registered, small UTF-8 text deliverable."""
    if job.get('p0_contract_version') != 1 or not isinstance(relative, str):
        raise ValueError('開発依頼の登録済み成果物を選択してください。')
    record = next((item for item in job.get('p0_artifacts') or []
                   if isinstance(item, dict) and item.get('path') == relative), None)
    if record is None:
        raise ValueError('登録済みの成果物を選択してください。')
    from consultation_read_tools import safe_path
    path = safe_path(Path(job['project']).resolve(), relative)
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != record.get('sha256'):
        raise ValueError('成果物が登録後に変更されています。')
    try:
        content = raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ValueError('UTF-8のテキストファイルだけ表示できます。') from exc
    from team_dlp import scan_input
    if any(scan_input(content).values()):
        raise ValueError('機密情報を含む可能性があるため画面には表示できません。')
    return {'path': relative, 'content': content, 'truncated': False}


def progress(job, tasks, approvals):
    """A user-facing projection from persisted work, never an independent ledger."""
    contract = job.get('goal_contract') or {}
    interruption = job.get('p0_interruption') or {}
    maintenance_wait = interruption.get('recovery_status') == 'maintenance_required'
    hidden_tasks = {item.get('task_id') for item in approvals
                    if item.get('job_id') == job.get('id')
                    and item.get('intended_role') == 'maintainer'}
    if maintenance_wait:
        hidden_tasks.add(interruption.get('interrupted_task_id'))
    def title(task):
        if task.get('id') in hidden_tasks:
            return '保守対応待ち' if maintenance_wait else '保守関連の作業'
        return task.get('title', '')
    owned = [task for task in tasks if task.get('job_id') == job.get('id')]
    completed = [title(task) for task in owned
                 if task.get('role') in ('builder', 'researcher')
                 and task.get('status') == 'succeeded'
                 and (task.get('result') or {}).get('status') == 'done']
    pending = [task for task in owned if task.get('status') in
               ('running', 'awaiting_approval', 'failed', 'blocked', 'interrupted', 'queued')]
    active = next((task for task in pending if task.get('status') != 'queued'), None)
    if active is None:
        active = next((task for task in pending if task.get('status') == 'queued'), None)
    remaining = [title(task) for task in pending
                 if task.get('status') == 'queued' and task.get('id') != (active or {}).get('id')]
    latest_completion = next((approval for approval in reversed(approvals)
                              if approval.get('job_id') == job.get('id')
                              and approval.get('kind') == 'completion'
                              and approval.get('status') == 'pending'
                              and approval.get('intended_role') == 'end_user'), None)
    actions = []
    if job.get('status') not in ('cancelled', 'accepted', 'accepted_with_pending_checks'):
        actions.append('cancel_job')
    if latest_completion:
        actions.append('review_completion')
    if (latest_completion and job.get('status') == 'awaiting_acceptance'
            and latest_completion.get('payload', {}).get('pending_items')):
        actions.append('record_completion_evidence')
    if job.get('status') in ('accepted', 'accepted_with_pending_checks'):
        unmet = []
        next_action = '受領済み'
    elif latest_completion:
        unmet = [str(check) for item in latest_completion.get('payload', {}).get('pending_items') or []
                 for check in item.get('checks') or []]
        next_action = '完成物を起動・操作し、残る受入条件を確認する' if unmet else '完成物の受入を判断する'
    else:
        unmet = list(contract.get('acceptance_criteria') or [])
        if maintenance_wait:
            next_action = '保守担当が開発エンジンを復旧する'
        elif interruption.get('recovery_status') == 'safety_blocked':
            next_action = '安全上の問題を確認する'
        elif job.get('status') == 'awaiting_approval':
            next_action = '開発計画またはAI利用条件を確認する'
        else:
            next_action = title(active) if active else (remaining[0] if remaining else '作業状態を確認する')
    return {
        'completed': completed,
        'current': ('保守対応待ち' if maintenance_wait else title(active) if active else
                    '完成物の確認待ち' if latest_completion else '次の作業を待機中'),
        'remaining': remaining + ([] if job.get('status') in ('accepted', 'accepted_with_pending_checks')
                                 else ['利用者による完成物の確認']),
        'next_action': next_action,
        'completion_status': ('accepted' if job.get('status') in ('accepted', 'accepted_with_pending_checks')
                              else 'awaiting_user_acceptance' if job.get('status') == 'awaiting_acceptance'
                              else 'in_progress'),
        'unmet_acceptance_criteria': unmet,
        'available_actions': actions,
        'deliverable_access': [
            {'path': item['path'], 'method': 'project_relative_file',
             'launch_status': 'unverified',
             'launch_hint': '起動手順は未登録です。成果物の内容を確認し、起動と操作は別途検証してください。'}
            for item in job.get('p0_artifacts') or []
            if isinstance(item, dict) and isinstance(item.get('path'), str)],
    }
