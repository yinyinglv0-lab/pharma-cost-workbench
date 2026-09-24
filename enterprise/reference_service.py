"""Authorized reference views and explicit, idempotent verification-only drafts.

No model, approval, outbox enqueue, RPA client, or dispatch is called here.
A browser supplies only a scope and alert identity; source facts are re-derived.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json

from enterprise.domain_profiles import load_domain_profile
from enterprise.security import require, filter_tables


def industry_view(application, product, specification, month, *, tables=None):
    from enterprise.analysis_service import report_evidence
    from enterprise.industry_benchmark import build_industry_comparison
    principal = application.principal
    profile = load_domain_profile()
    home = profile['factories']['home']
    require(principal, 'dashboard.read', factory=home, product=product)
    require(principal, 'data.read', factory=home, product=product)
    require(principal, 'knowledge.read', factory=home, product=product)
    # A caller's previously authorized snapshot may be reused, but its scope is
    # checked again. Never expose the peer merely because it exists in the input.
    scoped = application.tables() if tables is None else filter_tables(principal, tables)
    evidence = report_evidence(principal, product, specification, [month], root=application.root,
                               purposes=['industry_reference'])
    result = build_industry_comparison(product, specification, month, scoped, evidence, profile=profile)
    return {'result': result, 'evidence': list(evidence),
            'diagnostics': deepcopy(getattr(evidence, 'diagnostics', {}))}


def save_industry_verification(application, product, specification, month, alert_id):
    """Revalidate current source state, then create one manual verification draft.

    The selected month is a *review context*, not the industry's statistical
    period. That distinction is retained in the persisted finding and action.
    Repeated requests return the same task, never revise/approve/re-send it.
    """
    from enterprise.evidence_freeze import frozen_evidence_hashes
    from enterprise.task_plan import CRITERIA
    from enterprise.task_workflow import TaskConflict
    principal = application.principal
    profile = load_domain_profile()
    home = profile['factories']['home']
    require(principal, 'task.create', factory=home, product=product)
    view = industry_view(application, product, specification, month)
    matching = [row for row in view['result'].get('alerts', []) if row['alert_id'] == alert_id]
    if len(matching) != 1:
        raise ValueError('行业预警已变化、撤销或不再适用，请重新核对当前发布')
    alert = matching[0]
    hashes = frozen_evidence_hashes(view['evidence'], alert['evidence_ids'])
    if set(hashes) != set(alert['evidence_ids']):
        raise ValueError('行业预警缺少唯一、可冻结的原始参考记录')
    identity = {'tenant': principal.tenant_id, 'factory': home, 'product': product,
                'specification': specification, 'alert_id': alert_id}
    task_id = 'TASK-IND-' + sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:32]
    finding = (f"核查上下文：{product}（{specification}），查看月{month}；"
               f"行业文件年份{alert['reference_year']}，统计窗口尚待确认，不认定为该产品当月异常。"
               + alert['finding'] + alert['boundary'])
    seed = {'task_title': alert['title'],
            'assignee': {'name': '', 'department': '财务部', 'role': '待指定'},
            'source': {'analysis_type': '专题分析', 'analysis_month': month,
                       'product': product, 'finding': finding},
            'priority': alert['priority'], 'deadline': '', 'suggestion': alert['action'],
            'factories': [home], 'evidence_ids': alert['evidence_ids'], 'evidence_hashes': hashes,
            'analysis_run_id': alert_id,
            'action_plan': {'objects': [product],
                            'actions': ['核对原始记录', '按同口径复算', '登记资料缺口'],
                            'documents': ['原始凭证', '成本归集明细'],
                            'deliverables': ['差异核对表', '凭证索引', '资料缺口清单'],
                            'completion_criteria': list(CRITERIA)}}
    repository = application.tasks()
    try:
        return repository.create(seed, actor=principal, task_id=task_id)
    except TaskConflict:
        # The unique primary key is the concurrency guard. Existing human edits
        # and independently issued states are untouched, not silently reset.
        existing = repository.get(task_id, actor=principal)
        if existing['content'].get('analysis_run_id') != alert_id:
            raise
        return existing
