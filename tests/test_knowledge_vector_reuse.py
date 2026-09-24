from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

from enterprise.knowledge import Repository
from enterprise.knowledge_release import ControlledSearchEngine, ReleaseRepository
from enterprise.security import Principal
import enterprise.knowledge_release as kr

ADMIN = Principal('reuse-admin', '向量复用测试', ('knowledge_admin',), ('*',), ('*',))


def confirm(repo, title='工艺', text='银黄口服液处方组成：原料名称 金银花；金银花经过提取。'):
    stage = repo.stage(text.encode('utf-8'), title + '.txt', title, ['银黄口服液'],
                       '2026-01-01', '工艺', ADMIN.user_id, scope_factories=['中药一厂'],
                       visibility='scoped', principal=ADMIN)
    assert not stage['errors']
    return repo.commit(stage['stage_id'], ADMIN.user_id, '确认测试原文', principal=ADMIN)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    model = tmp_path / 'model'
    model.mkdir()
    (model / 'config.json').write_text('{}', encoding='utf-8')
    calls, loads = [], []
    class Encoder:
        def encode(self, texts, batch_size):
            calls.extend(texts)
            return {'dense_vecs': [[1.0, 0.25] for _ in texts]}
    def load(path):
        loads.append(str(path))
        return Encoder()
    monkeypatch.setattr(kr, '_load_local_bge', load)
    repo = Repository(tmp_path / 'managed')
    version = confirm(repo)
    releases = ReleaseRepository(repository=repo)
    first = releases.publish(principal=ADMIN, embedding_model_path=model, require_embeddings=True)
    assert first['status'] == 'published'
    return repo, releases, model, first, version, calls, loads


def test_same_inputs_reuse_all_vectors_without_loading_or_encoding(prepared, monkeypatch):
    repo, releases, model, first, version, calls, loads = prepared
    artifact = releases.artifact_dir / first['release_id'] / 'index.sqlite'
    before_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
    def unexpected(*args):
        pytest.fail('all verified vectors exist; no encoder handle is necessary')
    monkeypatch.setattr(kr, '_model_handle', unexpected)
    second = releases.publish(principal=ADMIN, embedding_model_path=model, require_embeddings=True)
    assert second['status'] == 'published'
    reuse = second['manifest']['vector_reuse']
    assert reuse['reused_count'] == first['manifest']['chunk_count'] == 1
    assert reuse['encoded_count'] == 0 and reuse['source_release_id'] == first['release_id']
    assert len(calls) == len(loads) == 1
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == before_hash
    assert releases.get_release(first['release_id'], principal=ADMIN)['status'] == 'published'


def test_new_text_encodes_only_new_chunks_and_rebuilds_every_route(prepared):
    repo, releases, model, first, version, calls, loads = prepared
    confirm(repo, title='新增设备', text='灌装 (10ml/支, 灌封一体机)；设备折旧为受控参考。')
    second = releases.publish(principal=ADMIN, embedding_model_path=model, require_embeddings=True)
    assert second['status'] == 'published'
    reuse = second['manifest']['vector_reuse']
    assert (reuse['reused_count'], reuse['encoded_count'], reuse['new_input_count']) == (1, 1, 1)
    assert len(calls) == 2 and len(loads) == 1
    artifact = releases.artifact_dir / second['release_id'] / 'index.sqlite'
    with sqlite3.connect(artifact) as conn:
        assert conn.execute('SELECT COUNT(*) FROM chunks WHERE vector IS NOT NULL').fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM domain_relations WHERE relation_type='process_uses_equipment'").fetchone()[0] > 0
        assert conn.execute('SELECT release_id FROM generation').fetchone()[0] == second['release_id']


def test_runtime_configuration_change_encodes_fresh(prepared, monkeypatch):
    repo, releases, model, first, version, calls, loads = prepared
    before_threads = first['manifest']['embedding']['model']['threads']
    monkeypatch.setenv('COST_EMBED_THREADS', '1' if before_threads != 1 else '2')
    second = releases.publish(principal=ADMIN, embedding_model_path=model, require_embeddings=True)
    reuse = second['manifest']['vector_reuse']
    assert second['status'] == 'published' and reuse['reused_count'] == 0
    assert reuse['encoded_count'] == 1 and reuse['reason'] == 'embedding_fingerprint_changed'
    assert len(calls) == len(loads) == 2


def test_mutated_source_artifact_fails_closed_without_reuse(prepared):
    repo, releases, model, first, version, calls, loads = prepared
    artifact = releases.artifact_dir / first['release_id'] / 'index.sqlite'
    artifact.write_bytes(artifact.read_bytes() + b'tampered fixture')
    failed = releases.publish(principal=ADMIN, embedding_model_path=model, require_embeddings=True)
    assert failed['status'] == 'failed' and '哈希校验失败' in failed['error']
    assert failed['manifest']['vector_reuse']['reused_count'] == 0
    assert len(calls) == 1
    assert releases.get_release(principal=ADMIN)['release_id'] == first['release_id']


def test_text_hash_mismatch_does_not_reuse_even_if_chunk_id_matches(prepared, monkeypatch):
    repo, releases, model, first, version, calls, loads = prepared
    original = kr._chunks
    def changed(*args, **kwargs):
        for row in original(*args, **kwargs):
            row['text'] += '追加受控原文'
            row['meta']['end_offset'] += len('追加受控原文')
            yield row
    monkeypatch.setattr(kr, '_chunks', changed)
    second = releases.publish(principal=ADMIN, embedding_model_path=model, require_embeddings=True)
    reuse = second['manifest']['vector_reuse']
    assert second['status'] == 'published'
    assert reuse['reused_count'] == 0 and reuse['encoded_count'] == 1
    assert calls[-1].endswith('追加受控原文')


@pytest.mark.parametrize('invalid', [[1.0], [0.0, 0.0], [float('nan'), 1.0], [True, 1.0]])
def test_invalid_cached_vector_is_rejected_even_with_valid_integrity_metadata(prepared, invalid):
    repo, releases, model, first, version, calls, loads = prepared
    # Deliberately construct a checksum-consistent bad base fixture. Production
    # release metadata is immutable; this probes validation beyond file hashing.
    record = deepcopy(first)
    artifact = releases.artifact_dir / first['release_id'] / 'index.sqlite'
    with sqlite3.connect(artifact) as conn:
        conn.execute('UPDATE chunks SET vector=?', (json.dumps(invalid),))
    record['manifest']['artifacts']['index.sqlite']['sha256'] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    chunker = {key: value for key, value in record['manifest']['chunker'].items() if key != 'fingerprint'}
    rows = list(kr._chunks(version, chunker))
    trace = releases._reuse_dense_vectors(record, rows, kr._model_fingerprint(model))
    assert trace['reused_count'] == 0 and trace['invalid_vector_count'] == 1
    assert rows[0]['vector'] is None
