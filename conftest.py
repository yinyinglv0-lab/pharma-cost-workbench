"""Tests never use the live managed database or configured model credentials."""
from __future__ import annotations
import atexit
import os
from pathlib import Path
import sys
import tempfile

# Set before test-module imports (some modules bind paths at import time).
_TEST_SESSION = tempfile.TemporaryDirectory(prefix='cost-workbench-tests-')
_SESSION_ROOT = Path(_TEST_SESSION.name)
atexit.register(_TEST_SESSION.cleanup)
os.environ['COST_MANAGED_DIR'] = str(_SESSION_ROOT / 'managed')
os.environ['COST_CHROMA_PATH'] = str(_SESSION_ROOT / 'chroma')
os.environ['COST_LLM_CONFIG_FILE'] = str(_SESSION_ROOT / 'no-model.json')
os.environ['COST_LLM_API_KEY'] = ''
os.environ.pop('DASHSCOPE_API_KEY', None)
os.environ.pop('DEEPSEEK_API_KEY', None)
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
os.environ.setdefault('STREAMLIT_SERVER_ADDRESS', '127.0.0.1')

import pytest


@pytest.fixture(autouse=True)
def isolated_system_root(tmp_path, monkeypatch):
    managed = tmp_path / 'isolated-system'
    monkeypatch.setenv('COST_MANAGED_DIR', str(managed))
    monkeypatch.setenv('COST_AUTH_MODE', 'local')
    monkeypatch.setenv('COST_TENANT_ID', 'default')
    monkeypatch.setenv('COST_LLM_CONFIG_FILE', str(tmp_path / 'no-model.json'))
    monkeypatch.setenv('COST_LLM_API_KEY', '')
    monkeypatch.delenv('DASHSCOPE_API_KEY', raising=False)
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.delenv('COST_WORKER_SUBJECT', raising=False)
    for name, module in list(sys.modules.items()):
        if module is None:
            continue
        if name == 'paths' or name == 'backend_api' or name.startswith(('enterprise.', 'dashboard.')):
            if hasattr(module, 'MANAGED_DIR'):
                monkeypatch.setattr(module, 'MANAGED_DIR', managed)
    if 'audit_log' in sys.modules:
        monkeypatch.setattr(sys.modules['audit_log'], 'DB_PATH', managed / 'kb_audit.db')
    import streamlit as st
    from streamlit import config
    config.set_option('server.address', '127.0.0.1')
    # AppTest uses a MagicMock client context; supply the simulated transport IP
    # in tests only. Real browser requests still undergo production IP checks.
    monkeypatch.setattr(type(st.context), 'ip_address', property(lambda self: '127.0.0.1'))
    yield managed
