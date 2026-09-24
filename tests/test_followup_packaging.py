"""Portable source-package contracts; no ZIPs, delivery outputs or original data.

Only literal public source inputs are read. Mutation probes use pytest tmp_path;
config review pins reject customer-specific replacements, even at allowed paths.
"""
import importlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import sys
import textwrap
from types import SimpleNamespace

import pytest

from enterprise import build_info
from scripts import build_source_bundle as builder

ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    'enterprise.domain_profiles', 'enterprise.tabular_knowledge',
    'enterprise.evidence_references', 'enterprise.industry_benchmark',
    'enterprise.reference_service', 'enterprise.manufacturing_adapter',
    'app_pages.reference_view',
)
CONFIGS = (
    'config/domain_profiles/pharma.json',
    'config/manufacturing_adapters/machinery.json',
    'config/manufacturing_adapters/auto_parts.json',
    'config/manufacturing_adapters/chemicals.json',
    'config/manufacturing_adapters/electronics.json',
)
FOLLOWUP_TESTS = (
    'tests/test_benchmark_years_followup.py', 'tests/test_forecast_followup.py',
    'tests/test_model_task_routing.py', 'tests/test_model_caller_followup.py',
    'tests/test_knowledge_categories.py', 'tests/test_knowledge_browser_followup.py',
    'tests/test_manufacturing_adapter.py', 'tests/test_followup_packaging.py',
)
PRIVATE_ROOTS = {'.local', '.runtime', '.artifacts', 'artifacts', 'data_upload', 'delivery',
                 'managed', 'kb', 'logs', 'outputs', 'backups', 'models', '.venv', '__pycache__'}


@pytest.fixture(scope='module')
def public_sources():
    # Never enumerate the workspace or build an archive, even temporarily.
    return {name: builder.read_regular(ROOT / name) for name in builder.allowed_paths()}


def test_literal_allowlist_includes_followups_and_excludes_private_roots():
    allowed = set(builder.allowed_paths())
    assert {name.replace('.', '/') + '.py' for name in MODULES} <= allowed
    assert set(CONFIGS) | set(builder.REVIEWED_SIMULATION_HASHES) | {
        'config/manufacturing_examples/README.md'} == {name for name in allowed if name.startswith('config/')}
    assert set(CONFIGS) == set(builder.REVIEWED_CONFIG_HASHES)
    assert set(CONFIGS) <= set(build_info.DEPLOYMENT_FILES)
    assert {'docs/任务级模型配置.md', 'docs/跨行业迁移与边界.md'} <= allowed
    assert set(FOLLOWUP_TESTS) <= set(builder.portable_test_paths()) <= allowed
    for name in allowed:
        assert builder.safe_name(name)
        assert not (set(PurePosixPath(name).parts) & PRIVATE_ROOTS), name
        suffix = PurePosixPath(name).suffix.lower()
        assert suffix not in {'.xlsx', '.pdf', '.docx', '.db', '.sqlite'}
        if suffix == '.csv':
            assert name in builder.REVIEWED_SIMULATION_HASHES
    assert not allowed & {'.env', '.streamlit/secrets.toml', 'deploy/authorization.json',
        'config/domain_profiles/customer.json', 'config/manufacturing_adapters/customer.json',
        'tests/test_tabular_knowledge.py', 'tests/test_industry_followup.py',
        'tests/test_report_references_followup.py'}


def test_public_sources_pass_existing_scan_and_import_closure(public_sources):
    failures = [failure for name, data in public_sources.items()
                for failure in builder.scan_text(name, data)[0]]
    assert failures == []
    builder.validate_examples(public_sources)
    builder.validate_reviewed_configs(public_sources)
    analysis = builder.source_analysis(public_sources)
    assert analysis['dynamic_ui_entrypoints'] == 'present'
    assert analysis['optional_unbundled_imports'] == [
        item for item in analysis['optional_unbundled_imports']
        if item['path'] == 'attribution_gen.py' and item['module'] == 'kb_search']


@pytest.mark.parametrize('module', MODULES)
def test_followup_module_is_importable_and_required_for_closure(public_sources, module):
    name = module.replace('.', '/') + '.py'
    loaded = importlib.import_module(module)
    assert Path(loaded.__file__).resolve() == (ROOT / name).resolve()
    incomplete = dict(public_sources)
    incomplete.pop(name)
    with pytest.raises(ValueError, match='Unresolved local source dependencies'):
        builder.source_analysis(incomplete)


