"""Immutable confirmed knowledge versions, staged confirmation, local parsers.

Confirmation and index publication are separate operations. ``index_status`` on
an immutable version describes its initial catalog state; publication state is
owned by knowledge_release. Repository(root) is a low-level storage adapter;
application callers bind/pass a Principal to enforce scopes on every operation.
An empty repository is not created by construction or reading.
"""
from __future__ import annotations
import csv
import difflib
import hashlib
import io
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path, PureWindowsPath
import re
import sqlite3
import unicodedata
from uuid import uuid4
from functools import wraps
import zipfile


def guarded_write(method):
    """Coordinate explicit writes with application backup/maintenance."""
    @wraps(method)
    def guarded(self, *args, **kwargs):
        from enterprise.operations import write_guard
        with write_guard(self.root):
            return method(self, *args, **kwargs)
    return guarded

MAX_BYTES = 20 * 1024 * 1024
SUPPORTED = {'.pdf', '.docx', '.txt', '.csv'}


class KnowledgeError(ValueError):
    pass


class VersionConflict(KnowledgeError):
    pass


class KnowledgeAccessError(PermissionError):
    """An authenticated principal or its explicit scope is insufficient."""


READ_ROLES = frozenset({'knowledge_admin', 'supervisor', 'analyst', 'auditor'})
WRITE_ROLES = frozenset({'knowledge_admin', 'supervisor'})


def require_principal(principal, *, write=False):
    """Duck-typed security boundary for this single-organization deployment."""
    if principal is None or not str(getattr(principal, 'user_id', '')).strip():
        raise KnowledgeAccessError('受控知识操作需要已认证身份')
    if getattr(principal, 'tenant_id', None) != 'default':
        raise KnowledgeAccessError('当前仅支持default单组织部署，不提供多租户隔离')
    if not set(getattr(principal, 'roles', ())) & (WRITE_ROLES if write else READ_ROLES):
        raise KnowledgeAccessError('当前身份无知识管理权限' if write else '当前身份无知识读取权限')
    if not getattr(principal, 'factories', ()) or not getattr(principal, 'products', ()):
        raise KnowledgeAccessError('工厂或产品授权为空，默认拒绝')
    return principal


def _grants(values):
    if not isinstance(values, (list, tuple, set, frozenset)) or any(not isinstance(v, str) or not v.strip() for v in values):
        raise KnowledgeAccessError('授权范围须为名称列表')
    return frozenset(values)


def allowed_scope(principal, allowed_scopes=None):
    require_principal(principal)
    scope = {key: _grants(getattr(principal, key, ())) for key in ('factories', 'products')}
    if allowed_scopes is not None:
        if not isinstance(allowed_scopes, dict) or set(allowed_scopes) - {'factories', 'products'}:
            raise KnowledgeAccessError('allowed_scopes仅允许factories与products')
        for key in scope:
            narrowed = _grants(allowed_scopes.get(key, ()))
            if '*' not in scope[key] and not narrowed <= scope[key]:
                raise KnowledgeAccessError('allowed_scopes不得扩大主体授权范围')
            scope[key] = narrowed
    return scope


def authorize_query(principal, *, product=None, factory=None, allowed_scopes=None):
    scope = allowed_scope(principal, allowed_scopes)
    for key, value in (('products', product), ('factories', factory)):
        if not scope[key] or (value is not None and (not isinstance(value, str) or not value.strip() or value == '*')):
            raise KnowledgeAccessError('查询范围无效或未授权')
        if value is not None and '*' not in scope[key] and value not in scope[key]:
            raise KnowledgeAccessError('查询工厂或产品不在授权范围内')
    return scope


def scope_permits(version, scope, *, product=None, factory=None):
    """A whole document requires all its declared scopes (aggregates stay private).

    Explicit public documents are organization-wide. Missing scope is never
    inferred to mean public, even when a filename happens to mention a product.
    """
    if not scope.get('products') or not scope.get('factories'):
        return False
    if version.get('visibility') == 'public':
        return True
    if version.get('visibility') != 'scoped':
        return False
    for key, field, requested in (('products', 'scope_products', product), ('factories', 'scope_factories', factory)):
        values = set(version.get(field) or ())
        if not values or '*' in values or (requested is not None and requested not in values):
            return False
        if '*' not in scope[key] and not values <= scope[key]:
            return False
    return True


