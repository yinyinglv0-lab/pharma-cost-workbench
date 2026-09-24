"""Real analytical facts reach the model without raw notes or invented detail."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
import attribution_gen as ag
from enterprise import benchmark_ai as bg
from enterprise.model_gateway import ModelConfiguration


def m2_input():
    fields={'unit_before':7.08,'unit_after':7.30,'unit_delta':.22,'amount_delta':34000,
            'contribution':67.09,'volume_effect':21240,'unit_effect':12760,
            'evidence_ids':['Fmain'],'detail':[{'name':'金银花','evidence_id':'Fdetail',
                'status':'complete','unit_before':3.35,'unit_after':3.5,'unit_delta':.15,
                'amount_before':184250,'amount_after':203000,'amount_delta':18750,
                'volume_effect':10050,'unit_effect':8700,'source_before':{'secret_note':'DO_NOT_SEND'}}]}
    facts={'current':{'volume':58000,'unit_cost':11.21,'total_cost':650180},
           'previous':{'volume':55000,'unit_cost':10.9,'total_cost':599500},
           'amount_delta':50680,'elements':{'材料':fields,'人工':{'evidence_ids':['Flabor'],
           'labor_factors':{'available':True,'unit_available':True,'evidence_id':'Flabor',
               'hours_before':1408,'hours_after':1344,'hours_delta':-64,'hours_effect':-3750,
               'rate_effect':9990,'cost_per_hour_before':58.59375,'cost_per_hour_after':66.0267857,
               'unit_hours_effect':-.1422413793,'unit_rate_effect':.1722413793}},
           '制费':{'evidence_ids':['Fmfg']}}}
    payload={'product':'银黄口服液','month':'2026-05','facts':facts,
             'elements':{e:{'dominant_driver':'output'} for e in ag.LABELS},
             'prompt_injection':'DO_NOT_SEND'}
    sources=[{'id':ident,'kind':'accounting_fact','elements':[element],'text':'DO_NOT_SEND'}
             for element,ids in [('材料',['Fmain','Fdetail']),('人工',['Flabor']),('制费',['Fmfg'])]
             for ident in ids]
    return payload,sources


def test_numeric_context_retains_amount_unit_bridges_and_focus():
    payload,sources=m2_input();original=deepcopy(payload)
    projected=ag._model_context(payload,sources,include_numeric=True)
    assert projected['schema_version']=='attribution-model-context/2.1'
    assert projected['observed_totals']['current']['volume']==58000
    row=projected['tasks_by_element']['材料']
    assert row['observed_costs']['contribution']==67.09
    assert row['observed_costs']['unit_effect']==12760
    assert row['observed_details'][0]['amount_delta']==18750
    assert row['observed_details'][0]['unit_after']==3.5
    assert row['focus']['name']=='金银花' and row['focus']['evidence_id']=='Fdetail'
    labor=projected['tasks_by_element']['人工']['observed_labor']
    assert labor['hours_after']==1344 and labor['hours_effect']==-3750 and labor['rate_effect']==9990
    assert 'DO_NOT_SEND' not in json.dumps(projected,ensure_ascii=False)
    assert payload==original
    assert len(json.dumps(projected,ensure_ascii=False))<12000


def test_actual_m2_worker_request_includes_numbers_but_keeps_numeric_output_gate():
    payload,sources=m2_input();calls=[]
    # ag.ACTIONS is a legacy fallback template, not an admissible new model
    # response: the live fixture names only input objects and safe voucher types.
    recommendations = {
        '材料': '请采购部核对已有金银花成本明细与计价口径，并核查结算单，生产部核查领退料单。',
        '人工': '请财务部核对已有工时与人工归集费用，生产部核查工时台账。',
        '制费': '请财务部核对已有制造费用汇总与产出口径，并核查计提计算表及费用分摊表。',
    }
    candidate={'elements':{element:{'hypothesis':'该项已定位至核算变化，实际业务原因尚不能确认，需要核查凭证。',
        'recommendation':recommendations[element], 'evidence_ids':[next(s['id'] for s in sources if element in s['elements'])]}
        for element in ag.LABELS}}
    candidate['elements']['材料']['hypothesis']='金银花单位费用与归集金额同步上行，尚不能据此确认采购实价或实物耗用变化。'
    candidate['elements']['人工']['hypothesis']='工时减少与小时归集费用上升方向相反，后者超过前者并相互抵消，具体费用归集原因尚待核查。'
    def provider(instruction,data,**kwargs):
        calls.append(deepcopy(data));return deepcopy(candidate)
    result=ag._llm_generate(payload,sources,request_fn=provider,
        config=ModelConfiguration('https://fixture.invalid/v1','fixture','fixture',True,40))
    assert result['attempts'][0]['status']=='validated'
    assert calls[0]['tasks_by_element']['材料']['observed_details'][0]['unit_effect']==8700
    bad=deepcopy(candidate);bad['elements']['材料']['hypothesis']='材料成本上升3.11%，可能来自材料计价，需要核查具体凭证。'
    assert any('自行书写的数值' in error for error in ag._model_errors(bad,sources,payload['facts']))


def test_legacy_projection_contract_stays_qualitative():
    payload,sources=m2_input()
    result=ag._model_context(payload,sources)
    assert result['schema_version']=='attribution-model-context/1.0'
    assert 'observed_costs' not in result['tasks_by_element']['材料']


def benchmark_input():
    rows=[{'element':e,'evidence_id':'B'+str(i),'unit_gap':gap,'home_unit_cost':home,
           'peer_unit_cost':peer,'normalized_amount':gap*58000,'contribution_pct':contribution}
          for i,(e,home,peer,gap,contribution) in enumerate([
              ('材料',7.3,7.29,.01,-2.56),('人工',1.53,1.73,-.2,51.28),('制费',2.38,2.58,-.2,51.28)],1)]
    facts={'elements':rows,'unit_gap':-.39,'normalized_amount':-22620,
           'home_factory':'中药一厂','peer_factory':'中药二厂',
           'home':{'unit_cost':11.21,'volume':58000,'total_cost':650180},
           'peer':{'unit_cost':11.6,'volume':50000,'total_cost':580000},
           'paired_drilldown':{'材料':{'rows':[{'name':'金银花','status':'missing_peer',
               'home':{'unit_cost':3.5,'amount':203000,'volume':58000,'evidence_id':'Bdetail'},
               'peer':None,'unit_gap':None,'normalized_amount':None}]}}}
    sources=[{'id':r['evidence_id'],'kind':'data_fact','elements':[r['element']],
              'text':'核算事实。','source':{'table':'synthetic'}} for r in rows]
    sources.append({'id':'Bdetail','kind':'data_fact','elements':['材料'],'text':'本厂明细。','source':{'table':'synthetic'}})
    return {'product':'银黄口服液','specification':'10ml×10支/盒','month':'2026-05','facts':facts},sources


def test_benchmark_keeps_negative_contribution_and_unpaired_peer_missing():
    payload,sources=benchmark_input();original=deepcopy(payload)
    result=bg.grouped_model_context(payload,sources,include_numeric=True)
    assert result['comparison_totals']['normalized_amount']==-22620
    item=result['tasks_by_element']['材料']
    assert item['observed_comparison']['contribution_pct']==-2.56
    assert item['observed_comparison']['unit_gap']==.01
    detail=item['observed_paired_details'][0]
    assert detail['home']['unit_cost']==3.5 and detail['peer'] is None
    assert detail['unit_gap'] is None and detail['normalized_amount'] is None
    assert payload==original


def test_benchmark_retains_available_labor_intensity_without_inventing_peer():
    payload,sources=benchmark_input()
    sources.append({'id':'BLaborDetail','kind':'data_fact','elements':['人工'],
                    'text':'本厂人工汇总。','source':{'table':'labor'}})
    payload['facts']['paired_drilldown']['人工']={'rows':[{
        'name':'直接人工','status':'missing_peer','peer':None,
        'home':{'unit_cost':1.53,'amount':88740,'volume':58000,'evidence_id':'BLaborDetail',
                'metrics':{'hours':1344,'hours_per_box':1344/58000,
                           'cost_per_hour':88740/1344,'output_per_hour':58000/1344}},
        'unit_gap':None,'normalized_amount':None}]}
    result=bg.grouped_model_context(payload,sources,include_numeric=True)
    detail=result['tasks_by_element']['人工']['observed_paired_details'][0]
    assert detail['peer'] is None
    assert detail['home']['metrics']['hours']==1344
    assert detail['home']['metrics']['hours_per_box']==1344/58000
    assert detail['home']['metrics']['output_per_hour']==58000/1344
    assert '非个人工资' in detail['home']['metric_units']['cost_per_hour']


def test_unit_driver_ties_are_explicit_not_unique_primary_claims():
    payload,sources=m2_input()
    second=deepcopy(payload['facts']['elements']['材料']['detail'][0])
    second.update(name='药材乙',evidence_id='Fother',amount_delta=10000)
    payload['facts']['elements']['材料']['detail'].append(second)
    sources.append({'id':'Fother','kind':'accounting_fact','elements':['材料']})
    projected=ag._model_context(payload,sources,include_numeric=True)
    focus=projected['tasks_by_element']['材料']['focus']
    assert focus['is_tied'] and focus['tied_objects']==['金银花','药材乙']
    from enterprise.analysis_context import observation_alignment_errors
    bad={'elements':{'材料':{'hypothesis':'金银花是主要驱动，实际原因尚待核查。','recommendation':'采购部核查凭证形成核对表。'}}}
    assert any('并列' in error for error in observation_alignment_errors(bad,projected))


def test_live_alignment_rejects_generic_mechanism_without_observed_direction():
    payload,sources=benchmark_input()
    context=bg.grouped_model_context(payload,sources,include_numeric=True)
    from enterprise.analysis_context import observation_alignment_errors
    bad={'elements':{'材料':{'hypothesis':'收率波动可能影响材料耗用，尚不能确认实际原因。','recommendation':'采购部核查原始凭证。'}}}
    assert len(observation_alignment_errors(bad,context))==2
    bad['elements']['材料']['hypothesis']='一厂单位材料费用高于二厂，反向抵消其他要素形成的净差额，尚不能确认采购实价或耗用差异。'
    assert observation_alignment_errors(bad,context)==[]


@pytest.mark.parametrize('text',['收率每降一个百分点可能推高材料成本，实际变化尚待核查。','实际耗用增加五克，原因尚不能确认，需要核查原始凭证。','四项主材构成主要核查对象，但尚不能确认原料实际耗用情况。'])
def test_chinese_quantities_cannot_bypass_model_numeric_guard(text):
    payload,sources=m2_input()
    candidate={'elements':{element:{'hypothesis':text,'recommendation':ag.ACTIONS[element],
               'evidence_ids':[next(s['id'] for s in sources if element in s['elements'])]}
               for element in ag.LABELS}}
    assert any('自行书写的数值' in error for error in ag._model_errors(candidate,sources,payload['facts']))
    bp,bs=benchmark_input()
    b={'elements':{element:{'claim_type':'hypothesis','hypothesis':text,
         'recommendation':'财务部核查两厂同口径原始记录，编制差异核对表并单列未闭合项。',
         'evidence_ids':[next(s['id'] for s in bs if element in s['elements'])],
         'missing_evidence':['两厂原始凭证']} for element in ag.LABELS}}
    assert any('自行书写的数值' in error for error in bg.validate_explanations(b,bs,bp['facts']))


@pytest.mark.parametrize('text', [
    '已有明细可定位熟地黄与山茱萸等主材归集金额及单位耗用，具体经营原因尚不能确认。',
    '现有数据已提供实际单耗，仍需核对二厂记录。',
    '已定位实物耗用量，尚不能确认差额根因。'])
def test_accounting_observations_do_not_prove_physical_material_usage(text):
    from enterprise.causal_guard import validate_cost_causality
    assert any('实物耗用' in error for error in validate_cost_causality(text))


@pytest.mark.parametrize('text', [
    '已有明细可定位熟地黄归集金额及单位耗用成本，具体原因尚不能确认。',
    '现有数据不足以确定实际单耗，需补齐领退料实物记录。',
    '采购部与生产部核对实际单耗及采购结算价格，形成差异表。',
    '已有明细不代表已知实物单耗，需要核查原始凭证。'])
def test_physical_material_record_requests_and_cost_units_remain_valid(text):
    from enterprise.causal_guard import validate_cost_causality
    assert validate_cost_causality(text)==[]


@pytest.mark.parametrize('value',[True,float('nan'),float('inf'),'not-a-number'])
def test_numeric_context_rejects_non_numeric_or_nonfinite_input(value):
    payload,sources=m2_input();payload['facts']['elements']['材料']['amount_delta']=value
    result=ag._model_context(payload,sources,include_numeric=True)
    assert result['tasks_by_element']['材料']['observed_costs']['amount_delta'] is None
    json.dumps(result,allow_nan=False)
