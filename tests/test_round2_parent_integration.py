"""Small offline integration regressions at parent-owned application boundaries."""
from copy import deepcopy
import json
from pathlib import Path

from enterprise.analysis_context import _units
from enterprise.analysis_contract import domain_descriptors

ROOT = Path(__file__).resolve().parents[1]


def test_model_unit_descriptor_uses_validated_product_unit_and_rejects_mixed_units():
    import pytest
    from enterprise.domain_profiles import DomainProfileError
    profile = json.loads((ROOT / 'config/manufacturing_examples/machinery/domain.json').read_text(encoding='utf-8'))
    product = profile['products'][0]
    payload = {'domain_config': profile, 'product': product['name'], 'specification': product['specification']}
    assert _units(payload)['quantity'] == product['reporting_unit'] == 'piece'
    assert domain_descriptors(payload)['reporting_unit'] == product['reporting_unit']
    # Current canonical schema deliberately requires separate profiles for units.
    profile['reporting_unit'] = 'kg'
    with pytest.raises(DomainProfileError, match='mixed reporting units'):
        _units(payload)
    with pytest.raises(DomainProfileError, match='mixed reporting units'):
        domain_descriptors(payload)


def test_partial_worker_audit_preserves_structured_feedback(tmp_path):
    from attribution_runtime import _partial_audit
    diagnostics = [{'rule_id': 'NO_NUMERIC_IN_PROSE', 'field': 'elements.制费.hypothesis',
                    'offending': ['三项'], 'expected': '用并列对象表述', 'message': '正文不写数值'}]
    path = tmp_path / 'worker.json'
    path.write_text(json.dumps({'model_run': {'attempts': [{'attempt': 1, 'status': 'rejected',
        'used': True, 'validation_diagnostics': diagnostics, 'model_calls': []}],
        'correction': {'attempted': True, 'status': 'running'}}}, ensure_ascii=False), encoding='utf-8')
    recovered = _partial_audit(path)
    assert recovered['attempts'][0]['validation_diagnostics'] == diagnostics
    assert recovered['attempts'][0]['used'] is False
    assert recovered['correction']['status'] == 'interrupted'


def test_application_manufacturing_facade_preserves_identity_and_root(tmp_path):
    from enterprise.application import Application
    from enterprise.security import Principal
    principal = Principal('analyst-test', 'SIMULATION analyst', ('analyst',), ('*',), ('*',))
    app = Application(principal, root=tmp_path)
    service = app.manufacturing()
    assert service.root == tmp_path
    assert service.principal is principal
    assert service.repository.principal is principal
    assert not service.db.exists() and not service.repository.db.exists()


def test_m2_api_result_exports_single_common_criteria_without_hidden_duplication(monkeypatch):
    import attribution_gen
    from enterprise.analysis_narrative import common_action_criteria
    from tests.test_round2_manufacturing_projection import analyze
    _, _, projected = analyze()
    payload = projected['attribution_payload']
    # This explicitly injected payload is a test double for the legacy data step;
    # rendering and API result assembly remain the real production functions.
    payload['告警_环比超正负10%'] = []
    payload['数据限制'] = 'SIMULATION injected data-step fixture; no production dataset or model.'
    payload['facts']['evidence'] = deepcopy(projected['sources']['attribution'])
    for row in payload.get('elements', {}).values():
        row['evidence'] = []
    monkeypatch.setattr(attribution_gen, 'build_dashboard_data', lambda *a, **kw: {})
    monkeypatch.setattr(attribution_gen, 'build_attribution_payload', lambda *a, **kw: deepcopy(payload))
    monkeypatch.setattr('enterprise.cost_imports.records', lambda tables: {})
    monkeypatch.setattr('enterprise.snapshots.current_provenance', lambda: {'test_double': True})
    monkeypatch.setattr('enterprise.industry_benchmark.build_industry_comparison',
                        lambda *a, **kw: {'available': False, 'rows': [], 'reason': 'test_no_references'})
    result = attribution_gen.generate_attribution(payload['product'], payload['month'], use_llm=False, d={})
    assert result['followup_criteria'] == common_action_criteria()
    assert result['narrative_schema'] == 'grounded-analysis-narrative/2.1'
    assert all(row['reading_style'] == 'contextual-reading/1.4' for row in result['sections'])
    assert result['text'].count(common_action_criteria()) == 1
    assert result['concise_text'].count(common_action_criteria()) == 1
    assert result['model_run']['provider_call_count'] == 0
