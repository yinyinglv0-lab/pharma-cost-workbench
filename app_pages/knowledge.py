"""Controlled document versions, originals, index releases and scoped retrieval."""
from datetime import date
from pathlib import Path
import json
import streamlit as st

from app_pages._shared import authorize, page_context, rerun_notice, show_notice
from enterprise.knowledge import MAX_BYTES, preview_file, allowed_scope, scope_permits
from enterprise.knowledge_release import ReleaseRepository, get_search_engine, result_to_api
from enterprise.security import can

principal, app = page_context('knowledge.read')
repo = app.knowledge()
releases = ReleaseRepository(app.root)
st.title('知识文档与版本')
st.caption('受控原件 → 正文与范围差异 → 确认版本 → 发布索引 → 有效知识检索')
show_notice('knowledge_notice')
public_admin = ('knowledge_admin' in principal.roles and '*' in principal.factories and '*' in principal.products)
local_admin = principal.auth_method == 'local_os_demo' and public_admin
try:
    docs = repo.list_documents()
    history = repo.history()
    release_history = releases.history(principal=principal)
except (ValueError, PermissionError, OSError) as exc:
    st.error(str(exc))
    st.stop()
try:
    active = releases.get_release(principal=principal)
except PermissionError:
    # Limited readers may use authorized search without seeing the whole manifest.
    active = None
except (ValueError, OSError) as exc:
    active = None
    st.warning(str(exc))
active_versions = set(active['manifest']['input_version_ids']) if active else set()

with st.expander('已登记文件与历史版本', expanded=True):
    if not docs:
        st.info('当前范围暂无确认版本。知识管理员可上传并登记资料。')
    else:
        st.dataframe([{'文档': x['title'], '版本': x['version'], '来源文件': x['filename'],
                       '可见性': x['visibility'], '适用工厂': '、'.join(x['scope_factories']),
                       '适用产品': '、'.join(x['scope_products']), '生效日期': x['effective_from'],
                       '失效日期': x['effective_to'], '确认时间': x['confirmed_at'],
                       '当前发布': '已纳入' if x['version_id'] in active_versions else '未纳入或清单不可见'}
                      for x in docs], hide_index=True)
        doc_id = st.selectbox('查看文档', [x['doc_id'] for x in docs], format_func=lambda ident: next(
            row['title'] for row in docs if row['doc_id'] == ident), key='knowledge_browse_doc')
        versions = repo.history(doc_id)
        version_id = st.selectbox('版本', [x['version_id'] for x in versions], format_func=lambda ident: next(
            f"v{x['version']} · {x['effective_from']} 生效" for x in versions if x['version_id'] == ident), key='knowledge_browse_version')
        selected = repo.get(version_id=version_id)
        st.caption(f"来源：{selected['filename']} · 确认人：{selected['confirmed_by']} · 原件 SHA256：{selected['sha256']}")
        st.text_area('已确认原文', selected['text'], disabled=True, height=300, key=f'knowledge_read_{version_id}')
        try:
            st.download_button('下载原件', repo.read_blob(selected['sha256'], version_id=version_id),
                               file_name=selected['filename'], key=f'knowledge_blob_{version_id}')
        except (ValueError, PermissionError, OSError) as exc:
            st.error(str(exc))

if local_admin:
    with st.expander('赛方资料核准导入'):
        st.caption('核准清单包含法规原文与摘要、三产品配方、工艺、设备、行情和行业基准共九份原件。已知条号、配方换算与设备数量冲突保留标注。2025-01-01 为演示回溯基线，不代表原文件实际法律或业务生效日。')
        acknowledged = st.checkbox('已核对核准清单、授权范围和演示生效日期说明', key='knowledge_bootstrap_ack')
        dense = st.checkbox('同时构建本地向量索引（首次加载可能较慢）', value=False, key='knowledge_bootstrap_dense')
        if st.button('按已核准清单登记赛方资料并发布', disabled=not acknowledged, key='knowledge_bootstrap'):
            try:
                authorize(principal, 'knowledge.stage')
                authorize(principal, 'knowledge.publish')
                from enterprise.bootstrap import bootstrap_knowledge
                with st.spinner('登记核准原件并发布索引…'):
                    result = bootstrap_knowledge(principal=principal, root=app.root, publish=True, dense=dense)
                rerun_notice('knowledge_notice', f"已登记 {len(result['versions'])} 份核准资料，发布 {result['release']['release_id']}。")
            except (ValueError, PermissionError, OSError, RuntimeError) as exc:
                st.error(str(exc))

