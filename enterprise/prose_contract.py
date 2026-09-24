"""Opt-in, source-bound numeric prose without a second calculation engine.

The server supplies complete immutable clauses from the shared deterministic
narrative.  Model output may *copy* those clauses; a bag of allowed numbers is
never an authority to move a value between subjects, units, periods or metrics.
This module performs no retrieval, model calls, configuration reads or writes.

The candidate/audit text is never repaired. ``prose_residual`` is only a temporary
validator view, to be used after numeric diagnostics alongside the existing
causal, evidence, action-availability and knowledge-mechanism checks.
"""
from __future__ import annotations

from copy import deepcopy
import json
import re
import unicodedata

from enterprise.analysis_narrative import (
    build_attribution_narrative, build_benchmark_narrative,
)

PROSE_MODE = SCHEMA_VERSION = "bound-numeric-prose/1"
PROVIDER_SCHEMA_VERSION = "bound-numeric-prose-provider/1"
NARRATIVE_CONTRACT_VERSION = "contextual-narrative/1.4"
MAX_STATEMENTS = 8
MAX_STATEMENT_CHARS = 1800
MAX_ELEMENT_CHARS = 6000
MAX_HYPOTHESIS_CHARS = 4000
COMPARISON_BOUNDARY = ("本项差异为同产量标准化会计对比，不表示已实现节约或效率优势；"
                       "单位成本高低不能单独证明成本管控优劣。")
_ELEMENTS = ("材料", "人工", "制费")
_ALIASES = {"材料": "材料", "material": "材料", "materials": "材料",
            "人工": "人工", "labor": "人工", "labour": "人工",
            "制费": "制费", "overhead": "制费", "manufacturing": "制费"}
_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_MARKER = re.compile(r"\[([A-Za-z][A-Za-z0-9_-]{0,63})\]")
_NUMERALS = "零〇○一二三四五六七八九十百千万亿兆两兩俩仨壹贰貳叁參肆伍陆陸柒捌玖拾佰仟萬億"
_QUANTITY = (r"百分点|百分比|百分数|百分位|千分位|万分位|毫克|千克|公斤|毫升|"
             r"小时|分钟|季度|个月|人民币|美元|万元|亿元|元|盒|支|粒|片|枚|件|套|"
             r"吨|克|升|米|人|天|次|项|种|条|个|份|批|台|成|倍|折|年|月|日|周|季|"
             r"秒|度|千瓦时|万|亿|兆|kg|mg|g|ml|mL|L|CNY|USD|%|％")
_NUMERIC = re.compile(
    r"[A-Za-z][A-Za-z0-9_./-]*[0-9０-９][A-Za-z0-9_./-]*|"
    r"[+＋\-−﹣－±∓]?(?:\d+(?:[,.．，]\d+)*)(?:[eE][+＋\-−－]?\d+)?(?:[%％‰‱])?|"
    r"(?:百分之|千分之|万分之)[" + _NUMERALS + r"\d点．.]+|"
    r"[" + _NUMERALS + r"]+(?:点[" + _NUMERALS + r"]+)?(?:个)?(?:" + _QUANTITY + r")|"
    r"[" + _NUMERALS + r"]+点[" + _NUMERALS + r"]+|"
    r"[" + _NUMERALS + r"]+分之[" + _NUMERALS + r"]+|"
    r"[" + _NUMERALS + r"]+(?:加|减|乘以?|除以?|等于)[" + _NUMERALS + r"]+|"
    r"[第前后][" + _NUMERALS + r"]+(?:期|步|名|阶段|季度|月|年|批)|"
    r"[" + _NUMERALS + r"]{2,}|"
    r"(?:为|是|达|至|占|共|合计|约|超过|不足|不低于|不高于|增加|减少|增长|下降|提高|降低|上升|相差|正|负)[" + _NUMERALS + r"]+|"
    r"半(?:个)?(?:" + _QUANTITY + r")(?!品)|"
    r"一半|半数|半成(?!品)|半倍|半个|减半|折半|对半|过半|翻番|翻倍|倍增|加倍|"
    r"[+＋\-−﹣－±∓×✕÷=＝<>≤≥≦≧%％‰‱]"
)
_SENTENCE_END = frozenset("。！？!?")
_CLOSING_WRAPPER = frozenset('”’"\'）)】]}〉》')


def is_prose_mode(payload_or_context):
    """Only an exact top-level server flag opts in; nested prose is not a flag."""
    return isinstance(payload_or_context, dict) and payload_or_context.get("prose_mode") == PROSE_MODE


def model_stage_budget(payload, *, config=None):
    """Pure budget policy: only a resolved server registry configuration widens it.

    No environment/configuration reads here. Business fields cannot supply model,
    timeout, identity or budget. Existing unselected prose/legacy limits remain.
    """
    from enterprise.model_gateway import ModelConfiguration, _timeout
    from enterprise.model_registry import REGISTRY_CONTRACT
    if (isinstance(config, ModelConfiguration) and config.registry_id != 'legacy'
            and config.budget_contract == REGISTRY_CONTRACT):
        timeout = _timeout(config.configured_timeout if config.configured_timeout is not None else config.timeout)
        return min(255, 2 * timeout + 15)
    return 95 if is_prose_mode(payload) else 45


def _mode(mode):
    if mode not in ("attribution", "benchmark"):
        raise ValueError("prose mode must be attribution or benchmark")
    return mode


def _ids(values):
    if not isinstance(values, (list, tuple)):
        return []
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and _ID.fullmatch(value)))


def _diagnostic(rule_id, field, offending, expected, message):
    return {"rule_id": rule_id, "field": field, "offending": deepcopy(offending),
            "expected": deepcopy(expected), "message": message}


def _numeric_tokens(text, known_ids=()):
    """Find quantities, coded IDs and Unicode numerics without normalizing prose."""
    found = [(match.start(), match.group()) for match in _NUMERIC.finditer(text)]
    # Python's \d covers Unicode decimal digits, but not circled numbers,
    # superscripts, Roman numeral glyphs or vulgar fractions. Chinese ordinary
    # words (一致/一厂/双方) are handled contextually by the quantity grammar above.
    for index, char in enumerate(text):
        if char.isnumeric() and char not in _NUMERALS and not char.isdecimal():
            found.append((index, char))
        if unicodedata.category(char) in ("Cf", "Cc") and char not in "\t\n\r":
            found.append((index, char))
    for ident in _ids(known_ids):
        for match in re.finditer(r"(?<![A-Za-z0-9_-])" + re.escape(ident) + r"(?![A-Za-z0-9_-])", text):
            found.append((match.start(), match.group()))
    return list(dict.fromkeys(value for _, value in sorted(found)))[:24]


