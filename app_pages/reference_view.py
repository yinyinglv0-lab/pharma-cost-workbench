"""Shared reference-only comparison UI. All writes require an explicit click."""
from pathlib import PureWindowsPath

import streamlit as st

from enterprise.security import can


def render_market_references(evidence, *, product, specification, month, key):
    """Authorized current/prior-month quotes, separate from causal explanation."""
    from enterprise.evidence_references import market_reading_rows
    from enterprise.numeric import format_percent
    rows = market_reading_rows(evidence, product, specification, month)
    with st.expander('市场参考与采购实价边界'):
        if not rows:
            st.info('当前生成未取得适用、已授权的市场参考；不从原始CSV旁路补充。')
            return
        st.dataframe([{'药材': row['material'], '等级': row['grade'], '月份': row['month'],
                       '单位': row['unit'], '上月参考价': row['previous_price'] or '无上月',
                       '本月参考价': row['current_price'],
                       '参考价环比': '无定义' if row['month_change_pct'] is None else format_percent(row['month_change_pct'], signed=True)+'%',
                       '来源市场': row['source_market'], '引用': '['+row['evidence_id']+']'} for row in rows],
                     hide_index=True, key=key+'_market')
        st.caption('只展示所选月份及上月，未来报价与整期趋势不进入此阅读层。' + rows[0]['boundary'])


def render_industry_view(view, application, *, product, specification, month, key, show_peer=True, radar=True):
    from dashboard.charts import industry_radar
    from dashboard.echarts_helper import render_echarts_multi
    from enterprise.reference_service import save_industry_verification
    result = view['result']
    st.caption('行业分位是年度类别参照；工厂值是所选产品规格月份的观测。文件“本厂水平”单独列示，不替代月度实测。')
    if not result.get('available'):
        st.info(result.get('reason', '当前发布未检索到适用行业基准行。'))
        return
    display = []
    for row in result['rows']:
        item = {'指标': row['metric'], '单位': row['unit'],
                '行业P25': row['p25']['value'], '行业P50': row['p50']['value'],
                '行业P75': row['p75']['value'], '文件本厂水平': row['source_reported_home']['value'],
                '本厂所选月': row['home']['value'], '本厂参考位置': row['home']['position_label'],
                '对标评价（原文件）': row.get('source_evaluation') or '—'}
        if show_peer:
            item.update({'同业工厂所选月': row['peer']['value'], '同业工厂参考位置': row['peer']['position_label']})
        display.append(item)
    st.dataframe(display, hide_index=True, width='stretch', key=key+'_table')
    if radar and result.get('radar', {}).get('available'):
        render_echarts_multi([(industry_radar(result), 470, True)])
        st.caption(result['radar']['caption'])
    st.caption(result['boundary'] + '成本占比偏低不等于效率更高；两厂同时偏离也不证明行业性根因。')
    with st.expander('查看行业参考原文、口径与追溯记录'):
        for source in result.get('sources', []):
            location = source['source']
            st.caption(f"{PureWindowsPath(location['file']).name} · 逻辑记录{location['record_number']} · 发布{source.get('index_release_id')}")
            st.text(source['text'])
            st.caption(source['claim_boundary'])
        st.json({'retrieval': view.get('diagnostics', {}), 'validation': result.get('diagnostics', []),
                 'profile_sha256': result.get('profile_sha256')}, expanded=False)
    for alert in result.get('alerts', []):
        # 同比偏离不再红色抢答：折叠为参考信息，是否写入对标文本由模型判断
        with st.expander('行业同比偏离参考（折叠）', expanded=False):
            st.info('待复核行业偏离：' + alert['finding'])
            st.caption(alert['boundary'])
            st.caption('所选产品和月份仅为核查入口；行业统计窗口、本厂同比计算底稿仍待确认。')
        if can(application.principal, 'task.create', factory=alert['factory'], product=product):
            if st.button('保存行业偏离验证草稿', key=key+'_task_'+alert['alert_id']):
                try:
                    task = save_industry_verification(application, product, specification, month, alert['alert_id'])
                    st.session_state['task_next_selection'] = task['task_id']
                    st.success('已保存或定位已有验证任务；本次没有调用模型、审批、签发或派发。')
                    st.caption('当前任务状态：' + task['workflow_status'])
                except (ValueError, PermissionError, OSError) as exc:
                    st.error(str(exc))
