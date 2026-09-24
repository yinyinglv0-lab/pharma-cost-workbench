"""Program-written financial findings and scoped, evidence-bound report prose."""
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP, localcontext

from .claims import render_element, render_actions

LABELS = {'材料': '直接材料', '人工': '直接人工', '制费': '制造费用'}


def amount(value, *, signed=False, suffix='', places=2):
    if value is None:
        return '无定义'
    from enterprise.numeric import format_number, format_percent
    return (format_percent(value, signed=signed) if suffix == '%' else format_number(value, places, signed=signed)) + suffix


def unit_amount(value, *, signed=False, minimum_places=2):
    """Retain four significant digits for small nonzero per-box effects."""
    if value is None:
        return '无定义'
    number = Decimal(str(value))
    places = minimum_places
    if number and abs(number) < Decimal(1).scaleb(-minimum_places):
        places = max(minimum_places, 3 - number.copy_abs().adjusted())
    return amount(number, signed=signed, places=places)


def labor_snapshot(rows, months):
    if rows is None or set(rows['月份'].astype(str)) != set(months):
        return None
    sums = {name: sum((Decimal(str(value)) for value in rows[column]), Decimal(0)) for name, column in
            (('hours', '总工时(小时)'), ('volume', '产量(盒)'), ('wages', '直接人工总额(元)'))}
    result = {name: float(value) for name, value in sums.items()}
    result['exact'] = {name: str(value) for name, value in sums.items()}
    result['output_per_hour'] = float(sums['volume'] / sums['hours']) if sums['hours'] else None
    result['hours_per_box'] = float(sums['hours'] / sums['volume']) if sums['volume'] else None
    result['wages_per_hour'] = float(sums['wages'] / sums['hours']) if sums['hours'] else None
    if len(months) == 1 and len(rows) == 1:
        people = Decimal(str(rows.iloc[0]['生产人数(人)']))
        result['headcount'] = float(people)
        result['hours_per_person'] = float(sums['hours'] / people) if people else None
    result['scope'] = '完整期间汇总；每工时人工费用为归集费率，不代表个人工资或岗位费率'
    return result


def labor_decomposition(labor):
    """Exact ordered substitution: hours/box first, allocated wages/hour second."""
    current, previous = labor.get('current'), labor.get('previous')
    if not current or not previous:
        return {'available': False, 'reason': '缺少完整前后期工时与人工费用，不能分解单位工时及归集费率影响。'}
    with localcontext() as context:
        context.prec = 40
        c = {key: Decimal(current.get('exact', {}).get(key, str(current[key]))) for key in ('hours', 'volume', 'wages')}
        p = {key: Decimal(previous.get('exact', {}).get(key, str(previous[key]))) for key in ('hours', 'volume', 'wages')}
        if any(not x for x in (c['hours'], p['hours'], c['volume'], p['volume'])):
            return {'available': False, 'reason': '工时或产量分母为零，单位工时与每工时归集费率影响无定义。'}
        hc, hp = c['hours'] / c['volume'], p['hours'] / p['volume']
        rc, rp = c['wages'] / c['hours'], p['wages'] / p['hours']
        delta = c['wages'] / c['volume'] - p['wages'] / p['volume']
        hours_effect = (hc - hp) * rp
        rate_effect = delta - hours_effect
        # Exact residuals also close for recurring quarterly weighted rates.
        amount_delta = c['wages'] - p['wages'] * c['volume'] / p['volume']
        hours_amount = hours_effect * c['volume']
        total_hours_effect = (c['hours'] - p['hours']) * rp
        total_wage_delta = c['wages'] - p['wages']
        values = {'total_hours_effect': total_hours_effect,
                  'total_rate_effect': total_wage_delta - total_hours_effect, 'total_wage_delta': total_wage_delta,
                  'hours_per_box_current': hc, 'hours_per_box_previous': hp,
                  'allocated_rate_current': rc, 'allocated_rate_previous': rp,
                  'hours_unit_effect': hours_effect, 'rate_unit_effect': rate_effect,
                  'unit_delta': delta, 'hours_amount_effect': hours_amount,
                  'rate_amount_effect': amount_delta - hours_amount, 'amount_delta': amount_delta,
                  'hours_change_pct': (hc / hp - 1) * 100,
                  'rate_change_pct': (rc / rp - 1) * 100 if rp else None}
    return {'available': True, 'method': '先单位工时、后每工时归集人工费用；金额影响按本期产量折算',
            **{key: float(value) if value is not None else None for key, value in values.items()},
            'exact': {key: str(value) if value is not None else None for key, value in values.items()}}


