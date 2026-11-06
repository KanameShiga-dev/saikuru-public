"""Goal and acceptance contract for new development jobs.

The original request remains in job.goal. A proposed plan becomes authoritative
only through the existing G3 decision path. No model can change job.goal.
"""

from copy import deepcopy
import hashlib
import hmac
import json
from pathlib import Path


MAX_ITEMS = 20
MAX_TEXT = 500


def workflow_version(job):
    """Validate an explicit workflow marker; absence alone denotes legacy data."""
    if 'workflow_contract_version' not in job:
        return None
    if type(job['workflow_contract_version']) is not int or job['workflow_contract_version'] != 1:
        raise ValueError('未対応の作業フロー版です。')
    if (job.get('request_origin') != 'new' or job.get('route') != 'full'
            or job.get('document_source') or type(job.get('p0_contract_version')) is not int
            or job['p0_contract_version'] != 1):
        raise ValueError('新しい作業フローは新規P0開発依頼だけが対象です。')
    return 1


def initial_workflow(job, *, enabled=False):
    """Called only for an explicitly selected new job, before its first save.

    This helper does not enable execution, migrate a runtime, or grant approval.
    """
    if enabled is not True or 'workflow_contract_version' in job:
        raise ValueError('作業フローの新規選択が有効ではありません。')
    result = deepcopy(job)
    result['workflow_contract_version'] = 1
    workflow_version(result)
    if result.get('status') != 'awaiting_approval':
        raise ValueError('受付時の新規依頼だけ作業フローを選択できます。')
    result.update(requirement_revision=0, requirements_approval_id=None,
                  plan_revision=0, plan_approval_id=None)
    return result


def checkpoint_scope(job, approval):
    """The file allowlist is an approved payload field, never a worker argument."""
    if (workflow_version(job) != 1 or type(job.get('plan_revision')) is not int or job['plan_revision'] < 1
            or approval.get('id') != job.get('plan_approval_id') or approval.get('job_id') != job.get('id')
            or approval.get('kind') != 'plan' or approval.get('status') != 'approved'):
        raise ValueError('Checkpointには現行の承認済み計画が必要です。')
    payload = approval.get('payload')
    if (not isinstance(payload, dict) or not isinstance(approval.get('digest'), str)
            or len(approval['digest']) != 64 or any(c not in '0123456789abcdef' for c in approval['digest'])):
        raise ValueError('Checkpointの承認対象を確認できません。')
    expected = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if not hmac.compare_digest(approval['digest'], expected):
        raise ValueError('Checkpointの承認対象が変化しています。')
    from team_checkpoint import scope_paths
    return scope_paths(payload.get('checkpoint_paths'))


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


def selected_project_context(project):
    """Make the registered project root authoritative in every worker prompt."""
    root = Path(project).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('対象プロジェクトのルートがフォルダではありません。')
    return (
        '\n\n【対象プロジェクトの正本】\n'
        f'依頼時に利用者が選択した実際の作業ルート: {root}\n'
        'この選択済みルートが今回の対象範囲の正本です。専用の読み取りツールもこのルートを対象にしています。'
        'SCOPE.mdなどの資料に別の絶対パスが記載されていても、移転前の記録として扱い、別の場所へ対象を切り替えないでください。'
        '実際に読めるファイルと依頼時の選択先が一致することを確認し、古いパスは警告として報告してください。'
        'プロジェクト資料のパス記載は自動修正しないでください。'
    )


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


def validate_plan(job, result, *, check_current_inputs=True):
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
    if job.get('criterion_evidence_version') == 1:
        covered = {link for spec in result.get('tasks') or [] for link in spec['goal_links']}
        missing = [item for item in contract['acceptance_criteria'] if item not in covered]
        if missing:
            raise ValueError('受入条件を担当する作業がありません: ' + ' / '.join(missing))
    if job.get('code_test_contract_version') == 1:
        from team_code_test_contract import validate_plan as validate_code_test_plan
        validate_code_test_plan(result.get('code_test_plan') or {})
    else:
        test_plan = result.get('code_test_plan') or {}
        if test_plan.get('required') is not True or not test_plan.get('cases') or not test_plan.get('command'):
            raise ValueError('開発計画には現行版で実行するテストと確認条件が必要です。')
    if workflow_version(job) == 1:
        from team_wbs import validate_work_packages
        validate_work_packages(result, job, check_current_inputs=check_current_inputs)
    return contract


