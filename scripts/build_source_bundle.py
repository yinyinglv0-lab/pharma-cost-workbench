"""Build a local, reviewable source-only ZIP. Never publish or import the app.

The allowlist is literal and reviewable. This program never enumerates the source
root, reads environment variables, loads .local, imports project modules, or opens
competition data. Network and secret-manager clients are deliberately absent.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import tomllib
import zipfile

ROOT = Path(__file__).absolute().parents[1]
SCHEMA = 'project4-source-bundle/1.0'
PUBLICATION_STATUS = 'local_review_bundle'
AUTHORIZATION_STATUS = 'user_authorized_source_only'
PUBLICATION_TARGET = 'https://github.com/yinyinglv0-lab/pharma-cost-workbench'
BUNDLE_NAME = 'project4-source.zip'
MANIFEST_NAME = 'source_bundle_manifest.json'
EMBEDDED_NAME = 'SOURCE_MANIFEST.json'
MAX_FILE = 2 * 1024 * 1024
MAX_TOTAL = 16 * 1024 * 1024
STAMP = (1980, 1, 1, 0, 0, 0)

CORE_ROOT = (
    'enterprise_app.py', 'admin_web.py', 'backend_api.py', 'launch_system.py', 'paths.py',
    'dashboard_web.py', 'report_web.py', 'attribution_gen.py',
    'attribution_facts.py', 'attribution_decomposition.py', 'attribution_narrative.py',
    'attribution_runtime.py', 'attribution_worker.py',
)
CORE_TREES = {
    'enterprise': ('__init__', 'analysis_service', 'analysis_context', 'application', 'benchmark', 'benchmark_ai',
        'bootstrap', 'build_info', 'cost_imports', 'forecast', 'agent_router', 'knowledge', 'knowledge_context', 'knowledge_release', 'knowledge_langchain', 'knowledge_applicability',
        'model_gateway', 'model_registry', 'operations', 'periods', 'report_records', 'report_service', 'rpa_client',
        'security', 'snapshots', 'task_worker', 'task_workflow', 'task_closure', 'task_plan',
        'causal_guard', 'numeric', 'citations', 'evidence_freeze', 'domain_graph', 'domain_keywords', 'knowledge_baseline', 'knowledge_runtime',
        'domain_profiles', 'tabular_knowledge', 'evidence_references', 'industry_benchmark',
        'reference_service', 'manufacturing_adapter', 'manufacturing_runtime',
        'manufacturing_repository', 'manufacturing_service', 'manufacturing_projection',
        'manufacturing_api', 'domain_vocabulary', 'analysis_contract', 'analysis_narrative',
        'prose_contract', 'prose_validation', 'manufacturing_report',
        'neural_rerank', 'crosscheck', 'anomaly_case', 'document_classifier', 'multimodal', 'vision_enhancement', 'local_extractor', 'model_settings'),
    'app_pages': ('_shared', 'design', 'benchmark', 'data', 'history', 'home', 'knowledge', 'settings', 'tasks', 'forecast', 'agent', 'citations', 'component_registry', 'reference_view', 'manufacturing', 'multimodal', 'model_config'),
    'dashboard': ('__init__', 'charts', 'data_layer', 'echarts_helper', 'validate', 'chart_link'),
    'report': ('__init__', 'datafill', 'export', 'model', 'registry', 'render', 'tables', 'claims', 'narrative', 'peer_analysis', 'structure', 'presentation', 'actions', 'narrative_adapter'),
    'scripts': ('backup_restore', 'bootstrap_system', 'preflight', 'run_task_worker',
        'freeze_core_wheels', 'verify_restored_system', 'validate_human_scores', 'build_source_bundle',
         'deep_review_evaluator_preflight', 'stability_runner'),
    'deploy': ('entrypoint', 'generate_inventory', 'healthcheck', 'container_smoke', 'install_cjk_font'),
    'rag_fixed_v1': ('__init__',),
}
DOCUMENTS = (
    'README.md', 'THIRD_PARTY_NOTICES.md', 'docs/技术方案与接口.md',
    'docs/Prompt与模型评测协议.md', 'docs/用户操作手册.md',
    'docs/数据字典与交付边界.md', 'docs/部署与运维手册.md', 'docs/模块一V2与分析辅助增量说明.md',
    'docs/企业打磨V3技术与运维增补.md', 'docs/28项整改与验收指南.md',
    'docs/任务级模型配置.md', 'docs/跨行业迁移与边界.md',
    'docs/第三机评委部署与验收.md', 'docs/各模块技术与能力边界.md',
    'docs/第二轮核查实施与运行说明.md', 'docs/受控散文生成与阅读导出.md',
    'config/manufacturing_examples/README.md',
)
# Public declarative semantics / inactive schema templates, not deployed business
# configuration. Pin canonical JSON content (not line endings) so editing these
# paths for a real customer cannot silently publish their identities or values.
# Any semantic change requires a new explicit source-distribution review.
REVIEWED_CONFIG_HASHES = {
    'config/domain_profiles/pharma.json': '27e2c4e342c1bbb85a6e27eb3e8d0265d474a80f5851ea7532700aa204095b9a',
    'config/manufacturing_adapters/machinery.json': '78e97da2d9092133ac9530e18f7c0849087ffc529416696eaf25d98985680640',
    'config/manufacturing_adapters/auto_parts.json': 'e27c75f4a303bc3437f59dc753d9fac4da32899b85f3db04628767771a5b7ee2',
    'config/manufacturing_adapters/chemicals.json': '1db9721142b26d23334384984c5a0909c4440d2b95be758e1a4dae50849042cc',
    'config/manufacturing_adapters/electronics.json': '11c23eb33f2d4ea3f6b02abb79cfe5f4aae346ccfa71cb84a525c42157acfb33',
}
# Reviewed individually on 2026-09-23: five industries x nine public SIMULATION
# files, never customer data. Raw SHA256 is intentional: even whitespace/encoding
# changes require review. Do not replace this literal map with discovery/globs,
# canonical schema acceptance, or a blanket CSV/secret-scanner exemption.
REVIEWED_SIMULATION_HASHES = {
    'config/manufacturing_examples/pharma/domain.json': 'e2869c9de86feb4242dbb6eec80c6ca2f5bd9c33f022a3df854da8472487b9db',
    'config/manufacturing_examples/pharma/adapter.json': '9cf32406d5b6980a40e1bb625d73cd30feed02d2d9a3a7f5cd85dc125f65be9e',
    'config/manufacturing_examples/pharma/actual.csv': 'ecf40b99bfabbb48f15d5316372a7e745c09828df6c33b236a1feaf1d5833597',
    'config/manufacturing_examples/pharma/budget.csv': '35de4b46cef80cd6dca4c3411152fa87c1239adaa54e627f2ddb9ee27a2a0d64',
    'config/manufacturing_examples/pharma/materials.csv': 'c2c9a8e4975425c85ddac3084b42655e285e81c8417ad6053b5b2eee16e9e09b',
    'config/manufacturing_examples/pharma/labor.csv': '5d8874fbc04e62e2d2b5238f6048a4f2fab3571ce039ef7a72b4c206703a43f0',
    'config/manufacturing_examples/pharma/overhead.csv': 'ad72653abc5db32b995c9c85fad4aa5cb5b5641fb3f8f40b28e1767fc8ba0a97',
    'config/manufacturing_examples/pharma/knowledge.txt': '79f2d26be2c4f28424a4f947b5c47a5403fab14b65856bfb5d87138ffafbeffa',
    'config/manufacturing_examples/pharma/manifest.json': 'b576847d3f85534f2d25fd2c4ac0c1cfd26428ed79ce0efd61b7f68e255ebdad',
    'config/manufacturing_examples/machinery/domain.json': '67724bb34c5be2670463c84a38caee0ba58a65d6256638b1574f51b3e62ed89e',
    'config/manufacturing_examples/machinery/adapter.json': '3768883acaf5f95157bf6d47d53a2f8b28fe9ff5a6c498dce69a1911f42ef6cf',
    'config/manufacturing_examples/machinery/actual.csv': 'ac3726de6735a29937a58fa59bf0fb1474f50bf16063edacb7f67814832e011f',
    'config/manufacturing_examples/machinery/budget.csv': '319b8bc1c6926e99996ea5946340e64597531d4033e4d3e3d2da20b38baf8133',
    'config/manufacturing_examples/machinery/materials.csv': 'f77b29dfcfcbd11ee0b518c219d481a281b50ecd01037a8c394fb8235a21017d',
    'config/manufacturing_examples/machinery/labor.csv': 'f4756d309ac501349b0c3261b47437df6c46590b33c46cdaddcd2201c1c44559',
    'config/manufacturing_examples/machinery/overhead.csv': '448a7789c48dde5f91669d65ebd96a0d1f7e79bf4246e97a34e2e61e9f29dca0',
    'config/manufacturing_examples/machinery/knowledge.txt': 'f7a539da5d88be80a44434e0ed6af0db509159f72850a1062170957072a8329a',
    'config/manufacturing_examples/machinery/manifest.json': '772d46bedc7ada582b55431fa3b078a8ce6f72f8264ee0984c871e4f2b2fa5f0',
    'config/manufacturing_examples/auto_parts/domain.json': '319a78f852ca01c459d9128740971beb9764e27dfabe13aa13cd5f5f668380fe',
    'config/manufacturing_examples/auto_parts/adapter.json': '1534fbf52f98635e3521def6b2dbadb8a6c4ab91c8d874c15e64b08e94788fab',
    'config/manufacturing_examples/auto_parts/actual.csv': '0c8f96d38b307c955ce7b8cd71f93b0cb3de182f72e4388428acb250c1747401',
    'config/manufacturing_examples/auto_parts/budget.csv': '8c7da24e5d551d82f052dba2c2672c03b0c64f4959dc722ff6bc1b70e73b20f5',
    'config/manufacturing_examples/auto_parts/materials.csv': '15d8d855ed029efc84f26e68af0af0e2133d44e81b6b961c05038270322a28d9',
    'config/manufacturing_examples/auto_parts/labor.csv': 'fa6cb3041a9a4365fffa14980f5537813aa46a8c368d1fb0c868495d6cb6d9ae',
    'config/manufacturing_examples/auto_parts/overhead.csv': 'd664f681ee7bbabbb669a9cc90a85386d470c8cc9c4acbc23dd586652140ee35',
    'config/manufacturing_examples/auto_parts/knowledge.txt': '888007c8769be3084dab378044f906238ffcc14377764ae4ba893eccf4622124',
    'config/manufacturing_examples/auto_parts/manifest.json': 'c9740e2a4322c5fcd84f0cf9f84c388f0a96d1cc3f98e936a991bd8cf17e744d',
    'config/manufacturing_examples/chemicals/domain.json': 'd4d3758e1710c463d2b5dd090ceb4583f50e322f5483baf6a1e6f5bb6a03272a',
    'config/manufacturing_examples/chemicals/adapter.json': 'f196ecae68d6940fc6652b21bdc59fab73ded178098888ef3f24de7cf98f8658',
    'config/manufacturing_examples/chemicals/actual.csv': 'c33d5b03689b8ffbc6a597e6d19f863401386646ebbf9a10a8f0e96bcded22fb',
    'config/manufacturing_examples/chemicals/budget.csv': 'cf5a4c0d9e7380d429d5364404b403ba217a9eabe281b0d18fcf3edd2c11e165',
    'config/manufacturing_examples/chemicals/materials.csv': '444defc27f00cd9e2c4ef7fd230a0e63afa2dc63c60f909b7af43270085a47e0',
    'config/manufacturing_examples/chemicals/labor.csv': '4e3628f5c1fd06247787e2a6abd40de8518a12546f9edb53960f9324c570a8b8',
    'config/manufacturing_examples/chemicals/overhead.csv': '094d977fcd19273a634f615b498b312d5521fb690771081799cf05ca0672bc8f',
    'config/manufacturing_examples/chemicals/knowledge.txt': 'd214dd9f20d0726fe304bfaef0653f2aa1627dc90f243f6e025115d078718249',
    'config/manufacturing_examples/chemicals/manifest.json': '993d81de3403795e1ca36c4011fdaa5b573fae8bc0cf647e9842682db75f9d53',
    'config/manufacturing_examples/electronics/domain.json': '152f0cfb8da1b043d4f6ed23a0b0076a0fefba2b70794b65872f6f2aa65bffde',
    'config/manufacturing_examples/electronics/adapter.json': '94d74ed83f1a4faa2f4193de17073d653a02567423376c10eaa7d078d7c312c6',
    'config/manufacturing_examples/electronics/actual.csv': 'd07a2c04b527f0ac5a82380c8d8037ade9f3ba8e96eac6bb7857ddddc0584ea1',
    'config/manufacturing_examples/electronics/budget.csv': '5a8fe2675f02c579efffa3d9c7d2769257a8d60643f686bb4517a47eb51f916e',
    'config/manufacturing_examples/electronics/materials.csv': '2ed5e10bdb21070b2fe668c18dd42ee3570f12d1a25f94a491df983e981df9f0',
    'config/manufacturing_examples/electronics/labor.csv': '763ea7148b4fb93c05cff87c62f3942aebfde31cf058a5f3e1b309133bb6bc3b',
    'config/manufacturing_examples/electronics/overhead.csv': '8eccc82271326cb7f7ae8432122e98bd15b0ef2c6db849c1b5b652032fcd61db',
    'config/manufacturing_examples/electronics/knowledge.txt': '982a1ae9ab3d4e5eb5bea9ef9bb54582745dfd8da44ca7da1de1fd43d8319549',
    'config/manufacturing_examples/electronics/manifest.json': '133bdabfa83f7ad9831aaa5d93c05c5c6e5415a178863362e2ab8426c2b5b015',
}
CONFIGURATION = (
    'requirements.txt', 'requirements-models.txt', 'requirements-legacy.txt', 'requirements-core-win-py312.lock.txt',
    'Dockerfile', 'compose.yaml', '.dockerignore', '.gitignore', '.env.example', '.github/workflows/ci.yml',
    'deploy/authorization.example.json', 'deploy/streamlit.secrets.example.toml',
    'deploy/streamlit.config.toml', '.streamlit/config.toml', 'deploy/compose.models.yaml',
    'deploy/compose.restore.example.yaml', 'deploy/nginx.conf.example',
) + tuple(REVIEWED_CONFIG_HASHES) + tuple(REVIEWED_SIMULATION_HASHES)
ASSETS = (
    'assets/echarts.min.js', 'assets/echarts-LICENSE.txt', 'assets/echarts-NOTICE.txt',
    'assets/licenses/LICENSE-d3', 'assets/licenses/LICENSE-zrender', 'assets/licenses/LICENSE-tslib',
)
TESTS = (
    'conftest.py', 'tests/test_model_gateway.py', 'tests/test_knowledge_versions.py',
    'tests/test_knowledge_release.py', 'tests/test_operations.py',
    'tests/test_system_observability.py', 'tests/test_launch_system.py',
    'tests/test_knowledge_langchain.py', 'tests/test_human_scores.py', 'tests/test_attribution_repair.py',
    'tests/test_agent_router.py', 'tests/test_forecast_baseline.py', 'tests/test_task_pagination.py',
    'tests/test_deployment_identity.py', 'tests/test_remediation_causality.py', 'tests/test_remediation_citations.py',
    'tests/test_remediation_rag.py', 'tests/test_benchmark_evidence_focus28.py',
    'tests/test_knowledge_vector_reuse.py', 'tests/test_task_closure.py',
    'tests/test_benchmark_years_followup.py', 'tests/test_forecast_followup.py',
    'tests/test_model_task_routing.py', 'tests/test_model_caller_followup.py',
    'tests/test_knowledge_categories.py', 'tests/test_knowledge_browser_followup.py',
    'tests/test_document_classifier.py', 'tests/test_vision_enhancement.py',
    'tests/test_anomaly_case.py',
    'tests/test_manufacturing_adapter.py', 'tests/test_followup_packaging.py',
    'tests/test_deep_review_numeric_context.py', 'tests/test_deep_review_hybrid.py',
    'tests/test_deep_review_process_graph.py', 'tests/test_deep_review_ingestion.py',
    'tests/test_deep_review_spec_scope.py', 'tests/test_deep_review_deployment.py',
    'tests/test_deep_review_graph_evidence.py', 'tests/test_deep_review_analysis_display.py',
    'tests/test_round2_manufacturing_runtime.py', 'tests/test_round2_manufacturing_projection.py',
    'tests/test_round2_manufacturing_repository_service.py', 'tests/test_round2_manufacturing_api_ui.py',
    'tests/test_round2_generic_references.py', 'tests/test_round2_domain_graph.py',
    'tests/test_round2_model_contract.py', 'tests/test_round2_narrative.py',
    'tests/test_round2_ui.py', 'tests/test_round2_packaging.py', 'tests/test_round2_parent_integration.py',
    'tests/test_round2_prose_contract.py', 'tests/test_round2_prose_integration.py',
    'tests/test_round2_prose_ui.py', 'tests/test_round2_manufacturing_report.py',
    'tests/test_round2_history_prose.py', 'tests/test_round2_prose_transport.py',
    'tests/test_round2_prose_lexical.py', 'tests/test_round2_prose_projection.py',
    'tests/test_round2_prose_budget.py',
    'tests/test_round3_narrative.py', 'tests/test_round3_model_selection.py',
    'tests/test_round3_presentation.py',
)
# Do not add test_tabular_knowledge, test_industry_followup, or
# test_report_references_followup: they require excluded originals unconditionally.
# test_round2_report_narrative likewise requires excluded real CSVs and a frozen
# audit report. Browser/live acceptance scripts are not portable production modules.
# The two forecast original-CSV probes already skip when their inputs are absent.
BINARY_ASSETS = {'assets/workbench-brand.png': 'e359ac2d6a9f75e898b4907691180bd962c989a8a1d7ffcb43ff82c2191070b5'}
ECHARTS_HASH = 'e84270bd0cd5bdf60fefc26d00c2a391cb2e81f4d26a7a9ee16185a54773a3cf'
EXCLUDED_CATEGORIES = [
    'all files not individually listed in this builder',
    'official CSV/XLSX/PDF/DOCX/template/source text/mock attachments',
    'delivery outputs, reports, presentations, video, detailed audit JSON, original extracts',
    '.local, live .env, live Streamlit secrets, credentials, authorization and model configuration',
    'managed/knowledge databases, releases, vectors, backups, copied originals, logs',
    'model weights, fonts, virtual environments, caches, personal reviews, historical backups',
    'legacy Chroma/FlagEmbedding/graph tools except an empty namespace initializer',
    'tests unconditionally requiring original tables/templates, real model workers, browsers or official mock code',
]


def allowed_paths():
    paths = set(CORE_ROOT + DOCUMENTS + CONFIGURATION + ASSETS + TESTS) | set(BINARY_ASSETS)
    paths.update(f'{directory}/{name}.py' for directory, names in CORE_TREES.items() for name in names)
    return tuple(sorted(paths))


def portable_test_paths():
    """The single reviewed pytest file list shared by source packaging and CI."""
    paths = tuple(name for name in TESTS if name.startswith('tests/'))
    if not paths or len(paths) != len(set(paths)) or any(
            not safe_name(name) or not name.endswith('.py') for name in paths):
        raise ValueError('Portable tests must be a nonempty unique literal file list')
    return paths


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')


def safe_name(name):
    if not isinstance(name, str) or not name or '\\' in name or ':' in name or '\x00' in name:
        return False
    parts = name.split('/')
    return not name.startswith('/') and all(part not in ('', '.', '..') for part in parts)


def no_links(path, *, leaf_may_be_missing=False):
    """Reject linked ancestors before opening any allowlisted file."""
    current = path
    while True:
        try:
            info = current.lstat()
        except FileNotFoundError:
            if current != path or not leaf_may_be_missing:
                raise ValueError('Missing required file or parent: ' + current.name) from None
        else:
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Symbolic links and reparse points are not permitted: ' + current.name)
            if current == path and stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                raise ValueError('Multiple hard links are not permitted: ' + current.name)
        if current == current.parent:
            break
        current = current.parent


def read_regular(path, *, limit=MAX_FILE):
    no_links(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise ValueError('Not a permitted bounded regular file: ' + path.name)
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError('File identity changed before reading: ' + path.name)
        data = stream.read(limit + 1)
    after = path.stat()
    if len(data) > limit or (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError('File changed while reading: ' + path.name)
    return data


HIGH_RISK = (
    ('private_key', re.compile(r'-{5}BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-{5}')),
    ('provider_key', re.compile(r'\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b')),
    ('aws_access_key', re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b')),
    ('github_token', re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b')),
    ('jwt', re.compile(r'\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\b')),
    ('personal_windows_path', re.compile(r'[A-Za-z]:[/\\](?:Users|Documents and Settings)[/\\][A-Za-z0-9_.-]+', re.I)),
    ('personal_unix_path', re.compile(r'/(?:home|Users)/[A-Za-z0-9_.-]+/')),
    ('complete_cost_csv_header', re.compile(r'^工厂,产品名称,产品规格,月份,产量\(盒\),直接材料', re.M)),
    ('large_embedded_blob', re.compile(r'[A-Za-z0-9+/]{12000,}={0,2}')),
)
LITERAL_SECRET = re.compile(r'''(?i)(?<![A-Za-z0-9_])["']?(?:api[_-]?key|client[_-]?secret|cookie[_-]?secret|password|access[_-]?token)["']?\s*[:=]\s*(["'])([^"'\n]{1,180})\1''')
TEST_PLACEHOLDERS = frozenset({'private', 'secret', 'secret-key', 'private-client-secret',
    'private-value-must-not-leak', 's', 'x', 'test-only', 'test', 'key', 'super-secret'})
TEST_PLACEHOLDERS_BY_FILE = {
    'tests/test_knowledge_langchain.py': frozenset({'sentinel', 'DO_NOT_EXPOSE', 'SENTINEL', 'AMBIENT_SENTINEL'}),
    'tests/test_attribution_repair.py': frozenset({'fixture'}),
    'tests/test_model_task_routing.py': frozenset({'not-a-config'}),
}
DOC_PLACEHOLDERS = frozenset({'由部署负责人生成的至少32字符随机值', 'IdP分配的真实客户端秘密', 'IdP登记的Web客户端ID'})


def scan_text(name, data):
    if name in BINARY_ASSETS:
        if digest(data) != BINARY_ASSETS[name] or not data.startswith(b'\x89PNG\r\n\x1a\n'):
            raise ValueError('Reviewed bitmap asset changed: ' + name)
        return [], [{'path': name, 'disposition': 'reviewed_generated_wordmark_exact_sha256'}]
    text = data.decode('utf-8-sig')
    blockers, reviewed = [], []
    if name in REVIEWED_SIMULATION_HASHES:
        if digest(data) != REVIEWED_SIMULATION_HASHES[name]:
            blockers.append({'path': name, 'line': 1, 'rule': 'reviewed_simulation_hash'})
        else:
            reviewed.append({'path': name, 'disposition': 'reviewed_public_SIMULATION_exact_raw_sha256'})
    # Exact fixture approval never bypasses any ordinary text/secret/path rule.
    for rule, pattern in HIGH_RISK:
        for found in pattern.finditer(text):
            blockers.append({'path': name, 'line': text.count('\n', 0, found.start()) + 1, 'rule': rule})
    for found in LITERAL_SECRET.finditer(text):
        value = found.group(2)
        record = {'path': name, 'line': text.count('\n', 0, found.start()) + 1, 'rule': 'secret_assignment_literal'}
        if name in TESTS and (value in TEST_PLACEHOLDERS or value in TEST_PLACEHOLDERS_BY_FILE.get(name, ())):
            reviewed.append({**record, 'disposition': 'reviewed_synthetic_test_placeholder'})
        elif name in DOCUMENTS and value in DOC_PLACEHOLDERS:
            reviewed.append({**record, 'disposition': 'reviewed_instructional_placeholder'})
        else:
            blockers.append(record)
    return blockers, reviewed


def validate_reviewed_configs(snapshot):
    """Refuse changed/business-bound copies without importing application code."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON fields in reviewed configuration')
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError('Non-finite JSON in reviewed configuration')

    for name, expected in REVIEWED_CONFIG_HASHES.items():
        value = json.loads(snapshot[name].decode('utf-8-sig'),
                           object_pairs_hook=unique_object, parse_constant=invalid_constant)
        if digest(canonical(value)) != expected:
            raise ValueError('Reviewed non-secret configuration changed; distribution review required: ' + name)
        if name.startswith('config/manufacturing_adapters/') and (
                value.get('status') != 'template' or value.get('factories') != []
                or value.get('product_units') != []):
            raise ValueError('Only inactive manufacturing schema templates may be packaged: ' + name)


