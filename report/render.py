# -*- coding: utf-8 -*-
"""子任务2：docx 占位符替换与导出。

- 数值类：datafill 映射表替换；
- 表格类（数据层 4 类）：tables 多行文本替换；改进建议/整改任务留待子任务4；
- 文本类：保留占位符（子任务3/4 填充）；
- 图表嵌入：matplotlib 趋势/瀑布/结构 3 张 PNG；
- 导出：docx 字节流（PDF 导出留待完整版）。
"""
import io
import re
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from docx import Document

from .datafill import build_mapping, load_data, resolve_period, product_specification, scoped_rows
import pandas as pd
from .registry import PH, TEMPLATE_PATH
from .tables import build_table_text

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei"]
plt.rcParams["axes.unicode_minus"] = False

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "outputs"


def _replace_in_paragraph(par, mapping, keep_placeholder_types=("文本",)):
    """替换段落内占位符；文本类按保留列表不替换（子任务3/4 处理）。

    上下文感知：模板中占位符后紧跟 '%'（如 {{环比}}%）时值不带 %，
    否则值保留 %（同一占位符在模板中可能一处带 % 一处不带）。
    """
    full = par.text
    if not PH.search(full):
        return False

    def sub(m):
        name = m.group(1)
        val = mapping.get(name)
        if val is None or (isinstance(val, str) and "{{" in str(val)):
            # 未映射：文本类/LLM表格保留（子任务3/4 填充）；数值类无数据填"—"（模板惯例）
            from .registry import _classify
            kind = _classify(name)
            if kind == "文本" or name in ("改进建议表格", "整改任务表格"):
                return m.group(0)
            return "—"
        s = str(val)
        if full[m.end():m.end() + 1] == "%" and s.endswith("%"):
            s = s[:-1]             # 模板自带 %，去掉值末尾的 %
        return s

    new_text = PH.sub(sub, full)
    for run in par.runs[1:]:
        run.text = ""
    if par.runs:
        par.runs[0].text = new_text
    return True


def _charts(product, month, theme, d=None, mapping=None):
    """Calendar-window trend and same-contract amount waterfall; no future rows."""
    d = load_data() if d is None else d
    mapping = build_mapping(product, month, theme, d) if mapping is None else mapping
    spec = product_specification(d, product)
    end = pd.Period(month, freq='M')
    window = [str(end-5+i) for i in range(6)]
    history = pd.concat([d.get('cost26',pd.DataFrame()),d.get('cost25',pd.DataFrame())],ignore_index=True)
    sub = scoped_rows(history,product,spec,window)
    values = {str(row['月份']):float(row['单位成本(元/盒)']) for _,row in sub.iterrows()} if sub is not None else {}
    pngs = {}
    def save(fig, name):
        buf=io.BytesIO()
        fig.savefig(buf, format='png', dpi=150, bbox_inches='tight')
        plt.close(fig)
        pngs[name]=buf.getvalue()
    fig, ax = plt.subplots(figsize=(6,2.6))
    ax.plot(window,[values.get(mm,float('nan')) for mm in window],marker='o',color='#2f6fb3')
    ax.tick_params(axis='x',labelsize=8)
    ax.set_title(f'{product} 单位成本趋势（截至{month}）')
    ax.set_ylabel('元/盒')
    save(fig,'趋势')
    amount = mapping['_amount_change']
    if amount.get('总变动额') is not None:
        fig, ax = plt.subplots(figsize=(6,2.6))
        base = amount['上月总成本']
        ax.bar('上月',base,color='#55798e')
        for i,key in enumerate(('材料','人工','制费'),1):
            delta=amount[key+'变动额']
            after=base+delta
            ax.bar(key,abs(delta),bottom=min(base,after),color='#c66a4a' if delta>=0 else '#237f83')
            ax.text(i,(base+after)/2,f'{delta:+,.2f}',ha='center',va='center',fontsize=8)
            base=after
        ax.bar('本月',amount['本月总成本'],color='#55798e')
        ax.axhline(0,color='gray',lw=.6)
        ax.set_ylabel('元')
        ax.set_title(f"{month} 总成本金额变动（{amount['总变动额']:+,.2f}元）")
        save(fig,'瀑布')
    vals=[mapping[key+'总金额'] for key in ('材料','人工','制造费用')]
    if all(isinstance(value,(int,float)) and value>=0 for value in vals) and sum(vals)>0:
        fig,ax=plt.subplots(figsize=(3.4,2.6))
        ax.pie(vals,labels=['材料','人工','制费'],autopct='%.1f%%',colors=['#2f6fb3','#dc9252','#278b7d'])
        ax.set_title(f'{month} 成本结构')
        save(fig,'结构')
    return pngs


