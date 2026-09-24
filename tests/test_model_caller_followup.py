"""Offline caller routing/trace/reference contracts; never a real provider or DB."""
from copy import deepcopy
import json
import socket
import sqlite3
from types import SimpleNamespace

import pytest

import attribution_gen as attribution
from attribution_runtime import _partial_audit, sanitize_model_calls
from enterprise import benchmark_ai, model_gateway as gateway
from enterprise.security import Principal
from enterprise.task_workflow import LLM_FIELDS, TaskRepository, _normalise

SECRET = 'offline-caller-fixture-secret'
PRODUCT, SPEC, MONTH = '银黄口服液', '10ml×10支/盒', '2026-05'
ELEMENTS = ('材料', '人工', '制费')


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    import httpx
    def forbidden(*args, **kwargs):
        raise AssertionError('No real sockets or HTTP transports permitted')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', forbidden)
    for name in ('COST_LLM_BASE_URL', 'DASHSCOPE_BASE_URL', 'COST_LLM_MODEL',
                 'COST_LLM_API_KEY', 'DASHSCOPE_API_KEY', 'COST_LLM_TIMEOUT',
                 'COST_LLM_APPROVED_CLOUD'):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / 'caller-config.json'
    path.write_text(json.dumps({'base_url': 'http://127.0.0.1:1/v1', 'api_key': SECRET,
        'model': 'default-alias', 'approved_cloud': False, 'task_routing': {
            'enabled': True,
            'profiles': {task: {'model': task+'-alias', 'timeout': 20} for task in ('attribution', 'benchmark', 'task')},
            'tasks': {task: task for task in ('attribution', 'benchmark', 'task')}}}), encoding='utf-8')
    monkeypatch.setenv('COST_LLM_CONFIG_FILE', str(path))


