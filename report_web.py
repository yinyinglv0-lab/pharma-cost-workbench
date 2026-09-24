"""Frozen reports: focused generation and review flows with authorized exports."""
import streamlit as st

from app_pages._shared import authorize, generation_notice, page_context, rerun_notice, show_notice
from app_pages.design import page_header, process_steps
from enterprise.analysis_service import report_evidence, validated_model
from enterprise.model_gateway import configuration
from enterprise.report_records import ReportRepository
from enterprise.report_service import build_report_payload
from enterprise.security import can
from report.datafill import THEMES, resolve_period

principal, app = page_context('report.read')
repo = ReportRepository(app.root)
STATUS_LABELS = {'draft': '草稿', 'submitted': '待主管审核', 'approved': '已审核签发', 'rejected': '已退回'}


def selected_default(key, options, default=None):
    if st.session_state.get(key) not in options:
        st.session_state[key] = default if default in options else options[0]


def generate_report():
    if not (can(principal, 'report.generate', factory='中药一厂') and can(principal, 'data.read', factory='中药一厂')):
        st.info('当前角色可在“审核与导出”查看已授权报告。生成报告需具备一厂数据与报告生成权限。')
        return
    try:
        tables = app.report_tables()
    except (ValueError, PermissionError) as exc:
        st.error(str(exc))
        return
    costs = tables['cost26']
    products = sorted(costs['产品名称'].dropna().unique()) if not costs.empty else []
    if not products:
        st.info('当前授权范围暂无可生成报告的一厂成本数据。请先导入月度数据并完成确认。')
        return
    prefill = st.session_state.pop('report_agent_prefill', None)
    if prefill:
        from report.datafill import validate_params
        try:
            authorize(principal, 'report.generate', factory='中药一厂', product=prefill.get('product'))
            valid, errors = validate_params(prefill, d=tables)
            if not valid or prefill.get('product') not in products:
                raise ValueError('助手带入的参数不再适用于当前数据：' + '；'.join(errors))
            widget_keys = {'product': 'report_product', 'specification': 'report_specification',
                           'month': 'report_month', 'theme': 'report_theme', 'formal': 'report_formal',
                           'use_llm': 'report_use_llm', 'include_benchmark': 'report_benchmark'}
            for field, key in widget_keys.items():
                st.session_state[key] = prefill[field]
            st.info('已带入助手校验的报告参数。请检查后点击生成并保存草稿。')
        except (ValueError, PermissionError, KeyError) as exc:
            st.error(str(exc))
    process_steps(['选择分析范围', '生成与核对草稿', '主管复核', '导出与跟进'], 0)
    st.subheader('分析参数')
    c_product, c_month, c_spec = st.columns([1.2, 1, 1.4])
    selected_default('report_product', products)
    with c_product:
        product = st.selectbox('目标产品', products, key='report_product', index=None)
    product_rows = costs.loc[costs['产品名称'].eq(product)]
    specifications = sorted(product_rows['产品规格'].dropna().unique())
    selected_default('report_specification', specifications)
    with c_spec:
        specification = st.selectbox('产品规格', specifications, key='report_specification', index=None)
    months = sorted(product_rows.loc[product_rows['产品规格'].eq(specification), '月份'].unique())
    selected_default('report_month', months, months[-1])
    with c_month:
        month = st.selectbox('分析月份 / 季度内月份', months, key='report_month', index=None)
    selected_default('report_theme', THEMES)
    theme = st.selectbox('报告主题', THEMES, key='report_theme', index=None)
    focus = st.selectbox('专题重点', ['材料', '人工', '制费'], key='report_focus') if theme == '专题分析' else None
    period_months, _, _, period_label = resolve_period(theme, month)
    st.caption(f'本次分析期间：{period_label} · ' + '、'.join(period_months))
    peer_allowed = can(principal, 'data.read', factory='中药二厂')
    for key, value in [('report_formal', True), ('report_benchmark', peer_allowed), ('report_use_llm', True)]:
        st.session_state.setdefault(key, value)
    if not peer_allowed:
        st.session_state['report_benchmark'] = False
    with st.form('report_params', border=True):
        formal = st.checkbox('校验正式报告所需完整数据', key='report_formal',
                             help='缺少完整期间数据时阻止生成；关闭后仅可生成资料核查稿。')
        include_benchmark = st.checkbox('包含同期跨厂对标', disabled=not peer_allowed, key='report_benchmark',
                                        help='只比较同产品、同规格、完整同期数据。')
        use_llm = st.checkbox('使用已配置模型辅助解释', key='report_use_llm')
        submitted = st.form_submit_button('生成并保存报告草稿', type='primary', icon=':material/description:')
    st.caption('生成后自动打开草稿。金额由程序计算，解释与建议需要业务复核；提交审核、签发与导出分别操作。')
    if not submitted:
        return
    try:
        authorize(principal, 'report.generate', factory='中药一厂', product=product)
        if include_benchmark:
            authorize(principal, 'report.generate', factory='中药二厂', product=product)
        params = {'product': product, 'month': month, 'specification': specification, 'theme': theme,
                  'formal': formal, 'include_benchmark': include_benchmark, 'use_llm': use_llm}
        if focus:
            params['focus'] = focus
        # 硬门禁：勾选"使用模型辅助解释"时，模型必须已配置成功（密钥+授权），否则拒绝生成
        if use_llm:
            from enterprise.model_settings import require_generation_model
            try:
                require_generation_model(task='report')
            except Exception as exc:
                st.error('⚠️ ' + str(exc))
                return
        with st.status('正在生成并保存冻结报告', expanded=True) as status:
            status.write('校验完整期间、计算金额与来源，再生成受约束的解释。')
            model_config = configuration(task='report').public()
            payload = build_report_payload(params, tables=tables,
                evidence_fn=lambda facts: report_evidence(principal, product, specification, period_months,
                                                         root=app.root, facts=facts, require_hybrid=use_llm),
                model_fn=validated_model if use_llm and model_config['configured'] else None,
                model_version=model_config['model'] if model_config['configured'] else 'not_configured',
                versions={'data_revision': tables['cost26'].attrs.get('cost_revision'),
                          'cost_snapshot_hash': tables['cost26'].attrs.get('cost_snapshot_hash')})
            record = repo.save(payload, actor=principal)
            status.update(label='报告草稿已保存', state='complete', expanded=False)
        st.session_state['report_next_selection'] = record['id']
        rerun_notice('report_notice', '报告已保存为待复核草稿。请查看模型状态、证据及期间完整性后提交。')
    except (ValueError, PermissionError, OSError, RuntimeError) as exc:
        st.error('报告未生成：' + str(exc))


