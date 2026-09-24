"""Explicit truncated decomposition, safe reading markup and scoped navigation."""
from copy import deepcopy
from math import isfinite
from pathlib import Path

import pytest

from dashboard.charts import waterfall, change_detail
from app_pages.citations import prose_reading_sections, reading_citation_html, CITATION_CSS


def fixture(previous, deltas):
    total = sum(deltas)
    return {'series': [{'month': '2026-05'}, {'month': '2026-06'}],
            'amount_change': [{'month': '2026-06', '上月总成本': previous,
                '本月总成本': previous + total, '总变动额': total,
                **dict(zip(('材料变动额', '人工变动额', '制费变动额'), deltas)),
                '贡献度': dict(zip(('材料', '人工', '制费'),
                                   [value / total * 100 if total else None for value in deltas]))}]}


@pytest.mark.parametrize('previous', [0, 1, 1000, 600000, -1000])
@pytest.mark.parametrize('deltas', [(0, 0, 0), (100, 20, 10), (-100, -20, -10),
                                  (100, -100, 0), (-2000, 1800, 400), (0.01, -0.02, 0.01)])
def test_both_views_read_exact_same_change_row_and_cover_all_endpoints(previous, deltas):
    data = fixture(previous, deltas)
    original = deepcopy(data)
    full, detail = waterfall(data, '模拟产品', '2026-06'), change_detail(data, '模拟产品', '2026-06')
    assert data == original
    metadata = [{key: value for key, value in option['accounting_source'].items() if key != 'axis_mode'}
                for option in (full, detail)]
    assert metadata[0] == metadata[1]
    assert detail['yAxis']['min'] == -detail['yAxis']['max']
    assert 'Y 轴截断以突出变动量' in full['title']['subtext']
    assert '柱长不可用于比较总额倍数' in full['aria']['description']
    assert full['yAxis']['min'] < full['yAxis']['max']
    cumulative = previous
    for delta in (0, *deltas):
        cumulative += delta
        assert full['yAxis']['min'] < cumulative < full['yAxis']['max']
    assert full['series'][3]['data'][0]['value'] == previous
    assert full['series'][3]['data'][-1]['value'] == previous + sum(deltas)
    for series in full['series']:
        assert series['stackStrategy'] == 'all'
        assert all(isfinite(point['value'] if isinstance(point, dict) else point)
                   for point in series['data'])
    running = previous
    for index, delta in enumerate(deltas):
        point = detail['series'][0]['data'][index]
        assert point['value'] == point['signed_delta'] == delta
        assert not point.get('stack_gap', False)  # Zero change is a real detail observation.
        candidates = [series['data'][index + 1] for series in full['series'][1:3]]
        observed = [item for item in candidates if not item.get('stack_gap', False)]
        assert [item['signed_delta'] for item in observed] == ([delta] if delta else [])
        after = round(running + delta, 2)
        assert full['series'][0]['data'][index + 1] == min(running, after)
        stack_top = sum(series['data'][index + 1]['value'] if isinstance(series['data'][index + 1], dict)
                        else series['data'][index + 1] for series in full['series'])
        assert stack_top == pytest.approx(max(running, after))
        running = after


@pytest.mark.parametrize('previous,deltas', [(600000, (-34000, -6000, -10000)), (0, (0, 0, 0)),
                                            (10000000, (1, -1, 0)), (-100, (-10, 5, 0))])
