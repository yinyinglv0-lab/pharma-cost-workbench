import streamlit as st

from app_pages._shared import page_context, show_notice
from enterprise.model_gateway import ModelUnavailable
from enterprise.model_registry import selection_state, save_selection
from enterprise.security import can

principal, app = page_context('system.read')
st.title('系统设置与运行检查')
st.caption('模型选择由服务端保存；查看或保存本页不会发起模型请求。')
show_notice('settings_notice')
# Discard stale credentials from the retired browser configuration form.
for old_key in ('settings_api_key', 'settings_base_url', 'settings_model_validation', 'settings_clear_secret'):
    st.session_state.pop(old_key, None)
try:
    state = selection_state()
except (ModelUnavailable, ValueError, OSError):
    state = {'selected_registry_id': None, 'entries': []}
    st.error('模型注册配置不可读取，请管理员检查服务端配置；原选择未更改。')
rows = {row['registry_id']: row for row in state['entries']}
current = rows.get(state['selected_registry_id'], {})
a, b, c = st.columns(3)
a.metric('身份模式', '本机 OS 演示' if principal.auth_method == 'local_os_demo' else '企业 OIDC')
b.metric('当前分析模型', current.get('model') or '未配置')
c.metric('任务发送通道', '官方 mock')
c.caption('127.0.0.1:8090 · 本机模拟')
st.subheader('分析模型选择（整个部署共用）')
st.caption('管理员保存后应用于所有用户的新模块二、模块三及同口径报告生成，不改变既有历史结果或其他任务路由。未明确选择时继续使用原有配置。')
st.caption('凭据、接口地址、模型注册与云数据授权仅由服务端运维配置。本页只选择名称，不接收密钥或地址；选择模型不等于授权云调用。')
if rows:
    st.dataframe([{'注册名称': row['registry_id'], '请求模型': row['model'], '配置档': row['profile'],
                   '凭据状态': '已配置' if row['configured'] else '未配置',
                   '云授权': '已授权' if row['approved_cloud'] else '未授权',
                   '每次请求时限（秒）': row['timeout_seconds'],
                   '最近实际耗时（秒）': row.get('last_observed_latency_seconds'),
                   '观测任务': {'attribution': '模块二', 'benchmark': '模块三', 'report': '报告'}.get(row.get('last_observed_task'), ''),
                   '调用状态': {'returned_json': '已返回JSON（未代表验收）', 'invalid_response': '返回无效', 'unavailable': '调用失败'}.get(row.get('last_observed_status'), '')}
                  for row in rows.values()], hide_index=True)
    st.caption('耗时仅来自同一配置的实际调用观测；空白表示未观测，不是预计速度。不同模型的付费验收结果不能相互继承；历史成功不保证当前可用。')
    if can(principal, 'system.configure'):
        names = list(rows)
        def option_label(name):
            row = rows[name]
            status = '可用配置' if row['selectable'] else '凭据未配置' if not row['configured'] else '尚未授权'
            latency = row.get('last_observed_latency_seconds')
            task_label = {'attribution': '模块二', 'benchmark': '模块三', 'report': '报告'}.get(row.get('last_observed_task'), '')
            observed = f'最近{task_label}实测 {latency:g} 秒' if latency is not None else '尚无实测'
            return f"{name} · {row['model']} · {status} · 超时 {row['timeout_seconds']:g} 秒 · {observed}"
        with st.form('settings_model_selection'):
            selected = st.selectbox('全局分析模型', names,
                index=names.index(state['selected_registry_id']),
                format_func=option_label,
                key='settings_selected_registry_id')
            saved = st.form_submit_button('保存全局模型选择', type='primary')
        if saved:
            try:
                save_selection(selected, principal=principal)
                st.session_state['settings_notice'] = '全局模型选择已保存；未发送测试请求，业务生成仍需证据与数值校验。'
                st.rerun()
            except (ModelUnavailable, ValueError, PermissionError, OSError) as exc:
                st.error('选择未保存：' + str(exc) if isinstance(exc, (ModelUnavailable, ValueError, PermissionError)) else '配置文件写入失败，原选择保留。')
    else:
        st.info('仅具有系统配置权限的管理员可更改全局模型；当前页面为只读。')

st.subheader('已接入能力与部署验收')
st.dataframe([
    {'范围': '身份与数据', '当前能力': '每页验权；工厂/产品过滤；成本预览、独立确认与关账重开', '部署验收': '企业 OIDC 授权映射及撤权测试'},
    {'范围': '知识', '当前能力': '元数据版本、受控原件、发布清单、按范围与日期检索', '部署验收': '本机索引模型可用性、实际资料检索质量'},
    {'范围': '模块一', '当前能力': '月度/季度/专题冻结报告；草稿、审核、Word/PDF', '部署验收': '实际模型与报告文字人工复核'},
    {'范围': '模块二/三', '当前能力': '原有成本图表、金额归因、跨厂三步分析、来源追溯', '部署验收': '实际模型输出与业务证据支撑'},
    {'范围': '模块四', '当前能力': '持久草稿、人工编辑、审批、签发、官方 mock 发送与回执', '部署验收': '官方 mock 服务运行及端到端回执；真实微信未启用'},
    {'范围': '生产运行', '当前能力': '受控记录与权限接口已接入', '部署验收': '目标环境备份恢复、并发容量、运维监控及用户验收'},
], hide_index=True)
st.caption('本机模式仅用于当前 OS 用户演示。企业模式使用 st.login 与服务端 subject→角色/范围映射，默认拒绝未授权账户。')
