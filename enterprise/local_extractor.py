# -*- coding: utf-8 -*-
"""本地小模型提取适配器（Qwen3-4B-Instruct-2507 + llama.cpp）。

定位：规则抽取的"兜底引擎"，只接管规则失败/低置信度的分支；规则命中路径不动。
数据不出本机（llama-server 127.0.0.1），服务未启动时优雅禁用、不阻塞任何流程。
结构化纪律：temperature 0 + 提示词内嵌 JSON 模板 + jsonschema 后校验 + 一次重试，
再失败返回规则降级结果并显式标注 extractor=rule。

环境变量：
  LOCAL_LLM_URL   llama-server 地址（默认 http://127.0.0.1:8081/v1）
"""
from __future__ import annotations

import json
import os
import re

LOCAL_LLM_URL = os.environ.get('LOCAL_LLM_URL', 'http://127.0.0.1:8081/v1')

EXTRACTION_SCHEMAS = {
    # 工序边抽取：文本片段 → 顺序工序列表
    'process_steps': {
        'template': '{"steps":[{"name":"","parameters":""}]}',
        'required': ['steps'],
        'prompt': ('从以下制药工艺文本中提取顺序工序步骤。只输出JSON，格式：'
                   '{"steps":[{"name":"工序名","parameters":"该工序关键参数"}]}。'
                   '不编造文本中没有的工序。文本：'),
    },
    # 实体抽取：物料/设备/条款编号
    'entities': {
        'template': '{"entities":[]}',
        'required': ['entities'],
        'prompt': ('从以下文本中提取实体（物料名、设备编号、GMP条款号），'
                   '只输出JSON，格式：{"entities":["实体1","实体2"]}。文本：'),
    },
}


def _chat(prompt: str, max_tokens: int = 300, timeout: float = 90.0):
    """OpenAI 兼容调用本地 llama-server；连接失败抛异常（由调用方降级）。"""
    import requests
    resp = requests.post(
        f"{LOCAL_LLM_URL}/chat/completions",
        json={"model": "local",
              "messages": [{"role": "user", "content": prompt}],
              "temperature": 0, "max_tokens": max_tokens},
        timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    return (data['choices'][0]['message']['content'] or '').strip()


def _parse_json(content: str, schema: dict):
    match = re.search(r'\{.*\}', content, re.S)
    if not match:
        raise ValueError('no_json_in_response')
    value = json.loads(match.group())
    for field in schema['required']:
        if field not in value:
            raise ValueError(f'missing_field:{field}')
    return value


def extract(kind: str, text: str) -> dict:
    """本地小模型抽取；任何失败返回 {'extractor': 'rule', 'value': None, 'reason': ...}。

    调用方应把此返回值视为"建议"：命中规则抽取时仍优先规则结果；
    本地结果必须经过与规则结果相同的下游校验才能进入领域图/知识链。
    """
    schema = EXTRACTION_SCHEMAS.get(kind)
    if not schema:
        return {'extractor': 'rule', 'value': None, 'reason': f'unknown_kind:{kind}'}
    if not text or not text.strip():
        return {'extractor': 'rule', 'value': None, 'reason': 'empty_text'}
    # 快速可达性门：服务未启动时立即回退规则（避免 fallback 路径长时间挂起）
    status = server_status()
    if not status.get('reachable'):
        return {'extractor': 'rule', 'value': None,
                'reason': 'local_server_unreachable:' + status.get('reason', 'unknown')}
    prompt = schema['prompt'] + text.strip() + '\n只输出JSON：' + schema['template']
    last_error = None
    for attempt in (1, 2):
        try:
            content = _chat(prompt)
            value = _parse_json(content, schema)
            return {'extractor': 'local_qwen3_4b', 'value': value,
                    'attempts': attempt, 'raw_length': len(content)}
        except Exception as exc:
            last_error = f'{type(exc).__name__}:{exc}'
    return {'extractor': 'rule', 'value': None, 'reason': last_error or 'unknown'}


def server_status() -> dict:
    """给页面/设置页的只读状态：不触发模型推理。"""
    try:
        import requests
        resp = requests.get(LOCAL_LLM_URL.replace('/v1', '') + '/health',
                            timeout=2)
        return {'reachable': True, 'http': resp.status_code,
                'url': LOCAL_LLM_URL}
    except Exception as exc:
        return {'reachable': False, 'reason': type(exc).__name__,
                'url': LOCAL_LLM_URL}
#（注：内容由AI生成）