def criterion_review(job, task):
    return (job.get('criterion_evidence_version') == 1 and task.get('role') == 'reviewer'
            and task.get('agent_name') != 'common-security-reviewer')


def pending_detail_matches(item, criterion):
    """Link explanatory text only through an exact quoted approved condition."""
    return (isinstance(item, dict) and isinstance(item.get('check'), str)
            and bool(item['check'].strip())
            and (item['check'] == criterion or (
                item.get('source_type') == 'approved_plan'
                and item.get('source_excerpt') == criterion)))


def criterion_review_findings(criteria, result, *, request=''):
    """Check coverage, not the semantic truth of an agent's evidence."""
    rows = result.get('acceptance_results')
    if not isinstance(rows, list) or len(rows) > 40:
        rows = []
    issues, accepted = [], {}
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {'criterion', 'outcome', 'evidence'}
                or not isinstance(row['criterion'], str) or row['criterion'] not in criteria
                or row['criterion'] in accepted
                or row['outcome'] not in ('passed', 'failed', 'user_pending')
                or not isinstance(row['evidence'], str) or not row['evidence'].strip()
                or len(row['evidence']) > 2000):
            issues.append({'requirement': '実行阻害', 'impact': '受入条件ごとの確認結果が重複・対象外・形式不正です。'})
            continue
        criterion = row['criterion']
        accepted[criterion] = row
        if row['outcome'] == 'failed':
            issues.append({'requirement': criterion, 'impact': row['evidence']})
        elif row['outcome'] == 'user_pending':
            details = result.get('pending_check_details') or []
            if (criterion not in (result.get('pending_checks') or []) or not any(
                    pending_detail_matches(item, criterion)
                    and item.get('owner') == 'user' and item.get('required') is True
                    and item.get('feasibility') == 'feasible'
                    and item.get('source_type') in ('request', 'approved_plan')
                    and isinstance(item.get('source_excerpt'), str) and item['source_excerpt'].strip()
                    and item['source_excerpt'].strip() in (
                        request if item['source_type'] == 'request' else criterion)
                    for item in details)):
                issues.append({'requirement': criterion, 'impact': '利用者による確認待ちの出典・担当・未確認項目がありません。'})
        elif criterion in (result.get('pending_checks') or []):
            issues.append({'requirement': criterion, 'impact': '同じ受入条件を合格と未確認の両方で報告しています。'})
    for criterion in criteria:
        if criterion not in accepted:
            issues.append({'requirement': criterion, 'impact': 'この受入条件の確認結果と具体的な根拠がありません。'})
    return issues


def approved_plan(job, result):
    contract = validate_plan(job, result)
    contract['completion_status'] = 'in_progress'
    return deepcopy(contract)


def artifact_relative_path(name, *, allow_data=False):
    """Check artifact path syntax/privacy without consulting a current file."""
    if not isinstance(name, str) or not name.strip() or len(name) > 260:
        raise ValueError('成果ファイルの報告形式が不正です。')
    relative = Path(name)
    if relative.is_absolute() or any(part in ('.', '..') for part in relative.parts):
        raise ValueError('成果ファイルは依頼フォルダ内の相対パスで指定してください。')
    forbidden = {'.git', 'data', '__pycache__', '.env', '.ssh', '.aws', '.azure',
                 'secrets', 'credentials', 'logs'}
    if allow_data:
        forbidden.remove('data')
    if any(part.casefold() in forbidden or part.startswith('.') or
           any(word in part.casefold() for word in ('secret', 'credential', 'private_key',
                                                   'access_token', 'api_key'))
           for part in relative.parts):
        raise ValueError('成果ファイルとして機密設定や内部データを指定できません。')
    return relative