def _builder_options(payload):
    """Validate an explicitly supplied profile; never load an ambient profile."""
    profile = payload.get("domain_config")
    if profile is None:
        return {}
    from enterprise.domain_profiles import validate_domain_profile
    profile = validate_domain_profile(profile)
    facts = payload.get("facts") or {}
    product = payload.get("product", facts.get("product"))
    specification = payload.get("specification", facts.get("specification"))
    definition = next((row for row in profile["products"]
                       if row["name"] == product and row["specification"] == specification), None)
    if definition is None:
        raise ValueError("分析产品规格未在受控领域配置中登记")
    return {"amount_unit": profile["currency"],
            "reporting_unit": definition.get("reporting_unit", profile["reporting_unit"]),
            "config": {"reference_profile": profile}}


def _clean_statement(raw, allowed_refs, fallback_refs=()):
    if not isinstance(raw, str):
        return None
    allowed = set(_ids(list(allowed_refs)))
    marked = _MARKER.findall(raw)
    if any(ident not in allowed for ident in marked):
        return None
    refs = _ids(marked or list(fallback_refs))
    if not refs or any(ident not in allowed for ident in refs):
        return None
    # Only markers issued by the authoritative builder are stripped. Numeric
    # substrings, signs, spaces inside prose and original knowledge excerpts stay
    # byte-for-byte as returned by that builder.
    text = _MARKER.sub("", raw).strip()
    text = re.sub(r"(?<=[。；]) +(?=[。；]|$)", "", text)
    if not text or len(text) > MAX_STATEMENT_CHARS or text[-1] not in _SENTENCE_END:
        return None
    return {"text": text, "evidence_ids": refs}


def _reason_groups(text):
    """Keep dependent offset/limitation tails with their owning complete clause."""
    if not isinstance(text, str) or not text:
        return []
    chunks = re.findall(r"[^。]+。(?:\s*\[[A-Za-z][A-Za-z0-9_-]{0,63}\])*", text)
    groups = []
    dependent = ("两项", "本项与", "本项反向", "量与单位成本", "该", "这是", "尚不能", "不能", "条件核查线索")
    for chunk in chunks:
        if groups and chunk.lstrip().startswith(dependent):
            groups[-1] += chunk
        else:
            groups.append(chunk)
    return groups


def _comparison_element(row, source_index):
    explicit = row.get("element")
    if explicit is not None:
        return _ALIASES.get(explicit, explicit)
    memberships = set()
    for ident in row.get("evidence_ids", []):
        memberships.update(_ALIASES.get(item, item) for item in source_index.get(ident, {}).get("elements", []))
    return next(iter(memberships)) if len(memberships) == 1 else None


def _primary_is_missing(payload, element, mode):
    facts = payload.get("facts", payload)
    rows = facts.get("elements") or {}
    if isinstance(rows, list):
        row = next((value for value in rows if isinstance(value, dict) and value.get("element") == element), {})
    else:
        row = rows.get(element, {})
    if not isinstance(row, dict) or row.get("available") is False:
        return True
    fields = (("unit_before_exact", "unit_before"), ("unit_after_exact", "unit_after")) if mode == "attribution" else (
        ("home_unit_cost_exact", "home_unit_cost"), ("peer_unit_cost_exact", "peer_unit_cost"), ("unit_gap_exact", "unit_gap"))
    return any(not any(row.get(name) is not None for name in alternatives) for alternatives in fields)


