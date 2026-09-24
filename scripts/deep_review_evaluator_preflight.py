"""Read-only evaluator filesystem/dependency preflight; stdout JSON only.

No application imports, model imports, DB opens, file writes, network, secret-file
reads, server starts or task dispatch. File presence is not runtime acceptance.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
COST_FILES = (
    '中药一厂_人工工时明细_2026年1-6月.csv', '中药一厂_制造费用明细_2026年1-6月.csv',
    '中药一厂_原材料消耗明细_2026年1-6月.csv', '中药一厂_成本汇总_2025年1-6月.csv',
    '中药一厂_成本汇总_2026年1-6月.csv', '中药一厂_预算数据_2026年.csv',
    '中药二厂_成本汇总_2025年1-6月.csv', '中药二厂_成本汇总_2026年1-6月.csv')
REFERENCE_FILES = ('药材市场价格行情_2026年上半年.csv', '行业成本基准数据_2026.csv')
PDF_FILES = ('GMP法规核心摘要_2010修订版.pdf', '产品配方文档_六味地黄胶囊.pdf',
             '产品配方文档_板蓝根颗粒.pdf', '产品配方文档_银黄口服液.pdf',
             '生产工艺文档_中药一厂.pdf', '药品生产质量管理规范GMP.pdf', '车间设备清单_中药一厂.pdf')
TEMPLATE = '月度成本分析报告模板.docx'


def regular(path):
    return path is not None and path.is_file() and not path.is_symlink()


def inventory(source):
    groups = [('01_成本明细数据', COST_FILES), ('02_行业参考数据', REFERENCE_FILES),
              ('03_制药知识文档', PDF_FILES), ('04_报告模板', (TEMPLATE,))]
    rows = []
    for folder, names in groups:
        for name in names:
            choices = (source / folder / name, source / name) if source else ()
            selected = next((p for p in choices if regular(p)), None)
            rows.append({'filename': name, 'kind': folder, 'exists': selected is not None,
                         'layout': 'nested_official' if selected and selected.parent.name == folder else 'flat' if selected else None,
                         'bytes': selected.stat().st_size if selected else None})
    return rows


def dependency_checks(requirements):
    rows = []
    for file in requirements:
        for line in file.read_text(encoding='utf-8').splitlines():
            match = re.fullmatch(r'([A-Za-z0-9_.-]+)(?:\[[^]]+\])?==([^\s;]+)', line.strip())
            if not match:
                continue
            name, expected = match.groups()
            try:
                installed = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                installed = None
            rows.append({'distribution': name, 'expected': expected, 'installed': installed,
                         'matches_pin': installed == expected, 'module_imported': False,
                         'group': 'bge_runtime' if file.name == 'requirements-models.txt' else 'core'})
    return rows


def model_checks(path, required):
    if path is None:
        return {'required_for_formal_dense': required, 'supplied': False, 'filesystem_ready': False,
                'weights_loaded': False, 'license_verified': False}
    names = ('model.safetensors', 'pytorch_model.bin')
    weights = [p for p in (path / n for n in names) if regular(p)]
    shard_count = 0
    index_valid = True
    for name in ('model.safetensors.index.json', 'pytorch_model.bin.index.json'):
        index = path / name
        if not regular(index):
            continue
        try:
            if index.stat().st_size > 4 * 1024 * 1024:
                raise ValueError('index too large')
            entries = set(json.loads(index.read_text(encoding='utf-8'))['weight_map'].values())
            if len(entries) > 1000:
                raise ValueError('too many shards')
            for entry in entries:
                if not isinstance(entry, str) or Path(entry).name != entry or '/' in entry or '\\' in entry:
                    raise ValueError('unsafe shard path')
                p = path / entry
                if not regular(p):
                    index_valid = False
                else:
                    weights.append(p)
                    shard_count += 1
        except (OSError, ValueError, KeyError, TypeError):
            index_valid = False
    weights = list({p.name: p for p in weights}.values())
    config = regular(path / 'config.json')
    tokenizer_config = regular(path / 'tokenizer_config.json')
    tokenizer = any(regular(path / n) for n in ('tokenizer.json', 'sentencepiece.bpe.model', 'tokenizer.model', 'vocab.txt'))
    ready = path.is_dir() and config and tokenizer_config and tokenizer and bool(weights) and index_valid
    return {'required_for_formal_dense': required, 'supplied': True, 'directory_exists': path.is_dir(),
            'config_exists': config, 'tokenizer_config_exists': tokenizer_config, 'tokenizer_exists': tokenizer,
            'weight_file_count': len(weights), 'shard_count': shard_count,
            'weight_bytes': sum(p.stat().st_size for p in weights), 'shard_index_complete': index_valid,
            'filesystem_ready': ready, 'weights_loaded': False, 'weights_hashed': False, 'license_verified': False}


def collect(args):
    source = args.source_root
    rows = inventory(source)
    template = args.template or (args.data_root / TEMPLATE if args.data_root else None)
    mock = args.mock_script
    if mock is None and source:
        mock = source / '05_RPA接口文档/mock_rpa_server.py'
    deps = dependency_checks([ROOT / 'requirements.txt', ROOT / 'requirements-models.txt'])
    dense = model_checks(args.model_dir, True)
    rerank = model_checks(args.reranker_dir, False)
    target = args.data_root
    target_rows = [{'filename': name, 'exists': regular(target / name) if target else False}
                   for name in COST_FILES + REFERENCE_FILES + PDF_FILES + (TEMPLATE,)]
    blockers = []
    if sys.version_info[:2] != (3, 12): blockers.append('python_target_is_3_12')
    if any(not row['matches_pin'] for row in deps): blockers.append('dependency_pin_missing_or_mismatch')
    if any(not row['exists'] for row in rows): blockers.append('authorized_source_incomplete_or_not_supplied')
    if any(not row['exists'] for row in target_rows): blockers.append('target_business_root_not_flat_and_complete')
    if not regular(template): blockers.append('authorized_report_template_missing')
    if not regular(mock): blockers.append('official_mock_script_missing')
    if not dense['filesystem_ready']: blockers.append('local_bge_files_missing_or_incomplete')
    if not regular(args.font_file): blockers.append('explicit_report_font_missing_or_not_supplied')
    return {
        'schema': 'evaluator-filesystem-preflight/1', 'checked_at': datetime.now(timezone.utc).isoformat(),
        'scope': 'current_host_filesystem_only_not_third_machine_acceptance',
        'python': {'version': platform.python_version(), 'tested_target': '3.12', 'platform': sys.platform},
        'read_only': True, 'network_calls': 0, 'model_loads': 0, 'database_opens': 0, 'secret_files_read': 0,
        'dependencies': deps, 'authorized_source': {'expected_csv': 10, 'expected_pdf': 7, 'expected_docx_template': 1, 'files': rows},
        'target_business_files': target_rows,
        'template': {'explicit_path_or_data_root': template is not None, 'exists': regular(template)},
        'official_mock': {'exists': regular(mock), 'executed': False},
        'font': {'exists': regular(args.font_file), 'glyph_coverage_tested': False},
        'bge': dense, 'reranker_optional_not_formal_rule_ranker': rerank,
        'docker': {'cli_available': shutil.which('docker') is not None, 'daemon_checked': False, 'build_executed': False},
        'identity_and_llm': {'oidc_validated': False, 'cloud_approval_validated': False, 'credentials_inspected': False,
                             'guidance': 'Configure privately on target machine; do not export workstation secrets.'},
        'blockers': blockers, 'filesystem_ready': not blockers, 'business_ready': False,
        'remaining_acceptance': ['source_and_model_distribution_authorization', 'fresh_target_install',
                                 'dense_publication_and_matching_process_readiness', 'model_actual_call_receipts',
                                 'three_authorized_mock_tasks', 'human_0_to_5_scores', 'production_oidc_if_used'],
        'boundary': 'Presence/version checks are not model loading, image build, retrieval, report quality or deployment proof.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, help='Authorized official packet root (flat or named numbered subfolders)')
    parser.add_argument('--data-root', type=Path, help='Prepared flat target COST_DATA_DIR; inspected, never created')
    parser.add_argument('--template', type=Path, help='Authorized DOCX template, otherwise data-root default')
    parser.add_argument('--mock-script', type=Path, help='Authorized official mock script; inspected, never imported')
    parser.add_argument('--model-dir', type=Path, default=Path(os.environ['BGE_M3_PATH']) if os.environ.get('BGE_M3_PATH') else None)
    parser.add_argument('--reranker-dir', type=Path, default=Path(os.environ['BGE_RERANKER_PATH']) if os.environ.get('BGE_RERANKER_PATH') else None)
    parser.add_argument('--font-file', type=Path, help='Approved report TrueType font file; no font download')
    parser.add_argument('--strict', action='store_true', help='Return 1 when filesystem blockers remain; default inventory-only 0')
    args = parser.parse_args(argv)
    result = collect(args)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 1 if args.strict and result['blockers'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