def unchanged(facts, key):
    item = facts['elements'][key]
    if 'period_gaps' in item:
        return bool(item['period_gaps']) and all(Decimal(str(r['unit_gap'])) == 0 for r in item['period_gaps'])
    return (item.get('amount_delta') == 0 and item.get('volume_effect') == 0 and item.get('unit_effect') == 0
            and not any(row.get('amount_delta') or row.get('unit_effect') for row in item.get('details', [])))


def _finding(facts, key):
    item = facts['elements'][key]
    delta = item['amount_delta']
    if delta is None:
        lead = f"{LABELS[key]}当期金额{amount(item['amount'])}元；缺完整可比前期，不作变动归因。"
    elif delta == 0:
        lead = f"{LABELS[key]}金额与前期持平。"
    else:
        lead = f"{LABELS[key]}金额{'增加' if delta > 0 else '减少'}{amount(abs(delta))}元。"
    if delta is not None and not unchanged(facts, key):
        if item['contribution_pct'] is not None:
            lead += f"占总金额变动的{amount(item['contribution_pct'], suffix='%')}。"
        elif facts['amount_change'].get('总变动额') == 0:
            lead += '各要素增减相抵，总净变动为零，金额贡献度无定义。'
        if item.get('volume_effect') is not None and item.get('unit_effect') is not None:
            lead += (f"产量变化带来{amount(item['volume_effect'], signed=True)}元，"
                     f"单位成本变化带来{amount(item['unit_effect'], signed=True)}元。")
    if item['unit_cost'] is None:
        lead += '本期产量为零，单位成本无定义。'
    elif item['previous_unit'] is None:
        lead += f"本期单位成本{amount(item['unit_cost'])}元/盒，前期不可比。"
    else:
        lead += f"单位成本{amount(item['previous_unit'])}→{amount(item['unit_cost'])}元/盒"
        lead += f"，变动{amount(item['change_pct'], suffix='%')}。" if item['change_pct'] is not None else '；前期单位成本为零，变化率无定义。'
    return lead


