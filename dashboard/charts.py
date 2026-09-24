# -*- coding: utf-8 -*-
"""ECharts 四图 option 构建（模块二 5.2.2，口径统一版）。

设计原则：
- 数据 100% 来自真实数据：所有数值只从 dashboard.data_layer 读取，本模块零硬编码数值；
- **口径统一（硬性要求）**：瀑布图、贡献度、归因分析全部使用金额口径——
  变动额 = 要素单位成本 × 当月产量（元），贡献度 = 要素金额变动 / 总金额变动 × 100%；
  瀑布图展示金额变动分解（元），绝不展示单位成本变动（元/盒）；
  验证基准：银黄口服液 2026-05 材料 67.09% / 人工 12.31% / 制费 20.60%。
  趋势/结构/热力图展示的是单位成本水平值（元/盒），属数据展示而非变动分解，
  不参与贡献度计算，与金额口径无冲突；
- 配色按职责分配：要素=分类色（固定槽位）、产品=分类色（固定槽位，与要素色区分）、
  瀑布=极性发散色（成本上升红=不利、下降蓝=有利，管理会计惯例）、
  热力=单一蓝色顺序渐变（禁止彩虹）；
- 每图 title.subtext 标注数据来源与时间范围（数据可追溯）；
- 瀑布图 4 series 结构（series[0] 基底透明、series[1] 增加、series[2] 减少、
  series[3] 合计）——透明底座堆叠实现悬空阶梯，正负分离；顺序被测试锁定。
"""
import pandas as pd

from .data_layer import (ELEMENT_LABELS, build_dashboard_data,
                         discover_months, discover_products, load_cost_data)

# ---------------- 设计令牌 ----------------
FONT = 'system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif'
SURFACE = "#ffffff"
INK_PRIMARY = "#202A35"
INK_SECONDARY = "#475569"
INK_MUTED = "#64748B"
GRID = "#E8EDF3"
AXIS = "#CBD5E1"
BORDER = "rgba(11,11,11,0.10)"

# 分类色板：要素身份，固定槽位（材料/人工/制费）
ELEMENT_COLORS = {"材料": "#286AB0", "人工": "#B87526", "制费": "#0F8574"}
# 分类色板：产品身份，固定槽位（与要素色板区分，经 CVD/对比度校验通过）
PRODUCT_COLORS = {"银黄口服液": "#8b5cf6", "板蓝根颗粒": "#0e9aa7", "六味地黄胶囊": "#d97706"}
# 动态发现新产品时的后备固定色序（同序稳定，不按排名换色）
_PRODUCT_COLOR_RESERVE = ["#5d6bc0", "#c94f7c", "#7c8a3d", "#b0611f", "#4a7a9d"]

# 发散色板：成本变动的极性。上升=不利差异=红（暖极），下降=有利差异=蓝（冷极）
POS_COLOR = "#e34948"
NEG_COLOR = "#2a78d6"
TOTAL_COLOR = "#52514e"
# 顺序色板：热力图的量级编码，单一蓝色由浅到深
SEQ_BLUE = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec",
            "#5598e7", "#3987e5", "#256abf", "#1c5cab", "#184f95", "#0d366b"]

# Reserve a stable heading block; plots start below it, including in narrow iframes.
_TITLE = {"left": "center", "top": 12, "itemGap": 8,
          "textStyle": {"fontSize": 16, "fontWeight": 600, "color": INK_PRIMARY,
                        "fontFamily": FONT, "lineHeight": 24, "height": 24,
                        "width": 290, "overflow": "truncate"},
          "subtextStyle": {"fontSize": 11, "color": INK_MUTED,
                           "fontFamily": FONT, "lineHeight": 17, "height": 34,
                           "width": 290, "overflow": "truncate"}}
_TOOLTIP_BASE = {"backgroundColor": "rgba(252,252,251,0.97)",
                 "borderColor": BORDER, "borderWidth": 1, "padding": [8, 12],
                 "textStyle": {"color": INK_PRIMARY, "fontSize": 12,
                               "fontFamily": FONT}}
_AXIS_LABEL = {"color": INK_MUTED, "fontSize": 12, "fontFamily": FONT}
_AXIS_LINE = {"show": True, "lineStyle": {"color": AXIS, "width": 1}}
_SPLIT_LINE = {"show": True, "lineStyle": {"color": GRID, "width": 1, "type": "solid"}}

# 要素中文名 ↔ 数据层字段
_ELEM_FIELDS = {"材料": "材料", "人工": "人工", "制费": "制费"}


def _source_subtext(months):
    """数据来源标注（动态月份范围，数据可追溯）。"""
    if not months:
        return "数据来源: 中药一厂成本汇总 | 无月份"
    return f"数据来源: 中药一厂成本汇总 | {months[0]}~{months[-1]}"


