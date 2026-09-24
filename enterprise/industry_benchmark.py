"""Deterministic industry reference comparisons over already authorized evidence.

No file/network access, no task writes, and no inference of an actual procurement
price, yield, peer detail, or saving. A category benchmark is a reference anchor,
not a same-product business observation. The source's home-factory column is kept
separate from calculations for the selected product and month.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction
import hashlib
import re

from enterprise.domain_profiles import load_domain_profile, metric_definition, product_definition, profile_fingerprint
from enterprise.numeric import format_number

ELEMENTS = {'材料':'直接材料(元/盒)', '人工':'直接人工(元/盒)', '制费':'制造费用(元/盒)'}
SUMMARY_TABLES = ('cost25','cost26','erchang25','erchang26')


class ReferenceError(ValueError):
    pass


def _decimal(raw, *, percent=False, nonnegative=False):
    if isinstance(raw,bool) or not isinstance(raw,(str,int,float,Decimal)):
        raise ReferenceError('基准数值无效')
    text=str(raw).strip()
    if percent:
        if not text.endswith('%'):
            raise ReferenceError('百分比基准必须显式包含%')
        text=text[:-1]
    elif '%' in text:
        raise ReferenceError('金额指标不能使用百分比')
    try:
        value=Decimal(text)
    except InvalidOperation:
        raise ReferenceError('基准数值不是十进制数') from None
    if not value.is_finite() or abs(value)>Decimal('1e20') or (nonnegative and value<0):
        raise ReferenceError('基准数值非有限或超出范围')
    return value


def _values(value):
    return {'value':float(value), 'exact':str(value)} if value is not None else {'value':None,'exact':None}


def _position(value, p25, p50, p75):
    if value is None:
        return 'not_available'
    if value < p25: return 'below_p25'
    if value == p25: return 'at_p25'
    if value < p50: return 'between_p25_p50'
    if value == p50: return 'at_p50'
    if value < p75: return 'between_p50_p75'
    if value == p75: return 'at_p75'
    return 'above_p75'


POSITION_LABELS={'not_available':'缺少可比值','below_p25':'低于P25','at_p25':'等于P25',
    'between_p25_p50':'介于P25与P50','at_p50':'等于P50','between_p50_p75':'介于P50与P75',
    'at_p75':'等于P75','above_p75':'高于P75'}
POSITION_LEVEL={'not_available':'无法判断','below_p25':'低','at_p25':'低','between_p25_p50':'中低',
    'at_p50':'中','between_p50_p75':'中高','at_p75':'高','above_p75':'高'}


def _observation(tables,factory,product,specification,month):
    matches=[]
    for key in SUMMARY_TABLES:
        frame=tables.get(key)
        if frame is None or frame.empty: continue
        identity={'工厂','产品名称','产品规格','月份'}
        if not identity<=set(frame.columns):
            continue
        rows=frame[(frame['工厂']==factory)&(frame['产品名称']==product)&
                   (frame['产品规格']==specification)&(frame['月份']==month)]
        matches.extend((key,row) for _,row in rows.iterrows())
    if not matches:
        return {'available':False,'reason':'同产品、规格、月份汇总记录未提供'}
    if len(matches)!=1:
        return {'available':False,'reason':'同口径汇总重复，未取任一行代替'}
    table,row=matches[0]
    try:
        unit=_decimal(row.get('单位成本(元/盒)'),nonnegative=True)
        elements={name:_decimal(row.get(field),nonnegative=True) for name,field in ELEMENTS.items()}
        if sum(elements.values())!=unit:
            raise ReferenceError('三要素与单位成本不闭合')
        volume=_decimal(row.get('产量(盒)'),nonnegative=True)
        total=_decimal(row.get('总成本(元)'),nonnegative=True)
        if abs(unit*volume-total)>Decimal('.01'):
            raise ReferenceError('汇总总成本与产量×单位成本不闭合')
    except ReferenceError as exc:
        return {'available':False,'reason':str(exc)}
    from enterprise.benchmark import _source
    return {'available':True,'unit':unit,'elements':elements,'source':_source(table,row)}


def _observed_value(observation,metric,definition):
    if not observation['available']:
        return None,observation['reason']
    if metric['calculation']=='element_share':
        if observation['unit']==0: return None,'单位总成本为零，占比不定义'
        return observation['elements'][metric['element']]/observation['unit']*100,None
    if metric['calculation']=='unit_conversion':
        if metric['base_unit']!=definition['base_unit']:
            return None,'基准单位与产品包装基础单位不同'
        return observation['unit']/Decimal(definition['units_per_box']),None
    return None,'行业文件企业指标的统计范围未给出，不用单产品月度值替代'


def _eligible_rows(evidence,product,specification,month,profile):
    """Recheck source scope and immutable tabular semantics at use time."""
    from enterprise.evidence_references import reference_source_reason
    diagnostics=[];rows=[];identities={}
    for source in evidence:
        if not isinstance(source,dict): continue
        table=source.get('table_row') or {}
        if not isinstance(table,dict) or table.get('schema')!='industry_benchmark': continue
        reason=reference_source_reason(source,product,specification,month,profile=profile)
        if reason:
            diagnostics.append({'id':source.get('id'),'reason':reason});continue
        if source.get('kind')!='industry_reference' or source.get('support_status')!='eligible':
            diagnostics.append({'id':source.get('id'),'reason':'not_authorized_reference_evidence'});continue
        if not source.get('id') or not source.get('source',{}).get('sha256'):
            diagnostics.append({'id':source.get('id'),'reason':'missing_source_identity'});continue
        columns=table.get('columns') or {}
        key=(columns.get('产品类别'),columns.get('指标'),table.get('year'))
        identities.setdefault(key,[]).append(source)
    for key,items in identities.items():
        # The same retrieved chunk can repeat across queries, not across versions.
        unique={item['id']:item for item in items}
        if len(unique)>1:
            diagnostics.append({'key':list(key),'reason':'ambiguous_reference_rows'});continue
        rows.extend(unique.values())
    return rows,diagnostics


def _alert(row,profile,month):
    rule=next((item for item in profile['alerts'] if item['metric_id']==row['metric_id']),None)
    if not rule or row['category']!=profile['industry_category'] or row['unit']!='%': return None
    source=row['source_reported_home']['value'];p50=row['p50']['value'];p75=row['p75']['value']
    if source is None or not (Decimal(str(p50))<0 and Decimal(str(source))>Decimal(str(p75))): return None
    delta50=Decimal(row['source_reported_home']['exact'])-Decimal(row['p50']['exact'])
    delta75=Decimal(row['source_reported_home']['exact'])-Decimal(row['p75']['exact'])
    identity='|'.join([profile['id'],rule['id'],row['evidence_id'],str(row['reference_year'])])
    return {'alert_id':'industry-'+hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24],
        'rule_id':rule['id'],'status':'verification_candidate','priority':'高',
        'title':'行业成本同比基准偏离核查','source_level':'source_reported_company',
        'factory':profile['factories']['home'],'reference_year':row['reference_year'],
        'selected_month_is_not_source_period':True,'view_month':month,
        'finding':f"行业文件列示本厂成本同比{source:+g}%，行业P50为{p50:+g}%、P75为{p75:+g}%；分别高{format_number(delta50)}和{format_number(delta75)}个百分点。",
        'evidence_ids':[row['evidence_id']],
        'differences_pp':{'above_p50':_values(delta50),'above_p75':_values(delta75)},
        'action':'财务部先确认行业样本、统计期间和成本定义，再用本厂同口径台账复算；形成差异核对表，确认后提交独立审核。',
        'missing_evidence':['行业统计窗口和样本说明','本厂同口径同比计算底稿'],
        'boundary':'行业文件原值预警，不是当前选择产品/月度的已确认异常；P75不是行业最大值，不自动审批或派发。',
        'side_effects':{'task_created':False,'approved':False,'dispatched':False}}


def build_industry_comparison(product,specification,month,tables,evidence,*,profile=None):
    profile=load_domain_profile() if profile is None else profile
    fingerprint=profile_fingerprint(profile)
    definition=product_definition(product,specification,profile)
    result={'schema_version':'industry-comparison/1','available':False,'product':product,'specification':specification,
        'month':month,'profile_id':profile['id'],'profile_sha256':fingerprint,'rows':[],'alerts':[],
        'diagnostics':[],'radar':{'available':False,'axes':[],'series':[]},
        'boundary':'产品类别/年度基准仅作参照，不能替代同品同规格同期两厂直接对标；外部相似不证明原因相同。'}
    if not isinstance(month,str) or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',month):
        return {**result,'reason':'月份格式须为YYYY-MM'}
    if definition is None:
        return {**result,'reason':'未配置此产品与规格的类别及单位映射，未猜测换算'}
    references,diagnostics=_eligible_rows(evidence,product,specification,month,profile)
    result['diagnostics']=diagnostics
    observations={side:_observation(tables,factory,product,specification,month)
                  for side,factory in profile['factories'].items()}
    with localcontext() as ctx:
        ctx.prec=40
        for source in references:
            table=source['table_row'];columns=table['columns']
            metric=metric_definition(columns.get('指标'),profile)
            if metric is None:
                result['diagnostics'].append({'id':source['id'],'reason':'metric_not_configured'});continue
            try:
                percent=metric['unit']=='%'
                values={key:_decimal(columns.get('行业'+key.upper()),percent=percent) for key in ('p25','p50','p75')}
                if not values['p25']<=values['p50']<=values['p75']:
                    raise ReferenceError('分位值非单调')
                home_column='本厂水平('+profile['factories']['home']+')'
                reported=_decimal(columns.get(home_column),percent=percent)
                if metric['calculation']=='unit_conversion' and metric['base_unit']!=definition['base_unit']:
                    raise ReferenceError('基准基础单位不匹配')
            except ReferenceError as exc:
                result['diagnostics'].append({'id':source['id'],'reason':str(exc)});continue
            row={'metric_id':metric['id'],'metric':columns['指标'],'category':columns['产品类别'],
                'unit':metric['unit'],'direction':metric['direction'],'calculation':metric['calculation'],
                **{key:_values(value) for key,value in values.items()},'source_reported_home':_values(reported),
                'source_evaluation':columns.get('对标评价',''),'reference_year':table['year'],
                'evidence_id':source['id'],'source':deepcopy(source['source']),
                'reference_period':'文件年份；未提供统计窗口','comparability':'category_annual_reference_only',
                'source_evaluation_boundary':'原文件评价，仅供阅读；未独立验证效率、采购策略或经营原因'}
            for side,observation in observations.items():
                observed,reason=_observed_value(observation,metric,definition)
                position=_position(observed,**values)
                row[side]={**_values(observed),'position':position,'position_label':POSITION_LABELS[position],
                    'gap_from_p50':_values(observed-values['p50']) if observed is not None else _values(None),
                    'gap_unit':'百分点' if percent else metric['unit'],'reason':reason,
                    'source':deepcopy(observation.get('source')),'factory':profile['factories'][side],
                    'measurement_scope':'selected_product_specification_month'}
            if metric['calculation']=='element_share':
                row['interpretation']='占比只反映成本结构，高低不能单独认定效率优劣。'
            else:
                row['interpretation']='类别基准提供位置参照，产品组合、质量等级和期间口径仍需核对。'
            result['rows'].append(row)
            alert=_alert(row,profile,month)
            if alert: result['alerts'].append(alert)
    result['available']=bool(result['rows'])
    if not result['available']:
        result['reason']='当前知识发布未检索到通过范围、期间及结构校验的行业基准行'
        return result
    # All axes are explicitly normalised by their positive P50. No interpolation
    # of percentile ranks or synthetic business performance score is performed.
    axes=[row for row in result['rows'] if row['category']==definition['category']
          and row['calculation'] in {'element_share','unit_conversion'} and row['p50']['value']>0]
    if len(axes)>=3:
        result['radar']={'available':True,'normalization':'selected value / industry P50 ×100; not a percentile or score',
            'caption':'行业P50=100；越外侧表示该项数值越高，不表示更好或更差；不同计量单位先按各自P50归一。',
            'axes':[{'name':row['metric'],'unit':row['unit'],'metric_id':row['metric_id'],'evidence_id':row['evidence_id']} for row in axes],
            'series':[{'name':'行业P50','values':[100.0]*len(axes),'kind':'reference'}]}
        for side,factory in profile['factories'].items():
            if all(row[side]['value'] is not None for row in axes):
                result['radar']['series'].append({'name':factory,'kind':'observed',
                    'values':[float(Decimal(row[side]['exact'])/Decimal(row['p50']['exact'])*100) for row in axes]})
    result['sources']=[deepcopy(source) for source in references if any(row['evidence_id']==source['id'] for row in result['rows'])]
    return result


def _canonical_observation(facts, side, profile, product, specification, month):
    row = facts.get('current') if side == 'home' else (facts.get('benchmark') or {}).get('peer')
    if not isinstance(row, dict):
        return {'available': False, 'reason': 'missing_canonical_observation'}
    expected = {'factory': profile['factories'][side], 'product': product, 'specification': specification,
                'month': month, 'currency': profile['currency'], 'unit': profile['reporting_unit']}
    if any(row.get(key) != value for key, value in expected.items()):
        return {'available': False, 'reason': 'canonical_observation_scope_or_unit_mismatch'}
    source = row.get('source')
    if (not isinstance(source, dict) or not source.get('source_id')
            or not all(re.fullmatch(r'[0-9a-f]{64}', str(source.get(k, ''))) for k in
                       ('source_sha256', 'source_row_sha256', 'adapter_sha256'))
            or type(source.get('row_number')) is not int or source['row_number'] < 1):
        return {'available': False, 'reason': 'missing_canonical_source_identity'}
    try:
        # canonical facts already contain exact units, not estimates from a BOM or
        # rounded reference. No arbitrary unit conversion or magnitude scaling.
        for key in ('unitcost', 'output', 'total', 'material', 'labor', 'overhead'):
            if not isinstance(row.get(key), str):
                raise ReferenceError('canonical amounts require exact decimal strings')
        unit, output, total = (_decimal(row[k], nonnegative=True) for k in ('unitcost', 'output', 'total'))
        elements = {key: _decimal(row[key], nonnegative=True) for key in ('material', 'labor', 'overhead')}
        if sum((Fraction(v) for v in elements.values()), Fraction(0)) != Fraction(unit):
            raise ReferenceError('canonical component costs do not close')
        if Fraction(unit) * Fraction(output) != Fraction(total):
            raise ReferenceError('canonical total does not close exactly')
    except ReferenceError as exc:
        return {'available': False, 'reason': str(exc)}
    return {'available': True, 'unit': unit, 'elements': elements, 'source': deepcopy(source)}


def _rational_values(value):
    """Keep nonterminating ratios exact as rational operands, not rounded claims."""
    if value is None:
        return _values(None)
    value = Fraction(value)
    denominator = value.denominator
    twos = fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    with localcontext() as context:
        context.prec = max(80, len(str(abs(value.numerator))) + max(twos, fives) + 5)
        decimal = Decimal(value.numerator) / Decimal(value.denominator)
    if denominator == 1:
        return {'value': float(decimal), 'exact': str(decimal)}
    return {'value': float(decimal), 'value_decimal': str(decimal),
            'exact_ratio': {'numerator': str(value.numerator), 'denominator': str(value.denominator)},
            'rounding': 'decimal_display_only_exact_ratio_is_authoritative'}


def build_canonical_industry_comparison(canonicalfacts, evidence, profile):
    """Compare authorized canonical actuals to exact domain/2 long-form references.

    No I/O, file paths, ambient profile, old pharmacy frames, source home-factory
    column, guessed packaging conversion, or authorization decisions occur here.
    The caller must obtain both facts and references through authorized releases.
    """
    from enterprise.domain_profiles import CANONICAL_SCHEMA, validate_domain_profile
    from enterprise.evidence_references import reference_source_reason
    from enterprise.tabular_knowledge import is_generic_table
    result = {'schema_version': 'industry-comparison/2', 'available': False,
        'rows': [], 'alerts': [], 'diagnostics': [], 'sources': [],
        'radar': {'available': False, 'axes': [], 'series': []},
        'boundary': '外部类别基准与所选产品同期实际核算值分列；不证明业务原因、效率优劣或可节约金额。'}
    try:
        profile = validate_domain_profile(profile)
        if profile['schema_version'] != CANONICAL_SCHEMA:
            raise ReferenceError('canonical references require manufacturing-domain/2')
        fingerprint = profile_fingerprint(profile)
        if not isinstance(canonicalfacts, dict) or canonicalfacts.get('schema_version') != 'manufacturing-facts/1':
            raise ReferenceError('canonical facts required')
        if canonicalfacts.get('provenance', {}).get('profile_sha256') != fingerprint:
            raise ReferenceError('canonical_profile_hash_mismatch')
        scope = canonicalfacts.get('scope') or {}
        product, specification, month = (scope.get(key) for key in ('product', 'specification', 'month'))
        if not isinstance(month, str) or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', month) or month.startswith('0000'):
            raise ReferenceError('invalid_canonical_month')
        definition = product_definition(product, specification, profile)
        if definition is None or scope.get('product_id') != definition['id']:
            raise ReferenceError('unknown_product_specification')
        if (scope.get('industry') != profile['industry'] or
                any(scope.get(side + '_factory') != profile['factories'][side] for side in ('home', 'peer'))):
            raise ReferenceError('canonical_factory_or_industry_mismatch')
        expected_measurement = {'currency': profile['currency'], 'quantity_unit': definition['reporting_unit'],
                                'unit_cost_unit': profile['currency'] + '/' + definition['reporting_unit']}
        if canonicalfacts.get('measurement') != expected_measurement:
            raise ReferenceError('canonical_measurement_mismatch')
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        return {**result, 'reason': str(exc)}
    result.update(product=product, specification=specification, month=month, category=definition['category'],
                  product_category=definition['category'], profile_id=profile['id'], profile_sha256=fingerprint)
    observations = {side: _canonical_observation(canonicalfacts, side, profile, product, specification, month)
                    for side in ('home', 'peer')}
    groups = {}
    for source in evidence or []:
        if not isinstance(source, dict) or source.get('kind') != 'industry_reference':
            continue
        table = source.get('table_row')
        if not is_generic_table(table):
            result['diagnostics'].append({'id': source.get('id'), 'reason': 'canonical_reference_schema_required'})
            continue
        reason = reference_source_reason(source, product, specification, month, profile=profile)
        if reason:
            result['diagnostics'].append({'id': source.get('id'), 'reason': reason})
            continue
        columns = table['columns']
        # Any overlapping eligible rows of the same category/metric/unit are
        # ambiguous, even when their period windows or quoted publishers differ.
        identity = tuple(columns[k] for k in ('category', 'metric', 'unit'))
        groups.setdefault(identity, {}).setdefault(source['id'], source)
    for identity, sources in groups.items():
        if len(sources) != 1:
            result['diagnostics'].append({'key': list(identity), 'reason': 'ambiguous_reference_rows'})
            continue
        source = next(iter(sources.values()))
        columns = source['table_row']['columns']
        metric = metric_definition(columns['metric'], profile)
        percentiles = {key: _decimal(columns[key]) for key in ('p25', 'p50', 'p75')}
        row = {'metric_id': metric['id'], 'metric': columns['metric'], 'element': metric['element'],
            'category': columns['category'], 'month': month, 'unit': columns['unit'],
            'direction': metric['direction'], 'calculation': metric['calculation'],
            **{key: _values(value) for key, value in percentiles.items()},
            'source_reported_home': _values(None), 'source_evaluation': '',
            'evidence_id': source['id'], 'source': deepcopy(source['source']),
            'source_name': columns['source_name'],
            'reference_period': columns['period_start'] + '至' + columns['period_end'],
            'period_start': columns['period_start'], 'period_end': columns['period_end'],
            'comparability': 'explicit_category_period_unit_reference_only',
            'interpretation': ('占比只反映成本结构，高低不能单独认定效率优劣。'
                               if metric['calculation'] == 'element_share' else
                               '类别基准提供位置参照，产品组合、质量等级和统计期间仍须核对。')}
        for side, observation in observations.items():
            value, reason = None, observation.get('reason')
            if observation['available']:
                if metric['calculation'] == 'element_share':
                    if observation['unit'] == 0:
                        reason = 'zero_total_cost_share_undefined'
                    else:
                        value = Fraction(observation['elements'][metric['element']]) / Fraction(observation['unit']) * 100
                elif metric['calculation'] == 'unit_cost':
                    value = Fraction(observation['unit'])
                else:
                    reason = 'source_only_metric_has_no_selected_product_observation'
            position = _position(value, **{key: Fraction(v) for key, v in percentiles.items()})
            row[side] = {**_rational_values(value), 'position': position, 'position_label': POSITION_LABELS[position],
                'gap_from_p50': _rational_values(value - Fraction(percentiles['p50'])) if value is not None else _values(None),
                'gap_unit': '百分点' if columns['unit'] == '%' else columns['unit'], 'reason': reason,
                'source': deepcopy(observation.get('source')), 'factory': profile['factories'][side],
                'measurement_scope': 'selected_product_specification_month'}
        result['rows'].append(row)
        result['sources'].append(deepcopy(source))
    result['available'] = bool(result['rows'])
    if not result['available']:
        result['reason'] = 'no_authorized_applicable_generic_industry_reference'
    return result
