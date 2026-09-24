"""Static packaging and synthetic read-only preflight checks, never a Docker build."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIGS = ('config/domain_profiles/pharma.json', 'config/manufacturing_adapters/machinery.json',
           'config/manufacturing_adapters/auto_parts.json', 'config/manufacturing_adapters/chemicals.json',
           'config/manufacturing_adapters/electronics.json')
SCRIPTS = ('scripts/bootstrap_system.py', 'scripts/deep_review_evaluator_preflight.py',
           'scripts/validate_human_scores.py')


def load_tool():
    path = ROOT / 'scripts/deep_review_evaluator_preflight.py'
    spec = importlib.util.spec_from_file_location('synthetic_evaluator_preflight', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_docker_semantic_configuration_is_exactly_allowlisted_and_copied():
    from hashlib import sha256
    from scripts import build_source_bundle as builder

    ignore = (ROOT / '.dockerignore').read_text(encoding='utf-8').splitlines()
    dockerfile = (ROOT / 'Dockerfile').read_text(encoding='utf-8')
    # Independently bound the reviewed map to the five named nine-file bundles;
    # neither a filesystem glob nor .dockerignore defines what is approved.
    expected_fixtures = {f'config/manufacturing_examples/{industry}/{name}'
        for industry in ('pharma', 'machinery', 'auto_parts', 'chemicals', 'electronics')
        for name in ('domain.json', 'adapter.json', 'actual.csv', 'budget.csv',
                     'materials.csv', 'labor.csv', 'overhead.csv', 'knowledge.txt', 'manifest.json')}
    assert len(expected_fixtures) == 45
    assert set(builder.REVIEWED_SIMULATION_HASHES) == expected_fixtures
    selected = [line[1:] for line in ignore if line.startswith('!config/') and not line.endswith('/')]
    expected = set(CONFIGS) | expected_fixtures | {'config/manufacturing_examples/README.md'}
    assert set(selected) == expected and len(selected) == len(expected)
    assert not any('*' in line or '?' in line for line in selected)
    fixtures = {path: builder.read_regular(ROOT / path) for path in sorted(expected_fixtures)}
    for path, data in fixtures.items():
        assert sha256(data).hexdigest() == builder.REVIEWED_SIMULATION_HASHES[path]
        assert 'SIMULATION' in data.decode('utf-8')
        assert builder.scan_text(path, data)[0] == []
        if path.endswith('.csv'):
            assert ignore.index('!' + path) > ignore.index('**/*.csv')
    builder.validate_reviewed_simulations(fixtures)
    assert 'COPY config/manufacturing_examples/ /app/config/manufacturing_examples/' in dockerfile
    assert 'validate_reviewed_simulations(s)' in dockerfile and 'scan_text(n, b)' in dockerfile
    for path in CONFIGS:
        assert '!' + path in ignore
        assert path in dockerfile
        config = json.loads((ROOT / path).read_text(encoding='utf-8'))
        if 'manufacturing_adapters' in path:
            assert config['status'] == 'template'
            assert config['factories'] == [] and config['product_units'] == []
    assert 'COPY config/ ' not in dockerfile
    for path in SCRIPTS:
        assert '!' + path in ignore and (ROOT / path).is_file()
    assert 'COPY scripts/ /app/scripts/' in dockerfile
    for denied in ('**/.local/**', '**/.streamlit/secrets.toml', '**/.env', '**/*.db', '**/*.pdf', '**/*.docx', '**/*.csv', '**/*.safetensors', '**/*.bin'):
        assert denied in ignore
        assert ignore.index(denied) > max(ignore.index('!' + path) for path in CONFIGS + SCRIPTS)


def test_missing_model_and_sources_are_reported_not_manufactured(tmp_path, monkeypatch):
    tool = load_tool()
    def denied(*args, **kwargs):
        pytest.fail('Preflight must not use network')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    before_modules = set(sys.modules)
    args = SimpleNamespace(source_root=tmp_path / 'missing-packet', data_root=tmp_path / 'missing-data',
                           template=None, mock_script=None, model_dir=None, reranker_dir=None, font_file=None)
    result = tool.collect(args)
    assert result['read_only'] and result['business_ready'] is False
    assert result['network_calls'] == result['model_loads'] == result['database_opens'] == result['secret_files_read'] == 0
    assert result['authorized_source']['expected_csv'] == 10
    assert result['authorized_source']['expected_pdf'] == 7
    assert len(result['authorized_source']['files']) == 18
    assert not result['filesystem_ready'] and result['blockers']
    assert not tmp_path.joinpath('missing-data').exists()
    assert 'torch' not in (set(sys.modules) - before_modules)
    assert 'transformers' not in (set(sys.modules) - before_modules)
    assert not result['docker']['build_executed']


def test_synthetic_model_presence_not_load_or_license_proof(tmp_path):
    tool = load_tool()
    for name in ('config.json', 'tokenizer_config.json', 'tokenizer.json'):
        (tmp_path / name).write_text('{}', encoding='utf-8')
    (tmp_path / 'model.safetensors').write_bytes(b'SYNTHETIC_NOT_REAL_WEIGHTS')
    result = tool.model_checks(tmp_path, True)
    assert result['filesystem_ready']
    assert result['weight_file_count'] == 1
    assert result['weights_loaded'] is False and result['license_verified'] is False


def test_weight_index_cannot_escape_model_directory(tmp_path):
    tool = load_tool()
    for name in ('config.json', 'tokenizer_config.json', 'tokenizer.json'):
        (tmp_path / name).write_text('{}', encoding='utf-8')
    (tmp_path / 'model.safetensors.index.json').write_text(json.dumps({'weight_map': {'a': '../outside.bin'}}), encoding='utf-8')
    assert not tool.model_checks(tmp_path, True)['filesystem_ready']
