"""Round-two UI: synthetic values, offline AppTest, no model/server/data writes."""
import ast
from copy import deepcopy
from html.parser import HTMLParser
import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app_pages.citations import compact_analysis_sections
from dashboard.charts import change_detail, waterfall
from dashboard.chart_link import chart_context_id, decode_chart_selection, safe_inline_option


def _data(deltas, previous=1000):
    net = sum(deltas)
    return {'series': [{'month': '2026-05'}, {'month': '2026-06'}],
            'amount_change': [{'month': '2026-06', '上月总成本': previous,
                               '本月总成本': previous + net, '总变动额': net,
                               **dict(zip(('材料变动额', '人工变动额', '制费变动额'), deltas)),
                               '贡献度': dict(zip(('材料', '人工', '制费'),
                                   [value / net * 100 if net else None for value in deltas]))}]}


@pytest.mark.parametrize('deltas', [(60, 30, 10), (-60, -30, -10), (60, -90, 10),
                                     (-60, 90, -10), (60, -60, 0), (0, 0, 0),
                                     (0.01, -0.02, 0.03)])
def test_explicit_truncated_and_change_only_share_exact_signed_identity(deltas):
    data = _data(deltas)
    before = deepcopy(data)
    full = waterfall(data, '测试产品', '2026-06')
    detail = change_detail(data, '测试产品', '2026-06')
    assert full['yAxis']['min'] < min(data['amount_change'][0]['上月总成本'], data['amount_change'][0]['本月总成本'])
    assert full['yAxis']['max'] > max(data['amount_change'][0]['上月总成本'], data['amount_change'][0]['本月总成本'])
    assert 'Y 轴截断以突出变动量' in full['title']['subtext']
    assert detail['yAxis']['min'] == -detail['yAxis']['max']
    assert detail['yAxis']['min'] < 0 < detail['yAxis']['max']
    assert detail['yAxis']['name'] == full['yAxis']['name'] == '元'
    assert '起止总额另列' in detail['title']['subtext']
    assert len(detail['series']) == 1
    bars = detail['series'][0]
    assert 'stack' not in bars  # No floating/rebased/truncated total bars.
    points = bars['data']
    assert [point['value'] for point in points] == [*deltas, sum(deltas)]
    for index, (element, delta) in enumerate(zip(('材料', '人工', '制费'), deltas)):
        point = points[index]
        assert point['contribution'] == data['amount_change'][0]['贡献度'][element]
        assert point['signed_delta'] == delta
        assert f'{delta:+,.2f} 元' in point['tooltip']['formatter']
        if delta:
            wf_point = full['series'][1 if delta > 0 else 2]['data'][index + 1]
            assert wf_point['signed_delta'] == point['signed_delta']
            assert wf_point['contribution'] == point['contribution']
    if not sum(deltas):
        assert all('贡献度无定义' in point['tooltip']['formatter'] for point in points[:3])
    json.dumps(safe_inline_option(detail), ensure_ascii=False, allow_nan=False)
    assert data == before


@pytest.mark.parametrize('previous,deltas', [(-100, (20, 30, -10)), (-10, (30, -5, -2))])
def test_waterfall_keeps_zero_and_real_floating_geometry_for_negative_totals(previous, deltas):
    option = waterfall(_data(deltas, previous), '冲回样例', '2026-06')
    assert option['yAxis']['min'] < 0 <= option['yAxis']['max']
    assert all(series['stackStrategy'] == 'all' for series in option['series'])
    # Cross-sign stacking must add the visible magnitude to the negative base,
    # rather than treating them as independent positive and negative stacks.
    running = previous
    for index, delta in enumerate(deltas, 1):
        base = option['series'][0]['data'][index]
        assert base == min(running, round(running + delta, 2))
        assert base + abs(delta) == pytest.approx(max(running, running + delta))
        running += delta


def test_detail_clicks_use_trusted_identity_and_reject_stale_context():
    option = change_detail(_data((60, -90, 10)), '测试产品', '2026-06')
    context = chart_context_id('测试产品', 'S', '2026-06', 'data1')
    event = {'context_id': context, 'chart_index': 0, 'series_index': 0,
             'data_index': 1, 'event_id': 'fixture', 'element': '伪造材料', 'month': '1900-01'}
    args = dict(context_id=context, month='2026-06', available_months=['2026-05', '2026-06'])
    assert decode_chart_selection(event, [(option, 420, True)], **args) == {
        'element': '人工', 'month': '2026-06', 'event_id': 'fixture'}
    assert decode_chart_selection(event, [(option, 420, True)], **{**args,
        'context_id': chart_context_id('其他产品', 'S', '2026-06', 'data1')}) is None
    assert option['media'][0]['option']['series'][0]['label']['show'] is False
    assert option['aria']['enabled']


