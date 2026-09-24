"""Authorized one-month forecasts; observed values never masquerade as forecasts."""
import json

import altair as alt
import pandas as pd
import streamlit as st

from app_pages._shared import page_context
from enterprise.forecast import ForecastInputError, METHOD_LABELS, forecast_method_availability
from enterprise.security import require


def _reset_invalid_selection(key, options, default=None):
    """Repair dependent widget state before rendering the widget, not afterwards."""
    if st.session_state.get(key) not in options:
        st.session_state[key] = options[0] if default is None else default


def _input_message(exc):
    """Translate known engine input failures without exposing raw exception text."""
    text = str(exc)
    if 'contiguous months' in text:
        return '所选方法的连续月份不足。请选择更晚的截止月或上期持平；缺失月份不能补齐或跨越。'
    if 'cutoff_month must have' in text:
        return '所选截止月没有对应工厂、产品和规格的实际成本。请刷新数据并重新选择月份。'
    if 'duplicate' in text:
        return '成本汇总存在重复身份记录或列名。请在数据管理中核对同工厂、产品、规格和月份的唯一记录。'
    if 'components do not sum' in text:
        return '成本三要素与单位成本不闭合。请先在数据管理中校验并确认修订，再计算预测。'
    if 'finite' in text or 'numeric output range' in text:
        return '成本数据含空值、负数、非有限值或超出计算范围。请核对原始金额并确认修订。'
    if 'snapshot' in text:
        return '成本数据版本不一致。请刷新已确认快照后重新计算，勿混用不同版本。'
    if 'YYYY-MM' in text or 'year' in text:
        return '月份格式无效。请使用 YYYY-MM 格式的有效年月，并保证下一月在支持范围内。'
    return '预测输入未通过校验。请核对成本表必填列、工厂、产品、规格、月份和金额，确认修订后重新计算。'


principal, application = page_context('analysis.generate')
st.title('成本预测基线')
st.caption('用已确认成本数据预测下一月单位成本，给出较本月方向与目标月预算对照；不自动选最优方法。')

# Only known data-boundary failures become business messages. UI/pandas bugs propagate.
try:
    tables = application.tables()
except PermissionError:
    st.error('当前账号无权读取预测数据。请切换到已授权账号，或联系管理员核对数据范围。')
    st.stop()
except ValueError as exc:
    if not str(exc).startswith('当前授权数据校验失败：'):
        raise
    st.error('当前授权数据校验失败。请在数据管理中查看校验明细并确认修订后重试。')
    st.stop()

columns = ['工厂', '产品名称', '产品规格', '月份']
frames = [frame for key, frame in tables.items() if key in ('cost25', 'cost26', 'erchang25', 'erchang26')
          and not frame.empty and set(columns).issubset(frame.columns)]
if not frames:
    st.info('当前授权范围内没有可用于预测的成本汇总。请先确认数据或联系管理员核对授权。')
    st.stop()
choices = pd.concat([frame[columns] for frame in frames], ignore_index=True).drop_duplicates()
with st.container(horizontal=True):
    factories = sorted(choices['工厂'].unique())
    _reset_invalid_selection('forecast_factory', factories)
    factory = st.selectbox('预测工厂', factories, key='forecast_factory')
    products = sorted(choices.loc[choices['工厂'].eq(factory), '产品名称'].unique())
    _reset_invalid_selection('forecast_product', products)
    product = st.selectbox('预测产品', products, key='forecast_product')
    scoped = choices[choices['工厂'].eq(factory) & choices['产品名称'].eq(product)]
    specs = sorted(scoped['产品规格'].unique())
    _reset_invalid_selection('forecast_spec', specs)
    spec = st.selectbox('预测规格', specs, key='forecast_spec')
