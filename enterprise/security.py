"""Shared authenticated identities and deny-by-default business authorization.

Local mode is a loopback-only, single-OS-user demonstration. Production uses
OIDC identities plus an operator-managed authorization file; request data can
never select a role, tenant or audit actor.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import getpass
import ipaddress
import json
import os
from pathlib import Path
import time


class AuthenticationError(PermissionError):
    pass


class AuthorizationError(PermissionError):
    pass


@dataclass(frozen=True)
class Principal:
    user_id: str
    display_name: str
    roles: tuple[str, ...]
    factories: tuple[str, ...]
    products: tuple[str, ...]
    tenant_id: str = 'default'
    auth_method: str = 'oidc'

    def __post_init__(self):
        if not self.user_id or not self.display_name:
            raise AuthenticationError('身份缺少稳定用户ID或显示名')
        for field in ('roles', 'factories', 'products'):
            object.__setattr__(self, field, tuple(getattr(self, field)))


PERMISSIONS = {
    'analyst': frozenset({'dashboard.read', 'data.read', 'data.stage', 'knowledge.read',
                         'analysis.generate', 'report.generate', 'report.read', 'task.create', 'task.read', 'task.rectify'}),
    'supervisor': frozenset({'dashboard.read', 'data.read', 'data.stage', 'data.confirm', 'period.manage',
                            'knowledge.read', 'knowledge.publish', 'analysis.generate', 'report.generate',
                            'report.read', 'report.approve', 'task.create', 'task.read', 'task.approve',
                            'task.send', 'task.audit', 'task.rectify', 'task.accept', 'task.remind', 'audit.read'}),
    'knowledge_admin': frozenset({'knowledge.read', 'knowledge.stage', 'knowledge.publish', 'knowledge.audit'}),
    'auditor': frozenset({'dashboard.read', 'data.read', 'knowledge.read', 'report.read', 'task.read',
                         'task.audit', 'audit.read'}),
    'system_admin': frozenset({'system.read', 'system.backup', 'system.configure'}),
}


def auth_mode():
    mode = os.environ.get('COST_AUTH_MODE', 'local').strip().lower()
    if mode not in ('local', 'oidc'):
        raise AuthenticationError('COST_AUTH_MODE须为local或oidc')
    return mode


def configured_tenant():
    return os.environ.get('COST_TENANT_ID', 'default')


def is_loopback(host):
    try:
        return ipaddress.ip_address(str(host).split('%')[0]).is_loopback
    except ValueError:
        return False


def local_principal(*, client_host=None):
    if auth_mode() != 'local':
        raise AuthenticationError('企业模式禁止本机身份回退')
    if client_host is not None and not is_loopback(client_host):
        raise AuthenticationError('本机演示身份仅允许回环地址访问；网络部署须配置OIDC')
    name = getpass.getuser()
    return Principal('local:' + name, name, tuple(PERMISSIONS), ('*',), ('*',),
                     configured_tenant(), 'local_os_demo')


def require(principal, action, *, factory=None, product=None):
    if not isinstance(principal, Principal):
        raise AuthenticationError('必须提供已认证身份')
    if principal.tenant_id != configured_tenant():
        raise AuthorizationError('该组织不在此独立部署的授权范围')
    if not any(action in PERMISSIONS.get(role, ()) for role in principal.roles):
        raise AuthorizationError('当前角色无权执行此操作')
    for requested, allowed, label in ((factory, principal.factories, '工厂'),
                                      (product, principal.products, '产品')):
        if requested is not None and '*' not in allowed and requested not in allowed:
            raise AuthorizationError(f'无权访问该{label}')
    return principal


def can(principal, action, *, factory=None, product=None):
    try:
        require(principal, action, factory=factory, product=product)
        return True
    except PermissionError:
        return False


def filter_tables(principal, tables, *, action='data.read'):
    require(principal, action)
    result = {}
    for key, frame in tables.items():
        selected = frame.copy()
        for column, allowed in (('工厂', principal.factories), ('产品名称', principal.products)):
            if not selected.empty and column in selected and '*' not in allowed:
                selected = selected.loc[selected[column].isin(allowed)].copy()
        result[key] = selected
    return result


def _authorization_entries():
    value = os.environ.get('COST_AUTHORIZATION_FILE', '')
    if not value:
        raise AuthenticationError('未配置服务端授权映射COST_AUTHORIZATION_FILE')
    try:
        data = json.loads(Path(value).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise AuthenticationError('服务端授权配置不可读取') from None
    if not isinstance(data, dict) or not isinstance(data.get('users'), dict):
        raise AuthenticationError('服务端授权配置格式错误')
    return data['users']


def principal_from_claims(claims):
    """Only use after cryptographic verification by JWT middleware or st.user."""
    issuer = os.environ.get('COST_OIDC_ISSUER', '').rstrip('/')
    if not issuer or str(claims.get('iss', '')).rstrip('/') != issuer:
        raise AuthenticationError('身份签发者不匹配')
    subject = str(claims.get('sub', ''))
    if not subject:
        raise AuthenticationError('身份缺少subject')
    # Roles/scopes from the server file only, never arbitrary token/request fields.
    entry = _authorization_entries().get(subject)
    if not isinstance(entry, dict) or not entry.get('enabled', True):
        raise AuthorizationError('此账户未授权或已停用')
    if 'enabled' in entry and not isinstance(entry['enabled'], bool):
        raise AuthorizationError('账户enabled配置须为布尔值')
    for key in ('roles', 'factories', 'products'):
        values = entry.get(key, [])
        if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
            raise AuthorizationError('账户角色/范围配置须为非空字符串数组')
    roles = tuple(entry.get('roles', ()))
    if any(role not in PERMISSIONS for role in roles):
        raise AuthorizationError('账户角色配置无效')
    return Principal(issuer + '|' + subject, str(entry.get('display_name') or claims.get('name') or subject),
                     roles, tuple(entry.get('factories', ())), tuple(entry.get('products', ())),
                     str(entry.get('tenant_id', configured_tenant())), 'oidc')


@lru_cache(maxsize=4)
def _jwk_client(url):
    import jwt
    return jwt.PyJWKClient(url, cache_keys=True, lifespan=300, timeout=5)


def verify_bearer(token):
    import jwt
    issuer = os.environ.get('COST_OIDC_ISSUER', '').rstrip('/')
    audience = os.environ.get('COST_OIDC_AUDIENCE', '')
    jwks_url = os.environ.get('COST_OIDC_JWKS_URL', '')
    if not issuer or not audience or not jwks_url:
        raise AuthenticationError('OIDC配置不完整')
    if not jwks_url.startswith('https://'):
        raise AuthenticationError('JWKS必须使用HTTPS')
    try:
        key = _jwk_client(jwks_url).get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=['RS256', 'ES256'], audience=audience,
                            issuer=issuer, options={'require': ['exp', 'iat', 'sub', 'iss', 'aud']})
    except (jwt.PyJWTError, ValueError, OSError):
        raise AuthenticationError('登录凭据无效或已过期') from None
    return principal_from_claims(claims)


def api_principal(request):
    if auth_mode() == 'local':
        # Forwarded headers are not trusted as identity or origin evidence.
        if request.headers.get('x-forwarded-for') or request.headers.get('forwarded'):
            raise AuthenticationError('本机模式不接受代理转发，请配置OIDC')
        from urllib.parse import urlsplit
        target = request.url.hostname
        if target != 'localhost' and not is_loopback(target):
            raise AuthenticationError('本机模式只接受本机Host，拒绝DNS重绑定访问')
        origin = request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            configured = [x.strip() for x in os.environ.get('COST_CORS_ORIGINS', '').split(',') if x.strip()]
            if origin not in configured or urlsplit(origin).hostname not in ('localhost', '127.0.0.1', '::1'):
                raise AuthenticationError('本机API拒绝跨站请求')
        return local_principal(client_host=request.client.host if request.client else '')
    header = request.headers.get('authorization', '')
    if not header.startswith('Bearer '):
        raise AuthenticationError('请先登录并提供Bearer凭据')
    return verify_bearer(header[7:].strip())


def resolve_principal(user_id):
    """Resolve a stored audit subject against today's authorization, for dispatch."""
    if auth_mode() == 'local':
        principal = local_principal()
        if principal.user_id != user_id:
            raise AuthorizationError('原签发人不再是当前本机授权身份')
        return principal
    issuer = os.environ.get('COST_OIDC_ISSUER', '').rstrip('/')
    prefix = issuer + '|'
    if not user_id.startswith(prefix):
        raise AuthenticationError('持久用户ID不属于当前签发者')
    return principal_from_claims({'iss': issuer, 'sub': user_id[len(prefix):]})


