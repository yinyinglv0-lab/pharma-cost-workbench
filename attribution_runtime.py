# -*- coding: utf-8 -*-
"""Killable subprocess boundary for attribution RAG/model stages.

No pipe transports and no background timeout threads. Each worker inherits the
current process environment; keys are never put in JSON input or diagnostics.
"""
from __future__ import annotations
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any

_WORKER = Path(__file__).with_name('attribution_worker.py')


class StageExecutionError(RuntimeError):
    """Only safe diagnostics: no child stderr, API keys or request headers."""
    def __init__(self, stage, error_type, message, timed_out=False, model_run=None):
        super().__init__(message)
        self.stage = stage
        self.error_type = error_type
        self.timed_out = timed_out
        self.model_run = model_run or {}


def credential_status(*, task=None, config=None):
    """Legacy diagnostic stays compatible; task-aware registry status reveals no length."""
    from enterprise.model_gateway import configuration, config_path, ModelConfiguration, ModelUnavailable
    config = config if config is not None else configuration(task=task) if task is not None else configuration()
    if not isinstance(config, ModelConfiguration):
        raise ModelUnavailable('凭据状态须使用服务端模型配置')
    if config.registry_id != 'legacy':
        source = {'dashscope': 'environment:DASHSCOPE_API_KEY', 'deepseek': 'environment:DEEPSEEK_API_KEY'}
        return {'configured': bool(config.api_key.strip()), 'registry_id': config.registry_id,
                'source': source.get(config.provider_name, 'not_configured') if config.api_key.strip() else 'not_configured'}
    source = ('environment:COST_LLM_API_KEY' if os.environ.get('COST_LLM_API_KEY') else
              'environment:DASHSCOPE_API_KEY' if os.environ.get('DASHSCOPE_API_KEY') else
              'local_configuration' if config_path().is_file() else 'not_configured')
    return {'configured': bool(config.api_key.strip()), 'length': len(config.api_key), 'source': source}


def sanitize_model_calls(value, *, secret=''):
    """Whitelist captured gateway records at the worker/persistent-audit boundary.

    Never accept arbitrary provider objects, response bodies, messages, URLs or
    headers. Gateway capture already redacts credentials; the optional secret also
    protects explicit caller configuration when applying this second projection.
    """
    import re
    from datetime import datetime
    from enterprise.model_gateway import provenance
    if not isinstance(value, list):
        return []
    identifiers = {'call_id', 'model', 'requested_model', 'request_id', 'completion_id',
                   'profile', 'failure_type', 'registry_id', 'returned_model', 'model_version'}
    hashes = {'prompt_sha256', 'request_sha256', 'response_sha256', 'config_sha256', 'paid_baseline_key'}
    booleans = {'routing_enabled', 'routing_fallback', 'request_attempted', 'response_observed'}
    enums = {
        'task': {'attribution', 'benchmark', 'report', 'task', 'agent_proposal', 'summary'},
        'provider': {'openai_compatible'},
        'request_id_source': {'unreported', 'http_request_id', 'response_id', 'http_error_request_id'},
        'routing_reason': {'unresolved', 'explicit_configuration', 'routing_disabled', 'task_unspecified',
                           'task_unmapped', 'default_profile', 'profile_missing', 'task_profile', 'selected_registry'},
        'source': {'not_called', 'request_attempt', 'provider_response'},
        'status': {'unavailable', 'invalid_response', 'returned_json'},
        'response_format': {'json_object'},
    }
    counts = {'prompt_tokens', 'completion_tokens', 'total_tokens', 'input_tokens', 'output_tokens',
              'cached_tokens', 'reasoning_tokens', 'audio_tokens',
              'accepted_prediction_tokens', 'rejected_prediction_tokens'}
    detail_counts = counts - {'prompt_tokens', 'completion_tokens', 'total_tokens', 'input_tokens', 'output_tokens'}
    details = {'prompt_tokens_details', 'completion_tokens_details', 'input_tokens_details', 'output_tokens_details'}
    result = []
    for trace in value[:16]:
        if not isinstance(trace, dict) or trace.get('schema') != 'enterprise-model-call/1.0':
            continue
        safe = {'schema': trace['schema']}
        for name in identifiers:
            item = trace.get(name)
            safe[name] = item if (isinstance(item, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}', item)
                and '://' not in item and not (secret and (item == secret or len(secret) >= 4 and secret in item))) else None
        for name in hashes:
            item = trace.get(name)
            safe[name] = item if isinstance(item, str) and re.fullmatch(r'[0-9a-f]{64}', item) else None
        for name in booleans:
            safe[name] = trace.get(name) is True
        for name, choices in enums.items():
            item = trace.get(name)
            safe[name] = item if isinstance(item, str) and item in choices else None
        for name, limit in (('duration_seconds', 3600), ('temperature', 2)):
            item = trace.get(name)
            safe[name] = item if type(item) in (int, float) and 0 <= item <= limit and math.isfinite(item) else None
        stamp = trace.get('started_at')
        try:
            safe['started_at'] = stamp if isinstance(stamp, str) and len(stamp) <= 40 and datetime.fromisoformat(stamp).tzinfo else None
        except ValueError:
            safe['started_at'] = None
        usage = trace.get('usage')
        safe['usage'] = None
        if isinstance(usage, dict):
            safe['usage'] = {key: item for key, item in usage.items()
                             if key in counts and type(item) is int and 0 <= item <= 10**15}
            for name in details:
                if not isinstance(usage.get(name), dict):
                    continue
                nested = {key: item for key, item in usage[name].items()
                          if key in detail_counts and type(item) is int and 0 <= item <= 10**15}
                if nested:
                    safe['usage'][name] = nested
        result.append(provenance(trace=safe))
    return result