def validate_reviewed_simulations(snapshot):
    """Only the individually reviewed public bytes qualify, never arbitrary /2 data.

    Deliberately independent of application imports and current configuration. This
    is a distribution review gate, not a customer-data loader or runtime validator.
    """
    for name, expected in REVIEWED_SIMULATION_HASHES.items():
        if name not in snapshot or digest(snapshot[name]) != expected:
            raise ValueError('Reviewed public SIMULATION changed; distribution review required: ' + name)
        text = snapshot[name].decode('utf-8')
        if 'SIMULATION' not in text:
            raise ValueError('Reviewed fixture must explicitly declare SIMULATION: ' + name)
        if name.endswith('/domain.json'):
            value = json.loads(text)
            if (value.get('schema_version') != 'manufacturing-domain/2'
                    or value.get('data_classification') != 'simulation' or value.get('status') != 'active'):
                raise ValueError('Invalid reviewed SIMULATION domain: ' + name)
        elif name.endswith('/adapter.json'):
            value = json.loads(text)
            if value.get('schema_version') != 'manufacturing-adapter/2' or value.get('status') != 'active':
                raise ValueError('Invalid reviewed SIMULATION adapter: ' + name)
        elif name.endswith('/manifest.json'):
            value = json.loads(text)
            if (value.get('schema_version') != 'manufacturing-example/1'
                    or value.get('data_classification') != 'simulation'):
                raise ValueError('Invalid reviewed SIMULATION inventory: ' + name)