def worker_principal():
    if auth_mode() == 'local':
        principal = local_principal()
    else:
        subject = os.environ.get('COST_WORKER_SUBJECT', '').strip()
        if not subject:
            raise AuthenticationError('生产worker必须配置独立COST_WORKER_SUBJECT')
        principal = principal_from_claims({'iss': os.environ.get('COST_OIDC_ISSUER', ''), 'sub': subject})
    require(principal, 'task.send')
    return principal


def streamlit_principal():
    """Re-evaluate the server authorization map on every rerun, including revocation."""
    import streamlit as st
    if auth_mode() == 'local':
        # Require both the bind setting and the client context to remain local.
        bind = str(st.get_option('server.address') or '')
        remote = getattr(st.context, 'ip_address', None)
        if bind not in ('localhost', '127.0.0.1', '::1'):
            st.error('本机模式必须绑定127.0.0.1；网络部署请配置OIDC身份认证。')
            st.stop()
        if remote and not is_loopback(remote):
            st.error('本机模式拒绝非本机访问。')
            st.stop()
        return local_principal()
    if not st.user.is_logged_in:
        st.title('制药成本分析工作台')
        st.info('请使用企业账户登录。')
        if st.button('企业账户登录', type='primary'):
            st.login()
        st.stop()
    claims = st.user.to_dict()
    # Streamlit identity cookies may outlive IdP token expiry; explicitly expire.
    if not claims.get('exp') or float(claims['exp']) <= time.time():
        st.warning('登录已过期，请重新登录。')
        if st.button('重新登录'):
            st.logout()
        st.stop()
    try:
        return principal_from_claims(claims)
    except PermissionError as exc:
        st.error(str(exc))
        if st.button('退出登录'):
            st.logout()
        st.stop()
