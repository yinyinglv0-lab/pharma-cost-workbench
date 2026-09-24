"""Deterministic accounting constraints for every free-text model field.

This is a conservative publication boundary, not a general truth classifier.
Volume/amount/unit identities are rendered by Python. A model cannot override
those identities by adding '可能', a valid citation, or a recommendation.
"""
from __future__ import annotations

import re
import unicodedata

VERSION = 'cost-causality/2.1'
_REDUCED = re.compile(r'减产|(?:产量|产出量|生产规模|产出)(?:的)?(?:减少|下降|降低|缩减|下滑|减小)')
_INCREASED = re.compile(r'增产|(?:产量|产出量|生产规模|产出)(?:的)?(?:增加|上升|增长|提高|扩大)')
_FIXED = re.compile(r'固定.{0,12}(?:人工|工资|费用|成本|支出|折旧|分摊)|(?:折旧|固定人工)')
_UNIT = re.compile(r'单位.{0,10}(?:成本|费用|人工|工资|分摊)|(?:每盒|单盒).{0,10}(?:成本|费用|人工|分摊)')
_LOWER = re.compile(r'摊薄|摊低|(?:分摊|分担|成本|费用|人工|负担).{0,6}(?:降低|减少|下降|减轻)|(?:降低|减少).{0,6}(?:分摊|成本|费用)')
_HIGHER = re.compile(r'摊高|(?:分摊|分担|成本|费用|人工|负担).{0,6}(?:提高|增加|上升|加重)')
_CAUSE = re.compile(r'导致|造成|带来|使(?:得|其)?|源于|归因|由于|因为|被动|受.{0,12}影响|因此|从而|摊薄|摊低|摊高')
_NEGATION = re.compile(r'不能|不可(?:以)?|不应(?:当)?|不得|不代表|不等于|不意味着|不支持|无法|不能仅|不足以|尚不能')
_UNSUPPORTED_ACTUAL = re.compile(r'(?:实际采购(?:单)?价|实际单耗|实物(?:单耗|耗用)|本期(?:实际)?收率).{0,8}(?:已经|已确认|证实|确定|必然)')
_DIRECTION_ERROR = '不能将产量减少解释为固定成本摊薄；须区分金额的产量影响与单位成本变化'


def _assertive_segments(text):
    # Split contrast clauses as well: a disclaimer in one clause must not license
    # a subsequent assertion ('不能确认原因，但减产会降低固定分摊').
    for sentence in re.split(r'[。！？;；\n]+', unicodedata.normalize('NFKC', str(text))):
        for segment in re.split(r'但是|然而|不过|但(?!位)', sentence):
            if segment.strip():
                yield segment.strip()


def _denies_relation(segment, relation_start):
    # A scoped denial must precede and govern the relation, not merely occur
    # somewhere else ('减产使分摊下降，不能确认采购价' is still invalid).
    prefix = segment[:relation_start]
    if _NEGATION.search(prefix):
        tail = re.split(r'[,，]', prefix)[-1]
        if _NEGATION.search(tail):
            return True
    if re.search(r'(?:' + _REDUCED.pattern + r').{0,4}(?:不能|不可|不应|不代表|不等于|不足以)', segment):
        return True
    if re.search(r'(?:这一|该|上述)?(?:解释|说法|归因|机制)(?:并)?(?:不成立|错误|不正确|违反)', segment[relation_start:]):
        return True
    return False


def validate_cost_causality(text, facts=None):
    """Return stable diagnostics, checking hypotheses AND recommendations.

    facts is optional normalized monthly/period accounting data. Directional
    impossibilities do not need live data: at fixed spend, less output cannot
    lower allocation per unit. Source citations never override this constraint.
    """
    errors = []
    for segment in _assertive_segments(text):
        # Current accounting detail contains currency-per-box, not physical
        # material quantities. Reject an explicit claim that those observations
        # already establish physical usage; requests to obtain records are valid.
        for clause in re.split(r'[,，]', segment):
            quantity = re.search(r'(?:单位耗用|实物单耗|实物耗用量|实际单耗)(?!成本|费用|金额)', clause)
            if quantity:
                prefix = clause[:quantity.start()]
                observed = re.search(r'已有(?:明细|数据|记录|核算)|现有(?:明细|数据)|已(?:提供|定位|知|确认|计算)', prefix)
                pending = re.search(r'尚未|未提供|缺少|缺乏|待核|需(?:要)?核|核对|核查|补齐|不能|不代表|不足以', prefix)
                if observed and not pending:
                    errors.append('成本明细仅提供单位消耗成本，不能称已知或已定位实物耗用；应另行核对实物记录')
        reduced = _REDUCED.search(segment)
        increased = _INCREASED.search(segment)
        if reduced and _CAUSE.search(segment) and _LOWER.search(segment):
            if (_FIXED.search(segment) or _UNIT.search(segment) or '摊薄' in segment or '摊低' in segment):
                if not _denies_relation(segment, reduced.start()):
                    errors.append(_DIRECTION_ERROR)
        if increased and _FIXED.search(segment) and _CAUSE.search(segment) and _HIGHER.search(segment):
            # Only reject an explicitly fixed total/unchanged-spend premise.
            if re.search(r'(?:总额|总费用|总成本|支出).{0,5}(?:不变|固定|恒定)', segment):
                if not _denies_relation(segment, increased.start()):
                    errors.append('固定支出总额不变时，增产不能解释为单位固定分摊上升')
        if _UNSUPPORTED_ACTUAL.search(segment):
            if not _NEGATION.search(segment):
                errors.append('缺少实际业务凭证，不能确认采购实价、实物耗用或实际收率')
    if facts and isinstance(facts, dict):
        elements = facts.get('elements', {})
        labor_element = elements.get('人工', {}) if isinstance(elements, dict) else next(
            (row for row in elements if isinstance(row, dict) and row.get('element') == '人工'), {}) if isinstance(elements, list) else {}
        labor = facts.get('labor_factors') or labor_element.get('labor_factors')
        if isinstance(labor, dict) and labor.get('available'):
            try:
                efficiency_declines = float(labor.get('output_per_hour_change_pct')) < 0
            except (ValueError, TypeError):
                efficiency_declines = False
            for segment in _assertive_segments(text):
                if efficiency_declines:
                    match = re.search(r'(?:总体|整体|劳动|生产|人工)?效率(?:提高|提升|改善)', segment)
                    if match and not _denies_relation(segment, match.start()):
                        errors.append('汇总产出每工时下降，不能将人工单位成本下降归因为总体效率提升')
                missing_hours = re.search(r'(?:未提供|缺少|缺乏|没有)(?:两期|本期|本月|实际|总)?工时(?:数据|总量|汇总)', segment)
                if missing_hours and not re.search(r'并非|不是|并不', segment[:missing_hours.start()]):
                    errors.append('已提供两期人工工时汇总，不能将其描述为工时数据缺失；原始考勤等凭证仍可要求核查')
    return list(dict.fromkeys(errors))
