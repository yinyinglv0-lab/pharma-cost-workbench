"""Exact cross-industry CSV reference contracts; no live provider or artifact writes.

Copyable machinery examples (UTF-8; the header order is part of the schema):

material,grade,unit,month,price,source_market
Alloy steel,A,CNY/kg,2026-08,10.25,Independent exchange
Alloy steel,A,CNY/kg,2026-09,11.50,Independent exchange

category,metric,unit,period_start,period_end,p25,p50,p75,source_name
Gearboxes,Material share,%,2026-01-01,2026-12-31,50,60,70,Industry survey
Gearboxes,Unit cost,CNY/piece,2026-01-01,2026-12-31,8,10,12,Industry survey

The knowledge administrator confirms business_metadata.manufacturing_domain_profile
from the installed validated manufacturing-domain/2 profile. That vocabulary is
not an ACL grant. Only separately released, authorized rows enter comparisons.
"""
from copy import deepcopy
import csv
from decimal import Decimal
import hashlib
import io

import pytest

from enterprise.domain_profiles import validate_domain_profile, profile_fingerprint
from enterprise.evidence_references import (
    build_reference_evidence, market_reading_rows, reference_model_context,
    reference_source_reason,
)
from enterprise.industry_benchmark import build_canonical_industry_comparison
from enterprise.tabular_knowledge import (
    GENERIC_INDUSTRY_HEADERS, GENERIC_MARKET_HEADERS, INDUSTRY_HEADERS,
    knowledge_type, reference_applicability_reason, schema_for_headers,
    source_period, table_chunks,
)


@pytest.fixture
def profile():
    value = {'schema_version': 'manufacturing-domain/2', 'id': 'reference-machinery', 'version': '2',
        'industry': 'machinery', 'label': 'Machinery references', 'status': 'active',
        'data_classification': 'simulation', 'source_dataset': 'isolated-test',
        'reporting_unit': 'piece', 'currency': 'CNY',
        'factories': {'home': 'Gear plant A', 'peer': 'Gear plant B'},
        'labels': {k: k for k in ('home', 'peer', 'material', 'labor', 'overhead', 'unitcost', 'output', 'total')},
        'products': [{'id': 'gearbox', 'name': 'Gearbox', 'specification': 'G1', 'category': 'Gearboxes',
            'reporting_unit': 'piece', 'materials': ['Alloy steel'],
            'bom': [{'material_id': 'steel', 'quantity_per_reporting_unit': '2.5', 'unit': 'kg'}],
            'process_ids': ['milling'], 'equipment_ids': ['mill']}],
        'materials': [{'id': 'steel', 'name': 'Alloy steel', 'unit': 'kg'}],
        'processes': [{'id': 'milling', 'name': 'Milling', 'product_ids': ['gearbox'], 'metric_ids': ['roughness']}],
        'equipment': [{'id': 'mill', 'name': 'Mill', 'process_ids': ['milling']}],
        'industry_category': 'Machinery', 'knowledge_types': ['process', 'industry_benchmark', 'market_prices'],
        'limitations': ['Simulation reference, never actual quantity or an authorization grant'],
        'reference_metrics': [
            {'id': 'material_share', 'source_name': 'Material share', 'unit': '%', 'direction': 'context_only',
             'calculation': 'element_share', 'element': 'material', 'process_ids': []},
            {'id': 'unit_cost', 'source_name': 'Unit cost', 'unit': 'CNY/piece', 'direction': 'lower_cost',
             'calculation': 'unit_cost', 'element': 'total', 'process_ids': []},
            {'id': 'roughness', 'source_name': 'Roughness', 'unit': 'um', 'direction': 'context_only',
             'calculation': 'source_only', 'element': 'none', 'process_ids': ['milling']},
            {'id': 'change', 'source_name': 'Cost change', 'unit': '%', 'direction': 'context_only',
             'calculation': 'source_only', 'element': 'none', 'process_ids': []}]}
    return validate_domain_profile(value)


def csv_text(headers, rows):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, lineterminator='\r\n')
    writer.writerow(headers)
    writer.writerows(rows)
    return stream.getvalue()


