"""Local recovery guidance. Never execute commands supplied by a model report."""
import json
import hashlib
import re
import sqlite3
import subprocess
import time
from pathlib import Path


USAGE_LIMIT = re.compile(r"hit your [a-z0-9 -]*limit|usage limit|quota exceeded|rate[ _-]?limit"
                         # 2026-10-15: GitHub Copilot running out of premium requests / AI credits
                         r"|exceeded your [a-z ]*(?:allowance|quota|limit)|(?:premium requests?|ai credits?) (?:limit|allowance|exhausted)"
                         r"|(?:insufficient|no remaining|out of) (?:ai )?credits", re.I)
# 2026-10-15: a GitHub Copilot sign-in failure (moves the task to Claude when allowed, see Engine._failover).
COPILOT_AUTH = re.compile(r"not (?:logged|signed) in|copilot login|authentication (?:failed|required)|unauthori[sz]ed"
                          r"|\b401\b|re-?authenticate|(?:oauth )?token (?:has )?(?:expired|is invalid|invalid)", re.I)
RESET_AT = re.compile(r'resets\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:\(([^)]+)\))?', re.I)

_WORKFLOW_FAILURE_GUIDANCE = {
    'authentication': ('認証状態の確認が必要です', '本人が対象Providerの認証状態を確認し、保守確認後に再試行を判断してください。', 'waiting'),
    'provider_limit': ('Providerが利用上限を報告しました', '利用枠の回復を確認してから、現在の承認と途中成果を保守担当が確認してください。自動再送はしません。', 'waiting'),
    'timeout': ('担当の実行時間上限で停止しました', '途中成果と処理の成否を確認し、同じ操作を再送せず再開方法を保守担当が判断してください。', 'maintenance'),
    'sandbox_setup': ('実行環境の準備で停止しました', 'サンドボックスの設定・権限を変えずに原因を切り分け、修復と再試験は別途承認範囲を確認してください。', 'maintenance'),
    'safety': ('安全上の確認が必要です', '安全な送信・操作範囲を確認してください。安全制約や認証を弱めて再開しないでください。', 'safety_blocked'),
    'cancelled': ('実行を中断しました', '保存済み成果を確認してください。中断の解除や新たな実行には利用者の判断が必要です。', 'cancelled'),
    'execution': ('実行結果の確認が必要です', '途中成果・操作の成否・現在の承認を確認してください。不明な操作を再送しないでください。', 'maintenance'),
}


def workflow_failure_plan(code, message='', *, cancelled=False, model_call_state='unknown'):
    """Bounded advisory classification only. Never permission, retry or a command.

    Provider text is untrusted and used only as a bounded classification hint;
    it is never copied into guidance. A native session/probe is not proof that
    an operation had no effect. Missing usage or model-call status is unknown.
    """
    text = message[:4096] if isinstance(message, str) else ''
    if cancelled:
        category = 'cancelled'
    elif p0_failure_category(code) == 'SAFETY_BLOCKED':
        category = 'safety'
    elif code == 'codex_sandbox':
        category = 'sandbox_setup'
    elif code in ('claude_auth', 'copilot_auth', 'codex_auth', 'authentication'):
        category = 'authentication'
    elif code in ('usage_limit', 'usage_limit_wait', 'rate_limit', 'quota_exceeded'):
        category = 'provider_limit'
    elif code in ('task_timeout', 'timeout'):
        category = 'timeout'
    elif re.search(r'\bunauthenticated\b|\bunauthorized\b|failed to authenticate|authentication failed|oauth.{0,40}expired|not (?:logged|signed) in', text, re.I):
        category = 'authentication'
    elif USAGE_LIMIT.search(text):
        category = 'provider_limit'
    elif re.search(r'\btimed? out\b|\btimeout\b|実行時間の上限', text, re.I):
        category = 'timeout'
    else:
        category = 'execution'
    title, next_action, state = _WORKFLOW_FAILURE_GUIDANCE[category]
    model_state = model_call_state if model_call_state in ('not_started', 'started') else 'unknown'
    return {'schema_version': 1, 'category': category, 'state': state,
            'title': title, 'next_action': next_action, 'model_call_state': model_state,
            'operation_state': 'unknown', 'resume_allowed': False, 'automatic_retry': False,
            'automatic_model_change': False, 'execution_permission': 'none',
            'required_before_retry': ['current_authorization', 'stopped_workers', 'effects_reconciled',
                                      'current_checkpoint_or_explicit_new_request'],
            'manual_alternative': '保存済み成果を確認し、必要なら利用者が手作業または別の依頼を選んでください。'}


