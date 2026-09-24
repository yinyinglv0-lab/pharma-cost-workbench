"""Offline empty-projection regression tests with public synthetic accounting data."""
from copy import deepcopy
import socket

import pytest

from tests.test_round2_prose_integration import case, validate
from attribution_gen import _model_context
from enterprise.benchmark_ai import grouped_model_context
from enterprise.prose_contract import prose_residual


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: (_ for _ in ()).throw(AssertionError('offline test')))


def quoted_case(mode, industry='machinery'):
    payload, existing, _, candidate = case(mode, industry)
    sources = deepcopy(existing)
    texts = {
        '材料': '材料费用应按同口径产量归集，核查周期为2天，原料成本不能代替实际耗用。',
        '人工': '人工费用应按工时归集，核查周期为2天，归集比率不能代替个人工资。',
        '制费': '制造费用应按同口径产量分配，核查周期为2天，费用归集不证明设备事件。',
    }
    for index, (element, text) in enumerate(texts.items()):
        sources.append({'id': 'KProjection' + str(index), 'kind': 'document_basis',
                        'evidence_role': 'document_basis', 'elements': [element],
                        'support_status': 'eligible', 'text': text,
                        'source': {'file': 'SIMULATION_projection_knowledge.txt'},
                        'scope': {'product': payload['product'], 'specification': payload['specification'],
                                  'months': [payload['month']]}})
    builder = _model_context if mode == 'attribution' else grouped_model_context
    context = builder(payload, sources, include_numeric=True)
    for element, section in context['prose_contract']['elements'].items():
        statements = section['statements']
        assert any(item['kind'] == 'document_quote' for item in statements)
        row = candidate['elements'][element]
        row['hypothesis'] = '\n'.join(item['text'] for item in statements)
        row['evidence_ids'] = list(dict.fromkeys(ref for item in statements for ref in item['evidence_ids']))
        row['evidence_ids'] += [ref for ref in context['tasks_by_element'][element].get('focus', {}).get('tied_evidence_ids', [])
                                if ref not in row['evidence_ids']]
        assert not prose_residual(row['hypothesis'], element, context['prose_contract']).strip()
    return payload, sources, context, candidate


@pytest.mark.parametrize('mode', ['attribution', 'benchmark'])
@pytest.mark.parametrize('industry', ['pharma', 'machinery', 'auto_parts', 'chemicals', 'electronics'])
def test_complete_admitted_quotes_need_no_invented_free_text(mode, industry):
    payload, sources, context, candidate = quoted_case(mode, industry)
    before = deepcopy((payload, sources, context, candidate))
    errors = validate(mode, candidate, sources, payload, context)
    assert not errors, errors
    assert (payload, sources, context, candidate) == before


@pytest.mark.parametrize('mode', ['attribution', 'benchmark'])
@pytest.mark.parametrize('fault', [
    'no_document_quote', 'no_document_citation', 'unknown_document_source',
    'wrong_source_kind', 'cross_element_source', 'ineligible_source', 'context_only_source',
    'wrong_product_scope', 'wrong_period_scope', 'not_available_document',
    'not_task_eligible', 'not_task_document', 'changed_task_excerpt', 'not_task_bound',
    'altered_quote', 'missing_quote_boundary', 'negative_prefix', 'duplicate_quote',
    'nonempty_affirmative_tail', 'numeric_tail',
])
def test_empty_projection_exception_requires_exact_admitted_document(mode, fault):
    payload, sources, context, candidate = quoted_case(mode)
    element = '材料'
    row = candidate['elements'][element]
    section = context['prose_contract']['elements'][element]
    quote = next(item for item in section['statements'] if item['kind'] == 'document_quote')
    ident = quote['evidence_ids'][0]
    source = next(item for item in sources if item['id'] == ident)
    task = context['tasks_by_element'][element]
    if fault == 'no_document_quote':
        row['hypothesis'] = row['hypothesis'].replace(quote['text'], '')
    elif fault == 'no_document_citation':
        row['evidence_ids'].remove(ident)
    elif fault == 'unknown_document_source':
        sources.remove(source)
    elif fault == 'wrong_source_kind':
        source['kind'] = 'market_reference'
    elif fault == 'cross_element_source':
        source['elements'] = ['人工']
    elif fault == 'ineligible_source':
        source['support_status'] = 'ineligible'
    elif fault == 'context_only_source':
        source['evidence_role'] = 'context_only'
    elif fault == 'wrong_product_scope':
        source['scope']['product'] = 'SIMULATION unrelated product'
    elif fault == 'wrong_period_scope':
        source['scope']['months'] = ['1900-01']
    elif fault == 'not_available_document':
        task['available_document_ids'] = []
    elif fault == 'not_task_eligible':
        task['eligible_evidence_ids'].remove(ident)
    elif fault == 'not_task_document':
        task['document_basis'] = [item for item in task['document_basis'] if item['id'] != ident]
    elif fault == 'changed_task_excerpt':
        document = next(item for item in task['document_basis'] if item['id'] == ident)
        document['untrusted_excerpt' if mode == 'attribution' else 'text'] = '其他要素的无关解释。'
    elif fault == 'not_task_bound':
        task['bound_numeric_statements'] = [item for item in task['bound_numeric_statements'] if item['id'] != quote['id']]
    elif fault == 'altered_quote':
        row['hypothesis'] = row['hypothesis'].replace('材料费用应按', '材料费用已经按')
    elif fault == 'missing_quote_boundary':
        row['hypothesis'] = row['hypothesis'].replace(quote['text'], quote['text'].split('该原文')[0])
    elif fault == 'negative_prefix':
        row['hypothesis'] = row['hypothesis'].replace(quote['text'], '并非' + quote['text'])
    elif fault == 'duplicate_quote':
        row['hypothesis'] += '\n' + quote['text']
    elif fault == 'nonempty_affirmative_tail':
        row['hypothesis'] += '\n已确认本期设备故障导致原料损失，主要原因是采购价格上涨。'
    elif fault == 'numeric_tail':
        row['hypothesis'] += '\n差额为999999元。'
    before = deepcopy(candidate)
    errors = validate(mode, candidate, sources, payload, context)
    assert errors, fault
    assert candidate == before
    if fault in {'no_document_quote', 'not_task_document', 'changed_task_excerpt', 'not_task_bound'}:
        assert {'PROSE_LENGTH', 'UNCERTAINTY_REQUIRED'} <= {item['rule_id'] for item in errors}
    if fault == 'nonempty_affirmative_tail':
        assert any(item['rule_id'] in {'UNVERIFIED_CONCLUSION', 'MECHANISM_DOCUMENT'} for item in errors)
    if fault == 'numeric_tail':
        assert any(item['rule_id'] == 'BOUND_NUMERIC_OUTSIDE_STATEMENT' for item in errors)