months = sorted(scoped.loc[scoped['产品规格'].eq(spec), '月份'].unique())
labels = METHOD_LABELS
# These widgets must be outside a form: changing cutoff reruns method availability.
with st.container(border=True):
    with st.container(horizontal=True):
        _reset_invalid_selection('forecast_cutoff', months, months[-1])
        cutoff = st.selectbox('数据截止月', months, key='forecast_cutoff')
        try:
            availability = forecast_method_availability(months, cutoff)
        except ForecastInputError as exc:
            st.error(_input_message(exc))
            st.stop()
        available = availability['available_methods']
        if not available:
            st.warning('该截止月缺少实际数据，请刷新后选择已有月份。')
            st.stop()
        _reset_invalid_selection('forecast_method', available)
        method = st.selectbox('预测方法', available, format_func=labels.get, key='forecast_method')
    for explanation in availability['unavailable_methods'].values():
        st.caption(explanation)
    st.caption('只预测一个月。缺失月份不补齐，训练不会跨过断档；未来产量未知，因此不预测总成本。')
    if method == 'ses':
        st.caption('固定 α=0.3，以连续段首月初始化后递推，未用回测调参。指数递减权重不同于近三月等权均值；α=0.5也不等价于MA3，并非必须避开。')
    submitted = st.button('计算预测与历史检验', type='primary', icon=':material/trending_up:', key='forecast_compute')
try:
    require(principal, 'analysis.generate', factory=factory, product=product)
except PermissionError:
    st.session_state.pop('forecast_result', None)
    st.error('当前账号无权预测该工厂或产品。请选择授权范围，或联系管理员。')
    st.stop()
identity = (factory, product, spec, cutoff, method)
if submitted:
    st.session_state.pop('forecast_result', None)
    with st.spinner('计算基线与逐月历史检验…'):
        try:
            result = application.forecast(factory=factory, product=product, specification=spec,
                                          cutoff_month=cutoff, method=method)
        except PermissionError:
            st.error('预测权限已变化。请刷新登录状态并重新选择已授权范围。')
            st.stop()
        except ForecastInputError as exc:
            st.error(_input_message(exc))
            st.stop()
        except ValueError as exc:
            if str(exc) != '预测输入缺少一致的已授权数据快照' and not str(exc).startswith('当前授权数据校验失败：'):
                raise
            st.error('已确认数据快照缺失、不一致或未通过校验。请刷新数据并在数据管理中确认修订后重试。')
            st.stop()
    st.session_state['forecast_result'] = (identity, result)
stored = st.session_state.get('forecast_result')
if stored:
    old_result = stored[1]
    stored_hash = (old_result['provenance'].get('snapshot_meta') or {}).get('cost_snapshot_hash')
    current_hashes = {frame.attrs.get('cost_snapshot_hash') for frame in tables.values() if not frame.empty}
    if (not stored_hash or current_hashes != {stored_hash}
            or 'direction' not in old_result or 'budget_comparison' not in old_result):
        st.session_state.pop('forecast_result', None)
        stored = None
        st.info('已确认数据版本或预测口径发生变化，请重新计算预测。')
