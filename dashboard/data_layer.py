# -*- coding: utf-8 -*-
"""模块二看板数据层（5.2.1）：时间序列提取 + 四项核心指标计算。

- 时间序列：6个月 × 3产品 × 6要素（材料/人工/制费/单位成本/产量/总成本）
- 环比（本月vs上月）、同比（本月vs去年同月）、预算偏差、贡献度
- 贡献度口径（赛题公式）：贡献度 = 该要素变动额 / 总成本变动额 × 100%
  变动额为金额口径（要素单位成本×产量）；源表勾稽且总变动非零时贡献度合计约100%。
- 边界安全：除零返回 None 并附告警；超±500% 波动标记预警。
"""
import glob
import os
import sys
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 支持直接运行脚本

import pandas as pd

from paths import DATA_DIR, MANAGED_DIR


def _find_latest(patterns):
    """优先复用根目录原始 CSV；根目录缺失时才回退 docs_upload。

    同一来源目录内仍按修改时间发现滚动数据，避免上传副本静默替换原始表。
    （保留兼容；看板/知识库的真实读取已走 load_merged_tables 合并逻辑。）
    """
    for uploaded in (False, True):
        candidates = []
        for pat in patterns:
            if pat.startswith("docs_upload/") == uploaded:
                candidates.extend(glob.glob(str(DATA_DIR / pat)))
        if candidates:
            return max(candidates, key=lambda path: (os.path.getmtime(path), path))
    return None


def find_cost_file(year):
    """成本汇总文件（按年份通配，取最新）"""
    return _find_latest([f"中药一厂_成本汇总_{year}*.csv",
                         f"docs_upload/中药一厂_成本汇总_{year}*.csv"])


def find_budget_file():
    return _find_latest(["中药一厂_预算数据*.csv", "docs_upload/中药一厂_预算数据*.csv"])


def find_material_file():
    return _find_latest(["中药一厂_原材料消耗明细*.csv",
                         "docs_upload/中药一厂_原材料消耗明细*.csv"])


def find_labor_file():
    return _find_latest(["中药一厂_人工工时明细*.csv",
                         "docs_upload/中药一厂_人工工时明细*.csv"])


def find_mfg_file():
    return _find_latest(["中药一厂_制造费用明细*.csv",
                         "docs_upload/中药一厂_制造费用明细*.csv"])


# ==================== 多文件合并读取（企业月度滚动 + 历史保留） ====================
# Web 上传的成本数据落盘目录（历史文件永不删除，审计可回溯到某年某月）
DATA_UPLOAD_DIR = DATA_DIR / "data_upload"

