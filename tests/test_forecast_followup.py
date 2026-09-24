"""FC follow-up: exact arithmetic, scoped budget evidence, headless page interaction.

No service startup, database write, model call, or network access is permitted.
"""
from copy import deepcopy
from decimal import Decimal
from fractions import Fraction
import json
from pathlib import Path
import socket
import sys

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from enterprise.forecast import (
    ELEMENTS, ForecastInputError, METHOD_LABELS, forecast_baseline, forecast_method_availability,
)

FACTORY, PRODUCT, SPEC = '中药一厂', '测试产品', 'S'


def cost_row(month, value=1, labor=2, overhead=3):
    values = list(map(lambda x: Decimal(str(x)), (value, labor, overhead)))
    return {'工厂': FACTORY, '产品名称': PRODUCT, '产品规格': SPEC, '月份': month,
            **dict(zip(ELEMENTS.values(), map(str, values))), '单位成本(元/盒)': str(sum(values))}


def table_data():
    return {'cost25': pd.DataFrame([cost_row(f'2025-{n:02d}', n + 100) for n in range(1, 7)]),
            'cost26': pd.DataFrame([cost_row(f'2026-{n:02d}', n) for n in range(1, 7)]),
            'budget': pd.DataFrame()}


def budget_row(month='2026-07', unit='10'):
    return {'工厂': FACTORY, '产品名称': PRODUCT, '产品规格': SPEC, '月份': month,
            '预算单位成本(元/盒)': unit, '_source_file': 'fixture-budget.csv',
            '_source_hash': 'b' * 64, '_source_row': 2, '_source_sheet': 'CSV'}


def forecast(data=None, **options):
    params = dict(factory=FACTORY, product=PRODUCT, specification=SPEC, cutoff_month='2026-06')
    params.update(options)
    return forecast_baseline(table_data() if data is None else data, **params)


@pytest.fixture(autouse=True)
def deny_external_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('followup tests prohibit network or managed database access')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    import enterprise.cost_imports as imports
    monkeypatch.setattr(imports.CostRepository, '_connect', forbidden)


@pytest.mark.parametrize('month,expected', [('2026-01', ['naive']), ('2026-02', ['naive', 'ses']),
                                           ('2026-03', ['naive', 'ma3', 'ses'])])
def test_availability_depends_on_contiguous_calendar_segment(month, expected):
    months = [f'{year}-{n:02d}' for year in (2025, 2026) for n in range(1, 7)]
    result = forecast_method_availability(months, month)
    assert result['available_methods'] == expected
    for note in result['unavailable_methods'].values():
        assert '连续月份' in note and '请选择' in note


def test_availability_does_not_bridge_gap_or_assume_cutoff_exists():
    months = ['2025-12', '2026-01', '2026-03', '2026-04']
    assert forecast_method_availability(months, '2026-01')['contiguous_months'] == 2
    assert forecast_method_availability(months, '2026-03')['available_methods'] == ['naive']
    assert forecast_method_availability(months, '2026-02')['available_methods'] == []


def test_ses_exact_recursive_state_common_origins_and_fold_replay():
    data = table_data()
    result = forecast(data, method='ses')
    assert result['schema_version'] == 'cost-forecast/1.0'
    assert result['method_parameters']['alpha_exact'] == '3/10'
    level = Fraction(1)
    for n in range(2, 7):
        level = Fraction(3, 10) * n + Fraction(7, 10) * level
    assert Fraction(result['forecast']['elements_exact']['材料']) == level
    assert result['method_training_months'] == [f'2026-{n:02d}' for n in range(1, 7)]
    assert result['backtest_policy']['common_origins'] == ['2026-03', '2026-04', '2026-05']
    assert set(result['backtests']) == {'naive', 'ma3', 'ses'}
    assert {x['sample_count'] for x in result['backtests'].values()} == {3}
    for fold in result['backtests']['ses']['folds']:
        replay = forecast(data, method='ses', cutoff_month=fold['origin_month'])
        assert fold['prediction'] == {k: v for k, v in replay['forecast'].items() if k != 'target_month'}
        assert fold['training_hash'] == replay['provenance']['training_hash']
        assert fold['method_training_months'] == fold['training_months']
        assert Fraction(fold['prediction']['unit_cost_exact']) == sum(map(Fraction, fold['prediction']['elements_exact'].values()))
    assert result['interval']['lower'] is result['interval']['upper'] is result['interval']['coverage'] is None
    assert result['method_selection']['status'] == 'not_performed'


