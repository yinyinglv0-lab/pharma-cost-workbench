# -*- coding: utf-8 -*-
"""上传资料自动归类建议：板块 / 资料类别 / 依据用途（仅建议，须人工确认）。

- 确定性规则层：CSV 已识别 schema > 文件名关键词 > 正文前若干字符，零成本零调用；
- 可选模型层：仅由页面按钮显式触发，用已配置供应商的文本模型判断；
  任何失败都回退为规则建议，绝不阻塞登记、不做付费重试；
- 纪律：建议不改变"上传≠生效"四段管线与两次人工确认，不自动提升来源权威；
  上传内容仅作为待分类文本发送，模型输出只用于预填表单且校验枚举值。
"""
from __future__ import annotations

import json
import os
import re

# Use actual catalog categories; retain older categories when updating a version.
CATEGORIES = ('法规原文', '法规摘要', '产品配方', '生产工艺', '设备参考', '行业基准',
              '市场参考', '派生成本基线', '通用制度', '产品工艺', '配方资料', '异常处理记录', '其他')

# 赛题知识域 5.1.2 三板块：产品知识 / 行业知识 / 企业内部知识。
# 仅用于页面展示、筛选与归类建议；检索仍按已发布索引的 knowledge_types 与授权范围执行。
BOARD_LABELS = ('产品知识', '行业知识', '企业内部知识', '未归类')
_BOARD_CATEGORIES = {
    '产品知识': frozenset({'产品配方', '产品工艺', '配方资料', '生产工艺'}),
    '行业知识': frozenset({'法规原文', '法规摘要', '行业基准', '市场参考'}),
    '企业内部知识': frozenset({'设备参考', '派生成本基线', '异常处理记录', '通用制度'}),
}

ROLE_KEYS = frozenset({'context_only', 'document_basis', 'benchmark_reference',
                       'market_reference', 'observed_baseline'})

# 类别 → 建议依据用途；参考类绝不建议为机制依据（与页面登记纪律一致）。
CATEGORY_ROLE_DEFAULTS = {
    '法规原文': 'document_basis', '法规摘要': 'document_basis', '产品配方': 'document_basis',
    '生产工艺': 'document_basis', '设备参考': 'document_basis', '行业基准': 'benchmark_reference',
    '市场参考': 'market_reference', '派生成本基线': 'observed_baseline',
    '异常处理记录': 'context_only', '通用制度': 'context_only', '产品工艺': 'document_basis',
    '配方资料': 'document_basis', '其他': 'context_only',
}

# CSV 已识别 schema → 类别 / 依据用途（优先于文件名规则）。
_TYPE_CATEGORY = {'formula': '产品配方', 'process': '生产工艺', 'equipment': '设备参考',
                  'regulation': '法规原文', 'regulation_summary': '法规摘要',
                  'industry_benchmark': '行业基准', 'market_prices': '市场参考',
                  'cost_baseline': '派生成本基线'}
_TYPE_ROLE = {'industry_benchmark': 'benchmark_reference', 'market_prices': 'market_reference',
              'cost_baseline': 'observed_baseline'}

# 文件名关键词规则，顺序即优先级（先命中先返回）。
_FILENAME_RULES = (
    (re.compile(r'摘要.*(gmp|法规|规范)|(gmp|法规|规范).*摘要', re.IGNORECASE), '法规摘要'),
    (re.compile(r'gmp|法规|规范', re.IGNORECASE), '法规原文'),
    (re.compile(r'配方'), '产品配方'),
    (re.compile(r'sop|工艺', re.IGNORECASE), '生产工艺'),
    (re.compile(r'设备'), '设备参考'),
    (re.compile(r'行情|价格|市场'), '市场参考'),
    (re.compile(r'基准'), '行业基准'),
    (re.compile(r'异常|案例'), '异常处理记录'),
    (re.compile(r'口径|制度|规定|流程|办法'), '通用制度'),
    (re.compile(r'汇总|明细|预算'), '派生成本基线'),
)

_TEXT_HEAD_CHARS = 1500


def board_of(category):
    """返回资料类别所属板块；未映射的历史类别归入「未归类」。"""
    for board, members in _BOARD_CATEGORIES.items():
        if category in members:
            return board
    return '未归类'


def _suggestion(category, role, confidence, reason, source):
    default = CATEGORY_ROLE_DEFAULTS.get(category, 'context_only')
    if role not in ROLE_KEYS:
        role = default
    elif role == 'document_basis' and default != 'document_basis':
        # 参考类（基准/行情/基线/异常案例）绝不建议为机制依据，与登记纪律一致
        role = default
    return {'category': category, 'board': board_of(category), 'evidence_role': role,
            'confidence': confidence, 'reason': str(reason)[:240], 'source': source}


def _match_rules(haystack):
    """按 _FILENAME_RULES 顺序返回首个命中的类别，未命中返回 None。"""
    for pattern, category in _FILENAME_RULES:
        if pattern.search(haystack):
            return category
    return None