def artifact_records(project, paths):
    if not isinstance(paths, list) or len(paths) > MAX_ITEMS or any(
            not isinstance(name, str) or not name.strip() or len(name) > 260 for name in paths):
        raise ValueError('成果ファイルの報告形式が不正です。')
    root = Path(project).resolve()
    records = []
    for name in paths:
        relative = artifact_relative_path(name, allow_data=True)
        target = (root / relative).resolve()
        if 'data' in [part.casefold() for part in relative.parts]:
            from consultation_read_tools import safe_path, screen_model_read
            target = safe_path(root, name, allow_data=True)
            try:
                screen_model_read(target.read_text(encoding='utf-8-sig'))
            except UnicodeError as exc:
                raise ValueError('開発データの成果物はUTF-8のテキストを指定してください。') from exc
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


class _WorkflowRecords:
    """Read-only typed record access; no database, files or permission changes."""
    def __init__(self, job, tasks, approvals, packages):
        self.rows = {'job':[job], 'task':tasks, 'approval':approvals, 'work_package':packages}
        self.index = {}
        for kind, rows in self.rows.items():
            for row in rows:
                key = (kind,row['id'])
                if key in self.index: raise ValueError('workflow_record_identity_invalid')
                self.index[key] = row

    def get(self, ident, kind): return self.index[(kind,ident)]
    def for_job(self, kind, ident): return [r for r in self.rows[kind] if r.get('job_id') == ident]


def _workflow_event(events, kind, approval_id):
    rows = [e for e in events if e.get('type') == kind and (e.get('details') or {}).get('approval_id') == approval_id]
    if not rows: return {}
    if all(type(e.get('seq')) is int for e in rows): rows = sorted(rows,key=lambda e:e['seq'])
    return rows[-1].get('details') or {}  # Otherwise the caller supplies chronological records.


def current_workflow_tasks(job, tasks, packages):
    """Current plan/generation only. Cancelled global queues remain history."""
    by_id = {w['id']:w for w in packages if w.get('job_id') == job['id'] and w.get('plan_revision') == job['plan_revision']}
    result = []; global_tasks = []
    generations = {ident:wp.get('workflow_generation',0) for ident,wp in by_id.items()}
    for task in tasks:
        if (task.get('job_id') != job['id'] or task.get('role') == 'planner'
                or type(task.get('plan_revision')) is not int or task['plan_revision'] != job['plan_revision']
                or type(task.get('requirement_revision')) is not int or task['requirement_revision'] != job['requirement_revision']
                or task.get('spec_source_approval_id') != job['plan_approval_id']): continue
        wp = by_id.get(task.get('work_package_id'))
        if wp:
            if task.get('workflow_generation',0) == wp.get('workflow_generation',0): result.append(task)
        elif task.get('work_package_id') is None and task.get('workflow_job_task') is True and task.get('status') != 'cancelled':
            binding = task.get('workflow_package_generations')
            if binding is None:
                if any(generations.values()): continue  # Historical initial queue only.
            elif (not isinstance(binding,dict) or any(type(v) is not int for v in binding.values()) or binding != generations): continue
            global_tasks.append(task)
    reviews = [t for t in global_tasks if t.get('role') == 'reviewer']
    if reviews:
        latest = max(enumerate(reviews),key=lambda pair:(pair[1].get('created_at') or 0,pair[0]))[1]
        cycle = latest.get('review_cycle')
        global_tasks = [t for t in global_tasks if t['id'] == cycle
                        or (t.get('role') == 'reviewer' and t.get('review_cycle') == cycle)
                        or t.get('role') not in ('builder','reviewer')]
    result.extend(global_tasks)
    return result


def _workflow_task_state(task):
    result = task.get('result') or {}
    if (task.get('current_attempt') or {}).get('status') == 'running': return 'running'
    if task.get('status') == 'handed_off' or result.get('status') == 'handoff': return 'handed_off'
    if task.get('status') == 'interrupted': return 'interrupted'
    if task.get('recovery_check_required'): return 'blocked'
    if task.get('status') == 'succeeded':
        if result.get('status') != 'done': return 'needs_changes'
        if result.get('pending_checks') or result.get('blocking_reasons'): return 'unconfirmed'
        return 'execution_succeeded'
    return task.get('status') if task.get('status') in ('queued','running','awaiting_approval','blocked','failed','cancelled') else 'unconfirmed'


