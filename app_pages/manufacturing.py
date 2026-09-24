"""Explicit, authenticated canonical manufacturing onboarding and frozen analysis."""
import json
from pathlib import Path

import streamlit as st

from app_pages._shared import generation_notice, page_context, rerun_notice, show_notice
from app_pages.citations import render_layered_analysis
from app_pages.design import page_header
from dashboard.chart_link import chart_context_id, render_linked_echarts
from enterprise.domain_profiles import parse_domain_profile
from enterprise.manufacturing_adapter import parse_adapter_config
from enterprise.manufacturing_repository import ManufacturingRepository, MAX_BUNDLE_BYTES
from enterprise.manufacturing_service import ManufacturingService
from enterprise.security import can

principal, application = page_context('data.read')
repository = ManufacturingRepository(application.root, principal)
service = ManufacturingService(application.root, principal)
page_header('制造业配置与分析', '声明式领域配置 → 五表完整快照 → 归因与对标 → 冻结分析与任务草稿')
st.caption('制造业数据与原制药数据分开保存；安装配置不授予权限、不切换默认领域。模拟样例不代表生产验收。')
show_notice('_manufacturing_notice')

try:
    profiles = repository.profiles()
except (ValueError, PermissionError, OSError) as exc:
    st.error(str(exc))
    st.stop()

with st.expander('第一步 · 安装或更新领域配置', expanded=not profiles):
    st.caption('上传 manufacturing-domain/2 与对应适配器 JSON 原文。重复字段、非有限数值或单位/工厂/产品不匹配会被拒绝。')
    if can(principal, 'system.configure'):
        with st.form('manufacturing_config', enter_to_submit=False):
            domain_file = st.file_uploader('领域配置 JSON', type='json', max_upload_size=1, key='mfg_domain_file')
            adapter_file = st.file_uploader('适配器 JSON', type='json', max_upload_size=1, key='mfg_adapter_file')
            expected_version = st.number_input('预期当前配置版本（首次为0）', min_value=0, step=1, key='mfg_config_version')
            install_reason = st.text_input('安装或更新理由', max_chars=1000, key='mfg_install_reason')
            install = st.form_submit_button('安装领域配置')
        if install:
            try:
                if domain_file is None or adapter_file is None:
                    raise ValueError('请同时上传领域配置与适配器JSON。')
                profile = parse_domain_profile(domain_file.getvalue().decode('utf-8-sig'))
                adapter = parse_adapter_config(adapter_file.getvalue().decode('utf-8-sig')).to_dict()
                installed = repository.install_profile(profile, adapter,
                    expected_version=expected_version, reason=install_reason)
                rerun_notice('_manufacturing_notice', f"已安装 {installed['profile_id']} 配置版本 {installed['version']}；权限未改变。")
            except (ValueError, PermissionError, OSError) as exc:
                st.error(str(exc))
    else:
        st.info('安装配置需要系统配置权限；已有配置仍按当前工厂和产品授权可见。')

if not profiles:
    st.info('暂无当前身份可访问的制造业配置。请由获授权管理员安装配置并核对数据范围。')
    st.stop()

by_id = {row['profile_id']: row for row in profiles}
profile_id = st.selectbox('制造业领域', list(by_id),
    format_func=lambda value: by_id[value]['profile'].get('label', value), key='mfg_profile')
selected = by_id[profile_id]
profile = selected['profile']
context = (profile_id, selected['version'], selected['config_hash'], selected['data_revision'])
if st.session_state.get('_manufacturing_context') != context:
    for key in ('_manufacturing_stage', '_manufacturing_snapshot', '_manufacturing_preview',
                '_manufacturing_run_id', '_manufacturing_selection', '_manufacturing_task',
                '_manufacturing_exports', '_manufacturing_knowledge_stage', 'mfg_knowledge_products',
                'mfg_product', 'mfg_specification', 'mfg_month', 'mfg_use_llm'):
        st.session_state.pop(key, None)
    st.session_state['_manufacturing_context'] = context
