"""Validate new bounded numeric prose without weakening frozen legacy contracts.

Only the server-built context enables this branch. The original model value is
never rewritten; a separate residual projection is used exclusively for the
existing business/causal/action validators. Reference-only IDs may license exact
reference sentences, never an unqualified business-cause claim.
"""
from __future__ import annotations

from copy import deepcopy


def _has_complete_admitted_document_quote(text, element, refs, contract, context, by_id, *, mode):
    """Verify an actually used K quote and its full server-written boundary.

    A generic accounting/comparison caveat is insufficient. Source eligibility,
    the admitted task excerpt and the exact trusted narrative must all agree.
    """
    from attribution_narrative import cited_quote
    from enterprise.analysis_narrative import _mechanism_text, _scope_matches
    from enterprise.prose_contract import _clean_statement, used_statement_ids

    if not isinstance(text, str) or len(text.strip()) < 15:
        return False
    if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) for ref in refs):
        return False
    task = context.get('tasks_by_element', {}).get(element)
    if not isinstance(task, dict):
        return False
    admitted = task.get('document_basis')
    eligible = task.get('eligible_evidence_ids')
    bound = task.get('bound_numeric_statements')
    if not all(isinstance(value, list) for value in (admitted, eligible, bound)):
        return False
    used = set(used_statement_ids(text, element, contract))
    statements = contract.get('elements', {}).get(element, {}).get('statements', [])
    for statement in statements:
        if (not isinstance(statement, dict) or statement.get('id') not in used
                or statement.get('kind') != 'document_quote' or statement not in bound):
            continue
        quote_refs = statement.get('evidence_ids')
        if not isinstance(quote_refs, list) or len(quote_refs) != 1:
            continue
        ref = quote_refs[0]
        if (not isinstance(ref, str) or not ref.startswith('K') or ref not in refs
                or ref not in eligible):
            continue
        if 'available_document_ids' in task and ref not in task['available_document_ids']:
            continue
        source = by_id.get(ref)
        if (not isinstance(source, dict) or source.get('kind') != 'document_basis'
                or element not in source.get('elements', [])
                or source.get('support_status', 'eligible') != 'eligible'
                or source.get('evidence_role', 'document_basis') != 'document_basis'):
            continue
        scope = contract.get('elements', {}).get(element, {}).get('scope', {})
        if not _scope_matches(source, scope):
            continue
        entries = [entry for entry in admitted if isinstance(entry, dict) and entry.get('id') == ref]
        if len(entries) != 1:
            continue
        quote = cited_quote([source], [ref], element)
        if not quote or entries[0].get('untrusted_excerpt', entries[0].get('text')) != quote['quote']:
            continue
        expected = _clean_statement(_mechanism_text(quote, benchmark=mode == 'benchmark'), [ref])
        if expected and statement.get('text') == expected['text']:
            return True
    return False