def workflow_state_digest(job, tasks, approvals, packages):
    """Stable acceptance binding excluding the acknowledgement/decision itself."""
    from team_wbs import digest, task_evidence_digest
    current = [w for w in packages if w.get('job_id') == job['id'] and w.get('plan_revision') == job['plan_revision']]
    ids = {job.get('plan_approval_id'),job.get('requirements_approval_id')} | {w.get('feature_review_approval_id') for w in current}
    return digest({'job_binding':{k:job.get(k) for k in ('id','project','request_origin','route','workflow_contract_version','p0_contract_version')},
        'versions':{k:job.get(k) for k in ('requirement_revision','requirements_approval_id','plan_revision','plan_approval_id')},
        'packages':sorted(current,key=lambda w:w['id']),
        'tasks':{t['id']:task_evidence_digest(t) for t in current_workflow_tasks(job,tasks,current)},
        'authorities':{a['id']:{k:a.get(k) for k in ('kind','status','digest','review_acknowledgement','workflow_revision')}
                       for a in approvals if a.get('job_id') == job['id'] and a.get('id') in ids}})


def workflow_progress(job, tasks, approvals, *, work_packages=None, events=None):
    """Persisted-state projection. It never checks files, starts work or grants acceptance."""
    from team_wbs import approved_source, validate_projection, feature_review_receipt
    result = {'state':'inconclusive','reason':'workflow_records_required','packages':[],
              'counts':{'packages':0,'technical_complete':0,'feature_approved':0,'feature_waiting':0,'invalidated':0},
              'tasks':{},'historical_tasks':0,'current_task_ids':[],
              'uat':{'required':0,'confirmed':0,'complete':False},'formal_receipt':False,
              'review_available':False,'evidence_recording_available':False,
              'ready_for_formal_acceptance':False,'complete':False,'filesystem_verified':False,
              'permission_effect':'none','decides_nothing':True}
    try:
        if workflow_version(job) != 1: raise ValueError('workflow_version_required')
        if work_packages is None or events is None: return result
        owned = [t for t in tasks if t.get('job_id') == job['id']]
        approvals = [a for a in approvals if a.get('job_id') == job['id']]
        events = [e for e in events if e.get('job_id') == job['id']]
        packages = [w for w in work_packages if w.get('job_id') == job['id'] and w.get('plan_revision') == job['plan_revision']]
        records = _WorkflowRecords(job,owned,approvals,packages)
        plan = records.get(job.get('plan_approval_id'),'approval')
        requirements = records.get(job.get('requirements_approval_id'),'approval')
        import os
        boundary = (plan.get('workflow_revision') or {}).get('workspace_boundary') or {}
        if (boundary.get('status') != 'ready' or not isinstance(boundary.get('execution_root'),str)
                or os.path.normcase(os.path.normpath(boundary['execution_root'])) != os.path.normcase(os.path.normpath(job['project']))):
            raise ValueError('workflow_project_binding_changed')
        # Stopped jobs may be inspected; this grants no permission to dispatch.
        source_job = dict(job,status='blocked') if job.get('status') in ('cancelled','interrupted') else job
        specs = approved_source(source_job,plan,requirements,for_review=True)
        if any((job.get('goal_contract') or {}).get(k) != requirements['payload']['goal_contract'].get(k)
               for k in ('primary_goal','deliverables','acceptance_criteria','non_goals','constraints')):
            raise ValueError('workflow_goal_contract_changed')
        validate_projection(job,plan,specs,packages)
        if any(type(w.get('workflow_generation',0)) is not int or w.get('workflow_generation',0) < 0 for w in packages):
            raise ValueError('workflow_generation_invalid')
        current = current_workflow_tasks(job,owned,packages)
        result['current_task_ids'] = [t['id'] for t in current]
        result['historical_tasks'] = len(owned)-len(current)
        states = [_workflow_task_state(t) for t in current]
        result['tasks'] = {s:states.count(s) for s in sorted(set(states))}
        result['tasks']['total'] = len(current)
        result['tasks']['incomplete'] = sum(s != 'execution_succeeded' for s in states)
        cache = {}; by_label = {s['wbs_id']:s for s in specs}
        for spec in specs:
            wp = next(w for w in packages if w['wbs_id'] == spec['wbs_id'])
            members = [t for t in current if t.get('work_package_id') == wp['id']]
            technical = (bool(members) and wp['execution_state'] == 'succeeded'
                         and any(t['role'] == 'reviewer' for t in members)
                         and all(_workflow_task_state(t) == 'execution_succeeded' for t in members))
            approved = False; reason = None
            if wp['review_status'] == 'approved':
                try:
                    feature_review_receipt(records,job,plan,requirements,wp,by_label,packages,cache,
                        decision_lookup=lambda ident:_workflow_event(events,'feature_review_decided',ident))
                    approved = technical
                except (ValueError,KeyError,TypeError): reason = 'feature_receipt_not_current'
            item = {'id':wp['id'],'wbs_id':wp['wbs_id'],'title':wp['title'],'execution_state':wp['execution_state'],
                    'review_status':wp['review_status'],'technical_complete':technical,'feature_approved':approved,
                    'reason':reason,'task_ids':[t['id'] for t in members]}
            result['packages'].append(item)
        counts = result['counts']; counts['packages'] = len(packages)
        counts['technical_complete'] = sum(w['technical_complete'] for w in result['packages'])
        counts['feature_approved'] = sum(w['feature_approved'] for w in result['packages'])
        counts['feature_waiting'] = len(packages)-counts['feature_approved']
        counts['invalidated'] = sum(w['review_status'] == 'invalidated' or bool(w['reason']) for w in result['packages'])
        all_features = bool(packages) and counts['feature_approved'] == len(packages)
        clean = bool(current) and result['tasks']['incomplete'] == 0
        completions = [a for a in approvals if a.get('kind') == 'completion']
        latest = completions[-1] if completions else None
        global_tasks = {t['id']:t for t in current if t.get('workflow_job_task') is True}
        payload = (latest or {}).get('payload') or {}; task = global_tasks.get((latest or {}).get('task_id'))
        receipt_current = (task is not None and latest.get('status') in ('pending','approved') and task.get('role') == 'reviewer'
                           and _workflow_task_state(task) == 'execution_succeeded'
                           and latest.get('attempt') == task.get('attempt')
                           and latest.get('digest') == hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()).hexdigest())
        required = list(requirements['payload']['goal_contract']['acceptance_criteria']) + ['完成物が起動し、操作できる']
        recorded_checks = set()
        if (receipt_current and all_features and clean and job.get('status') in
                ('awaiting_acceptance','accepted','accepted_with_pending_checks')):
            sources = {a['id'] for a in completions if a.get('task_id') == task['id'] and a.get('attempt') == task.get('attempt')}
            for item in payload.get('resolved_checks') or []:
                if (item.get('authority') == 'user_confirmed' and item.get('source_approval_id') in sources
                        and isinstance(item.get('evidence'),str) and item['evidence'].strip()
                        and isinstance(item.get('confirmed_at'),(int,float))): recorded_checks.add(item.get('check'))
        result['uat'] = {'required':len(required),'confirmed':sum(c in recorded_checks for c in required),
                         'complete':all(c in recorded_checks for c in required),
                         'unconfirmed':[c for c in required if c not in recorded_checks]}
        ack = (latest or {}).get('review_acknowledgement') or {}
        acknowledged = (receipt_current and ack.get('action') == 'review_result_confirmation'
            and ack.get('actor') == 'authenticated_local_app_session' and ack.get('approval_id') == latest['id']
            and ack.get('authorization') == 'session_cookie_and_ui_marker'
            and ack.get('task_id') == task['id'] and ack.get('from_status') == ack.get('to_status') == 'awaiting_acceptance'
            and ack.get('target_job_id') == job['id'] and ack.get('payload_digest') == latest['digest']
            and isinstance(ack.get('target_version_token'),str)
            and ack.get('target_state',{}).get('workflow_digest') == workflow_state_digest(job,owned,approvals,packages)
            and _workflow_event(events,'review_result_acknowledged',latest['id']) == ack)
        pending_other = any(a['status'] == 'pending' and a['id'] != (latest or {}).get('id') for a in approvals)
        result['review_available'] = bool(all_features and clean and receipt_current and not pending_other
                                          and latest['status'] == 'pending' and job['status'] == 'awaiting_acceptance')
        result['evidence_recording_available'] = bool(result['review_available'] and payload.get('pending_items'))
        evidence_ready = (all_features and clean and receipt_current and acknowledged
                 and result['uat']['complete'] and not payload.get('pending_items') and not payload.get('review_findings'))
        result['ready_for_formal_acceptance'] = bool(evidence_ready and not pending_other
            and latest['status'] == 'pending' and job['status'] == 'awaiting_acceptance')
        decision = (latest or {}).get('decision') or {}
        accepted = (evidence_ready and job['status'] == 'accepted' and latest['status'] == 'approved'
            and decision.get('action') == 'formal_acceptance' and decision.get('actor') == 'authenticated_local_app_session'
            and decision.get('authorization') == 'session_cookie_and_ui_marker' and decision.get('task_id') == task['id']
            and decision.get('from_status') == 'awaiting_acceptance'
            and decision.get('approval_id') == latest['id'] and decision.get('target_job_id') == job['id']
            and decision.get('to_status') == 'accepted' and decision.get('conditional') is False
            and decision.get('unresolved_items') == 0
            and _workflow_event(events,'formal_acceptance_decided',latest['id']) == decision)
        result.update(formal_receipt=bool(accepted),complete=bool(accepted),
            state='complete' if accepted else 'invalidated' if counts['invalidated'] else 'incomplete',reason=None)
    except (ValueError,KeyError,TypeError,AttributeError):
        result.update(state='inconclusive',reason='workflow_authority_or_records_invalid',complete=False,
                      formal_receipt=False,ready_for_formal_acceptance=False,review_available=False,evidence_recording_available=False)
    return result


