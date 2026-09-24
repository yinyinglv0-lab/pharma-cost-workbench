"""Synthetic offline specification-isolation regressions; no live tables or DB."""
import pandas as pd
import pytest

from dashboard.data_layer import (
    build_dashboard_data, discover_products, material_detail_series,
    labor_metrics_series, mfg_breakdown_series,
)

PRODUCT = '第五种测试品'
A, B = '10粒/盒', '100粒/盒'


def identity(spec, month, table, index):
    return {'工厂': '中药一厂', '产品名称': PRODUCT, '产品规格': spec, '月份': month,
            '_source_file': f'{table}-{spec}.csv', '_source_hash': f'hash-{table}-{spec}',
            '_source_row': index, '_source_sheet': 'CSV'}


def cost(spec, month, material, *, index=2):
    return {**identity(spec, month, 'cost', index), '直接材料(元/盒)': material,
            '直接人工(元/盒)': 2, '制造费用(元/盒)': 3,
            '单位成本(元/盒)': material + 5, '产量(盒)': 100,
            '总成本(元)': (material + 5) * 100}


def budget(spec, month, material):
    return {**identity(spec, month, 'budget', 7), '预算直接材料(元/盒)': material,
            '预算直接人工(元/盒)': 2, '预算制造费用(元/盒)': 3,
            '预算单位成本(元/盒)': material + 5, '预算产量(盒)': 100,
            '预算总成本(元)': (material + 5) * 100}


def tables():
    # Adversarial ordering: a naive first-row match sees the wrong specification.
    return {
        'cost26': pd.DataFrame([cost(B, '2026-01', 100), cost(A, '2026-01', 10),
                               cost(B, '2026-02', 900), cost(A, '2026-02', 20, index=9)]),
        'cost25': pd.DataFrame([cost(B, '2025-02', 800), cost(A, '2025-02', 5)]),
        'budget': pd.DataFrame([budget(B, '2026-02', 700), budget(A, '2026-02', 8)]),
        'material': pd.DataFrame([{**identity(s, '2026-02', 'material', i),
                                  '原材料名称': f'材料-{s}', '单位消耗成本(元/盒)': i,
                                  '原材料总成本(元)': i * 100, '占总材料成本比例': '100%'}
                                 for i, s in enumerate([B, A], 2)]),
        'labor': pd.DataFrame([{**identity(s, '2026-02', 'labor', i),
                               '直接人工总额(元)': i * 100, '总工时(小时)': 10,
                               '生产人数(人)': 2, '工作天数(天)': 5, '产量(盒)': 100}
                              for i, s in enumerate([B, A], 2)]),
        'mfg': pd.DataFrame([{**identity(s, '2026-02', 'mfg', i), '费用类别': f'费用-{s}',
                             '单位费用(元/盒)': i, '费用总额(元)': i * 100}
                            for i, s in enumerate([B, A], 2)]),
    }


def test_multiple_specs_require_explicit_keyword_selection():
    with pytest.raises(ValueError, match='多个产品规格'):
        build_dashboard_data(PRODUCT, tables())
    with pytest.raises(ValueError, match='无当期成本数据'):
        build_dashboard_data(PRODUCT, tables(), specification='missing')
    with pytest.raises(TypeError):
        build_dashboard_data(PRODUCT, tables(), A)


def test_current_previous_yoy_and_budget_are_same_spec_with_physical_source_identity():
    inputs = tables()
    before = {name: frame.copy(deep=True) for name, frame in inputs.items()}
    actual = build_dashboard_data(PRODUCT, inputs, specification=A)
    assert actual['specification'] == A
    assert [r['材料'] for r in actual['series']] == [10, 20]
    assert actual['mom'][-1]['材料'] == 100
    assert actual['yoy'][-1]['材料'] == 300
    assert actual['budget_var'][-1]['材料'] == 150
    assert actual['amount_change'][-1]['材料变动额'] == 1000
    assert actual['amount_change'][-1]['总变动额'] == 1000
    sources = actual['source_rows'][-1]
    for key in ('current', 'previous', 'yoy', 'budget'):
        assert sources[key]['产品规格'] == A
        assert A in sources[key]['_source_file']
        assert sources[key]['_source_hash'].endswith(A)
    assert sources['current']['_source_row'] == 9
    assert sources['yoy']['月份'] == '2025-02'
    assert sources['previous']['月份'] == '2026-01'
    for name in inputs:
        pd.testing.assert_frame_equal(inputs[name], before[name])


