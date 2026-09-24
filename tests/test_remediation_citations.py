from html import unescape
from html.parser import HTMLParser
from enterprise.citations import analysis_citation_html, citation_catalog, replace_citations
from app_pages.citations import standalone_citation_html


class Elements(HTMLParser):
    def __init__(self):super().__init__();self.tags=[]
    def handle_starttag(self,tag,attrs):self.tags.append((tag,dict(attrs)))


def evidence():
    return [{'id':'K001','kind':'document_basis','source':{'file':r'D:\internal\生产工艺文档.pdf','page':4,'offset':120,'end_offset':143},
             'text':'粉碎收率标准 ≥97%。标准不证明当期实际收率。','retrieval_scores':{'rrf_score':0.045678912}}]


def test_repeated_reference_has_stable_sup_and_source_content_score():
    body=analysis_citation_html([{'title':'材料','text':'原文事实[K001]。另处[K001]。','evidence_ids':['K001']}],evidence())
    parser=Elements();parser.feed(body)
    refs=[attrs for tag,attrs in parser.tags if tag=='sup']
    assert len(refs)==2 and body.count('>[1]</sup>')==2
    assert all(r['data-source']=='生产工艺文档.pdf' for r in refs)
    assert refs[0]['data-content']==evidence()[0]['text']
    assert refs[0]['data-score']=='0.0456789'
    assert 'D:\\internal' not in body and '页：4' in body


def test_old_frozen_sections_gain_inline_markers_without_mutation():
    sections=[{'title':'人工','text':'工时保持一致。','evidence_ids':['K001']}]
    html=analysis_citation_html(sections,evidence())
    assert '<sup ' in html and sections[0]['text']=='工时保持一致。'


def test_current_inline_citations_do_not_append_all_unused_input_sources():
    rows=evidence()+[{'id':'K002','source':{'file':'未引用资料.txt'},'text':'可用但未用于这段正文。'}]
    markup=analysis_citation_html([{'text':'已经逐句引用[K001]。','evidence_ids':['K001','K002']}],rows)
    assert markup.count('<sup ')==1 and '未引用资料.txt' not in markup


def test_hostile_source_and_prose_are_inert_in_attributes_and_dom():
    attack='\" onmouseover=\"window.XSS=1\"><img src=x onerror=window.XSS=2></script>'
    rows=[{'id':'K001','source':{'file':attack},'text':attack}]
    markup=replace_citations(attack+'[K001]',rows)
    parser=Elements();parser.feed(markup)
    assert [t for t,_ in parser.tags]==['sup']
    sup=parser.tags[0][1]
    assert 'onmouseover' not in sup and sup['data-content']==attack
    assert '<script' not in markup and '<img' not in markup
    full=standalone_citation_html([{'text':attack+'[K001]'}],rows)
    assert full.count('<script>')==1 and full.count('</script>')==1


def test_unknown_ambiguous_or_contentless_reference_is_not_invented():
    assert '<sup' not in replace_citations('不存在[K404]',evidence())
    assert '<sup' not in replace_citations('重复[K001]',evidence()*2)
    assert citation_catalog([{'id':'K001','source':'file.pdf'}])=={}


def test_fact_citation_does_not_fabricate_search_score():
    row={'id':'F003','kind':'accounting_fact','text':'两期人工核算汇总。',
         'source':{'table':'labor','records':[{'file':'人工.csv','key':{'月份':'2026-05'}},{'file':'人工.csv','key':{'月份':'2026-06'}}]}}
    value=citation_catalog([row])['F003']
    assert value['source']=='人工.csv' and '未提供' in value['score']
    assert '2026-05' in value['location'] and '2026-06' in value['location']