def validate_bound_prose(candidate, sources, facts, context, legacy_validator, *, mode):
    from enterprise.analysis_contract import diagnostic, observation_diagnostics
    from enterprise.prose_contract import (PROSE_MODE, numeric_prose_diagnostics,
        prose_residual, used_statement_evidence_ids)

    if not isinstance(candidate, dict) or set(candidate) != {'elements'} or not isinstance(candidate.get('elements'), dict):
        return legacy_validator(candidate, sources, facts, context=None)
    contract = context.get('prose_contract')
    if not isinstance(contract, dict) or contract.get('schema_version') != PROSE_MODE or contract.get('mode') != mode:
        return [diagnostic('PROSE_CONTRACT_REQUIRED', 'prose_contract', None,
                           '服务器构建的同任务数字散文合同', '缺少有效的数字散文绑定，不能接纳数字正文')]
    legacy_context = deepcopy(context)
    legacy_context.pop('prose_mode', None)
    legacy_context.pop('prose_contract', None)
    for task in legacy_context.get('tasks_by_element', {}).values():
        # Full original prose, not the residual, owns observed-object alignment.
        for key in ('focus', 'observed_labor', 'required_observations', 'bound_numeric_statements'):
            task.pop(key, None)
    residual_candidate = deepcopy(candidate)
    errors = []
    empty_document_fields = set()
    by_id = {row.get('id'): row for row in sources if isinstance(row, dict)}
    for element, row in candidate['elements'].items():
        expected = {'hypothesis', 'recommendation', 'evidence_ids'}
        if mode == 'benchmark':
            expected |= {'claim_type', 'missing_evidence'}
        if not isinstance(row, dict) or set(row) != expected:
            continue  # Existing validator owns strict shape and exact key coverage.
        if mode == 'benchmark' and row.get('claim_type') == 'no_difference':
            continue  # Existing validator enforces the complete immutable branch.
        refs = row.get('evidence_ids')
        for field in ('hypothesis', 'recommendation'):
            text = row.get(field)
            field_path = f'elements.{element}.{field}'
            numeric_errors = numeric_prose_diagnostics(text, element, contract, refs, field=field_path)
            errors.extend(numeric_errors)
            if isinstance(text, str) and field == 'hypothesis':
                residual = prose_residual(text, element, contract)
                residual_candidate['elements'][element][field] = residual
                if (not numeric_errors and not residual.strip()
                        and _has_complete_admitted_document_quote(
                            text, element, refs, contract, context, by_id, mode=mode)):
                    empty_document_fields.add(field_path)
        if mode == 'benchmark' and isinstance(row.get('missing_evidence'), list):
            for index, text in enumerate(row['missing_evidence']):
                errors.extend(numeric_prose_diagnostics(text, element, contract, refs,
                    field=f'elements.{element}.missing_evidence[{index}]'))
        used = set(used_statement_evidence_ids(row.get('hypothesis', ''), element, contract))
        if isinstance(refs, list) and all(isinstance(ref, str) for ref in refs):
            if len(refs) != len(set(refs)):
                errors.append(diagnostic('EVIDENCE_UNIQUE', f'elements.{element}.evidence_ids',
                    refs, '非空且无重复的证据ID', element + '引用重复，参考句不能豁免引用唯一性'))
            # Keep all accounting and K-mechanism citations for normal validation.
            # A typed market/industry ref is admitted only for its intact bound
            # reference statement. It cannot satisfy a mechanism citation rule.
            reference_only = []
            for ref in refs:
                source = by_id.get(ref) or {}
                if source.get('kind') not in ('market_reference', 'industry_reference'):
                    continue
                if (ref not in used or source.get('support_status', 'eligible') != 'eligible'
                        or source.get('evidence_role') == 'context_only'):
                    errors.append(diagnostic('REFERENCE_SENTENCE_ONLY',
                        f'elements.{element}.evidence_ids', ref,
                        '仅引用本项完整参考句所绑定且获准的ID，不能作为本厂原因证据',
                        element + '参考ID没有绑定到实际采用的完整参考句'))
                else:
                    reference_only.append(ref)
            residual_candidate['elements'][element]['evidence_ids'] = [
                ref for ref in refs if ref not in reference_only]
    legacy_errors = legacy_validator(residual_candidate, sources, facts, context=legacy_context)
    observation_errors = observation_diagnostics(candidate, context)
    # An empty validation projection is not an empty original hypothesis when a
    # complete, eligible document quote already supplies the mechanism boundary.
    # Preserve every other rejection, including errors in recommendations. Only
    # fields with no independent failures may waive these two projection errors.
    projection_rules = {'PROSE_LENGTH', 'UNCERTAINTY_REQUIRED'}
    all_errors = [*errors, *legacy_errors, *observation_errors]
    qualified = set()
    for field in empty_document_fields:
        element_path = field.rsplit('.', 1)[0]
        if not any((item.get('field') in {element_path, field, element_path + '.evidence_ids'}
                    or item.get('field', '').startswith(element_path + '.evidence_ids[')
                    or not item.get('field', '').startswith('elements.'))
                   and not (item.get('field') == field and item.get('rule_id') in projection_rules)
                   for item in all_errors):
            qualified.add(field)
    errors.extend(item for item in legacy_errors
                  if not (item.get('field') in qualified and item.get('rule_id') in projection_rules))
    errors.extend(observation_errors)
    return errors
