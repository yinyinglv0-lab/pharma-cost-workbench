"""Controlled document versions, originals, index releases and scoped retrieval."""
from datetime import date
from pathlib import Path
import csv
import hashlib
import importlib
import io
import streamlit as st

from app_pages._shared import authorize, page_context, rerun_notice, show_notice
from enterprise.knowledge import MAX_BYTES, KnowledgeError, preview_file, allowed_scope, scope_permits
from enterprise.knowledge_release import ReleaseRepository, get_search_engine, result_to_api
from enterprise.security import can
from enterprise.tabular_knowledge import KNOWLEDGE_TYPES, knowledge_type
from enterprise.document_classifier import (BOARD_LABELS, CATEGORIES, board_of,
                                            llm_classify, suggest_classification)
TYPE_LABELS = {'formula': '产品配方', 'process': '生产工艺', 'equipment': '设备参考',
               'regulation': '法规原文', 'regulation_summary': '法规摘要',
               'industry_benchmark': '行业基准', 'market_prices': '市场行情',
               'cost_baseline': '成本观察基线', 'other': '其他 / 未识别类型'}
ROLE_LABELS = {'context_only': '仅背景参考',
               'document_basis': '机制文档依据（不证明本期发生）',
               'benchmark_reference': '行业对标参考（不是机制依据）',
               'market_reference': '市场行情参考（不是实际采购或机制依据）',
               'observed_baseline': '观察基线（不是因果或机制依据）'}
ROLE_BOUNDARIES = {'context_only': '仅作背景参考，不进入经营原因解释依据',
                   'document_basis': '文档仅支持机制解释，不证明本期发生相应经营事件',
                   'benchmark_reference': '仅作行业对标参考，不等于实际可节约金额，不得充作机制依据',
                   'market_reference': '仅作市场行情参考，不证明实际采购价格或本期事件，不得充作机制依据',
                   'observed_baseline': '仅作来源期间的观察基线，不证明本期因果，不得充作机制依据'}
REFERENCE_ROLES = frozenset({'benchmark_reference', 'market_reference', 'observed_baseline'})
REFERENCE_TYPES = frozenset({'industry_benchmark', 'market_prices', 'cost_baseline'})
FORMAT_LABELS = {'pdf': 'PDF', 'docx': 'Word(DOCX)', 'txt': 'TXT', 'csv': 'CSV'}


def _render_parse_coverage(metadata, format_name):
    """Display untrusted metadata as literal text, never HTML or remote content."""
    if format_name not in {'pdf', 'docx'}:
        return
    st.caption('解析范围：仅抽取文本；未执行图片OCR或流程图拓扑识别。请人工核对原件。')
    metadata = metadata if isinstance(metadata, dict) else {}
    warnings = metadata.get('warnings')
    if isinstance(warnings, list) and warnings:
        st.warning('解析覆盖存在限制，登记前须人工确认原件与提取正文。')
        st.text('\n'.join(value for value in warnings if isinstance(value, str)))
    if 'coverage' not in metadata:
        st.info('此版本未记录图片覆盖检查；未重解析或更改已确认正文，请人工核对原件中的图片和绘图。')


def _document_format(row):
    return str(row.get('format') or Path(row['filename']).suffix.lstrip('.')).lower()


def _reference_only(current, category, parsed):
    """Classifications narrow role choices; they never grant mechanism authority."""
    current = current or {}
    metadata = current.get('business_metadata') or {}
    incoming = {'category': category, 'format': parsed.get('format'),
                'parse_metadata': parsed.get('metadata', {})}
    return (metadata.get('evidence_role') in REFERENCE_ROLES
            or metadata.get('authority') in {'market_reference', 'industry_reference'}
            or current.get('category') in {'行业基准', '市场参考', '派生成本基线', '异常处理记录'}
            or category in {'行业基准', '市场参考', '派生成本基线', '异常处理记录'}
            or knowledge_type(current) in REFERENCE_TYPES
            or knowledge_type(incoming) in REFERENCE_TYPES)