def workflow_failure_advice(task):
    """Render fixed guidance, never arbitrary strings from saved metadata."""
    plan = task.get('failure_recovery')
    if (not isinstance(plan, dict) or type(plan.get('schema_version')) is not int
            or plan['schema_version'] != 1 or not isinstance(plan.get('category'), str)
            or plan['category'] not in _WORKFLOW_FAILURE_GUIDANCE
            or plan.get('execution_permission') != 'none' or plan.get('resume_allowed') is not False
            or plan.get('automatic_retry') is not False or plan.get('automatic_model_change') is not False
            or not plan.get('attempt_id') or plan['attempt_id'] != (task.get('current_attempt') or {}).get('id')
            or type(plan.get('attempt_ordinal')) is not int or plan['attempt_ordinal'] != task.get('attempt')):
        return None
    title, next_step, state = _WORKFLOW_FAILURE_GUIDANCE[plan['category']]
    return {'attempt': task.get('attempt'), 'checked_at': time.time(), 'evidence': [],
            'code': 'workflow_' + plan['category'], 'title': title,
            'explanation': '処理の成否や途中成果の確認が必要です。保存やセッションの存在だけでは再開できません。',
            'next_step': next_step, 'draft': '', 'can_retry': False, 'resume_allowed': False,
            'state': state, 'execution_permission': 'none'}


def retry_local_read(operation, max_retries=2):
    """Retry only an idempotent local read on a transient SQLite lock."""
    for attempt in range(max_retries + 1):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if attempt >= max_retries or not re.search(r'locked|busy', str(exc), re.I):
                raise
            time.sleep(.05 * (attempt + 1))


def p0_failure_category(code):
    """Keep pre-provider DLP refusals distinct from repairable engine failures."""
    return ('SAFETY_BLOCKED' if code in
            ('reference_secret', 'prompt_secret', 'egress_blocked',
             'permission_denied', 'security') else 'MAINTENANCE_REQUIRED')


def p0_interruption(task, previous, code, category='MAINTENANCE_REQUIRED'):
    """Record bounded, non-sensitive failure metadata for resuming the same task."""
    code = str(code or 'execution')[:80]
    if category not in ('MAINTENANCE_REQUIRED', 'SAFETY_BLOCKED'):
        category = 'MAINTENANCE_REQUIRED'
    guidance = workflow_failure_advice(task)
    failure_kind = ':' + task['failure_recovery']['category'] if guidance else ''
    signature = hashlib.sha256(
        (code + failure_kind + ':' + task.get('role', '') + ':' +
         str((task.get('profile') or {}).get('adapter', ''))).encode()).hexdigest()[:20]
    count = (int(previous.get('attempt_count', 0)) + 1
             if isinstance(previous, dict) and previous.get('failure_signature') == signature else 1)
    result = {
        'interrupted_task_id': task['id'], 'interruption_reason': code,
        'recovery_status': 'safety_blocked' if category == 'SAFETY_BLOCKED' else 'maintenance_required',
        'resume_target': task['id'], 'failure_signature': signature,
        'attempt_count': count, 'last_failure': time.time(),
        'error_category': category,
        'next_action': '安全上の問題を確認する' if category == 'SAFETY_BLOCKED' else '保守担当が実行環境を確認する',
    }
    if guidance:
        result.update(failure_category=task['failure_recovery']['category'],
                      next_action=guidance['next_step'], resume_allowed=False, automatic_retry=False)
    return result