def chunks(profile, headers, rows, *, metadata=None):
    text = csv_text(headers, rows)
    meta = {'format': 'csv', 'filename': 'references.csv', 'sha256': hashlib.sha256(text.encode()).hexdigest(),
        'version_id': 'v1', 'business_metadata': {'evidence_role': 'document_basis',
        'manufacturing_domain_profile': deepcopy(profile),
        'source_period': {'start': '2025-01-01', 'end': '2027-12-31'}}}
    if metadata:
        meta['business_metadata'].update(metadata)
    result = []
    for ordinal, (start, end, rowmeta) in enumerate(table_chunks(text, meta)):
        rowmeta['ordinal'] = ordinal
        result.append({'meta': rowmeta, 'text': text[start:end], 'version_id': 'v1',
            'document_id': 'd1', 'release_id': 'release1', 'chunk_id': f'{meta["sha256"]}:{ordinal}'})
    return text, result


def evidence(profile, headers=GENERIC_INDUSTRY_HEADERS, rows=None, month='2026-09', **kwargs):
    rows = rows if rows is not None else [['Gearboxes', 'Material share', '%', '2026-01-01', '2026-12-31', '50', '60', '70', 'Survey']]
    _, parsed = chunks(profile, headers, rows, **kwargs)
    selected = []
    for row in parsed:
        own_month = row['meta']['table_row']['columns'].get('month', month)
        item, reason = build_reference_evidence(row, {'retrieval_mode': 'fixture_authorized', 'degraded': False, 'framework': {}},
            product='Gearbox', specification='G1', month=own_month, profile=profile)
        assert reason is None, reason
        selected.append(item)
    return selected


def facts(profile, material='6', labor='2', overhead='2', unit='10'):
    def observed(side, component, unitcost):
        return {'factory': profile['factories'][side], 'product': 'Gearbox', 'specification': 'G1', 'month': '2026-09',
            'currency': 'CNY', 'unit': 'piece', 'material': component, 'labor': labor, 'overhead': overhead,
            'unitcost': unitcost, 'output': '100', 'total': str(Decimal(unitcost) * 100),
            'source': {'source_id': side, 'source_sha256': 'a' * 64, 'source_row_sha256': 'b' * 64,
                       'adapter_sha256': 'c' * 64, 'row_number': 1}}
    home = observed('home', material, unit)
    peer = observed('peer', material, unit)
    return {'schema_version': 'manufacturing-facts/1',
        'scope': {'industry': 'machinery', 'product': 'Gearbox', 'product_id': 'gearbox', 'specification': 'G1',
                  'month': '2026-09', 'home_factory': 'Gear plant A', 'peer_factory': 'Gear plant B'},
        'measurement': {'currency': 'CNY', 'quantity_unit': 'piece', 'unit_cost_unit': 'CNY/piece'},
        'current': home, 'benchmark': {'peer': peer}, 'provenance': {'profile_sha256': profile_fingerprint(profile)}}


def test_exact_schemas_route_existing_types_and_preserve_contiguous_quote(profile):
    text, parsed = chunks(profile, GENERIC_MARKET_HEADERS,
        [['Alloy steel', 'A, inspected\nsecond line', 'CNY/kg', '2026-09', '11.50', 'Exchange']])
    row, = parsed
    meta, table = row['meta'], row['meta']['table_row']
    assert knowledge_type(meta) == 'market_prices'
    assert table['schema_version'] == 2 and table['schema_identifier'] == 'manufacturing-market-reference/2'
    assert row['text'] == text[meta['offset']:meta['end_offset']]
    assert table['row_sha256'] == hashlib.sha256(row['text'].encode()).hexdigest()
    assert table['header_quote'] == text[table['header_offset']:table['header_end_offset']]
    assert table['physical_line_start'] == 2 and table['physical_line_end'] == 3
    assert meta['source_period'] == {'start': '2026-09-01', 'end': '2026-09-30'}
    assert schema_for_headers(list(reversed(GENERIC_MARKET_HEADERS))) == 'unknown'
    assert schema_for_headers(GENERIC_MARKET_HEADERS + ('ref_kind',)) == 'unknown'
    assert schema_for_headers(GENERIC_INDUSTRY_HEADERS) == 'industry_benchmark'
    catalog_meta = {'format': 'csv', 'filename': 'all-months.csv', 'business_metadata': {
        'evidence_role': 'document_basis', 'manufacturing_domain_profile': profile},
        'parse_metadata': {'preview_rows': [list(GENERIC_MARKET_HEADERS), ['future', '99999']], 'row_count': 2}}
    _, _, selected = next(table_chunks(text, catalog_meta))
    assert 'preview_rows' not in selected['parse_metadata']
    assert catalog_meta['parse_metadata']['preview_rows'][-1] == ['future', '99999']


