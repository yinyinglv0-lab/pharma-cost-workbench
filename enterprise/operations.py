"""Small, offline operations primitives for one organization and one instance.

Every logical mutation (SQL + blob + publication pointer) must share write_guard
on the same MANAGED_DIR. The OS lock coordinates processes on a local filesystem;
network filesystems and multiple replicas are outside this deployment contract.
Backup additionally requires stopped writers because legacy tools may not use it.
"""
from __future__ import annotations

from contextlib import contextmanager, closing
from datetime import datetime, timezone
from functools import wraps
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
import uuid
import zipfile

LOCK_FILE = '.operations.lock'
MAINTENANCE_FILE = '.maintenance.json'
RESTORE_REVIEW_FILE = '.restore-review-required.json'
FORMAT = 'project4-backup-v1'
CHUNK = 1024 * 1024
MAX_MANIFEST = 16 * CHUNK
MAX_ENTRIES = 100_000
DEFAULT_MAX_BYTES = 50 * 1024 ** 3
_DATA_DIRS = {'kb', 'data_upload', 'docs_upload', 'outputs', 'artifacts',
              'report_templates', 'templates', 'releases', 'index_releases'}
_DATA_SUFFIXES = {'.csv', '.xlsx', '.xls', '.pdf', '.docx', '.doc', '.txt',
                  '.json', '.db', '.sqlite', '.sqlite3'}
_SKIP_DIRS = {'.git', '.venv', '__pycache__', '.pytest_cache', 'logs', 'models',
              'backups', '.streamlit', '.local', 'secrets'}
_SECRET_NAMES = {'secrets.toml', 'authorization.json', 'credentials.json',
                 'id_rsa', 'id_ed25519', 'requirements.txt'}
_TRANSIENT_NAMES = {LOCK_FILE, MAINTENANCE_FILE}
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_MUTEX = threading.Lock()
_LOCAL = threading.local()
_LOG = logging.getLogger('project4.operations')


class OperationsError(RuntimeError):
    pass


class MaintenanceError(OperationsError):
    pass


