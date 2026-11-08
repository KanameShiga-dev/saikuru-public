"""Diagnostic-only runtime. Never import the normal application or live Store.

This module is loaded from verified bytes by safe_server under -I -S -B.
Its SQLite input must be an independently published, frozen snapshot, not a
live database or an uncheckpointed copy. Python guards are not an OS sandbox.
"""
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from types import MappingProxyType
from datetime import datetime


SOURCE_NAMES = ('safe_server.py', 'safe_start_runtime.py', 'web/safe-start.html')
MAX_SNAPSHOT = 128 * 1024 * 1024
HEX256 = re.compile(r'[0-9a-f]{64}\Z')
JOB_ID = re.compile(r'[0-9a-f]{32}\Z')
SAFE_LABEL = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')


class SafeStartError(RuntimeError):
    """A classified failure; do not include paths, credentials or DB bodies."""


def checked_path(value, *, directory=False, missing=False):
    """Reject links/reparse points before resolving, including parent junctions."""
    if not isinstance(value, (str, Path)) or not str(value):
        raise SafeStartError('invalid_path')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise SafeStartError('absolute_path_required')
    if path.drive.startswith('\\\\') or any(':' in p for p in path.parts[1:]):
        raise SafeStartError('network_or_stream_path_forbidden')
    if any(p.lower() in {'.credentials.json', '.env', '.claude', '.claude-sairai'}
           for p in path.parts):
        raise SafeStartError('credential_path_forbidden')
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            if missing and part == path:
                continue
            raise SafeStartError('path_missing') from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise SafeStartError('reparse_path_forbidden')
    if missing:
        if path.exists():
            raise SafeStartError('output_must_be_new')
    elif directory:
        if not path.is_dir():
            raise SafeStartError('directory_required')
    elif not path.is_file() or path.stat().st_nlink != 1:
        raise SafeStartError('single_link_regular_file_required')
    return path.resolve(strict=not missing)


def inside(path, root):
    return path != root and path.is_relative_to(root)


def digest_file(path, limit):
    digest = hashlib.sha256()
    total = 0
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(65536), b''):
            total += len(block)
            if total > limit:
                raise SafeStartError('file_too_large')
            digest.update(block)
    return digest.hexdigest(), total


class SafeManifest:
    """Validated, immutable input; provider configuration is not accepted."""
    __slots__ = ('root', 'snapshot', 'snapshot_sha256', 'snapshot_size',
                 'created_at', 'allowed_job_ids', 'source_hashes', 'manifest_sha256')

    def __init__(self, raw, manifest_path, manifest_sha256, source_root,
                 approved_base):
        required = {'schema_version', 'mode', 'host', 'port', 'diagnostic_root',
                    'source_sha256', 'snapshot', 'allowed_job_ids'}
        if not isinstance(raw, dict) or set(raw) != required:
            raise SafeStartError('manifest_keys_invalid')
        if (type(raw['schema_version']) is not int or raw['schema_version'] != 1
                or raw['mode'] != 'safe' or raw['host'] != '127.0.0.1'
                or type(raw['port']) is not int or raw['port'] != 8877):
            raise SafeStartError('safe_mode_required')
        base = checked_path(approved_base, directory=True)
        root = checked_path(raw['diagnostic_root'], directory=True)
        if not inside(root, base) or not inside(manifest_path, root):
            raise SafeStartError('diagnostic_root_outside_scope')
        hashes = raw['source_sha256']
        if (not isinstance(hashes, dict) or set(hashes) != set(SOURCE_NAMES)
                or any(not isinstance(v, str) or not HEX256.fullmatch(v)
                       for v in hashes.values())):
            raise SafeStartError('source_hashes_invalid')
        for name, expected in hashes.items():
            path = checked_path(source_root / name)
            actual, _ = digest_file(path, 2 * 1024 * 1024)
            if actual != expected:
                raise SafeStartError('source_hash_mismatch')
        snap = raw['snapshot']
        if not isinstance(snap, dict) or set(snap) != {'path', 'sha256', 'size', 'created_at'}:
            raise SafeStartError('snapshot_fields_invalid')
        if (not isinstance(snap['path'], str) or Path(snap['path']).is_absolute()
                or '..' in Path(snap['path']).parts):
            raise SafeStartError('snapshot_relative_path_required')
        snapshot = checked_path(root / snap['path'])
        if (not inside(snapshot, root) or snapshot.suffix != '.sqlite3'
                or not isinstance(snap['sha256'], str)
                or not HEX256.fullmatch(snap['sha256'])
                or type(snap['size']) is not int or not 0 < snap['size'] <= MAX_SNAPSHOT):
            raise SafeStartError('snapshot_identity_invalid')
        try:
            created = datetime.fromisoformat(snap['created_at'])
            if created.tzinfo is None:
                raise ValueError()
        except (ValueError, TypeError):
            raise SafeStartError('snapshot_time_invalid') from None
        ids = raw['allowed_job_ids']
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 64
                or any(not isinstance(x, str) or not JOB_ID.fullmatch(x) for x in ids)
                or len(ids) != len(set(ids))):
            raise SafeStartError('job_scope_invalid')
        for name, value in {
            'root': root, 'snapshot': snapshot, 'snapshot_sha256': snap['sha256'],
            'snapshot_size': snap['size'], 'created_at': created.isoformat(),
            'allowed_job_ids': tuple(ids), 'source_hashes': MappingProxyType(dict(hashes)),
            'manifest_sha256': manifest_sha256,
        }.items():
            object.__setattr__(self, name, value)

    def __setattr__(self, name, value):
        raise SafeStartError('manifest_is_immutable')