def simulation_review_metadata():
    return {'classification': 'SIMULATION', 'real_dataset': False,
        'review_basis': 'individually_read_public_synthetic_sources_exact_raw_sha256',
        'hash_policy': 'raw_sha256_including_whitespace_and_encoding; no_glob_or_schema_only_approval',
        'files': [{'path': name, 'sha256': sha, 'classification': 'SIMULATION'}
                  for name, sha in sorted(REVIEWED_SIMULATION_HASHES.items())],
        'limitations': 'Synthetic five-industry fixtures are not actual deployments, researched benchmarks, source authenticity, model quality or task dispatch acceptance.'}


def validate_examples(snapshot):
    if json.loads(snapshot['deploy/authorization.example.json']) != {'users': {}}:
        raise ValueError('Authorization example must contain only an empty users mapping')
    auth = tomllib.loads(snapshot['deploy/streamlit.secrets.example.toml'].decode())
    expected = {'redirect_uri', 'cookie_secret', 'client_id', 'client_secret', 'server_metadata_url'}
    if set(auth) != {'auth'} or set(auth['auth']) != expected or any(auth['auth'].values()):
        raise ValueError('Streamlit auth example must contain only empty required values')
    public_defaults = {'COST_IMAGE_TAG': 'local', 'COST_WEB_PORT': '8501', 'COST_API_PORT': '8000',
        'COST_MEMORY_LIMIT': '8g', 'COST_CPUS': '2.0', 'INSTALL_LOCAL_MODELS': 'true',
        'TORCH_INDEX_URL': 'https://download.pytorch.org/whl/cpu'}
    for line in snapshot['.env.example'].decode().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, equal, value = line.partition('=')
        if not equal or value != public_defaults.get(key, ''):
            raise ValueError('Unexpected nonempty environment example value: ' + key)


