"""Server-owned selectable models; the browser supplies a registry ID, never secrets.

Provider keys and consent are independent. Legacy COST_LLM_* / llm.json keys are
used only by the legacy entry. Merely listing/saving entries never calls a model.
DeepSeek canonical name checked against official API docs on 2026-09-24:
https://api-docs.deepseek.com/updates/ (2026-09-10). Retired deepseek-chat is
not silently rewritten; administrators may register an explicit model ID.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

REGISTRY_CONTRACT = 'registered-model/1'
SELECTED_TASKS = frozenset({'attribution', 'benchmark', 'report'})
_PROVIDERS = {
    'dashscope': {'key': 'DASHSCOPE_API_KEY', 'url': 'DASHSCOPE_BASE_URL',
                  'consent': 'DASHSCOPE_APPROVED_CLOUD',
                  'default_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1'},
    'deepseek': {'key': 'DEEPSEEK_API_KEY', 'url': 'DEEPSEEK_BASE_URL',
                 'consent': 'DEEPSEEK_APPROVED_CLOUD', 'default_url': 'https://api.deepseek.com/v1'},
    'zhipu': {'key': 'ZHIPU_API_KEY', 'url': 'ZHIPU_BASE_URL',
              'consent': 'ZHIPU_APPROVED_CLOUD',
              'default_url': 'https://open.bigmodel.cn/api/paas/v4'},
    'moonshot': {'key': 'MOONSHOT_API_KEY', 'url': 'MOONSHOT_BASE_URL',
                 'consent': 'MOONSHOT_APPROVED_CLOUD',
                 'default_url': 'https://api.moonshot.cn/v1'},
}
_BUILTINS = {
    'qwen-plus': {'provider': 'dashscope', 'model': 'qwen-plus', 'timeout': 40},
    'qwen-turbo': {'provider': 'dashscope', 'model': 'qwen-turbo', 'timeout': 40},
    'deepseek': {'provider': 'deepseek', 'model': 'deepseek-flash', 'timeout': 80},
    'glm-air': {'provider': 'zhipu', 'model': 'glm-4.5-air', 'timeout': 80},
    'kimi-k3': {'provider': 'moonshot', 'model': 'kimi-k3', 'timeout': 120},
}


def _entries(local):
    from enterprise.model_gateway import ModelUnavailable, _identifier, _timeout
    custom = local.get('model_registry', {})
    if not isinstance(custom, dict) or len(custom) > 32:
        raise ModelUnavailable('服务端模型注册表无效')
    result = {name: dict(value) for name, value in _BUILTINS.items()}
    for name, raw in custom.items():
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name)
                or name == 'legacy' or not isinstance(raw, dict)
                or set(raw) - {'provider', 'model', 'timeout', 'base_url', 'approved_cloud'}):
            raise ModelUnavailable('模型注册项仅允许服务端provider/model/timeout/base_url/approved_cloud')
        entry = {**result.get(name, {}), **raw}
        if name in _BUILTINS and entry.get('provider') != _BUILTINS[name]['provider']:
            raise ModelUnavailable('内置模型不能改变服务提供方')
        result[name] = entry
    for entry in result.values():
        if entry.get('provider') not in _PROVIDERS or not _identifier(entry.get('model')):
            raise ModelUnavailable('服务端模型提供方或模型名称无效')
        _timeout(entry.get('timeout', 40))
        if type(entry.get('approved_cloud', False)) is not bool:
            raise ModelUnavailable('服务端模型授权须为布尔值')
    return result


def registered_configuration(registry_id, *, task, local):
    from enterprise.model_gateway import ModelConfiguration, ModelUnavailable, _identifier, _timeout
    entries = _entries(local)
    if not isinstance(registry_id, str) or registry_id not in entries:
        raise ModelUnavailable('所选模型未在服务端注册')
    entry = entries[registry_id]
    provider = entry['provider']
    settings = _PROVIDERS[provider]
    from enterprise.model_settings import resolved_api_key, resolved_approval
    key = resolved_api_key(settings['key'])
    endpoint = os.environ.get(settings['url']) or entry.get('base_url') or settings['default_url']
    try:
        url = urlsplit(endpoint)
        if (url.scheme != 'https' or not url.hostname or url.username or url.password
                or url.query or url.fragment or not url.port in (None, 443)
                or (key and key in endpoint)):
            raise ValueError('unsafe endpoint')
    except (ValueError, TypeError):
        raise ModelUnavailable('注册模型地址须为无凭据的HTTPS服务端地址') from None
    approved = resolved_approval(settings['consent'])
    approved = bool(approved) if approved is not None else entry.get('approved_cloud', False)
    model = (os.environ.get('DEEPSEEK_MODEL') or entry['model']) if provider == 'deepseek' else entry['model']
    if not _identifier(model, key) or not _identifier(registry_id, key):
        raise ModelUnavailable('服务端注册模型名称无效')
    timeout = _timeout(entry.get('timeout', 80 if provider == 'deepseek' else 40))
    return ModelConfiguration(endpoint.rstrip('/'), model, key, approved, timeout, task=task,
        profile=registry_id, routing_enabled=True, routing_reason='selected_registry',
        registry_id=registry_id, provider_name=provider, budget_contract=REGISTRY_CONTRACT,
        configured_timeout=timeout, thinking_mode='disabled' if provider == 'deepseek' else None)


def correction_configuration(task, *, local=None):
    """多模型分工：初稿用 selected_registry_id，修正轮可指定 correction_registry_id。

    未配置 correction_registry_id 或与初稿相同 → 返回 None（沿用初稿模型）。
    该函数只做配置解析，不发起任何调用。
    """
    from enterprise.model_gateway import _read_local
    local = _read_local() if local is None else local
    selected = local.get('selected_registry_id', 'legacy')
    correction_id = local.get('correction_registry_id')
    if not isinstance(correction_id, str) or correction_id == selected \
            or correction_id == 'legacy':
        return None
    if correction_id not in _entries(local):
        raise ModelUnavailable('修正轮模型未在服务端注册')
    return registered_configuration(correction_id, task=task, local=local)


def selection_state():
    """Safe UI projection, no endpoint, credential, secret length or active probes."""
    from enterprise.model_gateway import _read_local, _legacy_configuration, ModelUnavailable
    local = _read_local()
    selected = local.get('selected_registry_id', 'legacy')
    if not isinstance(selected, str):
        raise ModelUnavailable('已保存的注册模型名称无效')
    rows = []
    observed = _observations()
    legacy = _legacy_configuration(task='attribution', local=local)
    public = legacy.public()
    rows.append({'registry_id': 'legacy', 'model': public['model'], 'profile': public['profile'],
                 'provider': 'legacy', 'configured': public['configured'],
                 'approved_cloud': public['approved_cloud'], 'timeout_seconds': legacy.timeout,
                 'selectable': _selectable(legacy), 'paid_baseline_key': legacy.fingerprint(),
                 'last_observed_latency_seconds': None})
    for name in _entries(local):
        cfg = registered_configuration(name, task='attribution', local=local)
        rows.append({'registry_id': name, 'model': cfg.model, 'profile': cfg.profile,
                     'provider': cfg.provider_name, 'configured': bool(cfg.api_key),
                     'approved_cloud': cfg.approved_cloud, 'timeout_seconds': cfg.timeout,
                     'selectable': bool(cfg.api_key and cfg.approved_cloud),
                     'paid_baseline_key': cfg.fingerprint(),
                     'last_observed_latency_seconds': None})
    for row in rows:
        observations = []
        for task in SELECTED_TASKS:
            config = (_legacy_configuration(task=task, local=local) if row['registry_id'] == 'legacy'
                      else registered_configuration(row['registry_id'], task=task, local=local))
            observation = observed.get(config.fingerprint())
            if observation:
                observations.append({**observation, 'task': task})
        if observations:
            observation = max(observations, key=lambda item: item['started_at'])
            row['last_observed_latency_seconds'] = observation['duration_seconds']
            row['last_observed_status'] = observation['status']
            row['last_observed_at'] = observation['started_at']
            row['last_observed_task'] = observation['task']
    if selected not in {row['registry_id'] for row in rows}:
        raise ModelUnavailable('已保存模型未在服务端注册，请管理员重新选择')
    return {'selected_registry_id': selected, 'scope': 'deployment_all_users', 'entries': rows}


def _selectable(config):
    from enterprise.model_gateway import _validate_endpoint
    if config.budget_contract == REGISTRY_CONTRACT and config.approved_cloud is not True:
        return False
    try:
        _validate_endpoint(config.base_url, config.approved_cloud)
    except (ValueError, TypeError):
        return False
    return bool(config.api_key and config.model)


def save_selection(registry_id, *, principal):
    """Authorized atomic merge; does not alter secrets, consent or task mappings."""
    from enterprise.security import require
    from enterprise.model_gateway import (_read_local, _write_local, _legacy_configuration,
                                         ModelUnavailable, _CONFIG_LOCK)
    require(principal, 'system.configure')
    if not isinstance(registry_id, str):
        raise ModelUnavailable('请选择服务端注册的模型名称')
    from enterprise.model_gateway import config_path
    from enterprise.operations import write_guard
    with _CONFIG_LOCK, write_guard(config_path().parent):
        local = _read_local()
        config = (_legacy_configuration(task='attribution', local=local) if registry_id == 'legacy'
                  else registered_configuration(registry_id, task='attribution', local=local))
        if not _selectable(config):
            raise ModelUnavailable('所选模型未配置专属凭据或未获得服务端云授权，选择未保存')
        local['selected_registry_id'] = registry_id
        local['selection_updated'] = datetime.now(timezone.utc).isoformat()
        _write_local(local)
    return {'selected_registry_id': registry_id, 'scope': 'deployment_all_users'}


def _observation_path():
    from enterprise.model_gateway import config_path
    return config_path().with_name('model-observations.json')


def _observations():
    """Tiny metadata-only cache, scoped by immutable safe configuration hash."""
    path = _observation_path()
    try:
        if not path.is_file() or path.stat().st_size > 65536:
            return {}
        raw = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(raw, dict):
            return {}
        result = {}
        for key, row in list(raw.items())[-32:]:
            if (not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{64}', key)
                    or not isinstance(row, dict)):
                continue
            duration, stamp = row.get('duration_seconds'), row.get('started_at')
            if (type(duration) not in (float, int) or not math.isfinite(duration) or not 0 <= duration <= 3600
                    or row.get('status') not in ('returned_json', 'invalid_response', 'unavailable')
                    or not isinstance(stamp, str) or len(stamp) > 40 or not datetime.fromisoformat(stamp).tzinfo):
                continue
            result[key] = {'duration_seconds': duration, 'started_at': stamp, 'status': row['status']}
        return result
    except (OSError, ValueError, TypeError):
        return {}


def record_observation(trace):
    """Only gateway-observed attempts, never view/save/probe or candidate payloads."""
    from enterprise.model_gateway import _CONFIG_LOCK
    from enterprise.operations import write_guard
    if trace.get('registry_id') in (None, 'legacy') or trace.get('request_attempted') is not True:
        return
    key = trace.get('config_sha256')
    if not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{64}', key):
        return
    path = _observation_path()
    try:
        with _CONFIG_LOCK, write_guard(path.parent, timeout=0.05):
            rows = _observations()
            rows.pop(key, None)
            rows[key] = {name: trace[name] for name in ('duration_seconds', 'started_at', 'status')}
            rows = dict(list(rows.items())[-32:])
            fd, filename = tempfile.mkstemp(prefix='.observation-', dir=path.parent)
            temporary = Path(filename)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                    json.dump(rows, handle, allow_nan=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
    except Exception:
        # Optional telemetry must never alter generation or authorization outcomes.
        return
