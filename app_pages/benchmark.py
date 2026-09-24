"""Module 3 page. Navigation and page configuration belong to enterprise_app.py."""
import json
from pathlib import PureWindowsPath

import pandas as pd
import streamlit as st

from dashboard.echarts_helper import render_echarts_multi
from enterprise.benchmark import benchmark_options, build_benchmark, build_multi_product_summary
from enterprise.benchmark_ai import generate_benchmark_analysis
from enterprise.analysis_service import benchmark_retrieval_facts, report_evidence, validated_model
from enterprise.cost_imports import digest, records
from enterprise.model_gateway import configuration
from enterprise.security import can
from enterprise.numeric import format_number, format_percent
from app_pages._shared import authorize, generation_notice, page_context

principal, app = page_context('dashboard.read')
authorize(principal, 'data.read')


def _money(value, signed=False):
    return "—" if value is None else format_number(value, signed=signed)


def _percent(value, signed=False):
    return '无定义' if value is None else format_percent(value, signed=signed) + '%'


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
from enterprise.build_info import source_fingerprint
_current_model = configuration(task='benchmark').public()
benchmark_token = digest({'tables': records(tables), 'scope': repr(st.session_state['_principal_scope']),
                          'product': product, 'specification': specification, 'month': month,
                          'code_fingerprint': source_fingerprint(), 'model_configuration': _current_model})
analysis = generate_benchmark_analysis(product, specification, month, tables, use_llm=False)
stored = st.session_state.get('benchmark_analysis')
analysis_generated = False
if stored and stored['token'] == benchmark_token and can_analyze:
    try:
        fresh_evidence = report_evidence(principal, product, specification, [month], root=app.root, facts=benchmark_retrieval_facts(report), require_hybrid=True)
        permitted = {e['version_id'] for e in fresh_evidence}
        if all(not e.get('version_id') or e['version_id'] in permitted for e in stored['result'].get('evidence', [])):
            analysis = stored['result']
            analysis_generated = True
        else:
            st.session_state.pop('benchmark_analysis', None)
    except (ValueError, PermissionError, OSError):
        st.session_state.pop('benchmark_analysis', None)

# Re-resolve authorized industry rows on every rerun, including revocations and
# newly published source versions. The service only narrows the retrieval routes.
from enterprise.reference_service import industry_view
industry_state = {'result': {'available': False, 'reason': '当前身份无行业参考读取权限。'}}
if can(principal, 'knowledge.read', factory='中药一厂', product=product):
    try:
        industry_state = industry_view(app, product, specification, month, tables=tables)
    except (ValueError, PermissionError, OSError) as exc:
        industry_state = {'result': {'available': False, 'reason': '行业参考暂不可用：' + str(exc)}}

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
            st.metric("相对二厂差异率", _percent(report.get('gap_pct_exact', report['gap_pct']), True))
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

        st.subheader('行业参考（三方对标）')
        from app_pages.reference_view import render_industry_view
        render_industry_view(industry_state, app, product=product, specification=specification,
                             month=month, key='benchmark_industry', show_peer=True)
        with st.expander('两厂分别与去年同月比较'):
            for side, row in report.get('year_over_year', {}).items():
                name = '中药一厂' if side == 'home' else '中药二厂'
                if row.get('available'):
                    st.write(f"{name}：对比{row['comparison_month']}，单位成本差{_money(row['unit_delta'], True)}元/盒，"
                             f"单位成本同比{_percent(row['unit_yoy_pct_exact'], True)}。")
                    st.caption(row['boundary'])
                else:
                    st.caption(name + '：' + row.get('reason', '缺少上年同月记录'))

