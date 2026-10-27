"""Approval-time change analysis, never a worker's permission to execute."""
import copy
import hashlib
import json

CONTRACT_FIELDS = ('primary_goal', 'deliverables', 'acceptance_criteria', 'non_goals', 'constraints')
VERSION_FIELDS = ('requirement_revision', 'requirements_approval_id', 'plan_revision', 'plan_approval_id')
REVISION_STATUSES = ('running', 'blocked', 'failed', 'awaiting_acceptance', 'accepted', 'accepted_with_pending_checks')
ACCEPTED_STATUSES = ('accepted', 'accepted_with_pending_checks')


def proposal_status(context):
    if context is None:
        return 'awaiting_approval'
    if (not isinstance(context, dict) or set(context) != {'origin', 'prior_status'}
            or context.get('origin') != 'authenticated_user_revision'
            or context.get('prior_status') not in REVISION_STATUSES):
        raise ValueError('workflow_revision_context_invalid')
    return context['prior_status'] if context['prior_status'] in ACCEPTED_STATUSES else 'awaiting_approval'


def approval_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def current_plan(store, job):
    if job['plan_revision'] == 0:
        if job['requirement_revision'] != 0:
            raise ValueError('workflow_revision_basis_invalid')
        return None
    rows = []
    for key in ('plan_approval_id', 'requirements_approval_id'):
        row = store.get(job[key], 'approval')
        if (row.get('job_id') != job['id'] or row.get('kind') != 'plan'
                or row.get('status') != 'approved' or not isinstance(row.get('payload'), dict)
                or not isinstance(row['payload'].get('goal_contract'), dict)
                or row.get('digest') != approval_digest(row['payload'])):
            raise ValueError('workflow_revision_basis_invalid')
        rows.append(row)
    from team_p0_contract import validate_plan
    validate_plan(job, rows[0]['payload'], check_current_inputs=False)
    if any(rows[0]['payload']['goal_contract'].get(k) != rows[1]['payload']['goal_contract'].get(k) for k in CONTRACT_FIELDS):
        raise ValueError('workflow_revision_requirements_mismatch')
    if any((job.get('goal_contract') or {}).get(k) != rows[1]['payload']['goal_contract'].get(k) for k in CONTRACT_FIELDS):
        raise ValueError('workflow_revision_requirements_mismatch')
    return rows[0]


def analysis(store, job, payload, context=None):
    proposal_status(context)
    from team_p0_contract import validate_plan
    from team_dlp import require_clean_input
    require_clean_input(json.dumps(payload, ensure_ascii=False), False, '改訂計画')
    validate_plan(job, payload)  # Current inputs are checked before offering consent.
    from team_code_test_contract import validate_plan as validate_tests, snapshot_files
    tests = validate_tests(payload.get('code_test_plan'))
    if tests['existing_tests']:
        snapshot_files(job['project'], tests['existing_tests'])
    old_approval = current_plan(store, job)
    old = old_approval['payload'] if old_approval else {}
    old_contract = old.get('goal_contract') or {}
    contract = payload['goal_contract']
    scope_changes = {k: {'before': copy.deepcopy(old_contract.get(k)), 'after': copy.deepcopy(contract[k])}
                     for k in CONTRACT_FIELDS if old_contract.get(k) != contract[k]}
    before = {s['wbs_id']: s for s in old.get('work_packages', [])}
    after = {s['wbs_id']: s for s in payload['work_packages']}
    def task_specs(value, label):
        return [t for t in value.get('tasks', []) if t.get('wbs_id') == label]
    direct = {label for label in before.keys() | after.keys()
              if before.get(label) != after.get(label) or task_specs(old, label) != task_specs(payload, label)}
    global_changes = {k: {'before': copy.deepcopy(old.get(k)), 'after': copy.deepcopy(payload.get(k))}
                      for k in ('checkpoint_paths', 'code_test_plan', 'security_review_required') if old.get(k) != payload.get(k)}
    if scope_changes or global_changes:
        direct |= before.keys() | after.keys()
    affected = set(direct)
    while True:
        grown = affected | {label for graph in (before, after) for label, spec in graph.items()
                            if set(spec['depends_on']) & affected}
        if grown == affected: break
        affected = grown
    return {'schema_version': 1, 'job_id': job['id'], 'expected_job_revision': job['state_revision'],
        'proposal_context': copy.deepcopy(context),
        'base_versions': {k: job.get(k) for k in VERSION_FIELDS},
        'base_plan_digest': old_approval['digest'] if old_approval else None,
        'proposal_digest': approval_digest(payload), 'requirements_changed': bool(scope_changes),
        'scope_changes': scope_changes, 'global_plan_changes': global_changes,
        'package_changes': [{'wbs_id': label, 'before': copy.deepcopy(before.get(label)), 'after': copy.deepcopy(after.get(label))}
                            for label in sorted(direct)],
        'task_changes': [{'wbs_id': label, 'before': copy.deepcopy(task_specs(old, label)),
                          'after': copy.deepcopy(task_specs(payload, label))}
                         for label in sorted(before.keys() | after.keys()) if task_specs(old, label) != task_specs(payload, label)],
        'affected_work_packages': sorted(affected),
        # No proof transfer is inferred from a similar label or old success.
        'reconfirmation_required': sorted(before.keys() | after.keys()),
        'input_versions': [{'wbs_id': label, 'input_artifacts': copy.deepcopy(spec.get('input_artifacts', []))}
                           for label, spec in after.items()],
        'permission_effect': 'no_provider_model_deployment_or_external_action_permission',
        'evidence_policy': 'retain_history_invalidate_current_reviews_no_implicit_transfer'}
