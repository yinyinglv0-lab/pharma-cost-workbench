"""Historical and vertical comparisons must use actual same-scope source rows."""
from copy import deepcopy
from decimal import Decimal

import pandas as pd
import pytest

from enterprise.benchmark import benchmark_options, build_benchmark, build_multi_product_summary


def row(factory, month, *, spec='S', unit='6', volume=100):
    value = Decimal(unit)
    return {'工厂': factory, '产品名称': '合成产品', '产品规格': spec, '月份': month,
            '直接材料(元/盒)': str(value-2), '直接人工(元/盒)': '1', '制造费用(元/盒)': '1',
            '单位成本(元/盒)': str(value), '产量(盒)': volume, '总成本(元)': str(value*volume),
            '_source_file': factory+'_'+month+'.csv', '_source_row': 2}


def tables():
    return {key: pd.DataFrame([row(factory, year+'-'+str(m).zfill(2), unit=unit)
                              for m in range(1, 7)])
            for key, factory, year, unit in [('cost25','中药一厂','2025','6'),
                ('cost26','中药一厂','2026','7'),('erchang25','中药二厂','2025','8'),
                ('erchang26','中药二厂','2026','9')]}


def test_options_and_comparison_cover_both_real_half_years():
    data = tables()
    options = benchmark_options(data)
    assert [o['month'] for o in options] == [f'{y}-{m:02}' for y in (2025, 2026) for m in range(1, 7)]
    for option in options:
        result = build_benchmark(**option, tables=data)
        assert result['available'], result['reason']
        assert result['unit_gap'] == -2
        assert result['normalized_amount_exact'] == '-200'
        year = option['month'][2:4]
        assert [s['table'] for s in result['sources']] == ['cost'+year, 'erchang'+year]
        assert len(build_multi_product_summary(option['month'], data)) == 1
    assert not build_benchmark('合成产品','S','2025-07',data)['available']


def test_vertical_unit_and_amount_bridge_keep_years_and_sources():
    result = build_benchmark('合成产品','S','2026-05',tables())
    home = result['year_over_year']['home']
    assert home['available'] and home['comparison_month'] == '2025-05'
    assert home['unit_delta_exact'] == '1'
    assert home['total_delta_exact'] == '100'
    assert home['volume_effect_exact'] == '0' and home['unit_effect_exact'] == '100'
    assert Decimal(home['reconciliation_difference_exact']) == 0
    assert [s['table'] for s in home['sources']] == ['cost26', 'cost25']
    assert result['year_over_year']['peer']['previous_unit_cost'] == 8


def test_missing_prior_year_is_not_fabricated_or_blocking_current_peer():
    data = tables()
    data['cost25'] = pd.DataFrame()
    result = build_benchmark('合成产品','S','2026-05',data)
    assert result['available']
    assert result['year_over_year']['home']['available'] is False
    assert result['year_over_year']['peer']['available'] is True
    assert not build_benchmark('合成产品','S','2025-05',data)['available']


def test_2025_does_not_get_2026_material_details_or_previous_2024_values():
    data = tables()
    data['material'] = pd.DataFrame([{'工厂':'中药一厂','产品名称':'合成产品','产品规格':'S',
        '月份':'2026-05','原材料名称':'原料A','单位消耗成本(元/盒)':5,'原材料总成本(元)':500}])
    result = build_benchmark('合成产品','S','2025-05',data)
    assert result['available'] and result['home_materials'] == []
    assert not result['year_over_year']['home']['available']
    assert result['paired_drilldown']['材料']['paired_count'] == 0


def test_same_identity_across_partitions_fails_closed():
    data = tables()
    data['cost26'] = pd.concat([data['cost26'], data['cost25'].iloc[[0]]], ignore_index=True)
    result = build_benchmark('合成产品','S','2025-01',data)
    assert not result['available'] and '重复' in result['reason']


def test_missing_prior_specification_never_substitutes_other_spec():
    data = tables()
    data['cost25']['产品规格'] = 'OTHER'
    before = deepcopy(data)
    result = build_benchmark('合成产品','S','2026-05',data)
    assert result['available'] and not result['year_over_year']['home']['available']
    for key in data:
        pd.testing.assert_frame_equal(before[key],data[key])
