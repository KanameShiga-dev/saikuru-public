"""Approved-plan work packages; definitions/dependencies stay in plan payload.

WBS labels are business identifiers. Work package object IDs, Task IDs, native
session IDs and attempt IDs remain separate namespaces. Nothing here dispatches
a worker, approves a plan or accepts a feature/deliverable.
"""
from copy import deepcopy
import hashlib
import json
import re
import time

MAX_PACKAGES = 6  # Preserve the existing initial plan's 1..6 Task bound.
GUIDE = ('\n新方式の計画はwork_packagesとcheckpoint_pathsを含める。work_packagesは'
         'wbs_id,title,goal_links,done_when,depends_onを持つ。wbs_idは英字始まりの一意のラベルで、'
         'Task/session/attemptのIDではない。各tasks項目に既存ラベルのwbs_idを付け、'
         'goal_linksは承認対象の成果物・受入条件の文字列をそのまま参照する。または、成果物はD1,D2,...、'
         '受入条件はA1,A2,...（goal_contractの配列順で1始まり、最大各20）の番号だけで参照できる。'
         '番号は大文字・半角の完全一致のみ有効で、存在しない番号や言い換えは差し戻される。'
         'done_whenはこの作業パッケージの完了条件で、本人の機能確認・正式受領とは別。'
         '各パッケージは成果物と受入条件の両方へ対応させ、全体の成果物・受入条件を漏らさない。'
         'input_artifactsは既に存在する同じ依頼の登録済み先行成果物のjob_id,path,sha256。'
         'これから作る成果物はここへ捏造せずdepends_onで示し、既存入力がなければ空配列。'
         '作業の単位は確認可能な成果と完了条件とし、トークン数だけで分割しない。'
         '依存はパッケージのラベルで示す。checkpoint_pathsは承認を求める具体的な相対ファイルだけ。'
         '質問時はtasks/work_packages/checkpoint_pathsを空配列にし、未承認の実行はしない。'
         '一度の計画で扱う作業パッケージは1〜6件。7件以上が必要な依頼は、6件に詰め込んだり捨てたりせず、'
         '目的と依存の順を保った段階（先行の計画と後続の計画）に分け、今回は最初の段階だけを書く。'
         '後続の段階への成果物の受け渡しはinput_artifactsで示し、後続は再承認を経て進める。')

# 7件以上の依頼は切り捨てず、段階に分ける案内を返す。画面にもこの内容を出す。
SIZE_ADVICE = {
    'limit': MAX_PACKAGES,
    'reason': f'一度の計画で扱える作業パッケージは1〜{MAX_PACKAGES}件です。それを超える分を黙って省くことはしません。',
    'next': ['目的と依存の順を保ち、依頼を段階に分ける',
             '今回は最初の段階だけを計画して承認する',
             '次の段階は、先行段階の成果物を入力にして、あらためて計画・承認する'],
}

# 待機理由のコード → 種類と、次に動く人。種類ごとの説明文は画面側にある。未知のコードは保守担当の確認にする。
WAIT_ADVICE = {
    'job_not_running': ('stopped', 'user'),
    'workspace_or_series_busy': ('capacity', 'system'),
    'no_ready_task': ('dependency', 'system'),
    'after_not_succeeded': ('dependency', 'system'),
    'retry_wait': ('retry', 'system'),
    'recovery_confirmation_required': ('maintenance', 'maintainer'),
    'task_plan_not_current': ('consistency', 'maintainer'),
    'workflow_authority_or_records_invalid': ('consistency', 'maintainer'),
    'dependency_feature_review_invalid': ('consistency', 'maintainer'),
}
# 人の確認で解消する待機。確認が出ていればその相手（user / authorized_person）、まだ出ていなければ既定の担当。
REVIEW_WAITS = {'pending_decision': 'user', 'dependency_feature_review_required': 'system'}


def wait_advice(code, approver=None, preparing=False):
    """Read-only description of one dispatch reason code; never grants a dispatch.

    approver: who holds an actionable pending approval ('user' or 'authorized_person'), if any.
    preparing: a finished feature still needs its review started, which only the user can do.
    """
    if code in REVIEW_WAITS:
        kind = 'decision' if code == 'pending_decision' else 'dependency'
        owner = approver or ('user' if preparing else REVIEW_WAITS[code])
    elif code in WAIT_ADVICE:
        kind, owner = WAIT_ADVICE[code]
    elif code.startswith('dependency_'):
        kind, owner = 'dependency', 'system'
    else:
        kind, owner = 'unknown', 'maintainer'
    return {'code': code, 'kind': kind, 'owner': owner}

LINK_DESCRIPTION = ('goal_contractの成果物・受入条件の文字列そのもの、または成果物はD1,D2,...、'
                    '受入条件はA1,A2,...の番号（配列順で1始まり、大文字・半角の完全一致のみ）。'
                    '存在しない番号や言い換えは差し戻される。')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _texts(value, label, *, required=True):
    if (not isinstance(value, list) or len(value) > 20 or (required and not value)
            or any(not isinstance(item, str) or not item.strip() or item != item.strip()
                   or len(item) > 500 for item in value) or len(value) != len(set(value))):
        raise ValueError('work_package_invalid_' + label)
    return value