def test_ses_is_not_ma3_and_alpha_half_is_not_an_equivalence(monkeypatch):
    import enterprise.forecast as module
    data = {'cost26': pd.DataFrame([cost_row(f'2026-{n:02d}', n) for n in range(1, 5)])}
    monkeypatch.setattr(module, 'SES_ALPHA', Fraction(1, 2))
    ses = forecast(data, method='ses', cutoff_month='2026-04')
    ma3 = forecast(data, method='ma3', cutoff_month='2026-04')
    assert ses['forecast']['elements_exact']['材料'] == '25/8'
    assert ma3['forecast']['elements_exact']['材料'] == '3'
    assert ses['forecast'] != ma3['forecast']


def test_ses_no_future_leakage_no_gap_crossing_and_one_step_only():
    data = table_data()
    expected = forecast(data, method='ses')
    data['cost25'].loc[:, list(ELEMENTS.values())] = ['99999', '2', '3']
    data['cost25'].loc[:, '单位成本(元/盒)'] = '100004'
    data['cost26'] = pd.concat([data['cost26'], pd.DataFrame([cost_row('2026-07', 888)])], ignore_index=True)
    assert forecast(data, method='ses') == expected
    with pytest.raises(ForecastInputError, match='2 contiguous'):
        forecast(data, method='ses', cutoff_month='2026-01')
    with pytest.raises(ForecastInputError, match='one-step'):
        forecast(data, method='ses', horizon=2)


@pytest.mark.parametrize('values,method,expected,delta', [
    ([1, 2, 3], 'ma3', 'down', '-1'), ([3, 2, 1], 'ma3', 'up', '1'),
    ([1, 2, 3], 'naive', 'flat', '0'), ([0, 0, 0], 'naive', 'flat', '0'),
])
def test_programmatic_direction_is_exact_point_change_not_statistical_trend(values, method, expected, delta):
    data = {'cost26': pd.DataFrame([cost_row(f'2026-{n:02d}', x, 0, 0) for n, x in enumerate(values, 1)])}
    direction = forecast(data, method=method, cutoff_month='2026-03')['direction']
    assert direction['direction'] == expected
    assert direction['delta_exact'] == delta
    assert direction['trend_inference'] == 'not_performed'
    if values[-1] == 0:
        assert direction['delta_percent'] is direction['delta_percent_exact'] is None
        assert direction['percent_status'] == 'zero_reference'
    else:
        assert Fraction(direction['delta_percent_exact']) == Fraction(delta) / values[-1] * 100


def test_zero_reference_positive_forecast_never_invents_percent():
    data = {'cost26': pd.DataFrame([cost_row(f'2026-{n:02d}', x, 0, 0) for n, x in enumerate([3, 3, 0], 1)])}
    direction = forecast(data, method='ma3', cutoff_month='2026-03')['direction']
    assert direction['direction'] == 'up'
    assert direction['delta_exact'] == '2'
    assert direction['delta_percent'] is None


def test_exact_budget_match_is_auditable_and_does_not_mutate_input():
    data = table_data()
    data['budget'] = pd.DataFrame([budget_row()])
    before = deepcopy(data)
    value = forecast(data)['budget_comparison']
    assert value['status'] == 'matched'
    assert value['budget_unit_cost_exact'] == '10'
    assert value['delta_exact'] == '1' and value['delta_percent_exact'] == '10'
    assert value['direction'] == 'above'
    assert value['source']['source_hash'] == 'b' * 64 and value['source']['source_row'] == 2
    assert len(value['source']['comparison_hash']) == 64
    assert value['historical_vintage_verified'] is False
    for name in data:
        pd.testing.assert_frame_equal(data[name], before[name])


@pytest.mark.parametrize('patch', [
    {'月份': '2026-06'}, {'月份': '2025-07'}, {'月份': '2027-07'}, {'月份': '07'},
    {'工厂': '中药二厂'}, {'产品名称': '其他产品'}, {'产品规格': '其他规格'},
])
def test_budget_never_borrows_month_year_factory_product_or_specification(patch):
    data = table_data()
    data['budget'] = pd.DataFrame([{**budget_row(), **patch}])
    result = forecast(data)['budget_comparison']
    assert result['status'] == 'unavailable'
    assert result['reason_code'] == 'missing_target_budget'
    assert result['budget_unit_cost'] is result['delta'] is None


@pytest.mark.parametrize('bad', [None, '-1', 'NaN', 'Infinity', True, 'bad'])
def test_invalid_budget_withholds_comparison_but_keeps_forecast(bad):
    data = table_data()
    data['budget'] = pd.DataFrame([budget_row(unit=bad)])
    result = forecast(data)
    assert result['status'] == 'ok'
    assert result['budget_comparison']['status'] == 'invalid'
    assert result['budget_comparison']['budget_unit_cost'] is None
    json.dumps(result, allow_nan=False)