def _authorize_version(version, principal, *, write=False, allowed_scopes=None):
    require_principal(principal, write=write)
    scope = allowed_scope(principal, allowed_scopes)
    if write and version.get('visibility') == 'public' and not all('*' in scope[key] for key in ('products', 'factories')):
        raise KnowledgeAccessError('组织级公开资料的写操作需要完整组织范围')
    if not scope_permits(version, scope):
        raise KnowledgeAccessError('文档范围未知或不在当前授权范围内')


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def normalize_known_at(value):
    """Return an inclusive UTC cutoff; date-only input means end of that day."""
    if value is None:
        return _now()
    if not isinstance(value, str):
        raise KnowledgeError('操作时间格式无效')
    if len(value) == 10:
        _date(value, '操作日期')
        return value + 'T23:59:59.999999+00:00'
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise KnowledgeError('操作时间格式无效') from None
    if stamp.tzinfo is None:
        raise KnowledgeError('操作时间须包含时区')
    return stamp.astimezone(timezone.utc).isoformat(timespec='microseconds')


def _date(value, name):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise KnowledgeError(f'{name}须为YYYY-MM-DD')
    try:
        date.fromisoformat(value)
    except ValueError:
        raise KnowledgeError(f'{name}不是有效日期') from None
    return value


def _name(filename):
    if not isinstance(filename, str) or not filename or '\x00' in filename:
        raise KnowledgeError('文件名无效')
    name = PureWindowsPath(filename.replace('/', '\\')).name
    if name in ('', '.', '..') or ':' in name or len(name) > 240:
        raise KnowledgeError('文件名无效')
    return name


def _decode(content):
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            return content.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise KnowledgeError('文本无法按UTF-8或GB18030解码，请转换为UTF-8后重试')


def _normalize(text):
    text = unicodedata.normalize('NFC', text.replace('\r\n', '\n').replace('\r', '\n')).lstrip('\ufeff')
    return '\n'.join(line.rstrip() for line in text.split('\n')).strip('\n')


def preview_file(content: bytes, filename: str):
    """Read-only parse for original-file preview, without registering any version."""
    result = {'filename': None, 'text': '', 'sha256': None, 'text_sha256': None,
              'format': None, 'parser': None, 'metadata': {}, 'errors': []}
    try:
        filename = _name(filename)
        result['filename'] = filename
        suffix = Path(filename).suffix.lower()
        result['format'] = suffix.lstrip('.')
        if suffix == '.doc':
            raise KnowledgeError('不支持旧版.doc，请先转换为.docx')
        if suffix not in SUPPORTED:
            raise KnowledgeError('仅支持PDF、DOCX、TXT、CSV')
        if not isinstance(content, bytes):
            raise KnowledgeError('文件内容必须为bytes')
        if not content or len(content) > MAX_BYTES:
            raise KnowledgeError('文件不能为空且大小不得超过20MB')
        result['sha256'] = hashlib.sha256(content).hexdigest()
        if suffix in {'.txt', '.csv'}:
            text, encoding = _decode(content)
            result['parser'] = 'stdlib-text'
            result['metadata']['encoding'] = encoding
            if suffix == '.csv':
                try:
                    rows = list(csv.reader(io.StringIO(text, newline=''), strict=True))
                except csv.Error:
                    raise KnowledgeError('CSV结构无效，请检查引号和分隔符') from None
                result['metadata'].update(row_count=len(rows), preview_rows=rows[:50], preview_truncated=len(rows)>50)
                # Retain the source CSV lines instead of flattening rows into prose.
                result['parser'] = 'stdlib-csv'
        elif suffix == '.docx':
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    if sum(x.file_size for x in archive.infolist()) > 100 * 1024 * 1024:
                        raise KnowledgeError('DOCX解压内容过大，拒绝解析')
                from docx import Document
                from docx.table import Table
                from docx.text.paragraph import Paragraph
                document = Document(io.BytesIO(content))
                blocks, paragraphs, tables = [], 0, 0
                for child in document.element.body.iterchildren():
                    if child.tag.endswith('}p'):
                        blocks.append(Paragraph(child, document).text)
                        paragraphs += 1
                    elif child.tag.endswith('}tbl'):
                        table = Table(child, document)
                        blocks.extend('\t'.join(cell.text for cell in row.cells) for row in table.rows)
                        tables += 1
                text = '\n'.join(blocks)
                result['parser'] = 'python-docx'
                result['metadata'].update(paragraph_count=paragraphs, table_count=tables)
            except ImportError:
                raise KnowledgeError('当前环境缺少python-docx，无法解析DOCX') from None
        else:
            try:
                from pypdf import PdfReader
            except ImportError:
                raise KnowledgeError('PDF解析部署依赖pypdf缺失，请安装核心依赖后重试') from None
            document = PdfReader(io.BytesIO(content))
            if document.is_encrypted and not document.decrypt(''):
                raise KnowledgeError('PDF已加密，请先解除密码保护')
            pages = [page.extract_text() or '' for page in document.pages]
            result['parser'] = 'pypdf'
            if not any(page.strip() for page in pages):
                raise KnowledgeError('PDF没有可提取文本，扫描件请先完成OCR后再登记')
            text = '\n\n'.join(f'[第{i}页]\n{page}' for i, page in enumerate(pages, 1))
            result['metadata']['page_count'] = len(pages)
        text = _normalize(text)
        if not text.strip():
            raise KnowledgeError('未解析出正文，不能登记空知识版本')
        result['text'] = text
        result['text_sha256'] = hashlib.sha256(text.encode('utf-8')).hexdigest()
    except KnowledgeError as exc:
        result['errors'].append(str(exc))
    except Exception as exc:
        result['errors'].append(f'文件解析失败（{type(exc).__name__}），请检查文件是否损坏')
    return result