st.caption(f"配置版本 {selected['version']} · 数据版本 {selected['data_revision']} · "
           f"范围 {profile['factories']['home']} / {profile['factories']['peer']} · 数据类别 {profile.get('data_classification', '未声明')}")

with st.expander('第二步 · 五类CSV完整快照', expanded=not selected['data_revision']):
    st.caption('必须包含 actual、budget、materials、labor、overhead 五表及全部配置产品/工厂；UTF-8，每表≤20MiB，合计≤60MiB。暂存不会成为有效数据。')
    if can(principal, 'data.stage'):
        with st.form('manufacturing_stage', enter_to_submit=False):
            periods_text = st.text_input('完整连续月份（逗号分隔，YYYY-MM）', key='mfg_periods', placeholder='2026-05,2026-06')
            uploaded = {family: st.file_uploader(label, type='csv', max_upload_size=20, key='mfg_csv_' + family)
                        for family, label in (('actual', '实际成本 actual.csv'), ('budget', '预算 budget.csv'),
                                             ('materials', '材料明细 materials.csv'), ('labor', '人工明细 labor.csv'),
                                             ('overhead', '制造费用 overhead.csv'))}
            stage_clicked = st.form_submit_button('校验并暂存五表快照')
        if stage_clicked:
            try:
                if any(value is None for value in uploaded.values()):
                    raise ValueError('五类CSV必须全部上传，不能用空表代替缺失表。')
                if sum(value.size for value in uploaded.values()) > MAX_BUNDLE_BYTES:
                    raise ValueError('导入合计超过60MiB。')
                periods = [part.strip() for part in periods_text.replace('，', ',').split(',') if part.strip()]
                staged = repository.stage(profile_id,
                    {family: (file.name, file.getvalue()) for family, file in uploaded.items()},
                    periods=periods, expected_revision=selected['data_revision'])
                st.session_state['_manufacturing_stage'] = staged
            except (ValueError, PermissionError, OSError) as exc:
                st.error(str(exc))
    staged = st.session_state.get('_manufacturing_stage')
    if staged:
        st.write('暂存校验通过，尚未生效。')
        st.dataframe([{'表族': family, **value} for family, value in staged['tables'].items()], hide_index=True)
        st.caption(f"暂存 {staged['stage_id']} · 基础版本 {staged['base_revision']} · SHA256 {staged['sha256']}")
        with st.form('manufacturing_confirm', enter_to_submit=False):
            confirm_reason = st.text_input('确认导入理由', max_chars=1000, key='mfg_confirm_reason')
            confirmed_review = st.checkbox('已核对暂存范围、完整月份、单位与文件摘要', key='mfg_confirm_review')
            confirm_clicked = st.form_submit_button('确认此暂存快照', disabled=not can(principal, 'data.confirm'))
        if confirm_clicked:
            try:
                if not confirmed_review:
                    raise ValueError('请先核对暂存范围与摘要并勾选确认。')
                confirmed = repository.confirm(staged['stage_id'], expected_revision=staged['base_revision'], reason=confirm_reason)
                rerun_notice('_manufacturing_notice', f"已确认制造业数据版本 {confirmed['revision']}；原制药数据未改变。")
            except (ValueError, PermissionError, OSError) as exc:
                st.error(str(exc))

