"""Versioned, declarative manufacturing semantics; never executable configuration.

The profile is selected by the server operator, not a question or browser value.
Unknown products/specifications deliberately have no category or unit conversion.
It supplies reference semantics, not source observations or authorization grants.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re

from paths import BASE_DIR

SCHEMA = 'manufacturing-domain/1'
CANONICAL_SCHEMA = 'manufacturing-domain/2'
KNOWLEDGE_TYPES = frozenset({'formula', 'process', 'equipment', 'regulation',
    'regulation_summary', 'industry_benchmark', 'market_prices', 'cost_baseline', 'other'})
CALCULATIONS = frozenset({'element_share', 'unit_conversion', 'source_only'})
DIRECTIONS = frozenset({'context_only', 'lower_cost', 'higher'})
_FIELDS = frozenset({'schema_version', 'id', 'version', 'label', 'source_dataset',
    'reporting_unit', 'currency', 'factories', 'products', 'industry_category',
    'knowledge_types', 'reference_metrics', 'alerts', 'limitations'})


class DomainProfileError(ValueError):
    pass


def _text(value, label, limit=160):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise DomainProfileError(label+'须为非空受控文本')
    return value


def _strings(value, label, *, empty=False):
    if not isinstance(value, list) or (not empty and not value) or len(value) > 500:
        raise DomainProfileError(label+'须为有界文本列表')
    for item in value:
        _text(item, label, 500)
    if len(set(value)) != len(value):
        raise DomainProfileError(label+'存在重复项')
    return value


def validate_domain_profile(value):
    if type(value) is dict and value.get('schema_version') == CANONICAL_SCHEMA:
        return validate_canonical_domain_profile(value)
    if not isinstance(value, dict) or set(value)-_FIELDS or value.get('schema_version') != SCHEMA:
        raise DomainProfileError('行业配置schema或字段无效')
    required = {'id','version','label','reporting_unit','currency','factories','products',
                'industry_category','knowledge_types','reference_metrics','alerts','limitations'}
    if not required <= set(value):
        raise DomainProfileError('行业配置缺少必需字段')
    result = deepcopy(value)
    for field in ('id','version','label','reporting_unit','currency','industry_category'):
        _text(result[field],field)
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',result['id']):
        raise DomainProfileError('行业配置ID须为稳定标识')
    if result['currency'] != 'CNY':
        raise DomainProfileError('当前会计适配器只支持CNY；更换货币须增加明确换算适配器')
    factories = result['factories']
    if not isinstance(factories,dict) or set(factories) != {'home','peer'}:
        raise DomainProfileError('须分别配置home/peer工厂，不由界面选择授权身份')
    for field in factories:
        _text(factories[field],'工厂')
    if factories['home'] == factories['peer']:
        raise DomainProfileError('两工厂身份不得相同')
    _strings(result['knowledge_types'],'知识类型')
    if set(result['knowledge_types'])-KNOWLEDGE_TYPES:
        raise DomainProfileError('不支持的知识类型')
    products = result['products']
    if not isinstance(products,list) or not products or len(products) > 10000:
        raise DomainProfileError('须提供有界产品定义')
    identities=set()
    for product in products:
        if not isinstance(product,dict) or set(product) != {'name','specification','category','base_unit','units_per_box','materials'}:
            raise DomainProfileError('产品定义字段无效')
        for field in ('name','specification','category','base_unit'):
            _text(product[field],'产品/'+field)
        key=(product['name'],product['specification'])
        if key in identities:
            raise DomainProfileError('产品规格配置重复')
        identities.add(key)
        raw=product['units_per_box']
        if isinstance(raw,bool) or not isinstance(raw,(str,int)):
            raise DomainProfileError('包装换算须为精确十进制字符串或整数')
        try:
            count=Decimal(str(raw))
        except InvalidOperation:
            raise DomainProfileError('包装换算无效') from None
        if not count.is_finite() or count<=0 or count>Decimal('1000000000'):
            raise DomainProfileError('包装换算须为正有限值')
        product['units_per_box']=str(count)
        _strings(product['materials'],'产品材料',empty=True)
    metrics=result['reference_metrics']
    if not isinstance(metrics,list) or not metrics or len(metrics)>1000:
        raise DomainProfileError('指标定义须为有界非空列表')
    names=set();ids=set()
    for metric in metrics:
        if not isinstance(metric,dict) or set(metric)-{'source_name','id','unit','direction','calculation','element','base_unit'}:
            raise DomainProfileError('指标字段无效')
        for field in ('source_name','id','unit','direction','calculation'):
            _text(metric.get(field),'指标/'+field)
        if metric['source_name'] in names or metric['id'] in ids:
            raise DomainProfileError('指标名称或ID重复')
        names.add(metric['source_name']);ids.add(metric['id'])
        if metric['calculation'] not in CALCULATIONS or metric['direction'] not in DIRECTIONS:
            raise DomainProfileError('不允许执行自定义表达式或未知指标方向')
        if metric['calculation']=='element_share' and (metric.get('element') not in {'材料','人工','制费'} or metric['unit']!='%'):
            raise DomainProfileError('构成指标须明确材料、人工或制费，单位为%')
        if metric['calculation']=='unit_conversion':
            _text(metric.get('base_unit'),'换算基础单位')
            if metric['unit']!='元/'+metric['base_unit']:
                raise DomainProfileError('金额单位与包装基础单位不一致')
    alerts=result['alerts']
    if not isinstance(alerts,list) or len(alerts)>100:
        raise DomainProfileError('预警规则须为有界列表')
    alert_ids=set()
    for alert in alerts:
        if not isinstance(alert,dict) or set(alert)!={'id','metric_id','rule','action','requires_period_confirmation'}:
            raise DomainProfileError('预警规则字段无效')
        if alert.get('metric_id') not in ids or alert.get('id') in alert_ids:
            raise DomainProfileError('预警规则引用未知指标或ID重复')
        _text(alert['id'],'预警ID');alert_ids.add(alert['id'])
        if (alert.get('rule')!='home_above_p75_while_p50_negative' or alert.get('action')!='verification_draft_only'
                or alert.get('requires_period_confirmation') is not True):
            raise DomainProfileError('行业预警只允许待复核草稿，不允许自动审批或派发')
    _strings(result['limitations'],'配置边界',empty=True)
    return result


def _unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:
            raise DomainProfileError('行业配置包含重复JSON字段')
        result[key]=value
    return result


def parse_domain_profile(text):
    """Parse bounded operator-owned JSON without duplicate keys or NaN constants."""
    if type(text) is not str or len(text) > 1_000_000:
        raise DomainProfileError('行业配置JSON须为有界文本')
    def reject_constant(_):
        raise DomainProfileError('行业配置不允许非有限JSON常量')
    try:
        value = json.loads(text, object_pairs_hook=_unique_object, parse_constant=reject_constant)
        return validate_domain_profile(value)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, DomainProfileError):
            raise
        raise DomainProfileError('行业配置JSON无效') from None


def load_domain_profile(path=None):
    selected=Path(path) if path is not None else Path(os.environ.get('COST_DOMAIN_PROFILE') or BASE_DIR/'config/domain_profiles/pharma.json')
    try:
        if selected.stat().st_size>1_000_000:
            raise DomainProfileError('行业配置过大')
        raw=selected.read_text(encoding='utf-8-sig')
        return parse_domain_profile(raw)
    except (OSError,json.JSONDecodeError) as exc:
        raise DomainProfileError('行业配置不可读取：'+type(exc).__name__) from None


def profile_fingerprint(profile=None):
    value=load_domain_profile() if profile is None else validate_domain_profile(profile)
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()


def product_definition(product, specification, profile=None):
    value=load_domain_profile() if profile is None else validate_domain_profile(profile)
    return next((deepcopy(row) for row in value['products']
                 if row['name']==product and row['specification']==specification),None)


def metric_definition(source_name, profile=None):
    value=load_domain_profile() if profile is None else validate_domain_profile(profile)
    return next((deepcopy(row) for row in value['reference_metrics'] if row['source_name']==source_name),None)


_CANONICAL_FIELDS = {'schema_version', 'id', 'version', 'industry', 'label', 'status',
    'data_classification', 'source_dataset', 'reporting_unit', 'currency', 'factories',
    'labels', 'products', 'materials', 'processes', 'equipment', 'industry_category',
    'knowledge_types', 'reference_metrics', 'limitations'}


def validate_canonical_domain_profile(value):
    """Closed, declarative domain/2: reference semantics, never observations/grants.

    No expressions, loaders, paths, URLs, roles, dispatch policy or model settings
    are accepted. BOM quantities are reference quantities, NEVER actual usage.
    Unit packages are distinct identity units; domain/1 units_per_box is not used.
    """
    from enterprise.manufacturing_adapter import (
        INDUSTRIES, ManufacturingAdapterError, _object, _list, _text as exact_text,
        _decimal, _unit, _currency,
    )
    def text(v, name, limit=160):
        exact_text(v, name, limit)
        if re.search(r'(?i)([a-z][a-z0-9+.-]{1,31}://|javascript:|data:|<\|[^>]+\|>|\[/?INST\]|__import__|<script|</?(?:system|assistant|developer)>)', v):
            raise DomainProfileError(name + ': executable/URL/role markup is not domain vocabulary')
        return v
    def ident(v, name):
        text(v, name)
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', v):
            raise DomainProfileError(name + ': expected stable identifier')
        return v
    def refs(v, name, allowed, *, empty=True):
        _list(v, name, empty=empty)
        if any(type(x) is not str or x not in allowed for x in v) or len(set(v)) != len(v):
            raise DomainProfileError(name + ': unknown or duplicate reference')
    try:
        _object(value, _CANONICAL_FIELDS, 'canonical domain')
        result = deepcopy(value)
        if result['schema_version'] != CANONICAL_SCHEMA or result['status'] != 'active':
            raise DomainProfileError('canonical domain requires active domain/2')
        if result['industry'] not in INDUSTRIES | {'pharma'}:
            raise DomainProfileError('unsupported canonical industry')
        if result['data_classification'] not in {'simulation', 'operator_supplied'}:
            raise DomainProfileError('explicit data_classification required')
        ident(result['id'], 'id')
        for key in ('version', 'label', 'source_dataset', 'industry_category'):
            text(result[key], key, 500)
        _unit(result['reporting_unit'], 'reporting_unit', canonical=True)
        _currency(result['currency'], 'currency')
        _object(result['factories'], {'home', 'peer'}, 'factories')
        for name in result['factories'].values():
            text(name, 'factory')
        if result['factories']['home'] == result['factories']['peer']:
            raise DomainProfileError('home and peer identities must differ')
        _object(result['labels'], {'home', 'peer', 'material', 'labor', 'overhead', 'unitcost', 'output', 'total'}, 'labels')
        for name in result['labels'].values():
            text(name, 'label')
        refs(result['knowledge_types'], 'knowledge_types', KNOWLEDGE_TYPES, empty=False)
        for note in _list(result['limitations'], 'limitations', maximum=30):
            text(note, 'limitation', 1000)
        registries = {}
        fields = {
            'materials': {'id', 'name', 'unit'},
            'products': {'id', 'name', 'specification', 'category', 'reporting_unit', 'materials', 'bom', 'process_ids', 'equipment_ids'},
            'processes': {'id', 'name', 'product_ids', 'metric_ids'},
            'equipment': {'id', 'name', 'process_ids'},
            'reference_metrics': {'id', 'source_name', 'unit', 'direction', 'calculation', 'element', 'process_ids'},
        }
        for kind, keys in fields.items():
            registry = {}
            for item in _list(result[kind], kind, maximum=1000):
                _object(item, keys, kind)
                key = ident(item['id'], kind + '.id')
                if key in registry:
                    raise DomainProfileError(kind + ': duplicate ID')
                text(item.get('name', item.get('source_name')), kind + '.name')
                registry[key] = item
            registries[kind] = registry
        materials = registries['materials']
        for row in materials.values():
            _unit(row['unit'], 'material.unit', canonical=True)
        identities = set()
        for product in result['products']:
            for key in ('specification', 'category'):
                text(product[key], 'product.' + key)
            key = (product['name'], product['specification'])
            if key in identities:
                raise DomainProfileError('duplicate product/specification')
            identities.add(key)
            if product['reporting_unit'] != result['reporting_unit']:
                raise DomainProfileError('mixed reporting units need separate explicitly scoped domain profiles')
            _list(product['bom'], 'product.bom')
            material_ids = set()
            names = []
            for row in product['bom']:
                _object(row, {'material_id', 'quantity_per_reporting_unit', 'unit'}, 'bom')
                if row['material_id'] not in materials or row['material_id'] in material_ids:
                    raise DomainProfileError('BOM references unknown or duplicate material')
                material_ids.add(row['material_id'])
                material = materials[row['material_id']]
                names.append(material['name'])
                if row['unit'] != material['unit']:
                    raise DomainProfileError('BOM unit differs from material unit; no inferred conversion')
                row['quantity_per_reporting_unit'] = str(_decimal(row['quantity_per_reporting_unit'], 'reference BOM quantity', positive=True))
            refs(product['materials'], 'product.materials', names, empty=False)
            if set(product['materials']) != set(names):
                raise DomainProfileError('product material vocabulary must match explicit BOM')
            refs(product['process_ids'], 'product.process_ids', registries['processes'], empty=False)
            refs(product['equipment_ids'], 'product.equipment_ids', registries['equipment'], empty=False)
        source_names = set()
        for row in result['reference_metrics']:
            if row['source_name'] in source_names:
                raise DomainProfileError('duplicate metric source_name')
            source_names.add(row['source_name'])
            text(row['unit'], 'metric.unit')
            if row['direction'] not in DIRECTIONS or row['calculation'] not in {'element_share', 'unit_cost', 'source_only'}:
                raise DomainProfileError('unsupported metric direction/calculation; expressions prohibited')
            if row['element'] not in {'material', 'labor', 'overhead', 'total', 'none'}:
                raise DomainProfileError('unknown metric element')
            if row['calculation'] == 'element_share' and (row['unit'] != '%' or row['element'] not in {'material', 'labor', 'overhead'}):
                raise DomainProfileError('element_share requires a component and percent unit')
            if row['calculation'] == 'unit_cost' and (row['unit'] != result['currency'] + '/' + result['reporting_unit'] or row['element'] != 'total'):
                raise DomainProfileError('unit_cost metric must use explicit reporting currency/unit')
            refs(row['process_ids'], 'metric.process_ids', registries['processes'])
        for row in result['processes']:
            refs(row['product_ids'], 'process.product_ids', registries['products'], empty=False)
            refs(row['metric_ids'], 'process.metric_ids', registries['reference_metrics'], empty=False)
            for product_id in row['product_ids']:
                if row['id'] not in registries['products'][product_id]['process_ids']:
                    raise DomainProfileError('inconsistent product/process binding')
            for metric_id in row['metric_ids']:
                if row['id'] not in registries['reference_metrics'][metric_id]['process_ids']:
                    raise DomainProfileError('inconsistent process/metric binding')
        for product in result['products']:
            for process_id in product['process_ids']:
                if product['id'] not in registries['processes'][process_id]['product_ids']:
                    raise DomainProfileError('inconsistent process/product binding')
            for equipment_id in product['equipment_ids']:
                if not set(registries['equipment'][equipment_id]['process_ids']) & set(product['process_ids']):
                    raise DomainProfileError('equipment has no process for product')
        for row in result['equipment']:
            refs(row['process_ids'], 'equipment.process_ids', registries['processes'], empty=False)
        for metric in result['reference_metrics']:
            for process_id in metric['process_ids']:
                if metric['id'] not in registries['processes'][process_id]['metric_ids']:
                    raise DomainProfileError('inconsistent metric/process binding')
        return result
    except (ManufacturingAdapterError, TypeError, KeyError, ValueError, RecursionError) as exc:
        if isinstance(exc, DomainProfileError):
            raise
        raise DomainProfileError(str(exc)) from None