if can(principal, 'knowledge.stage'):
    st.subheader('上传或登记原件')
    methods = ['上传文件'] + (['选择项目已有原件'] if local_admin else [])
    method = st.segmented_control('文件来源', methods, default='上传文件', key='knowledge_source')
    content = filename = None
    if method == '上传文件':
        uploaded = st.file_uploader('选择 PDF / DOCX / TXT / CSV（最大 20MB）', type=['pdf', 'docx', 'txt', 'csv'], key='knowledge_upload')
        if uploaded is not None:
            if uploaded.size > MAX_BYTES:
                st.error('文件超过 20MB，请缩小后上传。')
            else:
                filename, content = uploaded.name, uploaded.getvalue()
    elif local_admin:
        from paths import DATA_DIR
        source_root = Path(DATA_DIR).resolve()
        files = sorted(p.name for p in source_root.iterdir() if p.is_file() and not p.is_symlink()
                       and p.suffix.lower() in {'.pdf', '.docx', '.txt', '.csv'})
        st.caption('本机演示管理员可登记本地赛题原件；企业模式仅使用受控上传与授权原件。')
        if files:
            filename = st.selectbox('项目原件', files, key='knowledge_original')
            path = (source_root / filename).resolve()
            if path.parent != source_root or path.is_symlink() or path.name not in files:
                st.error('原件路径不在允许范围。')
            elif path.stat().st_size > MAX_BYTES:
                st.error('文件超过 20MB。')
            else:
                content = path.read_bytes()
    if content is not None:
        parsed = preview_file(content, filename)
        with st.expander('先查看原件内容', expanded=True):
            for error in parsed['errors']:
                st.error(error)
            if not parsed['errors']:
                st.caption(f"提取方式：{parsed['parser']} · SHA256：{parsed['sha256']}")
                st.text_area('解析原文', parsed['text'], height=300, disabled=True, key='knowledge_upload_preview')
        if not parsed['errors']:
            target = st.selectbox('登记方式 / 更新文档', [None]+[x['doc_id'] for x in docs],
                                  format_func=lambda ident: '登记新文档' if ident is None else '更新：'+next(
                                      x['title'] for x in docs if x['doc_id'] == ident), key='knowledge_target')
            current = repo.get(target) if target else None
            suffix = (target or 'new') + parsed['sha256'][:12]
            with st.form('knowledge_stage_form_'+suffix):
                title = st.text_input('文档标题', value=current['title'] if current else Path(filename).stem, disabled=current is not None)
                categories = ['通用制度', '产品工艺', '配方资料', '市场参考', '其他']
                previous_category = current['category'] if current else '其他'
                if previous_category not in categories:
                    categories.append(previous_category)
                category = st.selectbox('资料类别', categories, index=categories.index(previous_category))
                previous_metadata = current.get('business_metadata', {}) if current else {}
                purpose = st.selectbox('依据用途', ['context_only', 'document_basis'],
                                       index=1 if previous_metadata.get('evidence_role') == 'document_basis' else 0,
                                       format_func=lambda value: '仅背景参考' if value == 'context_only' else '可用于机制解释（仍不能证明本期真实发生）')
                visibility_options = ['scoped'] + (['public'] if public_admin else [])
                current_visibility = current.get('visibility', 'scoped') if current else 'scoped'
                visibility = st.selectbox('可见范围', visibility_options,
                                          index=visibility_options.index(current_visibility) if current_visibility in visibility_options else 0,
                                          format_func=lambda value: '组织公开资料（明确核准）' if value == 'public' else '限定工厂与产品')
                factories = st.text_input('适用工厂（逗号分隔；公开资料留空）',
                                         value='，'.join(current['scope_factories']) if current else '')
                products = st.text_input('适用产品（逗号分隔；公开资料留空）',
                                        value='，'.join(current['scope_products']) if current else '')
                specification = st.text_input('适用规格（可选；需确认后才作为报告文档依据）',
                                              value=str(current.get('business_metadata', {}).get('specification', '')) if current else '')
                start = st.date_input('业务生效日期', value=date.fromisoformat(current['effective_from']) if current else date.today())
                has_end = st.checkbox('设置业务失效日期', value=bool(current and current['effective_to']))
                end = st.date_input('业务失效日期（仅勾选时应用）', value=date.fromisoformat(current['effective_to']) if current and current['effective_to'] else date.today())
                staged = st.form_submit_button('生成差异预览', type='primary')
            if staged:
                try:
                    authorize(principal, 'knowledge.stage')
                    if visibility == 'public' and not public_admin:
                        raise PermissionError('组织公开资料仅允许具备全部范围的知识管理员发布')
                    split = lambda value: [x.strip() for x in value.replace('，', ',').split(',') if x.strip()]
                    metadata = dict(current.get('business_metadata', {})) if current else {}
                    metadata.update(evidence_role=purpose, authority='enterprise_reviewed',
                                    claim_boundary='文档仅支持机制解释，不证明本期发生相应经营事件' if purpose == 'document_basis' else '仅作背景参考，不进入经营原因解释依据')
                    if specification.strip():
                        metadata['specification'] = specification.strip()
                    else:
                        metadata.pop('specification', None)
                    result = repo.stage(content, filename, title, split(products), start.isoformat(), category, principal.user_id,
                                        doc_id=target, effective_to=end.isoformat() if has_end else None,
                                        scope_factories=split(factories), visibility=visibility, metadata=metadata)
                    st.session_state['knowledge_stage'] = result
                except (ValueError, PermissionError, OSError) as exc:
                    st.error(str(exc))

