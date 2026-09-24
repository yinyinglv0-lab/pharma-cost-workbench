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

from dashboard.charts import change_detail, heatmap_rate, structure, trend_elements, waterfall
import pandas as pd
from dashboard.data_layer import build_dashboard_data, discover_months, discover_products
from dashboard.chart_link import render_linked_echarts, chart_context_id, rag_question
from app_pages._shared import authorize, home_tables, page_context
from app_pages.design import kpis, money_columns, page_header
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

# This page alone reserves the fixed header's 3.75rem plus breathing room.
# Do not shrink the shared mobile safe area or alter other pages' geometry.
st.html('''<style>
.stMainBlockContainer:has(.st-key-dashboard_header) {
    padding-top: calc(4.5rem + env(safe-area-inset-top, 0px));
}
</style>''')
with st.container(key='dashboard_header'):
    page_header('产品成本分析', '分析与行动 / 中药一厂月度经营分析')

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
raw = build_dashboard_data(product, d, specification=spec or None)
data = {'product':product, 'series':[{k:_disp(v) for k,v in s.items()} for s in raw['series']],
        'amount_change':raw['amount_change'], 'warnings':raw['warnings']}
import hashlib
from enterprise.knowledge_release import ReleaseRepository
try:
    knowledge_versions = app.knowledge().history()
    knowledge_token = '|'.join(v['version_id'] for v in knowledge_versions)
    releases = ReleaseRepository(app.root).history(principal=principal)
    knowledge_token += '|' + '|'.join(r['release_id'] + ':' + r['status'] + ':' + str(r.get('manifest_sha256', '')) for r in releases)
    from enterprise.knowledge_release import get_search_engine
    _active_release_id = get_search_engine(repository=app.knowledge()).readiness(principal=principal)['release_id']
    knowledge_token += '|active:' + str(_active_release_id)
except (ValueError, PermissionError, OSError):
    knowledge_token = 'catalog_unavailable'
    _active_release_id = None
    st.session_state.pop('attribution_result', None)
    st.warning('知识目录暂不可用，成本图表仍可查看；归因将使用结构化数据。')
from enterprise.snapshots import current_provenance
from enterprise.domain_profiles import load_domain_profile, profile_fingerprint
from enterprise.model_gateway import configuration
_current_model = configuration(task='attribution').public()
_pipeline_token = (repr(current_provenance()) + profile_fingerprint(load_domain_profile())
                   + repr(_current_model))
_data_token = hashlib.sha256((''.join(df.to_json(orient='split', force_ascii=False) for df in d.values()) + knowledge_token + _pipeline_token + repr(st.session_state['_principal_scope'])).encode()).hexdigest()

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
cards = [
    ('单位成本 · 元/盒', f"{current['单位成本']:.2f}", '材料 + 人工 + 制造费用', ''),
    ('本月产量 · 盒', f"{current['产量']:,.0f}", f'{month} 实际产出', ''),
    ('本月总成本 · 元', f"{current['总成本']:,.2f}", '单位成本 × 产量', ''),
    ('总成本环比变动 · 元', '—' if delta is None else f'{delta:+,.2f}',
     '缺少连续上月' if delta is None else ('较上月增加' if delta > 0 else '较上月减少' if delta < 0 else '与上月持平'),
     'rise' if delta and delta > 0 else 'fall' if delta and delta < 0 else ''),
]
kpis(cards)
st.caption(build_attribution_summary(data, month))
with st.expander('查看分析口径'):
    st.caption('要素变动额 = 本月要素单位成本 × 本月产量 − 上月要素单位成本 × 上月产量；贡献度 = 要素金额变动 ÷ 总金额变动 × 100%。')

# Industry anchors come from the governed reference routes, not an unregistered
# CSV read or an AI estimate. Only home-factory rows are passed on this page.
if can(principal, 'knowledge.read', factory='中药一厂', product=product):
    from enterprise.reference_service import industry_view
    from app_pages.reference_view import render_industry_view
    try:
        industry_state = industry_view(app, product, spec, month, tables=d)
        anchors = [row for row in industry_state['result'].get('rows', [])
                   if row['calculation'] == 'unit_conversion' and row['home']['value'] is not None]
        if anchors:
            anchor = anchors[0]
            st.caption(f"行业参考锚点：本厂所选月{anchor['home']['value']:.4f}{anchor['unit']}；"
                       f"类别P50 {anchor['p50']['value']:.4f}{anchor['unit']}，"
                       f"位置{anchor['home']['position_label']}。年度类别参照，不是同品月度排名。")
        with st.expander('行业基准与本厂雷达对照'):
            render_industry_view(industry_state, app, product=product, specification=spec,
                                 month=month, key='dashboard_industry', show_peer=False)
    except (ValueError, PermissionError, OSError) as exc:
        st.caption('行业参考暂不可用：' + str(exc))

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