def _labor_text(facts):
    pack = facts.get('labor', {})
    c, p = pack.get('current'), pack.get('previous')
    if not c:
        return '本期未提供完整工时记录，相关工时与归集费率指标不计算；该资料缺口不阻断已提供成本金额的核对。'
    text = f"已有汇总工时{amount(c['hours'])}小时，每工时产出{amount(c['output_per_hour'])}盒。"
    if p:
        text += f"前期为{amount(p['hours'])}小时、{amount(p['output_per_hour'])}盒/小时。"
    bridge = pack.get('decomposition', {})
    if bridge.get('available'):
        text += (f"按总工时×每工时归集人工费用分解，工时因素{amount(bridge['total_hours_effect'], signed=True)}元，"
                 f"归集费率因素{amount(bridge['total_rate_effect'], signed=True)}元，合计{amount(bridge['total_wage_delta'], signed=True)}元。")
        text += (f"\n单位工时由{amount(bridge['hours_per_box_previous'], places=4)}升至" if bridge['hours_per_box_current'] > bridge['hours_per_box_previous'] else
                 f"\n单位工时由{amount(bridge['hours_per_box_previous'], places=4)}变为")
        text += (f"{amount(bridge['hours_per_box_current'], places=4)}小时/盒（{amount(bridge['hours_change_pct'], signed=True)}%），"
                 f"每工时归集人工费用由{amount(bridge['allocated_rate_previous'])}变为{amount(bridge['allocated_rate_current'])}元/小时"
                 f"（{amount(bridge['rate_change_pct'], signed=True)}%）。按先单位工时、后归集费率的顺序替代，"
                 f"两项分别影响单位人工成本{unit_amount(bridge['hours_unit_effect'], signed=True, minimum_places=6)}、"
                 f"{unit_amount(bridge['rate_unit_effect'], signed=True, minimum_places=6)}元/盒，合计{unit_amount(bridge['unit_delta'], signed=True)}元/盒；"
                 f"按本期产量折算分别为{amount(bridge['hours_amount_effect'], signed=True)}、"
                 f"{amount(bridge['rate_amount_effect'], signed=True)}元，净影响{amount(bridge['amount_delta'], signed=True)}元。")
    if p and c['output_per_hour'] is not None and p['output_per_hour'] is not None:
        from enterprise.analysis_narrative import labor_ratio_boundary
        text += labor_ratio_boundary(p.get('hours'), c.get('hours'), p.get('volume'), c.get('volume'))
    text += '归集费率不等于个人时薪；其变化原因需财务部核对工资计提、分配及岗位班次记录。'
    return text


def _detail_text(facts, key):
    if key == '人工':
        return _labor_text(facts)
    rows = [r for r in facts['elements'][key].get('details', []) if r.get('amount_delta') is not None]
    if not rows:
        return '缺少完整前后期可比明细，相关分项归因不计算；缺失项目不按零计算，已提供成本汇总仍可先行核对。'
    if unchanged(facts, key):
        return '已提供的各分项金额及单位成本均与前期持平，本节无需新增差异原因任务。'
    top = sorted(rows, key=lambda r: (-abs(r['amount_delta']), r['name']))[:3]
    unit_label = '单位消耗成本' if key == '材料' else '单位费用'
    text = '主要金额变动项目为' + '；'.join(
        f"{r['name']}{amount(r['amount_delta'], signed=True)}元，{unit_label}{amount(r['previous_unit'])}→{amount(r['current_unit'])}元/盒"
        for r in top) + '。'
    largest = top[0]
    if largest.get('volume_effect') is not None:
        text += (f"金额变动最大的{largest['name']}，产量与单位成本影响分别为"
                 f"{amount(largest['volume_effect'], signed=True)}、{amount(largest['unit_effect'], signed=True)}元。")
    effects = [r for r in rows if r.get('unit_effect') is not None]
    if effects:
        best = max(effects, key=lambda r: abs(r['unit_effect']))
        text += (f"按单位成本影响绝对值，重点为{best['name']}：{amount(best['unit_effect'], signed=True)}元"
                 f"（单位变化{unit_amount(Decimal(str(best['current_unit'])) - Decimal(str(best['previous_unit'])), signed=True)}元/盒）。")
        if best['name'] != top[0]['name']:
            text += f"因此应同时核对{top[0]['name']}的金额变动与{best['name']}的单位成本变化。"
    if key == '材料':
        text += '现有材料明细未含实际领料结转单价和实物净耗用，尚不能把元/盒的变化归为采购降价或单耗改善；应核对结转计价、领退料及产出凭证。'
    else:
        text += f"{largest['name']}的实际计提及分配原因需核对分项凭证和分配底稿。"
    return text


