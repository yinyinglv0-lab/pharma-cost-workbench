"""Constrained, source-grounded task plans and neutral completion criteria.

The model chooses actions and requested document categories, never invents actual
plant equipment, a root cause, an employee, or a proven saving to satisfy a schema.
"""
from __future__ import annotations

from collections.abc import Mapping
import re

ACTIONS = ("核对原始记录", "按同口径复算", "逐项解释差异", "登记资料缺口", "复核整改前后效果")
DOCUMENTS = ("原始凭证", "成本归集明细", "生产记录", "费用分配底稿", "批次领退料记录", "采购结算凭证", "工时考勤记录")
DELIVERABLES = ("差异核对表", "凭证索引", "资料缺口清单", "效果对比表")
CRITERIA = ("比较范围、期间和计量单位一致", "差异可追溯至原始凭证", "无法核实的项目列明资料缺口", "结论由核查证据决定，不预设经营原因或节约额")
PLAN_FIELDS = {"objects", "actions", "documents", "deliverables", "completion_criteria"}


def default_plan(content):
    return {"objects": [content["source"]["product"]], "actions": list(ACTIONS),
            "documents": ["原始凭证", "成本归集明细", "生产记录"],
            "deliverables": list(DELIVERABLES), "completion_criteria": list(CRITERIA)}


def normalise_plan(value, content):
    from enterprise.task_workflow import TaskError, _strings
    if not isinstance(value, Mapping) or set(value) != PLAN_FIELDS:
        raise TaskError("action_plan须包含objects/actions/documents/deliverables/completion_criteria")
    result = {key: _strings(value[key], "action_plan." + key, required=True) for key in PLAN_FIELDS}
    source_text = content["source"]["product"] + "\n" + content["source"]["finding"] + "\n" + content.get("suggestion", "")
    if any(len(item) < 2 or item not in source_text for item in result["objects"]):
        raise TaskError("核查对象必须逐字来自源分析，不得虚构设备或工序")
    for key, allowed in (("actions", ACTIONS), ("documents", DOCUMENTS), ("deliverables", DELIVERABLES), ("completion_criteria", CRITERIA)):
        if not set(result[key]).issubset(allowed):
            raise TaskError(f"{key}须选用受控核查项，不能把预设根因作为验收条件")
    if not set(CRITERIA).issubset(result["completion_criteria"]):
        raise TaskError("完成判据须覆盖口径、凭证、缺口及不预设原因")
    return result


def render_plan(plan):
    return ("核查对象为" + "、".join(plan["objects"]) + "。请" + "、".join(plan["actions"]) + "。\n"
            "需要核对的凭证类别包括" + "、".join(plan["documents"]) + "，缺失资料逐项记录为待补充。\n"
            "提交" + "、".join(plan["deliverables"]) + "，完成时应做到：" + "；".join(plan["completion_criteria"]) + "。")


def validate_model_title(title, content):
    from enterprise.task_workflow import TaskError
    from enterprise.causal_guard import validate_cost_causality
    if validate_cost_causality(title):
        raise TaskError("模型任务标题违反会计因果边界")
    if re.search(r"(?:证实|确认|认定|证明|明确指出).{0,40}(?:导致|所致|根因|原因|系由)|(?:已经|必然|确定).{0,12}(?:节约|降本|改善)", title):
        raise TaskError("模型不得把待核查原因或收益预设为结论")
    # Titles use a neutral server-rendered label; this check also prevents an
    # injected unsupported equipment name from silently passing model validation.
    source_text = content["source"]["finding"] + content.get("suggestion", "") + content["source"]["product"]
    for term in re.findall(r"[\u4e00-\u9fffA-Za-z0-9_-]{1,12}(?:反应釜|干燥机|纯化系统|压片机|制粒机|设备|生产线)", title):
        if term not in source_text:
            raise TaskError("模型标题新增了源分析未提供的设备或生产线")
