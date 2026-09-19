"""Module 3 page. Navigation and page configuration belong to enterprise_app.py."""
import json
from pathlib import PureWindowsPath

import pandas as pd
import streamlit as st

from dashboard.echarts_helper import render_echarts_multi
from enterprise.benchmark import benchmark_options, build_benchmark, build_multi_product_summary
from enterprise.benchmark_ai import generate_benchmark_analysis
from enterprise.analysis_service import report_evidence, validated_model
from enterprise.cost_imports import digest, records
from enterprise.model_gateway import configuration
from enterprise.security import can
from app_pages._shared import authorize, page_context

principal, app = page_context('dashboard.read')
authorize(principal, 'data.read')


def _money(value, signed=False):
    return "—" if value is None else format(value, "+,.2f" if signed else ",.2f")


def _source_view(source):
    st.write(PureWindowsPath(source["file"]).name if source.get("file") else source.get("table", "数据表"))
    st.caption(" · ".join(f"{key}：{value}" for key, value in source.get("key", {}).items()))
    if source.get("line"):
        st.caption(f"原文件物理行：{source['line']}")
    if source.get('record_number'):
        st.caption(f"导入记录序号：{source['record_number']}（与 CSV 物理行分别记录）")
    if source.get('sheet'):
        st.caption('工作表：'+source['sheet'])
    if source.get('sha256'):
        st.caption('来源 SHA256：'+source['sha256'])
    if source.get("fields"):
        st.dataframe([source["fields"]], hide_index=True)


def _gap_chart(report):
    items = report["elements"]
    return {
        "animation": False,
        "title": {"text": "三要素标准化金额差", "subtext": "一厂－二厂 · 统一采用一厂产量 · 元", "left": "center", "top": 12,
                  "textStyle": {"fontSize": 16}, "subtextStyle": {"fontSize": 12}},
        "grid": {"left": 62, "right": 100, "top": 105, "bottom": 52, "containLabel": True},
        "tooltip": {"trigger": "axis", "confine": True, "axisPointer": {"type": "shadow"}},
        "xAxis": {"type": "value", "name": "元", "axisLabel": {"fontSize": 11}},
        "yAxis": {"type": "category", "data": [item["element"] for item in items]},
        "series": [{"name": "标准化金额差", "type": "bar", "barMaxWidth": 42,
                    "data": [{"value": item["normalized_amount"],
                              "itemStyle": {"color": "#c66a4a" if item["normalized_amount"] > 0 else "#237f83"},
                              "label": {"show": True, "position": "right" if item["normalized_amount"] >= 0 else "left", "fontSize": 11}}
                             for item in items]}],
        "media": [{"query": {"maxWidth": 500}, "option": {"grid": {"left": 40, "right": 68, "top": 110},
                                                                       "series": [{"label": {"show": False}}]}}],
    }


st.title("跨厂成本对标")
st.caption("中药一厂 / 中药二厂 · 同产品、同规格、同月份")
with st.spinner("读取两厂成本数据…"):
    tables = app.tables()
scopes = benchmark_options(tables)
if not scopes:
    st.info("暂无具备工厂、产品、规格和月份标识的成本数据，请先完成数据导入。")
    st.stop()

months = sorted({scope["month"] for scope in scopes})
c_month, c_product, c_spec = st.columns([1, 1.3, 1.5])
with c_month:
    month = st.selectbox("对标月份", months, index=len(months)-1, key="enterprise_benchmark_month")
products = sorted({scope["product"] for scope in scopes if scope["month"] == month})
with c_product:
    product = st.selectbox("对标产品", products, key="enterprise_benchmark_product")
specifications = sorted({scope["specification"] for scope in scopes if scope["month"] == month and scope["product"] == product})
with c_spec:
    specification = st.selectbox("产品规格", specifications, key="enterprise_benchmark_spec")

authorize(principal, 'dashboard.read', product=product)
report = build_benchmark(product, specification, month, tables)
can_analyze = all(can(principal, 'analysis.generate', factory=factory, product=product)
                  for factory in ('中药一厂', '中药二厂'))
benchmark_token = digest({'tables': records(tables), 'scope': repr(st.session_state['_principal_scope']),
                          'product': product, 'specification': specification, 'month': month})
