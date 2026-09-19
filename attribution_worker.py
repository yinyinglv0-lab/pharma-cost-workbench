# -*- coding: utf-8 -*-
"""Private worker for bounded attribution stages.

Protocol: input/output are UTF-8 JSON files. stdout/stderr are deliberately not
used for result transport, so a parent timeout can terminate this process on all
platforms without a pipe reader or a thread left behind.
"""
from __future__ import annotations
import json
import os
import sys
import time
from pathlib import Path


def _write(path: Path, value: dict) -> None:
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    os.replace(temporary, path)


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        return 2
    stage, input_path, output_path = argv[1], Path(argv[2]), Path(argv[3])
    audit = {}
    try:
        args = json.loads(input_path.read_text(encoding='utf-8'))
        if os.environ.get('ATTRIBUTION_RUNTIME_TEST_MODE') == '1' and '_test_model_run' in args:
            audit = args['_test_model_run']
            _write(output_path, {'ok': False, 'error_type': 'InProgress', 'model_run': audit})
        if os.environ.get('ATTRIBUTION_RUNTIME_TEST_MODE') == '1' and args.get('_test_pid_file'):
            Path(args['_test_pid_file']).write_text(str(os.getpid()), encoding='ascii')
        if os.environ.get('ATTRIBUTION_RUNTIME_TEST_MODE') == '1' and args.get('_test_identity_file'):
            _write(Path(args['_test_identity_file']), {'pid': os.getpid(), 'parent_pid': os.getppid(),
                   'sys_executable': sys.executable, 'sys_prefix': sys.prefix})
        if os.environ.get('ATTRIBUTION_RUNTIME_TEST_MODE') == '1' and args.get('_test_sleep') is not None:
            time.sleep(float(args['_test_sleep']))
        if os.environ.get('ATTRIBUTION_RUNTIME_TEST_MODE') == '1' and '_test_result' in args:
            result = args['_test_result']
        elif stage == 'rag':
            from attribution_gen import rag_evidence
            result = rag_evidence(args['query'], int(args.get('top_k', 4)))
        elif stage == 'model':
            from attribution_gen import _llm_generate
            def record_attempts(value):
                audit.clear()
                audit.update(value)
                _write(output_path, {'ok': False, 'error_type': 'InProgress', 'model_run': value})
            result = _llm_generate(args['payload'], args.get('evidence', []),
                                   deadline=args.get('_deadline'), attempt_recorder=record_attempts)
        else:
            raise ValueError('unsupported stage')
        _write(output_path, {'ok': True, 'result': result})
        return 0
    except Exception as exc:
        # Do not serialize traceback, environment, request headers, or API keys.
        try:
            _write(output_path, {'ok': False, 'error_type': type(exc).__name__,
                                 'error': 'worker stage failed', 'model_run': audit})
        except Exception:
            pass
        return 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
