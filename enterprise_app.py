"""Unified workbench with server-verified identity and per-page authorization."""
import streamlit as st

from app_pages._shared import page_context
from enterprise.security import can

st.set_page_config(page_title='制药成本分析工作台', page_icon=':material/analytics:', layout='wide')
principal, application = page_context()
st.session_state['enterprise_mode'] = True
with st.sidebar:
    st.markdown('### 制药成本分析工作台')
    st.caption('数据准备 → 分析报告 → 对标 → 整改')
    st.write(f'当前用户：{principal.display_name}')
    st.caption('角色：' + ' / '.join(principal.roles))
    if principal.auth_method == 'local_os_demo':
        st.info('本机演示 · 服务端 OS 身份 · 全角色\n仅限回环访问；本机审批为模拟审批。')
    else:
        st.caption('企业 OIDC 登录 · 权限由服务端映射')
        if st.button('退出登录', key='workbench_logout'):
            st.logout()

pages = {'工作台': [st.Page('app_pages/home.py', title='工作总览', icon=':material/home:', default=True)]}
entries = [
    ('资料与数据', 'knowledge.read', 'app_pages/knowledge.py', '知识文档库', 'library_books', 'knowledge'),
    ('资料与数据', 'data.read', 'app_pages/data.py', '月度数据管理', 'upload_file', 'data'),
    ('成本分析', 'report.read', 'report_web.py', '模块一 · 智能报告', 'description', 'reports'),
    ('成本分析', 'dashboard.read', 'dashboard_web.py', '模块二 · 产品成本', 'monitoring', 'dashboard'),
    ('成本分析', 'dashboard.read', 'app_pages/benchmark.py', '模块三 · 对标分析', 'compare_arrows', 'benchmark'),
    ('成本分析', 'task.read', 'app_pages/tasks.py', '模块四 · 整改任务', 'task_alt', 'tasks'),
    ('追溯与运行', 'report.read', 'app_pages/history.py', '分析档案与更新记录', 'history', 'history'),
    ('追溯与运行', 'system.read', 'app_pages/settings.py', '系统设置与运行检查', 'settings', 'settings'),
]
for section, action, path, title, icon, url in entries:
    if can(principal, action):
        pages.setdefault(section, []).append(st.Page(path, title=title, icon=f':material/{icon}:', url_path=url))
st.navigation(pages, position='sidebar').run()
