"""Scoped adapters joining released evidence, deterministic facts and models."""
from __future__ import annotations
from calendar import monthrange
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import math
import re

from enterprise.security import require
from enterprise.knowledge_applicability import (
    ELEMENT_WORDS, applicability_reason, element_terms, matching_view,
)

# Retained as a compatibility alias for callers that inspected the old labels.
_ELEMENT_WORDS = ELEMENT_WORDS


class EvidenceList(list):
    """The historical list API plus this invocation's non-semantic diagnostics."""
    __slots__ = ('diagnostics',)

    def __init__(self, values=(), diagnostics=None):
        super().__init__(values)
        self.diagnostics = deepcopy(diagnostics or {})


def _objects(facts, element):
    branch = facts.get('elements', {}).get(element, {})
    rows = branch.get('details', branch.get('detail', []))
    if not isinstance(rows, list):
        return []

    def impact(row):
        values = [row.get(name) for name in ('amount_delta', 'unit_effect', 'current_amount')]
        return next((abs(value) for value in values
                     if isinstance(value, (int, float)) and math.isfinite(value)), 0)

    names = []
    for row in sorted((row for row in rows if isinstance(row, dict)), key=impact, reverse=True):
        name = row.get('name')
        if isinstance(name, str) and name.strip():
            name = re.sub(r'\s+', ' ', matching_view(name)).strip()[:60]
            if name not in names:
                names.append(name)
        if len(names) == 3:
            break
    return names


def benchmark_retrieval_facts(benchmark):
    """Scope queries to observed home rows, never invent missing peer detail."""
    elements = {}
    for element, branch in benchmark.get('paired_drilldown', {}).items():
        rows = []
        for detail in branch.get('rows', []):
            home = detail.get('home') or {}
            rows.append({'name': detail['name'], 'current_amount': home.get('amount')})
        elements[element] = {'details': rows}
    return {'elements': elements}


def _queries(product, facts):
    if facts is None:
        # Existing integrations retain one authorized invocation per month. The
        # optional facts path is the finer element/object strategy, not a bypass.
        return [{'element': None, 'objects': [],
                 'query': f'{product} 配方 工艺 收率 人工 工时 设备 制造费用 GMP 行业基准'}]
    result = []
    for element, terms in (
        ('材料', '原材料 投料 配方 工艺 收率 损耗'),
        ('人工', '人工 工时 定员 排班 返工'),
        ('制费', '设备 能耗 蒸汽 折旧 维修 分摊'),
    ):
        objects = _objects(facts, element)
        result.append({'element': element, 'objects': objects,
                       'query': ' '.join([product, element, *objects, terms])})
    return result


def _partition_queries(product, specification, facts, profile=None):
    """Route only relevant classes; scope comes from an operator-owned profile."""
    from enterprise.domain_profiles import product_definition, load_domain_profile, validate_domain_profile
    profile = load_domain_profile() if profile is None else validate_domain_profile(profile)
    definition = product_definition(product, specification, profile)
    if definition is None:
        # Preserve conservative legacy behavior for unconfigured product scopes.
        return _queries(product, facts), False
    queries = []
    mechanism_types = {'材料': ['formula', 'process'], '人工': ['process'],
                       '制费': ['equipment', 'process']}
    for query in _queries(product, facts if facts is not None else {'elements': {}}):
        queries.append({**query, 'knowledge_types': mechanism_types[query['element']], 'purpose': 'mechanism'})
    for category in (definition['category'], profile['industry_category']):
        queries.append({'element': None, 'objects': [], 'purpose': 'industry_reference',
                        'knowledge_types': ['industry_benchmark'],
                        'query': category+' 材料 人工 制造费用 单位成本 行业P25 行业P50 行业P75'})
    objects = _objects(facts or {}, '材料')
    materials = [name for name in definition['materials'] if not objects or name in objects]
    # Actual observed material names refine reference retrieval; absent detail uses
    # an explicit declared bill-of-materials membership, not guessed synonyms.
    if materials:
        queries.append({'element': '材料', 'objects': materials, 'purpose': 'market_reference',
                        'knowledge_types': ['market_prices'],
                        'query': ' '.join(materials)+' 同期 市场价格 规格等级 单位'})
    return queries, True