def test_total_labels_are_item_owned_and_not_hidden_by_any_media(previous, deltas):
    option = waterfall(fixture(previous, deltas), '模拟产品', '2026-06')
    for index in (0, 4):
        item = option['series'][3]['data'][index]
        assert not item.get('stack_gap', False)  # A genuine zero total still owns a label.
        assert item['label']['show'] is True
        assert item['label']['align'] == ('left' if index == 0 else 'right')
        assert f"{item['value']:,.2f}" in item['label']['formatter']
        assert item['label']['distance'] >= 8
        assert item['label']['position'] == ('top' if item['value'] >= 0 else 'bottom')
        assert item['tooltip'].get('show', True) is True
    total_labels = [option['series'][3]['data'][index]['label'] for index in (0, 4)]
    if (previous >= 0) == (previous + sum(deltas) >= 0):
        assert sorted(label['distance'] for label in total_labels) == [8, 44]
    else:
        assert all(label['distance'] == 8 for label in total_labels)
    assert option['series'][3]['labelLayout']['hideOverlap'] is False
    for rule in option['media']:
        assert rule['option']['series'][3]['label']['show'] is True
    spread = max(*(abs(delta) for delta in deltas), max(abs(previous), abs(previous + sum(deltas))) * .002, .5)
    assert option['yAxis']['max'] - max(previous, previous + sum(deltas)) >= 3 * spread - .01


def test_new_benchmark_reading_has_structure_once_and_styled_advice():
    fact = '甲厂本项单位成本3.00元，乙厂4.00元；标准化金额差-100元。'
    reason = '已观察的核算差异为单位差-1.00元，贡献度50.00%。'
    action = '建议财务部核对已有记录。'
    section = {'title': '制造费用', 'fact': fact, 'observed_reason': reason,
               'comparison_explanation': reason, 'numeric_explanation': fact + reason,
               'prose_fact_role': 'structure', 'reading_style': 'contextual-reading/1.4',
               'immediate_action': action, 'recommendation': action,
               'text': fact + reason + action, 'evidence_ids': []}
    rows = prose_reading_sections([section], overview='总体差异。', analysis_kind='benchmark')
    markup = reading_citation_html(rows, [])
    assert markup.count(fact) == 1
    assert markup.count(reason) == 1
    assert '<h3>找差异</h3>' in markup and '<h3>拆结构</h3>' in markup and '<h3>拆原因</h3>' in markup
    assert '<div class="analysis-recommendation">' in markup
    assert '<span class="analysis-recommendation-label">建议</span>财务部' in markup
    assert 'border-left:3px solid #2a78d6' in CITATION_CSS
    assert 'color:#2a78d6;font-weight:700' in CITATION_CSS


def test_accepted_prose_is_not_trimmed_or_rewritten_to_remove_numbers():
    prose = '  已采用候选原文。\n重复数字1.00元仍保留。  '
    section = {'title': '<script>标题</script>', 'prose': prose, 'prose_mode': 'bound-numeric-prose/1',
               'fact': '结构事实。', 'fact_in_prose': True, 'prose_fact_role': 'structure',
               'model_followup_action': '建议复核<script>alert(1)</script>。'}
    rows = prose_reading_sections([section], analysis_kind='benchmark')
    assert rows[-1]['display_parts'][0]['text'] == prose
    markup = reading_citation_html(rows, [])
    assert '<script>' not in markup
    assert '&lt;script&gt;标题&lt;/script&gt;' in markup
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in markup


def test_report_styling_opt_in_includes_new_deterministic_sections_only():
    from report.export import _bound_reading
    old = {'schema_version': 'report-payload/2.1', 'sections': [{'text': '历史原文。'}]}
    assert not _bound_reading(old)
    current = deepcopy(old)
    current['sections'][0]['reading_style'] = 'contextual-reading/1.4'
    assert _bound_reading(current)
    assert not _bound_reading({**current, 'schema_version': 'report-payload/1.0'})
    assert old == {'schema_version': 'report-payload/2.1', 'sections': [{'text': '历史原文。'}]}


def test_main_navigation_removes_manufacturing_but_keeps_supported_module():
    root = Path(__file__).resolve().parents[1]
    nav = (root / 'enterprise_app.py').read_text(encoding='utf-8')
    assert "'app_pages/manufacturing.py'" not in nav
    assert "'app_pages/benchmark.py'" in nav
    assert (root / 'app_pages/manufacturing.py').is_file()
    assert (root / 'enterprise/manufacturing_api.py').is_file()