def build_prose_contract(payload, sources, mode):
    """Project bounded complete statements from the shared trusted narrative.

    The builder itself is explicit and pure. ``extend_context`` applies opt-in
    gating. No payload-provided prose contract, prompts, fewshots, cached model
    sentences or global number set is accepted as numeric authority.
    """
    mode = _mode(mode)
    if not isinstance(payload, dict):
        raise TypeError("payload must be a mapping")
    if sources is not None and not isinstance(sources, (list, tuple)):
        raise TypeError("sources must be a list or tuple")
    options = _builder_options(payload)
    if mode == "attribution":
        narrative = build_attribution_narrative(payload, None, sources, **options)
    else:
        narrative = build_benchmark_narrative(payload, sources, None, **options)
    source_index = {}
    for source in sources if sources is not None else narrative.get("sources", []):
        if isinstance(source, dict) and isinstance(source.get("id"), str):
            source_index[source["id"]] = source
    facts = payload.get("facts", payload)
    scope = {key: value for key, value in {
        "product": payload.get("product", facts.get("product")),
        "specification": payload.get("specification", facts.get("specification")),
        "period": payload.get("period", payload.get("current_period", payload.get("month", facts.get("period", facts.get("month"))))),
    }.items() if isinstance(value, str)}
    elements = {}
    for section in narrative["sections"]:
        element = section["element"]
        allowed_refs = _ids(section.get("evidence_ids", []))
        primary_refs = _ids(_MARKER.findall(section.get("fact", "")))
        required = section.get("claim_type") != "no_difference" and not _primary_is_missing(payload, element, mode)
        primary = _clean_statement(section.get("fact"), allowed_refs, primary_refs)
        if primary is None and required:
            raise ValueError("required numeric fact has no bounded complete statement: " + element)
        selected, pools = [], {}

        def add(raw, kind, *, required=False, fallback=(), extra_refs=(), priority=50):
            clean = _clean_statement(raw, [*allowed_refs, *extra_refs], fallback)
            if clean is None or (not required and kind != "document_quote" and not _numeric_tokens(clean["text"])):
                return
            candidate = {**clean, "kind": kind, "required": required}
            if required:
                selected.append(candidate)
            else:
                pools.setdefault((priority, kind), []).append(candidate)

        if primary:
            # 两种模式的主事实句均由结构区渲染，模型不负责复述数值
            item = {**primary, "kind": "primary_fact", "required": False,
                    "presentation": "structure"}
            selected.append(item)
        reason_text_for_contract = section.get("observed_reason", "")
        for focus in section.get("focus_statements", []):
            reason_text_for_contract = reason_text_for_contract.replace(focus["text"], "")
        reason_groups = _reason_groups(reason_text_for_contract)
        for index, group in enumerate(reason_groups):
            add(group, "comparison_reason" if mode == "benchmark" and index == 0 else "observed_reason",
                required=required and index == 0 and mode != "attribution",
                fallback=primary_refs, priority=10)
        for focus in section.get("focus_statements", []):
            add(focus["text"], focus["kind"], required=required and focus["required"] and mode != "attribution",
                priority=15)
        if mode == "benchmark" and required:
            add(COMPARISON_BOUNDARY, "comparison_boundary", required=True, fallback=primary_refs)
        fact_text, reason_text = section.get("fact", ""), section.get("observed_reason", "")
        market_ids = _ids([row.get("id") for row in section.get("market_references", [])])
        for line in section.get("numeric_explanation", "").splitlines():
            if line == fact_text or line == reason_text or not line.strip():
                continue
            if "同期市场参考价由" in line:
                refs = _MARKER.findall(line)
                if refs and set(refs) <= set(market_ids):
                    add(line, "market_reference", priority=25)
            elif "实际量价的核算原因" in line:
                add(line, "observed_quantity_price_bridge", priority=20)
            elif "源记录实际提供实物量" in line or "按源记录实物量" in line:
                add(line, "observed_physical_fact", priority=45)
            elif "小时归集费用不是个人工资" in line:
                labor = _clean_statement(line, allowed_refs)
                admitted_fact_ids = {item["id"] for item in narrative.get("sources", [])
                                     if item.get("kind") in {"accounting_fact", "data_fact", "observed_fact"}}
                labor_required = bool(required and mode != "attribution" and labor
                                      and set(labor["evidence_ids"]) <= admitted_fact_ids
                                      and len(selected) < MAX_STATEMENTS
                                      and sum(len(item["text"]) for item in selected) + len(labor["text"]) <= MAX_ELEMENT_CHARS)
                add(line, "observed_labor", required=labor_required, priority=20)
            else:
                add(line, "observed_detail", priority=60)
        if section.get("mechanism_evidence"):
            add(section.get("mechanism_note"), "document_quote", priority=35)
        for row in narrative.get("industry_comparisons", []):
            if _comparison_element(row, source_index) == _ALIASES.get(element, element):
                add(row.get("text"), "industry_reference", extra_refs=row.get("evidence_ids", []), priority=30)
        for row in narrative.get("yield_comparisons", []):
            if _comparison_element(row, source_index) == _ALIASES.get(element, element):
                add(row.get("text"), "measured_yield_comparison", extra_refs=row.get("evidence_ids", []), priority=40)
        # Rank only already-vetted complete market clauses, using the same
        # structured accounting focus as the authoritative narrative. A source
        # arriving first must not crowd the actual focus material out of the
        # bounded market window. Match the exact rendered subject, not caveat
        # words or untrusted source notes. Benchmark focus semantics are separate.
        if mode == "attribution":
            from enterprise.analysis_narrative import _detail_focus
            fact_elements = facts.get("elements") or {}
            fact_row = fact_elements.get(element, {}) if isinstance(fact_elements, dict) else {}
            details = [row for row in fact_row.get("detail", []) if isinstance(row, dict)]
            focus_rows, _ = _detail_focus(details)
            focus_names = {row["name"] for row in focus_rows if isinstance(row.get("name"), str) and row["name"]}
            periods = [scope.get("period"), *(payload.get("months") or [])]

            def market_focus_rank(item):
                subject = item["text"].split("同期市场参考价", 1)[0]
                for period in periods:
                    if isinstance(period, str) and subject.startswith(period + " "):
                        subject = subject[len(period) + 1:]
                        break
                return subject not in focus_names

            if focus_names:
                for (_, kind), rows in pools.items():
                    if kind == "market_reference":
                        rows.sort(key=market_focus_rank)
        # One from each role first: many detail rows must not crowd out the
        # source-sensitive market/standard boundaries. No clause is truncated.
        queues = [rows for _, rows in sorted(pools.items())]
        while queues and len(selected) < MAX_STATEMENTS:
            remaining = []
            for rows in queues:
                if len(selected) >= MAX_STATEMENTS:
                    break
                candidate, *rest = rows
                if candidate["text"] not in {row["text"] for row in selected} and (
                        sum(len(row["text"]) for row in selected) + len(candidate["text"]) <= MAX_ELEMENT_CHARS):
                    selected.append(candidate)
                if rest:
                    remaining.append(rest)
            queues = remaining
        statements = [{"id": f"{mode}:{element}:{index}", **item} for index, item in enumerate(selected, 1)]
        elements[element] = {"statements": statements, "scope": deepcopy(scope),
                             "no_difference": section.get("claim_type") == "no_difference",
                             "primary_missing": _primary_is_missing(payload, element, mode),
                             "mechanism_evidence": deepcopy(section.get("mechanism_evidence")),
                             "applicable_document_ids": list(section.get("applicable_document_ids", [])),
                             "comparison_label": section.get("comparison_label", "同期跨主体比较"),
                             "labor_ratio_only": _ALIASES.get(element, element) == "人工",
                             "primary_fact_presentation": "structure"}
    # Missing data is not an invitation to invent a primary numeric observation.
    for element in _ELEMENTS:
        elements.setdefault(element, {"statements": [], "scope": deepcopy(scope),
                                      "no_difference": False, "primary_missing": True})
    return {"schema_version": SCHEMA_VERSION, "narrative_contract_version": NARRATIVE_CONTRACT_VERSION,
            "mode": mode, "elements": elements,
            "boundary": {
                "numeric_authority": "仅限本要素statements中完整原句，不能按数字集合重组，不计算、不舍入、不换单位或期间",
                "numeric_fields": ["hypothesis"],
                "required_core": "非零且可比时hypothesis必须逐字包含所有required原句；零基期变化率或零净额贡献度保持无定义",
                "causality": "引用只是核算观察或参考，不证明业务原因；现有知识、因果和动作凭证守卫仍须通过",
                "references": "每个实际使用原句的全部evidence_ids必须出现在本要素引用数组；参考资料不能替代机制知识",
                "source_quotes": "技术标准只允许完整引用含边界的知识原文，不能转为本期实际值",
                "contribution": "保留正负号及反向抵消；不强求舍入后贡献度合计为百分之百，零净额不生成贡献度",
                "length": "每节百余字仅为软目标，必要事实及边界优先，不截断、不省略必需句",
                "known_evidence_ids": _ids([ident for row in elements.values() for item in row["statements"]
                                            for ident in item["evidence_ids"]]),
            }}