def source_analysis(snapshot):
    trees, failures = {}, []
    for name, data in snapshot.items():
        if name.endswith('.py'):
            trees[name] = ast.parse(data.decode('utf-8-sig'), filename=name)
    module_names = {name[:-3].replace('/', '.').removesuffix('.__init__') for name in trees}
    local_roots = {name.split('.')[0] for name in module_names}
    local_roots.add('kb_search')
    optional = []
    for name, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imports = []
                if node.level:
                    package = name.split('/')[:-1]
                    package = package[:len(package) - node.level + 1]
                    target = '.'.join([*package, *([node.module] if node.module else [])])
                else:
                    target = node.module or ''
                imports = [target]
            else:
                continue
            for target in imports:
                root = target.split('.')[0]
                if root not in local_roots:
                    continue
                exists = target in module_names or any(module.startswith(target + '.') for module in module_names)
                if exists:
                    continue
                item = {'path': name, 'line': node.lineno, 'module': target}
                if (name, target) == ('attribution_gen.py', 'kb_search'):
                    optional.append({**item, 'reason': 'unused legacy rag_evidence helper; governed entrypoint uses knowledge_context'})
                else:
                    failures.append(item)
    for required in CORE_ROOT + tuple(f'{directory}/{name}.py'
            for directory, names in CORE_TREES.items() for name in names):
        if required not in snapshot:
            failures.append({'missing_entrypoint': required})
    if failures:
        raise ValueError('Unresolved local source dependencies: ' + json.dumps(failures, ensure_ascii=False))
    return {'python_files_parsed': len(trees), 'static_local_import_check': 'passed_with_declared_optional_legacy_import',
            'optional_unbundled_imports': optional, 'dynamic_ui_entrypoints': 'present',
            'scope': 'AST and explicit page allowlist; not execution, pip dependency resolution or formal callgraph proof'}


