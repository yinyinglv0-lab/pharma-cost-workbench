"""模型配置页：页面填写 API 密钥 → 本机保存 → 硬门禁生效。

- 密钥只存本机 `.local/llm_keys.json`（已被 .gitignore 排除，不进公开源码包）；
- 页面不回显明文，只显示"已保存（长度 N）"；环境变量优先于页面保存值；
- "测试连接"发起一次微小付费调用（≤8 token）验证密钥；
- 硬门禁：所选模型的密钥+授权任一缺失时，归因/对标/报告生成入口直接拒绝生成
  相关文本（页面按钮禁用 + 明确提示），不做静默降级；
  已配置但调用期失败仍走确定性降级。
"""
import json

import streamlit as st

from enterprise.model_settings import (load_credentials, require_generation_model,
                                       resolved_approval, resolved_api_key,
                                       save_credentials, status_view, test_connection)
from enterprise.model_registry import SELECTED_TASKS, _BUILTINS, _PROVIDERS, selection_state
from enterprise.model_gateway import ModelUnavailable, config_path, _read_local

PROVIDER_LABELS = {
    'dashscope': '阿里云百炼（qwen-plus / qwen-turbo / qwen-vl-plus）',
    'deepseek': 'DeepSeek（deepseek-flash）',
    'zhipu': '智谱（glm-4.5-air / glm-4v-plus）',
    'moonshot': '月之暗面（kimi-k3）',
}


def _save_selection(selected, correction):
    path = config_path()
    local = _read_local()
    local = {**local, 'selected_registry_id': selected,
             'correction_registry_id': correction}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(local, ensure_ascii=False, indent=2), encoding='utf-8')


st.title('模型配置')
st.caption('在页面填写各模型 API 密钥与云端调用授权；未配置成功的模型，相关文本拒绝生成。')

with st.container(border=True):
    st.subheader('① 供应商密钥与授权')
    st.caption('密钥仅保存于本机 .local（不进入公开源码包）；环境变量已设置时优先于页面保存值。')
    for provider, label in PROVIDER_LABELS.items():
        settings = _PROVIDERS[provider]
        status = status_view(provider)
        saved = status.get('saved_key_length', 0)
        env_set = status.get('env_key_set', False)
        with st.expander(f'{label}', expanded=False):
            c_key, c_auth = st.columns([2, 1])
            with c_key:
                new_key = st.text_input(
                    'API 密钥', type='password', key=f'mc_key_{provider}',
                    placeholder=('已保存（长度 %d）' % saved) if saved else '粘贴密钥后保存',
                    help='留空保存 = 不修改已保存的密钥')
            with c_auth:
                approved = st.checkbox(
                    '云端调用授权', key=f'mc_approved_{provider}',
                    value=bool(status.get('saved_approved')),
                    help='必须勾选，对应模型才可用于生成')
            st.caption(f'状态：环境变量 {"已设置" if env_set else "未设置"}'
                       f' · 页面已保存密钥 {"是（长度 %d）" % saved if saved else "否"}'
                       f' · 当前有效授权 {"是" if resolved_approval(settings["consent"]) else "否"}')
            c_save, c_test = st.columns([1, 1])
            with c_save:
                if st.button('保存', key=f'mc_save_{provider}'):
                    try:
                        save_credentials(provider, new_key or None, approved)
                        st.success('已保存（本机文件）。密钥不回显。')
                    except Exception as exc:
                        st.error(str(exc))
            with c_test:
                if st.button('测试连接（1 次微小调用）', key=f'mc_test_{provider}'):
                    with st.spinner('测试中…'):
                        result = test_connection(provider)
                    if result['ok']:
                        st.success(f"连接成功：模型 {result.get('model')}，"
                                   f"耗时 {result.get('latency_seconds')} 秒")
                    else:
                        st.error(f"连接失败：{result.get('reason')}")

with st.container(border=True):
    st.subheader('② 主模型 / 修正模型选择（多模型协作）')
    st.caption('初稿用主模型；仅当合同校验违规时，修正轮使用修正模型（未选则沿用主模型）。')
    local = _read_local()
    selected = local.get('selected_registry_id', 'legacy')
    correction = local.get('correction_registry_id', '')
    options = ['legacy'] + list(_BUILTINS)
    c_main, c_corr = st.columns(2)
    with c_main:
        main = st.selectbox('主模型（初稿生成）', options,
                            index=options.index(selected) if selected in options else 0,
                            key='mc_main')
    with c_corr:
        corr = st.selectbox('修正模型（可选）', ['（沿用主模型）'] + options,
                            index=(options.index(correction) + 1 if correction in options else 0),
                            key='mc_corr')
    if st.button('保存选择', type='primary', key='mc_save_select'):
        _save_selection(main, '' if corr.startswith('（') else corr)
        st.success('已保存选择。生成入口将按新选择校验硬门禁。')

with st.container(border=True):
    st.subheader('③ 当前模型状态与硬门禁')
    try:
        state = selection_state()
        rows = state['entries']
    except ModelUnavailable as exc:
        st.error(str(exc))
        st.stop()
    st.caption(f"当前选定：{state.get('selected_registry_id', 'legacy')}")
    table = []
    for row in rows:
        table.append({'模型': row['registry_id'], '提供方': row['provider'],
                      '模型名': row['model'], '密钥': '✅' if row['configured'] else '❌',
                      '授权': '✅' if row['approved_cloud'] else '❌',
                      '可选': '✅' if row['selectable'] else '❌',
                      '超时(秒)': row['timeout_seconds']})
    st.dataframe(table, hide_index=True, width='stretch')
    st.warning('硬门禁：归因分析、跨厂对标、报告生成在选择"不可选"模型时会被明确拒绝'
               '（提示"模型未配置成功，无法生成相关文本"），不会静默降级为确定性文本；'
               '只有已配置成功的模型在调用期失败时才会降级确定性结果。')