@pytest.mark.parametrize('mode', ['attribution', 'benchmark'])
def test_no_knowledge_generic_accounting_or_comparison_boundary_is_insufficient(mode):
    payload, sources, context, candidate = case(mode)
    for element, row in candidate['elements'].items():
        row['hypothesis'] = '\n'.join(item['text'] for item in context['prose_contract']['elements'][element]['statements'])
    errors = validate(mode, candidate, sources, payload, context)
    assert {'PROSE_LENGTH', 'UNCERTAINTY_REQUIRED'} <= {item['rule_id'] for item in errors}


@pytest.mark.parametrize('mode', ['attribution', 'benchmark'])
def test_complete_quote_does_not_license_unknown_voucher_or_hide_its_rejection(mode):
    payload, sources, context, candidate = quoted_case(mode)
    candidate['elements']['人工']['recommendation'] = '建议财务部核查设备检修单，核对已给人工归集金额并形成核对表。'
    errors = validate(mode, candidate, sources, payload, context)
    assert {item['rule_id'] for item in errors} == {'RECORD_NAME_SCOPE'}
    assert all(item['field'] == 'elements.人工.recommendation' for item in errors)


@pytest.mark.parametrize('mode', ['attribution', 'benchmark'])
def test_empty_projection_never_disables_original_observation_alignment(mode):
    payload, sources, context, candidate = quoted_case(mode)
    task = context['tasks_by_element']['材料']
    if mode == 'attribution':
        task['focus'] = {'is_tied': True, 'tied_objects': ['未出现的并列原料'], 'tied_evidence_ids': []}
        expected = 'TIED_FOCUS'
    else:
        task['required_observations']['home_vs_peer'] = '低于'
        expected = 'COMPARISON_DIRECTION'
    errors = validate(mode, candidate, sources, payload, context)
    assert any(item['rule_id'] == expected for item in errors), errors


@pytest.mark.parametrize('mode', ['attribution', 'benchmark'])
def test_frozen_legacy_mode_still_rejects_original_numeric_hypotheses(mode):
    payload, sources, _, candidate = quoted_case(mode)
    payload.pop('prose_mode')
    builder = _model_context if mode == 'attribution' else grouped_model_context
    legacy = builder(payload, sources, include_numeric=True)
    legacy.pop('prose_mode', None)
    legacy.pop('prose_contract', None)
    errors = validate(mode, candidate, sources, payload, legacy)
    assert errors  # Legacy may reject the original long text before checking numbers.
    candidate['elements']['材料']['hypothesis'] = '材料费用差异为12元，现有核算数据尚不能证明采购计价或实际耗用原因。'
    errors = validate(mode, candidate, sources, payload, legacy)
    assert any(item['rule_id'] == 'NO_NUMERIC_IN_PROSE' for item in errors)
