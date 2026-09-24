"""Portable packaging checks: reviewed public SIMULATION bytes, never real data.

Build/verify only in memory or pytest temporary directories. No delivery artifact,
local database, original CSV, private configuration, model or network is needed.
"""
import ast
from copy import deepcopy
import importlib
import io
import json
from pathlib import Path, PurePosixPath
import zipfile

import pytest

from enterprise import build_info
from scripts import build_source_bundle as builder

ROOT = Path(__file__).resolve().parents[1]
INDUSTRIES = ('pharma', 'machinery', 'auto_parts', 'chemicals', 'electronics')
FIXTURE_NAMES = ('domain.json', 'adapter.json', 'actual.csv', 'budget.csv', 'materials.csv',
                 'labor.csv', 'overhead.csv', 'knowledge.txt', 'manifest.json')
MODULES = (
    'enterprise.domain_profiles', 'enterprise.manufacturing_adapter',
    'enterprise.manufacturing_runtime', 'enterprise.manufacturing_repository',
    'enterprise.manufacturing_service', 'enterprise.manufacturing_projection',
    'enterprise.manufacturing_api', 'enterprise.domain_vocabulary',
    'enterprise.analysis_contract', 'enterprise.analysis_narrative',
    'enterprise.tabular_knowledge', 'enterprise.evidence_references',
    'enterprise.industry_benchmark', 'report.narrative_adapter',
    'enterprise.prose_contract', 'enterprise.prose_validation', 'enterprise.manufacturing_report',
)
ROUND2_TESTS = (
    'tests/test_round2_manufacturing_runtime.py', 'tests/test_round2_manufacturing_projection.py',
    'tests/test_round2_manufacturing_repository_service.py', 'tests/test_round2_manufacturing_api_ui.py',
    'tests/test_round2_generic_references.py', 'tests/test_round2_domain_graph.py',
    'tests/test_round2_model_contract.py', 'tests/test_round2_narrative.py',
    'tests/test_round2_ui.py', 'tests/test_round2_packaging.py', 'tests/test_round2_parent_integration.py',
    'tests/test_round2_prose_contract.py', 'tests/test_round2_prose_integration.py',
    'tests/test_round2_prose_ui.py', 'tests/test_round2_manufacturing_report.py',
)
PUBLIC_DOCS = ('README.md', 'docs/跨行业迁移与边界.md', 'docs/第二轮核查实施与运行说明.md',
               'docs/受控散文生成与阅读导出.md', 'config/manufacturing_examples/README.md')


@pytest.fixture(scope='module')
def public_sources():
    return {name: builder.read_regular(ROOT / name) for name in builder.allowed_paths()}


def _external(snapshot, manifest):
    data, embedded = builder.make_zip(snapshot, manifest)
    external = {**deepcopy(manifest), 'generated_utc': '2026-09-23T00:00:00+00:00',
                'archive_path': 'delivery/temporary-review/project4-source.zip',
                'archive_bytes': len(data), 'archive_sha256': builder.digest(data),
                'embedded_manifest_sha256': builder.digest(embedded)}
    return data, external


