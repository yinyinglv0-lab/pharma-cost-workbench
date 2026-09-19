"""Monthly imports use the shared authorized application boundary."""
import json
import pandas as pd
import streamlit as st

from app_pages._shared import authorize, page_context, rerun_notice, show_notice
from enterprise.cost_imports import SCHEMA, IDENTITY
from enterprise.periods import PeriodRepository
from enterprise.security import can

principal, app = page_context('data.read')
st.title('月度数据管理')
st.caption('按工厂、产品、规格和业务月份识别；主管确认后供报告、看板和对标共用。')
show_notice('cost_notice')
current = app.current()
a, b, c = st.columns(3)
a.metric('当前数据版本', f"V{current['revision']}" if current['revision'] else '原始数据')
b.metric('产品数', len({r['产品名称'] for r in current['tables'].get('cost26', [])}))
c.metric('业务月份数', len({r['月份'] for r in current['tables'].get('cost26', [])}))
with st.expander('下载数据模板与导入说明'):
    st.write('支持 CSV 及 XLSX 多工作表。汇总和相关明细可多文件整批提交，校验后统一发布。')
    for key, cols in SCHEMA.items():
        csv = pd.DataFrame(columns=list(dict.fromkeys(IDENTITY + cols))).to_csv(index=False).encode('utf-8-sig')
        st.download_button(f'下载{key}模板', csv, file_name=f'{key}_模板.csv', key='template_'+key)
    st.caption('已关账期间须由授权主管填写原因重开，再发布历史修订。')

if can(principal, 'data.stage'):
    files = st.file_uploader('选择本次成本文件', type=['csv', 'xlsx'], accept_multiple_files=True, key='cost_batch')
    if st.button('校验并预览', type='primary', disabled=not files, key='cost_stage'):
        try:
            result = app.stage_costs([(f.name, f.getvalue()) for f in files])
            st.session_state['cost_stage_id'] = result['stage_id']
            st.session_state['cost_confirm'] = False
        except (ValueError, PermissionError, OSError) as exc:
            st.session_state.pop('cost_stage_id', None)
            st.error(str(exc))

if can(principal, 'data.confirm'):
    pending = app.pending_costs()
    if pending:
        selected = st.selectbox('主管待确认批次', [None]+[r['stage_id'] for r in pending],
                                format_func=lambda ident: '选择待确认批次' if ident is None else next(
                                    f"{r['stage_id'][:10]} · {r['submitted_by']} · {'、'.join(r['business_periods'])}"
                                    for r in pending if r['stage_id'] == ident), key='cost_pending')
        if selected:
            st.session_state['cost_stage_id'] = selected

stage_id = st.session_state.get('cost_stage_id')
if stage_id:
    try:
        preview = app.preview_costs(stage_id)  # re-authorize, never display cached records
    except (ValueError, PermissionError) as exc:
        st.session_state.pop('cost_stage_id', None)
        st.error(str(exc))
        preview = None
    if preview:
        st.subheader('本批次预览')
        st.caption(f"提交人：{preview['submitted_by']} · 基于 V{preview['base_revision']} · 业务月份：{'、'.join(preview['business_periods']) or '无变化'}")
        columns = st.columns(3)
        for col, (name, count) in zip(columns, preview['counts'].items()):
            col.metric(name, count)
        for error in preview['errors']:
            st.error(error)
        if preview['warnings']:
            with st.expander('明细完整性提示'):
                st.write(preview['warnings'])
        changes = [{**change['key'], '操作': change['action'], '字段': field,
                    '原值': str(values['before']), '新值': str(values['after']), '来源': change['file'],
                    '工作表': change['sheet'], '记录序号': change['row']}
                   for change in preview['changes'] for field, values in change['fields'].items()]
        st.dataframe(changes, hide_index=True)
        st.download_button('下载差异清单', json.dumps(preview['changes'], ensure_ascii=False, indent=2), file_name='导入差异.json')
        if can(principal, 'data.confirm'):
            own = preview['submitted_by'] == principal.user_id
            blocked = own and principal.auth_method != 'local_os_demo'
            if blocked:
                st.info('此批次由您提交，请由另一位财务主管复核发布。')
            if principal.auth_method == 'local_os_demo':
                st.caption('本机演示：当前 OS 用户模拟主管复核；企业模式执行提交与批准职责分离。')
            mode = st.segmented_control('本次业务操作', ['新月份新增', '历史修订'], default='新月份新增', key='cost_mode')
            reason = st.text_input('修订原因（历史修订必填）', key='cost_revision_reason')
            agreed = st.checkbox('已核对业务月份、差异及校验提示', key='cost_confirm')
            if st.button('确认生效', disabled=bool(preview['errors']) or not agreed or blocked,
                         type='primary', key='cost_commit'):
                try:
                    result = app.confirm_costs(stage_id, mode, reason)
                    st.session_state.pop('cost_stage_id', None)
                    rerun_notice('cost_notice', '内容相同，无需更新。' if result['duplicate'] else f"数据版本 V{result['revision']} 已生效。")
                except (ValueError, PermissionError, OSError) as exc:
                    st.error(str(exc))
        else:
            st.info('预览已持久保存，请由财务主管在待确认批次中复核。')

with st.expander('查看当前数据'):
    table = st.selectbox('数据表', list(current['tables']), key='data_table')
    st.dataframe(current['tables'][table], hide_index=True)
if can(principal, 'audit.read'):
    with st.expander('历史版本'):
        st.dataframe(app.cost_history(), hide_index=True)
if can(principal, 'period.manage'):
    with st.expander('月度关账与授权重开'):
        periods = PeriodRepository(app.root)
        states = periods.list(actor=principal)
        st.dataframe(states, hide_index=True)
        keys = sorted({(r['工厂'], r['产品名称'], r['月份']) for rows in current['tables'].values() for r in rows})
        if keys:
            selected = st.selectbox('业务期间', keys, format_func=lambda k: ' / '.join(k), key='cost_period')
            previous = next((r for r in states if (r['factory'], r['product'], r['month']) == selected), {})
            st.caption(f"当前状态：{'已关账' if previous.get('state') == 'closed' else '开放'} · 状态版本 {previous.get('version', 0)}")
            target = st.selectbox('目标状态', ['closed', 'open'], format_func=lambda v: '关账' if v == 'closed' else '重开', key='cost_period_target')
            reason = st.text_input('关账 / 重开原因', key='cost_period_reason')
            if st.button('保存期间状态', key='cost_period_save'):
                try:
                    authorize(principal, 'period.manage', factory=selected[0], product=selected[1])
                    periods.set_state(*selected, target, actor=principal, reason=reason, expected_version=previous.get('version', 0))
                    rerun_notice('cost_notice', '期间状态已保存。重开后的修订报告应注明重述原因。')
                except (ValueError, PermissionError, OSError) as exc:
                    st.error(str(exc))