def period_narrative(facts, generation, evidence, *, product, months,
                     specification='', market_reference=None, industry_comparison=None,
                     return_shared=False):
    current, previous = facts['current'], facts['previous']
    opening = (f"{facts['period_label']}产量{amount(current['volume'])}盒，总成本{amount(current['total_cost'])}元，"
               f"单位成本{amount(current['unit_cost'])}元/盒。")
    if previous:
        delta = facts['amount_change']['总变动额']
        opening += (f"与完整前期相比，总成本变动{amount(delta, signed=True)}元；产量影响{amount(facts['volume_effect'], signed=True)}元，"
                    f"单位成本影响{amount(facts['unit_effect'], signed=True)}元。")
        dominant = max(facts['elements'], key=lambda k: abs(facts['elements'][k]['amount_delta'] or 0))
        if any(row['amount_delta'] for row in facts['elements'].values()):
            opening += f"{LABELS[dominant]}是金额变动绝对值最大的要素。"
        if current['volume'] < previous['volume']:
            opening += '减产带来的支出减少不代表已实现节约。'
    else:
        opening += '缺少完整可比前期，不提供期间变化贡献度与产量/单位成本桥接。'
    if len(months) > 1:
        import re
        quarter = (len(months) == 3 and all(re.fullmatch(r'\d{4}-\d{2}', str(m)) for m in months)
                   and months[0][-2:] in ('01', '04', '07', '10')
                   and months == [f'{months[0][:4]}-{int(months[0][-2:])+step:02d}' for step in range(3)])
        opening += ('季度产量和金额为全部月份之和，单位成本按产量加权；结论覆盖完整季度。' if quarter else
                    '所选期间产量和金额为全部月份之和，单位成本按产量加权；不将自定义期间当作季度环比。')
    opening += ' ' + ' '.join('[' + r + ']' for r in facts['evidence_ids'])
    from .narrative_adapter import build_report_attribution_narrative
    shared = build_report_attribution_narrative(facts, evidence, product=product, specification=specification,
        generation=generation, market_reference=market_reference, industry_comparison=industry_comparison)
    shared_by_element = {row['element']: row for row in shared.get('sections', [])}
    sections, claim_ledger = [], []
    for key, label in LABELS.items():
        item = generation['contract']['elements'][key]
        no_change = unchanged(facts, key)
        mechanism, ledger = render_element(item, evidence, key) if not no_change else ('', [])
        refs = list(dict.fromkeys(facts['elements'][key]['evidence_ids'] +
                                 [ref for claim in item['claims'] for ref in claim['fact_ids'] + claim['knowledge_ids']]))
        shared_section = deepcopy(shared_by_element.get(key, {}))
        text = shared_section.get('text') or _finding(facts, key)
        # Preserve detailed report bridge/precision alongside the exact shared M2
        # sentences, rather than replacing the shared result with a focus card.
        bound_prose = shared_section.get('prose_mode') == 'bound-numeric-prose/1' and bool(shared_section.get('prose'))
        if not bound_prose:
            text += '\n完整期间明细：' + _detail_text(facts, key)
            text += ' ' + ' '.join('[' + r + ']' for r in facts['elements'][key]['evidence_ids'])
        adopted = (any(claim['knowledge_ids'] for claim in item['claims']) and not no_change
                   and generation.get('structured_contract_origin') != 'program_rule')
        reading_ledger = ledger if adopted else []
        if adopted:
            # The shared renderer already prints every accepted hypothesis.
            # Retain the report's located quote presentation without repeating it.
            for line in mechanism.splitlines():
                if line.startswith('《') and line not in text:
                    text += '\n' + line
            for claim in item['claims']:
                if claim['text'] not in text:
                    text += '\n' + claim['text'] + ' ' + ' '.join(
                        '[' + ref + ']' for ref in claim['fact_ids'] + claim['knowledge_ids'])
        # A deterministic knowledge boundary may cite a usable controlled source
        # even when the model is disabled/rejected. Keep a real contiguous quote
        # card for the existing resolver, never a convenient unrelated K ID.
        from .claims import quote_basis
        by_id = {source['id']: source for source in evidence}
        refs = list(dict.fromkeys(refs + shared_section.get('evidence_ids', [])))
        for ref in shared_section.get('evidence_ids', []):
            source = by_id.get(ref, {})
            if source.get('kind') != 'document_basis':
                continue
            selected_quote = shared_section.get('mechanism_evidence') or {}
            exact_quote = selected_quote.get('quote') if selected_quote.get('id') == ref else None
            card = quote_basis(source, key, claim_text=shared_section.get('mechanism_note', ''), exact_quote=exact_quote)
            if card is None:
                raise ValueError('共享叙事知识引用缺少可定位原文：' + ref)
            if any(existing['quote'] == card['quote'] for row in reading_ledger
                   for existing in row.get('quote_cards', []) if existing['evidence_id'] == ref):
                continue
            reading_ledger.append({'claim_id': key + '-shared-boundary-' + ref,
                'text': shared_section.get('mechanism_note') or card['basis_boundary'],
                'fact_ids': list(facts['elements'][key]['evidence_ids'][:1]), 'knowledge_ids': [ref],
                'quote_cards': [card], 'reference_check': 'valid',
                'semantic_support': 'pending_human_review', 'origin': 'program_knowledge_boundary'})
            if card['display_quote'] not in text:
                from pathlib import PureWindowsPath
                name = PureWindowsPath(card['source'].get('file', '受控文档')).name
                text += f'\n《{name}》{card["location"]}记载：“{card["display_quote"]}”[{ref}]。'
        gaps = [] if no_change else list(dict.fromkeys([*shared_section.get('evidence_gaps', []), *item['missing_evidence']]))
        additional_gaps = [gap for gap in gaps if gap not in shared_section.get('evidence_gaps', [])]
        if additional_gaps or (gaps and not bound_prose):
            text += '\n证据缺口（不作为当前核对的完成前提）：' + '；'.join(additional_gaps if bound_prose else gaps) + '。'
        recommendation = render_actions(item['actions'], product, months,
            immediate_action=shared_section.get('immediate_action'),
            accepted_model_recommendation=shared_section.get('accepted_model_recommendation')) if not no_change else ''
        section = {**shared_section, 'element': key, 'title': label, 'text': text,
                   'hypothesis': ' '.join(c['text'] for c in item['claims']), 'claim_type': 'hypothesis',
                   'claims': deepcopy(item['claims']), 'actions': deepcopy(item['actions']) if not no_change else [],
                   'recommendation': recommendation, 'evidence_ids': refs, 'no_difference': no_change,
                   'evidence_gaps': gaps, 'missing_evidence': gaps, 'claim_ledger': reading_ledger,
                   'shared_text': shared_section.get('text', '')}
        sections.append(section)
        claim_ledger.extend(reading_ledger)
    result = (opening, sections, claim_ledger)
    return (*result, shared) if return_shared else result


