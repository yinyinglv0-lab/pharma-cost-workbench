#!/usr/bin/env python3
"""Container-only supervisor. Fail closed unless enterprise OIDC is configured."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.preflight import collect_checks


def main():
    if os.environ.get('COST_AUTH_MODE') != 'oidc':
        print('Container deployment requires COST_AUTH_MODE=oidc.', file=sys.stderr)
        return 1
    report = collect_checks(production=True, bind_host='0.0.0.0')
    for check in report['checks']:
        if check['status'] != 'pass':
            print(f"{check['status'].upper()} {check['name']}: {check['detail']}", flush=True)
    if not report['ok']:
        return 1
    children = []
    stopping = False

    def stop(_signal, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    environment = dict(os.environ, COST_API_BASE='http://127.0.0.1:8000')
    commands = [
        [sys.executable, '-m', 'uvicorn', 'backend_api:app', '--host', '0.0.0.0',
         '--port', '8000', '--workers', '1', '--no-access-log', '--no-proxy-headers'],
        [sys.executable, '-m', 'streamlit', 'run', 'enterprise_app.py', '--server.address=0.0.0.0',
         '--server.port=8501', '--server.headless=true', '--browser.gatherUsageStats=false'],
        [sys.executable, 'scripts/run_task_worker.py'],
    ]
    try:
        for command in commands:
            children.append(subprocess.Popen(command, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL))
        while not stopping:
            if any(child.poll() is not None for child in children):
                print('Application process exited; shutting down the instance.', file=sys.stderr)
                return 1
            time.sleep(.25)
        return 0
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
        deadline = time.monotonic() + 20
        for child in children:
            try:
                child.wait(timeout=max(.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == '__main__':
    raise SystemExit(main())