with tab_structure:
    if not report["available"]:
        st.info("两厂同口径数据齐备后，展示材料、人工和制造费用的差异结构。")
    else:
        render_echarts_multi([(_gap_chart(report), 420, True)])
        structure = [{"要素": item["element"], "一厂单位成本": _money(item["home_unit_cost"]),
                      "二厂单位成本": _money(item["peer_unit_cost"]), "单位差(元/盒)": _money(item["unit_gap"], True),
                      "差异率(%)": _percent(item.get('gap_pct_exact', item.get('gap_pct')), True),
                      "标准化金额差(元)": _money(item["normalized_amount"], True),
                      "差异贡献度": _percent(item.get('contribution_pct_exact', item['contribution_pct'])),
                      "一厂构成占比": _percent(item.get('home_share_pct_exact', item['home_share_pct'])),
                      "二厂构成占比": _percent(item.get('peer_share_pct_exact', item['peer_share_pct']))}
                     for item in report["elements"]]
        st.dataframe(structure, hide_index=True)
        st.caption(f"标准化金额差合计 {_money(report['normalized_amount'], True)} 元；未舍入计算勾稽差额 {_money(report['reconciliation_difference'])} 元。")
        st.caption("差异贡献度＝要素标准化金额差÷全部标准化金额差。方向相反的项目可为负；净差额为零时不定义贡献度。")
        if abs(report["display_rounding_difference"]) > 0.000001:
            st.caption(f"分项按分显示产生的舍入差额：{report['display_rounding_difference']:+.2f}元。")
        st.subheader('查看要素明细')
        st.caption('同名明细按工厂、产品、规格和月份配对。缺少任一厂明细时，差额留空；标准化金额统一采用一厂产量。')
        for element, branch in report.get('paired_drilldown', {}).items():
            with st.expander(element + '明细与来源'):
                detail_rows = branch['rows']
                st.dataframe([{'明细':row['name'],
                    '一厂单位费用(元/盒)':_money(row['home']['unit_cost']) if row['home'] else '缺资料',
                    '二厂单位费用(元/盒)':_money(row['peer']['unit_cost']) if row['peer'] else '缺资料',
                    '单位差(元/盒)':_money(row['unit_gap'], True),
                    '标准化金额差(元)':_money(row['normalized_amount'], True),
                    '数据状态':'可配对' if row['status']=='paired' else row['gap_reason']}
                    for row in detail_rows], hide_index=True)
                if not branch['complete']:
                    st.caption(f"已配对 {branch['paired_count']} 项；尚未落实到两厂配对细目的金额差为 {_money(branch['unallocated_normalized_amount'], True)} 元。")
                for diagnostic in branch.get('diagnostics', []):
                    st.warning(diagnostic)
                if detail_rows:
                    selected_detail = st.selectbox('查看明细原始记录', range(len(detail_rows)),
                        format_func=lambda i, rows=detail_rows:rows[i]['name'], key='benchmark_detail_'+element)
                    detail = detail_rows[selected_detail]
                    for side, name in (('home','中药一厂'),('peer','中药二厂')):
                        if detail[side]:
                            st.caption(name)
                            _source_view(detail[side]['source'])
                            if detail[side].get('metrics'):
                                metric_names = {'hours':'总工时（小时）', 'hours_per_box':'单位工时（小时/盒）',
                                                'cost_per_hour':'归集费用（元/小时）', 'output_per_hour':'每工时产出（盒/小时）'}
                                st.dataframe([{metric_names.get(k,k):v for k,v in detail[side]['metrics'].items()}], hide_index=True)
                st.download_button('下载'+element+'配对明细', json.dumps(branch,ensure_ascii=False,indent=2),
                    file_name=f'{product}_{month}_{element}_配对明细.json', mime='application/json', key='benchmark_detail_download_'+element)