@pytest.mark.parametrize('industry', ['machinery', 'auto_parts', 'chemicals', 'electronics'])
def test_typed_reference_contract_applies_across_manufacturing_domains(profile, industry):
    profile['industry'] = industry
    profile['id'] = 'references-' + industry.replace('_', '-')
    value = facts(profile)
    value['scope']['industry'] = industry
    selected = evidence(profile)
    result = build_canonical_industry_comparison(value, selected, profile)
    assert result['available'] and result['rows'][0]['home']['exact'] == '60'
    assert selected[0]['reference_profile']['profile_sha256'] == profile_fingerprint(profile)


def test_frozen_profile_prefilter_does_not_consult_global_default(profile, monkeypatch):
    _, rows = chunks(profile, GENERIC_MARKET_HEADERS, [['Alloy steel', 'A', 'CNY/kg', '2026-09', '10', 'Exchange']])
    monkeypatch.setattr('enterprise.domain_profiles.load_domain_profile', lambda *a, **kw: pytest.fail('ambient domain loaded'))
    meta = rows[0]['meta']
    assert reference_applicability_reason(meta, 'Gearbox', 'G1', '2026-09-30') is None
    assert meta['reference_profile']['profile_sha256'] == profile_fingerprint(profile)
    assert meta['reference_profile']['products'][0]['materials'] == profile['materials']
    assert reference_applicability_reason(meta, 'Other', 'G1', '2026-09-30') == 'unknown_product_specification'
    assert reference_applicability_reason(meta, 'Gearbox', 'G1', '2026-08-31') == 'outside_source_period'
    changed = deepcopy(profile)
    changed['products'][0]['category'] = 'New category'
    assert reference_applicability_reason(meta, 'Gearbox', 'G1', '2026-09-30', profile=changed) == 'reference_profile_mismatch'


@pytest.mark.parametrize('extra', [None, {'roles': ['admin']}, {'path': 'C:/secrets'}, {'grants': ['*']}])
def test_missing_or_unvalidated_profile_never_creates_domain_or_auth(profile, extra):
    value = None if extra is None else {**profile, **extra}
    _, rows = chunks(value, GENERIC_MARKET_HEADERS, [['Alloy steel', 'A', 'CNY/kg', '2026-09', '10', 'Exchange']])
    assert not rows[0]['meta']['applicability']['eligible_for_reference']
    assert 'reference_profile' not in rows[0]['meta']
    assert reference_applicability_reason(rows[0]['meta'], 'Gearbox', 'G1', '2026-09-30') is not None


@pytest.mark.parametrize('unit', ['CNY/ton', 'USD/kg', '元/kg', 'CNY/piece'])
def test_market_wrong_currency_or_unit_never_coerces_magnitude(profile, unit):
    _, rows = chunks(profile, GENERIC_MARKET_HEADERS, [['Alloy steel', 'A', unit, '2026-09', '10', 'Exchange']])
    assert reference_applicability_reason(rows[0]['meta'], 'Gearbox', 'G1', '2026-09-30') == 'reference_unit_mismatch'


@pytest.mark.parametrize('values', [('70', '60', '80'), ('-1', '60', '70'), ('50', '60', '101'), ('50%', '60%', '70%'), ('NaN', '60', '70')])
def test_percentiles_reject_nonmonotonic_nonnumeric_and_invalid_share_bounds(profile, values):
    _, rows = chunks(profile, GENERIC_INDUSTRY_HEADERS,
        [['Gearboxes', 'Material share', '%', '2026-01-01', '2026-12-31', *values, 'Survey']])
    assert reference_applicability_reason(rows[0]['meta'], 'Gearbox', 'G1', '2026-09-30') is not None