if stored and stored[0] == identity:
    result = stored[1]
    point = result['forecast']
    direction = result['direction']
    with st.container(horizontal=True):
        st.metric(f"{point['target_month']} 单位成本", f"{point['unit_cost']:.4f} 元/盒")
        st.metric('连续历史月份', len(result['training_months']))
        st.metric('相同历史检验月份', result['backtests'][method]['sample_count'])
    direction_label = {'up': '上升', 'down': '下降', 'flat': '持平'}[direction['direction']]
    percent_text = (f"{direction['delta_percent']:+.2f}%" if direction['delta_percent'] is not None
                    else '本月为零或比例超出范围，不计算百分比')
    st.write(f"下月较本月：{direction_label}，变化 {direction['delta']:+.4f} 元/盒（{percent_text}）。")
    st.caption(direction['note'])
    st.caption(f"{factory} · {product} · {spec}；使用 {result['training_months'][0]} 至 {cutoff} 的连续数据。")
    if result['gaps']:
        st.info('历史断档：' + '；'.join('、'.join(row['missing_months']) for row in result['gaps']) + '。已排除断档前的数据。')
    chart_rows = [{'月份': row['month'], '单位成本（元/盒）': row['unit_cost'], '类型': '历史实际'}
                  for row in result['history']]
    chart_rows.append({'月份': point['target_month'], '单位成本（元/盒）': point['unit_cost'], '类型': '下一月预测'})
    chart_data = pd.DataFrame(chart_rows)
    base = alt.Chart(chart_data).encode(
        x=alt.X('月份:O', sort=[row['月份'] for row in chart_rows]),
        y=alt.Y('单位成本（元/盒）:Q', scale=alt.Scale(zero=False)),
        color=alt.Color('类型:N', scale=alt.Scale(domain=['历史实际', '下一月预测'], range=['#0F766E', '#D97706'])),
        tooltip=['月份:O', '类型:N', alt.Tooltip('单位成本（元/盒）:Q', format='.4f')],
    )
    # The forecast is a single explicit point, never an artificial historical anchor.
    chart = base.transform_filter(alt.datum['类型'] == '历史实际').mark_line() + base.mark_point(filled=True, size=80)
    st.altair_chart(chart, width='stretch')
    st.caption('圆点区分历史实际与下一月预测；未将最后一个历史实际值放入预测系列。纵轴为局部成本范围，不从零起。')

    st.subheader('目标月预算对照')
    budget = result['budget_comparison']
    if budget['status'] == 'matched':
        comparison = {'above': '高于', 'below': '低于', 'equal': '等于'}[budget['direction']]
        budget_percent = f"{budget['delta_percent']:+.2f}%" if budget['delta_percent'] is not None else '预算为零或比例超出范围，不计算百分比'
        st.write(f"{budget['target_month']} 预算 {budget['budget_unit_cost']:.4f} 元/盒；预测{comparison}预算，"
                 f"差额（预测−预算）{budget['delta']:+.4f} 元/盒（{budget_percent}）。")
        st.caption(budget['note'])
    else:
        st.info(budget['note'])
    st.caption('仅使用输入快照中精确匹配目标年月的预算，不跨年、跨月套值；历史截止月的预算版本并非经核验的当时快照。')
    st.subheader('下一月三要素')
    st.dataframe(pd.DataFrame([{'成本要素': name, '预测单位成本（元/盒）': value}
                              for name, value in point['elements'].items()]), hide_index=True,
                 column_config={'预测单位成本（元/盒）': st.column_config.NumberColumn(format='%.4f')})
    st.subheader('同一历史窗口的方法比较')
    metrics = [{'方法': labels[name], '检验月份数': item['sample_count'], 'MAE（元/盒）': item['mae'],
                'RMSE（元/盒）': item['rmse'], 'MAPE（%）': item['mape']}
               for name, item in result['backtests'].items()]
    st.dataframe(pd.DataFrame(metrics), hide_index=True, column_config={
        '检验月份数': st.column_config.NumberColumn(format='%d'),
        **{column: st.column_config.NumberColumn(format='%.4f')
           for column in ('MAE（元/盒）', 'RMSE（元/盒）', 'MAPE（%）')},
    })
    st.caption('每个检验月只使用此前连续月份。按当前数据版本回溯，并非当时留存快照的在线预测；不自动选取误差最小方法。空值表示无可用检验样本，或零实际值导致MAPE未定义。')
    st.info(f"可用于检验的误差样本为 {result['interval']['n_residuals']} 个；未提供校准区间或覆盖率。短样本不足以可靠识别趋势或区分方法；统计不显著不等于证明无趋势。结果仅作成本讨论基线，不能作为预算或收益承诺。")
    with st.expander('逐月检验与来源'):
        folds = []
        for name, item in result['backtests'].items():
            for fold in item['folds']:
                folds.append({'方法': labels[name], '训练截至': fold['origin_month'], '检验月份': fold['target_month'],
                              '预测（元/盒）': fold['prediction']['unit_cost'], '实际（元/盒）': fold['actual']['unit_cost'],
                              '误差（元/盒）': fold['errors']['unit_cost']})
        if folds:
            st.dataframe(pd.DataFrame(folds), hide_index=True, column_config={
                column: st.column_config.NumberColumn(format='%.4f')
                for column in ('预测（元/盒）', '实际（元/盒）', '误差（元/盒）')})
        else:
            st.caption('尚无共同历史检验月份；请补充连续月份后再比较。')
        st.json(result['provenance'])
        st.json(budget)
    st.download_button('下载预测与检验记录', json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False),
                       file_name=f'forecast-{product}-{cutoff}-{method}.json', mime='application/json')
elif stored:
    st.caption('选择已变化，请重新计算以显示当前范围的预测。')
