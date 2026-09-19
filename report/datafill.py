# -*- coding: utf-8 -*-
"""Report numbers from the same merged tables and amount contract as module 2.

The DOCX template labels some unit-cost cells as '金额(元/盒)'; those legacy
placeholder names retain their displayed unit. Contributions always use amounts.
"""
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import re

import pandas as pd

from paths import DATA_DIR
from dashboard.data_layer import load_merged_tables, _amount_contract

THEMES = ["月度成本分析", "季度成本分析", "专题分析"]
PRODUCT_SHORT = {"银黄口服液": "YH", "板蓝根颗粒": "BLG", "六味地黄胶囊": "LW"}
COST_COLS = {"材料": "直接材料(元/盒)", "人工": "直接人工(元/盒)", "制费": "制造费用(元/盒)",
             "单位成本": "单位成本(元/盒)", "总成本": "总成本(元)", "产量": "产量(盒)"}
BUDGET_COLS = {"材料": "预算直接材料(元/盒)", "人工": "预算直接人工(元/盒)", "制费": "预算制造费用(元/盒)",
               "单位成本": "预算单位成本(元/盒)", "总成本": "预算总成本(元)", "产量": "预算产量(盒)"}
MFG_NAMES = {"折旧费": "折旧", "动力费(水电气)": "动力", "人工(间接)": "间接人工", "检验费": "检验", "其他制造费用": "其他"}
ZERO = Decimal("0")
CENT = Decimal("0.01")


def load_data():
    """One merged read; retain legacy cost26_2 alias for report consumers."""
    data = load_merged_tables()
    data["cost26_2"] = data.get("erchang26", pd.DataFrame())
    market = DATA_DIR / "药材市场价格行情_2026年上半年.csv"
    data["market"] = pd.read_csv(market) if market.exists() else pd.DataFrame()
    if market.exists():
        from hashlib import sha256
        frame = data["market"]
        frame["_source_file"] = str(market.resolve())
        frame["_source_hash"] = sha256(market.read_bytes()).hexdigest()
        frame["_source_row"] = list(range(2, len(frame) + 2))
        frame["_source_sheet"] = None
        frame.attrs.update(source_file=str(market.resolve()), year=2026)
    return data


def _dec(value, field="数值"):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{field}不是有效数字") from None
    if not result.is_finite() or result < ZERO:
        raise ValueError(f"{field}缺失、非有限或为负")
    return result


def _num(value, places=2):
    if value is None:
        return "—"
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def _mom_pct(cur, prev):
    return None if cur is None or prev is None or prev == 0 else (cur-prev)/prev*100


def _fmt(value, suffix=""):
    return "—" if value is None else f"{Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP):.2f}{suffix}"