def _element_contract(element, contract):
    if not isinstance(contract, dict) or contract.get("schema_version") != SCHEMA_VERSION:
        return None
    elements = contract.get("elements")
    row = elements.get(element) if isinstance(elements, dict) else None
    if not isinstance(row, dict) or not isinstance(row.get("statements"), list):
        return None
    return row


def _standalone(text, start, end):
    prefix = text[:start].rstrip()
    if prefix and prefix[-1] not in _SENTENCE_END:
        return False
    suffix = text[end:].lstrip()
    if suffix and suffix[0] in _CLOSING_WRAPPER:
        return False
    return True


def _matches(text, element, contract):
    """Return only exact, standalone, unique, nonoverlapping statement spans."""
    row = _element_contract(element, contract)
    if not isinstance(text, str) or row is None:
        return [], []
    found, issues = [], []
    for statement in row["statements"]:
        if not isinstance(statement, dict) or not isinstance(statement.get("text"), str) or not statement["text"]:
            continue
        raw = list(re.finditer(re.escape(statement["text"]), text))
        if len(raw) > 1:
            issues.append(("BOUND_STATEMENT_DUPLICATE", statement))
            continue
        if not raw:
            continue
        match = raw[0]
        if not _standalone(text, match.start(), match.end()):
            issues.append(("BOUND_STATEMENT_CONTEXT", statement))
            continue
        found.append((match.start(), match.end(), statement))
    found.sort(key=lambda item: (item[0], -item[1]))
    overlapping = set()
    for index, left in enumerate(found):
        for later in range(index + 1, len(found)):
            right = found[later]
            if right[0] >= left[1]:
                break
            overlapping.update((index, later))
    for index in sorted(overlapping):
        issues.append(("BOUND_STATEMENT_OVERLAP", found[index][2]))
    return [item for index, item in enumerate(found) if index not in overlapping], issues


def prose_residual(text, element, contract):
    """Validation-only view; never overwrite the original candidate or audit.

    Evidence inclusion is checked by ``numeric_prose_diagnostics`` because this
    helper intentionally has no candidate-reference argument. Invalid boundaries,
    duplicate text and partial/altered copies are never removed.
    """
    if not isinstance(text, str):
        return text
    matches, _ = _matches(text, element, contract)
    cursor, pieces = 0, []
    for start, end, _ in matches:
        pieces.extend((text[cursor:start], "\n"))
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def used_statement_ids(text, element, contract):
    return [statement["id"] for _, _, statement in _matches(text, element, contract)[0]]


def used_statement_evidence_ids(text, element, contract):
    """Only references attached to actually used, valid complete statements."""
    return _ids([ident for _, _, statement in _matches(text, element, contract)[0]
                 for ident in statement.get("evidence_ids", [])])


def numeric_prose_diagnostics(text, element, contract, evidence_ids, *, field):
    """Check source-bound verbatim copying; do not calculate or repair anything."""
    hypothesis = str(field).rsplit(".", 1)[-1] == "hypothesis"
    maximum = MAX_HYPOTHESIS_CHARS if hypothesis else 500
    if not isinstance(text, str):
        return [_diagnostic("BOUND_PROSE_TYPE", field, text, "自然句文本", "散文字段必须是字符串，不得改变既有模型字段结构")]
    errors = []
    if len(text) > maximum:
        errors.append(_diagnostic("BOUND_PROSE_LENGTH", field, len(text), maximum,
                                  "散文超过安全长度上限；保留必需完整句，减少可选事实，不截断句子"))
    row = _element_contract(element, contract)
    if row is None:
        return [*errors, _diagnostic("BOUND_CONTRACT", field, element, SCHEMA_VERSION,
                                    "缺少本要素服务器散文合同，不能授权任何数值")]
    matches, issues = _matches(text, element, contract) if hypothesis else ([], [])
    for rule, statement in issues:
        message = ("同一授权句不得重复使用" if rule == "BOUND_STATEMENT_DUPLICATE" else
                   "授权句必须作为独立完整句，不得加否定前缀、另一指标标签或引用外壳" if rule == "BOUND_STATEMENT_CONTEXT" else
                   "授权句范围重叠，不能用于扩大数值许可")
        errors.append(_diagnostic(rule, field, statement["text"], statement["text"], message))
    found_ids = {statement.get("id") for _, _, statement in matches}
    if hypothesis:
        for statement in row["statements"]:
            if statement.get("required") and statement.get("id") not in found_ids:
                errors.append(_diagnostic("BOUND_PRIMARY_REQUIRED", field, text, statement["text"],
                                          "先逐字保留本要素必需核算现象句，再另写限定性机制解释"))
    refs = set(_ids(evidence_ids))
    for _, _, statement in matches:
        missing = [ident for ident in statement.get("evidence_ids", []) if ident not in refs]
        if missing:
            errors.append(_diagnostic("BOUND_STATEMENT_EVIDENCE", field, missing,
                                      statement.get("evidence_ids", []),
                                      "完整数值句必须同时引用其全部原始证据，不能仅保留当前侧或参考侧来源"))
    residual = prose_residual(text, element, contract) if hypothesis else text
    if hypothesis:
        # Copying a true statement does not license an adjoining sentence to
        # change its subject, period, unit or truth value. These explicit denial
        # and rebinding forms fail closed; passing still requires human review.
        for sentence in re.split(r'(?<=[。！？；;])|\n', residual):
            referred = re.search(r'上述|前述|以上|这些|该(?:项)?(?:数值|数字|金额|成本|观察|现象)|本项(?:数值|金额|核算)|所列|刚才', sentence)
            rebound = re.search(r'币种|计量单位|所属期间|归属期间|对应期间|描述的是|所指(?:的是)?|所属(?:主体|工厂)|对标方而非|本方而非', sentence)
            correction = re.search(r'实际(?:上)?(?:应)?为|实为|应为|实属|改为|而非|并非|不是|实际上是|其实是|描述的是', sentence)
            denial = re.search(r'并?不成立|不属实|并非事实|不是事实|只是假设|仅(?:为|是)假设|不是真实|不作数|应予否定', sentence)
            if referred and ((rebound and correction) or denial):
                errors.append(_diagnostic('BOUND_OBSERVATION_REBINDING', field, sentence,
                    '完整核算原句的主体、单位、期间与已核实观察不得被前后文改写；另写待核实业务机制',
                    '补充句重新解释或否定了服务器绑定的核算观察'))
    if hypothesis and contract.get("narrative_contract_version") == NARRATIVE_CONTRACT_VERSION:
        if row.get("comparison_label") != "环比" and "环比" in residual:
            errors.append(_diagnostic("BOUND_COMPARISON_PERIOD", field, "环比", row.get("comparison_label"),
                                      "当前不是明确相邻可比月或日历季度，不能将本次差异称为环比"))
        if row.get("labor_ratio_only"):
            for clause in re.split(r'[。！？；;，,\n]', residual):
                if (re.search(r'效率.{0,6}(?:提高|提升|改善|下降|降低|走低|恶化)', clause)
                        and not re.search(r'不能|未能|不代表|不等于|尚未|未证实|待核|是否|不推定', clause)):
                    errors.append(_diagnostic("BOUND_LABOR_EFFICIENCY", field, clause,
                        "工时及产量比率是核算观察，不证明岗位效率升降", "不能从汇总工时产出比率推定效率变化"))
    known_ids = (contract.get("boundary") or {}).get("known_evidence_ids", [])
    tokens = _numeric_tokens(residual, known_ids)
    if tokens:
        errors.append(_diagnostic("BOUND_NUMERIC_OUTSIDE_STATEMENT", field, tokens,
            [statement["text"] for statement in row["statements"]] if hypothesis else "建议与缺口只写无数值自然句，序号由渲染器提供",
            "数值、数量词、运算符和证据ID只可出现在本要素获准的完整假设事实句内；不能换指标、期间、方向、单位或自行运算"))
    return errors