def suggest_classification(filename, parsed) -> dict:
    """规则层建议：CSV schema > 文件名 > 正文关键词 > 兜底「其他」。"""
    filename = str(filename or '')
    parsed = parsed if isinstance(parsed, dict) else {}
    text_head = str(parsed.get('text') or '')[: _TEXT_HEAD_CHARS]
    # 1) CSV：以已验证表头识别的 schema 为准，不由文件名或展示类别提升权限。
    if str(parsed.get('format') or '').lower() == 'csv':
        try:
            from enterprise.tabular_knowledge import knowledge_type
            ktype = knowledge_type({'format': 'csv', 'parse_metadata': parsed.get('metadata', {})})
        except Exception:
            ktype = 'other'
        if ktype in _TYPE_CATEGORY:
            category = _TYPE_CATEGORY[ktype]
            role = _TYPE_ROLE.get(ktype, 'document_basis')
            return _suggestion(category, role, 'high', f'已识别 CSV 表头 schema（{ktype}）', 'rules')
    # 2) 文件名关键词。
    category = _match_rules(filename)
    if category:
        return _suggestion(category, CATEGORY_ROLE_DEFAULTS[category], 'high',
                           f'文件名命中关键词「{category}」', 'rules')
    # 3) 正文前 {_TEXT_HEAD_CHARS} 字符关键词。
    category = _match_rules(text_head)
    if category:
        return _suggestion(category, CATEGORY_ROLE_DEFAULTS[category], 'medium',
                           f'正文命中关键词「{category}」', 'rules')
    # 4) 兜底：不猜测，保持低置信度并提示人工选择。
    return _suggestion('其他', 'context_only', 'low',
                       '未识别到已知关键词；请人工选择类别与依据用途', 'rules')


def _provider_text_model(provider):
    """返回该供应商在注册表中的首个内置文本模型；无内置模型返回 None。"""
    from enterprise.model_registry import _BUILTINS
    for entry in _BUILTINS.values():
        if entry.get('provider') == provider:
            return entry['model']
    return None


def _classify_prompt(filename, text_head):
    board_lines = '\n'.join(f'- {board}: {"、".join(sorted(members))}'
                            for board, members in _BOARD_CATEGORIES.items() if board != '未归类')
    return (f'你是制药企业知识库的资料归类助手。请判断下列文件属于哪个资料类别。\n'
            f'板块与类别：\n{board_lines}\n- 未归类: 其他\n'
            f'依据用途可选值：{", ".join(sorted(ROLE_KEYS))}\n'
            f'规则：行业基准→benchmark_reference；市场参考→market_reference；'
            f'派生成本基线→observed_baseline；异常处理记录/通用制度/其他→context_only；'
            f'其余类别→document_basis。\n'
            f'文件名：{filename[:200]}\n'
            f'正文开头（节选）：\n{text_head[:1200]}\n'
            f'只输出 JSON 对象，字段为 category（须为上述类别之一）、evidence_role、'
            f'reasoning（一句话依据），不得输出其他内容。')


def llm_classify(filename, text_head, timeout=15.0):
    """模型辅助归类（页面按钮显式触发）。返回建议 dict 或 None；绝不抛异常。

    供应商顺序：环境变量 MULTIMODAL_PROVIDER 指定的先试，其次按已配置密钥
    依次尝试 zhipu / moonshot / dashscope。一次失败即换下一个，不做重试。
    """
    from enterprise.model_settings import resolved_api_key
    from enterprise.model_registry import _PROVIDERS
    preferred = os.environ.get('MULTIMODAL_PROVIDER', '')
    order = []
    for provider in (preferred, 'zhipu', 'moonshot', 'dashscope'):
        if provider in _PROVIDERS and provider not in order:
            order.append(provider)
    for provider in order:
        settings = _PROVIDERS[provider]
        key = resolved_api_key(settings['key'])
        model = _provider_text_model(provider)
        if not key or not model:
            continue
        try:
            from openai import OpenAI
            client = OpenAI(api_key=key, base_url=settings['default_url'], max_retries=0)
            params = {'model': model,
                      'messages': [{'role': 'user', 'content': _classify_prompt(filename, text_head)}],
                      'max_tokens': 200, 'timeout': timeout,
                      'response_format': {'type': 'json_object'}}
            # kimi-k3 推理模型只允许 temperature=1；其余提供方用 0 保持确定性。
            params['temperature'] = 1 if provider == 'moonshot' else 0
            resp = client.chat.completions.create(**params)
            data = json.loads((resp.choices[0].message.content or '').strip() or '{}')
            if not isinstance(data, dict):
                continue
            category = str(data.get('category') or '').strip()
            role = str(data.get('evidence_role') or '').strip()
            if category not in CATEGORIES or role not in ROLE_KEYS:
                continue
            reasoning = str(data.get('reasoning') or '').strip() or f'{provider} 模型判断'
            confidence = 'medium' if data.get('confidence') != 'high' else 'high'
            return _suggestion(category, role, confidence, reasoning, f'model:{provider}')
        except Exception:
            continue
    return None
