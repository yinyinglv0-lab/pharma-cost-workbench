"""History renders adopted frozen prose through the shared safe helper, read-only."""
from copy import deepcopy
from pathlib import Path
import json

import pytest
from streamlit.testing.v1 import AppTest

from enterprise.security import Principal

ROOT = Path(__file__).resolve().parents[1]
ACTOR = Principal('history-reader', 'History reader', ('analyst',), ('中药一厂',), ('测试产品',))


def _entry(page_path):
    import streamlit as st
    st.navigation([st.Page(page_path, title='分析档案', default=True)]).run()


def fixture(prose_mode='bound-numeric-prose/1'):
    section = {'title': '直接材料', 'fact': '材料差额 -100.00 元。[F001]',
               'prose': '材料金额下降 -100.00 元，可能涉及结构变化，尚待凭证核对。',
               'prose_mode': prose_mode, 'numeric_explanation': '审计明细：产量影响 -60.00 元，单位成本影响 -40.00 元。[F001]',
               'model_followup_action': '建议：财务部先用已有成本表复算差额。',
               'immediate_action': '确定性行动。', 'recommendation': '完整建议。',
               'text': '历史完整审计正文。[F001]', 'evidence_ids': ['F001']}
    return {'analysis': {'sections': [section], 'sources': [{'id': 'F001', 'kind': 'accounting_fact',
                'source': {'file': 'test.csv'}, 'text': '合成历史成本记录。'}],
                'text': '老版本完整正文', 'concise_text': '老版本摘要',
                'payload': {'product': '测试产品', 'month': '2026-06'}},
            'versions': {}, 'code_hashes': {}}


def setup(monkeypatch, tmp_path, payload, *, denied=False):
    import app_pages._shared as shared
    from enterprise.application import Application
    calls = []
    class Repo:
        def __init__(self, root, principal=None):
            assert principal == ACTOR
        def list(self):
            calls.append('list')
            return [{'id': 'a' * 32, 'product': '测试产品', 'month': '2026-06',
                     'created': '2026-09-23T12:00:00', 'actor': 'original-actor'}]
        def get(self, identifier):
            calls.append(('get', identifier))
            if denied:
                raise PermissionError('当前引用权限已撤销')
            return deepcopy(payload)
        def save(self, *args, **kwargs):
            pytest.fail('Opening frozen history must never save or regenerate')
    monkeypatch.setattr(shared, 'page_context', lambda action: (ACTOR, Application(ACTOR, root=tmp_path)))
    monkeypatch.setattr('enterprise.snapshots.SnapshotRepository', Repo)
    def forbidden(*args, **kwargs):
        pytest.fail('History must not retrieve or call a model')
    monkeypatch.setattr('enterprise.model_gateway.generate_json', forbidden)
    monkeypatch.setattr('enterprise.analysis_service.report_evidence', forbidden)
    app = AppTest.from_function(_entry, args=(str(ROOT / 'app_pages/history.py'),), default_timeout=20).run()
    assert not app.exception
    return app, calls


def test_verified_adopted_snapshot_uses_same_safe_prose_renderer(monkeypatch, tmp_path):
    payload = fixture()
    app, calls = setup(monkeypatch, tmp_path, payload)
    rendered = {node.key: json.loads(node.proto.json)['html'] for node in app.get('bidi_component')}
    assert 'history_citations_reading' in rendered
    assert rendered['history_citations_reading'].count(payload['analysis']['sections'][0]['prose']) == 1
    assert 'analysis-recommendation' in rendered['history_citations_reading']
    assert 'data-evidence-id="F001"' in rendered['history_citations_reading']
    assert 'history_citations_numeric_audit' in rendered
    assert calls == ['list', ('get', 'a' * 32)]
    assert not (tmp_path / 'snapshots.db').exists()
    assert any('不是重新生成结果' in caption.value for caption in app.caption)
    assert app.get('download_button')[0].proto.label == '下载完整分析快照'


@pytest.mark.parametrize('mode', ['', 'old/1'])
def test_historical_unadopted_schema_keeps_legacy_text_unchanged(monkeypatch, tmp_path, mode):
    payload = fixture(mode)
    app, calls = setup(monkeypatch, tmp_path, payload)
    assert not app.get('bidi_component')
    assert any(item.value == '老版本摘要' for item in app.markdown)
    assert calls == ['list', ('get', 'a' * 32)]


def test_revoked_snapshot_never_renders_prose_or_download(monkeypatch, tmp_path):
    app, _ = setup(monkeypatch, tmp_path, fixture(), denied=True)
    assert not app.get('bidi_component')
    assert not app.get('download_button')
    assert any('引用权限已撤销' in item.value for item in app.error)
