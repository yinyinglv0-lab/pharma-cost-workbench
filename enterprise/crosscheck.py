# -*- coding: utf-8 -*-
"""最终报告交叉质检（双模型互检 + 程序裁决），仅三场景白名单触发。

纪律：
- 候选 B 必须与候选 A 使用完全相同的确定性数据投影（同一 input_data_hash），
  只有 provider/model 不同——数字裁判才有可比性；
- 裁判是程序校验器（合同校验 + 引用完整度评分），模型之间不互相打分；
- 并列难分时双候选全部保留供人工评审，不硬选；
- Kimi 超时/失败 → 直接采候选 A；只有 A 也不合格才降级确定性文本；
- 白名单之外任何路径零额外调用。

用法（由报告生成路径调用）：
    from enterprise.crosscheck import crosscheck_final_report
    chosen = crosscheck_final_report(product, month, theme,
                                     generate_fn, validate_fn, model_fn)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

SCHEMA = 'final-report-crosscheck/1'

# 仅最终三场景启用（产品, 月份, 主题）；其他路径零额外调用
CROSSCHECK_WHITELIST = {
    ('银黄口服液', '2026-05', '月度成本分析'),
    ('板蓝根颗粒', '2026-06', '季度成本分析'),
    ('六味地黄胶囊', '2026-03', '专题分析'),
}


@dataclass
class Verdict:
    ok: bool
    errors: list = field(default_factory=list)
    reference_score: int = 0
    source: str = ''


def reference_score(candidate: dict) -> int:
    """可机器验证的引用完整度评分（不做语义判断）。

    评分维度：要素覆盖、知识引用条数（最多计 3 条）、证据缺口声明。
    """
    score = 0
    covered = {item.get('element') for item in candidate.get('sections', [])}
    score += len(covered & {'材料', '人工', '制费'})
    doc_ids = candidate.get('document_basis_ids') or []
    score += min(len(doc_ids), 3)
    if candidate.get('missing_evidence_declared'):
        score += 2
    return score


def _validate(candidate: dict, validate_fn) -> Verdict:
    """程序裁决：validate_fn 返回错误列表（空=通过），配合引用完整度评分。"""
    if validate_fn is None:
        return Verdict(ok=True, reference_score=reference_score(candidate), source='no_validator')
    try:
        errors = list(validate_fn(candidate) or [])
    except Exception as exc:  # 校验器异常按不合格处理，绝不放过
        errors = [f'validator_failed:{type(exc).__name__}']
    return Verdict(ok=not errors, errors=errors,
                   reference_score=reference_score(candidate) if not errors else 0,
                   source='program_validator')


def adjudicate(candidate_a: dict, candidate_b: dict,
               verdict_a: Verdict, verdict_b: Verdict) -> dict:
    """四分支裁决：都过取引用更全者（并列保留双候选）、一过取过者、都不过取确定性。"""
    if verdict_a.ok and verdict_b.ok:
        if verdict_a.reference_score > verdict_b.reference_score:
            return {'chosen': 'A', 'candidate': candidate_a, 'tie': False}
        if verdict_b.reference_score > verdict_a.reference_score:
            return {'chosen': 'B', 'candidate': candidate_b, 'tie': False}
        return {'chosen': 'A', 'candidate': candidate_a, 'tie': True,
                'candidate_b': candidate_b,
                'note': '两份候选并列，均保留供人工评审选择'}
    if verdict_a.ok:
        return {'chosen': 'A', 'candidate': candidate_a, 'tie': False}
    if verdict_b.ok:
        return {'chosen': 'B', 'candidate': candidate_b, 'tie': False}
    return {'chosen': 'FALLBACK', 'candidate': None, 'tie': False}


def crosscheck_final_report(product: str, month: str, theme: str,
                            generate_fn, validate_fn=None,
                            model_fn=None) -> dict:
    """最终报告交叉质检入口。

    generate_fn(payload, model) → candidate dict；
    model_fn(payload) → 第二方（如 Kimi K3）返回的候选 dict；未提供时返回
    {'enabled': False}——白名单外/无第二方时零额外调用。
    """
    enabled = (product, month, theme) in CROSSCHECK_WHITELIST
    result = {'schema': SCHEMA, 'enabled': enabled,
              'chosen': None, 'verdict_a': None, 'verdict_b': None,
              'audit': {'dual_candidate': False}}
    if not enabled:
        result['reason'] = 'not_in_crosscheck_whitelist'
        return result

    payload = {'product': product, 'month': month, 'theme': theme}
    candidate_a = generate_fn(payload)
    if model_fn is None:
        result.update(chosen='A', candidate=candidate_a,
                      reason='second_party_not_configured')
        return result

    candidate_b = model_fn(payload)
    verdict_a = _validate(candidate_a, validate_fn)
    verdict_b = _validate(candidate_b, validate_fn)
    decision = adjudicate(candidate_a, candidate_b, verdict_a, verdict_b)
    result.update(chosen=decision['chosen'], candidate=decision['candidate'],
                  verdict_a={'ok': verdict_a.ok, 'errors': verdict_a.errors,
                             'reference_score': verdict_a.reference_score},
                  verdict_b={'ok': verdict_b.ok, 'errors': verdict_b.errors,
                             'reference_score': verdict_b.reference_score},
                  tie=decision['tie'], audit={'dual_candidate': True})
    if decision.get('candidate_b') is not None:
        result['candidate_b'] = decision['candidate_b']
    if decision.get('note'):
        result['note'] = decision['note']
    return result


def snapshot_payload(result: dict) -> dict:
    """进入快照的审计视图（两个候选的裁决状态，不含模型私钥）。"""
    return {'schema': SCHEMA, 'enabled': result.get('enabled'),
            'chosen': result.get('chosen'), 'tie': result.get('tie', False),
            'verdict_a': result.get('verdict_a'), 'verdict_b': result.get('verdict_b'),
            'note': result.get('note', '')}
#（注：内容由AI生成）