class SnapshotReadLock:
    """Windows read handle with no write/delete sharing. Snapshot only."""
    def __init__(self, path):
        self.handle = None
        if os.name != 'nt':
            raise SafeStartError('windows_snapshot_lock_required')
        self.kernel = ctypes.WinDLL('kernel32.dll', use_last_error=True, winmode=0x800)
        create = self.kernel.CreateFileW
        create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        create.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel.CloseHandle.restype = wintypes.BOOL
        # GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING, OPEN_REPARSE_POINT.
        handle = create(str(path), 0x80000000, 1, None, 3, 0x00200080, None)
        if handle == ctypes.c_void_p(-1).value or handle is None:
            raise SafeStartError('snapshot_read_lock_failed')
        self.handle = handle
        try:
            class FileInfo(ctypes.Structure):
                _fields_ = [('attributes', wintypes.DWORD),
                            ('created', wintypes.FILETIME), ('accessed', wintypes.FILETIME),
                            ('written', wintypes.FILETIME), ('volume', wintypes.DWORD),
                            ('size_high', wintypes.DWORD), ('size_low', wintypes.DWORD),
                            ('links', wintypes.DWORD), ('index_high', wintypes.DWORD),
                            ('index_low', wintypes.DWORD)]
            get_info = self.kernel.GetFileInformationByHandle
            get_info.argtypes = (wintypes.HANDLE, ctypes.POINTER(FileInfo))
            get_info.restype = wintypes.BOOL
            info = FileInfo()
            if not get_info(handle, ctypes.byref(info)) or info.links != 1 or info.attributes & 0x400:
                raise SafeStartError('snapshot_file_identity_invalid')
            self.identity = (info.volume, info.index_high, info.index_low)
        except BaseException:
            self.close()
            raise

    def verify_path(self, path):
        other = SnapshotReadLock(path)
        try:
            if other.identity != self.identity:
                raise SafeStartError('snapshot_path_identity_changed')
        finally:
            other.close()

    def close(self):
        if self.handle is not None:
            handle, self.handle = self.handle, None
            if not self.kernel.CloseHandle(handle):
                raise SafeStartError('snapshot_handle_close_failed')


