# -*- coding: utf-8 -*-
"""视觉增强解析测试：候选页判定、稿件组装、stage 覆盖登记（零网络零付费调用）。"""
import hashlib
import io

import pytest

from enterprise.knowledge import Repository
from enterprise.vision_enhancement import candidate_pages, enhance_pdf, merged_text, render_pages


def scanned_pdf_bytes():
    """单页无文本 PDF（仅图形，无文字层）。"""
    from reportlab.pdfgen import canvas
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer)
    page.rect(10, 10, 100, 100, stroke=1, fill=0)
    page.showPage()
    page.save()
    return buffer.getvalue()


def test_candidate_pages_from_coverage():
    parsed = {'metadata': {'coverage': {'pages_without_text': [2, 5], 'image_pages': [1, 3, 100]}}}
    assert candidate_pages(parsed) == [1, 2, 3, 5, 100]
    assert candidate_pages({'metadata': {}}) == []
    assert candidate_pages({'metadata': {'coverage': {}}}) == []


def test_merged_text_marks_disclaimer_and_pages():
    parsed = {'text': '原有正文'}
    sections = [{'page': 2, 'text': '视觉内容'}, {'page': 3, 'text': ''}]
    text = merged_text(parsed, sections)
    assert '视觉增强解析稿' in text and '未经人工核对' in text
    assert '原有正文' in text
    assert '[视觉增强·第2页]' in text and '视觉内容' in text


def test_render_pages_fitz_renders_synthetic_pdf():
    content = scanned_pdf_bytes()
    images = render_pages(content, [1])
    assert 1 in images
    mime, data = images[1]
    assert mime == 'image/png' and data.startswith(b'\x89PNG')


def test_enhance_pdf_happy_path_with_fake_vision(monkeypatch):
    import enterprise.multimodal as multimodal
    import enterprise.vision_enhancement as module
    calls = []
    monkeypatch.setattr(multimodal, 'provider_config',
                        lambda: ('zhipu', 'glm-4v-plus', 'sk-fixture', 'https://fixture.invalid'))
    def fake_analyze(data, mime, task, timeout=120.0):
        calls.append((mime, task))
        return {'ok': True, 'text': f'视觉识别结果[{task}]', 'meta': {}, 'disclaimer': '模型输出'}
    monkeypatch.setattr(multimodal, 'analyze_image', fake_analyze)
    content = scanned_pdf_bytes()
    result = module.enhance_pdf(content, {'text': '', 'metadata': {'coverage': {'pages_without_text': [1]}}})
    assert result['ok'] and len(result['sections']) == 1
    assert calls == [('image/png', '扫描件页面')]
    assert '视觉识别结果' in result['merged_text']
    assert '未经人工核对' in result['merged_text']
    assert result['meta']['provider'] == 'zhipu'


def test_enhance_pdf_returns_clean_failure_without_key(monkeypatch):
    import enterprise.multimodal as multimodal
    monkeypatch.setattr(multimodal, 'provider_config',
                        lambda: ('zhipu', 'glm-4v-plus', '', 'https://fixture.invalid'))
    content = scanned_pdf_bytes()
    result = enhance_pdf(content, {'text': '', 'metadata': {'coverage': {'pages_without_text': [1]}}})
    assert result['ok'] is False and '密钥' in result['reason']


def test_enhance_pdf_no_candidate_pages_returns_clean_failure():
    result = enhance_pdf(b'%PDF-x', {'text': '有正文', 'metadata': {'coverage': {'pages_without_text': []}}})
    assert result['ok'] is False and '没有检测到' in result['reason']