def report_evidence(principal, product, specification, months, root=None, *, facts=None, purposes=None, require_hybrid=False, domain_profile=None):
    """Return eligible, source-scoped evidence; never turn query scope into truth.

    Authorization/effective dates/revocation precede every candidate via the real
    LangChain controlled retriever. ``facts`` only refines queries. Source text and
    offsets are untouched; eligibility is not a semantic endorsement of a claim.
    """
    if domain_profile is not None:
        from enterprise.domain_profiles import validate_domain_profile, product_definition
        domain_profile = validate_domain_profile(domain_profile)
        if product_definition(product, specification, domain_profile) is None:
            raise ValueError('检索产品规格未在受控领域中登记')
    home_factory = domain_profile['factories']['home'] if domain_profile is not None else '中药一厂'
    require(principal, 'knowledge.read', factory=home_factory, product=product)
    from enterprise.knowledge import KnowledgeError, Repository
    from enterprise.knowledge_release import get_search_engine
    from enterprise.knowledge_langchain import retrieve_rows
    if not isinstance(months, (list, tuple)) or any(
            not isinstance(month, str) or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', month) for month in months):
        raise KnowledgeError('月份须为YYYY-MM列表')
    if facts is not None and (not isinstance(facts, dict) or not isinstance(facts.get('elements', {}), dict)):
        raise KnowledgeError('facts须为确定性事实对象')
    if not isinstance(require_hybrid, bool):
        raise KnowledgeError('require_hybrid须为布尔值')
    repo = Repository(root, principal=principal)
    engine = get_search_engine(repository=repo)
    queries, partitioned = (_partition_queries(product, specification, facts, domain_profile)
                            if domain_profile is not None else _partition_queries(product, specification, facts))
    if purposes is not None:
        if (not isinstance(purposes, (list, tuple)) or not purposes
                or any(value not in {'mechanism', 'industry_reference', 'market_reference'} for value in purposes)):
            raise KnowledgeError('purposes须为非空受控检索用途列表')
        # Purpose selection only narrows the server-owned routes; it never grants
        # source authority or expands the already authorized document scope.
        queries = [query for query in queries if query.get('purpose', 'mechanism') in purposes]
    combined, candidates, query_stats = {}, {}, []
    release_id, known_at = None, datetime.now(timezone.utc).isoformat()
    for month in dict.fromkeys(months):
        year, mon = map(int, month.split('-'))
        for query in queries:
            rows, stats = retrieve_rows(query['query'] + ' ' + month,
                principal=principal, repository=repo, engine=engine,
                product=product, factory=home_factory, as_of=f'{month}-{monthrange(year, mon)[1]:02d}',
                top_k=12 if partitioned else (30 if facts is not None else 10),
                release_id=release_id, known_at=known_at,
                **({'require_hybrid': True, 'vector_timeout': 15.0} if require_hybrid else {}),
                **({'knowledge_types': query['knowledge_types']} if query.get('knowledge_types') else {}))
            if require_hybrid and stats.get('reason') in {'hybrid_unavailable', 'no_published_release', 'retrieval_not_ready'}:
                raise KnowledgeError('混合检索尚未就绪：' + ','.join(stats.get('degradation_reasons') or [stats.get('reason')])
                                     + '；保留数值看板，请完成向量发布和预热后再生成模型分析。')
            release_id = stats.get('release_id') or release_id
            query_stats.append({'month': month, **query, 'stats': deepcopy(stats)})
            for row in rows:
                meta, text, identity = row['meta'], row['text'], row['chunk_id']
                governance = meta.get('business_metadata', {})
                applicability = meta.get('applicability', {})
                relevant = element_terms(text)
                trace = candidates.setdefault(identity, {'chunk_id': identity, 'version_id': row['version_id'],
                    'retrieved': True, 'eligible': False, 'selected': False, 'months': [], 'reasons': []})
                if month not in trace['months']:
                    trace['months'].append(month)
                if meta.get('reference_kind') in {'industry_reference', 'market_reference'}:
                    from enterprise.evidence_references import reference_evidence
                    reference, reason = reference_evidence(row, stats, product=product,
                        specification=specification, month=month,
                        objects=query.get('objects') if query.get('purpose') == 'market_reference' else None,
                        **({'profile': domain_profile} if domain_profile is not None else {}))
                    if not stats.get('product_scope_known', False):
                        reference, reason = None, 'unknown_query_product'
                    if reason:
                        if reason not in trace['reasons']:
                            trace['reasons'].append(reason)
                        continue
                    trace['eligible'] = True
                    if identity not in combined:
                        combined[identity] = reference
                    evidence = combined[identity]
                    if month not in evidence['scope']['months']:
                        evidence['scope']['months'].append(month)
                    evidence['retrieval_degraded'] |= stats['degraded']
                    for observation in reference.get('market_observations', []):
                        if observation not in evidence.setdefault('market_observations', []):
                            evidence['market_observations'].append(observation)
                    evidence['retrieval_queries'].append({'element':query['element'],
                        'objects':query['objects'],'purpose':query.get('purpose')})
                    evidence['retrieval_observations'].append({'month':month,'element':query['element'],
                        **{key:deepcopy(row[key]) for key in ('score','rerank_score','source_routes') if key in row}})
                    continue
                reason = applicability_reason(applicability, product, specification)
                if not stats.get('product_scope_known', False):
                    reason = 'unknown_query_product'
                elif domain_profile is not None and governance.get('manufacturing_domain_profile') != domain_profile:
                    # A generic or legacy pharmaceutical scope is not evidence of
                    # applicability to a different installed manufacturing domain.
                    reason = 'manufacturing_domain_binding_missing_or_changed'
                elif governance.get('evidence_role', 'context_only') != 'document_basis':
                    reason = ('context_only' if governance.get('evidence_role', 'context_only') == 'context_only'
                              else 'unknown_evidence_role')
                elif not relevant:
                    reason = 'no_supported_element'
                if reason:
                    if reason not in trace['reasons']:
                        trace['reasons'].append(reason)
                    continue
                trace['eligible'] = True
                if identity not in combined:
                    products = list(applicability['products'])
                    specifications = list(applicability['specifications'])
                    combined[identity] = {
                        'id': 'K' + hashlib.sha256(identity.encode()).hexdigest()[:12],
                        'text': text, 'kind': 'document_basis', 'evidence_role': 'document_basis', 'elements': list(relevant),
                        'element_support': relevant, 'support_status': 'eligible',
                        'document_id': row['document_id'], 'business_metadata': deepcopy(governance),
                        'authority': governance.get('authority', 'unreviewed'),
                        'known_conflicts': deepcopy(governance.get('known_conflicts', [])),
                        'limitations': deepcopy(governance.get('limitations', [])),
                        'version_id': row['version_id'], 'document_sha256': meta['sha256'],
                        'chunk_id': identity, 'index_release_id': row['release_id'],
                        'applicability': deepcopy(applicability),
                        'source': {'file': meta['filename'], 'sha256': meta['sha256'],
                            'version_id': row['version_id'], 'ordinal': meta['ordinal'],
                            'offset': meta['offset'], 'end_offset': meta['end_offset'],
                            'offset_basis': 'confirmed_text_unicode_characters_end_exclusive',
                            'page': meta.get('page_hint'), 'pages': deepcopy(meta.get('pages', [])),
                            'page_spans': deepcopy(meta.get('page_spans', [])),
                            'section': meta.get('section', ''), 'quote': text},
                        'scope': {'product': products[0] if applicability['kind'] == 'product' else '*',
                                  'products': products,
                                  'specification': specifications[0] if len(specifications) == 1 else '*',
                                  'specifications': specifications, 'months': []},
                        'retrieval_mode': stats['retrieval_mode'], 'retrieval_degraded': stats['degraded'],
                        'retrieval_framework': deepcopy(stats['framework']),
                        **{key: deepcopy(row[key]) for key in ('score', 'rerank_score', 'route_scores',
                             'retrieval_scores', 'rerank_detail', 'source_routes') if key in row},
                        'retrieval_observations': [],
                        'retrieval_queries': [],
                        'vision_enhanced': bool((meta.get('parse_metadata') or {}).get('vision_enhanced')),
                        'vision_reviewed': bool((governance or {}).get('vision_reviewed')),
                        'claim_boundary': '原文仅支持所述产品或通用条款的机制及核查方向，不证明本期实际事件、实际收率、采购实价或因果已成立',
                        'support_boundary': {'supports': ['source_stated_mechanism', 'verification_direction'],
                            'cannot_prove': ['current_period_event', 'actual_yield', 'actual_purchase_price', 'confirmed_causality'],
                            'semantic_support': 'pending_human_review'},
                    }
                evidence = combined[identity]
                if month not in evidence['scope']['months']:
                    evidence['scope']['months'].append(month)
                evidence['retrieval_degraded'] |= stats['degraded']
                evidence['retrieval_observations'].append({'month': month, 'element': query['element'],
                    **{key: deepcopy(row[key]) for key in ('score', 'rerank_score', 'route_scores',
                         'retrieval_scores', 'rerank_detail', 'source_routes') if key in row}})
                query_ref = {'element': query['element'], 'objects': query['objects']}
                if query_ref not in evidence['retrieval_queries']:
                    evidence['retrieval_queries'].append(query_ref)
    # A revocation during a later query must invalidate earlier collected chunks.
    revoked = engine.releases._revoked()
    for identity, evidence in list(combined.items()):
        if evidence['document_id'] in revoked:
            combined.pop(identity)
            candidates[identity]['eligible'] = False
            candidates[identity]['reasons'].append('revoked_during_retrieval')
    ranked = sorted(combined.values(), key=lambda e: (
        e['applicability']['kind'] != 'product',
        -sum(name in matching_view(e['text']) for q in queries for name in q['objects']),
        -len(e['scope']['months']), -len(e['elements']), e['chunk_id']))
    # References and mechanism quotes have distinct budgets and authority. Long
    # regulations cannot consume a product's price/benchmark/mechanism coverage.
    selected, selected_ids, chars = [], set(), 0
    maximum = 20 if partitioned else 12
    quotas = {'document_basis': 6, 'industry_reference': 8, 'market_reference': 6}
    selected_counts = {}

    def choose(evidence):
        nonlocal chars
        kind = evidence['kind']
        if partitioned and selected_counts.get(kind, 0) >= quotas.get(kind, 0):
            return
        if evidence['chunk_id'] not in selected_ids and len(selected) < maximum and chars + len(evidence['text']) <= 14000:
            selected.append(evidence)
            selected_ids.add(evidence['chunk_id'])
            selected_counts[kind] = selected_counts.get(kind, 0)+1
            chars += len(evidence['text'])

    for element in ELEMENT_WORDS:
        for evidence in [e for e in ranked if e['kind'] == 'document_basis' and element in e['elements'] and len(e['text']) <= 14000][:2]:
            choose(evidence)
    if partitioned:
        for kind in ('industry_reference', 'market_reference'):
            for evidence in ranked:
                if evidence['kind'] == kind:
                    choose(evidence)
    for evidence in ranked:
        choose(evidence)
    for identity, trace in candidates.items():
        trace['selected'] = identity in selected_ids
        if trace['eligible'] and not trace['selected']:
            trace['reasons'].append('selection_budget')
    gaps = [{'element': element, 'reason': 'no_selected_applicable_mechanism'} for element in ELEMENT_WORDS
            if not any(e['kind'] == 'document_basis' and element in e['elements'] for e in selected)]
    diagnostics = {'schema_version': 'report-evidence/3.0' if partitioned else 'report-evidence/2.0',
        'index_release_id': release_id, 'release_id': release_id, 'known_at': known_at,
        'requested_scope': {'product': product, 'specification': specification, 'months': list(dict.fromkeys(months))},
        'query_strategy': 'typed_element_and_reference' if partitioned else ('element_objects' if facts is not None else 'legacy_combined'),
        'queries': query_stats, 'selected_by_kind': selected_counts,
        'recall_limit_per_query': 12 if partitioned else (30 if facts is not None else 10),
        'stages': {'retrieved': len(candidates), 'eligible': len(combined), 'selected': len(selected),
                   'cited': None, 'supported': None},
        'candidates': list(candidates.values()), 'gaps': gaps,
        'selection_budget': {'max_chunks': maximum, 'max_characters': 14000, 'selected_characters': chars,
                             'kind_quotas': quotas if partitioned else None},
        'retrieval_mode': 'typed_partitioned' if partitioned else 'legacy_combined',
        'degraded': any(q['stats'].get('degraded', True) for q in query_stats),
        'degradation_reasons': sorted({reason for q in query_stats for reason in q['stats'].get('degradation_reasons', [])}),
        'semantic_support': 'not_evaluated_by_retrieval'}
    return EvidenceList(selected, diagnostics)


def validated_model(payload, evidence, *, trace_recorder=None, model_config=None):
    """Preserve business return shapes; optionally record actual in-call traces.

    The benchmark branch remains its bounded-worker envelope. Report traces are
    captured where the gateway runs, never reconstructed from configuration or
    attached as extra keys to the strict model JSON contract.
    """
    from enterprise.model_gateway import capture_model_calls, generate_json
    if payload.get('schema_version') == 'benchmark-explanations/1.0':
        from attribution_runtime import run_stage, prepare_model_stage
        args, budget = prepare_model_stage(payload, evidence, task='benchmark', config=model_config)
        return run_stage('model', args, timeout=budget)
    instruction = payload.get('instruction')
    if not instruction:
        raise ValueError('模型调用缺少受控任务指令')
    clean = deepcopy(payload)
    clean.pop('instruction', None)
    is_report_v2 = payload.get('schema_version') == 'report-claims/2.0'
    max_tokens = 4500 if is_report_v2 else 2500
    if is_report_v2:
        # Model context contains readable facts and quotes, not repeated raw row
        # provenance or old nested analysis namespaces. Full sources stay frozen.
        evidence = [{key: deepcopy(row[key]) for key in
                     ('id', 'kind', 'elements', 'text', 'scope', 'applicability', 'claim_boundary', 'known_conflicts', 'limitations')
                     if key in row} for row in evidence]
        # Numeric standards remain in the frozen source/quote cards; hiding them
        # from the free-text model prevents unstructured repetition of standards.
        import re
        def hide_standard_numbers(text):
            return re.sub(r'(?<![A-Za-z])\d+(?:\.\d+)?\s*(?:%|％|元|盒|支|袋|粒|小时|℃|倍|次|MPa|ml|L|kg|g)', '[受控数值见引文卡]', text)
        for row in evidence:
            row['text'] = hide_standard_numbers(row.get('text', ''))
        for element in clean.get('facts', {}).get('elements', {}).values():
            focus = element.get('explanation_focus', {})
            if 'source_quote' in focus:
                focus['source_quote'] = hide_standard_numbers(focus['source_quote'])
    kwargs = {'max_tokens': max_tokens}
    if is_report_v2:
        # Real gateway routing is server-owned. Keep pre-routing, injected Python
        # test adapters callable without catching/retrying a provider TypeError.
        from inspect import Parameter, signature
        parameters = signature(generate_json).parameters
        if 'task' in parameters or any(p.kind == Parameter.VAR_KEYWORD for p in parameters.values()):
            kwargs['task'] = 'report'
    with capture_model_calls() as calls:
        try:
            return generate_json(instruction, {'facts': clean, 'evidence': evidence}, **kwargs)
        finally:
            if trace_recorder is not None:
                trace_recorder(deepcopy(calls))