_SCHEMA = '''
CREATE TABLE IF NOT EXISTS documents (
 doc_id TEXT PRIMARY KEY, title TEXT NOT NULL UNIQUE, head_version INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, created_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
 version_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL REFERENCES documents(doc_id), version INTEGER NOT NULL,
 filename TEXT NOT NULL, title TEXT NOT NULL, scope_products TEXT NOT NULL,
 effective_from TEXT NOT NULL, effective_to TEXT, category TEXT NOT NULL,
 sha256 TEXT NOT NULL, text_sha256 TEXT NOT NULL, text TEXT NOT NULL, format TEXT NOT NULL,
 parser TEXT NOT NULL, parse_metadata TEXT NOT NULL, byte_size INTEGER NOT NULL,
 confirmed_at TEXT NOT NULL, confirmed_by TEXT NOT NULL, reason TEXT NOT NULL,
 index_status TEXT NOT NULL DEFAULT 'catalog_only',
 scope_factories TEXT NOT NULL DEFAULT '[]', visibility TEXT NOT NULL DEFAULT 'unknown',
 business_metadata TEXT NOT NULL DEFAULT '{}', UNIQUE(doc_id, version)
);
CREATE INDEX IF NOT EXISTS version_hash ON versions(sha256);
CREATE INDEX IF NOT EXISTS version_text_hash ON versions(text_sha256);
CREATE TABLE IF NOT EXISTS stages (
 stage_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, base_version INTEGER NOT NULL,
 payload TEXT NOT NULL, content BLOB NOT NULL, created_at TEXT NOT NULL,
 actor TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', committed_version_id TEXT,
 payload_sha256 TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS events (
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, doc_id TEXT NOT NULL,
 version_id TEXT, stage_id TEXT, actor TEXT NOT NULL, operation_at TEXT NOT NULL,
 effective_from TEXT NOT NULL, effective_to TEXT, reason TEXT NOT NULL, details TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS immutable_version_update BEFORE UPDATE ON versions BEGIN
 SELECT RAISE(ABORT,'confirmed knowledge versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_version_delete BEFORE DELETE ON versions BEGIN
 SELECT RAISE(ABORT,'confirmed knowledge versions are immutable'); END;
'''


def business_metadata(version):
    return {key: version.get(key) for key in ('title', 'scope_products', 'scope_factories',
            'visibility', 'effective_from', 'effective_to', 'category', 'business_metadata')}


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