# 各表匹配模式：根目录 + 上传目录（csv/xlsx 均支持）
_TABLE_PATTERNS = {
    "cost26":    ["中药一厂_成本汇总_2026*.csv", "中药一厂_成本汇总_2026*.xlsx",
                  "data_upload/中药一厂_成本汇总_2026*.csv",
                  "data_upload/中药一厂_成本汇总_2026*.xlsx"],
    "cost25":    ["中药一厂_成本汇总_2025*.csv", "中药一厂_成本汇总_2025*.xlsx",
                  "data_upload/中药一厂_成本汇总_2025*.csv",
                  "data_upload/中药一厂_成本汇总_2025*.xlsx"],
    "budget":    ["中药一厂_预算数据*.csv", "中药一厂_预算数据*.xlsx",
                  "data_upload/中药一厂_预算数据*.csv",
                  "data_upload/中药一厂_预算数据*.xlsx"],
    "material":  ["中药一厂_原材料消耗明细*.csv", "中药一厂_原材料消耗明细*.xlsx",
                  "data_upload/中药一厂_原材料消耗明细*.csv",
                  "data_upload/中药一厂_原材料消耗明细*.xlsx"],
    "labor":     ["中药一厂_人工工时明细*.csv", "中药一厂_人工工时明细*.xlsx",
                  "data_upload/中药一厂_人工工时明细*.csv",
                  "data_upload/中药一厂_人工工时明细*.xlsx"],
    "mfg":       ["中药一厂_制造费用明细*.csv", "中药一厂_制造费用明细*.xlsx",
                  "data_upload/中药一厂_制造费用明细*.csv",
                  "data_upload/中药一厂_制造费用明细*.xlsx"],
    "erchang26": ["中药二厂_成本汇总_2026*.csv", "中药二厂_成本汇总_2026*.xlsx",
                  "data_upload/中药二厂_成本汇总_2026*.csv",
                  "data_upload/中药二厂_成本汇总_2026*.xlsx"],
    "erchang25": ["中药二厂_成本汇总_2025*.csv", "中药二厂_成本汇总_2025*.xlsx",
                  "data_upload/中药二厂_成本汇总_2025*.csv",
                  "data_upload/中药二厂_成本汇总_2025*.xlsx"],
}
# 同一表中"一行"的身份键：同键多版本时 mtime 较新的文件行胜出
_TABLE_DEDUPE = {
    "cost26": ["产品名称", "月份"], "cost25": ["产品名称", "月份"],
    "budget": ["产品名称", "月份"], "material": ["产品名称", "月份", "原材料名称"],
    "labor": ["产品名称", "月份"], "mfg": ["产品名称", "月份", "费用类别"],
    "erchang26": ["产品名称", "月份"], "erchang25": ["产品名称", "月份"],
}
# 无任何匹配文件时的兜底文件名（赛方原始数据包）
_FALLBACK_NAMES = {
    "cost26": "中药一厂_成本汇总_2026年1-6月.csv",
    "cost25": "中药一厂_成本汇总_2025年1-6月.csv",
    "budget": "中药一厂_预算数据_2026年.csv",
    "material": "中药一厂_原材料消耗明细_2026年1-6月.csv",
    "labor": "中药一厂_人工工时明细_2026年1-6月.csv",
    "mfg": "中药一厂_制造费用明细_2026年1-6月.csv",
    "erchang26": "中药二厂_成本汇总_2026年1-6月.csv",
    "erchang25": "中药二厂_成本汇总_2025年1-6月.csv",
}


def _find_all(patterns):
    """全部匹配文件（根目录 + data_upload/），按 mtime 升序返回（去重时新者胜）。"""
    candidates = []
    for pat in patterns:
        candidates.extend(glob.glob(str(DATA_DIR / pat)))
    return sorted(set(os.path.abspath(p) for p in candidates),
                  key=lambda p: (os.path.getmtime(p), p))


def _read_table(path):
    if str(path).lower().endswith('.xlsx'):
        with pd.ExcelFile(path) as workbook:
            sheet_name = workbook.sheet_names[0]
            frame = pd.read_excel(workbook, sheet_name=sheet_name)
    else:
        sheet_name = 'CSV'
        frame = pd.read_csv(path)
    # Retain the winning source file through merging; a DataFrame index is not a physical CSV line.
    frame['_source_file'] = str(Path(path).resolve())
    from hashlib import sha256
    frame['_source_hash'] = sha256(Path(path).read_bytes()).hexdigest()
    frame['_source_row'] = list(range(2, len(frame) + 2))
    frame['_source_sheet'] = sheet_name
    return frame


def load_legacy_tables(fallback=True):
    """合并读取全部匹配文件（根目录 + data_upload/）。

    同一身份键（如 产品×月份）出现多次时，mtime 较新的文件行覆盖较旧——
    同时支持"覆盖同名文件更新数值"与"新文件追加新月份"两种企业滚动方式；
    历史文件永不删除，审计/同比/近6月趋势都可回溯。
    返回 {table_key: DataFrame}，无数据时为空 DataFrame。
    """
    d = {}
    for key, patterns in _TABLE_PATTERNS.items():
        paths = _find_all(patterns)
        if not paths and fallback:
            fb = _FALLBACK_NAMES.get(key)
            if fb and (DATA_DIR / fb).exists():
                paths = [str(DATA_DIR / fb)]
        if not paths:
            d[key] = pd.DataFrame()
            continue
        frames = [_read_table(p) for p in paths]
        df = pd.concat(frames, ignore_index=True)
        identity = [c for c in ('工厂', '产品规格') if c in df.columns] + _TABLE_DEDUPE[key]
        d[key] = df.drop_duplicates(subset=identity, keep="last")
    return d


