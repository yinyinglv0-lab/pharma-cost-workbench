"""Deterministic CSV records and reference semantics over confirmed source text.

Quoted text is always one contiguous original record. Headers and parsed values
are separate metadata, never fabricated prefixes on a source quote. A schema is
not an authorization grant, and an external reference is not process evidence.
"""
from __future__ import annotations

from copy import deepcopy
from calendar import monthrange
import csv
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import io
import re

from enterprise.knowledge import KnowledgeError

TABULAR = {'name': 'schema-logical-csv-records', 'version': 1,
           'offset_unit': 'confirmed_text_unicode_characters', 'offset_end': 'exclusive'}
KNOWLEDGE_TYPES = frozenset({'formula', 'process', 'equipment', 'regulation',
    'regulation_summary', 'industry_benchmark', 'market_prices', 'cost_baseline', 'other'})
INDUSTRY_HEADERS = ('产品类别', '指标', '行业P25', '行业P50', '行业P75', '本厂水平(中药一厂)', '对标评价')
MARKET_HEADERS = ('药材名称', '规格等级', '单位', '1月价格', '2月价格', '3月价格',
                  '4月价格', '5月价格', '6月价格', '价格来源', '趋势分析')
# Stable, exact long-form contracts. Values are decimal strings in the declared
# unit (62 means 62%, not .62); a row is one observation, never a guessed period.
GENERIC_MARKET_HEADERS = ('material', 'grade', 'unit', 'month', 'price', 'source_market')
GENERIC_INDUSTRY_HEADERS = ('category', 'metric', 'unit', 'period_start', 'period_end',
                            'p25', 'p50', 'p75', 'source_name')
GENERIC_SCHEMA_IDENTIFIERS = {'market_prices': 'manufacturing-market-reference/2',
                              'industry_benchmark': 'manufacturing-industry-reference/2'}
COST_REQUIRED = frozenset({'工厂', '产品名称', '产品规格', '月份', '产量(盒)',
    '直接材料(元/盒)', '直接人工(元/盒)', '制造费用(元/盒)', '单位成本(元/盒)', '总成本(元)'})
_CATEGORY = {'产品配方': 'formula', '配方资料': 'formula', '配方': 'formula',
             '生产工艺': 'process', '工艺': 'process', '工艺资料': 'process',
             '设备参考': 'equipment', '设备资料': 'equipment', '设备': 'equipment',
             '法规原文': 'regulation', '法规': 'regulation', '法规摘要': 'regulation_summary',
             '行业基准': 'industry_benchmark', '市场参考': 'market_prices',
             '派生成本基线': 'cost_baseline'}
_SCHEMA_ROLE = {'industry_benchmark': 'benchmark_reference', 'market_prices': 'market_reference',
                'cost_baseline': 'observed_baseline'}
_SCHEMA_KIND = {'industry_benchmark': 'industry_reference', 'market_prices': 'market_reference',
                'cost_baseline': 'observed_baseline'}
_NUMERIC = re.compile(r'[+-]?\d+(?:\.\d+)?%?\Z')


def normalize_knowledge_types(values):
    if values is None:
        return None
    if not isinstance(values, (list, tuple, set, frozenset)) or not values or any(
            not isinstance(value, str) or value not in KNOWLEDGE_TYPES for value in values):
        raise KnowledgeError('knowledge_types须为非空已知知识类型列表')
    return tuple(sorted(set(values)))


def schema_for_headers(headers):
    if (not isinstance(headers, (list, tuple)) or any(not isinstance(h, str) or not h for h in headers)
            or len(headers) != len(set(headers))):
        return 'unknown'
    if tuple(headers) in (INDUSTRY_HEADERS, GENERIC_INDUSTRY_HEADERS):
        return 'industry_benchmark'
    if tuple(headers) in (MARKET_HEADERS, GENERIC_MARKET_HEADERS):
        return 'market_prices'
    if COST_REQUIRED <= set(headers):
        return 'cost_baseline'
    return 'unknown'


def knowledge_type(meta):
    """Derive routing from the verified schema, not a caller-supplied label."""
    if meta.get('format') == 'csv':
        table = meta.get('table_row') or {}
        if table:
            schema = schema_for_headers(table.get('headers') or [])
            if is_generic_table(table) and (table.get('schema_version') != 2
                    or table.get('schema_identifier') != GENERIC_SCHEMA_IDENTIFIERS.get(schema)):
                return 'other'
            return schema if table.get('schema') == schema and schema != 'unknown' else 'other'
        # Older immutable releases have parser metadata but no row metadata.
        previews = (meta.get('parse_metadata') or {}).get('preview_rows') or []
        schema = schema_for_headers(previews[0]) if previews else 'unknown'
        return schema if schema != 'unknown' else 'other'
    return _CATEGORY.get(meta.get('category'), 'other')


