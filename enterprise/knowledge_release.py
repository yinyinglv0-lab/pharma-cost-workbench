"""Single-organization, immutable knowledge releases and preauthorized retrieval.

No legacy Chroma/graph files are read. A release owns one SQLite artifact containing
chunks, lexical postings, term/evidence graph edges, and optional dense vectors.
All three candidate generators consume the SAME authorized version/chunk set.
No model is downloaded. Publishing is an explicit operation; imports are read-only.

Public entrypoints require the caller's trusted Principal. A supplied
allowed_scopes may narrow, never expand, that identity. The internal catalog
adapter is deliberately not a remotely exposed authorization boundary.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import date
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from uuid import uuid4

from enterprise.knowledge import (
    Repository, KnowledgeError, KnowledgeAccessError, VersionConflict,
    _authorize_version, _date, _now, allowed_scope, authorize_query,
    business_metadata, canonical_json, guarded_write, normalize_known_at,
    require_principal, scope_permits,
)

FORMAT_VERSION = 1
CHUNKER = {'name': 'character-window', 'version': 1, 'size': 900, 'overlap': 150}
TOKENIZER = {'name': 'unicode-cjk-overlapping-bigrams-ascii', 'version': 1}
GRAPH = {'name': 'term-evidence-bipartite', 'version': 1, 'hops': 2,
         'semantics': 'lexical co-occurrence, not a causal or expert relationship'}

_RELEASE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS releases (
 release_id TEXT PRIMARY KEY, status TEXT NOT NULL CHECK(status IN ('building','published','failed')),
 created_at TEXT NOT NULL, published_at TEXT, actor TEXT NOT NULL, base_release_id TEXT,
 manifest TEXT NOT NULL, manifest_sha256 TEXT NOT NULL, error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS active_release (singleton INTEGER PRIMARY KEY CHECK(singleton=1), release_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS revocations (
 doc_id TEXT PRIMARY KEY, revoked_at TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS release_terminal_immutable BEFORE UPDATE ON releases
 WHEN OLD.status IN ('published','failed') BEGIN SELECT RAISE(ABORT,'terminal releases are immutable'); END;
CREATE TRIGGER IF NOT EXISTS release_immutable_delete BEFORE DELETE ON releases
 BEGIN SELECT RAISE(ABORT,'release history is immutable'); END;
'''
_ARTIFACT_SCHEMA = '''
CREATE TABLE generation (release_id TEXT PRIMARY KEY);
CREATE TABLE chunks (chunk_id TEXT PRIMARY KEY, version_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
 text TEXT NOT NULL, metadata TEXT NOT NULL, tokens TEXT NOT NULL, token_count INTEGER NOT NULL,
 vector TEXT, UNIQUE(version_id,ordinal));
CREATE INDEX chunks_version ON chunks(version_id);
CREATE TABLE graph_edges (term TEXT NOT NULL, chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id),
 PRIMARY KEY(term,chunk_id));
CREATE INDEX graph_chunk ON graph_edges(chunk_id);
'''


def _digest(value):
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def _file_digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def _package_version(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return 'unavailable'


def tokenize(text):
    tokens = []
    for part in re.findall(r'[\u3400-\u9fff]+|[a-zA-Z0-9_]+', text.lower()):
        if '\u3400' <= part[0] <= '\u9fff':
            tokens.extend(part[i:i + 2] for i in range(len(part) - 1))
            if len(part) == 1:
                tokens.append(part)
        else:
            tokens.append(part)
    return tokens


def _metadata(version):
    return {**business_metadata(version), **{key: version[key] for key in (
        'doc_id', 'version_id', 'version', 'filename', 'sha256', 'text_sha256',
        'confirmed_at', 'parser', 'format', 'parse_metadata')}}


def _chunk_id(version, text, ordinal, start):
    return 'kr_' + _digest({'version_id': version['version_id'], 'text_sha256': version['text_sha256'],
        'metadata': _metadata(version), 'ordinal': ordinal, 'start': start, 'text': text})


def _chunks(version, chunker):
    step = chunker['size'] - chunker['overlap']
    for ordinal, start in enumerate(range(0, len(version['text']), step)):
        text = version['text'][start:start + chunker['size']]
        meta = {**_metadata(version), 'offset': start, 'ordinal': ordinal + 1,
                'kb': '受控知识文档', 'source': version['filename'], 'type': version['category']}
        page = re.findall(r'\[第(\d+)页\]', version['text'][:start + len(text)])
        if page:
            # Character offset is authoritative; a chunk can cross page boundaries.
            meta['page_hint'] = int(page[0] if start == 0 else page[-1])
        yield {'chunk_id': _chunk_id(version, text, ordinal, start), 'version_id': version['version_id'],
               'ordinal': ordinal, 'text': text, 'meta': meta, 'tokens': Counter(tokenize(text)), 'vector': None}
        if start + chunker['size'] >= len(version['text']):
            break


_MODEL_LOCK = threading.RLock()
_TORCH_RUNTIME_LOCK = threading.RLock()
_MODELS = {}
_FINGERPRINTS = {}


def _embedding_config():
    """Bound CPU concurrency; dense CLS, precision and token coverage stay fixed."""
    result = {'max_length': 1024, 'overflow': 'reject_without_truncation'}
    for name, key, default, maximum in (
        ('COST_EMBED_THREADS', 'threads', 4, 64),
        ('COST_EMBED_BATCH_SIZE', 'batch_size', 2, 32),
    ):
        try:
            value = int(os.environ.get(name, str(default)))
        except (ValueError, TypeError):
            raise KnowledgeError(f'{name}须为1至{maximum}的整数') from None
        if not 1 <= value <= maximum:
            raise KnowledgeError(f'{name}须为1至{maximum}的整数')
        result[key] = value
    return result


def _local_model_path(value=None):
    if value is None:
        from paths import MODEL_PATH
        value = MODEL_PATH
    if not value:
        return None
    path = Path(value).expanduser().resolve()
    return path if path.is_dir() else None


def _model_fingerprint(path):
    """Content hashes, including weights, cached only while file stats are stable."""
    files = sorted(p for p in path.rglob('*') if p.is_file() and '.git' not in p.parts)
    if not files or not (path / 'config.json').is_file():
        raise KnowledgeError('本机embedding目录缺少模型配置')
    signature = tuple((str(p.relative_to(path)), p.stat().st_size, p.stat().st_mtime_ns) for p in files)
    config = _embedding_config()
    key = (str(path), signature, canonical_json(config))
    with _MODEL_LOCK:
        cached = _FINGERPRINTS.get(key)
    if cached:
        return cached
    result = {'algorithm': 'BGE-M3-dense-cls', 'sha256': _digest([(name, _file_digest(path / name)) for name, _, _ in signature]),
              'file_count': len(files), 'torch': _package_version('torch'),
              'transformers': _package_version('transformers'), 'precision': 'fp32', 'device': 'cpu',
              **config, 'normalize': True, 'local_files_only': True}
    with _MODEL_LOCK:
        _FINGERPRINTS[key] = result
    return result


def _load_local_bge(path):
    # Use the dense BGE-M3 CLS path directly. Some installed FlagEmbedding
    # versions swallow local_files_only in **kwargs instead of forwarding it to
    # model/tokenizer loaders; strict offline loading must be enforced here.
    import torch
    from transformers import AutoModel, AutoTokenizer
    config = _embedding_config()
    with _TORCH_RUNTIME_LOCK:
        torch.set_num_threads(config['threads'])
        tokenizer = AutoTokenizer.from_pretrained(str(path), local_files_only=True, trust_remote_code=False)
        model = AutoModel.from_pretrained(str(path), local_files_only=True, trust_remote_code=False).float().eval().to('cpu')

    class LocalDenseBGE:
        runtime_config = config

        def encode(self, texts, batch_size=None):
            batch_size = config['batch_size'] if batch_size is None else batch_size
            vectors = []
            # PyTorch intra-op threads affect the process and must also be set
            # on the inference worker thread. Serialize CPU model generations so
            # one generation cannot change another's runtime mid-query.
            with _TORCH_RUNTIME_LOCK, torch.inference_mode():
                torch.set_num_threads(config['threads'])
                for offset in range(0, len(texts), batch_size):
                    tokens = tokenizer(texts[offset:offset + batch_size], padding=True, truncation=False,
                                       return_tensors='pt')
                    if tokens['input_ids'].shape[1] > config['max_length']:
                        raise KnowledgeError('embedding输入超过1024 token；请减小分块后重发，未截断正文')
                    dense = model(**tokens).last_hidden_state[:, 0]
                    vectors.extend(torch.nn.functional.normalize(dense, p=2, dim=1).cpu().tolist())
            return {'dense_vecs': vectors}
    return LocalDenseBGE()


class _ModelHandle:
    def __init__(self, path, fingerprint):
        self.path, self.fingerprint = path, fingerprint
        self.state, self.model, self.error = 'loading', None, ''
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='controlled-bge')
        self.capacity = threading.BoundedSemaphore(2)
        threading.Thread(target=self._load, name='controlled-bge-load', daemon=True).start()

    def _load(self):
        try:
            self.model = _load_local_bge(self.path)
            config = getattr(self.model, 'runtime_config', None)
            if config is not None and any(config[key] != self.fingerprint[key] for key in config):
                raise KnowledgeError('模型加载期间embedding运行配置改变，请重新预热')
            self.state = 'ready'
        except Exception as exc:
            self.error, self.state = type(exc).__name__, 'failed'
        finally:
            self.ready.set()

    def _encode(self, texts):
        with self.lock:
            output = self.model.encode(texts, batch_size=self.fingerprint['batch_size'])['dense_vecs']
            rows = output.tolist() if hasattr(output, 'tolist') else output
            result = [[float(value) for value in row] for row in rows]
            if len(result) != len(texts) or not result or not result[0]:
                raise KnowledgeError('embedding数量或维度无效')
            dimension = len(result[0])
            if any(len(row) != dimension or not all(math.isfinite(x) for x in row) or not any(row) for row in result):
                raise KnowledgeError('embedding包含无效数值')
            return result

    def encode(self, texts, timeout=None):
        if not self.capacity.acquire(blocking=False):
            raise KnowledgeError('embedding worker busy')
        future = self.pool.submit(self._encode, texts)
        future.add_done_callback(lambda _: self.capacity.release())
        try:
            return future.result(timeout=timeout)
        except FutureTimeout:
            # Queued jobs can be cancelled. A running native encoder cannot be
            # killed safely in a Python thread; the service supervisor must
            # restart this resident process if that encoder never returns.
            future.cancel()
            raise


def _model_handle(path, fingerprint):
    key = (str(path), _digest(fingerprint))
    with _MODEL_LOCK:
        if key not in _MODELS:
            _MODELS[key] = _ModelHandle(path, fingerprint)
        return _MODELS[key]


class ReleaseRepository:
    def __init__(self, root=None, *, repository=None):
        if root is not None and repository is not None:
            raise KnowledgeError('root和repository只指定一个')
        self.catalog = Repository(repository.root if repository is not None else root)
        self.principal = getattr(repository, 'principal', None)
        self.root = self.catalog.root
        self.db_path = self.root / 'knowledge_releases.db'
        self.artifact_dir = self.root / 'knowledge_releases'

    def _principal(self, principal):
        return require_principal(principal if principal is not None else self.principal)

    def _connect(self, write=False):
        if write:
            self.root.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.executescript(_RELEASE_SCHEMA)
        else:
            if not self.db_path.exists():
                return None
            conn = sqlite3.connect(self.db_path.as_uri() + '?mode=ro', uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _record(row):
        if row is None:
            return None
        value = dict(row)
        if hashlib.sha256(value['manifest'].encode('utf-8')).hexdigest() != value['manifest_sha256']:
            raise KnowledgeError('发布manifest校验失败')
        value['manifest'] = json.loads(value['manifest'])
        if any(value['manifest'].get(key) != value[key] for key in ('release_id', 'status', 'published_at')):
            raise KnowledgeError('发布状态与manifest不一致')
        value['generation'] = value['release_id']
        return value

    def _raw_release(self, release_id=None, known_at=None):
        conn = self._connect()
        if conn is None:
            return None
        try:
            if release_id is not None:
                row = conn.execute('SELECT * FROM releases WHERE release_id=?', (release_id,)).fetchone()
            elif known_at is not None:
                row = conn.execute("SELECT * FROM releases WHERE status='published' AND published_at<=? ORDER BY published_at DESC, release_id DESC LIMIT 1", (known_at,)).fetchone()
            else:
                row = conn.execute('SELECT r.* FROM releases r JOIN active_release a ON a.release_id=r.release_id WHERE a.singleton=1').fetchone()
            return self._record(row)
        finally:
            conn.close()

    def _visible_record(self, record, principal):
        if record is None:
            return None
        scope = allowed_scope(principal)
        manifest = record['manifest']
        documents = manifest.get('documents', [])
        revoked = self._revoked()
        visible = [doc for doc in documents if scope_permits(doc, scope) and doc['doc_id'] not in revoked]
        # Never expose a partial manifest as the verifiable original manifest.
        if len(visible) != len(documents):
            raise KnowledgeAccessError('完整发布清单包含授权范围以外的文档')
        return record

    def get_release(self, release_id=None, *, principal=None):
        principal = self._principal(principal)
        return self._visible_record(self._raw_release(release_id), principal)

    def history(self, *, principal=None):
        principal = self._principal(principal)
        conn = self._connect()
        if conn is None:
            return []
        try:
            result = []
            for row in conn.execute('SELECT * FROM releases ORDER BY created_at DESC, release_id DESC'):
                try:
                    result.append(self._visible_record(self._record(row), principal))
                except KnowledgeAccessError:
                    continue
            return result
        finally:
            conn.close()

    def _revoked(self):
        conn = self._connect()
        if conn is None:
            return set()
        try:
            return {row['doc_id'] for row in conn.execute('SELECT doc_id FROM revocations')}
        finally:
            conn.close()

    @guarded_write
    def revoke_document(self, doc_id, *, principal=None, reason):
        """Immediately block every release and historical query for a document."""
        principal = self._principal(principal)
        require_principal(principal, write=True)
        version = self.catalog.get(doc_id)
        if version is None:
            raise KnowledgeError('文档不存在')
        _authorize_version(version, principal, write=True)
        if not isinstance(reason, str) or not reason.strip():
            raise KnowledgeError('撤销原因必填')
        conn = self._connect(write=True)
        try:
            conn.execute('INSERT OR IGNORE INTO revocations VALUES(?,?,?,?)', (doc_id, _now(), principal.user_id, reason.strip()))
            conn.commit()
        finally:
            conn.close()

    @guarded_write
    def recover_interrupted_builds(self, *, principal=None):
        """Explicit recovery after a worker crash; never performed on import/read.

        Holding the shared process lock proves no compliant publisher is still
        building. Orphan artifacts remain for inspection and are never selected.
        """
        principal = self._principal(principal)
        require_principal(principal, write=True)
        conn = self._connect()
        if conn is None:
            return []
        try:
            pending = [self._record(row) for row in conn.execute("SELECT * FROM releases WHERE status='building'")]
        finally:
            conn.close()
        for record in pending:
            for document in record['manifest']['documents']:
                _authorize_version(document, principal, write=True)
        if not pending:
            return []
        conn = self._connect(write=True)
        try:
            for record in pending:
                manifest = {**record['manifest'], 'status': 'failed', 'published_at': None,
                            'recovered_at': _now(), 'recovered_by': principal.user_id}
                conn.execute("UPDATE releases SET status='failed',manifest=?,manifest_sha256=?,error=? WHERE release_id=? AND status='building'",
                             (canonical_json(manifest), _digest(manifest), 'interrupted_before_publication', record['release_id']))
            conn.commit()
        finally:
            conn.close()
        return [record['release_id'] for record in pending]

    @guarded_write
    def publish(self, version_ids=None, *, principal=None, embedding_model_path=None,
                require_embeddings=False, chunk_size=900, chunk_overlap=150, build_timeout=600.0):
        """Build a complete generation; fail without moving the active pointer.

        None selects ALL confirmed historical versions with explicit scope.
        Empty model path explicitly selects an offline lexical/graph release.
        Local BGE loading/encoding happens in this explicit build, never at import.
        Call from the application's managed job worker for a cold local model.
        """
        principal = self._principal(principal)
        require_principal(principal, write=True)
        if not isinstance(build_timeout, (int, float)) or not math.isfinite(build_timeout) or not 0 < build_timeout <= 3600:
            raise KnowledgeError('build_timeout须在0至3600秒之间')
        deadline = time.monotonic() + build_timeout
        if not isinstance(chunk_size, int) or not isinstance(chunk_overlap, int) or not 64 <= chunk_size <= 16000 or not 0 <= chunk_overlap < chunk_size:
            raise KnowledgeError('分块长度须为64至16000且overlap小于长度')
        all_versions = self.catalog.history()
        revoked = self._revoked()
        by_id = {v['version_id']: v for v in all_versions}
        if version_ids is None:
            version_ids = [v['version_id'] for v in all_versions if v['visibility'] in {'public', 'scoped'} and v['doc_id'] not in revoked]
        if not isinstance(version_ids, (list, tuple)) or any(not isinstance(v, str) for v in version_ids):
            raise KnowledgeError('version_ids须为已确认版本ID列表')
        ids = sorted(set(version_ids))
        if not ids:
            raise KnowledgeError('无可发布的已确认且范围明确的版本')
        versions = []
        for version_id in ids:
            if version_id not in by_id:
                raise KnowledgeError('发布版本不存在或未确认')
            _authorize_version(by_id[version_id], principal, write=True)
            if by_id[version_id]['doc_id'] in revoked:
                raise KnowledgeAccessError('撤回文档不得重新发布')
            versions.append(by_id[version_id])
        # A limited publisher cannot discard documents it cannot even read.
        previous = self._raw_release()
        if previous:
            for document in previous['manifest']['documents']:
                if document['doc_id'] not in revoked:
                    _authorize_version(document, principal, write=True)
        base_id = previous['release_id'] if previous else None
        release_id = uuid4().hex
        chunker = {**CHUNKER, 'size': chunk_size, 'overlap': chunk_overlap}
        parser_versions = {name: _package_version(name) for name in ('pypdf', 'python-docx')}
        parse_fp = {'catalog_parser_version': 2, 'packages': parser_versions,
                    'inputs': [{'version_id': v['version_id'], 'parser': v['parser'], 'text_sha256': v['text_sha256']} for v in versions]}
        manifest = {'schema_version': FORMAT_VERSION, 'release_id': release_id, 'generation': release_id,
                    'organization': 'default', 'created_at': _now(), 'published_at': None, 'status': 'building',
                    'input_version_ids': ids, 'documents': [_metadata(v) for v in versions],
                    'parse': {**parse_fp, 'fingerprint': _digest(parse_fp)},
                    'chunker': {**chunker, 'fingerprint': _digest(chunker)},
                    'bm25': {**TOKENIZER, 'fingerprint': _digest(TOKENIZER)},
                    'graph': {**GRAPH, 'fingerprint': _digest(GRAPH)},
                    'embedding': {'status': 'disabled', 'fingerprint': _digest({'embedding': 'none'})},
                    'reranker': {'status': 'disabled', 'reason': 'no uncalibrated rerank threshold'},
                    'degraded': True, 'degradation_reasons': [], 'artifacts': {}}
        conn = self._connect(write=True)
        try:
            conn.execute('INSERT INTO releases VALUES(?,?,?,?,?,?,?,?,?)', (release_id, 'building', manifest['created_at'], None,
                principal.user_id, base_id, canonical_json(manifest), _digest(manifest), ''))
            conn.commit()
        finally:
            conn.close()
        try:
            for version in versions:
                self.catalog.read_blob(version['sha256'], version_id=version['version_id'])
                if hashlib.sha256(version['text'].encode('utf-8')).hexdigest() != version['text_sha256']:
                    raise KnowledgeError('已确认正文哈希校验失败')
            rows = [chunk for version in versions for chunk in _chunks(version, chunker)]
            if not rows:
                raise KnowledgeError('发布不能包含空正文')
            path = _local_model_path(embedding_model_path)
            if path is None:
                if require_embeddings:
                    raise KnowledgeError('要求向量发布但本机BGE目录不存在')
                manifest['degradation_reasons'].append('local_embedding_unavailable')
            else:
                try:
                    fingerprint = _model_fingerprint(path)
                    handle = _model_handle(path, fingerprint)
                    if not handle.ready.wait(timeout=max(0, deadline - time.monotonic())):
                        raise KnowledgeError('embedding加载超过发布构建时限')
                    if handle.state != 'ready':
                        raise KnowledgeError('本机BGE加载失败: ' + handle.error)
                    for offset in range(0, len(rows), 32):
                        batch = rows[offset:offset + 32]
                        vectors = handle.encode([row['text'] for row in batch], timeout=max(0, deadline - time.monotonic()))
                        for row, vector in zip(batch, vectors):
                            row['vector'] = vector
                    manifest['embedding'] = {'status': 'ready', 'fingerprint': _digest(fingerprint),
                        'model': fingerprint, 'dimension': len(rows[0]['vector'])}
                    manifest['degraded'] = False
                except Exception as exc:
                    # Once an available local model was selected, a partial
                    # vector build is a failed release. An operator can request
                    # an explicit lexical release with embedding_model_path=''.
                    manifest['embedding'] = {'status': 'failed', 'fingerprint': _digest({'embedding': 'failed'}), 'error_type': type(exc).__name__}
                    raise
            folder = self.artifact_dir / release_id
            folder.mkdir(parents=True, exist_ok=False)
            temporary = folder / 'index.building.sqlite'
            self._build_artifact(temporary, release_id, rows)
            artifact = folder / 'index.sqlite'
            os.replace(temporary, artifact)
            manifest['chunk_ids'] = sorted(row['chunk_id'] for row in rows)
            manifest['chunk_ids_sha256'] = _digest(manifest['chunk_ids'])
            manifest['chunk_count'] = len(rows)
            manifest['artifacts'] = {'index.sqlite': {'sha256': _file_digest(artifact), 'bytes': artifact.stat().st_size,
                                    'generation': release_id, 'paths': ['bm25', 'vector', 'graph']}}
            self._validate_artifact(artifact, manifest)
            if time.monotonic() > deadline:
                raise KnowledgeError('索引构建超过发布时限')
            # The authoritative manifest lives in the same SQLite transaction
            # as publication status and the active pointer. A second JSON state
            # file would create a misleading published state on a failed switch.
            conn = self._connect(write=True)
            try:
                conn.execute('BEGIN IMMEDIATE')
                current = conn.execute('SELECT release_id FROM active_release WHERE singleton=1').fetchone()
                if (current['release_id'] if current else None) != base_id:
                    raise VersionConflict('构建期间已有新发布，请重新构建')
                manifest.update(status='published', published_at=_now())
                manifest_text = canonical_json(manifest)
                conn.execute("UPDATE releases SET status='published',published_at=?,manifest=?,manifest_sha256=? WHERE release_id=? AND status='building'", 
                             (manifest['published_at'], manifest_text, _digest(manifest), release_id))
                conn.execute('INSERT INTO active_release VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET release_id=excluded.release_id', (release_id,))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        except Exception as exc:
            manifest.update(status='failed', published_at=None)
            error = type(exc).__name__ + ': ' + str(exc)[:300]
            conn = self._connect(write=True)
            try:
                conn.execute("UPDATE releases SET status='failed',manifest=?,manifest_sha256=?,error=? WHERE release_id=? AND status='building'",
                             (canonical_json(manifest), _digest(manifest), error, release_id))
                conn.commit()
            finally:
                conn.close()
        return self.get_release(release_id, principal=principal)

    @staticmethod
    def _build_artifact(path, release_id, rows):
        conn = sqlite3.connect(path)
        try:
            conn.execute('PRAGMA foreign_keys=ON')
            conn.executescript(_ARTIFACT_SCHEMA)
            conn.execute('INSERT INTO generation VALUES(?)', (release_id,))
            for row in rows:
                conn.execute('INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?)', (row['chunk_id'], row['version_id'], row['ordinal'],
                    row['text'], canonical_json(row['meta']), canonical_json(row['tokens']), sum(row['tokens'].values()),
                    canonical_json(row['vector']) if row['vector'] is not None else None))
                terms = set(row['tokens']) | set(row['meta']['scope_products']) | set(row['meta']['scope_factories'])
                conn.executemany('INSERT INTO graph_edges VALUES(?,?)', [(term, row['chunk_id']) for term in sorted(terms) if len(term) >= 2])
            conn.commit()
            if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise KnowledgeError('索引SQLite完整性校验失败')
        finally:
            conn.close()

    @staticmethod
    def _validate_artifact(path, manifest):
        if _file_digest(path) != manifest['artifacts']['index.sqlite']['sha256']:
            raise KnowledgeError('发布索引哈希校验失败')
        conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        try:
            if conn.execute('SELECT release_id FROM generation').fetchone()[0] != manifest['release_id']:
                raise KnowledgeError('索引generation不一致')
            ids = [row[0] for row in conn.execute('SELECT chunk_id FROM chunks ORDER BY chunk_id')]
            if len(ids) != manifest['chunk_count'] or ids != manifest['chunk_ids'] or _digest(ids) != manifest['chunk_ids_sha256']:
                raise KnowledgeError('发布chunk清单不一致')
            version_ids = {row[0] for row in conn.execute('SELECT DISTINCT version_id FROM chunks')}
            if version_ids != set(manifest['input_version_ids']):
                raise KnowledgeError('发布输入版本清单不一致')
            vector_count = conn.execute('SELECT COUNT(*) FROM chunks WHERE vector IS NOT NULL').fetchone()[0]
            if vector_count != (len(ids) if manifest['embedding']['status'] == 'ready' else 0):
                raise KnowledgeError('向量构建不完整')
        finally:
            conn.close()


class ControlledSearchEngine:
    """Resident engine; authorization decisions are deliberately never cached."""
    def __init__(self, repository=None, *, root=None, embedding_model_path=None):
        self.releases = ReleaseRepository(root, repository=repository)
        self.embedding_model_path = embedding_model_path
        self._lock = threading.RLock()
        self._warm_state = {'state': 'not_started'}
        self._warm_requested = None
        self._handle = None

    def warmup(self, *, principal, wait=False, expected_fingerprint=None, force=False):
        require_principal(principal)
        # Model filesystem hashing and loading both happen outside request threads.
        with self._lock:
            changed = expected_fingerprint is not None and expected_fingerprint != self._warm_requested and expected_fingerprint != self._warm_state.get('fingerprint')
            if self._warm_state['state'] != 'loading' and (self._warm_state['state'] == 'not_started' or changed or force):
                self._warm_requested = expected_fingerprint
                self._warm_state = {'state': 'loading'}
                def load():
                    try:
                        path = _local_model_path(self.embedding_model_path)
                        if path is None:
                            self._warm_state = {'state': 'unavailable'}
                            return
                        fingerprint = _model_fingerprint(path)
                        handle = _model_handle(path, fingerprint)
                        handle.ready.wait()
                        self._handle = handle
                        self._warm_state = {'state': handle.state, 'fingerprint': _digest(fingerprint), 'error_type': handle.error}
                    except Exception as exc:
                        self._warm_state = {'state': 'failed', 'error_type': type(exc).__name__}
                self._warm_thread = threading.Thread(target=load, daemon=True, name='knowledge-warmup')
                self._warm_thread.start()
        if wait:
            self._warm_thread.join()
        return dict(self._warm_state)

    def _load_authorized(self, record, version_ids):
        if not version_ids:
            return [], {}
        manifest = record['manifest']
        path = self.releases.artifact_dir / record['release_id'] / 'index.sqlite'
        # Verify content, not only mtime/size: equal-length corruption must not
        # survive a warm-process cache. Model objects remain cached separately.
        self.releases._validate_artifact(path, manifest)
        conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        try:
            # This is an authorized ID set, fixed once and shared by all routes.
            conn.execute('CREATE TEMP TABLE permitted(version_id TEXT PRIMARY KEY)')
            conn.executemany('INSERT INTO permitted VALUES(?)', [(v,) for v in sorted(version_ids)])
            rows = []
            for row in conn.execute('SELECT c.* FROM chunks c JOIN permitted p ON p.version_id=c.version_id'):
                rows.append({'chunk_id': row['chunk_id'], 'version_id': row['version_id'], 'text': row['text'],
                    'meta': json.loads(row['metadata']), 'tokens': json.loads(row['tokens']), 'token_count': row['token_count'],
                    'vector': json.loads(row['vector']) if row['vector'] else None})
            graph = defaultdict(set)
            for edge in conn.execute('SELECT e.term,e.chunk_id FROM graph_edges e JOIN chunks c ON c.chunk_id=e.chunk_id JOIN permitted p ON p.version_id=c.version_id'):
                graph[edge['term']].add(edge['chunk_id'])
            return rows, graph
        finally:
            conn.close()

    @staticmethod
    def _bm25(query, rows):
        terms = set(tokenize(query))
        if not rows or not terms:
            return []
        count = len(rows)
        average = sum(row['token_count'] for row in rows) / count or 1
        df = Counter(term for row in rows for term in terms if term in row['tokens'])
        scored = []
        for row in rows:
            score = 0.0
            for term in terms:
                freq = row['tokens'].get(term, 0)
                if freq:
                    idf = math.log(1 + (count - df[term] + 0.5) / (df[term] + 0.5))
                    score += idf * freq * 2.5 / (freq + 1.5 * (0.25 + 0.75 * row['token_count'] / average))
            if score > 0:
                scored.append((row['chunk_id'], score))
        return sorted(scored, key=lambda pair: (-pair[1], pair[0]))

    @staticmethod
    def _graph(query, rows, graph):
        seeds = {term for term in tokenize(query) if term in graph}
        seeds.update(term for term in graph if len(term) >= 3 and term in query)
        if not seeds:
            return []
        direct = Counter(chunk for term in seeds for chunk in graph[term])
        # Two-hop expansion is limited to the authorized bipartite graph.
        related = {term for term, chunks in graph.items() if chunks & set(direct)}
        scores = Counter({chunk: 2.0 * count for chunk, count in direct.items()})
        for term in related:
            for chunk in graph[term]:
                scores[chunk] += 1.0 / (1 + len(graph[term]))
        return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))

    @staticmethod
    def _vector(query_vector, rows):
        norm = math.sqrt(sum(x * x for x in query_vector))
        scored = []
        for row in rows:
            vector = row['vector']
            if vector is None or len(vector) != len(query_vector):
                raise KnowledgeError('查询模型与持久向量维度不一致')
            denom = norm * math.sqrt(sum(x * x for x in vector))
            score = sum(a * b for a, b in zip(query_vector, vector)) / denom if denom else 0.0
            if score > 0:
                scored.append((row['chunk_id'], score))
        return sorted(scored, key=lambda pair: (-pair[1], pair[0]))

    def search(self, query, *, principal=None, product=None, factory=None, as_of=None, known_at=None,
               allowed_scopes=None, release_id=None, top_k=10, vector_timeout=3.0):
        principal = self.releases._principal(principal)
        scope = authorize_query(principal, product=product, factory=factory, allowed_scopes=allowed_scopes)
        as_of = _date(as_of or date.today().isoformat(), '业务日期')
        cutoff = normalize_known_at(known_at)
        if not isinstance(query, str) or not query.strip() or len(query) > 4000:
            raise KnowledgeError('query须为1至4000字符')
        if not isinstance(top_k, int) or not 1 <= top_k <= 100:
            raise KnowledgeError('top_k须为1至100')
        if not isinstance(vector_timeout, (int, float)) or not math.isfinite(vector_timeout) or not 0 < vector_timeout <= 30:
            raise KnowledgeError('vector_timeout须为0至30秒')
        record = self.releases._raw_release(release_id, cutoff if known_at is not None and release_id is None else None)
        stats = {'release_id': None, 'generation': None, 'as_of': as_of, 'known_at': cutoff,
                 'no_answer': True, 'reason': 'no_published_release', 'degraded': True,
                 'degradation_reasons': [], 'retrieval_mode': 'unavailable', 'vector_n': 0, 'bm25_n': 0,
                 'graph_n': 0, 'fused_n': 0, 'reranked': False, 'authorized_version_ids': [],
                 'route_generations': {}, 'authorization': 'before_all_candidates', 'organization': 'default'}
        if record is None:
            return [], stats
        if record['status'] != 'published':
            raise KnowledgeError('只允许检索published发布')
        if record['published_at'] > cutoff:
            stats['reason'] = 'release_not_known_at_cutoff'
            return [], stats
        manifest = record['manifest']
        stats.update(release_id=record['release_id'], generation=record['release_id'])
        # Choose the currently known effective catalog version before intersecting
        # with the release: an unpublished replacement cannot revive its old text.
        selected = self.releases.catalog.effective_versions(product, as_of, known_at=cutoff,
                         factory=factory, principal=principal, allowed_scopes=allowed_scopes)
        revoked = self.releases._revoked()
        published = set(manifest['input_version_ids'])
        allowed = {v['version_id'] for v in selected if v['version_id'] in published and v['doc_id'] not in revoked
                   and scope_permits(v, scope, product=product, factory=factory)
                   and not v['business_metadata'].get('is_demo') and not v['business_metadata'].get('is_simulation')}
        stats['authorized_version_ids'] = sorted(allowed)
        stats['route_generations'] = {name: record['release_id'] for name in ('bm25', 'vector', 'graph')}
        if not allowed:
            stats['reason'] = 'no_authorized_effective_published_versions'
            return [], stats
        rows, graph = self._load_authorized(record, allowed)
        lexical = self._bm25(query, rows)[:max(top_k * 3, 30)]
        graph_results = self._graph(query, rows, graph)[:max(top_k * 3, 30)]
        vector_results = []
        reasons = list(manifest['degradation_reasons'])
        if manifest['embedding']['status'] == 'ready':
            warm = self.warmup(principal=principal, expected_fingerprint=manifest['embedding']['fingerprint'])
            if warm['state'] != 'ready':
                reasons.append('embedding_' + warm['state'])
            elif warm['fingerprint'] != manifest['embedding']['fingerprint']:
                reasons.append('embedding_fingerprint_mismatch')
            else:
                try:
                    vector = self._handle.encode([query], timeout=vector_timeout)[0]
                    vector_results = self._vector(vector, rows)[:max(top_k * 3, 30)]
                except FutureTimeout:
                    reasons.append('query_embedding_timeout')
                except Exception as exc:
                    reasons.append('query_embedding_failed:' + type(exc).__name__)
        else:
            reasons.append('release_has_no_vectors')
        scores, routes = Counter(), defaultdict(dict)
        for name, results in (('bm25', lexical), ('vector', vector_results), ('graph', graph_results)):
            for rank, (chunk_id, score) in enumerate(results):
                scores[chunk_id] += 1.0 / (60 + rank + 1)
                routes[chunk_id][name] = score
        by_id = {row['chunk_id']: row for row in rows}
        # A revocation may arrive while the local encoder runs. Recheck before
        # returning evidence as well as before candidate generation.
        revoked_now = self.releases._revoked()
        withdrawn = {key for key, row in by_id.items() if row['meta']['doc_id'] in revoked_now}
        for key in withdrawn:
            scores.pop(key, None)
        if withdrawn:
            stats['authorized_version_ids'] = sorted({row['version_id'] for key, row in by_id.items() if key not in withdrawn})
        result = []
        for chunk_id, score in sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))[:top_k]:
            row = by_id[chunk_id]
            result.append({'text': row['text'], 'chunk_id': chunk_id, 'version_id': row['version_id'],
                'document_id': row['meta']['doc_id'], 'release_id': record['release_id'], 'generation': record['release_id'],
                'meta': {**row['meta'], 'release_id': record['release_id'], 'generation': record['release_id']},
                'score': score, 'route_scores': routes[chunk_id], 'rerank_score': None})
        stats.update(no_answer=not bool(result), reason='' if result else 'no_matching_evidence',
                     degraded=bool(reasons), degradation_reasons=sorted(set(reasons)),
                     retrieval_mode='vector_bm25_graph' if not reasons else 'bm25_graph_fallback',
                     vector_n=len(vector_results), bm25_n=len(lexical), graph_n=len(graph_results), fused_n=len(scores))
        return result, stats


_ENGINES = {}
_ENGINE_LOCK = threading.Lock()


def get_search_engine(repository=None, *, root=None, embedding_model_path=None):
    """Process-resident reusable engine; pass principal on every request."""
    if root is not None and repository is not None:
        raise KnowledgeError('root和repository只指定一个')
    location = Repository(repository.root if repository is not None else root).root
    key = (str(location), str(embedding_model_path))
    with _ENGINE_LOCK:
        if key not in _ENGINES:
            _ENGINES[key] = ControlledSearchEngine(root=location, embedding_model_path=embedding_model_path)
        return _ENGINES[key]


def result_to_api(item):
    meta = item['meta']
    return {'text': item['text'], 'source': meta['filename'], 'type': meta['category'], 'kb': meta['kb'],
            'chunk_id': item['chunk_id'], 'version_id': item['version_id'], 'document_id': item['document_id'],
            'release_id': item['release_id'], 'generation': item['generation'], 'score': item['score'],
            'rerank_score': None, 'scope_unknown': False, 'is_demo': False, 'is_sim_case': False,
            'scope_products': meta['scope_products'], 'scope_factories': meta['scope_factories'],
            'effective_from': meta['effective_from'], 'effective_to': meta['effective_to'],
            'confirmed_at': meta['confirmed_at'], 'document_sha256': meta['sha256'],
            'offset': meta['offset'], 'ordinal': meta['ordinal'], 'route_scores': item['route_scores'],
            'business_metadata': meta['business_metadata'],
            'evidence_role': meta['business_metadata'].get('evidence_role', 'context_only'),
            'authority': meta['business_metadata'].get('authority', 'unreviewed'),
            'limitations': meta['business_metadata'].get('limitations', []),
            'known_conflicts': meta['business_metadata'].get('known_conflicts', [])}


def bootstrap_official(source_root, *, repository, principal, documents, reason, publish=False,
                       embedding_model_path=None, require_embeddings=False, build_timeout=600.0):
    """Explicit, caller-reviewed import; never runs automatically on startup.

    documents is an allowlist of dicts with filename/title/scope_products/
    scope_factories/visibility/effective_from/category (optional effective_to,
    metadata, sha256). Source files must be direct PDF/CSV children. Each source
    keeps its complete binary original; all metadata is supplied by the caller.
    Public visibility is valid only for explicitly reviewed public documents.
    """
    require_principal(principal, write=True)
    if not isinstance(build_timeout, (int, float)) or not math.isfinite(build_timeout) or not 0 < build_timeout <= 3600:
        raise KnowledgeError('build_timeout须在0至3600秒之间')
    if not isinstance(reason, str) or not reason.strip():
        raise KnowledgeError('官方原件导入须有核准原因')
    source_root = Path(source_root).resolve()
    if not isinstance(documents, list) or not documents:
        raise KnowledgeError('须显式列出已核准官方原件和业务metadata')
    prepared = []
    for spec in documents:
        filename = spec.get('filename', '')
        path = (source_root / filename).resolve()
        if path.parent != source_root or path.suffix.lower() not in {'.pdf', '.csv'} or not path.is_file():
            raise KnowledgeError('bootstrap仅允许源目录内明确列出的PDF/CSV原件')
        content = path.read_bytes()
        if spec.get('sha256') and hashlib.sha256(content).hexdigest() != spec['sha256']:
            raise KnowledgeError('官方原件与核准SHA256不符')
        _authorize_version({**spec, 'business_metadata': spec.get('metadata', {})}, principal, write=True)
        prepared.append((spec, content))
    imported = []
    for spec, content in prepared:
        existing = next((v for v in repository.list_documents(principal=principal) if v['title'] == spec['title']), None)
        unknown = None
        if existing is None and 'knowledge_admin' in principal.roles and all('*' in getattr(principal, key) for key in ('products', 'factories')):
            unknown = next((v for v in Repository(repository.root).list_documents()
                            if v['title'] == spec['title'] and v['visibility'] == 'unknown'), None)
        if unknown is not None:
            if unknown['sha256'] != hashlib.sha256(content).hexdigest():
                raise KnowledgeError('旧未知范围文档与核准原件不同，请单独复核后迁移')
            pending = repository.stage_scope_review(unknown['doc_id'], expected_version_id=unknown['version_id'],
                expected_sha256=unknown['sha256'], principal=principal, scope_products=spec['scope_products'],
                scope_factories=spec['scope_factories'], visibility=spec['visibility'], effective_from=spec['effective_from'],
                effective_to=spec.get('effective_to'), metadata=spec.get('metadata', {}), review_reason=reason)
        else:
            pending = repository.stage(content, spec['filename'], spec['title'], spec['scope_products'], spec['effective_from'],
                spec['category'], principal.user_id, doc_id=existing['doc_id'] if existing else None,
                effective_to=spec.get('effective_to'), scope_factories=spec['scope_factories'], visibility=spec['visibility'],
                metadata=spec.get('metadata', {}), principal=principal)
        if pending['errors']:
            raise KnowledgeError('; '.join(pending['errors']))
        if pending['stage_id']:
            value = repository.commit(pending['stage_id'], principal.user_id, reason, principal=principal)
        else:
            value = repository.get(version_id=pending['change']['duplicate_of']['version_id'], principal=principal)
        imported.append(value)
    release = ReleaseRepository(repository=repository).publish(principal=principal,
        embedding_model_path=embedding_model_path, require_embeddings=require_embeddings,
        build_timeout=build_timeout) if publish else None
    return {'versions': imported, 'release': release, 'status': release['status'] if release else 'confirmed_not_published'}
