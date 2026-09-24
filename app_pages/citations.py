"""Dependency-free citation cards, embedded as an inline Streamlit v2 component."""
from enterprise.citations import analysis_citation_html

CITATION_CSS = r'''
.citation-analysis {color:var(--st-text-color,#172d48);font-family:var(--st-font,sans-serif);font-size:1rem;line-height:1.85;}
.analysis-overview {margin:0 0 1rem;line-height:1.8;}
.analysis-section {padding:.15rem 0 .85rem;}
.citation-analysis .analysis-section h3 {font-size:14px;font-weight:700;color:#0b0b0b;margin:14px 0 4px;line-height:1.6;}
.analysis-prose {margin:0;white-space:pre-wrap;overflow-wrap:anywhere;}
.citation-analysis .analysis-recommendation {border-left:3px solid #2a78d6;padding:6px 0 6px 14px;margin:10px 0;}
.citation-analysis .analysis-recommendation-label {color:#2a78d6;font-weight:700;}
.citation-analysis .analysis-reading-note {margin:10px 0 4px;font-weight:600;}
.citation-analysis .analysis-sources {margin:6px 0 0;white-space:pre-wrap;overflow-wrap:anywhere;}
.cite {display:inline;vertical-align:super;font-size:.7em;line-height:0;color:#1D4ED8;border-radius:4px;padding:2px 3px;margin:0 1px;cursor:pointer;white-space:nowrap;font-weight:650;touch-action:manipulation;}
.cite:hover,.cite:focus-visible,.cite[aria-expanded="true"] {background:#DBEAFE;color:#1E40AF;outline:2px solid transparent;}
.cite:focus-visible {outline-color:#93C5FD;outline-offset:2px;}
.citation-card {position:fixed;inset:auto;margin:0;box-sizing:border-box;width:min(440px,calc(100vw - 24px));padding:16px 18px;background:#fff;color:#24374e;border:1px solid #d8e3ef;border-radius:12px;box-shadow:0 10px 32px #18365524;z-index:1000001;font:14px/1.65 var(--st-font,sans-serif);max-height:calc(100vh - 24px);overflow-y:auto;overscroll-behavior:contain;}
.citation-card[hidden] {display:none !important;}
.citation-card::backdrop {background:transparent;pointer-events:none;}
.citation-card.preview {width:min(400px,calc(100vw - 24px));padding:11px 14px;pointer-events:none;}
.citation-head {display:flex;gap:8px;align-items:flex-start;margin-bottom:8px;position:sticky;top:0;background:#fff;z-index:1;}
.citation-filename {font-weight:700;font-size:14px;overflow-wrap:anywhere;flex:1;min-width:0;}
.citation-type {display:inline-block;padding:1px 6px;border-radius:4px;background:#edf4fe;color:#315a8c;font-size:10px;white-space:nowrap;}
.citation-close {border:0;border-radius:5px;background:transparent;color:#63778e;cursor:pointer;font-size:22px;line-height:20px;padding:1px 4px;}
.citation-close:hover,.citation-close:focus-visible {color:#1D4ED8;background:#eff6ff;}
.citation-location,.citation-score {font-size:12px;color:#60738a;overflow-wrap:anywhere;}
.citation-location {margin:8px 0 0;}
.citation-content {margin:0;white-space:pre-wrap;overflow-wrap:anywhere;overflow-y:auto;overscroll-behavior:contain;max-height:min(45vh,360px);scrollbar-width:thin;}
.citation-score {border-top:1px solid #e6edf5;margin-top:11px;padding-top:9px;}
.preview .citation-content {overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-height:24px;}
.preview .citation-type,.preview .citation-close,.preview .citation-location,.preview .citation-score {display:none;}
@media(max-width:600px){.citation-card {padding:14px;width:calc(100vw - 24px);max-height:70vh;}.citation-content{max-height:42vh}.cite{padding:5px 4px}.analysis-section{padding-bottom:1rem}}
'''