def _registration_metadata(current, purpose, category, parsed, specification):
    if purpose not in ROLE_LABELS:
        raise ValueError('依据用途不在允许范围内')
    if purpose == 'document_basis' and _reference_only(current, category, parsed):
        raise ValueError('参考资料或观察基线不能登记为机制依据；请保留参考用途')
    metadata = dict((current or {}).get('business_metadata') or {})
    metadata['evidence_role'] = purpose
    # Confirmation is not a new source-authority assessment.
    metadata.setdefault('authority', 'unreviewed')
    boundary = ROLE_BOUNDARIES[purpose]
    previous = str(metadata.get('claim_boundary') or '')
    metadata['claim_boundary'] = previous if boundary in previous else '；'.join(filter(None, (previous, boundary)))
    if specification.strip():
        metadata['specification'] = specification.strip()
    else:
        metadata.pop('specification', None)
    return metadata


def _csv_records(text):
    """Parse confirmed text only, retaining blank/ragged records and string cells."""
    reader = csv.reader(io.StringIO(text, newline=''), strict=True)
    try:
        rows = list(reader)
    except csv.Error as exc:
        raise ValueError(f'CSV 在物理行 {reader.line_num} 附近无法完整解析，未展示部分结果；请查看完整原文或下载原件。') from exc
    if not rows:
        return [], []
    width = max(len(row) for row in rows)
    # Never use source headers as dict keys: duplicate/blank headers lose data.
    columns = [f'列 {index + 1}' for index in range(width)]
    records = [{'CSV记录': number, '字段数': len(row),
                **{name: row[index] if index < len(row) else None
                   for index, name in enumerate(columns)}}
               for number, row in enumerate(rows, 1)]
    ragged = [number for number, row in enumerate(rows, 1) if len(row) != len(rows[0])]
    return records, ragged


def _plain_blocks(text):
    """Group the confirmed DOCX text's paragraphs and tab-delimited table lines."""
    blocks = []
    for line in text.split('\n'):
        kind = 'table' if '\t' in line else 'text'
        if not blocks or blocks[-1][0] != kind:
            blocks.append((kind, []))
        blocks[-1][1].append(line)
    return blocks


def _pdf_available():
    if not callable(getattr(st, 'pdf', None)):
        return False
    try:
        component = importlib.import_module('streamlit_pdf')
        return callable(getattr(component, 'pdf_viewer', None))
    except (ImportError, OSError, RuntimeError):
        return False