def _calendar_window(months, count=6):
    """返回按日历连续性截取的最近窗口，保留完整 YYYY-MM 标签。"""
    if not months:
        return []
    ordered = sorted(str(m) for m in months)
    end = pd.Period(ordered[-1], freq="M")
    start = max(end - (count - 1), pd.Period(ordered[0], freq="M"))
    return [f"{p.year:04d}-{p.month:02d}" for p in pd.period_range(start, end, freq="M")]


def _cat_axis(data):
    return {"type": "category", "data": data,
            "axisLabel": _AXIS_LABEL, "axisLine": _AXIS_LINE,
            "axisTick": {"show": False}}


def _yaxis(name):
    return {"type": "value", "name": name, "nameTextStyle": {"color": INK_MUTED},
            "axisLabel": _AXIS_LABEL, "axisLine": {"show": False},
            "splitLine": _SPLIT_LINE}


def _fmt_amt(v):
    """金额格式化：千分位 + 正负号（元）。"""
    return f"{v:+,.0f}"


def _product_color(name):
    """产品固定槽位色；动态发现的新产品从后备色序稳定分配（按名称序，不按排名）。"""
    if name in PRODUCT_COLORS:
        return PRODUCT_COLORS[name]
    unknown = sorted(n for n in discover_products() if n not in PRODUCT_COLORS)
    idx = unknown.index(name) if name in unknown else 0
    return _PRODUCT_COLOR_RESERVE[idx % len(_PRODUCT_COLOR_RESERVE)]


# ==================== ① 趋势折线图（近6个月单位成本，按产品分线） ====================
def trend(d: dict, products: list, months: list) -> dict:
    """单位成本趋势：每产品一条线（图例切换），dataZoom 时间范围选择，末点直标。"""
    win = _calendar_window(months)  # 最近六个日历月（缺测不挤入更早月份）
    series = []
    for p in products:
        by_m = {s["month"]: s["单位成本"]
                for s in build_dashboard_data(p, d)["series"]}
        data = []
        for j, m in enumerate(win):
            v = by_m.get(m)
            # 末点直标数值（文本用墨色，不用系列色；系列身份由图例+颜色承担）
            if j == len(win) - 1 and v is not None:
                data.append({"value": v, "label": {
                    "show": True, "position": "top", "distance": 8,
                    "formatter": f"{v:.2f}", "color": INK_SECONDARY,
                    "fontSize": 11, "fontFamily": FONT}})
            else:
                data.append(v)
        series.append({
            "name": p, "type": "line", "data": data,
            "smooth": 0.3, "symbol": "circle", "symbolSize": 7,
            "lineStyle": {"width": 2, "color": _product_color(p),
                          "cap": "round", "join": "round"},
            "itemStyle": {"color": _product_color(p), "borderColor": SURFACE,
                          "borderWidth": 2},
        })
    return {
        "title": {"text": "单位成本趋势",
                  "subtext": _source_subtext(win),
                  **_TITLE},
        "tooltip": {**_TOOLTIP_BASE, "trigger": "axis",
                    "axisPointer": {"type": "line",
                                    "lineStyle": {"color": AXIS, "width": 1,
                                                  "type": "solid"}}},
        "legend": {"type": "scroll", "bottom": 10, "left": "center", "width": "90%", "icon": "circle",
                   "itemWidth": 8, "itemHeight": 8, "itemGap": 18,
                   "textStyle": {"color": INK_SECONDARY, "fontSize": 12,
                                 "fontFamily": FONT}},
        "grid": {"left": 16, "right": 30, "top": 112, "bottom": 58,
                 "containLabel": True},
        "xAxis": _cat_axis(win),
        "yAxis": _yaxis("元/盒"),
        # The full six-month window needs no separate slider below the axis.
        "dataZoom": [{"type": "inside"}],
        "series": series,
    }