def usage_limit_retry_at(message, now_ts=None):
    """2026-10-08: when a provider stops a run because a usage limit was reached, return when to try again
    (the reported reset time + 5 minutes, or 30 minutes when no time is given), else None.
    Example: "You've hit your session limit · resets 3pm (Asia/Tokyo)"."""
    from datetime import datetime, timedelta
    text = str(message or '')
    if not USAGE_LIMIT.search(text):
        return None
    now_ts = time.time() if now_ts is None else now_ts
    match = RESET_AT.search(text)
    if not match:
        return now_ts + 1800
    hour, minute, half = int(match.group(1)), int(match.group(2) or 0), (match.group(3) or '').lower()
    if half == 'pm' and hour < 12:
        hour += 12
    elif half == 'am' and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return now_ts + 1800
    zone = None
    if match.group(4):
        try:
            from zoneinfo import ZoneInfo
            zone = ZoneInfo(match.group(4).strip())
        except Exception:
            zone = None  # no tz database on this PC: the PC's local time (Japan) is used
    base = datetime.fromtimestamp(now_ts, zone) if zone else datetime.fromtimestamp(now_ts)
    target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target.timestamp() <= now_ts:
        target += timedelta(days=1)
    return min(target.timestamp() + 300, now_ts + 24 * 3600)


def desktop_connection_state():
    """Read only the current session state, without account names or screen data."""
    import os
    if os.name != 'nt':
        return None
    import ctypes
    from ctypes import wintypes
    session = wintypes.DWORD()
    api = ctypes.WinDLL('wtsapi32', use_last_error=True)
    query = api.WTSQuerySessionInformationW
    query.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_int,
                      ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    query.restype = wintypes.BOOL
    api.WTSFreeMemory.argtypes = [ctypes.c_void_p]
    buffer, size = ctypes.c_void_p(), wintypes.DWORD()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
        return None
    if not query(None, session.value, 8, ctypes.byref(buffer), ctypes.byref(size)):
        return None
    try:
        return ctypes.cast(buffer, ctypes.POINTER(ctypes.c_int)).contents.value if size.value >= 4 else None
    finally:
        api.WTSFreeMemory(buffer)


def handoff_report_advice(task):
    recovery = task.get('recovery') or {}
    if recovery.get('code') != 'handoff_rejected':
        return None
    if task.get('role') not in ('builder', 'researcher') or '未完了項目の引き継ぎには利用者の範囲指定' not in recovery.get('message', ''):
        return None
    summary = recovery.get('reported_summary', '')
    if not re.search(r'工程[0-9０-９]+.{0,18}((?<!未)完了|検証を終え(?!てい(?:ない|ません)))|担当.{0,12}(?<!未)完了', summary):
        return None
    return {'attempt': task['attempt'], 'checked_at': time.time(), 'evidence': [],
        'code': 'task_completion', 'title': '担当工程の完了を報告する回答案',
        'explanation': '担当は今回の工程を終えたと報告していますが、未完了作業の移管を意味するhandoffを返したため、統括が拒否しました。報告された証拠の合格を統括が確認済みという意味ではありません。',
        'next_step': '移管欄は使わず、下の回答案で担当工程の完了条件と証拠を確認して再報告してください。',
        'draft': '今回の担当工程が完了しており、残る作業が元の計画どおり後続工程の担当範囲なら、今回の結果をstatus=doneで報告してください。'
            '\n既存の報告書と検証証拠を確認し、変更がなければ退避・テストを重複実行しないでください。'
            '\n依頼全体が未完了であることと、今回の担当工程が完了したことを区別してください。今回の担当工程自体に未完了項目がある場合のみ、具体的に報告してください。',
        'can_retry': True}