@pytest.fixture
def provider(monkeypatch):
    import openai
    class FakeProvider:
        calls = []
        responses = []
        error = None
        response_model = 'actual-provider-snapshot'
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=self)
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def create(self, **kwargs):
            self.calls.append(kwargs)
            if self.error:
                raise self.error
            content = self.responses.pop(0)
            if isinstance(content, dict):
                content = json.dumps(content, ensure_ascii=False)
            number = len(self.calls)
            return SimpleNamespace(model=self.response_model, id='completion-'+str(number),
                _request_id='request-'+str(number),
                usage={'prompt_tokens': 11, 'completion_tokens': 7, 'total_tokens': 18,
                       'secret': SECRET, 'prompt': 'do-not-log-provider-prompt',
                       'completion_tokens_details': {'reasoning_tokens': 2, 'body': SECRET}},
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
    monkeypatch.setattr(openai, 'OpenAI', FakeProvider)
    return FakeProvider


def contract(task):
    evidence = [{'id': ('F' if task == 'attribution' else 'B')+str(index),
                 'kind': 'accounting_fact' if task == 'attribution' else 'data_fact',
                 'elements': [element], 'source': {'table': 'synthetic'}, 'text': '核算事实。'}
                for index, element in enumerate(ELEMENTS)]
    candidate = {'elements': {element: {
        'hypothesis': '现有核算差异尚不能确认实际经营原因，需要配对原始业务凭证核查。',
        'recommendation': '财务部应核对原始归集凭证与业务记录，形成同口径差异核对表。',
        'evidence_ids': [source['id']],
        **({'claim_type': 'hypothesis', 'missing_evidence': ['配对的原始归集凭证']}
           if task == 'benchmark' else {})} for element, source in zip(ELEMENTS, evidence)}}
    facts = ({'elements': [{'element': element, 'unit_gap': 1, 'evidence_id': source['id']}
                          for element, source in zip(ELEMENTS, evidence)]}
             if task == 'benchmark' else {'elements': {element: {'evidence_ids': [source['id']]}
                          for element, source in zip(ELEMENTS, evidence)}})
    payload = {'product': PRODUCT, 'specification': SPEC, 'month': MONTH, 'facts': facts}
    if task == 'benchmark':
        payload['schema_version'] = benchmark_ai.SCHEMA_VERSION
    return payload, evidence, candidate


def invoke(task, **kwargs):
    payload, evidence, _ = contract(task)
    # M3 uses the same attribution worker dispatch as production.
    return attribution._llm_generate(payload, evidence, **kwargs)


@pytest.mark.parametrize('task', ['attribution', 'benchmark'])
def test_actual_routed_provider_trace_survives_bounded_correction(task, provider):
    _, _, candidate = contract(task)
    rejected = deepcopy(candidate)
    rejected['elements']['材料']['hypothesis'] += '3%'
    provider.responses = [rejected, candidate]
    progress = []
    result = invoke(task, attempt_recorder=progress.append)
    assert type(result) is dict and type(result['candidate']) is dict
    assert result['candidate'] == candidate and set(result['candidate']) == {'elements'}
    assert [call['model'] for call in provider.calls] == [task+'-alias']*2
    assert [row['status'] for row in result['attempts']] == ['rejected', 'validated']
    assert result['correction'] == {'attempted': True, 'status': 'validated'}
    assert len(result['model_calls']) == 2
    for number, attempt in enumerate(result['attempts'], 1):
        trace, = attempt['model_calls']
        assert trace['task'] == task and trace['requested_model'] == task+'-alias'
        assert trace['model'] == 'actual-provider-snapshot' and trace['response_observed']
        assert trace['request_id'] == 'request-'+str(number)
        assert trace['completion_id'] == 'completion-'+str(number)
        assert trace['usage'] == {'prompt_tokens': 11, 'completion_tokens': 7, 'total_tokens': 18,
                                  'completion_tokens_details': {'reasoning_tokens': 2}}
        assert trace['duration_seconds'] >= 0
    assert result['model_calls'][0]['call_id'] != result['model_calls'][1]['call_id']
    assert progress[-1]['model_calls'] == result['model_calls']
    audit = json.dumps(progress, ensure_ascii=False)
    assert SECRET not in audit and 'do-not-log-provider-prompt' not in audit
    assert all('candidate' not in item and 'instruction' not in item for item in progress)
    assert attribution.M2_INSTRUCTION not in audit and benchmark_ai.BENCHMARK_PROMPT not in audit


@pytest.mark.parametrize('task', ['attribution', 'benchmark'])
@pytest.mark.parametrize('failure', ['provider', 'json'])
def test_failed_provider_and_invalid_json_keep_actual_trace_without_retry(task, failure, provider):
    if failure == 'provider':
        provider.error = RuntimeError(SECRET+' full prompt https://private.invalid')
        provider.error.request_id = 'request-error'
    else:
        provider.responses = ['[]']
    result = invoke(task)
    assert len(provider.calls) == 1 and len(result['attempts']) == 1
    assert result['failure_type'] == 'ModelUnavailable'
    trace, = result['attempts'][0]['model_calls']
    assert trace['requested_model'] == task+'-alias'
    assert trace['model'] == ('actual-provider-snapshot' if failure == 'json' else None)
    assert trace['response_observed'] is (failure == 'json')
    assert trace['status'] == ('invalid_response' if failure == 'json' else 'unavailable')
    assert SECRET not in json.dumps(result) and 'private.invalid' not in json.dumps(result)


@pytest.mark.parametrize('task', ['attribution', 'benchmark'])
def test_legacy_request_function_signature_and_no_fabricated_observation(task):
    _, _, candidate = contract(task)
    configs = []
    def request(instruction, data, *, max_tokens, config):
        configs.append(config)
        return deepcopy(candidate)
    legacy = gateway.ModelConfiguration('https://fixture.invalid/v1', 'legacy', SECRET, True, 10)
    result = invoke(task, config=legacy, request_fn=request)
    assert len(configs) == 1 and configs[0].task == task
    assert legacy.task is None and result['candidate'] == candidate
    assert result['attempts'][0]['model_calls'] == [] and result['model_calls'] == []


@pytest.mark.parametrize('task', ['attribution', 'benchmark'])
def test_explicit_injected_request_wrapper_can_capture_actual_gateway(task, provider):
    provider.responses = [contract(task)[2]]
    def request(instruction, data, *, max_tokens, config):
        return gateway.generate_json(instruction, data, max_tokens=max_tokens, config=config)
    result = invoke(task, request_fn=request)
    assert result['model_calls'][0]['task'] == task
    assert result['model_calls'][0]['model'] == 'actual-provider-snapshot'


def test_partial_audit_projects_nested_model_calls_and_retains_completed_trace(provider, tmp_path):
    provider.responses = [{}]
    with gateway.capture_model_calls() as calls:
        gateway.generate_json('private system prompt', {'body': 'private data'}, task='attribution')
    trace = deepcopy(calls[0])
    trace.update(prompt='private system prompt', api_key=SECRET, provider_object={'raw': SECRET},
                 model='Bearer '+SECRET, duration_seconds=float('nan'))
    trace['usage']['prompt'] = SECRET
    trace['usage']['completion_tokens_details']['raw'] = SECRET
    path = tmp_path / 'partial.json'
    path.write_text(json.dumps({'model_run': {'attempts': [
        {'attempt': 1, 'status': 'rejected', 'model_calls': [trace], 'used': True, 'raw': SECRET},
        {'attempt': 2, 'status': 'running', 'model_calls': [], 'used': False}],
        'correction': {'attempted': True, 'status': 'running', 'raw': SECRET},
        'candidate': {'secret': SECRET}}}), encoding='utf-8')
    result = _partial_audit(path)
    assert result['attempts'][1]['status'] == result['correction']['status'] == 'interrupted'
    assert all(not row['used'] for row in result['attempts'])
    safe, = result['attempts'][0]['model_calls']
    assert safe['model'] is None and safe['duration_seconds'] is None
    assert safe['request_id'] == 'request-1' and safe['usage']['total_tokens'] == 18
    assert result['model_calls'] == [safe]
    encoded = json.dumps(result, allow_nan=False)
    assert SECRET not in encoded and 'private system prompt' not in encoded
    assert 'provider_object' not in encoded and 'candidate' not in result
    assert sanitize_model_calls([{'model': 'fabricated'}, None, 'raw']) == []
    assert sanitize_model_calls({'model': 'fabricated'}) == []


def references():
    return [
        {'id': 'Kindustry', 'kind': 'industry_reference', 'support_status': 'eligible',
         'elements': list(ELEMENTS), 'text': '原料损耗增加会导致材料成本增加。',
         'source': {'file': 'synthetic-reference.csv'}, 'claim_boundary': 'reference only',
         'table_row': {'columns': {'产品类别': '口服液类', '指标': '单位成本'}}},
        {'id': 'Kmarket', 'kind': 'market_reference', 'support_status': 'eligible',
         'elements': ['材料'], 'text': '采购提价影响材料成本。', 'source': {'file': 'market.csv'},
         'market_observations': [{'month': MONTH, 'material': '金银花', 'grade': '统货', 'unit': '元/公斤',
                                  'direction': '上涨', 'boundary': '不是本厂采购价'},
                                 {'month': '2026-06', 'material': '未来资料', 'grade': '统货', 'unit': '元/公斤',
                                  'direction': '下降', 'boundary': '未来数据不应投影'}]}]


@pytest.mark.parametrize('task', ['attribution', 'benchmark'])
def test_reference_context_never_enters_document_quotes_or_causal_ids(task):
    from attribution_narrative import cited_quote
    payload, evidence, candidate = contract(task)
    original = deepcopy(references())
    evidence += original
    grouped = (attribution._model_context(payload, evidence) if task == 'attribution'
               else benchmark_ai.grouped_model_context(payload, evidence))
    for element, item in grouped['tasks_by_element'].items():
        assert item['references'] and item['references'][0]['id'] == 'Kindustry'
        assert not {'Kindustry', 'Kmarket'}.intersection(item['eligible_evidence_ids'])
        assert item['document_basis'] == []
        assert cited_quote(original, ['Kindustry', 'Kmarket'], element) is None
    assert len(grouped['tasks_by_element']['材料']['references']) == 2
    assert '未来' not in json.dumps(grouped, ensure_ascii=False)
    candidate['elements']['材料']['evidence_ids'].append('Kindustry')
    errors = (attribution._model_errors(candidate, evidence) if task == 'attribution'
              else benchmark_ai.validate_explanations(candidate, evidence, payload['facts']))
    assert errors
    assert original == references()


def draft():
    return {'task_title': '核查成本差异', 'assignee': {'name': '测试责任人', 'department': '财务部', 'role': '会计'},
            'source': {'analysis_type': '跨厂对标', 'analysis_month': MONTH, 'product': PRODUCT,
                       'finding': '成本差异尚待核对原始归集凭证'},
            'priority': 'medium', 'deadline': '2026-10-01', 'factories': ['中药一厂'],
            'evidence_ids': ['F0'], 'analysis_run_id': 'synthetic-analysis'}


def actor():
    return Principal('caller-test', '测试', ('analyst',), ('中药一厂',), (PRODUCT,))


def test_task_capture_uses_actual_task_route_and_keeps_only_draft_side_effect(provider, tmp_path):
    content = _normalise(draft())
    provider.responses = [{key: content[key] for key in LLM_FIELDS}]
    repo = TaskRepository(tmp_path / 'new-test-database')
    result = repo.generate(draft(), actor=actor())
    assert result['generation']['mode'] == 'ai'
    trace, = result['generation']['model_calls']
    assert trace['task'] == 'task' and trace['requested_model'] == 'task-alias'
    assert trace['model'] == 'actual-provider-snapshot'
    assert result['generation']['model_provenance'] == trace
    assert result['workflow_status'] == 'draft' and result['dispatch_status'] == 'not_sent'
    assert result['outbox'] is None
    with sqlite3.connect(repo.db) as con:
        assert con.execute('select count(*) from tasks').fetchone()[0] == 1
        assert con.execute('select count(*) from outbox').fetchone()[0] == 0
    assert SECRET not in json.dumps(result, ensure_ascii=False)


def test_task_failure_capture_and_injected_single_prompt_contract(provider, tmp_path):
    repo = TaskRepository(tmp_path / 'new-test-database')
    provider.error = RuntimeError(SECRET+' private prompt')
    result = repo.generate(draft(), actor=actor())
    assert result['generation']['mode'] == 'rule_fallback'
    assert result['generation']['model_calls'][0]['status'] == 'unavailable'
    assert len(provider.calls) == 1 and SECRET not in json.dumps(result)
    prompts = []
    def injected(prompt):
        prompts.append(prompt)
        data = json.loads(prompt.split('\n', 1)[1])
        return {key: data[key] for key in LLM_FIELDS}
    result = repo.generate(draft(), actor=actor(), llm_fn=injected)
    assert len(prompts) == 1 and result['generation']['mode'] == 'ai'
    assert result['generation']['model_calls'] == []
    assert result['generation']['model_provenance']['source'] == 'not_observed'
    assert len(provider.calls) == 1


def test_task_scope_gate_precedes_provider_or_database_write(provider, tmp_path):
    repo = TaskRepository(tmp_path / 'new-test-database')
    outsider = Principal('outside', 'outside', ('analyst',), ('中药二厂',), (PRODUCT,))
    with pytest.raises(PermissionError):
        repo.generate(draft(), actor=outsider)
    assert not provider.calls and not repo.db.exists()


def benchmark_tables():
    import pandas as pd
    rows = []
    for factory, value in [('中药一厂', 2), ('中药二厂', 1)]:
        rows.append({'工厂': factory, '产品名称': PRODUCT, '产品规格': SPEC, '月份': MONTH,
                     '产量(盒)': 10, '直接材料(元/盒)': value, '直接人工(元/盒)': 1,
                     '制造费用(元/盒)': 1, '单位成本(元/盒)': value+2, '总成本(元)': (value+2)*10})
    return {'cost26': pd.DataFrame([rows[0]]), 'erchang26': pd.DataFrame([rows[1]])}


def test_benchmark_industry_result_uses_tables_and_only_admitted_evidence(monkeypatch):
    import enterprise.industry_benchmark as industry
    tables = benchmark_tables()
    allowed = {'id': 'Kallowed', 'kind': 'document_basis', 'text': '已授权的资料。',
               'elements': ['材料'], 'source': {'file': 'synthetic.txt'},
               'scope': {'product': PRODUCT, 'specification': SPEC, 'months': [MONTH]}}
    denied = {**allowed, 'id': 'Kdenied', 'scope': {**allowed['scope'], 'product': 'wrong-product'}}
    observed = []
    def build(product, specification, month, actual_tables, evidence):
        assert actual_tables is tables
        observed.append(deepcopy(evidence))
        return {'available': False, 'synthetic_only': True}
    monkeypatch.setattr(industry, 'build_industry_comparison', build)
    result = benchmark_ai.generate_benchmark_analysis(PRODUCT, SPEC, MONTH, tables,
        evidence=[allowed, denied], use_llm=False)
    assert result['industry_comparison'] == {'available': False, 'synthetic_only': True}
    assert [row['id'] for row in observed[0]] == ['Kallowed']
    assert result['generation_status'] == 'deterministic_requested'


def test_benchmark_analysis_rehydrates_actual_worker_traces(monkeypatch, provider):
    import attribution_runtime
    import enterprise.analysis_service as service
    tables = benchmark_tables()
    facts = benchmark_ai.build_benchmark(PRODUCT, SPEC, MONTH, tables)
    evidence = benchmark_ai._facts_evidence(facts)
    candidate = contract('benchmark')[2]
    for row in facts['elements']:
        if row['unit_gap'] == 0:
            candidate['elements'][row['element']] = benchmark_ai._zero_explanation(row)
            continue
        # A valid synthetic response must state the observed cost direction.
        direction = '高于' if row['unit_gap'] > 0 else '低于'
        offset = ('本项反向抵消其他要素形成的净差额。'
                  if row['unit_gap'] * facts['unit_gap'] < 0 else '')
        candidate['elements'][row['element']].update(
            hypothesis=f"一厂{row['element']}单位费用{direction}二厂。" + offset
                       + '现有核算差异尚不能确认实际经营原因，需要配对原始业务凭证核查。',
            evidence_ids=[row['evidence_id']])
    from enterprise.prose_contract import PROSE_MODE
    payload = {'product': PRODUCT, 'specification': SPEC, 'month': MONTH, 'facts': facts,
               'schema_version': benchmark_ai.SCHEMA_VERSION, 'prose_mode': PROSE_MODE}
    context = benchmark_ai.grouped_model_context(payload, evidence, include_numeric=True)
    for element, item in candidate['elements'].items():
        if item['claim_type'] == 'no_difference':
            continue
        required = [row for row in context['prose_contract']['elements'][element]['statements'] if row['required']]
        assert required
        item['hypothesis'] = ''.join(row['text'] for row in required) + item['hypothesis']
        item['evidence_ids'] = list(dict.fromkeys(item['evidence_ids']
            + [ref for row in required for ref in row['evidence_ids']]))
    before = deepcopy(candidate)
    provider.responses = [candidate]
    worker = benchmark_ai._llm_generate(payload, evidence, clock=lambda: 0.0, deadline=95)
    assert worker['candidate'] == before and worker['attempts'][0]['status'] == 'validated'
    stages = []
    def bounded_stage(stage, args, timeout):
        stages.append((stage, timeout))
        assert args[0]['prose_mode'] == PROSE_MODE
        return deepcopy(worker)
    monkeypatch.setattr(attribution_runtime, 'run_stage', bounded_stage)
    result = benchmark_ai.generate_benchmark_analysis(PRODUCT, SPEC, MONTH, tables,
                                                     model_fn=service.validated_model)
    assert stages == [('model', 95)] and len(provider.calls) == 1
    assert result['model_run']['hard_budget_seconds'] == 95
    assert result['used_llm'] and result['model_run']['execution'] == 'bounded_worker'
    assert result['model_explanations'] == before and candidate == before
    assert result['model_run']['model_calls'] == worker['model_calls']
    trace, = result['model_run']['model_calls']
    assert trace['task'] == 'benchmark' and trace['requested_model'] == 'benchmark-alias'
    assert trace['model'] == 'actual-provider-snapshot' and trace['response_observed']
    assert trace['request_id'] == 'request-1' and trace['completion_id'] == 'completion-1'
    assert trace['usage']['total_tokens'] == 18 and SECRET not in json.dumps(trace)


def test_partial_worker_deadline_preserves_completed_trace_without_provider_call(monkeypatch):
    import attribution_runtime
    monkeypatch.setenv('ATTRIBUTION_RUNTIME_TEST_MODE', '1')
    trace = {'schema': 'enterprise-model-call/1.0', 'task': 'attribution',
             'model': 'prior-snapshot', 'requested_model': 'alias', 'status': 'returned_json',
             'source': 'provider_response', 'response_observed': True, 'request_attempted': True,
             'request_id': 'prior-request', 'usage': {'total_tokens': 18}}
    with pytest.raises(attribution_runtime.StageExecutionError) as caught:
        attribution_runtime.run_stage('model', {'_test_model_run': {'attempts': [
            {'attempt': 1, 'status': 'rejected', 'model_calls': [trace]},
            {'attempt': 2, 'status': 'running', 'model_calls': []}],
            'correction': {'attempted': True, 'status': 'running'}}, '_test_sleep': 30}, timeout=1.5)
    audit = caught.value.model_run
    assert caught.value.timed_out and audit['attempts'][1]['status'] == 'interrupted'
    assert audit['model_calls'][0]['model'] == 'prior-snapshot'
    assert audit['model_calls'][0]['request_id'] == 'prior-request'
    assert audit['attempts'][1]['model_calls'] == []


def test_m2_partitioned_adapter_preserves_reference_shapes_and_actual_facts(monkeypatch, provider):
    import attribution_narrative
    import enterprise.analysis_service as service
    import enterprise.knowledge_context as legacy
    import enterprise.snapshots as snapshots
    payload, evidence, candidate = contract('attribution')
    payload.update({'elements': {}, '告警_环比超正负10%': [], '数据限制': 'synthetic-only'})
    payload['facts'].update(available=True, evidence=evidence,
        current={'source': {'key': {'产品规格': SPEC}}})
    selected = references()
    observed = []
    def retrieve(principal, product, specification, months, root, *, facts):
        observed.append((principal, product, specification, months, facts))
        return service.EvidenceList(deepcopy(selected), {'retrieval_mode': 'typed_partitioned',
            'degraded': False, 'release_id': 'synthetic-release'})
    monkeypatch.setattr(service, 'report_evidence', retrieve)
    monkeypatch.setattr(legacy, 'context', lambda *a, **k: pytest.fail('legacy retrieval must not run'))
    monkeypatch.setattr(snapshots, 'current_provenance', lambda: {})
    monkeypatch.setattr(attribution, 'build_dashboard_data', lambda *a: {})
    monkeypatch.setattr(attribution, 'build_attribution_payload', lambda *a, **k: deepcopy(payload))
    monkeypatch.setattr(attribution, 'render_report', lambda *a: 'synthetic deterministic report')
    monkeypatch.setattr(attribution_narrative, 'render', lambda *a: ('overview', [], 'text'))
    provider.responses = [candidate]
    worker = attribution._llm_generate(payload, evidence)
    monkeypatch.setattr(attribution, '_execute_stage', lambda *a: deepcopy(worker))
    principal = actor()
    result = attribution.generate_attribution(PRODUCT, MONTH, d={}, principal=principal)
    assert len(observed) == 1 and observed[0][0] is principal
    assert observed[0][2:4] == (SPEC, [MONTH]) and observed[0][4] == payload['facts']
    assert result['sources'][-2:] == selected
    assert result['retrieval_stats']['retrieval_mode'] == 'typed_partitioned'
    assert result['index_release_id'] == 'synthetic-release'
    assert result['generation_status'] == 'model_validated'
    assert len(provider.calls) == 1
    assert result['model_run']['model_calls'] == worker['model_calls']
    assert result['model_run']['attempts'][0]['model_calls'][0]['model'] == 'actual-provider-snapshot'
    assert result['reference_evidence'] == selected
    assert result['reference_context']['材料'][1]['id'] == 'Kmarket'


# Real-original/no-key/revocation cases live in test_industry_followup.py;
# this caller suite remains self-contained in the source-only distribution.