# This function can also be inlined into any static report template. Its only
# inputs are an element containing server-escaped sup tags and native DOM APIs.
CITATION_RUNTIME = r'''
function installCitations(root) {
  const doc=root.ownerDocument || document;
  const card=doc.createElement('aside');
  card.className='citation-card';card.hidden=true;card.setAttribute('popover','manual');
  card.innerHTML='<div class="citation-head"><strong class="citation-filename"></strong><span class="citation-type"></span><button type="button" class="citation-close" aria-label="关闭引用详情">×</button></div><p class="citation-content"></p><p class="citation-location"></p><div class="citation-score"></div>';
  root.appendChild(card);
  let active=null,pinned=false,hoverTimer=null,resizeFrame=null,restoringFocus=false,disposed=false;
  const file=card.querySelector('.citation-filename');
  const type=card.querySelector('.citation-type');
  const location=card.querySelector('.citation-location');
  const content=card.querySelector('.citation-content');
  const score=card.querySelector('.citation-score');
  const closeButton=card.querySelector('.citation-close');
  function position() {
    if(!active || card.hidden)return;
    const target=active.getBoundingClientRect();
    const w=doc.documentElement.clientWidth,h=window.innerHeight;
    const rect=card.getBoundingClientRect();
    let left=Math.max(12,Math.min(target.left,w-rect.width-12));
    let top=target.bottom+10;
    if(top+rect.height>h-12)top=target.top-rect.height-10;
    top=Math.max(12,Math.min(top,h-rect.height-12));
    card.style.left=left+'px';card.style.top=top+'px';
  }
  function close(restore=false) {
    clearTimeout(hoverTimer);
    const old=active;
    if(old)old.setAttribute('aria-expanded','false');
    if(card.matches(':popover-open'))card.hidePopover();
    card.hidden=true;active=null;pinned=false;
    if(restore && old && old.isConnected){restoringFocus=true;old.focus({preventScroll:true});restoringFocus=false;}
  }
  function show(sup,sticky=false,keyboard=false) {
    clearTimeout(hoverTimer);
    if(disposed || !root.isConnected || !card.isConnected || !sup.isConnected || !root.contains(sup))return;
    if(active && active!==sup)active.setAttribute('aria-expanded','false');
    active=sup;pinned=sticky;sup.setAttribute('aria-expanded',sticky?'true':'false');
    // All source properties remain text. Never interpret evidence as HTML.
    file.textContent=sup.dataset.source || '来源未提供';
    type.textContent=sup.dataset.type || '资料';
    location.textContent=sup.dataset.location || '';
    content.textContent=sup.dataset.content || '';
    score.textContent=(sup.dataset.scoreLabel || '相关度')+'：'+(sup.dataset.score || '未提供')+' · 检索分数不代表结论置信度';
    card.classList.toggle('preview',!sticky);card.hidden=false;
    card.setAttribute('role',sticky?'dialog':'tooltip');
    card.setAttribute('aria-label',sticky?'引用原文详情':'引用摘要');
    if(typeof card.showPopover==='function' && !card.matches(':popover-open'))card.showPopover();
    position();
    if(keyboard && sticky)closeButton.focus({preventScroll:true});
  }
  const find=(event)=>event.composedPath().find(n=>n instanceof Element && n.matches && n.matches('sup.cite') && root.contains(n));
  function click(event) {
    const sup=find(event);
    if(!sup)return;
    event.preventDefault();
    if(pinned && active===sup)close();else show(sup,true,event.detail===0);
  }
  function enter(event) {
    if(event.pointerType==='touch' || pinned)return;
    const sup=find(event);if(!sup)return;
    clearTimeout(hoverTimer);hoverTimer=setTimeout(()=>show(sup,false),120);
  }
  function leave(event) {
    const sup=find(event);
    if(!sup || pinned)return;
    if(event.relatedTarget && sup.contains(event.relatedTarget))return;
    clearTimeout(hoverTimer);close();
  }
  function focus(event) {const sup=find(event);if(sup && !pinned && !restoringFocus)show(sup,false);}
  function blur(event) {if(find(event) && !pinned)close();}
  function key(event) {
    const sup=find(event);
    if((event.key==='Enter'||event.key===' ') && sup){event.preventDefault();if(pinned&&active===sup)close(true);else show(sup,true,true);}
    else if(event.key==='Escape' && active){event.preventDefault();close(pinned);}
  }
  function outside(event) {
    if(!active)return;
    const path=event.composedPath();
    if(!path.includes(active)&&!path.includes(card))close();
  }
  function viewport() {cancelAnimationFrame(resizeFrame);resizeFrame=requestAnimationFrame(()=>{if(pinned)position();else close();});}
  closeButton.addEventListener('click',()=>close(true));
  root.addEventListener('click',click);root.addEventListener('pointerover',enter);root.addEventListener('pointerout',leave);root.addEventListener('focusin',focus);root.addEventListener('focusout',blur);
  doc.addEventListener('keydown',key);doc.addEventListener('pointerdown',outside,true);
  window.addEventListener('resize',viewport);doc.addEventListener('scroll',viewport,true);
  return ()=>{disposed=true;close();card.remove();root.removeEventListener('click',click);root.removeEventListener('pointerover',enter);root.removeEventListener('pointerout',leave);root.removeEventListener('focusin',focus);root.removeEventListener('focusout',blur);doc.removeEventListener('keydown',key);doc.removeEventListener('pointerdown',outside,true);window.removeEventListener('resize',viewport);doc.removeEventListener('scroll',viewport,true);cancelAnimationFrame(resizeFrame);};
}
'''

