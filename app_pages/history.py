import json
import streamlit as st

from app_pages._shared import page_context
from enterprise.security import can
from enterprise.snapshots import SnapshotRepository

principal, app = page_context('report.read')
st.title('分析档案与更新追溯')
analysis, audit = st.tabs(['分析快照', '更新记录'])
with analysis:
    st.caption('打开历史快照回看已保存结果；不重新检索或调用模型。下载前仍会核对当前权限。')
    if not can(principal, 'report.read', factory='中药一厂'):
        st.info('当前授权范围没有一厂分析档案。')
    else:
        repo = SnapshotRepository(app.root, principal=principal)
        rows = repo.list()
        if not rows:
            st.info('暂无快照。可在产品成本分析的归因区保存本次分析。')
        else:
            months = sorted({r['month'] for r in rows}, reverse=True)
            month = st.selectbox('业务月份', months, key='snapshot_month')
            selected = [r for r in rows if r['month'] == month]
            ident = st.selectbox('分析记录', [r['id'] for r in selected], format_func=lambda i: next(
                f"{r['product']} · {r['created'][:19]} · {r['actor']}" for r in selected if r['id'] == i), key='snapshot_id')
            try:
                payload = repo.get(ident)
                result = payload['analysis']
                sections = result.get('sections') or []
                has_adopted_prose = any(section.get('prose_mode') == 'bound-numeric-prose/1'
                    and isinstance(section.get('prose'), str) and section['prose'].strip()
                    for section in sections if isinstance(section, dict))
                if has_adopted_prose:
                    # repo.get() already verified the frozen payload hash and
                    # current access. Replay only; no retrieval or model call.
                    from app_pages.citations import render_layered_analysis
                    st.caption('已保存分析的散文阅读副本；不是重新生成结果。')
                    render_layered_analysis(sections, result.get('sources', []),
                        overview=result.get('overview', ''), key='history_citations',
                        followup_criteria=result.get('followup_criteria', ''),
                        limitations=result.get('limitations', []), analysis_kind='attribution')
                else:
                    st.write(result.get('concise_text', result.get('text', '')))
                with st.expander('快照证据与数据版本'):
                    st.json({'versions': payload['versions'], 'sources': result.get('sources', []), 'code_hashes': payload['code_hashes']})
                st.download_button('下载完整分析快照', json.dumps(payload, ensure_ascii=False, indent=2),
                                   file_name=f'{ident}.json', mime='application/json', key='snapshot_download')
            except (ValueError, PermissionError, OSError) as exc:
                st.error(str(exc))
with audit:
    if can(principal, 'audit.read'):
        revisions = app.cost_history()
        st.caption('仅展示当前用户有权查看的完整修订批次。')
        st.dataframe(revisions, hide_index=True)
    else:
        st.info('更新审计由财务主管或审计角色查看。')
    st.caption('知识的业务生效时间、确认时间、索引发布及原件在知识文档库查看；报告审核记录在智能报告页查看。')