# ==================== ② 环形结构图（本月成本构成） ====================
def structure(data: dict, product: str, month: str) -> dict:
    series = data["series"]
    months = [s["month"] for s in series]
    cur = next(s for s in series if s["month"] == month)
    total = cur["单位成本"]           # 与趋势图/热力图同源同字段（一致性测试锁定）
    items = [
        {"name": "直接材料", "value": cur["材料"],
         "itemStyle": {"color": ELEMENT_COLORS["材料"],
                       "borderColor": SURFACE, "borderWidth": 2}},
        {"name": "直接人工", "value": cur["人工"],
         "itemStyle": {"color": ELEMENT_COLORS["人工"],
                       "borderColor": SURFACE, "borderWidth": 2}},
        {"name": "制造费用", "value": cur["制费"],
         "itemStyle": {"color": ELEMENT_COLORS["制费"],
                       "borderColor": SURFACE, "borderWidth": 2}},
    ]
    return {
        "title": {"text": "本月成本构成", 
                  "subtext": f"{month} · {product}\n单位成本 {total:.2f} 元/盒 · 扇区表示构成占比",
                  **_TITLE},
        "tooltip": {**_TOOLTIP_BASE, "trigger": "item",
                    "formatter": "{b}: {c} 元/盒 ({d}%)"},
        "legend": {"type": "scroll", "orient": "horizontal", "left": "center", "bottom": 10, "width": "90%",
                   "icon": "circle", "itemWidth": 8, "itemHeight": 8, "itemGap": 12,
                   "textStyle": {"color": INK_SECONDARY, "fontSize": 12,
                                 "fontFamily": FONT}},
        "series": [{
            "type": "pie", "top": 100, "bottom": 52, "left": 12, "right": 12,
            # Leave room outside narrow sectors for complete, dark percentage labels.
            "radius": ["44%", "70%"], "center": ["50%", "50%"],
            "minShowLabelAngle": 0, "avoidLabelOverlap": True,
            "labelLayout": {"hideOverlap": False},
            "label": {"show": True, "position": "outside", "formatter": "{d}%", "color": INK_PRIMARY,
                      "alignTo": "edge", "edgeDistance": 10, "distanceToLabelLine": 4,
                      "fontSize": 12, "fontWeight": 600, "fontFamily": FONT, "lineHeight": 16},
            "labelLine": {"show": True, "length": 12, "length2": 8,
                          "lineStyle": {"color": AXIS, "width": 1}},
            "data": items}],
        # At phone widths, reserve real text width rather than silently ellipsizing percentages.
        "media": [
            {"query": {"maxWidth": 400}, "option": {"series": [{"radius": ["36%", "58%"]}]}},
            {"query": {"minWidth": 401}, "option": {"series": [{"radius": ["44%", "70%"]}]}},
        ],
    }


