# -*- coding: utf-8 -*-
"""模块二 5.2.2 单品成本看板（Streamlit + ECharts，FastAPI 数据接口）。

四图按"趋势→构成→变动→全局"递进排列：
  ① 趋势：近6个月单位成本走势（折线，按产品分线，图例可切换）
  ② 构成：本月成本结构（环形）
  ③ 变动：总成本变动分解（瀑布，**金额口径·元**，与贡献度/归因同一口径）
  ④ 全局：产品×月份×成本要素三维交叉（热力图，下拉切换要素）
  ⑤ RAG联动：一键对当前产品/月份检索知识库（混合检索，来源标注）

口径统一（硬性）：瀑布图/贡献度/归因分析共用金额口径
  变动额=单位成本×产量（元）；贡献度=要素金额变动/总金额变动×100%；
  验证基准：银黄口服液 2026-05 材料67.09% / 人工12.31% / 制费20.60%。

统一应用服务按已验证身份过滤成本表；知识检索使用受控发布服务。
认证或授权拒绝立即阻断，不作为可绕行的服务故障。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import streamlit as st

from dashboard.charts import heatmap_rate, structure, trend_elements, waterfall
import pandas as pd
from dashboard.data_layer import build_dashboard_data, discover_months, discover_products
from dashboard.echarts_helper import render_echarts_multi
from app_pages._shared import authorize, home_tables, page_context
from enterprise.security import can

st.set_page_config(page_title="单品成本看板", page_icon=":material/monitoring:", layout="wide")
principal, app = page_context('dashboard.read')
authorize(principal, 'data.read', factory='中药一厂')
try:
    all_tables = home_tables(app)
    d = {key: all_tables[key] for key in ('cost26', 'cost25', 'budget', 'material', 'labor', 'mfg')}
except (ValueError, PermissionError) as exc:
    st.error(str(exc))
    st.stop()
prods = discover_products(d)
months = discover_months(d)

if not prods or not months:
    st.error("未找到有效成本汇总数据，请检查源 CSV。")
    st.stop()

st.html('''<style>
.stMainBlockContainer { padding-top: 2rem; padding-bottom: 3rem; max-width: 1480px; }
.cost-kpis { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:16px; margin:16px 0 24px; }
.cost-kpi { background:#fff; border:1px solid #dfe6ed; border-top:3px solid #167b78; border-radius:8px; padding:20px 22px; min-width:0; }
.cost-kpi .name { color:#536579; font-size:14px; margin-bottom:10px; }
.cost-kpi .value { color:#142a3c; font-size:30px; font-weight:700; line-height:1.3; font-variant-numeric:tabular-nums; white-space:nowrap; }
.cost-kpi .note { color:#63768a; font-size:12px; margin-top:10px; }
.cost-kpi .rise { color:#bc4946; } .cost-kpi .fall { color:#2464a1; }
@media(max-width:800px) { .cost-kpis {grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px;} .cost-kpi {padding:14px 12px;} .cost-kpi .value {font-size:23px;} }
@media(max-width:420px) { .cost-kpi .value {font-size:19px;} }
</style>''')
st.title("产品成本分析")
st.caption("中药一厂  /  月度经营分析")

# ---- 筛选器 ----
col1, col2 = st.columns(2)
with col1:
    product = st.selectbox("分析产品", prods, key="dash_product",
                           index=prods.index('银黄口服液') if '银黄口服液' in prods else 0)
with col2:
    month = st.selectbox("分析月份", months, index=len(months) - 1,
                         key="dash_month")

if not product or not month:
    st.warning("请选择产品与月份")
    st.stop()

authorize(principal, 'dashboard.read', factory='中药一厂', product=product)
# 已授权成本表仍执行产品数据质量检查。
def _disp(v):
    """展示层格式化 round 2（与 API 路径一致；数据层保持全精度）。"""
    return round(v, 2) if isinstance(v, (int, float)) else v


specs = sorted(d['cost26'].loc[d['cost26']['产品名称'] == product, '产品规格'].dropna().unique())
spec = st.selectbox('产品规格', specs, key='dash_spec') if len(specs) > 1 else (specs[0] if specs else '')
d = {k: df[(df['产品名称'] == product) & (df['产品规格'] == spec)].copy()
     if not df.empty and '产品规格' in df else df for k,df in d.items()}
from dashboard.validate import run_all_validation
validation = run_all_validation(d, verbose=False)
issues = {name: errors for name,(errors,_) in validation.items() if errors}
if issues:
    st.error('当前产品数据校验未通过，请先修订数据。')
    st.json(issues)
    st.stop()
raw = build_dashboard_data(product, d)
data = {'product':product, 'series':[{k:_disp(v) for k,v in s.items()} for s in raw['series']],
        'amount_change':raw['amount_change'], 'warnings':raw['warnings']}
import hashlib
from enterprise.knowledge_release import ReleaseRepository
try:
    knowledge_versions = app.knowledge().history()
    knowledge_token = '|'.join(v['version_id'] for v in knowledge_versions)
    releases = ReleaseRepository(app.root).history(principal=principal)
    knowledge_token += '|' + '|'.join(r['release_id'] for r in releases)
except (ValueError, PermissionError, OSError):
    knowledge_token = 'catalog_unavailable'
    st.session_state.pop('attribution_result', None)
    st.warning('知识目录暂不可用，成本图表仍可查看；归因将使用结构化数据。')
market = app.report_tables().get('market', pd.DataFrame()) if can(principal, 'report.generate') else pd.DataFrame()
market_token = hashlib.sha256(market.to_json(orient='split', force_ascii=False).encode()).hexdigest()
_data_token = hashlib.sha256((''.join(df.to_json(orient='split', force_ascii=False) for df in d.values()) + knowledge_token + market_token + repr(st.session_state['_principal_scope'])).encode()).hexdigest()

if not data.get("series"):
    st.error("数据不可用（接口与本地均失败）")
    st.stop()

from dashboard.data_layer import build_attribution_summary

current = next((s for s in data["series"] if s["month"] == month), None)
if current is None:
    st.warning(f"{product} {month} 缺少数据")
    st.stop()
change = next(a for a in data["amount_change"] if a["month"] == month)
delta = change['总变动额']
from html import escape
cards = [
    ('单位成本 · 元/盒', f"{current['单位成本']:.2f}", '材料 + 人工 + 制造费用', ''),
    ('本月产量 · 盒', f"{current['产量']:,.0f}", f'{month} 实际产出', ''),
    ('本月总成本 · 元', f"{current['总成本']:,.2f}", '单位成本 × 产量', ''),
    ('总成本环比变动 · 元', '—' if delta is None else f'{delta:+,.2f}',
     '缺少连续上月' if delta is None else ('较上月增加' if delta > 0 else '较上月减少' if delta < 0 else '与上月持平'),
     'rise' if delta and delta > 0 else 'fall' if delta and delta < 0 else ''),
]
st.html('<div class="cost-kpis">' + ''.join(
    f'<div class="cost-kpi"><div class="name">{escape(name)}</div><div class="value {color}">{escape(value)}</div><div class="note">{escape(note)}</div></div>'
    for name,value,note,color in cards) + '</div>')
with st.expander('查看分析口径'):
    st.caption('要素变动额 = 本月要素单位成本 × 本月产量 − 上月要素单位成本 × 上月产量；贡献度 = 要素金额变动 ÷ 总金额变动 × 100%。')

# ---- 四图 option（全部数值来自真实数据层）----
end_month = pd.Period(month, freq="M")
window = [str(m) for m in pd.period_range(end_month - 5, end_month, freq="M")]
line_opt = trend_elements(data, product, window)
st.caption(f"分析期间：{window[0]} 至 {window[-1]} · 单位：元/盒")
pie_opt = structure(data, product, month)      # ② 结构：环形（占比与金额口径一致）
try:
    wf_opt = waterfall(data, product, month)   # ③ 瀑布：金额口径（元）
except ValueError:
    wf_opt = None

# ---- 渲染前三图（趋势+结构一行，瀑布全宽一行）----
charts = [(line_opt, 430), (pie_opt, 430)]
if wf_opt is not None:
    charts.append((wf_opt, 380, True))   # True=全宽
else:
    st.info(f"{month} 为该产品数据首月，无上月可分解瀑布图")
render_echarts_multi(charts)
# 归因统一在下方展示，避免同一金额结论重复占用页面。
# ---- ④ 热力图：单独一排 + 板块内左上角下拉 ----
with st.container():
    st.subheader("成本要素变化率")
    heat_elem = st.segmented_control("比较口径", ["环比变化率", "同比变化率"],
                                     default="环比变化率", key="heat_elem",
                                     selection_mode="single")
    rate = "yoy" if heat_elem == "同比变化率" else "mom"
    heat_opt = heatmap_rate(d, product, window, rate)
    render_echarts_multi([(heat_opt, 430, True)])

with st.expander("查看同源明细与导出数据"):
    st.dataframe(data["series"], hide_index=True)
    import json
    st.download_button("下载金额归因 JSON", json.dumps(change, ensure_ascii=False, indent=2),
                       file_name=f"{product}_{month}_金额归因.json", mime="application/json")


# ---- ⑤ 对当前产品、工厂和业务日期执行受控发布检索 ----
if can(principal, 'knowledge.read'):
    with st.expander("知识依据检索", expanded=False):
        default_q = f"{product}{month}成本构成与变动原因，涉及哪些原材料、工艺和市场行情"
        query = st.text_input("检索问题", value=default_q, key="rag_query")
        st.caption('只检索已发布、有效且属于当前授权范围的正式知识版本。')
        if st.button("检索知识库", type="primary", key="rag_btn"):
            authorize(principal, 'knowledge.read', factory='中药一厂', product=product)
            from enterprise.knowledge_release import get_search_engine, result_to_api
            try:
                final, stats = get_search_engine(repository=app.knowledge()).search(
                    query, principal=principal, product=product, factory='中药一厂',
                    as_of=pd.Period(month, freq='M').end_time.date().isoformat(), top_k=5)
                st.caption(f"向量 {stats['vector_n']} 条 | BM25 {stats['bm25_n']} 条 | 图谱 {stats['graph_n']} 条 · 发布 {stats.get('release_id') or '无'}")
                if stats.get('degraded'):
                    st.info('检索模式：' + stats['retrieval_mode'] + '；' + '、'.join(stats.get('degradation_reasons', [])))
                if not final:
                    st.info('没有可用依据：' + stats.get('reason', '无匹配证据'))
                for index, item in enumerate(final, 1):
                    row = result_to_api(item)
                    with st.expander(f"{index}. {row['source']}"):
                        st.caption(f"版本 {row['version_id']} · 生效 {row['effective_from']} · 发布 {row['release_id']}")
                        st.write(row['text'])
            except (ValueError, PermissionError, OSError) as exc:
                st.error(str(exc))

# ---- ④ 归因分析自动生成（5.2.3：大模型 + RAG + ±10% 阈值告警）----
from attribution_gen import generate_attribution as _generate_attribution
_auto_analysis = (_generate_attribution(product, month, use_llm=False, d=d, principal=principal, root=app.root)
                  if can(principal, 'analysis.generate', factory='中药一厂', product=product) else None)
with st.container():
    st.subheader('成本归因分析')
    st.caption("归因结果需复核；金额与贡献度沿用看板同一计算结果。")
    c_btn, c_llm = st.columns([1, 2])
    with c_btn:
        do_attr = st.button("生成归因分析", type="primary", key="attr_btn", disabled=not can(principal, 'analysis.generate', factory='中药一厂', product=product))
    with c_llm:
        st.caption('结合成本明细与知识依据，生成可供复核的分析与建议。')
    if do_attr:
        from attribution_gen import generate_attribution
        with st.status('正在生成归因分析', expanded=True) as generation:
            generation.write('成本结论已计算。检索最多12秒，模型最多40秒，超时后显示已有分析。')
            authorize(principal, 'analysis.generate', factory='中药一厂', product=product)
            result = generate_attribution(product, month, use_llm=True, d=d, progress=generation.write, principal=principal, root=app.root)
            generation.update(label='归因分析已完成' if result['used_llm'] else '已生成数据分析，请查看生成状态', state='complete', expanded=False)
        st.session_state['attribution_result'] = (product, month, result)
        st.session_state['attribution_data_token'] = _data_token
    stored = st.session_state.get('attribution_result') if st.session_state.get('attribution_data_token') == _data_token else None
    if stored:
        try:
            for source in stored[2].get('sources', []):
                if source.get('version_id'):
                    if not app.knowledge().get(version_id=source['version_id']):
                        raise PermissionError('知识引用已不可用')
            from enterprise.knowledge_release import get_search_engine
            _, access_stats = get_search_engine(repository=app.knowledge()).search(
                product, principal=principal, product=product, factory='中药一厂',
                as_of=pd.Period(month, freq='M').end_time.date().isoformat(), top_k=1)
            allowed = set(access_stats.get('authorized_version_ids', []))
            if any(s.get('version_id') and s['version_id'] not in allowed for s in stored[2].get('sources', [])):
                raise PermissionError('知识引用已撤销或不再有效')
        except (ValueError, PermissionError, OSError):
            st.session_state.pop('attribution_result', None)
            stored = None
    result = stored[2] if stored and stored[:2] == (product, month) else _auto_analysis
    if result:
        st.caption(f"{'AI 辅助分析' if result['used_llm'] else '数据分析摘要'} · 待业务复核")
        if stored and stored[:2] == (product, month) and not result['used_llm']:
            messages = {'no_api_key':'尚未配置模型 API 密钥，请由系统管理员在系统设置中配置并验证。', 'model_unavailable':'模型未在时限内完成或接口调用失败，已保留计算结论。', 'model_rejected':'模型输出未通过数字与引用检查，已保留计算结论。'}
            st.info(messages.get(result['generation_status'], '当前使用确定性计算结果。'))
        with st.expander('生成耗时与诊断'):
            st.json({'status':result['generation_status'], 'timings':result.get('timings', {}), 'diagnostics':result.get('validation',{}).get('diagnostics',[]), 'model_run':result.get('model_run', {})})
            if result.get('model_run'):
                st.caption('模型可在同一45秒时限内最多修订一次；校验通过不替代人工审核。')
        if result["alerts"]:
            st.warning("⚠️ 波动阈值告警：以下要素环比变动超过 ±10%，已生成重点分析段落")
            for a in result["alerts"]:
                st.write(f"  · {a['要素']} 环比 {a['环比%']:+.2f}%")
        st.write(result.get('overview', ''))
        for section in result.get('sections', []):
            st.markdown(f"**{section['title']}**")
            for paragraph in section['text'].split('\n'):
                st.write(paragraph)
            ids = set(section.get('evidence_ids', []))
            with st.expander(f"查看{section['title']}的来源文档"):
                for document in result.get('source_documents', []):
                    if ids.intersection(document.get('evidence_ids', [])):
                        st.markdown(f"**{document['file']}**")
                        for loc in document.get('locations', []):
                            st.caption(' · '.join(f'{k}：{v}' for k,v in loc.get('key', {}).items()))
                for source in result.get('sources', []):
                    if source['id'] in ids and str(source['id']).startswith('R'):
                        st.caption(str(source.get('source', '')))
                        st.write(source.get('text', ''))
        if not result.get('sections'):
            st.write(result.get('concise_text', result['text']))
        from enterprise.snapshots import SnapshotRepository
        if st.button('保存本次分析快照', key='save_analysis_snapshot'):
            versions = {'data_fingerprint':_data_token,'knowledge_versions':[s['version_id'] for s in result['sources'] if s.get('version_id')], 'product_specification':spec}
            try:
                authorize(principal, 'analysis.generate', factory='中药一厂', product=product)
                snapshot_id = SnapshotRepository(app.root, principal=principal).save(result, d, principal.user_id, versions)
                st.success(f'已保存快照 {snapshot_id[:12]}，可到分析档案查看。')
            except (ValueError, PermissionError, OSError) as exc:
                st.error(str(exc))
        st.download_button("下载分析正文", result.get('concise_text', result['text']), file_name=f'{product}_{month}_归因.txt', key='download_attribution')
        with st.expander('查看完整数值分析'):
            st.write(result['text'])
        with st.expander("查看计算JSON与生成检查"):
            st.json(result['payload'])
            st.json(result.get('validation', {}))



st.caption(f"数据来源：中药一厂成本明细数据（五层验证保障数据质量）｜"
           f"产品 {len(prods)} 种 × 月份 {len(months)} 个（数据驱动动态窗口）｜"
           f"口径：瀑布图/贡献度/归因统一金额口径（变动额=单位成本×产量，元）｜"
           '业务入口：已认证统一应用服务')
#（注：内容由AI生成）