analysis = generate_benchmark_analysis(product, specification, month, tables, use_llm=False)
stored = st.session_state.get('benchmark_analysis')
if stored and stored['token'] == benchmark_token and can_analyze:
    try:
        fresh_evidence = report_evidence(principal, product, specification, [month], root=app.root)
        permitted = {e['version_id'] for e in fresh_evidence}
        if all(not e.get('version_id') or e['version_id'] in permitted for e in stored['result'].get('evidence', [])):
            analysis = stored['result']
        else:
            st.session_state.pop('benchmark_analysis', None)
    except (ValueError, PermissionError, OSError):
        st.session_state.pop('benchmark_analysis', None)
tab_difference, tab_structure, tab_cause, tab_summary = st.tabs(["① 找差异", "② 拆结构", "③ 核查原因", "多产品汇总"])

with tab_difference:
    if not report["available"]:
        st.warning(report["reason"])
    else:
        columns = st.columns(4, border=True)
        with columns[0]:
            st.metric("一厂单位成本 · 元/盒", _money(report["home"]["unit_cost"]))
        with columns[1]:
            st.metric("二厂单位成本 · 元/盒", _money(report["peer"]["unit_cost"]))
        with columns[2]:
            st.metric("单位成本差 · 元/盒", _money(report["unit_gap"], True))
        with columns[3]:
            st.metric("相对二厂差异率", "不可计算" if report["gap_pct"] is None else f"{report['gap_pct']:+.2f}%")
        st.write(report["overview"])
        comparison = [{"指标": "产量（盒）", "中药一厂": f"{report['home']['volume']:,.0f}", "中药二厂": f"{report['peer']['volume']:,.0f}"},
                      {"指标": "实际总成本（元）", "中药一厂": _money(report['home']['total_cost']), "中药二厂": _money(report['peer']['total_cost'])}]
        st.dataframe(comparison, hide_index=True)
        st.caption("实际总成本仅作为规模背景。金额差统一按一厂产量计算，正值表示一厂较高，负值表示一厂较低。")
        with st.expander("比较口径与源记录"):
            st.write(report["formula"])
            for source in report["sources"]:
                _source_view(source)
            st.caption("差异率分母为二厂单位成本；二厂为零时不定义差异率。")

with tab_structure:
    if not report["available"]:
        st.info("两厂同口径数据齐备后，展示材料、人工和制造费用的差异结构。")
    else:
        render_echarts_multi([(_gap_chart(report), 420, True)])
        structure = [{"要素": item["element"], "一厂单位成本": _money(item["home_unit_cost"]),
                      "二厂单位成本": _money(item["peer_unit_cost"]), "单位差(元/盒)": _money(item["unit_gap"], True),
                      "标准化金额差(元)": _money(item["normalized_amount"], True),
                      "差异贡献度": "无定义" if item["contribution_pct"] is None else f"{item['contribution_pct']:.2f}%",
                      "一厂构成占比": "—" if item["home_share_pct"] is None else f"{item['home_share_pct']:.2f}%",
                      "二厂构成占比": "—" if item["peer_share_pct"] is None else f"{item['peer_share_pct']:.2f}%"}
                     for item in report["elements"]]
        st.dataframe(structure, hide_index=True)
        st.caption(f"标准化金额差合计 {_money(report['normalized_amount'], True)} 元；未舍入计算勾稽差额 {_money(report['reconciliation_difference'])} 元。")
        st.caption("差异贡献度＝要素标准化金额差÷全部标准化金额差。方向相反的项目可为负；净差额为零时不定义贡献度。")
        if abs(report["display_rounding_difference"]) > 0.000001:
            st.caption(f"分项按分显示产生的舍入差额：{report['display_rounding_difference']:+.2f}元。")