def extend_context(base, payload, sources, mode):
    """Copy the existing bounded context; numeric maps and legacy views survive."""
    if not isinstance(base, dict):
        raise TypeError("base model context must be a mapping")
    result = deepcopy(base)
    if not is_prose_mode(payload):
        return result
    contract = build_prose_contract(payload, sources, mode)
    result["prose_mode"] = PROSE_MODE
    result["prose_contract"] = contract
    # Retain all observed values, but replace the superseded output prohibition.
    result.setdefault('numeric_contract', {})['output'] = 'copy_only_complete_bound_statements'
    tasks = result.setdefault("tasks_by_element", {})
    if not isinstance(tasks, dict):
        raise TypeError("tasks_by_element must be a mapping")
    for element, row in contract["elements"].items():
        task = tasks.setdefault(element, {})
        if not isinstance(task, dict):
            raise TypeError("element task must be a mapping")
        task["bound_numeric_statements"] = deepcopy(row["statements"])
        # Mechanism permission follows the same focus-aware selector as fallback.
        # Nonfocus documents remain in the caller's source catalog, not a K quota.
        quote = row.get("mechanism_evidence")
        documents = task.get("document_basis")
        if isinstance(documents, list):
            relevant_ids = set(row.get("applicable_document_ids", []))
            excluded = [deepcopy(item) for item in documents if item.get("id") not in relevant_ids]
            task["document_basis"] = [item for item in documents if item.get("id") in relevant_ids]
            if excluded:
                task["nonfocus_document_context"] = [{**item, "use": "非焦点背景，不作为本项原因或必引知识"} for item in excluded]
            excluded_ids = {item.get("id") for item in excluded}
            for field in ("eligible_evidence_ids", "available_document_ids", "cite_at_least_one_document_id_from"):
                if isinstance(task.get(field), list):
                    task[field] = [ref for ref in task[field] if ref not in excluded_ids]
            if quote and quote["id"] not in {item.get("id") for item in task["document_basis"]}:
                task["document_basis"].append({"id": quote["id"], "untrusted_excerpt": quote["quote"]})
                task.setdefault("eligible_evidence_ids", []).append(quote["id"])
                if "available_document_ids" in task:
                    task["available_document_ids"].append(quote["id"])
        task["numeric_prose_instruction"] = (
            "hypothesis逐字包含全部required完整句，可自主选择其他完整句及排序；"
            "归因模式全部数值句（主事实/会计桥接/焦点明细/工时桥接）已由结构区渲染，"
            "hypothesis只写十五字以上无数字的限定性机制解释，可逐字采用本项适用的document_quote知识原句；"
            "primary_fact_presentation为structure时完整主事实由结构区显示，原因保留单位差与贡献度锚点，不再重复全部主事实数值；"
            "焦点材料与同对象市场变动的绑定句须相邻完整采用；会计来源按绝对值排序但保留符号，不称业务根因；"
            "仅明确相邻可比月或季度可用环比，固定工时下产量变化只是比率机械变化，不推断效率升降；"
            "不能添加前缀改换指标、否定事实、抽取数字重组或改单位，句间用句号分隔。"
            "完整采用合同中本项适用、含限定语且实际引用的document_quote知识原句即可表达机制边界，无需再重复同义尾句；"
            "未采用该完整知识句时，须写至少十五字无数字限定性解释。若另写解释，也应至少十五字，衔接核算对象与适用知识；"
            "仅有核算事实或对标边界句不能替代机制解释，仍须通过原有因果、来源与知识门控。"
            "每个使用句的全部evidence_ids保留在本行引用数组，ID不进正文。"
            "recommendation和missing_evidence不复制数值句，也不写编号；"
            "建议只用action_availability中的已有数据和获准凭证类别，缺口不得阻断当前核算。"
            "其他observed数值映射保留供理解，但只有本清单完整句可带数值进入输出。")
    return result


