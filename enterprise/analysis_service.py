"""Scoped adapters joining released evidence, deterministic facts and models."""
from __future__ import annotations
from calendar import monthrange
from copy import deepcopy
import hashlib

from enterprise.security import require


_ELEMENT_WORDS = {
    '材料': ('原料', '原材料', '配方', '药材', '采购', '投料', '收率', '提取', '包材', '物料'),
    '人工': ('人工', '工时', '人员', '考勤', '工资', '操作人员', '培训', '排班'),
    '制费': ('设备', '折旧', '能源', '蒸汽', '电力', '维修', '制造费用', '分摊'),
}


def report_evidence(principal, product, specification, months, root=None):
    """Keep each chunk's true date coverage; never label month-end evidence as a quarter."""
    require(principal, 'knowledge.read', factory='中药一厂', product=product)
    from enterprise.knowledge import Repository
    from enterprise.knowledge_release import get_search_engine
    from enterprise.knowledge_langchain import retrieve_rows
    repo = Repository(root, principal=principal)
    engine = get_search_engine(repository=repo)
    combined = {}
    release_id = None
    from datetime import datetime, timezone
    known_at = datetime.now(timezone.utc).isoformat()
    for month in months:
        year, mon = map(int, month.split('-'))
        rows, stats = retrieve_rows(f'{product} 配方 工艺 收率 人工 工时 设备 制造费用 GMP 行业基准',
                                   principal=principal, repository=repo, engine=engine,
                                   product=product, factory='中药一厂',
                                   as_of=f'{month}-{monthrange(year,mon)[1]:02d}', top_k=10,
                                   release_id=release_id, known_at=known_at)
        release_id = stats.get('release_id') or release_id
        for row in rows:
            meta = row['meta']
            governance = meta.get('business_metadata', {})
            if governance.get('evidence_role', 'context_only') == 'context_only':
                continue
            identity = row['chunk_id']
            text = row['text']
            relevant = [element for element, words in _ELEMENT_WORDS.items() if any(word in text for word in words)]
            if not relevant:
                # Generic policy is contextual, not a manufactured causal basis.
                continue
            if identity not in combined:
                combined[identity] = {
                    'id': 'K' + hashlib.sha256(identity.encode()).hexdigest()[:12],
                    'text': text, 'kind': 'document_basis', 'elements': relevant,
                    'support_status': 'eligible', 'document_id': row['document_id'],
                    'business_metadata': deepcopy(governance),
                    'authority': governance.get('authority', 'unreviewed'),
                    'known_conflicts': governance.get('known_conflicts', []),
                    'limitations': governance.get('limitations', []),
                    'version_id': row['version_id'], 'document_sha256': meta['sha256'],
                    'chunk_id': identity, 'index_release_id': row['release_id'],
                    'source': {'file': meta['filename'], 'sha256': meta['sha256'],
                               'version_id': row['version_id'], 'ordinal': meta['ordinal'],
                               'offset': meta['offset']},
                    'scope': {'product': product, 'specification': specification, 'months': []},
                    'retrieval_mode': stats['retrieval_mode'], 'retrieval_degraded': stats['degraded'],
                    'retrieval_framework': deepcopy(stats['framework']),
                    'claim_boundary': '文档仅支持机制解释，不证明本期发生了相应经营事件',
                }
            combined[identity]['scope']['months'].append(month)
    return list(combined.values())


def validated_model(payload, evidence):
    from enterprise.model_gateway import generate_json
    instruction = payload.get('instruction')
    if not instruction:
        raise ValueError('模型调用缺少受控任务指令')
    clean = deepcopy(payload)
    clean.pop('instruction', None)
    return generate_json(instruction, {'facts': clean, 'evidence': evidence}, max_tokens=2500)
