"""Safe, single-pass adopted prose and recommendation styling; offline only."""
from copy import deepcopy
from html import escape
from html.parser import HTMLParser
import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app_pages.citations import (CITATION_CSS, CITATION_RUNTIME, prose_reading_sections,
                                 reading_citation_html)


class Markup(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.words, self.tags, self.refs = [], [], []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if tag == 'sup':
            self.refs.append(attrs)

    def handle_data(self, value):
        self.words.append(value)

    @property
    def text(self):
        return ''.join(self.words)


def fixture():
    prose = ('材料金额下降 -66,900.00 元，贡献 +64.53%。  保留双空格。\n'
             '市场参考价由 138.00 变为 133.50 元/kg，若结算同步下降则价格因素可能作用，待凭证确认。')
    action = ('建议：①采购部用已有明细核对结算口径；②生产部核对批次投料。\n'
              + '保留未核实原因与缺口边界，不以缺失凭证阻断当前核算。' * 12)
    numeric = '确定性审计独有句：产量影响 -58,400.00 元，单位成本影响 -8,500.00 元。[F001]'
    section = {'title': '直接材料', 'element': '材料', 'fact': '材料单位差 +0.01 元/盒，贡献 -2.56%。[F001]',
               'prose': prose, 'prose_mode': 'bound-numeric-prose/1',
               'hypothesis': prose, 'numeric_explanation': numeric,
               'mechanism_note': '提取收率基准仅作机制边界。[K001]',
               'immediate_action': '确定性动作只作审计，已有明细先核对。',
               'model_followup_action': action, 'accepted_model_recommendation': action,
               'recommendation': '确定性动作只作审计。\n已校验补充建议：' + action,
               'model_action_evidence_ids': ['K001', 'G001'],
               'evidence_ids': ['F001', 'K001', 'G001'],
               'evidence_gaps': ['尚未提供二厂批次记录'], 'claim_type': 'hypothesis',
               'text': numeric + '\n' + prose + '\n' + action,
               'followup_criteria': '共同完成口径：已有明细先勾稽，外部资料取得后再核实。'}
    sources = [{'id': ident, 'kind': 'accounting_fact' if ident == 'F001' else 'document_basis',
                'source': {'file': ident + '.txt', 'line': 2}, 'text': '原文记录：仅供测试。'}
               for ident in ('F001', 'K001', 'G001')]
    return [section], sources


def display(sections, sources, kind, generated):
    from app_pages.citations import render_layered_analysis
    render_layered_analysis(sections, sources, key='prose_test', generated=generated,
                            overview='两厂同品同规格总单位差 -0.39 元/盒。', analysis_kind=kind,
                            followup_criteria=sections[0].get('followup_criteria', ''))


def components(app):
    return {node.key: Markup(json.loads(node.proto.json)['html']) for node in app.get('bidi_component')}


def run(sections, sources, kind='attribution', generated=True):
    app = AppTest.from_function(display, args=(sections, sources, kind, generated), default_timeout=20).run()
    assert not app.exception
    return app


def test_multi_sentence_recommendation_renders_numbered_list():
    sections, sources = fixture()
    action = '建议采购部核对已有甲料成本明细与产出口径。生产部核对批次投料记录。'
    for key in ('model_followup_action', 'accepted_model_recommendation'):
        sections[0][key] = action
    app = run(sections, sources)
    rendered = components(app)
    main = rendered['prose_test_reading'].text
    assert '①' in main and '②' in main
    assert main.count('建议') == 1  # 前缀只渲染一次，不重复打印


def test_bound_prose_once_main_and_complete_deterministic_audit_retained():
    sections, sources = fixture()
    before = deepcopy(sections)
    app = run(sections, sources)
    rendered = components(app)
    assert 'prose_test_reading' in rendered
    assert 'prose_test_summary' not in rendered and 'prose_test_actions' not in rendered
    main = rendered['prose_test_reading'].text
    section = sections[0]
    assert main.count(section['prose']) == 1
    assert main.count(section['model_followup_action']) == 1
    assert main.count('建议') == 1  # Style the existing prefix, not a duplicate label.
    assert len(section['model_followup_action']) > 160
    assert '确定性审计独有句' not in main
    assert '确定性动作只作审计' not in main
    assert '提取收率基准仅作机制边界' not in main
    assert '确定性审计独有句' in rendered['prose_test_numeric_audit'].text
    assert section['prose'] in rendered['prose_test_full'].text
    audit = next(exp for exp in app.expander if exp.label == '展开完整分析与引用')
    assert any(node.key == 'prose_test_numeric_audit' for node in audit.get('bidi_component'))
    assert any(node.key == 'prose_test_reading' for node in app.main.children.values()
               if node.type == 'bidi_component')
    assert sum(section['followup_criteria'] in item.value for item in app.markdown) == 1
    assert sections == before


def test_three_step_benchmark_reading_keeps_signed_structure_and_exact_cause():
    sections, sources = fixture()
    main = components(run(sections, sources, kind='benchmark'))['prose_test_reading']
    text = main.text
    for heading in ('找差异', '拆结构', '拆原因', '直接材料'):
        assert heading in text
    assert text.index('找差异') < text.index('拆结构') < text.index('拆原因') < text.index('直接材料')
    assert '贡献 -2.56%' in text
    assert text.count(sections[0]['prose']) == 1
    assert '建议' in text
    assert {'F001', 'K001', 'G001'} == {item['data-evidence-id'] for item in main.refs}


def test_safe_styling_applies_only_to_recommendations_and_never_evidence_html():
    sections, sources = fixture()
    attack = '<img src=x onerror="window.XSS=1"><script>alert(1)</script>'
    sections[0]['title'] += attack
    sections[0]['prose'] += attack
    sections[0]['model_followup_action'] += attack
    sources[1]['source']['file'] = '恶意" onmouseover="x.txt'
    sources[1]['text'] = attack
    before = deepcopy(sections)
    rows = prose_reading_sections(sections)
    html = reading_citation_html(rows, sources)
    parsed = Markup(html)
    assert {'script', 'img'}.isdisjoint(tag for tag, _ in parsed.tags)
    assert escape(attack) in html
    assert parsed.text.count(attack) == 3  # title, prose and recommendation stay copyable text.
    blocks = [attrs for tag, attrs in parsed.tags if attrs.get('class') == 'analysis-recommendation']
    assert len(blocks) == 1
    assert 'border-left:3px solid #2a78d6' in CITATION_CSS
    assert '.citation-analysis .analysis-recommendation {' in CITATION_CSS
    assert 'padding:6px 0 6px 14px' in CITATION_CSS
    assert '.citation-analysis .analysis-recommendation-label {color:#2a78d6;font-weight:700;}' in CITATION_CSS
    assert '.citation-analysis .analysis-section h3 {font-size:14px;font-weight:700;color:#0b0b0b;margin:14px 0 4px;' in CITATION_CSS
    prose_rule = CITATION_CSS.split('.analysis-prose {', 1)[1].split('}', 1)[0]
    assert 'border' not in prose_rule and 'padding' not in prose_rule
    assert 'style=' not in html
    assert sections == before


def test_citations_keep_real_accessible_anchors_original_tooltips_and_ids():
    sections, sources = fixture()
    sections[0]['prose'] += '[K001]'
    section = sections[0]
    section['model_followup_action'] += '[K001]'
    markup = Markup(reading_citation_html(prose_reading_sections(sections), sources))
    refs = markup.refs
    assert len([ref for ref in refs if ref['data-evidence-id'] == 'K001']) == 2
    assert all(ref['tabindex'] == '0' and ref['role'] == 'button' and ref['aria-expanded'] == 'false' for ref in refs)
    assert all(ref['data-source'] and ref['data-content'] and ref['data-location'] for ref in refs)
    assert '¹' not in markup.text  # no hardcoded pseudo footnote.
    assert "event.key==='Enter'||event.key===' '" in CITATION_RUNTIME
    assert "event.key==='Escape'" in CITATION_RUNTIME
    assert 'file.textContent=sup.dataset.source' in CITATION_RUNTIME


@pytest.mark.parametrize('mode,prose', [('', 'unadopted prose'), ('unknown/1', 'unadopted prose'),
                                      ('bound-numeric-prose/1', ''), ('bound-numeric-prose/1', '   ')])
def test_prose_mode_requires_exact_contract_and_nonblank_text(mode, prose):
    sections, sources = fixture()
    sections[0].update(prose_mode=mode, prose=prose)
    rendered = components(run(sections, sources))
    assert 'prose_test_reading' not in rendered
    assert '确定性审计独有句' in rendered['prose_test_actions'].text


def test_generated_false_never_exposes_accepted_prose_or_advice():
    sections, sources = fixture()
    rendered = components(run(sections, sources, generated=False))
    assert set(rendered) == {'prose_test_summary'}
    assert sections[0]['prose'] not in rendered['prose_test_summary'].text
    assert sections[0]['model_followup_action'] not in rendered['prose_test_summary'].text


def test_nonprose_shared_numeric_and_action_show_once_with_single_criteria():
    sections, sources = fixture()
    section = sections[0]
    section.update(prose='', prose_mode='', hypothesis='可能为归集口径差异，尚待核对。')
    app = run(sections, sources)
    rendered = components(app)
    assert 'prose_test_reading' not in rendered and 'prose_test_summary' not in rendered
    main = rendered['prose_test_actions'].text
    assert main.count('确定性审计独有句') == 1
    assert main.count(section['immediate_action']) == 1
    assert main.count(section['model_followup_action']) == 1
    assert sum(section['followup_criteria'] in item.value for item in app.markdown) == 1


def test_current_page_callers_explicitly_select_analysis_reading_kind():
    root = Path(__file__).resolve().parents[1]
    assert "analysis_kind='attribution'" in (root / 'dashboard_web.py').read_text(encoding='utf-8')
    assert "analysis_kind='benchmark'" in (root / 'app_pages/benchmark.py').read_text(encoding='utf-8')
