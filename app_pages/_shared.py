"""UI-only helpers. Identity and authorization always come from the server."""
from __future__ import annotations

import streamlit as st

from enterprise.application import Application
from enterprise.security import require, streamlit_principal


def page_context(action=None):
    principal = streamlit_principal()
    scope = (principal.user_id, principal.tenant_id, principal.roles,
             principal.factories, principal.products, principal.auth_method)
    if st.session_state.get('_principal_scope') != scope:
        # A role/scope change invalidates every retained record, download and form.
        for key in list(st.session_state):
            del st.session_state[key]
        st.session_state['_principal_scope'] = scope
    if action:
        try:
            require(principal, action)
        except PermissionError as exc:
            st.error(str(exc))
            st.stop()
    return principal, Application(principal)


def authorize(principal, action, **scope):
    try:
        require(principal, action, **scope)
    except PermissionError as exc:
        st.error(str(exc))
        st.stop()


def show_notice(key):
    notice = st.session_state.pop(key, None)
    if notice:
        if isinstance(notice, dict):
            renderer = {'success': st.success, 'warning': st.warning, 'info': st.info}.get(notice.get('level'), st.info)
            renderer(notice['message'])
        else:
            st.success(notice)


def rerun_notice(key, message, *, level='success'):
    st.session_state[key] = {'message': message, 'level': level}
    st.rerun()


def generation_notice(status):
    """Explain model fallback without exposing provider diagnostics in the main flow."""
    messages = {
        'deterministic_requested': '本次选择规则分析，金额与来源由程序计算；未请求模型生成解释。',
        'model_not_configured': '尚未配置可用的模型服务，已使用规则分析。请由运维配置服务端凭据与授权，再由管理员在系统设置中选择分析模型。',
        'no_api_key': '所选模型尚未配置服务端专属凭据，已使用规则分析。请联系管理员检查配置，页面不接收密钥。',
        'retrieval_unavailable': '正式模型分析要求向量＋BM25混合检索；当前未就绪，未调用模型。请完成向量发布和模型预热后重试，数值事实仍可查看。',
        'model_unavailable': '模型服务暂不可用或未在时限内完成，已保留规则分析结果。请在系统设置中检查模型连接、配额和网络后，再生成新草稿。',
        'model_rejected': '模型输出未通过结构、数字或证据检查，已保留规则分析结果。请核对证据与检查详情，再决定是否重新生成；结果仍需业务复核。',
    }
    return messages.get(status, '当前使用规则分析结果，模型未提供可采用的解释。请查看生成检查详情并进行业务复核。')


def wait_for_analysis_warmup(principal, repository):
    """Wait only for this UI process's already-started embedding cold load.

    API readiness belongs to a different process. This never publishes knowledge,
    sends a model request, or treats a missing/lexical release as compliant.
    The analysis service still independently enforces strict hybrid retrieval.
    """
    from enterprise.knowledge_runtime import readiness
    state = readiness(principal, repository=repository, require_hybrid=True)
    if not state.get('ready') and state.get('state') == 'loading':
        with st.spinner('正在预热当前页面进程的本地向量模型；尚未调用生成模型…'):
            state = readiness(principal, repository=repository, wait=True, timeout=90,
                              require_hybrid=True)
    return state


def home_tables(application):
    """Restrict module one/two to their explicit home-factory accounting basis."""
    tables = application.tables()
    return {key: frame.loc[frame['工厂'].eq('中药一厂')].copy()
            if not frame.empty and '工厂' in frame else frame
            for key, frame in tables.items()}

