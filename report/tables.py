# -*- coding: utf-8 -*-
"""Deterministic report tables, scoped to the selected product/specification/month."""
from decimal import Decimal
import re

import pandas as pd

from .datafill import load_data, product_specification, scoped_rows, _dec, _fmt, _mom_pct


def material_detail_table(product, month, d=None):
    d = load_data() if d is None else d
    spec = product_specification(d, product)
    prev = str(pd.Period(month, freq='M')-1)
    rows = scoped_rows(d.get('material'), product, spec, [prev, month], extra_key='原材料名称')
    if rows is None:
        return '暂无同产品同规格原料明细'
    lines=[]
    for i,(_,row) in enumerate(rows[rows['月份']==month].iterrows(),1):
        prior = rows[(rows['月份']==prev)&(rows['原材料名称']==row['原材料名称'])]
        current = _dec(row['单位消耗成本(元/盒)'])
        previous = _dec(prior.iloc[0]['单位消耗成本(元/盒)']) if not prior.empty else None
        rate = _mom_pct(current,previous)
        note = '单位消耗成本变化，采购价与实际耗用原因待核查' if rate is not None else '缺少可比上月或上月为零'
        lines.append(f"{i}\t{row['原材料名称']}\t{_fmt(current)}\t{_fmt(previous)}\t{_fmt(rate,'%')}\t{note}")
    return '\n'.join(lines) if lines else '暂无当月明细'


def trend_table(product, month, d=None):
    d = load_data() if d is None else d
    spec=product_specification(d,product)
    end=pd.Period(month,freq='M')
    window=[str(end-5+n) for n in range(6)]
    history=pd.concat([d.get('cost26',pd.DataFrame()),d.get('cost25',pd.DataFrame())],ignore_index=True)
    rows=scoped_rows(history,product,spec,[str(end-6),*window])
    lines=[]
    for mm in window:
        current=rows[rows['月份']==mm] if rows is not None else pd.DataFrame()
        if current.empty:
            lines.append(f'{mm}\t—\t—\t—\t—\t—\t缺少数据')
            continue
        row=current.iloc[0]
        prior=rows[rows['月份']==str(pd.Period(mm,freq='M')-1)]
        unit=_dec(row['单位成本(元/盒)'])
        previous=_dec(prior.iloc[0]['单位成本(元/盒)']) if not prior.empty else None
        lines.append(f"{mm}\t{int(_dec(row['产量(盒)']))}\t{_fmt(_dec(row['直接材料(元/盒)']))}\t"
                     f"{_fmt(_dec(row['直接人工(元/盒)']))}\t{_fmt(_dec(row['制造费用(元/盒)']))}\t"
                     f"{_fmt(unit)}\t{_fmt(_mom_pct(unit,previous),'%')}")
    return '\n'.join(lines)


def price_track_table(month, d=None):
    d=load_data() if d is None else d
    frame=d.get('market',pd.DataFrame())
    if not month.startswith('2026-'):
        return '市场参考CSV仅覆盖2026年，不沿用其他年份报价'
    selected=int(month[5:7])
    cols=[str(col) for col in frame if re.fullmatch(r'\d+月价格',str(col)) and int(str(col).split('月')[0])<=selected]
    if frame.empty or not cols or '1月价格' not in frame:
        return '暂无可用市场参考报价'
    target=max(cols,key=lambda col:int(col.split('月')[0]))
    note='' if target==f'{selected}月价格' else f'（仅有{target}，未代替当月采购价）'
    lines=[]
    for _,row in frame.iterrows():
        first,current=_dec(row['1月价格']),_dec(row[target])
        lines.append(f"{row['药材名称']}（{row.get('单位','未标单位')}）\t{_fmt(first)}\t{_fmt(current)}\t"
                     f"{_fmt(_mom_pct(current,first),'%')}\t{row.get('趋势分析','')}\t市场参考{note}")
    return '\n'.join(lines)


def benchmark_table(product, month=None, d=None):
    if month is None:
        return '请指定同产品同规格对标月份'
    from enterprise.benchmark import build_benchmark
    d=load_data() if d is None else d
    spec=product_specification(d,product)
    report=build_benchmark(product,spec,month,d)
    if not report['available']:
        return report['reason']
    lines=[f"{month} / {spec}；单位差=一厂-二厂，标准化金额采用一厂产量，不代表已实现节约。"]
    for row in report['elements']:
        lines.append(f"{row['element']}\t{row['home_unit_cost']:.2f}\t{row['peer_unit_cost']:.2f}\t"
                     f"{row['unit_gap']:+.2f}\t{_fmt(row['gap_pct'],'%')}\t标准化金额差{row['normalized_amount']:+.2f}元")
    lines.append(f"单位成本\t{report['home']['unit_cost']:.2f}\t{report['peer']['unit_cost']:.2f}\t"
                 f"{report['unit_gap']:+.2f}\t{_fmt(report['gap_pct'],'%')}\t标准化金额差{report['normalized_amount']:+.2f}元")
    return '\n'.join(lines)


def build_table_text(kind, product, month, d=None):
    d=load_data() if d is None else d
    functions={'原材料成本明细表格':material_detail_table,'近6个月成本趋势表格':trend_table,'对标差异表格':benchmark_table}
    if kind in functions:
        return functions[kind](product,month,d)
    if kind=='原材料价格跟踪表格':
        return price_track_table(month,d)
    return None