def advice(task, project):
    workflow = workflow_failure_advice(task)
    if workflow:
        return workflow
    completed = handoff_report_advice(task)
    if completed:
        return completed
    summary = task.get('summary', '')
    result = task.get('result') or {}
    report = summary + '\n' + json.dumps(result, ensure_ascii=False)
    answer = {'attempt': task['attempt'], 'checked_at': time.time(), 'evidence': [],
              'code': 'unknown', 'title': '停止理由の確認が必要です',
              'explanation': summary or '具体的な停止理由は報告されていません。',
              'next_step': '担当へ、失敗した操作とエラー全文の報告を依頼してください。',
              'draft': '失敗した実行ファイル・操作、作業ディレクトリ、終了コード、エラー全文を報告してください。秘密情報は含めないでください。同じ復旧待ちの案内だけを繰り返さないでください。',
              'can_retry': True}
    if re.search(r"hit your session limit", summary, re.I):
        reset = re.search(r'resets\s+(.+)', summary, re.I)
        answer.update(code='quota', title='Claudeの5時間枠を使い切りました',
            explanation='Claude CLIが利用枠の上限を報告しています。' + ('リセット案内: '+reset[1] if reset else ''),
            next_step='Codexに切り替えるか、利用枠の回復を確認して再試行してください。', draft='', can_retry=False)
    elif re.search(r'Failed to authenticate|OAuth.*expired|authentication', summary, re.I):
        answer.update(code='auth', title='ログイン状態の復旧が必要です',
            next_step='対象CLIへ再ログインし、成功後に再試行してください。', draft='', can_retry=False)
    elif '引き継ぎ文脈が大きすぎ' in summary:
        answer.update(code='context_size', title='引き継ぎ情報が一度に渡せる量を超えました',
            explanation='ゲームの実行エラーではなく、担当へ渡す引き継ぎ情報のサイズ上限で停止しました。履歴DBは保持されています。情報を索引付きの分割ファイルにして順番に読み取る方式へ変更しました。',
            next_step='下の回答案で再開してください。最新の利用者判断と現在の事実を先に確認し、必要な報告を分割して読み取ります。',
            draft='引き継ぎ文脈の分割読込に対応しました。索引の最新の利用者判断・現在の事実は全partを読み、関連する担当報告を確認して未完了部分から続行してください。Computer Useは使わず、元のデータと証拠を保持し、完了済みの処理を重複実行しないでください。',
            can_retry=True)
    elif re.search(r'Python', report, re.I) and re.search(r'起動失敗|起動でき|プロセス作成失敗|Unable to create process', report, re.I):
        root = Path(project).resolve()
        names = ['.venv']
        for name in re.findall(r'(?<![\w])\.tmp_[A-Za-z0-9_-]*venv[A-Za-z0-9_-]*', report):
            if name not in names:
                names.append(name)
        for name in names[:4]:
            path = root / name / 'Scripts' / 'python.exe'
            item = {'file': str(path), 'ok': False}
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                item['result'] = 'プロジェクト外への参照のため確認しません。'
            elif not path.is_file():
                item['result'] = '実行ファイルが見つかりません。'
            else:
                try:
                    proc = subprocess.run([str(path), '--version'], cwd=root, shell=False,
                        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    version = (proc.stdout.strip() or proc.stderr.strip())
                    item.update(ok=proc.returncode == 0 and bool(re.fullmatch(r'Python \d+\.\d+\.\d+\S*', version)),
                                exit_code=proc.returncode)
                    # Keep version or a fixed description, never arbitrary child output.
                    item['result'] = version if item['ok'] else '起動確認に失敗しました。'
                except (OSError, subprocess.TimeoutExpired) as exc:
                    item['result'] = '起動できませんでした（'+type(exc).__name__+'）。'
            answer['evidence'].append(item)
        ready = all(item['ok'] for item in answer['evidence'])
        answer.update(code='python', title='テスト用Pythonの起動確認',
            explanation='担当はPython起動失敗で停止しました。今回の確認は統括サービス側です。担当側の実行環境の復旧はまだ確認できていません。',
            next_step='移管欄は使わず、下の回答案を再試行の入力欄へ入れて再開してください。' if ready else
                '統括側でも起動確認に失敗しました。Python環境を復旧してから再確認してください。工程3への移管では解消しません。',
            can_retry=ready,
            draft=('統括サービス側では、対象仮想環境のpython.exe --versionが成功しました。確認結果: '
                   + ' / '.join(item['file']+' = '+item['result'] for item in answer['evidence'])
                   + '\n担当側でも起動を再確認し、成功したら現在の工程の残作業を続行してください。既存の退避は重複実行しないでください。'
                   + '\n再び失敗する場合は、使用した実行ファイル、作業ディレクトリ、終了コード、エラー全文を記録して報告してください。同じ復旧待ちの案内だけを繰り返さないでください。') if ready else '')
    elif 'Computer Use was not approved' in report or 'Computer Useの承認' in report:
        session_state = desktop_connection_state()
        disconnected = session_state is not None and session_state != 0
        answer.update(code='computer_use', title='PCの画面接続が必要です' if disconnected else 'ゲーム画面への操作許可を確認してください',
            explanation='担当はゲームのウィンドウ取得をComputer Useの承認チェックで拒否されました。ボードの操作承認とは別のチェックです。'
                + (' 現在、統括のWindows画面セッションも接続されていません。背景処理と実画面操作は条件が異なります。' if disconnected else ''),
            next_step='PCでWindowsにサインインし、画面のロックを解除して対象ゲームを表示してください。その後「原因を確認して回答案を作る」を押し、回答案から一度だけ再開してください。' if disconnected else
                '下の回答案で一度だけ再開してください。再び拒否された場合は担当環境の許可反映が未解消なので、同じ再試行を繰り返さないでください。',
            draft='対象はC:\\AI_WorkSpace\\Projects\\Games\\bravia-New-Games\\builds\\pc-runtime-smoke\\BraveStrategyPcRuntimeSmoke.exeだけです。利用者はこのゲームの画面取得と検証のための操作を承認しています。'
                '既存のゲーム・隔離データ・証拠を保持し、再ビルド・再seedはしないでください。'
                '最初に対象ウィンドウ1件を選択してComputer Useの取得・画面取得を確認し、成功した場合だけ未完了の実操作を続けてください。'
                '拒否や画面取得失敗の場合は一度で停止し、操作名とエラーを報告してください。承認チェックの迂回や別手段の入力は禁止です。',
            can_retry=not disconnected)
        answer['evidence'].append({'file': 'Windows session', 'result': 'disconnected' if disconnected else 'active' if session_state == 0 else 'unconfirmed'})
    elif re.search(r'ACL|PreToolUse|フック.*拒否', summary, re.I):
        answer.update(code='permission', title='実行権限またはフックによる拒否です',
            next_step='権限・フックの設定と拒否理由を確認する必要があります。移管や再試行だけでは解消しません。',
            draft='', can_retry=False)
    elif re.search(r'未完了項目の引き継ぎには利用者の範囲指定', summary):
        answer.update(code='transfer', title='引き継ぎ範囲の登録が必要です',
            next_step='「ボードから復旧する」で、登録済み移管を確認するか、後続工程へ移す必須作業を指定してください。',
            draft='', can_retry=False)
    return answer

class CheckpointRecoveryBlocked(ValueError):
    """Fixed internal reason code; never contains a path or provider message."""


def checkpoint_recovery_decision(job, task, tasks, record, refs, workspace, assets):
    """Pure conservative selection; never resets or queues successful tasks."""
    reasons, affected = set(), set()
    current = (refs.get('evidence_refs') or {}).get('recovery_context') or {}
    observed = (record.get('evidence_refs') or {}).get('recovery_context') or {}
    by_id = {item['id']: item for item in tasks}
    all_ids = set(by_id)
    old, new = observed.get('tasks') or {}, current.get('tasks') or {}
    if (type(observed.get('schema_version')) is not int or observed['schema_version'] != 1
            or not old or set(old) != all_ids or set(new) != all_ids):
        reasons.add('recovery_context_missing_or_changed'); affected |= all_ids
    if observed.get('job') != current.get('job'):
        reasons.add('requirements_or_policy_changed'); affected |= all_ids
    for identifier in all_ids & set(old) & set(new):
        before, after = old[identifier], new[identifier]
        if any(before.get(k) != after.get(k) for k in ('semantic', 'validation', 'tests', 'result_status', 'attempt_id')):
            reasons.add('task_evidence_changed'); affected.add(identifier)
        if before.get('status') != after.get('status'):
            reasons.add('task_state_changed'); affected.add(identifier)
    if ((record.get('evidence_refs') or {}).get('job_validation')
            != (refs.get('evidence_refs') or {}).get('job_validation')):
        reasons.add('job_validation_changed'); affected |= all_ids
    if any(record.get(key) != job.get(key) for key in (
            'requirement_revision', 'requirements_approval_id', 'plan_revision', 'plan_approval_id')):
        reasons.add('approval_changed'); affected |= all_ids
    for field, code in (('decision_refs', 'decision_changed'), ('handoff_observation', 'handoff_changed'),
                        ('artifact_refs', 'artifact_references_changed'), ('operation_refs', 'operation_reconciliation_required')):
        if record.get(field) != refs.get(field):
            reasons.add(code); affected |= all_ids
    if record.get('operation_refs') or record.get('resume_preconditions') != ['checkpoint_not_ready']:
        reasons.add('operation_reconciliation_required'); affected |= all_ids
    if workspace['head_changed']:
        reasons.add('git_head_changed'); affected |= all_ids
    changed = set(workspace['changed_paths'])
    asset_owners = {name: {identifier for identifier in by_id
                          if name in assets.get(identifier, {}).get('tracked_paths', [])} for name in changed}
    if changed:
        reasons.add('workspace_changed')
        if any(not owners for owners in asset_owners.values()):
            reasons.add('change_scope_unresolved'); affected |= all_ids
        else:
            for owners in asset_owners.values(): affected |= owners
    elif not workspace['current']:
        reasons.add('workspace_metadata_changed'); affected |= all_ids
    for identifier, evidence in assets.items():
        if not evidence['valid']:
            reasons.add(evidence['reason']); affected.add(identifier)
    # Current Task.after is only the existing execution projection. Unknown,
    # cyclic or foreign edges block selection; approved WP DAG gates are separate.
    for identifier, item in by_id.items():
        parent = item.get('after')
        seen = {identifier}
        while parent:
            if parent not in by_id or parent in seen:
                reasons.add('dependency_unresolved'); affected |= all_ids; break
            seen.add(parent); parent = by_id[parent].get('after')
    while True:
        expanded = affected | {identifier for identifier, item in by_id.items() if item.get('after') in affected}
        if expanded == affected: break
        affected = expanded
    preserved = sorted(identifier for identifier, item in by_id.items()
                       if item.get('status') == 'succeeded' and old.get(identifier, {}).get('status') == 'succeeded'
                       and identifier not in affected)
    candidate_ids = []
    if not reasons:
        for identifier, item in by_id.items():
            if item.get('status') not in ('queued', 'blocked', 'failed', 'interrupted'):
                continue
            if (old.get(identifier, {}).get('status') == 'succeeded' or item.get('recovery_check_required')
                    or (item.get('current_attempt') or {}).get('status') == 'running'):
                continue
            if item.get('after') and item['after'] not in preserved:
                continue
            candidate_ids.append(identifier)
    if any(item.get('recovery_check_required') for item in tasks):
        reasons.add('writer_reconciliation_required'); candidate_ids = []
    return {'status': 'blocked' if reasons else 'candidate', 'resume_allowed': False,
            'reasons': sorted(reasons) if reasons else ['authorization_and_fresh_attempt_required'],
            'checkpoint_id': record['id'], 'changed_paths': sorted(changed),
            'preserved_succeeded_task_ids': preserved, 'revalidate_task_ids': sorted(affected),
            'candidate_task_ids': sorted(candidate_ids), 'assessment_only': True}