def test_negative_source_only_percent_is_valid_not_misread_as_share(profile):
    selected = evidence(profile, rows=[['Machinery', 'Cost change', '%', '2026-01-01', '2026-12-31', '-5', '-2', '1', 'Survey']])
    result = build_canonical_industry_comparison(facts(profile), selected, profile)
    row, = result['rows']
    assert row['p25']['exact'] == '-5'
    assert row['home']['value'] is None and row['peer']['value'] is None


def test_row_period_overrides_metadata_range_and_partial_month_refused(profile):
    _, rows = chunks(profile, GENERIC_MARKET_HEADERS, [['Alloy steel', 'A', 'CNY/kg', '2026-10', '999', 'Exchange']])
    assert source_period(rows[0]['meta']) == {'start': '2026-10-01', 'end': '2026-10-31'}
    assert reference_applicability_reason(rows[0]['meta'], 'Gearbox', 'G1', '2026-09-30') == 'outside_source_period'
    _, partial = chunks(profile, GENERIC_INDUSTRY_HEADERS,
        [['Gearboxes', 'Material share', '%', '2026-09-15', '2026-12-31', '50', '60', '70', 'Survey']])
    item, reason = build_reference_evidence(partial[0], {'retrieval_mode': 'test', 'degraded': False, 'framework': {}},
        product='Gearbox', specification='G1', month='2026-09')
    assert item is None and reason == 'incomplete_reference_month'


@pytest.mark.parametrize('mutation', ['quote', 'headers', 'header_quote', 'row_sha', 'offset', 'profile_hash', 'profile_material',
                                      'role', 'kind', 'scope', 'source_period', 'release_id', 'evidence_id'])
def test_generic_evidence_source_gate_rejects_mutated_frozen_fields(profile, mutation):
    source, = evidence(profile)
    if mutation == 'quote': source['source']['quote'] += 'x'
    elif mutation == 'headers': source['source']['headers'] = list(reversed(source['source']['headers']))
    elif mutation == 'header_quote': source['source']['header_quote'] += ',extra'
    elif mutation == 'row_sha': source['source']['row_sha256'] = 'f' * 64
    elif mutation == 'offset': source['source']['end_offset'] += 1
    elif mutation == 'profile_hash': source['reference_metadata']['reference_profile']['profile_sha256'] = 'e' * 64
    elif mutation == 'profile_material': source['reference_metadata']['business_metadata']['manufacturing_domain_profile']['materials'][0]['name'] = 'Other'
    elif mutation == 'role': source['evidence_role'] = 'document_basis'
    elif mutation == 'kind': source['kind'] = 'market_reference'
    elif mutation == 'scope': source['scope']['months'] = ['2026-08']
    elif mutation == 'source_period': source['reference_metadata']['source_period']['start'] = '2020-01-01'
    elif mutation == 'release_id': source['index_release_id'] = ''
    elif mutation == 'evidence_id': source['id'] += 'x'
    assert reference_source_reason(source, 'Gearbox', 'G1', '2026-09', profile=profile) is not None


def test_real_independent_market_rows_pair_with_both_citations_no_future_or_invented_past(profile):
    selected = evidence(profile, GENERIC_MARKET_HEADERS, [
        ['Alloy steel', 'A', 'CNY/kg', '2026-08', '10', 'Exchange'],
        ['Alloy steel', 'A', 'CNY/kg', '2026-09', '11.50', 'Exchange'],
        ['Alloy steel', 'A', 'CNY/kg', '2026-10', '99999', 'Exchange']])
    assert all(s['market_observations'][0]['previous_price'] is None for s in selected)
    row, = market_reading_rows(selected, 'Gearbox', 'G1', '2026-09', profile=profile)
    assert row['current_price'] == '11.50' and row['previous_price'] == '10'
    assert Decimal(row['month_change_pct']) == 15
    assert row['evidence_ids'] == [selected[0]['id'], selected[1]['id']]
    assert row['previous_source']['quote'] == selected[0]['text']
    context = reference_model_context(selected, 'material', month='2026-09')
    assert context[0]['market_direction'] == '上涨' and context[0]['evidence_ids'] == row['evidence_ids']
    assert '99999' not in str(context) and '2026-10' not in str(context)
    current_only, = market_reading_rows(selected[1:], 'Gearbox', 'G1', '2026-09')
    assert current_only['previous_price'] is None and current_only['month_change_pct'] is None
    selected[0]['text'] += 'tampered'
    current_only, = market_reading_rows(selected, 'Gearbox', 'G1', '2026-09')
    assert current_only['previous_price'] is None


