"""Scoped checkpoint collections. No Git mutation, restore, dispatch or ready commit."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import uuid

from team_dlp import scan_input

MAX_FILES = 64
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 8_000_000
MAX_GIT_BYTES = 2_000_000
SUFFIXES = {'.py', '.js', '.ts', '.tsx', '.jsx', '.html', '.css', '.md', '.txt', '.json',
            '.toml', '.yaml', '.yml', '.svg', '.ps1', '.cs', '.sh'}
FORBIDDEN = {'data', 'logs', 'attachments', 'node_modules', 'venv', 'legacy', 'secrets',
             'credentials', 'conversations', 'transcripts', 'chat_history'}
HEX = re.compile(r'^[0-9a-f]{64}$')
IDENTIFIER = re.compile(r'^[0-9a-f]{32}$')


class CheckpointUnavailable(ValueError):
    """A temporary/caller gate, not proof that the stored draft is corrupt."""

DRAFT_FIELDS = {'id', 'schema_version', 'state', 'state_revision', 'updated_at', 'job_id', 'task_id',
    'work_package_id', 'attempt_id', 'attempt_ordinal', 'attempt_start_event_seq', 'snapshot_generation',
    'requirement_revision', 'requirements_approval_id', 'plan_revision', 'plan_approval_id', 'collection_ref',
    'ready_binding', 'resume_position', 'resume_preconditions', 'created_at', 'snapshot_revisions',
    'artifact_refs', 'evidence_refs', 'decision_refs', 'operation_refs', 'handoff_observation', 'manifest_digest'}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def checkpoint_digest(record):
    mutable = {'state', 'state_revision', 'updated_at', 'ready_binding', 'manifest_digest'}
    return digest({key: value for key, value in record.items() if key not in mutable})


def draft_preconditions(refs, collection):
    result = ['checkpoint_not_ready']
    if refs['operation_refs']:
        result.append('operation_reconciliation_required')
    if refs['handoff_observation']['unresolved_conflicts']:
        result.append('handoff_conflicts')
    files = collection['git_snapshot']['files']
    if any((files[item['path']]['worktree'] or {}).get('sha256') != item['sha256']
           for item in refs['artifact_refs']):
        result.append('artifacts_changed')
    return result


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Checkpointの重複したキーは使用できません。')
        result[key] = value
    return result


def _link(path):
    return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()


def _root(path):
    raw = Path(path).absolute()
    if raw != raw.resolve() or any(_link(p) for p in (raw, *raw.parents)):
        raise ValueError('Checkpointの参照先にリンクは使えません。')
    return raw


def scope_paths(paths):
    if not isinstance(paths, list) or not 1 <= len(paths) <= MAX_FILES:
        raise ValueError('Checkpointには承認された有限のファイル一覧が必要です。')
    result = []
    for name in paths:
        if not isinstance(name, str) or not name or len(name) > 260 or '\\' in name or ':' in name:
            raise ValueError('Checkpointの相対パスが不正です。')
        p = PurePosixPath(name)
        if (p.is_absolute() or p.as_posix() != name or any(ord(c) < 32 for c in name)
                or any(part in ('.', '..') or part.startswith('.') or part.casefold() in FORBIDDEN
                       or any(word in part.casefold() for word in
                              ('secret', 'credential', 'private_key', 'access_token', 'api_key'))
                       for part in p.parts) or p.suffix.casefold() not in SUFFIXES):
            raise ValueError('機密設定・運用記録・範囲外のファイルは参照できません。')
        privacy = scan_input(name)
        if privacy['block'] or privacy['confirm']:
            raise ValueError('Checkpointのファイル名を安全に保存できません。')
        result.append(name)
    if len({p.casefold() for p in result}) != len(result):
        raise ValueError('Checkpointのファイル一覧に重複があります。')
    return sorted(result)


def _path(root, relative):
    path = root
    for part in PurePosixPath(relative).parts:
        path = path / part
        if _link(path):
            raise ValueError('Checkpointのファイルにリンクは使えません。')
    if not path.resolve().is_relative_to(root):
        raise ValueError('Checkpointの参照が作業範囲外です。')
    return path


def _clean(data):
    if len(data) > MAX_FILE_BYTES or b'\0' in data:
        raise ValueError('Checkpointのファイルが大きすぎるか、テキストではありません。')
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ValueError('CheckpointはUTF-8テキストだけを対象にします。') from None
    privacy = scan_input(text)
    conversation = (re.search(r'(?im)^\s*(?:user|assistant|system|human|developer|ユーザー|アシスタント|利用者)\s*[:：]', text)
                    or re.search(r'"(?:messages|conversation|transcript)"\s*:', text)
                    or (re.search(r'"(?:role|speaker)"\s*:', text)
                        and re.search(r'"(?:content|text)"\s*:', text)))
    if privacy['block'] or privacy['confirm'] or conversation:
        raise ValueError('秘密・個人情報・会話記録の疑いがあるため保存しません。')
    return data


def _read(path):
    if _link(path):
        raise ValueError('リンクは読み取れません。')
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_FILE_BYTES:
        raise ValueError('安全な通常ファイルを確認できません。')
    with path.open('rb') as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
        observed = os.fstat(stream.fileno())
    after = path.stat()
    identity = lambda s: (s.st_ino, s.st_size, s.st_mtime_ns)
    if _link(path) or identity(before) != identity(observed) or identity(before) != identity(after):
        raise ValueError('保存中にファイルが変化しました。')
    return _clean(data)


def _git(root, args, *, input_data=None, codes=(0,)):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS='0', GIT_NO_REPLACE_OBJECTS='1',
               GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
    base = ['git', '--no-pager', '--no-optional-locks', '-c', 'core.fsmonitor=false',
            '-c', 'core.untrackedCache=false', '-C', str(root)]
    try:
        # status can otherwise invoke repository-defined clean/process filters.
        # Read names only, never their command values, and override for this child.
        filters = subprocess.run([*base, 'config', '--null', '--name-only', '--get-regexp',
            r'^filter\..*\.(clean|smudge|process|required)$'], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=10, env=env)
        if filters.returncode not in (0, 1) or len(filters.stdout) > MAX_GIT_BYTES:
            raise ValueError('Gitフィルター設定を安全に確認できません。')
        overrides = []
        for raw in filters.stdout.split(b'\0'):
            if not raw:
                continue
            key = raw.decode('ascii')
            if not re.fullmatch(r'filter\.[A-Za-z0-9_.-]+\.(clean|smudge|process|required)', key):
                raise ValueError('未対応のGitフィルター設定です。')
            overrides += ['-c', key + ('=false' if key.endswith('.required') else '=')]
        # check-ignore consumes literal filenames over stdin and does not accept
        # the literal-pathspec magic used by the tree/index commands.
        literal = [] if args[0] in ('check-ignore', 'config') else ['--literal-pathspecs']
        result = subprocess.run([*base, *overrides, *literal, *args],
            input=input_data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, env=env)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError('Gitの参照を確認できません。') from None
    if result.returncode not in codes or len(result.stdout) > MAX_GIT_BYTES:
        raise ValueError('Gitの参照または出力量を確認できません。')
    return result.stdout


def _status(data, allowed):
    tokens = data.split(b'\0')
    rows = []
    i = 0
    while i < len(tokens) and tokens[i]:
        token = tokens[i]
        i += 1
        if len(token) < 4 or token[2:3] != b' ':
            raise ValueError('Git変更一覧の形式が不正です。')
        code, name = token[:2].decode('ascii'), token[3:].decode('utf-8')
        row = {'status': code, 'path': name}
        names = [name]
        if 'R' in code or 'C' in code:
            if i >= len(tokens) or not tokens[i]:
                raise ValueError('Git移動元の参照がありません。')
            row['from'] = tokens[i].decode('utf-8')
            names.append(row['from'])
            i += 1
        if any(name not in allowed for name in names):
            raise ValueError('承認されたファイル範囲の外に変更があります。')
        rows.append(row)
    return sorted(rows, key=lambda r: (r['path'], r.get('from', '')))


def _entries(data, allowed, *, index):
    result = {}
    for row in data.split(b'\0'):
        if not row:
            continue
        metadata, raw_path = row.split(b'\t', 1)
        mode, oid, third = metadata.decode('ascii').split() if index else ('', '', '')
        if not index:
            mode, kind, oid = metadata.decode('ascii').split()
            if kind != 'blob':
                raise ValueError('Git参照は通常ファイルだけが対象です。')
        name = raw_path.decode('utf-8')
        if (name not in allowed or mode not in ('100644', '100755')
                or (index and third != '0') or not re.fullmatch(r'[0-9a-f]{40,64}', oid)):
            raise ValueError('Gitの競合・リンク・範囲外参照は保存できません。')
        result[name] = {'mode': mode, 'git_oid': oid}
    return result


def _observe(root, paths):
    # Reject selected reparse paths before asking Git to inspect the worktree.
    for name in paths:
        _path(root, name)
    top = _git(root, ['rev-parse', '--show-toplevel']).decode('utf-8').strip()
    if Path(top).resolve() != root:
        raise ValueError('Checkpointの作業ルートとGitルートが一致しません。')
    head = _git(root, ['rev-parse', '--verify', 'HEAD']).decode('ascii').strip()
    if not re.fullmatch(r'[0-9a-f]{40,64}', head):
        raise ValueError('Gitの基準版がありません。')
    all_index = _git(root, ['ls-files', '--stage', '-z'])
    if any(row.startswith(b'160000 ') for row in all_index.split(b'\0')):
        raise ValueError('サブモジュールを含むCheckpointはまだ扱いません。')
    dirty = _status(_git(root, ['status', '--porcelain=v1', '-z', '--untracked-files=all',
                               '--ignore-submodules=all']), set(paths))
    if _git(root, ['check-ignore', '-z', '--stdin'], input_data=('\0'.join(paths) + '\0').encode(), codes=(0, 1)):
        raise ValueError('Gitで除外されたファイルは自動保存しません。')
    indexed = _entries(_git(root, ['ls-files', '--stage', '-z', '--', *paths]), set(paths), index=True)
    baseline = _entries(_git(root, ['ls-tree', '-r', '-z', head, '--', *paths]), set(paths), index=False)
    files, blobs = {}, {}
    total = 0
    for name in paths:
        path = _path(root, name)
        states = {'index': None, 'worktree': None}
        if name not in indexed and name not in baseline and not path.exists():
            raise ValueError('Checkpointのファイルが存在しません。')
        for state in ('index', 'worktree'):
            if state == 'index':
                if name not in indexed:
                    continue
                data = _clean(_git(root, ['cat-file', 'blob', indexed[name]['git_oid']]))
            else:
                if not path.exists():
                    continue
                data = _read(path)
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise ValueError('Checkpointの総容量を超えました。')
            sha = hashlib.sha256(data).hexdigest()
            blobs[sha] = data
            states[state] = {'sha256': sha, 'blob': 'blobs/' + sha + '.blob'}
        files[name] = states
    return {'head': head, 'index_entries': indexed, 'head_entries': baseline,
            'dirty': dirty, 'files': files}, blobs


def _atomic_write(path, data):
    if _link(path) or _link(path.parent):
        raise ValueError('保存先のリンクは使用できません。')
    temporary = path.with_name(uuid.uuid4().hex + '.tmp')
    with temporary.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _storage(project, directory):
    storage = _root(directory)
    if storage.is_relative_to(project) or project.is_relative_to(storage):
        raise ValueError('Checkpoint保管先は作業プロジェクトの外に限定します。')
    return storage


def collect_project(project, allowed_paths, directory):
    """Copy only screened scoped text to a new local collection, never to Git."""
    root = _root(project)
    storage = _storage(root, directory)
    paths = scope_paths(allowed_paths)
    observation, blobs = _observe(root, paths)
    collection_id = uuid.uuid4().hex
    target = storage / collection_id
    target.mkdir(parents=True, mode=0o700, exist_ok=False)
    (target / 'blobs').mkdir(mode=0o700)
    for sha, data in blobs.items():
        _atomic_write(target / 'blobs' / (sha + '.blob'), data)
    current, _ = _observe(root, paths)
    if current != observation:
        raise ValueError('保存中にGitまたは作業ファイルが変化しました。')
    manifest = {'schema_version': 1, 'collection_id': collection_id, 'scope': paths,
                'git_snapshot': observation}
    manifest['manifest_digest'] = digest(manifest)
    _atomic_write(target / 'manifest.json', json.dumps(manifest, ensure_ascii=False,
                  sort_keys=True, indent=2).encode('utf-8'))
    return manifest


def load_collection(project, directory, collection_id):
    root = _root(project)
    storage = _storage(root, directory)
    if not isinstance(collection_id, str) or not IDENTIFIER.fullmatch(collection_id):
        raise ValueError('Checkpoint保存先の識別が不正です。')
    target = _path(storage, collection_id)
    path = _path(target, 'manifest.json')
    if not path.is_file() or path.stat().st_size > MAX_GIT_BYTES:
        raise ValueError('Checkpointのmanifestを確認できません。')
    try:
        manifest = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_unique_json)
    except (ValueError, OSError, RecursionError):
        raise ValueError('Checkpointのmanifestが不正です。') from None
    if (not isinstance(manifest, dict) or set(manifest) != {
            'schema_version', 'collection_id', 'scope', 'git_snapshot', 'manifest_digest'}
            or type(manifest.get('schema_version')) is not int or manifest['schema_version'] != 1
            or manifest.get('collection_id') != collection_id
            or manifest.get('manifest_digest') != digest({k: v for k, v in manifest.items() if k != 'manifest_digest'})):
        raise ValueError('Checkpointのmanifestが不正です。')
    return manifest


def inspect_collection(project, directory, manifest, allowed_paths):
    """Validate stored blobs and report scoped changes without restoring files."""
    root = _root(project)
    storage = _storage(root, directory)
    paths = scope_paths(allowed_paths)
    if (not isinstance(manifest, dict) or set(manifest) != {
            'schema_version', 'collection_id', 'scope', 'git_snapshot', 'manifest_digest'}
            or type(manifest.get('schema_version')) is not int or manifest['schema_version'] != 1
            or manifest.get('scope') != paths
            or manifest.get('manifest_digest') != digest({k: v for k, v in manifest.items() if k != 'manifest_digest'})):
        raise ValueError('Checkpointの対象版・範囲が一致しません。')
    identifier = manifest.get('collection_id')
    if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
        raise ValueError('Checkpoint保存先の識別が不正です。')
    target = _path(storage, identifier)
    snapshot = manifest.get('git_snapshot')
    if (not isinstance(snapshot, dict) or set(snapshot) != {'head', 'index_entries', 'head_entries', 'dirty', 'files'}
            or not isinstance(snapshot.get('files'), dict) or set(snapshot['files']) != set(paths)):
        raise ValueError('Checkpointのファイル参照が不正です。')
    for states in manifest['git_snapshot']['files'].values():
        if not isinstance(states, dict) or set(states) != {'index', 'worktree'}:
            raise ValueError('Checkpointの差分状態が不正です。')
        for reference in states.values():
            if reference is None:
                continue
            if not isinstance(reference, dict) or set(reference) != {'sha256', 'blob'}:
                raise ValueError('Checkpointの差分参照が不正です。')
            sha = reference.get('sha256')
            if (not isinstance(sha, str) or not HEX.fullmatch(sha)
                    or reference.get('blob') != 'blobs/' + sha + '.blob'):
                raise ValueError('Checkpointの差分参照が不正です。')
            data = _read(_path(target, reference['blob']))
            if hashlib.sha256(data).hexdigest() != sha:
                raise ValueError('Checkpointの差分が変化しています。')
    current, _ = _observe(root, paths)
    previous = manifest['git_snapshot']
    changed = [name for name in paths if previous['files'][name] != current['files'][name]]
    return {'current': current == previous, 'changed_paths': changed,
            'head_changed': current['head'] != previous['head']}


def verify_collection(project, directory, manifest, allowed_paths):
    if not inspect_collection(project, directory, manifest, allowed_paths)['current']:
        raise ValueError('Git・index・作業ファイルが保存時から変化しています。')
    return True


def reference_snapshot(store, handoff, job, task):
    """Hash existing authorities; never copy their text into checkpoint bodies."""
    attempt = task.get('current_attempt') or {}
    keys = {'requirement_revision', 'requirements_approval_id', 'plan_revision', 'plan_approval_id'}
    if (not isinstance(attempt.get('approved_versions'), dict) or set(attempt['approved_versions']) != keys
            or type(attempt.get('start_event_seq')) is not int or attempt['start_event_seq'] < 1):
        raise ValueError('試行の承認参照が不正です。')
    for identifier in (job['id'], task['id'], attempt.get('id')):
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', identifier):
            raise ValueError('Checkpointの識別子が不正です。')
    row = store.db.execute('SELECT body FROM events WHERE seq=?', (attempt.get('start_event_seq'),)).fetchone()
    if not row:
        raise ValueError('試行の開始記録がありません。')
    start = json.loads(row[0])
    if (start.get('type') != 'attempt_started' or start.get('task_id') != task['id']
            or start.get('job_id') != job['id'] or start.get('details', {}).get('attempt_id') != attempt.get('id')
            or start.get('details', {}).get('ordinal') != task.get('attempt')):
        raise ValueError('試行の開始記録が一致しません。')
    for key, value in (attempt.get('approved_versions') or {}).items():
        if job.get(key) != value:
            raise ValueError('試行の承認版が現在の依頼と一致しません。')
    wp = store.get(task['work_package_id'], 'work_package') if task.get('work_package_id') else None
    if wp and wp.get('job_id') != job['id']:
        raise ValueError('別の依頼の作業パッケージは参照できません。')
    decisions = []
    for approval in store.for_job('approval', job['id']):
        # IDs/digests only: no question, answer, user name or credential capability.
        if not isinstance(approval.get('id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', approval['id']):
            raise ValueError('Checkpointの承認識別子が不正です。')
        decisions.append({'origin': 'store', 'kind': 'approval', 'id': approval['id'],
                          'state_digest': digest(approval)})
    operations = []
    rows = store.db.execute("SELECT seq,body FROM events WHERE json_extract(body,'$.job_id')=? "
                            "AND json_extract(body,'$.type') LIKE 'operation_%' ORDER BY seq LIMIT 1001", (job['id'],)).fetchall()
    if len(rows) > 1000 or len(decisions) > 1000:
        raise ValueError('Checkpointの参照件数を超えています。')
    for seq, body in rows:
        event = json.loads(body)
        operations.append({'origin': 'store', 'table': 'events', 'seq': seq, 'state_digest': digest(event)})
    with handoff.lock:
        facts = handoff.db.execute('SELECT seq,authority,status,value,evidence FROM facts WHERE job_id=? ORDER BY seq', (job['id'],)).fetchall()
        conflicts = handoff.db.execute('SELECT id,old_fact,new_fact,status,resolution FROM conflicts WHERE job_id=? ORDER BY id', (job['id'],)).fetchall()
        from team_handoff import canonical
        skills = handoff.db.execute('SELECT a.name,a.version_id,a.enabled,v.payload FROM skill_active a '
            'LEFT JOIN skill_versions v ON v.id=a.version_id WHERE a.project=? ORDER BY a.name', (canonical(job['project']),)).fetchall()
        # Our Store-derived receipt is not a new independent Handoff authority.
        watermark = handoff.db.execute("SELECT COALESCE(max(seq),0) FROM records WHERE job_id=? AND kind!='checkpoint_ready'", (job['id'],)).fetchone()[0]
    observation = {'records_seq': watermark, 'facts_digest': digest(facts),
                   'conflicts_digest': digest(conflicts), 'skills_digest': digest(skills),
                   'unresolved_conflicts': any(row[3] == 'pending' for row in conflicts)}
    decisions.extend({'origin': 'handoff', 'kind': 'fact', 'id': row[0], 'state_digest': digest(row)} for row in facts)
    decisions.extend({'origin': 'handoff', 'kind': 'conflict', 'id': row[0], 'state_digest': digest(row)} for row in conflicts)
    artifacts = job.get('p0_artifacts') or []
    if (not isinstance(artifacts, list) or any(not isinstance(item, dict)
            or not isinstance(item.get('path'), str) or not isinstance(item.get('sha256'), str)
            or not HEX.fullmatch(item['sha256']) for item in artifacts)):
        raise ValueError('成果物の参照が不正です。')
    return {'snapshot_revisions': {'job': job['state_revision'], 'task': task['state_revision'],
                                    'work_package': wp['state_revision'] if wp else None},
            'artifact_refs': [{'path': item['path'], 'sha256': item['sha256']} for item in artifacts],
            'evidence_refs': {'job_validation': digest(job.get('validation_evidence')),
                              'task_validation': digest(task.get('validation_runs')),
                              'test_assets': digest(task.get('test_asset_snapshot')),
                              'recovery_context': recovery_context(store, job)},
            'decision_refs': decisions, 'operation_refs': operations, 'handoff_observation': observation}


def recovery_context(store, job):
    """Bounded semantic hashes, never raw task instructions/profiles/results."""
    job_fields = ('goal', 'goal_contract', 'project', 'route', 'document_source',
                  'p0_contract_version', 'workflow_contract_version', 'validation_commands',
                  'code_test_plan', 'auto_execute', 'requirement_revision', 'requirements_approval_id',
                  'plan_revision', 'plan_approval_id')
    task_fields = ('title', 'instruction', 'role', 'profile', 'after', 'work_package_id',
                   'repair', 'agent_name', 'review_cycle', 'confirmation_origin', 'plan_revision',
                   'spec_source_approval_id', 'validation_commands')
    tasks = store.for_job('task', job['id'])
    if len(tasks) > 256:
        raise ValueError('再開照合の担当件数を超えています。')
    entries = {}
    for task in tasks:
        identifier = task.get('id')
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', identifier):
            raise ValueError('再開照合の担当識別子が不正です。')
        if task.get('status') not in ('queued', 'running', 'awaiting_approval', 'succeeded', 'failed', 'blocked', 'interrupted', 'cancelled', 'handed_off'):
            raise ValueError('再開照合の担当状態が不正です。')
        entries[identifier] = {'semantic': digest({key: task.get(key) for key in task_fields}),
            'validation': digest(task.get('validation_runs')), 'tests': digest(task.get('test_asset_snapshot')),
            'status': task.get('status'), 'attempt_id': (task.get('current_attempt') or {}).get('id'),
            'result_status': digest((task.get('result') or {}).get('status'))}
    return {'schema_version': 1, 'job': digest({key: job.get(key) for key in job_fields}), 'tasks': entries}


def ready_candidate(store, task):
    """Validate original immutable publication; advanced current state is separate.

    Never falls back to an older ready snapshot. Never authorizes execution.
    """
    record = store.get(task.get('resume_checkpoint_id'), 'checkpoint')
    binding = record.get('ready_binding')
    if (set(record) != DRAFT_FIELDS or record.get('state') != 'ready'
            or type(record.get('schema_version')) is not int or record['schema_version'] != 1
            or record.get('manifest_digest') != checkpoint_digest(record)
            or record.get('task_id') != task['id'] or record.get('job_id') != task['job_id']
            or not isinstance(binding, dict) or set(binding) != {'job', 'task', 'work_package', 'event_seq'}
            or any(type(binding[key]) is not int or binding[key] < 1 for key in ('job', 'task', 'event_seq'))
            or (binding['work_package'] is not None and (type(binding['work_package']) is not int or binding['work_package'] < 1))):
        raise ValueError('Checkpointの確定参照を確認できません。')
    revisions = {key: binding[key] for key in ('job', 'task', 'work_package')}
    row = store.db.execute('SELECT body FROM events WHERE seq=?', (binding['event_seq'],)).fetchone()
    event = json.loads(row[0]) if row else {}
    if (event.get('type') != 'checkpoint_ready' or event.get('job_id') != task['job_id']
            or event.get('task_id') != task['id'] or event.get('details') != {
                'checkpoint_id': record['id'], 'manifest_digest': record['manifest_digest'], 'revisions': revisions}):
        raise ValueError('Checkpointの確定イベントを確認できません。')
    return record, dict(event, seq=binding['event_seq'])


def validate_draft_record(record, job, task, refs):
    """Pure typed binding check, with no filesystem IO or new authority."""
    revisions = record.get('snapshot_revisions')
    if (not isinstance(revisions, dict) or set(revisions) != {'job', 'task', 'work_package'}
            or any(type(revisions[key]) is not int or revisions[key] < 1 for key in ('job', 'task'))
            or (revisions['work_package'] is not None and (type(revisions['work_package']) is not int or revisions['work_package'] < 1))):
        raise ValueError('Checkpointの観測版が不正です。')
    if (set(record) != DRAFT_FIELDS or type(record.get('schema_version')) is not int
            or record['schema_version'] != 1 or record.get('state') != 'writing'
            or record.get('ready_binding') is not None
            or any(type(record.get(key)) is not int or record[key] < 1 for key in (
                'attempt_ordinal', 'attempt_start_event_seq', 'snapshot_generation', 'requirement_revision', 'plan_revision'))
            or not isinstance(record.get('collection_ref'), dict)
            or set(record['collection_ref']) != {'id', 'digest'}
            or record.get('manifest_digest') != checkpoint_digest(record)):
        raise ValueError('Checkpointの下書きが不正です。')
    if (record['job_id'] != job['id'] or record['task_id'] != task['id']
            or record['attempt_id'] != task['current_attempt']['id']
            or record['work_package_id'] != task.get('work_package_id')
            or record['attempt_ordinal'] != task['attempt']
            or record['attempt_start_event_seq'] != task['current_attempt']['start_event_seq']
            or record['collection_ref']['id'] != record['id']
            or record['resume_position'] != {'task_id': task['id'], 'stage': 'after_attempt'}
            or any(record.get(key) != job.get(key) for key in (
                'requirement_revision', 'requirements_approval_id', 'plan_revision', 'plan_approval_id'))
            or any(record.get(key) != value for key, value in refs.items())):
        raise ValueError('Checkpointの参照が変化しています。')


def verify_draft_files(record, job, paths, refs, directory):
    """Run only outside Store/Handoff locks; caller rechecks references at commit."""
    collection = load_collection(job['project'], directory, record['collection_ref']['id'])
    if collection['manifest_digest'] != record['collection_ref']['digest']:
        raise ValueError('Checkpointの対象版が一致しません。')
    verify_collection(job['project'], directory, collection, paths)
    if record['resume_preconditions'] != draft_preconditions(refs, collection):
        raise ValueError('Checkpointの再開条件が一致しません。')
    return collection
