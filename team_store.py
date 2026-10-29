"""Durable task state. All state transitions are serialized in this process."""
import copy
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager, closing
from pathlib import Path

from team_p0_contract import workflow_version


class StaleAttempt(ValueError):
    """A dispatch generation or persistent state revision is no longer current."""


def uid():
    return uuid.uuid4().hex


def now():
    return time.time()


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.revision = 0
        self.depth = 0
        self.runtime_id = uid()
        self._worker = threading.local()
        self.db = sqlite3.connect(self.directory / 'team.sqlite3', check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS objects (kind TEXT, id TEXT PRIMARY KEY, body TEXT NOT NULL)')
        # Per-job lookups (for_job, transition reasons) read only that job's rows instead of decoding every body.
        self.db.execute("CREATE INDEX IF NOT EXISTS objects_job ON objects(kind, json_extract(body,'$.job_id'))")
        self.db.execute('CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS transitions (seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, id TEXT, job_id TEXT, from_status TEXT, to_status TEXT, at REAL, reason TEXT)')
        self.db.execute('CREATE INDEX IF NOT EXISTS transition_job ON transitions(job_id, seq)')
        self.db.execute('CREATE TABLE IF NOT EXISTS metrics_meta (key TEXT PRIMARY KEY, value REAL)')
        self.db.execute("INSERT OR IGNORE INTO metrics_meta VALUES ('started_at', ?)", (now(),))
        self.db.commit()

    def put(self, kind, obj, transition_reason=None):
        with self.lock:
            if getattr(self._worker, 'task', None) is not None and self.depth == 0:
                with self.atomic():
                    return self.put(kind, obj, transition_reason)
            self._check_worker()
            obj = copy.deepcopy(obj)
            self._workflow_storage(kind, obj)
            self.revision += 1
            obj['updated_at'] = now()
            body = json.dumps(obj, ensure_ascii=False)
            if kind in ('job', 'task'):
                previous = self.db.execute('SELECT body FROM objects WHERE id=?', (obj['id'],)).fetchone()
                if kind == 'job' and previous:
                    original = json.loads(previous[0])
                    if original.get('p0_contract_version') == 1:
                        if (obj.get('goal') != original.get('goal')
                                or (obj.get('goal_contract') or {}).get('original_request') != original.get('goal')):
                            raise ValueError('元の依頼は変更できません。要求変更は利用者の承認が必要です。')
                        publishing = (getattr(self._worker, 'workflow_plan_commit', None)
                                      == obj.get('plan_approval_id') and obj.get('plan_approval_id') is not None)
                        protected_workflow = workflow_version(original) == 1 and original.get('requirement_revision', 0) > 0
                        if (protected_workflow or original.get('status') not in ('planning', 'awaiting_approval')) and not publishing:
                            fixed = ('primary_goal', 'deliverables', 'acceptance_criteria',
                                     'non_goals', 'constraints')
                            previous_contract = original.get('goal_contract') or {}
                            next_contract = obj.get('goal_contract') or {}
                            if any(next_contract.get(key) != previous_contract.get(key) for key in fixed):
                                raise ValueError('承認済みの達成条件は変更できません。要求変更には利用者の承認が必要です。')
                old = json.loads(previous[0]).get('status') if previous else None
                status = obj.get('status')
                if old != status:
                    job_id = obj['id'] if kind == 'job' else obj['job_id']
                    reason = str(transition_reason)[:80] if transition_reason else ('system' if status == 'interrupted' else 'other')
                    if kind == 'job' and status == 'failed':
                        reason = 'task_error'
                    if reason != 'system':
                        pending = self.db.execute("SELECT body FROM objects WHERE kind='approval' AND json_extract(body,'$.job_id')=? AND json_extract(body,'$.status')='pending' ORDER BY rowid DESC", (job_id,)).fetchall()
                        for (raw,) in pending:
                            approval = json.loads(raw)
                            if kind == 'job' or approval.get('task_id') == obj['id']:
                                reason = approval.get('kind', 'other'); break
                        if reason == 'other' and kind == 'job':
                            reason = {'awaiting_approval':'plan', 'awaiting_acceptance':'completion'}.get(status, 'other')
                        if reason == 'other':
                            event = self.db.execute("SELECT body FROM events WHERE json_extract(body,'$.job_id')=? ORDER BY seq DESC LIMIT 1", (job_id,)).fetchone()
                            if event:
                                reason = json.loads(event[0]).get('type', 'other')
                        if kind == 'job' and status == 'blocked' and reason not in {'repair_limit','task_error'}:
                            tasks = self.db.execute("SELECT body FROM objects WHERE kind='task' AND json_extract(body,'$.job_id')=? ORDER BY rowid DESC", (job_id,)).fetchall()
                            latest = max((json.loads(raw) for (raw,) in tasks), key=lambda t:t.get('updated_at',0), default={})
                            report = latest.get('result') or {}
                            if latest.get('status')=='blocked' and (report.get('questions') or report.get('question')):
                                reason = 'question'
                            elif report.get('status')=='needs_changes':
                                reason = 'repair_limit'
                            elif latest.get('status')=='failed':
                                reason = 'task_error'
                    self.db.execute('INSERT INTO transitions(kind,id,job_id,from_status,to_status,at,reason) VALUES (?,?,?,?,?,?,?)',
                                    (kind, obj['id'], job_id, old, status, obj['updated_at'], reason))
            self.db.execute('INSERT INTO objects VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',
                            (kind, obj['id'], body))
            if self.depth == 0:
                self.db.commit()
            return obj

    def publish_workflow_plan(self, job_id, approval_id, expected_revision):
        """Publish consented versions only, outside any worker attempt scope."""
        if getattr(self._worker, 'task', None) is not None:
            raise ValueError('worker_cannot_publish_workflow_plan')
        with self.atomic():
            from team_plan_revision import approval_digest, VERSION_FIELDS, CONTRACT_FIELDS, proposal_status
            from team_p0_contract import validate_plan
            job = self.get(job_id, 'job')
            approval = self.get(approval_id, 'approval')
            target = approval.get('workflow_revision') or {}
            if (workflow_version(job) != 1 or job['status'] != proposal_status(approval.get('workflow_revision_context'))
                    or type(expected_revision) is not int or job['state_revision'] != expected_revision
                    or target.get('expected_job_revision') != expected_revision
                    or target.get('proposal_context') != approval.get('workflow_revision_context')
                    or target.get('base_versions') != {k: job.get(k) for k in VERSION_FIELDS}
                    or approval.get('job_id') != job_id or approval.get('kind') != 'plan'
                    or approval.get('status') != 'approved'
                    or approval.get('digest') != approval_digest(approval['payload'])
                    or approval.get('workflow_confirmation') != approval_digest(target)):
                raise ValueError('workflow_revision_consent_not_current')
            contract = validate_plan(job, approval['payload'])
            changed = any((job.get('goal_contract') or {}).get(k) != contract[k] for k in CONTRACT_FIELDS)
            previous = getattr(self._worker, 'workflow_plan_commit', None)
            self._worker.workflow_plan_commit = approval_id
            try:
                return self.update(job_id, goal_contract=contract, status='awaiting_approval',
                    requirement_revision=job['requirement_revision'] + int(changed or job['requirement_revision'] == 0),
                    requirements_approval_id=approval_id if changed or job['requirement_revision'] == 0 else job['requirements_approval_id'],
                    plan_revision=job['plan_revision'] + 1, plan_approval_id=approval_id,
                    validation_evidence=None)
            finally:
                self._worker.workflow_plan_commit = previous

    def _workflow_storage(self, kind, obj):
        """Additive storage checks; dispatch/fenced callbacks arrive in W02.02.

        No legacy object is stamped or converted. Checkpoints are drafts only
        until the dedicated ready/pointer transaction is implemented.
        """
        if not isinstance(obj.get('id'), str) or not obj['id'].strip():
            raise ValueError('保存IDが必要です。')
        row = self.db.execute('SELECT kind, body FROM objects WHERE id=?', (obj['id'],)).fetchone()
        if row and row[0] != kind:
            raise ValueError('保存IDは別の種類で使用されています。')
        previous = json.loads(row[1]) if row else None
        if obj['id'].startswith('native-reconciliation-') and kind != 'native_effect_reconciliation':
            raise ValueError('native_reconciliation_id_reserved')
        if kind == 'native_effect_reconciliation':
            if previous or getattr(self._worker, 'native_reconciliation_commit', None) != obj['id']:
                raise ValueError('native_reconciliation_requires_server_commit')
            return
        if obj['id'].startswith('native-baseline-') and kind != 'native_scope_baseline':
            raise ValueError('native_baseline_id_reserved')
        if kind == 'native_scope_baseline':
            if previous or getattr(self._worker, 'native_baseline_commit', None) != obj['id']:
                raise ValueError('native_baseline_requires_immutable_server_commit')
            return
        if kind == 'job':
            version = workflow_version(obj)
            if previous and previous.get('workflow_contract_version') != obj.get('workflow_contract_version'):
                raise ValueError('既存依頼の作業フロー版は変更できません。')
            if version is None:
                return
            if previous and getattr(self._worker, 'task', None) is not None and any(
                    obj.get(key) != previous.get(key) for key in
                    ('requirement_revision', 'requirements_approval_id', 'plan_revision', 'plan_approval_id')):
                raise ValueError('worker_cannot_publish_workflow_plan')
            for revision_key, approval_key in (
                    ('requirement_revision', 'requirements_approval_id'), ('plan_revision', 'plan_approval_id')):
                revision = obj.get(revision_key)
                if type(revision) is not int or revision < 0:
                    raise ValueError('要求・計画の版が不正です。')
                approval_id = obj.get(approval_key)
                if revision == 0:
                    if approval_id is not None:
                        raise ValueError('未承認版に承認参照は付けられません。')
                else:
                    if not isinstance(approval_id, str) or not approval_id.strip():
                        raise ValueError('承認参照が必要です。')
                    approval = self.get(approval_id, 'approval')
                    if (approval.get('job_id') != obj['id'] or approval.get('kind') != 'plan'
                            or approval.get('status') != 'approved'):
                        raise ValueError('現行依頼の承認済み計画を参照してください。')
                if previous and revision < previous[revision_key]:
                    raise ValueError('要求・計画の版は戻せません。')
        elif kind in ('task', 'work_package', 'checkpoint'):
            if not isinstance(obj.get('job_id'), str) or not obj['job_id'].strip():
                raise ValueError('保存対象の依頼IDが必要です。')
            tracked = 'state_revision' in obj or (previous is not None and 'state_revision' in previous)
            if previous and tracked and previous.get('job_id') != obj['job_id']:
                raise ValueError('保存済み記録の依頼は変更できません。')
            try:
                job = self.get(obj['job_id'], 'job')
            except ValueError:
                # Legacy task-only fixtures/records were accepted before this
                # extension. Missing parents must not enroll or block them.
                if kind == 'task' and obj.get('work_package_id') is None and not tracked:
                    return
                raise ValueError('新しい作業フローの記録には依頼が必要です。') from None
            if workflow_version(job) is None:
                if kind != 'task' or obj.get('work_package_id') is not None or tracked:
                    raise ValueError('旧依頼には新しい作業フローの記録を追加できません。')
                return
            if kind != 'task':
                if type(obj.get('schema_version')) is not int or obj['schema_version'] != 1:
                    raise ValueError('未対応の保存形式です。')
            if kind == 'work_package':
                if 'task_ids' in obj or 'depends_on' in obj:
                    raise ValueError('所属と依存は承認済み計画・実行Taskから参照してください。')
                if (type(obj.get('plan_revision')) is not int or obj['plan_revision'] < 1
                        or obj['plan_revision'] != job['plan_revision']
                        or obj.get('spec_source_approval_id') != job['plan_approval_id']):
                    raise ValueError('作業パッケージの計画版が不正です。')
                if (obj.get('execution_state') not in ('planned', 'ready', 'running', 'succeeded', 'blocked', 'cancelled')
                        or obj.get('review_status') not in ('not_requested', 'pending', 'approved', 'changes_requested', 'invalidated')):
                    raise ValueError('作業パッケージの状態が不正です。')
            if kind in ('task', 'checkpoint') and obj.get('work_package_id') is not None:
                wp = self.get(obj['work_package_id'], 'work_package')
                if wp.get('job_id') != job['id']:
                    raise ValueError('別の依頼の作業パッケージは参照できません。')
            if kind == 'checkpoint':
                if not isinstance(obj.get('task_id'), str) or not obj['task_id'].strip():
                    raise ValueError('Checkpointの担当IDが必要です。')
                task = self.get(obj['task_id'], 'task')
                if (task.get('job_id') != job['id']
                        or obj.get('work_package_id') != task.get('work_package_id')):
                    raise ValueError('Checkpointの担当・所属が不正です。')
                publishing = getattr(self._worker, 'checkpoint_commit', None) == obj['id']
                if previous and previous.get('state') == 'ready':
                    raise ValueError('確定Checkpointは変更できません。')
                if obj.get('state') not in (('writing', 'invalid', 'ready') if publishing else ('writing', 'invalid')):
                    raise ValueError('Checkpoint確定は専用の保存手続きが必要です。')
                if previous and previous['state'] == 'invalid' and obj['state'] != 'invalid':
                    raise ValueError('無効なCheckpointは再利用できません。')
            if (kind == 'task' and obj.get('resume_checkpoint_id') != (previous or {}).get('resume_checkpoint_id')
                    and (not getattr(self._worker, 'checkpoint_commit', None)
                         or self._worker.checkpoint_commit != obj.get('resume_checkpoint_id'))):
                raise ValueError('Checkpoint参照は専用の確定手続きで設定してください。')
            if kind == 'task':
                recovery_writer = getattr(self._worker, 'recovery_commit', None) == obj['id']
                if (previous and previous.get('recovery_check_required') and not obj.get('recovery_check_required')
                        and not recovery_writer):
                    raise ValueError('停止確認は専用の保守手続きで記録してください。')
                if (obj.get('recovery_confirmation') is not None
                        and obj.get('recovery_confirmation') != (previous or {}).get('recovery_confirmation')
                        and not recovery_writer):
                    raise ValueError('保守確認の記録は直接作成できません。')
            if previous and (obj['job_id'] != previous['job_id']
                             or obj.get('work_package_id') != previous.get('work_package_id')):
                raise ValueError('保存済み記録の所属は変更できません。')
        else:
            return
        if previous:
            if type(obj.get('state_revision')) is not int or obj['state_revision'] != previous['state_revision']:
                raise ValueError('状態の版が古くなりました。再読み込みしてください。')
            obj['state_revision'] = previous['state_revision'] + 1
        else:
            if 'state_revision' in obj:
                raise ValueError('初回保存の状態版はサーバーが設定します。')
            obj['state_revision'] = 1

    def get(self, object_id, kind=None):
        with self.lock:
            row = self.db.execute('SELECT kind, body FROM objects WHERE id=?', (object_id,)).fetchone()
            if not row or (kind and row[0] != kind):
                raise ValueError('対象が見つかりません。')
            return json.loads(row[1])

    def native_checkpoint_requires_transition(self, task):
        """A confirmed native attempt cannot use ordinary checkpoint publication."""
        marker = task.get('recovery_confirmation')
        if not isinstance(marker, dict) or not marker:
            return False
        attempt_id = (task.get('current_attempt') or {}).get('id')
        return ('native_reconciliation' in marker or bool(task.get('agent_run') or task.get('agent_run_id'))
            or any(run.get('task_id') == task['id'] or (attempt_id and run.get('attempt_id') == attempt_id)
                   for run in self.for_job('agent_run', task['job_id'])))

    def commit_checkpoint(self, record, references):
        """Internal publisher: caller verifies files outside Store lock first.

        No permission or filesystem validation is inferred from this method.
        Its transaction only binds the exact observed Store revisions/event.
        """
        from team_checkpoint import validate_draft_record
        with self.atomic():
            current = self.get(record['id'], 'checkpoint')
            if current != record:
                raise StaleAttempt('Checkpointの下書きが変化しました。')
            task = self.get(record['task_id'], 'task')
            job = self.get(record['job_id'], 'job')
            if self.native_checkpoint_requires_transition(task):
                raise ValueError('native_checkpoint_transition_required')
            validate_draft_record(current, job, task, references)
            if current['resume_preconditions'] != ['checkpoint_not_ready']:
                raise ValueError('未解決の条件があるCheckpointは確定できません。')
            revisions = references['snapshot_revisions']
            wp = self.get(task['work_package_id'], 'work_package') if task.get('work_package_id') else None
            actual = {'job': job['state_revision'], 'task': task['state_revision'],
                      'work_package': wp['state_revision'] if wp else None}
            if actual != revisions:
                raise StaleAttempt('Checkpointの対象版が変化しました。')
            if (task.get('current_attempt', {}).get('status') not in ('closed', 'fenced')
                    or task.get('recovery_check_required')
                    or job.get('status') not in ('planning', 'running', 'blocked', 'failed', 'interrupted', 'awaiting_acceptance')):
                raise StaleAttempt('Checkpointを確定できる状態ではありません。')
            previous = getattr(self._worker, 'checkpoint_commit', None)
            self._worker.checkpoint_commit = record['id']
            try:
                task = self.update(task['id'], resume_checkpoint_id=record['id'])
                binding = dict(actual, task=task['state_revision'])
                seq = self.event('checkpoint_ready', 'Checkpointの保存を確定しました。', task['id'], job['id'],
                    details={'checkpoint_id': record['id'], 'manifest_digest': record['manifest_digest'],
                             'revisions': binding})
                return self.put('checkpoint', dict(current, state='ready', ready_binding=dict(binding, event_seq=seq)))
            finally:
                self._worker.checkpoint_commit = previous

    def checkpoint_binding(self, checkpoint_id):
        """Persistence candidate only: does not verify files or authorize resume."""
        from team_checkpoint import DRAFT_FIELDS, checkpoint_digest
        with self.lock:
            record = self.get(checkpoint_id, 'checkpoint')
            binding = record.get('ready_binding')
            if (set(record) != DRAFT_FIELDS or type(record.get('schema_version')) is not int
                    or record['schema_version'] != 1 or record.get('state') != 'ready'
                    or record.get('manifest_digest') != checkpoint_digest(record)
                    or not isinstance(binding, dict) or set(binding) != {'job', 'task', 'work_package', 'event_seq'}
                    or any(type(binding[key]) is not int or binding[key] < 1 for key in ('job', 'task', 'event_seq'))
                    or (binding['work_package'] is not None and (type(binding['work_package']) is not int or binding['work_package'] < 1))):
                raise ValueError('Checkpointの確定記録が不正です。')
            task = self.get(record['task_id'], 'task')
            job = self.get(record['job_id'], 'job')
            wp = self.get(task['work_package_id'], 'work_package') if task.get('work_package_id') else None
            revisions = {'job': job['state_revision'], 'task': task['state_revision'],
                         'work_package': wp['state_revision'] if wp else None}
            if (workflow_version(job) != 1 or task['job_id'] != job['id']
                    or task.get('resume_checkpoint_id') != checkpoint_id
                    or task.get('work_package_id') != record.get('work_package_id')
                    or (task.get('current_attempt') or {}).get('id') != record['attempt_id']
                    or any(binding[key] != value for key, value in revisions.items())):
                raise ValueError('Checkpointの確定対象が変化しています。')
            row = self.db.execute('SELECT body FROM events WHERE seq=?', (binding['event_seq'],)).fetchone()
            event = json.loads(row[0]) if row else {}
            if (event.get('type') != 'checkpoint_ready' or event.get('job_id') != job['id']
                    or event.get('task_id') != task['id'] or event.get('details') != {
                        'checkpoint_id': checkpoint_id, 'manifest_digest': record['manifest_digest'], 'revisions': revisions}):
                raise ValueError('Checkpointの確定イベントが一致しません。')
            return record, dict(event, seq=binding['event_seq'])

    def invalidate_checkpoint(self, checkpoint_id):
        with self.atomic():
            record = self.get(checkpoint_id, 'checkpoint')
            if record.get('state') == 'writing':
                self.update(checkpoint_id, state='invalid')
                self.event('checkpoint_invalid', 'Checkpointの参照が変化したため確定しません。',
                           record['task_id'], record['job_id'], details={'checkpoint_id': checkpoint_id})

    def has_operation_history(self, job_id):
        """Unknown/untyped operations are not proof of no effect, even on exit 0."""
        with self.lock:
            return bool(self.db.execute("SELECT 1 FROM events WHERE json_extract(body,'$.job_id')=? "
                "AND json_extract(body,'$.type') GLOB 'operation_*' LIMIT 1", (job_id,)).fetchone())

    def operation_assessment(self, job_id):
        """Read-only protocol pairing; observed results are never resume permission."""
        with self.lock:
            self.get(job_id, 'job')
            rows = [(seq, json.loads(body)) for seq, body in self.db.execute(
                "SELECT seq,body FROM events WHERE json_extract(body,'$.job_id')=? ORDER BY seq", (job_id,))]
            attempts = {}
            for seq, event in rows:
                if event.get('type') == 'attempt_started':
                    key = (event.get('task_id'), (event.get('details') or {}).get('attempt_id'))
                    attempts.setdefault(key, []).append(seq)
            groups, unresolved = {}, []
            operations = [(seq, e) for seq, e in rows if str(e.get('type', '')).startswith('operation_')]
            for seq, event in operations:
                details = event.get('details') or {}
                task_id, attempt_id = event.get('task_id'), details.get('attempt_id')
                operation_id = details.get('operation_id')
                valid = (event.get('type') in ('operation_bash', 'operation_result')
                         and details.get('correlated') is True and isinstance(attempt_id, str)
                         and isinstance(operation_id, str) and len(operation_id) == 64
                         and all(c in '0123456789abcdef' for c in operation_id)
                         and len(attempts.get((task_id, attempt_id), [])) == 1
                         and attempts[(task_id, attempt_id)][0] < seq)
                if valid:
                    try:
                        task = self.get(task_id, 'task')
                        valid = task.get('job_id') == job_id and details.get('work_package_id') == task.get('work_package_id')
                    except (ValueError, KeyError, TypeError):
                        valid = False
                if not valid:
                    unresolved.append({'event_seq': seq, 'code': 'operation_identity_unresolved'})
                    continue
                groups.setdefault((task_id, attempt_id, operation_id), []).append((seq, event))
            observed = []
            for (task_id, attempt_id, operation_id), items in groups.items():
                starts = [(seq, e) for seq, e in items if e['type'] == 'operation_bash']
                results = [(seq, e) for seq, e in items if e['type'] == 'operation_result']
                code = None
                if len(starts) != 1 or len(results) != 1:
                    code = 'operation_pair_incomplete_or_duplicate'
                elif starts[0][0] >= results[0][0]:
                    code = 'operation_order_invalid'
                elif (not isinstance(starts[0][1]['details'].get('command_digest'), str)
                      or len(starts[0][1]['details']['command_digest']) != 64
                      or starts[0][1]['details']['command_digest'] != results[0][1]['details'].get('command_digest')):
                    code = 'operation_command_changed'
                elif (type(results[0][1]['details'].get('exit_code')) is not int
                      or results[0][1]['details'].get('timed_out') is not False):
                    code = 'operation_result_uncertain'
                item = {'task_id': task_id, 'attempt_id': attempt_id, 'operation_id': operation_id,
                        'event_seqs': [seq for seq, _ in items]}
                if code:
                    unresolved.append(dict(item, code=code))
                else:
                    observed.append(dict(item, status='result_observed'))
            signature = hashlib.sha256(json.dumps(operations, sort_keys=True, ensure_ascii=False,
                                                  separators=(',', ':')).encode()).hexdigest()
            return {'operation_digest': signature, 'result_observed': observed, 'unresolved': unresolved,
                    'execution_permission': 'none', 'effects_reconciled': False}

    def confirm_recovery(self, task_id, task_revision, job_revision, attempt_id, *, prior_operation_check=None):
        """Internal CAS for authenticated maintenance service; never queues work."""
        if getattr(self._worker, 'task', None) is not None:
            raise ValueError('worker_cannot_confirm_recovery')
        with self.atomic():
            task = self.get(task_id, 'task')
            job = self.get(task['job_id'], 'job')
            attempt = task.get('current_attempt') or {}
            if (workflow_version(job) != 1 or type(task_revision) is not int or type(job_revision) is not int
                    or task.get('state_revision') != task_revision or job.get('state_revision') != job_revision
                    or job.get('status') not in ('failed', 'blocked', 'interrupted')
                    or task.get('status') not in ('failed', 'blocked', 'interrupted')
                    or not isinstance(attempt_id, str) or not attempt_id or attempt.get('id') != attempt_id
                    or attempt.get('status') not in ('closed', 'fenced')):
                raise ValueError('現在の停止状態・試行・操作の確認が必要です。')
            operations = {'operations': 'none'}
            if self.has_operation_history(job['id']):
                if not callable(prior_operation_check):
                    raise ValueError('操作の作用が未照合です。この経路では再開できません。')
                proof = prior_operation_check()
                if (not isinstance(proof, dict) or proof.get('unresolved')
                        or not proof.get('reviewed_prior_operations')
                        or len(proof['reviewed_prior_operations']) != len(proof.get('result_observed') or [])):
                    raise ValueError('操作の作用が未照合です。この経路では再開できません。')
                operations = {'operations': 'reviewed_prior', 'operation_digest': proof['operation_digest']}
            native_started = bool(task.get('agent_run') or task.get('agent_run_id') or any(
                run.get('task_id') == task_id or run.get('attempt_id') == attempt_id
                for run in self.for_job('agent_run', job['id'])))
            native = getattr(self._worker, 'native_confirmation_commit', None)
            if native_started and (not isinstance(native, dict) or native.get('source_target', {}).get('task_id') != task_id
                    or native.get('source_target', {}).get('attempt_id') != attempt_id):
                raise ValueError('native_confirmation_requires_verified_reconciliation')
            marker = {'attempt_id': attempt_id, 'job_revision': job_revision,
                      'task_revision': task_revision + 1}
            if native_started:
                marker['native_reconciliation'] = copy.deepcopy(native)
                operations = dict(operations, native_reconciliation=copy.deepcopy(native))
            marker['event_seq'] = self.event('recovery_confirmed', '保守用経路で作業停止の確認を記録しました。',
                task_id, job['id'], details={'attempt_id': attempt_id, 'task_revision': task_revision,
                                           'job_revision': job_revision, **operations})
            previous = getattr(self._worker, 'recovery_commit', None)
            self._worker.recovery_commit = task_id
            try:
                return self.update(task_id, recovery_check_required=False, recovery_confirmation=marker)
            finally:
                self._worker.recovery_commit = previous

    def confirm_native_recovery(self, task_id, task_revision, job_revision, attempt_id, target,
                                database_stamp, assert_process, prior_operation_proof=None):
        """Server-only native confirmation transition; no retry/session permission."""
        if getattr(self._worker, 'task', None) is not None:
            raise ValueError('worker_cannot_confirm_native_recovery')
        with self.atomic():
            if (self.db.total_changes, self.db.execute('PRAGMA data_version').fetchone()[0]) != database_stamp:
                raise StaleAttempt('workflow_native_confirmation_changed')
            assert_process()  # Metadata only; Engine performed file observations outside this txn.
            task = self.get(task_id, 'task'); job = self.get(task['job_id'], 'job')
            if (not isinstance(target, dict) or target.get('task_id') != task_id
                    or type(target.get('contract_version')) is not int or target.get('contract_version') != 1
                    or any(type(target.get(k)) is not int for k in ('task_revision', 'job_revision'))
                    or target.get('job_id') != job['id'] or target.get('attempt_id') != attempt_id
                    or target.get('task_revision') != task_revision or target.get('job_revision') != job_revision
                    or task.get('state_revision') != task_revision or job.get('state_revision') != job_revision
                    or task.get('recovery_confirmation') is not None):
                raise StaleAttempt('workflow_native_confirmation_changed')
            record = self.native_reconciliation(task_id, target)
            if record is None: raise ValueError('workflow_native_reconciliation_required')
            fields = ('state_revision', 'updated_at', 'recovery_check_required', 'recovery_confirmation')
            proof = {'record_id': record['id'], 'record_digest': record['record_digest'],
                'record_event_seq': record['event_seq'], 'source_target': copy.deepcopy(target),
                'source_fields': {key: {'present': key in task, 'value': copy.deepcopy(task.get(key))} for key in fields},
                'task_invariants_digest': hashlib.sha256(json.dumps({k:v for k,v in task.items() if k not in fields},
                    sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()}
            previous = getattr(self._worker, 'native_confirmation_commit', None)
            self._worker.native_confirmation_commit = proof
            try:
                return self.confirm_recovery(task_id, task_revision, job_revision, attempt_id,
                    prior_operation_check=lambda: prior_operation_proof)
            finally:
                self._worker.native_confirmation_commit = previous

    def native_confirmation_basis(self, task_id):
        """Read the exact confirmed source projection; never execution permission."""
        if getattr(self._worker, 'task', None) is not None:
            raise ValueError('worker_cannot_read_native_confirmation_basis')
        with self.lock:
            task = self.get(task_id, 'task'); job = self.get(task['job_id'], 'job')
            marker = task.get('recovery_confirmation'); attempt = task.get('current_attempt') or {}
            if (not isinstance(attempt, dict) or not isinstance(marker, dict) or set(marker) != {
                    'attempt_id', 'job_revision', 'task_revision', 'event_seq', 'native_reconciliation'}
                    or any(type(marker.get(k)) is not int or marker[k] < 1
                        for k in ('job_revision', 'task_revision', 'event_seq'))
                    or marker['attempt_id'] != attempt.get('id')
                    or type(job.get('state_revision')) is not int or type(task.get('state_revision')) is not int
                    or marker['job_revision'] != job.get('state_revision')
                    or marker['task_revision'] != task.get('state_revision')
                    or task.get('recovery_check_required') is not False
                    or attempt.get('status') not in ('closed', 'fenced')
                    or task.get('status') not in ('failed', 'blocked', 'interrupted')
                    or job.get('status') not in ('failed', 'blocked', 'interrupted')):
                raise ValueError('native_confirmation_transition_invalid')
            proof = marker['native_reconciliation']
            fields = {'state_revision', 'updated_at', 'recovery_check_required', 'recovery_confirmation'}
            if (not isinstance(proof, dict) or set(proof) != {'record_id', 'record_digest', 'record_event_seq',
                    'source_target', 'source_fields', 'task_invariants_digest'}
                    or not isinstance(proof['source_fields'], dict) or set(proof['source_fields']) != fields
                    or any(not isinstance(v, dict) or set(v) != {'present', 'value'} or type(v['present']) is not bool
                        for v in proof['source_fields'].values())):
                raise ValueError('native_confirmation_projection_invalid')
            target = proof['source_target']
            expected = {'contract_version': 1, 'action': 'reconcile_recovery', 'job_id': job['id'],
                'task_id': task_id, 'attempt_id': attempt.get('id'), 'job_revision': job['state_revision'],
                'task_revision': marker['task_revision'] - 1}
            if (not isinstance(target, dict) or set(target) != set(expected) | {'state_digest'}
                    or any(target.get(k) != v for k, v in expected.items())
                    or any(type(target.get(k)) is not int for k in ('contract_version', 'job_revision', 'task_revision'))):
                raise ValueError('native_confirmation_source_target_invalid')
            invariants = {k:v for k,v in task.items() if k not in fields}
            digest = hashlib.sha256(json.dumps(invariants, sort_keys=True, ensure_ascii=False,
                separators=(',', ':')).encode()).hexdigest()
            if proof['task_invariants_digest'] != digest:
                raise ValueError('native_confirmation_invariants_changed')
            source = copy.deepcopy(task)
            for key, previous in proof['source_fields'].items():
                if previous['present']: source[key] = copy.deepcopy(previous['value'])
                else: source.pop(key, None)
            if (type(source.get('state_revision')) is not int or source['state_revision'] != target['task_revision']
                    or source.get('recovery_confirmation') is not None):
                raise ValueError('native_confirmation_projection_invalid')
            record = self.native_reconciliation(task_id, target)
            if (record is None or record['id'] != proof['record_id'] or record['record_digest'] != proof['record_digest']
                    or type(proof['record_event_seq']) is not int or record['event_seq'] != proof['record_event_seq']
                    or record['event_seq'] >= marker['event_seq']):
                raise ValueError('native_confirmation_reconciliation_invalid')
            operations = {'operations': 'none'}
            if self.has_operation_history(job['id']):
                operations = {'operations': 'reviewed_prior',
                    'operation_digest': self.operation_assessment(job['id'])['operation_digest']}
            rows = self.db.execute("SELECT seq,body FROM events WHERE json_extract(body,'$.type')='recovery_confirmed' "
                "AND json_extract(body,'$.job_id')=? AND json_extract(body,'$.task_id')=? "
                "AND json_extract(body,'$.details.attempt_id')=? ORDER BY seq LIMIT 2",
                (job['id'], task_id, marker['attempt_id'])).fetchall()
            expected_details = {'attempt_id': marker['attempt_id'], 'task_revision': target['task_revision'],
                'job_revision': marker['job_revision'], **operations, 'native_reconciliation': proof}
            if (len(rows) != 1 or rows[0][0] != marker['event_seq']
                    or json.loads(rows[0][1]).get('details') != expected_details):
                raise ValueError('native_confirmation_event_invalid')
            return {'source_task': source, 'source_target': copy.deepcopy(target),
                'confirmation_event_seq': marker['event_seq'], 'reconciliation_id': record['id']}

    def commit_native_scope_baseline(self, task_id, expected_task_revision, expected_job_revision, snapshot):
        """Server-only immutable primitive; the engine supplies pre-provider observations.

        This validates durable authority without filesystem IO or granting any
        effect-reconciliation/recovery permission. It is not a worker API.
        """
        if getattr(self._worker, 'task', None) is not None:
            raise ValueError('worker_cannot_commit_native_baseline')
        from team_p0_contract import checkpoint_scope
        from team_harness import workspace_identity
        with self.atomic():
            task = self.get(task_id, 'task'); job = self.get(task['job_id'], 'job')
            self.assert_attempt(task)
            if (task.get('role') != 'builder' or not task.get('work_package_id')
                    or task.get('state_revision') != expected_task_revision
                    or job.get('state_revision') != expected_job_revision
                    or type(expected_task_revision) is not int or type(expected_job_revision) is not int):
                raise StaleAttempt('native_baseline_authority_changed')
            attempt_id = task['current_attempt']['id']
            plan = self.get(job.get('plan_approval_id'), 'approval')
            paths = checkpoint_scope(job, plan)
            boundary = (plan.get('workflow_revision') or {}).get('workspace_boundary')
            if not isinstance(boundary, dict) or boundary.get('status') != 'ready':
                raise ValueError('native_baseline_workspace_binding_required')
            expected = {'contract_version': 1, 'basis': 'attempt_start', 'job_id': job['id'],
                'task_id': task_id, 'attempt_id': attempt_id, 'plan_approval_id': plan['id'],
                'plan_digest': plan['digest'], 'workspace_identity': workspace_identity(boundary)}
            if (not isinstance(snapshot, dict) or set(snapshot) != set(expected) | {'files'}
                    or type(snapshot.get('contract_version')) is not int
                    or any(snapshot.get(key) != value for key, value in expected.items())
                    or not isinstance(snapshot.get('files'), dict) or set(snapshot['files']) != set(paths)
                    or any(value is not None and (not isinstance(value, str) or len(value) != 64
                        or any(c not in '0123456789abcdef' for c in value)) for value in snapshot['files'].values())):
                raise ValueError('native_baseline_observation_invalid')
            runs = [task.get('agent_run') or {}, *task.get('agent_run_history', []),
                    *self.for_job('agent_run', job['id'])]
            if any(run.get('attempt_id') == attempt_id for run in runs):
                raise ValueError('native_baseline_after_launch_refused')
            key = hashlib.sha256(json.dumps([job['id'], task_id, attempt_id]).encode()).hexdigest()
            record = {'id': 'native-baseline-' + key, 'job_id': job['id'], 'task_id': task_id,
                'attempt_id': attempt_id, 'snapshot': copy.deepcopy(snapshot),
                'snapshot_digest': hashlib.sha256(json.dumps(snapshot, sort_keys=True,
                    ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()}
            previous = getattr(self._worker, 'native_baseline_commit', None)
            self._worker.native_baseline_commit = record['id']
            try:
                saved = self.put('native_scope_baseline', record)
                self.event('native_scope_baseline_committed', '開始時の範囲の観測を保存しました。', task_id, job['id'],
                    details={'attempt_id': attempt_id, 'baseline_id': record['id'],
                             'snapshot_digest': record['snapshot_digest']})
                return saved
            finally:
                self._worker.native_baseline_commit = previous

    def native_scope_baseline(self, task_id, attempt_id):
        """Read the immutable record plus its unique matching commit event."""
        with self.lock:
            task = self.get(task_id, 'task')
            key = hashlib.sha256(json.dumps([task['job_id'], task_id, attempt_id]).encode()).hexdigest()
            ident = 'native-baseline-' + key
            row = self.db.execute('SELECT kind,body FROM objects WHERE id=?', (ident,)).fetchone()
            if row is None: return None
            record = json.loads(row[1]); snapshot = record.get('snapshot')
            if (row[0] != 'native_scope_baseline' or record.get('id') != ident
                    or record.get('job_id') != task['job_id'] or record.get('task_id') != task_id
                    or record.get('attempt_id') != attempt_id or not isinstance(snapshot, dict)
                    or snapshot.get('job_id') != task['job_id'] or snapshot.get('task_id') != task_id
                    or snapshot.get('attempt_id') != attempt_id
                    or record.get('snapshot_digest') != hashlib.sha256(json.dumps(snapshot, sort_keys=True,
                        ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()):
                raise ValueError('native_baseline_record_invalid')
            rows = self.db.execute("SELECT seq,body FROM events WHERE json_extract(body,'$.type')=? "
                "AND json_extract(body,'$.job_id')=? AND json_extract(body,'$.task_id')=? "
                "AND json_extract(body,'$.details.baseline_id')=? ORDER BY seq LIMIT 2",
                ('native_scope_baseline_committed', task['job_id'], task_id, ident)).fetchall()
            expected = {'attempt_id': attempt_id, 'baseline_id': ident, 'snapshot_digest': record['snapshot_digest']}
            if len(rows) != 1 or json.loads(rows[0][1]).get('details') != expected:
                raise ValueError('native_baseline_event_invalid')
            return dict(record, event_seq=rows[0][0])

    def commit_native_reconciliation(self, task_id, target, summary, note, database_stamp, assert_process):
        """Durable server CAS for a maintenance judgment; never clears a fence."""
        if getattr(self._worker, 'task', None) is not None:
            raise ValueError('worker_cannot_reconcile_native_effects')
        with self.atomic():
            if (self.db.total_changes, self.db.execute('PRAGMA data_version').fetchone()[0]) != database_stamp:
                raise StaleAttempt('native_reconciliation_state_changed')
            assert_process()  # Engine metadata only, no Git/file IO in this txn.
            task = self.get(task_id, 'task'); job = self.get(task['job_id'], 'job')
            expected = {'contract_version': 1, 'action': 'reconcile_recovery', 'job_id': job['id'],
                'task_id': task_id, 'attempt_id': (task.get('current_attempt') or {}).get('id'),
                'job_revision': job['state_revision'], 'task_revision': task['state_revision']}
            if (not isinstance(target, dict) or set(target) != set(expected) | {'state_digest'}
                    or any(target.get(k) != v for k, v in expected.items())
                    or any(type(target.get(k)) is not int for k in ('contract_version', 'job_revision', 'task_revision'))
                    or not isinstance(target.get('state_digest'), str) or len(target['state_digest']) != 64
                    or any(c not in '0123456789abcdef' for c in target['state_digest'])
                    or task.get('role') != 'builder' or not task.get('work_package_id')
                    or task.get('status') not in ('failed', 'blocked', 'interrupted')
                    or job.get('status') not in ('failed', 'blocked', 'interrupted')
                    or (task.get('current_attempt') or {}).get('status') not in ('closed', 'fenced')
                    or not isinstance(summary, dict) or summary.get('observation_stable') is not True
                    or not isinstance(note, str) or not note.strip() or len(note) > 1000):
                raise ValueError('native_reconciliation_target_invalid')
            baseline = self.native_scope_baseline(task_id, expected['attempt_id'])
            if baseline is None: raise ValueError('native_reconciliation_baseline_required')
            key = hashlib.sha256(json.dumps([job['id'], task_id, expected['attempt_id'], target['state_digest']]).encode()).hexdigest()
            record = {'id': 'native-reconciliation-' + key, 'job_id': job['id'], 'task_id': task_id,
                'attempt_id': expected['attempt_id'], 'target': copy.deepcopy(target), 'summary': copy.deepcopy(summary),
                'baseline_id': baseline['id'], 'actor': 'authenticated_maintenance_session',
                'writer_stopped': True, 'checked_changes': True, 'external_effects': 'none_confirmed', 'note': note}
            record['record_digest'] = hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False,
                separators=(',', ':')).encode()).hexdigest()
            previous = getattr(self._worker, 'native_reconciliation_commit', None)
            self._worker.native_reconciliation_commit = record['id']
            try:
                saved = self.put('native_effect_reconciliation', record)
                self.event('recovery_effects_reconciled', '保守担当の作用の照合を記録しました。', task_id, job['id'],
                    {'attempt_id': expected['attempt_id'], 'reconciliation_id': record['id'],
                     'target_digest': target['state_digest'], 'record_digest': record['record_digest']})
                return saved
            finally:
                self._worker.native_reconciliation_commit = previous

    def native_reconciliation(self, task_id, target):
        with self.lock:
            job_id = self.get(task_id, 'task')['job_id']
            key = hashlib.sha256(json.dumps([job_id, task_id, target['attempt_id'], target['state_digest']]).encode()).hexdigest()
            ident = 'native-reconciliation-' + key
            row = self.db.execute('SELECT kind,body FROM objects WHERE id=?', (ident,)).fetchone()
            if row is None: return None
            record = json.loads(row[1])
            body = {k:v for k,v in record.items() if k not in ('record_digest', 'updated_at')}
            if (row[0] != 'native_effect_reconciliation' or record.get('id') != ident
                    or record.get('job_id') != job_id or record.get('task_id') != task_id
                    or record.get('target') != target or record.get('attempt_id') != target['attempt_id']
                    or record.get('record_digest') != hashlib.sha256(json.dumps(body, sort_keys=True,
                        ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()):
                raise ValueError('native_reconciliation_record_invalid')
            rows = self.db.execute("SELECT seq,body FROM events WHERE json_extract(body,'$.type')=? "
                "AND json_extract(body,'$.job_id')=? AND json_extract(body,'$.task_id')=? "
                "AND json_extract(body,'$.details.reconciliation_id')=? ORDER BY seq LIMIT 2",
                ('recovery_effects_reconciled', job_id, task_id, ident)).fetchall()
            details = {'attempt_id': target['attempt_id'], 'reconciliation_id': ident,
                'target_digest': target['state_digest'], 'record_digest': record['record_digest']}
            if len(rows) != 1 or json.loads(rows[0][1]).get('details') != details:
                raise ValueError('native_reconciliation_event_invalid')
            return dict(record, event_seq=rows[0][0])

    def update(self, object_id, _transition_reason=None, **values):
        with self.lock:
            row = self.db.execute('SELECT kind, body FROM objects WHERE id=?', (object_id,)).fetchone()
            if not row:
                raise ValueError('対象が見つかりません。')
            obj = json.loads(row[1])
            obj.update(values)
            return self.put(row[0], obj, transition_reason=_transition_reason)

    def all(self, kind):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute('SELECT body FROM objects WHERE kind=? ORDER BY rowid', (kind,))]

    def for_job(self, kind, job_id):
        """Objects of one job, filtered in SQLite so only that job's bodies are decoded (same order as all())."""
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute(
                "SELECT body FROM objects WHERE kind=? AND json_extract(body,'$.job_id')=? ORDER BY rowid", (kind, job_id))]

    def event(self, event_type, message, task_id=None, job_id=None, details=None):
        # Persist summaries, not raw provider protocol messages or environment variables.
        record = dict(type=event_type, message=str(message)[:3000], task_id=task_id,
                      job_id=job_id, at=now())
        if details is not None:
            if not isinstance(details, dict):
                raise ValueError('イベント詳細はオブジェクトで指定してください。')
            record['details'] = copy.deepcopy(details)
        with self.lock:
            if getattr(self._worker, 'task', None) is not None and self.depth == 0:
                with self.atomic():
                    return self.event(event_type, message, task_id, job_id, details)
            self._check_worker()
            cursor = self.db.execute('INSERT INTO events(body) VALUES (?)', (json.dumps(record, ensure_ascii=False),))
            if self.depth == 0:
                self.db.commit()
            return cursor.lastrowid

    def assert_attempt(self, task):
        """Check persisted generation, never the in-memory Store.revision."""
        with self.lock:
            try:
                job = self.get(task['job_id'], 'job')
            except ValueError:
                if 'state_revision' not in task and not task.get('current_attempt'):
                    return
                raise StaleAttempt('実行対象の依頼を確認できません。') from None
            if workflow_version(job) is None:
                return
            current = self.get(task['id'], 'task')
            expected = task.get('current_attempt') or {}
            actual = current.get('current_attempt') or {}
            binding = actual.get('approved_versions') or {}
            if (not expected.get('id') or actual.get('id') != expected['id']
                    or actual.get('runtime_id') != self.runtime_id or actual.get('status') != 'running'
                    or current.get('attempt') != task.get('attempt')
                    or current.get('status') in ('cancelled', 'interrupted')
                    or any(job.get(key) != value for key, value in binding.items())
                    or job.get('status') in ('cancelled', 'interrupted', 'accepted', 'accepted_with_pending_checks')):
                raise StaleAttempt('古い実行結果は保存できません。現在の状態を確認してください。')

    def _check_worker(self):
        task = getattr(self._worker, 'task', None)
        if task is not None:
            self.assert_attempt(task)

    @contextmanager
    def attempt_scope(self, task):
        previous = getattr(self._worker, 'task', None)
        self.assert_attempt(task)
        self._worker.task = task
        try:
            yield
        finally:
            self._worker.task = previous

    def reserve_attempt(self, task_id, expected_revision):
        with self.atomic():
            task = self.get(task_id, 'task')
            job = self.get(task['job_id'], 'job')
            if (workflow_version(job) != 1 or type(expected_revision) is not int
                    or task.get('state_revision') != expected_revision
                    or task['status'] != 'queued' or job['status'] not in ('planning', 'running')
                    or (task.get('current_attempt') or {}).get('status') == 'running'
                    or task.get('recovery_check_required')):
                raise StaleAttempt('この担当は起動できません。現在の状態を確認してください。')
            if task.get('role') != 'planner':
                approval = self.get(job.get('plan_approval_id'), 'approval')
                if (approval.get('job_id') != job['id'] or approval.get('kind') != 'plan'
                        or approval.get('status') != 'approved'):
                    raise StaleAttempt('承認済み計画がないため起動できません。')
            attempt = {'id': uid(), 'ordinal': task['attempt'] + 1, 'status': 'running',
                       'runtime_id': self.runtime_id, 'started_at': now(),
                       'approved_versions': {key: job.get(key) for key in
                           ('requirement_revision', 'requirements_approval_id', 'plan_revision', 'plan_approval_id')}}
            attempt['start_event_seq'] = self.event('attempt_started', '担当の実行世代を予約しました。',
                task_id, task['job_id'], details={'attempt_id': attempt['id'], 'ordinal': attempt['ordinal']})
            return self.update(task_id, status='running', attempt=attempt['ordinal'],
                               current_attempt=attempt, started_at=attempt['started_at'], retry_at=None)

    def close_attempt(self, task_id, status, reason):
        if status not in ('closed', 'fenced'):
            raise ValueError('試行の終了状態が不正です。')
        with self.atomic():
            task = self.get(task_id, 'task')
            attempt = task.get('current_attempt') or {}
            if not attempt or attempt.get('status') != 'running':
                return
            self.event('attempt_closed', '担当の実行世代を終了しました。', task_id, task['job_id'],
                       details={'attempt_id': attempt['id'], 'ordinal': attempt['ordinal'],
                                'status': status, 'reason': str(reason)[:80]})
            self.update(task_id, current_attempt=dict(attempt, status=status, closed_at=now()))

    @contextmanager
    def atomic(self):
        with self.lock:
            outer = self.depth == 0
            if outer:
                self.db.execute('BEGIN IMMEDIATE')
            self.depth += 1
            try:
                self._check_worker()
                yield
                if outer:
                    self.db.commit()
            except BaseException:
                if outer:
                    self.db.rollback()
                raise
            finally:
                self.depth -= 1

    def job_events(self, job_id, event_type):
        with self.lock:
            rows = self.db.execute("SELECT body FROM events WHERE json_extract(body,'$.job_id')=? AND json_extract(body,'$.type')=? ORDER BY seq",
                                   (job_id, event_type))
            return [json.loads(row[0]) for row in rows]

    def events(self, count=100):
        with self.lock:
            rows = self.db.execute('SELECT seq, body FROM events ORDER BY seq DESC LIMIT ?', (count,))
            return [dict(json.loads(row[1]), seq=row[0]) for row in rows]

    def recover(self):
        with self.atomic():
            for task in self.all('task'):
                versioned = 'state_revision' in task
                interrupted = task['status'] in ('running', 'awaiting_approval')
                retry_queued = versioned and task['status'] == 'queued' and bool(task.get('current_attempt'))
                if versioned and (interrupted or retry_queued):
                    job = self.get(task['job_id'], 'job')
                    if job.get('status') in ('cancelled', 'accepted', 'accepted_with_pending_checks'):
                        self.close_attempt(task['id'], 'fenced', 'terminal-restart')
                        self.update(task['id'], status='cancelled', recovery_check_required=True)
                        continue
                if interrupted or retry_queued:
                    if 'state_revision' in task:
                        self.close_attempt(task['id'], 'fenced', 'restart')
                        self.update(task['id'], recovery_check_required=True)
                    self.update(task['id'], status='interrupted', summary='サービス再起動により中断。変更内容を確認して再試行してください。')
                    self.update(task['job_id'], status='interrupted')
            for approval in self.all('approval'):
                if approval['status'] == 'pending' and approval['kind'] == 'tool':
                    self.update(approval['id'], status='expired', note='再起動により元の要求は失効しました。')

    def backup(self):
        target = self.directory / ('backup-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uid()[:6] + '.sqlite3')
        with self.lock, closing(sqlite3.connect(target)) as dest:
            self.db.backup(dest)
        return target.name