with st.expander('第三步 · 制造业知识资料受控接入'):
    st.caption('领域产品、规格、工厂、图谱术语与配置指纹由服务端绑定；文档原文仍须人工核对。暂存不等于入库，更不等于完成向量发布。')
    if can(principal, 'knowledge.stage'):
        product_defs = {item['id']: item for item in profile['products']}
        categories = {'process': '生产工艺', 'formula': '产品配方', 'equipment': '设备参考',
                      'industry_benchmark': '行业基准', 'market_prices': '市场参考', 'other': '其他（仅背景）'}
        with st.form('manufacturing_knowledge_stage', enter_to_submit=False):
            knowledge_file = st.file_uploader('知识原文文件', type=['txt', 'md', 'pdf', 'docx', 'csv'],
                                              max_upload_size=20, key='mfg_knowledge_file')
            knowledge_title = st.text_input('资料标题', max_chars=200, key='mfg_knowledge_title')
            knowledge_products = st.multiselect('适用配置产品', list(product_defs),
                format_func=lambda value: product_defs[value]['name'] + ' · ' + product_defs[value]['specification'],
                key='mfg_knowledge_products')
            knowledge_category = st.selectbox('资料类别', list(categories), format_func=categories.get,
                                               key='mfg_knowledge_category')
            st.caption('生产工艺、配方、设备文档每次只选一个产品，不能用多产品授权范围代替内容适用性。')
            effective_from = st.text_input('生效日期 YYYY-MM-DD', max_chars=10, key='mfg_knowledge_from')
            effective_to = st.text_input('失效日期（可留空）', max_chars=10, key='mfg_knowledge_to')
            knowledge_reason = st.text_input('领域适用性复核理由', max_chars=1000, key='mfg_knowledge_reason')
            knowledge_clicked = st.form_submit_button('校验并暂存知识资料')
        if knowledge_clicked:
            try:
                if knowledge_file is None:
                    raise ValueError('请上传知识原文文件。')
                staged_knowledge = service.stage_knowledge(profile_id, content=knowledge_file.getvalue(),
                    filename=knowledge_file.name, title=knowledge_title, product_ids=knowledge_products,
                    category=knowledge_category, effective_from=effective_from, effective_to=effective_to or None,
                    reason=knowledge_reason)
                st.session_state['_manufacturing_knowledge_stage'] = staged_knowledge
            except (ValueError, PermissionError, OSError) as exc:
                st.error(str(exc))
    else:
        st.info('暂存知识资料需要知识管理权限；领域配置不会自动授予此权限。')
    knowledge_stage = st.session_state.get('_manufacturing_knowledge_stage')
    if knowledge_stage:
        st.caption('知识暂存标识：' + str(knowledge_stage.get('stage_id', '未生成')) + ' · 尚未发布')
        if knowledge_stage.get('errors'):
            st.error('知识校验未通过，请核对下方原始错误后重新暂存。')
        st.json(knowledge_stage)
        with st.form('manufacturing_knowledge_confirm', enter_to_submit=False):
            knowledge_commit_reason = st.text_input('确认知识入库理由', max_chars=1000, key='mfg_knowledge_commit_reason')
            knowledge_reviewed = st.checkbox('已核对原文、产品适用性、期间及领域配置指纹', key='mfg_knowledge_reviewed')
            knowledge_confirm = st.form_submit_button('确认知识版本（不自动发布索引）',
                disabled=bool(knowledge_stage.get('errors')) or not can(principal, 'knowledge.publish'))
        if knowledge_confirm:
            try:
                if not knowledge_reviewed:
                    raise ValueError('请先核对原文适用性与领域配置指纹。')
                committed = service.commit_knowledge(profile_id, stage_id=knowledge_stage['stage_id'],
                                                      reason=knowledge_commit_reason)
                st.session_state.pop('_manufacturing_knowledge_stage', None)
                rerun_notice('_manufacturing_notice', '知识版本已确认入库，但尚未发布检索索引；请到知识文档库完成受控向量发布。')
            except (ValueError, PermissionError, OSError) as exc:
                st.error(str(exc))
    st.caption('校验后须确认知识版本并在知识文档库完成受控向量发布；本页不会自动发布索引、调用生成模型或替代人工原文核对。')
    if can(principal, 'knowledge.read'):
        st.page_link(str(Path(__file__).with_name('knowledge.py')), label='打开知识文档库核对版本与发布索引', icon=':material/library_books:')