def load_merged_tables(fallback=True):
    """Confirmed managed revision wins atomically; before first commit use original sources."""
    from enterprise.cost_imports import active_tables
    active = active_tables()
    return active if active is not None else load_legacy_tables(fallback)


def load_cost_tables_full():
    """8 张成本表合并结果（一厂6 + 二厂2），供 cost_analyzer / anomaly 共用。
    键名与 cost_analyzer 历史键一致。"""
    d = load_merged_tables()
    return {"yichang_2026": d["cost26"], "yichang_2025": d["cost25"],
            "erchang_2026": d["erchang26"], "erchang_2025": d["erchang25"],
            "labor": d["labor"], "budget": d["budget"],
            "material": d["material"], "mfg": d["mfg"]}


def cost_src_files():
    """全部参与成本计算/知识生成的数据文件路径（供哈希指纹与变更检测监视）。"""
    files = _find_all([p for pats in _TABLE_PATTERNS.values() for p in pats])
    managed = MANAGED_DIR / 'cost_versions.db'
    if managed.exists():
        files.append(str(managed))
    return files

ELEMENTS = ["直接材料(元/盒)", "直接人工(元/盒)", "制造费用(元/盒)",
            "单位成本(元/盒)", "产量(盒)", "总成本(元)"]
ELEMENT_LABELS = {"直接材料(元/盒)": "材料", "直接人工(元/盒)": "人工",
                  "制造费用(元/盒)": "制费", "单位成本(元/盒)": "单位成本",
                  "产量(盒)": "产量", "总成本(元)": "总成本"}
CONTRIB_ELEMENTS = ["直接材料(元/盒)", "直接人工(元/盒)", "制造费用(元/盒)"]


def load_cost_data():
    """读取一厂 6 张成本表（合并根目录 + data_upload/ 全部匹配文件）。

    同身份键（产品×月份）mtime 新者胜；历史文件永不删除，同比/近6月趋势可回溯。
    原材料明细的元/盒字段表示单位消耗成本，不能作为采购单价；人工表为
    总额/工时/人数/天数，制费表为分项费用。二厂当期成本与市场行情是原始
    八表中的对标、外部参考数据，不参与一厂金额分解或直接证明经营原因。
    """
    d = load_merged_tables()
    return {k: d[k].loc[d[k]['工厂'].eq('中药一厂')].copy() if not d[k].empty and '工厂' in d[k] else d[k]
            for k in ('cost26', 'cost25', 'budget', 'material', 'labor', 'mfg')}


def discover_products(d=None):
    """产品列表：从成本汇总 CSV 动态发现（3/4/5 种自动适应，不写死）。"""
    if d is None:
        d = load_cost_data()
    df = d.get("cost26")
    if df is None or df.empty:
        return []
    return sorted(df["产品名称"].unique())


def discover_months(d=None):
    """月份窗口：从成本汇总 CSV 动态发现（月度滚动自动平移，不写死 1-6 月）。"""
    if d is None:
        d = load_cost_data()
    df = d.get("cost26")
    if df is None or df.empty:
        return []
    return sorted(df["月份"].unique())