def build_report(params, out_path=None, d=None):
    """子任务2 主流程：数据填充 → 输出 docx（数据部分已填充，文本类保留）。"""
    from .datafill import validate_params
    if params.get('theme') != '月度成本分析':
        raise ValueError('当前DOCX模板仅验收月度数据填充；季度及专题可下载计算映射，模板尚待适配。')
    d = load_data() if d is None else d
    ok, errors = validate_params(params, d)
    if not ok:
        raise ValueError("参数校验失败: " + "；".join(errors))
    product, month, theme = params["product"], params["month"], params["theme"]

    mapping = build_mapping(product, month, theme, d)
    # 表格类（数据层）
    for kind in ("原材料成本明细表格", "近6个月成本趋势表格",
                 "原材料价格跟踪表格", "对标差异表格"):
        mapping[kind] = ('本报告未选择对标分析' if kind == '对标差异表格' and not params.get('include_benchmark', True)
                         else build_table_text(kind, product, month, d))

    doc = Document(str(TEMPLATE_PATH))
    # 替换段落与表格单元格
    for par in doc.paragraphs:
        _replace_in_paragraph(par, mapping)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for par in cell.paragraphs:
                    _replace_in_paragraph(par, mapping)

    # Clarify the legacy template's unit-cost cells versus monetary contributions.
    for table in doc.tables:
        if table.rows and any('成本要素' in cell.text for cell in table.rows[0].cells):
            if len(table.rows[0].cells) >= 5:
                table.rows[0].cells[1].text = '单位成本(元/盒)'
                table.rows[0].cells[4].text = '金额变动贡献度'
                if mapping['_amount_change'].get('总变动额') in (None, 0):
                    table.rows[-1].cells[4].text = '—'
    doc.paragraphs[0].insert_paragraph_before('数据填充草稿：数值使用合并成本数据；文字、引用、建议及审核尚未完成，不作为正式发布报告。')
    doc.add_paragraph('计算口径：瀑布图与贡献度均按金额变化（元）；单品结构表展示单位成本（元/盒）。零净变动或缺少上月时贡献度无定义。')
    doc.add_paragraph('本次使用源文件：' + '；'.join(Path(path).name for path in mapping.get('_source_files', [])))
    # 图表嵌入（指定章节标题后）
    try:
        pngs = _charts(product, month, theme, d, mapping)
        targets = {"趋势": "重点产品专项分析", "瀑布": "总成本概览", "结构": "总成本概览"}
        for key, buf in pngs.items():
            heading = targets[key]
            for par in doc.paragraphs:
                if heading in par.text and "{{" not in par.text:
                    from docx.shared import Inches
                    new_par = par.insert_paragraph_before()
                    new_par.add_run().add_picture(io.BytesIO(buf), width=Inches(5.6))
                    break
    except Exception:
        doc.add_paragraph('图表生成未完成；本文件仅保留数据填充结果，请核查后再发布。')

    buf = io.BytesIO()
    doc.save(buf)
    data = buf.getvalue()
    if out_path:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data)
    return data


def residue_check(data):
    """零残留检查：数值/表格类应无残留；返回残留占位符清单（文本类除外）。"""
    doc = Document(io.BytesIO(data))
    text = "\n".join(p.text for p in doc.paragraphs)
    for t in doc.tables:
        for row in t.rows:
            for c in row.cells:
                text += "\n" + c.text
    residues = [m for m in PH.findall(text)]
    # 豁免清单：文本类（子任务3/4）+ 依赖 LLM 的两个表格（子任务4）
    text_ph = [m for m in residues if any(k in m for k in
               ["分析文本", "排查分析", "拆解", "亮点", "问题", "引用"])]
    llm_tables = [m for m in residues if m in ("改进建议表格", "整改任务表格")]
    numeric_residues = [m for m in residues if m not in text_ph and m not in llm_tables]
    return numeric_residues, text_ph + llm_tables
#（注：内容由AI生成）
