"""Bounded matching vocabulary projected from a validated server domain profile.

This is NOT an upload format, document scope, a source of graph edges, or an
executable rule language. Callers select the profile on the server, review the
projection in the normal knowledge preview/confirmation flow, then publish it.
No current/global profile is consulted while loading or querying a release.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata

VOCABULARY_SCHEMA = 'graph-vocabulary/1'
ENTITY_TYPES = ('product', 'material', 'process', 'equipment', 'metric')
_FIELDS = frozenset({'schema_version', 'domain_id', 'source_config_fingerprint',
                     'entities', 'process_metric_bindings'})
MAX_ENTITIES = 2000
MAX_ALIASES = 16
MAX_TEXT = 160
MAX_BYTES = 512_000


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _text(value, label, limit=MAX_TEXT):
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or len(value) > limit or '*' in value or any(unicodedata.category(c).startswith('C') for c in value)):
        raise ValueError(label + '须为有界非空文本，不允许控制字符或通配符')
    if re.search(r'(?i)([a-z][a-z0-9+.-]{1,31}://|javascript:|data:|<\|[^>]+\|>|\[/?INST\]|__import__|<script|</?(?:system|assistant|developer)>)', value):
        raise ValueError(label + '不允许URL、代码或角色标记')
    return value


def _key(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value))


def validate_graph_vocabulary(value):
    """Return a canonical deep copy; reject unknown fields, IDs and code rules."""
    if type(value) is not dict or set(value) != _FIELDS or value.get('schema_version') != VOCABULARY_SCHEMA:
        raise ValueError('graph_vocabulary schema或字段无效')
    domain = _text(value['domain_id'], 'domain_id', 64)
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', domain):
        raise ValueError('graph_vocabulary domain_id无效')
    fingerprint = value['source_config_fingerprint']
    if not isinstance(fingerprint, str) or not re.fullmatch(r'[0-9a-f]{64}', fingerprint):
        raise ValueError('graph_vocabulary source_config_fingerprint须为SHA256')
    raw_entities = value['entities']
    if type(raw_entities) is not dict or set(raw_entities) != set(ENTITY_TYPES):
        raise ValueError('graph_vocabulary仅允许五种实体类型')
    entities, count = {}, 0
    for kind in ENTITY_TYPES:
        definitions = raw_entities[kind]
        if type(definitions) is not dict:
            raise ValueError('graph_vocabulary实体须为名称到别名列表')
        count += len(definitions)
        if count > MAX_ENTITIES:
            raise ValueError('graph_vocabulary实体数量超限')
        names, alias_owners = {}, {}
        for name, aliases in sorted(definitions.items()):
            _text(name, '实体名称')
            if not isinstance(aliases, (list, tuple)) or not 1 <= len(aliases) <= MAX_ALIASES:
                raise ValueError('graph_vocabulary别名须为有界非空列表')
            clean = sorted(set([name, *[_text(alias, '实体别名') for alias in aliases]]))
            if len(clean) > MAX_ALIASES:
                raise ValueError('graph_vocabulary别名数量超限')
            for alias in clean:
                key = _key(alias)
                if key in alias_owners and alias_owners[key] != name:
                    raise ValueError('graph_vocabulary同类型别名有歧义')
                alias_owners[key] = name
            names[name] = clean
        entities[kind] = names
    raw_bindings = value['process_metric_bindings']
    if not isinstance(raw_bindings, (list, tuple)) or len(raw_bindings) > MAX_ENTITIES:
        raise ValueError('graph_vocabulary指标绑定须为有界列表')
    bindings = set()
    for binding in raw_bindings:
        if type(binding) is not dict or set(binding) != {'process', 'metric'}:
            raise ValueError('graph_vocabulary指标绑定仅允许process/metric名称')
        process = _text(binding['process'], '绑定工序')
        metric = _text(binding['metric'], '绑定指标')
        if process not in entities['process'] or metric not in entities['metric']:
            raise ValueError('graph_vocabulary指标绑定引用未知实体')
        bindings.add((process, metric))
    result = {'schema_version': VOCABULARY_SCHEMA, 'domain_id': domain,
              'source_config_fingerprint': fingerprint, 'entities': entities,
              'process_metric_bindings': [{'process': p, 'metric': m} for p, m in sorted(bindings)]}
    if len(_json(result).encode('utf-8')) > MAX_BYTES:
        raise ValueError('graph_vocabulary大小超限')
    return result


def vocabulary_fingerprint(value):
    return hashlib.sha256(_json(validate_graph_vocabulary(value)).encode('utf-8')).hexdigest()


def build_graph_vocabulary(profile):
    """Project a server-selected manufacturing-domain/2 after full validation.

    IDs are only used to resolve explicit references here; the graph receives
    names/aliases and process-metric bindings, never BOM edges, grants or code.
    Bindings disambiguate matching; a source predicate/value is still mandatory.
    """
    from enterprise.domain_profiles import validate_domain_profile, profile_fingerprint
    profile = validate_domain_profile(profile)
    if profile.get('schema_version') != 'manufacturing-domain/2':
        raise ValueError('图词表投影需要已验证manufacturing-domain/2')
    entities = {kind: {} for kind in ENTITY_TYPES}
    fields = {'product': 'products', 'material': 'materials', 'process': 'processes',
              'equipment': 'equipment', 'metric': 'reference_metrics'}
    ids = {kind: {} for kind in ENTITY_TYPES}
    for kind, field in fields.items():
        for definition in profile[field]:
            name = definition['source_name'] if kind == 'metric' else definition['name']
            ids[kind][definition['id']] = name
            aliases = definition.get('aliases', ())
            if not isinstance(aliases, (list, tuple)):
                raise ValueError('行业配置实体别名须为列表')
            entities[kind][name] = sorted(set([*entities[kind].get(name, ()), name, *aliases]))
    bindings = set()
    for process in profile['processes']:
        for metric_id in process.get('metric_ids', ()):
            bindings.add((ids['process'][process['id']], ids['metric'][metric_id]))
    for metric in profile['reference_metrics']:
        for process_id in metric.get('process_ids', ()):
            bindings.add((ids['process'][process_id], ids['metric'][metric['id']]))
    return validate_graph_vocabulary({'schema_version': VOCABULARY_SCHEMA,
        'domain_id': profile['id'], 'source_config_fingerprint': profile_fingerprint(profile),
        'entities': entities, 'process_metric_bindings': [
            {'process': process, 'metric': metric} for process, metric in sorted(bindings)]})


def vocabulary_from_metadata(meta):
    """Only the reviewed business-metadata namespace carries this configuration."""
    business = meta.get('business_metadata') or {}
    if 'graph_vocabulary' not in business:
        return None
    return validate_graph_vocabulary(business['graph_vocabulary'])


def explicit_other_domain(meta):
    """An explicit non-pharma domain must never silently inherit medicine names."""
    business = meta.get('business_metadata') or {}
    labels = [business.get(key, meta.get(key)) for key in ('domain_id', 'domain_profile_id', 'industry')]
    return any(isinstance(label, str) and label and label not in {
        'pharma', 'pharmaceutical', '中药制造', '中药', '制药', '医药制造'} for label in labels)
