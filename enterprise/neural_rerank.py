# -*- coding: utf-8 -*-
"""可选神经重排层（bge-reranker-v2-m3），叠加在受控域词典重排之上。

默认关闭：只有发布 manifest 显式声明 neural_rerank.status == 'ready' 且本地
RERANKER_PATH 指向真实模型目录时才启用；否则检索顺序与受控重排完全一致。
任何加载/打分失败都原序回退并显式标注，绝不冒充神经重排。
"""
from __future__ import annotations

import os

from paths import RERANKER_PATH

MANIFEST_KEY = 'neural_rerank'


class NeuralReranker:
    """进程级单例，测试可通过 reset() 清除。"""
    _model = None

    @classmethod
    def available(cls) -> bool:
        return bool(RERANKER_PATH and os.path.isdir(RERANKER_PATH))

    @classmethod
    def instance(cls):
        if cls._model is None:
            from FlagEmbedding import FlagReranker
            cls._model = FlagReranker(RERANKER_PATH, use_fp16=False)
        return cls._model

    @classmethod
    def reset(cls):
        cls._model = None


def neural_rerank_enabled(manifest, *, switch=None) -> bool:
    """启用条件：发布 manifest 显式 ready，或管理员设置 COST_NEURAL_RERANK=true；
    二者都必须同时满足本地 reranker 模型存在。默认关闭——排序变化需先过 8 项受控查询回归。"""
    cfg = (manifest or {}).get(MANIFEST_KEY) or {}
    if switch is None:
        switch = os.environ.get('COST_NEURAL_RERANK', '').lower() == 'true'
    return (cfg.get('status') == 'ready' or switch) and NeuralReranker.available()


def neural_rerank_declaration() -> dict:
    """发布时写入 manifest 的声明（仅在本地模型可用时 status=ready）。"""
    return {'name': 'bge-reranker-v2-m3', 'version': 1, 'neural': True,
            'status': 'ready' if NeuralReranker.available() else 'disabled',
            'boundary': '仅在受控候选集内重排；不改变授权、期间与要素筛选结果'}


def rerank(query: str, ranked, rows_by_id):
    """对受控重排后的 [(chunk_id, score, detail)] 做神经重排。

    返回 (resorted_list, trace)。失败/未启用时原序返回，trace 说明原因。
    """
    before = [item[0] for item in ranked]
    if not ranked:
        return ranked, {'enabled': False, 'reason': 'no_candidates', 'neural': True,
                        'before': before, 'after': before, 'changed': False}
    try:
        model = NeuralReranker.instance()
        pairs = [[query, rows_by_id[chunk_id]['text']] for chunk_id, _, _ in ranked]
        scores = model.compute_score(pairs, normalize=True)
        scored = sorted(zip(ranked, scores), key=lambda pair: -pair[1])
        resorted = [(chunk_id, float(base), detail)
                    for (chunk_id, base, detail), _score in scored]
        after = [item[0] for item in resorted]
        return resorted, {'enabled': True, 'neural': True, 'changed': before != after,
                          'before': before, 'after': after}
    except Exception as exc:
        return ranked, {'enabled': False, 'neural': True,
                        'reason': f'neural_rerank_failed:{type(exc).__name__}',
                        'before': before, 'after': before, 'changed': False}
#（注：内容由AI生成）
