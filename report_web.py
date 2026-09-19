"""Frozen period reports with persistent review and authorized exports."""
import streamlit as st

from app_pages._shared import authorize, page_context, rerun_notice, show_notice
from enterprise.analysis_service import report_evidence, validated_model
from enterprise.model_gateway import configuration
from enterprise.report_records import ReportRepository
from enterprise.report_service import build_report_payload
from enterprise.security import can
from report.datafill import THEMES, resolve_period

principal, app = page_context('report.read')
repo = ReportRepository(app.root)
st.title('成本智能报告')
st.caption('月度 / 季度 / 专题 · 冻结数据与证据 → 持久草稿 → 主管复核 → Word / PDF')
show_notice('report_notice')

if can(principal, 'report.generate', factory='中药一厂') and can(principal, 'data.read', factory='中药一厂'):
    try:
        tables = app.report_tables()
    except (ValueError, PermissionError) as exc:
        st.error(str(exc))
        st.stop()
    costs = tables['cost26']
    products = sorted(costs['产品名称'].dropna().unique()) if not costs.empty else []
    if products:
        st.subheader('分析参数')
        c_product, c_month, c_spec = st.columns([1.2, 1, 1.4])
        with c_product:
            product = st.selectbox('目标产品', products, key='report_product')
        product_rows = costs.loc[costs['产品名称'].eq(product)]
        specifications = sorted(product_rows['产品规格'].dropna().unique())
        with c_spec:
            specification = st.selectbox('产品规格', specifications, key='report_specification')
        months = sorted(product_rows.loc[product_rows['产品规格'].eq(specification), '月份'].unique())
        with c_month:
            month = st.selectbox('分析月份 / 季度内月份', months, index=len(months)-1, key='report_month')
        theme = st.selectbox('报告主题', THEMES, key='report_theme')
        focus = st.selectbox('专题重点', ['材料', '人工', '制费'], key='report_focus') if theme == '专题分析' else None
        with st.form('report_params', border=True):
            formal = st.checkbox('按正式报告完整性要求生成（缺期资料会阻止生成）', value=True, key='report_formal')
            include_benchmark = st.checkbox('包含同产品同规格同期跨厂对标', value=can(principal, 'data.read', factory='中药二厂'),
                                            disabled=not can(principal, 'data.read', factory='中药二厂'), key='report_benchmark')
            use_llm = st.checkbox('使用已配置模型辅助解释', value=True, key='report_use_llm')
            submitted = st.form_submit_button('生成并保存报告草稿', type='primary', icon=':material/description:')
        if submitted:
            try:
                authorize(principal, 'report.generate', factory='中药一厂', product=product)
                if include_benchmark:
                    authorize(principal, 'report.generate', factory='中药二厂', product=product)
                params = {'product': product, 'month': month, 'specification': specification, 'theme': theme,
                          'formal': formal, 'include_benchmark': include_benchmark, 'use_llm': use_llm}
                if focus:
                    params['focus'] = focus
                period_months = resolve_period(theme, month)[0]
                with st.status('正在生成并保存冻结报告', expanded=True) as status:
                    status.write('校验完整期间、计算金额与来源，再生成受约束的解释。')
                    evidence = report_evidence(principal, product, specification, period_months, root=app.root)
                    model_config = configuration().public()
                    payload = build_report_payload(params, tables=tables, evidence=evidence,
                        model_fn=validated_model if use_llm and model_config['configured'] else None,
                        model_version=model_config['model'] if model_config['configured'] else 'not_configured',
                        versions={'data_revision': tables['cost26'].attrs.get('cost_revision'),
                                  'cost_snapshot_hash': tables['cost26'].attrs.get('cost_snapshot_hash'),
                                  'index_releases': sorted({e['index_release_id'] for e in evidence})})
                    record = repo.save(payload, actor=principal)
                    status.update(label='报告草稿已保存', state='complete', expanded=False)
                st.session_state['report_next_selection'] = record['id']
                rerun_notice('report_notice', '报告已保存为待复核草稿。请查看模型状态、证据及期间完整性后提交。')
            except (ValueError, PermissionError, OSError, RuntimeError) as exc:
                st.error('报告未生成：' + str(exc))
    else:
        st.info('当前授权范围暂无可生成报告的一厂成本数据。')

records = repo.list(actor=principal)
st.subheader('报告档案与审核')
if not records:
    st.info('当前授权范围暂无报告。')
    st.stop()
by_id = {r['id']: r for r in records}
next_id = st.session_state.pop('report_next_selection', None)
if next_id in by_id:
    st.session_state['report_selected'] = next_id
if st.session_state.get('report_selected') not in by_id:
    st.session_state.pop('report_selected', None)
ident = st.selectbox('选择报告', list(by_id), format_func=lambda key: f"{key} · {by_id[key]['product']} · {by_id[key]['status']}", key='report_selected')
try:
    record = repo.get(ident, actor=principal)
except (ValueError, PermissionError, OSError) as exc:
    st.error(str(exc))
    st.stop()
payload, params = record['payload'], record['payload']['params']
status_labels = {'draft': '草稿', 'submitted': '待主管审核', 'approved': '已审核签发', 'rejected': '已退回'}
a, b, c = st.columns(3)
a.metric('审核状态', status_labels.get(record['status'], record['status']))
b.metric('状态版本', record['version'])
c.metric('生成方式', 'AI 辅助分析' if payload['used_llm'] else '规则分析')
st.caption(f"{params['product']} · {params['specification']} · {payload['period']['label']} · {params['theme']}")
if not payload['used_llm']:
    st.info(f"{payload['generation_status']}：{payload.get('fallback_reason') or '使用确定性分析；模型尚未生成可用结果。'}")