_GOAL_LABEL = re.compile(r'[DA][1-9][0-9]*')


def normalize_goal_links(payload):
    """New proposals only: expand exact D<n>/A<n> labels in goal_links to contract text.

    Numbers follow the contract array order (1-based). Exact contract text is kept,
    any other string is left for validation to reject, and an unknown number is
    refused here. Never applied to stored approvals, so their digests stay put.
    """
    contract = payload.get('goal_contract')
    table = {}
    if isinstance(contract, dict):
        for prefix, key in (('D', 'deliverables'), ('A', 'acceptance_criteria')):
            items = contract.get(key)
            if isinstance(items, list):
                table.update({f'{prefix}{n}': text for n, text in enumerate(items, 1) if isinstance(text, str)})
    texts = set(table.values())
    result = deepcopy(payload)
    for spec in (result.get('work_packages') or []) + (result.get('tasks') or []):
        links = spec.get('goal_links') if isinstance(spec, dict) else None
        if not isinstance(links, list):
            continue
        for index, link in enumerate(links):
            if isinstance(link, str) and link not in texts and _GOAL_LABEL.fullmatch(link):
                if link not in table:
                    raise ValueError('work_package_goal_label_unknown')
                links[index] = table[link]
    return result


def validate_work_packages(payload, job=None, *, check_current_inputs=True):
    """Validate an initial proposal without making it authoritative."""
    from team_checkpoint import scope_paths
    if not isinstance(payload, dict): raise ValueError('work_package_payload_invalid')
    packages = payload.get('work_packages')
    tasks = payload.get('tasks')
    contract = payload.get('goal_contract') or {}
    if not isinstance(contract, dict): raise ValueError('work_package_contract_invalid')
    deliverables = set(_texts(contract.get('deliverables'), 'deliverables'))
    criteria = set(_texts(contract.get('acceptance_criteria'), 'acceptance_criteria'))
    allowed = deliverables | criteria
    if isinstance(packages, list) and len(packages) > MAX_PACKAGES:
        raise ValueError('work_package_count_over_limit')
    if not isinstance(packages, list) or not 1 <= len(packages) <= MAX_PACKAGES:
        raise ValueError('work_package_count_invalid')
    labels, folded = {}, set()
    for spec in packages:
        # Older approved W04.01 payloads lack input_artifacts; do not rewrite
        # their authority/digest. Newly generated schemas require the field.
        required = {'wbs_id', 'title', 'goal_links', 'done_when', 'depends_on'}
        if not isinstance(spec, dict) or not required <= set(spec) or set(spec) - required - {'input_artifacts'}:
            raise ValueError('work_package_fields_invalid')
        label = spec['wbs_id']
        if (not isinstance(label, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9._-]{0,63}', label)
                or label.casefold() in folded):
            raise ValueError('work_package_label_invalid')
        if not isinstance(spec['title'], str) or not spec['title'].strip() or len(spec['title']) > 160:
            raise ValueError('work_package_title_invalid')
        links = _texts(spec['goal_links'], 'goal_links')
        _texts(spec['done_when'], 'done_when')
        dependencies = _texts(spec['depends_on'], 'depends_on', required=False)
        if (not set(links) <= allowed or not set(links) & deliverables
                or not set(links) & criteria or label in dependencies):
            raise ValueError('work_package_reference_invalid')
        validate_input_artifacts(spec.get('input_artifacts', []), job, check_current=check_current_inputs)
        labels[label] = spec; folded.add(label.casefold())
    if set().union(*(set(spec['goal_links']) for spec in packages)) != allowed:
        raise ValueError('work_package_goal_coverage_missing')
    if any(not set(spec['depends_on']) <= set(labels) for spec in packages):
        raise ValueError('work_package_dependency_unknown')
    # Structural cycles are invalid data; dispatch/readiness is W04.02+.
    seen, visiting = set(), set()
    def visit(label):
        if label in visiting: raise ValueError('work_package_dependency_cycle')
        if label in seen: return
        visiting.add(label)
        for dependency in labels[label]['depends_on']: visit(dependency)
        visiting.remove(label); seen.add(label)
    for label in labels: visit(label)
    if isinstance(tasks, list) and len(tasks) > MAX_PACKAGES:
        raise ValueError('work_package_count_over_limit')
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_PACKAGES:
        raise ValueError('work_package_tasks_invalid')
    assigned = set()
    for task in tasks:
        if not isinstance(task, dict) or not isinstance(task.get('wbs_id'), str) or task['wbs_id'] not in labels:
            raise ValueError('work_package_task_reference_invalid')
        if (task.get('role') not in ('builder', 'researcher')
                or not isinstance(task.get('title'), str) or not task['title'].strip() or len(task['title']) > 160
                or not isinstance(task.get('instruction'), str) or not task['instruction'].strip() or len(task['instruction']) > 12000
                or not set(_texts(task.get('goal_links'), 'task_goal_links')) <= set(labels[task['wbs_id']]['goal_links'])):
            raise ValueError('work_package_task_spec_invalid')
        assigned.add(task['wbs_id'])
    if assigned != set(labels):
        raise ValueError('work_package_without_task')
    positions = {label: [i for i, task in enumerate(tasks) if task['wbs_id'] == label] for label in labels}
    if any(max(positions[dependency]) >= min(positions[label])
           for label, spec in labels.items() for dependency in spec['depends_on']):
        raise ValueError('work_package_after_order_conflict')
    scope_paths(payload.get('checkpoint_paths'))
    return deepcopy(packages)


