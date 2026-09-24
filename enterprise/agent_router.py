"""Bounded Chinese query/form-preview router; a rule-based planner, never an LLM.

``plan_intent(principal, message, context=None, *, catalog=None)`` performs no
I/O and returns an ``enterprise-agent-plan/1.0`` JSON object. Optional catalog
is a sequence of {factory, product, specification, month} records, NOT a grant.
Only authorized records are considered. Context accepts exactly those four
scope fields plus theme/method; it cannot supply tools, plans or identities.

``execute_request(application, message, context=None)`` rebuilds that catalog
from this request's authorized Application.tables() snapshot, replans from raw
text, and rechecks permissions at dispatch. Success has status ``completed``;
refusals retain ``denied``/``needs_clarification``; operational failures use ``failed``
with a safe error code and correlation id. Responses include the plan, Chinese
answer, bounded result, trace, provenance, and explicit no-write/no-send effects.
No result, table, document, principal or plan is cached between requests.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
import json
import logging
import math
from numbers import Integral, Real
import re
import unicodedata
from uuid import uuid4

from enterprise.knowledge_applicability import evidence_policy
from enterprise.security import Principal, can, require

PLAN_SCHEMA = "enterprise-agent-plan/1.0"
RESULT_SCHEMA = "enterprise-agent-result/1.0"
_LOG = logging.getLogger('project4.agent')
TOOLS = frozenset({"cost_summary", "attribution", "benchmark", "knowledge_search",
                   "forecast", "open_reports", "prepare_report"})
CONTEXT_FIELDS = frozenset({"factory", "product", "specification", "month", "theme", "method"})
FACTORIES = ("中药一厂", "中药二厂")
IDENTITY = {"factory": "工厂", "product": "产品名称", "specification": "产品规格", "month": "月份"}
SUMMARY_TABLES = ("cost25", "cost26", "erchang25", "erchang26")
ELEMENTS = {"材料": "直接材料(元/盒)", "人工": "直接人工(元/盒)", "制费": "制造费用(元/盒)"}
THEMES = ("月度成本分析", "季度成本分析", "专题分析")
PRODUCT_ALIASES = {"银黄口服液": "银黄口服液", "银黄": "银黄口服液",
                   "板蓝根颗粒": "板蓝根颗粒", "板蓝根": "板蓝根颗粒",
                   "六味地黄胶囊": "六味地黄胶囊", "六味地黄": "六味地黄胶囊"}
FACTORY_ALIASES = {"中药一厂": FACTORIES[0], "一厂": FACTORIES[0], "1厂": FACTORIES[0],
                   "中药二厂": FACTORIES[1], "二厂": FACTORIES[1], "2厂": FACTORIES[1],
                   "factory 1": FACTORIES[0], "factory 2": FACTORIES[1]}
INTENTS = {
    "cost_summary": r"成本(?:汇总|概览|情况|查询|摘要)|单位成本|总成本|产量|cost_summary|\bcost\s+summary\b",
    "attribution": r"(?:成本)?归因(?:分析)?|原因分析|分析原因|\battribution\b",
    "benchmark": r"跨厂(?:对标|比较|对比)?|两厂(?:对标|比较|对比)|对标(?:分析)?|\bbenchmark\b",
    "knowledge_search": r"知识(?:库)?(?:检索|查询|搜索)|检索(?:知识|资料|文档)|查找(?:知识|资料|文档)|knowledge_search|\bknowledge\s+search\b",
    "forecast": r"预测|\bforecast\b",
    "open_reports": r"打开报告(?:列表|页面|中心|库|页)?|查看(?:已有)?报告(?:列表|页面|记录|档案)|open_reports|\bopen\s+reports\b",
    "prepare_report": r"准备报告(?:表单|参数|预览)?|预览报告(?:表单|参数)?|报告(?:表单|参数|预览)|prepare_report|\bprepare\s+report\b",
}
# Conservative input refusal is intentional. These words never unlock a tool,
# even for local all-role principals; the entire message is checked first.
FORBIDDEN = re.compile(
    r"审批|批准|审核通过|提交|签发|下发|发送|发给|发邮件|发布|保存|持久化|写入|删除|清空|"
    r"上传|导入|导出|修改|更新|新增|创建任务|建立任务|执行|运行|调用模型|大模型|云模型|"
    r"推送|寄送|外发|写文件|读文件|系统命令|脚本|"
    r"忽略.{0,12}(?:指令|规则|权限)|绕过|无视.{0,8}(?:规则|权限)|伪造|冒充|"
    r"\b(?:approve|approval|submit|issue|send|publish|save|write|delete|update|insert|drop|"
    r"alter|truncate|grant|revoke|select|union|sql|exec|execute|run|eval|shell|bash|sh|powershell|cmd|"
    r"curl|wget|python|subprocess|rpa|llm|system|sudo|import|script|mkdir|rm|chmod)\b|"
    r"(?:https?|ftp|file|javascript|data)\s*:|www\.|\b[\w-]+\.(?:com|net|org|cn|io|invalid)\b|"
    r"[a-z]:[\\/]|\\\\|\.\.[\\/]|`|\$\(|&&|\|\||<\s*(?:script|iframe)|[{}]",
    re.IGNORECASE,
)


class _Clarify(ValueError):
    def __init__(self, reason, *fields):
        super().__init__(reason)
        self.fields = list(fields)


def _plan(status, tool=None, arguments=None, reason=None, missing=()):
    return {"schema_version": PLAN_SCHEMA, "planner": "rule_based", "status": status,
            "tool": tool, "arguments": arguments or {}, "missing_fields": list(missing), "reason": reason}


def _text(value, label, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise _Clarify(f"{label}须为非空文本，且不超过{limit}字。", label)
    # NFKC handles full-width command text; invisible controls are not discarded.
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise _Clarify(f"{label}含不可识别控制字符。", label)
    return unicodedata.normalize("NFKC", value).strip()


def _month(value):
    match = re.fullmatch(r"([0-9]{4})-([0-9]{2})", value)
    if not match:
        match = re.fullmatch(r"([0-9]{4})年([0-9]{1,2})月", value)
    if not match:
        raise _Clarify("月份须为YYYY-MM或YYYY年M月，不能省略年份。", "month")
    year, month = map(int, match.groups())
    try:
        date(year, month, 1)
    except ValueError:
        raise _Clarify("月份或年份无效。", "month") from None
    return f"{year:04d}-{month:02d}"


def _merge(field, values, context):
    candidates = set(values)
    if field in context:
        candidates.add(context[field])
    if len(candidates) > 1:
        raise _Clarify(f"{field}存在多个值或与上下文冲突，请明确单一范围。", field)
    return next(iter(candidates), None)


def _catalog(principal, catalog):
    if catalog is None:
        return None
    if not isinstance(catalog, (list, tuple)):
        raise _Clarify("目录须为范围记录列表。", "catalog")
    rows = []
    for row in catalog:
        if not isinstance(row, dict) or not set(IDENTITY) <= row.keys():
            raise _Clarify("目录记录缺少工厂、产品、规格或月份。", "catalog")
        if any(not isinstance(row[k], str) or not row[k].strip() for k in IDENTITY):
            raise _Clarify("目录范围字段无效。", "catalog")
        # A forged catalog can describe a scope but cannot grant it.
        if can(principal, "data.read", factory=row["factory"], product=row["product"]):
            normalized = {k: row[k] for k in IDENTITY}
            normalized["month"] = _month(row["month"])
            rows.append(normalized)
    return rows


def _extract_dates(text, context):
    values = []
    pattern = r"(?<![0-9])[0-9]{2,6}(?:[-/.][0-9]{1,4}(?:[-/.][0-9]{1,4})?|年[0-9]{1,3}月(?:[0-9]{1,3}日)?)"
    def consume(match):
        values.append(_month(match.group()))
        return " "
    rest = re.sub(pattern, consume, text)
    month = _merge("month", values, context)
    quarters = []
    quarter_pattern = r"(?:(?<![0-9])([0-9]{4})\s*年?\s*[- ]?\s*)?(?:Q([0-9]+)|第?([一二三四五六七八九0-9]+)季度)"
    def quarter(match):
        raw = match.group(2) or match.group(3)
        number = {"一": 1, "二": 2, "三": 3, "四": 4}.get(raw)
        if number is None and raw.isascii() and raw.isdigit():
            number = int(raw)
        if number not in (1, 2, 3, 4):
            raise _Clarify("季度须为Q1至Q4或第一至第四季度。", "month")
        year = match.group(1) or (month[:4] if month else None)
        if year is None:
            raise _Clarify("季度需要明确年份。", "month")
        anchor = _month(f"{year}-{number * 3:02d}")
        quarters.append(anchor)
        return " "
    rest = re.sub(quarter_pattern, quarter, rest, flags=re.I)
    if len(set(quarters)) > 1:
        raise _Clarify("请选择一个季度。", "month")
    if quarters:
        anchor = quarters[0]
        if month and (month[:4] != anchor[:4] or (int(month[5:]) - 1) // 3 != (int(anchor[5:]) - 1) // 3):
            raise _Clarify("月份与季度冲突。", "month")
        month = month or anchor
    if re.search(r"(?:[0-9]+|[一二三四五六七八九十]+)月|[0-9]{4}年|\bQ[0-9]|下一个月|下一月|下个月|下月|\bnext\s+month\b", rest, re.I):
        raise _Clarify("日期不完整，请明确YYYY-MM或带年份的季度。", "month")
    return month, bool(quarters), rest


def _extract_aliases(text, aliases, *, product=False):
    found = []
    rest = text
    for alias in sorted(aliases, key=len, reverse=True):
        label = r"(?:产品(?:名称)?\s*[:=：]?\s*)?" if product else r"(?:工厂\s*[:=：]?\s*)?"
        pattern = label + re.escape(alias)
        if product and alias != aliases[alias]:
            # 银黄 is an alias, 银黄颗粒 is a different, unknown product.
            pattern += r"(?=$|[^\u4e00-\u9fffA-Za-z]|的|成本|产量|归因|预测|对标|知识|报告)"
        if re.search(pattern, rest, re.I):
            found.append(aliases[alias])
            rest = re.sub(pattern, " ", rest, flags=re.I)
    return found, rest


def _authorize(principal, tool, args):
    if tool not in TOOLS:
        raise PermissionError("操作不在查询与预览白名单内。")
    product, factory = args.get("product"), args.get("factory")
    if tool in {"attribution", "prepare_report"} and factory not in (None, FACTORIES[0]):
        raise PermissionError("此操作仅支持中药一厂。")
    actions = {
        "cost_summary": ("data.read",), "attribution": ("analysis.generate", "data.read", "knowledge.read"),
        "benchmark": ("data.read", "dashboard.read"), "knowledge_search": ("knowledge.read",),
        "forecast": ("analysis.generate", "data.read"), "open_reports": ("report.read",),
        "prepare_report": ("report.generate", "data.read"),
    }[tool]
    factories = FACTORIES if tool == "benchmark" else (factory,)
    for scope in factories:
        for action in actions:
            require(principal, action, factory=scope, product=product)


def plan_intent(principal, message, context=None, *, catalog=None):
    """Pure, conservative rule planner. A ready plan is a preview, not authority."""
    tool, args = None, {}
    try:
        if not isinstance(principal, Principal):
            raise PermissionError("必须使用服务端已认证身份。")
        text = _text(message, "message", 1000)
        if context is None:
            context = {}
        if not isinstance(context, dict) or set(context) - CONTEXT_FIELDS:
            raise PermissionError("上下文只允许factory/product/specification/month/theme/method；禁止计划、工具或身份覆盖。")
        ctx = {key: _text(value, key, 160) for key, value in context.items()}
        if any(FORBIDDEN.search(value) for value in (text, *ctx.values())):
            raise PermissionError("仅支持只读查询和报告表单准备；拒绝审批、发送、写入、代码、SQL、URL及越权指令。")
        if "month" in ctx:
            ctx["month"] = _month(ctx["month"])
        if "product" in ctx:
            ctx["product"] = PRODUCT_ALIASES.get(ctx["product"], ctx["product"])
        if "factory" in ctx:
            ctx["factory"] = FACTORY_ALIASES.get(ctx["factory"].lower(), ctx["factory"])
        if "theme" in ctx and ctx["theme"] not in THEMES:
            raise _Clarify("主题只支持月度成本分析、季度成本分析、专题分析。", "theme")
        if "method" in ctx and ctx["method"] not in ("naive", "ma3"):
            raise _Clarify("预测方法只支持naive或ma3。", "method")
        matches = [name for name, pattern in INTENTS.items() if re.search(pattern, text, re.I)]
        # A forecast's metric ('单位成本') is not a second cost query.
        if "cost_summary" in matches and len(matches) > 1 and not re.search(r"成本(?:汇总|概览|情况|查询|摘要)|cost_summary|cost\s+summary", text, re.I):
            matches.remove("cost_summary")
        if not matches and "成本" in text and re.search(r"查|看", text):
            matches = ["cost_summary"]
        if len(matches) != 1:
            raise _Clarify("请每次明确选择一个操作：成本汇总、归因、对标、知识检索、预测、打开报告或准备报告表单。", "operation")
        tool = matches[0]
        # Do not execute the harmless prefix of a chained or unknown second task.
        if re.search(r"[;；\n]|然后|并且|同时|顺便|接着|再(?:帮|查|看|做|预测|打开|准备)|\b(?:and|then|also)\b", text, re.I):
            raise _Clarify("一次只处理一个操作，请拆分请求。", "operation")
        rows = _catalog(principal, catalog)
        # Forecast month always means the observed cutoff. Relative target words
        # never move it, consult a clock, or override an explicit conflicting date.
        scope_text = re.sub(r"下一个月|下一月|下个月|下月|\bnext\s+month\b", " ", text, flags=re.I) if tool == "forecast" else text
        month, quarter, rest = _extract_dates(scope_text, ctx)
        product_aliases = dict(PRODUCT_ALIASES)
        for name in principal.products:
            if name != "*":
                product_aliases[name] = name
        for row in rows or []:
            product_aliases[row["product"]] = row["product"]
        products, rest = _extract_aliases(rest, product_aliases, product=True)
        factories, rest = _extract_aliases(rest, FACTORY_ALIASES)
        # Explicit labels and unknown dosage/factory names cannot fall back to ctx.
        if re.search(r"(?:产品|工厂)\s*[:=：]\s*[^ ,，。]|[\u4e00-\u9fff]{2,}(?:胶囊|颗粒|口服液|注射液|片剂)|(?:中药)?[三四五六七八九十0-9]+厂", rest):
            raise _Clarify("无法识别请求中的产品或工厂，请明确完整授权名称。", "product", "factory")
        product = _merge("product", products, ctx)
        if tool == "benchmark":
            # Naming both factories is integral to this one comparison. Context
            # still cannot introduce a third factory or grant either peer.
            if "factory" in ctx and ctx["factory"] not in FACTORIES:
                raise _Clarify("跨厂对标只支持中药一厂和中药二厂。", "factory")
            factory = None
        else:
            factory = _merge("factory", factories, ctx)
            if tool in {"attribution", "prepare_report"} and factory is None:
                factory = FACTORIES[0]
        args = {key: value for key, value in (("factory", factory), ("product", product), ("month", month)) if value}
        _authorize(principal, tool, args)
        if product and product not in set(PRODUCT_ALIASES.values()) | {name for name in principal.products if name != "*"} | {r["product"] for r in rows or []}:
            raise _Clarify("产品未知，请指定授权目录中的完整产品名称。", "product")
        if factory and factory not in FACTORIES:
            raise _Clarify("工厂未知，只支持中药一厂或中药二厂。", "factory")
        if rows is not None and product and not any(r["product"] == product for r in rows):
            raise _Clarify("授权成本目录中没有该产品，不能替代为其他产品。", "product")
        candidates = [r for r in rows or [] if r["product"] == product and (not factory or r["factory"] == factory)]
        if not factory and tool not in {"benchmark", "open_reports"}:
            choices = {r["factory"] for r in candidates}
            if len(choices) == 1:
                factory = args["factory"] = choices.pop()
        specifications = {r["specification"] for r in candidates}
        found_specs = []
        for spec in sorted(specifications | ({ctx["specification"]} if "specification" in ctx else set()), key=len, reverse=True):
            if spec in rest:
                found_specs.append(spec)
                rest = re.sub(r"(?:规格\s*[:=：]?\s*)?" + re.escape(spec), " ", rest)
        labelled = re.search(r"规格\s*[:=：]?\s*([^ ,，。;；]+)", rest)
        if labelled:
            found_specs.append(labelled.group(1))
            rest = rest[:labelled.start()] + " " + rest[labelled.end():]
        specification = _merge("specification", found_specs, ctx)
        if specification is None and len(specifications) == 1:
            specification = next(iter(specifications))
        if specification:
            args["specification"] = specification
            if rows is not None and product and specification not in specifications:
                raise _Clarify("授权目录中没有所选产品/工厂的该规格，不能替代或合并。", "specification")
        themes = [theme for theme in THEMES if theme in rest]
        if quarter:
            themes.append("季度成本分析")
        theme = _merge("theme", themes, ctx)
        if quarter and tool != "prepare_report":
            raise _Clarify("此查询需要单一月份；季度仅用于报告表单，请选择YYYY-MM。", "month")
        methods = re.findall(r"\b(?:naive|ma3)\b", rest, re.I)
        if "三月移动平均" in rest or "三个月移动平均" in rest:
            methods.append("ma3")
        if "朴素预测" in rest:
            methods.append("naive")
        method = _merge("method", [value.lower() for value in methods], ctx)
        if method and tool != "forecast":
            raise _Clarify("method仅适用于预测。", "method")
        if theme and tool != "prepare_report":
            raise _Clarify("theme仅适用于报告表单准备。", "theme")
        if tool == "forecast":
            args["method"] = method or "naive"
        if tool == "prepare_report":
            args.update(theme=theme or "月度成本分析", use_llm=False, formal=True, include_benchmark=False)
        if tool == "knowledge_search":
            args["query"] = text  # Opaque search data; never parsed again after retrieval.
        else:
            residue = rest.replace("草稿", " ") if tool == "prepare_report" else rest
            for pattern in INTENTS.values():
                residue = re.sub(pattern, " ", residue, flags=re.I)
            for word in (*THEMES, "三个月移动平均", "三月移动平均", "朴素", "naive", "ma3"):
                residue = residue.replace(word, " ")
            residue = re.sub(r"帮我|请问|请|查看|查询|查一下|看一下|看看|显示|我要|想要|需要|做一下|一下|产品|工厂|规格|月份|主题|方法|截至|截止|使用|采用|的|与|和|成本|两厂|本期|[\s,，。:：=?？!！/()（）-]+", "", residue)
            if residue:
                raise _Clarify("有未识别内容或额外操作，请使用明确的单一查询和完整范围。", "message")
        required = [] if tool == "open_reports" else ["product", "specification", "month"]
        if tool not in {"benchmark", "open_reports"}:
            required.insert(0, "factory")
        missing = [field for field in required if not args.get(field)]
        if missing:
            raise _Clarify("请补充" + "、".join(missing) + "；不会猜测产品、月份或合并规格。", *missing)
        _authorize(principal, tool, args)
        return _plan("ready", tool, args)
    except PermissionError as exc:
        return _plan("denied", tool, {}, str(exc))
    except _Clarify as exc:
        return _plan("needs_clarification", tool, args, str(exc), exc.fields)


def _json_safe(value):
    def scalar(item):
        if isinstance(item, Integral):
            return int(item)
        if isinstance(item, Real):
            return float(item)
        if isinstance(item, Decimal):
            return str(item)
        raise TypeError("结果含非JSON字段")
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False, default=scalar))


def _authorized_tables(principal, tables):
    import pandas as pd
    if not isinstance(tables, dict):
        raise ValueError("成本快照不是数据表集合。")
    result = {}
    for name, frame in tables.items():
        if not isinstance(name, str) or not isinstance(frame, pd.DataFrame) or frame.columns.duplicated().any():
            raise ValueError("成本数据表结构无效。")
        if frame.empty:
            result[name] = frame.copy()
            continue
        if not set(IDENTITY.values()) <= set(frame.columns):
            raise ValueError("数据表缺少完整范围，禁止无范围读取。")
        mask = [isinstance(factory, str) and isinstance(product, str) and can(
            principal, "data.read", factory=factory, product=product)
            for factory, product in zip(frame["工厂"], frame["产品名称"])]
        result[name] = frame.loc[mask].copy()
    return result


def _server_catalog(tables):
    rows = []
    for name in SUMMARY_TABLES:
        frame = tables.get(name)
        if frame is not None and not frame.empty:
            for row in frame.to_dict("records"):
                rows.append({key: row[column] for key, column in IDENTITY.items()})
    return rows


def _scope_tables(tables, args, *, benchmark=False, history=False):
    import pandas as pd
    selected = {}
    factories = FACTORIES if benchmark else (args["factory"],)
    for name, frame in tables.items():
        if frame.empty:
            selected[name] = frame.copy()
            continue
        mask = frame["工厂"].isin(factories) & frame["产品名称"].eq(args["product"]) & frame["产品规格"].eq(args["specification"])
        if history:
            mask &= frame["月份"].le(args["month"])
        selected[name] = frame.loc[mask].copy()
    for name in (*SUMMARY_TABLES, "budget", "material", "labor", "mfg"):
        selected.setdefault(name, pd.DataFrame())
    return selected


def _provenance(tables):
    result = []
    for name, frame in tables.items():
        if frame.empty:
            continue
        metadata = {key: frame.attrs[key] for key in
                    ("cost_revision", "cost_snapshot_hash", "source_hash", "source_sha256") if key in frame.attrs}
        result.append({"table": name, **_json_safe(metadata)})
    return result


def _number(value, label):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{label}缺失或不是有效数字。") from None
    if not number.is_finite() or number < 0 or not math.isfinite(float(number)):
        raise ValueError(f"{label}必须为非负有限数字。")
    return number


def _summary(tables, args):
    matches = []
    for name in SUMMARY_TABLES:
        frame = tables.get(name)
        if frame is None or frame.empty:
            continue
        rows = frame
        for key, column in IDENTITY.items():
            rows = rows.loc[rows[column].eq(args[key])]
        matches.extend((name, frame, row) for _, row in rows.iterrows())
    if len(matches) != 1:
        raise ValueError("同工厂、产品、规格、月份的实际汇总记录缺失或重复；不能替代、合并或推算。")
    name, frame, row = matches[0]
    with localcontext() as ctx:
        ctx.prec = 50
        elements = {key: _number(row.get(column), column) for key, column in ELEMENTS.items()}
        unit = _number(row.get("单位成本(元/盒)"), "单位成本")
        volume = _number(row.get("产量(盒)"), "产量")
        total = _number(row.get("总成本(元)"), "总成本")
        if sum(elements.values()) != unit or abs(unit * volume - total) > Decimal("0.01"):
            raise ValueError("实际成本三要素或总成本勾稽不闭合。")
        amounts = {key: value * volume for key, value in elements.items()}
        result = {"factory": args["factory"], "product": args["product"], "specification": args["specification"],
                  "month": args["month"], "unit_cost": float(unit), "volume": float(volume), "total_cost": float(total),
                  "elements": {key: float(value) for key, value in elements.items()},
                  "element_amounts": {key: float(value) for key, value in amounts.items()},
                  "exact": {"unit_cost": str(unit), "volume": str(volume), "total_cost": str(total),
                            "elements": {key: str(value) for key, value in elements.items()},
                            "element_amounts": {key: str(value) for key, value in amounts.items()}},
                  "calculation": "实际汇总行；要素金额=要素单位成本×实际产量", "used_llm": False}
    return result, _provenance({name: frame})


def _dispatch(application, principal, plan, tables):
    tool, args = plan["tool"], plan["arguments"]
    # This check is deliberately independent of the planning checks and happens
    # immediately before the hardcoded handlers. No caller chooses a callable.
    if application.principal != principal:
        raise PermissionError("当前身份已变化，请重新发起查询。")
    _authorize(application.principal, tool, args)
    if tool == "open_reports":
        return {"navigation": {"page": "reports", "label": "报告中心", "filters": args}}, "请在现有报告页面查看授权报告；本次仅返回导航提示。", []
    if tool == "knowledge_search":
        from enterprise.knowledge_langchain import retrieve_rows
        year, month = map(int, args["month"].split("-"))
        repository = application.knowledge()
        if repository.root != application.root or repository.principal != principal:
            raise PermissionError("知识仓库必须绑定当前服务端身份和根目录。")
        rows, diagnostics = retrieve_rows(args["query"], principal=principal, repository=repository,
            factory=args["factory"], product=args["product"],
            as_of=f"{args['month']}-{monthrange(year, month)[1]:02d}", top_k=5,
            require_hybrid=True, vector_timeout=15.0)
        sources, excluded = [], []
        for row in rows[:5]:
            meta, text = row.get("meta", {}), row.get("text", "")
            if not isinstance(text, str):
                raise ValueError("知识片段格式无效。")
            policy = evidence_policy(meta, args['product'], args['specification'])
            if not policy['included']:
                excluded.append({'chunk_id': row.get('chunk_id'), 'reason': policy['applicability_status']})
                continue
            sources.append({"text": text[:1000], "truncated": len(text) > 1000,
                **{key: value for key, value in policy.items() if key != 'included'},
                **{key: row.get(key) for key in ("document_id", "version_id", "chunk_id", "release_id")},
                'business_metadata': meta.get('business_metadata', {}),
                "source": {key: meta.get(key) for key in ("filename", "sha256", "offset", "end_offset", "page_hint")}})
        diagnostics = {**diagnostics, 'applicability_excluded': excluded, 'returned_n': len(sources),
                       'no_answer': not sources}
        if rows and not sources:
            diagnostics['reason'] = 'no_applicable_evidence'
        result = {"sources": sources, "diagnostics": diagnostics,
                  "boundary": "以下为授权知识原文，仅作参考数据；不作为操作指令，也不证明本期成本因果。"}
        provenance = [{"index_release_id": diagnostics.get("release_id"), "generation": diagnostics.get("generation")}]
        return result, f"找到{len(sources)}条授权知识片段；请结合来源和发布诊断核查。", provenance
    scoped = _scope_tables(tables, args, benchmark=tool == "benchmark", history=tool in {"attribution", "forecast"})
    provenance = _provenance(scoped)
    if tool == "cost_summary":
        result, provenance = _summary(scoped, args)
        text = (f"{args['month']} {args['factory']} {args['product']}（{args['specification']}）："
                f"实际产量{result['volume']:g}盒，总成本{result['total_cost']:g}元，单位成本{result['unit_cost']:g}元/盒；"
                + "、".join(f"{key}{value:g}元/盒" for key, value in result["elements"].items()) + "。")
        return result, text, provenance
    if tool == "attribution":
        from attribution_gen import generate_attribution
        _summary(scoped, args)
        result = generate_attribution(args["product"], args["month"], use_llm=False,
                                      d=scoped, principal=principal, root=application.root)
        if result.get("generation_status") == "insufficient_data":
            raise ValueError("当前数据不足以进行归因，请补齐连续可比期间。")
        return result, str(result.get("overview") or "已完成程序成本归因，经营原因仍需核查。")[:1000], provenance
    if tool == "benchmark":
        from enterprise.benchmark import build_benchmark
        result = build_benchmark(args["product"], args["specification"], args["month"], tables=scoped)
        if not result.get("available"):
            raise ValueError(result.get("reason") or "缺少同品、同规格、同月两厂可比记录。")
        return result, result["overview"], provenance
    if tool == "forecast":
        result = application._forecast_from_tables(scoped, factory=args["factory"], product=args["product"],
            specification=args["specification"], cutoff_month=args["month"], method=args["method"])
        return result, f"已用{args['method']}计算截至{args['month']}的下一月单位成本基线；预测值不是实际成本。", provenance
    if tool == "prepare_report":
        from report.datafill import resolve_period, validate_params
        params = {key: args[key] for key in ("product", "specification", "month", "theme", "use_llm", "formal", "include_benchmark")}
        valid, errors = validate_params(params, d=scoped)
        if not valid:
            raise ValueError("报告表单参数未通过校验：" + "；".join(errors))
        # Validate actual values as well as period completeness, without creating
        # report models, repositories, snapshot records or task suggestions.
        for month in resolve_period(params["theme"], params["month"])[0]:
            _summary(scoped, {**args, "month": month})
        return {"params": params, "validated": True, "persisted_draft": False}, "报告表单参数已校验；仅供填入现有报告表单，未生成报告、未创建或保存草稿。", provenance
    raise PermissionError("操作不在白名单内。")


def _response(plan, *, result=None, answer=None, provenance=None, executed=None, planned_status=None):
    trace = [{"phase": "planned", "tool": plan["tool"], "status": planned_status or plan["status"], "planner": "rule_based"}]
    if executed is not None:
        trace.append({"phase": "executed", "tool": plan["tool"], "status": executed})
    return _json_safe({"schema_version": RESULT_SCHEMA, "planner": "rule_based",
        "status": executed if executed in {"completed", "failed"} else plan["status"], "plan": plan,
        "answer": answer or plan["reason"], "result": result, "trace": trace,
        "provenance": provenance or [], "effects": {"persisted_write": False, "outbound_send": False}})


def execute_request(application, message, context=None):
    """Execute a freshly planned raw request against only hardcoded safe handlers."""
    principal = application.principal
    plan = plan_intent(principal, message, context)
    # Refuse bad/forbidden raw requests before accessing any cost repository.
    if plan["status"] == "denied" or plan["tool"] is None:
        return _response(plan)
    attempted = False
    try:
        tables = {}
        if plan["tool"] != "open_reports" and can(principal, "data.read"):
            require(principal, "data.read")
            tables = _authorized_tables(principal, application.tables())
            plan = plan_intent(application.principal, message, context, catalog=_server_catalog(tables))
        if plan["status"] != "ready":
            return _response(plan)
        # The application may have re-resolved/revoked the principal during the
        # table read; the dispatch check must reject that stale snapshot.
        attempted = True
        result, answer, provenance = _dispatch(application, principal, plan, tables)
        return _response(plan, result=result, answer=answer, provenance=provenance, executed="completed")
    except PermissionError as exc:
        failed = _plan("denied", plan["tool"], {}, str(exc))
    except ValueError as exc:
        failed = _plan("needs_clarification", plan["tool"], plan["arguments"], str(exc)[:300], ("data",))
    except Exception as exc:
        error_id = uuid4().hex
        # Correlate operational failures without logging input, source data,
        # provider messages, credentials, or exception tracebacks.
        _LOG.error(json.dumps({'event': 'agent_operation_failed', 'error_id': error_id,
                               'tool': plan['tool'], 'error_type': type(exc).__name__[:100]}))
        response = _response(plan, executed='failed',
                             answer=f'查询服务暂不可用，请联系管理员并提供错误编号 {error_id}。')
        response['error'] = {'code': 'service_unavailable', 'id': error_id}
        return response
    return _response(failed, executed="refused" if attempted else None,
                     planned_status=plan["status"] if attempted else None)