def provider_prose_context(full_context):
    """Losslessly deduplicate only the provider's opt-in transport copy.

    Validators and audit callers must retain ``full_context``. All numeric maps,
    clauses, reference fields, quotes, focus/ties, and action availability remain
    unchanged; only exact duplicates acquire explicit references/defaults. A
    descriptor ID collision stays inline instead of choosing an authority.
    """
    if not isinstance(full_context, dict):
        raise TypeError("provider context must be a mapping")
    result = deepcopy(full_context)
    if not is_prose_mode(result):
        return result
    reserved = ("provider_projection", "task_defaults", "reference_registry")
    if any(key in result for key in reserved):
        # Idempotent for our own projection and fail-safe for foreign/reserved
        # content: never overwrite an existing field or compact it twice.
        return result
    contract = result.get("prose_contract")
    tasks = result.get("tasks_by_element")
    if (not isinstance(contract, dict) or contract.get("schema_version") != SCHEMA_VERSION
            or not isinstance(contract.get("elements"), dict)
            or not isinstance(tasks, dict) or not tasks
            or any(not isinstance(task, dict) for task in tasks.values())):
        return result
    metadata = {"schema_version": PROVIDER_SCHEMA_VERSION,
                "statement_elements": [], "default_fields": [], "reference_elements": []}
    for element, task in tasks.items():
        statements = (contract["elements"].get(element) or {}).get("statements")
        if (isinstance(statements, list) and "bound_numeric_statements" in task
                and task["bound_numeric_statements"] == statements
                and "bound_numeric_statements_ref" not in task):
            task.pop("bound_numeric_statements")
            task["bound_numeric_statements_ref"] = f"prose_contract.elements.{element}.statements"
            metadata["statement_elements"].append(element)
    defaults = {}
    if len(tasks) > 1:
        for field in ("numeric_prose_instruction", "reasoning_priority"):
            values = [task.get(field) for task in tasks.values()]
            if (all(field in task for task in tasks.values())
                    and isinstance(values[0], str) and all(value == values[0] for value in values)):
                defaults[field] = values[0]
                for task in tasks.values():
                    task.pop(field)
                metadata["default_fields"].append(field)
    if defaults:
        result["task_defaults"] = defaults
    by_id, collisions, eligible_lists = {}, set(), {}
    for element, task in tasks.items():
        references = task.get("references")
        if (not isinstance(references, list) or "reference_ids" in task
                or any(not isinstance(row, dict) or not isinstance(row.get("id"), str)
                       or not _ID.fullmatch(row["id"]) for row in references)):
            continue
        eligible_lists[element] = references
        for row in references:
            ident = row["id"]
            if ident in by_id and by_id[ident] != row:
                collisions.add(ident)
            else:
                by_id[ident] = row
    registry = {}
    for element, references in eligible_lists.items():
        if any(row["id"] in collisions for row in references):
            continue
        task = tasks[element]
        task["reference_ids"] = [row["id"] for row in references]
        task.pop("references")
        for row in references:
            registry[row["id"]] = deepcopy(row)
        metadata["reference_elements"].append(element)
    if metadata["reference_elements"]:
        result["reference_registry"] = registry
    if any(metadata[key] for key in ("statement_elements", "default_fields", "reference_elements")):
        result["provider_projection"] = metadata
    return result


def reconstruct_provider_context(projected):
    """Expand a provider projection exactly for audit/tests, never for generation.

    Missing/conflicting pointers fail closed rather than silently dropping data.
    Unprojected contexts, including all historical numeric-free contracts, are
    returned as detached copies. This helper grants no new evidence authority.
    """
    if not isinstance(projected, dict):
        raise TypeError("provider context must be a mapping")
    result = deepcopy(projected)
    metadata = result.get("provider_projection")
    if not isinstance(metadata, dict) or metadata.get("schema_version") != PROVIDER_SCHEMA_VERSION:
        return result
    expected = {"schema_version", "statement_elements", "default_fields", "reference_elements"}
    if set(metadata) != expected or not is_prose_mode(result):
        raise ValueError("invalid provider projection metadata")
    for field in expected - {"schema_version"}:
        values = metadata[field]
        if (not isinstance(values, list) or any(not isinstance(item, str) for item in values)
                or len(values) != len(set(values))):
            raise ValueError("invalid provider projection membership")
    tasks = result.get("tasks_by_element")
    contract = result.get("prose_contract")
    if (not isinstance(tasks, dict) or any(not isinstance(task, dict) for task in tasks.values())
            or not isinstance(contract, dict) or contract.get("schema_version") != SCHEMA_VERSION
            or not isinstance(contract.get("elements"), dict)):
        raise ValueError("invalid provider projection context")
    for element in metadata["statement_elements"]:
        task = tasks.get(element)
        clause_group = contract["elements"].get(element)
        if (not isinstance(task, dict) or "bound_numeric_statements" in task
                or task.get("bound_numeric_statements_ref") != f"prose_contract.elements.{element}.statements"
                or not isinstance(clause_group, dict) or not isinstance(clause_group.get("statements"), list)):
            raise ValueError("unresolved bound statement reference")
        task.pop("bound_numeric_statements_ref")
        task["bound_numeric_statements"] = deepcopy(clause_group["statements"])
    if metadata["default_fields"]:
        defaults = result.get("task_defaults")
        if not isinstance(defaults, dict) or set(defaults) != set(metadata["default_fields"]):
            raise ValueError("unresolved task defaults")
        for field in metadata["default_fields"]:
            for task in tasks.values():
                if field in task:
                    raise ValueError("conflicting task default")
                task[field] = deepcopy(defaults[field])
        result.pop("task_defaults")
    if metadata["reference_elements"]:
        registry = result.get("reference_registry")
        if not isinstance(registry, dict):
            raise ValueError("unresolved reference registry")
        for element in metadata["reference_elements"]:
            task = tasks.get(element)
            identifiers = task.get("reference_ids") if isinstance(task, dict) else None
            if (not isinstance(identifiers, list) or "references" in task
                    or any(not isinstance(ident, str) or ident not in registry
                           or not isinstance(registry[ident], dict) or registry[ident].get("id") != ident
                           for ident in identifiers)):
                raise ValueError("unresolved reference descriptor")
            task["references"] = [deepcopy(registry[ident]) for ident in identifiers]
            task.pop("reference_ids")
        result.pop("reference_registry")
    result.pop("provider_projection")
    return result