class BackupError(OperationsError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _root(root=None):
    if root is None:
        from paths import MANAGED_DIR
        root = MANAGED_DIR
    return Path(root).resolve()


def _roots(data_root=None, managed_root=None):
    if data_root is None:
        from paths import DATA_DIR
        data_root = DATA_DIR
    data = Path(data_root).resolve()
    managed = _root(managed_root)
    if data == managed or data.is_relative_to(managed):
        raise OperationsError('Data root and managed root must be distinct; managed may be inside data.')
    return data, managed


def _json_write(path, value):
    """Replace a small metadata file atomically; no secret values are recorded."""
    path = Path(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _exclusive(root, timeout=30):
    """Thread-reentrant local OS advisory lock; never unlink a live lock file."""
    root = _root(root)
    key = os.path.normcase(str(root))
    with _LOCKS_MUTEX:
        mutex = _LOCKS.setdefault(key, threading.RLock())
    deadline = time.monotonic() + max(0, timeout)
    if not mutex.acquire(timeout=max(0, timeout)):
        raise MaintenanceError('Timed out waiting for an in-process writer.')
    held = getattr(_LOCAL, 'held', None)
    if held is None:
        held = _LOCAL.held = {}
    stream = None
    acquired = False
    try:
        if key in held:
            yield root
            return
        root.mkdir(parents=True, exist_ok=True)
        path = root / LOCK_FILE
        if path.is_symlink():
            raise MaintenanceError('Lock file must not be a symbolic link.')
        stream = path.open('a+b')
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        while True:
            try:
                if os.name == 'nt':
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise MaintenanceError('Timed out waiting for another process or maintenance.') from None
                time.sleep(min(.05, max(0, deadline - time.monotonic())))
        held[key] = True
        yield root
    finally:
        if acquired:
            held.pop(key, None)
            if os.name == 'nt':
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        if stream is not None:
            stream.close()
        mutex.release()


@contextmanager
def write_guard(root=None, timeout=30):
    """Guard a complete logical mutation, including schema initialization.

    Also usable as @write_guard() for mutations using the default managed root.
    Existing maintenance markers fail closed, including after a crashed backup.
    """
    with _exclusive(root, timeout) as managed:
        if (managed / MAINTENANCE_FILE).exists():
            raise MaintenanceError('Maintenance is active; writes are paused.')
        yield


def guarded_write(func=None, *, root=None):
    """Decorator for repository methods; infer self.root or use a root callable.

    @guarded_write uses self.root when present, otherwise MANAGED_DIR.
    @guarded_write(root=lambda self, *a, **kw: self.root) is explicit.
    """
    def decorate(method):
        @wraps(method)
        def call(*args, **kwargs):
            selected = root(*args, **kwargs) if callable(root) else root
            if selected is None and args:
                selected = getattr(args[0], 'root', None)
            with write_guard(selected):
                return method(*args, **kwargs)
        return call
    return decorate(func) if func is not None else decorate


@contextmanager
def maintenance_guard(root=None, timeout=30, reason='consistent-backup'):
    with _exclusive(root, timeout) as managed:
        marker = managed / MAINTENANCE_FILE
        if marker.exists():
            raise MaintenanceError('A maintenance marker already exists; inspect it with writers stopped.')
        operation = uuid.uuid4().hex
        _json_write(marker, {'operation_id': operation, 'started_at': _now(),
                             'pid': os.getpid(), 'reason': reason})
        try:
            yield
        finally:
            # An unexpected replacement is never silently removed.
            try:
                if json.loads(marker.read_text(encoding='utf-8')).get('operation_id') == operation:
                    marker.unlink()
            except (OSError, ValueError):
                pass


def clear_maintenance(root=None, *, writers_stopped=False):
    if not writers_stopped:
        raise MaintenanceError('Stop all writers before clearing an abandoned maintenance marker.')
    with _exclusive(root) as managed:
        (managed / MAINTENANCE_FILE).unlink(missing_ok=True)


def assert_dispatch_allowed(root=None):
    managed = _root(root)
    if (managed / RESTORE_REVIEW_FILE).exists():
        raise MaintenanceError('Restored tasks require external receipt reconciliation; dispatch is blocked.')
    if (managed / MAINTENANCE_FILE).exists():
        raise MaintenanceError('Maintenance is active; dispatch is blocked.')


def acknowledge_restore_review(root=None, *, actor, note, writers_stopped=False):
    """Record the operator's explicit reconciliation, never change task states.

    Call only after task-by-task receipt checks. This is an OS-admin CLI operation,
    not a network API. The task workflow must separately approve any new send.
    """
    if not writers_stopped or not str(actor).strip() or not str(note).strip():
        raise OperationsError('Stopped writers, an operator and a reconciliation note are required.')
    with _exclusive(root) as managed:
        marker = managed / RESTORE_REVIEW_FILE
        if not marker.is_file():
            raise OperationsError('There is no pending restore review.')
        report = json.loads(marker.read_text(encoding='utf-8'))
        report.update(reviewed_at=_now(), reviewed_by=str(actor).strip(),
                      reconciliation_note=str(note).strip())
        folder = managed / 'restore_reviews'
        folder.mkdir(exist_ok=True)
        _json_write(folder / (uuid.uuid4().hex + '.json'), report)
        marker.unlink()
        return {'reviewed_at': report['reviewed_at'], 'dispatch_gate_cleared': True,
                'task_states_unchanged': True}


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sqlite(path):
    with path.open('rb') as stream:
        return stream.read(16) == b'SQLite format 3\x00'


def _connect_ro(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)


def _check_sqlite(path):
    with closing(_connect_ro(path)) as connection:
        if connection.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise BackupError('SQLite integrity check failed.')
        if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise BackupError('SQLite foreign-key integrity check failed.')


def _immutable_sqlite(path):
    """Published index bytes are bound by the canonical manifest in the catalog DB.

    No manifest.json is required or trusted beside the artifact. Mutable catalog
    databases use online backup; this immutable index keeps its exact file hash.
    """
    if path.name != 'index.sqlite' or path.parent.parent.name != 'knowledge_releases':
        return False
    catalog = path.parent.parent.parent / 'knowledge_releases.db'
    if not catalog.is_file():
        return False
    try:
        with closing(_connect_ro(catalog)) as connection:
            row = connection.execute('SELECT status,manifest,manifest_sha256 FROM releases WHERE release_id=?',
                                     (path.parent.name,)).fetchone()
        if row is None or row[0] != 'published':
            return False
        if hashlib.sha256(row[1].encode('utf-8')).hexdigest() != row[2]:
            raise BackupError('Published release manifest hash mismatch.')
        manifest = json.loads(row[1])
        entry = manifest.get('artifacts', {}).get(path.name)
        if (manifest.get('release_id') != path.parent.name or not isinstance(entry, dict)
                or sha256_file(path) != entry.get('sha256')):
            raise BackupError('Published SQLite artifact hash mismatch.')
        for suffix in ('-wal', '-journal'):
            sidecar = path.with_name(path.name + suffix)
            if sidecar.exists() and sidecar.stat().st_size:
                raise BackupError('Published immutable SQLite artifact has a live journal/WAL.')
        _check_sqlite(path)
        return True
    except (OSError, ValueError, AttributeError, sqlite3.Error):
        raise BackupError('Published artifact manifest cannot be validated.') from None


def _safe_name(name):
    if not isinstance(name, str) or not name or '\\' in name or ':' in name or '\x00' in name:
        raise BackupError('Unsafe archive path.')
    pure = PurePosixPath(name)
    if pure.is_absolute() or name != pure.as_posix():
        raise BackupError('Unsafe archive path.')
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *('COM' + str(i) for i in range(1, 10)),
                *('LPT' + str(i) for i in range(1, 10))}
    for part in pure.parts:
        if part in ('', '.', '..') or part.endswith(('.', ' ')) or part.split('.')[0].upper() in reserved:
            raise BackupError('Unsafe archive path.')
    return pure


def _excluded(path):
    name = path.name.lower()
    for setting in ('COST_AUTHORIZATION_FILE', 'COST_LLM_CONFIG_FILE', 'COST_STREAMLIT_SECRETS'):
        secret_file = os.environ.get(setting)
        if secret_file and path.resolve() == Path(secret_file).resolve():
            return True
    return (name in _SECRET_NAMES or name in _TRANSIENT_NAMES or name == '.env'
            or name.startswith('.env.') or name.endswith(('.key', '.pem', '.p12', '.pfx'))
            or name.startswith(('credentials.', 'secrets.', 'authorization.')))


def _walk_files(root, *, excluded_root=None):
    if not root.exists():
        return
    for folder, directories, files in os.walk(root, followlinks=False):
        current = Path(folder)
        kept = []
        for name in sorted(directories):
            child = current / name
            if child.is_symlink() or (hasattr(child, 'is_junction') and child.is_junction()):
                raise BackupError('Symbolic links and junctions are not allowed in a backup root.')
            if name.lower() in _SKIP_DIRS or (excluded_root and child.resolve() == excluded_root):
                continue
            kept.append(name)
        directories[:] = kept
        for name in sorted(files):
            path = current / name
            if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                raise BackupError('Only regular files may be backed up.')
            if _excluded(path):
                continue
            # WAL/journal changes are captured through SQLite's online backup API.
            if path.name.endswith(('-wal', '-shm', '-journal')):
                suffix = next(x for x in ('-wal', '-shm', '-journal') if path.name.endswith(x))
                database = path.with_name(path.name[:-len(suffix)])
                if not database.is_file() or not _is_sqlite(database):
                    raise BackupError('Orphan SQLite sidecar found; recover the database before backup.')
                continue
            yield path


def _backup_inputs(data, managed):
    for path in _walk_files(managed):
        yield path, 'managed/' + path.relative_to(managed).as_posix()
    if not data.is_dir():
        raise BackupError('Data root does not exist.')
    for child in sorted(data.iterdir()):
        if child.resolve() == managed:
            continue
        if child.is_symlink() or (hasattr(child, 'is_junction') and child.is_junction()):
            raise BackupError('Symbolic links and junctions are not allowed in a backup root.')
        # The reserved local backup directory never becomes source input, even
        # if the business directory allowlist is expanded in a future release.
        if child.name.lower() in _SKIP_DIRS:
            continue
        if child.is_dir() and child.name in _DATA_DIRS:
            for path in _walk_files(child, excluded_root=managed):
                yield path, 'data/' + path.relative_to(data).as_posix()
        elif child.is_file() and child.suffix.lower() in _DATA_SUFFIXES and not _excluded(child):
            yield child, 'data/' + child.name


def create_backup(destination, *, data_root=None, managed_root=None, writers_stopped=False):
    """Create a new ZIP and SHA256 sidecar; never overwrite an existing archive.

    This command cannot prove that an uncooperative writer has stopped. The flag
    is an operator assertion that UI/API, workers, ingest and watch tools stopped.
    Targets may be outside data roots, or under the excluded DATA_DIR/backups
    directory. Targets inside MANAGED_DIR or through links are always forbidden.
    A local copy does not replace an independently stored offsite backup.
    """
    if not writers_stopped:
        raise BackupError('Stop API/UI, workers, ingest and watchers; explicitly confirm writers_stopped.')
    data, managed = _roots(data_root, managed_root)
    # Check the requested path before resolve(), so a linked backups directory
    # cannot disguise an internal destination as an allowed external location.
    destination = Path(destination).absolute()
    for item in (destination, *destination.parents):
        if item.is_symlink() or (hasattr(item, 'is_junction') and item.is_junction()):
            raise BackupError('Backup destination must not traverse symbolic links or junctions.')
    destination = destination.resolve()
    checksum = destination.with_name(destination.name + '.sha256')
    if destination.is_relative_to(managed):
        raise BackupError('Backup destination must be outside the managed root, including custom managed roots.')
    local_backups = data / 'backups'
    if destination.is_relative_to(data) and (destination == local_backups or not destination.is_relative_to(local_backups)):
        raise BackupError('Backup destination must be outside the data root or inside its excluded backups directory.')
    if destination.exists() or checksum.exists():
        raise BackupError('Backup archive or checksum already exists.')
    if not destination.parent.is_dir():
        raise BackupError('Create the backup destination directory first.')
    started = _now()
    entries = []
    with maintenance_guard(managed):
        if inspect_managed_integrity(managed, deep=True)['errors']:
            raise BackupError('Managed data integrity failed; investigate before backup.')
        with tempfile.TemporaryDirectory(prefix='.project4-backup-', dir=destination.parent) as temp:
            temp = Path(temp)
            candidate = temp / 'backup.zip'
            seen = set()
            with zipfile.ZipFile(candidate, 'w', compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                for source, name in _backup_inputs(data, managed):
                    _safe_name(name)
                    if name.casefold() in seen:
                        raise BackupError('Archive contains case-insensitive filename collisions.')
                    seen.add(name.casefold())
                    kind = 'file'
                    copied = source
                    sqlite_file = _is_sqlite(source)
                    if source.suffix.lower() in ('.db', '.sqlite', '.sqlite3') and not sqlite_file:
                        raise BackupError('A database has an invalid SQLite header; backup refused.')
                    if sqlite_file:
                        kind = 'sqlite_immutable' if _immutable_sqlite(source) else 'sqlite'
                        if kind == 'sqlite':
                            copied = temp / ('sqlite-' + uuid.uuid4().hex)
                            with closing(_connect_ro(source)) as src, closing(sqlite3.connect(copied)) as dst:
                                src.backup(dst, pages=256, sleep=.05)
                            _check_sqlite(copied)
                    before = copied.stat()
                    digest = hashlib.sha256()
                    size = 0
                    with copied.open('rb') as stream, archive.open(name, 'w', force_zip64=True) as out:
                        for chunk in iter(lambda: stream.read(CHUNK), b''):
                            out.write(chunk)
                            digest.update(chunk)
                            size += len(chunk)
                    after = copied.stat()
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or size != before.st_size:
                        raise BackupError('A source changed during backup; stop all writers and retry.')
                    entries.append({'path': name, 'size': size, 'sha256': digest.hexdigest(),
                                    'kind': kind, 'mtime_ns': source.stat().st_mtime_ns})
                    if kind == 'sqlite':
                        copied.unlink()
                manifest = {'format': FORMAT, 'backup_id': uuid.uuid4().hex, 'started_at': started,
                            'finished_at': _now(), 'consistency': 'stopped-writers+shared-os-lock+sqlite-online-backup',
                            'tenant_id': os.environ.get('COST_TENANT_ID', 'default'),
                            'roots': ['data', 'managed'], 'entries': entries,
                            'exclusions': ['code', 'secrets', 'models', 'operational_logs', 'prior_backups'],
                            'restore_requires_review': True}
                archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, sort_keys=True))
            verify_backup(candidate)
            # Exclusive creation, including a race with a second administrator.
            made = False
            try:
                descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                made = True
                with os.fdopen(descriptor, 'wb') as out, candidate.open('rb') as stream:
                    shutil.copyfileobj(stream, out, CHUNK)
                    out.flush()
                    os.fsync(out.fileno())
                digest = sha256_file(destination)
                descriptor = os.open(checksum, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, 'w', encoding='ascii') as stream:
                    stream.write(digest + '\n')
            except BaseException:
                if made:
                    destination.unlink(missing_ok=True)
                raise
    return {'backup_id': manifest['backup_id'], 'files': len(entries),
            'bytes': sum(entry['size'] for entry in entries), 'archive_sha256': digest,
            'finished_at': manifest['finished_at']}