@pytest.mark.parametrize('missing', ['previous', 'yoy', 'budget'])
def test_missing_same_spec_counterpart_does_not_borrow_other_spec(missing):
    inputs = tables()
    key, month = {'previous': ('cost26', '2026-01'), 'yoy': ('cost25', '2025-02'),
                  'budget': ('budget', '2026-02')}[missing]
    frame = inputs[key]
    inputs[key] = frame.loc[~(frame['产品规格'].eq(A) & frame['月份'].eq(month))]
    result = build_dashboard_data(PRODUCT, inputs, specification=A)
    metric = {'previous': 'mom', 'yoy': 'yoy', 'budget': 'budget_var'}[missing]
    assert result[metric][-1]['材料'] is None
    assert result['source_rows'][-1][missing] is None
    if missing == 'previous':
        assert result['amount_change'][-1]['总变动额'] is None


@pytest.mark.parametrize('helper, list_key', [(material_detail_series, 'materials'),
                                            (labor_metrics_series, None),
                                            (mfg_breakdown_series, 'items')])
def test_all_detail_sources_obey_same_spec(helper, list_key):
    inputs = tables()
    with pytest.raises(ValueError, match='多个产品规格'):
        helper(PRODUCT, inputs)
    results = helper(PRODUCT, inputs, specification=A)
    assert len(results) == 1
    rows = results[0][list_key] if list_key else results
    assert len(rows) == 1 and rows[0]['source']['产品规格'] == A
    key = {material_detail_series: 'material', labor_metrics_series: 'labor',
           mfg_breakdown_series: 'mfg'}[helper]
    inputs[key] = inputs[key].loc[inputs[key]['产品规格'].eq(B)]
    assert helper(PRODUCT, inputs, specification=A) == []


def test_missing_spec_labels_do_not_establish_counterpart_identity():
    inputs = tables()
    inputs['cost25'] = inputs['cost25'].drop(columns='产品规格')
    inputs['budget'] = inputs['budget'].drop(columns='产品规格')
    assert build_dashboard_data(PRODUCT, inputs, specification=A)['yoy'][-1]['材料'] is None
    assert build_dashboard_data(PRODUCT, inputs, specification=A)['budget_var'][-1]['材料'] is None
    inputs['cost26'].loc[0, '产品规格'] = None
    inputs['cost26'] = inputs['cost26'].loc[inputs['cost26']['产品规格'].ne(B)]
    with pytest.raises(ValueError, match='未标明产品规格'):
        build_dashboard_data(PRODUCT, inputs)


def test_duplicate_counterparts_are_unavailable_not_first_row_wins():
    inputs = tables()
    inputs['budget'] = pd.concat([inputs['budget'], inputs['budget'].iloc[[1]]], ignore_index=True)
    inputs['cost25'] = pd.concat([inputs['cost25'], inputs['cost25'].iloc[[1]]], ignore_index=True)
    result = build_dashboard_data(PRODUCT, inputs, specification=A)
    assert result['yoy'][-1]['材料'] is None and '不唯一' in result['yoy'][-1]['_note']
    assert result['budget_var'][-1]['材料'] is None and '不唯一' in result['budget_var'][-1]['_note']
    inputs['cost26'] = pd.concat([inputs['cost26'], inputs['cost26'].iloc[[1]]], ignore_index=True)
    with pytest.raises(ValueError, match='多条成本记录'):
        build_dashboard_data(PRODUCT, inputs, specification=A)


def test_single_spec_and_unlabelled_legacy_preserve_numeric_results_and_box_boundary():
    inputs = {key: frame.loc[frame['产品规格'].eq(A)].copy() for key, frame in tables().items()}
    inferred = build_dashboard_data(PRODUCT, inputs)
    selected = build_dashboard_data(PRODUCT, inputs, specification=A)
    assert inferred == selected
    legacy = {key: frame.drop(columns='产品规格') for key, frame in inputs.items()}
    unlabelled = build_dashboard_data(PRODUCT, legacy)
    for key in ('series', 'mom', 'yoy', 'budget_var', 'amount_change', 'contribution', 'contribution_raw'):
        assert unlabelled[key] == inferred[key]
    assert discover_products(inputs) == [PRODUCT]
    assert inferred['quantity_unit'] == '盒' and '自动换算' in inferred['quantity_unit_boundary']
