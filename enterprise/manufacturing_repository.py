"""Versioned canonical manufacturing imports, separate from legacy pharmacy tables.

This repository never changes an authorization grant, default domain, existing cost
revision, knowledge release or task. Operator-installed declarative profiles and
uploaded CSV bytes are frozen in SQLite; confirm uses compare-and-swap. Every
public operation receives the authenticated Principal bound by the application.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import csv
import hashlib
import io
import json
from pathlib import Path, PureWindowsPath
import re
import sqlite3
from uuid import uuid4

from enterprise.operations import write_guard
from enterprise.security import Principal, require

TABLES = frozenset({'actual', 'budget', 'materials', 'labor', 'overhead'})
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_BUNDLE_BYTES = 60 * 1024 * 1024
SCHEMA = 'manufacturing-repository/1'


class ManufacturingConflict(ValueError):
    """The reviewed base/configuration changed; stage again rather than overwrite."""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def _id(value, label='标识'):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value):
        raise ValueError(label + '无效')
    return value


def _version(value):
    if type(value) is not int or value < 0:
        raise ValueError('期望版本须为非负整数')
    return value


def _reason(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 1000 or '\x00' in value:
        raise ValueError('须提供不超过1000字的操作理由')
    return value.strip()


def _csv(name, content, family):
    if (not isinstance(name, str) or PureWindowsPath(name).name != name or '/' in name
            or not name.lower().endswith('.csv') or len(name) > 200 or '\x00' in name):
        raise ValueError('每个表族须提供无目录的CSV文件名')
    if not isinstance(content, bytes) or not 0 < len(content) <= MAX_FILE_BYTES:
        raise ValueError('CSV文件为空或超过20MiB')
    try:
        text = content.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ValueError('规范制造业CSV必须采用UTF-8编码') from None
    if '\x00' in text:
        raise ValueError('CSV包含无效空字符')
    try:
        reader = csv.reader(io.StringIO(text, newline=''), strict=True)
        columns = next(reader)
        rows = []
        if not columns or len(columns) > 100 or len(set(columns)) != len(columns):
            raise ValueError('CSV表头为空、重复或超过限制')
        for row in reader:
            if len(row) != len(columns):
                raise ValueError('CSV行列数与表头不一致')
            if any(len(cell) > 10000 for cell in row):
                raise ValueError('CSV单元格超过限制')
            rows.append(row)
            if len(rows) > 100000:
                raise ValueError('CSV超过十万行')
    except (csv.Error, StopIteration):
        raise ValueError('CSV格式无效') from None
    return {'columns': columns, 'rows': rows, 'source_id': family + ':' + name,
            'source_sha256': hashlib.sha256(content).hexdigest()}


class ManufacturingRepository:
    def __init__(self, root, principal):
        if not isinstance(principal, Principal):
            raise PermissionError('制造业服务需要已认证身份')
        self.root = Path(root)
        self.principal = principal
        self.db = self.root / 'manufacturing.db'

    def _connect(self, *, create=False):
        if not create and not self.db.is_file():
            raise ValueError('尚未安装制造业领域配置')
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    @staticmethod
    def _schema(con):
        con.executescript('''
        CREATE TABLE IF NOT EXISTS manufacturing_profiles (
          id TEXT NOT NULL, version INTEGER NOT NULL, content TEXT NOT NULL,
          sha256 TEXT NOT NULL, actor TEXT NOT NULL, created TEXT NOT NULL, reason TEXT NOT NULL,
          PRIMARY KEY(id,version));
        CREATE TABLE IF NOT EXISTS manufacturing_sources (
          sha256 TEXT PRIMARY KEY, content BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS manufacturing_stages (
          id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, profile_version INTEGER NOT NULL,
          base_revision INTEGER NOT NULL, content TEXT NOT NULL, sha256 TEXT NOT NULL,
          actor TEXT NOT NULL, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS manufacturing_revisions (
          profile_id TEXT NOT NULL, revision INTEGER NOT NULL, stage_id TEXT NOT NULL UNIQUE,
          content TEXT NOT NULL, sha256 TEXT NOT NULL, actor TEXT NOT NULL, created TEXT NOT NULL,
          reason TEXT NOT NULL, PRIMARY KEY(profile_id,revision));
        CREATE TABLE IF NOT EXISTS manufacturing_events (
          id INTEGER PRIMARY KEY, action TEXT NOT NULL, entity TEXT NOT NULL,
          actor TEXT NOT NULL, created TEXT NOT NULL, detail TEXT NOT NULL);
        ''')
        for table in ('manufacturing_profiles', 'manufacturing_sources', 'manufacturing_stages',
                      'manufacturing_revisions', 'manufacturing_events'):
            for operation in ('UPDATE', 'DELETE'):
                con.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} "
                            f"BEFORE {operation} ON {table} BEGIN "
                            "SELECT RAISE(ABORT, 'immutable manufacturing history'); END")

    def _authorize(self, profile, action):
        # Complete bundles require every configured scope. A config is NOT a grant.
        require(self.principal, action)
        for factory in profile['factories'].values():
            for product in profile['products']:
                require(self.principal, action, factory=factory, product=product['name'])

    @staticmethod
    def _event(con, actor, action, entity, detail):
        con.execute('INSERT INTO manufacturing_events(action,entity,actor,created,detail) VALUES(?,?,?,?,?)',
                    (action, entity, actor.user_id, _now(), canonical(detail)))

    @staticmethod
    def _profile_row(con, profile_id):
        row = con.execute('SELECT * FROM manufacturing_profiles WHERE id=? ORDER BY version DESC LIMIT 1',
                          (_id(profile_id),)).fetchone()
        if row is None:
            raise ValueError('制造业配置不存在或不可用')
        value = json.loads(row['content'])
        if digest(value) != row['sha256']:
            raise ValueError('制造业配置完整性校验失败')
        return row, value

    def _authorize_pinned_profile(self, con, profile_id, version, action):
        row = con.execute('SELECT content,sha256 FROM manufacturing_profiles WHERE id=? AND version=?',
                          (_id(profile_id), _version(version))).fetchone()
        if row is None:
            raise ValueError('冻结配置版本缺失')
        value = json.loads(row['content'])
        if digest(value) != row['sha256']:
            raise ValueError('冻结配置完整性校验失败')
        self._authorize(value['profile'], action)
        return row['sha256']

    def install_profile(self, profile, adapter, *, expected_version, reason):
        require(self.principal, 'system.configure')
        from enterprise.domain_profiles import validate_domain_profile
        from enterprise.manufacturing_runtime import validate_binding
        validated = validate_domain_profile(profile)
        if validated.get('schema_version') != 'manufacturing-domain/2':
            raise ValueError('跨行业运行配置必须使用manufacturing-domain/2')
        # Validate unit/factory/product/currency bindings before installing config.
        validated, adapter = validate_binding(validated, adapter)
        self._authorize(validated, 'data.read')
        expected_version, reason = _version(expected_version), _reason(reason)
        profile_id = _id(validated['id'])
        value = {'profile': validated, 'adapter': json.loads(canonical(adapter))}
        sha = digest(value)
        with write_guard(self.root), closing(self._connect(create=True)) as con:
            self._schema(con)
            con.execute('BEGIN IMMEDIATE')
            old = con.execute('SELECT version,content FROM manufacturing_profiles WHERE id=? ORDER BY version DESC LIMIT 1',
                              (profile_id,)).fetchone()
            actual = old['version'] if old else 0
            if actual != expected_version:
                raise ManufacturingConflict('领域配置版本已变化，请重新核查')
            if old:
                self._authorize(json.loads(old['content'])['profile'], 'data.read')
            version = actual + 1
            con.execute('INSERT INTO manufacturing_profiles VALUES(?,?,?,?,?,?,?)',
                        (profile_id, version, canonical(value), sha, self.principal.user_id, _now(), reason))
            self._event(con, self.principal, 'profile.install', profile_id,
                        {'version': version, 'config_hash': sha, 'reason': reason})
            con.commit()
        return {'profile_id': profile_id, 'version': version, 'config_hash': sha,
                'status': 'installed', 'grants_changed': False, 'default_domain_changed': False}

    def profiles(self):
        require(self.principal, 'data.read')
        if not self.db.is_file():
            return []
        result = []
        with closing(self._connect()) as con:
            ids = con.execute('SELECT DISTINCT id FROM manufacturing_profiles ORDER BY id').fetchall()
            for item in ids:
                row, value = self._profile_row(con, item['id'])
                try:
                    self._authorize(value['profile'], 'data.read')
                except PermissionError:
                    continue
                current = con.execute('SELECT revision FROM manufacturing_revisions WHERE profile_id=? ORDER BY revision DESC LIMIT 1',
                                      (row['id'],)).fetchone()
                result.append({'profile_id': row['id'], 'version': row['version'], 'config_hash': row['sha256'],
                               'profile': value['profile'], 'data_revision': current['revision'] if current else 0})
        return result

    def configuration(self, profile_id):
        with closing(self._connect()) as con:
            row, value = self._profile_row(con, profile_id)
            self._authorize(value['profile'], 'data.read')
            return {**value, 'profile_id': row['id'], 'version': row['version'], 'config_hash': row['sha256']}

    def stage(self, profile_id, files, *, periods, expected_revision):
        require(self.principal, 'data.stage')
        if not isinstance(files, dict) or set(files) != TABLES:
            raise ValueError('须同时上传actual/budget/materials/labor/overhead五类CSV完整快照')
        if any(not isinstance(item, (tuple, list)) or len(item) != 2 for item in files.values()):
            raise ValueError('每类文件须包含文件名和原始字节')
        if any(not isinstance(item[1], bytes) for item in files.values()):
            raise ValueError('CSV须为原始字节')
        if sum(len(item[1]) for item in files.values()) > MAX_BUNDLE_BYTES:
            raise ValueError('导入总量超过60MiB')
        expected_revision = _version(expected_revision)
        config = self.configuration(profile_id)
        self._authorize(config['profile'], 'data.stage')
        tables = {key: _csv(*files[key], key) for key in sorted(TABLES)}
        from enterprise.manufacturing_runtime import build_manufacturing_runtime
        runtime = build_manufacturing_runtime(config['profile'], config['adapter'], tables=tables, periods=periods)
        value = {'schema_version': SCHEMA, 'profile_id': profile_id, 'profile_version': config['version'],
                 'config_hash': config['config_hash'], 'periods': periods, 'tables': tables,
                 'runtime_fingerprint': runtime.fingerprint}
        # Canonical serialize before opening a write transaction: never partial data.
        content, sha, stage_id = canonical(value), digest(value), 'ms_' + uuid4().hex
        with write_guard(self.root), closing(self._connect()) as con:
            con.execute('BEGIN IMMEDIATE')
            current_config, _ = self._profile_row(con, profile_id)
            if current_config['sha256'] != config['config_hash'] or current_config['version'] != config['version']:
                raise ManufacturingConflict('校验期间领域配置已变更')
            current = con.execute('SELECT revision FROM manufacturing_revisions WHERE profile_id=? ORDER BY revision DESC LIMIT 1',
                                  (profile_id,)).fetchone()
            if (current['revision'] if current else 0) != expected_revision:
                raise ManufacturingConflict('数据版本已变化，请基于最新快照重新暂存')
            for key in sorted(TABLES):
                con.execute('INSERT OR IGNORE INTO manufacturing_sources VALUES(?,?)',
                            (tables[key]['source_sha256'], files[key][1]))
            con.execute('INSERT INTO manufacturing_stages VALUES(?,?,?,?,?,?,?,?)',
                        (stage_id, profile_id, config['version'], expected_revision, content, sha, self.principal.user_id, _now()))
            self._event(con, self.principal, 'data.stage', stage_id,
                        {'profile_id': profile_id, 'base_revision': expected_revision, 'sha256': sha})
            con.commit()
        return self._stage_view(stage_id)

    def _stage_view(self, stage_id):
        with closing(self._connect()) as con:
            row = con.execute('SELECT * FROM manufacturing_stages WHERE id=?', (_id(stage_id),)).fetchone()
            if row is None:
                raise ValueError('暂存记录不存在或不可用')
            config_row, config = self._profile_row(con, row['profile_id'])
            self._authorize(config['profile'], 'data.read')
            value = json.loads(row['content'])
            if digest(value) != row['sha256']:
                raise ValueError('暂存快照完整性校验失败')
            pinned_hash = self._authorize_pinned_profile(con, row['profile_id'], row['profile_version'], 'data.read')
            if pinned_hash != value['config_hash']:
                raise ValueError('暂存快照与冻结配置不匹配')
            return {'stage_id': row['id'], 'profile_id': row['profile_id'], 'base_revision': row['base_revision'],
                    'profile_version': row['profile_version'], 'config_hash': value['config_hash'],
                    'config_current': config_row['version'] == row['profile_version'],
                    'sha256': row['sha256'], 'periods': value['periods'],
                    'tables': {key: {'rows': len(table['rows']), 'source_sha256': table['source_sha256'],
                                    'source_id': table['source_id']} for key, table in value['tables'].items()},
                    'runtime_fingerprint': value['runtime_fingerprint'], 'status': 'staged_not_active'}

    def confirm(self, stage_id, *, expected_revision, reason):
        require(self.principal, 'data.confirm')
        reason, expected_revision = _reason(reason), _version(expected_revision)
        with write_guard(self.root), closing(self._connect()) as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT * FROM manufacturing_stages WHERE id=?', (_id(stage_id),)).fetchone()
            if row is None:
                raise ValueError('暂存记录不存在或不可用')
            config_row, config = self._profile_row(con, row['profile_id'])
            self._authorize(config['profile'], 'data.confirm')
            value = json.loads(row['content'])
            if digest(value) != row['sha256']:
                raise ValueError('暂存快照完整性校验失败')
            if config_row['version'] != row['profile_version'] or config_row['sha256'] != value['config_hash']:
                raise ManufacturingConflict('暂存对应配置已变更，须重新暂存')
            current = con.execute('SELECT revision FROM manufacturing_revisions WHERE profile_id=? ORDER BY revision DESC LIMIT 1',
                                  (row['profile_id'],)).fetchone()
            actual = current['revision'] if current else 0
            if actual != expected_revision or actual != row['base_revision']:
                raise ManufacturingConflict('数据版本已变化，禁止覆盖并行确认')
            self._runtime(con, config, value)
            revision = actual + 1
            con.execute('INSERT INTO manufacturing_revisions VALUES(?,?,?,?,?,?,?,?)',
                        (row['profile_id'], revision, stage_id, row['content'], row['sha256'],
                         self.principal.user_id, _now(), reason))
            self._event(con, self.principal, 'data.confirm', row['profile_id'],
                        {'revision': revision, 'stage_id': stage_id, 'sha256': row['sha256'], 'reason': reason})
            con.commit()
        return {'profile_id': row['profile_id'], 'revision': revision, 'sha256': row['sha256'],
                'stage_id': stage_id, 'status': 'confirmed', 'legacy_data_changed': False}

    @staticmethod
    def _runtime(con, config, value):
        # Recreate tables from original bytes, not a mutable client-provided digest.
        tables = value['tables']
        if not isinstance(tables, dict) or set(tables) != TABLES:
            raise ValueError('快照表族损坏')
        for family, table in tables.items():
            source = con.execute('SELECT content FROM manufacturing_sources WHERE sha256=?',
                                 (table['source_sha256'],)).fetchone()
            if source is None:
                raise ValueError('原始CSV缺失')
            prefix = family + ':'
            if not table['source_id'].startswith(prefix):
                raise ValueError('原始CSV标识不匹配')
            rebuilt = _csv(table['source_id'][len(prefix):], source['content'], family)
            if canonical(rebuilt) != canonical(table):
                raise ValueError('原始CSV与规范表快照不一致')
        from enterprise.manufacturing_runtime import build_manufacturing_runtime
        runtime = build_manufacturing_runtime(config['profile'], config['adapter'], tables=tables, periods=value['periods'])
        if runtime.fingerprint != value['runtime_fingerprint']:
            raise ValueError('规范计算版本或指纹变化；须重新校验确认数据')
        return runtime

    def current(self, profile_id):
        require(self.principal, 'data.read')
        with closing(self._connect()) as con:
            config_row, config = self._profile_row(con, profile_id)
            self._authorize(config['profile'], 'data.read')
            row = con.execute('SELECT * FROM manufacturing_revisions WHERE profile_id=? ORDER BY revision DESC LIMIT 1',
                              (_id(profile_id),)).fetchone()
            if row is None:
                raise ValueError('此领域尚无已确认的规范数据快照')
            value = json.loads(row['content'])
            if digest(value) != row['sha256']:
                raise ValueError('已确认快照完整性校验失败')
            if config_row['sha256'] != value['config_hash'] or config_row['version'] != value['profile_version']:
                raise ManufacturingConflict('配置已变更，旧数据未按新配置确认，禁止混用')
            runtime = self._runtime(con, config, value)
            return {'profile_id': profile_id, 'revision': row['revision'], 'sha256': row['sha256'],
                    'config_hash': value['config_hash'], 'profile_version': value['profile_version'],
                    'periods': value['periods'], 'runtime': runtime, 'profile': config['profile'],
                    'actor': row['actor'], 'created': row['created']}

    def history(self, profile_id):
        config = self.configuration(profile_id)
        self._authorize(config['profile'], 'audit.read')
        with closing(self._connect()) as con:
            rows = con.execute('SELECT revision,stage_id,sha256,actor,created,reason,content '
                'FROM manufacturing_revisions WHERE profile_id=? ORDER BY revision DESC', (_id(profile_id),)).fetchall()
            result = []
            for row in rows:
                frozen = json.loads(row['content'])
                if digest(frozen) != row['sha256']:
                    raise ValueError('历史快照完整性校验失败')
                try:
                    pinned_hash = self._authorize_pinned_profile(con, profile_id, frozen['profile_version'], 'audit.read')
                except PermissionError:
                    continue
                if pinned_hash != frozen['config_hash']:
                    raise ValueError('历史快照与冻结配置不匹配')
                result.append({key: row[key] for key in row.keys() if key != 'content'})
            return result
