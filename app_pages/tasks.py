"""Persistent task review, issuance, official mock dispatch and receipt tracking."""
from datetime import date, timedelta
import json
import streamlit as st

from app_pages._shared import authorize, page_context, rerun_notice, show_notice
from app_pages.design import page_header, process_steps
from enterprise.rpa_client import RPAClient, RPAError
from enterprise.security import can

principal, app = page_context('task.read')
repo = app.tasks()
page_header('整改任务与跟踪', '分析与行动 / 责任分配、审核与进度跟踪')
st.caption('当前发送通道为官方本机 mock。发送与回执均为模拟，不代表真实微信送达或实际整改完成。')
show_notice('task_notice')

# Industry alerts are discovered on demand, not dispatched on page load. The
# service rechecks current release/ACL on every displayed view and again on save.
if (can(principal, 'data.read', factory='中药一厂')
        and can(principal, 'dashboard.read', factory='中药一厂')
        and can(principal, 'knowledge.read', factory='中药一厂')):
    with st.expander('行业偏离候选 · 先核实口径，再决定任务'):
        from enterprise.benchmark import benchmark_options
        from enterprise.reference_service import industry_view
        from app_pages.reference_view import render_industry_view
        st.caption('这里只检测源文件列示的参考偏离，不判定根因；保存也仅创建验证草稿，不自动审批或派发。')
        try:
            reference_tables = app.tables()
            reference_scopes = benchmark_options(reference_tables)
            if reference_scopes:
                reference_month = st.selectbox('行业参考查看月', sorted({r['month'] for r in reference_scopes}, reverse=True), key='task_reference_month')
                reference_products = sorted({r['product'] for r in reference_scopes if r['month'] == reference_month})
                reference_product = st.selectbox('行业参考产品', reference_products, key='task_reference_product')
                reference_specs = sorted({r['specification'] for r in reference_scopes
                                          if r['month'] == reference_month and r['product'] == reference_product})
                reference_spec = st.selectbox('行业参考规格', reference_specs, key='task_reference_spec')
                reference_scope = (reference_product, reference_spec, reference_month)
                if st.button('核查行业参考偏离', key='task_scan_industry'):
                    st.session_state['task_reference_requested'] = reference_scope
                if st.session_state.get('task_reference_requested') == reference_scope:
                    reference_result = industry_view(app, reference_product, reference_spec, reference_month,
                                                     tables=reference_tables)
                    render_industry_view(reference_result, app, product=reference_product,
                                         specification=reference_spec, month=reference_month,
                                         key='task_industry', show_peer=False, radar=False)
            else:
                st.info('当前授权范围没有可用于核查的产品月份记录。')
        except (ValueError, PermissionError, OSError) as exc:
            st.error(str(exc))
labels = {'draft': '草稿', 'submitted': '待审核', 'approved': '已批准', 'rejected': '已退回', 'issued': '已签发',
          'not_sent': '未发送', 'pending': '等待发送', 'sending': '发送中', 'retry': '等待重试', 'unknown': '结果待确认',
          'failed': '发送失败', 'accepted': 'mock 已受理', 'sent': '模拟已发送', 'received': '模拟已接收',
          'confirmed': '模拟已确认', 'in_progress': '模拟进行中', 'completed': '模拟执行完成', 'overdue': '远端提示逾期',
          'open': '待提交整改', 'pending_acceptance': '待独立验收', 'rework': '返工中', 'closed': '已验收关闭',
          'business_overdue': '超过业务截止日'}
summary = repo.summary(actor=principal)
a, b, c, d = st.columns(4)
a.metric('已生成任务', summary['generated'])
b.metric('模拟执行完成', summary['execution_completed'])
c.metric('待独立验收', summary['pending_acceptance'])
d.metric('已验收关闭', summary['closed'])
if summary['needs_attention']:
    st.warning(f"{summary['needs_attention']}项任务需要关注，其中{summary['business_overdue']}项超过业务截止日；请查看发送异常、整改材料或验收进度。")
