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


def credential_status():
    from enterprise.model_gateway import configuration, config_path
    config = configuration()
    source = ('environment:COST_LLM_API_KEY' if os.environ.get('COST_LLM_API_KEY') else
              'environment:DASHSCOPE_API_KEY' if os.environ.get('DASHSCOPE_API_KEY') else
              'local_configuration' if config_path().is_file() else 'not_configured')
    return {'configured': bool(config.api_key.strip()), 'length': len(config.api_key), 'source': source}


def _partial_audit(path):
    """Only the trusted worker's metadata, never partial candidates/provider data."""
    try:
        value = json.loads(path.read_text(encoding='utf-8')).get('model_run', {})
        if not isinstance(value, dict):
            return {}
        allowed = {'attempt', 'kind', 'prompt_version', 'instruction_sha256', 'request_sha256',
                   'response_sha256', 'status', 'diagnostics', 'elapsed_seconds',
                   'request_timeout_seconds', 'used', 'failure_type'}
        attempts = [{key: item for key, item in row.items() if key in allowed}
                    for row in value.get('attempts', [])[:2] if isinstance(row, dict)]
        for row in attempts:
            row['used'] = False
            if row.get('status') == 'running':
                row['status'] = 'interrupted'
        correction = dict(value.get('correction', {}))
        if correction.get('status') == 'running':
            correction['status'] = 'interrupted'
        return {'attempts': attempts, 'correction': correction}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def run_stage(stage: str, args: list | dict, timeout: float = 30.0) -> Any:
    """Return original JSON result, or raise StageExecutionError.

    rag args: [query] / [query, top_k] / {query, top_k?}.
    model args: [payload, evidence] / {payload, evidence}.
    The hard deadline includes worker imports, local model loading and network calls.
    """
    if stage not in {'rag', 'model'}:
        raise ValueError('stage must be rag or model')
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
