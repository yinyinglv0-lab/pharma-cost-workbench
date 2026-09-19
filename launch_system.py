"""Start the local API and dashboard with readiness checks and owned-process cleanup."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(url):
    with OPENER.open(url, timeout=1) as response:
        return response.read()


def free_port(preferred):
    for port in range(preferred, preferred + 30):
        with socket.socket() as sock:
            try:
                sock.bind(('127.0.0.1', port))
                return port
            except OSError:
                continue
    raise RuntimeError(f'No free local port near {preferred}')


def wait_ready(url, child, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError('Service exited during startup; see logs/startup/')
        try:
            get(url)
            return
        except (OSError, ValueError):
            time.sleep(0.4)
    raise RuntimeError(f'Service startup timed out: {url}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true', help='Check dependencies without launching')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--smoke-test', action='store_true', help='Start, check, and stop owned services')
    parser.add_argument('--no-worker', action='store_true', help='Do not start the approved-task worker')
    parser.add_argument('--with-mock', action='store_true', help='Start official mock only if port 8090 is unused')
    args = parser.parse_args()
    required = ['streamlit', 'uvicorn', 'fastapi', 'pandas', 'requests', 'reportlab', 'pypdf', 'jwt', 'authlib',
                'langchain_core', 'langsmith']
    missing = [name for name in required if importlib.util.find_spec(name) is None]
    if missing:
        print('Missing dependencies: ' + ', '.join(missing))
        print(f'Run: "{sys.executable}" -m pip install -r requirements.txt')
        return 1
    if args.check:
        print(f'OK: Python {sys.version.split()[0]}, core dependencies available')
        return 0

    children, handles = [], []
    logs = ROOT / 'logs' / 'startup'
    logs.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d_%H%M%S') + f'_{os.getpid()}'

    def start(name, command, env=None):
        handle = (logs / f'{stamp}_{name}.log').open('w', encoding='utf-8')
        handles.append(handle)
        child = subprocess.Popen(command, cwd=ROOT, env=env, stdout=handle,
                                 stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        children.append(child)
        return child

    try:
        api_port = 8000
        reuse = False
        try:
            spec = json.loads(get('http://127.0.0.1:8000/openapi.json'))
            from enterprise.build_info import source_fingerprint
            health = json.loads(get('http://127.0.0.1:8000/api/health'))
            reuse = (spec.get('info', {}).get('title') == '制药企业成本智能分析系统 API'
                     and spec.get('info', {}).get('version') == '2.0.0'
                     and health.get('source_fingerprint') == source_fingerprint()
                     and '/api/reports' in spec.get('paths', {}))
        except (OSError, ValueError):
            pass
        if not reuse:
            api_port = free_port(8000)
            child = start('api', [sys.executable, '-m', 'uvicorn', 'backend_api:app',
                                  '--host', '127.0.0.1', '--port', str(api_port)])
            wait_ready(f'http://127.0.0.1:{api_port}/api/health', child)
        api_url = f'http://127.0.0.1:{api_port}'
        if args.with_mock:
            try:
                mock_health = json.loads(get('http://127.0.0.1:8090/health'))
                if mock_health.get('status') != 'ok' or 'tasks_count' not in mock_health:
                    raise RuntimeError('Port 8090 is not the official mock; no unrelated process will be stopped')
            except OSError:
                configured = os.environ.get('COST_OFFICIAL_MOCK_SCRIPT')
                mock = Path(configured) if configured else ROOT.parent / '创灵境_考题模拟数据0816' / '创灵境_考题模拟数据' / '05_RPA接口文档' / 'mock_rpa_server.py'
                if not mock.is_file():
                    raise RuntimeError('Official mock script is missing; set COST_OFFICIAL_MOCK_SCRIPT')
                # Import official app via uvicorn, explicitly loopback (the source main binds all interfaces).
                child = start('mock', [sys.executable, '-m', 'uvicorn', 'mock_rpa_server:app',
                                      '--app-dir', str(mock.parent), '--host', '127.0.0.1', '--port', '8090'])
                wait_ready('http://127.0.0.1:8090/health', child)
        if not args.no_worker and not args.smoke_test:
            start('task_worker', [sys.executable, '-B', 'scripts/run_task_worker.py', '--interval', '2'])
        ui_port = free_port(8501)
        env = dict(os.environ, COST_API_BASE=api_url)
        child = start('dashboard', [sys.executable, '-m', 'streamlit', 'run',
                                    'enterprise_app.py', '--server.address', '127.0.0.1',
                                    '--server.port', str(ui_port), '--server.headless', 'true',
                                    '--browser.gatherUsageStats', 'false'], env)
        url = f'http://127.0.0.1:{ui_port}'
        wait_ready(url + '/_stcore/health', child)
        print(f'Dashboard: {url}', flush=True)
        print(f'API: {api_url} ({"existing service" if reuse else "started here"})', flush=True)
        print(f'Logs: {logs}', flush=True)
        if args.smoke_test:
            print('Startup smoke test passed; stopping owned services.', flush=True)
            return 0
        if not args.no_browser:
            webbrowser.open(url)
        print('Keep this window open. Press Ctrl+C to stop services started here.', flush=True)
        while True:
            if any(child.poll() is not None for child in children):
                raise RuntimeError('A service exited; see logs/startup/')
            time.sleep(1)
    except KeyboardInterrupt:
        print('\nStopping owned services...')
        return 0
    except (OSError, RuntimeError) as exc:
        print(f'Startup failed: {exc}', file=sys.stderr)
        return 1
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        for handle in handles:
            handle.close()


if __name__ == '__main__':
    raise SystemExit(main())