def test_missing_month_does_not_invent_change_detail():
    data = _data((1, 2, 3))
    data['amount_change'][0]['上月总成本'] = None
    with pytest.raises(ValueError):
        change_detail(data, '测试产品', '2026-06')


def _fixture_sections():
    numeric = ('总成本变动 -103,680.00 元；材料贡献 +64.53%。[F001]\n'
               + '同口径量价分解：产量影响 -58,400.00 元，单位成本影响 -8,500.00 元。' * 6
               + '金银花市场参考价由138.00变为133.50元/kg；不是实际结算价。[K001]')
    return [{'title': '直接材料', 'fact': '总成本变动 -103,680.00 元。[F001]',
             'numeric_explanation': numeric,
             'mechanism_note': '知识工序收率≥85%，仅支持核查方向，不证明实际收率下降。[K001]',
             'hypothesis': '尚不能确认采购价格机制。',
             'immediate_action': '建议财务部先按已有材料明细复算量价桥接。',
             'recommendation': '不应覆盖当前行动的旧建议。',
             'evidence_gaps': ['二厂结算单未提供'], 'missing_evidence': ['过时缺口'],
             'followup_criteria': '现有明细先勾稽，外部凭证取得后再复核机制。',
             'text': numeric + '\n旧建议需补齐二厂结算单后完成核对。',
             'evidence_ids': ['F001', 'K001', 'G001', '7'], 'claim_type': 'hypothesis'}]


def _evidence():
    return [{'id': 'F001', 'kind': 'accounting_fact', 'source': {'file': 'fixture.csv'},
             'text': '成本变动 -103680 元的合成核算记录。'},
            {'id': 'K001', 'kind': 'document_basis', 'source': {'file': '工艺.txt'},
             'text': '提取收率≥85%；标准不是当期实绩。'},
            {'id': 'G001', 'kind': 'document_basis', 'source': {'file': '图谱证据.txt', 'line': 3},
             'text': '提取→浓缩，图谱边的原文依据。'},
            {'id': '7', 'source': {'file': '历史引用.txt'}, 'text': '历史数字引用标识。'}]