def snapshot_sources():
    snapshot = {}
    blockers, reviewed = [], []
    for name in allowed_paths():
        if not safe_name(name):
            raise ValueError('Invalid allowlisted path')
        data = read_regular(ROOT / name)
        failed, acknowledged = scan_text(name, data)
        blockers.extend(failed)
        reviewed.extend(acknowledged)
        snapshot[name] = data
    if sum(map(len, snapshot.values())) > MAX_TOTAL:
        raise ValueError('Source bundle exceeds the reviewed total size')
    if blockers:
        # Never print matched values or line contents, even for a blocked build.
        raise ValueError('Source scan blocked: ' + json.dumps(blockers, ensure_ascii=False))
    if digest(snapshot['assets/echarts.min.js']) != ECHARTS_HASH:
        raise ValueError('ECharts bytes changed; review upstream version and notices before allowing')
    validate_examples(snapshot)
    validate_reviewed_configs(snapshot)
    validate_reviewed_simulations(snapshot)
    analysis = source_analysis(snapshot)
    records = [{'path': name, 'bytes': len(data), 'sha256': digest(data), 'source_sha256': digest(data),
                'transformation': 'none'} for name, data in snapshot.items()]
    tree_hash = digest(canonical([{key: row[key] for key in ('path', 'bytes', 'sha256')} for row in records]))
    manifest = {'schema': SCHEMA, 'publication_status': PUBLICATION_STATUS, 'publication_target': PUBLICATION_TARGET,
        'project_license': 'opensource_license_not_selected', 'authorization_status': AUTHORIZATION_STATUS,
        'source_tree_sha256': tree_hash, 'file_count': len(records), 'files': records,
        'scope': 'literal source-only allowlist; excluded directories never enumerated or read',
        'exclusions': EXCLUDED_CATEGORIES,
        'configuration_exceptions': ['.env.example', 'deploy/authorization.example.json',
            'deploy/streamlit.secrets.example.toml', *REVIEWED_CONFIG_HASHES, *REVIEWED_SIMULATION_HASHES],
        'scan': {'high_confidence_findings': [], 'reviewed_placeholder_findings': reviewed,
            'files_scanned': len(records), 'format': 'UTF-8 text plus exact-hash reviewed PNG wordmark', 'python': analysis,
            'rule_names': [rule for rule, _ in HIGH_RISK] + ['secret_assignment_literal', 'reviewed_simulation_hash'],
            'limitation': 'Pattern/allowlist checks are not a proof of no secrets and do not grant distribution rights.'},
        'content_review': {'test_files': list(portable_test_paths()),
            'public_simulation_examples': simulation_review_metadata(),
            'synthetic_test_review': 'static_review_by_builder; synthetic fixtures except documented conditional originals; test_execution_evidence_is_separate',
            'conditional_original_data_tests': [{
                'nodeid': 'tests/test_forecast_baseline.py::test_real_legacy_loader_cost_summaries_are_read_only_and_gap_aware',
                'excluded_inputs': [f'{factory}_成本汇总_{year}年1-6月.csv'
                                    for factory in ('中药一厂', '中药二厂') for year in (2025, 2026)],
                'source_only_behavior': 'existing pytest.skip when source CSVs are absent; test retained unchanged',
            }, {
                'nodeid': 'tests/test_forecast_followup.py::test_real_csv_budget_only_matches_january_to_june_2026',
                'excluded_inputs': ['中药一厂_预算数据_2026年.csv',
                                    '中药一厂_成本汇总_2025年1-6月.csv', '中药一厂_成本汇总_2026年1-6月.csv'],
                'source_only_behavior': 'existing pytest.skip when source CSVs are absent; test retained unchanged',
            }],
            'validation_scope': 'builder_only; no claim about separate application, CI or remote publication runs',
            'small_product_names_numeric_examples_and_audit_metadata': 'included_in_user_authorized_source_scope; original_data_excluded',
            'private_document_references': 'links to omitted internal originals/manifests remain documented as separately controlled',
            'legacy_mode': 'excluded; requirements-legacy is informational only'},
        'zip_policy': {'member_order': 'source paths sorted, generated manifest last', 'timestamp': list(STAMP),
            'regular_file_mode': '0644', 'compression': 'deflate', 'source_bytes_unmodified': True},
        'third_party': {'echarts_version': '5.5.1', 'echarts_sha256': ECHARTS_HASH,
            'source': 'https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js',
            'notices': list(ASSETS[1:]), 'project_authorization': 'not_implied'},
        'validation_not_performed': ['full_test_suite', 'real_llm', 'production_oidc', 'container_build', 'public_upload']}
    return snapshot, manifest