def _partial_audit(path):
    """Only the trusted worker's metadata, never partial candidates/provider data."""
    try:
        value = json.loads(path.read_text(encoding='utf-8')).get('model_run', {})
        if not isinstance(value, dict):
            return {}
        allowed = {'attempt', 'kind', 'prompt_version', 'instruction_sha256', 'request_sha256',
                   'response_sha256', 'status', 'diagnostics', 'validation_diagnostics', 'elapsed_seconds',
                   'request_timeout_seconds', 'used', 'failure_type', 'model_calls'}
        attempts = [{key: item for key, item in row.items() if key in allowed}
                    for row in value.get('attempts', [])[:2] if isinstance(row, dict)]
        for row in attempts:
            row['model_calls'] = sanitize_model_calls(row.get('model_calls'))
            row['used'] = False
            if row.get('status') == 'running':
                row['status'] = 'interrupted'
        original_correction = value.get('correction', {})
        correction = {'attempted': original_correction.get('attempted') is True,
                      'status': original_correction.get('status')}
        if correction['status'] not in {'not_requested', 'not_needed', 'running', 'validated', 'rejected',
                'skipped_insufficient_budget', 'failed_unavailable', 'failed_budget', 'interrupted'}:
            correction['status'] = 'interrupted'
        if correction.get('status') == 'running':
            correction['status'] = 'interrupted'
        return {'attempts': attempts, 'correction': correction,
                'model_calls': [call for row in attempts for call in row['model_calls']]}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


class ModelStageArguments(list):
    """Trusted Python capability, not constructible through a business JSON payload.

    List shape preserves existing stage adapters. Only safe configuration identity
    crosses the worker file boundary; credentials remain in server configuration.
    """
    def __init__(self, payload, evidence, *, config):
        from enterprise.prose_contract import model_stage_budget
        super().__init__([payload, evidence])
        self.identity = config.identity()
        self.budget = model_stage_budget(payload, config=config)


def prepare_model_stage(payload, evidence, *, task, config=None):
    from enterprise.model_gateway import configuration, ModelConfiguration, ModelUnavailable
    from dataclasses import replace
    config = config if config is not None else configuration(task=task)
    if not isinstance(config, ModelConfiguration):
        raise ModelUnavailable('模型配置须由服务端解析')
    if config.task is None and not config.routing_enabled:
        config = replace(config, task=task)
    if config.task != task:
        raise ModelUnavailable('模型配置任务与工作器不一致')
    args = ModelStageArguments(payload, evidence, config=config)
    return args, args.budget


