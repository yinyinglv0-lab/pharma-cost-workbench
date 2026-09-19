import streamlit as st

from app_pages._shared import authorize, page_context, show_notice
from enterprise.model_gateway import configuration, generate_json, save_local_configuration, ModelUnavailable
from enterprise.security import can

principal, app = page_context('system.read')
st.title('系统设置与运行检查')
st.caption('配置与连通验证分别记录；模型未配置或调用失败时，业务页明确显示规则分析状态。')
show_notice('settings_notice')
if st.session_state.pop('settings_clear_secret', False):
    st.session_state.pop('settings_api_key', None)
try:
    public = configuration().public()
except (ModelUnavailable, ValueError):
    public = {'base_url': '', 'model': '', 'configured': False, 'approved_cloud': False}
    st.error('模型配置不可读取，请检查服务端配置文件。')
a, b, c = st.columns(3)
a.metric('身份模式', '本机 OS 演示' if principal.auth_method == 'local_os_demo' else '企业 OIDC')
b.metric('模型凭据', '已配置' if public['configured'] else '待配置')
c.metric('任务发送通道', '官方 mock · 127.0.0.1:8090')

if can(principal, 'system.configure'):
    with st.form('settings_model_form'):
        st.subheader('模型 API 配置')
        base_url = st.text_input('OpenAI 兼容接口地址', value=public['base_url'], key='settings_base_url')
        model = st.text_input('模型名称', value=public['model'], key='settings_model')
        api_key = st.text_input('API 密钥', type='password', key='settings_api_key',
                               help='用于模型 API 配置；不会显示已保存密钥。保存后清空输入。')
        approved = st.checkbox('已确认外部接口及分析数据的使用权限', value=public['approved_cloud'], key='settings_cloud_approved')
        saved = st.form_submit_button('保存模型配置', type='primary')
    if saved:
        try:
            save_local_configuration(base_url, model, api_key, approved, principal=principal)
            st.session_state['settings_clear_secret'] = True
            st.session_state['settings_notice'] = '模型配置已保存，密钥输入已清空。请执行连通验证。'
            st.session_state.pop('settings_model_validation', None)
            st.rerun()
        except (ValueError, PermissionError, OSError) as exc:
            # Never echo widget values, provider exceptions, or configuration objects.
            st.error('配置未保存：' + str(exc) if isinstance(exc, (ValueError, PermissionError)) else '配置文件写入失败。')
    if st.button('验证模型连通', disabled=not public['configured'], key='settings_test_model'):
        authorize(principal, 'system.configure')
        try:
            with st.spinner('发送不含业务数据的 JSON 连通请求…'):
                response = generate_json('仅返回JSON对象 {"ok":true}。', {'purpose': 'connection_check'}, max_tokens=32)
            if response.get('ok') is not True:
                raise ModelUnavailable('接口返回未通过 JSON 连通检查')
            st.session_state['settings_model_validation'] = (public['base_url'], public['model'], 'success')
        except ModelUnavailable as exc:
            st.session_state['settings_model_validation'] = (public['base_url'], public['model'], str(exc))
    validation = st.session_state.get('settings_model_validation')
    if validation and validation[:2] == (public['base_url'], public['model']):
        if validation[2] == 'success':
            st.success('当前会话已通过模型 JSON 连通验证。具体业务输出仍需各模块校验。')
        else:
            st.warning(validation[2])
    else:
        st.caption('连通状态：待验证。保存配置不代表模型调用已经成功。')

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
