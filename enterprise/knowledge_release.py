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

from enterprise.knowledge_applicability import (
    APPLICABILITY, MATCHING_VIEW, chunk_metadata, matching_view, source_sections,
)
from enterprise.domain_graph import (
    DOMAIN_GRAPH, DOMAIN_GRAPH_V4, ENTITY_ALIASES, DomainGraphIndex, build_domain_graph, load_domain_graph,
)
from enterprise.domain_vocabulary import (
    VOCABULARY_SCHEMA, explicit_other_domain, vocabulary_from_metadata, vocabulary_fingerprint,
)
from enterprise.domain_keywords import KEYWORDS, SYNONYMS, expand_query
from enterprise.tabular_knowledge import (
    TABULAR, knowledge_type, normalize_knowledge_types, source_period_reason, table_chunks,
)

# ``schema_version`` remains 2 for old chunk/parser consumers. The additive
# formal retrieval contract is recorded separately so old releases and clients
# remain readable while new releases carry the domain graph and controlled ranker.
FORMAT_VERSION = 2
RETRIEVAL_SCHEMA_VERSION = 3
CHUNKER = {'name': 'product-section-and-schema-records', 'version': 2, 'size': 900, 'overlap': 150,
           'tabular': TABULAR}
TOKENIZER = {'name': 'unicode-cjk-overlapping-bigrams-ascii', 'version': 2, 'matching_view': MATCHING_VIEW}
GRAPH = {'name': 'term-evidence-bipartite', 'version': 1, 'hops': 2,
         'semantics': 'lexical co-occurrence, not a causal or expert relationship'}
RERANKER = {'name': 'controlled-domain-lexical-reranker', 'version': 1, 'neural': False,
            'score': 'weighted entity coverage, relation support, exact phrase and RRF',
            'semantic_boundary': 'ranking aid only; no calibrated relevance or causal claim'}

# Response display bound only, not a ranking or persisted release parameter.
# A bounded evidence list is not a claim of complete process-path coverage.
DOMAIN_EVIDENCE_LIMIT = 8

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
CREATE TABLE graph_configuration (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 configuration TEXT NOT NULL, fingerprint TEXT NOT NULL);
CREATE TABLE chunks (chunk_id TEXT PRIMARY KEY, version_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
 text TEXT NOT NULL, metadata TEXT NOT NULL, tokens TEXT NOT NULL, token_count INTEGER NOT NULL,
 vector TEXT, UNIQUE(version_id,ordinal));
CREATE INDEX chunks_version ON chunks(version_id);
CREATE TABLE graph_edges (term TEXT NOT NULL, chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id),
 PRIMARY KEY(term,chunk_id));
CREATE INDEX graph_chunk ON graph_edges(chunk_id);
CREATE TABLE domain_entities (entity_id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, name TEXT NOT NULL,
  aliases TEXT NOT NULL);
CREATE TABLE domain_mentions (entity_id TEXT NOT NULL REFERENCES domain_entities(entity_id), entity_type TEXT NOT NULL,
  name TEXT NOT NULL, alias TEXT NOT NULL, chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id),
  version_id TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL, quote TEXT NOT NULL, source TEXT NOT NULL,
  PRIMARY KEY(entity_id,chunk_id,start,end));
CREATE INDEX domain_mentions_version ON domain_mentions(version_id);
CREATE TABLE domain_relations (relation_id TEXT PRIMARY KEY, source_entity_id TEXT NOT NULL REFERENCES domain_entities(entity_id),
  target_entity_id TEXT NOT NULL REFERENCES domain_entities(entity_id), relation_type TEXT NOT NULL,
  source_type TEXT NOT NULL, target_type TEXT NOT NULL, semantic_status TEXT NOT NULL,
  chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id), version_id TEXT NOT NULL, support TEXT NOT NULL);
CREATE INDEX domain_relations_version ON domain_relations(version_id);
CREATE INDEX domain_relations_chunk ON domain_relations(chunk_id);
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
    for part in re.findall(r'[\u3400-\u9fff]+|[a-zA-Z0-9_]+', matching_view(text).lower()):
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


def _graph_configuration(documents):
    """Freeze per-version matching semantics; never access a runtime profile."""
    configured = any('graph_vocabulary' in (doc.get('business_metadata') or {})
                     or explicit_other_domain(doc) for doc in documents)
    if not configured:
        return None  # Historical pharma V3 manifests remain in their old form.
    by_version, entries = {}, {}
    for document in documents:
        vocabulary = vocabulary_from_metadata(document)
        if vocabulary is not None:
            fingerprint = vocabulary_fingerprint(vocabulary)
            entries[fingerprint] = vocabulary
            by_version[document['version_id']] = {'mode': 'reviewed_vocabulary',
                'vocabulary_fingerprint': fingerprint,
                'source_config_fingerprint': vocabulary['source_config_fingerprint']}
        elif explicit_other_domain(document):
            by_version[document['version_id']] = {'mode': 'explicit_domain_without_vocabulary'}
        else:
            by_version[document['version_id']] = {'mode': 'legacy_pharma_vocabulary',
                                                 'vocabulary_fingerprint': _digest(ENTITY_ALIASES)}
    frozen = {'schema_version': VOCABULARY_SCHEMA, 'by_version': by_version, 'entries': entries}
    if any(entry['mode'] == 'legacy_pharma_vocabulary' for entry in by_version.values()):
        frozen['legacy_pharma_aliases'] = json.loads(canonical_json(ENTITY_ALIASES))
    return {**frozen, 'fingerprint': _digest(frozen)}


def _chunk_id(version, text, ordinal, start, chunker=None):
    identity = {'version_id': version['version_id'], 'text_sha256': version['text_sha256'],
                'metadata': _metadata(version), 'ordinal': ordinal, 'start': start, 'text': text}
    if chunker is not None and chunker.get('version', 1) >= 2:
        identity.update(chunker=chunker, applicability=APPLICABILITY)
    return 'kr_' + _digest(identity)