# ==================== ③ 瀑布图（金额口径，4 series 结构，测试锁定） ====================
def waterfall(data: dict, product: str, month: str) -> dict:
    """本月总成本变动的三要素金额分解（元）：透明底座堆叠悬空阶梯 + 正负分离。

    口径统一要求（硬性）：
      变动额 = 要素单位成本 × 当月产量（本月总额 - 上月总额，元）；
      贡献度 = 要素金额变动 / 总金额变动 × 100%，与 data_layer.contribution 完全同源；
      本图绝不展示单位成本变动（元/盒）。

    series 顺序被测试锁定（tests/test_chart_structure.py）：
      series[0]=基底（透明，只垫高度）、series[1]=增加（红）、
      series[2]=减少（蓝）、series[3]=合计（灰，上月基准柱+本月汇总柱）
    """
    ac = {a["month"]: a for a in data["amount_change"]}
    series = data["series"]
    months = [s["month"] for s in series]
    cur = next(s for s in series if s["month"] == month)
    idx = next(i for i, s in enumerate(series) if s["month"] == month)
    if idx == 0 or ac[month]["上月总成本"] is None:
        raise ValueError(f"{month} 为该产品首月，无上月无法绘制瀑布图")

    a = ac[month]
    prev_total = a["上月总成本"]
    cur_total = a["本月总成本"]
    d_amt = {"材料": a["材料变动额"], "人工": a["人工变动额"], "制费": a["制费变动额"]}
    pct = a["贡献度"]
    cats = ["上月", "材料变动", "人工变动", "制费变动", "本月"]

    bases = [0]
    running = prev_total
    endpoints = [running]
    for delta in d_amt.values():
        after = round(running + delta, 2)
        bases.append(min(running, after))
        endpoints.append(after)
        running = after
    bases.append(0)

    # 台阶标签：金额（元）+ 贡献度%（与归因分析完全同源，禁止两套口径）
    def _step_label(v, p):
        return f"{_fmt_amt(v)}元\n贡献{p:+.2f}%" if p is not None else f"{_fmt_amt(v)}元"

    # ECharts stackStrategy='all' can propagate a missing ('-'/NaN) lower
    # series into the next stack. Use finite, invisible zero-height layout
    # placeholders, never display them as observations or clickable facts.
    def _stack_gap():
        return {"value": 0, "stack_gap": True,
                "label": {"show": False}, "tooltip": {"show": False},
                "itemStyle": {"opacity": 0},
                "emphasis": {"disabled": True, "label": {"show": False}}}

    # 台阶数据点：带逐点标签（堆叠柱 label 位置随正负方向）
    def _step_point(name, v):
        if v == 0:
            return _stack_gap()
        pos = v > 0
        return {
            "value": abs(v),
            "signed_delta": v,
            "contribution": pct[name],
            "tooltip": {"formatter": f"{month} {product}<br/>{name}变动 "
                        + _step_label(v, pct[name]).replace("\n", "<br/>")},
            "label": {
                "position": "top" if pos else "bottom",
                "formatter": _step_label(v, pct[name]),
                "color": INK_SECONDARY, "fontSize": 11,
                "fontFamily": FONT, "lineHeight": 14,
            },
        }

    # This explicitly truncated axis emphasizes decomposition, not total magnitude.
    # Three maximum steps also cover offsetting intermediate balances; the small
    # floor keeps all-zero/unchanged inputs drawable without changing any value.
    spread = max(*(abs(value) for value in d_amt.values()),
                 max(abs(prev_total), abs(cur_total)) * 0.002, 0.5)
    axis_min = round(min(prev_total, cur_total) - 3 * spread, 2)
    axis_max = round(max(prev_total, cur_total) + 3 * spread, 2)

    def _total_point(value, align, outer):
        return {
            "value": value,
            "label": {"show": True, "position": "top" if value >= 0 else "bottom",
                      "distance": 44 if outer else 8, "align": align,
                      "formatter": f"{value:,.2f}\n元", "color": INK_PRIMARY,
                      "fontSize": 11, "fontFamily": FONT, "fontWeight": 600,
                      "lineHeight": 14},
            "tooltip": {"formatter": f"{{b}}总成本 {value:,.2f} 元"},
        }

    return {
        "title": {"text": "总成本变动分解",
                  "subtext": f"{month} · {product} · 合计 {_fmt_amt(a['总变动额'])} 元\n"
                  + "Y 轴截断以突出变动量；数值仍为实际金额",
                  **_TITLE},
        "aria": {"enabled": True, "description":
                 f"{product} {month}，Y轴截断以突出变动量，柱长不可用于比较总额倍数。"
                 + f"上月{prev_total:,.2f}元，本月{cur_total:,.2f}元。"
                 + _source_subtext(months)},
        "tooltip": {**_TOOLTIP_BASE, "trigger": "item", "confine": True,
                    "formatter": "{b}: {c} 元"},
        "grid": {"left": 20, "right": 16, "top": 112, "bottom": 30,
                 "containLabel": True},
        "xAxis": {**_cat_axis(cats), "axisLabel": {**_AXIS_LABEL, "interval": 0}},
        "media": [
            {"query": {"maxWidth": 500}, "option": {
                "xAxis": {"data": ["上月", "材料", "人工", "制费", "本月"],
                          "axisLabel": {"interval": 0, "fontSize": 10}},
                "series": [{"label": {"show": False}} for _ in range(3)] +
                          [{"label": {"show": True}}]}},
            {"query": {"minWidth": 501}, "option": {
                "xAxis": {"data": cats, "axisLabel": {"interval": 0, "fontSize": 12}},
                "series": [{"label": {"show": False}}] +
                          [{"label": {"show": True}} for _ in range(3)]}},
        ],
        "yAxis": _yaxis("元") | {"min": axis_min, "max": axis_max},
        "accounting_source": {"row": "amount_change", "month": month,
                              "previous_total": prev_total, "current_total": cur_total,
                              "signed_changes": d_amt, "net_change": a['总变动额'],
                              "contributions": pct, "axis_mode": "explicitly_truncated"},
        "series": [
            # series[0] 基底：透明垫高度（悬空台阶的起点）
            {"name": "基底", "type": "bar", "stack": "wf", "stackStrategy": "all",
             "itemStyle": {"color": "transparent"},
             "tooltip": {"show": False}, "label": {"show": False},
             "silent": True, "data": bases},
            # series[1] 增加：Δ>0 的要素柱（红=不利差异）
            {"name": "增加（不利）", "type": "bar", "stack": "wf", "stackStrategy": "all",
             "label": {"show": True},
             "itemStyle": {"color": POS_COLOR, "borderRadius": [4, 4, 0, 0]},
             "data": [_stack_gap(), _step_point("材料", d_amt["材料"]) if d_amt["材料"] > 0 else _stack_gap(),
                      _step_point("人工", d_amt["人工"]) if d_amt["人工"] > 0 else _stack_gap(),
                      _step_point("制费", d_amt["制费"]) if d_amt["制费"] > 0 else _stack_gap(), _stack_gap()]},
            # series[2] 减少：Δ<0 的要素柱（蓝=有利差异）
            {"name": "减少（有利）", "type": "bar", "stack": "wf", "stackStrategy": "all",
             "label": {"show": True},
             "itemStyle": {"color": NEG_COLOR, "borderRadius": [0, 0, 4, 4]},
             "data": [_stack_gap(), _step_point("材料", d_amt["材料"]) if d_amt["材料"] < 0 else _stack_gap(),
                      _step_point("人工", d_amt["人工"]) if d_amt["人工"] < 0 else _stack_gap(),
                      _step_point("制费", d_amt["制费"]) if d_amt["制费"] < 0 else _stack_gap(), _stack_gap()]},
            # series[3] 合计：上月基准柱 + 本月汇总柱（中性深灰）
            {"name": "合计", "type": "bar", "stack": "wf", "stackStrategy": "all",
              "labelLayout": {"moveOverlap": "shiftY", "hideOverlap": False},
             "itemStyle": {"color": TOTAL_COLOR, "borderRadius": 3},
             "label": {"show": True, "position": "top",
                       "formatter": "{c}", "color": INK_PRIMARY,
                       "fontSize": 12, "fontFamily": FONT, "fontWeight": 600},
             "data": [_total_point(prev_total, "left", (prev_total >= 0) == (cur_total >= 0) and abs(prev_total) >= abs(cur_total)),
                       _stack_gap(), _stack_gap(), _stack_gap(),
                       _total_point(cur_total, "right", (prev_total >= 0) == (cur_total >= 0) and abs(cur_total) > abs(prev_total))]},
        ],
    }