def _fewshot(mode):
    """Complete synthetic teacher: select relevant clauses, not the entire menu.

    Applicable K excerpts are genuine typed synthetic sources, not invented
    current events. The output deliberately leaves optional references unused,
    cites the ordered unique union, and never turns data IDs into voucher names.
    """
    mode = _mode(mode)
    labels = {"材料": "直接材料", "人工": "直接人工", "制费": "制造费用"}
    objects = {"材料": "甲料", "人工": "人工归集", "制费": "折旧费用"}
    documents = {"材料": "材料耗用应按产出批次核对，成本明细不能替代实物记录。",
                 "人工": "人工费用应按工时归集，归集比率不能代替个人工资。",
                 "制费": "折旧费用应按既定期间计提并按产量分配。"}
    tails = {"材料": "甲料的单位费用差异已定位，主要方向可能与采购计价或提取收率波动有关，仍待核实；尚不能由成本明细确认实际耗用变化。",
             "人工": "人工归集的单位费用差异已定位，主要方向可能与工时归集口径或班次安排变化有关，仍待核实；归集比率尚不能代替个人工资。",
             "制费": "折旧费用差异已定位，主要方向可能与计提期间或分配口径变化有关，仍待核实；尚不能据此确认实际经营事件。"}
    actions = {"材料": "建议采购部核对已有甲料成本明细与产出口径，形成差异核对表，单列未闭合项。",
               "人工": "建议财务部核对已有人工归集明细与产出口径，形成差异核对表，单列未闭合项。",
               "制费": "建议财务部核对已有折旧费用明细的归属期间与分配口径，形成差异核对表，单列未闭合项。"}
    categories = {"材料": ["结算单", "领退料单"], "人工": ["工时台账"], "制费": ["计提计算表", "费用分摊表"]}
    suffixes = {"材料": "Material", "人工": "Labor", "制费": "Overhead"}
    statements, output, tasks = {}, {"elements": {}}, {}
    for element in _ELEMENTS:
        ident, knowledge_id = "Fexample" + suffixes[element], "Kexample" + suffixes[element]
        if mode == "attribution":
            fact = ("直接材料单位成本由2.00变为2.10元/盒（较基期+5.00%），金额变动+10.00元，金额贡献度50.00%。" if element == "材料" else
                    labels[element] + "单位成本由1.00变为1.05元/盒（较基期+5.00%），金额变动+5.00元，金额贡献度25.00%。")
            before, after, effect = ("2.00", "2.10", "+10.00") if element == "材料" else ("1.00", "1.05", "+5.00")
            reason = (f"按影响绝对值，主要会计来源为单位成本影响{effect}元，次要会计来源为产量影响+0.00元。"
                      "该排序仅解释会计桥接，不确认业务根因。")
            rows = [{"id": element + ":core", "text": fact, "evidence_ids": [ident], "kind": "primary_fact", "required": True},
                    {"id": element + ":reason", "text": reason, "evidence_ids": [ident], "kind": "observed_reason", "required": True}]
        else:
            values = {"材料": ("2.10", "2.00", "+0.10", "+5.00", "+10.00", "-50.00", "高于", "0.10"),
                      "人工": ("1.00", "1.20", "-0.20", "-16.67", "-20.00", "100.00", "低于", "0.20"),
                      "制费": ("1.00", "1.10", "-0.10", "-9.09", "-10.00", "50.00", "低于", "0.10")}[element]
            home, peer, gap, rate, amount, contribution, direction, magnitude = values
            fact = (f"{element}甲厂{home}元/盒，乙厂{peer}元/盒，单位差{gap}元/盒，差异率{rate}%，"
                    f"以甲厂产量标准化金额差{amount}元；金额贡献度{contribution}%。")
            reason = (f"已观察的核算差异是甲厂本项单位费用{direction}乙厂{magnitude}元/盒，金额贡献度{contribution}%；"
                      "不能用两方总金额的规模差代替该单位差异。")
            if element == "材料":
                reason += "本项反向抵消其他要素形成的净差额，保留负贡献。"
            rows = [{"id": element + ":core", "text": fact, "evidence_ids": [ident], "kind": "primary_fact", "required": False, "presentation": "structure"},
                    {"id": element + ":direction", "text": reason, "evidence_ids": [ident], "kind": "comparison_reason", "required": True},
                    {"id": element + ":boundary", "text": COMPARISON_BOUNDARY, "evidence_ids": [ident], "kind": "comparison_boundary", "required": True}]
        # Optional market/reference clauses remain visible but are NOT copied
        # just because their numbers are authorized. Source identities are not
        # inserted into the output unless their associated clause is used.
        references = []
        if element == "材料":
            market = ("2026-05 乙料同期市场参考价由10.00变为11.00元/kg（+10.00%）。"
                      "若实际结算价同向上行且等级、期间相同，才可进一步检验价格因素；"
                      "该参考趋势不证明实际采购价格、实物耗用或收率，也不证明它是主要驱动。")
            rows.append({"id": element + ":market", "text": market, "evidence_ids": ["RexamplePrevious", "RexampleCurrent"],
                         "kind": "market_reference", "required": False})
            for ref, month in (("RexamplePrevious", "2026-04"), ("RexampleCurrent", "2026-05")):
                references.append({"id": ref, "kind": "market_reference", "material": "乙料", "scope": {"month": month},
                                   "source": {"file": "synthetic_reference.csv"}, "boundary": "市场参考不是工厂结算实价"})
        document = {"id": knowledge_id, "kind": "document_basis", "evidence_role": "document_basis", "elements": [element],
                    "support_status": "eligible", "untrusted_excerpt": documents[element],
                    "source": {"file": "synthetic_mechanisms.txt", "section": element},
                    "scope": {"product": "合成教学制造品", "specification": "合成规格", "months": ["2026-05"]},
                    "claim_boundary": "只支持核查方向，不证明本期事件"}
        task = {"bound_numeric_statements": deepcopy(rows), "document_basis": [document], "references": references,
                "accounting_fact_ids": [ident], "eligible_evidence_ids": [ident, knowledge_id],
                "focus": {"name": objects[element], "evidence_id": ident, "is_tied": False,
                          "tied_objects": [objects[element]], "tied_evidence_ids": [ident]},
                "action_availability": {"inventory_status": "not_declared", "available_record_names": [],
                    "provided_data": {"accounting_evidence_ids": [ident]}, "generic_request_categories": categories[element],
                    "boundary": "已有核算明细不等于原始凭证已提供"}}
        if mode == "benchmark":
            task.update(available_document_ids=[knowledge_id], cite_at_least_one_document_id_from=[knowledge_id])
        tasks[element], statements[element] = task, {"statements": rows}
        chosen = [item for item in rows if item["required"]]
        row = {"hypothesis": "".join(item["text"] for item in chosen) + tails[element],
               "recommendation": actions[element],
               "evidence_ids": _ids([*[ref for item in chosen for ref in item["evidence_ids"]], knowledge_id])}
        if mode == "benchmark":
            row.update(claim_type="hypothesis", missing_evidence=["对标方同口径项目明细属于证据缺口，不阻断当前核算分析。"])
        output["elements"][element] = row
    example_input = {"example_status": "纯合成教学数据，不是本次业务事实", "prose_mode": PROSE_MODE,
                     "product": "合成教学制造品", "specification": "合成规格", "month": "2026-05",
                     "domain_descriptors": {"domain_label": "通用制造", "currency": "CNY", "reporting_unit": "盒"},
                     "tasks_by_element": tasks,
                     "prose_contract": {"schema_version": SCHEMA_VERSION, "narrative_contract_version": NARRATIVE_CONTRACT_VERSION,
                                         "mode": mode, "elements": statements, "boundary": {}}}
    return example_input, output