# A context change invalidates both the old question and its evidence. Point
# events carry this context and are re-derived from server-side chart options.
_chart_context = chart_context_id(product, spec, month, _data_token + ':' + str(st.session_state.get('heat_elem', '环比变化率')))
if st.session_state.get('_rag_context') != _chart_context:
    st.session_state['_rag_context'] = _chart_context
    _initial_compare = '同比' if st.session_state.get('heat_elem') == '同比变化率' else '环比'
    st.session_state['rag_query'] = rag_question(product, spec, month, comparison=_initial_compare)
    st.session_state['_rag_focus'] = {'month': month, 'element': '单位成本', 'comparison':_initial_compare}
    # Explicit assignments send set_value to the existing browser widgets.
    # Deleting keys alone resets Python state but can leave stale frontend input.
    st.session_state['knowledge_focus_month'] = month
    st.session_state['knowledge_focus_element'] = '单位成本'
    st.session_state['knowledge_focus_comparison'] = _initial_compare
    st.session_state.pop('_rag_results', None)
    st.session_state.pop('_rag_last_event', None)
_chart_events = []
st.caption('点击趋势点、成本扇区、变动柱或热力单元格，可查看对应月份与要素的知识依据。')
trend_tab, amount_tab, rate_tab = st.tabs(['趋势与结构', '金额变动分解', '要素变化率'])
with trend_tab:
    _chart_events.append(render_linked_echarts([(line_opt, 420), (pie_opt, 420)], context_id=_chart_context,
        month=month, available_months=months, key='dashboard_trend_charts'))
with amount_tab:
    if wf_opt is not None:
        full_amount_tab, change_detail_tab = st.tabs(['金额分解视图（截断轴）', '差额视图（零基线）'])
        with full_amount_tab:
            st.caption('Y 轴截断以突出变动量；起止总额与三要素变动仍取自同一核算记录。')
            _chart_events.append(render_linked_echarts([(wf_opt, 420, True)], context_id=_chart_context,
                month=month, available_months=months, key='dashboard_amount_charts'))
        with change_detail_tab:
            st.caption(f"起止总额另列：上月 {change['上月总成本']:,.2f} 元 → 本月 {change['本月总成本']:,.2f} 元；"
                       f"净变动 {change['总变动额']:+,.2f} 元。")
            st.caption('只放大差额：各柱从零开始，纵轴关于零对称；不是截断总额轴的瀑布图。'
                       '与金额分解视图、归因共用同一金额变动及贡献度；负贡献表示抵消净方向，不删除、不取绝对值。')
            _detail_opt = change_detail(data, product, month)
            _chart_events.append(render_linked_echarts([(_detail_opt, 420, True)], context_id=_chart_context,
                month=month, available_months=months, key='dashboard_change_detail_charts'))
            st.dataframe([{'要素': element, '金额变动（元）': change[element + '变动额'],
                           '贡献度（%）': change['贡献度'][element]}
                          for element in ('材料', '人工', '制费')]
                         + [{'要素': '净变动', '金额变动（元）': change['总变动额'], '贡献度（%）': None}],
                         hide_index=True, column_config={
                             '金额变动（元）': st.column_config.NumberColumn(format='%+.2f'),
                             '贡献度（%）': st.column_config.NumberColumn(format='%+.2f')})
            st.caption('贡献度 = 要素金额变动 ÷ 净变动 × 100%；净变动为零时贡献度无定义（表中留空），金额仍完整显示。')
    else:
        st.info(f'{month} 缺少连续上月数据，无法绘制金额变动分解。')
with rate_tab:
    st.subheader('成本要素变化率')
    heat_elem = st.segmented_control('比较口径', ['环比变化率', '同比变化率'],
                                     default='环比变化率', key='heat_elem',
                                     selection_mode='single')
    rate = 'yoy' if heat_elem == '同比变化率' else 'mom'
    heat_opt = heatmap_rate(d, product, window, rate)
    _chart_events.append(render_linked_echarts([(heat_opt, 420, True)], context_id=_chart_context,
        month=month, available_months=months, key='dashboard_rate_charts'))