with tab_cause:
    if not report["available"]:
        st.info("先补齐两厂同品同规格同月数据，再定位差异原因。")
    else:
        st.subheader("已确定的结构差异")
        st.caption('金额与结构差异由源表计算；模型只补充待核查解释。')
        # 着重一厂问题：一厂高于二厂的要素按影响绝对值排序，作为核查重点
        home_issues = [row for row in report['elements'] if (row.get('unit_gap') or 0) > 0]
        if home_issues:
            with st.expander(f'一厂问题小结（一厂高于二厂的要素，{len(home_issues)} 项，需重点核查）', expanded=True):
                for row in sorted(home_issues, key=lambda r: -abs(r['normalized_amount'])):
                    st.markdown(
                        f"- **{row['element']}**：一厂单位成本 {_money(row['home_unit_cost'])} 元/盒，"
                        f"高于二厂 {_money(row['unit_gap'])} 元/盒"
                        f"（差异率 +{row['gap_pct_display']}%，占跨厂差异 {row['contribution_pct_display']}%）")
        fact_slot = st.empty()
        st.caption(f"本次模型：{_current_model['model']} · 任务配置：{_current_model.get('profile', 'default')}；生成后可查看实际调用回执。")
        # 硬门禁：模型未配置成功（密钥+授权）→ 拒绝生成对标归因文本
        from enterprise.model_settings import require_generation_model
        try:
            require_generation_model(task='benchmark')
            _bench_model_ready = True
            _bench_model_ready_msg = ''
        except Exception as exc:
            _bench_model_ready = False
            _bench_model_ready_msg = str(exc)
        if not _bench_model_ready:
            st.error('⚠️ ' + _bench_model_ready_msg)
        if st.button('生成跨厂原因分析', type='primary',
                     disabled=not can_analyze or not _bench_model_ready,
                     key='benchmark_generate_ai'):
            try:
                for factory in ('中药一厂', '中药二厂'):
                    authorize(principal, 'analysis.generate', factory=factory, product=product)
                from app_pages._shared import wait_for_analysis_warmup
                wait_for_analysis_warmup(principal, app.knowledge())
                with st.spinner('检索受控知识并校验模型解释…'):
                    evidence = report_evidence(principal, product, specification, [month], root=app.root, facts=benchmark_retrieval_facts(report), require_hybrid=True)
                    model = configuration(task='benchmark').public()
                    analysis = generate_benchmark_analysis(product, specification, month, tables, evidence=evidence,
                        model_fn=validated_model if model['configured'] else None,
                        model_version=model['model'] if model['configured'] else 'not_configured',
                        versions={'data_revision': app.current()['revision']})
                st.session_state['benchmark_analysis'] = {'token': benchmark_token, 'result': analysis}
                analysis_generated = True
            except (ValueError, PermissionError, OSError, RuntimeError) as exc:
                st.error(str(exc))
        if not analysis_generated:
            with fact_slot.container():
                st.dataframe([{'要素': row['element'], '一厂单位成本(元/盒)': _money(row['home_unit_cost']),
                               '二厂单位成本(元/盒)': _money(row['peer_unit_cost']),
                               '单位差(元/盒)': _money(row['unit_gap'], True),
                               '标准化金额差(元)': _money(row['normalized_amount'], True),
                               '差异贡献度': _percent(row.get('contribution_pct_exact', row.get('contribution_pct')))}
                              for row in report['elements']], hide_index=True)
                st.caption('以上为源表直接计算的确定性事实；点击生成后才显示跨厂解释、证据缺口和核查任务。')
        else:
            st.caption(('AI 辅助解释' if analysis['used_llm'] else '规则分析') + ' · 待业务复核')
            if not analysis['used_llm']:
                st.info(generation_notice(analysis.get('generation_status')))
                with st.expander('模型生成检查与下一步'):
                    st.caption('技术状态码：' + str(analysis.get('generation_status')))
                    st.caption('对标金额和来源仍由程序计算；模型解释未通过时不会替换确定性结果。')
                    if can(principal, 'system.read'):
                        st.page_link('app_pages/settings.py', label='打开系统设置检查模型', icon=':material/settings:')
            with st.expander('实际模型调用与校验回执', expanded=False):
                st.json({'status': analysis.get('generation_status'), 'model_run': analysis.get('model_run', {}),
                         'diagnostics': analysis.get('validation', {}).get('diagnostics', [])})
                st.caption('requested_model是请求模型，model来自服务商实际响应；配置已保存不等于调用成功。结构校验不替代归因语义人工审核。')
            from app_pages.citations import render_layered_analysis
            render_layered_analysis(analysis.get('sections', []), analysis.get('evidence', []),
                                    overview=report['overview'], key='benchmark_citations', generated=True,
                                    followup_criteria=analysis.get('followup_criteria', ''), analysis_kind='benchmark')
            from app_pages.reference_view import render_market_references
            render_market_references(analysis.get('evidence', []), product=product,
                                     specification=specification, month=month, key='benchmark_reference')
            if report["home_materials"]:
                # 一厂原料环比波动：上月/本月单位消耗成本对比（市场参考价对照见下方市场引用区）
                year, m = int(month[:4]), int(month[5:7])
                prev = f"{year}-{m-1:02d}" if m > 1 else f"{year-1}-12"
                prev_report = build_benchmark(product, specification, prev, tables)
                prev_materials = ({item["material"]: item.get("unit_consumption_cost")
                                   for item in prev_report.get("home_materials", [])}
                                  if prev_report.get("available") else {})
                def _mom(before, after):
                    if before in (None, 0) or after is None:
                        return "缺少上月可比值"
                    return f"{(after - before) / before * 100:+.1f}%"
                with st.expander("一厂原料核查明细（环比波动）"):
                    st.dataframe([{"原材料": item["material"],
                                   "上月单位消耗成本(元/盒)": _money(prev_materials.get(item["material"])),
                                   "本月单位消耗成本(元/盒)": _money(item["unit_consumption_cost"]),
                                   "环比": _mom(prev_materials.get(item["material"]), item["unit_consumption_cost"]),
                                   "一厂金额(元)": _money(item["home_amount"]), "二厂对应明细": "未提供"}
                                  for item in report["home_materials"]], hide_index=True)
                    st.caption('原料级环比来自一厂源明细；市场参考价与折算单耗请在下方"市场参考"区结合原文核对，'
                               '不把参考价当作实际采购结算价。')
                    for item in report["home_materials"]:
                        st.caption(f"{item['material']} · " + (PureWindowsPath(item["source"]["file"]).name if item["source"].get("file") else "material") + " · " + month + " · " + specification)
            st.subheader('核查任务')
            with st.expander('查看核查建议并选择是否生成任务', expanded=False):
                suggestions = analysis['suggestions']
                if not suggestions:
                    st.write('三要素单位成本均无差异，本期未形成差异核查建议。')
                for suggestion in suggestions:
                    st.markdown(f"**{suggestion['title']} · {suggestion['owner_role']}**")
                    st.caption(suggestion['source'])
                st.download_button('下载核查建议 JSON', json.dumps(suggestions, ensure_ascii=False, indent=2),
                                   file_name=f'{product}_{month}_对标核查建议.json', mime='application/json', key='benchmark_suggestions_download')
                if suggestions and can(principal, 'task.create') and can_analyze:
                    choice = st.selectbox('转为整改任务的建议', range(len(suggestions)), format_func=lambda i: suggestions[i]['title'], key='benchmark_task_suggestion')
                    if st.button('AI 生成持久草稿任务', key='benchmark_generate_task'):
                        try:
                            suggestion = suggestions[choice]
                            from enterprise.evidence_freeze import frozen_evidence_hashes
                            evidence_hashes = frozen_evidence_hashes(analysis.get('evidence', []), suggestion.get('evidence_ids', []))
                            seed = {'task_title': suggestion['title'], 'assignee': {'name': '', 'department': '', 'role': suggestion['owner_role']},
                                    'source': {'analysis_type': '跨厂对标', 'analysis_month': month, 'product': product,
                                               'finding': f'{month} {product}（{specification}）；'+report['overview']+'；'+suggestion['source']},
                                    'priority': suggestion.get('priority', '中'), 'deadline': '', 'suggestion': suggestion['action'],
                                    'factories': ['中药一厂', '中药二厂'], 'evidence_ids': suggestion.get('evidence_ids', []),
                                    'evidence_hashes': evidence_hashes, 'analysis_run_id': 'benchmark-'+benchmark_token[:24],
                                    'analysis_period': {'months':[month], 'label':month, 'coverage':'full_period'}}
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
