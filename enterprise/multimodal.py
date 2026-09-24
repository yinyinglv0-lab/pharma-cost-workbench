# -*- coding: utf-8 -*-
"""多模态视觉理解服务（制药成本场景）。

- 供应商：默认阿里云 DashScope 视觉模型（OpenAI 兼容），可用环境变量切换：
    MULTIMODAL_PROVIDER=dashscope|zhipu|moonshot
    MULTIMODAL_MODEL=qwen-vl-plus（默认，可改 qwen-vl-max / glm-4v / moonshot-v1-*）
    MULTIMODAL_API_KEY / 对应厂商 *_API_KEY
- 任务模板：工艺流程图理解 / 配方表格识别 / 设备铭牌识别 / 扫描件页面 / 通用理解；
- 纪律：输出一律标注"模型输出、未经人工确认，不得直接入库或写入正式报告"；
  审计记录 model/耗时/token/任务类型；密钥只走环境变量；单图 ≤10MB、仅 png/jpg/jpeg/webp；
  调用失败返回明确降级提示，不重试付费调用。
"""
from __future__ import annotations

import base64
import json
import os
import re
import time

ALLOWED_TYPES = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp'}
MAX_BYTES = 10 * 1024 * 1024

_PROVIDERS = {
    'dashscope': {'key': 'DASHSCOPE_API_KEY',
                  'url': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
                  'model': 'qwen-vl-plus'},
    'zhipu': {'key': 'ZHIPU_API_KEY',
              'url': 'https://open.bigmodel.cn/api/paas/v4',
              'model': 'glm-4v-plus'},
    'moonshot': {'key': 'MOONSHOT_API_KEY',
                 'url': 'https://api.moonshot.cn/v1',
                 # moonshot-v1-8k-vision-preview 已下线；kimi-k3 支持视觉输入，
                 # 但为推理模型，只允许 temperature=1（analyze_image 已适配）
                 'model': 'kimi-k3'},
}

TASKS = {
    '工艺流程图理解': ('你是制药工艺助理。请识别图中生产工序及其先后顺序，'
                     '提取每个工序名称、关键工艺参数与图中标注的收率/损耗指标。'
                     '只输出结构化文本；图中没有的信息不得编造，'
                     '并注明"无法从图中确认"的项。'),
    '配方表格识别': ('你是制药配方助理。请识别图中配方/物料表格，'
                   '逐行提取物料名称、用量、单位与备注。'
                   '保留原始单位，不得换算；表格外的推测不得写入。'),
    '设备铭牌识别': ('你是制药设备助理。请识别图中设备铭牌/台账信息：'
                   '设备编号、名称、规格型号、制造商、出厂年份、额定参数。'
                   '识别不清的字段标注"不清晰"。'),
    '扫描件页面': ('你是文档理解助理。请转录图中页面文字并总结要点。'
                 '表格内容按行输出；无法辨认处标注"□"并说明。'),
    '通用理解': ('请描述图中与制药成本相关的信息：对象、数据、异常点。'
               '只描述图中可见内容，不推测。'),
}


def provider_config():
    provider = os.environ.get('MULTIMODAL_PROVIDER', 'dashscope')
    if provider not in _PROVIDERS:
        raise ValueError(f'不支持的多模态供应商: {provider}')
    settings = _PROVIDERS[provider]
    from enterprise.model_settings import resolved_api_key
    api_key = resolved_api_key(settings['key'])
    model = os.environ.get('MULTIMODAL_MODEL', settings['model'])
    base_url = os.environ.get('MULTIMODAL_BASE_URL', settings['url'])
    return provider, model, api_key, base_url


def analyze_image(image_bytes: bytes, mime_type: str, task: str,
                  timeout: float = 120.0) -> dict:
    """视觉理解主入口。返回 {ok, text, meta, disclaimer}；失败返回明确提示。"""
    if mime_type not in ALLOWED_TYPES:
        return {'ok': False, 'text': '', 'meta': {},
                'reason': f'仅支持 png/jpg/webp，收到 {mime_type or "未知类型"}'}
    if not image_bytes or len(image_bytes) > MAX_BYTES:
        return {'ok': False, 'text': '', 'meta': {},
                'reason': '图片为空或超过 10MB'}
    if task not in TASKS:
        return {'ok': False, 'text': '', 'meta': {},
                'reason': f'不支持的任务类型: {task}'}
    provider, model, api_key, base_url = provider_config()
    if not api_key:
        return {'ok': False, 'text': '', 'meta': {'provider': provider, 'model': model},
                'reason': f'{provider} 视觉模型密钥未配置（{_PROVIDERS[provider]["key"]}）'}
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url)
        b64 = base64.b64encode(image_bytes).decode('ascii')
        started = time.monotonic()
        params = {'model': model,
                  'messages': [{'role': 'user', 'content': [
                      {'type': 'image_url',
                       'image_url': {'url': f'data:{mime_type};base64,{b64}'}},
                      {'type': 'text', 'text': TASKS[task]},
                  ]}],
                  'timeout': timeout}
        if provider == 'moonshot':
            params['temperature'] = 1
            # 推理 token 计入 max_tokens，需给思考过程留余量，否则输出被截断
            params['max_tokens'] = 3000
        else:
            params['temperature'] = 0
            params['max_tokens'] = 1500
        resp = client.chat.completions.create(**params)
        elapsed = round(time.monotonic() - started, 2)
        text = (resp.choices[0].message.content or '').strip()
        usage = getattr(resp, 'usage', None)
        meta = {'provider': provider, 'model': model, 'task': task,
                'latency_seconds': elapsed,
                'prompt_tokens': getattr(usage, 'prompt_tokens', None),
                'completion_tokens': getattr(usage, 'completion_tokens', None)}
        return {'ok': True, 'text': text, 'meta': meta,
                'disclaimer': '模型输出、未经人工确认，不得直接入库或写入正式报告。'}
    except Exception as exc:
        return {'ok': False, 'text': '', 'meta': {'provider': provider, 'model': model},
                'reason': f'视觉模型调用失败（{type(exc).__name__}），未重试。'}
#（注：内容由AI生成）