with st.expander('授权范围内的催办通知'):
    st.caption('已读只记录本人已查看本地通知。只有官方通知接口返回有效回执，才显示模拟微信已送达。')
    notifications = repo.notifications(actor=principal, unread_only=True)
    if not notifications:
        st.write('暂无未读催办。')
    for note in notifications:
        st.write(note['payload']['message'])
        delivery_label = {'delivered': '模拟微信已送达', 'not_sent': '外部未通知', 'unknown': '外部是否送达待核对'}[note['external_status']]
        st.caption(f"{note['created_utc']} · {delivery_label}")
        if st.button('确认已读', key='task_note_read_'+note['id']):
            try:
                repo.acknowledge_notification(note['id'], actor=principal)
                st.session_state['task_next_selection'] = note['task_id']
                rerun_notice('task_notice', '已记录本人查看，任务已打开。')
            except (ValueError, PermissionError, OSError) as exc:
                st.error(str(exc))
with st.container(horizontal=True):
    query = st.text_input('搜索任务', placeholder='标题、产品、责任人或任务编号', key='task_search', max_chars=200)
    filter_status = st.selectbox('筛选状态', [None] + list(labels),
                                 format_func=lambda value: '全部状态' if value is None else labels[value],
                                 key='task_filter', width=180)
    page_size = st.selectbox('每页条数', [10, 20, 50], key='task_page_size', width=110)
filter_token = (query.strip(), filter_status, page_size)
if st.session_state.get('task_filter_token') != filter_token:
    st.session_state['task_page_offset'] = 0
    st.session_state['task_filter_token'] = filter_token