st.subheader('第四步 · 同源计算与冻结分析')
st.caption('读取和预览不调用生成模型。生成并冻结分析为显式操作；模型辅助默认关闭，启用后仍须受控混合检索与模型合同校验。')
if st.button('读取已确认快照', key='mfg_load_snapshot', disabled=not selected['data_revision']):
    try:
        snapshot = repository.current(profile_id)
        st.session_state['_manufacturing_snapshot'] = {key: value for key, value in snapshot.items() if key != 'runtime'}
    except (ValueError, PermissionError, OSError) as exc:
        st.error(str(exc))

snapshot = st.session_state.get('_manufacturing_snapshot')
if snapshot:
    products = sorted({item['name'] for item in profile['products']})
    product = st.selectbox('制造业产品', products, key='mfg_product')
    specifications = [item['specification'] for item in profile['products'] if item['name'] == product]
    if st.session_state.get('mfg_specification') not in specifications:
        st.session_state.pop('mfg_specification', None)
    specification = st.selectbox('制造业规格', specifications, key='mfg_specification')
    month = st.selectbox('制造业月份', snapshot['periods'], index=len(snapshot['periods']) - 1, key='mfg_month')
    scope = {'product': product, 'specification': specification, 'month': month}
    selection = (profile_id, product, specification, month, snapshot['sha256'], snapshot['config_hash'])
    if st.session_state.get('_manufacturing_selection') != selection:
        for key in ('_manufacturing_preview', '_manufacturing_run_id', '_manufacturing_task', 'mfg_use_llm'):
            st.session_state.pop(key, None)
        st.session_state['_manufacturing_selection'] = selection
    use_llm = st.checkbox('启用模型辅助解释（可能产生费用，需正式混合检索）', value=False, key='mfg_use_llm')
    with st.container(horizontal=True):
        preview_clicked = st.button('仅预览确定性计算', key='mfg_preview', disabled=not can(principal, 'analysis.generate'))
        analyze_clicked = st.button('生成并冻结本次分析', key='mfg_analyze', type='primary', disabled=not can(principal, 'analysis.generate'))
    try:
        if preview_clicked:
            st.session_state['_manufacturing_preview'] = service.preview(profile_id, **scope)
        if analyze_clicked:
            with st.spinner('按当前配置与确认快照计算，完成后冻结分析…'):
                result = service.analyze(profile_id, **scope, use_llm=use_llm)
            st.session_state['_manufacturing_run_id'] = result['analysis_run_id']
            st.session_state.pop('_manufacturing_task', None)
    except (ValueError, PermissionError, OSError) as exc:
        st.error(str(exc))
    preview = st.session_state.get('_manufacturing_preview')
    if preview:
        st.caption('预览模式：无模型调用、无分析写入、无任务。')
        charts = preview['projection']['charts']
        render_linked_echarts([(charts[kind]['option'], 420) for kind in ('attribution', 'benchmark')],
            context_id=chart_context_id(product, specification, month, preview['data_hash']),
            month=month, available_months=snapshot['periods'], key='manufacturing_preview_charts')
        with st.expander('核对预览原始事实、单位与来源'):
            st.json(preview['facts'])

with st.expander('按标识读取已有冻结分析'):
    run_id = st.text_input('冻结分析标识', key='mfg_existing_run', max_chars=64)
    if st.button('读取冻结分析', key='mfg_load_run'):
        try:
            result = service.get_run(run_id)
            if result['profile_id'] != profile_id:
                raise ValueError('请先切换到该分析所属领域。')
            st.session_state['_manufacturing_run_id'] = result['analysis_run_id']
        except (ValueError, PermissionError, OSError) as exc:
            st.error(str(exc))

