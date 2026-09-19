import streamlit as st
from app_pages._shared import page_context
from enterprise.security import can
from enterprise.snapshots import SnapshotRepository

principal, app = page_context()
st.title('成本分析工作台')
st.caption('已授权业务范围 / 成本管理闭环')

with st.container(horizontal=True):
    links = [('data.read', 'app_pages/data.py', '月度数据', 'upload_file'),
             ('knowledge.read', 'app_pages/knowledge.py', '知识文档', 'library_books'),
             ('dashboard.read', 'dashboard_web.py', '产品分析', 'monitoring'),
             ('report.read', 'report_web.py', '报告与审核', 'description'),
             ('task.read', 'app_pages/tasks.py', '整改任务', 'task_alt'),
             ('system.read', 'app_pages/settings.py', '系统设置', 'settings')]
    for action, path, label, icon in links:
        if can(principal, action):
            st.page_link(path, label=label, icon=f':material/{icon}:')

if can(principal, 'data.read'):
    current = app.current()
    rows = current['tables'].get('cost26', []) + current['tables'].get('erchang26', [])
    products = sorted({r['产品名称'] for r in rows})
    months = sorted({r['月份'] for r in rows})
    a, b = st.columns(2)
    a.metric('在分析产品', len(products), border=True)
    b.metric('最新业务月份', months[-1] if months else '—', border=True)
    st.subheader('当期产品数据')
    if months:
        month = st.selectbox('业务月份', months, index=len(months)-1, key='overview_month')
        fields = ['工厂', '产品名称', '产品规格', '产量(盒)', '单位成本(元/盒)', '总成本(元)']
        st.dataframe([{k: r.get(k) for k in fields} for r in rows if r['月份'] == month], hide_index=True)
    else:
        st.info('当前授权范围暂无成本数据。')
    st.caption(f"当前成本数据版本：{'V'+str(current['revision']) if current['revision'] else '赛题原始数据基线'}")
else:
    st.info('请从上述入口处理当前角色的工作。业务数据按工厂和产品授权范围展示。')

if can(principal, 'knowledge.read'):
    try:
        knowledge = app.knowledge().list_documents()
    except (ValueError, PermissionError, OSError) as exc:
        knowledge = []
        st.warning(str(exc))
    st.subheader('最新知识版本')
    if knowledge:
        st.dataframe([{'文档': v['title'], '版本': v['version'], '生效日期': v['effective_from']}
                      for v in knowledge[:5]], hide_index=True)
    else:
        st.info('当前授权范围暂无确认知识版本。')
if can(principal, 'report.read', factory='中药一厂'):
    snapshots = SnapshotRepository(app.root, principal=principal).list()
    st.subheader('最近分析档案')
    if snapshots:
        st.dataframe([{'产品': r['product'], '月份': r['month'], '保存时间': r['created'][:19]}
                      for r in snapshots[:5]], hide_index=True)
    else:
        st.info('暂无已保存的分析快照。')
if can(principal, 'task.read'):
    tasks = app.tasks().list(actor=principal)
    st.caption(f'整改任务 {len(tasks)} 条 · 草稿、审核、签发、官方 mock 发送与回执分别跟踪。')
if can(principal, 'system.read') and not can(principal, 'data.read'):
    st.caption('系统管理员可配置模型与检查运行能力；业务数据需单独授权。')