def make_zip(snapshot, manifest):
    stream = io.BytesIO()
    embedded = canonical(manifest)
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in [*snapshot.items(), (EMBEDDED_NAME, embedded)]:
            info = zipfile.ZipInfo(name, date_time=STAMP)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return stream.getvalue(), embedded


def verify_bytes(archive_bytes, external):
    if external.get('schema') != SCHEMA or digest(archive_bytes) != external.get('archive_sha256'):
        raise ValueError('Bundle schema or archive hash mismatch')
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        entries = archive.infolist()
        if archive.comment or any(entry.comment or entry.extra for entry in entries):
            raise ValueError('Archive comments and extra metadata are not permitted')
        names = [entry.filename for entry in entries]
        expected = set(allowed_paths()) | {EMBEDDED_NAME}
        if len(names) != len(set(names)) or set(names) != expected or any(not safe_name(name) for name in names):
            raise ValueError('Duplicate, missing, unexpected or unsafe archive members')
        if sum(entry.file_size for entry in entries) > MAX_TOTAL + MAX_FILE:
            raise ValueError('Expanded archive too large')
        for entry in entries:
            if (entry.file_size > MAX_FILE or entry.flag_bits & 1
                    or entry.external_attr >> 16 != stat.S_IFREG | 0o644
                    or entry.date_time != STAMP or entry.compress_type != zipfile.ZIP_DEFLATED):
                raise ValueError('Unsupported archive member type, mode, timestamp or size')
        embedded = archive.read(EMBEDDED_NAME)
        if digest(embedded) != external.get('embedded_manifest_sha256'):
            raise ValueError('Embedded manifest hash mismatch')
        internal = json.loads(embedded)
        manifest_fields = {'schema', 'publication_status', 'publication_target', 'project_license', 'authorization_status',
            'source_tree_sha256', 'file_count', 'files', 'scope', 'exclusions', 'configuration_exceptions',
            'scan', 'content_review', 'zip_policy', 'third_party', 'validation_not_performed'}
        extra_fields = {'generated_utc', 'archive_path', 'archive_bytes', 'archive_sha256', 'embedded_manifest_sha256'}
        if set(internal) != manifest_fields or set(external) != manifest_fields | extra_fields:
            raise ValueError('Unexpected manifest fields')
        if embedded != canonical(internal) or scan_text(EMBEDDED_NAME, embedded)[0]:
            raise ValueError('Embedded manifest must use the canonical safe form')
        for key in manifest_fields:
            if internal[key] != external.get(key):
                raise ValueError('Inner/outer manifest mismatch: ' + key)
        if (internal['publication_status'] != PUBLICATION_STATUS
                or internal['publication_target'] != PUBLICATION_TARGET
                or internal['project_license'] != 'opensource_license_not_selected'
                or internal['authorization_status'] != AUTHORIZATION_STATUS
                or not safe_name(external['archive_path'])
                or PurePosixPath(external['archive_path']).parts[0] != 'delivery'
                or PurePosixPath(external['archive_path']).name != BUNDLE_NAME
                or external['archive_bytes'] != len(archive_bytes)):
            raise ValueError('Unexpected publication, authorization or archive metadata')
        records = internal['files']
        record_paths = [record['path'] for record in records]
        if (internal['file_count'] != len(records) or len(record_paths) != len(set(record_paths))
                or set(record_paths) != set(allowed_paths())
                or any(set(record) != {'path', 'bytes', 'sha256', 'source_sha256', 'transformation'}
                       or record['transformation'] != 'none' for record in records)):
            raise ValueError('Manifest does not match unique unchanged allowlisted files')
        snapshot = {}
        for record in internal['files']:
            data = archive.read(record['path'])
            if len(data) != record['bytes'] or digest(data) != record['sha256'] or record['sha256'] != record['source_sha256']:
                raise ValueError('Member/source hash mismatch: ' + record['path'])
            blockers, _ = scan_text(record['path'], data)
            if blockers:
                raise ValueError('Archive secret/path scan failed without exposing matches')
            snapshot[record['path']] = data
        recomputed = digest(canonical([{key: row[key] for key in ('path', 'bytes', 'sha256')} for row in internal['files']]))
        if recomputed != internal['source_tree_sha256']:
            raise ValueError('Source tree hash mismatch')
        validate_examples(snapshot)
        validate_reviewed_configs(snapshot)
        validate_reviewed_simulations(snapshot)
        if internal['content_review'].get('public_simulation_examples') != simulation_review_metadata():
            raise ValueError('Public SIMULATION review metadata mismatch')
        source_analysis(snapshot)
        if digest(snapshot['assets/echarts.min.js']) != ECHARTS_HASH:
            raise ValueError('ECharts pinned hash mismatch')
    return {'status': 'verified', 'members': len(names), 'source_files': len(snapshot),
            'archive_sha256': digest(archive_bytes), 'source_tree_sha256': internal['source_tree_sha256'],
            'publication_status': internal['publication_status'], 'authorization_status': internal['authorization_status']}