page_offset = st.session_state.get('task_page_offset', 0)
page = repo.page(actor=principal, query=query, status=filter_status, limit=page_size, offset=page_offset)
if page_offset and not page['items']:
    page_offset = max(0, ((page['total'] - 1) // page_size) * page_size)
    st.session_state['task_page_offset'] = page_offset
    page = repo.page(actor=principal, query=query, status=filter_status, limit=page_size, offset=page_offset)
rows = page['items']
if rows:
    st.dataframe([{'任务': r['content']['task_title'], '产品': r['content']['source']['product'],
                   '责任人': r['content']['assignee']['name'] or '待指定',
                   '部门': r['content']['assignee']['department'] or '待指定', '截止日': r['content']['deadline'] or '待指定',
                   '整改': labels[r['business_status']], '期限': '已逾期' if r['is_overdue'] else '未逾期',
                   '审核': labels.get(r['workflow_status'], r['workflow_status']),
                   '发送': labels.get(r['dispatch_status'], r['dispatch_status']),
                   '回执': labels.get(r['receipt_status'], r['receipt_status']) or '尚无回执'}
                  for r in rows], hide_index=True, height=min(320, 35 * (len(rows)+1) + 3),
                 column_config={'任务': st.column_config.TextColumn(width=260, pinned=True)})
else:
    st.info('没有符合条件的任务。可调整搜索条件，或新建一份任务草稿。')
with st.container(horizontal=True):
    if st.button('上一页', key='task_prev_page', disabled=not page_offset, icon=':material/chevron_left:'):
        st.session_state['task_page_offset'] = max(0, page_offset - page_size)
        st.rerun()
    if st.button('下一页', key='task_next_page', disabled=page_offset+len(rows) >= page['total'], icon=':material/chevron_right:'):
        st.session_state['task_page_offset'] = page_offset + page_size
        st.rerun()
    st.caption(f"匹配 {page['total']} 项 · 第 {page_offset // page_size + 1} / {max(1, (page['total'] + page_size - 1) // page_size)} 页 · 上方指标覆盖全部授权任务")
by_id = {r['task_id']: r for r in rows}
# A freshly generated task can be outside the active filter; authorize it again
# and make it reachable without silently dropping the user's selected record.
pending_id = st.session_state.get('task_next_selection') or st.session_state.get('task_selected')
if pending_id and pending_id != 'new' and pending_id not in by_id:
    try:
        by_id[pending_id] = repo.get(pending_id, actor=principal)
        st.caption('当前打开的任务位于其他列表页或筛选范围之外，仍可继续处理。')
    except (ValueError, PermissionError, OSError) as exc:
        st.warning(str(exc))
options = (['new'] if can(principal, 'task.create') else []) + list(by_id)
if not options:
    st.info('当前授权范围暂无任务。')
    st.stop()
next_id = st.session_state.pop('task_next_selection', None)
if next_id in options:
    st.session_state['task_selected'] = next_id
if st.session_state.get('task_selected') not in options:
    st.session_state.pop('task_selected', None)
# Streamlit serializes the formatted label. Keep it unchanged across lifecycle
# transitions so a pending browser event still resolves to the same task ID.
selected_id = st.selectbox('任务对象', options, format_func=lambda key: '新建草稿' if key == 'new' else
                           f"{by_id[key]['content']['task_title']} · {key} · v{by_id[key]['version']}", key='task_selected')
try:
    current = repo.get(selected_id, actor=principal) if selected_id != 'new' else None
except (ValueError, PermissionError, OSError) as exc:
    st.error(str(exc))
    st.stop()
seed = current['content'] if current else {}
process_steps(['编辑与责任分配', '主管审核', '签发', '模拟执行', '整改与验收', '关闭'],
              (5 if current['business_status'] == 'closed' else 4 if current['business_status'] in ('pending_acceptance', 'rework') or current['receipt_status'] == 'completed'
               else {'draft': 0, 'rejected': 0, 'submitted': 1, 'approved': 2, 'issued': 3}.get(current['workflow_status'], 0)) if current else 0)

if current:
    a, b, c = st.columns(3)
    a.metric('工作流', labels.get(current['workflow_status'], current['workflow_status']))
    b.metric('发送状态', labels.get(current['dispatch_status'], current['dispatch_status']))
    c.metric('回执状态', labels.get(current['receipt_status'], current['receipt_status']) or '尚无回执')
    # 官方 mock 创建任务回执中的微信送达信息（模拟，不代表真实微信送达）
    try:
        receipt = current.get('receipt')
        notify = receipt.get('notify_status') if isinstance(receipt, dict) else None
        wechat_line = notify.get('wechat') if isinstance(notify, dict) else None
        sent_at = notify.get('sent_at') if isinstance(notify, dict) else None
        if isinstance(wechat_line, str) and wechat_line.strip():
            st.caption(f'模拟微信：{wechat_line}' + (f' · 发送时间 {sent_at}' if sent_at else '')
                        + '（官方 mock 回执，不代表真实送达）')
    except (ValueError, TypeError, KeyError):
        pass
    st.write('业务进度：' + labels[current['business_status']])
    if current['is_overdue']:
        st.warning(f"该任务已超过{seed['deadline']}截止日，仍待整改验收。截止日包含北京时间当日。")
    if current['receipt_status'] == 'completed' and current['business_status'] != 'closed':
        st.info('模拟执行已结束，请提交整改证据与效果比较，并由独立人员验收。')
    st.caption(current['generation']['label'])
    st.write(seed['source']['finding'])
    st.write(seed['suggestion'])
    if seed.get('action_plan'):
        plan = seed['action_plan']
        st.write('核查对象：' + '、'.join(plan['objects']))
        st.write('执行动作：' + '、'.join(plan['actions']))
        st.write('待核对凭证：' + '、'.join(plan['documents']))
        st.write('交付材料：' + '、'.join(plan['deliverables']))
        st.write('完成要求：' + '；'.join(plan['completion_criteria']))
    else:
        st.caption('此历史任务尚无结构化核查计划，原始版本保持不变。')
    if current['generation'].get('deadline_basis'):
        st.caption('期限依据：' + current['generation']['deadline_basis'])
    st.caption(f"任务 ID：{selected_id} · 产品：{seed['source']['product']} · 工厂：{'、'.join(seed['factories'])}")
    if seed.get('analysis_period'):
        st.caption('完整分析期间：' + seed['analysis_period']['label'] + ' · ' + '、'.join(seed['analysis_period']['months']))

editable = can(principal, 'task.create') and (current is None or current['workflow_status'] != 'issued')
if editable:
    if current is None:
        tables = app.tables()
        summary = [r for key in ('cost26', 'erchang26') for r in tables[key].to_dict('records')]
        products = sorted({r['产品名称'] for r in summary})
        if not products:
            st.info('当前范围暂无产品数据，请先导入成本数据或从已授权报告创建任务。')
            st.stop()
        product = st.selectbox('任务产品', products, key='task_new_product')
        factory_options = sorted({r['工厂'] for r in summary if r['产品名称'] == product})
        factories = st.multiselect('任务涉及工厂', factory_options, default=factory_options[:1], key='task_new_factories')
    else:
        product, factories = seed['source']['product'], seed['factories']
    suffix = f"{selected_id}_{current['version'] if current else 0}"
    source = seed.get('source', {})
    assignee = seed.get('assignee', {})
    with st.form('task_edit_'+suffix, border=True):
        st.subheader('人工编辑与责任分配')
        title = st.text_input('任务标题', value=seed.get('task_title', ''), max_chars=160, key='task_title_'+suffix)
        name_col, department_col, role_col = st.columns(3)
        with name_col:
            name = st.text_input('责任人姓名（提交必填）', value=assignee.get('name', ''), key='task_name_'+suffix)
        with department_col:
            department = st.text_input('责任部门（提交必填）', value=assignee.get('department', ''), key='task_department_'+suffix)
        with role_col:
            role = st.text_input('责任岗位（可选）', value=assignee.get('role', ''), key='task_role_'+suffix)
        priorities = ['high', 'medium', 'low']
        priority = st.selectbox('优先级', priorities, index=priorities.index(seed.get('priority', 'medium')),
                                format_func=lambda value: {'high': '高', 'medium': '中', 'low': '低'}[value], key='task_priority_'+suffix)
        deadline = st.date_input('计划截止日期', value=date.fromisoformat(seed['deadline']) if seed.get('deadline') else date.today()+timedelta(days=7), key='task_deadline_'+suffix)
        types = ['月度成本分析', '季度成本分析', '专题分析']
        analysis_type = st.selectbox('分析类型', types, index=types.index(source.get('analysis_type', types[0])), key='task_type_'+suffix)
        analysis_month = st.text_input('分析月份／季度内锚点月份（YYYY-MM）', value=source.get('analysis_month', date.today().strftime('%Y-%m')), key='task_month_'+suffix)
        st.caption('季度任务按锚点月份所在完整自然季度保存，核查范围包含全部三个月。')
        finding = st.text_area('分析结论与来源', value=source.get('finding', ''), key='task_finding_'+suffix)
        suggestion = st.text_area('核查内容与交付要求', value=seed.get('suggestion', ''), height=150, key='task_suggestion_'+suffix)
        from enterprise.task_plan import ACTIONS, DOCUMENTS, DELIVERABLES, CRITERIA, default_plan
        editable_plan = seed.get('action_plan') or default_plan({'source': {'product': product}})
        st.caption('核查对象逐字取自产品或源分析，每行一个；资料类别代表待核对要求，不表示已经取得凭证。')
        plan_objects = st.text_area('结构化核查对象', value='\n'.join(editable_plan['objects']), key='task_plan_objects_'+suffix)
        plan_actions = st.multiselect('执行动作', ACTIONS, default=editable_plan['actions'], key='task_plan_actions_'+suffix)
        plan_documents = st.multiselect('待核对凭证类别', DOCUMENTS, default=editable_plan['documents'], key='task_plan_documents_'+suffix)
        plan_deliverables = st.multiselect('交付材料', DELIVERABLES, default=editable_plan['deliverables'], key='task_plan_deliverables_'+suffix)
        st.write('完成要求：' + '；'.join(CRITERIA))
        save = st.form_submit_button('保存持久草稿', type='primary')
        generate = st.form_submit_button('AI 生成并保存任务建议') if current is None else False
    if save or generate:
        content = {'task_title': title, 'assignee': {'name': name, 'department': department, 'role': role},
                   'priority': priority, 'deadline': deadline.isoformat(), 'suggestion': suggestion,
                   'source': {'analysis_type': analysis_type, 'analysis_month': analysis_month, 'product': product, 'finding': finding},
                   'factories': factories, 'evidence_ids': seed.get('evidence_ids', []), 'analysis_run_id': seed.get('analysis_run_id', ''),
                   'evidence_hashes': seed.get('evidence_hashes', {})}
        content['action_plan'] = {'objects': [value.strip() for value in plan_objects.splitlines() if value.strip()],
                                  'actions': plan_actions, 'documents': plan_documents, 'deliverables': plan_deliverables,
                                  'completion_criteria': list(CRITERIA)}
        try:
            authorize(principal, 'task.create')
            from report.datafill import resolve_period
            period_months, _, _, period_label = resolve_period(analysis_type, analysis_month)
            content['analysis_period'] = {'months': period_months, 'label': period_label, 'coverage': 'full_period'}
            if generate:
                with st.spinner('调用模型并校验任务建议…'):
                    result = repo.generate(content, actor=principal)
            elif current:
                result = repo.update(selected_id, content, actor=principal, expected_version=current['version'])
            else:
                result = repo.create(content, actor=principal)
            st.session_state['task_next_selection'] = result['task_id']
            rerun_notice('task_notice', '任务已保存：'+result['generation']['label']+'。修改内容会使既有批准失效。')
        except (ValueError, PermissionError, OSError, RuntimeError) as exc:
            st.error(str(exc))

if current:
    state, version = current['workflow_status'], current['version']
    if state in ('draft', 'rejected') and can(principal, 'task.create'):
        if st.button('提交任务审核', type='primary', key='task_submit'):
            try:
                repo.submit(selected_id, actor=principal, expected_version=version)
                rerun_notice('task_notice', '任务已提交主管审核。')
            except (ValueError, PermissionError, OSError) as exc:
                st.error(str(exc))
    if state in ('submitted', 'approved') and can(principal, 'task.approve'):
        own = current['created_by']['user_id'] == principal.user_id and principal.auth_method != 'local_os_demo'
        if own:
            st.info('请由另一位主管批准，企业模式禁止创建人自批。')
        elif principal.auth_method == 'local_os_demo':
            st.caption('本机演示：当前 OS 用户模拟另一位主管审批。')
        review = st.text_input('主管审核意见', key=f'task_review_{selected_id}_{version}')
        a, b = st.columns(2)
        with a:
            approve = st.button('批准任务', disabled=own or state != 'submitted', type='primary', key='task_approve')
        with b:
            reject = st.button('退回任务', disabled=own, key='task_reject')
        if approve or reject:
            try:
                if approve:
                    repo.approve(selected_id, actor=principal, expected_version=version, comment=review)
                else:
                    repo.reject(selected_id, actor=principal, expected_version=version, reason=review)
                rerun_notice('task_notice', '任务已批准；待签发后发送。' if approve else '任务已退回。')
            except (ValueError, PermissionError, OSError) as exc:
                st.error(str(exc))
    if state == 'approved' and can(principal, 'task.send'):
        if st.button('签发批准版本', type='primary', key='task_issue'):
            try:
                repo.enqueue(selected_id, actor=principal, expected_version=version)
                rerun_notice('task_notice', '当前批准版本已签发并进入发送队列。')
            except (ValueError, PermissionError, OSError) as exc:
                st.error(str(exc))
    if state == 'issued' and can(principal, 'task.send'):
        a, b = st.columns(2)
        with a:
            dispatch = st.button('发送到官方 mock / 执行到期重试', type='primary', disabled=current['dispatch_status'] == 'accepted', key='task_dispatch')
        with b:
            sync = st.button('查询官方 mock 回执', key='task_sync')
        if dispatch or sync:
            try:
                authorize(principal, 'task.send')
                with RPAClient() as client:
                    if dispatch:
                        results = repo.dispatch(client, actor=principal, task_id=selected_id, limit=1)
                        result = results[0] if results else repo.get(selected_id, actor=principal)
                    else:
                        result = repo.sync(selected_id, client, actor=principal)
                status = labels.get(result['dispatch_status'], result['dispatch_status'])
                level = 'success' if result['dispatch_status'] == 'accepted' else 'warning' if result['dispatch_status'] in ('unknown', 'failed', 'retry') else 'info'
                rerun_notice('task_notice', f'官方 mock 当前状态：{status}；详情见发送记录。', level=level)
            except (ValueError, PermissionError, OSError, RPAError) as exc:
                st.error('mock 操作未完成：'+str(exc))
    if state == 'issued':
        with st.container(border=True):
            st.subheader('整改材料与效果验收')
            history = repo.rectifications(selected_id, actor=principal)
            latest = history['submissions'][-1] if history['submissions'] else None
            if latest:
                material = latest['payload']
                st.write(material['summary'] or '整改结果待补充。')
                st.write(material['evaluation'] or '效果评估待补充。')
                if material['metrics']:
                    st.dataframe([{'指标': item['name'] or '待补充', '整改前': item['before'] if item['before'] is not None else '待补充',
                                   '整改后': item['after'] if item['after'] is not None else '待补充', '单位': item['unit'] or '待补充',
                                   '前期': item['before_period'] or '待补充', '后期': item['after_period'] or '待补充',
                                   '比较范围': item['scope'] or '待补充', '计算口径': item['method'] or '待补充'} for item in material['metrics']], hide_index=True)
                for evidence in material['evidence']:
                    st.write(f"凭证：{evidence['name'] or '待补充'}；归档定位：{evidence['reference'] or '待补充'}")
                st.caption(f"材料提交人：{latest['actor']['display_name']} · 提交时间：{latest['created_utc']}")
                if latest['missing_material']:
                    st.warning('待补充：' + '、'.join(latest['missing_material']))
            else:
                st.write('尚未提交整改材料，效果值与验收人均待补充。')
            for review_item in history['reviews']:
                conclusion = '验收通过' if review_item['decision'] == 'accept' else '退回返工'
                st.write(f"{review_item['actor']['display_name']}于{review_item['created_utc']}{conclusion}：{review_item['comment']}")
            revision = current['closure_revision']
            closure_key = f'{selected_id}_{revision}'
            if current['business_status'] in ('open', 'rework') and can(principal, 'task.rectify'):
                material = latest['payload'] if latest else {}
                st.caption('请填写实际结果。未取得的数据留空，页面将显示待补充；费用增加或未节约也可据实提交。')
                evidence_count = st.number_input('本次凭证份数', min_value=1, max_value=30, value=max(1, len(material.get('evidence', []))), key='task_evidence_count_'+closure_key)
                metric_count = st.number_input('本次效果指标数', min_value=1, max_value=30, value=max(1, len(material.get('metrics', []))), key='task_metric_count_'+closure_key)
                with st.form('task_rectification_'+closure_key):
                    result_text = st.text_area('实际整改过程与结果', value=material.get('summary', ''), key='task_result_'+closure_key)
                    evidence_values = []
                    st.caption('填写原件归档位置和原件 SHA-256，供独立验收人复核。系统保存声明，不会自动读取外部文件。')
                    for index in range(evidence_count):
                        item = material.get('evidence', [])[index] if index < len(material.get('evidence', [])) else {}
                        item_key = closure_key+'_'+str(index)
                        evidence_values.append({'name': st.text_input(f'凭证{index+1}名称', value=item.get('name', ''), key='task_evidence_name_'+item_key),
                                                'reference': st.text_input(f'凭证{index+1}归档定位', value=item.get('reference', ''), key='task_evidence_reference_'+item_key),
                                                'sha256': st.text_input(f'凭证{index+1}原件SHA-256', value=item.get('sha256', ''), key='task_evidence_hash_'+item_key)})
                    metric_values = []
                    for index in range(metric_count):
                        item = material.get('metrics', [])[index] if index < len(material.get('metrics', [])) else {}
                        item_key = closure_key+'_'+str(index)
                        metric = {}
                        for field, label in [('name', '指标名称'), ('before', '整改前数值'), ('after', '整改后数值'), ('unit', '计量单位'),
                                             ('before_period', '整改前期间'), ('after_period', '整改后期间'), ('scope', '工厂、产品与规格范围'), ('method', '同口径计算与调整方法')]:
                            metric[field] = st.text_input(f'指标{index+1}：{label}', value=item.get(field) or '', key='task_metric_'+field+'_'+item_key)
                        references = st.text_input(f'指标{index+1}：凭证名称（以中文逗号分隔）', value='，'.join(item.get('evidence_ids', [])), key='task_metric_refs_'+item_key)
                        metric['evidence_ids'] = [value.strip() for value in references.replace(',', '，').split('，') if value.strip()]
                        metric_values.append(metric)
                    evaluation = st.text_area('效果评估与局限', value=material.get('evaluation', ''), key='task_evaluation_'+closure_key)
                    submit_material = st.form_submit_button('提交整改材料供独立验收', type='primary')
                if submit_material:
                    try:
                        repo.submit_rectification(selected_id, {'summary': result_text, 'evidence': evidence_values, 'metrics': metric_values, 'evaluation': evaluation},
                                                  actor=principal, expected_version=version, expected_closure_revision=revision)
                        rerun_notice('task_notice', '整改材料已提交，等待独立验收；尚未关闭任务。')
                    except (ValueError, PermissionError, OSError) as exc:
                        st.error(str(exc))
            if current['business_status'] == 'pending_acceptance' and can(principal, 'task.accept') and latest:
                independent = (principal.user_id not in {latest['actor']['user_id'], current['created_by']['user_id']}
                               and seed['assignee']['name'] not in {principal.user_id, principal.display_name})
                if not independent:
                    st.info('请由另一位有权限的人员验收。任务作者、责任人和本次材料提交人不能自验，本机演示同样适用。')
                with st.form('task_acceptance_'+closure_key):
                    acceptance_comment = st.text_area('验收结论与核对说明', key='task_acceptance_comment_'+closure_key)
                    decision = st.selectbox('验收处理', ['accept', 'rework'], format_func=lambda value: '通过并关闭' if value == 'accept' else '退回返工', key='task_acceptance_decision_'+closure_key)
                    st.caption('通过前请核对归档原件、比较范围和期间、计算口径。远端执行须完成，效果允许无节约或成本增加。')
                    review = st.form_submit_button('记录验收结论', disabled=not independent, type='primary')
                if review:
                    try:
                        result = repo.review_rectification(selected_id, decision=decision, comment=acceptance_comment, actor=principal,
                                                           expected_version=version, expected_closure_revision=revision)
                        if decision == 'accept':
                            anomaly_case = result.get('anomaly_case') or {}
                            if anomaly_case.get('staged'):
                                rerun_notice('task_notice', '任务已验收关闭；已自动生成异常案例候选，请在知识文档库人工确认并发布。')
                            else:
                                rerun_notice('task_notice', '任务已验收关闭。')
                        else:
                            rerun_notice('task_notice', '任务已退回返工，原材料与意见均已保留。')
                    except (ValueError, PermissionError, OSError) as exc:
                        st.error(str(exc))
            if current['business_status'] == 'closed':
                st.success('整改已由独立人员验收关闭，关闭时间：'+current['closed_utc'])
                # 手动兜底：自动暂存未发生时，知识管理员可在此补生成案例候选
                if can(principal, 'knowledge.stage'):
                    from enterprise.anomaly_case import case_title, stage_closed_task
                    title = case_title(selected_id)
                    knowledge_repo = app.knowledge()
                    existing = next((row for row in knowledge_repo.list_documents() if row['title'] == title), None)
                    latest = knowledge_repo.get(existing['doc_id']) if existing else None
                    staged_meta = (latest or {}).get('business_metadata') or {}
                    pending = staged_meta.get('closure_revision') == current.get('closure_revision') \
                        and staged_meta.get('task_id') == selected_id
                    if pending:
                        st.caption(f'异常案例候选已自动暂存（{title}），请在知识文档库确认并发布。')
                    elif st.button('生成异常案例候选（待知识管理员确认）', key='task_case_stage_'+selected_id):
                        submissions = repo.rectifications(selected_id, actor=principal)['submissions']
                        if not submissions:
                            st.error('缺少整改材料，无法生成案例候选。')
                        else:
                            st.session_state['task_case_stage_result'] = stage_closed_task(
                                app.root, selected_id, seed, submissions[-1]['payload'],
                                current['closure_revision'], current['closed_utc'], principal)
                            if st.session_state['task_case_stage_result'].get('staged'):
                                rerun_notice('task_notice', '异常案例候选已生成，请在知识文档库人工确认并发布。')
                            else:
                                st.error('生成失败：' + str(st.session_state['task_case_stage_result'].get('reason')))
        if can(principal, 'task.remind') and current['business_status'] != 'closed':
            if st.button('检查逾期并执行到期催办', key='task_remind', disabled=not current['is_overdue']):
                try:
                    repo.schedule_reminders(actor=principal, task_id=selected_id, limit=1)
                    with RPAClient() as client:
                        repo.dispatch_reminders(client, actor=principal, task_id=selected_id, limit=1)
                    rerun_notice('task_notice', '催办检查已完成。请在通知记录中核对模拟送达结果；同一任务至少间隔24小时。')
                except (ValueError, PermissionError, OSError, RuntimeError) as exc:
                    st.error(str(exc))
        with st.expander('整改历次材料与催办记录'):
            st.json(history)
            st.json(repo.notifications(actor=principal, task_id=selected_id))
    if current.get('last_error'):
        st.warning(current['last_error'].get('message', '发送结果需核对'))
    if current.get('outbox'):
        outbox = current['outbox']
        st.caption(f"发送尝试 {outbox['attempts']} 次 · 查询核对 {outbox['query_attempts']} 次 · 下次尝试 {outbox.get('next_attempt_utc') or '无自动重试'}")
    with st.expander('当前任务 JSON 与回执'):
        st.json(current)
        st.download_button('下载当前任务记录', json.dumps(current, ensure_ascii=False, indent=2), file_name=f'{selected_id}.json', mime='application/json', key='task_download')
    with st.expander('任务版本'):
        st.json(repo.versions(selected_id, actor=principal))
    if can(principal, 'task.audit'):
        with st.expander('审批、发送与回执审计'):
            st.dataframe(repo.events(selected_id, actor=principal), hide_index=True)