def _scope_product_tables(d, product, specification=None):
    """Select one product/specification before any comparison or drill-down.

    Missing specification columns are accepted only for legacy inputs whose
    primary cost table also has no specification. An explicitly scoped product
    must never borrow an unlabelled or different-spec counterpart.
    """
    primary = d.get('cost26')
    candidates = primary if primary is not None else next(
        (frame for frame in d.values() if frame is not None and not frame.empty), pd.DataFrame())
    if '产品名称' in candidates:
        candidates = candidates.loc[candidates['产品名称'].eq(product)]
    else:
        candidates = candidates.iloc[0:0]
    has_spec = '产品规格' in candidates
    values = candidates['产品规格'] if has_spec else pd.Series(dtype=object)
    valid = values.notna() & values.astype(str).str.strip().ne('')
    choices = list(pd.unique(values.loc[valid]))
    if specification is None:
        if len(choices) > 1:
            raise ValueError(f'{product}存在多个产品规格，请显式提供 specification；不得混算')
        if has_spec and not candidates.empty and not valid.all():
            raise ValueError(f'{product}存在未标明产品规格的成本数据，无法确定分析口径')
        selected = choices[0] if choices else None
    else:
        if not isinstance(specification, str) or not specification.strip():
            raise ValueError('specification须为非空产品规格')
        if not has_spec or specification not in choices:
            raise ValueError(f'{product}的产品规格 {specification} 无当期成本数据')
        selected = specification
    scoped = {}
    for key, frame in d.items():
        if frame is None:
            scoped[key] = pd.DataFrame()
            continue
        if frame.empty or '产品名称' not in frame:
            scoped[key] = frame.iloc[0:0].copy()
            continue
        mask = frame['产品名称'].eq(product)
        if selected is not None:
            mask &= frame['产品规格'].eq(selected) if '产品规格' in frame else False
        elif '产品规格' in frame:
            # An unlabelled legacy primary cannot prove a labelled counterpart
            # applies to it, even when that counterpart happens to occur first.
            mask &= frame['产品规格'].isna() | frame['产品规格'].astype(str).str.strip().eq('')
        scoped[key] = frame.loc[mask].copy()
    return scoped, selected


def _row_source(row):
    """Keep physical source identity; never turn a DataFrame index into a row."""
    fields = ('工厂', '产品名称', '产品规格', '月份', '_source_file', '_source_hash',
              '_source_row', '_source_sheet', '_source_revision', '_source_version')
    result = {}
    for key in fields:
        if key in row and pd.notna(row[key]):
            value = row[key]
            result[key] = value.item() if hasattr(value, 'item') else value
    return result


def material_detail_series(product, d=None, *, specification=None):
    """原材料消耗逐月序列（5.2.3 归因下钻到原料级的输入）。"""
    if d is None:
        d = load_cost_data()
    d, _ = _scope_product_tables(d, product, specification)
    df = d.get("material")
    if df is None or df.empty or "产品名称" not in df.columns:
        return []
    sub = df[df["产品名称"] == product].sort_values("月份")
    series = []
    for month, grp in sub.groupby("月份"):
        series.append({"month": month,
                       "materials": [{"name": r["原材料名称"],
                                      "unit_cost": r["单位消耗成本(元/盒)"],
                                      "total_cost": r["原材料总成本(元)"],
                                      "pct": r["占总材料成本比例"], "source": _row_source(r)}
                                     for _, r in grp.iterrows()]})
    return series


def labor_metrics_series(product, d=None, *, specification=None):
    """人工工时/时薪/效率逐月序列。"""
    if d is None:
        d = load_cost_data()
    d, _ = _scope_product_tables(d, product, specification)
    df = d.get("labor")
    if df is None or df.empty or "产品名称" not in df.columns:
        return []
    sub = df[df["产品名称"] == product].sort_values("月份")
    series = []
    for _, r in sub.iterrows():
        series.append({"month": r["月份"],
                       "labor_total": r["直接人工总额(元)"],
                       "hours": r["总工时(小时)"],
                       "headcount": r["生产人数(人)"],
                       "workdays": r["工作天数(天)"],
                       "labor_per_box": round(r["直接人工总额(元)"] / r["产量(盒)"], 6),
                       "wage_rate": round(r["直接人工总额(元)"] / r["总工时(小时)"], 6),
                       "efficiency": round(r["产量(盒)"] / (r["生产人数(人)"] * r["工作天数(天)"]), 6),
                       "source": _row_source(r)})
    return series


