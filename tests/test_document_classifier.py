# -*- coding: utf-8 -*-
"""自动归类建议测试：规则层映射、CSV schema 优先、角色纪律、模型层无密钥降级（零网络零付费调用）。"""
import pytest

from enterprise.document_classifier import (BOARD_LABELS, CATEGORIES, ROLE_KEYS,
                                           board_of, llm_classify, suggest_classification)


@pytest.mark.parametrize('filename,expected', [
    ('药品生产质量管理规范GMP.pdf', '法规原文'),
    ('GMP法规核心摘要_2010修订版.pdf', '法规摘要'),
    ('产品配方文档_银黄口服液.pdf', '产品配方'),
    ('产品配方文档_板蓝根颗粒.pdf', '产品配方'),
    ('产品配方文档_六味地黄胶囊.pdf', '产品配方'),
    ('生产工艺文档_中药一厂.pdf', '生产工艺'),
    ('测试文档_中药提取车间SOP.docx', '生产工艺'),
    ('车间设备清单_中药一厂.pdf', '设备参考'),
    ('药材市场价格行情_2026年上半年.csv', '市场参考'),
    ('行业成本基准数据_2026.csv', '行业基准'),
    ('历史成本异常处理案例库.json', '异常处理记录'),
    ('中药一厂成本分析口径说明.txt', '通用制度'),
])
def test_official_filenames_map_to_expected_categories(filename, expected):
    suggestion = suggest_classification(filename, {'format': 'txt', 'text': ''})
    assert suggestion['category'] == expected
    assert suggestion['confidence'] == 'high'
    assert suggestion['board'] in BOARD_LABELS


def test_board_mapping_covers_all_catalog_categories():
    for category in CATEGORIES:
        assert board_of(category) in BOARD_LABELS
    assert board_of('产品配方') == '产品知识'
    assert board_of('法规原文') == '行业知识'
    assert board_of('异常处理记录') == '企业内部知识'
    assert board_of('未知历史类别') == '未归类'


def test_text_keyword_fallback_after_filename_miss():
    suggestion = suggest_classification('扫描件1.pdf', {'format': 'pdf', 'text': '本文件为银黄口服液产品配方，含金银花提取物…'})
    assert suggestion['category'] == '产品配方'
    assert suggestion['confidence'] == 'medium'


def test_unknown_file_falls_back_to_low_confidence_other():
    suggestion = suggest_classification('材料.docx', {'format': 'docx', 'text': '无关键字的正文内容'})
    assert suggestion['category'] == '其他'
    assert suggestion['evidence_role'] == 'context_only'
    assert suggestion['confidence'] == 'low'


def test_csv_schema_takes_precedence_over_filename():
    parsed = {'format': 'csv', 'text': 'x',
              'metadata': {'preview_rows': [['药材名称', '规格等级', '单位', '1月价格', '2月价格',
                                             '3月价格', '4月价格', '5月价格', '6月价格', '价格来源', '趋势分析']]}}
    suggestion = suggest_classification('配方.csv', parsed)
    assert suggestion['category'] == '市场参考'
    assert suggestion['evidence_role'] == 'market_reference'
    assert suggestion['confidence'] == 'high'


def test_reference_categories_never_suggest_document_basis():
    suggestion = suggest_classification('行业成本基准数据_2026.csv', {'format': 'txt', 'text': ''})
    assert suggestion['category'] == '行业基准'
    assert suggestion['evidence_role'] == 'benchmark_reference'
    suggestion = suggest_classification('历史成本异常处理案例库.json', {'format': 'txt', 'text': ''})
    assert suggestion['evidence_role'] == 'context_only'


def test_llm_classify_returns_none_without_any_key(monkeypatch):
    import enterprise.model_settings as model_settings
    monkeypatch.setattr(model_settings, 'resolved_api_key', lambda env_name: '')
    monkeypatch.delenv('MULTIMODAL_PROVIDER', raising=False)
    assert llm_classify('产品配方文档_X.pdf', '正文') is None


def test_llm_classify_returns_none_on_provider_failure(monkeypatch):
    import enterprise.model_settings as model_settings
    monkeypatch.setattr(model_settings, 'resolved_api_key', lambda env_name: 'sk-fixture-invalid')
    monkeypatch.delenv('MULTIMODAL_PROVIDER', raising=False)
    # 无网络环境：所有供应商调用失败 → 返回 None 而不是抛异常
    assert llm_classify('任意文档.pdf', '正文', timeout=1.0) is None


def test_role_keys_cover_category_defaults():
    from enterprise.document_classifier import CATEGORY_ROLE_DEFAULTS
    for category, role in CATEGORY_ROLE_DEFAULTS.items():
        assert category in CATEGORIES and role in ROLE_KEYS
