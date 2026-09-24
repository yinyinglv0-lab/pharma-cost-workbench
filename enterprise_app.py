"""Unified workbench with server-verified identity and per-page authorization."""
import streamlit as st

from app_pages._shared import page_context
from app_pages.design import apply_design
from enterprise.security import can

st.set_page_config(page_title='制药成本分析工作台', page_icon=':material/analytics:', layout='wide')
apply_design()
st.logo('assets/workbench-brand.png', size='large', icon_image=':material/analytics:')
principal, application = page_context()
if can(principal, 'knowledge.read'):
    from enterprise.knowledge_runtime import begin_warmup
    st.session_state['_knowledge_readiness'] = begin_warmup(principal, repository=application.knowledge())
st.session_state['enterprise_mode'] = True
with st.sidebar:
    st.caption(f'当前用户 · {principal.display_name}')
    role_labels = {'analyst': '分析员', 'supervisor': '审核主管', 'knowledge_admin': '知识管理员',
                   'auditor': '审计员', 'system_admin': '系统管理员'}
    with st.expander(f'角色与数据范围（{len(principal.roles)}项）', icon=':material/verified_user:'):
        st.write('、'.join(role_labels.get(role, role) for role in principal.roles))
        st.caption('工厂：' + '、'.join(principal.factories))
        st.caption('产品：' + '、'.join(principal.products))
    if principal.auth_method == 'local_os_demo':
        st.badge('本机演示', color='orange', icon=':material/computer:')
        st.caption('审批与发送均为模拟。')
    else:
        st.caption('企业 OIDC 登录 · 权限由服务端映射')
        if st.button('退出登录', key='workbench_logout'):
            st.logout()

pages = {'工作台': [st.Page('app_pages/home.py', title='工作总览', icon=':material/home:', default=True)]}
entries = [
    ('资料与数据', 'knowledge.read', 'app_pages/knowledge.py', '知识文档库', 'library_books', 'knowledge'),
    ('资料与数据', 'data.read', 'app_pages/data.py', '月度数据管理', 'upload_file', 'data'),
    ('分析与行动', 'dashboard.read', 'dashboard_web.py', '产品成本分析', 'monitoring', 'dashboard'),
    ('分析与行动', 'dashboard.read', 'app_pages/benchmark.py', '跨厂成本对标', 'compare_arrows', 'benchmark'),
    ('分析与行动', 'report.read', 'report_web.py', '报告中心', 'description', 'reports'),
    ('分析与行动', 'task.read', 'app_pages/tasks.py', '整改任务', 'task_alt', 'tasks'),
    ('分析辅助', 'analysis.generate', 'app_pages/forecast.py', '成本预测基线', 'trending_up', 'forecast'),
    ('分析辅助', 'analysis.generate', 'app_pages/multimodal.py', '多模态辅助理解', 'image', 'multimodal'),
    ('分析辅助', 'data.read', 'app_pages/agent.py', '分析助手', 'smart_toy', 'assistant'),
    ('追溯与运行', 'report.read', 'app_pages/history.py', '分析档案与更新记录', 'history', 'history'),
    ('追溯与运行', 'system.read', 'app_pages/settings.py', '系统设置与运行检查', 'settings', 'settings'),
    ('追溯与运行', 'system.read', 'app_pages/model_config.py', '模型配置', 'key', 'model_config'),
]
for section, action, path, title, icon, url in entries:
    if can(principal, action):
        pages.setdefault(section, []).append(st.Page(path, title=title, icon=f':material/{icon}:', url_path=url))
st.navigation(pages, position='sidebar').run()