CITATION_JS = CITATION_RUNTIME + r'''
export default function(component) {
  const {parentElement,data}=component;
  const host=parentElement.querySelector('.citation-host');
  host.innerHTML=data.html;
  return installCitations(host);
}
'''
def render_analysis_citations(sections, evidence, *, overview='', key):
    """Mount escaped prose and citation cards without changing retrieval behavior."""
    from app_pages.component_registry import inline_component
    renderer=inline_component('analysis_citations',
        html='<div class="citation-host"></div>',css=CITATION_CSS,js=CITATION_JS)
    # Some historical producers kept valid adopted IDs only in evidence_ids.
    # Keep every resolvable audit reference, even when other inline IDs exist.
    from enterprise.citations import citation_catalog
    known = citation_catalog(evidence)
    rows = []
    for section in sections or []:
        row = dict(section)
        text = str(row.get('text') or '')
        missing = [ref for ref in dict.fromkeys(row.get('evidence_ids') or [])
                   if ref in known and '[' + ref + ']' not in text]
        if missing:
            text += '\n核查依据：' + ' '.join('[' + ref + ']' for ref in missing)
        row['text'] = text
        if 'display_parts' not in row:
            # Style only an explicitly stored recommendation, never a coincidental
            # occurrence of 建议 inside evidence/model prose. Text stays byte-exact.
            action = row.get('recommendation') or row.get('immediate_action')
            if isinstance(action, str) and action and action in text:
                before, _, after = text.partition(action)
                row['display_parts'] = ([{'kind': 'prose', 'text': before}] if before else [])
                row['display_parts'].append({'kind': 'recommendation', 'text': action})
                if after:
                    row['display_parts'].append({'kind': 'prose', 'text': after})
        rows.append(row)
    markup = (reading_citation_html(rows, evidence, overview=overview)
              if any('display_parts' in row for row in rows)
              else analysis_citation_html(rows, evidence, overview=overview))
    return renderer(key=key,data={'html':markup},height='content')


def _narrative_text(value):
    """Accept scalar prose or paragraph lists without repr/list artifacts."""
    if isinstance(value, (list, tuple)):
        return '\n'.join(str(item).strip() for item in value if item is not None and str(item).strip())
    return str(value or '').strip()


def _section_refs(section, *texts):
    import re
    valid = r'[A-Za-z][A-Za-z0-9_-]{0,63}|\d{1,4}'
    refs = [ref for ref in section.get('evidence_ids') or []
            if isinstance(ref, str) and re.fullmatch(valid, ref)]
    for text in texts:
        refs.extend(re.findall(r'\[(' + valid + r')\]', str(text)))
    return list(dict.fromkeys(refs))