def test_production_and_portable_lists_are_literal_reviewable_and_complete(public_sources):
    tree = ast.parse(public_sources['scripts/build_source_bundle.py'].decode('utf-8-sig'))
    assignments = {node.targets[0].id: node.value for node in tree.body
                   if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)}
    assert ast.literal_eval(assignments['CORE_TREES']) == builder.CORE_TREES
    assert ast.literal_eval(assignments['TESTS']) == builder.TESTS
    assert ast.literal_eval(assignments['REVIEWED_SIMULATION_HASHES']) == builder.REVIEWED_SIMULATION_HASHES
    expected = {f'config/manufacturing_examples/{industry}/{name}'
                for industry in INDUSTRIES for name in FIXTURE_NAMES}
    assert len(expected) == 45
    assert set(builder.REVIEWED_SIMULATION_HASHES) == expected
    allowed = set(builder.allowed_paths())
    assert expected | set(PUBLIC_DOCS) <= allowed
    assert {module.replace('.', '/') + '.py' for module in MODULES} <= allowed
    assert 'app_pages/manufacturing.py' in allowed
    assert 'enterprise/manufacturing_application.py' not in allowed
    assert set(ROUND2_TESTS) <= set(builder.portable_test_paths())
    assert not allowed & {'tests/test_round2_report_narrative.py', 'tests/test_round2_report_prose.py',
                          'tests/test_round2_prose_stability.py',
                          'docs/散文提示词规范核查与采用说明.md',
                          'scripts/round2_manufacturing_acceptance.py',
                          'scripts/round2_browser_ui.py', 'scripts/round2_regression.py',
                          'tests/test_tabular_knowledge.py', 'tests/test_industry_followup.py',
                          'tests/test_report_references_followup.py'}
    assert {name for name in allowed if name.endswith('.csv')} == {
        name for name in expected if name.endswith('.csv')}
    assert not any('*' in name or '?' in name for name in allowed)
    for name in allowed:
        assert builder.safe_name(name)
        assert not set(PurePosixPath(name).parts) & {
            '.local', 'managed', '.runtime', 'artifacts', 'delivery', 'models', 'kb', '.venv', 'backups'}


@pytest.mark.parametrize('module', MODULES)
def test_required_module_imports_and_missing_source_fails_closure(public_sources, module):
    loaded = importlib.import_module(module)
    name = module.replace('.', '/') + '.py'
    assert Path(loaded.__file__).resolve() == (ROOT / name).resolve()
    incomplete = dict(public_sources)
    del incomplete[name]
    with pytest.raises(ValueError, match='Unresolved local source dependencies'):
        builder.source_analysis(incomplete)