def progress(job, tasks, approvals, *, work_packages=None, events=None):
    base = _legacy_progress(job,tasks,approvals)
    if 'workflow_contract_version' not in job: return base
    workflow = workflow_progress(job,tasks,approvals,work_packages=work_packages,events=events)
    base['workflow'] = workflow
    base['completed'] = [w['title'] for w in workflow['packages'] if w['feature_approved']]
    base['remaining'] = [w['title'] for w in workflow['packages'] if not w['feature_approved']]
    if not workflow['complete']: base['remaining'].append('利用者による依頼全体の確認・正式受領')
    base['completion_status'] = ('accepted' if workflow['complete'] else
        'accepted_with_pending_checks' if job.get('status') == 'accepted_with_pending_checks' else
        'awaiting_user_acceptance' if job.get('status') == 'awaiting_acceptance' else 'in_progress')
    if not workflow['complete']:
        criteria = list((job.get('goal_contract') or {}).get('acceptance_criteria') or [])
        base['unmet_acceptance_criteria'] = (criteria if workflow['state'] == 'inconclusive' else
            [c for c in criteria if c in workflow['uat'].get('unconfirmed',[])])
        base['next_action'] = ('対象版と機能・受領の証拠を確認する' if workflow['state'] in ('inconclusive','invalidated')
                               else '依頼全体の正式受領を判断する' if workflow['ready_for_formal_acceptance']
                               else '完成物を起動・操作し、残る受入条件を確認する' if workflow['evidence_recording_available']
                               else '機能の実装・技術確認・本人確認を順に進める')
        base['available_actions'] = [a for a in base['available_actions'] if a == 'cancel_job']
        if workflow['review_available']: base['available_actions'].append('review_completion')
        if workflow['evidence_recording_available']: base['available_actions'].append('record_completion_evidence')
        if workflow['ready_for_formal_acceptance']: base['available_actions'].append('accept_completion')
    return base


def _legacy_progress(job, tasks, approvals):
    """A user-facing projection from persisted work, never an independent ledger."""
    contract = job.get('goal_contract') or {}
    interruption = job.get('p0_interruption') or {}
    maintenance_wait = interruption.get('recovery_status') == 'maintenance_required'
    network_wait = interruption.get('recovery_status') == 'network_wait'
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
        elif network_wait:
            next_action = '接続と途中成果を確認し、停止した担当から再開する'
        elif interruption.get('recovery_status') == 'safety_blocked':
            next_action = '安全上の問題を確認する'
        elif job.get('status') == 'awaiting_approval':
            next_action = '開発計画またはAI利用条件を確認する'
        else:
            next_action = title(active) if active else (remaining[0] if remaining else '作業状態を確認する')
    return {
        'completed': completed,
        'current': ('保守対応待ち' if maintenance_wait else '通信回復後の再開待ち' if network_wait else title(active) if active else
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