def change_detail(data: dict, product: str, month: str) -> dict:
    """Signed change-only bars; NOT a cropped or rebased total-cost waterfall.

    Values, net change and contributions are read from the very same canonical
    amount_change row used by waterfall(). Starting/ending totals are metadata,
    never drawn as tiny bars on the change-only axis. Every visible bar starts at
    zero, including offsetting/negative contributions and the separate net bar.
    """
    a = next(row for row in data['amount_change'] if row['month'] == month)
    index = next(i for i, row in enumerate(data['series']) if row['month'] == month)
    if index == 0 or a['上月总成本'] is None:
        raise ValueError(f'{month} 无连续上月，无法绘制差额放大视图')
    elements = ('材料', '人工', '制费')
    values = [a[element + '变动额'] for element in elements] + [a['总变动额']]
    extent = max(max(abs(value) for value in values) * 1.25, 0.5)
    categories = ['材料变动', '人工变动', '制费变动', '净变动']
    points = []
    for index, (name, value) in enumerate(zip(categories, values)):
        contribution = a['贡献度'][elements[index]] if index < 3 else None
        share = (f'贡献度 {contribution:+.2f}%' if contribution is not None
                 else '贡献度无定义（净变动为零）') if index < 3 else '三要素金额变动净额'
        points.append({
            'value': value, 'signed_delta': value, 'contribution': contribution,
            'itemStyle': {'color': TOTAL_COLOR if index == 3 else POS_COLOR if value > 0 else NEG_COLOR},
            'tooltip': {'formatter': f'{month} · {product}<br/>{name} {value:+,.2f} 元<br/>{share}'},
            'label': {'position': 'top' if value >= 0 else 'bottom',
                      'formatter': f'{value:+,.2f}', 'color': INK_SECONDARY,
                      'fontSize': 11, 'fontFamily': FONT},
        })
    return {
        'title': {'text': '差额放大视图',
                  'subtext': f'{month} · {product}\n仅显示差额（元），起止总额另列', **_TITLE},
        'tooltip': {**_TOOLTIP_BASE, 'trigger': 'item', 'confine': True},
        'grid': {'left': 24, 'right': 24, 'top': 112, 'bottom': 38, 'containLabel': True},
        'xAxis': {**_cat_axis(categories), 'axisLine': {**_AXIS_LINE, 'onZero': False},
                  'axisLabel': {**_AXIS_LABEL, 'interval': 0, 'margin': 16}},
        'yAxis': _yaxis('元') | {'min': -extent, 'max': extent},
        'accounting_source': {'row': 'amount_change', 'month': month,
                              'previous_total': a['上月总成本'], 'current_total': a['本月总成本'],
                              'signed_changes': {element: a[element + '变动额'] for element in elements},
                              'net_change': a['总变动额'], 'contributions': a['贡献度'],
                              'axis_mode': 'zero_based_changes'},
        'aria': {'enabled': True, 'description':
                 f'{product} {month} 差额放大视图，所有柱从零开始，起止总额另列。'
                 + '；'.join(f'{name}{value:+,.2f}元' for name, value in zip(categories, values))
                 + f"。上月总成本{a['上月总成本']:,.2f}元，本月总成本{a['本月总成本']:,.2f}元。"},
        'series': [{'name': '金额变动（元）', 'type': 'bar', 'barMaxWidth': 64,
                    'label': {'show': True}, 'data': points,
                    'markLine': {'silent': True, 'symbol': 'none', 'label': {'show': False},
                                 'lineStyle': {'color': INK_MUTED, 'width': 1},
                                 'data': [{'yAxis': 0}]}}],
        'media': [
            {'query': {'maxWidth': 500}, 'option': {
                'xAxis': {'data': ['材料', '人工', '制费', '净变动'],
                          'axisLabel': {'fontSize': 10, 'interval': 0}},
                'series': [{'label': {'show': False}}]}},
            {'query': {'minWidth': 501}, 'option': {
                'xAxis': {'data': categories, 'axisLabel': {'fontSize': 12, 'interval': 0}},
                'series': [{'label': {'show': True}}]}},
        ],
    }