def review_actions(record):
    ident, payload = record['id'], record['payload']
    if record['status'] in ('draft', 'rejected') and can(principal, 'report.generate'):
        if st.button('提交主管审核', type='primary', key='report_submit', icon=':material/fact_check:'):
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
        with st.container(horizontal=True):
            do_approve = st.button('批准并签发报告', type='primary', disabled=own or not payload.get('formal'), key='report_approve')
            do_reject = st.button('退回报告', disabled=own, key='report_reject')
        if do_approve or do_reject:
            try:
                action = repo.approve if do_approve else repo.reject
                action(ident, actor=principal, expected_version=record['version'], reason=reason)
                rerun_notice('report_notice', '报告已批准签发。' if do_approve else '报告已退回；原冻结内容保留。')
            except (ValueError, PermissionError, OSError) as exc:
                st.error(str(exc))
    approved = record['status'] == 'approved'
    st.caption('正式导出绑定当前审核版本及同一冻结数据。' if approved else '导出文件标注“草稿／未签发”，供复核使用。')
    with st.container(horizontal=True):
        for fmt, label, mime in [('docx', 'Word', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
                                 ('pdf', 'PDF', 'application/pdf'),
                                 ('audit_json', '审计附件', 'application/json')]:
            artifact_key = f'report_export_{ident}_{record["version"]}_{fmt}'
            if st.button(f"准备{'正式' if approved else '草稿'} {label}", key='prepare_'+artifact_key, icon=':material/download:'):
                st.session_state[artifact_key] = True
            if st.session_state.get(artifact_key):
                try:
                    content = repo.export(ident, fmt, actor=principal)
                    extension = '审计附件.json' if fmt == 'audit_json' else fmt
                    st.download_button(f"下载{'正式' if approved else '草稿'} {label}", content,
                                       file_name=f"{'正式' if approved else '草稿'}_{ident}.{extension}", mime=mime, key='download_'+artifact_key)
                except (ValueError, PermissionError, OSError, RuntimeError) as exc:
                    st.error('导出未完成：' + str(exc))


def review_report():
    records = repo.list(actor=principal)
    st.subheader('报告档案与审核')
    if not records:
        st.info('当前授权范围暂无报告。请在“生成报告”中创建第一份草稿。')
        return
    by_id = {row['id']: row for row in records}
    next_id = st.session_state.pop('report_next_selection', None)
    if next_id in by_id:
        st.session_state['report_selected'] = next_id
    with st.expander('筛选报告'):
        with st.container(horizontal=True):
            product_filter = st.selectbox('筛选产品', ['全部产品'] + sorted({r['product'] for r in records}), key='report_filter_product')
            status_filter = st.selectbox('审核状态筛选', [None] + list(STATUS_LABELS),
                                         format_func=lambda v: '全部状态' if v is None else STATUS_LABELS[v], key='report_filter_status')
        search = st.text_input('搜索报告编号或产品', key='report_filter_text', max_chars=120)
    visible = {key: row for key, row in by_id.items()
               if (product_filter == '全部产品' or row['product'] == product_filter)
               and (status_filter is None or row['status'] == status_filter)
               and (not search.strip() or search.strip().lower() in (key + ' ' + row['product']).lower())}
    selected = st.session_state.get('report_selected')
    if selected in by_id and selected not in visible:
        st.caption('当前打开的报告不符合本次筛选，已保留以便继续处理。')
        visible[selected] = by_id[selected]
    if not visible:
        st.info('没有匹配报告。请调整产品、状态或搜索词。')
        return
    if selected not in visible:
        st.session_state.pop('report_selected', None)
    # Labels contain only immutable metadata so status transitions preserve selection.
    ident = st.selectbox('选择报告', list(visible), key='report_selected',
                         format_func=lambda key: f"{by_id[key]['product']} · {by_id[key]['created'][:10]}生成 · {key}")
    try:
        record = repo.get(ident, actor=principal)
    except (ValueError, PermissionError, OSError) as exc:
        st.error(str(exc))
        return
    payload, params = record['payload'], record['payload']['params']
    process_steps(['生成草稿', '主管复核', '签发与导出'], {'draft': 0, 'rejected': 0, 'submitted': 1, 'approved': 2}.get(record['status']))
    a, b, c = st.columns(3)
    a.metric('审核状态', STATUS_LABELS.get(record['status'], record['status']))
    b.metric('状态版本', record['version'])
    c.metric('生成方式', 'AI 辅助分析' if payload['used_llm'] else '规则分析')
    st.caption(f"{params['product']} · {params['specification']} · {payload['period']['label']} · {params['theme']}")
    if not payload.get('formal'):
        st.warning('此为资料完整性核查稿，不能批准为正式报告。请补齐数据后重新生成。')
    review_actions(record)
    st.subheader('报告摘要与依据')
    if not payload['used_llm']:
        st.info(generation_notice(payload.get('generation_status')))
        with st.expander('模型生成检查与下一步'):
            st.caption('技术状态码：' + str(payload.get('generation_status')))
            st.caption('已有报告的冻结内容不会因重试而改变；重新生成将形成新的报告。')
            if can(principal, 'system.read'):
                st.page_link('app_pages/settings.py', label='打开系统设置检查模型', icon=':material/settings:')
            else:
                st.caption('如需调整模型服务，请联系系统管理员。')
    st.write(payload['overview'])
    # Show frozen additions only. Never re-query a newer reference release while
    # reviewing an already signed report, and do not alter its stored payload.
    if params.get('theme') == '专题分析' and payload.get('special_analysis'):
        with st.expander('专题背景、重点明细与核查链'):
            st.write(payload['special_analysis'])
    supplement_specs = (
        ('industry_comparison', '冻结行业参考与三方位置', ('industry_reference', 'industry_observed')),
        ('market_reference', '冻结市场参考（非采购实价）', ('market_reference',)),
        ('forecast_baseline', '冻结预测基线（非预算）', ('forecast_baseline', 'forecast_backtest', 'forecast_budget')),
    )
    for supplement_key, label, names in supplement_specs:
        supplement = payload.get(supplement_key)
        if not supplement:
            continue
        with st.expander(label):
            st.caption(supplement.get('boundary', ''))
            if not supplement.get('available'):
                st.info(supplement.get('reason') or '原报告未取得此项资料。')
            for name in names:
                table = payload.get('tables', {}).get(name, {})
                if table.get('rows'):
                    import pandas as pd
                    st.dataframe(pd.DataFrame(table['rows'], columns=table['headers']), hide_index=True)
                if table.get('note'):
                    st.caption(table['note'])
            if supplement_key == 'industry_comparison':
                seen = set()
                for period in supplement.get('periods', []):
                    for alert in period.get('alerts', []):
                        if alert['alert_id'] not in seen:
                            seen.add(alert['alert_id'])
                            st.warning(alert['finding'])
                            st.caption(alert['boundary'])
            if supplement_key == 'forecast_baseline':
                st.caption(supplement.get('interval_reason', '未提供校准预测区间。'))
    for section in payload.get('sections', []):
        with st.expander(section['title']):
            st.write(section['text'])
            missing = section.get('missing_evidence', [])
            if missing:
                st.caption('待补充证据：' + '、'.join(missing))
    peer = payload.get('benchmark', {}).get('analysis')
    if peer:
        with st.expander('同期间跨厂归因'):
            st.caption('对标解释在本报告冻结前生成；会计差异不等于已实现节约。')
            st.caption('对标生成方式：' + ('AI辅助解释，待专业复核' if peer.get('used_llm') else '规则分析／' + generation_notice(peer.get('generation_status'))))
            for section in peer.get('sections', []):
                st.markdown('**' + section['element'] + '对标解释**')
                st.write(section['text'])
            if not peer.get('available'):
                st.info(peer.get('text', '缺少完整可比期间资料'))
    with st.expander('正文知识引用与核查状态'):
        usage = payload.get('knowledge_usage')
        if usage:
            st.write(f"单厂正文机制段 {usage['claims_total']} 项，其中 {usage['claims_with_knowledge']} 项引用知识依据。")
            st.caption('这是可追溯采用统计，不是语义支持率或人工得分。逐句依据仍需业务复核。')
            st.json(usage)
        else:
            st.caption('该历史报告未记录逐句知识采用台账；原冻结内容保持不变。')
    with st.expander('期间口径、来源与版本'):
        st.json({'period': payload['period'], 'validation': payload['validation'],
                 'versions': payload['versions'], 'sources': payload['sources']})
    for warning in payload.get('warnings', []):
        st.warning(warning)
    st.subheader('改进建议')
    st.dataframe([{'建议': s.get('title'), '核查行动': s.get('action'), '责任部门': s.get('department'),
                   '优先级': s.get('priority'), '计划日期': s.get('due_date')} for s in payload['suggestions']],
                 hide_index=True, column_config={'核查行动': st.column_config.TextColumn(width='large')})
    with st.expander('报告审核记录'):
        st.dataframe(repo.events(ident, actor=principal), hide_index=True)
    if can(principal, 'task.create') and payload['suggestions']:
        with st.expander('从报告建议生成整改任务'):
            suggestion_index = st.selectbox('选择改进建议', range(len(payload['suggestions'])),
                                            format_func=lambda i: payload['suggestions'][i]['title'], key='report_task_suggestion')
            if st.button('AI 生成持久草稿任务', key='report_generate_task'):
                try:
                    fresh = repo.get(ident, actor=principal)
                    suggestion = fresh['payload']['suggestions'][suggestion_index]
                    from enterprise.evidence_freeze import frozen_evidence_hashes
                    evidence_hashes = frozen_evidence_hashes(fresh['payload'].get('sources', []), suggestion.get('evidence_ids', []))
                    analysis = {'task_title': suggestion['title'], 'assignee': {'name': '', 'department': suggestion['department'], 'role': suggestion['owner_role']},
                                'priority': suggestion['priority'], 'deadline': suggestion['due_date'], 'suggestion': suggestion['action'],
                                'source': {'analysis_type': params['theme'], 'analysis_month': params['month'], 'product': params['product'],
                                           'finding': f"{fresh['payload']['period']['label']} {params['product']}（{params['specification']}）；"+suggestion['source']+'；'+suggestion['action']}, 'factories': fresh['factories'],
                                'evidence_ids': suggestion.get('evidence_ids', []), 'evidence_hashes': evidence_hashes,
                                'analysis_run_id': fresh['payload']['analysis_run_id'],
                                'analysis_period': {'months': fresh['payload']['period']['months'],
                                                    'label': fresh['payload']['period']['label'], 'coverage': 'full_period'}}
                    with st.spinner('生成并保存任务草稿…'):
                        task = app.tasks().generate(analysis, actor=principal)
                    st.session_state['task_next_selection'] = task['task_id']
                    st.success('任务草稿已保存：'+task['generation']['label']+'。请到整改任务填写责任人并提交审核。')
                except (ValueError, PermissionError, OSError, RuntimeError) as exc:
                    st.error(str(exc))
            st.page_link('app_pages/tasks.py', label='打开整改任务', icon=':material/task_alt:')


page_header('成本智能报告', '报告中心 / 月度、季度与专题分析')
show_notice('report_notice')
def remember_report_view():
    st.session_state['_report_view'] = st.session_state['report_view']


if st.session_state.get('report_agent_prefill'):
    st.session_state['_report_view'] = '生成报告'
elif st.session_state.get('report_next_selection'):
    st.session_state['_report_view'] = '审核与导出'
elif '_report_view' not in st.session_state:
    st.session_state['_report_view'] = '生成报告' if can(principal, 'report.generate', factory='中药一厂') else '审核与导出'
# Keep navigation independent of widget cleanup across page changes and reruns.
st.session_state['report_view'] = st.session_state['_report_view']
creation, review = st.tabs(['生成报告', '审核与导出'], key='report_view', on_change=remember_report_view)
if creation.open:
    with creation:
        generate_report()
if review.open:
    with review:
        review_report()