def _manifest(archive, max_bytes):
    infos = archive.infolist()
    if not infos or len(infos) > MAX_ENTRIES:
        raise BackupError('Archive entry count is invalid.')
    seen = set()
    total = 0
    for info in infos:
        _safe_name(info.filename)
        folded = info.filename.casefold()
        if folded in seen:
            raise BackupError('Duplicate archive entry.')
        seen.add(folded)
        mode = info.external_attr >> 16
        if info.is_dir() or (stat.S_IFMT(mode) and not stat.S_ISREG(mode)) or info.flag_bits & 1:
            raise BackupError('Archive entries must be unencrypted regular files.')
        total += info.file_size
        if total > max_bytes:
            raise BackupError('Archive exceeds the configured uncompressed size limit.')
    try:
        info = archive.getinfo('manifest.json')
        if info.file_size > MAX_MANIFEST:
            raise BackupError('Backup manifest is too large.')
        manifest = json.loads(archive.read(info))
    except (KeyError, ValueError, UnicodeDecodeError):
        raise BackupError('Backup manifest is missing or malformed.') from None
    if not isinstance(manifest, dict) or manifest.get('format') != FORMAT or manifest.get('roots') != ['data', 'managed']:
        raise BackupError('Unsupported backup format.')
    if (not re.fullmatch('[0-9a-f]{32}', str(manifest.get('backup_id', '')))
            or manifest.get('tenant_id') != 'default' or manifest.get('restore_requires_review') is not True):
        raise BackupError('Backup identity, organization or restore policy is invalid.')
    entries = manifest.get('entries')
    if not isinstance(entries, list) or len(entries) != len(infos) - 1:
        raise BackupError('Manifest does not describe every archive entry.')
    expected = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise BackupError('Invalid manifest entry.')
        path = _safe_name(entry.get('path'))
        if len(path.parts) < 2 or path.parts[0] not in ('data', 'managed'):
            raise BackupError('Unexpected root in manifest.')
        if path.name in _TRANSIENT_NAMES:
            raise BackupError('Transient lock metadata must not be restored.')
        name = path.as_posix()
        if name.casefold() in expected or name.casefold() not in seen:
            raise BackupError('Manifest entry mismatch.')
        expected.add(name.casefold())
        size = entry.get('size')
        if type(size) is not int or size < 0 or size != archive.getinfo(name).file_size:
            raise BackupError('Manifest file size mismatch.')
        if 'mtime_ns' in entry and (type(entry['mtime_ns']) is not int or not 0 <= entry['mtime_ns'] < 2 ** 63):
            raise BackupError('Manifest timestamp is invalid.')
        if not re.fullmatch('[0-9a-f]{64}', str(entry.get('sha256', ''))) or entry.get('kind') not in ('file', 'sqlite', 'sqlite_immutable'):
            raise BackupError('Manifest hash or file kind is invalid.')
    if expected | {'manifest.json'} != seen:
        raise BackupError('Archive has unlisted files.')
    return manifest