with st.expander('按月份和要素选择知识依据'):
    # Native controls provide the same operation for keyboard and assistive tech.
    _c_month, _c_element, _c_compare = st.columns(3)
    _focus_month = _c_month.selectbox('知识依据月份', months, index=months.index(month), key='knowledge_focus_month')
    _focus_element = _c_element.selectbox('知识依据要素', ['单位成本','材料','人工','制费'], key='knowledge_focus_element')
    _focus_compare = _c_compare.selectbox('知识依据比较口径', ['环比','同比'], index=1 if st.session_state.get('heat_elem') == '同比变化率' else 0, key='knowledge_focus_comparison')
    if st.button('检索所选月份与要素', key='knowledge_focus_search'):
        from uuid import uuid4
        _chart_events.append({'month':_focus_month, 'element':_focus_element, 'comparison':_focus_compare, 'event_id':uuid4().hex})

_rag_auto_search = False
for _event in _chart_events:
    if _event and _event['event_id'] != st.session_state.get('_rag_last_event'):
        st.session_state['_rag_last_event'] = _event['event_id']
        st.session_state['_rag_focus'] = _event
        st.session_state['rag_query'] = rag_question(product, spec, _event['month'], _event['element'], comparison=_event.get('comparison','环比'))
        st.session_state.pop('_rag_results', None)
        _rag_auto_search = True

with st.expander("查看同源明细与导出数据"):
    st.dataframe(data['series'], hide_index=True, column_config={**money_columns('单位成本', '总成本', '材料', '人工', '制费'),
                                                              '产量': st.column_config.NumberColumn(format='%,d')})
    import json
    st.download_button("下载金额归因 JSON", json.dumps(change, ensure_ascii=False, indent=2),
                       file_name=f"{product}_{month}_金额归因.json", mime="application/json")


# ---- ⑤ 对当前产品、工厂和业务日期执行受控发布检索 ----
if can(principal, 'knowledge.read'):
    with st.expander("知识依据检索", expanded=_rag_auto_search or bool(st.session_state.get('_rag_results'))):
        _focus = st.session_state['_rag_focus']
        query = st.text_input("检索问题", key="rag_query")
        st.caption(f"当前依据范围：{product} · {_focus['month']} · {_focus['element']} · {_focus.get('comparison','环比')}。修改文字不会自动改变有效日期，可在“按月份和要素选择知识依据”中调整。")
        _manual_search = st.button("检索知识库", type="primary", key="rag_btn")
        if _manual_search or _rag_auto_search:
            authorize(principal, 'knowledge.read', factory='中药一厂', product=product)
            from enterprise.knowledge_release import get_search_engine, result_to_api
            from enterprise.knowledge_applicability import evidence_policy
            try:
                with st.spinner('正在查找对应知识依据…'):
                    final, stats = get_search_engine(repository=app.knowledge()).search(
                        query, principal=principal, product=product, factory='中药一厂',
                        as_of=pd.Period(_focus['month'], freq='M').end_time.date().isoformat(), top_k=5)
                final = [row for row in final if evidence_policy(row['meta'], product, spec,
                    as_of=pd.Period(_focus['month'], freq='M').end_time.date().isoformat())['included']]
                st.session_state['_rag_results'] = {'query':query, 'rows':[result_to_api(row) for row in final],
                                                    'stats':stats, 'context':_chart_context}
            except (ValueError, PermissionError, OSError) as exc:
                st.session_state.pop('_rag_results', None)
                st.error(str(exc))
        _cached = st.session_state.get('_rag_results')
        if _cached and _cached['context'] == _chart_context:
            # Revalidate current version visibility before reusing session text.
            try:
                if _cached['stats'].get('release_id') != _active_release_id:
                    raise PermissionError('正式知识发布已切换，请重新检索。')
                for row in _cached['rows']:
                    if not app.knowledge().get(version_id=row['version_id']):
                        raise PermissionError('知识版本已不可用，请重新检索。')
                stats = _cached['stats']
                st.caption(f"向量 {stats['vector_n']} 条 | BM25 {stats['bm25_n']} 条 | 图谱 {stats['graph_n']} 条")
                if _cached['query'] != query:
                    st.caption('以下为上次检索结果；修改问题后点击“检索知识库”更新。')
                if stats.get('degraded'):
                    st.info('本次检索使用可用支路：' + '、'.join(stats.get('degradation_reasons', [])))
                if not _cached['rows']:
                    st.info('当前范围没有可用知识依据，请补充相关资料。')
                for index, row in enumerate(_cached['rows'], 1):
                    with st.expander(f"{index}. {row['source']}"):
                        st.caption(f"版本 {row['version_id']} · 生效 {row['effective_from']}")
                        st.write(row.get('content', row.get('text', '')))
            except (ValueError, PermissionError, OSError) as exc:
                st.session_state.pop('_rag_results', None)
                st.info('知识依据已更新，请重新检索。')