class _Markup(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.words = []
        self.refs = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'sup':
            self.refs.append(values)

    def handle_data(self, data):
        self.words.append(data)

    @property
    def text(self):
        return ''.join(self.words)


def _display(sections, evidence):
    import streamlit as st
    from app_pages.citations import render_layered_analysis
    st.session_state.setdefault('generated', False)
    if st.button('生成合成分析', key='generate'):
        st.session_state.generated = True
    render_layered_analysis(sections, evidence, key='round2', generated=st.session_state.generated,
                            followup_criteria='共同完成口径：当前分析不以外部凭证为前提。')


def _components(app):
    return {node.key: _Markup(json.loads(node.proto.json)['html'])
            for node in app.get('bidi_component')}


def test_shared_numeric_bridge_survives_limit_with_no_input_mutation():
    sections = _fixture_sections()
    before = deepcopy(sections)
    row, = compact_analysis_sections(sections, conclusions=True)
    assert len(row['text']) > 160
    assert row['text'] == sections[0]['numeric_explanation']
    assert row['recommendation'] == sections[0]['immediate_action']
    assert row['evidence_gaps'] == sections[0]['evidence_gaps']
    assert set(row['recommendation_evidence_ids']) == {'F001', 'K001', 'G001', '7'}
    assert sections == before


def test_apptest_full_numeric_causes_visible_with_actions_not_completion_gaps():
    sections = _fixture_sections()
    app = AppTest.from_function(_display, args=(sections, _evidence()), default_timeout=20).run()
    assert not app.exception
    assert set(_components(app)) == {'round2_summary'}
    assert '建议财务部' not in _components(app)['round2_summary'].text
    app.button(key='generate').click().run()
    assert not app.exception
    actions = _components(app)['round2_actions']
    assert len(actions.text) > 160
    assert '产量影响 -58,400.00 元，单位成本影响 -8,500.00 元' in actions.text
    assert '133.50元/kg' in actions.text
    assert '已计算事实与数字原因' in actions.text
    assert '解释与机制边界（待核查）' in actions.text
    assert '现在可执行的核查' in actions.text
    assert sections[0]['immediate_action'] in actions.text
    assert '需补齐' not in actions.text and '二厂结算单' not in actions.text
    assert {'F001', 'K001', 'G001', '7'} == {ref['data-evidence-id'] for ref in actions.refs}
    assert all(ref['data-source'] and ref['data-content'] for ref in actions.refs)
    assert all(ref['tabindex'] == '0' and ref['role'] == 'button' for ref in actions.refs)
    assert any(node.type == 'bidi_component' and node.key == 'round2_actions'
               for node in app.main.children.values())
    gaps = next(exp for exp in app.expander if exp.label == '证据缺口与假设边界')
    assert '二厂结算单未提供' in str(gaps.dataframe[0].value)
    assert any('共同完成口径' in row.value for row in app.markdown)


def test_accepted_model_action_stays_verbatim_in_visible_and_full_surfaces():
    sections = _fixture_sections()
    accepted = ('建议采购部按既有结算资料核查价差。\n'
                + '逐项核对已有记录并保留尚未证实的原因边界。' * 20
                + '  保留双空格、换行与全部原文。[K001]')
    sections[0].update(model_followup_action=accepted,
                       accepted_model_recommendation=accepted,
                       model_action_evidence_ids=['K001', 'G001'],
                       recommendation=sections[0]['immediate_action'] + '\n已校验补充建议：' + accepted,
                       text=sections[0]['numeric_explanation'] + '\n' + accepted)
    row, = compact_analysis_sections(sections, conclusions=True)
    assert row['model_followup_action'] == accepted
    assert row['recommendation'] == sections[0]['immediate_action']
    app = AppTest.from_function(_display, args=(sections, _evidence()), default_timeout=20)
    app.session_state['generated'] = True
    app.run()
    assert not app.exception
    components = _components(app)
    # Inline reference IDs render as accessible citation labels; prose itself is
    # never stripped, shortened or normalized, including meaningful whitespace.
    prose = accepted.removesuffix('[K001]')
    assert prose in components['round2_actions'].text
    assert prose in components['round2_full'].text
    assert '已校验补充建议' in components['round2_actions'].text
    assert any('已校验补充建议' in item.value for item in app.caption)
    assert {'K001', 'G001'} <= {ref['data-evidence-id'] for ref in components['round2_actions'].refs}


def test_legacy_explicit_advice_never_recovers_a_completion_prerequisite():
    action = '采购部核对现有材料明细。'
    row, = compact_analysis_sections([{'title': '材料', 'fact': '金额差 -100元。[F001]',
        'recommendation': action,
        'text': '金额差 -100元。[F001]\n' + action + '需补齐二厂结算单后完成核对。',
        'missing_evidence': ['二厂结算单'], 'evidence_ids': ['F001']}], conclusions=True)
    assert row['recommendation'] == action


@pytest.mark.parametrize('extra', [{'immediate_action': ''}, {'claim_type': 'no_difference'}])
def test_shared_explicit_empty_and_no_difference_do_not_recover_old_advice(extra):
    source = {**_fixture_sections()[0], **extra}
    row, = compact_analysis_sections([source], conclusions=True)
    assert 'recommendation' not in row


def test_dashboard_header_patch_is_page_scoped_and_truncated_axis_is_explicit():
    source = (Path(__file__).resolve().parents[1] / 'dashboard_web.py').read_text(encoding='utf-8')
    assert '.stMainBlockContainer:has(.st-key-dashboard_header)' in source
    assert 'calc(4.5rem + env(safe-area-inset-top, 0px))' in source
    assert "st.container(key='dashboard_header')" in source
    assert "st.tabs(['金额分解视图（截断轴）', '差额视图（零基线）'])" in source
    assert 'Y 轴截断以突出变动量' in source
    assert "st.session_state.pop('_rag_results', None)" in source
    assert "st.session_state.pop('_rag_last_event', None)" in source


def _focus_reset_app(*, historical_deletion=False):
    """Run the actual dashboard guard/widgets, without loading costs or models."""
    source = (Path(__file__).resolve().parents[1] / 'dashboard_web.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    context = next(node for node in tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == '_chart_context' for target in node.targets))
    guard = next(node for node in tree.body if isinstance(node, ast.If)
                 and "st.session_state.get('_rag_context')" in ast.unparse(node.test))
    controls = next(node for node in tree.body if isinstance(node, ast.With)
                    and '按月份和要素选择知识依据' in ast.unparse(node.items[0].context_expr))
    if historical_deletion:
        # Mutation reproduces the exact old bug; value-only assertions would
        # falsely pass because Python resets but no frontend set_value is sent.
        class DeleteInsteadOfAssign(ast.NodeTransformer):
            def visit_Assign(self, node):
                target = ast.unparse(node.targets[0])
                for key in ('knowledge_focus_month', 'knowledge_focus_element', 'knowledge_focus_comparison'):
                    if target == f"st.session_state['{key}']":
                        return ast.parse(f"st.session_state.pop('{key}', None)").body[0]
                return node
        guard = DeleteInsteadOfAssign().visit(deepcopy(guard))
    setup = '''import streamlit as st
from dashboard.chart_link import chart_context_id, rag_question
months = ['2026-05', '2026-06']
product = st.selectbox('Fixture product', ['测试产品甲', '测试产品乙'], key='fixture_product')
month = st.selectbox('Fixture month', months, index=1, key='fixture_month')
spec = 'S'
scope = st.selectbox('Fixture principal scope', ['scope-a', 'scope-b'], key='fixture_scope')
data_version = st.selectbox('Fixture data version', ['data-a', 'data-b'], key='fixture_data')
st.selectbox('Fixture heat comparison', ['环比变化率', '同比变化率'], key='heat_elem')
_data_token = data_version + ':' + scope
_chart_events = []
'''
    app = AppTest.from_string(setup + '\n'.join(ast.unparse(node) for node in (context, guard, controls)),
                              default_timeout=10).run()
    assert not app.exception
    return app


def _choose_manual_focus(app):
    app.selectbox(key='knowledge_focus_month').select('2026-05')
    app.selectbox(key='knowledge_focus_element').select('人工')
    app.selectbox(key='knowledge_focus_comparison').select('同比').run()
    assert not app.exception
    assert [app.selectbox(key=key).value for key in
            ('knowledge_focus_month', 'knowledge_focus_element', 'knowledge_focus_comparison')] == ['2026-05', '人工', '同比']
    app.session_state['_rag_results'] = {'cached': 'old context'}
    app.session_state['_rag_last_event'] = 'old event'


@pytest.mark.parametrize('key,choice', [('fixture_product', '测试产品乙'), ('fixture_month', '2026-05'),
    ('fixture_scope', 'scope-b'), ('fixture_data', 'data-b'), ('heat_elem', '同比变化率')])
def test_actual_dashboard_context_reset_sends_all_three_frontend_values(key, choice):
    app = _focus_reset_app()
    _choose_manual_focus(app)
    app.selectbox(key=key).select(choice).run()
    assert not app.exception
    expected = {'knowledge_focus_month': app.selectbox(key='fixture_month').value,
                'knowledge_focus_element': '单位成本',
                'knowledge_focus_comparison': '同比' if key == 'heat_elem' else '环比'}
    for focus_key, value in expected.items():
        widget = app.selectbox(key=focus_key)
        assert widget.value == value
        assert widget.proto.set_value is True, 'Python-only reset must not leave stale React Aria input'
        assert widget.proto.raw_value == value
    assert '_rag_results' not in app.session_state and '_rag_last_event' not in app.session_state
    assert app.session_state['_rag_focus'] == {'month': expected['knowledge_focus_month'],
        'element': '单位成本', 'comparison': expected['knowledge_focus_comparison']}
    assert app.selectbox(key='fixture_product').value in app.session_state['rag_query']


def test_actual_dashboard_same_context_keeps_manual_focus_and_cached_evidence():
    app = _focus_reset_app()
    _choose_manual_focus(app)
    context = app.session_state['_rag_context']
    app.run()
    assert not app.exception
    assert app.session_state['_rag_context'] == context
    for key, value in [('knowledge_focus_month', '2026-05'), ('knowledge_focus_element', '人工'),
                       ('knowledge_focus_comparison', '同比')]:
        widget = app.selectbox(key=key)
        assert widget.value == value
        assert widget.proto.set_value is False
    assert app.session_state['_rag_results'] == {'cached': 'old context'}
    assert app.session_state['_rag_last_event'] == 'old event'


def test_old_delete_only_reset_fails_frontend_update_contract_even_when_python_resets():
    app = _focus_reset_app(historical_deletion=True)
    _choose_manual_focus(app)
    app.selectbox(key='fixture_month').select('2026-05').run()
    assert not app.exception
    widget = app.selectbox(key='knowledge_focus_element')
    assert widget.value == '单位成本'  # This alone incorrectly certified the old implementation.
    assert widget.proto.set_value is False and widget.proto.raw_value == ''
