"""Small versioned retrieval vocabulary; no business-quantity inference.

Expansion affects only keyword/entity matching. Dense encoders and source quotes
retain the user's query/original source. No neural reranker is claimed here.
"""
from enterprise.knowledge_applicability import matching_view

KEYWORDS = {'name': 'controlled-cost-process-synonyms', 'version': 1,
            'boundary': 'lexical synonyms only; not facts, prices, yields or causes'}
SYNONYMS = {
    '灌装封口机': '灌封一体机',
    '灌装封口': '灌装 灌封',
    '水煎提取': '水提取 提取',
    '计提折旧': '折旧',
    '折旧计提': '折旧',
    '单位生产成本': '单位成本',
    '制造间接费用': '制造费用',
    '人工耗时': '人工 工时',
    '设备保养': '设备 维护',
}


def expand_query(query):
    view = matching_view(query)
    expansions = [{'term': term, 'expansion': expansion} for term, expansion in SYNONYMS.items() if term in view]
    return view + ''.join(' ' + item['expansion'] for item in expansions), expansions
