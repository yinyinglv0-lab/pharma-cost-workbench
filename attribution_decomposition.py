"""Reference-only sequential decomposition, never actual purchase/physical usage facts.

Fixed source: 药材市场价格行情_2026年上半年.csv. Wide monthly columns are
2026 values, not rolling month numbers. Injected frames may set attrs['year'].
No uploaded copies, mtime selection, fuzzy names or unit conversions are used.
"""
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

import pandas as pd
from paths import DATA_DIR

MARKET_FILENAME = '药材市场价格行情_2026年上半年.csv'
ZERO = Decimal('0')
BASIS = '市场参考测算：单耗=单位消耗成本/市场参考价；非实际采购价及实物耗用量'


def _d(value):
    try:
        x = Decimal(str(value))
        return x if x.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _f(value):
    return float(value) if value is not None else None


def _driver(left, right, left_name, right_name):
    """Compare signed effects without calling an offset a joint increase."""
    if left is None or right is None:
        return {'dominant': None, 'direction': 'unavailable', 'relationship': 'unavailable'}
    # Ignore sub-cent arithmetic noise from recurring Decimal reference usage.
    left = left.quantize(Decimal('0.00000001'))
    right = right.quantize(Decimal('0.00000001'))
    total = left + right
    dominant = ('none' if left == right == 0 else 'balanced' if abs(left) == abs(right)
                else left_name if abs(left) > abs(right) else right_name)
    return {'dominant': dominant,
            'direction': 'increase' if total > 0 else 'decrease' if total < 0 else 'unchanged',
            'relationship': 'offset' if left * right < 0 else 'same_direction' if left * right > 0
                            else 'single_factor' if left or right else 'unchanged'}


def _load_market():
    path = Path(DATA_DIR) / MARKET_FILENAME
    try:
        frame = pd.read_csv(path)
    except (OSError, ValueError):
        frame = pd.DataFrame()
    frame.attrs.update(source_file=str(path), year=2026)
    return frame


def _market_pair(frame, name, previous, current):
    required = {'药材名称', '单位'}
    if not required.issubset(frame.columns):
        return None, '市场表缺少药材名称或单位字段'
    rows = frame.loc[frame['药材名称'].eq(name)]  # intentionally exact; no substring substitutions
    if len(rows) != 1:
        return None, '无精确名称匹配' if rows.empty else '同名市场报价不唯一，规格/来源有歧义'
    row = rows.iloc[0]
    if row['单位'] != '元/kg':
        return None, '市场报价单位不匹配，仅接受元/kg，不自动换算'
    year = frame.attrs.get('year', 2026)
    months = (previous, current)
    try:
        if any(int(m[:4]) != int(year) for m in months):
            return None, '市场报价年份不匹配'
        cols = [f'{int(m[5:7])}月价格' for m in months]
    except (ValueError, TypeError):
        return None, '月份格式无效'
    prices = [_d(row.get(col)) for col in cols]
    if any(p is None or p <= 0 for p in prices):
        return None, '缺少两期有效正市场单价'
    sources = [{'file': frame.attrs.get('source_file'),
                'key': {'药材名称': name, '规格等级': str(row.get('规格等级', '未提供')),
                        '月份': month, '价格字段': col, '单位': '元/kg',
                        '价格来源': str(row.get('价格来源', '未提供'))},
                'note': '市场参考价，不能证明本厂实际采购价；未提供原始行号'}
               for month, col in zip(months, cols)]
    return (prices[0], prices[1], sources), None


