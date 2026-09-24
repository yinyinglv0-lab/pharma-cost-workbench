"""Same-product, specification and period factory benchmarking.

All comparisons use unit cost or the home factory's volume. They are accounting
comparisons, not verified efficiency savings or proof of operational causes.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import re

import pandas as pd

from dashboard.data_layer import load_merged_tables
from enterprise.numeric import format_number, percentage_fields

FACTORIES = {"cost25": "中药一厂", "cost26": "中药一厂",
             "erchang25": "中药二厂", "erchang26": "中药二厂"}
ELEMENTS = {
    "材料": "直接材料(元/盒)",
    "人工": "直接人工(元/盒)",
    "制费": "制造费用(元/盒)",
}
IDENTITY = ["工厂", "产品名称", "产品规格", "月份"]
CENT = Decimal("0.01")
ZERO = Decimal("0")


class BenchmarkError(ValueError):
    pass


def _number(value, field):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise BenchmarkError(f"{field}不是有效数字") from None
    if not number.is_finite() or number < ZERO:
        raise BenchmarkError(f"{field}缺失、非有限或为负")
    return number


def _display(value, precision=CENT):
    return None if value is None else float(value.quantize(precision, rounding=ROUND_HALF_UP))


def _rate(change, base):
    return change / base * 100 if base else None


def _source(table, row):
    """Keep explicit physical lines separate from importer record ordinals.

    A DataFrame index is neither a source line nor a record number. In particular,
    CSV records may span physical lines and Excel records belong to a sheet.
    """
    def text(*names):
        for name in names:
            value = row.get(name)
            if value is not None and pd.notna(value) and str(value).strip():
                return str(value)
        return None

    def position(*names):
        for name in names:
            try:
                number = Decimal(str(row.get(name)))
                if number.is_finite() and number > 0 and number == int(number):
                    return int(number)
            except (InvalidOperation, TypeError, ValueError, OverflowError):
                pass
        return None

    file = text("_source_file", "source_file")
    return {
        "table": table, "file": file,
        "line": position("_source_line", "source_line") if file else None,
        "record_number": position("_source_row", "source_record_number") if file else None,
        "sheet": text("_source_sheet", "source_sheet") if file else None,
        "sha256": text("_source_hash", "source_hash"),
        "key": {key: str(row[key]) for key in [*IDENTITY, "原材料名称", "费用类别"] if key in row and pd.notna(row[key])},
        "fields": {key: str(row[key]) for key in [*ELEMENTS.values(), "单位成本(元/盒)", "产量(盒)", "总成本(元)", "单位消耗成本(元/盒)", "原材料总成本(元)", "单位费用(元/盒)", "费用总额(元)", "直接人工总额(元)", "总工时(小时)", "生产人数(人)", "工作天数(天)"] if key in row and pd.notna(row[key])},
        "note": "源文件与业务主键；记录序号不等于物理行号" if file else "合并数据表与业务主键；未提供原始文件路径",
    }


def benchmark_options(tables=None):
    """Union of scope keys, so missing peers remain visible instead of silently hidden."""
    tables = load_merged_tables() if tables is None else tables
    options = set()
    for table, factory in FACTORIES.items():
        frame = tables.get(table)
        if frame is None or frame.empty or not set(IDENTITY).issubset(frame.columns):
            continue
        for _, row in frame.iterrows():
            if row["工厂"] != factory or any(pd.isna(row[k]) or not str(row[k]).strip() for k in IDENTITY):
                continue
            product, specification, month = (str(row[k]) for k in ("产品名称", "产品规格", "月份"))
            if re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
                options.add((product, specification, month))
    return [{"product": product, "specification": specification, "month": month}
            for product, specification, month in sorted(options)]


def _record(tables, table, product, specification, month):
    factory = FACTORIES[table]
    frame = tables.get(table)
    if frame is None or frame.empty:
        raise BenchmarkError(f"缺少{factory}当期成本数据，禁止跨厂比较")
    if not set(IDENTITY).issubset(frame.columns):
        raise BenchmarkError(f"{factory}缺少工厂、产品、规格或月份字段")
    rows = frame[(frame["工厂"] == factory) & (frame["产品名称"] == product)
                 & (frame["产品规格"] == specification) & (frame["月份"] == month)]
    if rows.empty:
        raise BenchmarkError(f"缺少{factory} {month} {product} / {specification}同口径记录，禁止替代比较")
    if len(rows) != 1:
        raise BenchmarkError(f"{factory}同产品同规格同月存在重复记录，需先消除版本歧义")
    row = rows.iloc[0]
    values = {name: _number(row.get(column), f"{factory}/{column}") for name, column in ELEMENTS.items()}
    unit = _number(row.get("单位成本(元/盒)"), f"{factory}/单位成本")
    volume = _number(row.get("产量(盒)"), f"{factory}/产量")
    total = _number(row.get("总成本(元)"), f"{factory}/总成本")
    if sum(values.values(), ZERO) != unit:
        raise BenchmarkError(f"{factory}三要素之和与单位成本不闭合")
    if abs(total - unit * volume) > CENT:
        raise BenchmarkError(f"{factory}单位成本×产量与总成本不闭合")
    return {"factory": factory, "unit_cost": unit, "volume": volume, "total_cost": total,
            "elements": values, "source": _source(table, row)}


def _factory_record(tables, factory, product, specification, month):
    """Resolve the requested period across all confirmed summary partitions.

    Table suffixes are legacy storage names, not a licence to replace a missing
    year. Duplicates across partitions are as ambiguous as duplicates in one table.
    """
    matches = []
    for table, owner in FACTORIES.items():
        if owner != factory:
            continue
        frame = tables.get(table)
        if frame is None or frame.empty:
            continue
        if not set(IDENTITY).issubset(frame.columns):
            raise BenchmarkError(f"{factory}/{table}缺少工厂、产品、规格或月份字段")
        rows = frame[(frame['工厂'] == factory) & (frame['产品名称'] == product)
                     & (frame['产品规格'] == specification) & (frame['月份'] == month)]
        if not rows.empty:
            matches.append((table, len(rows)))
    if not matches:
        raise BenchmarkError(f"缺少{factory} {month} {product} / {specification}同口径记录，禁止替代比较")
    if sum(count for _, count in matches) != 1:
        raise BenchmarkError(f"{factory}同产品同规格同月存在重复记录（含跨表），需先消除版本歧义")
    return _record(tables, matches[0][0], product, specification, month)


def _year_over_year(tables, factory, product, specification, month, current):
    """Same-month history is supplementary; absent history cannot block peers."""
    year = int(month[:4])
    if year <= 1:
        return {'available': False, 'reason': '无可用上年同期', 'comparison_month': None}
    previous_month = f'{year-1:04d}-{month[5:]}'
    result = {'available': False, 'comparison_month': previous_month,
              'factory': factory, 'period_basis': '去年同月；不跨缺失月份插值'}
    try:
        previous = _factory_record(tables, factory, product, specification, previous_month)
    except BenchmarkError as exc:
        return {**result, 'reason': str(exc)}
    delta = current['unit_cost'] - previous['unit_cost']
    amount_delta = current['total_cost'] - previous['total_cost']
    volume_effect = (current['volume'] - previous['volume']) * previous['unit_cost']
    unit_effect = delta * current['volume']
    return {**result, 'available': True, 'previous_unit_cost': float(previous['unit_cost']),
            'unit_delta': float(delta), 'unit_delta_exact': str(delta),
            **_percent_columns('unit_yoy_pct', _rate(delta, previous['unit_cost'])),
            'total_delta_exact': str(amount_delta), 'volume_effect_exact': str(volume_effect),
            'unit_effect_exact': str(unit_effect),
            'reconciliation_difference_exact': str(amount_delta-volume_effect-unit_effect),
            'sources': [current['source'], previous['source']],
            'boundary': '单位成本同比与总成本同比不同；本厂汇总不能替代行业文件中的企业统计口径'}


def _home_materials(tables, product, specification, month):
    """A one-sided detail list is a checking lead, never a fabricated peer comparison."""
    frame = tables.get("material")
    if frame is None or frame.empty:
        return [], "一厂原料明细缺失；二厂原料明细未提供，不能进行原料级跨厂归因。"
    required = {*IDENTITY, "原材料名称", "单位消耗成本(元/盒)", "原材料总成本(元)"}
    if not required.issubset(frame.columns):
        return [], "一厂明细缺少关键字段；不能进行原料级跨厂归因。"
    rows = frame[(frame["工厂"] == "中药一厂") & (frame["产品名称"] == product)
                 & (frame["产品规格"] == specification) & (frame["月份"] == month)]
    if rows.empty:
        return [], "一厂同品同规格同月明细缺失；二厂原料明细未提供。"
    if rows["原材料名称"].isna().any() or rows["原材料名称"].duplicated().any():
        return [], "一厂明细存在原料身份缺失或重复，暂停明细核查展示。"
    details = []
    try:
        for _, row in rows.iterrows():
            unit = _number(row["单位消耗成本(元/盒)"], "原料单位消耗成本")
            amount = _number(row["原材料总成本(元)"], "原料金额")
            details.append({"material": str(row["原材料名称"]), "unit_consumption_cost": float(unit),
                            "home_amount": _display(amount), "source": _source("material", row),
                            "peer_unit_consumption_cost": None})
    except BenchmarkError as exc:
        return [], str(exc) + "；未采用无效明细。"
    details.sort(key=lambda item: (-item["home_amount"], item["material"]))
    return details, "仅提供一厂原料明细供核查；二厂无原料明细，不能确认采购价、单耗或收率的跨厂差异。"


def _percent_columns(name, value):
    fields = percentage_fields(value)
    return {name: float(fields['display']) if fields['display'] is not None else None,
            name + '_exact': fields['exact'], name + '_unrounded': fields['value'],
            name + '_display': fields['display']}


def _detail_side(tables, table, factory, product, specification, month, summary):
    """Accept real same-scope records only; peer_* tables use the same strict schema."""
    settings = {'material': ('原材料名称', '单位消耗成本(元/盒)', '原材料总成本(元)'),
                'labor': (None, None, '直接人工总额(元)'),
                'mfg': ('费用类别', '单位费用(元/盒)', '费用总额(元)')}
    name_column, unit_column, amount_column = settings[table]
    selected, notes = {}, []
    keys = (table, 'peer_' + table) if factory == '中药二厂' else (table,)
    for table_key in keys:
        frame = tables.get(table_key)
        if frame is None or frame.empty:
            continue
        required = {*IDENTITY, amount_column} | ({name_column} if name_column else set()) | ({unit_column} if unit_column else {'总工时(小时)'})
        if not required.issubset(frame.columns):
            notes.append(f'{factory}/{table_key}缺少身份或金额字段，未采用其明细')
            continue
        rows = frame[(frame['工厂'] == factory) & (frame['产品名称'] == product)
                     & (frame['产品规格'] == specification) & (frame['月份'] == month)]
        for _, row in rows.iterrows():
            name = str(row.get(name_column, '')).strip() if name_column else '直接人工'
            if not name or name == 'nan' or name in selected:
                return {}, notes + [f'{factory}/{table_key}明细名称缺失或重复，暂停该侧下钻']
            try:
                amount = _number(row[amount_column], amount_column)
                volume = summary['volume']
                if '产量(盒)' in row and _number(row['产量(盒)'], '明细产量') != volume:
                    raise BenchmarkError('明细产量与同厂汇总不一致')
                unit = _number(row[unit_column], unit_column) if unit_column else (amount/volume if volume else None)
                if unit is None or abs(unit*volume-amount) > CENT:
                    raise BenchmarkError('明细金额与同厂产量×单位费用不闭合')
                metrics = {}
                if table == 'labor':
                    hours = _number(row['总工时(小时)'], '总工时')
                    metrics = {'hours': float(hours), 'hours_per_box': float(hours/volume) if volume else None,
                               'cost_per_hour': float(amount/hours) if hours else None,
                               'output_per_hour': float(volume/hours) if hours else None}
                selected[name] = {'name': name, 'factory': factory, 'unit_cost': float(unit),
                                  'unit_cost_exact': str(unit), 'amount': _display(amount), 'amount_exact': str(amount),
                                  'volume': float(volume), 'volume_source_id': 'B001' if factory == '中药一厂' else 'B002',
                                  'source': _source(table_key, row), 'metrics': metrics}
            except BenchmarkError as exc:
                return {}, notes + [f'{factory}/{table_key}：{exc}，暂停该侧下钻']
    return selected, notes


def _paired_drilldown(tables, product, specification, month, home, peer):
    result, sources = {}, []
    for element, table in (('材料', 'material'), ('人工', 'labor'), ('制费', 'mfg')):
        left, left_notes = _detail_side(tables, table, '中药一厂', product, specification, month, home)
        right, right_notes = _detail_side(tables, table, '中药二厂', product, specification, month, peer)
        rows = []
        for name in sorted(set(left) | set(right)):
            sides = (left.get(name), right.get(name))
            refs = []
            for side in sides:
                if side is not None:
                    ident = f'B{len(sources)+6:03d}'
                    side['evidence_id'] = ident
                    refs.append(ident)
                    sources.append({'id': ident, 'kind': 'data_fact', 'elements': [element],
                        'text': f"{side['factory']}{name}归集金额{side['amount_exact']}元，单位费用{side['unit_cost_exact']}元/盒；同厂产量{side['volume']}盒。",
                        'source': side['source']})
            paired = all(side is not None for side in sides)
            gap = Decimal(sides[0]['unit_cost_exact'])-Decimal(sides[1]['unit_cost_exact']) if paired else None
            impact = gap*home['volume'] if paired else None
            rows.append({'name': name, 'element': element, 'home': sides[0], 'peer': sides[1],
                         'status': 'paired' if paired else 'missing_peer' if sides[0] else 'missing_home',
                         'unit_gap': float(gap) if gap is not None else None,
                         'normalized_amount': _display(impact), 'normalized_amount_exact': str(impact) if impact is not None else None,
                         'normalization_volume': float(home['volume']), 'normalization_source_id': 'B001',
                         'evidence_ids': refs,
                         'gap_reason': None if paired else '缺少另一厂同名同产品同规格同月明细；差值保留空值'})
        rows.sort(key=lambda row: (-abs(row['normalized_amount'] or (row['home'] or row['peer'])['amount']), row['name']))
        coverage = {}
        for side, records, summary in (('home', left, home), ('peer', right, peer)):
            covered = sum((Decimal(row['unit_cost_exact']) for row in records.values()), ZERO)
            coverage[side] = {'provided_count': len(records), 'covered_unit_cost': float(covered),
                              'uncovered_unit_cost': float(summary['elements'][element]-covered),
                              'reconciled': bool(records) and covered == summary['elements'][element]}
        paired_total = sum((Decimal(row['normalized_amount_exact']) for row in rows if row['normalized_amount_exact'] is not None), ZERO)
        total = (home['elements'][element]-peer['elements'][element])*home['volume']
        result[element] = {'rows': rows, 'coverage': coverage, 'diagnostics': left_notes+right_notes,
                           'paired_count': sum(row['status'] == 'paired' for row in rows),
                           'paired_normalized_amount': _display(paired_total),
                           'unallocated_normalized_amount': _display(total-paired_total),
                           'complete': all(side['reconciled'] for side in coverage.values()) and all(row['status'] == 'paired' for row in rows),
                           'scope': '同产品、规格、月份与明细名称；标准化金额仅使用一厂产量，实物量价与经营条件尚需原始记录核查'}
    return result, sources


def _suggestions(product, specification, month, elements, material_details):
    names = "、".join(row["material"] for row in material_details[:2]) or "主要原材料"
    checks = {
        "材料": ("采购负责人", f"两厂补齐{names}同等级材料的领料计价、领退料量与批次产出，按相同计价和耗用口径区分价格及实物单耗差异。"),
        "人工": ("生产负责人", "两厂对齐工时范围与工资归集，比较工时/盒及人工费用/工时，并核对外包与加班是否计入同一要素。"),
        "制费": ("财务负责人", "两厂对齐折旧、动力、间接人工及维修归集，核对固定/变动费用划分、分配基数和产能利用情况。"),
    }
    rows = []
    for element in sorted(elements, key=lambda item: abs(item["normalized_amount"]), reverse=True):
        if element["unit_gap"] == 0:
            continue
        role, check = checks[element["element"]]
        rows.append({
            "title": f"{product}跨厂{element['element']}差异核查", "owner_role": role,
            "source": f"模块三 / {product} / {specification} / {month} / {element['element']}",
            "priority": "高" if element["unit_gap"] > 0 else "中",
            "due_date": None, "action": check, "status": "草稿",
            "evidence_ids": ["B001", "B002"], "dispatch_status": "未发送",
        })
    return rows


def build_benchmark(product, specification, month, tables=None):
    """Return a JSON-safe report, blocking any non-comparable or invalid scope."""
    result = {"available": False, "product": product, "specification": specification, "month": month,
              "home_factory": "中药一厂", "peer_factory": "中药二厂", "reason": None,
              "analysis_method": "确定性会计对标", "used_llm": False, "elements": [], "sources": [], "suggestions": []}
    if not isinstance(month, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        return {**result, "reason": "月份必须为YYYY-MM"}
    if not isinstance(product, str) or not product.strip() or not isinstance(specification, str) or not specification.strip():
        return {**result, "reason": "必须明确选择产品与规格，不允许跨规格合并"}
    tables = load_merged_tables() if tables is None else tables
    try:
        with localcontext() as ctx:
            ctx.prec = 40
            home = _factory_record(tables, "中药一厂", product, specification, month)
            peer = _factory_record(tables, "中药二厂", product, specification, month)
            gap = home["unit_cost"] - peer["unit_cost"]
            normalized = gap * home["volume"]
            elements = []
            for element in ELEMENTS:
                difference = home["elements"][element] - peer["elements"][element]
                impact = difference * home["volume"]
                elements.append({
                    "element": element, "home_unit_cost": float(home["elements"][element]),
                    "peer_unit_cost": float(peer["elements"][element]), "unit_gap": float(difference),
                    **_percent_columns('gap_pct', _rate(difference, peer['elements'][element])),
                    "normalized_amount": _display(impact), "normalized_amount_exact": str(impact),
                    **_percent_columns('contribution_pct', _rate(impact, normalized)),
                    **_percent_columns('home_share_pct', _rate(home['elements'][element], home['unit_cost'])),
                    **_percent_columns('peer_share_pct', _rate(peer['elements'][element], peer['unit_cost'])),
                    'evidence_id': f'B{len(elements)+3:03d}',
                })
            check = normalized - sum((Decimal(row["normalized_amount_exact"]) for row in elements), ZERO)
            if check != 0:
                raise BenchmarkError("三要素标准化金额差未闭合，禁止输出对标结论")
            drilldown, detail_sources = _paired_drilldown(tables, product, specification, month, home, peer)
            materials = [{'material': row['name'], 'unit_consumption_cost': row['home']['unit_cost'],
                          'home_amount': row['home']['amount'], 'source': row['home']['source'],
                          'evidence_id': row['home']['evidence_id'],
                          'peer_unit_consumption_cost': row['peer']['unit_cost'] if row['peer'] else None}
                         for row in drilldown['材料']['rows'] if row['home']]
            missing = [label for label, branch in drilldown.items() if not branch['coverage']['peer']['provided_count']]
            detail_note = ('二厂无原料明细；' if '材料' in missing else '') + (
                '二厂缺少' + '、'.join(missing) + '配对明细，已有一厂记录保留展示，二厂对应项及其差值为空。'
                if missing else '两厂已提供明细；配对与覆盖情况按要素展示，未闭合部分单列。')
            direction = "高于" if gap > 0 else "低于" if gap < 0 else "等于"
            largest = max(elements, key=lambda row: abs(row["unit_gap"]))
            overview = (f"{month}同产品同规格下，中药一厂单位成本{direction}中药二厂"
                        + (f"{abs(gap):.2f}元/盒" if gap else "，单位成本无差异")
                        + f"；以一厂产量{home['volume']:,.0f}盒标准化，金额差为{normalized:+,.2f}元。")
            if home["volume"] == 0:
                overview += "本厂产量为零，标准化金额差为零，不代表实际节约或支出。"
            if any(row["unit_gap"] for row in elements):
                overview += f"{largest['element']}是单位差异绝对值最大的要素；该结果不是已核实的效率差或降本成果。"
            else:
                overview += "三要素单位成本均无差异，不据此认定工艺与管理效率完全相同。"
            result.update({
                "available": True, "home": {key: (float(value) if isinstance(value, Decimal) else value) for key, value in home.items() if key not in ("elements", "source")},
                "peer": {key: (float(value) if isinstance(value, Decimal) else value) for key, value in peer.items() if key not in ("elements", "source")},
                "unit_gap": float(gap), **_percent_columns('gap_pct', _rate(gap, peer['unit_cost'])),
                "normalized_amount": _display(normalized), "normalized_amount_exact": str(normalized),
                "reconciliation_difference": float(check),
                "display_rounding_difference": float(Decimal(str(_display(normalized))) - sum((Decimal(str(row["normalized_amount"])) for row in elements), ZERO)),
                "elements": elements, "overview": overview,
                "year_over_year": {
                    'home': _year_over_year(tables, '中药一厂', product, specification, month, home),
                    'peer': _year_over_year(tables, '中药二厂', product, specification, month, peer),
                },
                "sources": [{"id": "B001", **home["source"]}, {"id": "B002", **peer["source"]}],
                "home_materials": materials, "detail_note": detail_note,
                'paired_drilldown': drilldown, 'detail_evidence': detail_sources,
                'contribution_tree': {'name': '单位成本差异', 'normalized_amount': _display(normalized),
                    'normalized_amount_exact': str(normalized), 'normalization_volume': float(home['volume']),
                    'evidence_ids': ['B001', 'B002'],
                    'children': [{**row, 'children': drilldown[row['element']]['rows'],
                                  'unallocated_normalized_amount': drilldown[row['element']]['unallocated_normalized_amount']}
                                 for row in elements]},
                "cause_status": "成本结构及同口径明细差异；实际经营原因需结算、生产与分配记录核查",
                "suggestions": _suggestions(product, specification, month, elements, materials),
                "formula": "单位差=一厂单位成本-二厂单位成本；差异率=单位差/二厂单位成本；标准化金额差=单位差×一厂产量。",
                "limitations": ["不同工厂总成本受各自产量影响，不能直接作为效率差。", "同产品同规格同期是必要条件，不保证质量等级、产能利用与核算分配口径完全一致。", "标准化金额差是同产量会计情景，不是实际节约或已批准目标。"],
            })
    except (BenchmarkError, KeyError, InvalidOperation, OverflowError) as exc:
        result["reason"] = str(exc)
    if result['available']:
        result['overview'] += '[B001] [B002]'
        result['evidence'] = benchmark_evidence(result)
    return result


def benchmark_evidence(report):
    evidence = [{'id': source['id'], 'kind': 'data_fact', 'elements': list(ELEMENTS),
                 'text': '同期同产品同规格成本汇总，仅支持差异定位，不能证明经营原因。',
                 'source': {key: value for key, value in source.items() if key != 'id'}}
                for source in report['sources']]
    for row in report['elements']:
        evidence.append({'id': row['evidence_id'], 'kind': 'data_fact', 'elements': [row['element']],
            'text': f"{row['element']}单位差{row['unit_gap']}元/盒；以一厂产量标准化金额差{row['normalized_amount_exact']}元；不是节约额。",
            'source': {'table': 'benchmark_calculation', 'records': report['sources'],
                       'key': {'产品名称': report['product'], '产品规格': report['specification'], '月份': report['month']}}})
    evidence.extend(report.get('detail_evidence', []))
    return evidence


def build_multi_product_summary(month, tables=None):
    """One comparable row per product/specification, never aggregate unlike units."""
    tables = load_merged_tables() if tables is None else tables
    rows = []
    for scope in benchmark_options(tables):
        if scope["month"] != month:
            continue
        report = build_benchmark(scope["product"], scope["specification"], month, tables)
        rows.append({"产品": scope["product"], "规格": scope["specification"], "月份": month,
                     "可比较": report["available"], "一厂单位成本": report.get("home", {}).get("unit_cost"),
                     "二厂单位成本": report.get("peer", {}).get("unit_cost"), "单位差(元/盒)": report.get("unit_gap"),
                     "差异率(%)": report.get("gap_pct"), "一厂产量标准化差额(元)": report.get("normalized_amount"),
                     "数据状态": "同品同规格同期" if report["available"] else report["reason"]})
    return rows
