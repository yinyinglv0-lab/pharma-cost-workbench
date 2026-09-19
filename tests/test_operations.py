"""Operations tests use only tmp_path databases/blobs; never the running stores."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import zipfile

import pytest

from enterprise.operations import (BackupError, MaintenanceError, RESTORE_REVIEW_FILE,
    acknowledge_restore_review, assert_dispatch_allowed, clear_maintenance,
    create_backup, guarded_write, inspect_managed_integrity, maintenance_guard,
    operations_metrics, redact, restore_backup, sha256_file, verify_backup, write_guard)


@pytest.fixture
def roots(tmp_path):
    data, managed = tmp_path / 'data', tmp_path / 'data' / 'managed'
    managed.mkdir(parents=True)
    (data / 'artifacts' / 'reports').mkdir(parents=True)
    (data / 'artifacts' / 'reports' / 'report.pdf').write_bytes(b'synthetic-report')
    (managed / 'blobs').mkdir()
    content = b'synthetic-original'
    sha = hashlib.sha256(content).hexdigest()
    (managed / 'blobs' / sha).write_bytes(content)
    with closing(sqlite3.connect(managed / 'knowledge.db')) as con, con:
        con.execute('CREATE TABLE versions(version_id TEXT PRIMARY KEY, sha256 TEXT)')
        con.execute('INSERT INTO versions VALUES(?,?)', ('v1', sha))
    with closing(sqlite3.connect(managed / 'snapshots.db')) as con, con:
        con.execute('CREATE TABLE snapshots(hash TEXT, payload TEXT)')
        con.execute('INSERT INTO snapshots VALUES(?,?)', (hashlib.sha256(b'{}').hexdigest(), '{}'))
    (managed / 'releases' / 'r1').mkdir(parents=True)
    (managed / 'releases' / 'r1' / 'manifest.json').write_text('{"release":"r1"}', encoding='utf-8')
    (data / 'input.csv').write_text('month,amount\n2026-01,1\n', encoding='utf-8')
    return data, managed


def make_backup(tmp_path, roots):
    archive = tmp_path / 'backup.zip'
    result = create_backup(archive, data_root=roots[0], managed_root=roots[1], writers_stopped=True)
    return archive, result


def test_backup_restores_multiple_sqlites_blob_release_and_report(tmp_path, roots):
    archive, result = make_backup(tmp_path, roots)
    manifest = verify_backup(archive, expected_sha256=result['archive_sha256'])
    assert len([x for x in manifest['entries'] if x['kind'] == 'sqlite']) == 2
    target = tmp_path / 'restore'
    report = restore_backup(archive, target, expected_sha256=result['archive_sha256'])
    assert report['dispatch_blocked'] is True
    assert (target / 'data/artifacts/reports/report.pdf').read_bytes() == b'synthetic-report'
    assert (target / 'managed/releases/r1/manifest.json').is_file()
    assert inspect_managed_integrity(target / 'managed', deep=True)['errors'] == []
    with pytest.raises(MaintenanceError, match='receipt reconciliation'):
        assert_dispatch_allowed(target / 'managed')
    acknowledgement = acknowledge_restore_review(target / 'managed', actor='test-operator',
        note='offline rehearsal; no sends', writers_stopped=True)
    assert acknowledgement['task_states_unchanged']
    assert_dispatch_allowed(target / 'managed')
    assert list((target / 'managed/restore_reviews').glob('*.json'))


def test_published_sqlite_preserves_manifest_byte_hash(tmp_path, roots):
    folder = roots[1] / 'knowledge_releases/r1'
    folder.mkdir(parents=True)
    artifact = folder / 'index.sqlite'
    with closing(sqlite3.connect(artifact)) as connection, connection:
        connection.execute('CREATE TABLE chunks(id INTEGER PRIMARY KEY, text TEXT)')
        connection.execute('INSERT INTO chunks(text) VALUES(?)', ('published-text',))
    expected = sha256_file(artifact)
    manifest = json.dumps({'release_id': 'r1', 'artifacts': {'index.sqlite': {'sha256': expected}}}, sort_keys=True, separators=(',', ':'))
    with closing(sqlite3.connect(roots[1] / 'knowledge_releases.db')) as connection, connection:
        connection.execute('CREATE TABLE releases(release_id TEXT, status TEXT, manifest TEXT, manifest_sha256 TEXT)')
        connection.execute('INSERT INTO releases VALUES(?,?,?,?)', ('r1', 'published', manifest, hashlib.sha256(manifest.encode()).hexdigest()))
    original = roots[0] / 'input.csv'
    os.utime(original, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
    archive, _ = make_backup(tmp_path, roots)
    restored = tmp_path / 'restored'
    restore_backup(archive, restored)
    assert sha256_file(restored / 'managed/knowledge_releases/r1/index.sqlite') == expected
    assert (restored / 'data/input.csv').stat().st_mtime_ns == original.stat().st_mtime_ns
    assert next(x for x in verify_backup(archive)['entries'] if x['path'].endswith('index.sqlite'))['kind'] == 'sqlite_immutable'


def test_real_knowledge_release_restore_remains_searchable(tmp_path):
    from enterprise.knowledge import Repository
    from enterprise.knowledge_release import ReleaseRepository, ControlledSearchEngine
    from enterprise.security import Principal
    admin = Principal('ops-test', 'Ops test', ('knowledge_admin',), ('*',), ('*',))
    data, managed = tmp_path / 'data', tmp_path / 'managed'
    data.mkdir()
    repository = Repository(managed, principal=admin)
    stage = repository.stage('甲产品 提取收率 工艺资料'.encode('utf-8'), filename='synthetic.txt',
        title='工艺', scope_products=['甲产品'], scope_factories=['一厂'], visibility='scoped',
        effective_from='2026-01-01', category='工艺', actor=admin.user_id, principal=admin)
    version = repository.commit(stage['stage_id'], admin.user_id, 'synthetic test', principal=admin)
    release = ReleaseRepository(repository=repository).publish(principal=admin, embedding_model_path='')
    assert release['status'] == 'published'
    archive, _ = make_backup(tmp_path, (data, managed))
    restored = tmp_path / 'restored'
    restore_backup(archive, restored)
    restored_repo = Repository(restored / 'managed', principal=admin)
    rows, stats = ControlledSearchEngine(repository=restored_repo, embedding_model_path='').search(
        '提取收率', principal=admin, product='甲产品', factory='一厂', as_of='2026-06-30')
    assert rows and rows[0]['version_id'] == version['version_id']
    assert set(stats['route_generations'].values()) == {release['release_id']}
    assert inspect_managed_integrity(restored / 'managed', deep=True)['errors'] == []


def test_backup_rejects_registered_corrupt_blob(tmp_path, roots):
    next((roots[1] / 'blobs').iterdir()).write_bytes(b'tampered')
    with pytest.raises(BackupError, match='integrity failed'):
        make_backup(tmp_path, roots)
    assert not (tmp_path / 'backup.zip').exists()


def test_online_backup_includes_committed_wal(tmp_path, roots):
    database = roots[1] / 'wal.db'
    with closing(sqlite3.connect(database)) as connection:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA wal_autocheckpoint=0')
        connection.execute('CREATE TABLE values_table(value TEXT)')
        connection.commit()
        connection.execute('INSERT INTO values_table VALUES(?)', ('only-in-wal',))
        connection.commit()
        assert database.with_name('wal.db-wal').stat().st_size > 0
        archive, _ = make_backup(tmp_path, roots)
    target = tmp_path / 'restored'
    restore_backup(archive, target)
    with closing(sqlite3.connect(target / 'managed/wal.db')) as restored:
        assert restored.execute('SELECT value FROM values_table').fetchone() == ('only-in-wal',)
    assert not any(x['path'].endswith(('-wal', '-shm')) for x in verify_backup(archive)['entries'])


def test_backup_requires_stopped_writers_and_refuses_overwrite(tmp_path, roots):
    with pytest.raises(BackupError, match='Stop API'):
        create_backup(tmp_path / 'unsafe.zip', data_root=roots[0], managed_root=roots[1])
    archive, _ = make_backup(tmp_path, roots)
    with pytest.raises(BackupError, match='already exists'):
        create_backup(archive, data_root=roots[0], managed_root=roots[1], writers_stopped=True)
    with pytest.raises(BackupError, match='outside'):
        create_backup(roots[0] / 'nested.zip', data_root=roots[0], managed_root=roots[1], writers_stopped=True)


def test_internal_backups_directory_is_excluded_from_repeated_archives(tmp_path, roots):
    data, managed = roots
    backup_dir = data / 'backups'
    backup_dir.mkdir()
    # Exclusion applies to every file type, not merely ZIP suffixes.
    (backup_dir / 'old-manifest.json').write_text('{"private_backup_metadata":true}', encoding='utf-8')
    first = backup_dir / 'first.zip'
    create_backup(first, data_root=data, managed_root=managed, writers_stopped=True)
    second_dir = backup_dir / 'daily'
    second_dir.mkdir()
    second = second_dir / 'second.zip'
    create_backup(second, data_root=data, managed_root=managed, writers_stopped=True)
    for archive in (first, second):
        names = {entry['path'] for entry in verify_backup(archive)['entries']}
        assert 'data/input.csv' in names
        assert not any('backups' in Path(name).parts or name.endswith('.zip') for name in names)
        assert not any('project4-backup-' in name for name in names)
    restore_backup(second, tmp_path / 'internal-restored')
    assert not (tmp_path / 'internal-restored/data/backups').exists()
    from enterprise.operations import _walk_files
    assert not any(path.is_relative_to(backup_dir) for path in _walk_files(data))


@pytest.mark.parametrize('target_kind', ['ordinary_data', 'custom_managed', 'managed_under_backups', 'similar_prefix', 'backup_root_itself'])
def test_local_backup_exception_never_admits_other_data_or_managed_paths(tmp_path, target_kind):
    data = tmp_path / 'data'
    data.mkdir()
    managed = data / ('backups' if target_kind == 'managed_under_backups' else 'custom-managed')
    managed.mkdir()
    if target_kind == 'backup_root_itself':
        destination = data / 'backups'
    elif target_kind == 'similar_prefix':
        destination = data / 'backups-other/copy.zip'
    else:
        destination = (data / 'artifacts' if target_kind == 'ordinary_data' else managed / 'backups') / 'copy.zip'
    destination.parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(BackupError, match='outside'):
        create_backup(destination, data_root=data, managed_root=managed, writers_stopped=True)
    assert not destination.exists()


@pytest.mark.parametrize('link_kind', ['symlink', 'junction'])
@pytest.mark.parametrize('nested', [False, True])
def test_backup_destination_rejects_linked_local_backup_directories(tmp_path, roots, link_kind, nested):
    data, managed = roots
    external = tmp_path / 'external'
    external.mkdir()
    (external / 'keep').write_bytes(b'unchanged')
    link = data / 'backups'
    if nested:
        link.mkdir()
        link /= 'linked-child'
    if link_kind == 'junction':
        if os.name != 'nt':
            pytest.skip('Windows junction test')
        created = subprocess.run(['cmd', '/d', '/c', 'mklink', '/J', str(link), str(external)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        if created.returncode:
            pytest.skip('Directory junction creation unavailable')
    else:
        try:
            link.symlink_to(external, target_is_directory=True)
        except OSError:
            pytest.skip('Directory symlink creation unavailable')
    try:
        with pytest.raises(BackupError, match='symbolic links or junctions'):
            create_backup(link / 'copy.zip', data_root=data, managed_root=managed, writers_stopped=True)
        assert not (external / 'copy.zip').exists()
        assert (external / 'keep').read_bytes() == b'unchanged'
    finally:
        if link_kind == 'junction':
            link.rmdir()
        else:
            link.unlink()


def test_secret_code_and_model_files_are_excluded(tmp_path, roots, monkeypatch):
    data, managed = roots
    for name in ('.env', 'authorization.json', 'secrets.toml', 'requirements.txt'):
        (managed / name).write_text('private', encoding='utf-8')
    (managed / '.local').mkdir()
    (managed / '.local/llm.json').write_text('{"api_key":"private"}', encoding='utf-8')
    (data / '.local').mkdir()
    (data / '.local/llm.json').write_text('{"api_key":"private"}', encoding='utf-8')
    (data / 'key-setting.json').write_text('{"api_key":"private"}', encoding='utf-8')
    monkeypatch.setenv('COST_LLM_CONFIG_FILE', str(data / 'key-setting.json'))
    (data / 'main.py').write_text('private_code = 1', encoding='utf-8')
    (data / 'models').mkdir()
    (data / 'models/weight.bin').write_bytes(b'weight')
    archive, _ = make_backup(tmp_path, roots)
    names = {x['path'] for x in verify_backup(archive)['entries']}
    assert not any('llm.json' in name or 'key-setting' in name or 'main.py' in name or 'weight.bin' in name for name in names)
    assert not any(Path(name).name in ('.env', 'authorization.json', 'secrets.toml', 'requirements.txt') for name in names)


def test_corrupted_content_and_checksum_are_rejected(tmp_path, roots):
    archive, _ = make_backup(tmp_path, roots)
    corrupted = tmp_path / 'corrupt.zip'
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(corrupted, 'w') as destination:
        for entry in source.infolist():
            value = source.read(entry)
            if entry.filename.endswith('report.pdf'):
                value = value[:-1] + b'!'
            destination.writestr(entry.filename, value)
    with pytest.raises(BackupError, match='SHA256 mismatch'):
        verify_backup(corrupted)
    with pytest.raises(BackupError, match='trusted digest'):
        verify_backup(archive, expected_sha256='0' * 64)
    with pytest.raises(BackupError):
        restore_backup(corrupted, tmp_path / 'new-root')
    assert not (tmp_path / 'new-root').exists()


@pytest.mark.parametrize('entry', ['../escaped.txt', 'managed/../../escaped.txt', '/absolute.txt',
    'C:/escaped.txt', 'managed\\..\\escaped.txt', 'managed/CON', 'managed/x. ',
    'managed//duplicate', 'managed/./hidden'])
def test_restore_rejects_zip_traversal(tmp_path, entry):
    archive = tmp_path / 'attack.zip'
    with zipfile.ZipFile(archive, 'w') as stream:
        stream.writestr(entry, b'attack')
        stream.writestr('manifest.json', '{}')
    with pytest.raises(BackupError):
        restore_backup(archive, tmp_path / 'destination')
    assert not (tmp_path / 'destination').exists()
    assert not (tmp_path / 'escaped.txt').exists()


def test_restore_rejects_duplicate_symlink_and_size_bomb(tmp_path, roots):
    archive, _ = make_backup(tmp_path, roots)
    with pytest.raises(BackupError, match='size limit'):
        verify_backup(archive, max_bytes=1)
    linked = tmp_path / 'symlink.zip'
    info = zipfile.ZipInfo('managed/link')
    info.create_system = 3
    info.external_attr = (0o120777 << 16)
    with zipfile.ZipFile(linked, 'w') as stream:
        stream.writestr(info, '/etc/passwd')
    with pytest.raises(BackupError, match='regular files'):
        restore_backup(linked, tmp_path / 'destination')
    duplicate = tmp_path / 'duplicate.zip'
    with zipfile.ZipFile(duplicate, 'w') as stream:
        stream.writestr('managed/File', 'a')
        stream.writestr('managed/file', 'b')
    with pytest.raises(BackupError, match='Duplicate'):
        verify_backup(duplicate)


def test_restore_never_overwrites_existing_root(tmp_path, roots):
    archive, _ = make_backup(tmp_path, roots)
    existing = tmp_path / 'production'
    existing.mkdir()
    (existing / 'keep').write_bytes(b'current-production')
    with pytest.raises(BackupError, match='new or empty'):
        restore_backup(archive, existing)
    assert (existing / 'keep').read_bytes() == b'current-production'
    empty = tmp_path / 'empty'
    empty.mkdir()
    restore_backup(archive, empty)
    assert (empty / 'managed' / RESTORE_REVIEW_FILE).exists()


def test_unknown_outbox_schema_is_preserved_but_dispatch_blocked(tmp_path, roots):
    with closing(sqlite3.connect(roots[1] / 'tasks.db')) as connection, connection:
        connection.execute("CREATE TABLE outbox(status TEXT CHECK(status IN ('pending','delivered')))")
        connection.executemany('INSERT INTO outbox VALUES(?)', [('pending',), ('delivered',)])
    archive, _ = make_backup(tmp_path, roots)
    restored = tmp_path / 'restored'
    restore_backup(archive, restored)
    with closing(sqlite3.connect(restored / 'managed/tasks.db')) as connection:
        assert connection.execute('SELECT status FROM outbox').fetchall() == [('pending',), ('delivered',)]
    with pytest.raises(MaintenanceError):
        assert_dispatch_allowed(restored / 'managed')


def test_write_guard_reentrant_decorator_and_stale_maintenance(tmp_path):
    root = tmp_path / 'managed'
    class Repo:
        def __init__(self):
            self.root = root
        @guarded_write
        def mutate(self):
            with write_guard(self.root):
                return 7
    assert Repo().mutate() == 7
    with maintenance_guard(root):
        with pytest.raises(MaintenanceError, match='Maintenance'):
            Repo().mutate()
    assert Repo().mutate() == 7
    (root / '.maintenance.json').write_text('{}', encoding='utf-8')
    with pytest.raises(MaintenanceError):
        Repo().mutate()
    with pytest.raises(MaintenanceError):
        clear_maintenance(root)
    clear_maintenance(root, writers_stopped=True)
    assert Repo().mutate() == 7


def test_os_lock_blocks_another_process(tmp_path):
    root = tmp_path / 'managed'
    script = "from enterprise.operations import write_guard, MaintenanceError\nimport sys\ntry:\n with write_guard(sys.argv[1], timeout=.1): pass\nexcept MaintenanceError:\n sys.exit(7)\n"
    with write_guard(root):
        result = subprocess.run([sys.executable, '-B', '-c', script, str(root)],
            cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    assert result.returncode == 7


def test_integrity_checks_detect_missing_and_corrupt_blob(roots):
    managed = roots[1]
    blob = next((managed / 'blobs').iterdir())
    blob.write_bytes(b'corrupt')
    assert 'blob_hash_mismatch' in inspect_managed_integrity(managed, deep=True)['errors']
    blob.unlink()
    assert 'registered_blob_missing' in inspect_managed_integrity(managed, deep=True)['errors']


def test_report_blobs_are_verified_and_restored(tmp_path, roots):
    report = b'synthetic-pdf-artifact'
    with closing(sqlite3.connect(roots[1] / 'reports.db')) as connection, connection:
        connection.execute('CREATE TABLE report_artifacts(hash TEXT, content BLOB)')
        connection.execute('INSERT INTO report_artifacts VALUES(?,?)', (hashlib.sha256(report).hexdigest(), report))
    archive, _ = make_backup(tmp_path, roots)
    restored = tmp_path / 'restored'
    restore_backup(archive, restored)
    with closing(sqlite3.connect(restored / 'managed/reports.db')) as connection, connection:
        assert connection.execute('SELECT content FROM report_artifacts').fetchone()[0] == report
        connection.execute('UPDATE report_artifacts SET content=?', (b'tampered',))
    assert 'report_artifact_hash_mismatch' in inspect_managed_integrity(restored / 'managed', deep=True)['errors']


def test_known_outbox_recovery_report_counts_pending_without_mutation(tmp_path, roots):
    with closing(sqlite3.connect(roots[1] / 'task_workflow.db')) as connection, connection:
        connection.execute('CREATE TABLE outbox(status TEXT)')
        connection.executemany('INSERT INTO outbox VALUES(?)', [('pending',), ('unknown',), ('leased',), ('delivered',)])
    archive, _ = make_backup(tmp_path, roots)
    restored = tmp_path / 'restored'
    report = restore_backup(archive, restored)
    assert report['outbox']['needs_review_count'] == 3
    assert report['outbox']['status_counts']['delivered'] == 1
    assert report['outbox']['updated_rows'] == 0
    assert operations_metrics(data_root=restored / 'data', managed_root=restored / 'managed')['outbox_status_counts']['pending'] == 1


def test_cli_backup_verify_restore_in_temporary_roots(tmp_path, roots):
    from scripts.backup_restore import main
    archive = tmp_path / 'cli.zip'
    assert main(['--data-root', str(roots[0]), '--managed-root', str(roots[1]),
                 'backup', '--writers-stopped', '--output', str(archive)]) == 0
    assert main(['verify', '--archive', str(archive)]) == 0
    assert main(['restore', '--archive', str(archive), '--target', str(tmp_path / 'cli-restored')]) == 0
    assert main(['verify', '--archive', str(tmp_path / 'missing')]) == 1


def test_metrics_and_redaction_do_not_include_payloads(roots):
    metrics = operations_metrics(data_root=roots[0], managed_root=roots[1])
    assert metrics['sqlite_databases'] == 2
    assert metrics['disk_free_bytes'] > 0
    value = redact({'api_key': 'secret-key', 'nested': {'prompt': 'private'},
                    'message': 'Bearer abc token=xyz alice@example.com'})
    serialized = json.dumps(value)
    assert all(secret not in serialized for secret in ('secret-key', 'private', 'abc', 'xyz', 'alice@example.com'))


@pytest.fixture
def oidc_preflight_env(tmp_path, monkeypatch):
    from scripts import preflight
    monkeypatch.setattr(preflight, '_dependency', lambda *args: (True, 'test-version'))
    monkeypatch.setenv('COST_AUTH_MODE', 'oidc')
    monkeypatch.setenv('COST_TENANT_ID', 'default')
    monkeypatch.setenv('COST_OIDC_ISSUER', 'https://identity.invalid')
    monkeypatch.setenv('COST_OIDC_AUDIENCE', 'cost-api')
    monkeypatch.setenv('COST_OIDC_JWKS_URL', 'https://identity.invalid/jwks')
    monkeypatch.setenv('COST_WORKER_SUBJECT', 'synthetic-service-subject')
    authorization = tmp_path / 'authorization.json'
    entry = {'enabled': True, 'roles': ['supervisor'], 'products': ['synthetic'], 'factories': ['synthetic']}
    authorization.write_text(json.dumps({'users': {'synthetic-service-subject': entry}}), encoding='utf-8')
    monkeypatch.setenv('COST_AUTHORIZATION_FILE', str(authorization))
    secrets = tmp_path / 'secrets.toml'
    secrets.write_text('[auth]\nredirect_uri="https://app.invalid/oauth2callback"\ncookie_secret="' + 's' * 40 +
        '"\nclient_id="client"\nclient_secret="private-client-secret"\nserver_metadata_url="https://identity.invalid/metadata"\n', encoding='utf-8')
    return authorization, entry, {'data_root': tmp_path, 'managed_root': tmp_path / 'managed',
                                  'secrets_file': secrets, 'min_free_mib': 0}


@pytest.mark.parametrize('case', ['valid', 'missing_subject', 'unknown_subject', 'disabled', 'no_send_role', 'empty_scope'])
def test_preflight_checks_actual_worker_mapping(oidc_preflight_env, monkeypatch, case):
    from scripts import preflight
    authorization, entry, options = oidc_preflight_env
    if case == 'missing_subject':
        monkeypatch.delenv('COST_WORKER_SUBJECT')
    elif case == 'unknown_subject':
        monkeypatch.setenv('COST_WORKER_SUBJECT', 'not-in-map')
    elif case == 'disabled':
        entry['enabled'] = False
    elif case == 'no_send_role':
        entry['roles'] = ['analyst']
    elif case == 'empty_scope':
        entry['products'] = []
    authorization.write_text(json.dumps({'users': {'synthetic-service-subject': entry}}), encoding='utf-8')
    result = preflight.collect_checks(**options)
    check = next(row for row in result['checks'] if row['name'] == 'worker.authorization')
    assert check['required'] is True
    assert check['status'] == ('pass' if case == 'valid' else 'fail')
    assert result['ok'] is (case == 'valid')
    assert 'synthetic-service-subject' not in json.dumps(result)
    assert 'private-client-secret' not in json.dumps(result)


def test_preflight_core_only_ignores_retired_legacy_kb(oidc_preflight_env, monkeypatch):
    from scripts import preflight
    _, _, options = oidc_preflight_env
    monkeypatch.setattr(preflight, '_dependency', lambda distribution, module:
        (distribution in preflight.CORE, 'test-version' if distribution in preflight.CORE else None))
    legacy = options['data_root'] / 'kb/chroma_db'
    legacy.mkdir(parents=True)
    (legacy / 'chroma.sqlite3').write_bytes(b'broken retired database')
    result = preflight.collect_checks(**options)
    assert result['ok']
    checks = {row['name']: row for row in result['checks']}
    assert checks['dependency.torch']['status'] == 'warn'
    for dependency in ('pypdf', 'reportlab', 'PyJWT', 'Authlib'):
        assert checks['dependency.' + dependency]['required']
    assert 'dependency.chromadb' not in checks
    options['managed_root'].mkdir()
    (options['managed_root'] / 'broken.db').write_bytes(b'broken active database')
    assert not preflight.collect_checks(**options)['ok']


def test_container_preflight_failure_starts_no_process(monkeypatch):
    from deploy import entrypoint
    monkeypatch.setenv('COST_AUTH_MODE', 'oidc')
    monkeypatch.setattr(entrypoint, 'collect_checks', lambda **kwargs: {'ok': False, 'checks': [
        {'name': 'worker.authorization', 'status': 'fail', 'detail': 'worker subject missing'}]})
    def forbidden_start(*args, **kwargs):
        raise AssertionError('No process may start when worker authorization fails')
    monkeypatch.setattr(entrypoint.subprocess, 'Popen', forbidden_start)
    assert entrypoint.main() == 1


def test_container_supervises_worker_and_cleans_up_on_exit(monkeypatch):
    from deploy import entrypoint
    monkeypatch.setenv('COST_AUTH_MODE', 'oidc')
    monkeypatch.setattr(entrypoint, 'collect_checks', lambda **kwargs: {'ok': True, 'checks': []})
    monkeypatch.setattr(entrypoint.signal, 'signal', lambda *args: None)
    children = []
    class FakeProcess:
        def __init__(self, command, **kwargs):
            self.command = command
            self.returncode = None
            self.stopped = False
            children.append(self)
        def poll(self):
            return 9 if self.command[-1] == 'scripts/run_task_worker.py' else self.returncode
        def terminate(self):
            self.stopped = True
            self.returncode = 0
        def wait(self, **kwargs):
            return self.returncode or 9
    monkeypatch.setattr(entrypoint.subprocess, 'Popen', FakeProcess)
    assert entrypoint.main() == 1
    assert len(children) == 3
    assert children[2].command == [sys.executable, 'scripts/run_task_worker.py']
    assert all(child.stopped for child in children[:2])


def test_preflight_oidc_reports_missing_or_invalid_without_secrets(tmp_path, monkeypatch):
    from scripts import preflight
    monkeypatch.setattr(preflight, '_dependency', lambda *args: (True, 'test-version'))
    monkeypatch.setenv('COST_AUTH_MODE', 'oidc')
    monkeypatch.setenv('COST_OIDC_ISSUER', 'http://untrusted.invalid')
    monkeypatch.setenv('COST_OIDC_AUDIENCE', 'cost-api')
    monkeypatch.setenv('COST_OIDC_JWKS_URL', 'https://identity.invalid/jwks')
    authorization = tmp_path / 'authorization.json'
    authorization.write_text(json.dumps({'users': {'private-subject': {'roles': ['analyst'],
        'products': ['synthetic'], 'factories': ['synthetic']}}}), encoding='utf-8')
    monkeypatch.setenv('COST_AUTHORIZATION_FILE', str(authorization))
    secrets = tmp_path / 'secrets.toml'
    secrets.write_text('[auth]\nredirect_uri="https://app.invalid/oauth2callback"\ncookie_secret="' + 's' * 40 +
        '"\nclient_id="client"\nclient_secret="private-client-secret"\nserver_metadata_url="https://identity.invalid/metadata"\n', encoding='utf-8')
    result = preflight.collect_checks(data_root=tmp_path, managed_root=tmp_path / 'managed',
                                     secrets_file=secrets, min_free_mib=0)
    assert not result['ok']
    assert next(c for c in result['checks'] if c['name'] == 'oidc.issuer')['status'] == 'fail'
    assert all(secret not in json.dumps(result) for secret in ('private-subject', 'private-client-secret', 's' * 40))
