"""Authorized operating overview; financial amounts retain factory/product identity."""
import streamlit as st

from app_pages._shared import page_context
from app_pages.design import banner, kpis, money_columns, page_header
from enterprise.security import can
from enterprise.snapshots import SnapshotRepository

principal, app = page_context()
page_header('成本分析工作台', '工作总览 / 已授权业务范围')
banner('经营分析总览', '当前授权业务范围', '制药成本管理')

current, rows, products, months = None, [], [], []
if can(principal, 'data.read'):
    current = app.current()
    rows = current['tables'].get('cost26', []) + current['tables'].get('erchang26', [])
    products = sorted({r['产品名称'] for r in rows})
    months = sorted({r['月份'] for r in rows})
tasks = app.tasks().summary(actor=principal) if can(principal, 'task.read') else None
pending_tasks = app.tasks().page(actor=principal, status='submitted', limit=1)['total'] if tasks else None
items = [('在分析产品', len(products) if current else '—', '当前授权范围'),
         ('最新业务月份', months[-1] if months else '—', '成本汇总可用期间'),
         ('待审核任务', pending_tasks if pending_tasks is not None else '—', '已提交，等待主管处理'),
         ('需关注任务', tasks['needs_attention'] if tasks else '—', '异常发送、暂停或模拟逾期')]
kpis(items)

with st.container(horizontal=True):
    links = [('dashboard.read', 'dashboard_web.py', '查看产品成本', 'monitoring'),
             ('report.read', 'report_web.py', '进入报告中心', 'description'),
             ('task.read', 'app_pages/tasks.py', '处理整改任务', 'task_alt'),
             ('data.read', 'app_pages/data.py', '导入月度数据', 'upload_file')]
    for action, path, label, icon in links:
        if can(principal, action):
            st.page_link(path, label=label, icon=f':material/{icon}:')

if current:
    st.subheader('当期产品数据')
    if months:
        with st.container(horizontal=True):
            month = st.selectbox('业务月份', months, index=len(months)-1, key='overview_month', width=200)
            factory = st.selectbox('工厂范围', ['全部已授权工厂'] + sorted({r['工厂'] for r in rows}), key='overview_factory', width=220)
        fields = ['工厂', '产品名称', '产品规格', '产量(盒)', '单位成本(元/盒)', '总成本(元)']
        visible = [{k: r.get(k) for k in fields} for r in rows
                   if r['月份'] == month and (factory == '全部已授权工厂' or r['工厂'] == factory)]
        st.dataframe(visible, hide_index=True, width='stretch',
                     column_config={**money_columns('单位成本(元/盒)', '总成本(元)'),
                                    '产量(盒)': st.column_config.NumberColumn(format='%,d'),
                                    '产品名称': st.column_config.TextColumn(pinned=True)})
    else:
        st.info('当前授权范围暂无成本数据。导入月度数据并由主管确认后，即可开始分析。')
    st.caption(f"数据版本：{'V'+str(current['revision']) if current['revision'] else '赛题原始数据基线'} · 各产品分别呈现单位成本，不跨产品求平均。")
else:
    st.info('请从侧栏进入当前角色可用的工作。业务数据按工厂和产品授权范围展示。')

knowledge_tab, history_tab = st.tabs(['最新知识版本', '最近分析档案'])
with knowledge_tab:
    if can(principal, 'knowledge.read'):
        try:
            knowledge = app.knowledge().list_documents()
        except (ValueError, PermissionError, OSError) as exc:
            knowledge = []
            st.warning(str(exc))
        if knowledge:
            st.dataframe([{'文档': v['title'], '版本': v['version'], '生效日期': v['effective_from']}
                          for v in knowledge[:5]], hide_index=True)
        else:
            st.info('当前授权范围暂无确认知识版本。请联系知识管理员登记并发布资料。')
        st.page_link('app_pages/knowledge.py', label='查看知识文档库', icon=':material/library_books:')
    else:
        st.caption('当前角色未授予知识读取权限。')
with history_tab:
    if can(principal, 'report.read', factory='中药一厂'):
        snapshots = SnapshotRepository(app.root, principal=principal).list()
        if snapshots:
            st.dataframe([{'产品': r['product'], '月份': r['month'], '保存时间': r['created'][:19]}
                          for r in snapshots[:5]], hide_index=True)
        else:
            st.info('暂无已保存分析。可在产品成本分析的归因区保存快照。')
        st.page_link('app_pages/history.py', label='打开分析档案', icon=':material/history:')
    else:
        st.caption('当前授权范围没有一厂分析档案。')