def test_market_previous_requires_exact_grade_source_unit_and_immediate_month(profile):
    selected = evidence(profile, GENERIC_MARKET_HEADERS, [
        ['Alloy steel', 'A', 'CNY/kg', '2026-07', '9', 'Exchange'],
        ['Alloy steel', 'B', 'CNY/kg', '2026-08', '10', 'Exchange'],
        ['Alloy steel', 'A', 'CNY/kg', '2026-09', '11', 'Exchange']])
    row, = market_reading_rows(selected, 'Gearbox', 'G1', '2026-09')
    assert row['previous_price'] is None
    assert market_reading_rows(selected + [deepcopy(selected[-1])], 'Gearbox', 'G1', '2026-09') == []


def test_market_pairing_refuses_different_profile_or_release_generation(profile):
    current, = evidence(profile, GENERIC_MARKET_HEADERS,
        [['Alloy steel', 'A', 'CNY/kg', '2026-09', '12', 'Exchange']])
    prior_profile = deepcopy(profile)
    prior_profile['version'] = 'prior-version'
    previous, = evidence(prior_profile, GENERIC_MARKET_HEADERS,
        [['Alloy steel', 'A', 'CNY/kg', '2026-08', '10', 'Exchange']])
    reading, = market_reading_rows([previous, current], 'Gearbox', 'G1', '2026-09')
    assert reading['previous_price'] is None
    previous, = evidence(profile, GENERIC_MARKET_HEADERS,
        [['Alloy steel', 'A', 'CNY/kg', '2026-08', '10', 'Exchange']])
    previous['index_release_id'] = 'another-generation'
    reading, = market_reading_rows([previous, current], 'Gearbox', 'G1', '2026-09')
    assert reading['previous_price'] is None


def test_canonical_actual_share_and_unit_cost_not_source_reported_company(profile):
    selected = evidence(profile, rows=[
        ['Gearboxes', 'Material share', '%', '2026-01-01', '2026-12-31', '50', '55', '70', 'Survey'],
        ['Gearboxes', 'Unit cost', 'CNY/piece', '2026-01-01', '2026-12-31', '8', '9', '12', 'Survey']])
    selected[0]['source_reported_home'] = {'exact': '99999', 'value': 99999}
    result = build_canonical_industry_comparison(facts(profile), selected, profile)
    assert result['available'] and result['category'] == 'Gearboxes' and result['month'] == '2026-09'
    share, unit = result['rows']
    assert share['home']['exact'] == share['peer']['exact'] == '60'
    assert share['home']['gap_from_p50']['exact'] == '5'
    assert share['source_reported_home']['value'] is None
    assert unit['home']['exact'] == '10' and unit['unit'] == 'CNY/piece'
    assert share['element'] == 'material' and share['home']['measurement_scope'] == 'selected_product_specification_month'
    assert result['alerts'] == []


def test_nonterminating_shares_preserve_exact_ratio_and_position(profile):
    result = build_canonical_industry_comparison(facts(profile, '1', '1', '1', '3'), evidence(profile), profile)
    observed = result['rows'][0]['home']
    assert observed['exact_ratio'] == {'numerator': '100', 'denominator': '3'}
    assert 'exact' not in observed
    assert observed['position'] == 'below_p25'


@pytest.mark.parametrize('mutation', ['profile', 'unit', 'total', 'component', 'product', 'source'])
def test_canonical_wrong_scope_units_hash_or_nonclosing_actuals_not_used(profile, mutation):
    value = facts(profile)
    if mutation == 'profile': value['provenance']['profile_sha256'] = 'f' * 64
    elif mutation == 'unit': value['current']['unit'] = 'box'
    elif mutation == 'total': value['current']['total'] = '9999'
    elif mutation == 'component': value['current']['material'] = '99'
    elif mutation == 'product': value['current']['product'] = 'Other'
    elif mutation == 'source': value['current']['source']['source_sha256'] = 'invalid'
    result = build_canonical_industry_comparison(value, evidence(profile), profile)
    assert not result['available'] or result['rows'][0]['home']['value'] is None


