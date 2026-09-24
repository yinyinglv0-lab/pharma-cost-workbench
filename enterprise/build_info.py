"""Source identity for managed launcher reuse; excludes data, secrets and tests."""
from hashlib import sha256
from paths import BASE_DIR


def source_fingerprint():
    paths = list(BASE_DIR.glob('*.py'))
    for directory in ('enterprise', 'dashboard', 'app_pages', 'report'):
        paths.extend((BASE_DIR / directory).rglob('*.py'))
    digest = sha256()
    for path in sorted(paths):
        if path.name.startswith('test_') or '__pycache__' in path.parts:
            continue
        digest.update(path.relative_to(BASE_DIR).as_posix().encode('utf-8'))
        digest.update(b'\0')
        digest.update(path.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()


# Explicitly non-secret deployment inputs. Installed wheels, environment values,
# fonts/model weights and business state have separate operational inventories.
DEPLOYMENT_FILES = (
    'requirements.txt', 'requirements-models.txt', 'requirements-legacy.txt',
    'requirements-core-win-py312.lock.txt', 'Dockerfile', 'compose.yaml', '.dockerignore',
    '.streamlit/config.toml', 'deploy/streamlit.config.toml', 'deploy/compose.models.yaml',
    'deploy/compose.restore.example.yaml', 'deploy/nginx.conf.example',
    'assets/echarts.min.js', 'assets/workbench-brand.png',
    # Reviewed public semantics / inactive starter schemas, never a config glob
    # or an environment-selected customer profile. Source-only contract is unchanged.
    'config/domain_profiles/pharma.json',
    'config/manufacturing_adapters/machinery.json', 'config/manufacturing_adapters/auto_parts.json',
    'config/manufacturing_adapters/chemicals.json', 'config/manufacturing_adapters/electronics.json',
    'README.md', 'docs/跨行业迁移与边界.md', 'docs/第二轮核查实施与运行说明.md',
    'docs/受控散文生成与阅读导出.md',
    'config/manufacturing_examples/README.md',
    # Exact public SIMULATION paths only; never glob customer CSV/configuration.
    'config/manufacturing_examples/pharma/domain.json',
    'config/manufacturing_examples/pharma/adapter.json',
    'config/manufacturing_examples/pharma/actual.csv',
    'config/manufacturing_examples/pharma/budget.csv',
    'config/manufacturing_examples/pharma/materials.csv',
    'config/manufacturing_examples/pharma/labor.csv',
    'config/manufacturing_examples/pharma/overhead.csv',
    'config/manufacturing_examples/pharma/knowledge.txt',
    'config/manufacturing_examples/pharma/manifest.json',
    'config/manufacturing_examples/machinery/domain.json',
    'config/manufacturing_examples/machinery/adapter.json',
    'config/manufacturing_examples/machinery/actual.csv',
    'config/manufacturing_examples/machinery/budget.csv',
    'config/manufacturing_examples/machinery/materials.csv',
    'config/manufacturing_examples/machinery/labor.csv',
    'config/manufacturing_examples/machinery/overhead.csv',
    'config/manufacturing_examples/machinery/knowledge.txt',
    'config/manufacturing_examples/machinery/manifest.json',
    'config/manufacturing_examples/auto_parts/domain.json',
    'config/manufacturing_examples/auto_parts/adapter.json',
    'config/manufacturing_examples/auto_parts/actual.csv',
    'config/manufacturing_examples/auto_parts/budget.csv',
    'config/manufacturing_examples/auto_parts/materials.csv',
    'config/manufacturing_examples/auto_parts/labor.csv',
    'config/manufacturing_examples/auto_parts/overhead.csv',
    'config/manufacturing_examples/auto_parts/knowledge.txt',
    'config/manufacturing_examples/auto_parts/manifest.json',
    'config/manufacturing_examples/chemicals/domain.json',
    'config/manufacturing_examples/chemicals/adapter.json',
    'config/manufacturing_examples/chemicals/actual.csv',
    'config/manufacturing_examples/chemicals/budget.csv',
    'config/manufacturing_examples/chemicals/materials.csv',
    'config/manufacturing_examples/chemicals/labor.csv',
    'config/manufacturing_examples/chemicals/overhead.csv',
    'config/manufacturing_examples/chemicals/knowledge.txt',
    'config/manufacturing_examples/chemicals/manifest.json',
    'config/manufacturing_examples/electronics/domain.json',
    'config/manufacturing_examples/electronics/adapter.json',
    'config/manufacturing_examples/electronics/actual.csv',
    'config/manufacturing_examples/electronics/budget.csv',
    'config/manufacturing_examples/electronics/materials.csv',
    'config/manufacturing_examples/electronics/labor.csv',
    'config/manufacturing_examples/electronics/overhead.csv',
    'config/manufacturing_examples/electronics/knowledge.txt',
    'config/manufacturing_examples/electronics/manifest.json',
)
DEPLOYMENT_SCRIPTS = (
    'scripts/backup_restore.py', 'scripts/bootstrap_system.py', 'scripts/preflight.py',
    'scripts/run_task_worker.py', 'scripts/freeze_core_wheels.py', 'scripts/verify_restored_system.py',
    'scripts/build_source_bundle.py', 'scripts/validate_human_scores.py',
    'scripts/deep_review_evaluator_preflight.py',
    'deploy/entrypoint.py', 'deploy/healthcheck.py', 'deploy/generate_inventory.py',
    'deploy/container_smoke.py', 'deploy/install_cjk_font.py',
)


def deployment_fingerprint():
    """Deployment-file identity, including missing inputs, never secrets or state.

    This is not a container image digest or proof of installed dependency versions.
    The original source_fingerprint retains its application-Python-only contract.
    """
    digest = sha256(b'deployment-files/1.0\0')
    digest.update(source_fingerprint().encode('ascii'))
    for relative in sorted(DEPLOYMENT_FILES + DEPLOYMENT_SCRIPTS):
        path = BASE_DIR / relative
        digest.update(relative.encode('utf-8') + b'\0')
        if path.is_symlink() or not path.resolve().is_relative_to(BASE_DIR.resolve()):
            raise ValueError('Deployment identity refuses paths outside its source root')
        digest.update(path.read_bytes() if path.is_file() else b'\0missing-file\0')
        digest.update(b'\0')
    return digest.hexdigest()