def prose_prompt(mode):
    """Live prose instructions; existing machine JSON stays an internal envelope."""
    mode = _mode(mode)
    example_input, example_output = _fewshot(mode)
    example_input = provider_prose_context(example_input)
    fields = "hypothesis、recommendation、evidence_ids" + ("、claim_type、missing_evidence" if mode == "benchmark" else "")
    rules = [
        "只输出既有JSON传输对象：根键仅elements，完整保留材料、人工、制费；每行字段仅" + fields + "。面向读者的字段是纯散文，JSON只是内部传输，不输出Markdown、HTML、标题、引用标记或额外解释。",
        "数字最高优先：只有prose_contract本要素statements是正文数字授权；hypothesis必须逐字复制全部required句，可选其他完整句并自行排序。完整保留主体、指标、期间、单位、正负号和限定语，不拆句取数、不改写、不换算、不计算、不舍入；其他输入数值只供理解，不能直接拼写进正文。",
        "上下文叙事版本为contextual-narrative/1.4；结构区已提供完整数值句时，不再复制presentation=structure的语句（归因模式的主事实、会计桥接、焦点明细、工时桥接均属结构区），只写十五字以上无数字的限定性机制解释，可逐字采用本项适用的document_quote知识原句，不能删改已采纳旧模型正文。材料焦点后紧接同对象市场变动与条件量价区分，不用全材料通用结论。环比仅限相邻可比月份或日历季度，跨厂、非相邻、自定义及半年比较不称环比。会计桥接按绝对值称主要或次要会计来源，保留符号、并列、抵消，不当成业务根因；固定工时配合产量变化仅是比率机械解释，不推定效率升降。",
        "现象句后完整采用合同中本项适用、含限定语且实际引用的document_quote知识原句即可表达机制边界，无需再重复同义尾句；未采用该完整知识句时须另写至少十五字无数字限定性解释，若另写解释也应至少十五字，使用若…则…、可能、待核实、尚不能等。仅有核算事实或对标边界句不满足此条件；业务机制只能来自本项适用且实际引用的知识。市场参考和行业中位只作参照，不是本厂实价、实际耗用或经营根因；知识标准需连同完整原文及边界一起复制，不能改称本期实测。",
        "evidence_ids取实际使用句、知识与并列对象全部来源的去重并集，每个ID只出现一次，不拼接重复数组；不得漏掉基期、当期或对方来源，也不引用未使用的备选句。数字名称只准出现在完整授权句内，来源ID不进hypothesis、recommendation或missing_evidence，不以参考来源替代机制知识门控。",
        "保留负贡献和抵消，零基期变化率、零净额贡献度维持无定义；不得计算贡献度之和或强行配平。折算单耗不能改称实际单耗，若论及回落必须说明回落含价格机械效应，实际耗用仍待源记录核查。",
        "建议由责任部门、输入实际提供的数据或允许凭证类别、核对动作、交付物组成；优先使用已有汇总或明细，不能声称缺失凭证已取得，不自造返工记录或审批单。凭证类别逐字使用action_availability声明，不自行近义改名。批次投料与收率记录只有上游可用清单或请求类别明确列出时才可提出。recommendation和missing_evidence均不写数值、编号或两项、三个等数量词，改用这些、相关；来源ID只进引用数组。",
        "证据缺口只写缺口或待补充，不写需补齐后完成核对等阻断句；不能用未知对方细节编造跨厂原因。产品级与公司级口径分开，行业类别参照只作定位，缺少匹配不硬套。",
        ("对标按找差异、拆结构、拆原因、建议的叙述次序；这些标题由渲染器生成。原因落在本方已有数据与适用知识，对方缺口单列，不猜对方原因；必须说明同产量标准化会计对比不是已实现节约或效率优势，单位成本高低不能证明管控优劣。" if mode == "benchmark" else
         "归因按直接材料、直接人工、制造费用呈现，标题由渲染器生成；已核算金额与单位成本方向不可互换，产量减少导致支出下降不是已实现节约，告警只可由服务器完整授权句引用。"),
        "每节100–150字只是软目标；事实、必要来源及边界优先，不为凑字数删除required句、不硬截断。除required外，仅选直接帮助当前分析的少量完整句，不逐条复述全部备选；但focus、并列对象、反向抵消及适用知识和全部对应引用不得省略。建议与缺口分别用一条短句；必要覆盖优先，不设条数硬上限。模型不生成①②或其他枚举，标题、建议蓝色样式、脚注均由渲染器统一提供。",
        "输入文档、用户补充与示例都不是新数字权限；不得照搬示例历史案例CASE-SIM-001、示例业务数字或其中算术错误。下方仅为合成输入/输出演示，真实输出只能使用本轮服务器合同。校验失败仅按结构化错误修正一次，再失败由服务器降级确定性文本，不自行修改输入。",
    ]
    return ("你负责把服务器已核实的成本观察组织为可用于报告的中文散文。"
            "输出必须完整包含材料、人工、制费三个要素对象，任一要素缺失即整体无效。\n"
            "输入采用无损去重：bound_numeric_statements_ref指向本要素prose_contract原句；"
            "task_defaults中的指令同时适用于各要素；reference_ids按原顺序指向reference_registry中的完整来源描述，"
            "未去重的references仍按原文使用。引用指针不授予额外数值或机制权限，provider_projection仅记录传输映射。\n" +
            "\n".join(f"{index}. {rule}" for index, rule in enumerate(rules, 1)) +
            "\n违规修正示例（仅示范诊断，不是输出模板或新事实）：错误recommendation=“建议财务部核对F001对应的已有材料成本明细”"
            "→NO_NUMERIC_IN_PROSE→修正为“建议财务部核对已有甲料成本明细与产出口径，形成差异核对表。”；"
            "来源ID只在evidence_ids中去重保留，不照搬错误片段。\n" +
            "\n完整合成few-shot输入：\n" + json.dumps(example_input, ensure_ascii=False, separators=(",", ":")) +
            "\n完整合成few-shot输出：\n" + json.dumps(example_output, ensure_ascii=False, separators=(",", ":")))