def test_stage_override_replaces_text_and_keeps_original_blob(tmp_path):
    repo = Repository(tmp_path / 'managed')
    content = scanned_pdf_bytes()
    result = repo.stage(content, '扫描件.pdf', '扫描工艺卡', ['甲产品'], '2026-01-01',
                        '生产工艺', 'tester', scope_factories=['一厂'], visibility='scoped',
                        metadata={'evidence_role': 'document_basis'},
                        extracted_text_override='视觉增强稿正文')
    assert not result['errors']
    assert result['text'] == '视觉增强稿正文'
    assert result['sha256'] == hashlib.sha256(content).hexdigest()
    version = repo.commit(result['stage_id'], 'tester', '视觉增强核准')
    assert version['text'] == '视觉增强稿正文'
    assert version['parser'] == 'vision_enhanced_pypdf'
    assert version['parse_metadata']['vision_enhanced'] is True
    # 原件仍是原 PDF 字节（blob 按原文件哈希保存）
    assert version['sha256'] == hashlib.sha256(content).hexdigest()
    blob = repo.read_blob(version['sha256'], version_id=version['version_id'])
    assert blob == content


def test_stage_override_rescues_pure_image_pdf(tmp_path):
    repo = Repository(tmp_path / 'managed')
    content = scanned_pdf_bytes()
    result = repo.stage(content, '纯扫描.pdf', '扫描车间图', ['甲产品'], '2026-01-01',
                        '设备参考', 'tester', scope_factories=['一厂'], visibility='scoped',
                        metadata={'evidence_role': 'document_basis'},
                        extracted_text_override='【视觉增强解析稿】设备铭牌内容')
    assert not result['errors']
    assert result['text'].startswith('【视觉增强解析稿】')
    version = repo.commit(result['stage_id'], 'tester', '扫描件增强核准')
    assert version['text'] == result['text']
    assert version['parse_metadata']['vision_enhanced'] is True
    assert version['format'] == 'pdf'


def test_stage_rejects_invalid_override(tmp_path):
    repo = Repository(tmp_path / 'managed')
    for bad in ('', '   ', 123):
        with pytest.raises(Exception):
            repo.stage(b'x', 'a.txt', '标题', ['甲产品'], '2026-01-01', '其他', 'tester',
                       scope_factories=['一厂'], visibility='scoped', extracted_text_override=bad)


def test_vision_enhanced_chunks_need_review_for_mechanism():
    from enterprise.knowledge_applicability import applicability_reason, range_applicability
    text = '通用工艺\n公用工程消耗与设备利用率说明。'
    meta = {'scope_products': ['银黄口服液'],
            'business_metadata': {'evidence_role': 'document_basis'},
            'parse_metadata': {'vision_enhanced': True}}
    unreviewed = range_applicability(text, meta, 0, len(text))
    assert unreviewed['eligible_for_mechanism'] is False
    assert unreviewed['basis'] == 'vision_enhanced_unreviewed'
    assert applicability_reason(unreviewed, '银黄口服液') == 'vision_enhanced_unreviewed'
    reviewed = {'scope_products': ['银黄口服液'],
                'business_metadata': {'evidence_role': 'document_basis', 'vision_reviewed': True},
                'parse_metadata': {'vision_enhanced': True}}
    result = range_applicability(text, reviewed, 0, len(text))
    assert result['eligible_for_mechanism'] is True
    assert applicability_reason(result, '银黄口服液') is None


def test_specification_axis_reads_specification_field():
    from enterprise.knowledge_applicability import applicability_reason, range_applicability
    text = '银黄口服液 生产工艺\n配方正文'
    meta = {'scope_products': ['银黄口服液'], 'business_metadata': {'specification': '10ml×10支'}}
    result = range_applicability(text, meta, 0, len(text))
    assert result['specifications'] == ['10ml×10支']
    assert applicability_reason(result, '银黄口服液', '10ml×10支') is None
    assert applicability_reason(result, '银黄口服液', '其它规格') == 'specification_not_applicable'


def test_stage_without_override_unchanged(tmp_path):
    repo = Repository(tmp_path / 'managed')
    result = repo.stage('普通正文'.encode('utf-8'), '说明.txt', '说明', ['甲产品'], '2026-01-01',
                        '其他', 'tester', scope_factories=['一厂'], visibility='scoped')
    assert not result['errors'] and result['text'] == '普通正文'