# ==================== ④ 热力图（产品 × 月份矩阵，下拉切换要素） ====================
# element 取值 = 数据层 ELEMENT_LABELS 的标签名（单位成本/材料/人工/制费/产量/总成本），
# 其中"单位成本"即三产品整体成本视图。
_HEAT_UNITS = {"产量": "盒", "总成本": "元"}


def heatmap(d: dict, products: list, months: list, element: str = "单位成本") -> dict:
    """产品 × 月份矩阵热力图：x 轴=月份、y 轴=产品，下拉切换成本要素（第三维）。

    数据点结构被测试锁定（tests/test_chart_structure.py）：
      [月份索引, 产品索引, 数值] —— x 轴维度在前、y 轴维度在后。
    数值保持数据层原始精度（不做四舍五入），缺测月份写 None 留空格，
    绝不补零、不编造数值。
    """
    if element not in ELEMENT_LABELS.values():
        raise ValueError(f"不支持的热力图要素: {element}"
                         f"（可选: {'/'.join(sorted(set(ELEMENT_LABELS.values())))}）")
    unit = _HEAT_UNITS.get(element, "元/盒")

    heat_data, vals = [], []
    for pi, p in enumerate(products):
        by_month = {s["month"]: s.get(element)
                    for s in build_dashboard_data(p, d)["series"]}
        for mi, m in enumerate(months):
            v = by_month.get(m)
            if v is None or pd.isna(v):
                heat_data.append([mi, pi, None, p, m, element])   # 缺格留空（不补零）
                continue
            v = float(v)
            heat_data.append([mi, pi, v, p, m, element])
            vals.append(v)

    # visualMap 量程：全等值或空数据时给非退化区间，避免 min==max 渲染异常
    lo, hi = (min(vals), max(vals)) if vals else (0.0, 1.0)
    if hi - lo < 1e-9:
        lo, hi = lo - 0.5, hi + 0.5

    return {
        "title": {"text": f"{element}热力图",
                  "subtext": _source_subtext(months), **_TITLE},
        "tooltip": {**_TOOLTIP_BASE, "confine": True, "trigger": "item"},
        "grid": {"left": 8, "right": 92, "top": 76, "bottom": 12,
                 "containLabel": True},
        "xAxis": _cat_axis(months),
        "yAxis": {**_cat_axis(list(products)), "splitArea": {"show": False}},
        "visualMap": {"min": lo, "max": hi, "calculable": True, "precision": 2,
                      "orient": "vertical", "right": 0, "top": "middle",
                      "itemWidth": 12, "itemHeight": 110,
                      "text": ["高", "低"],
                      "textStyle": {"color": INK_MUTED, "fontSize": 11,
                                    "fontFamily": FONT},
                      "inRange": {"color": SEQ_BLUE}, "dimension": 2},
        "series": [{"name": element, "type": "heatmap", "data": heat_data,
                    "dimensions": ["月份索引", "产品索引", f"数值（{unit}）", "产品", "月份", "要素"],
                    "encode": {"x": 0, "y": 1, "value": 2, "tooltip": [3, 4, 5, 2]},
                    "label": {"show": True, "formatter": "{@[2]}",
                              "fontSize": 11, "fontFamily": FONT, "color": SURFACE,
                              "textBorderColor": "rgba(11,11,11,0.45)",
                              "textBorderWidth": 2},
                    "itemStyle": {"borderColor": SURFACE, "borderWidth": 2,
                                  "borderRadius": 2},
                    "emphasis": {"itemStyle": {"borderColor": INK_PRIMARY,
                                               "borderWidth": 2}}}],
    }


CHART_BUILDERS = {"趋势": trend, "结构": structure, "瀑布": waterfall,
                  "热力图": heatmap}