def test_duplicate_budget_and_conflicting_snapshot_fail_closed():
    data = table_data()
    data['budget'] = pd.DataFrame([budget_row(), budget_row()])
    assert forecast(data)['budget_comparison']['reason_code'] == 'ambiguous_target_budget'
    data['budget'] = pd.DataFrame([budget_row()])
    data['cost26'].attrs['cost_snapshot_hash'] = 'a' * 64
    data['budget'].attrs['cost_snapshot_hash'] = 'b' * 64
    assert forecast(data)['budget_comparison']['reason_code'] == 'snapshot_mismatch'


@pytest.mark.parametrize('metadata', [float('nan'), '', 3, {'bad': 'hash'}])
def test_invalid_budget_snapshot_metadata_does_not_escape_as_invalid_json(metadata):
    data = table_data()
    data['budget'] = pd.DataFrame([budget_row()])
    data['budget'].attrs['cost_snapshot_hash'] = metadata
    result = forecast(data)
    assert result['budget_comparison']['reason_code'] == 'invalid_budget_snapshot'
    json.dumps(result, allow_nan=False)


def test_budget_components_and_zero_denominator():
    data = table_data()
    data['budget'] = pd.DataFrame([{**budget_row(), '预算直接材料(元/盒)': 1,
                                   '预算直接人工(元/盒)': 2, '预算制造费用(元/盒)': 3}])
    assert forecast(data)['budget_comparison']['reason_code'] == 'nonclosing_budget'
    data['budget'] = pd.DataFrame([budget_row(unit=0)])
    value = forecast(data)['budget_comparison']
    assert value['status'] == 'matched' and value['delta_exact'] == '11'
    assert value['delta_percent'] is None and value['percent_status'] == 'zero_reference'


def test_real_csv_budget_only_matches_january_to_june_2026():
    names = {'cost25': '中药一厂_成本汇总_2025年1-6月.csv',
             'cost26': '中药一厂_成本汇总_2026年1-6月.csv', 'budget': '中药一厂_预算数据_2026年.csv'}
    if not all((ROOT / name).is_file() for name in names.values()):
        pytest.skip('original workspace CSVs unavailable')
    data = {key: pd.read_csv(ROOT / name) for key, name in names.items()}
    seen = set()
    for _, row in data['cost26'].loc[data['cost26']['月份'].eq('2026-06')].iterrows():
        params = dict(factory=row['工厂'], product=row['产品名称'], specification=row['产品规格'])
        may = forecast_baseline(data, **params, cutoff_month='2026-05', method='ses')['budget_comparison']
        june = forecast_baseline(data, **params, cutoff_month='2026-06')['budget_comparison']
        prior_year = forecast_baseline(data, **params, cutoff_month='2025-05')['budget_comparison']
        assert may['status'] == 'matched' and may['target_month'] == '2026-06'
        seen.add(may['budget_unit_cost'])
        assert june['reason_code'] == prior_year['reason_code'] == 'missing_target_budget'
    assert seen == {10.6, 7.0, 17.1}


def test_application_adapter_preserves_authorization_and_passes_original_budget(monkeypatch):
    from enterprise.application import Application
    from enterprise.security import Principal
    data = table_data()
    data['budget'] = pd.DataFrame([budget_row()])
    for frame in data.values():
        frame.attrs.update(cost_snapshot_hash='a' * 64, cost_revision=1)
    principal = Principal('forecast-test', '预测测试', ('analyst',), (FACTORY,), (PRODUCT,))
    application = Application(principal)
    monkeypatch.setattr(application, 'tables', lambda: data)
    result = application.forecast(factory=FACTORY, product=PRODUCT, specification=SPEC,
                                  cutoff_month='2026-06', method='ses')
    assert result['budget_comparison']['status'] == 'matched'
    assert result['provenance']['snapshot_meta']['cost_snapshot_hash'] == 'a' * 64
    with pytest.raises(PermissionError):
        application.forecast(factory='中药二厂', product=PRODUCT, specification=SPEC,
                             cutoff_month='2026-06', method='ses')


def test_malformed_budget_schema_and_partial_components_withhold_comparison():
    data = table_data()
    data['budget'] = pd.DataFrame([budget_row()]).drop(columns='工厂')
    assert forecast(data)['budget_comparison']['reason_code'] == 'invalid_budget_schema'
    data['budget'] = pd.DataFrame([{**budget_row(), '预算直接材料(元/盒)': 1}])
    assert forecast(data)['budget_comparison']['reason_code'] == 'incomplete_budget_components'


