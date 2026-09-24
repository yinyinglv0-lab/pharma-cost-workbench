"""Generated advice must be visible, traceable, and gated (offline AppTest)."""
from copy import deepcopy
from decimal import Decimal
from html import escape
from html.parser import HTMLParser
import json
import re

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from app_pages.citations import compact_analysis_sections


class _Markup(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.text = []
        self.refs = []
        self.tags = []
        self.classes = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.classes.extend(dict(attrs).get('class', '').split())
        ident = dict(attrs).get('data-evidence-id')
        if ident:
            self.refs.append(ident)

    def handle_data(self, data):
        self.text.append(data)

    @property
    def visible(self):
        return ''.join(self.text)


def _display_script(sections, evidence):
    import streamlit as st
    from app_pages.citations import render_layered_analysis
    st.session_state.setdefault('generated', False)
    if st.button('生成分析', key='generate'):
        st.session_state.generated = True
    render_layered_analysis(sections, evidence, key='review',
                            generated=st.session_state.generated,
                            overview='完整总览', limitations=['核算差异不是已确认业务原因。'])


def _components(app):
    return {node.key: json.loads(node.proto.json)['html']
            for node in app.get('bidi_component')}


def _run_display(sections, evidence, *, generated=True):
    app = AppTest.from_function(_display_script, args=(sections, evidence), default_timeout=20)
    app.session_state['generated'] = generated
    app.run()
    assert not app.exception
    return app


def _cost_row(factory, month, material, labor, overhead, volume=100):
    values = [Decimal(str(value)) for value in (material, labor, overhead)]
    return {'工厂': factory, '产品名称': '测试产品', '产品规格': 'S', '月份': month,
            '直接材料(元/盒)': float(values[0]), '直接人工(元/盒)': float(values[1]),
            '制造费用(元/盒)': float(values[2]), '单位成本(元/盒)': float(sum(values)),
            '总成本(元)': float(sum(values) * volume), '产量(盒)': volume,
            '_source_file': factory + '.csv'}


@pytest.fixture
def tables():
    return {'cost26': pd.DataFrame([
                _cost_row('中药一厂', '2026-04', 7, 2, 3),
                _cost_row('中药一厂', '2026-05', 8, 3, 4)]),
            'erchang26': pd.DataFrame([_cost_row('中药二厂', '2026-05', 7, 2, 3)]),
            **{name: pd.DataFrame() for name in ('cost25', 'budget', 'material', 'labor', 'mfg')}}


@pytest.fixture(autouse=True)
def no_external_generation(monkeypatch):
    import attribution_gen
    import enterprise.model_gateway as gateway

    def forbidden(*args, **kwargs):
        raise AssertionError('Display regression must not call a provider or worker')

    monkeypatch.setattr(gateway, 'generate_json', forbidden)
    monkeypatch.setattr(attribution_gen, '_execute_stage', forbidden)


def _model(mode, *, benchmark):
    def generate(payload, evidence):
        if mode == 'unavailable':
            raise TimeoutError('offline fixture')
        if mode == 'rejected':
            return {'invalid': 'offline fixture'}
        # Current generation uses input-local IDs and declared voucher categories;
        # historical display-only fixtures below retain their saved wording.
        from attribution_gen import _model_context
        from enterprise.benchmark_ai import grouped_model_context
        context = (grouped_model_context(payload, evidence, include_numeric=True) if benchmark
                   else _model_context(payload, evidence, include_numeric=True))
        actions = {
            '材料': '请采购部核对已有材料成本汇总及计价口径，生产部核查领退料单，采购部核查结算单。',
            '人工': '请财务部核对已有人工费用汇总与产出口径，生产部核查工时台账。',
            '制费': '请财务部核对已有制造费用汇总与产出口径，核查计提计算表及费用分摊表。',
        }
        rows = {}
        for element in ('材料', '人工', '制费'):
            task = context['tasks_by_element'][element]
            if benchmark and task.get('mode') == 'no_difference':
                rows[element] = deepcopy(task['no_difference_result'])
                continue
            ident = next(ref for ref in task['eligible_evidence_ids']
                         if any(source['id'] == ref and source.get('kind') in
                                ('data_fact', 'accounting_fact') for source in evidence))
            direction = ('本厂本项单位费用高于对标厂，' if benchmark
                         else '本项金额与单位成本同步上行，')
            row = {'hypothesis': direction + '现有核算差异尚不能确认实际业务原因，仍待核查。',
                   'recommendation': actions[element], 'evidence_ids': [ident]}
            if benchmark:
                row.update(claim_type='hypothesis', missing_evidence=['二厂同口径原始凭证'])
            rows[element] = row
        return {'elements': rows}
    return generate


def _benchmark_result(tables, mode):
    from enterprise.benchmark_ai import generate_benchmark_analysis
    return generate_benchmark_analysis('测试产品', 'S', '2026-05', tables,
                                       model_fn=_model(mode, benchmark=True))


def _attribution_result(tables, mode, tmp_path, monkeypatch):
    import attribution_gen
    # Isolate file provenance and optional external market inputs as well as DB.
    monkeypatch.setattr('enterprise.snapshots.current_provenance', lambda: {})
    monkeypatch.setattr('attribution_decomposition._load_market', lambda: pd.DataFrame())
    return attribution_gen.generate_attribution('测试产品', '2026-05', True, d=tables,
                                                root=tmp_path, model_fn=_model(mode, benchmark=False))


def test_admitted_m2_role_and_complete_advice_survive_generator_and_display(tables, tmp_path, monkeypatch):
    import attribution_gen
    original_model = _model('valid', benchmark=False)
    advice = ('建议人事主管先核对所选期间已有人工费用汇总与产出口径，逐项匹配归集范围，'
              '向生产部核查工时台账，向财务部核查费用分摊表；缺失的岗位依据单列为证据缺口，'
              '不阻断当前核算复核，尚不能据此确认效率原因或承诺节约。')
    def model(payload, evidence):
        candidate = original_model(payload, evidence)
        candidate['elements']['人工']['recommendation'] = advice
        return candidate
    monkeypatch.setattr('enterprise.snapshots.current_provenance', lambda: {})
    monkeypatch.setattr('attribution_decomposition._load_market', lambda: pd.DataFrame())
    result = attribution_gen.generate_attribution('测试产品', '2026-05', True, d=tables,
                                                  root=tmp_path, model_fn=model)
    assert result['used_llm']
    assert result['model_explanations']['elements']['人工']['recommendation'] == advice
    for text in (result['text'], result['concise_text']):
        assert advice in text
    displayed = next(row for row in compact_analysis_sections(result['sections'], conclusions=True)
                     if row['title'] == attribution_gen.LABELS['人工'])
    assert displayed['model_followup_action'] == advice
    assert displayed['recommendation'] != advice
    app = _run_display(result['sections'], result['sources'])
    components = _components(app)
    assert set(components) == {'review_reading', 'review_full', 'review_numeric_audit'}
    reading = _Markup(components['review_reading'])
    assert reading.visible.count(advice) == 1
    assert reading.classes.count('analysis-recommendation-label') == 6
    assert '机制边界（待核查）' in reading.visible
    assert _Markup(components['review_full']).visible.count(advice) == 1


def test_renderer_preserves_every_admitted_fact_and_document_reference():
    from attribution_narrative import narrative_references
    sources=[{'id':'Ffirst','kind':'accounting_fact'}, {'id':'Fsecond','kind':'accounting_fact'},
             {'id':'Kfirst','kind':'document_basis'}, {'id':'Ksecond','kind':'document_basis'}]
    ids=['Ffirst','Kfirst','Fsecond','Ksecond','Ffirst']
    assert narrative_references(sources,ids)==ids[:4]


@pytest.mark.parametrize('effects,expected', [
    ((-900,-900,-1800),('间接人工',)),
    ((1160,1160,500),('折旧费','动力费'))])
def test_default_manufacturing_deliverable_covers_actual_largest_unit_effect(tables, tmp_path, monkeypatch, effects, expected):
    import attribution_gen
    from enterprise.analysis_narrative import build_attribution_narrative
    monkeypatch.setattr('enterprise.snapshots.current_provenance', lambda: {})
    monkeypatch.setattr('attribution_decomposition._load_market', lambda: pd.DataFrame())
    result=attribution_gen.generate_attribution('测试产品','2026-05',False,d=tables,root=tmp_path)
    payload=result['payload']
    payload['facts']['elements']['制费']['detail']=[
        {'name':name,'evidence_id':'Ffixture'+str(i),'unit_effect':effect,'volume_effect':200,
         'amount_delta':effect+200,'unit_before':1,'unit_after':.9}
        for i,(name,effect) in enumerate(zip(('折旧费','动力费','间接人工'),effects))]
    narrative = build_attribution_narrative(payload)
    section = next(row for row in narrative['sections'] if row['element']=='制费')
    action = section['immediate_action']
    assert all(name in action for name in expected)
    assert all(name not in action for name in ('折旧费','动力费','间接人工') if name not in expected)
    assert '核对金额、产量及现有分配口径' in action
    # The common deliverable remains required, but is no longer duplicated in
    # each object's action or inferred from the final evidence-gap paragraph.
    criteria = narrative['followup_criteria']
    assert '差异核对表' in criteria and '归集金额勾稽' in criteria and '未闭合项' in criteria
    assert narrative['text'].count(criteria) == 1
    assert '差异核对表' not in action


def test_warmup_waits_only_for_local_loading_and_keeps_strict_mode(monkeypatch):
    from app_pages._shared import wait_for_analysis_warmup
    from contextlib import nullcontext
    import enterprise.knowledge_runtime as runtime
    import streamlit as st
    calls=[]
    states=iter([{'ready':False,'state':'loading'},{'ready':True,'state':'ready'}])
    def ready(principal, **kwargs):
        calls.append(kwargs);return next(states)
    monkeypatch.setattr(runtime,'readiness',ready)
    monkeypatch.setattr(st,'spinner',lambda *a,**k:nullcontext())
    assert wait_for_analysis_warmup('fixture','fixture-repository')['ready']
    assert len(calls)==2 and all(call['require_hybrid'] for call in calls)
    assert calls[1]['wait'] is True and calls[1]['timeout']==90
    calls.clear()
    states=iter([{'ready':False,'state':'hybrid_required_no_vectors'}])
    assert not wait_for_analysis_warmup('fixture','fixture-repository')['ready']
    assert len(calls)==1


def test_long_recommendation_preserves_conditions_uncertainty_and_all_sources():
    advice = ('建议采购部、生产部核对结算与领退料记录，形成差异表。'
              + '逐项比对凭证日期、材料等级及批次范围，记录未闭合项目。' * 8
              + '仅在原始凭证齐全并经业务复核后调整，尚不能确认实际耗用异常。[K001]')
    source = {'title': '材料', 'fact': '材料金额增加。[F001]',
              'hypothesis': '尚不能确认实际采购价格与耗用原因。',
              'recommendation': advice, 'text': '材料金额增加。[F001]\n' + advice,
              'evidence_ids': ['F001', 'K001']}
    before = deepcopy(source)
    row, = compact_analysis_sections([source], conclusions=True)
    assert len(row['text']) <= 160
    assert row['text'].startswith('材料金额增加。[F001]')
    assert row['recommendation'] == advice
    assert set(row['recommendation_evidence_ids']) == {'F001', 'K001'}
    assert source == before


def test_hypothesis_does_not_lose_shared_source_or_late_condition_to_fit_limit():
    hypothesis = '可能涉及归集口径。' + '仅在原始记录完整配对后才能确认。' * 12 + '[K001]'
    row, = compact_analysis_sections([{'title': '材料', 'fact': '已计算金额差异。[F001]',
                                      'hypothesis': hypothesis}], conclusions=True)
    assert len(row['text']) <= 160
    assert '可能涉及归集口径' not in row['text']
    assert row['evidence_ids'] == ['F001']


def test_structured_hypothesis_retains_sources_from_matching_narrative():
    hypothesis = '尚不能据汇总差额确认实际采购原因。'
    row, = compact_analysis_sections([{'title': '材料', 'fact': '材料存在金额差额。[F001]',
                                      'hypothesis': hypothesis,
                                      'text': '材料存在金额差额。[F001]\n' + hypothesis + '[K001]'}],
                                    conclusions=True)
    assert hypothesis + '[K001]' in row['text']
    assert row['evidence_ids'] == ['F001', 'K001']


def test_structured_action_does_not_revive_legacy_missing_evidence_prerequisite():
    advice = '采购部应核对结算凭证，形成同口径差异表。'
    prerequisite = '需补齐两厂领退料单及批次记录后完成核对。'
    source = {'title': '材料', 'fact': '已计算差额。[B003]',
              'recommendation': advice,
              'text': '已计算差额。[B003]\n' + advice + prerequisite,
              'missing_evidence': ['两厂领退料单及批次记录'], 'evidence_ids': ['B003']}
    before = deepcopy(source)
    row, = compact_analysis_sections([source], conclusions=True)
    assert row['recommendation'] == advice
    assert prerequisite not in row['recommendation']
    assert row['recommendation_evidence_ids'] == ['B003']
    assert source == before  # Historical raw text is immutable, not rewritten.


def test_legacy_action_keeps_multiline_supplement_and_late_condition():
    action = ('建议财务部核对原始凭证，形成差异表。\n'
              '只有补齐两期配对记录后才开展后续复核，尚不能据此确认根因。[K001]')
    row, = compact_analysis_sections([{'title': '材料', 'text': '已计算差额。[F001]\n' + action}],
                                    conclusions=True)
    assert row['recommendation'] == action
    assert set(row['recommendation_evidence_ids']) == {'F001', 'K001'}


def test_oversize_fact_does_not_hide_existing_advice_or_its_sources():
    advice = '建议采购部核对原始记录，凭证配对后形成差异表。'
    row, = compact_analysis_sections([{'title': '材料', 'fact': '完整核算事实' * 100 + '。[F001]',
                                      'recommendation': advice}], conclusions=True)
    assert len(row['text']) <= 160
    assert row['recommendation'] == advice
    assert row['recommendation_evidence_ids'] == ['F001']


def test_explicit_action_wins_over_inconsistent_legacy_tail():
    row, = compact_analysis_sections([{'title': '材料', 'fact': '已计算差额。[F001]',
                                      'recommendation': '建议财务部核对本次有效记录。',
                                      'text': '已计算差额。[F001]\n建议采购部使用过期记录。'}],
                                    conclusions=True)
    assert row['recommendation'] == '建议财务部核对本次有效记录。'
    assert '过期' not in str(row)


@pytest.mark.parametrize('extra', [{}, {'recommendation': ''}, {'claim_type': 'no_difference'}])
def test_missing_or_intentionally_empty_advice_is_not_invented(extra):
    section = {'title': '材料', 'fact': '材料单位成本无差异。[F001]',
               'text': '材料单位成本无差异。[F001]', **extra}
    if extra:
        section['text'] += '\n建议采购部核对旧记录。'
    row, = compact_analysis_sections([section], conclusions=True)
    assert 'recommendation' not in row


@pytest.mark.parametrize('mode', ['validated', 'unavailable', 'rejected'])
def test_benchmark_recommendations_are_visible_outside_expanders(tables, mode):
    result = _benchmark_result(tables, mode)
    assert result['generation_status'] == 'model_' + mode
    assert result['used_llm'] is (mode == 'validated')
    app = _run_display(result['sections'], result['evidence'])
    components = _components(app)
    assert set(components) == {'review_reading', 'review_full', 'review_numeric_audit'}
    actions = _Markup(components['review_reading'])
    assert not app.subheader  # Advice now belongs to one uninterrupted reading surface.
    assert '机制边界（待核查）' in actions.visible
    assert actions.classes.count('analysis-recommendation-label') == (6 if mode == 'validated' else 3)
    assert any('核查建议不等于整改或节约' in item.value for item in app.caption)
    assert not any('置信度' in item.value for item in app.caption)
    assert any(node.type == 'bidi_component' and node.key == 'review_reading'
               for node in app.main.children.values())
    assert all(node.key != 'review_reading' for expander in app.expander
               for node in expander.get('bidi_component'))
    for section in result['sections']:
        assert section['immediate_action'] in actions.visible
        assert section['recommendation'].startswith(section['immediate_action'])
        assert section['title'] in actions.visible
        assert set(section['evidence_ids']).issubset(actions.refs)
        assert '需补齐' not in actions.visible and '后完成核对' not in actions.visible
        # HTML replaces inline IDs with numbered citation links; the factual
        # text itself, including late numeric causes, must not be truncated.
        numeric = re.sub(r'\[[A-Za-z][A-Za-z0-9_-]*\]', '', section['numeric_explanation'])
        assert all(part in actions.visible for part in numeric.split('\n'))
        if mode == 'validated':
            assert section['model_followup_action'] == result['model_explanations']['elements'][section['element']]['recommendation']
            assert section['model_followup_action'] in actions.visible
    compact = compact_analysis_sections(result['sections'], conclusions=True)
    assert all(row['text'] == section['numeric_explanation'] for row, section in zip(compact, result['sections']))
    gaps = next(expander for expander in app.expander if expander.label == '证据缺口与假设边界')
    shown_gaps = gaps.dataframe[0].value
    for section in result['sections']:
        assert all(gap in '\n'.join(shown_gaps['证据缺口（不阻断当前核算分析）'])
                   for gap in section['evidence_gaps'])
    assert any(item.label == '展开完整分析与引用' for item in app.expander)
    assert any(item.label == '证据缺口与假设边界' for item in app.expander)


@pytest.mark.parametrize('mode', ['validated', 'unavailable', 'rejected'])
def test_attribution_existing_final_actions_are_visible_even_on_fallback(tables, mode, tmp_path, monkeypatch):
    result = _attribution_result(tables, mode, tmp_path, monkeypatch)
    assert result['generation_status'] == 'model_' + mode
    assert result['used_llm'] is (mode == 'validated')
    app = _run_display(result['sections'], result['sources'])
    components = _components(app)
    assert set(components) == {'review_reading', 'review_full', 'review_numeric_audit'}
    actions = _Markup(components['review_reading'])
    assert actions.classes.count('analysis-recommendation') == (6 if mode == 'validated' else 3)
    assert actions.classes.count('analysis-recommendation-label') == (6 if mode == 'validated' else 3)
    assert len(result['sections']) == 3
    for section in result['sections']:
        # Action, accepted model prose, and evidence gaps are distinct fields;
        # the final paragraph is now a nonblocking gap, not an action.
        action = section['immediate_action']
        assert action.startswith('建议') and section['recommendation'].startswith(action)
        assert action in actions.visible
        assert section['title'] in actions.visible
        assert set(section['evidence_ids']).issubset(actions.refs)
        numeric = re.sub(r'\[[A-Za-z][A-Za-z0-9_-]*\]', '', section['numeric_explanation'])
        assert all(part in actions.visible for part in numeric.split('\n'))
        assert '需补齐' not in actions.visible
        if mode == 'validated':
            accepted = result['model_explanations']['elements'][section['element']]['recommendation']
            assert section['model_followup_action'] == accepted and accepted in actions.visible
        else:
            assert section['model_followup_action'] == ''


@pytest.mark.parametrize('producer', ['benchmark', 'attribution'])
def test_real_generated_sections_stay_factual_until_explicit_click(tables, producer, tmp_path, monkeypatch):
    result = (_benchmark_result(tables, 'validated') if producer == 'benchmark'
              else _attribution_result(tables, 'validated', tmp_path, monkeypatch))
    assert result['used_llm'] and result['generation_status'] == 'model_validated'
    evidence = result['evidence'] if producer == 'benchmark' else result['sources']
    accepted = [section['model_followup_action'] for section in result['sections']]
    assert all(accepted)
    app = _run_display(result['sections'], evidence, generated=False)
    components = _components(app)
    assert set(components) == {'review_summary'}
    summary = _Markup(components['review_summary']).visible
    assert all(advice not in summary for advice in accepted)
    assert '尚不能确认实际业务原因' not in summary
    assert not app.subheader and not app.expander
    app.button(key='generate').click().run()
    assert not app.exception
    components = _components(app)
    assert set(components) == {'review_reading', 'review_full', 'review_numeric_audit'}
    visible = _Markup(components['review_reading']).visible
    assert all(visible.count(advice) == 1 for advice in accepted)
    assert all(section['immediate_action'] in visible for section in result['sections'])


def test_zero_difference_does_not_create_action_for_that_element(tables):
    tables['erchang26'] = pd.DataFrame([_cost_row('中药二厂', '2026-05', 8, 2, 3)])
    result = _benchmark_result(tables, 'unavailable')
    material = next(row for row in result['sections'] if row['element'] == '材料')
    assert material['claim_type'] == 'no_difference' and material['recommendation'] == ''
    app = _run_display(result['sections'], result['evidence'])
    components = _components(app)
    assert set(components) == {'review_reading', 'review_full', 'review_numeric_audit'}
    markup = components['review_reading']
    # The zero-difference element remains visible in the shared reading surface,
    # but only the two nonzero elements have an actual recommendation block.
    rendered_sections = re.findall(r'<section class="analysis-section">(.*?)</section>', markup, re.S)
    assert len(rendered_sections) == 3
    for section, rendered in zip(result['sections'], rendered_sections):
        visible = _Markup(rendered)
        assert '<h3>' + section['title'] + '</h3>' in rendered
        if section['element'] == '材料':
            assert visible.classes.count('analysis-recommendation') == 0
            assert visible.classes.count('analysis-recommendation-label') == 0
            assert re.sub(r'\[[A-Za-z][A-Za-z0-9_-]*\]', '', material['fact']) in visible.visible
        else:
            assert visible.classes.count('analysis-recommendation') == 1
            assert visible.classes.count('analysis-recommendation-label') == 1
            assert section['immediate_action'] in visible.visible


def test_all_zero_difference_has_no_advice_list(tables):
    tables['erchang26'] = pd.DataFrame([_cost_row('中药二厂', '2026-05', 8, 3, 4)])
    result = _benchmark_result(tables, 'unavailable')
    assert result['generation_status'] == 'no_difference'
    app = _run_display(result['sections'], result['evidence'])
    components = _components(app)
    assert set(components) == {'review_reading', 'review_full', 'review_numeric_audit'}
    reading = _Markup(components['review_reading'])
    assert reading.classes.count('analysis-section') == 3
    assert reading.classes.count('analysis-recommendation') == 0
    assert reading.classes.count('analysis-recommendation-label') == 0
    assert all(section['recommendation'] == '' and section['immediate_action'] == ''
               for section in result['sections'])
    assert not app.subheader


def test_visible_long_advice_is_not_clipped_and_keeps_safe_citations():
    advice = ('建议采购部核对原始记录。' + '仅在原始凭证完整且经过业务复核后处理差异。' * 10
              + '尚不能据此确认根因。[K001] <img src=x onerror=alert(1)>')
    sources = [{'id': ident, 'kind': 'data_fact', 'text': '<script>not executable</script>',
                'source': {'file': 'source<unsafe>.csv'}} for ident in ('F001', 'K001')]
    section = {'title': '材料<script>alert(1)</script>', 'fact': '已计算材料差额。[F001]',
               'text': '已计算材料差额。[F001]\n' + advice,
               'recommendation': advice, 'evidence_ids': ['F001', 'K001']}
    app = _run_display([section], sources)
    html = _components(app)['review_actions']
    markup = _Markup(html)
    assert advice.split('[K001]')[0] in markup.visible
    assert '尚不能据此确认根因。' in markup.visible
    assert set(markup.refs) == {'F001', 'K001'}
    assert 'img' not in markup.tags and 'script' not in markup.tags
    assert escape('<img src=x onerror=alert(1)>') in html
    assert escape('source<unsafe>.csv', quote=True) in html
    assert '请展开' not in markup.visible