with tab_cause:
    if not report["available"]:
        st.info("先补齐两厂同品同规格同月数据，再定位差异原因。")
    else:
        st.subheader("已确定的结构差异")
        st.caption('金额与结构差异由源表计算；模型只补充待核查解释。')
        if st.button('生成 AI 跨厂原因分析', type='primary', disabled=not can_analyze, key='benchmark_generate_ai'):
            try:
                for factory in ('中药一厂', '中药二厂'):
                    authorize(principal, 'analysis.generate', factory=factory, product=product)
                with st.spinner('检索受控知识并校验模型解释…'):
                    evidence = report_evidence(principal, product, specification, [month], root=app.root)
                    model = configuration().public()
                    analysis = generate_benchmark_analysis(product, specification, month, tables, evidence=evidence,
                        model_fn=validated_model if model['configured'] else None,
                        model_version=model['model'] if model['configured'] else 'not_configured',
                        versions={'data_revision': app.current()['revision']})
                st.session_state['benchmark_analysis'] = {'token': benchmark_token, 'result': analysis}
            except (ValueError, PermissionError, OSError, RuntimeError) as exc:
                st.error(str(exc))
        st.caption(('AI 辅助解释' if analysis['used_llm'] else '规则分析') + ' · '+analysis['generation_status']+' · 待业务复核')
        if analysis.get('fallback_reason'):
            st.info(analysis['fallback_reason'])
        for item in sorted(report["elements"], key=lambda row: abs(row["normalized_amount"]), reverse=True):
            direction = "较高" if item["unit_gap"] > 0 else "较低" if item["unit_gap"] < 0 else "相同"
            st.write(f"**{item['element']}**：一厂单位成本{direction}，单位差{_money(item['unit_gap'], True)}元/盒，对标准化金额差的影响为{_money(item['normalized_amount'], True)}元。")
        st.info(report["detail_note"])
        if report["home_materials"]:
            with st.expander("一厂原料核查明细"):
                st.dataframe([{"原材料": item["material"], "一厂单位消耗成本(元/盒)": _money(item["unit_consumption_cost"]),
                               "一厂金额(元)": _money(item["home_amount"]), "二厂对应明细": "未提供"}
                              for item in report["home_materials"]], hide_index=True)
                for item in report["home_materials"]:
                    st.caption(f"{item['material']} · " + (PureWindowsPath(item["source"]["file"]).name if item["source"].get("file") else "material") + " · " + month + " · " + specification)
        for section in analysis.get('sections', []):
            with st.expander(section['title']+' · 待核查解释', expanded=True):
                st.write(section['hypothesis'])
                st.caption('缺少证据：'+'、'.join(section['missing_evidence']))
                for evidence in analysis.get('evidence', []):
                    if evidence['id'] in section['evidence_ids']:
                        st.caption('引用 '+evidence['id'])
                        st.write(evidence['text'])
                        st.json(evidence['source'], expanded=False)
        st.subheader('核查建议')
        suggestions = analysis['suggestions']
        if not suggestions:
            st.write('三要素单位成本均无差异，本期未形成差异核查建议。')
        for suggestion in suggestions:
            st.markdown(f"**{suggestion['title']} · {suggestion['owner_role']}**")
            st.write(suggestion['action'])
            st.caption(suggestion['source'])
        st.download_button('下载核查建议 JSON', json.dumps(suggestions, ensure_ascii=False, indent=2),
                           file_name=f'{product}_{month}_对标核查建议.json', mime='application/json', key='benchmark_suggestions_download')
        if suggestions and can(principal, 'task.create') and can_analyze:
            choice = st.selectbox('转为整改任务的建议', range(len(suggestions)), format_func=lambda i: suggestions[i]['title'], key='benchmark_task_suggestion')
            if st.button('AI 生成持久草稿任务', key='benchmark_generate_task'):
                try:
                    suggestion = suggestions[choice]
                    seed = {'task_title': suggestion['title'], 'assignee': {'name': '', 'department': '', 'role': suggestion['owner_role']},
                            'source': {'analysis_type': '跨厂对标', 'analysis_month': month, 'product': product,
                                       'finding': report['overview']+'；'+suggestion['source']},
                            'priority': suggestion.get('priority', '中'), 'deadline': '', 'suggestion': suggestion['action'],
                            'factories': ['中药一厂', '中药二厂'], 'evidence_ids': suggestion.get('evidence_ids', []),
                            'analysis_run_id': 'benchmark-'+benchmark_token[:24]}
                    with st.spinner('生成并保存任务草稿…'):
                        task = app.tasks().generate(seed, actor=principal)
                    st.session_state['task_next_selection'] = task['task_id']
                    st.success('任务草稿已保存：'+task['generation']['label']+'。请填写责任人并提交审核。')
                except (ValueError, PermissionError, OSError, RuntimeError) as exc:
                    st.error(str(exc))
            st.page_link('app_pages/tasks.py', label='打开整改任务', icon=':material/task_alt:')
        with st.expander('查看对标计算与生成检查'):
            st.json(analysis)

with tab_summary:
    st.caption("逐项按产品和规格对齐，仅比较本月两厂同口径记录。不同产品不合并单位成本或平均差异率。")
    summary = build_multi_product_summary(month, tables)
    st.dataframe(pd.DataFrame(summary), hide_index=True)
    st.download_button("下载本月对标汇总", json.dumps(summary, ensure_ascii=False, indent=2),
                       file_name=f"{month}_跨厂对标汇总.json", mime="application/json", key="benchmark_summary_download")