def logical_records(text):
    """Yield csv.reader records and exact ranges, including multiline cells.

    record_number counts logical CSV records including the header, not physical
    lines. Empty records retain their numbering but are not emitted as chunks.
    """
    lines = text.splitlines(keepends=True)
    offsets, pos = [0], 0
    for line in lines:
        pos += len(line)
        offsets.append(pos)
    reader = csv.reader(io.StringIO(text, newline=''), strict=True)
    previous = 0
    try:
        for number, cells in enumerate(reader, 1):
            end_line = reader.line_num
            start, end = offsets[previous], offsets[end_line]
            if end > start and text[end - 1] == '\n':
                end -= 1
                if end > start and text[end - 1] == '\r':
                    end -= 1
            yield {'record_number': number, 'cells': cells, 'offset': start, 'end_offset': end,
                   'physical_line_start': previous + 1, 'physical_line_end': end_line}
            previous = end_line
    except (csv.Error, IndexError) as exc:
        raise KnowledgeError('CSV逻辑记录无法安全定位') from exc


def is_generic_table(table):
    return (isinstance(table, dict) and isinstance(table.get('headers'), (list, tuple))
            and tuple(table['headers']) in (GENERIC_MARKET_HEADERS, GENERIC_INDUSTRY_HEADERS))


def _generic_number(value):
    if not isinstance(value, str) or len(value) > 80 or not re.fullmatch(r'[+-]?\d+(?:\.\d+)?', value):
        raise ValueError('exact decimal required')
    number = Decimal(value)
    if not number.is_finite() or abs(number) > Decimal('1e20'):
        raise ValueError('bounded finite decimal required')
    return number


