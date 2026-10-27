"""Domain-checkpoint session binding, without execution or native-resume authority.

These pure helpers are not dispatch entry points. The caller must validate the
checkpoint files/current authority, stop old writers and use the normal scheduler
before reserving a fresh attempt. A digest is an integrity check, not permission.
"""
import copy
import hashlib
import json
import re


VERSION_KEYS = ('requirement_revision', 'requirements_approval_id',
                'plan_revision', 'plan_approval_id')
TASK_KEYS = ('id', 'job_id', 'title', 'instruction', 'role', 'profile', 'after',
             'work_package_id', 'goal_links')
IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,128}')
HEX = re.compile(r'[0-9a-f]{64}')


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _id(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError('session_binding_identifier')
    return value


def continuation_reference_digest(refs, target_id):
    """Ignore only derived revisions and the authorized target generation.

    Target instruction/profile/test/result hashes remain part of the proof.
    Handoff's Store-derived record watermark can advance when the new attempt
    is mirrored; independent decisions/facts/conflicts/skills remain bound.
    """
    value = copy.deepcopy(refs)
    value.pop('snapshot_revisions', None)
    value['handoff_observation'].pop('records_seq', None)
    target = value['evidence_refs']['recovery_context']['tasks'][target_id]
    target.pop('status', None)
    target.pop('attempt_id', None)
    return _digest(value)


def _profile(value):
    if (not isinstance(value, dict) or value.get('adapter') not in ('codex', 'claude', 'copilot')
            or not isinstance(value.get('model'), str)
            or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,128}', value['model'])
            or value.get('effort') not in ('low', 'medium')):
        raise ValueError('session_profile_invalid')
    # Keep every selected profile setting; do not choose another model/provider
    # or manufacture a default for a missing setting.
    return copy.deepcopy(value)


def checkpoint_session_binding(job, source, target, checkpoint, assessment):
    """Bind a validated candidate to selected settings, never authorize it.

    Source can be a successful boundary preceding a queued task, or a failed
    boundary needing an independently authorized retry. Retry preparation must
    not erase evidence or silently change instructions/profile to fit a binding.
    """
    for row in (job, source, target, checkpoint): _id(row.get('id'))
    old = source.get('current_attempt') or {}
    if (job.get('workflow_contract_version') != 1
            or job.get('status') not in ('running', 'interrupted', 'failed', 'blocked')
            or source.get('job_id') != job['id'] or target.get('job_id') != job['id']
            or checkpoint.get('job_id') != job['id'] or checkpoint.get('task_id') != source['id']
            or source.get('resume_checkpoint_id') != checkpoint['id']
            or checkpoint.get('state') != 'ready'
            or not isinstance(checkpoint.get('manifest_digest'), str)
            or not HEX.fullmatch(checkpoint['manifest_digest'])
            or old.get('status') not in ('closed', 'fenced')
            or old.get('id') != checkpoint.get('attempt_id')
            or source.get('status') not in ('succeeded', 'failed', 'blocked', 'interrupted')
            or target.get('status') not in ('queued', 'failed', 'blocked', 'interrupted')
            or target.get('recovery_check_required') or source.get('recovery_check_required')
            or source.get('work_package_id') or target.get('work_package_id')):
        raise ValueError('checkpoint_session_not_candidate')
    if (assessment.get('status') != 'candidate' or assessment.get('assessment_only') is not True
            or assessment.get('resume_allowed') is not False
            or assessment.get('checkpoint_id') != checkpoint['id']
            or assessment.get('reasons') != ['authorization_and_fresh_attempt_required']
            or target['id'] not in assessment.get('candidate_task_ids', [])
            or assessment.get('revalidate_task_ids')):
        raise ValueError('checkpoint_assessment_required')
    versions = {key: job.get(key) for key in VERSION_KEYS}
    if (any(checkpoint.get(key) != value for key, value in versions.items())
            or type(versions['requirement_revision']) is not int or versions['requirement_revision'] < 1
            or type(versions['plan_revision']) is not int or versions['plan_revision'] < 1):
        raise ValueError('session_approval_changed')
    _id(versions['requirements_approval_id']); _id(versions['plan_approval_id'])
    _id(old['id'])
    profile = _profile(target.get('profile'))
    body = {'schema_version': 1, 'mode': 'fresh_domain_checkpoint', 'job_id': job['id'],
            'source_task_id': source['id'], 'target_task_id': target['id'],
            'checkpoint_id': checkpoint['id'], 'checkpoint_digest': checkpoint['manifest_digest'],
            'source_attempt_id': old['id'],
            'target_attempt_before': (target.get('current_attempt') or {}).get('id'),
            'target_ordinal_before': target.get('attempt', 0),
            'task_digest': _digest({key: target.get(key) for key in TASK_KEYS}),
            'profile_digest': _digest(profile), 'approved_versions': versions,
            'native_resume': False, 'execution_permission': 'none'}
    if type(body['target_ordinal_before']) is not int or body['target_ordinal_before'] < 0:
        raise ValueError('session_attempt_invalid')
    if body['target_attempt_before'] is not None: _id(body['target_attempt_before'])
    return dict(body, binding_digest=_digest(body))


