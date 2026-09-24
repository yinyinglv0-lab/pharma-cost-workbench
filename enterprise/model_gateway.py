"""Server-owned task routing and observed OpenAI-compatible JSON call traces.

Business results stay exact ``dict`` objects. Capture traces explicitly in the
worker making the request; configuration alone is never a response observation.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4

from paths import BASE_DIR

_LOCK = threading.BoundedSemaphore(2)
_CONFIG_LOCK = threading.RLock()
TASKS = frozenset({'attribution', 'benchmark', 'report', 'task', 'agent_proposal', 'summary'})
_TRACE_SCHEMA = 'enterprise-model-call/1.0'
_TRACE_SINKS = ContextVar('model_gateway_trace_sinks', default=())
_IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}\Z')
_PROFILE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z')


class ModelUnavailable(RuntimeError):
    def __init__(self, message, *, metadata=None):
        super().__init__(message)
        self.metadata = deepcopy(metadata) if metadata is not None else None


def _identifier(value, secret=''):
    """Allow bounded identifiers, not URLs, headers, prose or credential echoes."""
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value) or '://' in value:
        return None
    if secret and (value == secret or (len(secret) >= 4 and secret in value)):
        return None
    return value


def _public_endpoint(value, secret=''):
    # Keep safe legacy values; never echo credentials or secret-bearing URL parts.
    try:
        url = urlsplit(value)
        if (url.scheme not in ('http', 'https') or not url.hostname or url.username
                or url.password or url.query or url.fragment
                or (secret and secret in value)):
            return None
        return value
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True)
class ModelConfiguration:
    # The original five positional arguments remain unchanged.
    base_url: str
    model: str
    api_key: str = field(repr=False)
    approved_cloud: bool
    timeout: float = 40.0
    task: str | None = None
    profile: str = 'default'
    routing_enabled: bool = False
    routing_fallback: bool = False
    routing_reason: str = 'explicit_configuration'
    registry_id: str = 'legacy'
    provider_name: str = 'legacy'
    budget_contract: str | None = None
    configured_timeout: float | None = None
    thinking_mode: str | None = None

    def request_options(self):
        # Fixed server option only; no caller-controlled extra_body passthrough.
        if self.provider_name == 'deepseek' and self.budget_contract == 'registered-model/1':
            if self.thinking_mode != 'disabled':
                raise ModelUnavailable('受控DeepSeek配置须禁用思考模式')
            return {'extra_body': {'thinking': {'type': 'disabled'}}}
        if self.thinking_mode is not None:
            raise ModelUnavailable('模型提供方不支持该服务端请求选项')
        return {}

    def fingerprint(self):
        """Non-secret configuration identity, independent of any observed latency."""
        value = {'endpoint': self.base_url, 'model': self.model, 'approved_cloud': self.approved_cloud,
                 'timeout': self.configured_timeout if self.configured_timeout is not None else self.timeout,
                 'task': self.task, 'profile': self.profile, 'registry_id': self.registry_id,
                 'provider': self.provider_name, 'routing_enabled': self.routing_enabled,
                 'routing_fallback': self.routing_fallback, 'routing_reason': self.routing_reason,
                 'budget_contract': self.budget_contract, 'thinking_mode': self.thinking_mode,
                 'configured': bool(self.api_key)}
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                         allow_nan=False).encode()).hexdigest()

    def identity(self):
        return {'registry_id': _identifier(self.registry_id, self.api_key), 'requested_model': _identifier(self.model, self.api_key),
                'task': self.task, 'config_sha256': self.fingerprint()}

    def public(self):
        return {'base_url': _public_endpoint(self.base_url, self.api_key),
                'model': _identifier(self.model, self.api_key),
                'configured': bool(self.api_key and self.model and self.base_url),
                'approved_cloud': self.approved_cloud, 'timeout_seconds': self.timeout,
                'task': self.task, 'profile': _identifier(self.profile, self.api_key),
                'routing_enabled': self.routing_enabled,
                'routing_fallback': self.routing_fallback, 'routing_reason': self.routing_reason,
                'registry_id': _identifier(self.registry_id, self.api_key),
                'provider_name': _identifier(self.provider_name, self.api_key),
                'config_sha256': self.fingerprint()}


def config_path():
    return Path(os.environ.get('COST_LLM_CONFIG_FILE', str(BASE_DIR / '.local' / 'llm.json')))


def _task(value):
    if value is not None and (not isinstance(value, str) or value not in TASKS):
        raise ModelUnavailable('模型任务不在服务端白名单中')
    return value


def _timeout(value):
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ModelUnavailable('模型时限须在1至120秒之间') from None
    if isinstance(value, bool) or not math.isfinite(result) or not 1 <= result <= 120:
        raise ModelUnavailable('模型时限须在1至120秒之间')
    return result


def _read_local():
    local = {}
    path = config_path()
    if path.is_file():
        try:
            local = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise ModelUnavailable('本机模型配置不可读取') from None
    if not isinstance(local, dict):
        raise ModelUnavailable('模型配置须为JSON对象')
    return local


def configuration(task=None):
    """Resolve server selection only for M2/M3/report; preserve other task routes."""
    task = _task(task)
    local = _read_local()
    from enterprise.model_registry import SELECTED_TASKS, registered_configuration
    selected = local.get('selected_registry_id', 'legacy')
    if selected != 'legacy' and task in SELECTED_TASKS:
        return registered_configuration(selected, task=task, local=local)
    return _legacy_configuration(task=task, local=local)


def _legacy_configuration(task=None, *, local):
    """Existing environment/local precedence and task mapping, without selection."""
    config = ModelConfiguration(
        os.environ.get('COST_LLM_BASE_URL') or os.environ.get('DASHSCOPE_BASE_URL') or local.get('base_url') or 'https://dashscope.aliyuncs.com/compatible-mode/v1',
        os.environ.get('COST_LLM_MODEL') or local.get('model') or 'qwen-plus',
        os.environ.get('COST_LLM_API_KEY') or os.environ.get('DASHSCOPE_API_KEY') or local.get('api_key') or '',
        os.environ.get('COST_LLM_APPROVED_CLOUD', str(local.get('approved_cloud', False))).lower() == 'true',
        _timeout(os.environ.get('COST_LLM_TIMEOUT', '40')),
        task=task, routing_reason='routing_disabled',
    )
    routing = local.get('task_routing', {})
    if not isinstance(routing, dict) or type(routing.get('enabled', False)) is not bool:
        raise ModelUnavailable('任务路由须为服务端对象且enabled须为布尔值')
    if not routing.get('enabled', False):
        return config
    if set(routing) - {'enabled', 'profiles', 'tasks'}:
        raise ModelUnavailable('任务路由包含不允许的配置字段')
    profiles, tasks = routing.get('profiles', {}), routing.get('tasks', {})
    if not isinstance(profiles, dict) or not isinstance(tasks, dict):
        raise ModelUnavailable('任务路由profiles与tasks须为对象')
    for name, profile in profiles.items():
        if (not isinstance(name, str) or not _PROFILE.fullmatch(name) or name == 'default'
                or not isinstance(profile, dict) or set(profile) - {'model', 'timeout'}
                or not _identifier(profile.get('model'), config.api_key)):
            raise ModelUnavailable('模型profile无效；仅允许服务端model与timeout字段')
        if 'timeout' in profile:
            _timeout(profile['timeout'])
    for name, profile in tasks.items():
        if name not in TASKS or not isinstance(profile, str) or not _PROFILE.fullmatch(profile):
            raise ModelUnavailable('任务路由映射不在服务端白名单中')
    config = replace(config, routing_enabled=True)
    if task is None:
        return replace(config, routing_reason='task_unspecified')
    profile_name = tasks.get(task)
    if profile_name is None:
        return replace(config, routing_fallback=True, routing_reason='task_unmapped')
    if profile_name == 'default':
        return replace(config, routing_reason='default_profile')
    if profile_name not in profiles:
        return replace(config, routing_fallback=True, routing_reason='profile_missing')
    profile = profiles[profile_name]
    return replace(config, model=profile['model'], profile=profile_name,
                   timeout=_timeout(profile.get('timeout', config.timeout)), routing_reason='task_profile')


def _validate_endpoint(value, approved_cloud):
    url = urlsplit(value)
    from enterprise.security import is_loopback
    if url.username or url.password or url.query or url.fragment or not url.hostname:
        raise ValueError('模型地址须为无凭据、查询参数和片段的基础URL')
    local = url.hostname == 'localhost' or is_loopback(url.hostname)
    if local:
        if url.scheme not in ('http', 'https'):
            raise ValueError('本地模型地址协议无效')
    elif url.scheme != 'https' or not approved_cloud:
        raise ValueError('外部模型须使用HTTPS并确认接口及分析数据的使用权限')
    return local


def save_local_configuration(base_url, model, api_key, approved_cloud, *, principal):
    from enterprise.security import require
    require(principal, 'system.configure')
    _validate_endpoint(base_url, approved_cloud)
    if not model.strip() or not api_key.strip():
        raise ValueError('模型名与密钥必填；密钥不会在页面回显')
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {'base_url': base_url.rstrip('/'), 'model': model.strip(), 'api_key': api_key.strip(),
               'approved_cloud': bool(approved_cloud), 'updated': datetime.now(timezone.utc).isoformat()}
    # Legacy admin API stays compatible but does not erase unrelated routing/selection.
    from enterprise.operations import write_guard
    with _CONFIG_LOCK, write_guard(path.parent):
        existing = _read_local()
        existing.update(payload)
        _write_local(existing)
    return {'configured': True, 'model': _identifier(model.strip(), api_key.strip())}


def _write_local(payload):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.model-', dir=path.parent)
    temp = Path(temporary)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    try:
        path.chmod(0o600)
    except OSError:
        pass


@contextmanager
def capture_model_calls():
    """Collect safe call records made in this context, including failed attempts.

    Nested captures also reach their outer capture, once each. Nothing is cached
    globally. In subprocess workers create the capture there and send the plain
    records through the existing result channel; ContextVars are not an IPC API.
    """
    records = []
    token = _TRACE_SINKS.set(_TRACE_SINKS.get() + (records,))
    try:
        yield records
    finally:
        _TRACE_SINKS.reset(token)


def _emit_trace(trace):
    for records in _TRACE_SINKS.get():
        records.append(deepcopy(trace))


def _get(value, name):
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _usage(value):
    """Copy only known numeric accounting fields, never arbitrary provider extras."""
    if value is None:
        return None
    result = {}
    for name in ('prompt_tokens', 'completion_tokens', 'total_tokens', 'input_tokens',
                 'output_tokens', 'cached_tokens', 'reasoning_tokens', 'audio_tokens',
                 'accepted_prediction_tokens', 'rejected_prediction_tokens'):
        number = _get(value, name)
        if type(number) is int and 0 <= number <= 10**15:
            result[name] = number
    for name in ('prompt_tokens_details', 'completion_tokens_details',
                 'input_tokens_details', 'output_tokens_details'):
        details = _get(value, name)
        if details is not None:
            nested = {}
            for key in ('cached_tokens', 'reasoning_tokens', 'audio_tokens',
                        'accepted_prediction_tokens', 'rejected_prediction_tokens'):
                number = _get(details, key)
                if type(number) is int and 0 <= number <= 10**15:
                    nested[key] = number
            if nested:
                result[name] = nested
    return result


def _observe(trace, response, secret):
    trace.update(response_observed=True, source='provider_response',
                 returned_model=_identifier(_get(response, 'model'), secret),
                 model_version=_identifier(_get(response, 'model_version'), secret) or 'unknown',
                 model=_identifier(_get(response, 'model'), secret),
                 completion_id=_identifier(_get(response, 'id'), secret),
                 usage=_usage(_get(response, 'usage')))
    request_id = _identifier(_get(response, '_request_id'), secret)
    trace['request_id'] = request_id or trace['completion_id']
    trace['request_id_source'] = ('http_request_id' if request_id else
                                  'response_id' if trace['completion_id'] else 'unreported')


def generate_json(instruction, data, *, max_tokens=2500, config=None, task=None):
    """Return an exact business dict; use capture_model_calls for observed metadata.

    Explicit config remains a trusted Python injection for existing callers, not
    a request-body configuration API. Resolve it with the same task, if routing.
    No provider failure triggers a retry, a larger model or another endpoint.
    """
    started = time.monotonic()
    trace = {'schema': _TRACE_SCHEMA, 'call_id': uuid4().hex,
             'started_at': datetime.now(timezone.utc).isoformat(), 'task': None,
             'provider': 'openai_compatible', 'model': None, 'requested_model': None,
             'returned_model': None, 'model_version': 'unknown', 'registry_id': None,
             'config_sha256': None, 'paid_baseline_key': None,
             'request_id': None, 'request_id_source': 'unreported', 'completion_id': None,
             'usage': None, 'duration_seconds': None, 'profile': None,
             'routing_enabled': False, 'routing_fallback': False, 'routing_reason': 'unresolved',
             'request_attempted': False, 'response_observed': False, 'source': 'not_called',
             'status': 'unavailable', 'failure_type': None, 'prompt_sha256': None,
             'request_sha256': None, 'response_sha256': None,
             'temperature': 0.1, 'response_format': 'json_object'}
    locked = False
    failure = None
    try:
        task = _task(task)
        trace['task'] = task
        config = config if config is not None else (configuration(task=task) if task is not None else configuration())
        if not isinstance(config, ModelConfiguration):
            raise ModelUnavailable('模型配置须由服务端网关提供')
        config_task = _task(config.task)
        if task is not None and ((config_task is not None and config_task != task)
                                 or (config.routing_enabled and config_task != task)):
            raise ModelUnavailable('显式模型配置与调用任务不一致')
        trace.update(task=task or config_task, requested_model=_identifier(config.model, config.api_key),
                     profile=_identifier(config.profile, config.api_key), routing_enabled=config.routing_enabled,
                     routing_fallback=config.routing_fallback, routing_reason=config.routing_reason,
                     registry_id=_identifier(config.registry_id, config.api_key),
                     config_sha256=config.fingerprint(), paid_baseline_key=config.fingerprint())
        if not isinstance(config.api_key, str) or not config.api_key:
            raise ModelUnavailable('未配置模型密钥，请在运行设置中完成配置')
        if not isinstance(config.model, str) or not config.model.strip():
            raise ModelUnavailable('模型名称无效')
        _timeout(config.timeout)
        if config.budget_contract == 'registered-model/1' and config.approved_cloud is not True:
            raise ModelUnavailable('注册模型尚未获得服务端云数据授权')
        try:
            local = _validate_endpoint(config.base_url, config.approved_cloud)
        except (ValueError, TypeError):
            raise ModelUnavailable('模型地址无效或外部模型尚未获授权') from None
        if not isinstance(instruction, str):
            raise ModelUnavailable('模型指令须为文本')
        if type(max_tokens) is not int or max_tokens < 1:
            raise ModelUnavailable('模型输出上限须为正整数')
        output_tokens = min(max_tokens, 5000)
        user_content = json.dumps(data, ensure_ascii=False, allow_nan=False)
        trace['prompt_sha256'] = hashlib.sha256(instruction.encode('utf-8')).hexdigest()
        request_options = config.request_options()
        # kimi-k3 推理模型只允许 temperature=1；其余提供方沿用 0.1 低温度
        temperature = 1.0 if config.provider_name == 'moonshot' else 0.1
        trace['temperature'] = temperature
        trace['request_sha256'] = hashlib.sha256(json.dumps(
            [instruction, user_content, config.fingerprint(), output_tokens, temperature, 'json_object', request_options],
            ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
        if not _LOCK.acquire(timeout=1):
            raise ModelUnavailable('模型并发已满，请稍后重试')
        locked = True
        import httpx
        from openai import OpenAI
        # Only loopback bypasses proxies. Existing enterprise proxy/CA rules remain.
        with httpx.Client(trust_env=not local, timeout=config.timeout, follow_redirects=False) as http:
            with OpenAI(api_key=config.api_key, base_url=config.base_url, max_retries=0,
                        timeout=config.timeout, http_client=http) as client:
                trace.update(request_attempted=True, source='request_attempt')
                response = client.chat.completions.create(
                    model=config.model,
                    messages=[{'role': 'system', 'content': instruction},
                              {'role': 'user', 'content': user_content}],
                    response_format={'type': 'json_object'}, temperature=temperature,
                    max_tokens=output_tokens, **request_options)
                _observe(trace, response, config.api_key)
        def object_pairs(pairs):
            result = {}
            for name, item in pairs:
                if name in result:
                    raise ValueError('duplicate JSON key')
                result[name] = item
            return result
        def invalid_constant(value):
            raise ValueError('non-finite JSON value')
        content = response.choices[0].message.content
        if not isinstance(content, str) or not content.strip():
            raise ValueError('empty or non-text JSON response')
        trace['response_sha256'] = hashlib.sha256(content.encode('utf-8')).hexdigest()
        value = json.loads(content, object_pairs_hook=object_pairs, parse_constant=invalid_constant)
        if not isinstance(value, dict):
            raise ValueError('expected JSON object')
        trace['status'] = 'returned_json'
        return value
    except Exception as exc:
        trace['status'] = 'invalid_response' if trace['response_observed'] else 'unavailable'
        trace['failure_type'] = type(exc).__name__
        # Status exceptions may carry a request id but no completion. Never
        # inspect their body, message or headers: these can contain input/keys.
        if trace['request_attempted'] and not trace['request_id']:
            secret = config.api_key if isinstance(config, ModelConfiguration) else ''
            trace['request_id'] = _identifier(getattr(exc, 'request_id', None), secret)
            if trace['request_id']:
                trace['request_id_source'] = 'http_error_request_id'
        message = (str(exc) if isinstance(exc, ModelUnavailable) and not trace['request_attempted']
                   else '模型调用未完成：' + type(exc).__name__)
        failure = ModelUnavailable(message)
        raise failure from None
    finally:
        if locked:
            _LOCK.release()
        trace['duration_seconds'] = round(max(0.0, time.monotonic() - started), 6)
        if failure is not None:
            failure.metadata = deepcopy(trace)
        _emit_trace(trace)
        if trace.get('registry_id') not in (None, 'legacy'):
            from enterprise.model_registry import record_observation
            record_observation(trace)


def provenance(instruction='', *, task=None, trace=None):
    """Return an explicit observation, or clearly labelled legacy config preflight.

    ``provenance(instruction)`` keeps its original fields/positional signature for
    compatibility, but ``model`` there is a requested configuration, not proof of
    a provider response. Actual audit records must pass a captured trace instead.
    """
    task = _task(task)
    if trace is not None:
        if not isinstance(trace, dict) or trace.get('schema') != _TRACE_SCHEMA:
            raise ValueError('须提供capture_model_calls捕获的调用记录')
        if task is not None and trace.get('task') != task:
            raise ValueError('调用记录任务不一致')
        return json.loads(json.dumps(trace, ensure_ascii=False, allow_nan=False))
    config = configuration(task=task) if task is not None else configuration()
    return {'model': _identifier(config.model, config.api_key),
            'requested_model': _identifier(config.model, config.api_key),
            'provider_endpoint': _public_endpoint(config.base_url, config.api_key),
            'prompt_sha256': hashlib.sha256(instruction.encode('utf-8')).hexdigest(),
            'temperature': 0.1, 'response_format': 'json_object',
            'source': 'configured_only', 'response_observed': False,
            'request_attempted': False, 'task': task or config.task,
            'profile': _identifier(config.profile, config.api_key), 'routing_enabled': config.routing_enabled,
            'routing_fallback': config.routing_fallback, 'routing_reason': config.routing_reason}