def test_builder_uses_only_stdlib_and_never_discovers_or_reads_excluded_files(monkeypatch):
    reads = []
    original = builder.read_regular
    allowed = set(builder.allowed_paths())

    def checked_read(path, **kwargs):
        name = path.relative_to(ROOT).as_posix()
        assert name in allowed, 'unexpected source read: ' + name
        reads.append(name)
        return original(path, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail('Source builder must not enumerate directories')

    monkeypatch.setattr(builder, 'read_regular', checked_read)
    monkeypatch.setattr(Path, 'glob', forbidden)
    monkeypatch.setattr(Path, 'rglob', forbidden)
    monkeypatch.setattr(Path, 'iterdir', forbidden)
    snapshot, manifest = builder.snapshot_sources()
    assert set(reads) == allowed and len(reads) == len(allowed)
    assert set(snapshot) == allowed
    assert manifest['scan']['python']['dynamic_ui_entrypoints'] == 'present'
    review = manifest['content_review']['public_simulation_examples']
    assert review['classification'] == 'SIMULATION' and review['real_dataset'] is False
    assert {row['path']: row['sha256'] for row in review['files']} == builder.REVIEWED_SIMULATION_HASHES
    assert all(row['classification'] == 'SIMULATION' for row in review['files'])
    data, external = _external(snapshot, manifest)
    assert builder.verify_bytes(data, external)['status'] == 'verified'
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert set(archive.namelist()) == allowed | {builder.EMBEDDED_NAME}
        for name in builder.REVIEWED_SIMULATION_HASHES:
            assert archive.read(name) == snapshot[name]
    # Parsing source does not import application/runtime/configuration readers.
    parsed = ast.parse(snapshot['scripts/build_source_bundle.py'].decode('utf-8-sig'))
    roots = {node.module.split('.')[0] for node in ast.walk(parsed)
             if isinstance(node, ast.ImportFrom) and node.module}
    roots |= {alias.name.split('.')[0] for node in ast.walk(parsed)
              if isinstance(node, ast.Import) for alias in node.names}
    import sys
    assert roots <= sys.stdlib_module_names


@pytest.mark.parametrize('name', tuple(builder.REVIEWED_SIMULATION_HASHES))
def test_each_simulation_file_is_frozen_raw_and_text_scanned(public_sources, name):
    data = public_sources[name]
    assert builder.digest(data) == builder.REVIEWED_SIMULATION_HASHES[name]
    assert 'SIMULATION' in data.decode('utf-8')
    assert builder.scan_text(name, data)[0] == []
    changed = dict(public_sources)
    changed[name] = data + b'\n'  # Even schema-neutral whitespace is a new review.
    assert any(item['rule'] == 'reviewed_simulation_hash'
               for item in builder.scan_text(name, changed[name])[0])
    with pytest.raises(ValueError, match='distribution review required'):
        builder.validate_reviewed_simulations(changed)


@pytest.mark.parametrize('industry', INDUSTRIES)
def test_schema_valid_customer_substitution_is_not_publicly_reviewed(public_sources, industry):
    from enterprise.domain_profiles import validate_domain_profile
    name = f'config/manufacturing_examples/{industry}/domain.json'
    value = json.loads(public_sources[name])
    value['label'] = 'SIMULATION altered but schema-valid customer label'
    validate_domain_profile(value)
    changed = dict(public_sources)
    changed[name] = builder.canonical(value)
    with pytest.raises(ValueError, match='distribution review required'):
        builder.validate_reviewed_simulations(changed)


@pytest.mark.parametrize('raw', [b'{"id":1,"id":2}', b'{"id":NaN}', b'{"schema_version":"manufacturing-domain/1"}'])
def test_raw_schema_changes_cannot_replace_reviewed_simulation(public_sources, raw):
    changed = dict(public_sources)
    changed['config/manufacturing_examples/pharma/domain.json'] = raw
    with pytest.raises(ValueError, match='distribution review required'):
        builder.validate_reviewed_simulations(changed)


def test_fixture_hash_review_never_bypasses_existing_secret_or_data_scan(monkeypatch):
    name = 'config/manufacturing_examples/pharma/knowledge.txt'
    specimens = [
        (b'SIMULATION\nsk-' + b'a' * 30, 'provider_key'),
        (b'SIMULATION\n' + b'-' * 5 + b'BEGIN PRIVATE KEY' + b'-' * 5, 'private_key'),
        (b'SIMULATION\napi' + b'_key = "unreviewed-sensitive-value"', 'secret_assignment_literal'),
        (('SIMULATION\n' + '工厂,产品名称,产品规格,月份,产量(盒),' + '直接材料').encode(), 'complete_cost_csv_header'),
        (b'SIMULATION\nC:/Users/' + b'synthetic-owner/private.txt', 'personal_windows_path'),
    ]
    for raw, rule in specimens:
        # Stronger than ordinary mutation rejection: even an exact hash approval
        # must not turn into a broad scan exemption for a fixture path.
        monkeypatch.setitem(builder.REVIEWED_SIMULATION_HASHES, name, builder.digest(raw))
        blockers, _ = builder.scan_text(name, raw)
        assert rule in {item['rule'] for item in blockers}
    builder.self_check()


def test_review_metadata_cannot_be_changed_in_otherwise_valid_archive():
    snapshot, manifest = builder.snapshot_sources()
    manifest['content_review']['public_simulation_examples']['real_dataset'] = True
    data, external = _external(snapshot, manifest)
    with pytest.raises(ValueError, match='SIMULATION review metadata mismatch'):
        builder.verify_bytes(data, external)


@pytest.mark.parametrize('name', tuple(builder.REVIEWED_SIMULATION_HASHES) + PUBLIC_DOCS)
def test_explicit_public_inputs_change_deployment_not_source_identity(tmp_path, monkeypatch, name):
    assert name in build_info.DEPLOYMENT_FILES
    monkeypatch.setattr(build_info, 'BASE_DIR', tmp_path)
    source = build_info.source_fingerprint()
    missing = build_info.deployment_fingerprint()
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'SIMULATION temporary public identity probe')
    first = build_info.deployment_fingerprint()
    path.write_bytes(b'SIMULATION changed public identity probe')
    assert len({missing, first, build_info.deployment_fingerprint()}) == 3
    assert build_info.source_fingerprint() == source