def verify_backup(archive_path, *, expected_sha256=None, max_bytes=DEFAULT_MAX_BYTES):
    """Validate every entry before any extraction; the checksum is not a signature."""
    path = Path(archive_path)
    try:
        if expected_sha256 is not None:
            if not re.fullmatch('[0-9a-f]{64}', expected_sha256) or sha256_file(path) != expected_sha256:
                raise BackupError('Archive SHA256 does not match the supplied trusted digest.')
        with zipfile.ZipFile(path) as archive:
            manifest = _manifest(archive, max_bytes)
            for entry in manifest['entries']:
                digest = hashlib.sha256()
                size = 0
                with archive.open(entry['path']) as stream:
                    for chunk in iter(lambda: stream.read(CHUNK), b''):
                        size += len(chunk)
                        if size > entry['size']:
                            raise BackupError('Expanded file exceeds its manifest size.')
                        digest.update(chunk)
                if size != entry['size'] or digest.hexdigest() != entry['sha256']:
                    raise BackupError('Backup file SHA256 mismatch; restore refused.')
        return manifest
    except (zipfile.BadZipFile, KeyError, OSError, RuntimeError) as exc:
        if isinstance(exc, BackupError):
            raise
        raise BackupError('Backup cannot be read or is corrupt (' + type(exc).__name__ + ').') from None