class Repository:
    def __init__(self, root=None, *, principal=None):
        if root is None:
            from paths import MANAGED_DIR
            root = MANAGED_DIR
        self.root = Path(root).resolve()
        self.db_path = self.root / 'knowledge.db'
        self.blob_dir = self.root / 'blobs'
        self.principal = principal

    def _principal(self, principal):
        return principal if principal is not None else self.principal

    def _revoked_documents(self):
        """Read live revocations on every authorized operation, never cache them."""
        path = self.root / 'knowledge_releases.db'
        if not path.exists():
            return set()
        conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=15)
        try:
            return {row[0] for row in conn.execute('SELECT doc_id FROM revocations')}
        finally:
            conn.close()

    def _connect(self, write=False):
        if write:
            self.root.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, timeout=15)
            conn.execute('PRAGMA foreign_keys=ON')
            conn.executescript(_SCHEMA)
            # Additive migration only on an explicit write. Old records remain
            # unknown-scope until an approved metadata revision is confirmed.
            columns = {row[1] for row in conn.execute('PRAGMA table_info(versions)')}
            for name, declaration in (('scope_factories', "TEXT NOT NULL DEFAULT '[]'"),
                                      ('visibility', "TEXT NOT NULL DEFAULT 'unknown'"),
                                      ('business_metadata', "TEXT NOT NULL DEFAULT '{}'")):
                if name not in columns:
                    conn.execute(f'ALTER TABLE versions ADD COLUMN {name} {declaration}')
            stage_columns = {row[1] for row in conn.execute('PRAGMA table_info(stages)')}
            if 'payload_sha256' not in stage_columns:
                conn.execute("ALTER TABLE stages ADD COLUMN payload_sha256 TEXT NOT NULL DEFAULT ''")
        else:
            if not self.db_path.exists():
                return None
            conn = sqlite3.connect(self.db_path.as_uri() + '?mode=ro', uri=True, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _version(row):
        if row is None:
            return None
        data = dict(row)
        for field, fallback in (('scope_products', '[]'), ('scope_factories', '[]'),
                                ('parse_metadata', '{}'), ('business_metadata', '{}')):
            data[field] = json.loads(data.get(field, fallback))
        data.setdefault('visibility', 'unknown')
        data['status'] = 'confirmed'
        data['source'] = {'doc_id': data['doc_id'], 'version_id': data['version_id'],
                          'version': data['version'], 'filename': data['filename'], 'sha256': data['sha256']}
        return data

    def _duplicate(self, sha256, text_sha256, conn=None):
        own = conn is None
        conn = conn or self._connect()
        if conn is None:
            return None
        try:
            row = conn.execute('SELECT * FROM versions WHERE sha256=? ORDER BY confirmed_at DESC LIMIT 1', (sha256,)).fetchone()
            kind = 'binary_duplicate'
            if row is None:
                row = conn.execute('SELECT * FROM versions WHERE text_sha256=? ORDER BY confirmed_at DESC LIMIT 1', (text_sha256,)).fetchone()
                kind = 'text_duplicate'
            return {'kind': kind, 'version': self._version(row)} if row else None
        finally:
            if own:
                conn.close()

    @guarded_write
    def stage(self, content: bytes, filename, title, scope_products, effective_from, category, actor,
              doc_id=None, effective_to=None, *, scope_factories=None, visibility=None,
              metadata=None, principal=None):
        principal = self._principal(principal)
        if principal is not None:
            require_principal(principal, write=True)
            if 'knowledge_admin' not in principal.roles:
                raise KnowledgeAccessError('知识登记预览需要knowledge_admin角色')
            actor = principal.user_id
        # A binary duplicate reuses the parse, not the logical document identity.
        known = None
        if isinstance(content, bytes) and 0 < len(content) <= MAX_BYTES:
            try:
                clean = _name(filename)
                if Path(clean).suffix.lower() in SUPPORTED:
                    known = self._duplicate(hashlib.sha256(content).hexdigest(), '')
            except KnowledgeError:
                pass
        if known and known['kind'] == 'binary_duplicate' and Path(clean).suffix.lower().lstrip('.') == known['version']['format']:
            previous = known['version']
            parsed = {'filename': clean, 'text': previous['text'], 'sha256': previous['sha256'],
                      'text_sha256': previous['text_sha256'], 'errors': [], 'format': previous['format'],
                      'parser': previous['parser'], 'metadata': previous['parse_metadata']}
        else:
            parsed = preview_file(content, filename)
        result = {'stage_id': None, 'doc_id': doc_id, 'base_version': 0, 'change': {},
                  'text': parsed['text'], 'diff': '', 'errors': list(parsed['errors']),
                  'filename': parsed['filename'], 'sha256': parsed['sha256'],
                  'text_sha256': parsed['text_sha256'], 'index_status': 'catalog_only'}
        try:
            if result['errors']:
                return result
            title, actor, category = str(title or '').strip(), str(actor or '').strip(), str(category or '').strip()
            if not title or len(title) > 240 or not actor or not category:
                raise KnowledgeError('标题、操作人和资料类别必填；标题不超过240字符')
            scopes = {}
            for field, values in (('scope_products', scope_products), ('scope_factories', [] if scope_factories is None else scope_factories)):
                if not isinstance(values, list) or any(not isinstance(p, str) or not p.strip() or p.strip() == '*' for p in values):
                    raise KnowledgeError('适用范围须为明确名称列表，不使用通配符')
                scopes[field] = sorted(set(p.strip() for p in values))
            visibility = visibility or ('scoped' if scopes['scope_products'] and scopes['scope_factories'] else 'unknown')
            if visibility not in {'public', 'scoped', 'unknown'}:
                raise KnowledgeError('visibility须为public、scoped或unknown')
            if visibility == 'scoped' and not all(scopes.values()):
                raise KnowledgeError('受限文档须同时明确产品和工厂范围')
            if visibility == 'public' and any(scopes.values()):
                raise KnowledgeError('明确公开资料使用空范围；受限资料请使用scoped')
            metadata = {} if metadata is None else metadata
            if not isinstance(metadata, dict) or any(not isinstance(k, str) for k in metadata):
                raise KnowledgeError('业务metadata须为JSON对象')
            try:
                metadata = json.loads(canonical_json(metadata))
            except (ValueError, TypeError):
                raise KnowledgeError('业务metadata须为可序列化JSON对象') from None
            effective_from = _date(effective_from, '生效日期')
            effective_to = _date(effective_to, '失效日期') if effective_to else None
            if effective_to and effective_to < effective_from:
                raise KnowledgeError('失效日期不得早于生效日期（生效与失效日均包含）')
            proposed = {'title': title, **scopes, 'visibility': visibility,
                        'effective_from': effective_from, 'effective_to': effective_to,
                        'category': category, 'business_metadata': metadata}
            if principal is not None:
                _authorize_version(proposed, principal, write=True)
                if visibility == 'public' and not all('*' in getattr(principal, key) for key in ('products', 'factories')):
                    raise KnowledgeAccessError('公开组织级资料需完整组织范围的知识管理者确认')
            base = self.get(doc_id, principal=principal) if doc_id else None
            if doc_id and base is None:
                raise KnowledgeError('待更新文档不存在')
            # Same title identifies an existing logical document for a no-op only.
            # Changed metadata/content still requires an explicit document choice.
            title_match = None
            if not doc_id:
                conn = self._connect()
                if conn:
                    try:
                        row = conn.execute('SELECT doc_id FROM documents WHERE title=?', (title,)).fetchone()
                        title_match = self.get(row['doc_id'], principal=principal) if row else None
                    finally:
                        conn.close()
            comparison = base or title_match
            if base:
                if principal is not None:
                    _authorize_version(base, principal, write=True)
                result['base_version'] = base['version']
                if title != base['title']:
                    raise KnowledgeError('更新必须保留文档标题；请选择正确的已有文档')
            if comparison and parsed['text_sha256'] == comparison['text_sha256'] and business_metadata(comparison) == proposed:
                kind = 'binary_duplicate' if parsed['sha256'] == comparison['sha256'] else 'text_duplicate'
                result['change'] = {'kind': kind, 'requires_confirmation': False,
                    'summary': '正文与业务元数据均相同，不必更新', 'duplicate_of': comparison['source']}
                return result
            if title_match:
                raise KnowledgeError('同标题文档已存在，请选择该文档进行更新')
            conn = self._connect(write=True)
            try:
                doc_id = doc_id or uuid4().hex
                stage_id = uuid4().hex
                before = base['text'] if base else ''
                diff = '\n'.join(difflib.unified_diff(before.splitlines(), parsed['text'].splitlines(),
                        fromfile=f"版本{result['base_version']}", tofile='待确认版本', lineterm=''))
                metadata_changes = {key: {'before': business_metadata(base).get(key) if base else None, 'after': value}
                                    for key, value in proposed.items() if not base or business_metadata(base).get(key) != value}
                metadata_only = bool(base and parsed['text_sha256'] == base['text_sha256'])
                change = {'kind': 'metadata_revision' if metadata_only else ('new_version' if base else 'new_document'),
                          'requires_confirmation': True, 'metadata_changes': metadata_changes,
                          'summary': '确认后新增版本，旧版永久保留；原件按SHA256去重' if base else '确认后登记独立业务文档',
                          'added_lines': sum(1 for x in diff.splitlines() if x.startswith('+') and not x.startswith('+++')),
                          'removed_lines': sum(1 for x in diff.splitlines() if x.startswith('-') and not x.startswith('---'))}
                payload = {**parsed, **proposed, 'change': change, 'diff': diff, 'byte_size': len(content)}
                now = _now()
                encoded = canonical_json(payload)
                conn.execute('INSERT INTO stages(stage_id,doc_id,base_version,payload,content,created_at,actor,payload_sha256) VALUES(?,?,?,?,?,?,?,?)',
                             (stage_id, doc_id, result['base_version'], encoded, content, now, actor,
                              hashlib.sha256(encoded.encode('utf-8')).hexdigest()))
                self._event(conn, 'staged', doc_id, None, stage_id, actor, now, effective_from, effective_to,
                            '差异预览；尚未确认生效', change)
                conn.commit()
                result.update(stage_id=stage_id, doc_id=doc_id, change=change, diff=diff, **proposed)
            finally:
                conn.close()
        except KnowledgeError as exc:
            result['errors'].append(str(exc))
        return result

    @guarded_write
    def stage_scope_review(self, doc_id, *, expected_version_id, expected_sha256, principal=None,
                           scope_products, scope_factories, visibility, effective_from,
                           review_reason, effective_to=None, metadata=None):
        """Explicitly classify one legacy unknown document, preserving identity.

        Only a knowledge_admin with both organization-wide scopes can inspect
        this preview. The caller must pin the previously reviewed version and
        original hash; confirmation remains a separate, concurrent-safe action.
        """
        principal = self._principal(principal)
        require_principal(principal, write=True)
        if 'knowledge_admin' not in principal.roles or not all('*' in getattr(principal, key) for key in ('products', 'factories')):
            raise KnowledgeAccessError('未知范围治理仅允许完整组织范围的知识管理员')
        if not isinstance(review_reason, str) or not review_reason.strip():
            raise KnowledgeError('范围核准原因必填')
        raw = Repository(self.root)
        version = raw.get(doc_id)
        if version is None or version['visibility'] != 'unknown':
            raise KnowledgeError('范围复核仅适用于已有未知范围文档')
        if version['version_id'] != expected_version_id or version['sha256'] != expected_sha256:
            raise VersionConflict('范围复核所依据的版本或原件已变化')
        if doc_id in self._revoked_documents():
            raise KnowledgeAccessError('撤回文档不得重新分类')
        if visibility not in {'public', 'scoped'}:
            raise KnowledgeError('范围复核必须给出明确public或scoped范围')
        content = raw.read_blob(expected_sha256, version_id=expected_version_id)
        pending = raw.stage(content, version['filename'], version['title'], scope_products, effective_from,
            version['category'], principal.user_id, doc_id=doc_id, effective_to=effective_to,
            scope_factories=scope_factories, visibility=visibility,
            metadata=version['business_metadata'] if metadata is None else metadata)
        if pending['errors'] or not pending['stage_id']:
            return pending
        conn = self._connect(write=True)
        try:
            payload = json.loads(conn.execute('SELECT payload FROM stages WHERE stage_id=?', (pending['stage_id'],)).fetchone()['payload'])
            review = {'expected_version_id': expected_version_id, 'expected_sha256': expected_sha256,
                      'reviewed_by': principal.user_id, 'reason': review_reason.strip()}
            payload['scope_review'] = review
            encoded = canonical_json(payload)
            conn.execute('UPDATE stages SET payload=?,payload_sha256=? WHERE stage_id=?',
                         (encoded, hashlib.sha256(encoded.encode('utf-8')).hexdigest(), pending['stage_id']))
            self._event(conn, 'scope_review_staged', doc_id, None, pending['stage_id'], principal.user_id, _now(),
                        effective_from, effective_to, review_reason.strip(), review)
            conn.commit()
            pending['scope_review'] = review
        finally:
            conn.close()
        return pending

    @staticmethod
    def _event(conn, action, doc_id, version_id, stage_id, actor, now, start, end, reason, details):
        conn.execute('INSERT INTO events(action,doc_id,version_id,stage_id,actor,operation_at,effective_from,effective_to,reason,details) VALUES(?,?,?,?,?,?,?,?,?,?)',
                     (action, doc_id, version_id, stage_id, actor, now, start, end, reason, json.dumps(details, ensure_ascii=False)))

    @guarded_write
    def commit(self, stage_id, actor, reason, *, principal=None):
        principal = self._principal(principal)
        if principal is not None:
            require_principal(principal, write=True)
            actor = principal.user_id
        actor, reason = str(actor or '').strip(), str(reason or '').strip()
        if not actor or not reason:
            raise KnowledgeError('确认操作人和更新原因必填')
        conn = self._connect(write=True)
        try:
            conn.execute('BEGIN IMMEDIATE')
            stage = conn.execute('SELECT * FROM stages WHERE stage_id=?', (stage_id,)).fetchone()
            if stage is None:
                raise KnowledgeError('预览不存在，请重新生成预览')
            payload = json.loads(stage['payload'])
            payload.setdefault('scope_factories', [])
            payload.setdefault('visibility', 'unknown')
            payload.setdefault('business_metadata', {})
            if principal is not None:
                _authorize_version(payload, principal, write=True)
                if payload['visibility'] == 'public' and not all('*' in getattr(principal, key) for key in ('products', 'factories')):
                    raise KnowledgeAccessError('公开组织级资料需完整组织范围的知识管理者确认')
            if principal is not None and stage['doc_id'] in self._revoked_documents():
                raise KnowledgeAccessError('该文档已撤回')
            if stage['status'] == 'committed':
                value = self._version(conn.execute('SELECT * FROM versions WHERE version_id=?', (stage['committed_version_id'],)).fetchone())
                if principal is not None:
                    _authorize_version(value, principal, write=True)
                return value
            if stage['status'] != 'pending':
                raise KnowledgeError('该预览已结束，请重新生成预览')
            if stage['payload_sha256'] and hashlib.sha256(stage['payload'].encode('utf-8')).hexdigest() != stage['payload_sha256']:
                raise KnowledgeError('暂存正文或metadata校验失败，已拒绝确认')
            document = conn.execute('SELECT * FROM documents WHERE doc_id=?', (stage['doc_id'],)).fetchone()
            head = document['head_version'] if document else 0
            if head != stage['base_version']:
                raise VersionConflict('预览后文档已产生新版本，请重新查看差异并确认')
            if document is None and conn.execute('SELECT 1 FROM documents WHERE title=?', (payload['title'],)).fetchone():
                raise VersionConflict('预览后同标题文档已被登记，请选择该文档重新预览')
            if document and principal is not None:
                current = self._version(conn.execute('SELECT * FROM versions WHERE doc_id=? ORDER BY version DESC LIMIT 1', (stage['doc_id'],)).fetchone())
                review = payload.get('scope_review')
                if current['visibility'] == 'unknown' and review:
                    if 'knowledge_admin' not in principal.roles or not all('*' in getattr(principal, key) for key in ('products', 'factories')):
                        raise KnowledgeAccessError('未知范围治理确认需要完整组织范围的知识管理员')
                    if current['version_id'] != review['expected_version_id'] or current['sha256'] != review['expected_sha256']:
                        raise VersionConflict('范围复核所依据版本已变化')
                else:
                    _authorize_version(current, principal, write=True)
            content = bytes(stage['content'])
            if hashlib.sha256(content).hexdigest() != payload['sha256']:
                raise KnowledgeError('暂存内容哈希不一致，已拒绝确认')
            if hashlib.sha256(payload['text'].encode('utf-8')).hexdigest() != payload['text_sha256']:
                raise KnowledgeError('暂存正文哈希不一致，已拒绝确认')
            self.blob_dir.mkdir(parents=True, exist_ok=True)
            blob = self.blob_dir / payload['sha256']
            try:
                with blob.open('xb') as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError:
                if hashlib.sha256(blob.read_bytes()).hexdigest() != payload['sha256']:
                    raise KnowledgeError('原件存储损坏，已拒绝覆盖') from None
            now, version_id, version = _now(), uuid4().hex, head + 1
            if document is None:
                conn.execute('INSERT INTO documents VALUES(?,?,?,?,?)', (stage['doc_id'], payload['title'], 0, now, actor))
            conn.execute('''INSERT INTO versions(version_id,doc_id,version,filename,title,scope_products,effective_from,effective_to,category,
                sha256,text_sha256,text,format,parser,parse_metadata,byte_size,confirmed_at,confirmed_by,reason,index_status,
                scope_factories,visibility,business_metadata)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (version_id,stage['doc_id'],version,payload['filename'],payload['title'],json.dumps(payload['scope_products'],ensure_ascii=False),
                 payload['effective_from'],payload['effective_to'],payload['category'],payload['sha256'],payload['text_sha256'],payload['text'],
                 payload['format'],payload['parser'],json.dumps(payload['metadata'],ensure_ascii=False),payload['byte_size'],now,actor,reason,'catalog_only',
                 canonical_json(payload['scope_factories']),payload['visibility'],canonical_json(payload['business_metadata'])))
            conn.execute('UPDATE documents SET head_version=? WHERE doc_id=?', (version,stage['doc_id']))
            conn.execute("UPDATE stages SET status='committed',committed_version_id=?,content=X'' WHERE stage_id=?", (version_id,stage_id))
            self._event(conn,'confirmed',stage['doc_id'],version_id,stage_id,actor,now,payload['effective_from'],payload['effective_to'],reason,
                        {'base_version':head,'version':version,'sha256':payload['sha256'],'index_status':'catalog_only'})
            conn.commit()
            return self._version(conn.execute('SELECT * FROM versions WHERE version_id=?', (version_id,)).fetchone())
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get(self, doc_id=None, version=None, *, version_id=None, principal=None):
        principal = self._principal(principal)
        if principal is not None:
            require_principal(principal)
        conn = self._connect()
        if conn is None:
            return None
        try:
            if version_id is not None:
                row = conn.execute('SELECT * FROM versions WHERE version_id=?', (version_id,)).fetchone()
            elif version is not None:
                row = conn.execute('SELECT * FROM versions WHERE doc_id=? AND version=?', (doc_id, version)).fetchone()
            else:
                row = conn.execute('SELECT * FROM versions WHERE doc_id=? ORDER BY version DESC LIMIT 1', (doc_id,)).fetchone()
            value = self._version(row)
            if value and principal is not None:
                _authorize_version(value, principal)
                if value['doc_id'] in self._revoked_documents():
                    raise KnowledgeAccessError('该文档已撤回')
            return value
        finally:
            conn.close()

    def read_blob(self, sha256, *, principal=None, version_id=None):
        principal = self._principal(principal)
        if principal is not None:
            require_principal(principal)
        if not isinstance(sha256, str) or not re.fullmatch('[0-9a-f]{64}', sha256):
            raise KnowledgeError('原件哈希须为完整SHA256')
        conn = self._connect()
        if conn is None:
            raise KnowledgeError('原件未登记')
        try:
            rows = [self._version(row) for row in conn.execute('SELECT * FROM versions WHERE sha256=?', (sha256,))]
            if version_id is not None:
                rows = [row for row in rows if row['version_id'] == version_id]
            if principal is not None:
                scope = allowed_scope(principal)
                revoked = self._revoked_documents()
                rows = [row for row in rows if scope_permits(row, scope) and row['doc_id'] not in revoked]
                if not rows:
                    raise KnowledgeAccessError('无权读取该原件或原件不存在')
            elif not rows:
                raise KnowledgeError('原件未确认登记')
        finally:
            conn.close()
        try:
            content = (self.blob_dir / sha256).read_bytes()
        except OSError:
            raise KnowledgeError('已登记原件不可读取') from None
        if hashlib.sha256(content).hexdigest() != sha256:
            raise KnowledgeError('原件哈希校验失败')
        return content

    def history(self, doc_id=None, *, effective_from=None, effective_to=None, operation_from=None,
                operation_to=None, principal=None, allowed_scopes=None):
        principal = self._principal(principal)
        scope = allowed_scope(principal, allowed_scopes) if principal is not None else None
        where, args = self._filters(effective_from, effective_to, operation_from, operation_to, 'confirmed_at')
        if doc_id is not None:
            where.append('doc_id=?'); args.append(doc_id)
        conn = self._connect()
        if conn is None:
            return []
        try:
            query = 'SELECT * FROM versions' + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY version DESC,confirmed_at DESC'
            rows = [self._version(r) for r in conn.execute(query, args)]
            revoked = self._revoked_documents() if scope is not None else set()
            return [row for row in rows if scope is None or (scope_permits(row, scope) and row['doc_id'] not in revoked)]
        finally:
            conn.close()

    def list_documents(self, *, effective_from=None, effective_to=None, operation_from=None,
                       operation_to=None, principal=None, allowed_scopes=None):
        """Select heads before authorization so a hidden head cannot revive an old one."""
        principal = self._principal(principal)
        scope = allowed_scope(principal, allowed_scopes) if principal is not None else None
        rows = Repository(self.root).history(effective_from=effective_from, effective_to=effective_to,
                operation_from=operation_from, operation_to=operation_to)
        chosen = {}
        for row in rows:
            chosen.setdefault(row['doc_id'], row)
        revoked = self._revoked_documents() if scope is not None else set()
        return sorted((row for row in chosen.values() if scope is None or (scope_permits(row, scope) and row['doc_id'] not in revoked)),
                      key=lambda r: (r['title'], r['doc_id']))

    def effective_versions(self, product, as_of, *, known_at=None, factory=None, principal=None, allowed_scopes=None):
        """Select by business date and confirmation cutoff before applying authorization."""
        principal = self._principal(principal)
        scope = authorize_query(principal, product=product, factory=factory, allowed_scopes=allowed_scopes) if principal is not None else None
        as_of = _date(as_of, '业务日期')
        rows = Repository(self.root).history(operation_to=normalize_known_at(known_at))
        eligible = [r for r in rows if r['effective_from'] <= as_of]
        chosen = {}
        for row in sorted(eligible, key=lambda r: (r['effective_from'], r['confirmed_at'], r['version']), reverse=True):
            chosen.setdefault(row['doc_id'], row)
        effective = [r for r in chosen.values() if r['effective_to'] is None or as_of <= r['effective_to']]
        if scope is not None:
            revoked = self._revoked_documents()
            return sorted((r for r in effective if r['doc_id'] not in revoked and scope_permits(r, scope, product=product, factory=factory)), key=lambda r: r['title'])
        return sorted((r for r in effective if not r['scope_products'] or product is None or product in r['scope_products']), key=lambda r: r['title'])

    @staticmethod
    def _filters(start, end, op_start, op_end, operation_field):
        where, args = [], []
        if start:
            where.append('effective_from>=?'); args.append(_date(start, '业务生效起日'))
        if end:
            where.append('effective_from<=?'); args.append(_date(end, '业务生效止日'))
        for value, sign in ((op_start, '>='), (op_end, '<=')):
            if value:
                if isinstance(value, str) and len(value) == 10:
                    _date(value, '操作日期')
                    where.append(f'substr({operation_field},1,10){sign}?')
                else:
                    value = normalize_known_at(value)
                    where.append(f'{operation_field}{sign}?')
                args.append(value)
        return where, args

    def events(self, doc_id=None, *, effective_from=None, effective_to=None, operation_from=None,
               operation_to=None, principal=None, allowed_scopes=None):
        principal = self._principal(principal)
        scope = allowed_scope(principal, allowed_scopes) if principal is not None else None
        where, args = self._filters(effective_from, effective_to, operation_from, operation_to, 'operation_at')
        if doc_id:
            where.append('doc_id=?'); args.append(doc_id)
        conn = self._connect()
        if conn is None:
            return []
        try:
            rows = conn.execute('SELECT * FROM events' + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY event_id DESC', args)
            result = []
            revoked = self._revoked_documents() if scope is not None else set()
            for raw in rows:
                row = {**dict(raw), 'details': json.loads(raw['details'])}
                if scope is not None:
                    if row['doc_id'] in revoked:
                        continue
                    if row['version_id']:
                        version = self._version(conn.execute('SELECT * FROM versions WHERE version_id=?', (row['version_id'],)).fetchone())
                    else:
                        pending = conn.execute('SELECT payload FROM stages WHERE stage_id=?', (row['stage_id'],)).fetchone()
                        version = json.loads(pending['payload']) if pending else None
                    if not version or not scope_permits(version, scope):
                        continue
                result.append(row)
            return result
        finally:
            conn.close()