@pytest.fixture
def page_harness(monkeypatch):
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    from enterprise.security import Principal
    import app_pages._shared as shared

    class ApplicationStub:
        def __init__(self):
            self.data = table_data()
            self.data['budget'] = pd.DataFrame([budget_row('2026-06')])
            self.calls = []
            self.charts = []
            self.failure = None
            self.version = 'a' * 64

        def tables(self):
            for frame in self.data.values():
                frame.attrs['cost_snapshot_hash'] = self.version
                frame.attrs['cost_revision'] = 1
            return self.data

        def forecast(self, **kwargs):
            self.calls.append(kwargs)
            if self.failure:
                raise self.failure
            return forecast_baseline(self.tables(), **kwargs,
                                     snapshot_meta={'cost_snapshot_hash': self.version, 'cost_revision': 1})

    app = ApplicationStub()
    principal = Principal('forecast-test', '预测测试', ('analyst',), (FACTORY,), (PRODUCT,))
    monkeypatch.setattr(shared, 'page_context', lambda action: (principal, app))
    original_chart = st.altair_chart

    def capture_chart(chart, **kwargs):
        app.charts.append(chart.to_dict())
        return original_chart(chart, **kwargs)

    monkeypatch.setattr(st, 'altair_chart', capture_chart)
    # Isolated entrypoint executes the real page, without application startup or navigation.
    wrapper = f"import runpy\nrunpy.run_path({str(ROOT / 'app_pages' / 'forecast.py')!r})"
    return AppTest.from_string(wrapper, default_timeout=15), app


def test_page_cutoff_method_linkage_for_all_original_24_combinations(page_harness):
    at, app = page_harness
    at.run()
    assert not at.exception
    attempted, prevented = 0, 0
    for year in (2025, 2026):
        for month in range(1, 7):
            at.selectbox(key='forecast_cutoff').select(f'{year}-{month:02d}').run()
            assert not at.exception
            for method in ('naive', 'ma3'):
                if month < 3 and method == 'ma3':
                    assert METHOD_LABELS[method] not in at.selectbox(key='forecast_method').options
                    prevented += 1
                    continue
                at.selectbox(key='forecast_method').select(method).run()
                at.button(key='forecast_compute').click().run()
                assert not at.exception and not at.error
                attempted += 1
    assert (attempted, prevented) == (20, 4)
    assert len(app.calls) == 20
    at.selectbox(key='forecast_cutoff').select('2026-01').run()
    assert at.selectbox(key='forecast_method').value == 'naive'
    assert not at.metric  # the previous cutoff's result is no longer displayed


def test_page_ses_budget_direction_chart_and_numeric_formatting(page_harness):
    at, app = page_harness
    at.run()
    at.selectbox(key='forecast_cutoff').select('2026-05').run()
    at.selectbox(key='forecast_method').select('ses').run()
    at.button(key='forecast_compute').click().run()
    assert not at.exception and not at.error
    assert any('下月较本月：下降' in item.value for item in at.markdown)
    assert any('2026-06 预算 10.0000' in item.value for item in at.markdown)
    result = at.session_state['forecast_result'][1]
    assert len(result['backtests']) == 3
    chart = app.charts[-1]
    chart_rows = next(iter(chart['datasets'].values()))
    predictions = [row for row in chart_rows if row['类型'] == '下一月预测']
    assert predictions == [{'月份': '2026-06', '单位成本（元/盒）': result['forecast']['unit_cost'], '类型': '下一月预测'}]
    assert all(row['类型'] == '历史实际' for row in chart_rows[:-1])
    for element in at.dataframe:
        configs = json.loads(element.proto.columns)
        for name in element.value.columns:
            if '元/盒' in name or name == 'MAPE（%）':
                assert configs[name]['type_config']['format'] == '%.4f'


def test_page_discards_result_after_revision_change(page_harness):
    at, app = page_harness
    at.run().button(key='forecast_compute').click().run()
    assert at.metric and not at.exception
    app.version = 'c' * 64
    at.run()
    assert not at.metric
    assert any('重新计算' in x.value for x in at.info)


@pytest.mark.parametrize('failure', [ForecastInputError('ma3 requires 3 contiguous months ending at cutoff_month'),
                                     PermissionError('raw permission failure'),
                                     ValueError('预测输入缺少一致的已授权数据快照')])
def test_page_known_failures_are_actionable_chinese(page_harness, failure):
    at, app = page_harness
    app.failure = failure
    at.run().button(key='forecast_compute').click().run()
    assert not at.exception and at.error
    message = at.error[0].value
    assert '请' in message and 'requires' not in message and 'raw permission' not in message


def test_page_unexpected_value_error_is_not_swallowed_as_business_message(page_harness):
    at, app = page_harness
    app.failure = ValueError('unexpected internal defect')
    at.run().button(key='forecast_compute').click().run()
    assert at.exception
    assert 'unexpected internal defect' in at.exception[0].message
    assert not at.error