stage = st.session_state.get('knowledge_stage')
if stage and can(principal, 'knowledge.stage'):
    try:
        if stage.get('doc_id') and stage.get('base_version'):
            repo.get(stage['doc_id'])
        if not stage['errors'] and stage.get('stage_id') and not scope_permits(stage, allowed_scope(principal)):
            raise PermissionError('预览范围已不在当前授权范围')
    except PermissionError as exc:
        st.session_state.pop('knowledge_stage', None)
        st.error(str(exc))
        stage = None
    if stage:
        st.subheader('待确认差异')
        for error in stage['errors']:
            st.error(error)
        if not stage['errors']:
            st.info(stage['change']['summary'])
            if stage.get('stage_id'):
                st.caption(f"生效：{stage['effective_from']} · 工厂：{'、'.join(stage['scope_factories'])} · 产品：{'、'.join(stage['scope_products'])} · 可见性：{stage['visibility']}")
                st.code(stage['diff'] or '正文未变化；本次调整业务元数据。', language='diff')
                st.json(stage['change'].get('metadata_changes', {}), expanded=False)
                with st.form('knowledge_confirm_'+stage['stage_id']):
                    reason = st.text_input('登记 / 更新原因')
                    acknowledged = st.checkbox('已核对正文、适用范围、生效日期与差异')
                    confirmed = st.form_submit_button('确认登记此版本', type='primary', disabled=not can(principal, 'knowledge.publish'))
                if confirmed:
                    try:
                        authorize(principal, 'knowledge.publish')
                        if not acknowledged:
                            raise ValueError('请先核对并勾选确认')
                        if stage['visibility'] == 'public' and not public_admin:
                            raise PermissionError('组织公开资料需全范围知识管理员确认')
                        result = repo.commit(stage['stage_id'], principal.user_id, reason)
                        st.session_state.pop('knowledge_stage', None)
                        rerun_notice('knowledge_notice', f"已确认版本 v{result['version']}，请选择该版本发布索引。")
                    except (ValueError, PermissionError, OSError) as exc:
                        st.error(str(exc))
        if st.button('清除本次预览', key='knowledge_clear_stage'):
            st.session_state.pop('knowledge_stage', None)
            st.rerun()

