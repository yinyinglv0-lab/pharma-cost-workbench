#!/usr/bin/env python3
"""Read-only startup/rehearsal checks. No model import/download and no key output.

Exit 0: all mandatory checks pass (warnings may describe degraded capabilities).
Exit 1: at least one mandatory check failed. Exit 2: invalid CLI arguments.
OIDC reachability is only tested with --network; without it the report says so.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import shutil
import sys
import tomllib
from urllib.parse import urlsplit
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from enterprise.operations import (MAINTENANCE_FILE, RESTORE_REVIEW_FILE,
    OperationsError, inspect_managed_integrity, operations_metrics, sha256_file)

PROJECT = Path(__file__).resolve().parents[1]
CORE = {'streamlit': 'streamlit', 'fastapi': 'fastapi', 'uvicorn': 'uvicorn',
        'pandas': 'pandas', 'numpy': 'numpy', 'requests': 'requests', 'httpx': 'httpx',
        'python-docx': 'docx', 'openpyxl': 'openpyxl', 'pypdf': 'pypdf',
        'reportlab': 'reportlab', 'matplotlib': 'matplotlib', 'openai': 'openai',
        'python-multipart': 'python_multipart', 'pydantic': 'pydantic',
        'Authlib': 'authlib', 'PyJWT': 'jwt', 'cryptography': 'cryptography',
        'langchain-core': 'langchain_core', 'langsmith': 'langsmith'}
# Current managed dense retrieval uses transformers directly. Legacy Chroma,
# FlagEmbedding/BM25/Jieba/NetworkX tools are not application prerequisites.
OPTIONAL = {'torch': 'torch', 'transformers': 'transformers', 'tokenizers': 'tokenizers',
            'safetensors': 'safetensors', 'sentencepiece': 'sentencepiece',
            'huggingface-hub': 'huggingface_hub'}
ROLES = {'analyst', 'supervisor', 'knowledge_admin', 'auditor', 'system_admin'}


def _https(value):
    try:
        url = urlsplit(value)
        return (url.scheme == 'https' and bool(url.hostname) and not url.username
                and not url.password and not url.fragment and not url.query)
    except (TypeError, ValueError):
        return False


def _nearest(path):
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def _memory_available():
    """Host available memory, reduced by a cgroup v2 limit when present."""
    available = None
    try:
        if os.name == 'nt':
            import ctypes
            class MemoryStatus(ctypes.Structure):
                _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong),
                    ('total_phys', ctypes.c_ulonglong), ('avail_phys', ctypes.c_ulonglong),
                    ('total_page', ctypes.c_ulonglong), ('avail_page', ctypes.c_ulonglong),
                    ('total_virtual', ctypes.c_ulonglong), ('avail_virtual', ctypes.c_ulonglong),
                    ('avail_extended', ctypes.c_ulonglong)]
            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                available = status.avail_phys
        elif Path('/proc/meminfo').exists():
            lines = Path('/proc/meminfo').read_text().splitlines()
            available = next(int(line.split()[1]) * 1024 for line in lines if line.startswith('MemAvailable:'))
        if Path('/sys/fs/cgroup/memory.max').is_file():
            maximum = Path('/sys/fs/cgroup/memory.max').read_text().strip()
            if maximum != 'max':
                used = int(Path('/sys/fs/cgroup/memory.current').read_text().strip())
                remaining = max(0, int(maximum) - used)
                available = remaining if available is None else min(available, remaining)
    except (OSError, ValueError, AttributeError, StopIteration):
        pass
    return available


def _dependency(distribution, module):
    try:
        present = importlib.util.find_spec(module) is not None
        version = importlib.metadata.version(distribution) if present else None
        return present, version
    except (ImportError, ValueError, importlib.metadata.PackageNotFoundError):
        return False, None


def collect_checks(*, data_root=None, managed_root=None, bind_host=None, production=False,
                   network=False, deep_hash=False, min_free_mib=1024, secrets_file=None):
    from paths import DATA_DIR, MANAGED_DIR, MODEL_PATH, RERANKER_PATH
    data = Path(data_root or DATA_DIR).resolve()
    managed = Path(managed_root or MANAGED_DIR).resolve()
    mode = os.environ.get('COST_AUTH_MODE', 'local').lower().strip()
    production = production or mode == 'oidc'
    checks = []

    def add(name, ok, required, detail):
        checks.append({'name': name, 'required': required,
                       'status': 'pass' if ok else ('fail' if required else 'warn'), 'detail': detail})

    add('python', sys.version_info >= (3, 11), True, 'Python ' + sys.version.split()[0] + '; requires >=3.11')
    dependencies = {**CORE, **OPTIONAL}
    for distribution, module in dependencies.items():
        present, version = _dependency(distribution, module)
        required = distribution in CORE
        missing = ('missing; install approved requirements.txt' if required else
                   'missing; local semantic retrieval needs requirements-models.txt')
        add('dependency.' + distribution, present, required, version if present else missing)
    add('auth.mode', mode in ('local', 'oidc') and (not production or mode == 'oidc'), True,
        'production requires oidc; local requires loopback and a single OS user')
    if mode == 'local':
        host = bind_host or os.environ.get('COST_BIND_HOST', '127.0.0.1')
        try:
            local = host == 'localhost' or ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = False
        add('auth.local_bind', local, True, 'local mode binding must be 127.0.0.1/::1/localhost')
    add('tenant', os.environ.get('COST_TENANT_ID', 'default') == 'default', True,
        'only tenant default is supported by this delivery')
    auth_secrets = None
    if mode == 'oidc':
        issuer = os.environ.get('COST_OIDC_ISSUER', '')
        jwks = os.environ.get('COST_OIDC_JWKS_URL', '')
        add('oidc.issuer', _https(issuer), True, 'HTTPS issuer configured' if _https(issuer) else 'missing or invalid HTTPS issuer')
        add('oidc.jwks', _https(jwks), True, 'HTTPS JWKS configured' if _https(jwks) else 'missing or invalid HTTPS JWKS URL')
        audience = os.environ.get('COST_OIDC_AUDIENCE', '').strip()
        add('oidc.audience', bool(audience) and 'replace' not in audience.lower(), True, 'configured' if audience else 'missing')
        try:
            authorization = Path(os.environ.get('COST_AUTHORIZATION_FILE', ''))
            document = json.loads(authorization.read_text(encoding='utf-8'))
            users = document['users']
            valid = isinstance(users, dict)
            enabled = 0
            if valid:
                for subject, entry in users.items():
                    if not isinstance(subject, str) or not subject or not isinstance(entry, dict):
                        valid = False
                        break
                    if not entry.get('enabled', True):
                        continue
                    enabled += 1
                    roles = entry.get('roles')
                    valid = valid and isinstance(roles, list) and bool(roles) and set(roles) <= ROLES
                    valid = valid and entry.get('tenant_id', 'default') == 'default'
                    for field in ('factories', 'products'):
                        scope = entry.get(field)
                        valid = valid and isinstance(scope, list) and all(isinstance(x, str) and x.strip() for x in scope)
                        if roles and set(roles) != {'system_admin'}:
                            valid = valid and bool(scope)
            add('oidc.authorization', bool(valid and enabled), True,
                f'{enabled} enabled entries; schema valid={bool(valid)}; no subject values emitted')
        except (OSError, ValueError, KeyError, TypeError):
            add('oidc.authorization', False, True, 'authorization JSON missing, unreadable or malformed')
        try:
            from enterprise.security import worker_principal
            worker = worker_principal()
            worker_ok = bool(worker.factories and worker.products)
            add('worker.authorization', worker_ok, True,
                'mapped service subject has task.send and nonempty scopes; subject withheld' if worker_ok else
                'worker scope is empty; configure approved factories/products before startup')
        except (PermissionError, OSError, ValueError, TypeError) as exc:
            add('worker.authorization', False, True,
                'configure COST_WORKER_SUBJECT as an enabled server-mapped identity with task.send and scopes; '
                'worker will not start (' + type(exc).__name__ + ')')
        candidate = Path(secrets_file or os.environ.get('COST_STREAMLIT_SECRETS', str(PROJECT / '.streamlit' / 'secrets.toml')))
        try:
            auth_secrets = tomllib.loads(candidate.read_text(encoding='utf-8'))['auth']
            required = ('redirect_uri', 'cookie_secret', 'client_id', 'client_secret', 'server_metadata_url')
            valid = all(isinstance(auth_secrets.get(key), str) and auth_secrets[key].strip() for key in required)
            valid = valid and all('replace' not in auth_secrets[key].lower() and 'changeme' not in auth_secrets[key].lower() for key in required)
            valid = valid and len(auth_secrets.get('cookie_secret', '')) >= 32
            valid = valid and _https(auth_secrets.get('redirect_uri')) and str(auth_secrets.get('redirect_uri', '')).endswith('/oauth2callback')
            valid = valid and _https(auth_secrets.get('server_metadata_url'))
            add('oidc.streamlit_secrets', bool(valid), True,
                'auth configuration validated; values withheld' if valid else 'required auth keys/HTTPS callback/cookie secret are invalid')
        except (OSError, ValueError, KeyError, TypeError):
            add('oidc.streamlit_secrets', False, True, 'Streamlit secrets missing or malformed; no values emitted')
        if network and _https(issuer) and _https(jwks):
            try:
                discovery_url = (auth_secrets or {}).get('server_metadata_url', issuer.rstrip('/') + '/.well-known/openid-configuration')
                if not _https(discovery_url):
                    raise ValueError('OIDC discovery must use HTTPS without embedded credentials.')
                with urllib.request.urlopen(discovery_url, timeout=5) as response:
                    metadata = json.loads(response.read(1024 * 1024))
                with urllib.request.urlopen(jwks, timeout=5) as response:
                    keys = json.loads(response.read(1024 * 1024))
                valid = (metadata.get('issuer', '').rstrip('/') == issuer.rstrip('/')
                         and metadata.get('jwks_uri') == jwks and bool(keys.get('keys')))
                add('oidc.network', valid, True, 'discovery issuer/JWKS match; no token sent')
            except Exception as exc:
                add('oidc.network', False, True, 'discovery/JWKS failed: ' + type(exc).__name__)
        else:
            add('oidc.network', False, False, 'not contacted; run --network in the target network before acceptance')
    for name, path in (('data', data), ('managed', managed)):
        ancestor = _nearest(path)
        valid = ancestor.is_dir() and os.access(ancestor, os.R_OK | os.W_OK)
        add('path.' + name, valid, True, 'existing root/parent accessible' if valid else 'root/parent is not readable and writable')
        add('path.' + name + '.initialized', path.is_dir(), False, 'present' if path.is_dir() else 'not initialized; writes will create it')
    add('path.separation', data != managed and not data.is_relative_to(managed), True,
        'managed may be inside data but roots cannot coincide')
    if production:
        add('path.code_separation', data != PROJECT and managed != PROJECT, True,
            'production data must be outside the application code root')
    for name, path in (('data', data), ('managed', managed)):
        usage = shutil.disk_usage(_nearest(path))
        add('disk.free.' + name, usage.free >= min_free_mib * 1024 ** 2, True,
            f'{usage.free // 1024 ** 2} MiB free; minimum {min_free_mib} MiB (backup space is additional)')
    memory = _memory_available()
    add('memory.available', memory is not None and memory >= 1024 ** 3, False,
        ('unknown' if memory is None else f'{memory // 1024 ** 2} MiB available') + '; model capacity needs target-host measurement')
    for name, path in (('embedding', Path(MODEL_PATH)), ('reranker', Path(RERANKER_PATH))):
        valid = path.is_dir() and (path / 'config.json').is_file()
        weights = valid and any(path.glob('*.safetensors')) or valid and any(path.glob('*.bin'))
        add('model.' + name, bool(valid and weights), False,
            'local config/weights found; no model loaded' if valid and weights else 'missing/incomplete; semantic retrieval/reranking may degrade')
    offline = os.environ.get('HF_HUB_OFFLINE') == '1' and os.environ.get('TRANSFORMERS_OFFLINE') == '1'
    add('model.offline_policy', offline, False, 'automatic Hugging Face downloads disabled' if offline else 'set HF_HUB_OFFLINE=1 and TRANSFORMERS_OFFLINE=1 for offline delivery')
    add('data.present', managed.is_dir() and any(managed.glob('*.db')) or data.is_dir() and any(data.glob('*.csv')), False,
        'business input availability; empty installations require authorized import')
    add('maintenance', not (managed / MAINTENANCE_FILE).exists(), True, 'write gate open' if not (managed / MAINTENANCE_FILE).exists() else 'maintenance marker present; investigate before startup')
    add('restore.review', not (managed / RESTORE_REVIEW_FILE).exists(), False,
        'no restore review gate' if not (managed / RESTORE_REVIEW_FILE).exists() else 'all external dispatch blocked until receipt reconciliation')
    try:
        integrity = inspect_managed_integrity(managed, deep=deep_hash)
        add('data.integrity', not integrity['errors'], True, json.dumps(integrity, ensure_ascii=False))
    except (OperationsError, OSError) as exc:
        add('data.integrity', False, True, 'cannot inspect: ' + type(exc).__name__)
    add('data.deep_hash', deep_hash, False, 'registered blob/source/snapshot checks performed' if deep_hash else 'not requested; use --hash during delivery/recovery verification')
    manifest_path = os.environ.get('COST_MODEL_MANIFEST', '')
    if deep_hash and manifest_path:
        try:
            manifest_path = Path(manifest_path).resolve()
            inventory = json.loads(manifest_path.read_text(encoding='utf-8'))
            valid = isinstance(inventory.get('files'), list) and bool(inventory['files'])
            for entry in inventory.get('files', []):
                relative = Path(entry['path'])
                item = (manifest_path.parent / relative).resolve()
                valid = valid and not relative.is_absolute() and item.is_relative_to(manifest_path.parent)
                if not valid or not item.is_file() or sha256_file(item) != entry['sha256']:
                    valid = False
                    break
            add('model.hash', bool(valid), True, 'provided model manifest verified' if valid else 'model manifest mismatch')
        except (OSError, ValueError, KeyError, TypeError):
            add('model.hash', False, True, 'provided model manifest is invalid')
    else:
        add('model.hash', False, False, 'not verified; set COST_MODEL_MANIFEST and pass --hash')
    return {'ok': not any(check['status'] == 'fail' for check in checks), 'checks': checks,
            'metrics': operations_metrics(data_root=data, managed_root=managed)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path)
    parser.add_argument('--managed-root', type=Path)
    parser.add_argument('--bind-host')
    parser.add_argument('--production', action='store_true')
    parser.add_argument('--network', action='store_true')
    parser.add_argument('--hash', dest='deep_hash', action='store_true')
    parser.add_argument('--secrets-file', type=Path)
    parser.add_argument('--min-free-mib', type=int, default=1024)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    if args.min_free_mib < 0:
        parser.error('--min-free-mib must not be negative')
    try:
        result = collect_checks(**{key: value for key, value in vars(args).items() if key != 'json'})
    except Exception as exc:
        result = {'ok': False, 'checks': [{'name': 'preflight', 'required': True, 'status': 'fail',
                                         'detail': 'inspection failed: ' + type(exc).__name__}]}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for check in result['checks']:
            print(f"{check['status'].upper():4} {'MUST' if check['required'] else 'OPTIONAL':8} {check['name']}: {check['detail']}")
        print('READY (review warnings)' if result['ok'] else 'NOT READY (mandatory checks failed)')
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