def test_ambiguous_industry_sources_refused_and_legacy_metadata_unchanged(profile):
    selected = evidence(profile, rows=[
        ['Gearboxes', 'Material share', '%', '2026-01-01', '2026-12-31', '50', '60', '70', 'Survey A'],
        ['Gearboxes', 'Material share', '%', '2026-08-01', '2026-12-31', '50', '61', '70', 'Survey B']])
    result = build_canonical_industry_comparison(facts(profile), selected, profile)
    assert not result['available'] and result['diagnostics'][0]['reason'] == 'ambiguous_reference_rows'
    legacy = csv_text(INDUSTRY_HEADERS, [['口服液类', '材料成本占比', '55%', '62%', '68%', '64.6%', '参考']])
    meta = {'format': 'csv', 'filename': '行业成本基准数据_2026.csv', 'business_metadata': {'evidence_role': 'document_basis'}}
    _, _, parsed = next(table_chunks(legacy, meta))
    assert parsed['table_row']['schema_version'] == 1
    assert 'schema_identifier' not in parsed['table_row'] and 'reference_profile' not in parsed
    assert parsed['business_metadata'] == {'evidence_role': 'benchmark_reference'}


def test_real_confirmed_release_routes_generic_rows_without_global_profile(tmp_path, profile, monkeypatch):
    from enterprise.knowledge import Repository
    from enterprise.knowledge_release import ControlledSearchEngine, ReleaseRepository
    from enterprise.domain_vocabulary import build_graph_vocabulary
    from enterprise.security import Principal
    principal = Principal('reference-admin', 'Reference reviewer', ('knowledge_admin',), ('*',), ('*',))
    repository = Repository(tmp_path / 'isolated-generic-references', principal=principal)
    fixtures = [
        ('market.csv', GENERIC_MARKET_HEADERS, [
            ['Alloy steel', 'A', 'CNY/kg', '2026-08', '10', 'Exchange'],
            ['Alloy steel', 'A', 'CNY/kg', '2026-09', '11', 'Exchange'],
            ['Alloy steel', 'A', 'CNY/kg', '2026-10', '999', 'Exchange']]),
        ('industry.csv', GENERIC_INDUSTRY_HEADERS, [
            ['Gearboxes', 'Material share', '%', '2026-01-01', '2026-12-31', '50', '60', '70', 'Survey']])]
    for filename, headers, rows in fixtures:
        staged = repository.stage(csv_text(headers, rows).encode(), filename, filename, ['Gearbox'],
            '2026-01-01', '市场参考' if filename == 'market.csv' else '行业基准', principal.user_id,
            scope_factories=list(profile['factories'].values()), principal=principal,
            metadata={'evidence_role': 'document_basis', 'manufacturing_domain_profile': profile,
                      'graph_vocabulary': build_graph_vocabulary(profile),
                      'graph_vocabulary_review': {'reason': 'Isolated domain reference review'}})
        assert not staged['errors']
        repository.commit(staged['stage_id'], principal.user_id, 'Isolated confirmed CSV contract test', principal=principal)
    release = ReleaseRepository(repository=repository).publish(principal=principal, embedding_model_path='')
    assert release['status'] == 'published'
    monkeypatch.setattr('enterprise.domain_profiles.load_domain_profile', lambda *a, **k: pytest.fail('read ambient domain'))
    engine = ControlledSearchEngine(repository=repository, embedding_model_path='')
    allowed = Principal('analyst', 'Authorized analyst', ('analyst',), tuple(profile['factories'].values()), ('Gearbox',))
    selected = []
    for month in ('2026-08', '2026-09'):
        rows, stats = engine.search('Alloy steel', principal=allowed, product='Gearbox', factory='Gear plant A',
            as_of=month + '-01', knowledge_types=['market_prices'], top_k=20)
        assert rows and all(row['meta']['table_row']['columns']['month'] == month for row in rows)
        for row in rows:
            built, reason = build_reference_evidence(row, stats, product='Gearbox', specification='G1', month=month)
            assert reason is None
            selected.append(built)
    reading, = market_reading_rows(selected, 'Gearbox', 'G1', '2026-09')
    assert reading['current_price'] == '11' and reading['previous_price'] == '10'
    assert len(reading['evidence_ids']) == 2
    rows, stats = engine.search('Material share', principal=allowed, product='Gearbox', factory='Gear plant A',
        as_of='2026-09-01', knowledge_types=['industry_benchmark'])
    assert rows
    industry, reason = build_reference_evidence(rows[0], stats, product='Gearbox', specification='G1', month='2026-09')
    assert reason is None and build_canonical_industry_comparison(facts(profile), [industry], profile)['available']
    outsider = Principal('other', 'Other', ('analyst',), tuple(profile['factories'].values()), ('Other product',))
    rows, _ = engine.search('Alloy steel', principal=outsider, product='Other product', factory='Gear plant A',
        as_of='2026-09-01', knowledge_types=['market_prices'])
    assert rows == []  # Profile vocabulary never extends authorization.