st.subheader('索引发布与状态')
if active:
    st.caption(f"当前发布：{active['release_id']} · 发布时间：{active.get('published_at')} · 状态：{active['status']}")
    with st.expander('当前发布清单与检索能力'):
        st.json(active['manifest'])
else:
    st.info('没有当前用户可查看的完整发布清单；受限用户仍可查询自己有权访问的已发布资料。')
if release_history:
    st.dataframe([{'发布ID': x['release_id'], '状态': x['status'], '构建时间': x['created_at'],
                   '发布时间': x.get('published_at')} for x in release_history], hide_index=True)
if can(principal, 'knowledge.publish'):
    choices = [x for x in history if x['visibility'] == 'scoped' or (x['visibility'] == 'public' and public_admin)]
    selected_ids = st.multiselect('本次发布包含的确认版本（保留历史版本以支持历史日期查询）',
                                 [x['version_id'] for x in choices], default=[x['version_id'] for x in choices],
                                 format_func=lambda ident: next(f"{x['title']} · v{x['version']}" for x in choices if x['version_id'] == ident), key='knowledge_release_versions')
    embeddings = st.checkbox('构建本地向量索引（需已安装并配置本地模型）', key='knowledge_release_embeddings')
    st.caption('未勾选时发布 BM25 与图谱索引，并标记向量检索降级；发布失败保留上一发布。')
    if st.button('构建并发布索引', type='primary', disabled=not selected_ids, key='knowledge_publish'):
        try:
            authorize(principal, 'knowledge.publish')
            for ident in selected_ids:
                version = repo.get(version_id=ident)
                if version['visibility'] == 'public' and not public_admin:
                    raise PermissionError('组织公开发布需全范围知识管理员')
            with st.spinner('构建、校验并发布索引…'):
                result = releases.publish(selected_ids, principal=principal,
                                          embedding_model_path=None if embeddings else '', require_embeddings=embeddings)
            rerun_notice('knowledge_notice', f"发布 {result['release_id']} 已完成。")
        except (ValueError, PermissionError, OSError) as exc:
            st.error(str(exc))

with st.expander('按业务日期检索已发布知识'):
    product = st.text_input('查询产品（可留空）', key='knowledge_query_product')
    factory = st.text_input('查询工厂（可留空）', key='knowledge_query_factory')
    as_of = st.date_input('业务有效日期', value=date.today(), key='knowledge_as_of')
    query = st.text_input('检索内容', key='knowledge_query')
    if st.button('查询已发布知识', key='knowledge_search'):
        try:
            results, stats = get_search_engine(repository=repo).search(query, principal=principal, product=product.strip() or None,
                factory=factory.strip() or None, as_of=as_of.isoformat(), top_k=10)
            st.caption(f"发布：{stats.get('release_id') or '无'} · 模式：{stats['retrieval_mode']}")
            if not results:
                st.info('无可用依据：'+stats['reason'])
            for item in results:
                row = result_to_api(item)
                st.markdown(f"**{row['source']}**")
                st.caption(f"版本 {row['version_id']} · 生效 {row['effective_from']}")
                st.write(row['text'])
        except (ValueError, PermissionError, OSError) as exc:
            st.error(str(exc))
if can(principal, 'knowledge.audit'):
    with st.expander('版本操作记录'):
        st.dataframe(repo.events(), hide_index=True)