def _task_counts(managed):
    path = managed / 'task_workflow.db'
    if not path.is_file():
        return {}
    with closing(_connect_ro(path)) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'outbox' not in tables:
            return {}
        return dict(connection.execute('SELECT status, COUNT(*) FROM outbox GROUP BY status'))


def _quarantine_outbox(managed):
    """Preserve checked enum values; a persistent root gate blocks every sender.

    GET receipt synchronization remains available for reconciliation. The operator
    must account for remote effects newer than the restored local database.
    """
    counts = _task_counts(managed)
    return {'updated_rows': 0, 'gate': 'all_dispatch_blocked', 'status_counts': counts,
            'needs_review_count': sum(counts.get(status, 0) for status in ('blocked', 'pending', 'retry', 'unknown', 'leased')),
            'review_status': 'needs_review', 'task_states_preserved': True}


def restore_backup(archive_path, target_root, *, expected_sha256=None, max_bytes=DEFAULT_MAX_BYTES):
    """Restore into a NEW/EMPTY root with data/ and managed/ children.

    No dispatch is performed. The root review gate always starts closed. Production
    is never overwritten; switch configured roots only after an isolated rehearsal.
    """
    target = Path(target_root).absolute()
    if (target.is_symlink() or (hasattr(target, 'is_junction') and target.is_junction())
            or (target.exists() and (not target.is_dir() or any(target.iterdir())))):
        raise BackupError('Restore target must be a new or empty directory.')
    if not target.parent.is_dir() or target.parent.resolve() != target.parent:
        raise BackupError('Restore parent must exist and must not traverse symbolic links.')
    manifest = verify_backup(archive_path, expected_sha256=expected_sha256, max_bytes=max_bytes)
    temporary = Path(tempfile.mkdtemp(prefix='.project4-restore-', dir=target.parent))
    try:
        (temporary / 'data').mkdir()
        (temporary / 'managed').mkdir()
        with zipfile.ZipFile(archive_path) as archive:
            for entry in manifest['entries']:
                destination = temporary.joinpath(*PurePosixPath(entry['path']).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                size = 0
                with destination.open('xb') as out, archive.open(entry['path']) as stream:
                    for chunk in iter(lambda: stream.read(CHUNK), b''):
                        size += len(chunk)
                        if size > entry['size']:
                            raise BackupError('Archive changed while restoring.')
                        digest.update(chunk)
                        out.write(chunk)
                if digest.hexdigest() != entry['sha256'] or size != entry['size']:
                    raise BackupError('Archive changed while restoring.')
                if entry['kind'] in ('sqlite', 'sqlite_immutable'):
                    _check_sqlite(destination)
                if 'mtime_ns' in entry:
                    os.utime(destination, ns=(entry['mtime_ns'], entry['mtime_ns']))
        if inspect_managed_integrity(temporary / 'managed', deep=True)['errors']:
            raise BackupError('Restored registered data hashes failed; target was not published.')
        quarantine = _quarantine_outbox(temporary / 'managed')
        report = {'backup_id': manifest['backup_id'], 'restored_at': _now(),
                  'archive_sha256': sha256_file(archive_path), 'dispatch_blocked': True,
                  'outbox': quarantine, 'action': 'Reconcile every pending/unknown external receipt before approval.'}
        _json_write(temporary / 'managed' / RESTORE_REVIEW_FILE, report)
        _json_write(temporary / 'restore_manifest.json', manifest)
        _json_write(temporary / 'restore_report.json', report)
        if target.exists():
            target.rmdir()  # Fails safely if another process populated it meanwhile.
        temporary.rename(target)
        return report
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


_SENSITIVE = re.compile(r'(secret|password|passwd|token|api.?key|authorization|cookie|prompt|content|payload|email)', re.I)


def redact(value, key=''):
    """Best-effort defense for ops metadata; never log request bodies/SQL values."""
    if _SENSITIVE.search(str(key)):
        return '[REDACTED]'
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, key) for item in value]
    if isinstance(value, str):
        value = re.sub(r'(?i)Bearer\s+[^\s,;]+', 'Bearer [REDACTED]', value)
        value = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b', '[REDACTED]', value)
        value = re.sub(r'(?i)((?:secret|password|token|api_key|key)\s*[=:]\s*)[^\s&;,]+', r'\1[REDACTED]', value)
        value = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[REDACTED]', value)
        return value[:2000]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return type(value).__name__