def operating_observations(facts):
    current, previous = facts['current'], facts['previous']
    lines = []
    if previous and current['unit_cost'] is not None and previous['unit_cost'] is not None:
        delta = current['unit_cost'] - previous['unit_cost']
        lines.append(f"本期单位成本较前期{'降低' if delta < 0 else '增加' if delta > 0 else '持平'}"
                     + (f"{unit_amount(abs(delta))}元/盒。" if delta else '。'))
    c, p = facts.get('labor', {}).get('current'), facts.get('labor', {}).get('previous')
    if c and p and c['output_per_hour'] is not None and p['output_per_hour']:
        change = (c['output_per_hour'] / p['output_per_hour'] - 1) * 100
        lines.append(f"每工时产出由{amount(p['output_per_hour'])}变为{amount(c['output_per_hour'])}盒，变动{amount(change, signed=True)}%。")
        from enterprise.analysis_narrative import labor_ratio_boundary
        lines.append(labor_ratio_boundary(p.get('hours'), c.get('hours'), p.get('volume'), c.get('volume')))
    budget = facts.get('budget')
    if budget and current['unit_cost'] is not None and budget['unit_cost'] is not None:
        gap = current['unit_cost'] - budget['unit_cost']
        lines.append(f"单位成本较预算{'高' if gap > 0 else '低' if gap < 0 else '持平'}"
                     + (f"{unit_amount(abs(gap))}元/盒。" if gap else '。'))
    lines.append('尚无采购实价、实耗及整改效果记录可确认已实现的经营降本收益。')
    return ''.join(lines)