def mfg_breakdown_series(product, d=None, *, specification=None):
    """制造费用五类逐月序列。"""
    if d is None:
        d = load_cost_data()
    d, _ = _scope_product_tables(d, product, specification)
    df = d.get("mfg")
    if df is None or df.empty or "产品名称" not in df.columns:
        return []
    sub = df[df["产品名称"] == product].sort_values("月份")
    series = []
    for month, grp in sub.groupby("月份"):
        series.append({"month": month,
                       "items": [{"category": r["费用类别"],
                                  "unit_cost": r["单位费用(元/盒)"],
                                  "total_cost": r["费用总额(元)"], "source": _row_source(r)}
                                 for _, r in grp.iterrows()]})
    return series


def _mom(cur, prev):
    """环比：除零安全（prev<=0 返回 None，不抛异常、不产出 inf）。"""
    if prev is None or prev == 0 or pd.isna(prev) or pd.isna(cur):
        return None
    return (cur - prev) / prev * 100


def _decimal(value):
    """从十进制文本恢复金额，禁止将二进制 float 直接交给 Decimal。"""
    try:
        value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value if value.is_finite() else None


def _rounded(value, places=2):
    """API 保持 JSON 可用的 float；计算和四舍五入均在 Decimal 内完成。"""
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def _previous_month_note(current, previous):
    if previous is None:
        return "首月无上月"
    try:
        cy, cm = map(int, str(current["月份"]).split("-"))
        py, pm = map(int, str(previous["月份"]).split("-"))
        if 1 <= cm <= 12 and 1 <= pm <= 12 and cy * 12 + cm - py * 12 - pm == 1:
            return None
    except (TypeError, ValueError):
        pass
    return "缺少连续上月数据，无法计算环比及金额贡献度"


def _amount_contract(current, previous, note=None):
    """唯一金额计算入口：本期要素单位成本×本期产量－上期同项金额。

    贡献度的分母保留源表总成本变动，不用三要素合计替代，勾稽差额显式披露。
    raw 字段供精度校验；两位显示字段供图表、表格、摘要共同使用。
    """
    labels = [ELEMENT_LABELS[c] for c in CONTRIB_ELEMENTS]
    result = {"month": current["月份"], "上月总成本": None,
              **{f"{label}变动额": None for label in labels},
              "本月总成本": None, "总变动额": None,
              "贡献度": dict.fromkeys(labels), "贡献度_raw": dict.fromkeys(labels),
              "产量(盒)": float(current["产量(盒)"]), "勾稽差额": None}
    if note:
        result["_note"] = note
        return result
    cols = [*CONTRIB_ELEMENTS, "产量(盒)", "总成本(元)"]
    cur = {c: _decimal(current[c]) for c in cols}
    prev = {c: _decimal(previous[c]) for c in cols}
    if any(v is None for v in (*cur.values(), *prev.values())):
        result["_note"] = "金额源数据缺失或非有限数，无法计算贡献度"
        return result
    steps = {ELEMENT_LABELS[c]: cur[c] * cur["产量(盒)"] - prev[c] * prev["产量(盒)"]
             for c in CONTRIB_ELEMENTS}
    total = cur["总成本(元)"] - prev["总成本(元)"]
    result.update({"上月总成本": _rounded(prev["总成本(元)"]),
                   "本月总成本": _rounded(cur["总成本(元)"]),
                   "总变动额": _rounded(total),
                   **{f"{label}变动额": _rounded(value) for label, value in steps.items()},
                   "勾稽差额": _rounded(total - sum(steps.values()))})
    if total == 0:
        result["_note"] = "总成本变动额为零，贡献度无定义"
    else:
        ratios = {label: value / total * Decimal(100) for label, value in steps.items()}
        result["贡献度"] = {label: _rounded(value) for label, value in ratios.items()}
        result["贡献度_raw"] = {label: _rounded(value, 6) for label, value in ratios.items()}
    if result["勾稽差额"] != 0:
        result["_note"] = (result.get("_note", "") + "；源表总变动额与三要素金额变动不闭合").lstrip("；")
    return result