def test_unreviewed_similar_files_never_enter_allowlist_or_deployment(tmp_path, monkeypatch):
    names = ('config/manufacturing_examples/pharma/customer.csv',
             'config/manufacturing_examples/private/domain.json',
             'config/manufacturing_examples/pharma/secret.json',
             'config/manufacturing_examples/pharma/live.db',
             '.local/llm.json', '.streamlit/secrets.toml', 'managed/live.db', 'real-cost.csv')
    monkeypatch.setattr(build_info, 'BASE_DIR', tmp_path)
    before = build_info.deployment_fingerprint()
    allowed = builder.allowed_paths()
    for name in names:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'SYNTHETIC forbidden-path sentinel')
        assert name not in allowed
    assert build_info.deployment_fingerprint() == before
    assert builder.allowed_paths() == allowed


def test_docker_only_reopens_exact_reviewed_simulation_paths(public_sources):
    ignore = public_sources['.dockerignore'].decode('utf-8')
    lines = [line.strip() for line in ignore.splitlines()]
    entries = {line[1:] for line in lines if line.startswith('!config/manufacturing_examples/')
               and not line.endswith('/')}
    assert entries == set(builder.REVIEWED_SIMULATION_HASHES) | {'config/manufacturing_examples/README.md'}
    assert not any('*' in name or '?' in name for name in entries)
    for name in builder.REVIEWED_SIMULATION_HASHES:
        if name.endswith('.csv'):
            assert lines.index('!' + name) > lines.index('**/*.csv')
    for pattern in ('**/.local/**', '**/*.db', '**/*.sqlite*', '**/*secret*',
                    '**/*.key', '**/*.pem', '**/*.safetensors', '**/*.bin', '**/*.onnx'):
        assert pattern in lines
    docker = public_sources['Dockerfile'].decode('utf-8')
    assert 'COPY config/manufacturing_examples/ /app/config/manufacturing_examples/' in docker
    assert 'COPY README.md /app/README.md' in docker
    assert 'validate_reviewed_simulations(s)' in docker and 'scan_text(n, b)' in docker
    assert not any(line.startswith('COPY ') and ('/models/' in line or '.local/' in line)
                   for line in docker.splitlines())


@pytest.mark.parametrize('output_dir', ['delivery', 'delivery/new-review'])
def test_existing_bundle_destination_never_overwritten(tmp_path, monkeypatch, capsys, output_dir):
    monkeypatch.setattr(builder, 'ROOT', tmp_path)
    destination = tmp_path / output_dir
    destination.mkdir(parents=True)
    old = destination / builder.BUNDLE_NAME
    old.write_bytes(b'previous immutable package')
    monkeypatch.setattr(builder, 'snapshot_sources', lambda: pytest.fail('Must refuse before collecting sources'))
    assert builder.main(['--output-dir', output_dir]) == 1
    assert 'already contains an output' in capsys.readouterr().err
    assert old.read_bytes() == b'previous immutable package'
    assert not (destination / builder.MANIFEST_NAME).exists()


def test_atomic_publish_is_no_clobber_even_without_cli_guard(tmp_path):
    target = tmp_path / 'source.zip'
    builder.atomic_write(target, b'first immutable package')
    with pytest.raises(FileExistsError):
        builder.atomic_write(target, b'forbidden replacement')
    assert target.read_bytes() == b'first immutable package'
    assert target.stat().st_nlink == 1
    assert list(tmp_path.iterdir()) == [target]