def get_valid_months(d=None, product=None):
    data = load_data() if d is None else d
    frame = data.get("cost26")
    if frame is None or frame.empty or not {"工厂", "月份", "产品名称"}.issubset(frame.columns):
        return []
    rows = frame[frame["工厂"] == "中药一厂"]
    if product is not None:
        rows = rows[rows["产品名称"] == product]
    return sorted(str(value) for value in rows["月份"].dropna().unique() if re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", str(value)))


def get_valid_products(d=None):
    data = load_data() if d is None else d
    frame = data.get("cost26")
    if frame is None or frame.empty or not {"工厂", "产品名称"}.issubset(frame.columns):
        return []
    return sorted(str(value) for value in frame.loc[frame["工厂"] == "中药一厂", "产品名称"].dropna().unique())


def product_specification(data, product, specification=None):
    frame = data.get("cost26", pd.DataFrame())
    if not {"工厂", "产品名称", "产品规格"}.issubset(frame.columns):
        raise ValueError("成本表缺少工厂、产品或规格字段")
    rows = frame[(frame["工厂"] == "中药一厂") & (frame["产品名称"] == product)]
    if rows.empty:
        raise ValueError(f"产品不存在：{product}")
    if rows["产品规格"].isna().any() or any(not str(value).strip() for value in rows["产品规格"]):
        raise ValueError("产品规格缺失，不能生成报告")
    specs = rows["产品规格"].astype(str).unique()
    if specification is not None:
        if not isinstance(specification, str) or not specification.strip() or specification not in specs:
            raise ValueError("所选产品规格不存在，禁止替代或合并生成")
        return specification
    if len(specs) != 1:
        raise ValueError(f"{product}存在多种规格，当前报告未提供规格选择，禁止合并生成")
    return specs[0]


def resolve_period(theme, month):
    """Full calendar quarter (not a rolling three-month window), including year rollover."""
    if theme not in THEMES:
        raise ValueError("分析主题不合法")
    if not isinstance(month, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise ValueError("月份必须为YYYY-MM")
    period = pd.Period(month, freq="M")
    if theme == "季度成本分析":
        start = period - ((period.month - 1) % 3)
        months = [str(start + n) for n in range(3)]
        prev = [str(start - 3 + n) for n in range(3)]
        yoy = [str(start - 12 + n) for n in range(3)]
        return months, prev, yoy, f"{period.year}年第{(period.month-1)//3+1}季度"
    return [str(period)], [str(period-1)], [str(period-12)], f"{period.year}年{period.month}月"


def scoped_rows(frame, product, specification, months=None, factory="中药一厂", extra_key=None, complete=False):
    if frame is None or frame.empty:
        return None
    keys = ["工厂", "产品名称", "产品规格", "月份"]
    if not set(keys).issubset(frame.columns):
        raise ValueError("数据表缺少工厂、产品、规格或月份字段，禁止模糊匹配")
    rows = frame[(frame["工厂"] == factory) & (frame["产品名称"] == product) & (frame["产品规格"] == specification)]
    if months is not None:
        rows = rows[rows["月份"].isin(months)]
    if rows.empty:
        return None
    if extra_key:
        if extra_key not in rows:
            raise ValueError(f"明细缺少{extra_key}字段")
        keys.append(extra_key)
    if rows.duplicated(keys).any():
        raise ValueError("相同工厂、产品、规格、月份存在重复记录，需先确认有效版本")
    if complete and months is not None and set(rows["月份"].astype(str)) != set(months):
        return None
    return rows.sort_values("月份")


def validate_params(params, d=None):
    data = load_data() if d is None else d
    errors = []
    product, month, theme = params.get("product"), params.get("month"), params.get("theme")
    try:
        months, _, _, _ = resolve_period(theme, month)
        spec = product_specification(data, product, params.get("specification"))
        rows = scoped_rows(data.get("cost26"), product, spec, months, complete=True)
        if rows is None:
            errors.append("本期数据不完整：需同品同规格覆盖 " + "、".join(months))
    except ValueError as exc:
        errors.append(str(exc))
    return not errors, errors


def _period_values(rows, cols):
    if rows is None:
        return None
    volume = total = ZERO
    amounts = {element: ZERO for element in ("材料", "人工", "制费")}
    source_units = None
    for _, row in rows.iterrows():
        values = {key: _dec(row.get(column), column) for key, column in cols.items()}
        if sum((values[key] for key in amounts), ZERO) != values["单位成本"]:
            raise ValueError("三要素之和与单位成本不闭合，禁止生成报告")
        if abs(values["单位成本"] * values["产量"] - values["总成本"]) > CENT:
            raise ValueError("单位成本×产量与总成本不闭合，禁止生成报告")
        volume += values["产量"]
        total += values["总成本"]
        for key in amounts:
            amounts[key] += values[key] * values["产量"]
        source_units = values
    values = {"产量": volume, "总成本": total, "amounts": amounts}
    for key in ["单位成本", "材料", "人工", "制费"]:
        # A single-month unit cost remains defined in its source, even at zero output.
        values[key] = source_units[key] if len(rows) == 1 else ((total if key == "单位成本" else amounts[key])/volume if volume else None)
    return values


def _period_amount(cur_rows, prev_rows, cur, prev, month, is_q):
    if not is_q:
        return _amount_contract(cur_rows.iloc[0], prev_rows.iloc[0] if prev_rows is not None else None,
                                note="缺少连续上月数据" if prev_rows is None else None)
    contract = {"month": month, "上月总成本": _num(prev["总成本"]) if prev else None,
                "本月总成本": _num(cur["总成本"]), "总变动额": None, "勾稽差额": None,
                "贡献度": dict.fromkeys(("材料", "人工", "制费")),
                **{key+"变动额": None for key in ("材料", "人工", "制费")}}
    if prev is None:
        contract["_note"] = "缺少完整上季度数据"
        return contract
    total_delta = cur["总成本"] - prev["总成本"]
    changes = {key: cur["amounts"][key] - prev["amounts"][key] for key in cur["amounts"]}
    contract.update({"总变动额": _num(total_delta), "勾稽差额": _num(total_delta-sum(changes.values(), ZERO)),
                     **{key+"变动额": _num(value) for key, value in changes.items()},
                     "贡献度": {key: _num(value/total_delta*100) if total_delta else None for key, value in changes.items()}})
    if not total_delta:
        contract["_note"] = "总成本变动额为零，贡献度无定义"
    return contract


def _labor_values(rows):
    if rows is None:
        return dict.fromkeys(("人工单位成本", "工时", "时薪", "效率"))
    sums = {key: ZERO for key in ("volume", "amount", "hours", "persondays")}
    for _, row in rows.iterrows():
        sums["volume"] += _dec(row.get("产量(盒)"), "人工产量")
        sums["amount"] += _dec(row.get("直接人工总额(元)"), "人工总额")
        sums["hours"] += _dec(row.get("总工时(小时)"), "人工工时")
        sums["persondays"] += _dec(row.get("生产人数(人)"), "生产人数") * _dec(row.get("工作天数(天)"), "工作天数")
    q, a, h, days = (sums[key] for key in ("volume", "amount", "hours", "persondays"))
    return {"人工单位成本": a/q if q else None, "工时": h/q*10000 if q else None,
            "时薪": a/h if h else None, "效率": q/days if days else None}


def build_mapping(product, month, theme="月度成本分析", d=None, specification=None):
    """Numbers are deterministic; current, previous, YoY and budget use their own rows."""
    data = load_data() if d is None else d
    ok, errors = validate_params({"product": product, "month": month, "theme": theme, "specification": specification}, data)
    if not ok:
        raise ValueError("；".join(errors))
    months, prev_months, yoy_months, label = resolve_period(theme, month)
    spec = product_specification(data, product, specification)
    history = pd.concat([data.get("cost26", pd.DataFrame()), data.get("cost25", pd.DataFrame())], ignore_index=True)
    cur_rows = scoped_rows(data.get("cost26"), product, spec, months, complete=True)
    prev_rows = scoped_rows(history, product, spec, prev_months, complete=True)
    yoy_rows = scoped_rows(history, product, spec, yoy_months, complete=True)
    budget_rows = scoped_rows(data.get("budget"), product, spec, months, complete=True)
    with localcontext() as ctx:
        ctx.prec = 40
        cur = _period_values(cur_rows, COST_COLS)
        prev = _period_values(prev_rows, COST_COLS)
        yoy = _period_values(yoy_rows, COST_COLS)
        budget = _period_values(budget_rows, BUDGET_COLS)
        is_q = theme == "季度成本分析"
        contract = _period_amount(cur_rows, prev_rows, cur, prev, month, is_q)
        out = {"报告标题": f"{label}{product}成本分析报告", "报告类型": theme,
               "编制日期": date.today().isoformat(), "分析月份": label if is_q else month,
               "产品名称": product, "产品规格": spec, "报告编号": f"CB-{month.replace('-', '')}-{PRODUCT_SHORT.get(product,'XX')}",
               "_amount_change": contract, "_months": months, "_prev_months": prev_months,
               "_yoy_months": yoy_months, "_period_label": label, "_warnings": [],
               "_source_files": sorted({str(path) for frame in (cur_rows, prev_rows, yoy_rows, budget_rows) if frame is not None and '_source_file' in frame for path in frame['_source_file'].dropna()})}
        if prev is None:
            out["_warnings"].append("缺少完整可比前期，环比及金额贡献度不计算")
        for key, field in [("产量", "产量"), ("单位成本", "单位成本"), ("总成本", "总成本"),
                           ("材料成本", "材料"), ("人工成本", "人工"), ("制造费用", "制费")]:
            c, p = cur[field], prev[field] if prev else None
            y, b = yoy[field] if yoy else None, budget[field] if budget else None
            for prefix, value in (("本月", c), ("上月", p), ("去年", y), ("预算", b)):
                out[prefix+key] = _num(value)
            out[key+"环比"] = _fmt(_mom_pct(c,p), "%")
            out[key+"同比"] = _fmt(_mom_pct(c,y), "%")
            short = {"材料成本":"材料", "人工成本":"人工"}.get(key,key)
            out[short+"预算偏差"] = _fmt(_mom_pct(c,b), "%")
        out["去年同月产量"] = out.pop("去年产量")
        for key, name in (("材料","材料"),("人工","人工"),("制费","制造费用")):
            c, p = cur[key], prev[key] if prev else None
            out[name+"金额"] = _num(c)  # Template cell unit is explicitly 元/盒.
            out[name+"总金额"] = _num(cur['amounts'][key])
            out[name+"变动额"] = contract.get(key+"变动额")
            out[name+"占比"] = _fmt(c/cur['单位成本']*100 if c is not None and cur['单位成本'] else None, "%")
            out[name+"环比"] = _fmt(_mom_pct(c,p), "%")
            out[name+"贡献度"] = _fmt(contract['贡献度'][key], "%")
        out["单位成本"] = _num(cur['单位成本'])
        out["总环比"] = _fmt(_mom_pct(cur['单位成本'], prev['单位成本'] if prev else None), "%")
        out["总成本变动额"] = contract.get('总变动额')
        labor = {}
        for span, prefix in ((months, "本月"),(prev_months,"上月")):
            rows = scoped_rows(data.get('labor'),product,spec,span,complete=True)
            values = _labor_values(rows)
            labor[prefix] = values
            for key, value in values.items():
                out[prefix+key] = _num(value, 1 if key in ('工时','效率') else 2)
        for key in ('工时','时薪','效率'):
            out[key+'环比'] = _fmt(_mom_pct(labor['本月'][key],labor['上月'][key]),'%')
        out['人工单位成本'] = out.pop('本月人工单位成本')
        mfg_values = {}
        for span,prefix,cost in ((months,'本月',cur),(prev_months,'上月',prev)):
            rows = scoped_rows(data.get('mfg'),product,spec,span,extra_key='费用类别')
            values = {}
            for category,key in MFG_NAMES.items():
                group = rows[rows['费用类别']==category] if rows is not None else None
                if group is None or set(group['月份']) != set(span) or not cost or not cost['产量']:
                    value = None
                else:
                    amount = sum((_dec(v,'费用总额') for v in group['费用总额(元)']),ZERO)
                    value = amount/cost['产量']
                values[key] = value
                out[prefix+key] = _num(value)
            mfg_values[prefix] = values
        for category,key in MFG_NAMES.items():
            c,p = mfg_values['本月'][key],mfg_values['上月'][key]
            rate = _mom_pct(c,p)
            out[key+'环比'] = _fmt(rate,'%')
            out[key+'变动说明'] = '缺少可比前期或前期为零' if rate is None else f"单位{category}{'上升' if rate>0 else '下降' if rate<0 else '持平'}{abs(rate):.2f}%"
        totals = {prefix: sum(values.values(),ZERO) if all(v is not None for v in values.values()) else None for prefix,values in mfg_values.items()}
        out['制造费用合计'],out['上月制造费用合计'] = _num(totals['本月']),_num(totals['上月'])
        out['制造费用合计环比'] = _fmt(_mom_pct(totals['本月'],totals['上月']),'%')
        alerts = []
        for key in ('材料','人工','制费','单位成本'):
            rate = _mom_pct(cur[key],prev[key] if prev else None)
            if rate is not None and abs(rate)>10:
                alerts.append(f"{key}{'环季' if is_q else '环比'}{rate:+.2f}%（超过±10%，需重点分析）")
        out['波动告警描述'] = '；'.join(alerts) if alerts else ('缺少完整可比前期，无法判断环比阈值。' if prev is None else '可计算的成本要素变化率未严格超过±10%；零基期不计算变化率。')
        return out
