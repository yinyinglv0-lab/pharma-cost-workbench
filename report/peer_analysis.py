"""Period-aware peer explanations frozen before report export, never appended later."""
from copy import deepcopy
from decimal import Decimal

from .claims import generate_claims, render_element
from .narrative import amount, knowledge_summary, LABELS, unchanged


def explain_peer(benchmark, home_facts, knowledge, *, product, specification, months,
                 model_fn=None, use_llm=True, model_version='not_configured'):
    if not benchmark.get('available'):
        return {'available': False, 'text': benchmark.get('reason') or '未启用或缺少可比期间资料',
                'used_llm': False, 'generation_status': 'insufficient_data',
                'fallback_reason': benchmark.get('reason'), 'sections': [], 'evidence': [], 'claim_ledger': []}
    evidence, facts = [], deepcopy(home_facts)
    facts.update(analysis_scope='cross_factory', months=list(months),
                 peer_periods=deepcopy(benchmark['periods']),
                 missing_facts=['二厂原材料配对明细', '二厂班次工时与工资归集', '二厂费用分项与分配基数'])
    facts.pop('monthly_attribution', None)
    facts.pop('reference_decomposition', None)
    facts['single_factory_amount_change'] = facts.pop('amount_change', None)
    totals = {}
    for index, element in enumerate(('材料', '人工', '制费'), 1):
        parts = [next(row for row in period['elements'] if row['element'] == element) for period in benchmark['periods']]
        total = sum((Decimal(part['normalized_amount_exact']) for part in parts), Decimal(0))
        totals[element] = total
        refs = [deepcopy(source) for period in benchmark['periods'] for source in period['sources']]
        evidence.append({'id': f'BP{index:03d}', 'kind': 'data_fact', 'elements': [element],
                         'text': f"{LABELS[element]}完整期间逐月同品同规格标准化金额差{amount(total, signed=True)}元。",
                         'source': {'records': refs}, 'support_status': 'eligible'})
        facts['elements'][element].update(normalized_amount=float(total),
            amount_delta=float(total), evidence_ids=[f'BP{index:03d}'], period_gaps=parts,
            comparison_basis='逐月同品同规格单位差乘当月一厂产量，再按完整期间求和')
    overall = sum(totals.values(), Decimal(0))
    if overall != sum((Decimal(p['normalized_amount_exact']) for p in benchmark['periods']), Decimal(0)):
        raise ValueError('期间跨厂要素差额未闭合')
    facts['comparison'] = {'normalized_amount_exact': str(overall),
                           'periods': list(months), 'formula': benchmark['formula'],
                           'not_realized_savings': True}
    evidence.extend(deepcopy(knowledge))
    generation = generate_claims(facts, evidence, product=product, specification=specification, months=months,
                                 model_fn=model_fn, use_llm=use_llm, cross_factory=True, model_version=model_version)
    sections, ledger = [], []
    gaps = {'材料': '二厂缺少同原料领料结转单价、净耗用及批次收率，不能确认采购或耗用差异；由采购部、生产部配对两厂同批次口径记录。',
            '人工': '二厂未提供工时及工资归集明细，不能确认差额来自工时投入还是归集费率；由生产部、财务部补齐同期间工时及工资分配记录。',
            '制费': '二厂未提供费用分项和分配基数，不能确认折旧、动力等分项的跨厂差额；由财务部、设备部配对分项费用及分配底稿。'}
    for element in ('材料', '人工', '制费'):
        item = generation['contract']['elements'][element]
        no_change = unchanged(facts, element)
        ref = f"BP{('材料', '人工', '制费').index(element) + 1:03d}"
        fact = f"{LABELS[element]}完整期间标准化金额差{amount(totals[element], signed=True)}元。[{ref}]"
        if no_change:
            text = fact + '各可比月份单位成本均相同，本项不新增差异原因核查任务。'
            claims, actions, missing = [], [], []
        else:
            _, claims = render_element(item, evidence, element)
            # The exact document quotes already appear in section 3 / the short
            # citation appendix. Do not duplicate a full mechanism/action block.
            mechanisms = [claim['text'].split('；二厂缺少')[0].rstrip('。；') + '。 ' +
                          ' '.join('[' + r + ']' for r in claim['knowledge_ids'])
                          for claim in item['claims'] if claim['knowledge_ids']]
            text = fact + '\n' + gaps[element]
            if mechanisms:
                text += '\n' + ' '.join(mechanisms)
            claims = [claim for claim in claims if claim['knowledge_ids']]
            actions, missing = deepcopy(item['actions']), item['missing_evidence']
        sections.append({'element': element, 'fact': fact, 'text': text, 'claims': deepcopy(item['claims']) if not no_change else [],
                         'actions': actions, 'no_difference': no_change,
                         'evidence_ids': list(dict.fromkeys([ref] + [r for c in claims for r in c['fact_ids'] + c['knowledge_ids']])),
                         'missing_evidence': missing})
        ledger.extend(claims)
    lead = '本节比较期间为' + '、'.join(months) + '，逐月差额和要素结构分别见5.1、5.2。'
    return {'available': True, 'product': product, 'specification': specification, 'months': list(months),
            'text': lead + '\n\n' + '\n\n'.join(row['text'] for row in sections),
            'sections': sections, 'evidence': evidence, 'claim_ledger': ledger,
            'normalized_amount_exact': str(overall), 'periods': deepcopy(benchmark['periods']),
            'used_llm': generation['used_llm'], 'generation_status': generation['generation_status'],
            'fallback_reason': generation['fallback_reason'], 'generation': generation,
            'prose_route': 'legacy-report-claims',
            'prose_note': '跨厂章节仍按逐月标准化金额差的既有报告合同生成；未宣称已采用完整期间数字散文新合同。',
            'knowledge_usage': knowledge_summary(evidence, ledger)}