def _render_confirmed_document(selected, blob):
    """Read-only views; no HTML conversion, remote URL, or original-file reparse."""
    text, version_id = selected['text'], selected['version_id']
    format_name = _document_format(selected)
    st.subheader('内容阅读')
    _render_parse_coverage(selected.get('parse_metadata'), format_name)
    if format_name == 'csv':
        try:
            records, ragged = _csv_records(text)
        except ValueError as exc:
            st.warning(str(exc))
        else:
            st.caption(f'共 {len(records)} 条 CSV 记录（含表头与空记录）。第 1 条保留原始表头；字段均按文本显示，不计算公式。')
            if ragged:
                st.warning(f'{len(ragged)} 条记录的字段数与首条不同；全部保留，缺失字段显示为空。请结合“字段数”和完整原文核对。')
            if records:
                pages = (len(records) + 99) // 100
                page = st.selectbox('CSV 记录页', range(1, pages + 1),
                                    key=f'knowledge_csv_page_{version_id}') if pages > 1 else 1
                start = (page - 1) * 100
                st.caption(f'显示记录 {start + 1}–{min(start + 100, len(records))} / {len(records)}')
                st.dataframe(records[start:start + 100], hide_index=True, width='stretch')
    elif format_name == 'pdf':
        if blob is not None and _pdf_available():
            try:
                st.pdf(blob, height=500, key=f'knowledge_pdf_{version_id}')
            except Exception:
                # Optional component failures must not hide confirmed text/download.
                st.info('PDF 内嵌预览暂不可用；请下载原件，或阅读下方保留页码标记的正文。')
        else:
            st.info('当前未启用 PDF 内嵌预览；可下载原件，或阅读下方保留页码标记的正文。')
        st.caption('PDF 提取正文（保留 [第N页] 标记；版式以原件为准）')
        with st.container(height=420, border=True):
            st.text(text, width='stretch')
    elif format_name == 'docx':
        st.caption('依据已确认正文按段落 / 制表符分组；不是 Word 版式还原，不执行 HTML 或外部内容。')
        lines = text.split('\n')
        pages = max(1, (len(lines) + 99) // 100)
        page = st.selectbox('DOCX 正文段落页', range(1, pages + 1),
                            key=f'knowledge_docx_page_{version_id}') if pages > 1 else 1
        start = (page - 1) * 100
        st.caption(f'显示正文行 {start + 1}–{min(start + 100, len(lines))} / {len(lines)}')
        for kind, block in _plain_blocks('\n'.join(lines[start:start + 100])):
            if kind == 'table':
                cells = [line.split('\t') for line in block]
                width = max(map(len, cells))
                st.dataframe([{f'列 {i + 1}': row[i] if i < len(row) else None
                               for i in range(width)} for row in cells], hide_index=True, width='stretch')
            else:
                st.text('\n'.join(block), width='stretch')
    else:
        with st.container(height=420, border=True):
            st.text(text, width='stretch')
    with st.expander('完整已确认原文（逐字核对）'):
        st.text_area('已确认原文', text, disabled=True, height=300, key=f'knowledge_read_{version_id}')

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
    # 分类选项=统一目录（含暂无文档的类别），并标注授权可见的已登记文档数；
    # 历史遗留类别仍保留展示，避免旧数据不可筛选。
    counts = {row['category']: sum(1 for x in docs if x['category'] == row['category']) for row in docs}
    categories = sorted(set(CATEGORIES) | {row['category'] for row in docs})
    board_options = list(BOARD_LABELS)
    if st.session_state.get('knowledge_browse_board') not in board_options:
        st.session_state.pop('knowledge_browse_board', None)
    browse_board_value = st.session_state.get('knowledge_browse_board')
    category_options = ([category for category in categories if board_of(category) == browse_board_value]
                        if browse_board_value else categories)
    for key, options in (('knowledge_browse_category', category_options), ('knowledge_browse_format', FORMAT_LABELS)):
        if st.session_state.get(key) is not None and st.session_state[key] not in options:
            st.session_state.pop(key, None)
    def _board_label(value):
        if value is None:
            return '全部板块'
        total = sum(counts.get(category, 0) for category in CATEGORIES if board_of(category) == value)
        return f'{value}（{total} 份）' if total else f'{value}（暂无文档）'
    def _category_label(value):
        if value is None:
            return '全部授权类别'
        total = counts.get(value, 0)
        return f'{board_of(value)} · {value}（{total} 份）' if total else f'{board_of(value)} · {value}（暂无文档）'
    board_col, category_col, format_col = st.columns(3)
    with board_col:
        browse_board = st.selectbox('按知识板块筛选', [None] + board_options,
                                    format_func=_board_label,
                                    key='knowledge_browse_board')
    with category_col:
        browse_category = st.selectbox('按资料类别筛选', [None] + category_options,
                                       format_func=_category_label,
                                       key='knowledge_browse_category')
    with format_col:
        browse_format = st.selectbox('按文件格式筛选', [None] + list(FORMAT_LABELS),
                                     format_func=lambda value: FORMAT_LABELS[value] if value else '全部授权格式',
                                     key='knowledge_browse_format')
    st.caption('支持 PDF、Word(DOCX)、TXT、CSV；旧版 .doc 不支持，请先转换为 .docx。')
    if not docs:
        label = FORMAT_LABELS.get(browse_format, '所选格式')
        st.info(f'当前授权范围暂无确认版本，没有{label}文档。知识管理员可上传并登记资料。')
    else:
        visible_docs = [row for row in docs if (browse_board is None or board_of(row['category']) == browse_board)
                        and (browse_category is None or row['category'] == browse_category)
                        and (browse_format is None or _document_format(row) == browse_format)]
        st.caption(f'当前授权文档 {len(docs)} 份 · 筛选结果 {len(visible_docs)} 份。筛选按最新可见版本分类，历史版本保留自身类别与格式。')
        if not visible_docs:
            label = FORMAT_LABELS.get(browse_format, '所选格式')
            st.info(f'当前授权范围与类别筛选下没有文档（{label}）；格式仍受支持，可调整筛选或由知识管理员登记资料。')
        else:
            st.dataframe([{'板块': board_of(x['category']), '文档': x['title'], '资料类别': x['category'],
                           '格式': _document_format(x).upper(),
                           '知识类型': TYPE_LABELS[knowledge_type(x)], '版本': x['version'],
                           '依据用途': ROLE_LABELS.get(x.get('business_metadata', {}).get('evidence_role', 'context_only'), '未识别用途（仅背景）'),
                           '生效日期': x['effective_from'],
                           '当前发布': '已纳入' if x['version_id'] in active_versions else '未纳入或清单不可见'}
                          for x in visible_docs], hide_index=True, width='stretch')
            doc_options = [x['doc_id'] for x in visible_docs]
            if st.session_state.get('knowledge_browse_doc') not in doc_options:
                st.session_state['knowledge_browse_doc'] = doc_options[0]
            doc_id = st.selectbox('查看文档', doc_options, format_func=lambda ident: next(
                row['title'] for row in visible_docs if row['doc_id'] == ident), key='knowledge_browse_doc', index=None)
            try:
                versions = repo.history(doc_id)
                version_options = [x['version_id'] for x in versions]
                if not versions:
                    st.info('当前文档暂无可访问版本，请刷新列表。')
                else:
                    if st.session_state.get('knowledge_browse_version') not in version_options:
                        st.session_state['knowledge_browse_version'] = version_options[0]
                    version_id = st.selectbox('版本', version_options, format_func=lambda ident: next(
                        f"v{x['version']} · {x['effective_from']} 生效 · {_document_format(x).upper()}" for x in versions if x['version_id'] == ident),
                        key='knowledge_browse_version', index=None)
                    selected = repo.get(version_id=version_id)
                    if selected is None:
                        raise ValueError('所选版本不可用，请刷新列表。')
                    blob = None
                    try:
                        blob = repo.read_blob(selected['sha256'], version_id=version_id)
                    except PermissionError:
                        raise
                    except (ValueError, OSError) as exc:
                        st.warning(str(exc))
                    st.text(f"来源：{selected['filename']} · v{selected['version']} · 类别：{selected['category']} · 格式：{_document_format(selected).upper()}")
                    st.text(f"版本 ID：{version_id}\n确认人：{selected['confirmed_by']} · 确认时间：{selected['confirmed_at']}\n原件 SHA256：{selected['sha256']}\n正文 SHA256：{selected['text_sha256']}")
                    metadata = selected.get('business_metadata') or {}
                    st.text('依据用途：' + ROLE_LABELS.get(metadata.get('evidence_role', 'context_only'), '未识别用途（仅背景）')
                            + ' · 来源权威标记：' + str(metadata.get('authority', 'unreviewed')))
                    st.caption('参考资料和观察基线不能充作机制依据；可阅读不等于可证明本期经营事实。')
                    if metadata.get('claim_boundary'):
                        st.text('原有使用边界：' + str(metadata['claim_boundary']))
                    with st.expander('适用范围与登记元数据'):
                        st.json({key: selected.get(key) for key in ('visibility', 'scope_factories', 'scope_products',
                                 'effective_from', 'effective_to', 'parser', 'parse_metadata', 'business_metadata')})
                    if blob is not None:
                        mime = {'csv': 'text/csv', 'pdf': 'application/pdf', 'txt': 'text/plain',
                                'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}
                        st.download_button('下载原件', blob, file_name=selected['filename'],
                                           mime=mime.get(_document_format(selected), 'application/octet-stream'),
                                           key=f'knowledge_blob_{version_id}')
                    _render_confirmed_document(selected, blob)
            except (ValueError, PermissionError, OSError) as exc:
                st.error(str(exc))

if local_admin:
    with st.expander('赛方资料核准导入'):
        st.caption('核准清单包含法规原文与摘要、三产品配方、工艺、设备、行情和行业基准共九份原件。已知条号、配方换算与设备数量冲突保留标注。2025-01-01 为演示回溯基线，不代表原文件实际法律或业务生效日。')
        acknowledged = st.checkbox('已核对核准清单、授权范围和演示生效日期说明', key='knowledge_bootstrap_ack')
        st.caption('同时构建本地真实向量索引；缺失模型或构建失败不切换正式发布。首次完整构建可能需要数分钟。')
        if st.button('按已核准清单登记赛方资料并发布', disabled=not acknowledged, key='knowledge_bootstrap'):
            try:
                authorize(principal, 'knowledge.stage')
                authorize(principal, 'knowledge.publish')
                from enterprise.bootstrap import bootstrap_knowledge
                with st.spinner('登记核准原件并发布索引…'):
                    result = bootstrap_knowledge(principal=principal, root=app.root, publish=True, dense=True, build_timeout=3600)
                if result['release']['status'] != 'published':
                    raise ValueError('原件已登记，但向量构建未完成；未切换正式发布，请检查模型和构建记录。')
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
        try:
            parsed = preview_file(content, filename)
        except (KnowledgeError, ValueError, OSError) as exc:
            # 纯扫描件等无可提取文本的 PDF：保留错误供视觉增强流程处理，不再中断页面
            parsed = {'errors': [str(exc)], 'format': Path(filename).suffix.lstrip('.').lower(),
                      'text': '', 'metadata': {}, 'sha256': hashlib.sha256(content).hexdigest(),
                      'text_sha256': '', 'parser': '', 'filename': filename}
        with st.expander('先查看原件内容', expanded=True):
            for error in parsed['errors']:
                st.error(error)
            _render_parse_coverage(parsed.get('metadata'), parsed.get('format'))
            if not parsed['errors']:
                st.caption(f"提取方式：{parsed['parser']} · SHA256：{parsed['sha256']}")
                st.text_area('解析原文', parsed['text'], height=300, disabled=True, key='knowledge_upload_preview')
        # 视觉增强解析（仅 PDF，且存在无文本页/图片页或文本层整体缺失时显示）
        vision_merged_key = 'knowledge_vision_merge_' + parsed['sha256']
        vision_merged = st.session_state.get(vision_merged_key)
        coverage = (parsed.get('metadata') or {}).get('coverage') or {}
        if parsed['format'] == 'pdf' and (coverage.get('pages_without_text') or coverage.get('image_pages') or parsed['errors']):
            with st.expander('视觉增强解析（图片页 / 无文本页）', expanded=bool(parsed['errors'])):
                try:
                    from enterprise.multimodal import TASKS, provider_config
                    provider, model, api_key, _ = provider_config()
                except ValueError as exc:
                    provider, model = '未知', '未知'
                    api_key = ''
                    st.error(str(exc))
                st.caption(f'当前视觉供应商：{provider} · {model}' + ('（密钥已配置）' if api_key else '（⚠️ 密钥未配置，请先在模型配置页保存）'))
                from enterprise.vision_enhancement import candidate_pages, enhance_pdf
                pages = candidate_pages(parsed)
                st.caption(f'候选页：{pages or "无（文本层整体缺失，按预览失败处理）"}；逐页调用视觉模型、按页面计费，失败不重试。')
                task = st.selectbox('理解任务', list(TASKS), key='knowledge_vision_task_' + parsed['sha256'][:12])
                if st.button('开始视觉增强', key='knowledge_vision_run_' + parsed['sha256'][:12], disabled=not api_key):
                    with st.spinner('视觉模型逐页理解中…'):
                        result = enhance_pdf(content, parsed, task=task)
                    st.session_state['knowledge_vision_result_' + parsed['sha256']] = result
                vision_result = st.session_state.get('knowledge_vision_result_' + parsed['sha256'])
                if vision_result is not None:
                    if not vision_result['ok']:
                        st.error(vision_result['reason'])
                        for section in vision_result.get('sections', []):
                            st.text(f"[第{section['page']}页]\n{section['text']}")
                    else:
                        st.success(f"已增强 {len(vision_result['sections'])} 页"
                                   + (f"（{vision_result['meta'].get('model')}）" if vision_result.get('meta', {}).get('model') else ''))
                        st.warning('视觉输出未经人工确认，不得直接入库或写入正式报告。')
                        for section in vision_result['sections']:
                            with st.expander(f"第 {section['page']} 页视觉结果", expanded=False):
                                st.text(section['text'])
                        if st.button('将增强稿并入登记文本（原件仍为原PDF）', key='knowledge_vision_merge_btn_' + parsed['sha256'][:12]):
                            st.session_state[vision_merged_key] = vision_result['merged_text']
                            st.success('已并入。请核对下方差异预览，确认无误后再登记。')
                            st.rerun()
        stageable = not parsed['errors'] or bool(vision_merged)
        if stageable:
            # 自动归类建议（规则层；模型层由按钮显式触发）。仅预填表单，须人工确认。
            suggestion_key = 'knowledge_suggest_' + parsed['sha256']
            if (not isinstance(st.session_state.get(suggestion_key), dict)
                    or st.session_state[suggestion_key].get('category') not in CATEGORIES):
                st.session_state[suggestion_key] = suggest_classification(filename, parsed)
            suggestion = st.session_state[suggestion_key]
            with st.container(border=True):
                st.markdown(f"**自动归类建议**（仅预填，须人工确认）："
                            f"板块「{suggestion['board']}」 · 类别「{suggestion['category']}」 · "
                            f"依据用途「{ROLE_LABELS[suggestion['evidence_role']]}」")
                st.caption(f"依据：{suggestion['reason']}（置信度：{suggestion['confidence']} · 来源：{suggestion['source']}）")
                if st.button('让模型再判断一次（1 次微小调用）', key='knowledge_suggest_llm_' + parsed['sha256'][:12]):
                    with st.spinner('模型判断中…'):
                        model_suggestion = llm_classify(filename, str(parsed.get('text') or '')[:1500])
                    if model_suggestion:
                        st.session_state[suggestion_key] = model_suggestion
                        st.rerun()
                    else:
                        st.info('模型未给出有效建议（未配置可用密钥或调用失败），保留规则建议。')
            target = st.selectbox('登记方式 / 更新文档', [None]+[x['doc_id'] for x in docs],
                                  format_func=lambda ident: '登记新文档' if ident is None else '更新：'+next(
                                      x['title'] for x in docs if x['doc_id'] == ident), key='knowledge_target')
            current = repo.get(target) if target else None
            suffix = (target or 'new') + parsed['sha256'][:12]
            with st.form('knowledge_stage_form_'+suffix):
                title = st.text_input('文档标题', value=current['title'] if current else Path(filename).stem, disabled=current is not None)
                categories = list(CATEGORIES)
                previous_category = current['category'] if current else '其他'
                if previous_category not in categories:
                    categories.append(previous_category)
                default_category = previous_category if current else suggestion['category']
                category = st.selectbox('资料类别', categories, index=categories.index(default_category),
                                        format_func=lambda value: f'{board_of(value)} · {value}')
                previous_metadata = current.get('business_metadata', {}) if current else {}
                previous_role = previous_metadata.get('evidence_role', 'context_only')
                if current:
                    default_role = previous_role
                elif vision_merged:
                    # 视觉增强稿默认仅背景参考；人工核对后可显式选择机制依据（会留痕）
                    default_role = 'context_only'
                else:
                    default_role = suggestion['evidence_role']
                role_options = list(ROLE_LABELS)
                purpose = st.selectbox('依据用途', role_options,
                                       index=role_options.index(default_role) if default_role in role_options else 0,
                                       format_func=ROLE_LABELS.get)
                st.caption('用途需显式核准。行业、行情与观察基线不得登记为机制依据；更新保留原有来源权威标记与使用边界，不自动提升权威。')
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
                    metadata = _registration_metadata(current, purpose, category, parsed, specification)
                    if vision_merged and purpose == 'document_basis':
                        # 视觉增强内容须留痕：管理员显式选择机制依据即视为已人工核对
                        metadata['vision_reviewed'] = True
                        previous = str(metadata.get('claim_boundary') or '')
                        boundary = '视觉增强解析内容已经人工核对'
                        metadata['claim_boundary'] = previous if boundary in previous else '；'.join(filter(None, (previous, boundary)))
                    result = repo.stage(content, filename, title, split(products), start.isoformat(), category, principal.user_id,
                                        doc_id=target, effective_to=end.isoformat() if has_end else None,
                                        scope_factories=split(factories), visibility=visibility, metadata=metadata,
                                        extracted_text_override=vision_merged or None)
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

if can(principal, 'knowledge.publish'):
    own_stage_id = (st.session_state.get('knowledge_stage') or {}).get('stage_id')
    pending_rows = [row for row in repo.pending_stages() if row['stage_id'] != own_stage_id]
    if pending_rows:
        with st.expander(f'待确认登记（{len(pending_rows)} 项，含系统自动生成的异常案例候选）', expanded=True):
            for pending in pending_rows:
                st.markdown(f"**{pending['title']}** · 板块「{board_of(pending['category'] or '其他')}」"
                            f" · 类别「{pending['category'] or '其他'}」 · 暂存于 {pending['created_at']}"
                            f" · 范围：{('、'.join(pending['scope_products'])) or '公开'} / "
                            f"{('、'.join(pending['scope_factories'])) or '公开'}")
                with st.expander('查看暂存正文'):
                    st.text(pending['text'][:4000])
                with st.form('knowledge_confirm_pending_' + pending['stage_id']):
                    reason = st.text_input('登记 / 更新原因', key='knowledge_pending_reason_' + pending['stage_id'])
                    acknowledged = st.checkbox('已核对正文、适用范围、生效日期与差异',
                                               key='knowledge_pending_ack_' + pending['stage_id'])
                    confirmed = st.form_submit_button('确认登记此版本', type='primary')
                if confirmed:
                    try:
                        authorize(principal, 'knowledge.publish')
                        if not acknowledged:
                            raise ValueError('请先核对并勾选确认')
                        result = repo.commit(pending['stage_id'], principal.user_id, reason, principal=principal)
                        rerun_notice('knowledge_notice', f"已确认登记「{result['title']}」v{result['version']}，请选择该版本发布索引。")
                    except (ValueError, PermissionError, OSError) as exc:
                        st.error(str(exc))

st.subheader('索引发布与状态')
if active:
    st.caption(f"当前发布：{active['release_id']} · 发布时间：{active.get('published_at')} · 状态：{active['status']}")
    manifest = active['manifest']
    graph = manifest.get('domain_graph', {})
    counts = graph.get('counts', {})
    if graph.get('status') == 'ready':
        st.caption(f"领域图谱：{counts.get('entities', 0)} 个实体 · {counts.get('relations', 0)} 条关联记录；每条关联保留原文与适用范围。")
    from enterprise.knowledge_runtime import readiness
    ready_state = readiness(principal, repository=repo)
    if not ready_state['ready']:
        st.info('混合检索正在预热，请稍后重试；正式分析不会把关键词回退标成混合检索成功。' if ready_state.get('state') == 'loading' else '向量＋BM25混合检索未就绪，正式模型分析暂停，请检查向量发布与本地模型状态。')
    with st.expander('当前发布清单与检索能力'):
        st.json({'readiness': ready_state, 'manifest': manifest})
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
    st.caption('正式发布必须同时构建本地语义向量、BM25与领域图谱。缺模型或向量失败不会切换发布；完整重建可能需要数分钟。')
    if st.button('构建并发布索引', type='primary', disabled=not selected_ids, key='knowledge_publish'):
        try:
            authorize(principal, 'knowledge.publish')
            for ident in selected_ids:
                version = repo.get(version_id=ident)
                if version['visibility'] == 'public' and not public_admin:
                    raise PermissionError('组织公开发布需全范围知识管理员')
            with st.spinner('构建、校验并发布索引…'):
                result = releases.publish(selected_ids, principal=principal,
                                          embedding_model_path=None, require_embeddings=True, build_timeout=3600)
            if result['status'] == 'published':
                rerun_notice('knowledge_notice', f"发布 {result['release_id']} 已完成。")
            else:
                st.error('索引构建或校验未完成，本次未切换正式发布。请检查发布记录后重试。')
        except (ValueError, PermissionError, OSError) as exc:
            st.error(str(exc))

with st.expander('按业务日期检索已发布知识'):
    product = st.text_input('查询产品（可留空）', key='knowledge_query_product')
    factory = st.text_input('查询工厂（可留空）', key='knowledge_query_factory')
    as_of = st.date_input('业务有效日期', value=date.today(), key='knowledge_as_of')
    search_types = st.multiselect('知识类型（可选，留空查询全部授权类型）',
                                  [value for value in TYPE_LABELS if value in KNOWLEDGE_TYPES],
                                  format_func=TYPE_LABELS.get, key='knowledge_query_types')
    st.caption('知识类型使用已发布索引的分类；CSV 按已验证表头识别，不由文件名或展示类别提升权限。')
    query = st.text_input('检索内容', key='knowledge_query')
    if st.button('查询已发布知识', key='knowledge_search'):
        try:
            results, stats = get_search_engine(repository=repo).search(query, principal=principal, product=product.strip() or None,
                factory=factory.strip() or None, as_of=as_of.isoformat(), top_k=10, knowledge_types=search_types or None,
                require_hybrid=True, vector_timeout=15.0)
            st.caption(f"发布：{stats.get('release_id') or '无'} · 模式：{stats['retrieval_mode']}")
            if not results:
                st.info('无可用依据：'+stats['reason'])
            for item in results:
                row = result_to_api(item)
                st.markdown(f"**{row['source']}**")
                st.caption(f"版本 {row['version_id']} · 生效 {row['effective_from']}")
                st.write(row['text'])
                relation_labels = {'product_contains_material':'配方包含药材', 'material_undergoes_process':'药材经过工序',
                                   'process_uses_equipment':'工序使用设备', 'process_has_controlled_metric':'工序受控指标',
                                   'process_precedes_process':'原文箭头所示工序先后'}
                for edge in row.get('domain_evidence', []):
                    support = edge.get('support', {})
                    st.caption('图谱关系依据：' + relation_labels.get(edge['relation_type'], edge['relation_type']))
                    st.write(support.get('quote', ''))
                if row.get('domain_evidence'):
                    st.caption('上述关系由文档原文支持，不证明本期实际发生异常或构成因果。')
                if row.get('rerank_details'):
                    with st.expander('查看本片段的检索评分依据'):
                        st.json({'scores':row.get('retrieval_scores', {}), 'rerank':row['rerank_details']})
        except (ValueError, PermissionError, OSError) as exc:
            st.error(str(exc))
if can(principal, 'knowledge.audit'):
    with st.expander('版本操作记录'):
        st.dataframe(repo.events(), hide_index=True)