def test_full_canonical_service_typed_references_hybrid_and_shared_narrative(tmp_path, monkeypatch):
    """Real repositories/retriever/service; encoder is an explicit offline test double.

    This exercises production routing, not model quality or genuine dense weights.
    All fixture observations are labelled SIMULATION and all writes are temporary.
    """
    import json
    from pathlib import Path
    import socket
    import enterprise.knowledge_release as kr
    from enterprise.analysis_service import report_evidence
    from enterprise.knowledge import Repository
    from enterprise.manufacturing_repository import ManufacturingRepository
    from enterprise.manufacturing_service import ManufacturingService
    from enterprise.security import Principal
    from attribution_gen import _model_context
    from enterprise.benchmark_ai import grouped_model_context

    def forbidden(*args, **kwargs):
        raise AssertionError('No network or provider in offline generic-reference integration')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr('attribution_runtime.run_stage', forbidden)
    monkeypatch.setenv('COST_TENANT_ID', 'default')
    folder = Path(__file__).resolve().parents[1] / 'config/manufacturing_examples/machinery'
    profile = json.loads((folder / 'domain.json').read_text(encoding='utf-8-sig'))
    adapter = json.loads((folder / 'adapter.json').read_text(encoding='utf-8-sig'))
    files = {family: (family + '.csv', (folder / (family + '.csv')).read_bytes())
             for family in ('actual', 'budget', 'materials', 'labor', 'overhead')}
    principal = Principal('generic-integration', 'Offline simulation reviewer',
                          ('system_admin', 'supervisor', 'knowledge_admin'), ('*',), ('*',))
    root = tmp_path / 'canonical-generic-integration'
    repository = ManufacturingRepository(root, principal)
    repository.install_profile(profile, adapter, expected_version=0, reason='Offline SIMULATION fixture')
    stage = repository.stage(profile['id'], files, periods=['2026-05', '2026-06'], expected_revision=0)
    repository.confirm(stage['stage_id'], expected_revision=0, reason='Offline SIMULATION observed fixture')
    service = ManufacturingService(root, principal)
    product = profile['products'][0]
    material = next(row for row in profile['materials'] if row['name'] == product['materials'][0])
    metric = next(row for row in profile['reference_metrics'] if row['calculation'] == 'element_share' and row['element'] == 'material')
    market_rows = [[material['name'], 'SIMULATION grade', 'CNY/' + material['unit'], month, price, 'SIMULATION exchange']
                   for month, price in [('2026-05', '10'), ('2026-06', '12'), ('2026-07', '99999')]]
    industry_rows = [[product['category'], metric['source_name'], '%', '2026-01-01', '2026-12-31',
                      '50', '60', '70', 'SIMULATION industry survey']]
    for name, kind, headers, rows in [('market', 'market_prices', GENERIC_MARKET_HEADERS, market_rows),
                                      ('industry', 'industry_benchmark', GENERIC_INDUSTRY_HEADERS, industry_rows)]:
        pending = service.stage_knowledge(profile['id'], content=csv_text(headers, rows).encode(),
            filename=name + '.csv', title='SIMULATION ' + name, product_ids=[product['id']],
            effective_from='2026-01-01', category=kind, reason='SIMULATION generic-reference review')
        assert not pending['errors']
        service.commit_knowledge(profile['id'], stage_id=pending['stage_id'], reason='SIMULATION confirmed reference')

    model_path = tmp_path / 'explicit-offline-encoder-test-double'
    model_path.mkdir()
    (model_path / 'config.json').write_text('{"test_double":true}', encoding='utf-8')
    class OfflineEncoderTestDouble:
        def encode(self, texts, batch_size):
            return {'dense_vecs': [[1.0, .5] for _ in texts]}
    monkeypatch.setattr(kr, '_load_local_bge', lambda path: OfflineEncoderTestDouble())
    knowledge = Repository(root, principal=principal)
    released = kr.ReleaseRepository(repository=knowledge).publish(principal=principal,
        embedding_model_path=model_path, require_embeddings=True)
    assert released['status'] == 'published'
    engine = kr.ControlledSearchEngine(repository=knowledge, embedding_model_path=model_path)
    engine.warmup(principal=principal, wait=True)
    monkeypatch.setattr(kr, 'get_search_engine', lambda **kwargs: engine)
    monkeypatch.setattr('enterprise.domain_profiles.load_domain_profile', lambda *a, **k: pytest.fail('ambient profile'))
    selected = report_evidence(principal, product['name'], product['specification'], ['2026-05', '2026-06'], root,
                               domain_profile=profile, require_hybrid=True)
    assert not selected.diagnostics['degraded'], [(q['purpose'], q['stats'].get('reason'),
        q['stats'].get('degradation_reasons')) for q in selected.diagnostics['queries']]
    assert {s['kind'] for s in selected} == {'market_reference', 'industry_reference'}
    assert all(s['retrieval_framework']['name'] == 'langchain-core' for s in selected)
    for source in selected:
        assert source['elements'] == source['reference_metadata']['elements']
        assert reference_source_reason(source, product['name'], product['specification'], source['scope']['months'][0], profile=profile) is None
    result = service.analyze(profile['id'], product=product['name'], specification=product['specification'],
                             month='2026-06', use_llm=False)
    assert not result['retrieval_diagnostics']['degraded']
    assert result['industry_comparison']['available']
    reference, = result['market_reference']
    assert reference['previous_price'] == '10' and reference['current_price'] == '12'
    assert len(reference['evidence_ids']) == 2
    attribution = result['analyses']['attribution']
    section = next(row for row in attribution['narrative']['sections'] if row['element'] == '材料')
    assert '参考价由10' in section['text'] and '变为12' in section['text']
    assert set(reference['evidence_ids']) <= set(section['evidence_ids'])
    def scalar_values(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from scalar_values(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from scalar_values(item)
        else:
            yield value
    # Exact future cell, not a substring in a benign cosine 0.9999999999998.
    assert not any(value in ('99999', 99999, '2026-07') for value in scalar_values(attribution['narrative']))
    assert attribution['narrative']['industry_comparisons']
    assert 'P50' in attribution['narrative']['industry_comparisons'][0]['text']
    assert '60' in attribution['narrative']['industry_comparisons'][0]['text']
    context = _model_context(attribution['payload'], attribution['sources'])
    market_context = [row for row in context['tasks_by_element']['材料']['references'] if row['kind'] == 'market_reference']
    assert market_context and market_context[0]['market_direction'] == '上涨'
    assert set(market_context[0]['evidence_ids']) == set(reference['evidence_ids'])
    benchmark = result['analyses']['benchmark']
    benchmark_section = next(row for row in benchmark['narrative']['sections'] if row['element'] == '材料')
    assert set(reference['evidence_ids']) <= set(benchmark_section['evidence_ids'])
    assert len(section['market_references']) == len(benchmark_section['market_references']) == 2
    assert len(attribution['narrative']['industry_comparisons']) == len(benchmark['narrative']['industry_comparisons']) == 2
    assert not any(value in ('99999', 99999, '2026-07') for value in scalar_values(benchmark['narrative']))
    grouped = grouped_model_context(benchmark['payload'], benchmark['sources'])
    assert any(row['kind'] == 'market_reference' for row in grouped['tasks_by_element']['材料']['references'])
    assert result['effects'] == {'task_created': False, 'task_sent': False}
    assert all(not item['used_llm'] for item in result['analyses'].values())