def reading_citation_html(sections, evidence, *, overview=''):
    """Escape every prose segment; only renderer-owned recommendation markup is HTML.

    Citation numbering is shared within this reading surface. Keep the existing
    accessible sup anchors/data attributes and never put source text into styles.
    """
    from html import escape
    from enterprise.citations import citation_catalog, replace_citations
    known, numbering, blocks = citation_catalog(evidence), {}, []
    if overview:
        blocks.append('<p class="analysis-overview">' + replace_citations(overview, evidence, numbering=numbering) + '</p>')
    for row in sections or []:
        title = str(row.get('title') or row.get('element') or '')
        block = ['<section class="analysis-section"><h3>' + escape(title) + '</h3>']
        parts = row.get('display_parts', [{'kind': 'prose', 'text': row.get('text', '')}])
        rendered_text = []
        for part in parts:
            text = str(part.get('text') or '')
            if not text:
                if part.get('label'):
                    block.append('<p class="analysis-reading-note">' + escape(str(part['label'])) + '</p>')
                continue
            rendered_text.append(text)
            prose = replace_citations(text, evidence, numbering=numbering)
            if part.get('kind') == 'recommendation':
                # Wrap an existing prefix without altering copyable text; do
                # not print a second 建议 when the accepted action has one.
                import re
                body = text[2:] if text.startswith('建议') else text
                items = [item.strip() for item in re.split(r'(?<=[。；])\s*', body) if item.strip()]
                if len(items) > 1 and not any(marker in text for marker in '①②③④⑤') and '\n' not in text:
                    # 多句且无编号的建议按句拆分为 ①②③ 列表（对齐赛题合格示例格式）
                    label = ('<span class="analysis-recommendation-label">建议</span>' if text.startswith('建议')
                             else '<div class="analysis-recommendation-label">建议</div>')
                    markers = '①②③④⑤'
                    numbered = ''.join(
                        '<p class="analysis-prose">' + (markers[index] if index < 5 else f'{index + 1}.')
                        + replace_citations(item, evidence, numbering=numbering) + '</p>'
                        for index, item in enumerate(items))
                    block.append('<div class="analysis-recommendation">' + label + numbered + '</div>')
                elif text.startswith('建议'):
                    # 单句建议：前缀 span 保留在段落内，保持原文连续性
                    prose = ('<span class="analysis-recommendation-label">建议</span>'
                             + replace_citations(body, evidence, numbering=numbering))
                    block.append('<div class="analysis-recommendation">'
                                 + '<p class="analysis-prose">' + prose + '</p></div>')
                else:
                    prose = replace_citations(body, evidence, numbering=numbering)
                    block.append('<div class="analysis-recommendation">'
                                 + '<div class="analysis-recommendation-label">建议</div>'
                                 + '<p class="analysis-prose">' + prose + '</p></div>')
            else:
                label = part.get('label')
                if label:
                    block.append('<p class="analysis-reading-note">' + escape(str(label)) + '</p>')
                block.append('<p class="analysis-prose">' + prose + '</p>')
        present = set(_section_refs({}, *rendered_text))
        extras = [ref for ref in _section_refs(row) if ref in known and ref not in present]
        if extras:
            marks = ' '.join('[' + ref + ']' for ref in extras)
            block.append('<p class="analysis-sources">核查依据：' + replace_citations(marks, evidence, numbering=numbering) + '</p>')
        block.append('</section>')
        blocks.append(''.join(block))
    return '<article class="citation-analysis">' + ''.join(blocks) + '</article>'


def _bound_prose(section):
    prose = section.get('prose')
    return (section.get('prose_mode') == 'bound-numeric-prose/1'
            and isinstance(prose, str) and bool(prose.strip()))


def prose_reading_sections(sections, *, overview='', analysis_kind='attribution'):
    """Choose accepted prose verbatim, never split/rewrite its numerical claims."""
    rows = []
    if analysis_kind == 'benchmark':
        if overview:
            rows.append({'title': '找差异', 'text': overview,
                         'display_parts': [{'kind': 'prose', 'text': overview}]})
        structure = '\n'.join(_narrative_text(section.get('fact')) for section in sections if section.get('fact'))
        if structure:
            rows.append({'title': '拆结构', 'text': structure,
                         'display_parts': [{'kind': 'prose', 'text': structure}]})
        rows.append({'title': '拆原因', 'display_parts': [], 'text': ''})
    for section in sections:
        parts = []
        if _bound_prose(section):
            # Exact adopted text is neither trimmed nor duplicated as hypothesis.
            parts.append({'kind': 'prose', 'text': section['prose']})
            action = section.get('model_followup_action') or section.get('accepted_model_recommendation')
            if not isinstance(action, str) or not action.strip():
                action = section.get('recommendation') or section.get('immediate_action')
            if action and section.get('claim_type') != 'no_difference':
                parts.append({'kind': 'recommendation', 'text': action})
        else:
            # Mixed adopted/fallback outputs preserve every deterministic numeric
            # cause and actionable sentence; never compact them to 160 chars.
            compact = compact_analysis_sections([section], conclusions=True)[0]
            facts = (section.get('comparison_explanation')
                     if analysis_kind == 'benchmark' and section.get('prose_fact_role') == 'structure'
                     else compact.get('numeric_explanation') or compact['text'])
            if facts:
                parts.append({'kind': 'prose', 'text': facts})
            interpretation = '\n'.join(value for value in
                (compact.get('mechanism_note'), compact.get('hypothesis')) if value)
            if interpretation:
                parts.append({'kind': 'prose', 'label': '机制边界（待核查）', 'text': interpretation})
            for field in ('recommendation', 'model_followup_action'):
                if compact.get(field):
                    parts.append({'kind': 'recommendation', 'text': compact[field]})
        refs = _section_refs(section, *(part['text'] for part in parts))
        refs.extend(_section_refs({'evidence_ids': section.get('model_action_evidence_ids') or []}))
        rows.append({'title': section.get('title') or section.get('element'),
                     'display_parts': parts, 'text': '\n\n'.join(part['text'] for part in parts),
                     'evidence_ids': list(dict.fromkeys(refs))})
    return rows