def special_observations(facts, focus, theme):
    available = [r for r in facts['trend'] if r.get('unit_cost') is not None]
    prefix = (f"专题焦点：{LABELS[focus]}。" if theme == '专题分析' else
              '期间专项：按全部所选月份加权比较，并观察期内月份变化。' if len(facts['months']) > 1 else '月度专项：结合已提供月份识别变化。')
    if available:
        low, high = min(available, key=lambda r: r['unit_cost']), max(available, key=lambda r: r['unit_cost'])
        prefix += (f"近六个月已提供记录中，单位成本最低为{low['month']}的{amount(low['unit_cost'])}元/盒，"
                   f"最高为{high['month']}的{amount(high['unit_cost'])}元/盒。")
    item = facts['elements'][focus]
    if unchanged(facts, focus):
        return prefix + f"本期{LABELS[focus]}未发生可比分项差异，不新增原因核查任务。"
    return prefix + f"本次以{LABELS[focus]}的单位成本影响及重点明细为核查顺序，量化结果见3.{list(LABELS).index(focus)+1}，凭证与交付要求集中列于6.3。"


def market_observations(facts):
    rows = facts.get('market', [])
    comparable = [r for r in rows if r.get('previous') is not None]
    if not comparable:
        return '未提供与前期配对的市场参考报价，不能据此说明本期材料价格方向。'
    ordered = sorted(comparable, key=lambda r: -abs(r.get('mom_pct') or 0))[:3]
    text = '期末月相对上月，' + '；'.join(
        f"{r['material']}市场参考价{amount(r['previous'])}→{amount(r['current'])}{r['unit']}（{amount(r['mom_pct'], signed=True)}%）[{r['evidence_id']}]"
        for r in ordered) + '。'
    if len(facts['months']) == 1:
        cost = facts['elements']['材料']
        direction = (cost['unit_cost'] or 0) - (cost['previous_unit'] or 0)
        if cost['previous_unit'] is not None and direction and all((r['current'] - r['previous']) * direction > 0 for r in comparable):
            from enterprise.analysis_narrative import comparison_label
            label = comparison_label(facts)
            text += f'已匹配材料的市场参考价与本厂材料单位成本{label}方向一致。'
    else:
        text += '该报价变化只覆盖期末月，不能替代整个所选期间的采购价格变化。'
    return text + '市场报价不等于实际结转单价；元/kg或元/万粒与元/盒量纲不同，不能据此计算采购价差、实物单耗或收率。'


def knowledge_summary(evidence, ledger, retrieval_diagnostics=None):
    available = {s['id'] for s in evidence if s.get('kind') == 'document_basis'}
    cited = {ref for claim in ledger for ref in claim['knowledge_ids']}
    retrieval = deepcopy(retrieval_diagnostics or {})
    cited_chunks = {source.get('chunk_id') for source in evidence if source['id'] in cited}
    if retrieval:
        retrieval.setdefault('stages', {}).update(cited=len(cited), supported=None)
        for candidate in retrieval.get('candidates', []):
            candidate['cited'] = candidate['chunk_id'] in cited_chunks
            candidate['supported'] = None
        retrieval['support_review_status'] = 'pending_human_review'
    return {'retrieval': retrieval, 'eligible_ids': sorted(available),
            'cited_ids': sorted(cited), 'eligible_not_cited_ids': sorted(available - cited),
            'claims_total': len(ledger), 'claims_with_knowledge': sum(bool(c['knowledge_ids']) for c in ledger),
            'traceable_quotes': sum(len(c['quote_cards']) for c in ledger),
            'claim_ledger': deepcopy(ledger), 'semantic_support_status': 'pending_human_review',
            'note': '引用存在和逐字引文可程序核对；原文是否支持该机制须专业复核，不能自动换算人工评分。'}