def resolve_worker_configuration(identity):
    """Resolve again before work; never silently switch if selection changed."""
    from enterprise.model_gateway import configuration, ModelUnavailable
    if not isinstance(identity, dict) or set(identity) != {'registry_id', 'requested_model', 'task', 'config_sha256'}:
        raise ModelUnavailable('工作器缺少受控模型配置身份')
    config = configuration(task=identity.get('task'))
    if config.identity() != identity:
        raise ModelUnavailable('模型配置已更改，请重新发起分析')
    return config


def run_stage(stage: str, args: list | dict, timeout: float = 30.0) -> Any:
    """Return original JSON result, or raise StageExecutionError.

    rag args: [query] / [query, top_k] / {query, top_k?}.
    model args: [payload, evidence] / {payload, evidence}.
    The hard deadline includes worker imports, local model loading and network calls.
    """
    if stage not in {'rag', 'model'}:
        raise ValueError('stage must be rag or model')
    if not isinstance(args, (list, dict)):
        raise TypeError('args must be list or dict')
    trusted_identity = None
    if stage == 'model':
        if isinstance(args, ModelStageArguments):
            frozen = resolve_worker_configuration(args.identity)
            from enterprise.prose_contract import model_stage_budget
            expected_budget = model_stage_budget(args[0], config=frozen)
            if expected_budget != args.budget or float(timeout) != expected_budget:
                raise ValueError('model stage budget does not match frozen configuration')
            trusted_identity = frozen.identity()
        else:
            # Compatibility entry point: resolve from server, never from JSON flags.
            # Caller timeout can shorten, never enlarge, the authoritative budget.
            payload = args[0] if isinstance(args, list) and args else args.get('payload', {}) if isinstance(args, dict) else {}
            task = 'benchmark' if payload.get('schema_version') == 'benchmark-explanations/1.0' else 'attribution'
            prepared, maximum = prepare_model_stage(payload, [], task=task)
            trusted_identity = prepared.identity
            timeout = min(float(timeout), maximum)
    limit = float(timeout)
    if not math.isfinite(limit) or not 0 < limit <= 3600:
        raise ValueError('timeout must be finite and >0, <=3600 seconds')
    if isinstance(args, list):
        if stage == 'rag' and len(args) in (1, 2):
            args = {'query': args[0], 'top_k': args[1] if len(args) == 2 else 4}
        elif stage == 'model' and len(args) == 2:
            args = {'payload': args[0], 'evidence': args[1]}
        else:
            raise ValueError('invalid stage argument count')
    if not isinstance(args, dict):
        raise TypeError('args must be list or dict')
    deadline = time.monotonic() + limit
    # The internal deadline is host-controlled; caller metadata cannot extend it.
    args = {**args, '_deadline': deadline}
    args.pop('_model_identity', None)
    if trusted_identity is not None:
        args['_model_identity'] = trusted_identity
    encoded = json.dumps(args, ensure_ascii=False, allow_nan=False)
    with tempfile.TemporaryDirectory(prefix='attr_stage_') as folder:
        root = Path(folder)
        inp, out = root / 'input.json', root / 'output.json'
        inp.write_text(encoded, encoding='utf-8')
        # Dependency libraries sometimes print exception request details. Discard
        # raw output instead of persisting potentially sensitive third-party logs.
        with open(os.devnull, 'wb') as output:
            try:
                proc = subprocess.Popen(
                    [sys.executable, str(_WORKER), stage, str(inp), str(out)],
                    stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                    cwd=str(_WORKER.parent), env=os.environ.copy(),
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            except OSError as exc:
                raise StageExecutionError(stage, type(exc).__name__, 'could not start worker') from None
            try:
                proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
                raise StageExecutionError(stage, 'TimeoutError',
                                          f'{stage} stage exceeded time limit', True,
                                          model_run=_partial_audit(out)) from None
            except BaseException:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
                raise
        try:
            message = json.loads(out.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise StageExecutionError(stage, 'WorkerError', 'worker produced no valid result') from None
        if not isinstance(message, dict) or message.get('ok') is not True:
            error_type = message.get('error_type', 'WorkerError') if isinstance(message, dict) else 'WorkerError'
            raise StageExecutionError(stage, error_type, 'worker stage failed', model_run=_partial_audit(out))
        return message.get('result')
