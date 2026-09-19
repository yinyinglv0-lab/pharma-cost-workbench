"""Explicit reviewed source manifest for the competition deployment.

No automatic import at module load/startup. Raw official files are preserved;
source conflicts remain visible metadata and are never silently corrected.
"""
from __future__ import annotations
import hashlib
from pathlib import Path
from paths import DATA_DIR, MANAGED_DIR
from enterprise.security import require

PRODUCTS = ['银黄口服液', '板蓝根颗粒', '六味地黄胶囊']


def official_document_specs(source_root=None):
    root = Path(source_root) if source_root else DATA_DIR
    specs = []
    def add(filename, category, products, factories, *, public=False, metadata=None):
        path = root / filename
        if not path.is_file():
            raise ValueError('缺少核准原件：' + filename)
        specs.append({'filename': filename, 'title': path.stem, 'scope_products': products,
                      'scope_factories': factories, 'visibility': 'public' if public else 'scoped',
                      'effective_from': '2025-01-01', 'category': category,
                      'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                      'metadata': {'source_dataset': '赛方模拟数据',
                                   'date_basis': '演示回溯基线；非宣称原文件实际法律/业务生效日',
                                   'evidence_role': 'document_basis', **(metadata or {})}})
    add('药品生产质量管理规范GMP.pdf', '法规原文', [], [], public=True,
        metadata={'authority': 'primary', 'applicability': '按实际用途和适用法规确认，不用成本工具替代质量决策'})
    add('GMP法规核心摘要_2010修订版.pdf', '法规摘要', [], [], public=True,
        metadata={'authority': 'summary', 'evidence_role': 'context_only',
                  'known_conflicts': ['摘要章节条号与法规原文存在偏移，禁止用摘要条号作法规原文引用']})
    for product in PRODUCTS:
        metadata = {'authority': 'competition_reference', 'not_actual_consumption': True}
        if product == '六味地黄胶囊':
            metadata.update(evidence_role='context_only', known_conflicts=[
                '每1000粒公斤表与每粒毫克表存在十倍换算差异；不用于定额、实物耗用或采购价格计算'])
        add(f'产品配方文档_{product}.pdf', '产品配方', [product], ['中药一厂'], metadata=metadata)
    add('生产工艺文档_中药一厂.pdf', '生产工艺', PRODUCTS, ['中药一厂'],
        metadata={'authority': 'competition_reference', 'claim_boundary': '工艺机制不证明本期实际发生异常'})
    add('车间设备清单_中药一厂.pdf', '设备参考', PRODUCTS, ['中药一厂'],
        metadata={'authority': 'competition_reference', 'evidence_role': 'context_only',
                  'known_conflicts': ['29为型号记录行数，数量列合计37台套',
                                      '参考月折旧与残值率假设不一致；财务金额只使用成本CSV已确认记录']})
    add('药材市场价格行情_2026年上半年.csv', '市场参考', PRODUCTS, ['中药一厂', '中药二厂'],
        metadata={'authority': 'market_reference', 'not_actual_procurement': True,
                  'unit_note': '胶囊为元/万粒，药材通常为元/kg，禁止混用'})
    add('行业成本基准数据_2026.csv', '行业基准', PRODUCTS, ['中药一厂', '中药二厂'],
        metadata={'authority': 'industry_reference', 'claim_boundary': '区间基准不等于实际可节约金额'})
    return specs


def bootstrap_knowledge(*, principal, source_root=None, root=None, publish=True, dense=True, build_timeout=600.0):
    require(principal, 'knowledge.stage')
    if 'knowledge_admin' not in principal.roles or '*' not in principal.factories or '*' not in principal.products:
        raise PermissionError('整包核准导入须由具备全部工厂/产品范围的知识管理员执行')
    from enterprise.knowledge import Repository
    from enterprise.knowledge_release import bootstrap_official
    source = Path(source_root) if source_root else DATA_DIR
    return bootstrap_official(source, repository=Repository(root or MANAGED_DIR, principal=principal),
                              principal=principal, documents=official_document_specs(source),
                              reason='依据赛方全量数据审计核准原件与范围；保留已知冲突和模拟数据边界',
                              publish=publish, embedding_model_path=None if dense else '', build_timeout=build_timeout)
