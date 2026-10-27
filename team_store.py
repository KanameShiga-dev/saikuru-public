"""Durable task state. All state transitions are serialized in this process."""
import copy
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
            from team_plan_revision import approval_digest, VERSION_FIELDS, CONTRACT_FIELDS
            from team_p0_contract import validate_plan
            job = self.get(job_id, 'job')
            approval = self.get(approval_id, 'approval')
            target = approval.get('workflow_revision') or {}
            if (workflow_version(job) != 1 or job['status'] != 'awaiting_approval'
                    or type(expected_revision) is not int or job['state_revision'] != expected_revision
                    or target.get('expected_job_revision') != expected_revision
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
                return self.update(job_id, goal_contract=contract,
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

    def confirm_recovery(self, task_id, task_revision, job_revision, attempt_id):
        """Internal CAS for authenticated maintenance service; never queues work."""
        with self.atomic():
            task = self.get(task_id, 'task')
            job = self.get(task['job_id'], 'job')
            attempt = task.get('current_attempt') or {}
            if (workflow_version(job) != 1 or type(task_revision) is not int or type(job_revision) is not int
                    or task.get('state_revision') != task_revision or job.get('state_revision') != job_revision
                    or job.get('status') not in ('failed', 'blocked', 'interrupted')
                    or task.get('status') not in ('failed', 'blocked', 'interrupted')
                    or not isinstance(attempt_id, str) or not attempt_id or attempt.get('id') != attempt_id
                    or attempt.get('status') not in ('closed', 'fenced') or self.has_operation_history(job['id'])):
                raise ValueError('現在の停止状態・試行・操作の確認が必要です。')
            marker = {'attempt_id': attempt_id, 'job_revision': job_revision,
                      'task_revision': task_revision + 1}
            marker['event_seq'] = self.event('recovery_confirmed', '保守用経路で作業停止の確認を記録しました。',
                task_id, job['id'], details={'attempt_id': attempt_id, 'task_revision': task_revision,
                                           'job_revision': job_revision, 'operations': 'none'})
            previous = getattr(self._worker, 'recovery_commit', None)
            self._worker.recovery_commit = task_id
            try:
                return self.update(task_id, recovery_check_required=False, recovery_confirmation=marker)
            finally:
                self._worker.recovery_commit = previous

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
