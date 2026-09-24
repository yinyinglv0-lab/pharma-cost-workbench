# -*- coding: utf-8 -*-
"""整改任务闭环自动生成「历史成本异常处理记录」知识候选（真实数据，不伪造）。

- 触发点：review_rectification 验收通过（business_status=closed）后尽力而为地把
  闭环事实组装为案例文本，自动 stage 到知识库（类别=异常处理记录，
  evidence_role=context_only、authority=task_closure）；
- 纪律：自动生成仅到"差异预览"为止——知识管理员仍需在知识文档库人工确认并发布，
  "上传≠生效"不变；同任务同一 closure_revision 不重复暂存（幂等）；
  自动暂存失败绝不影响业务关闭结果；
- 告警口径：单位成本环比变动超过 ±10%（与 attribution_gen.MOM_ALERT_THRESHOLD 一致），
  系统只记录"当时触发告警并已闭环"的事实，不伪造异常。
"""
from __future__ import annotations

from pathlib import Path

CASE_CATEGORY = '异常处理记录'
CASE_METADATA_BOUNDARY = ('历史案例参考：记录当时闭环事实，不证明本期异常成因，'
                          '整改效果不可直接外推')
ALERT_RULE_LINE = '触发口径：单位成本环比变动超过±10%告警阈值（系统按归因告警口径自动记录）'


def case_title(task_id):
    """稳定文档标题：同一任务多轮闭环复用同一知识文档，逐轮产生新版本。"""
    return f'成本异常处理记录·{task_id}'


def build_case_text(content, submission, task_id, closure_revision, closed_utc, actor_name):
    """仅使用已校验的闭环事实组装案例文本；任何字段缺失如实留空，不补零不猜测。"""
    source = content.get('source') or {}
    period = content.get('analysis_period') or {}
    lines = ['【历史成本异常处理记录】']
    lines.append(f'任务ID：{task_id}')
    lines.append(f'产品：{source.get("product", "")}')
    lines.append(f'工厂：{"、".join(content.get("factories") or [])}')
    lines.append(f'分析月份：{source.get("analysis_month", "")}')
    lines.append(f'异常发现：{source.get("finding", "")}')
    lines.append(ALERT_RULE_LINE)
    if period:
        lines.append(f'核查期间：{period.get("label", "")}（{"、".join(period.get("months") or [])}）')
    lines.append('')
    lines.append(f'整改结果：{submission.get("summary", "")}')
    lines.append(f'效果评估：{submission.get("evaluation", "")}')
    for metric in submission.get('metrics') or []:
        lines.append(f'指标 {metric.get("name", "")}：{metric.get("before", "")} → {metric.get("after", "")} '
                     f'{metric.get("unit", "")}（前期间 {metric.get("before_period", "")} / '
                     f'后期间 {metric.get("after_period", "")}；范围 {metric.get("scope", "")}；'
                     f'方法 {metric.get("method", "")}）')
    for evidence in submission.get('evidence') or []:
        lines.append(f'凭证：{evidence.get("name", "")}（{evidence.get("reference", "")}）'
                     f'sha256={evidence.get("sha256", "")}')
    lines.append('')
    lines.append(f'关闭时间：{closed_utc}')
    lines.append(f'验收人：{actor_name}')
    lines.append(f'闭环修订：v{closure_revision}')
    lines.append('本记录由整改任务闭环自动生成，仅作历史案例参考；不得作为本期归因的机制依据，'
                 '使用前须经知识管理员确认发布。')
    return '\n'.join(lines)


def stage_closed_task(root, task_id, content, submission, closure_revision, closed_utc, actor):
    """把闭环事实暂存为异常案例知识候选（服务端内部调用，幂等）。

    返回 {'staged', 'reason', 'title', 'doc_id', 'stage_id'}；绝不抛异常。
    """
    from enterprise.knowledge import Repository
    repo = Repository(Path(root))
    title = case_title(task_id)
    # 幂等：同任务同闭环修订已有待确认暂存则跳过
    pending = repo.pending_case_stage(task_id, closure_revision)
    if pending:
        return {'staged': False, 'reason': 'already_staged', 'title': title,
                'doc_id': pending['doc_id'], 'stage_id': pending['stage_id']}
    existing = next((row for row in repo.list_documents() if row['title'] == title), None)
    doc_id = existing['doc_id'] if existing else None
    if existing:
        latest = repo.get(doc_id)
        meta = latest.get('business_metadata') or {}
        if meta.get('closure_revision') == closure_revision and meta.get('task_id') == task_id:
            return {'staged': False, 'reason': 'already_staged', 'title': title,
                    'doc_id': doc_id, 'stage_id': None}
    source = content.get('source') or {}
    product = str(source.get('product') or '').strip()
    factories = [str(item).strip() for item in (content.get('factories') or []) if str(item).strip()]
    if not product or not factories:
        return {'staged': False, 'reason': 'case_scope_incomplete', 'title': title,
                'doc_id': doc_id, 'stage_id': None}
    text = build_case_text(content, submission, task_id, closure_revision, closed_utc,
                           getattr(actor, 'user_id', str(actor)))
    metadata = {'evidence_role': 'context_only', 'authority': 'task_closure',
                'task_id': task_id, 'closure_revision': closure_revision,
                'claim_boundary': CASE_METADATA_BOUNDARY}
    try:
        result = repo.stage(text.encode('utf-8'), f'anomaly_{task_id}.txt', title,
                            [product], str(closed_utc)[:10], CASE_CATEGORY,
                            getattr(actor, 'user_id', str(actor)),
                            doc_id=doc_id, scope_factories=factories, visibility='scoped',
                            metadata=metadata)
    except Exception as exc:
        return {'staged': False, 'reason': type(exc).__name__, 'title': title,
                'doc_id': doc_id, 'stage_id': None}
    if result.get('errors'):
        return {'staged': False, 'reason': '；'.join(result['errors']), 'title': title,
                'doc_id': doc_id, 'stage_id': None}
    return {'staged': True, 'reason': 'staged_pending_confirmation', 'title': title,
            'doc_id': result['doc_id'], 'stage_id': result['stage_id']}