# ==================== ⑤ 要素多线趋势（单位成本 + 三要素） ====================
def trend_elements(data: dict, product: str, months: list) -> dict:
    """单产品要素多线趋势：单位成本（深灰加粗）+ 材料/人工/制费（要素固定色）。
    展示的是单位成本水平值（元/盒）及其构成，属数据展示口径，不参与贡献度计算。
    """
    by_m = {s["month"]: s for s in data["series"]}
    win = _calendar_window(months)
    lines = [("单位成本", TOTAL_COLOR, 2.5), ("材料", ELEMENT_COLORS["材料"], 2),
             ("人工", ELEMENT_COLORS["人工"], 2), ("制费", ELEMENT_COLORS["制费"], 2)]
    series = []
    for label, color, width in lines:
        data_pts = []
        for j, m in enumerate(win):
            v = (by_m.get(m) or {}).get(label)
            if j == len(win) - 1 and v is not None:
                data_pts.append({"value": v, "label": {
                    "show": True, "position": "bottom" if label == "人工" else "top", "distance": 8,
                    "formatter": f"{v:.2f}", "color": INK_SECONDARY,
                    "fontSize": 11, "fontFamily": FONT}})
            else:
                data_pts.append(v)
        series.append({
            "name": label, "type": "line", "data": data_pts,
            "smooth": 0.3, "symbol": "circle", "symbolSize": 6,
            "lineStyle": {"width": width, "color": color,
                          "cap": "round", "join": "round"},
            "itemStyle": {"color": color, "borderColor": SURFACE, "borderWidth": 2},
        })
    return {
        "title": {"text": "单位成本及要素走势",
                  "subtext": f"{product} · 元/盒\n" + _source_subtext(win),
                  **_TITLE},
        "tooltip": {**_TOOLTIP_BASE, "trigger": "axis",
                    "axisPointer": {"type": "line",
                                    "lineStyle": {"color": AXIS, "width": 1,
                                                  "type": "solid"}}},
        "legend": {"type": "scroll", "bottom": 10, "left": "center", "width": "90%", "icon": "circle",
                   "itemWidth": 8, "itemHeight": 8, "itemGap": 18,
                   "textStyle": {"color": INK_SECONDARY, "fontSize": 12,
                                 "fontFamily": FONT}},
        "grid": {"left": 16, "right": 30, "top": 112, "bottom": 58,
                 "containLabel": True},
        "xAxis": _cat_axis(win),
        "yAxis": _yaxis("元/盒"),
        # Keep touch/wheel zoom without a mostly empty miniature chart strip.
        "dataZoom": [{"type": "inside"}],
        "series": series,
    }


# ==================== ⑥ 变化率热力图（要素 × 月份，颜色=环比/同比%） ====================
# 发散色板：蓝=下降（有利）、白=无变动、红=上涨（不利）——量纲为正负变化率，
# 禁止复用单蓝色顺序色板（顺序色板只表达"量级"，不表达"方向"）。
RATE_DIVERGING = ["#2a78d6", "#93bce9", "#f4f4f2", "#f29e95", "#e34948"]
RATE_THRESHOLD = 10.0   # 赛题波动阈值告警：环比变动超过±10%

RATE_ELEMENTS = ["单位成本", "材料", "人工", "制费"]