if st.session_state.get('_manufacturing_run_id'):
    try:
        # Revalidate current actor/source permissions before redisplay or export.
        result = service.get_run(st.session_state['_manufacturing_run_id'])
    except (ValueError, PermissionError, OSError) as exc:
        st.session_state.pop('_manufacturing_run_id', None)
        st.session_state.pop('_manufacturing_exports', None)
        st.error(str(exc))
    else:
        st.caption(f"冻结分析 {result['analysis_run_id']} · {result['scope']['product']} / {result['scope']['month']} · 待人工复核")
        st.download_button('下载冻结分析JSON', json.dumps(result, ensure_ascii=False, indent=2),
            file_name=result['analysis_run_id'] + '.json', mime='application/json', key='mfg_download_run')
        export_key = (result['analysis_run_id'], result['analysis_hash'], principal.user_id)
        if (st.session_state.get('_manufacturing_exports') or {}).get('key') != export_key:
            st.session_state.pop('_manufacturing_exports', None)
        if st.button('准备Word与PDF阅读副本', key='mfg_prepare_exports'):
            try:
                with st.spinner('从同一冻结分析生成阅读副本，不重新调用模型…'):
                    docx = service.export_report(result['analysis_run_id'], 'docx')
                    pdf = service.export_report(result['analysis_run_id'], 'pdf')
                st.session_state['_manufacturing_exports'] = {'key': export_key, 'docx': docx, 'pdf': pdf}
            except (ValueError, PermissionError, OSError, RuntimeError):
                st.session_state.pop('_manufacturing_exports', None)
                st.error('阅读副本生成失败，请核对访问权限与受控字体资源；冻结分析未改变。')
        exports = st.session_state.get('_manufacturing_exports')
        if exports and exports['key'] == export_key:
            st.caption('Word/PDF是待人工复核、未签发的阅读副本；完整冻结JSON保留审计依据。')
            with st.container(horizontal=True):
                st.download_button('下载Word阅读副本', exports['docx'],
                    file_name=result['analysis_run_id'] + '.docx',
                    mime='application/vnd.openxmlformats-officedocument.wordprocessingml.document', key='mfg_download_docx')
                st.download_button('下载PDF阅读副本', exports['pdf'],
                    file_name=result['analysis_run_id'] + '.pdf', mime='application/pdf', key='mfg_download_pdf')
        for tab, kind in zip(st.tabs(['制造业归因', '制造业对标']), ('attribution', 'benchmark')):
            with tab:
                analysis = result['analyses'][kind]
                narrative = analysis['narrative']
                if not analysis['used_llm']:
                    st.info(generation_notice(analysis['generation_status']))
                render_linked_echarts([(result['charts'][kind]['option'], 420, True)],
                    context_id=result['analysis_hash'] + kind, month=result['scope']['month'],
                    available_months=[result['scope']['month']], key='manufacturing_frozen_chart_' + kind)
                render_layered_analysis(narrative['sections'], analysis['sources'],
                    overview=narrative.get('overview', ''), key='manufacturing_' + kind,
                    followup_criteria=narrative.get('followup_criteria', ''), analysis_kind=kind)
                with st.expander('模型与检索审计回执'):
                    st.json({'generation_status': analysis['generation_status'], 'model_run': analysis['model_run'],
                             'retrieval': result['retrieval_diagnostics']})
        st.subheader('第五步 · 创建待审核任务草稿')
        st.caption('仅从冻结分析建立草稿，不自动批准、分派或发送。负责人、期限及审核请在整改任务页完成。')
        with st.form('manufacturing_task', enter_to_submit=False):
            task_kind = st.selectbox('草稿分析依据', ['attribution', 'benchmark'],
                format_func=lambda value: '归因分析' if value == 'attribution' else '跨厂对标')
            task_element = st.selectbox('草稿成本要素', ['材料', '人工', '制费'])
            task_clicked = st.form_submit_button('创建待审核草稿', disabled=not can(principal, 'task.create'))
        if task_clicked:
            try:
                draft = service.task_draft(result['analysis_run_id'], kind=task_kind, element=task_element)
                st.session_state['_manufacturing_task'] = draft
                st.success('任务草稿已创建；未批准、未发送。')
            except (ValueError, PermissionError, OSError) as exc:
                st.error(str(exc))
        if st.session_state.get('_manufacturing_task'):
            st.json(st.session_state['_manufacturing_task'])

st.caption('知识接入：在知识文档库上传原文并完成受控发布；资料适用产品、工厂、期间与领域配置必须匹配。配置术语本身不是实际业务证据。')
