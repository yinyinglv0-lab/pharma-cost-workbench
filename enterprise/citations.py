"""Safe citation markup over already-authorized analysis evidence.

This module performs no retrieval or file reads. It never executes HTML from
model prose, filenames or source text. Every attribute and visible text is
escaped; the UI uses textContent for the popover. Missing/duplicate identities
are deliberately not clickable.
"""
from __future__ import annotations

from collections import Counter
from html import escape
import math
from pathlib import PureWindowsPath
import re

_MARKER = re.compile(r'\[([A-Za-z][A-Za-z0-9_-]{0,63}|\d{1,4})\]')
_SCORE_FIELDS = (('rerank_score','重排相关度'),('rrf_score','RRF 融合相关度'),
                 ('vector_score','向量相似度'),('bm25_score','BM25 分数'),('graph_score','图谱分数'))


def _sources(value):
    if isinstance(value, str):
        return [PureWindowsPath(value).name] if value else []
    if not isinstance(value, dict):
        return []
    names = []
    if value.get('file') or value.get('filename'):
        names.append(PureWindowsPath(str(value.get('file') or value['filename'])).name)
    for record in value.get('records') or []:
        names.extend(_sources(record))
    if not names and value.get('table'):
        names.append(str(value['table']))
    return list(dict.fromkeys(names))


def _locations(value):
    if not isinstance(value, dict):
        return []
    result=[]
    key=value.get('key')
    if isinstance(key, dict):
        result.append(' · '.join(f'{k}：{v}' for k,v in key.items() if v is not None))
    for field,label in (('page','页'),('section','章节'),('line','物理行'),('record_number','记录序号')):
        if value.get(field) is not None:
            result.append(f'{label}：{value[field]}')
    if value.get('offset') is not None and value.get('end_offset') is not None:
        result.append(f"原文字符：{value['offset']}–{value['end_offset']}")
    for row in value.get('records') or []:
        result.extend(_locations(row))
    return list(dict.fromkeys(x for x in result if x))


def citation_catalog(evidence):
    """Normalize the source/content contract without inventing missing fields."""
    rows=[r for r in evidence or [] if isinstance(r, dict) and isinstance(r.get('id'), str)]
    counts=Counter(r['id'] for r in rows)
    catalog={}
    for row in rows:
        ident=row['id']
        if counts[ident]!=1:
            continue
        source=row.get('source', {})
        names=_sources(source)
        content=row.get('content') or row.get('text') or (source.get('quote') if isinstance(source,dict) else '')
        if not names or not isinstance(content,str) or not content.strip():
            continue
        suffixes=list(dict.fromkeys(PureWindowsPath(name).suffix.lstrip('.').upper() or '数据' for name in names))
        score_label,score='相关度','未提供（核算记录无检索分数）' if row.get('kind') in ('accounting_fact','data_fact') else '未提供'
        scores=row.get('retrieval_scores') or {}
        for field,label in _SCORE_FIELDS:
            value=scores.get(field, row.get(field))
            if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value):
                score_label,score=label,format(value,'.6g')
                break
        source_label='；'.join(names)
        if row.get('vision_enhanced'):
            source_label += '（视觉增强）' if row.get('vision_reviewed') else '（视觉增强·未经人工核对）'
        catalog[ident]={'source':source_label,'content':content,'type':' / '.join(suffixes),
                        'location':'；'.join(_locations(source)), 'score':score,'score_label':score_label,
                        'chunk_id':str(row.get('chunk_id') or ''),'evidence_id':ident}
    return catalog


def replace_citations(text, evidence, *, numbering=None):
    """Replace [id] with safe <sup class=cite data-source/content> tags.

    numbering is shared across sections to keep repeated citations stable.
    The returned markup is safe for innerHTML; source text is data, never markup.
    """
    catalog=citation_catalog(evidence)
    numbering={} if numbering is None else numbering
    def marker(match):
        ident=match.group(1)
        row=catalog.get(ident)
        if row is None:
            return escape(match.group(0))
        if ident not in numbering:
            numbering[ident]=len(numbering)+1
        number=numbering[ident]
        attributes=' '.join(f'data-{key.replace("_","-")}="{escape(str(value),quote=True)}"' for key,value in row.items())
        return (f'<sup class="cite" role="button" tabindex="0" aria-expanded="false" '
                f'aria-label="查看引用{number}：{escape(row["source"],quote=True)}" {attributes}>[{number}]</sup>')
    parts=[];last=0
    for match in _MARKER.finditer(str(text or '')):
        parts.append(escape(str(text)[last:match.start()]))
        parts.append(marker(match));last=match.end()
    parts.append(escape(str(text or '')[last:]))
    return ''.join(parts)


def analysis_citation_html(sections, evidence, *, overview=''):
    numbering={};blocks=[]
    if overview:
        blocks.append('<p class="analysis-overview">'+replace_citations(overview,evidence,numbering=numbering)+'</p>')
    for section in sections or []:
        title=str(section.get('title') or section.get('element') or '')
        prose=str(section.get('text') or '')
        # Older frozen sections store refs separately. They remain traceable
        # without rewriting their stored prose; only add missing known refs.
        present={m.group(1) for m in _MARKER.finditer(prose)}
        known=citation_catalog(evidence)
        extra=[ident for ident in dict.fromkeys(section.get('evidence_ids') or []) if ident in known] if not present else []
        if extra:
            prose+=' '+''.join('['+ident+']' for ident in extra)
        blocks.append('<section class="analysis-section"><h3>'+escape(title)+'</h3><p class="analysis-prose">'
                      +replace_citations(prose,evidence,numbering=numbering)+'</p></section>')
    return '<article class="citation-analysis">'+''.join(blocks)+'</article>'