def heatmap_rate(d: dict, product: str, months: list, rate: str = "mom") -> dict:
    """单产品变化率热力图：y 轴=成本要素、x 轴=月份，颜色=环比/同比变化率（%）。

    数据点结构被测试锁定（tests/test_chart_structure.py）：
      [月份索引, 要素索引, 变化率%, 月份, 要素, 环比/同比标识]
    - 变化率为要素单位成本（元/盒）的环比/同比，即赛题 5.2.3 波动阈值告警口径；
      与瀑布图金额口径贡献度为不同指标（各自口径自洽，图中已标注）；
    - |变化率|>±10% 的格子红色边框高亮 + ⚠ 标签（赛题"波动阈值告警"可视化）；
    - 首月无环比 / 无同比数据 → None 留空格，绝不补零。
    """
    if rate not in ("mom", "yoy"):
        raise ValueError(f"不支持的变化率类型: {rate}（可选 mom/yoy）")
    data = build_dashboard_data(product, d)
    rows = data["mom"] if rate == "mom" else data["yoy"]
    rate_label = "环比" if rate == "mom" else "同比"
    cells, vals = [], []
    for ei, e in enumerate(RATE_ELEMENTS):
        for mi, m in enumerate(months):
            row = next((r for r in rows if r["month"] == m), None)
            v = row.get(e) if row else None
            if v is None or pd.isna(v):
                cells.append([mi, ei, None, m, e, rate])   # 缺格留空
                continue
            v = float(v)
            alert = rate == "mom" and abs(v) > RATE_THRESHOLD
            item = {"value": [mi, ei, v, m, e, rate_label],
                    "label": {"formatter": f"{v:+.1f}%"}}
            if alert:
                item["itemStyle"] = {"borderColor": "#e34948", "borderWidth": 2.5}
                item["label"] = {"show": True,
                                 "formatter": f"⚠{v:+.1f}%",
                                 "fontSize": 11, "fontFamily": FONT,
                                 "color": INK_PRIMARY,
                                 "textBorderColor": "rgba(255,255,255,0.9)",
                                 "textBorderWidth": 2}
            if abs(v) > RATE_THRESHOLD:
                item.setdefault("itemStyle", {})["color"] = RATE_DIVERGING[-1 if v > 0 else 0]
            cells.append(item)
            vals.append(v)
    # 固定颜色刻度为 ±10%；数据保留真实值，超范围使用端点颜色。
    span = RATE_THRESHOLD

    return {
        "title": {"text": f"成本要素{rate_label}变化率",
                  "subtext": f"{product} · 单位成本{rate_label}（%）\n"
                  + ("红框：环比超过 ±10%" if rate == "mom" else "同比参考，不触发环比告警"),
                  **_TITLE},
        "tooltip": {**_TOOLTIP_BASE, "position": "top", "confine": True,
                    "trigger": "item"},
        "grid": {"left": "center", "width": "64%", "top": 112, "bottom": 12,
                 "containLabel": True},
        "media": [
            {"query": {"maxWidth": 820}, "option": {
                "grid": {"left": 8, "width": "72%"},
                "visualMap": {"right": 0}}},
            {"query": {"minWidth": 821}, "option": {
                "grid": {"left": "center", "width": "64%"},
                "visualMap": {"right": "12%"}}},
        ],
        "xAxis": _cat_axis(months),
        "yAxis": {**_cat_axis(RATE_ELEMENTS), "splitArea": {"show": False}},
        "visualMap": {"min": -span, "max": span, "calculable": False, "precision": 1,
                      "orient": "vertical", "right": 0, "top": "middle",
                      "itemWidth": 12, "itemHeight": 110,
                      "text": ["上涨 +10%", "下降 −10%"],
                      "textStyle": {"color": INK_MUTED, "fontSize": 11,
                                    "fontFamily": FONT},
                      "inRange": {"color": RATE_DIVERGING}, "dimension": 2},
        "series": [{"name": f"{rate_label}变化率", "type": "heatmap", "data": cells,
                    "dimensions": ["月份索引", "要素索引", "变化率(%)", "月份", "要素", "类型"],
                    "encode": {"x": 0, "y": 1, "value": 2, "tooltip": [3, 4, 5, 2]},
                    "label": {"show": True, "formatter": "{@[2]}%",
                              "fontSize": 11, "fontFamily": FONT, "color": INK_PRIMARY,
                              "textBorderColor": "rgba(255,255,255,0.85)",
                              "textBorderWidth": 2},
                    "itemStyle": {"borderColor": SURFACE, "borderWidth": 2,
                                  "borderRadius": 2},
                    "emphasis": {"itemStyle": {"borderColor": INK_PRIMARY,
                                               "borderWidth": 2}}}],
    }


def industry_radar(result):
    """Build a reference-vs-observed radar; values are P50-normalised, not scores."""
    radar = (result or {}).get('radar') or {}
    axes = radar.get('axes') or []
    if not radar.get('available') or not axes:
        raise ValueError('行业基准不足三个可比轴，无法绘制雷达图')
    labels = [axis['name'] for axis in axes]
    values = [value for item in radar.get('series', []) for value in item.get('values', [])]
    maximum = max(160, max(values, default=100) * 1.15)
    series = []
    for item in radar.get('series', []):
        series.append({'name': item['name'], 'type': 'radar',
                       'data': [{'value': item['values'], 'name': item['name']}],
                       'symbol': 'circle', 'symbolSize': 5,
                       'lineStyle': {'width': 2},
                       'areaStyle': {'opacity': 0.08 if item.get('kind') == 'reference' else 0.16}})
    return {
        'animation': False,
        'title': {'text': '行业基准与本厂月度观测',
                  'subtext': '行业 P50=100；仅作参照，不是百分位或优劣评分',
                  'left': 'center', 'top': 10,
                  'textStyle': {'fontSize': 16}, 'subtextStyle': {'fontSize': 11}},
        'tooltip': {'trigger': 'item', 'confine': True},
        'legend': {'type': 'scroll', 'bottom': 4, 'left': 'center'},
        'radar': {'center': ['50%', '52%'], 'radius': '62%',
                  'indicator': [{'name': label, 'min': 0, 'max': maximum} for label in labels],
                  'axisName': {'color': INK_SECONDARY, 'fontSize': 11},
                  'splitArea': {'areaStyle': {'color': ['#ffffff', '#f7fafc']}},
                  'splitLine': {'lineStyle': {'color': GRID}},
                  'axisLine': {'lineStyle': {'color': AXIS}}},
        'series': series,
        'media': [{'query': {'maxWidth': 500}, 'option': {'radar': {'radius': '54%'}}}],
    }
#（注：内容由AI生成）