def atomic_write(path, data):
    no_links(path.parent)
    no_links(path, leaf_may_be_missing=True)
    fd, temporary = tempfile.mkstemp(prefix='.source-bundle-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Publish atomically without replacement, including a concurrent writer.
        # Both paths share the same directory/filesystem; unlink the staging name
        # immediately so the final regular file has exactly one hard link.
        os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def self_check():
    assert all(safe_name(name) for name in allowed_paths())
    assert not set(allowed_paths()) & {'.env', '.local/llm.json', 'delivery/competition_data_inventory.json'}
    for value in ('../file.py', '/absolute.py', 'C:/key', 'a\\b', 'a//b', 'a/./b'):
        assert not safe_name(value)
    for value in ('sk-' + 'a' * 30, '-' * 5 + 'BEGIN PRIVATE KEY' + '-' * 5,
                  'AKIA' + 'A' * 16, 'C:/Users/' + 'synthetic-owner/private.txt'):
        assert scan_text('README.md', value.encode())[0]
    assignment = 'api' + '_key = ' + chr(34)
    assert scan_text('README.md', (assignment + 'unreviewed-new-value' + chr(34)).encode())[0]
    fake_assignment = (assignment + 'secret' + chr(34)).encode()
    assert not scan_text('tests/test_model_gateway.py', fake_assignment)[0]
    assert scan_text('tests/test_model_gateway.py', fake_assignment)[1]
    assert set(ASSETS[1:]) <= set(allowed_paths())
    assert '.github/workflows/ci.yml' in allowed_paths() and safe_name('.github/workflows/ci.yml')
    sentinel_assignment = (assignment + 'SENTINEL' + chr(34)).encode()
    assert not scan_text('tests/test_knowledge_langchain.py', sentinel_assignment)[0]
    assert scan_text('README.md', sentinel_assignment)[0]
    assert scan_text('tests/test_model_gateway.py', sentinel_assignment)[0]
    fixture_assignment = (assignment + 'fixture' + chr(34)).encode()
    assert not scan_text('tests/test_attribution_repair.py', fixture_assignment)[0]
    assert scan_text('README.md', fixture_assignment)[0]
    assert scan_text('tests/test_model_gateway.py', fixture_assignment)[0]
    print(json.dumps({'self_check': 'passed', 'scope': 'new builder path and synthetic secret rejection probes; no files or network touched'}))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--verify-only', action='store_true')
    modes.add_argument('--self-check', action='store_true')
    parser.add_argument('--output-dir', default='delivery', help='Workspace delivery subdirectory for an additive local bundle')
    args = parser.parse_args(argv)
    try:
        if args.self_check:
            self_check()
            return 0
        destination = (ROOT / args.output_dir).absolute()
        if '..' in Path(args.output_dir).parts or not destination.is_relative_to(ROOT / 'delivery'):
            raise ValueError('Output directory must be within workspace delivery/')
        if not args.verify_only and any((destination / name).exists() for name in (BUNDLE_NAME, MANIFEST_NAME)):
            raise ValueError('Additive bundle destination already contains an output; choose a new directory')
        if not args.verify_only:
            no_links(destination, leaf_may_be_missing=True)
            destination.mkdir(exist_ok=True)
        archive_path, manifest_path = destination / BUNDLE_NAME, destination / MANIFEST_NAME
        if args.verify_only:
            manifest = json.loads(read_regular(manifest_path))
            report = verify_bytes(read_regular(archive_path, limit=MAX_TOTAL + MAX_FILE), manifest)
        else:
            snapshot, manifest = snapshot_sources()
            archive_bytes, embedded = make_zip(snapshot, manifest)
            external = {**manifest, 'generated_utc': datetime.now(timezone.utc).isoformat(),
                'archive_path': archive_path.relative_to(ROOT).as_posix(), 'archive_bytes': len(archive_bytes),
                'archive_sha256': digest(archive_bytes), 'embedded_manifest_sha256': digest(embedded)}
            report = verify_bytes(archive_bytes, external)
            atomic_write(archive_path, archive_bytes)
            atomic_write(manifest_path, canonical(external))
            report['outputs'] = [archive_path.relative_to(ROOT).as_posix(), manifest_path.relative_to(ROOT).as_posix()]
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except (OSError, ValueError, SyntaxError, zipfile.BadZipFile, KeyError) as exc:
        print('Source bundle refused: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