def validate_input_artifacts(refs, job, *, check_current=True):
    """Existing inputs only; future outputs use depends_on, not fake hashes.

    Proposal/current-input mode reuses Job.p0_artifacts and its safe file
    verifier. Historical mode validates syntax and Job only; an approved
    payload's digest authenticates its approval-time version. Neither mode
    establishes producer WP review or dispatch permission (W04.03+).
    """
    if not isinstance(refs, list) or len(refs) > 20:
        raise ValueError('work_package_input_artifacts_invalid')
    seen = set()
    for ref in refs:
        if (not isinstance(ref, dict) or set(ref) != {'job_id', 'path', 'sha256'}
                or not isinstance(ref['job_id'], str) or not ref['job_id']
                or not isinstance(ref['path'], str) or not ref['path']
                or not isinstance(ref['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', ref['sha256'])):
            raise ValueError('work_package_input_artifacts_invalid')
        if not isinstance(job, dict) or ref['job_id'] != job.get('id'):
            raise ValueError('work_package_input_job_mismatch')
        from team_p0_contract import artifact_relative_path
        relative = artifact_relative_path(ref['path'])
        if relative.drive or relative.root or relative.as_posix() != ref['path']:
            raise ValueError('work_package_input_artifacts_invalid')
        if ref['path'] in seen:
            raise ValueError('work_package_input_artifacts_invalid')
        seen.add(ref['path'])
        if not check_current:
            # An approved input describes its approval-time version. Reading
            # that plan must not depend on today's registry or working tree.
            continue
        # Reject unregistered/ambiguous refs before reading any file.
        registry = job.get('p0_artifacts') or []
        if not isinstance(registry, list):
            raise ValueError('work_package_input_artifacts_invalid')
        matches = [row for row in registry if isinstance(row, dict) and row.get('path') == ref['path']]
        if len(matches) != 1 or matches[0].get('sha256') != ref['sha256']:
            raise ValueError('work_package_input_artifact_missing_or_changed')
        from team_p0_contract import artifact_records
        try:
            actual = artifact_records(job['project'], [ref['path']])
        except (ValueError, KeyError, TypeError, OSError):
            raise ValueError('work_package_input_artifact_unavailable') from None
        if actual != [{'path': ref['path'], 'sha256': ref['sha256']}]:
            raise ValueError('work_package_input_artifact_missing_or_changed')


def plan_schema(base):
    """Opt-in schema only; caller keeps every existing G3/goal/test field."""
    schema = deepcopy(base)
    strings = {'type': 'array', 'maxItems': 20, 'items': {'type': 'string'}}
    links = dict(deepcopy(strings), description=LINK_DESCRIPTION)
    properties = {'wbs_id': {'type': 'string'}, 'title': {'type': 'string'},
                  'goal_links': links, 'done_when': deepcopy(strings), 'depends_on': deepcopy(strings)}
    properties['input_artifacts'] = {'type': 'array', 'maxItems': 20, 'items': {'type': 'object',
        'properties': {name: {'type': 'string'} for name in ('job_id', 'path', 'sha256')},
        'required': ['job_id', 'path', 'sha256'], 'additionalProperties': False}}
    schema['properties']['work_packages'] = {'type': 'array', 'maxItems': MAX_PACKAGES,
        'items': {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}}
    from team_checkpoint import MAX_FILES
    schema['properties']['checkpoint_paths'] = {'type': 'array', 'maxItems': MAX_FILES, 'items': {'type': 'string'}}
    schema['required'] += ['work_packages', 'checkpoint_paths']
    task = schema['properties']['tasks']['items']
    task['properties']['goal_links'] = dict(task['properties'].get('goal_links') or strings, description=LINK_DESCRIPTION)
    task['properties']['wbs_id'] = {'type': 'string'}
    task['required'].append('wbs_id')
    return schema


def approved_source(job, approval, requirements, *, for_review=False):
    from team_p0_contract import workflow_version
    statuses = ('awaiting_approval','running','blocked','failed','awaiting_acceptance','accepted','accepted_with_pending_checks') if for_review else ('awaiting_approval','running')
    if (workflow_version(job) != 1 or job.get('status') not in statuses
            or type(job.get('plan_revision')) is not int or job['plan_revision'] < 1
            or type(job.get('requirement_revision')) is not int or job['requirement_revision'] < 1):
        raise ValueError('work_package_current_plan_required')
    for row, pointer in ((approval, 'plan_approval_id'), (requirements, 'requirements_approval_id')):
        if (row.get('id') != job.get(pointer) or row.get('job_id') != job['id']
                or row.get('kind') != 'plan' or row.get('status') != 'approved'
                or not isinstance(row.get('payload'), dict)
                or row.get('digest') != hashlib.sha256(json.dumps(row['payload'], ensure_ascii=False,
                                                               sort_keys=True).encode()).hexdigest()):
            raise ValueError('work_package_approval_invalid')
    payload = approval['payload']
    original = requirements['payload'].get('goal_contract') or {}
    current = payload.get('goal_contract') or {}
    if not isinstance(original, dict) or not isinstance(current, dict) or payload.get('questions'):
        raise ValueError('work_package_requirements_invalid')
    if any(original.get(key) != current.get(key) for key in (
            'primary_goal', 'deliverables', 'acceptance_criteria', 'non_goals', 'constraints')):
        raise ValueError('work_package_requirements_changed')
    return validate_work_packages(payload, job, check_current_inputs=False)


def projection(job, approval, spec):
    return dict(schema_version=1, job_id=job['id'], wbs_id=spec['wbs_id'], title=spec['title'],
                goal_links=deepcopy(spec['goal_links']), done_when=deepcopy(spec['done_when']),
                requirement_revision=job['requirement_revision'], requirements_approval_id=job['requirements_approval_id'],
                plan_revision=job['plan_revision'], spec_source_approval_id=approval['id'], spec_digest=digest(spec))


def validate_projection(job, approval, specs, records):
    expected = {spec['wbs_id']: projection(job, approval, spec) for spec in specs}
    if len(records) != len(expected) or {row.get('wbs_id') for row in records} != set(expected):
        raise ValueError('work_package_projection_incomplete')
    for row in records:
        if (any(type(row.get(key)) is not type(value) or row[key] != value
                for key, value in expected[row['wbs_id']].items())
                or row.get('execution_state') not in ('planned', 'ready', 'running', 'succeeded', 'blocked', 'cancelled')
                or row.get('review_status') not in ('not_requested', 'pending', 'approved', 'changes_requested', 'invalidated')
                or 'task_ids' in row or 'depends_on' in row):
            raise ValueError('work_package_projection_changed')
    return {row['wbs_id']: row['id'] for row in records}


def task_evidence_digest(task):
    return digest({key: task.get(key) for key in ('id', 'role', 'agent_name', 'profile','instruction','after','goal_links',
        'work_package_id','plan_revision','requirement_revision','spec_source_approval_id','workflow_generation',
        'workflow_authority','review_cycle','status', 'result',
        'validation_runs', 'test_asset_snapshot', 'test_assets', 'current_attempt', 'workflow_artifact_records',
        'workflow_validation_evidence')})


def package_members(store, job_id, wp):
    generation = wp.get('workflow_generation', 0)
    return [t for t in store.for_job('task', job_id) if t.get('work_package_id') == wp['id']
            and t.get('workflow_generation', 0) == generation]


def issued_task_digest(task):
    return digest({k:task.get(k) for k in ('id','job_id','work_package_id','plan_revision',
        'requirement_revision','spec_source_approval_id','workflow_generation','workflow_authority',
        'title','instruction','role','profile','agent_name','goal_links','after','repair','review_cycle')})


def issued_task_valid(store, task, wp):
    authority = task.get('workflow_authority') or {}
    if (authority.get('kind') not in ('package_review','package_repair','package_recheck')
            or task.get('workflow_generation', 0) != wp.get('workflow_generation', 0)):
        return False
    row = store.db.execute("SELECT body FROM events WHERE json_extract(body,'$.type')='workflow_task_issued' "
        "AND json_extract(body,'$.task_id')=? ORDER BY seq DESC LIMIT 1", (task['id'],)).fetchone()
    if not row: return False
    details = json.loads(row[0]).get('details') or {}
    candidate = deepcopy(task)
    history = task.get('instruction_history') or []
    if history:
        if not isinstance(history,list) or not isinstance(history[0],dict): return False
        original = history[0].get('instruction')
        if not isinstance(original,str) or not plan_instruction_matches(task,original): return False
        candidate['instruction'] = original
    return details.get('task_digest') == issued_task_digest(candidate)


def repair_impact(store, job, plan, seed, extra_paths=()):
    """Reverse dependency/file-reference closure with evidence retained as history."""
    specs = {s['wbs_id']:s for s in plan['payload']['work_packages']}
    rows = [w for w in store.for_job('work_package',job['id']) if w['plan_revision'] == job['plan_revision']]
    changed = set(extra_paths)
    paths = set(changed)
    for t in package_members(store,job['id'],seed):
        paths.update(r['path'] for r in t.get('workflow_artifact_records') or [])
        paths.update(p for p in (t.get('result') or {}).get('artifacts') or [] if isinstance(p,str))
    if seed.get('feature_review_approval_id'):
        old = (store.get(seed['feature_review_approval_id'],'approval').get('payload') or {}).get('feature_target') or {}
        paths.update(old.get('source_snapshot') or {})
    affected = {seed['wbs_id']}; reasons = {seed['id']:['reported_defect']}
    for wp in rows:
        if wp['id'] == seed['id']: continue
        touched = {r['path'] for r in specs[wp['wbs_id']].get('input_artifacts', [])}
        test_refs = set()
        if wp.get('feature_review_approval_id'):
            target = (store.get(wp['feature_review_approval_id'],'approval').get('payload') or {}).get('feature_target') or {}
            touched.update(target.get('source_snapshot') or {})
            test_refs.update(target.get('test_snapshot') or {})
        # A broad executed-test snapshot is evidence, not ownership. A file
        # reported by the rejected builder must not invalidate another WP
        # merely because it was also included in that WP's test run.
        if paths & touched or changed & test_refs:
            affected.add(wp['wbs_id']); reasons[wp['id']] = ['shared_evidence_files']
    while True:
        grown = affected | {s['wbs_id'] for s in specs.values() if set(s['depends_on']) & affected}
        if grown == affected: break
        affected = grown
    upstream = set(specs[seed['wbs_id']]['depends_on'])
    while True:
        grown = upstream | {label for parent in upstream for label in specs[parent]['depends_on']}
        if grown == upstream: break
        upstream = grown
    return {'affected_ids':[w['id'] for w in rows if w['wbs_id'] in affected],
            'preserved_ids':[w['id'] for w in rows if w['wbs_id'] not in affected],
            'upstream_impact_ids':[w['id'] for w in rows if w['wbs_id'] in affected & upstream],
            'reasons':{w['id']:reasons.get(w['id'],['dependent_on_changed_feature']) for w in rows if w['wbs_id'] in affected},
            'file_refs':sorted(paths)}


def scope_file_observation(job, plan):
    """Observe the finite approved scope, including creation/deletion; no content."""
    from pathlib import Path
    from team_p0_contract import artifact_records, artifact_relative_path
    result = {}
    for path in plan['payload']['checkpoint_paths']:
        artifact_relative_path(path)
        candidate = Path(job['project'])/path
        if not candidate.exists() and not candidate.is_symlink(): result[path] = None
        else: result[path] = artifact_records(job['project'],[path])[0]['sha256']
    return result


def feature_basis(store, job_id, wp_id):
    job = store.get(job_id,'job'); plan = store.get(job.get('plan_approval_id'),'approval')
    requirements = store.get(job.get('requirements_approval_id'),'approval')
    specs = approved_source(job,plan,requirements,for_review=True)
    rows = [w for w in store.for_job('work_package',job_id) if w['plan_revision'] == job['plan_revision']]
    validate_projection(job,plan,specs,rows)
    wp = next((w for w in rows if w['id'] == wp_id),None)
    if wp is None: raise ValueError('work_package_not_current')
    for approval in store.for_job('approval',job_id):
        if (approval['status'] == 'pending' and not (approval['id'] == wp.get('feature_review_approval_id')
                                                    and approval.get('kind') == 'feature_review')):
            raise ValueError('workflow_pending_decision')
    tasks = store.for_job('task',job_id)
    if any(t['status'] in ('running','awaiting_approval') or (t.get('current_attempt') or {}).get('status') == 'running' for t in tasks):
        raise ValueError('workflow_writer_not_stopped')
    members = package_members(store,job_id,wp)
    if (not members or any(t['status'] != 'succeeded' or (t.get('result') or {}).get('status') != 'done'
                           or (t.get('result') or {}).get('pending_checks') or (t.get('result') or {}).get('blocking_reasons') for t in members)
            or not any(t['role'] == 'reviewer' for t in members)):
        raise ValueError('work_package_technical_review_required')
    builders = [t for t in members if t['role'] == 'builder']
    reviews = [t for t in members if t['role'] == 'reviewer']
    if any(not issued_task_valid(store,t,wp) for t in reviews):
        raise ValueError('work_package_review_authority_required')
    if not builders or any(t.get('agent_name') in {b.get('agent_name') for b in builders} for t in reviews):
        raise ValueError('work_package_independent_review_required')
    dependencies = {}; files = {}; cache = {}; by_label = {w['wbs_id']:w for w in rows}
    labels = next(s['depends_on'] for s in specs if s['wbs_id'] == wp['wbs_id'])
    for label in labels:
        dep = by_label[label]
        proof = feature_review_receipt(store,job,plan,requirements,dep,{s['wbs_id']:s for s in specs},rows,cache)
        dependencies[dep['id']] = {k:proof[k] for k in ('approval_id','target_digest')}; files.update(proof['files'])
    return {'job':job,'plan':plan,'requirements':requirements,'wp':wp,'members':members,'builders':builders,
            'dependencies':dependencies,'dependency_files':files,
            'signature':digest({'job':job,'plan':plan,'requirements':requirements,'wp':wp,'tasks':tasks,
                                'packages':rows,'approvals':store.for_job('approval',job_id)})}


def feature_target(basis):
    """Technical/file observation outside database locks; no user decision."""
    from team_p0_contract import artifact_records
    from team_code_test_contract import builder_issues, commands
    job, wp, plan = basis['job'], basis['wp'], basis['plan']
    allowed = set(plan['payload']['checkpoint_paths']) | set(plan['payload']['code_test_plan'].get('existing_tests',[]))
    records = {}; tests = {}
    for task in basis['builders']:
        issues, snapshot = builder_issues(job,task,task.get('result') or {})
        if issues: raise ValueError('work_package_validation_required')
        if commands(job) and not all(any(r.get('index') == i and r.get('exit_code') == 0 and not r.get('timed_out')
                                       for r in task.get('validation_runs') or []) for i in range(len(commands(job)))):
            raise ValueError('work_package_validation_required')
        tests.update(snapshot or {})
        for row in task.get('workflow_artifact_records') or []: records[row['path']] = row['sha256']
    if not records or not set(records) <= allowed or not set(tests) <= allowed:
        raise ValueError('work_package_artifacts_required')
    observed = artifact_records(job['project'],list(records))
    if observed != [{'path':p,'sha256':records[p]} for p in records]: raise ValueError('work_package_artifacts_changed')
    registry = {r['path']:r['sha256'] for r in job.get('p0_artifacts') or []}
    if any(registry.get(p) != h for p,h in records.items()): raise ValueError('work_package_artifacts_changed')
    if any(path in tests and tests[path] != sha for path,sha in basis['dependency_files'].items()):
        raise ValueError('dependency_artifact_changed')
    for path, sha in dict(tests,**basis['dependency_files']).items():
        if artifact_records(job['project'],[path]) != [{'path':path,'sha256':sha}]:
            raise ValueError('dependency_artifact_changed')
    return {'schema_version':1,'job_id':job['id'],'work_package_id':wp['id'],
            'workflow_generation':wp.get('workflow_generation',0),
            'requirement_revision':job['requirement_revision'],'plan_revision':job['plan_revision'],
            'requirements_approval_digest':basis['requirements']['digest'],'plan_approval_digest':plan['digest'],
            'criteria':{'goal_links':wp['goal_links'],'done_when':wp['done_when']},'blocking_refs':[],
            'evidence_refs':{t['id']:task_evidence_digest(t) for t in basis['members']},
            'source_snapshot':records,'test_snapshot':tests,'artifacts':observed,'dependency_targets':basis['dependencies']}


def transferred_target(old_target, job, plan, requirements, wp, evidence_refs, dependency_targets, transfer):
    """Feature target of a reused package in the new plan revision: same files and criteria, new versions and task ids."""
    criteria = {'goal_links':wp['goal_links'],'done_when':wp['done_when']}
    if old_target.get('criteria') != criteria: raise ValueError('workflow_transfer_criteria_changed')
    return dict(deepcopy(old_target),work_package_id=wp['id'],workflow_generation=wp.get('workflow_generation',0),
        requirement_revision=job['requirement_revision'],plan_revision=job['plan_revision'],
        requirements_approval_digest=requirements['digest'],plan_approval_digest=plan['digest'],
        evidence_refs=evidence_refs,dependency_targets=dependency_targets,evidence_transfer=transfer)


def workspace_resources(job, approval):
    from team_handoff import canonical
    boundary = (approval.get('workflow_revision') or {}).get('workspace_boundary') or {}
    paths = [job['project']]
    if boundary.get('status') == 'ready': paths.append(boundary['integration_root'])
    return sorted({canonical(path) for path in paths})


def plan_instruction_matches(task, expected):
    from team_instructions import compact_instruction
    history = task.get('instruction_history') or []
    if not isinstance(history, list) or len(history) > 1000: return False
    for row in history:
        if (not isinstance(row,dict) or row.get('instruction') != expected
                or row.get('reason') not in ('再試行前の指示整理','重複指示の整理')
                or not isinstance(row.get('submitted_note'),str)):
            return False
        expected = compact_instruction(expected,row['submitted_note'])
    return task.get('instruction') == expected


def feature_review_receipt(store, job, plan, requirements, wp, specs, records, cache, *, decision_lookup=None):
    """Read an existing typed decision; never issue or infer feature approval.

    W04.05 owns target creation, technical checks and authenticated decisions.
    The returned file refs are observations to verify outside the Store lock.
    """
    if wp['id'] in cache: return cache[wp['id']]
    if wp.get('execution_state') != 'succeeded' or wp.get('review_status') != 'approved':
        raise ValueError('dependency_feature_review_required')
    review = store.get(wp.get('feature_review_approval_id'), 'approval')
    payload = review.get('payload') or {}; target = payload.get('feature_target')
    if (review.get('job_id') != job['id'] or review.get('kind') != 'feature_review'
            or review.get('status') != 'approved' or review.get('intended_role') != 'end_user'
            or not isinstance(target, dict) or review.get('digest') != hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()):
        raise ValueError('dependency_feature_review_invalid')
    target_hash = digest(target)
    if (wp.get('feature_target_digest') != target_hash or payload.get('feature_target_digest') != target_hash
            or type(target.get('schema_version')) is not int or target['schema_version'] != 1
            or target.get('job_id') != job['id'] or target.get('work_package_id') != wp['id']
            or type(target.get('requirement_revision')) is not int or type(target.get('plan_revision')) is not int
            or target.get('requirement_revision') != job['requirement_revision']
            or target.get('plan_revision') != job['plan_revision']
            or target.get('requirements_approval_digest') != requirements['digest']
            or target.get('plan_approval_digest') != plan['digest']
            or target.get('criteria') != {'goal_links':wp['goal_links'], 'done_when':wp['done_when']}
            or target.get('blocking_refs') != []):
        raise ValueError('dependency_feature_target_changed')
    ack = review.get('review_acknowledgement') or {}
    if (ack.get('action') != 'feature_review_confirmation' or ack.get('actor') != 'authenticated_local_app_session'
            or ack.get('target_digest') != target_hash):
        raise ValueError('dependency_feature_confirmation_required')
    if decision_lookup is None:
        row = store.db.execute("SELECT body FROM events WHERE json_extract(body,'$.type')='feature_review_decided' "
            "AND json_extract(body,'$.details.approval_id')=? ORDER BY seq DESC LIMIT 1", (review['id'],)).fetchone()
        decision = (json.loads(row[0]).get('details') or {}) if row else {}
    else:
        decision = decision_lookup(review['id']) or {}
    expected = {'approval_id':review['id'], 'work_package_id':wp['id'], 'target_digest':target_hash,
        'decision':'approved', 'actor':'authenticated_local_app_session',
        'requirement_revision':job['requirement_revision'], 'plan_revision':job['plan_revision']}
    if any(decision.get(key) != value for key,value in expected.items()):
        raise ValueError('dependency_feature_decision_required')
    if target.get('workflow_generation',0) != wp.get('workflow_generation',0):
        raise ValueError('dependency_feature_target_changed')
    members = package_members(store,job['id'],wp)
    if (not members or any(t.get('status') != 'succeeded' for t in members)
            or not any(t.get('role') == 'reviewer' for t in members)
            or target.get('evidence_refs') != {t['id']:task_evidence_digest(t) for t in members}):
        raise ValueError('dependency_feature_evidence_changed')
    allowed = set(plan['payload']['checkpoint_paths']) | set(plan['payload']['code_test_plan'].get('existing_tests', []))
    files = {}
    from team_p0_contract import artifact_relative_path
    for key in ('source_snapshot', 'test_snapshot'):
        snapshot = target.get(key)
        required = key == 'source_snapshot' or plan['payload']['code_test_plan'].get('required')
        if not isinstance(snapshot, dict) or (required and not snapshot) or len(snapshot) > 84:
            raise ValueError('dependency_feature_snapshot_required')
        for path, sha in snapshot.items():
            artifact_relative_path(path)
            if path not in allowed or not isinstance(sha,str) or not re.fullmatch('[0-9a-f]{64}',sha):
                raise ValueError('dependency_feature_snapshot_invalid')
            if path in files and files[path] != sha: raise ValueError('dependency_feature_snapshot_invalid')
            files[path] = sha
    artifacts = target.get('artifacts')
    raw_registry = job.get('p0_artifacts') or []
    registry = {row['path']:row['sha256'] for row in raw_registry}
    if len(registry) != len(raw_registry): raise ValueError('dependency_feature_artifacts_changed')
    if (not isinstance(artifacts,list) or not artifacts or any(not isinstance(row,dict)
            or set(row) != {'path','sha256'} or files.get(row['path']) != row['sha256']
            or registry.get(row['path']) != row['sha256'] for row in artifacts)):
        raise ValueError('dependency_feature_artifacts_changed')
    dependencies = {}
    by_label = {row['wbs_id']:row for row in records}
    for label in specs[wp['wbs_id']]['depends_on']:
        proof = feature_review_receipt(store,job,plan,requirements,by_label[label],specs,records,cache,
                                      decision_lookup=decision_lookup)
        dependencies[by_label[label]['id']] = {'approval_id':proof['approval_id'],'target_digest':proof['target_digest']}
        for path,sha in proof['files'].items():
            if path in files and files[path] != sha: raise ValueError('dependency_feature_snapshot_invalid')
            files[path] = sha
    if target.get('dependency_targets') != dependencies:
        raise ValueError('dependency_feature_target_changed')
    proof = {'approval_id':review['id'],'target_digest':target_hash,'files':files}
    cache[wp['id']] = proof
    return proof


def dispatch_state(store, job_id, *, active=(), running_task_id=None):
    """Database-only single-series candidate selection; not permission to run."""
    from team_handoff import canonical
    job = store.get(job_id,'job')
    plan = store.get(job.get('plan_approval_id'),'approval')
    requirements = store.get(job.get('requirements_approval_id'),'approval')
    specs_list = approved_source(job,plan,requirements)
    specs = {s['wbs_id']:s for s in specs_list}
    records = [r for r in store.for_job('work_package',job_id) if r.get('plan_revision') == job['plan_revision']]
    mapping = validate_projection(job,plan,specs_list,records)
    tasks = store.for_job('task',job_id); approvals = store.for_job('approval',job_id)
    resources = workspace_resources(job,plan)
    busy = []
    for other in store.all('task'):
        if other['id'] == running_task_id: continue
        attempt = other.get('current_attempt') or {}
        if other['status'] not in ('running','awaiting_approval') and attempt.get('status') != 'running' and other['id'] not in active:
            continue
        other_job = store.get(other['job_id'],'job')
        held = attempt.get('workflow_resources') or [canonical(other_job['project'])]
        if other['job_id'] == job_id or set(resources) & set(held): busy.append(other['id'])
    base = {'job':job,'plan':plan,'requirements':requirements,'records':records,'tasks':tasks,'approvals':approvals,'busy':busy}
    signature = digest(base)
    result = {'status':'waiting','candidate_task_id':None,'reasons':[], 'state_signature':signature}
    if job['status'] != 'running': return dict(result,reasons=['job_not_running'])
    if busy: return dict(result,reasons=['workspace_or_series_busy'])
    if any(a['status'] == 'pending' for a in approvals): return dict(result,reasons=['pending_decision'])
    by_id = {r['id']:r for r in records}; cache = {}; waiting = []
    queued = [t for t in tasks if t['status'] == 'queued' or t['id'] == running_task_id]
    for task in queued:
        if task.get('role') == 'planner': continue
        reasons = []
        if (task.get('plan_revision') != job['plan_revision'] or task.get('requirement_revision') != job['requirement_revision']
                or task.get('spec_source_approval_id') != plan['id']): reasons.append('task_plan_not_current')
        if task.get('recovery_check_required'): reasons.append('recovery_confirmation_required')
        if task.get('retry_at') and time.time() < task['retry_at']: reasons.append('retry_wait')
        wp = by_id.get(task.get('work_package_id'))
        inputs, files, proofs = [], {}, {}
        try:
            if wp:
                index = task.get('plan_task_index')
                planned = plan['payload']['tasks']
                if task.get('workflow_authority'):
                    if not issued_task_valid(store,task,wp): raise ValueError('task_issued_authority_changed')
                else:
                    if task.get('workflow_generation',0) != wp.get('workflow_generation',0): raise ValueError('task_generation_changed')
                    if type(index) is not int or not 0 <= index < len(planned): raise ValueError('task_plan_spec_required')
                    spec = planned[index]
                    if (mapping.get(spec['wbs_id']) != wp['id'] or any(task.get(k) != spec[k] for k in ('title','role','goal_links'))
                            or not plan_instruction_matches(task,spec['instruction'])):
                        raise ValueError('task_plan_spec_changed')
                    prior = [t for t in tasks if t.get('plan_revision') == job['plan_revision']
                             and t.get('spec_source_approval_id') == plan['id'] and t.get('plan_task_index') == index-1]
                    expected_after = plan['task_id'] if index == 0 else prior[0]['id'] if len(prior) == 1 else None
                    if task.get('after') != expected_after or expected_after is None: raise ValueError('task_after_changed')
                if wp['execution_state'] not in ('planned','ready','running'): raise ValueError('work_package_not_executable')
                for label in specs[wp['wbs_id']]['depends_on']:
                    proof = feature_review_receipt(store,job,plan,requirements,by_id[mapping[label]],specs,records,cache)
                    proofs[mapping[label]] = {k:proof[k] for k in ('approval_id','target_digest')}
                    files.update(proof['files'])
                root = (task.get('workflow_authority') or {}).get('recheck_root_id')
                if root:
                    proof = feature_review_receipt(store,job,plan,requirements,by_id[root],specs,records,cache)
                    proofs[root] = {k:proof[k] for k in ('approval_id','target_digest')}; files.update(proof['files'])
                for ref in specs[wp['wbs_id']].get('input_artifacts', []):
                    current = dict(ref)
                    producers = [t for t in tasks if t.get('work_package_id') == wp['id'] and t.get('status') == 'succeeded'
                        and t.get('plan_revision') == job['plan_revision'] and ref['path'] in ((t.get('result') or {}).get('artifacts') or [])]
                    if producers:
                        matches = [r for r in producers[-1].get('workflow_artifact_records') or [] if r['path'] == ref['path']]
                        if len(matches) != 1: raise ValueError('input_artifact_registry_invalid')
                        current['sha256'] = matches[0]['sha256']
                    inputs.append(current)
            elif task.get('workflow_job_task') is True:
                for row in records:
                    proof = feature_review_receipt(store,job,plan,requirements,row,specs,records,cache)
                    proofs[row['id']] = {k:proof[k] for k in ('approval_id','target_digest')}; files.update(proof['files'])
            else: raise ValueError('task_work_package_authority_required')
            if task.get('after'):
                predecessor = store.get(task['after'],'task')
                if predecessor['job_id'] != job_id or predecessor['status'] != 'succeeded': reasons.append('after_not_succeeded')
        except (ValueError,KeyError,TypeError) as error:
            code = str(error)
            reasons.append(code if re.fullmatch('[a-z_]+',code) else 'dependency_feature_proof_invalid')
        if reasons:
            waiting.append({'task_id':task['id'],'reasons':sorted(set(reasons))}); continue
        descriptor = {'task_id':task['id'],'job_id':job_id,'work_package_id':wp['id'] if wp else None,
            'requirement_revision':job['requirement_revision'],'requirements_approval_id':job['requirements_approval_id'],
            'plan_revision':job['plan_revision'],'plan_approval_id':plan['id'],'plan_digest':plan['digest'],
            'task_spec_digest':digest({k:task.get(k) for k in ('title','instruction','role','profile','goal_links','after')}),
            'dependency_targets':proofs,'resources':resources,'input_refs':inputs,'dependency_files':files}
        return dict(result,status='ready',candidate_task_id=task['id'],reasons=[],descriptor=descriptor,waiting=waiting)
    return dict(result,reasons=['no_ready_task'],waiting=waiting)