def build_attribution_summary(data, month=None):
    """共享确定性金额归因摘要；只描述数值，不推断采购、工艺等经营原因。"""
    rows = data.get("amount_change", [])
    selected = next((r for r in rows if r["month"] == month), None) if month else (rows[-1] if rows else None)
    if selected is None:
        return "所选月份无金额归因数据。"
    prefix = f"{data.get('product', '')} {selected['month']}"
    if selected.get("总变动额") is None:
        return f"{prefix}：{selected.get('_note', '缺少连续上月数据')}，暂不提供金额归因。"
    total = selected["总变动额"]
    direction = "增加" if total > 0 else "减少" if total < 0 else "不变"
    headline = (f"{prefix}：总成本较上月{direction} {abs(total):,.2f} 元。"
                if total else f"{prefix}：总成本较上月不变；总变动额为零，贡献度无定义。")
    parts = []
    for label in ("材料", "人工", "制费"):
        amount = selected[f"{label}变动额"]
        pct = selected["贡献度"][label]
        part = f"{label}金额变动 {amount:+,.2f} 元"
        if pct is not None:
            part += f"（贡献度 {pct:.2f}%）"
        parts.append(part)
    note = f"源表勾稽差额 {selected['勾稽差额']:+,.2f} 元。" if selected.get("勾稽差额") else ""
    return headline + "；".join(parts) + "。" + note + "金额变动包含单位成本及产量共同影响，具体原因待核查。"