def log_operation(event, **fields):
    if not re.fullmatch(r'[a-z][a-z0-9_.-]{0,80}', event):
        raise ValueError('Operational event names must be fixed identifiers.')
    _LOG.info(json.dumps({'ts': _now(), 'event': event, **redact(fields)}, ensure_ascii=False))


def operations_metrics(*, data_root=None, managed_root=None):
    """Read-only aggregates suitable for a protected monitoring endpoint/CLI."""
    data, managed = _roots(data_root, managed_root)
    disk_root = data
    while not disk_root.exists():
        disk_root = disk_root.parent
    usage = shutil.disk_usage(disk_root)
    metrics = {'timestamp': _now(), 'disk_free_bytes': usage.free,
               'disk_total_bytes': usage.total, 'managed_bytes': 0,
               'sqlite_bytes': 0, 'sqlite_wal_bytes': 0, 'sqlite_databases': 0,
               'maintenance_active': (managed / MAINTENANCE_FILE).exists(),
               'restore_review_required': (managed / RESTORE_REVIEW_FILE).exists()}
    if managed.is_dir():
        for folder, directories, files in os.walk(managed, followlinks=False):
            directories[:] = [name for name in directories if not (Path(folder) / name).is_symlink()]
            for name in files:
                path = Path(folder) / name
                if path.is_symlink() or not path.is_file():
                    continue
                size = path.stat().st_size
                metrics['managed_bytes'] += size
                if name.endswith('-wal'):
                    metrics['sqlite_wal_bytes'] += size
                elif path.suffix in ('.db', '.sqlite', '.sqlite3'):
                    metrics['sqlite_bytes'] += size
                    metrics['sqlite_databases'] += 1
    try:
        metrics['outbox_status_counts'] = _task_counts(managed)
    except (sqlite3.Error, OSError):
        metrics['outbox_status_counts'] = None
    return metrics


