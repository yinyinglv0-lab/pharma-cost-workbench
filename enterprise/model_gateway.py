"""Configured OpenAI-compatible JSON generation; no secrets in business records."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
import threading
from urllib.parse import urlsplit

from paths import BASE_DIR

_LOCK = threading.BoundedSemaphore(2)


class ModelUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelConfiguration:
    base_url: str
    model: str
    api_key: str
    approved_cloud: bool
    timeout: float = 40.0

    def public(self):
        return {'base_url': self.base_url, 'model': self.model,
                'configured': bool(self.api_key and self.model and self.base_url),
                'approved_cloud': self.approved_cloud, 'timeout_seconds': self.timeout}


def config_path():
    return Path(os.environ.get('COST_LLM_CONFIG_FILE', str(BASE_DIR / '.local' / 'llm.json')))


def configuration():
    local = {}
    if config_path().is_file():
        try:
            local = json.loads(config_path().read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise ModelUnavailable('本机模型配置不可读取') from None
    if not isinstance(local, dict):
        raise ModelUnavailable('模型配置须为JSON对象')
    timeout = float(os.environ.get('COST_LLM_TIMEOUT', '40'))
    if not math.isfinite(timeout) or not 1 <= timeout <= 120:
        raise ModelUnavailable('模型时限须在1至120秒之间')
    return ModelConfiguration(
        os.environ.get('COST_LLM_BASE_URL') or os.environ.get('DASHSCOPE_BASE_URL') or local.get('base_url') or 'https://dashscope.aliyuncs.com/compatible-mode/v1',
        os.environ.get('COST_LLM_MODEL') or local.get('model') or 'qwen-plus',
        os.environ.get('COST_LLM_API_KEY') or os.environ.get('DASHSCOPE_API_KEY') or local.get('api_key') or '',
        os.environ.get('COST_LLM_APPROVED_CLOUD', str(local.get('approved_cloud', False))).lower() == 'true',
        timeout,
    )


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
    return {'configured': True, 'model': model.strip()}


def generate_json(instruction, data, *, max_tokens=2500, config=None):
    config = config or configuration()
    if not config.api_key:
        raise ModelUnavailable('未配置模型密钥，请在运行设置中完成配置')
    try:
        local = _validate_endpoint(config.base_url, config.approved_cloud)
    except ValueError as exc:
        raise ModelUnavailable(str(exc)) from None
    if not _LOCK.acquire(timeout=1):
        raise ModelUnavailable('模型并发已满，请稍后重试')
    try:
        import httpx
        from openai import OpenAI
        # Only loopback bypasses environment proxies. Enterprise outbound proxy/CA
        # settings remain effective for the approved cloud endpoint.
        with httpx.Client(trust_env=not local, timeout=config.timeout, follow_redirects=False) as http:
            with OpenAI(api_key=config.api_key, base_url=config.base_url, max_retries=0,
                        timeout=config.timeout, http_client=http) as client:
                response = client.chat.completions.create(
                    model=config.model,
                    messages=[{'role': 'system', 'content': instruction},
                              {'role': 'user', 'content': json.dumps(data, ensure_ascii=False, allow_nan=False)}],
                    response_format={'type': 'json_object'}, temperature=0.1,
                    max_tokens=min(max_tokens, 5000))
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
        value = json.loads(content, object_pairs_hook=object_pairs, parse_constant=invalid_constant)
        if not isinstance(value, dict):
            raise ValueError('expected JSON object')
        return value
    except Exception as exc:
        # Third-party messages may contain URLs/headers/prompts; never expose them.
        raise ModelUnavailable('模型调用未完成：' + type(exc).__name__) from None
    finally:
        _LOCK.release()


def provenance(instruction=''):
    config = configuration()
    return {'model': config.model, 'provider_endpoint': config.base_url,
            'prompt_sha256': hashlib.sha256(instruction.encode('utf-8')).hexdigest(),
            'temperature': 0.1, 'response_format': 'json_object'}