def validate_fresh_session(binding, job, task, manifest):
    """Validate a NEW already-authorized attempt against current domain context.

    Does not reserve, restore files, clear recovery flags, invoke a Provider or
    validate permissions/files for the caller. No native session identifier is
    accepted or returned. Store.assert_attempt and current checkpoint inspection
    remain mandatory at the integration boundary.
    """
    body = {key: value for key, value in binding.items() if key != 'binding_digest'}
    attempt = task.get('current_attempt') or {}
    versions = {key: job.get(key) for key in VERSION_KEYS}
    if (binding.get('binding_digest') != _digest(body)
            or binding.get('schema_version') != 1 or type(binding.get('schema_version')) is not int
            or binding.get('mode') != 'fresh_domain_checkpoint'
            or binding.get('native_resume') is not False or binding.get('execution_permission') != 'none'
            or job.get('workflow_contract_version') != 1 or job.get('status') != 'running'
            or binding.get('job_id') != job.get('id') or task.get('job_id') != job.get('id')
            or binding.get('target_task_id') != task.get('id') or task.get('status') != 'running'
            or task.get('recovery_check_required') or task.get('work_package_id')
            or binding.get('approved_versions') != versions or attempt.get('approved_versions') != versions
            or attempt.get('status') != 'running'
            or not attempt.get('id') or attempt.get('id') in
               (binding.get('source_attempt_id'), binding.get('target_attempt_before'))
            or type(task.get('attempt')) is not int
            or task['attempt'] != binding.get('target_ordinal_before', -2) + 1
            or attempt.get('ordinal') != task['attempt']
            or binding.get('task_digest') != _digest({key: task.get(key) for key in TASK_KEYS})
            or binding.get('profile_digest') != _digest(_profile(task.get('profile')))):
        raise ValueError('fresh_session_binding_changed')
    _id(attempt['id'])
    from team_context_pages import CONTEXT_FORMAT
    current = {key: value for key, value in manifest.items() if key != 'manifest_digest'}
    if (manifest.get('format') != CONTEXT_FORMAT or manifest.get('manifest_digest') != _digest(current)
            or manifest.get('job_id') != job['id'] or manifest.get('task_id') != task['id']
            or manifest.get('attempt_id') != attempt['id']
            or manifest.get('state_revisions') != {'job': job.get('state_revision'), 'task': task.get('state_revision')}
            or manifest.get('execution_permission') != 'none'):
        raise ValueError('fresh_session_context_changed')
    return {'mode': 'fresh_domain_checkpoint', 'native_resume': False,
            'checkpoint_id': binding['checkpoint_id'], 'checkpoint_digest': binding['checkpoint_digest'],
            'attempt_id': attempt['id'], 'manifest_digest': manifest['manifest_digest'],
            'execution_permission': 'none'}
