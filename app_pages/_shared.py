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


def home_tables(application):
    """Restrict module one/two to their explicit home-factory accounting basis."""
    tables = application.tables()
    return {key: frame.loc[frame['工厂'].eq('中药一厂')].copy()
            if not frame.empty and '工厂' in frame else frame
            for key, frame in tables.items()}