if not payload.get('formal'):
    st.warning('此为资料完整性核查稿，不能批准为正式报告。请补齐数据后重新生成。')
st.write(payload['overview'])
for section in payload.get('sections', []):
    with st.expander(section['title'], expanded=True):
        st.write(section['text'])
        st.caption('缺少证据：' + '、'.join(section.get('missing_evidence', [])))
with st.expander('期间口径、来源与版本'):
    st.json({'period': payload['period'], 'validation': payload['validation'],
             'versions': payload['versions'], 'sources': payload['sources']})
for warning in payload.get('warnings', []):
    st.warning(warning)
st.subheader('改进建议')
st.dataframe([{k: suggestion.get(k) for k in ('title', 'action', 'department', 'priority', 'due_date')}
              for suggestion in payload['suggestions']], hide_index=True)

if record['status'] in ('draft', 'rejected') and can(principal, 'report.generate'):
    if st.button('提交主管审核', type='primary', key='report_submit'):
        try:
            repo.submit(ident, actor=principal, expected_version=record['version'])
            rerun_notice('report_notice', '报告已提交主管审核。')
        except (ValueError, PermissionError, OSError) as exc:
            st.error(str(exc))
if record['status'] == 'submitted' and can(principal, 'report.approve'):
    own = record['creator'] == principal.user_id and principal.auth_method != 'local_os_demo'
    if own:
        st.info('请由另一位主管审核，报告创建人不可自批。')
    elif principal.auth_method == 'local_os_demo':
        st.caption('本机演示：当前 OS 用户模拟独立主管审批。')
    reason = st.text_input('审核意见', key=f'report_review_{ident}_{record["version"]}')
    approve, reject = st.columns(2)
    with approve:
        do_approve = st.button('批准并签发报告', type='primary', disabled=own or not payload.get('formal'), key='report_approve')
    with reject:
        do_reject = st.button('退回报告', disabled=own, key='report_reject')
    if do_approve or do_reject:
        try:
            action = repo.approve if do_approve else repo.reject
            action(ident, actor=principal, expected_version=record['version'], reason=reason)
            rerun_notice('report_notice', '报告已批准签发。' if do_approve else '报告已退回；原冻结内容保留。')
        except (ValueError, PermissionError, OSError) as exc:
            st.error(str(exc))

st.subheader('报告导出')
approved = record['status'] == 'approved'
st.caption('正式导出绑定当前审核版本及同一冻结数据。' if approved else '下载文件明确标注“草稿／未签发”，用于审核。')
with st.container(horizontal=True):
    for format, label, mime in [('docx', 'Word', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
                                ('pdf', 'PDF', 'application/pdf')]:
        artifact_key = f'report_export_{ident}_{record["version"]}_{format}'
        if st.button(f"准备{'正式' if approved else '草稿'} {label}", key='prepare_'+artifact_key):
            st.session_state[artifact_key] = True
        if st.session_state.get(artifact_key):
            try:
                content = repo.export(ident, format, actor=principal)
                st.download_button(f"下载{'正式' if approved else '草稿'} {label}", content,
                                   file_name=f"{'正式' if approved else '草稿'}_{ident}.{format}", mime=mime, key='download_'+artifact_key)
            except (ValueError, PermissionError, OSError, RuntimeError) as exc:
                st.error('导出未完成：'+str(exc))
with st.expander('报告审核记录'):
    st.dataframe(repo.events(ident, actor=principal), hide_index=True)

if can(principal, 'task.create') and payload['suggestions']:
    st.subheader('从报告建议生成整改任务')
    suggestion_index = st.selectbox('选择改进建议', range(len(payload['suggestions'])),
                                    format_func=lambda i: payload['suggestions'][i]['title'], key='report_task_suggestion')
    if st.button('AI 生成持久草稿任务', key='report_generate_task'):
        try:
            fresh = repo.get(ident, actor=principal)
            suggestion = fresh['payload']['suggestions'][suggestion_index]
            analysis = {'task_title': suggestion['title'], 'assignee': {'name': '', 'department': suggestion['department'], 'role': suggestion['owner_role']},
                        'priority': suggestion['priority'], 'deadline': suggestion['due_date'], 'suggestion': suggestion['action'],
                        'source': {'analysis_type': params['theme'], 'analysis_month': params['month'], 'product': params['product'],
                                   'finding': suggestion['source']+'；'+suggestion['action']}, 'factories': fresh['factories'],
                        'evidence_ids': suggestion.get('evidence_ids', []), 'analysis_run_id': fresh['payload']['analysis_run_id']}
            with st.spinner('生成并保存任务草稿…'):
                task = app.tasks().generate(analysis, actor=principal)
            st.session_state['task_next_selection'] = task['task_id']
            st.success('任务草稿已保存：'+task['generation']['label']+'。请到整改任务填写责任人并提交审核。')
        except (ValueError, PermissionError, OSError, RuntimeError) as exc:
            st.error(str(exc))
    st.page_link('app_pages/tasks.py', label='打开整改任务', icon=':material/task_alt:')