def compact_analysis_sections(sections, *, conclusions=False, limit=160):
    """Adapt old snapshots; never compact shared numeric causes or actions.

    New analysis_narrative sections keep numeric, interpretation, action and gap
    fields separate. Only legacy summary prose uses the historical length budget;
    generated actions are never invented or admitted before explicit generation.
    """
    import re
    if not isinstance(limit, int) or limit < 80:
        raise ValueError('summary limit must be at least 80 characters')
    compact = []
    marker = r'\[(?:[A-Za-z][A-Za-z0-9_-]{0,63}|\d{1,4})\]'
    sentence_pattern = r'[^。！？]+[。！？](?:\s*' + marker + r')*|[^。！？]+$'
    uncertain = ('可能', '尚不能', '不能', '尚缺', '待核查', '待核实', '不足以')
    for section in sections or []:
        if 'numeric_explanation' in section or 'observed_reason' in section:
            numeric = _narrative_text(section.get('numeric_explanation')) or '\n'.join(
                part for part in (_narrative_text(section.get('fact')),
                                  _narrative_text(section.get('observed_reason'))) if part)
            # Numeric-only summary cites its own observed sources; model and
            # mechanism audit IDs belong to their labeled action/detail surface.
            refs = _section_refs({}, numeric)
            row = {'title': section.get('title') or section.get('element'),
                   'text': numeric, 'numeric_explanation': numeric, 'evidence_ids': refs}
            if conclusions:
                row['mechanism_note'] = _narrative_text(section.get('mechanism_note'))
                hypothesis = _narrative_text(section.get('hypothesis'))
                if hypothesis and hypothesis != row['mechanism_note']:
                    row['hypothesis'] = hypothesis
                row['evidence_gaps'] = section.get('evidence_gaps', section.get('missing_evidence', []))
                row['followup_criteria'] = _narrative_text(section.get('followup_criteria'))
                if section.get('claim_type') != 'no_difference':
                    # Accepted model prose is an independently audited field.
                    # Preserve it byte-for-byte; do not replace it with the
                    # deterministic immediate action or a lossy compact summary.
                    model_action = section.get('model_followup_action',
                                               section.get('accepted_model_recommendation', ''))
                    if isinstance(model_action, str) and model_action.strip():
                        row['model_followup_action'] = model_action
                        row['model_action_evidence_ids'] = _section_refs(
                            {'evidence_ids': section.get('model_action_evidence_ids') or []}, model_action)
                    action = _narrative_text(section.get('immediate_action')
                                             if 'immediate_action' in section else section.get('recommendation'))
                    if action:
                        row['recommendation'] = action
                        row['recommendation_evidence_ids'] = _section_refs(section, numeric, action,
                            row['mechanism_note'], hypothesis, section.get('text', ''))
            compact.append(row)
            continue
        fact = section.get('fact') or str(section.get('text') or '').split('\n', 1)[0]
        sentences = [part.strip() for part in re.findall(sentence_pattern, fact) if part.strip()]
        fact_refs = list(dict.fromkeys(re.findall(marker, fact)))
        selected = []
        for part in sentences[:2]:
            candidate = ''.join(selected + [part])
            # Facts in the original paragraph may share final source markers.
            suffix = ''.join(ref for ref in fact_refs if ref not in candidate)
            if len(candidate + suffix) <= limit:
                selected.append(part)
        text = ''.join(selected)
        if text:
            text += ''.join(ref for ref in fact_refs if ref not in text)
        hypothesis = section.get('hypothesis')
        if conclusions and hypothesis:
            # Structured fields omit inline IDs in benchmark_ai; the matching
            # narrative paragraph carries the same hypothesis with its sources.
            hypothesis = next((part.strip() for part in str(section.get('text') or '').split('\n')
                               if part.strip().startswith(hypothesis)), hypothesis)
        if conclusions and not hypothesis:
            hypothesis = next((part.strip() for part in str(section.get('text') or '').split('\n')[1:]
                               if any(word in part for word in uncertain)), '')
        if conclusions and hypothesis:
            # A later sentence can contain a condition or shared source marker.
            # Admit the whole qualified unit, never an isolated causal sentence.
            part = hypothesis.strip()
            if (part and any(word in part for word in uncertain)
                    and len(text + part) <= limit):
                text += part
        text = text or '本段较长，请展开完整分析查看原始结论与引用。'
        ids = re.findall(r'\[([A-Za-z][A-Za-z0-9_-]{0,63}|\d{1,4})\]', text)
        row = {'title': section.get('title') or section.get('element'),
               'text': text, 'evidence_ids': list(dict.fromkeys(ids))}
        if conclusions and section.get('claim_type') != 'no_difference':
            narrative = str(section.get('text') or '').strip()
            if 'recommendation' in section:
                # An explicit empty field is intentional (e.g. no difference),
                # not permission to recover a stale action from another field.
                recommendation = str(section.get('recommendation') or '').strip()
                # Do not append the historical narrative tail: older benchmark
                # text made absent external vouchers a completion prerequisite.
                # The authoritative action stays separate from missing_evidence.
            else:
                # attribution_narrative.render appends an action starting with
                # 建议. Its validated model supplement may contain newlines;
                # retain the whole tail, including later conditions and sources.
                paragraphs = narrative.split('\n')
                action_start = next((index for index, part in enumerate(paragraphs[1:], 1)
                                     if part.strip().startswith('建议')), None)
                recommendation = ('\n'.join(paragraphs[action_start:]).strip()
                                  if action_start is not None else '')
            if recommendation:
                # Advice has its own visible row; the fact limit must not hide
                # the action, qualification or valid source IDs (including old
                # numeric IDs supported by the underlying citation renderer).
                refs = _section_refs(section, recommendation, fact, narrative)
                row['recommendation'] = recommendation
                row['recommendation_evidence_ids'] = list(dict.fromkeys(refs))
        compact.append(row)
    return compact


