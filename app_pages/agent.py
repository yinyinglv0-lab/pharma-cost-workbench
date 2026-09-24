"""Bounded conversation over existing authorized analysis services."""
import json

import pandas as pd
import streamlit as st

from app_pages._shared import page_context
from app_pages.design import page_header
from enterprise.agent_router import execute_request
from enterprise.security import can, require

principal, application = page_context('data.read')
page_header('分析助手', '分析辅助 / 在当前范围内查询成本、证据与预测，或准备报告参数。')

try:
    tables = application.tables()
    columns = ['工厂', '产品名称', '产品规格', '月份']
    frames = [frame for key, frame in tables.items() if key in ('cost25', 'cost26', 'erchang25', 'erchang26')
              and not frame.empty and set(columns).issubset(frame.columns)]
    if not frames:
        st.info('当前授权范围内没有可供查询的成本数据。')
        st.stop()
    choices = pd.concat([frame[columns] for frame in frames], ignore_index=True).drop_duplicates()
    with st.container(horizontal=True):
        factory = st.selectbox('当前工厂', sorted(choices['工厂'].unique()), key='agent_factory')
        subset = choices[choices['工厂'].eq(factory)]
        product = st.selectbox('当前产品', sorted(subset['产品名称'].unique()), key='agent_product')
        subset = subset[subset['产品名称'].eq(product)]
        specification = st.selectbox('当前规格', sorted(subset['产品规格'].unique()), key='agent_specification')
        months = sorted(subset.loc[subset['产品规格'].eq(specification), '月份'].unique())
        month = st.selectbox('当前月份', months, index=len(months) - 1, key='agent_month')
    context = {'factory': factory, 'product': product, 'specification': specification, 'month': month}
    st.caption('每次处理一项请求。结果限定在所选工厂、产品、规格和月份，切换范围会清空本页对话。')
    revision_hashes = sorted({frame.attrs.get('cost_snapshot_hash', '') for frame in frames})
    scope = (factory, product, specification, month, tuple(revision_hashes))
    if st.session_state.get('agent_conversation_scope') != scope:
        st.session_state['agent_messages'] = []
        st.session_state['agent_conversation_scope'] = scope
    messages = st.session_state.setdefault('agent_messages', [])
    selected_prompt = None
    if not messages:
        st.subheader('从一项业务问题开始')
        with st.container(horizontal=True):
            for label, example, icon in [('成本汇总', '查看成本汇总', 'payments'),
                                         ('跨厂对标', '做跨厂对标', 'compare_arrows'),
                                         ('预测下一月', '预测下一月单位成本', 'trending_up'),
                                         ('准备报告', '准备报告表单', 'description')]:
                if st.button(label, key='agent_example_' + icon, icon=f':material/{icon}:'):
                    selected_prompt = example
        with st.expander('支持哪些问题'):
            st.write('成本汇总 · 成本归因 · 跨厂对标 · 知识检索 · 下一月预测 · 查看报告 · 准备报告表单')
            st.caption('例如：检索知识 工艺收率。系统使用固定规则选择已授权工具；报告准备只带入参数，后续在报告中心操作。')
    if st.button('清空本页对话', key='agent_clear', icon=':material/refresh:', disabled=not messages):
        st.session_state['agent_messages'] = []
        st.rerun()
    typed_prompt = st.chat_input('输入本次分析请求', key='agent_input', submit_mode='disable', max_chars=1000)
    prompt = selected_prompt or typed_prompt
    if prompt:
        messages.append({'role': 'user', 'text': prompt})
        with st.status('正在核对范围并执行查询', expanded=False) as status:
            response = execute_request(application, prompt, context)
            status.update(label='查询完成' if response['status'] == 'completed' else '查询服务暂不可用' if response['status'] == 'failed' else '请调整本次请求',
                          state='complete' if response['status'] == 'completed' else 'error')
        messages.append({'role': 'assistant', 'response': response})
        # Session-only, bounded history; never use previous answers as tool input.
        st.session_state['agent_messages'] = messages[-20:]
        messages = st.session_state['agent_messages']
    for index, entry in enumerate(messages):
        with st.chat_message(entry['role']):
            if entry['role'] == 'user':
                st.text(entry['text'])
                continue
            response = entry['response']
            if response['status'] == 'failed':
                st.error(response['answer'])
                st.caption('无需改写问题。请稍后重试；管理员可按错误编号查询服务日志。')
            elif response['status'] == 'denied':
                st.warning(response['answer'])
            else:
                st.write(response['answer'])
            tool = response['plan'].get('tool')
            result = response.get('result') or {}
            if response['status'] == 'completed':
                if tool == 'cost_summary':
                    st.dataframe(pd.DataFrame([{'成本要素': name, '单位成本（元/盒）': amount}
                                                for name, amount in result['elements'].items()]), hide_index=True)
                elif tool == 'forecast':
                    point = result['forecast']
                    st.metric(point['target_month'] + ' 预测单位成本', f"{point['unit_cost']:.2f} 元/盒")
                    st.caption(f"共同历史检验 {result['backtests'][result['method']]['sample_count']} 个月；未提供校准区间。")
                    if can(principal, 'analysis.generate'):
                        st.page_link('app_pages/forecast.py', label='查看完整预测与检验', icon=':material/trending_up:')
                elif tool == 'attribution':
                    for section in result.get('sections', []):
                        st.write(section.get('text', section.get('summary', '')))
                elif tool == 'benchmark':
                    rows = result.get('rows') or result.get('elements') or []
                    if isinstance(rows, list) and rows:
                        st.dataframe(pd.DataFrame(rows), hide_index=True)
                elif tool == 'knowledge_search':
                    for source in result.get('sources', []):
                        location = source.get('source', {})
                        with st.expander(str(location.get('filename') or source.get('chunk_id') or '授权知识片段')):
                            st.text(source['text'])
                            st.caption(f"版本 {source.get('version_id')} · 发布 {source.get('release_id')}")
                            st.caption('证据用途：' + ('可用于核查机制' if source.get('evidence_role') == 'document_basis' else '仅作背景资料'))
                            st.caption(source.get('claim_boundary', '仍需核对本期实际业务证据。'))
                            st.write('适用范围', source.get('applicability', {}))
                elif tool == 'prepare_report':
                    params = result['params']
                    st.dataframe(pd.DataFrame([{'产品': params['product'], '规格': params['specification'],
                                                '月份': params['month'], '主题': params['theme']}]), hide_index=True)
                    if st.button('填入报告页', key=f'agent_report_{index}', icon=':material/description:'):
                        require(principal, 'report.generate', factory='中药一厂', product=params['product'])
                        st.session_state['report_agent_prefill'] = {key: params[key] for key in
                            ('product', 'specification', 'month', 'theme', 'formal', 'use_llm', 'include_benchmark')}
                        st.switch_page('report_web.py')
                elif tool == 'open_reports' and can(principal, 'report.read'):
                    st.page_link('report_web.py', label='打开报告中心', icon=':material/description:')
            with st.expander('本次使用的范围、操作与来源'):
                st.json({'status': response['status'], 'arguments': response['plan'].get('arguments'),
                         'trace': response['trace'], 'provenance': response['provenance'], 'effects': response['effects']})
            st.download_button('下载本次查询记录', json.dumps(response, ensure_ascii=False, indent=2),
                               file_name='analysis-assistant-result.json', mime='application/json', key=f'agent_download_{index}')
except (PermissionError, ValueError) as exc:
    st.error(str(exc))