def _date_period(start, end):
    try:
        if not all(isinstance(item, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', item) for item in (start, end)):
            return {'invalid': True}
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        return {'start': start, 'end': end} if first <= last else {'invalid': True}
    except (ValueError, TypeError):
        return {'invalid': True}


def _generic_row_period(table):
    columns = table.get('columns') or {}
    if tuple(table.get('headers') or ()) == GENERIC_MARKET_HEADERS:
        month = columns.get('month')
        if not isinstance(month, str) or not re.fullmatch(r'\d{4}-(?:0[1-9]|1[0-2])', month):
            return {'invalid': True}
        try:
            year, number = map(int, month.split('-'))
            return _date_period(month + '-01', month + f'-{monthrange(year, number)[1]:02d}')
        except (ValueError, TypeError):
            return {'invalid': True}
    return _date_period(columns.get('period_start'), columns.get('period_end'))


def _profile_binding(profile):
    """Detached validated vocabulary only; it grants no source or factory access."""
    from enterprise.domain_profiles import CANONICAL_SCHEMA, profile_fingerprint, validate_domain_profile
    value = validate_domain_profile(profile)
    if value['schema_version'] != CANONICAL_SCHEMA:
        raise ValueError('generic references require manufacturing-domain/2')
    materials = {item['id']: item for item in value['materials']}
    return {'schema_version': 'manufacturing-reference-binding/1',
        'profile_sha256': profile_fingerprint(value), 'profile': value,
        'products': [{'product_id': p['id'], 'product': p['name'], 'specification': p['specification'],
            'category': p['category'], 'reporting_unit': p['reporting_unit'], 'currency': value['currency'],
            'materials': [deepcopy(materials[b['material_id']]) for b in p['bom']]} for p in value['products']]}


def _generic_profile(meta, profile=None):
    """No ambient profile fallback: replay the confirmed row's frozen binding."""
    try:
        binding = meta.get('reference_profile')
        if not isinstance(binding, dict):
            return None, 'missing_reference_profile'
        reviewed = (meta.get('business_metadata') or {}).get('manufacturing_domain_profile')
        expected = _profile_binding(reviewed)
        if binding != expected:
            return None, 'reference_profile_binding_mismatch'
        if profile is not None and _profile_binding(profile) != expected:
            return None, 'reference_profile_mismatch'
        return expected['profile'], None
    except (ValueError, TypeError, KeyError, RecursionError):
        return None, 'invalid_reference_profile'


def _generic_metric_reason(columns, profile):
    metric = next((m for m in profile['reference_metrics'] if m['source_name'] == columns['metric']), None)
    if metric is None:
        return 'metric_not_configured'
    if metric['unit'] != columns['unit']:
        return 'reference_unit_mismatch'
    try:
        values = [_generic_number(columns[name]) for name in ('p25', 'p50', 'p75')]
        if not values[0] <= values[1] <= values[2]:
            return 'nonmonotonic_percentiles'
        if metric['calculation'] == 'element_share' and not all(0 <= v <= 100 for v in values):
            return 'invalid_share_percentile'
        if metric['calculation'] == 'unit_cost' and any(v < 0 for v in values):
            return 'negative_cost_percentile'
    except (ValueError, InvalidOperation, KeyError):
        return 'invalid_reference_number'
    return None


def source_period(meta, schema=None):
    table = meta.get('table_row') or {}
    if is_generic_table(table):
        period = _generic_row_period(table)
        if period.get('invalid'):
            return period
        # A reviewed range may narrow admissibility, but never replaces the row
        # observation period or stretches one month across later reports.
        reviewed = (meta.get('business_metadata') or {}).get('source_period')
        if reviewed is not None:
            if not isinstance(reviewed, dict) or set(reviewed) != {'start', 'end'}:
                return {'invalid': True}
            outer = _date_period(reviewed['start'], reviewed['end'])
            if outer.get('invalid') or not outer['start'] <= period['start'] <= period['end'] <= outer['end']:
                return {'invalid': True}
        return period
    schema = schema or knowledge_type(meta)
    if schema not in {'industry_benchmark', 'market_prices', 'cost_baseline'}:
        return None
    reviewed = meta.get('source_period') or (meta.get('business_metadata') or {}).get('source_period')
    if reviewed is not None:
        if not isinstance(reviewed, dict) or set(reviewed) != {'start', 'end'}:
            return {'invalid': True}
        try:
            start, end = date.fromisoformat(reviewed['start']), date.fromisoformat(reviewed['end'])
        except (ValueError, TypeError, KeyError):
            return {'invalid': True}
        if start > end:
            return {'invalid': True}
        return {'start': start.isoformat(), 'end': end.isoformat()}
    # Compatibility for reviewed original CSVs whose old bootstrap date was a
    # demonstration baseline. Only an unambiguous explicit source year is used.
    years = set(re.findall(r'(?<!\d)(20\d{2})(?!\d)', str(meta.get('filename', ''))))
    if len(years) != 1:
        return {'invalid': True}
    year = years.pop()
    end = f'{year}-06-30' if schema == 'market_prices' else f'{year}-12-31'
    return {'start': f'{year}-01-01', 'end': end}


def source_period_reason(meta, as_of):
    period = source_period(meta)
    if period is None:
        return None
    if period.get('invalid'):
        return 'unknown_source_period'
    if as_of is None:
        return 'missing_reference_date'
    if not period['start'] <= as_of <= period['end']:
        return 'outside_source_period'
    table = meta.get('table_row') or {}
    if table.get('schema') == 'cost_baseline':
        month = (table.get('columns') or {}).get('月份', '')
        if not re.fullmatch(r'\d{4}-(?:0[1-9]|1[0-2])', month) or month > as_of[:7]:
            return 'future_or_unknown_observation'
    return None


def _valid_columns(schema, columns):
    if set(columns) in (set(GENERIC_INDUSTRY_HEADERS), set(GENERIC_MARKET_HEADERS)):
        if any(not isinstance(value, str) or not value.strip() or len(value) > 2000 for value in columns.values()):
            return False
        try:
            if schema == 'market_prices' and set(columns) == set(GENERIC_MARKET_HEADERS):
                return (not _generic_row_period({'headers': GENERIC_MARKET_HEADERS, 'columns': columns}).get('invalid')
                        and _generic_number(columns['price']) >= 0)
            if schema == 'industry_benchmark' and set(columns) == set(GENERIC_INDUSTRY_HEADERS):
                values = [_generic_number(columns[name]) for name in ('p25', 'p50', 'p75')]
                return (not _generic_row_period({'headers': GENERIC_INDUSTRY_HEADERS, 'columns': columns}).get('invalid')
                        and values[0] <= values[1] <= values[2])
        except (ValueError, InvalidOperation, KeyError):
            return False
        return False
    if schema == 'industry_benchmark':
        return bool(columns['产品类别'].strip() and columns['指标'].strip() and all(
            _NUMERIC.fullmatch(columns[h].strip()) for h in INDUSTRY_HEADERS[2:6]))
    if schema == 'market_prices':
        return bool(columns['药材名称'].strip() and columns['单位'].strip() and all(
            re.fullmatch(r'\d+(?:\.\d+)?', columns[f'{m}月价格'].strip()) for m in range(1, 7)))
    if schema == 'cost_baseline':
        return bool(columns['产品名称'].strip() and columns['工厂'].strip() and
                    re.fullmatch(r'\d{4}-(?:0[1-9]|1[0-2])', columns['月份']))
    return False


def table_chunks(text, meta):
    """Return exact CSV row spans and source-validated, additive metadata."""
    records = list(logical_records(text))
    if not records or not records[0]['cells']:
        raise KnowledgeError('CSV缺少明确表头')
    header, headers = records[0], records[0]['cells']
    schema = schema_for_headers(headers)
    generic = tuple(headers) in (GENERIC_MARKET_HEADERS, GENERIC_INDUSTRY_HEADERS)
    period = source_period(meta, schema) if not generic else None
    binding = None
    if generic:
        try:
            binding = _profile_binding((meta.get('business_metadata') or {}).get('manufacturing_domain_profile'))
        except (ValueError, TypeError, KeyError, RecursionError):
            pass  # Typed CSV alone is never permission or a phantom domain.
    for record in records[1:]:
        if not record['cells']:
            continue
        start, end = record['offset'], record['end_offset']
        columns = dict(zip(headers, record['cells'])) if len(record['cells']) == len(headers) else {}
        valid = bool(schema != 'unknown' and len(columns) == len(headers) and _valid_columns(schema, columns))
        result = deepcopy(meta)
        row_schema = schema if valid else 'unknown'
        result['knowledge_type'] = row_schema if valid else 'other'
        result['table_row'] = {'schema': row_schema, 'schema_version': 1, 'headers': list(headers),
            'columns': columns, 'record_number': record['record_number'],
            'physical_line_start': record['physical_line_start'], 'physical_line_end': record['physical_line_end'],
            'header_offset': header['offset'], 'header_end_offset': header['end_offset'],
            'row_sha256': hashlib.sha256(text[start:end].encode('utf-8')).hexdigest(),
            'year': int(period['start'][:4]) if period and not period.get('invalid') else None}
        if generic:
            # The confirmed catalog retains its full preview. A selected row must
            # not carry other periods' prices through nested parser metadata.
            if isinstance(result.get('parse_metadata'), dict):
                result['parse_metadata'] = {key: deepcopy(value) for key, value in result['parse_metadata'].items()
                                            if key != 'preview_rows'}
            result['table_row'].update(schema_version=2,
                schema_identifier=GENERIC_SCHEMA_IDENTIFIERS[schema],
                header_quote=text[header['offset']:header['end_offset']],
                header_sha256=hashlib.sha256(text[header['offset']:header['end_offset']].encode('utf-8')).hexdigest())
            if binding is not None:
                result['reference_profile'] = deepcopy(binding)
                result['business_metadata'] = {**result.get('business_metadata', {}),
                    'manufacturing_domain_profile': deepcopy(binding['profile'])}
            period = source_period(result, schema)
            result['table_row']['year'] = int(period['start'][:4]) if not period.get('invalid') else None
        if period:
            result['source_period'] = period
        result['reference_kind'] = _SCHEMA_KIND.get(row_schema)
        declared = result.get('business_metadata', {}).get('evidence_role', 'context_only')
        role = _SCHEMA_ROLE.get(row_schema, 'context_only')
        if declared not in {'document_basis', role}:
            role = 'context_only'
        result['business_metadata'] = {**result.get('business_metadata', {}), 'evidence_role': role}
        result.update(offset=start, end_offset=end, pages=[], page_spans=[], section='')
        result['applicability'] = {'schema_version': 2, 'kind': _SCHEMA_KIND.get(row_schema, 'unknown'),
            'products': [], 'specifications': [], 'basis': 'csv_schema_record' if valid else 'unknown_csv_schema',
            'offset': start, 'end_offset': end, 'sections': [], 'section': '', 'legacy_release': False,
            'eligible_for_mechanism': False, 'eligible_for_reference': valid}
        if generic:
            configured = next((m for m in (binding or {}).get('profile', {}).get('reference_metrics', [])
                               if m['source_name'] == columns.get('metric')), None)
            result['elements'] = (['material'] if row_schema == 'market_prices' else
                [configured['element']] if configured and configured['element'] in {'material', 'labor', 'overhead'}
                else ['material', 'labor', 'overhead'] if row_schema == 'industry_benchmark' else [])
            if binding is not None:
                scoped = [p for p in binding['products'] if
                    (row_schema == 'market_prices' and columns.get('material') in {m['name'] for m in p['materials']}) or
                    (row_schema == 'industry_benchmark' and columns.get('category') in
                     {p['category'], binding['profile']['industry_category']})]
                result['applicability'].update(products=sorted({p['product'] for p in scoped}),
                    specifications=sorted({p['specification'] for p in scoped}),
                    profile_sha256=binding['profile_sha256'])
            result['applicability']['eligible_for_reference'] = valid and binding is not None
        elif row_schema == 'industry_benchmark':
            metric = columns['指标']
            element_by_metric = [('材料成本占比', '材料'), ('人工成本占比', '人工'),
                                 ('制造费用占比', '制费')]
            result['elements'] = [element for label, element in element_by_metric if label in metric] or ['材料', '人工', '制费']
        elif row_schema == 'market_prices':
            result['elements'] = ['材料']
        else:
            result['elements'] = []
        yield start, end, result


def reference_applicability_reason(meta, product, specification=None, as_of=None, objects=None, *, profile=None):
    """Reference-only admission; never changes ACL or mechanism eligibility.

    Product matching is from a validated business profile, not from a broad
    document authorization scope. Callers still enforce authorization first.
    """
    if not isinstance(meta, dict):
        return 'missing_reference_schema'
    table = meta.get('table_row') or {}
    if not isinstance(table, dict):
        return 'missing_reference_schema'
    schema = table.get('schema')
    generic = is_generic_table(table)
    if schema not in _SCHEMA_KIND or table.get('schema_version') != (2 if generic else 1):
        return 'missing_reference_schema'
    if (schema_for_headers(table.get('headers') or []) != schema or
            (generic and table.get('schema_identifier') != GENERIC_SCHEMA_IDENTIFIERS[schema])):
        return 'invalid_reference_schema'
    columns = table.get('columns') or {}
    if not isinstance(columns, dict) or set(columns) != set(table['headers']) or not _valid_columns(schema, columns):
        return 'invalid_reference_row'
    if generic and (meta.get('reference_kind') != _SCHEMA_KIND[schema] or meta.get('knowledge_type') != schema):
        return 'reference_kind_mismatch'
    app = meta.get('applicability') or {}
    if not isinstance(app, dict) or app.get('kind') != _SCHEMA_KIND[schema] or not app.get('eligible_for_reference'):
        return 'invalid_reference_applicability'
    if generic and (app.get('eligible_for_mechanism') is not False or app.get('offset') != meta.get('offset')
                    or app.get('end_offset') != meta.get('end_offset')):
        return 'invalid_reference_applicability'
    business = meta.get('business_metadata') or {}
    if not isinstance(business, dict) or business.get('evidence_role') != _SCHEMA_ROLE[schema]:
        return 'context_only'
    if generic and meta.get('source_period') != source_period(meta):
        return 'reference_period_metadata_mismatch'
    reason = source_period_reason(meta, as_of)
    if reason:
        return reason
    from enterprise.domain_profiles import load_domain_profile, product_definition, validate_domain_profile
    if generic:
        profile, reason = _generic_profile(meta, profile)
        if reason:
            return reason
        if app.get('profile_sha256') != meta['reference_profile']['profile_sha256']:
            return 'reference_profile_binding_mismatch'
    else:
        profile = load_domain_profile() if profile is None else validate_domain_profile(profile)
    definition = product_definition(product, specification, profile=profile)
    if definition is None:
        return 'unknown_product_specification'
    if generic:
        if schema == 'industry_benchmark':
            if columns['category'] not in {definition['category'], profile['industry_category']}:
                return 'benchmark_category_not_applicable'
            return _generic_metric_reason(columns, profile)
        material_ids = {row['material_id'] for row in definition['bom']}
        materials = [m for m in profile['materials'] if m['id'] in material_ids and m['name'] == columns['material']]
        if columns['material'] not in definition['materials'] or not materials:
            return 'material_not_applicable'
        if len(materials) != 1:
            return 'ambiguous_material_binding'
        if columns['unit'] != profile['currency'] + '/' + materials[0]['unit']:
            return 'reference_unit_mismatch'
        if objects is not None and columns['material'] not in objects:
            return 'material_not_in_query_objects'
        return None
    if schema == 'industry_benchmark':
        if columns['产品类别'] not in {definition['category'], profile['industry_category']}:
            return 'benchmark_category_not_applicable'
    elif schema == 'market_prices':
        material = columns['药材名称']
        if material not in definition['materials']:
            return 'material_not_applicable'
        if objects is not None and material not in objects:
            return 'material_not_in_query_objects'
    else:
        if columns['产品名称'] != product or columns['产品规格'] != specification:
            return 'observation_not_applicable'
    return None