def render_layered_analysis(sections, evidence, *, overview='', key, generated=True,
                            limitations=(), followup_criteria='', analysis_kind='attribution'):
    """Keep core numeric causes beside recommendations, without mixing claim types."""
    import streamlit as st
    sections = list(sections or [])
    compact = compact_analysis_sections(sections, conclusions=generated)
    prose_mode = generated and any(_bound_prose(section)
        or section.get('reading_style') == 'contextual-reading/1.4' for section in sections)
    if prose_mode:
        # One uninterrupted main reading surface; deterministic paragraphs stay
        # accessible in the audit expander instead of repeating accepted prose.
        render_analysis_citations(prose_reading_sections(sections, overview=overview,
            analysis_kind=analysis_kind), evidence, key=key+'_reading')
    else:
        # Generated shared numeric facts already appear beside their action.
        # Keep only actionless/legacy summaries here to avoid double-reading.
        summary = [row for row in compact if not (generated and row.get('numeric_explanation')
                    and (row.get('recommendation') or row.get('model_followup_action')))]
        if summary:
            render_analysis_citations(summary, evidence, key=key+'_summary')
    if generated:
        actions = []
        for row in ([] if prose_mode else compact):
            if not row.get('recommendation') and not row.get('model_followup_action'):
                continue
            parts = []
            display_parts = []
            if row.get('numeric_explanation'):
                parts.append('已计算事实与数字原因\n' + row['numeric_explanation'])
                display_parts.append({'kind': 'prose', 'label': '已计算事实与数字原因', 'text': row['numeric_explanation']})
            interpretation = '\n'.join(part for part in
                (row.get('mechanism_note'), row.get('hypothesis')) if part)
            if interpretation:
                parts.append('解释与机制边界（待核查）\n' + interpretation)
                display_parts.append({'kind': 'prose', 'label': '解释与机制边界（待核查）', 'text': interpretation})
            if row.get('recommendation'):
                parts.append(('现在可执行的核查\n' if row.get('numeric_explanation') else '')
                             + row['recommendation'])
                if row.get('numeric_explanation'):
                    display_parts.append({'kind': 'prose', 'label': '现在可执行的核查', 'text': ''})
                display_parts.append({'kind': 'recommendation', 'text': row['recommendation']})
            if row.get('model_followup_action'):
                parts.append('已校验补充建议\n' + row['model_followup_action'])
                display_parts.append({'kind': 'prose', 'label': '已校验补充建议', 'text': ''})
                display_parts.append({'kind': 'recommendation', 'text': row['model_followup_action']})
            advice = '\n\n'.join(parts)
            refs = list(dict.fromkeys([*row.get('recommendation_evidence_ids', []),
                                      *row.get('model_action_evidence_ids', [])]))
            missing_refs = [ref for ref in refs if '[' + ref + ']' not in advice]
            if missing_refs:
                advice += '\n核查依据：' + ' '.join('[' + ref + ']' for ref in missing_refs)
            actions.append({'title': row['title'], 'text': advice, 'evidence_ids': refs,
                            'display_parts': display_parts})
        if actions:
            st.subheader('核查建议')
            st.caption('数字原因、机制边界与当前行动分别列示；以下不代表已确认原因、已完成整改或已实现节约。')
            if any(row.get('model_followup_action') for row in compact):
                st.caption('已校验补充建议：保留模型已通过校验的原文，单独标注；不替代当前可执行行动，也不把证据缺口设为前提。')
            render_analysis_citations(actions, evidence, key=key+'_actions')
        criteria = list(dict.fromkeys(part for part in
            [_narrative_text(followup_criteria), *(_narrative_text(row.get('followup_criteria')) for row in sections)] if part))
        if criteria:
            st.caption('后续复核与完成口径（不作为当前核算分析的启动前提）')
            for criterion in criteria:
                st.write(criterion)
        with st.expander('展开完整分析与引用'):
            if prose_mode:
                render_analysis_citations(prose_reading_sections(sections, overview=overview,
                    analysis_kind=analysis_kind), evidence, key=key+'_full')
            else:
                render_analysis_citations(sections, evidence, overview=overview, key=key+'_full')
            if prose_mode:
                audit_rows = [{'title': section.get('title') or section.get('element'),
                               'text': _narrative_text(section.get('numeric_explanation')),
                               'evidence_ids': _section_refs({}, section.get('numeric_explanation', ''))}
                              for section in sections if section.get('numeric_explanation')]
                if audit_rows:
                    st.caption('确定性数值原因与明细（审计核对，不重复作为散文正文）')
                    render_analysis_citations(audit_rows, evidence, key=key+'_numeric_audit')
        missing = []
        for row in sections:
            gaps = row.get('evidence_gaps', row.get('missing_evidence', []))
            if gaps:
                missing.append({'要素': row.get('title') or row.get('element'),
                                '证据缺口（不阻断当前核算分析）': _narrative_text(gaps)})
        if missing or limitations:
            with st.expander('证据缺口与假设边界'):
                if missing:
                    st.dataframe(missing, hide_index=True)
                for item in limitations:
                    st.write(item)
                st.caption('证据缺口单列，不是当前分析或核对的完成前提；缺口不等于已确认异常，核查建议不等于整改或节约。')


def standalone_citation_html(sections,evidence,*,overview=''):
    """Self-contained local preview for interaction/XSS acceptance tests."""
    body=analysis_citation_html(sections,evidence,overview=overview)
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<style>body{margin:24px auto;padding:0 18px;max-width:920px;font-family:system-ui,sans-serif;}'+CITATION_CSS+'</style>'
            '</head><body><main class="citation-host">'+body+'</main><script>'+CITATION_RUNTIME+
            '\ninstallCitations(document.querySelector(".citation-host"));</script></body></html>')