def build_dashboard_data(product, d=None, *, specification=None):
    """同产品同规格看板；多规格未选时拒绝，缺对照时不借其它规格。

    Source units remain 元/盒 and 盒; adding products does not establish a
    conversion to pieces, kilograms or another production unit.
    """
    if d is None:
        d = load_cost_data()
    d, specification = _scope_product_tables(d, product, specification)
    df = d["cost26"]
    sub = df[df["产品名称"] == product].sort_values("月份").reset_index(drop=True)
    if sub['月份'].duplicated().any():
        raise ValueError(f'{product}同一规格同月存在多条成本记录，不能按首行或跨工厂混算')
    warnings = []
    source_rows = [{"month": r['月份'], "current": _row_source(r),
                    "previous": (_row_source(sub.iloc[i - 1])
                                 if i and _previous_month_note(r, sub.iloc[i - 1]) is None else None),
                    "yoy": None, "budget": None} for i, r in sub.iterrows()]

    series = []
    for _, r in sub.iterrows():
        series.append({"month": r["月份"],
                       **{ELEMENT_LABELS[c]: r[c] for c in ELEMENTS}})

    # 环比（本月 vs 上月）
    mom = []
    for i, r in sub.iterrows():
        row = {"month": r["月份"]}
        prev = sub.iloc[i - 1] if i else None
        note = _previous_month_note(r, prev)
        if note:
            row.update({ELEMENT_LABELS[c]: None for c in ELEMENTS})
            row["_note"] = note
            if i:
                warnings.append(f"{product} {r['月份']} {note}")
        else:
            for c in ELEMENTS:
                v = _mom(r[c], prev[c])
                row[ELEMENT_LABELS[c]] = round(v, 6) if v is not None else None   # 全精度（0.01% 复算阈值）
                if v is not None and abs(v) > 500:
                    warnings.append(f"{product} {r['月份']} {ELEMENT_LABELS[c]} "
                                    f"环比{v:+.1f}%超±500%，触发极端波动预警")
        mom.append(row)

    # 同比（本月 vs 去年同月；无去年数据时整组 None，不崩溃）
    yoy = []
    df25 = pd.concat([d['cost25'], d['cost26']], ignore_index=True)
    has25 = (not df25.empty) and "产品名称" in df25.columns
    for i, r in sub.iterrows():
        row = {"month": r["月份"]}
        m25 = f"{int(r['月份'][:4]) - 1:04d}{r['月份'][4:]}"
        p25 = df25[(df25["产品名称"] == product) & (df25["月份"] == m25)] if has25 \
            else pd.DataFrame()
        if len(p25) != 1:
            row.update({ELEMENT_LABELS[c]: None for c in ELEMENTS})
            row["_note"] = "去年同月无同规格数据" if p25.empty else "去年同月同规格对照不唯一"
        else:
            r25 = p25.iloc[0]
            source_rows[i]['yoy'] = _row_source(r25)
            for c in ELEMENTS:
                v = _mom(r[c], r25[c])
                row[ELEMENT_LABELS[c]] = round(v, 6) if v is not None else None
        yoy.append(row)

    # 预算偏差（无预算数据时整组 None，不崩溃）
    budget_var = []
    bd = d["budget"]
    has_bd = (not bd.empty) and "产品名称" in bd.columns
    for i, r in sub.iterrows():
        row = {"month": r["月份"]}
        b = bd[(bd["产品名称"] == product) & (bd["月份"] == r["月份"])] if has_bd \
            else pd.DataFrame()
        if len(b) != 1:
            row.update({ELEMENT_LABELS[c]: None for c in ELEMENTS})
            row["_note"] = "同规格预算数据缺失" if b.empty else "同规格预算对照不唯一"
            budget_var.append(row)
            continue
        b = b.iloc[0]
        source_rows[i]['budget'] = _row_source(b)
        bcol = {"直接材料(元/盒)": "预算直接材料(元/盒)",
                "直接人工(元/盒)": "预算直接人工(元/盒)",
                "制造费用(元/盒)": "预算制造费用(元/盒)",
                "单位成本(元/盒)": "预算单位成本(元/盒)",
                "产量(盒)": "预算产量(盒)",
                "总成本(元)": "预算总成本(元)"}
        for c in ELEMENTS:
            v = _mom(r[c], b[bcol[c]])
            row[ELEMENT_LABELS[c]] = round(v, 6) if v is not None else None
        budget_var.append(row)

    # 金额、贡献度及摘要共用唯一计算结果，禁止显示层自行重算贡献度。
    amount_change, contribution, contribution_raw = [], [], []
    for i, r in sub.iterrows():
        prev = sub.iloc[i - 1] if i else None
        amount = _amount_contract(r, prev, _previous_month_note(r, prev))
        amount_change.append(amount)
        for target, field, places in ((contribution, "贡献度", 2),
                                      (contribution_raw, "贡献度_raw", 6)):
            values = amount[field]
            row = {"month": r["月份"], **values}
            row["合计"] = (_rounded(sum(Decimal(str(v)) for v in values.values()), places)
                           if all(v is not None for v in values.values()) else None)
            if "_note" in amount:
                row["_note"] = amount["_note"]
            target.append(row)
        if amount.get("勾稽差额"):
            warnings.append(f"{product} {r['月份']} {amount['_note']}，差额{amount['勾稽差额']:.2f}元")

    return {"product": product, "specification": specification,
            "source_rows": source_rows,
            "quantity_unit": "盒", "unit_cost_unit": "元/盒",
            "quantity_unit_boundary": "仅使用源表盒口径；新增产品不代表支持粒、支、kg等单位或自动换算",
            "series": series, "mom": mom, "yoy": yoy,
            "budget_var": budget_var, "contribution": contribution,
            "contribution_raw": contribution_raw,
            "amount_change": amount_change, "warnings": warnings}


def build_all_dashboard(d=None):
    """全部产品看板数据（3产品）。"""
    if d is None:
        d = load_cost_data()
    products = sorted(d["cost26"]["产品名称"].unique())
    return {p: build_dashboard_data(p, d) for p in products}
#（注：内容由AI生成）