def _chunks(version, chunker):
    original = version['text']
    if version.get('format') == 'csv' and chunker.get('tabular', {}).get('version') == 1:
        for ordinal, (start, end, meta) in enumerate(table_chunks(original, _metadata(version))):
            text = original[start:end]
            meta.update(ordinal=ordinal + 1, kb='受控知识文档', source=version['filename'], type=version['category'])
            yield {'chunk_id': _chunk_id(version, text, ordinal, start, chunker),
                   'version_id': version['version_id'], 'ordinal': ordinal, 'text': text, 'meta': meta,
                   'tokens': Counter(tokenize(text)), 'vector': None}
        return
    legacy = chunker.get('version', 1) == 1
    sections = source_sections(original, _metadata(version))
    ranges = [{'offset': 0, 'end_offset': len(original)}] if legacy else sections
    step, ordinal = chunker['size'] - chunker['overlap'], 0
    for section in ranges:
        for start in range(section['offset'], section['end_offset'], step):
            end = min(start + chunker['size'], section['end_offset'])
            text = original[start:end]
            meta = {**_metadata(version), 'offset': start, 'ordinal': ordinal + 1,
                    'kb': '受控知识文档', 'source': version['filename'], 'type': version['category']}
            if legacy:
                page = re.findall(r'\[第(\d+)页\]', original[:end])
                if page:
                    meta['page_hint'] = int(page[0] if start == 0 else page[-1])
            else:
                meta = chunk_metadata(original, meta, start, end, sections=sections)
            yield {'chunk_id': _chunk_id(version, text, ordinal, start, chunker),
                   'version_id': version['version_id'], 'ordinal': ordinal, 'text': text, 'meta': meta,
                   'tokens': Counter(tokenize(text)), 'vector': None}
            ordinal += 1
            if end >= section['end_offset']:
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
        graph_configuration = _graph_configuration([_metadata(v) for v in versions])
        graph_spec = DOMAIN_GRAPH_V4 if graph_configuration is not None else DOMAIN_GRAPH
        manifest = {'schema_version': FORMAT_VERSION, 'retrieval_schema_version': RETRIEVAL_SCHEMA_VERSION,
                     'release_id': release_id, 'generation': release_id,
                    'organization': 'default', 'created_at': _now(), 'published_at': None, 'status': 'building',
                    'input_version_ids': ids, 'documents': [_metadata(v) for v in versions],
                    'parse': {**parse_fp, 'fingerprint': _digest(parse_fp)},
                    'chunker': {**chunker, 'fingerprint': _digest(chunker)},
                    **({'applicability': {**APPLICABILITY, 'fingerprint': _digest(APPLICABILITY)}}
                       if chunker.get('version', 1) >= 2 else {}),
                    'bm25': {**TOKENIZER, 'fingerprint': _digest(TOKENIZER)},
                     'domain_keywords': {**KEYWORDS, 'entries': SYNONYMS,
                                         'fingerprint': _digest({'metadata': KEYWORDS, 'entries': SYNONYMS})},
                    'graph': {**GRAPH, 'fingerprint': _digest(GRAPH)},
            'domain_graph': {**graph_spec, 'status': 'building',
                                     'fingerprint': _digest(graph_spec), 'counts': {},
                                     **({'vocabulary': graph_configuration} if graph_configuration is not None else {})},
                    'embedding': {'status': 'disabled', 'fingerprint': _digest({'embedding': 'none'})},
                    'reranker': {**RERANKER, 'status': 'ready', 'fingerprint': _digest(RERANKER)},
                     'rrf': {'name': 'reciprocal-rank-fusion', 'version': 1, 'k': 60,
            'routes': ['bm25', 'vector', 'graph', 'domain_graph'], 'equal_weight': True},
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
                    manifest['vector_reuse'] = {'status': 'validating', 'source_release_id': base_id,
                                                'reused_count': 0, 'encoded_count': 0}
                    manifest['vector_reuse'] = self._reuse_dense_vectors(previous, rows, fingerprint)
                    pending = [row for row in rows if row['vector'] is None]
                    if pending:
                        handle = _model_handle(path, fingerprint)
                        if not handle.ready.wait(timeout=max(0, deadline - time.monotonic())):
                            raise KnowledgeError('embedding加载超过发布构建时限')
                        if handle.state != 'ready':
                            raise KnowledgeError('本机BGE加载失败: ' + handle.error)
                        for offset in range(0, len(pending), 32):
                            batch = pending[offset:offset + 32]
                            vectors = handle.encode([matching_view(row['text']) for row in batch], timeout=max(0, deadline - time.monotonic()))
                            for row, vector in zip(batch, vectors):
                                row['vector'] = vector
                            manifest['vector_reuse']['encoded_count'] += len(batch)
                    dimension = len(rows[0]['vector'])
                    if any(not self._valid_dense_vector(row['vector'], dimension) for row in rows):
                        raise KnowledgeError('复用及新增向量的维度或数值不一致')
                    manifest['vector_reuse']['status'] = 'complete'
                    manifest['embedding'] = {'status': 'ready', 'fingerprint': _digest(fingerprint),
                        'model': fingerprint, 'dimension': dimension}
                    manifest['degraded'] = False
                except Exception as exc:
                    # Once an available local model was selected, a partial
                    # vector build is a failed release. An operator can request
                    # an explicit lexical release with embedding_model_path=''.
                    manifest['embedding'] = {'status': 'failed', 'fingerprint': _digest({'embedding': 'failed'}), 'error_type': type(exc).__name__}
                    if 'vector_reuse' in manifest:
                        manifest['vector_reuse'].update(status='failed', error_type=type(exc).__name__)
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
            with sqlite3.connect(artifact.as_uri() + '?mode=ro', uri=True) as domain_conn:
                 domain_counts = {
                     'entities': domain_conn.execute('SELECT COUNT(*) FROM domain_entities').fetchone()[0],
                     'mentions': domain_conn.execute('SELECT COUNT(*) FROM domain_mentions').fetchone()[0],
                     'relations': domain_conn.execute('SELECT COUNT(*) FROM domain_relations').fetchone()[0],
                 }
            manifest['domain_graph'].update(status='ready', counts=domain_counts)
            manifest['artifacts'] = {'index.sqlite': {'sha256': _file_digest(artifact), 'bytes': artifact.stat().st_size,
            'generation': release_id, 'paths': ['bm25', 'vector', 'graph', 'domain_graph']}}
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
    def _valid_dense_vector(vector, dimension):
        return (isinstance(dimension, int) and not isinstance(dimension, bool) and dimension > 0
                and isinstance(vector, list) and len(vector) == dimension
                and all(isinstance(value, (float, int)) and not isinstance(value, bool)
                        and math.isfinite(value) for value in vector) and any(vector))

    def _reuse_dense_vectors(self, previous, rows, fingerprint):
        """Copy only verified, identical dense inputs from the pinned base release.

        A changed model/configuration or input is encoded afresh. Integrity
        failure in an otherwise eligible base is fatal, keeping its pointer and
        retaining a failed build record; corrupted artifacts never seed vectors.
        """
        trace = {'status': 'pending', 'source_release_id': previous['release_id'] if previous else None,
                 'reused_count': 0, 'encoded_count': 0, 'invalid_vector_count': 0,
                 'new_input_count': len(rows), 'policy': 'verified-base_same-fingerprint_chunk-id_text-sha256'}
        if not previous or previous.get('status') != 'published':
            return {**trace, 'reason': 'no_published_base'}
        old = previous['manifest']
        embedding = old.get('embedding', {})
        if embedding.get('status') != 'ready':
            return {**trace, 'reason': 'base_has_no_ready_vectors'}
        if embedding.get('fingerprint') != _digest(fingerprint) or embedding.get('model') != fingerprint:
            return {**trace, 'reason': 'embedding_fingerprint_changed'}
        dimension = embedding.get('dimension')
        if not isinstance(dimension, int) or isinstance(dimension, bool) or dimension <= 0:
            return {**trace, 'reason': 'base_dimension_invalid'}
        artifact = self.artifact_dir / previous['release_id'] / 'index.sqlite'
        self._validate_artifact(artifact, old)
        targets = {row['chunk_id']: row for row in rows}
        copied = []
        with sqlite3.connect(artifact.as_uri() + '?mode=ro', uri=True) as conn:
            for chunk_id, version_id, text, raw_vector in conn.execute(
                    'SELECT chunk_id,version_id,text,vector FROM chunks'):
                row = targets.get(chunk_id)
                if row is None or row['version_id'] != version_id:
                    continue
                text_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
                if text_hash != hashlib.sha256(row['text'].encode('utf-8')).hexdigest():
                    continue
                try:
                    vector = json.loads(raw_vector)
                except (ValueError, TypeError):
                    vector = None
                if not self._valid_dense_vector(vector, dimension):
                    trace['invalid_vector_count'] += 1
                    continue
                copied.append((row, vector))
        # Avoid copying from a source modified between hash validation and read.
        if _file_digest(artifact) != old['artifacts']['index.sqlite']['sha256']:
            raise KnowledgeError('复用来源索引在读取期间改变')
        for row, vector in copied:
            row['vector'] = vector
        trace.update(reused_count=len(copied), new_input_count=len(rows) - len(copied),
                     source_artifact_sha256=old['artifacts']['index.sqlite']['sha256'],
                     source_embedding_fingerprint=embedding['fingerprint'],
                     reason='verified_base_checked')
        return trace

    @staticmethod
    def _build_artifact(path, release_id, rows):
        conn = sqlite3.connect(path)
        try:
            conn.execute('PRAGMA foreign_keys=ON')
            conn.executescript(_ARTIFACT_SCHEMA)
            conn.execute('INSERT INTO generation VALUES(?)', (release_id,))
            configuration = _graph_configuration(list({row['version_id']: row['meta'] for row in rows}.values()))
            if configuration is not None:
                conn.execute('INSERT INTO graph_configuration VALUES(1,?,?)',
                             (canonical_json(configuration), configuration['fingerprint']))
            domain = build_domain_graph(rows)
            for entity in sorted(domain.entities.values(), key=lambda item: item['entity_id']):
                conn.execute('INSERT INTO domain_entities VALUES(?,?,?,?)',
                             (entity['entity_id'], entity['entity_type'], entity['name'], canonical_json(entity['aliases'])))
            for row in rows:
                conn.execute('INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?)', (row['chunk_id'], row['version_id'], row['ordinal'],
                    row['text'], canonical_json(row['meta']), canonical_json(row['tokens']), sum(row['tokens'].values()),
                    canonical_json(row['vector']) if row['vector'] is not None else None))
                terms = set(row['tokens']) | set(row['meta']['scope_products']) | set(row['meta']['scope_factories'])
                conn.executemany('INSERT INTO graph_edges VALUES(?,?)', [(term, row['chunk_id']) for term in sorted(terms) if len(term) >= 2])
            for mention in domain.mentions:
                conn.execute('INSERT INTO domain_mentions VALUES(?,?,?,?,?,?,?,?,?,?)',
                             (mention['entity_id'], mention['entity_type'], mention['name'], mention['alias'],
                              mention['chunk_id'], mention['version_id'], mention['start'], mention['end'],
                              mention['quote'], canonical_json(mention['source'])))
            for relation in domain.relations:
                support = relation['support']
                conn.execute('INSERT INTO domain_relations VALUES(?,?,?,?,?,?,?,?,?,?)',
                             (relation['relation_id'], relation['source_entity_id'], relation['target_entity_id'],
                              relation['relation_type'], relation['source_type'], relation['target_type'],
                              relation['semantic_status'], support['chunk_id'], support.get('version_id'),
                              canonical_json(support)))
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
            domain_spec = manifest.get('domain_graph')
            if domain_spec and domain_spec.get('status') == 'ready':
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                required = {'domain_entities', 'domain_mentions', 'domain_relations'}
                if not required <= tables:
                    raise KnowledgeError('领域图索引表不完整')
                observed = {
                    'entities': conn.execute('SELECT COUNT(*) FROM domain_entities').fetchone()[0],
                    'mentions': conn.execute('SELECT COUNT(*) FROM domain_mentions').fetchone()[0],
                    'relations': conn.execute('SELECT COUNT(*) FROM domain_relations').fetchone()[0],
                }
                if observed != domain_spec.get('counts'):
                    raise KnowledgeError('领域图计数与manifest不一致')
                if domain_spec.get('version', 0) >= 4:
                    configuration = domain_spec.get('vocabulary')
                    if not isinstance(configuration, dict):
                        raise KnowledgeError('领域图V4缺少冻结词表')
                    frozen = {key: value for key, value in configuration.items() if key != 'fingerprint'}
                    if _digest(frozen) != configuration.get('fingerprint'):
                        raise KnowledgeError('领域图词表fingerprint不一致')
                    if set(configuration.get('by_version', {})) != version_ids:
                        raise KnowledgeError('领域图词表版本清单不一致')
                    entries = configuration.get('entries', {})
                    for fingerprint, vocabulary in entries.items():
                        if vocabulary_fingerprint(vocabulary) != fingerprint:
                            raise KnowledgeError('领域图词表配置哈希不一致')
                    for declared in configuration['by_version'].values():
                        mode = declared.get('mode')
                        if mode == 'reviewed_vocabulary':
                            vocabulary = entries.get(declared.get('vocabulary_fingerprint'))
                            if not vocabulary or vocabulary['source_config_fingerprint'] != declared.get('source_config_fingerprint'):
                                raise KnowledgeError('领域图词表版本映射不一致')
                        elif mode == 'legacy_pharma_vocabulary':
                            if _digest(configuration.get('legacy_pharma_aliases', {})) != declared.get('vocabulary_fingerprint'):
                                raise KnowledgeError('领域图旧词表快照不一致')
                        elif mode != 'explicit_domain_without_vocabulary':
                            raise KnowledgeError('领域图词表模式无效')
                    if 'graph_configuration' not in tables:
                        raise KnowledgeError('领域图索引缺少冻结配置')
                    stored = conn.execute('SELECT configuration,fingerprint FROM graph_configuration WHERE singleton=1').fetchone()
                    if not stored or json.loads(stored[0]) != configuration or stored[1] != configuration['fingerprint']:
                        raise KnowledgeError('领域图索引与manifest词表不一致')
                    for raw_meta, version_id in conn.execute('SELECT metadata,version_id FROM chunks'):
                        metadata = json.loads(raw_meta)
                        vocabulary = vocabulary_from_metadata(metadata)
                        declared = configuration['by_version'][version_id]
                        if vocabulary is not None and (
                                declared.get('mode') != 'reviewed_vocabulary'
                                or declared.get('vocabulary_fingerprint') != vocabulary_fingerprint(vocabulary)
                                or declared.get('source_config_fingerprint') != vocabulary['source_config_fingerprint']):
                            raise KnowledgeError('领域图片段词表与冻结配置不一致')
                        if vocabulary is None:
                            expected = ('explicit_domain_without_vocabulary' if explicit_other_domain(metadata)
                                        else 'legacy_pharma_vocabulary')
                            if declared.get('mode') != expected:
                                raise KnowledgeError('领域图片段词表模式与冻结配置不一致')
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

    def warmup(self, *, principal, wait=False, expected_fingerprint=None, force=False, timeout=None):
        require_principal(principal)
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                    or not math.isfinite(timeout) or not 0 <= timeout <= 3600):
            raise KnowledgeError('warmup timeout须在0至3600秒之间')
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
        timed_out = False
        thread = getattr(self, '_warm_thread', None)
        if wait and thread is not None:
            thread.join(timeout=60.0 if timeout is None else timeout)
            timed_out = thread.is_alive()
        state = dict(self._warm_state)
        if timed_out:
            state['wait_timed_out'] = True
        return state

    def readiness(self, *, principal, release_id=None, wait=False, timeout=None, require_hybrid=False):
        """Expose readiness; lexical availability is not hybrid compliance.

        Legacy callers may explicitly retain lexical operation. A caller declaring
        ``require_hybrid`` must have an immutable dense release and a ready local
        encoder with the identical fingerprint; warmup never repairs an index.
        """
        principal = self.releases._principal(principal)
        require_principal(principal)
        if not isinstance(require_hybrid, bool):
            raise KnowledgeError('require_hybrid须为布尔值')
        record = self.releases._raw_release(release_id)
        if record is None:
            return {'ready': False, 'state': 'no_published_release', 'release_id': None,
                    'domain_graph': 'unavailable', 'embedding': 'unavailable'}
        manifest = record['manifest']
        domain_spec = manifest.get('domain_graph')
        domain_state = (domain_spec or {}).get('status')
        if domain_state is None and manifest.get('retrieval_schema_version', 2) < RETRIEVAL_SCHEMA_VERSION:
            domain_state = 'disabled_legacy'
        domain_state = domain_state or 'unavailable'
        embedding = manifest.get('embedding', {})
        if domain_state not in {'ready', 'disabled', 'disabled_legacy'}:
            return {'ready': False, 'state': 'domain_graph_not_ready', 'release_id': record['release_id'],
                    'domain_graph': domain_state, 'embedding': embedding.get('status', 'unknown')}
        if embedding.get('status') != 'ready':
            return {'ready': not require_hybrid,
                    'state': 'hybrid_required_no_vectors' if require_hybrid else 'ready_lexical',
                    'release_id': record['release_id'], 'require_hybrid': require_hybrid,
                    'domain_graph': domain_state, 'embedding': embedding.get('status', 'disabled'),
                    'fingerprint': None}
        state = self.warmup(principal=principal, wait=wait, timeout=timeout,
                            expected_fingerprint=embedding.get('fingerprint'))
        fingerprint_ok = state.get('fingerprint') == embedding.get('fingerprint')
        ready = state.get('state') == 'ready' and fingerprint_ok and not state.get('wait_timed_out')
        readiness_state = ('ready' if ready else 'embedding_fingerprint_mismatch'
                           if state.get('state') == 'ready' and not fingerprint_ok
                           else state.get('state', 'unknown'))
        return {'ready': ready, 'state': readiness_state,
                'release_id': record['release_id'], 'domain_graph': domain_state,
                'embedding': embedding.get('status'), 'fingerprint': state.get('fingerprint'),
                'expected_fingerprint': embedding.get('fingerprint'),
                'wait_timed_out': bool(state.get('wait_timed_out')), 'error_type': state.get('error_type')}

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
            graph = DomainGraphIndex()
            for edge in conn.execute('SELECT e.term,e.chunk_id FROM graph_edges e JOIN chunks c ON c.chunk_id=e.chunk_id JOIN permitted p ON p.version_id=c.version_id'):
                graph[edge['term']].add(edge['chunk_id'])
            domain = load_domain_graph(conn, version_ids,
                frozen_vocabulary=manifest.get('domain_graph', {}).get('version', 0) >= 4)
            graph.entities = domain.entities
            graph.mentions = domain.mentions
            graph.relations = domain.relations
            graph.chunk_entities = domain.chunk_entities
            graph.chunk_relations = domain.chunk_relations
            graph._aliases = domain._aliases
            return rows, graph
        finally:
            conn.close()

    @staticmethod
    def _restrict_candidates(rows, graph, knowledge_types, as_of, product=None, factory=None):
        """Apply additive type/period constraints to one already-authorized set.

        Both graph routes use the same chunk set as BM25/vector; traces and
        aliases are narrowed too, rather than filtering only returned results.
        """
        kept = []
        for row in rows:
            meta = row['meta']
            category = knowledge_type(meta)
            if knowledge_types is not None and category not in knowledge_types:
                continue
            if source_period_reason(meta, as_of):
                continue
            applicability = meta.get('applicability') or {}
            if (product is not None and applicability.get('kind') == 'product'
                    and product not in applicability.get('products', [])):
                continue
            table = meta.get('table_row') or {}
            if table.get('schema') == 'cost_baseline':
                columns = table.get('columns') or {}
                if ((product is not None and columns.get('产品名称') != product)
                        or (factory is not None and columns.get('工厂') != factory)):
                    continue
            row['meta'] = {**meta, 'knowledge_type': category, 'query_as_of': as_of}
            kept.append(row)
        ids = {row['chunk_id'] for row in kept}
        narrowed = DomainGraphIndex() if isinstance(graph, DomainGraphIndex) else defaultdict(set)
        for term, chunks in graph.items():
            visible = chunks & ids
            if visible:
                narrowed[term].update(visible)
        if isinstance(graph, DomainGraphIndex):
            narrowed.mentions = [m for m in graph.mentions if m['chunk_id'] in ids]
            def relation_allowed(relation):
                support = relation['support']
                applicability = support.get('applicability') or {}
                return (support['chunk_id'] in ids and (product is None
                    or applicability.get('kind') != 'product'
                    or product in applicability.get('products', [])))
            narrowed.relations = [r for r in graph.relations if relation_allowed(r)]
            entity_ids = {m['entity_id'] for m in narrowed.mentions}
            for relation in narrowed.relations:
                entity_ids.update((relation['source_entity_id'], relation['target_entity_id']))
            narrowed.entities = {key: value for key, value in graph.entities.items() if key in entity_ids}
            if any('vocabulary_aliases' in mention['source'] for mention in graph.mentions):
                # A shared canonical name must not retain an alias supplied only
                # by an excluded version/type/product chunk.
                narrowed.entities = {}
                for mention in narrowed.mentions:
                    narrowed.add_entity(mention['entity_type'], mention['name'],
                        mention['source'].get('vocabulary_aliases', [mention['name'], mention['alias']]))
            else:
                for alias, entities in graph._aliases.items():
                    if entities & entity_ids:
                        narrowed._aliases[alias].update(entities & entity_ids)
            for chunk, entities in graph.chunk_entities.items():
                if chunk in ids:
                    narrowed.chunk_entities[chunk].update(entities & entity_ids)
            for chunk, relations in graph.chunk_relations.items():
                if chunk in ids:
                    narrowed.chunk_relations[chunk].extend(r for r in relations if relation_allowed(r))
        return kept, narrowed

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
        seeds.update(term for term in graph if len(term) >= 3 and matching_view(term) in matching_view(query))
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
    def _domain_graph(query, rows, graph):
        if not isinstance(graph, DomainGraphIndex) or not graph.entities:
            return [], {'matched_entities': [], 'relation_hits': {}}
        return graph.domain_search(query)

    @staticmethod
    def _controlled_rerank(query, candidates, rows, graph, *, legacy_order=None):
        """Deterministic domain-aware rank with a reviewable feature trace.

        This is a controlled lexical ranker, not a neural model and not a
        calibrated relevance score.  It only changes the order of already
        authorized same-release candidates.
        """
        if not isinstance(graph, DomainGraphIndex) or not graph.entities or not candidates:
            return candidates, {'enabled': False, 'reason': 'domain_graph_unavailable', 'changed': False,
                                'before': [item[0] for item in candidates], 'after': [item[0] for item in candidates],
                                'legacy_before': legacy_order or [item[0] for item in candidates],
                                'changed_from_legacy': bool(legacy_order and legacy_order != [item[0] for item in candidates]),
                                'matched_query_entities': []}
        matches = graph.query_entities(query)
        query_ids = {item['entity_id'] for item in matches}
        query_names = {item['entity_id']: (item.get('entity') or {}).get('name') for item in matches}
        by_id = {row['chunk_id']: row for row in rows}
        max_rrf = max((score for _, score in candidates), default=1.0) or 1.0
        traced, before = [], [item[0] for item in candidates]
        query_view = matching_view(query).lower()
        for chunk_id, rrf_score in candidates:
            row = by_id[chunk_id]
            row_entities = set(graph.chunk_entities.get(chunk_id, ()))
            matched = row_entities & query_ids
            relation_evidence = graph.evidence_for_chunk(chunk_id, query)
            pair_hit = any({edge['source_entity_id'], edge['target_entity_id']} >= query_ids
                            for edge in relation_evidence) if len(query_ids) > 1 else bool(relation_evidence)
            phrase_hits = 0
            for item in matches:
                entity = item.get('entity') or {}
                for alias in [entity.get('name', ''), *entity.get('aliases', [])]:
                    alias_view = matching_view(alias).lower()
                    if alias_view and alias_view in matching_view(row['text']).lower():
                        phrase_hits += 1
                        break
            coverage = len(matched) / len(query_ids) if query_ids else 0.0
            relation_score = min(1.0, (0.65 if pair_hit else 0.0) + min(0.35, len(relation_evidence) * 0.08))
            phrase_score = min(1.0, phrase_hits / len(query_ids)) if query_ids else 0.0
            base_score = min(1.0, rrf_score / max_rrf)
            # Entity coverage and an evidenced relation dominate the prior RRF;
            # RRF remains a tie-breaking signal for otherwise similar chunks.
            score = (0.55 * coverage + 0.25 * relation_score + 0.12 * phrase_score + 0.08 * base_score)
            displayed_evidence = relation_evidence[:DOMAIN_EVIDENCE_LIMIT]
            traced.append((chunk_id, score, {
                'rrf_score': rrf_score, 'entity_coverage': round(coverage, 6),
                'relation_support': round(relation_score, 6), 'exact_entity_phrase': round(phrase_score, 6),
                'rrf_normalized': round(base_score, 6),
                'entity_hits': sorted(query_names[entity_id] for entity_id in matched if query_names.get(entity_id)),
                'relation_count': len(relation_evidence),
                'domain_evidence': displayed_evidence,
                'domain_evidence_count': len(displayed_evidence),
                'domain_evidence_limit': DOMAIN_EVIDENCE_LIMIT,
                'domain_evidence_truncated': len(displayed_evidence) < len(relation_evidence),
            }))
        traced.sort(key=lambda item: (-item[1], item[0]))
        after = [item[0] for item in traced]
        scores = {chunk_id: (score, detail) for chunk_id, score, detail in traced}
        return [(chunk_id, scores[chunk_id][0], scores[chunk_id][1]) for chunk_id in after], {
            'enabled': True, 'algorithm': RERANKER['name'], 'neural': False,
            'matched_query_entities': [item.get('entity') for item in matches],
            'before': before, 'after': after, 'legacy_before': legacy_order or before,
            'reranker_changed': before != after,
            'changed': (before != after) or bool(legacy_order and legacy_order != after),
            'changed_from_legacy': bool(legacy_order and legacy_order != after),
        }

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
               allowed_scopes=None, release_id=None, top_k=10, vector_timeout=3.0,
               use_domain_graph=True, use_rerank=True, require_ready=False, ready_timeout=None,
               knowledge_types=None, require_hybrid=False):
        principal = self.releases._principal(principal)
        scope = authorize_query(principal, product=product, factory=factory, allowed_scopes=allowed_scopes)
        knowledge_types = normalize_knowledge_types(knowledge_types)
        as_of = _date(as_of or date.today().isoformat(), '业务日期')
        cutoff = normalize_known_at(known_at)
        if not isinstance(query, str) or not query.strip() or len(query) > 4000:
            raise KnowledgeError('query须为1至4000字符')
        if not isinstance(top_k, int) or not 1 <= top_k <= 100:
            raise KnowledgeError('top_k须为1至100')
        if not isinstance(vector_timeout, (int, float)) or not math.isfinite(vector_timeout) or not 0 < vector_timeout <= 30:
            raise KnowledgeError('vector_timeout须为0至30秒')
        if ready_timeout is not None and (isinstance(ready_timeout, bool) or not isinstance(ready_timeout, (int, float))
                                          or not math.isfinite(ready_timeout) or not 0 <= ready_timeout <= 3600):
            raise KnowledgeError('ready_timeout须在0至3600秒之间')
        if not all(isinstance(value, bool) for value in (use_domain_graph, use_rerank, require_ready, require_hybrid)):
            raise KnowledgeError('use_domain_graph、use_rerank、require_ready和require_hybrid须为布尔值')
        record = self.releases._raw_release(release_id, cutoff if known_at is not None and release_id is None else None)
        stats = {'release_id': None, 'generation': None, 'as_of': as_of, 'known_at': cutoff,
                 'no_answer': True, 'reason': 'no_published_release', 'degraded': True,
                 'degradation_reasons': [], 'retrieval_mode': 'unavailable', 'vector_n': 0, 'bm25_n': 0,
                 'graph_n': 0, 'domain_graph_n': 0, 'fused_n': 0, 'reranked': False,
                  'rerank_trace': {'enabled': False, 'reason': 'not_started'},
                  'authorized_version_ids': [],
                 'route_generations': {}, 'authorization': 'before_all_candidates', 'organization': 'default',
                 'product_scope_known': bool(product and product in principal.products),
                 'knowledge_types': list(knowledge_types) if knowledge_types is not None else None,
                 'category_filter_stage': 'after_authorization_before_all_candidates',
                 'require_hybrid': require_hybrid,
                 'graph_vocabulary_version': None, 'graph_vocabulary_fingerprint': None,
                 'source_config_fingerprints': [],
                 'applicability_version': APPLICABILITY['version']}
        if record is None:
            return [], stats
        if record['status'] != 'published':
            raise KnowledgeError('只允许检索published发布')
        if record['published_at'] > cutoff:
            stats['reason'] = 'release_not_known_at_cutoff'
            return [], stats
        manifest = record['manifest']
        configuration = manifest.get('domain_graph', {}).get('vocabulary')
        frozen_vocabulary = configuration if isinstance(configuration, dict) else None
        stats.update(release_id=record['release_id'], generation=record['release_id'],
                     domain_graph_version=manifest.get('domain_graph', {}).get('version'),
                     graph_vocabulary_version=(frozen_vocabulary or {}).get('schema_version'),
                     graph_vocabulary_fingerprint=(frozen_vocabulary or {}).get('fingerprint'))
        readiness = self.readiness(principal=principal, release_id=record['release_id'],
                                   wait=require_ready, timeout=ready_timeout, require_hybrid=require_hybrid)
        stats['readiness'] = readiness
        if (require_ready or require_hybrid) and not readiness['ready']:
            reason = 'hybrid_unavailable' if require_hybrid else 'retrieval_not_ready'
            stats.update(reason=reason, degradation_reasons=[readiness['state']])
            return [], stats
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
        if frozen_vocabulary:
            stats['source_config_fingerprints'] = sorted({entry['source_config_fingerprint']
                for version_id, entry in frozen_vocabulary['by_version'].items()
                if version_id in allowed and entry.get('source_config_fingerprint')})
        stats['product_scope_known'] = stats['product_scope_known'] or any(
            product in v['scope_products'] for v in selected if v['version_id'] in allowed)
        route_names = ['bm25', 'vector', 'graph']
        if use_domain_graph and manifest.get('domain_graph', {}).get('status') == 'ready':
            route_names.append('domain_graph')
        stats['route_generations'] = {name: record['release_id'] for name in route_names}
        if not allowed:
            stats['reason'] = 'no_authorized_effective_published_versions'
            return [], stats
        rows, graph = self._load_authorized(record, allowed)
        # Read legacy generations without mutating their text, IDs, offsets or
        # SQLite artifact. Only already-authorized originals may supply context.
        if manifest.get('chunker', {}).get('version', 1) == 1:
            originals = {v['version_id']: v for v in selected if v['version_id'] in allowed}
            sections = {key: source_sections(v['text'], _metadata(v)) for key, v in originals.items()}
            graph = defaultdict(set)
            for row in rows:
                original = originals[row['version_id']]['text']
                start, end = row['meta']['offset'], row['meta']['offset'] + len(row['text'])
                if original[start:end] != row['text']:
                    raise KnowledgeError('旧发布片段与已确认原文offset不一致')
                row['meta'] = chunk_metadata(original, row['meta'], start, end,
                    sections=sections[row['version_id']], legacy=True)
                row['tokens'] = Counter(tokenize(row['text']))
                row['token_count'] = sum(row['tokens'].values())
                terms = set(row['tokens']) | set(row['meta']['scope_products']) | set(row['meta']['scope_factories'])
                for term in terms:
                    if len(term) >= 2:
                        graph[matching_view(term)].add(row['chunk_id'])
            stats['legacy_matching_view'] = MATCHING_VIEW
        authorized_chunk_count = len(rows)
        excluded_product_scope = sum(1 for row in rows if product is not None
            and (row['meta'].get('applicability') or {}).get('kind') == 'product'
            and product not in (row['meta'].get('applicability') or {}).get('products', []))
        rows, graph = self._restrict_candidates(rows, graph, knowledge_types, as_of, product, factory)
        stats['candidate_filter'] = {'authorized_chunks': authorized_chunk_count, 'permitted_chunks': len(rows),
                                    'excluded_type_or_period': authorized_chunk_count - len(rows) - excluded_product_scope,
                                    'excluded_product_scope': excluded_product_scope,
                                    'product_applicability_stage': 'before_all_routes'}
        # Empty after authorized type/period filtering is a coverage gap, not
        # infrastructure degradation. Still execute/validate the dense query:
        # a warm handle alone must not conceal encoder failure on an empty set.
        empty_candidate_reason = 'no_matching_knowledge_type_or_source_period' if not rows else None
        # V4 entity matching uses only aliases frozen on authorized mentions.
        # Do not inject the current global pharmaceutical synonym dictionary.
        keyword_query, expansions = ((query, []) if frozen_vocabulary else
            expand_query(query) if manifest.get('domain_keywords') else (query, []))
        stats['keyword_expansions'] = expansions
        lexical = self._bm25(keyword_query, rows)[:max(top_k * 3, 30)]
        graph_results = self._graph(query, rows, graph)[:max(top_k * 3, 30)]
        domain_results, domain_trace = (self._domain_graph(keyword_query, rows, graph)
                                        if use_domain_graph and manifest.get('domain_graph', {}).get('status') == 'ready'
                                        else ([], {'matched_entities': [], 'relation_hits': {}}))
        domain_results = domain_results[:max(top_k * 3, 30)]
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
                    dense_query = matching_view(query) if manifest.get('schema_version', 1) >= 2 else query
                    vector = self._handle.encode([dense_query], timeout=vector_timeout)[0]
                    if not self.releases._valid_dense_vector(vector, manifest['embedding'].get('dimension')):
                        raise KnowledgeError('查询向量与冻结embedding维度或数值不一致')
                    vector_results = self._vector(vector, rows)[:max(top_k * 3, 30)]
                except FutureTimeout:
                    reasons.append('query_embedding_timeout')
                except Exception as exc:
                    reasons.append('query_embedding_failed:' + type(exc).__name__)
        else:
            reasons.append('release_has_no_vectors')
        if require_hybrid and (reasons or (rows and not vector_results)):
            # Dense query failure must not turn a declared hybrid request into
            # a successful lexical answer. Candidates above were already ACL,
            # type and period filtered; none are exposed on this failure path.
            stats.update(reason='hybrid_unavailable', degraded=True,
                         degradation_reasons=sorted(set(reasons or ['vector_candidates_unavailable'])),
                         retrieval_mode='unavailable')
            return [], stats
        scores, legacy_scores, routes = Counter(), Counter(), defaultdict(dict)
        for name, results in (('bm25', lexical), ('vector', vector_results), ('graph', graph_results),
                              ('domain_graph', domain_results)):
            for rank, item in enumerate(results):
                chunk_id, score = item[0], item[1]
                contribution = 1.0 / (60 + rank + 1)
                scores[chunk_id] += contribution
                if name != 'domain_graph':
                    legacy_scores[chunk_id] += contribution
                routes[chunk_id][name] = score
        by_id = {row['chunk_id']: row for row in rows}
        # A revocation may arrive while the local encoder runs. Recheck before
        # returning evidence as well as before candidate generation.
        revoked_now = self.releases._revoked()
        withdrawn = {key for key, row in by_id.items() if row['meta']['doc_id'] in revoked_now}
        for key in withdrawn:
            scores.pop(key, None)
            legacy_scores.pop(key, None)
        if withdrawn:
            stats['authorized_version_ids'] = sorted({row['version_id'] for key, row in by_id.items() if key not in withdrawn})
            if frozen_vocabulary:
                stats['source_config_fingerprints'] = sorted({entry['source_config_fingerprint']
                    for version_id, entry in frozen_vocabulary['by_version'].items()
                    if version_id in stats['authorized_version_ids'] and entry.get('source_config_fingerprint')})
            # Diagnostic traces are evidence too. Re-load the still-authorized
            # graph so revoked names, relation quotes and ids cannot leak there.
            rows, graph = self._load_authorized(record, set(stats['authorized_version_ids']))
            rows, graph = self._restrict_candidates(rows, graph, knowledge_types, as_of, product, factory)
            by_id = {row['chunk_id']: row for row in rows}
            domain_results, domain_trace = (self._domain_graph(keyword_query, rows, graph)
                                            if use_domain_graph else ([], {'matched_entities': [], 'relation_hits': {}}))
            domain_results = domain_results[:max(top_k * 3, 30)]
        ordered = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
        legacy_order = [item[0] for item in sorted(legacy_scores.items(), key=lambda pair: (-pair[1], pair[0]))]
        rerank_trace = {'enabled': False, 'reason': 'disabled_by_request', 'changed': False,
                        'before': [item[0] for item in ordered], 'after': [item[0] for item in ordered],
                        'legacy_before': legacy_order, 'changed_from_legacy': legacy_order != [item[0] for item in ordered]}
        reranked = []
        if use_rerank and manifest.get('reranker', {}).get('status') == 'ready' and domain_results:
            reranked, rerank_trace = self._controlled_rerank(keyword_query, ordered, rows, graph, legacy_order=legacy_order)
        if reranked:
            ranked = [(chunk_id, score, detail) for chunk_id, score, detail in reranked]
        else:
            ranked = [(chunk_id, score, None) for chunk_id, score in ordered]
            if use_rerank and manifest.get('reranker', {}).get('status') == 'ready' and not domain_results:
                rerank_trace = {'enabled': False, 'reason': 'no_domain_candidates', 'changed': False,
                                'before': [item[0] for item in ordered], 'after': [item[0] for item in ordered],
                                'legacy_before': legacy_order, 'changed_from_legacy': legacy_order != [item[0] for item in ordered]}
        # 可选神经重排层：仅当发布 manifest 显式启用且本地 reranker 模型可用；
        # 未启用/失败时保持受控重排原序，并在 trace 中显式标注（不冒充神经重排）。
        from enterprise.neural_rerank import (neural_rerank_enabled,
                                              rerank as neural_rerank_pass)
        if neural_rerank_enabled(manifest, switch=None):
            ranked, neural_trace = neural_rerank_pass(keyword_query, ranked, by_id)
            rerank_trace.update(neural=neural_trace)
        result = []
        for chunk_id, rerank_score, rerank_detail in ranked[:top_k]:
            row = by_id[chunk_id]
            base_score = scores[chunk_id]
            result.append({'text': row['text'], 'chunk_id': chunk_id, 'version_id': row['version_id'],
                'document_id': row['meta']['doc_id'], 'release_id': record['release_id'], 'generation': record['release_id'],
                'meta': {**row['meta'], 'release_id': record['release_id'], 'generation': record['release_id']},
                'score': base_score, 'route_scores': routes[chunk_id],
                'rerank_score': rerank_score if rerank_detail is not None else None,
                'rerank_details': rerank_detail,
                'domain_evidence': (rerank_detail or {}).get('domain_evidence', []),
                'domain_evidence_count': (rerank_detail or {}).get('domain_evidence_count', 0),
                'domain_evidence_limit': DOMAIN_EVIDENCE_LIMIT,
                'domain_evidence_truncated': (rerank_detail or {}).get('domain_evidence_truncated', False)})
        stats.update(no_answer=not bool(result), reason='' if result else empty_candidate_reason or 'no_matching_evidence',
                     degraded=bool(reasons), degradation_reasons=sorted(set(reasons)),
                     retrieval_mode='vector_bm25_graph' if not reasons else 'bm25_graph_fallback',
                     vector_n=len(vector_results), bm25_n=len(lexical), graph_n=len(graph_results),
                     domain_graph_n=len(domain_results), fused_n=len(scores), reranked=bool(reranked),
                     rerank_trace=rerank_trace, domain_trace=domain_trace)
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
            'rerank_score': item.get('rerank_score'),
             'retrieval_scores': {'rrf_score': item['score'],
                                  'rerank_score': item.get('rerank_score'),
                                  'vector_score': item.get('route_scores', {}).get('vector'),
                                  'bm25_score': item.get('route_scores', {}).get('bm25'),
                                  'graph_score': item.get('route_scores', {}).get('graph'),
                                  'domain_graph_score': item.get('route_scores', {}).get('domain_graph')},
             'rerank_details': item.get('rerank_details'),
             'domain_evidence': item.get('domain_evidence', []),
              'domain_evidence_count': item.get('domain_evidence_count', len(item.get('domain_evidence', []))),
              'domain_evidence_limit': item.get('domain_evidence_limit', DOMAIN_EVIDENCE_LIMIT),
              'domain_evidence_truncated': item.get('domain_evidence_truncated', False),
             'scope_unknown': False, 'is_demo': False, 'is_sim_case': False,
            'scope_products': meta['scope_products'], 'scope_factories': meta['scope_factories'],
            'effective_from': meta['effective_from'], 'effective_to': meta['effective_to'],
            'confirmed_at': meta['confirmed_at'], 'document_sha256': meta['sha256'],
            'offset': meta['offset'], 'end_offset': meta.get('end_offset', meta['offset'] + len(item['text'])),
            'ordinal': meta['ordinal'], 'route_scores': item['route_scores'],
            'page': meta.get('page_hint'), 'pages': meta.get('pages', []),
            'page_spans': meta.get('page_spans', []), 'section': meta.get('section', ''),
            'applicability': meta.get('applicability', {}), 'elements': meta.get('elements', []),
             'knowledge_type': knowledge_type(meta), 'table_row': meta.get('table_row'),
             'reference_kind': meta.get('reference_kind'), 'source_period': meta.get('source_period'),
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
