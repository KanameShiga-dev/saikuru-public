"""Approved-plan work packages; definitions/dependencies stay in plan payload.

WBS labels are business identifiers. Work package object IDs, Task IDs, native
session IDs and attempt IDs remain separate namespaces. Nothing here dispatches
a worker, approves a plan or accepts a feature/deliverable.
"""
from copy import deepcopy
import hashlib
import json
import re

MAX_PACKAGES = 6  # Preserve the existing initial plan's 1..6 Task bound.
GUIDE = ('\n新方式の計画はwork_packagesとcheckpoint_pathsを含める。work_packagesは'
         'wbs_id,title,goal_links,done_when,depends_onを持つ。wbs_idは英字始まりの一意のラベルで、'
         'Task/session/attemptのIDではない。各tasks項目に既存ラベルのwbs_idを付け、'
         'goal_linksは承認対象の成果物・受入条件の文字列をそのまま参照する。'
         'done_whenはこの作業パッケージの完了条件で、本人の機能確認・正式受領とは別。'
         '各パッケージは成果物と受入条件の両方へ対応させ、全体の成果物・受入条件を漏らさない。'
         'input_artifactsは既に存在する同じ依頼の登録済み先行成果物のjob_id,path,sha256。'
         'これから作る成果物はここへ捏造せずdepends_onで示し、既存入力がなければ空配列。'
         '作業の単位は確認可能な成果と完了条件とし、トークン数だけで分割しない。'
         '依存はパッケージのラベルで示す。checkpoint_pathsは承認を求める具体的な相対ファイルだけ。'
         '質問時はtasks/work_packages/checkpoint_pathsを空配列にし、未承認の実行はしない。')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _texts(value, label, *, required=True):
    if (not isinstance(value, list) or len(value) > 20 or (required and not value)
            or any(not isinstance(item, str) or not item.strip() or item != item.strip()
                   or len(item) > 500 for item in value) or len(value) != len(set(value))):
        raise ValueError('work_package_invalid_' + label)
    return value


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
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 6:
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
    properties = {'wbs_id': {'type': 'string'}, 'title': {'type': 'string'},
                  'goal_links': deepcopy(strings), 'done_when': deepcopy(strings), 'depends_on': deepcopy(strings)}
    properties['input_artifacts'] = {'type': 'array', 'maxItems': 20, 'items': {'type': 'object',
        'properties': {name: {'type': 'string'} for name in ('job_id', 'path', 'sha256')},
        'required': ['job_id', 'path', 'sha256'], 'additionalProperties': False}}
    schema['properties']['work_packages'] = {'type': 'array', 'maxItems': MAX_PACKAGES,
        'items': {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}}
    from team_checkpoint import MAX_FILES
    schema['properties']['checkpoint_paths'] = {'type': 'array', 'maxItems': MAX_FILES, 'items': {'type': 'string'}}
    schema['required'] += ['work_packages', 'checkpoint_paths']
    task = schema['properties']['tasks']['items']
    task['properties']['wbs_id'] = {'type': 'string'}
    task['required'].append('wbs_id')
    return schema


def approved_source(job, approval, requirements):
    from team_p0_contract import workflow_version
    if (workflow_version(job) != 1 or job.get('status') not in ('awaiting_approval', 'running')
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
