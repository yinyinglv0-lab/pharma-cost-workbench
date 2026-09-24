# -*- coding: utf-8 -*-
"""模型配置（系统界面板块）的本地凭据存储与硬门禁。

- 密钥只存本机 `.local/llm_keys.json`（已被 .gitignore 排除，不进公开源码包）；
- 解析顺序：环境变量优先，其次本机凭据文件——两种来源任一即可；
- 页面可保存密钥与云端调用授权开关；保存后不自动调用模型；
- "测试连接"按钮会发起一次微小付费调用（≤16 token）验证密钥有效性；
- 硬门禁：require_generation_model() 在生成入口强制校验——所选模型
  密钥+授权任一缺失即抛 ModelUnavailable，相关文本拒绝生成（页面显示明确提示），
  不做静默降级；已配置但调用期失败仍走既有确定性降级路径。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from enterprise.model_registry import _PROVIDERS

CREDENTIALS_FILE = Path(os.environ.get(
    'COST_MODEL_CREDENTIALS_FILE',
    str(Path(__file__).resolve().parent.parent / '.local' / 'llm_keys.json')))

SCHEMA = 'local-model-credentials/1'


def load_credentials() -> dict:
    if not CREDENTIALS_FILE.is_file():
        return {}
    try:
        value = json.loads(CREDENTIALS_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _guard_write():
    CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)


def save_credentials(provider: str, key: str, approved: bool) -> dict:
    """保存某供应商的密钥与授权开关（本机文件）。返回保存后的状态视图（不含明文）。"""
    if provider not in _PROVIDERS:
        raise ValueError(f'未知模型供应商: {provider}')
    value = load_credentials()
    entry = dict(value.get(provider, {}))
    if not isinstance(entry, dict):
        entry = {}
    if key is not None:
        entry['key'] = key.strip()
    entry['approved'] = bool(approved)
    value[provider] = entry
    _guard_write()
    CREDENTIALS_FILE.write_text(json.dumps({'schema': SCHEMA, **value},
                                           ensure_ascii=False, indent=2),
                                encoding='utf-8')
    return status_view(provider)


def resolved_api_key(env_name: str) -> str:
    """环境变量优先，其次本机凭据文件。"""
    env_value = os.environ.get(env_name, '')
    if env_value:
        return env_value
    for provider, settings in _PROVIDERS.items():
        if settings['key'] == env_name:
            return str((load_credentials().get(provider) or {}).get('key', '')).strip()
    return ''


def resolved_approval(env_name: str) -> bool | None:
    """授权开关：环境变量优先，其次本机凭据文件；都没有返回 None。"""
    env_value = os.environ.get(env_name)
    if env_value is not None:
        return env_value.lower() == 'true'
    for provider, settings in _PROVIDERS.items():
        if settings['consent'] == env_name:
            entry = load_credentials().get(provider) or {}
            if 'approved' in entry:
                return bool(entry['approved'])
    return None


def status_view(provider: str | None = None) -> dict:
    """给页面的只读状态：每供应商已保存密钥长度/授权/环境变量是否存在，不回显明文。"""
    rows = {}
    for name, settings in _PROVIDERS.items():
        if provider and name != provider:
            continue
        entry = load_credentials().get(name) or {}
        rows[name] = {
            'provider': name,
            'env_key_set': bool(os.environ.get(settings['key'], '')),
            'saved_key_length': len(str(entry.get('key', '')).strip()),
            'saved_approved': bool(entry.get('approved')),
            'env_approval': resolved_approval(settings['consent']),
        }
    return rows if provider is None else rows.get(provider, {})


def test_connection(provider: str, timeout: float = 30.0) -> dict:
    """一次微小付费调用（≤16 token）验证密钥有效。返回 {ok, latency, model, reason}。"""
    from enterprise.model_registry import _PROVIDERS as PROVIDERS, _BUILTINS
    settings = PROVIDERS[provider]
    key = resolved_api_key(settings['key'])
    if not key:
        return {'ok': False, 'reason': '密钥未配置'}
    model = next((entry['model'] for name, entry in _BUILTINS.items()
                  if entry.get('provider') == provider), None)
    if model is None:
        return {'ok': False, 'reason': '该供应商没有内置模型'}
    try:
        import time
        from openai import OpenAI
        client = OpenAI(api_key=key, base_url=settings['default_url'])
        started = time.monotonic()
        params = {'model': model,
                  'messages': [{'role': 'user', 'content': '只回复OK'}],
                  'max_tokens': 8, 'timeout': timeout}
        if provider != 'moonshot':
            params['temperature'] = 0
        # kimi-k3 推理模型只允许 temperature=1，省略则用服务端默认值
        resp = client.chat.completions.create(**params)
        elapsed = round(time.monotonic() - started, 2)
        content = (resp.choices[0].message.content or '').strip()
        return {'ok': True, 'latency_seconds': elapsed, 'model': model,
                'reply': content[:16]}
    except Exception as exc:
        return {'ok': False, 'reason': type(exc).__name__}


def require_generation_model(task='attribution'):
    """硬门禁：所选生成模型必须已配置密钥且已授权，否则抛 ModelUnavailable。

    调用位置：attribution_gen.generate_attribution / benchmark_ai / 报告生成入口，
    仅当 use_llm=True 时执行。未配置 → 明确拒绝生成（不静默降级）；
    已配置但调用期失败 → 走既有确定性降级路径。
    """
    from enterprise.model_gateway import ModelUnavailable, configuration
    config = configuration(task=task)
    if config.registry_id == 'legacy':
        key_ok = bool(config.api_key)
        approved_ok = bool(config.approved_cloud)
    else:
        key_ok = bool(config.api_key)
        approved_ok = bool(config.approved_cloud)
    if not key_ok or not approved_ok:
        raise ModelUnavailable(
            '模型未配置成功，无法生成相关文本：请先在「系统设置 → 模型配置」中'
            '填写该模型的 API 密钥并开启云端调用授权。')
    return config
#（注：内容由AI生成）