# ---- ④ 归因分析自动生成（5.2.3：大模型 + RAG + ±10% 阈值告警）----
with st.container():
    st.subheader('成本归因分析')
    st.caption("归因结果需复核；金额与贡献度沿用看板同一计算结果。")
    fact_slot = st.empty()
    st.caption(f"本次模型：{_current_model['model']} · 任务配置：{_current_model.get('profile', 'default')}；是否实际调用以生成后的回执为准。")
    # 硬门禁：所选模型密钥/授权未配置成功 → 拒绝生成归因文本（不静默降级）
    from enterprise.model_settings import require_generation_model
    try:
        require_generation_model(task='attribution')
        _model_ready = True
        _model_ready_msg = ''
    except Exception as exc:
        _model_ready = False
        _model_ready_msg = str(exc)
    if not _model_ready:
        st.error('⚠️ ' + _model_ready_msg)
    do_attr = st.button("生成归因分析", type="primary", key="attr_btn",
                        disabled=not can(principal, 'analysis.generate', factory='中药一厂', product=product) or not _model_ready)
    if do_attr and _model_ready:
        from attribution_gen import generate_attribution
        with st.status('正在生成归因分析', expanded=True) as generation:
            generation.write('先完成向量＋BM25受控检索，再调用所选模型；模型阶段有硬期限（未选注册模型 95 秒，注册模型最长 255 秒）。检索未就绪时保留事实并明确提示，不冒充RAG成功。')
            authorize(principal, 'analysis.generate', factory='中药一厂', product=product)
            from app_pages._shared import wait_for_analysis_warmup
            wait_for_analysis_warmup(principal, app.knowledge())
            result = generate_attribution(product, month, use_llm=True, d=d, progress=generation.write, principal=principal, root=app.root, require_hybrid=True)
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
    result = stored[2] if stored and stored[:2] == (product, month) else None
    if result is None:
        with fact_slot.container():
            from enterprise.numeric import format_number, format_percent
            st.dataframe([{'要素': label, '本月单位成本(元/盒)': format_number(current[element]),
                           '金额环比变动(元)': '—' if change[element+'变动额'] is None else format_number(change[element+'变动额'], signed=True),
                           '金额变动贡献度': '无定义' if change['贡献度'][element] is None else format_percent(change['贡献度'][element])+'%'}
                          for element, label in (('材料', '直接材料'), ('人工', '直接人工'), ('制费', '制造费用'))], hide_index=True)
            st.caption('这里只展示已计算事实；点击生成后查看解释、建议与证据缺口。')
    if result:
        st.caption(f"{'AI 辅助分析' if result['used_llm'] else '数据分析摘要'} · 待业务复核")
        if stored and stored[:2] == (product, month) and not result['used_llm']:
            messages = {'retrieval_unavailable':'混合检索未就绪，本次未调用模型。请完成真实向量发布和本地模型预热后重试；数值事实保留。', 'no_api_key':'尚未配置模型 API 密钥，请由系统管理员在系统设置中配置并验证。', 'model_unavailable':'模型未在时限内完成或接口调用失败，已保留计算结论。', 'model_rejected':'模型输出未通过数字与引用检查，已保留计算结论。'}
            st.info(messages.get(result['generation_status'], '当前使用确定性计算结果。'))
        with st.expander('生成耗时与诊断'):
            st.json({'status':result['generation_status'], 'timings':result.get('timings', {}), 'diagnostics':result.get('validation',{}).get('diagnostics',[]), 'model_run':result.get('model_run', {})})
            if result.get('model_run'):
                st.caption('模型可在同一时限内最多修订一次（需在「模型配置」选择修正模型，未选则不做修正轮）；校验通过不替代人工审核。')
        if result["alerts"]:
            st.warning("⚠️ 波动阈值告警：以下要素环比变动超过 ±10%，已生成重点分析段落")
            for a in result["alerts"]:
                st.write(f"  · {a['要素']} 环比 {a['环比%']:+.2f}%")
        from app_pages.citations import render_layered_analysis
        render_layered_analysis(result.get('sections', []), result.get('sources', []),
                                overview=result.get('overview', ''), key='attribution_citations',
                                limitations=result.get('limitations', []),
                                followup_criteria=result.get('followup_criteria', ''), analysis_kind='attribution')
        if not result.get('sections'):
            st.write(result.get('concise_text', result['text']))
        from app_pages.reference_view import render_market_references
        render_market_references(result.get('reference_evidence', result.get('sources', [])),
                                 product=product, specification=spec, month=month, key='attribution_reference')
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