def build_decomposition(facts, market=None):
    """Return {材料: {...}, 人工: {...}, 制费: {...}}; unavailable facts -> {}.

    Material identity: full-element output_effect + covered price_effect + covered
    usage_effect + unexplained_effect = change_amount. The residual is explicitly
    *not* actual unexplained causality: it is the unallocated unit-cost effect of
    unmatchable items, missing detail or reconciliation differences. Labor/mfg
    price/usage are None; output + unit_cost_effect = change_amount instead.
    """
    if not facts.get('available'):
        return {}
    market = _load_market() if market is None else market
    if not isinstance(market, pd.DataFrame):
        raise TypeError('market must be a pandas DataFrame')
    q0 = _d(facts.get('previous', {}).get('volume'))
    q1 = _d(facts.get('current', {}).get('volume'))
    if q0 is None or q1 is None or q0 < 0 or q1 < 0:
        return {}
    elements = facts.get('elements', {})
    biggest = max((abs(_d(e.get('amount_delta')) or ZERO) for e in elements.values()), default=ZERO)
    gross_movement = sum(abs(_d(e.get('amount_delta')) or ZERO) for e in elements.values())
    result = {}
    with localcontext() as ctx:
        ctx.prec = 40
        for name, element in elements.items():
            change = _d(element.get('amount_delta'))
            output = _d(element.get('volume_effect'))
            unit = _d(element.get('unit_effect'))
            if any(v is None for v in (change, output, unit)):
                continue
            pct = _d(element.get('mom_pct_decimal', element.get('mom_pct')))
            reasons = []
            if pct is not None and abs(pct) >= 5:
                reasons.append('abs_mom_gte_5')
            if biggest and abs(change) == biggest:
                reasons.append('largest_absolute_amount_change')
            if gross_movement and abs(change) / gross_movement >= Decimal('.20'):
                reasons.append('absolute_movement_share_gte_20pct')
            if element.get('alert'):
                reasons.append('threshold_alert')
            if pct is None:
                reasons.append('undefined_mom_requires_review')
            driver = _driver(output, unit, 'output', 'unit_cost')
            entry = {
                'change_amount': _f(change), 'contribution_pct': element.get('contribution'),
                'output_effect': _f(output), 'unit_cost_effect': _f(unit),
                'price_effect': None, 'usage_effect': None, 'top_materials': [],
                'decomposition_basis': '不适用：该要素采用产量与单位成本金额桥接，不套用药材量价分解',
                'dominant_driver': driver['dominant'], 'driver_relationship': driver,
                'price_usage_driver': _driver(None, None, 'price', 'usage'),
                'reconciliation_difference': _f(change-output-unit),
                'unexplained_effect': _f(change-output-unit),
                'analysis_level': 'detailed' if reasons else 'brief', 'detail_reasons': reasons,
                'importance_amount_threshold': None,
                'unmatched_materials': [], 'market_sources': [], 'evidence': [],
            }
            result[name] = entry
            if name != '材料':
                continue
            price_total, usage_total = ZERO, ZERO
            matched = []
            seen = set()
            detail = element.get('detail', [])
            duplicate_names = {x.get('name') for x in detail if sum(y.get('name') == x.get('name') for y in detail) > 1}
            for item in detail:
                material = item.get('name')
                c0, c1 = _d(item.get('unit_before')), _d(item.get('unit_after'))
                reason = None
                pair = None
                if material in duplicate_names:
                    reason = '同名成本明细重复，禁止重复分配'
                elif item.get('status') != 'complete' or c0 is None or c1 is None or c0 < 0 or c1 < 0:
                    reason = '缺少有效两期单位消耗成本'
                elif '元/盒' not in str(item.get('unit_semantics', '')):
                    reason = '材料成本单位未声明为元/盒'
                else:
                    pair, reason = _market_pair(market, material, facts['previous_month'], facts['month'])
                if reason:
                    if material not in seen:
                        unit_effect = q1*(c1-c0) if item.get('status') == 'complete' and c0 is not None and c1 is not None and material not in duplicate_names else None
                        entry['unmatched_materials'].append({'name': material, 'reason': reason,
                                                            'unit_cost_effect':_f(unit_effect),'evidence_id':item.get('evidence_id')})
                        seen.add(material)
                    continue
                p0, p1, sources = pair
                u0, u1 = c0/p0, c1/p1
                output_part = (q1-q0)*c0
                price_part = q1*(p1-p0)*u0
                usage_part = q1*p1*(u1-u0)
                amount = q1*c1-q0*c0
                actual_delta = _d(item.get('amount_delta'))
                if actual_delta is not None and abs(actual_delta-amount) > Decimal('0.01'):
                    entry['unmatched_materials'].append({'name': material, 'reason': '明细金额与单位成本×产量不闭合'})
                    continue
                price_total += price_part
                usage_total += usage_part
                matched.append({
                    'name': material, 'change_amount': _f(amount),
                    'output_effect': _f(output_part), 'price_effect': _f(price_part), 'usage_effect': _f(usage_part),
                    'reference_price_before': _f(p0), 'reference_price_after': _f(p1), 'price_unit': '元/kg',
                    'reference_usage_before': _f(u0), 'reference_usage_after': _f(u1), 'usage_unit': 'kg/盒（参考折算）',
                    'price_change_pct': _f((p1-p0)/p0*100),
                    'usage_change_pct': _f((u1-u0)/u0*100) if u0 else None,
                    'price_usage_driver': _driver(price_part, usage_part, 'price', 'usage'),
                    'decomposition_basis': BASIS,
                    'reconciliation_difference': _f(amount-output_part-price_part-usage_part),
                    'market_sources': sources,
                    'cost_sources': [item.get('source_before'), item.get('source_after')],
                    'evidence_id': item.get('evidence_id'),
                })
            matched.sort(key=lambda x: (-abs(x['change_amount']), x['name']))
            for index, material in enumerate(matched, 1):
                ident = f'M{index:03d}'
                material['reference_evidence_id'] = ident
                entry['market_sources'].extend(material['market_sources'])
                entry['evidence'].append({
                    'id': ident,
                    'text': f"{material['name']}市场参考价{material['reference_price_before']}→{material['reference_price_after']}元/kg，"
                            f"参考价格影响{material['price_effect']}元，参考折算单耗影响{material['usage_effect']}元。{BASIS}",
                    'source': {'table': 'market', 'file': None, 'key': {'药材名称': material['name']},
                               'records': material['market_sources'], 'note': BASIS},
                })
            residual = change-output-price_total-usage_total
            entry.update({
                'price_effect': _f(price_total) if matched else None,
                'usage_effect': _f(usage_total) if matched else None,
                'top_materials': matched, 'decomposition_basis': BASIS if matched else '不可进行参考量价测算：无有效匹配市场单价',
                'price_usage_driver': _driver(price_total if matched else None, usage_total if matched else None, 'price', 'usage'),
                'unexplained_effect': _f(residual),
                'unexplained_effect_note': '未匹配明细的单位成本影响单独列示，其余才是尚未勾稽分配部分；不得归入价格或实际单耗',
                'unallocated_residual': _f(residual-sum(_d(x.get('unit_cost_effect')) or ZERO for x in entry['unmatched_materials'])),
                'reference_usage_increase_names': [x['name'] for x in matched if x['reference_usage_after']>x['reference_usage_before']],
                'reconciliation_difference': _f(change-output-price_total-usage_total-residual),
                'coverage': {'matched_count': len(matched), 'detail_count': len(detail),
                             'scope': '仅精确匹配且有效的明细；产量影响覆盖整个材料要素'},
                'formula': 'output=(Q1-Q0)c0; price=Q1(P1-P0)(c0/P0); usage=Q1P1(c1/P1-c0/P0)',
            })
    return result