def inspect_managed_integrity(root=None, *, deep=False):
    """Read-only database/registered-blob checks; never initializes repositories."""
    managed = _root(root)
    results = {'databases_checked': 0, 'blobs_checked': 0, 'payloads_checked': 0, 'errors': []}
    if not managed.exists():
        return results
    for path in _walk_files(managed):
        if path.parent.name == 'blobs' and re.fullmatch('[0-9a-f]{64}', path.name) and deep:
            results['blobs_checked'] += 1
            if sha256_file(path) != path.name:
                results['errors'].append('blob_hash_mismatch')
        if not _is_sqlite(path):
            if path.suffix in ('.db', '.sqlite', '.sqlite3'):
                results['errors'].append('invalid_sqlite_header')
            continue
        try:
            _check_sqlite(path)
            if deep:
                _immutable_sqlite(path)
            results['databases_checked'] += 1
            with closing(_connect_ro(path)) as connection:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if path.name == 'knowledge.db' and 'versions' in tables:
                    for (sha,) in connection.execute('SELECT DISTINCT sha256 FROM versions'):
                        if not re.fullmatch('[0-9a-f]{64}', str(sha)) or not (managed / 'blobs' / sha).is_file():
                            results['errors'].append('registered_blob_missing')
                if deep and path.name == 'knowledge.db' and 'versions' in tables:
                    columns = {row[1] for row in connection.execute('PRAGMA table_info(versions)')}
                    if {'text', 'text_sha256'} <= columns:
                        for text, sha in connection.execute('SELECT text,text_sha256 FROM versions'):
                            results['payloads_checked'] += 1
                            if hashlib.sha256(text.encode('utf-8')).hexdigest() != sha:
                                results['errors'].append('knowledge_text_hash_mismatch')
                if deep and path.name == 'cost_versions.db' and 'revisions' in tables:
                    for sha, text in connection.execute('SELECT hash,data FROM revisions'):
                        results['payloads_checked'] += 1
                        if hashlib.sha256(text.encode('utf-8')).hexdigest() != sha:
                            results['errors'].append('cost_revision_hash_mismatch')
                if deep and 'source_files' in tables:
                    for sha, content in connection.execute('SELECT hash,content FROM source_files'):
                        results['payloads_checked'] += 1
                        if hashlib.sha256(content).hexdigest() != sha:
                            results['errors'].append('source_file_hash_mismatch')
                if deep and path.name == 'reports.db' and 'reports' in tables:
                    for sha, text in connection.execute('SELECT hash,payload FROM reports'):
                        payload = json.loads(text)
                        saved = payload.pop('frozen_hash', None)
                        actual = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                                separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
                        results['payloads_checked'] += 1
                        if actual != sha or saved != sha:
                            results['errors'].append('report_payload_hash_mismatch')
                if deep and 'report_artifacts' in tables:
                    for sha, content in connection.execute('SELECT hash,content FROM report_artifacts'):
                        results['payloads_checked'] += 1
                        if hashlib.sha256(content).hexdigest() != sha:
                            results['errors'].append('report_artifact_hash_mismatch')
                if deep and 'snapshots' in tables:
                    for sha, content in connection.execute('SELECT hash,payload FROM snapshots'):
                        results['payloads_checked'] += 1
                        if hashlib.sha256(content.encode('utf-8')).hexdigest() != sha:
                            results['errors'].append('snapshot_hash_mismatch')
                if deep and path.name == 'knowledge_releases.db' and 'releases' in tables:
                    for release_id, text, sha in connection.execute("SELECT release_id,manifest,manifest_sha256 FROM releases WHERE status='published'"):
                        results['payloads_checked'] += 1
                        if hashlib.sha256(text.encode('utf-8')).hexdigest() != sha:
                            results['errors'].append('release_manifest_hash_mismatch')
                        release_path = managed / 'knowledge_releases' / release_id
                        if not release_path.resolve().is_relative_to(managed / 'knowledge_releases'):
                            results['errors'].append('release_path_invalid')
                            continue
                        if not _immutable_sqlite(release_path / 'index.sqlite'):
                            results['errors'].append('release_artifact_missing')
        except (sqlite3.Error, OperationsError, OSError, ValueError, TypeError, AttributeError):
            results['errors'].append('sqlite_integrity_error')
    return results
