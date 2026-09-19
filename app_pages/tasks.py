"""Persistent task review, issuance, official mock dispatch and receipt tracking."""
from datetime import date, timedelta
import json
import streamlit as st

from app_pages._shared import authorize, page_context, rerun_notice, show_notice
from enterprise.rpa_client import RPAClient, RPAError
from enterprise.security import can

principal, app = page_context('task.read')
repo = app.tasks()
st.title('整改任务与跟踪')
st.caption('模块四 · 持久草稿 → 人工编辑 → 提交 → 主管批准 → 签发 → 官方 mock → 回执')
st.info('发送仅连接官方本机 mock（127.0.0.1:8090）。消息与生命周期均为模拟，不代表真实微信送达或实际整改完成。')
show_notice('task_notice')
labels = {'draft': '草稿', 'submitted': '待审核', 'approved': '已批准', 'rejected': '已退回', 'issued': '已签发',
          'not_sent': '未发送', 'pending': '等待发送', 'sending': '发送中', 'retry': '等待重试', 'unknown': '结果待确认',
          'failed': '发送失败', 'accepted': 'mock 已受理', 'sent': '模拟已发送', 'received': '模拟已接收',
          'confirmed': '模拟已确认', 'in_progress': '模拟进行中', 'completed': '模拟已完成', 'overdue': '模拟逾期'}
summary = repo.summary(actor=principal)
a, b, c, d = st.columns(4)
a.metric('已生成任务', summary['generated'])
b.metric('模拟已接收', summary['received'])
c.metric('模拟已确认', summary['confirmed'])
d.metric('模拟已完成', summary['completed'])
if summary['needs_attention']:
    st.warning(f"{summary['needs_attention']}项任务需处理未知结果、暂停、失败或逾期状态。")
rows = repo.list(actor=principal)
if summary['generated'] > len(rows):
    st.caption('下表展示最近100项可见任务；计数覆盖全部已授权任务。')
if rows:
    st.dataframe([{'任务': r['content']['task_title'], '任务 ID': r['task_id'], '责任人': r['content']['assignee']['name'],
                   '部门': r['content']['assignee']['department'], '截止日': r['content']['deadline'],
                   '审核': labels.get(r['workflow_status'], r['workflow_status']),
                   '发送': labels.get(r['dispatch_status'], r['dispatch_status']),
                   '回执': labels.get(r['receipt_status'], r['receipt_status']) or '尚无回执', '版本': r['version']}
                  for r in rows], hide_index=True)
by_id = {r['task_id']: r for r in rows}
options = (['new'] if can(principal, 'task.create') else []) + list(by_id)
if not options:
    st.info('当前授权范围暂无任务。')
    st.stop()
next_id = st.session_state.pop('task_next_selection', None)
if next_id in options:
    st.session_state['task_selected'] = next_id
if st.session_state.get('task_selected') not in options:
    st.session_state.pop('task_selected', None)
selected_id = st.selectbox('任务对象', options, format_func=lambda key: '新建草稿' if key == 'new' else
                           f"{by_id[key]['content']['task_title']} · {key} · {labels.get(by_id[key]['workflow_status'], by_id[key]['workflow_status'])} · v{by_id[key]['version']}", key='task_selected')
try:
    current = repo.get(selected_id, actor=principal) if selected_id != 'new' else None
except (ValueError, PermissionError, OSError) as exc:
    st.error(str(exc))
    st.stop()
seed = current['content'] if current else {}

if current:
    a, b, c = st.columns(3)
    a.metric('工作流', labels.get(current['workflow_status'], current['workflow_status']))
    b.metric('发送状态', labels.get(current['dispatch_status'], current['dispatch_status']))
    c.metric('回执状态', labels.get(current['receipt_status'], current['receipt_status']) or '尚无回执')
    st.caption(current['generation']['label'])
    st.write(seed['source']['finding'])
    st.write(seed['suggestion'])
    st.caption(f"任务 ID：{selected_id} · 产品：{seed['source']['product']} · 工厂：{'、'.join(seed['factories'])}")

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
        analysis_month = st.text_input('分析月份（YYYY-MM）', value=source.get('analysis_month', date.today().strftime('%Y-%m')), key='task_month_'+suffix)
        finding = st.text_area('分析结论与来源', value=source.get('finding', ''), key='task_finding_'+suffix)
        suggestion = st.text_area('核查内容与交付要求', value=seed.get('suggestion', ''), height=150, key='task_suggestion_'+suffix)
        save = st.form_submit_button('保存持久草稿', type='primary')
        generate = st.form_submit_button('AI 生成并保存任务建议') if current is None else False
    if save or generate:
        content = {'task_title': title, 'assignee': {'name': name, 'department': department, 'role': role},
                   'priority': priority, 'deadline': deadline.isoformat(), 'suggestion': suggestion,
                   'source': {'analysis_type': analysis_type, 'analysis_month': analysis_month, 'product': product, 'finding': finding},
                   'factories': factories, 'evidence_ids': seed.get('evidence_ids', []), 'analysis_run_id': seed.get('analysis_run_id', '')}
        try:
            authorize(principal, 'task.create')
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
