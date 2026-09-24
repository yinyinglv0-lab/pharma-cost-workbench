"""Explicit derived baseline from the two confirmed cost-summary CSVs.

This helper copies observed CSV fields into one reviewable baseline document and
records exact source hashes/rows.  It performs no purchase-price, unit-consumption,
yield, causal, or remediation-closure inference.  Callers must still preview,
stage, confirm, and publish the generated file in an isolated repository.
"""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path


SCHEMA = 'derived-factory-cost-baseline/1'
DEFAULT_FILENAMES = ('中药一厂_成本汇总_2026年1-6月.csv', '中药二厂_成本汇总_2026年1-6月.csv')
SOURCE_FIELDS = ('工厂', '产品名称', '产品规格', '月份', '产量(盒)', '直接材料(元/盒)',
                 '直接人工(元/盒)', '制造费用(元/盒)', '单位成本(元/盒)', '总成本(元)')
OUTPUT_FIELDS = SOURCE_FIELDS + ('source_file', 'source_sha256', 'source_row', 'derivation_status', 'derivation_note')
BOUNDARY = (
    '逐行保留两厂已提供成本汇总CSV的原值；仅作为观察基线。'
    '不推导采购价、实际单耗、收率、工艺异常原因、节约收益或处置/验收闭环。'
)
GAPS = (
    '二厂未提供配对原材料、工时或制造费用明细，不能计算跨厂实际量价差；'
    '当前目录的历史成本异常处理案例库字段标注为模拟，未纳入真实闭环知识。'
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _decode(path: Path):
    raw = path.read_bytes()
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            return raw, raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError(f'CSV无法按UTF-8或GB18030解码: {path.name}')


def _read_source(path: Path):
    raw, text = _decode(path)
    reader = csv.DictReader(text.splitlines())
    if tuple(reader.fieldnames or ()) != SOURCE_FIELDS:
        raise ValueError(f'成本汇总CSV字段不匹配: {path.name}')
    rows = []
    for csv_row, row in enumerate(reader, start=2):
        if any(row.get(field) in (None, '') for field in SOURCE_FIELDS):
            raise ValueError(f'成本汇总CSV存在空字段: {path.name}:{csv_row}')
        rows.append({field: row[field] for field in SOURCE_FIELDS} | {
            'source_file': path.name,
            'source_sha256': hashlib.sha256(raw).hexdigest(),
            'source_row': str(csv_row),
            'derivation_status': 'observed_cost_summary',
            'derivation_note': BOUNDARY,
        })
    return raw, rows


def build_factory_cost_baseline(source_root, output_path, *, filenames=DEFAULT_FILENAMES):
    """Create a deterministic derived CSV and a provenance manifest.

    ``source_root`` and ``output_path`` are explicit caller-selected paths.  No
    repository or release database is opened or changed by this function.
    """
    root = Path(source_root).resolve()
    output = Path(output_path).resolve()
    if not isinstance(filenames, (tuple, list)) or not filenames:
        raise ValueError('filenames须为非空明确CSV文件名列表')
    all_rows, sources = [], []
    for filename in filenames:
        path = (root / filename).resolve()
        if path.parent != root or path.suffix.lower() != '.csv' or not path.is_file():
            raise ValueError('派生基线仅允许源目录内明确列出的CSV原件')
        raw, rows = _read_source(path)
        all_rows.extend(rows)
        all_rows_count = len(rows)
        sources.append({'filename': path.name, 'sha256': hashlib.sha256(raw).hexdigest(),
                        'bytes': len(raw), 'row_count': all_rows_count,
                        'factory': sorted({row['工厂'] for row in rows}),
                        'fields': list(SOURCE_FIELDS)})
    all_rows.sort(key=lambda row: (row['工厂'], row['产品名称'], row['产品规格'], row['月份'], row['source_file']))
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=OUTPUT_FIELDS, lineterminator='\n')
        writer.writeheader()
        writer.writerows(all_rows)
    products = sorted({row['产品名称'] for row in all_rows})
    factories = sorted({row['工厂'] for row in all_rows})
    months = sorted({row['月份'] for row in all_rows})
    manifest = {
        'schema': SCHEMA,
        'title': '两厂成本汇总派生观察基线（2026年1-6月）',
        'output_file': output.name,
        'output_sha256': _sha256(output),
        'source_files': sources,
        'row_count': len(all_rows),
        'coverage': {'factories': factories, 'products': products, 'months': months,
                     'observed_fields': list(SOURCE_FIELDS)},
        'derivation': {'method': 'source-row-union-preserving-original-string-values',
                       'formula': None, 'rounding': None, 'status': 'derived_observation_only'},
        'claim_boundary': BOUNDARY,
        'known_gaps': [GAPS],
        'review_status': 'pending_explicit_preview_confirmation_publication',
    }
    manifest_path = output.with_suffix('.manifest.json')
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'output': output, 'manifest': manifest_path, 'metadata': manifest}


def bootstrap_spec(output_path, metadata):
    """Return a caller-reviewable knowledge.stage spec for the derived file."""
    output = Path(output_path).resolve()
    coverage = metadata.get('coverage') or {}
    source_files = metadata.get('source_files') or []
    return {
        'filename': output.name,
        'title': metadata.get('title', '两厂成本汇总派生观察基线（2026年1-6月）'),
        'scope_products': list(coverage.get('products') or []),
        'scope_factories': list(coverage.get('factories') or []),
        'visibility': 'scoped',
        'effective_from': '2026-01-01',
        'effective_to': '2026-06-30',
        'category': '派生成本基线',
        'sha256': _sha256(output),
        'metadata': {
            'authority': 'derived_from_explicit_confirmed_cost_csv',
            'evidence_role': 'document_basis',
            'source_files': source_files,
            'source_hashes': [item.get('sha256') for item in source_files],
            'derivation_schema': SCHEMA,
            'claim_boundary': metadata.get('claim_boundary', BOUNDARY),
            'known_gaps': metadata.get('known_gaps', [GAPS]),
            'not_actual_procurement': True,
            'not_actual_consumption': True,
            'not_yield_evidence': True,
        },
    }
