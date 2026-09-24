"""Pure report-to-M2 narrative adapter over a frozen, complete-period snapshot.

No table loads, filesystem reads, retrieval, model calls or source substitution.
References come only from the report's admitted and month-bounded projections;
a quarter is never represented by its last month's costs or market quotation.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, localcontext

LABELS = ('材料', '人工', '制费')
ADAPTER_VERSION = 'report-period-to-grounded-attribution/1.0'


def _difference(after, before):
    if after is None or before is None:
        return None
    return float(Decimal(str(after)) - Decimal(str(before)))


def _percent(after, before):
    if after is None or before in (None, 0):
        return None
    with localcontext() as context:
        context.prec = 60
        return str((Decimal(str(after)) / Decimal(str(before)) - 1) * 100)


def _labor(facts, evidence_ids):
    from attribution_facts import labor_factor_bridge
    pack = facts.get('labor', {})
    current, previous = pack.get('current') or {}, pack.get('previous') or {}
    def value(row, key):
        return row.get('exact', {}).get(key, row.get(key))
    factors = labor_factor_bridge(value(previous, 'wages'), value(current, 'wages'),
        value(previous, 'hours'), value(current, 'hours'),
        value(previous, 'volume'), value(current, 'volume'))
    if evidence_ids:
        factors['evidence_id'] = evidence_ids[-1]
    return factors


def report_attribution_payload(facts, *, product, specification, sources,
                               market_reference=None, industry_comparison=None):
    """Return the shared M2 input without mutating report facts or source IDs.

    Unit costs are the report authority's sum(amount)/sum(volume); effects are
    copied verbatim, not recomputed from a rounded display or a selected month.
    """
    from attribution_decomposition import _driver
    months = list(facts['months'])
    previous_months = list(facts['previous_months'])
    period_label = months[0] if len(months) == 1 else months[0] + '至' + months[-1]
    previous_label = previous_months[0] if len(previous_months) == 1 else previous_months[0] + '至' + previous_months[-1]
    known = {source['id'] for source in sources}
    summary_id = next((ref for ref in facts.get('evidence_ids', []) if ref in known), None)
    current = deepcopy(facts['current'])
    previous = deepcopy(facts.get('previous'))
    current.update(month=period_label, months=months, evidence_id=summary_id)
    if previous is not None:
        previous.update(month=previous_label, months=previous_months, evidence_id=summary_id)
    shared = {'available': True, 'comparable': previous is not None,
              'reason': None if previous else '缺少完整可比前期',
              'product': product, 'specification': specification, 'month': period_label,
              'months': months, 'previous_month': previous_label, 'previous_months': previous_months,
              'period_label': facts['period_label'], 'current': current, 'previous': previous,
              'elements': {}, 'evidence': deepcopy([source for source in sources
                  if source['id'] in {ref for item in facts['elements'].values() for ref in item['evidence_ids']}
                  or source['id'] == summary_id]), 'alerts': [],
              'amount_delta': facts['amount_change'].get('总变动额')}
    decomposition = {}
    for key in LABELS:
        item = facts['elements'][key]
        details = []
        for row in item.get('details', []):
            ids = [ref for ref in row.get('evidence_ids', []) if ref in known]
            details.append({'name': row['name'], 'unit_before': row.get('previous_unit'),
                'unit_after': row.get('current_unit'),
                'unit_delta': _difference(row.get('current_unit'), row.get('previous_unit')),
                'amount_before': row.get('previous_amount'), 'amount_after': row.get('current_amount'),
                'amount_delta': row.get('amount_delta'), 'volume_effect': row.get('volume_effect'),
                'unit_effect': row.get('unit_effect'), 'evidence_id': ids[0] if ids else None,
                'evidence_ids': ids, 'unit_semantics': '单位消耗成本（元/盒），不是采购单价' if key == '材料' else '单位制造费用（元/盒）',
                'status': 'complete' if row.get('current_complete') and row.get('previous_complete') else
                          'missing_previous' if not row.get('previous_complete') else 'missing_current'})
        ids = [ref for ref in item.get('evidence_ids', []) if ref in known]
        unit = {'unit_before': item.get('previous_unit'), 'unit_after': item.get('unit_cost'),
                'unit_delta': _difference(item.get('unit_cost'), item.get('previous_unit')),
                'mom_pct': item.get('change_pct'),
                'mom_pct_decimal': _percent(item.get('unit_cost'), item.get('previous_unit')),
                'amount_before': previous['elements'][key]['amount'] if previous else None,
                'amount_after': item['amount'], 'amount_delta': item.get('amount_delta'),
                'contribution': item.get('contribution_pct'), 'volume_effect': item.get('volume_effect'),
                'unit_effect': item.get('unit_effect'), 'alert': item.get('alert', False),
                'detail': details, 'evidence_ids': ids}
        if key == '人工':
            unit['labor_factors'] = _labor(facts, ids)
            labor = facts.get('labor', {})
            if labor.get('current') or labor.get('previous'):
                c, p = labor.get('current') or {}, labor.get('previous') or {}
                unit['detail'] = [{'name': '直接人工', 'unit_before': item.get('previous_unit') if p else None,
                    'unit_after': item.get('unit_cost') if c else None, 'unit_delta': unit['unit_delta'],
                    'amount_before': p.get('wages'), 'amount_after': c.get('wages'),
                    'amount_delta': item.get('amount_delta') if c and p else None,
                    'volume_effect': item.get('volume_effect') if c and p else None,
                    'unit_effect': item.get('unit_effect') if c and p else None,
                    'evidence_id': ids[-1] if ids else None, 'evidence_ids': ids[-1:],
                    'status': 'complete' if c and p else 'missing_previous' if not p else 'missing_current',
                    'unit_semantics': '人工总额/产量（元/盒），不是员工时薪'}]
        shared['elements'][key] = unit
        left = Decimal(str(item['volume_effect'])) if item.get('volume_effect') is not None else None
        right = Decimal(str(item['unit_effect'])) if item.get('unit_effect') is not None else None
        driver = _driver(left, right, 'output', 'unit_cost')
        decomposition[key] = {'change_amount': item.get('amount_delta'),
            'contribution_pct': item.get('contribution_pct'), 'output_effect': item.get('volume_effect'),
            'unit_cost_effect': item.get('unit_effect'), 'dominant_driver': driver['dominant'],
            'driver_relationship': driver, 'analysis_level': 'detailed', 'top_materials': [],
            'price_effect': None, 'usage_effect': None,
            'decomposition_basis': '完整期间产量与单位成本金额桥接；不将市场参考价视为采购实价'}
        if unit['alert']:
            shared['alerts'].append({'要素': key, '环比%': unit['mom_pct'], 'element': key,
                'mom_pct': unit['mom_pct'], 'threshold': 10, 'comparison': 'strict_abs_gt'})
    market = deepcopy(market_reference or {'available': False, 'rows': []})
    # Monthly reference scenarios are still not actual price or physical usage.
    # For a quarter preserve every month's quotation instead of inventing a
    # weighted quarterly procurement price from unweighted external references.
    if len(months) == 1:
        for row in market.get('rows', []):
            if row.get('month') != months[0] or row.get('evidence_id') not in known:
                continue
            p0, p1 = row.get('previous_price'), row.get('current_price')
            detail = next((item for item in shared['elements']['材料']['detail'] if item['name'] == row.get('material')), None)
            if p0 is None or p1 is None or p0 <= 0 or p1 <= 0:
                continue
            material = {'name': row['material'], 'month': row['month'], 'price_unit': row['unit'],
                'reference_price_before': p0, 'reference_price_after': p1,
                'price_change_pct': row.get('month_change_pct'),
                'price_change_pct_exact': row.get('month_change_pct_exact') or _percent(p1, p0),
                'reference_evidence_id': row['evidence_id'],
                'evidence_id': detail.get('evidence_id') if detail else None}
            # These optional scenarios retain their exact legacy semantics; no
            # yield or actual physical measure is inferred from a price quotient.
            if (detail and row['unit'] == '元/kg' and detail['status'] == 'complete'
                    and detail['unit_before'] is not None and detail['unit_after'] is not None):
                with localcontext() as context:
                    context.prec = 60
                    c0, c1 = Decimal(str(detail['unit_before'])), Decimal(str(detail['unit_after']))
                    before, after = Decimal(str(p0)), Decimal(str(p1))
                    material.update(reference_usage_before=float(c0 / before),
                        reference_usage_after=float(c1 / after), usage_unit='kg/盒（参考折算）',
                        usage_change_pct=float(((c1 / after) / (c0 / before) - 1) * 100) if c0 else None)
            decomposition['材料']['top_materials'].append(material)
    return {'adapter_version': ADAPTER_VERSION, 'product': product, 'specification': specification,
        'month': period_label, 'months': months, 'previous_months': previous_months,
        'period_label': facts['period_label'], 'period_basis': 'complete_period_weighted',
        'facts': shared, 'elements': decomposition, '金额口径': deepcopy(facts['amount_change']),
        '告警_环比超正负10%': deepcopy(shared['alerts']),
        'market_reference': market, 'industry_comparison': deepcopy(industry_comparison),
        '数据限制': '期间金额与产量均覆盖全部所选月，单位成本为金额合计除以产量合计；市场参考不证明采购价、实耗或收率。'}


def build_report_attribution_narrative(facts, sources, *, product, specification,
                                       generation=None, market_reference=None, industry_comparison=None):
    """Compile once with the very same deterministic entrypoint used by M2."""
    from enterprise.analysis_narrative import build_attribution_narrative
    payload = report_attribution_payload(facts, product=product, specification=specification,
        sources=sources, market_reference=market_reference, industry_comparison=industry_comparison)
    if (generation or {}).get('prose_mode') == 'bound-numeric-prose/1':
        payload['prose_mode'] = 'bound-numeric-prose/1'
    result = build_attribution_narrative(payload, report_model_explanations(generation or {}), sources,
        detailed=True, industry_comparison=industry_comparison)
    # Industry authority supplies independent month/category mappings. Render
    # each with its actual observed scope rather than inventing a quarter P50.
    from enterprise.analysis_narrative import render_industry_comparisons
    comparisons = []
    for period in (industry_comparison or {}).get('periods', []):
        projected = deepcopy(period)
        for row in projected.get('rows', []):
            row['element'] = {'material_share': '材料', 'labor_share': '人工', 'mfg_share': '制费',
                              'manufacturing_share': '制费', 'overhead_share': '制费'}.get(row.get('metric_id'))
            for side in ('home', 'peer'):
                row[side]['evidence_ids'] = list(row.get('observation_evidence_ids', []))
        # Every row has already passed the real profile's category mapping.
        for category in dict.fromkeys(row['category'] for row in projected.get('rows', [])):
            comparisons.extend(render_industry_comparisons({**projected, 'category': category}, sources, context={
                'period': period['month'], 'months': [period['month']],
                'product': product, 'specification': specification}))
    result['industry_comparisons'] = comparisons
    for section in result['sections']:
        relevant = [row for row in comparisons if row.get('element') == section['element'] and row.get('side') == 'home']
        section['industry_comparisons'] = relevant
        if relevant:
            reading = '\n'.join(row['text'] for row in relevant)
            section['text'] += '\n行业结构参考（非原因证据）：' + reading
            section['evidence_ids'] = list(dict.fromkeys([*section['evidence_ids'],
                *[ref for row in relevant for ref in row['evidence_ids']]]))
    result['text'] = '\n\n'.join([result['overview'], *[row['text'] for row in result['sections']],
        *[row['text'] for row in comparisons if row.get('element') not in LABELS or row.get('side') != 'home'],
        result['followup_criteria']])
    result['evidence_ids'] = list(dict.fromkeys([*result['evidence_ids'],
        *[ref for row in comparisons for ref in row['evidence_ids']]]))
    source_index = {row['id']: row for row in sources}
    def provenance(refs):
        return [{'id': ref, 'kind': source_index[ref].get('kind'),
                 'evidence_role': source_index[ref].get('evidence_role', source_index[ref].get('kind')),
                 'source': deepcopy(source_index[ref].get('source')), 'scope': deepcopy(source_index[ref].get('scope'))}
                for ref in refs if ref in source_index]
    result['sources'] = [deepcopy(source_index[ref]) for ref in result['evidence_ids'] if ref in source_index]
    result['provenance'] = provenance(result['evidence_ids'])
    for section in result['sections']:
        section['provenance'] = provenance(section['evidence_ids'])
    result['input'] = payload
    return result


def report_model_explanations(generation):
    """Translate only an already validated, adopted report model candidate.

    Program fallbacks stay distinct from adopted model wording. Missing evidence
    remains a separate channel; do not turn required records into prerequisites
    for the immediate calculations over the available report facts.
    """
    if not generation.get('used_llm') or generation.get('generation_status') != 'model_validated':
        return None
    if generation.get('prose_mode') == 'bound-numeric-prose/1':
        return deepcopy(generation.get('prose_explanations'))
    elements = {}
    for key, item in generation['contract']['elements'].items():
        elements[key] = {'hypothesis': ' '.join(claim['text'] for claim in item['claims']),
            'recommendation': '',
            'evidence_ids': list(dict.fromkeys(ref for claim in item['claims']
                for ref in claim['fact_ids'] + claim['knowledge_ids'])),
            'missing_evidence': deepcopy(item['missing_evidence'])}
    return {'elements': elements}