def test_reviewed_configs_are_nonsecret_semantics_and_inactive_templates(public_sources):
    from enterprise.domain_profiles import validate_domain_profile
    from enterprise.manufacturing_adapter import parse_adapter_config
    profile = validate_domain_profile(json.loads(public_sources[CONFIGS[0]]))
    assert profile['id'] == 'pharma'
    for name in CONFIGS[1:]:
        value = parse_adapter_config(public_sources[name].decode('utf-8')).to_dict()
        assert value['status'] == 'template'
        assert value['factories'] == value['product_units'] == []
    # Formatting changes across checkouts do not count as semantic alterations.
    reformatted = {name: json.dumps(json.loads(public_sources[name]), ensure_ascii=True).encode()
                   for name in CONFIGS}
    builder.validate_reviewed_configs(reformatted)


@pytest.mark.parametrize('name', CONFIGS)
def test_customer_specific_config_replacement_refused(public_sources, name):
    changed = dict(public_sources)
    value = json.loads(changed[name])
    value['label'] = 'SYNTHETIC_UNREVIEWED_CUSTOMER_CONFIGURATION'
    if name != CONFIGS[0]:
        value['status'] = 'active'
        value['factories'] = ['SYNTHETIC_CUSTOMER_FACTORY']
    changed[name] = builder.canonical(value)
    with pytest.raises(ValueError, match='distribution review required'):
        builder.validate_reviewed_configs(changed)


@pytest.mark.parametrize('raw', [b'{"id":1,"id":2}', b'{"id":NaN}'])
def test_ambiguous_reviewed_json_is_rejected(public_sources, raw):
    changed = dict(public_sources)
    changed[CONFIGS[0]] = raw
    with pytest.raises(ValueError):
        builder.validate_reviewed_configs(changed)


@pytest.mark.parametrize('name', CONFIGS)
def test_reviewed_config_changes_only_deployment_identity(tmp_path, monkeypatch, name):
    monkeypatch.setattr(build_info, 'BASE_DIR', tmp_path)
    source = build_info.source_fingerprint()
    missing = build_info.deployment_fingerprint()
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'synthetic-first')
    first = build_info.deployment_fingerprint()
    path.write_bytes(b'synthetic-second')
    assert len({missing, first, build_info.deployment_fingerprint()}) == 3
    assert build_info.source_fingerprint() == source


def test_private_profiles_and_environment_do_not_enter_deployment_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(build_info, 'BASE_DIR', tmp_path)
    initial = build_info.deployment_fingerprint()
    for name in ('config/domain_profiles/customer.json', 'config/manufacturing_adapters/live.json',
                 '.local/llm.json', 'managed/config.json', '.env', '.streamlit/secrets.toml'):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('SYNTHETIC PRIVATE SENTINEL', encoding='utf-8')
    monkeypatch.setenv('COST_DOMAIN_PROFILE', str(tmp_path / 'config/domain_profiles/customer.json'))
    assert build_info.deployment_fingerprint() == initial


def test_ci_runs_exact_reviewed_list_without_shell_splitting(tmp_path, monkeypatch):
    workflow = (ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8')
    script = textwrap.dedent(workflow.split("python - <<'PY'\n", 1)[1].split('\n          PY', 1)[0])
    root = tmp_path / 'checkout with spaces'
    for name in builder.portable_test_paths():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('# synthetic CI path probe\n', encoding='utf-8')
    observed = []
    monkeypatch.setenv('GITHUB_WORKSPACE', str(root))
    monkeypatch.setitem(sys.modules, 'pytest', SimpleNamespace(main=lambda args: observed.append(args) or 7))
    with pytest.raises(SystemExit) as result:
        exec(compile(script, '.github/workflows/ci.yml', 'exec'), {})
    assert result.value.code == 7  # CI must propagate pytest's exit status.
    assert observed == [['-q', '--strict-config', '--strict-markers',
                         '--rootdir', str(root.resolve()), '--confcutdir', str(root.resolve()),
                         *[str(root.resolve() / name) for name in builder.portable_test_paths()]]]
    observed.clear()
    (root / builder.portable_test_paths()[0]).unlink()
    with pytest.raises(SystemExit, match='Missing reviewed portable tests'):
        exec(compile(script, '.github/workflows/ci.yml', 'exec'), {})
    assert observed == []


@pytest.mark.parametrize('filename,function,needs_monkeypatch', [
    ('test_forecast_baseline.py', 'test_real_legacy_loader_cost_summaries_are_read_only_and_gap_aware', True),
    ('test_forecast_followup.py', 'test_real_csv_budget_only_matches_january_to_june_2026', False),
])
def test_original_csv_probes_retain_existing_clean_skip(tmp_path, monkeypatch, filename, function, needs_monkeypatch):
    # Exercise the tests' own existing skip, not a CI deselection or a new skip.
    spec = importlib.util.spec_from_file_location('source_only_forecast_probe', ROOT / 'tests' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    with pytest.raises(pytest.skip.Exception, match='CSV'):
        getattr(module, function)(*([monkeypatch] if needs_monkeypatch else []))


def test_existing_builder_self_check_remains_read_only(capsys):
    builder.self_check()
    assert json.loads(capsys.readouterr().out)['self_check'] == 'passed'