class ReadOnlySnapshot:
    """No migration, recovery, arbitrary SQL or mutation methods."""
    def __init__(self, manifest):
        if not isinstance(manifest, SafeManifest):
            raise SafeStartError('validated_manifest_required')
        self.manifest = manifest
        self.lock = None
        self.db = None
        try:
            self.lock = SnapshotReadLock(manifest.snapshot)
            for suffix in ('-wal', '-shm', '-journal'):
                if manifest.snapshot.with_name(manifest.snapshot.name + suffix).exists():
                    raise SafeStartError('snapshot_sidecar_forbidden')
            actual, size = digest_file(manifest.snapshot, MAX_SNAPSHOT)
            if (actual, size) != (manifest.snapshot_sha256, manifest.snapshot_size):
                raise SafeStartError('snapshot_hash_mismatch')
            uri = manifest.snapshot.as_uri() + '?mode=ro&immutable=1'
            self.db = sqlite3.connect(uri, uri=True)
            self.lock.verify_path(manifest.snapshot)
            self.db.enable_load_extension(False)
            self.db.set_authorizer(self._authorize)
            self.db.execute('PRAGMA query_only=ON')
            self.db.execute('PRAGMA trusted_schema=OFF')
            kind = self.db.execute("SELECT type,sql FROM sqlite_master WHERE name='objects'").fetchone()
            if (not kind or kind[0] != 'table' or not isinstance(kind[1], str)
                    or not kind[1].upper().lstrip().startswith('CREATE TABLE')):
                raise SafeStartError('snapshot_schema_invalid')
            columns = self.db.execute('PRAGMA table_info(objects)').fetchall()
            if [(r[1], r[2].upper()) for r in columns] != [('kind', 'TEXT'), ('id', 'TEXT'), ('body', 'TEXT')]:
                raise SafeStartError('snapshot_schema_invalid')
            if self.db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise SafeStartError('snapshot_integrity_failed')
            count = self.db.execute('SELECT count(*) FROM objects').fetchone()[0]
            if count > 10000:
                raise SafeStartError('snapshot_row_limit')
            bad = self.db.execute('SELECT count(*) FROM objects WHERE length(body)>131072 OR NOT json_valid(body)').fetchone()[0]
            if bad:
                raise SafeStartError('snapshot_body_invalid')
            found = {r['id'] for r in self.jobs()}
            if found != set(manifest.allowed_job_ids):
                raise SafeStartError('snapshot_job_scope_missing')
            self.lock.verify_path(manifest.snapshot)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _authorize(action, first, second, database, trigger):
        if database not in (None, 'main') or trigger is not None:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ and first in ('objects', 'sqlite_master', 'sqlite_schema'):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION and (second or '').lower() in {
                'count', 'length', 'json_valid', 'json_extract'}:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_PRAGMA:
            if ((first or '').lower(), (second or '').lower()) in {
                    ('query_only', 'on'), ('trusted_schema', 'off'),
                    ('table_info', 'objects'), ('integrity_check', '')}:
                return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    def _scope(self, job_id):
        if job_id is None:
            return self.manifest.allowed_job_ids
        if job_id not in self.manifest.allowed_job_ids:
            raise PermissionError('job_outside_scope')
        return (job_id,)

    @staticmethod
    def _label(value):
        return value if isinstance(value, str) and SAFE_LABEL.fullmatch(value) else 'unknown'

    def jobs(self, job_id=None):
        ids = self._scope(job_id)
        marks = ','.join('?' for _ in ids)
        rows = self.db.execute("SELECT id,json_extract(body,'$.status'),json_extract(body,'$.g2_gate_version'),json_extract(body,'$.g2_status') FROM objects WHERE kind='job' AND id IN (" + marks + ') ORDER BY id', ids)
        return [{'id': row[0], 'status': self._label(row[1]),
                 'g2_gate_version': row[2] if type(row[2]) is int else None,
                 'g2_status': self._label(row[3]), 'execution_enabled': False}
                for row in rows]

    def approvals(self, job_id=None):
        ids = self._scope(job_id)
        marks = ','.join('?' for _ in ids)
        rows = self.db.execute("SELECT id,json_extract(body,'$.job_id'),json_extract(body,'$.kind'),json_extract(body,'$.status') FROM objects WHERE kind='approval' AND json_extract(body,'$.job_id') IN (" + marks + ') ORDER BY id', ids)
        return [{'id': self._label(row[0]), 'job_id': row[1],
                 'kind': self._label(row[2]), 'status': self._label(row[3]),
                 'execution_enabled': False} for row in rows]

    def close(self):
        try:
            if self.db is not None:
                db, self.db = self.db, None
                db.close()
        finally:
            if self.lock is not None:
                lock, self.lock = self.lock, None
                lock.close()


class DiagnosticRuntime:
    __slots__ = ('manifest', 'snapshot')

    def __init__(self, manifest):
        self.manifest = manifest
        self.snapshot = ReadOnlySnapshot(manifest)

    @property
    def mode(self):
        return 'safe'

    def execute(self, *args, **kwargs):
        raise SafeStartError('execution_forbidden_in_safe_mode')

    resume = execute
    dispatch = execute
    retry = execute
    claim = execute

    def status(self):
        return {'mode': 'safe', 'engine': 'not_created', 'execution_enabled': False,
                'provider_auth': 'not_checked', 'provider_monitors': 'not_created',
                'snapshot_created_at': self.manifest.created_at,
                'snapshot_sha256': self.manifest.snapshot_sha256,
                'source_sha256': dict(self.manifest.source_hashes),
                'original_runtime_opened': False,
                'coverage': 'application guards; not an OS-wide isolation claim'}

    def close(self):
        self.snapshot.close()
