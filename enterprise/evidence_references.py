"""Build scoped reference evidence without turning reference rows into mechanisms."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation, localcontext
import hashlib

from enterprise.numeric import format_number, format_percent


def reference_evidence(row, stats, *, product, specification, month, objects=None, profile=None):
    from enterprise.tabular_knowledge import reference_applicability_reason, is_generic_table
    from calendar import monthrange
    import re
    if not isinstance(month, str) or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', month) or month.startswith('0000'):
        return None, 'invalid_reference_month'
    year, mon = map(int,month.split('-'))
    meta=row['meta']
    reason=reference_applicability_reason(meta,product,specification,
        as_of=f'{month}-{monthrange(year,mon)[1]:02d}',objects=objects,profile=profile)
    if reason:
        return None,reason
    table=meta['table_row'];kind=meta['reference_kind'];governance=meta.get('business_metadata',{})
    if kind not in {'industry_reference','market_reference'}:
        return None,'non_reference_observation'
    elements=meta.get('elements') or ['材料','人工','制费']
    source={'file':meta['filename'],'sha256':meta['sha256'],'version_id':row['version_id'],
        'ordinal':meta['ordinal'],'offset':meta['offset'],'end_offset':meta['end_offset'],
        'offset_basis':'confirmed_text_unicode_characters_end_exclusive',
        'record_number':table['record_number'],'line':table['physical_line_start'],
        'line_end':table['physical_line_end'],'headers':deepcopy(table['headers']),
        'header_offset':table['header_offset'],'header_end_offset':table['header_end_offset'],
        'quote':row['text'],'row_sha256':table['row_sha256']}
    evidence={'id':'K'+hashlib.sha256(row['chunk_id'].encode('utf-8')).hexdigest()[:12],
        'kind':kind,'knowledge_type':meta['knowledge_type'],'text':row['text'],
        'elements':list(elements),'evidence_role':governance['evidence_role'],'support_status':'eligible',
        'source':source,'table_row':deepcopy(table),'reference_metadata':deepcopy(meta),
        'document_id':row['document_id'],'version_id':row['version_id'],'chunk_id':row['chunk_id'],
        'document_sha256':meta['sha256'],'index_release_id':row['release_id'],
        'business_metadata':deepcopy(governance),'authority':governance.get('authority','unreviewed'),
        'known_conflicts':deepcopy(governance.get('known_conflicts',[])),
        'limitations':deepcopy(governance.get('limitations',[])),
        'applicability':deepcopy(meta['applicability']),
        'scope':{'product':product,'products':[product],'specification':specification,
                 'specifications':[specification],'months':[month]},
        'retrieval_mode':stats['retrieval_mode'],'retrieval_degraded':stats['degraded'],
        'retrieval_framework':deepcopy(stats.get('framework', {})),
        'retrieval_observations':[],'retrieval_queries':[],
        'claim_boundary':'外部行业/市场参考，不是实际采购、实际收率、两厂原因或可节约金额的证明',
        'support_boundary':{'supports':['external_reference','comparison_anchor','verification_direction'],
            'cannot_prove':['actual_purchase_price','actual_yield','confirmed_causality','actual_savings'],
            'semantic_support':'reference_only'},
        **{key:deepcopy(row[key]) for key in ('score','rerank_score','route_scores','retrieval_scores',
                                               'rerank_detail','source_routes') if key in row}}
    if is_generic_table(table):
        source.update(header_quote=table['header_quote'], header_sha256=table['header_sha256'],
                      schema_identifier=table['schema_identifier'])
        evidence['reference_profile'] = deepcopy(meta['reference_profile'])
        reason = reference_source_reason(evidence, product, specification, month, profile=profile)
        if reason:
            return None, reason
        columns = table['columns']
        if kind == 'market_reference':
            price = Decimal(columns['price'])
            evidence['market_observations'] = [{'month': columns['month'], 'material': columns['material'],
                'grade': columns['grade'], 'unit': columns['unit'], 'source_market': columns['source_market'],
                'current_price': float(price), 'current_price_exact': str(price),
                'previous_price': None, 'previous_price_exact': None, 'month_change_pct': None,
                'month_change_pct_exact': None, 'direction': '无上月可比值',
                'months_exposed_to_model': [columns['month']], 'future_prices_and_full_period_trend_excluded': True,
                'evidence_ids': [evidence['id']],
                'boundary': '单条来源仅证明该月外部报价；上月须另有通过授权与来源校验的记录，不能反推。'}]
            evidence['reference_fact_text'] = (f"{columns['material']} {columns['month']}市场参考价"
                f"{format_number(price)}{columns['unit']}；不是任一工厂实际采购价。")
        else:
            evidence['reference_fact_text'] = (f"{columns['category']} {columns['metric']}行业参考："
                f"P25 {columns['p25']}、P50 {columns['p50']}、P75 {columns['p75']}（{columns['unit']}）；"
                f"来源期间{columns['period_start']}至{columns['period_end']}，不代表所选产品实测。")
        return evidence, None
    if kind=='market_reference':
        columns=table['columns'];current=columns.get(f'{mon}月价格');previous=columns.get(f'{mon-1}月价格')
        try:
            price=Decimal(str(current)); prior=Decimal(str(previous)) if previous is not None else None
            if not price.is_finite() or price<0 or (prior is not None and (not prior.is_finite() or prior<0)):
                return None,'invalid_market_number'
            with localcontext() as ctx:
                ctx.prec=40
                change=(price-prior)/prior*100 if prior else None
        except InvalidOperation:
            return None,'missing_monthly_market_reference'
        direction='无上月可比值' if prior is None else '上涨' if price>prior else '下降' if price<prior else '持平'
        projection={'month':month,'material':columns['药材名称'],'grade':columns['规格等级'],
            'unit':columns['单位'],'source_market':columns['价格来源'],
            'current_price':float(price),'current_price_exact':str(price),
            'previous_price':float(prior) if prior is not None else None,
            'previous_price_exact':str(prior) if prior is not None else None,
            'month_change_pct':float(change) if change is not None else None,
            'month_change_pct_exact':str(change) if change is not None else None,
            'direction':direction,'months_exposed_to_model':[month] if prior is None else [f'{year:04d}-{mon-1:02d}',month],
            'future_prices_and_full_period_trend_excluded':True,
            'boundary':'市场报价不等于任一工厂结算价；同向市场变动不能确认或排除两厂采购价差'}
        evidence['market_observations']=[projection]
        evidence['reference_fact_text']=f"{columns['药材名称']} {month}市场参考价{format_number(price)}{columns['单位']}"+(f"，较上月{format_percent(change,signed=True)}%" if change is not None else '')+'；不是本厂或二厂采购实价。'
    else:
        columns=table['columns']
        evidence['reference_fact_text']=(f"行业文件列示{columns['产品类别']}的{columns['指标']}：P25 {columns['行业P25']}、P50 {columns['行业P50']}、P75 {columns['行业P75']}；统计窗口与同口径适用性仍须核对。")
    return evidence,None


def reference_source_reason(source, product, specification, month, *, profile=None):
    """Check redundant frozen fields against the original CSV record, not authority.

    The caller still obtains rows only from an authorized release. This secondary
    gate prevents stale/mutated UI or report projections from changing the trusted
    metadata, row values, kind, source identity, or business period at use time.
    """
    import csv
    import io
    import re
    from calendar import monthrange
    from enterprise.tabular_knowledge import reference_applicability_reason, is_generic_table
    if not isinstance(source, dict) or source.get('support_status') != 'eligible':
        return 'not_eligible_reference'
    meta = source.get('reference_metadata')
    if not isinstance(meta, dict):
        return 'missing_reference_metadata'
    if source.get('kind') not in {'industry_reference', 'market_reference'} or source['kind'] != meta.get('reference_kind'):
        return 'reference_kind_mismatch'
    table = source.get('table_row')
    if not isinstance(table, dict) or table != meta.get('table_row'):
        return 'reference_row_metadata_mismatch'
    scope = source.get('scope') or {}
    declared_months = scope.get('months') if isinstance(scope, dict) else None
    if (not isinstance(scope, dict) or scope.get('product') != product
            or scope.get('specification') != specification
            or not isinstance(declared_months, (list, tuple)) or not declared_months
            or any(not isinstance(item, str) or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', item)
                   for item in declared_months)
            or month not in declared_months):
        return 'reference_scope_mismatch'
    if not isinstance(month, str) or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', month):
        return 'invalid_reference_month'
    if month.startswith('0000'):
        return 'invalid_reference_month'
    generic = is_generic_table(table)
    year, number = map(int, month.split('-'))
    as_of = month + f'-{monthrange(year, number)[1]:02d}' if generic else month + '-01'
    reason = reference_applicability_reason(meta, product, specification, as_of=as_of, profile=profile)
    if reason:
        return reason
    if generic:
        # Industry month-level comparisons require the entire observed month,
        # not a partial window promoted by a single as-of date.
        if source['kind'] == 'industry_reference' and table['columns']['period_start'] > month + '-01':
            return 'incomplete_reference_month'
        if (source.get('reference_profile') != meta.get('reference_profile')
                or source.get('business_metadata') != meta.get('business_metadata')
                or source.get('applicability') != meta.get('applicability')
                or source.get('evidence_role') != meta.get('business_metadata', {}).get('evidence_role')
                or source.get('knowledge_type') != meta.get('knowledge_type')
                or source.get('elements') != meta.get('elements')
                or scope.get('products') != [product] or scope.get('specifications') != [specification]):
            return 'reference_frozen_metadata_mismatch'
        if any(not isinstance(source.get(key), str) or not source[key] for key in
               ('id', 'document_id', 'version_id', 'chunk_id', 'index_release_id')):
            return 'missing_reference_release_identity'
        if source['id'] != 'K' + hashlib.sha256(source['chunk_id'].encode('utf-8')).hexdigest()[:12]:
            return 'reference_evidence_id_mismatch'
        if (('doc_id' in meta and source['document_id'] != meta['doc_id'])
                or ('release_id' in meta and source['index_release_id'] != meta['release_id'])):
            return 'reference_release_identity_mismatch'
    location = source.get('source') or {}
    text = source.get('text')
    if not isinstance(location, dict) or not isinstance(text, str) or not text:
        return 'missing_reference_source'
    for key in ('file', 'sha256', 'offset', 'end_offset'):
        meta_key = 'filename' if key == 'file' else key
        if location.get(key) != meta.get(meta_key):
            return 'reference_source_identity_mismatch'
    if (not re.fullmatch(r'[0-9a-f]{64}', str(location.get('sha256', '')))
            or source.get('version_id') != meta.get('version_id')
            or location.get('version_id') != source.get('version_id')
            or location.get('quote') != text
            or location.get('row_sha256') != table.get('row_sha256')
            or hashlib.sha256(text.encode('utf-8')).hexdigest() != table.get('row_sha256')):
        return 'reference_source_hash_mismatch'
    try:
        records = list(csv.reader(io.StringIO(text, newline=''), strict=True))
        if len(records) != 1 or records[0] != [table['columns'][key] for key in table['headers']]:
            return 'reference_columns_text_mismatch'
    except (csv.Error, KeyError, TypeError):
        return 'invalid_reference_record'
    if generic:
        for key, expected in {'record_number': table.get('record_number'),
                'line': table.get('physical_line_start'), 'line_end': table.get('physical_line_end'),
                'headers': table.get('headers'), 'header_offset': table.get('header_offset'),
                'header_end_offset': table.get('header_end_offset'), 'header_quote': table.get('header_quote'),
                'header_sha256': table.get('header_sha256'), 'schema_identifier': table.get('schema_identifier'),
                'ordinal': meta.get('ordinal')}.items():
            if location.get(key) != expected:
                return 'reference_source_location_mismatch'
        positions = [location.get(k) for k in ('header_offset', 'header_end_offset', 'offset', 'end_offset')]
        if any(type(value) is not int for value in positions):
            return 'invalid_reference_offsets'
        hs, he, start, end = positions
        header = table.get('header_quote')
        if (not isinstance(header, str) or not 0 <= hs < he <= start < end
                or end - start != len(text) or he - hs != len(header)
                or hashlib.sha256(header.encode('utf-8')).hexdigest() != table.get('header_sha256')
                or source.get('document_sha256') != meta.get('sha256')
                or source.get('source', {}).get('offset_basis') != 'confirmed_text_unicode_characters_end_exclusive'):
            return 'reference_header_or_offset_mismatch'
        try:
            if list(csv.reader(io.StringIO(header, newline=''), strict=True)) != [table['headers']]:
                return 'reference_header_text_mismatch'
        except csv.Error:
            return 'reference_header_text_mismatch'
    return None


def build_reference_evidence(row, stats, *, product, specification, month, objects=None, profile=None):
    """Canonical call spelling; only consumes already release-authorized rows."""
    return reference_evidence(row, stats, product=product, specification=specification,
                              month=month, objects=objects, profile=profile)


def market_reading_rows(evidence, product, specification, month, *, profile=None):
    """Recompute display values from admitted original rows, never cached prices."""
    from enterprise.tabular_knowledge import is_generic_table
    rows = []
    seen = set()
    for source in evidence or []:
        if not isinstance(source, dict) or source.get('kind') != 'market_reference':
            continue
        if is_generic_table(source.get('table_row')):
            continue
        if reference_source_reason(source, product, specification, month, profile=profile) is not None:
            continue
        columns = source['table_row']['columns']
        identity = (columns['药材名称'], columns['规格等级'], columns['单位'], columns['价格来源'])
        if identity in seen:
            # Ambiguous versions must not silently choose a different price.
            rows = [row for row in rows if row['_identity'] != identity]
            continue
        seen.add(identity)
        number = int(month[-2:])
        try:
            current = Decimal(columns[f'{number}月价格'])
            previous = Decimal(columns[f'{number-1}月价格']) if number > 1 else None
            if current < 0 or not current.is_finite() or (previous is not None and (not previous.is_finite() or previous < 0)):
                continue
            with localcontext() as context:
                context.prec = 40
                change = (current - previous) / previous * 100 if previous else None
        except (InvalidOperation, KeyError, TypeError, ValueError):
            continue
        rows.append({'_identity': identity, 'material': columns['药材名称'], 'grade': columns['规格等级'],
                     'unit': columns['单位'], 'source_market': columns['价格来源'], 'month': month,
                     'previous_price': str(previous) if previous is not None else None,
                     'current_price': str(current), 'month_change_pct': str(change) if change is not None else None,
                     'evidence_id': source['id'],
                     'boundary': '市场报价不等于任一工厂结算价；不能据此确认或排除两厂采购价差。'})
    return ([{key: value for key, value in row.items() if key != '_identity'} for row in rows]
            + _generic_market_reading_rows(evidence, product, specification, month, profile=profile))


def _generic_market_reading_rows(evidence, product, specification, month, *, profile=None):
    """Pair exactly adjacent observations, each with its own authorized citation."""
    import re
    from enterprise.tabular_knowledge import is_generic_table
    if not isinstance(month, str) or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', month) or month.startswith('0000'):
        return []
    year, number = map(int, month.split('-'))
    previous_month = f'{year:04d}-{number-1:02d}' if number > 1 else f'{year-1:04d}-12'
    groups = {}
    for source in evidence or []:
        if not isinstance(source, dict) or source.get('kind') != 'market_reference' or not is_generic_table(source.get('table_row')):
            continue
        columns = source['table_row'].get('columns') or {}
        observed_month = columns.get('month')
        if observed_month not in (month, previous_month):
            continue
        if reference_source_reason(source, product, specification, observed_month, profile=profile) is not None:
            continue
        identity = (tuple(columns[k] for k in ('material', 'grade', 'unit', 'source_market'))
                    + (source['reference_profile']['profile_sha256'], source['index_release_id']))
        groups.setdefault(identity, {}).setdefault(observed_month, []).append(source)
    rows = []
    for identity, observations in groups.items():
        current_rows, previous_rows = observations.get(month, []), observations.get(previous_month, [])
        # Do not select a convenient quote/version when sources disagree or repeat.
        if len(current_rows) != 1:
            continue
        current_source = current_rows[0]
        previous_source = previous_rows[0] if len(previous_rows) == 1 else None
        current = Decimal(current_source['table_row']['columns']['price'])
        previous = Decimal(previous_source['table_row']['columns']['price']) if previous_source else None
        with localcontext() as context:
            context.prec = 40
            change = (current - previous) / previous * 100 if previous else None
        ids = [current_source['id']] if previous_source is None else [previous_source['id'], current_source['id']]
        rows.append({'material': identity[0], 'grade': identity[1], 'unit': identity[2],
            'source_market': identity[3], 'month': month,
            'previous_month': previous_month if previous_source else None,
            'previous_price': str(previous) if previous is not None else None,
            'current_price': str(current), 'month_change_pct': str(change) if change is not None else None,
            'evidence_id': current_source['id'], 'evidence_ids': ids,
            'current_evidence_id': current_source['id'],
            'previous_evidence_id': previous_source['id'] if previous_source else None,
            'current_source': deepcopy(current_source['source']),
            'previous_source': deepcopy(previous_source['source']) if previous_source else None,
            'comparison_reason': ('ambiguous_previous_reference_rows' if len(previous_rows) > 1 else
                                  'previous_month_not_provided' if previous_source is None else None),
            'boundary': '各月外部报价各有独立来源；不是任一工厂结算价，不能确认或排除工厂采购价差。'})
    return rows


def reference_model_context(evidence, element, *, month=None):
    """Bounded qualitative projection. Exact quotes stay in the audit/UI only."""
    from enterprise.tabular_knowledge import is_generic_table
    result=[]
    seen_generic=set()
    for source in evidence:
        generic = is_generic_table(source.get('table_row'))
        requested_element = {'材料': 'material', '人工': 'labor', '制费': 'overhead'}.get(element, element) if generic else element
        if source.get('support_status')!='eligible' or requested_element not in source.get('elements',[]): continue
        if generic:
            scope = source.get('scope') or {}
            selected_month = month or next(iter(scope.get('months') or []), None)
            product, specification = scope.get('product'), scope.get('specification')
            if reference_source_reason(source, product, specification, selected_month) is not None:
                continue
            columns = source['table_row']['columns']
            if source['kind'] == 'market_reference':
                for reading in _generic_market_reading_rows(evidence, product, specification, selected_month):
                    if reading['current_evidence_id'] != source['id'] or source['id'] in seen_generic:
                        continue
                    seen_generic.add(source['id'])
                    before, after = reading['previous_price'], reading['current_price']
                    direction = ('无上月可比值' if before is None else '上涨' if Decimal(after) > Decimal(before)
                                 else '下降' if Decimal(after) < Decimal(before) else '持平')
                    result.append({'id': source['id'], 'evidence_ids': reading['evidence_ids'],
                        'kind': 'market_reference', 'material': reading['material'], 'grade': reading['grade'],
                        'unit': reading['unit'], 'market_direction': direction, 'boundary': reading['boundary']})
            else:
                result.append({'id': source['id'], 'kind': 'industry_reference', 'category': columns['category'],
                    'metric': columns['metric'], 'unit': columns['unit'], 'boundary': source['claim_boundary'],
                    'period_basis': '逐行明确统计窗口的外部类别参照，不是所选产品当月实测'})
            continue
        if source.get('kind')=='market_reference' and source.get('market_observations'):
            observations=[x for x in source['market_observations'] if month is None or x['month']==month]
            if not observations: continue
            result.append({'id':source['id'],'kind':'market_reference',
                'material':observations[-1]['material'],'grade':observations[-1]['grade'],
                'unit':observations[-1]['unit'],'market_direction':observations[-1]['direction'],
                'boundary':observations[-1]['boundary']})
        elif source.get('kind')=='industry_reference':
            cols=source.get('table_row',{}).get('columns',{})
            result.append({'id':source['id'],'kind':'industry_reference','category':cols.get('产品类别'),
                'metric':cols.get('指标'),'boundary':source['claim_boundary'],
                'period_basis':'文档年度类别参照；未明确统计窗口，不是所选产品当月实测'})
    return result
