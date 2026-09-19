# Prompt与模型评测协议

> 文档性质：当前实现的模型合同与可重复执行的评测协议。实际执行状态以绑定输入、代码版本与时间的受控评测记录为准，不能将本协议初次编写时的静态审查范围套用为项目当前“未运行”。三场景、真实调用/降级、官方mock及人工评分按不同轮次分别留证；本协议不给审核者生成分数，也不以配置连通或单元测试代替业务输出验收。公开源码包不包含私有assessment、原始回复、报告和评分附件。

## 1. 依据、资料版本与评测范围

赛题要求来源为[赛题原文](../赛题原文.txt)及与 inventory 登记哈希一致的[赛题原文提取](../artifacts/requirements/赛题原文提取.txt)：5.1.4 要求三个不同产品/月份场景的报告结构完整度及人工归因评分 0—5 分；5.3.3 要求三步法格式正确率与差异计算误差不超过 1%；第八节还要求 RPA 触发成功率。5.1.3 的月度、季度、专题均应覆盖。6.3 与第十节规定赛题数据仅限本赛、禁止外传。示例中的采购涨价、合同和收率叙事不构成本数据包的标准答案。

资料事实采用[赛题数据与功能验收矩阵](../delivery/赛题数据与功能验收矩阵.md)和[competition_data_inventory.json](../delivery/competition_data_inventory.json)的可定位记录，优先级为：冻结原始记录及业务键 → 独立复算 → 当期源码 → 历史审计说明。发生冲突时登记差异，不能以旧文档的“通过”覆盖现行实现。

| 已读取的元信息 | 可以表述的事实 | 本协议不据此推断的内容 |
|---|---|---|
| inventory `audited_at_utc=2026-09-18T10:35:22.257102+00:00`、`summary`、`files[]` | 该次审计登记正式文件 25 个、排除元数据 5 个；10 个 CSV 共 334 条、7 个 PDF 共 116 页 | 未重新遍历或重新读取赛方原件；这些是历史审计数量，不是当前 managed 中的有效文档数、chunk 数或召回数 |
| inventory `requirements_source` | 提取文本 SHA256 为 `f96e4fb113d7f5deab62cd2d3f4787777c113cc4395c3ec2bb51b6ecc2bd5968`，本次核对一致 | 两份赛题文本排版与行号不同，不混用其行号 |
| inventory `files[].structure`、矩阵 D03/D08/D09 | 登记有三产品、明确规格、两厂 2025/2026 年 1—6 月汇总；一厂材料/人工/制费明细、预算与参考行情 | 没有二厂对应明细、真实采购成交单价、领退料实物量、批次收率或采购合同，不能补造 |
| inventory `implementation_checks.method`、矩阵第 4 节 | 历史记录注明纯计算注入、离线探针；检查并未运行真实模型/检索服务/应用入口 | 702 条数据勾稽、78 个不同回归用例等历史计数不计入本次模型或三场景分母；`implementation_checks_before_fix` 不代表现状 |
| 赛题“给定 3 个场景”、inventory 文件元信息 | 已确认三场景评测要求 | 尚未确认一份由赛方单独指定产品/月份/预期答案的“三场景原件”；第6节为项目选定的三场景合同，具体运行以assessment中的参数为准 |

本文只摘录必要元信息，不复制资料全文。内部 inventory 含原文与明细，不随公开材料分发；对外只引用其摘要和必要元信息。`record_number` 是含表头的记录序号，`line` 仅在源数据明确提供物理行时使用；不得将 DataFrame 索引或记录序号伪称 PDF 页码/CSV 物理行。相关实现：[attribution_facts._source](../attribution_facts.py#L56)、[report.model.source_ref](../report/model.py#L72)、[benchmark._source](../enterprise/benchmark.py#L48)。

## 2. 当前受控 Prompt 注册表与调用链

链接行号来自协议核读快照，函数名/常量名是稳定检索入口；历史指纹与当前交付清单的区别见第10节。提示词、网关或校验器变更后，执行记录必须另存真实指令和源码hash，不沿用旧版本标签作为证明。

| 类别 | Prompt 的真实来源与调用入口 | 实际输入/输出与版本 |
|---|---|---|
| P1 单品月度看板归因（系统模块M2） | [attribution_gen.py](../attribution_gen.py)的`M2_INSTRUCTION`与`_llm_generate()`；入口`generate_attribution()`，校验`_model_errors()` | 初次user数据为`看板波动数据`和`证据`；输出为§3.1三字段。`M2_PROMPT_VERSION=attribution-hypotheses/1.3`，`max_tokens=2000`。如进行唯一一次校验反馈修订，追加`M2_CORRECTION_INSTRUCTION`，user增加`previous_candidate/validation_errors`；修订标签`attribution-feedback/1.0`，实际完整指令hash另存 |
| P2 跨厂差异假设 | [enterprise/benchmark_ai.py](../enterprise/benchmark_ai.py)的`EXPLANATION_PROMPT`加跨厂限定组成`BENCHMARK_PROMPT`；入口`generate_benchmark_analysis()` | 回调收到product/specification/month/analysis_type、facts、schema/prompt/versions/instruction及独立evidence。输出为§3.2五字段；`schema=benchmark-explanations/1.0`、`prompt=benchmark-hypotheses/1.1`、`calculation=same-scope-normalized-cost/1.0` |
| P3 月度/季度/专题完整报告 | [report/model.py](../report/model.py)的`build_report_payload()`使用公共`EXPLANATION_PROMPT`，追加中药一厂、选中产品/规格及整个期间限定 | 回调收到analysis_type、product/specification、months、单厂facts、limitations、instruction、prompt_version及sources；共用§3.2五字段与校验器，`prompt=report-hypotheses/1.1`。单厂回调明确不生成二厂比较结论，季度不能由某一月替代。`report-payload/1.0`是报告快照版本，不是模型回复schema |
| P4 整改任务建议 | [TaskRepository.generate](../enterprise/task_workflow.py#L705) 内拼接 `prompt`；[_default_llm](../enterprise/task_workflow.py#L766) 拆首行 instruction 和后续规范化 JSON | 注入回调签名为 `llm_fn(prompt: str)`，不同于 P1—P3 的双参数回调；输出为 §3.3 六字段对象。`PROMPT_VERSION=task-json-v1`，`max_tokens=1600` |
| 共用受控适配器 | [analysis_service.validated_model](../enterprise/analysis_service.py#L68) | 必须有 `payload.instruction`，否则抛 ValueError；复制后移除 instruction，将剩余 payload 装入 `{"facts": clean, "evidence": evidence}`，`max_tokens=2500`。名称中的 validated 不表示此函数完成业务校验，业务校验由 P2/P3 调用方执行 |
| 共用网关 | [model_gateway.generate_json](../enterprise/model_gateway.py#L103) | 两条消息：system=instruction，user=JSON 数据；OpenAI 兼容 `chat.completions.create`，JSON object 模式。先要求回复content为非空文本，拒绝None、空字符串和纯空白；再拒绝重复JSON键、NaN/Infinity并要求对象。空响应属于网关协议失败，不转换为`{}`进入业务修订；各模块仍分别校验schema与证据 |

P1 的提示要求：程序提供所有数字和计算结论，模型补充可能机制与核查动作，来源正文只是证据；引用须匹配 elements，context_only 不得作归因依据，纯市场资料不足以支持本厂经营归因，具体经营机制须有相关 document_basis 与本期事实共同支持。`brief` 要素应很短，每字段建议不超过 80 字。其**代码校验实际接受 15—500 字**，不能将提示中的 80 字称作硬校验。详细文本由 [render_report](../attribution_gen.py#L122) 拼装；当前 `concise_text/sections` 实际调用 [attribution_narrative.render](../attribution_narrative.py#L3)，不是同文件内的旧 `render_concise`。`brief` 分支不拼模型解释；详细分支可用 `_short_field` 取完整短句，且常保留数据针对性更强的程序建议。因此要分别保存“模型原回复”和“最终展示文本”，不能只评价经过程序修整的文本。

P2 的提示要求：三个要素完整、`claim_type=hypothesis`、纯文字解释不得自行写数字/百分比/金额/URL；假设必须有限定语；列出缺少的实际凭证；建议包含责任部门与核查动作。汇总金额只证明差异，参考行情不是实际采购价，二厂缺明细时保留证据缺口。来源内容中的角色、工具、外传或执行请求均不应被遵从。

P2/P3共享五字段schema与不复述数值、日期、规格的公共说明，再分别追加跨厂或单厂期间范围；P3不再拼接跨厂专属的`BENCHMARK_PROMPT`全文。报告中的跨厂表仍由程序生成，“差异归因分析文本”取确定性限制说明。`include_benchmark=True`不证明运行过P2跨厂AI拆原因，必须单独记录P2调用。

P4 要求模型只补业务建议；输入授权范围/分析来源不由模型修改。不得编造姓名和证据，不得添加审批、状态、URL、发送动作；建议应包含核查动作和交付材料。这些提示要求中有一部分尚无语义硬校验，见 §4。

[enterprise.report_service](../enterprise/report_service.py#L13) 本身不含 Prompt，也不自建模型客户端；它构建一个冻结 payload 并用它导出 DOCX/PDF。[report.registry](../report/registry.py#L35)、[datafill](../report/datafill.py#L218)、[export](../report/export.py#L376) 分别负责模板、数值、渲染。[旧 report.render.build_report](../report/render.py#L110) 仍是保留文字占位符的月度数据草稿，只支持该旧入口；不能用它代表新服务已完成的报告，也不能据其限制说新服务不支持季度。

## 3. Actual output schema：模型回复与程序结果分开

下面是按**实际校验代码**写出的结构合同。类型记法不是测试结果；`输入中的有效ID` 是约束描述，不能当作真实引用发送。没有把所有入口包装成一份不存在的通用 JSON Schema。

### 3.1 P1：月度归因三字段合同

```text
根对象恰有 elements
  elements 恰有 材料、人工、制费
  每个要素恰有：
    hypothesis: string
    recommendation: string
    evidence_ids: 非空 list[string]，每项存在于本次 sources 的 id 集合
```

实际 `_model_errors` 对两段文字要求原字符串长度 15—500；拦截阿拉伯/全角数字、百分号及若干中文数值单位表达，禁止贡献度及“已确认/确定是/主要原因是”等固定用语，禁止 HTML/URL/方括号引用；hypothesis 需含“可能/待核查/待核实/尚不能/不足以”之一；recommendation须匹配明确部门/责任角色，如采购部、生产车间、设备管理部、成本会计，并含核对/核查/复核/检查/排查之一；“采购合同”“设备运行”“生产记录”只是业务对象，不能满足责任主体条件。还窄匹配拒绝把“产量减少导致固定成本摊薄”当作正向解释，保留明确否定这种解释的语句；该规则不等于完整因果语义判断。

最新 [_model_errors 的证据分支](../attribution_gen.py#L84) 还会检查：引用存在后，若证据声明了非空 elements，则必须包含当前要素；拒绝 `evidence_role=context_only`；拒绝所有引用都为 `kind=market_reference`；hypothesis 命中设备故障、停机、加班、工资上调/上涨、收率下降/降低、采购提价/降价、工艺变更/调整时，引用中至少有一项 document_basis。

当前仍未统一做到：重复 ID 检查、缺失 elements 元数据的强制拒绝、完整 kind/source/support_status 合同、所述机制词与具体文档正文的对应检查、所有语义数字和事实错误识别。上述经营机制检查只要求存在 document_basis，不代表正文真的支持这一机制。注入的 `model_fn` 返回 JSON 字符串不会在 `_model_errors` 中自动解析，会因不是 dict 被拒绝；真实网关返回 dict。

服务外壳由 [generate_attribution 返回值](../attribution_gen.py#L348) 生成，主要包括 `text/overview/sections/concise_text`、`sources/source_documents`、`payload`、`alerts`、`used_llm`、`generation_status`、`review_status`、`timings`、`model_run`、`input_provenance/input_data_hash`、`retrieval_stats/index_release_id`、`validation/limitations`。这些不是模型能够直接返回或覆盖的字段。

P1默认执行由`attribution_runtime.run_stage('model', ..., 45)`创建可终止worker；45秒总预算从启动前计时，包含导入、首次请求、校验、可选修订和序列化。只在首个回复已被网关解析为对象、但业务校验失败，且扣除2秒收尾后仍有至少12秒时，使用相同事实与证据，带真实拒绝诊断进行一次修订。每次请求timeout取配置值、40秒和剩余预算的最小值。认证、网络、超时、None/空白/非文本响应、JSON解析或非对象错误直接结束，不作为修订触发；修订失败、不足预算或到期，保留规则结果与真实失败状态，不清洗回复成合格文本。

45秒是模型阶段的执行预算，不涵盖此前独立的检索阶段；到期后终止本次启动的worker并进行有界回收。`tests/test_attribution_repair.py`包含真实PID探针，核对执行前进程存活及超时回收后的退出状态；Windows和Linux按各自运行记录说明覆盖范围，CI合成测试不会被写成真实模型效果。

父进程会再次校验worker候选。`model_run.schema=attribution-model-run/1.0`保留`attempts`、`correction`、`hard_budget_seconds`、`provider_call_count`与执行方式；每次尝试记录初次/修订、实际指令hash、输入hash、可取得的回复hash、诊断、耗时、timeout和`used`。修订成功也保留初次拒绝；超时从临时记录回收安全审计信息，原回复正文不进入错误日志。`provider_call_count`是开始网关调用的尝试数，是否真正发出HTTP还须结合响应/失败证据，不能把该字段等同于成功网络请求。

测试专用`model_fn`注入只调用一次、没有此worker硬时限或自动修订，返回的是普通候选，不能伪装worker审计外壳；注入回调抛出的异常也不采信其自带的`model_run/error_type/timed_out`属性。仅默认worker的`StageExecutionError`提供受控部分审计。正式UI与生产验收均走默认worker（验收`m2_model=None`）；注入通过不计真实业务模型通过。默认结果保存采用后的文本和回复hash，若人工要评价完整原回复，应经授权另行保留原始材料，不能凭hash复原正文。

### 3.2 P2/P3：五字段假设合同

```text
根对象恰有 elements
  elements 恰有 材料、人工、制费
  每个要素恰有：
    claim_type: "hypothesis"
    hypothesis: string，strip 后长度 15..500
    recommendation: string，strip 后长度 15..500
    evidence_ids: 非空 list[string]，不重复
    missing_evidence: list[string]，1..8 项；每项 strip 后长度 2..160
```

[validate_explanations / parse_explanations](../enterprise/benchmark_ai.py#L83) 适用于两种业务。其真实规则为：

- 字符串输入须可被严格 JSON 解析；在**该解析器直接收到原字符串**时拒绝重复键和 NaN/Infinity；检查嵌套深度不超过 15、容器/标量类型、有限数值，根与要素键必须恰好匹配。
- 两个主文字字段禁止数值表达、固定未核实结论词及“已节约/实现节约/可节约/预计节约”等；禁止控制字符、HTML、URL、方括号引用、双花括号和代码围栏。限定语、责任部门和核查动作的判断仍是词表/正则，不能证明语义正确。
- 引用须可定位 `source`（非空 file/table/document_id，或非空 records 对象列表），`kind` 为 `data_fact` 或 `document_basis`，`elements` 包含该要素，`support_status` 为 eligible（缺省按 eligible）。输入证据重复 ID 直接失败。
- `missing_evidence` 禁止 HTML/URL/双花括号/代码围栏；没有与 hypothesis 完全相同的数字扫描，不能将“所有字符串都禁数字”写成当前合同。
- 提及“设备故障/泄漏/事故/停机/工艺变更/配方变更/违规/合同违约”时，至少一项本要素可用 document_basis 正文须包含该机制词；提及采购价/实物单耗/收率升降时需有明确待核查限定。词匹配依然不证明事件与当期因果关系。

P2 的结果外壳 [generate_benchmark_analysis](../enterprise/benchmark_ai.py#L244) 包括确定性对标字段、`schema_version/facts/evidence/sections/assumptions`、`versions/input_data_hash`、`used_llm/generation_status/fallback_reason/review_status/validation` 等。数值句由程序 `fact` 生成，再追加假设、建议、缺口；模型不能给出标准化差额或贡献度。

P3 的冻结报告 [payload](../report/model.py#L508) 包括 `params/period/facts/mapping/sections/tables/sources/benchmark/suggestions/task_drafts`、状态/限制、版本、模板字节、图表、语义 blocks 与 `frozen_hash`。`verify_payload` 核对快照哈希、冻结模板哈希和渲染器版本；导出使用同一 snapshot。哈希只证明冻结内容未改变，不能证明冻结内容的业务真实性。

### 3.3 P4：整改任务六字段合同

```text
模型根对象恰有：
  task_title: string
  assignee: object，仅允许 name、department、role
  priority: "high" | "medium" | "low"（代码也接受 高/中/低 并归一化）
  deadline: 有效 YYYY-MM-DD 字符串
  suggestion: string
  evidence_ids: 字符串列表，是输入 evidence_ids 的子集
```

[LLM_FIELDS](../enterprise/task_workflow.py#L50) 固定六个根字段；[_normalise](../enterprise/task_workflow.py#L86) 与 [generate 的后校验](../enterprise/task_workflow.py#L738) 联合决定接受范围：

- title 非空且不超过 160；suggestion 非空且不超过 8000；assignee 的 name/department/role 归一为文本，各不超过 160，department 必须非空；name 必须与规范化输入的 name **完全一致**。输入没姓名时可以留空，不能由模型擅自补人。
- Prompt 写三项 assignee 子字段，但校验允许缺少子字段并补空；不能将三个子字段均必填当作实际模型硬合同。
- 列表最多 100 项，每项去空白后 1—256 字，禁止 `*`；实现接受 list/tuple、去重排序，允许空 evidence_ids。模型不得添加输入没有的 ID。
- 字符串原回复超过 100000 字符拒绝；规范化完整业务对象 JSON 超过 100000 字符拒绝；文本禁止 NUL。字符串用普通 `json.loads`，没有重复键检测。
- deadline 不提供时分析输入先补当前 UTC 日期后 7 天，成功候选仍须有有效日期；没有“日期一定在未来”或“必须等于输入日期”的检查。
- `source`、`factories`、`analysis_run_id` 来自授权分析输入；模型没有这些根键。生成只创建待审核草稿，不审批、不签发、不调用 RPA。

正式 RPA payload 不是上述模型回复。[TaskRepository._official](../enterprise/task_workflow.py#L147) 增加 `task_id/source/created_at/notify_method="wechat"` 等并由 [rpa_client.validate_payload](../enterprise/rpa_client.py#L50) 校验；official 请求要求非空姓名及部门、完整 source、有效日期和带时区 created_at，`role` 可选，内部 factories/evidence_ids/analysis_run_id 不直接放入官方请求。空姓名草稿可保存，但提交审批时会被拒绝。

## 4. 数值、证据、范围检查与真实拒绝/降级分支

### 4.1 确定性 ground truth 与模型 hypotheses

本协议的数值 ground truth 指“已冻结数据版本上的可复算期望值”，不表示真实企业经营事件已经证实。评测执行前用**独立计算表/独立 Decimal 复算**得到期望值，审核业务键、单位、月份和公式并留来源。不得将被测函数自己的输出再喂给自己作为唯一标准答案；inventory 既有复算可作起点，但必须先确认原件哈希一致。人工语义 ground truth 则须由审核者对具体证据和可接受解释范围标注，本次没有制造根因标签。

| 数值任务 | 固定计算定义与缺失规则 | 源码位置 |
|---|---|---|
| 月度三要素金额贡献 | 令 Q 为产量，c 为要素元/盒：ΔA_e=Q1×c1,e−Q0×c0,e；ΔT=T1−T0；贡献=ΔA_e/ΔT×100%。ΔT=0 时无定义，可有负贡献或超过 100%，不能截断 | [attribution_facts.build_facts](../attribution_facts.py#L151)、[report.datafill._period_amount](../report/datafill.py#L183) |
| 会计金额桥接 | 产量影响=(Q1−Q0)c0；单位成本影响=(c1−c0)Q1；固定先产量后单位成本，交互项归后者，两者之和等于要素金额变动 | [attribution_facts](../attribution_facts.py#L229)、[report.model](../report/model.py#L360) |
| 环比/同比/预算偏差 | (本期−对应基期)/对应基期×100%；月份必须连续/同月/同预算期，基期缺失或零分母为 null/不可计算，不以远月替代、不补零 | [datafill._mom_pct](../report/datafill.py#L60)、[resolve_period](../report/datafill.py#L106) |
| 完整季度 | 自然季度三个完整月份；金额与产量分别求和，单位成本=期间金额/期间产量，不能平均月单位成本。前季度、去年同季分别取各自完整期间 | [datafill._period_values](../report/datafill.py#L159) |
| 人工指标 | 元/盒=人工总额/产量；工时/万盒=工时/产量×10000；时薪=人工总额/工时；效率=产量/Σ(人数×天数)。季度先汇总分子分母，零分母无定义 | [datafill._labor_values](../report/datafill.py#L204) |
| 跨厂 | 同产品、同规格、同月：单位差=c一厂−c二厂；差异率=单位差/c二厂×100%；标准化金额差=单位差×一厂当月产量；三要素加总须闭合，二厂零基准差异率为 null | [benchmark.build_benchmark](../enterprise/benchmark.py#L178) |
| 报告多月跨厂 | 分别算每月同口径差额后求和，保留每月一厂产量；任一月不可比则完整期间 unavailable | [report.model._benchmark](../report/model.py#L282) |
| 告警 | 在未显示舍入前比较成本要素/单位成本变化率绝对值是否严格大于 10%；恰 ±10% 不告警；缺前期/零基期不生成该变化率告警 | [attribution_facts](../attribution_facts.py#L213)、[datafill](../report/datafill.py#L301) |
| 市场参考量价 | 匹配精确名称、2026 年两期、正报价且元/kg，u*=c/P。参考价格影响=Q1(P1−P0)u*0，参考折算用量影响=Q1P1(u*1−u*0)。加产量影响及未匹配残差后闭合 | [attribution_decomposition](../attribution_decomposition.py#L56)、[build_decomposition](../attribution_decomposition.py#L86) |

模型 hypotheses 只能提出待核查机制与建议。市场报价不是本厂成交价，u* 不是实物耗用，理论配方不是领料记录，标准化差额不是节约成果；金额下降也可能由减产引起。二厂只有汇总时，不能输出其原料采购价、工时或收率细项。六味配方、设备折旧、维修与 CSV 变化、GMP 正文与摘要等既有源间冲突见验收矩阵 D10—D13、D18；若引用须保留冲突和页码/片段，不能选一个来源包装成已核实根因。

### 4.2 证据适用范围的实际分层

| 层次 | 当前检查 | 边界与评测要求 |
|---|---|---|
| 授权入口 | [backend_api.create_report](../backend_api.py#L187) 检查一厂报告权限，包含对标时检查二厂 data.read；[api_benchmark](../backend_api.py#L307) 检查两厂 analysis.generate；P1 [generate_attribution](../attribution_gen.py#L278) 在读取计算输入前检查已提供 principal 的权限；任务先 require + `_scope` | 纯计算函数的 evidence 注入不自动获得授权。离线 fixture 不能冒充正式发布知识 |
| 受控检索 | [ControlledSearchEngine.search](../enterprise/knowledge_release.py#L715) 在候选生成前限定身份、产品、工厂、业务 as_of、known_at、有效版本、已发布代与撤销状态，排除 demo/simulation；返回前再排撤销文档 | 必须保存 release_id、chunk/version/document hash、检索模式与降级原因。`vector_bm25_graph` 不能只凭函数名认定成功，需实际 vector_n/bm25_n 与结果证据；当前返回 `reranked=False` |
| LangChain框架适配 | [ControlledKnowledgeRetriever](../enterprise/knowledge_langchain.py)继承`langchain-core==1.6.2`的BaseRetriever，正式context与报告证据通过invoke取得实际Document；绑定服务器Principal及日期/发布/范围，每次复验当前撤销与代际 | `framework`统计保留版本与禁追踪状态。禁止外部回调/配置覆盖，隔离环境继承的LangSmith追踪；不复用旧Document绕过授权。干净环境框架验收使用合成知识，不能代替真实模型效果 |
| 报告/对标适配 | [analysis_service.report_evidence](../enterprise/analysis_service.py#L17) 按每月月末查询，同一 release/known_at 聚合；排除 context_only、按要素词标记，生成 `K`+chunk_id SHA256 前 12 位；保存真正在查询中命中的月份 | `scope.product/specification` 使用请求值，检索并无独立 specification 参数；词匹配不证明资料适用于该规格。月末可用不等于整月事件已发生，正文适用性须人工核实 |
| 外部文档筛选 | [benchmark_ai.filter_evidence](../enterprise/benchmark_ai.py#L163) 要求有效且唯一 ID（禁保留 B/R 数字 ID）、document_basis、可定位来源、正文 1—12000 字、合法 elements；排除 demo/sim/draft/revoked/expired；product/specification 相等；months 覆盖全部所选月份，或生效日期覆盖完整起止区间 | 缺范围即不采用并留 diagnostics。该函数不做身份鉴权，也不独立检查 factory；其授权依赖上游。季度末单月命中不能被扩成整个季度依据 |
| 单品适配 | [knowledge_context.context](../enterprise/knowledge_context.py#L10) 从已发布版本取证，使用 K001 等本次局部 ID，保留 business_metadata/限制/冲突/降级信息；最新 [generate_attribution](../attribution_gen.py#L289) 将数值来源标 accounting_fact、行情标 market_reference、受控文档标 document_basis，并补 elements | P1 有 §3.1 所列要素与用途检查，但仍未采用 P2/P3 的完整 filter_evidence/parse_explanations 合同。文档 elements 按词匹配，未命中词时回退为三要素；不能将每个 K ID 都称为已证实因果依据 |
| 原始回复解析 | 最新 [网关解析](../enterprise/model_gateway.py#L127) 使用 object_pairs_hook 拒绝重复键、parse_constant 拒绝 NaN/Infinity，再把 dict 交业务层；P2/P3 严格解析器也可直接接收字符串 | 网关解析失败包装为 ModelUnavailable，并非业务层 model_rejected。P4 注入回调若直接返回字符串，仍用普通 json.loads，不能把网关保障推广到所有注入路径；分支覆盖以对应合成测试记录为准 |

最新受控检索仍按900字符、150字符重叠切片；1024为本地编码token上限，`truncation=False`，超限拒绝并要求缩小分块后再发布，不能把静默截断后的向量说成全文覆盖。CPU线程/批次与模型文件一起纳入指纹。真实向量的维数、片段数、构建/查询状态和超时结果从本轮发布manifest、检索统计及恢复验证记录读取，不用历史片段数代替当前发布证据。

P2/P3 数字事实 `data_fact`（P1 为 `accounting_fact`）可以支持“存在什么差异”，`document_basis` 可以支持机制的合理性；两者同时存在也不自动证明因果。P3 报告中的市场来源标 `reference_scenario`，不能通过 P2/P3 的原因引用检查；P1 行情是 `market_reference`，不得单独作为经营归因依据。证据 ID 只在某次输入集合内解析，不能跨运行沿用同一个 R001/B003/K001 来证明同一内容。

### 4.3 按真实分支记录结果

| 入口/触发条件 | 实际结果 | 应当如何记录 |
|---|---|---|
| P1 `facts.available=False`（如缺连续上月、歧义/不闭合） | `insufficient_data`，不生成跨月归因 | 记录 reason；不是模型答错或成功降级归因 |
| P1 `use_llm=False` / 无注入且无 key | `deterministic_requested` / `no_api_key`，`used_llm=False` | 原因写真实状态，不称为 AI 文本 |
| P1 知识读取异常 | diagnostics 标“受控知识读取失败”，继续使用可用结构化事实/参考来源 | 单列检索失败；有合法模型回复也不能改写成 RAG 全链成功 |
| 网关回复None/空白/非文本、JSON重复键、NaN/Infinity或非对象 | ModelUnavailable；调用已进入捕获分支时，P1/P2/P3 为 model_unavailable，P4 为 rule_fallback | 与业务 schema 校验导致的 model_rejected 区分；JSON object 请求模式不保证提供方一定输出合格 JSON |
| 配置读取/timeout 校验在调用前失败 | P1 条件判断中的 configuration()、报告/对标应用入口的 configuration() 可直接抛异常 | 不能保证所有配置错误都会生成 fallback；最新 configuration() 要求对象配置、有限且 1—120 秒的 timeout，类型转换错误也应据实记录 |
| P1初次对象回复业务校验失败 | 仅在总预算允许时修订一次；修订通过且父进程复验通过才为`model_validated` | `model_run.attempts`保留首败诊断/hash；修订不重置45秒预算；详情见§3.1 |
| P1最终字段/文字/ID失败，或修订因剩余预算不足未启动 | `model_rejected`、`used_llm=False`；保留程序叙述 | 明确记录`rejected`或`skipped_insufficient_budget`，不隐藏首次失败 |
| P1网关/worker失败、硬预算到期 | `model_unavailable`、`used_llm=False`；超时终止并回收worker | 不因认证/网络/JSON失败再修订；保存安全失败类型和可得的部分attempt审计。注入回调单次且无此worker硬预算 |
| P2 确定性对标 unavailable | `insufficient_data` 提前返回 | 不替代规格/月份/对标厂，不发模型请求 |
| P2 无模型请求/未注入 | `deterministic_requested` / `model_not_configured` | 继续确定性事实与三要素规则核查建议 |
| P2 `BenchmarkValidationError` / 其他调用异常 | `model_rejected` / `model_unavailable` | `fallback_reason`、diagnostics；`validation.model_explanations=not_used`、`semantic_review=required` |
| P3 本期成本/键不完整或明细不闭合 | 生成前抛 ValueError/ReportError，不能得到完整报告 payload | 属数据/报告拒绝，不是模型 fallback；记录异常类型和安全摘要 |
| P3 formal=True 且本期明细缺月 | ReportError，提示可选非正式核查稿 | 不自动把正式请求改成 formal=False；非正式稿缺口须可见。本期汇总不完整不能靠非正式模式绕过 |
| P3 前期/同比/预算不完整 | 保留不可计算与 warnings；前期缺失不输出期间贡献度/桥接 | 正式“本期完整”不代表所有比较期齐全 |
| P3 无模型请求/未注入/ValueError/其他异常 | `deterministic_requested` / `model_not_configured` / `model_rejected` / `model_unavailable` | 数值仍由程序生成，文字用缺口说明和规则建议；`review_status=needs_review` |
| P2/P3 授权或 `report_evidence` 读取在应用入口失败 | 错误可能在进入生成函数前直接传播 | 不能假设与 P1 相同的“RAG 异常自动继续”分支 |
| P4 输入/权限检查失败 | 发生在模型 try 之前，拒绝创建 | 不能把越权伪装成“规则 fallback 已建任务” |
| P4 缺模型/调用错误/六字段或姓名、ID 校验失败 | `generation.mode=rule_fallback`；failure_code 为 `llm_not_configured` 或 `llm_failed_or_invalid`，另有 failure_type；创建待审核规则草稿 | 按 LLMNotConfigured 异常类型划分 not_configured（默认路径在无 key 时抛出）；其他网关失败属于 failed_or_invalid；成功注入 stub 仍可标 mode=ai，评测须另标 stub |
| P4 模型接受 | `generation.mode=ai`、review_required=True；工作流 draft | 不代表已批准、已发送、已送达；role、URL、因果措辞、数值、具体交付材料并无全面语义硬校验，仍需人工审阅 |
| 报告冻结校验/字体/渲染失败 | 校验或导出异常 | [verify_payload](../report/model.py#L691) 和 [export](../report/export.py#L28) 不会把损坏快照/缺字体默认为导出成功；DOCX 成功不能补写 PDF 成功 |

P1—P3 的 `model_validated`、`validation.model_explanations=passed` 指本入口自动检查通过，**不表示真实性、完整 RAG、人工归因合理性或全项目验收通过**。数值输出使用 `numeric_facts=program_rendered` 表明来源是程序，也不是独立数值准确率的实测结论。

## 5. 模型版本、配置、数据外发许可与失败留档

### 5.1 当前网关实际配置

[model_gateway.configuration](../enterprise/model_gateway.py#L41) 支持 `COST_LLM_CONFIG_FILE` 指定配置文件，缺省 `.local/llm.json`；base_url 优先 COST_LLM_BASE_URL、DASHSCOPE_BASE_URL、本地配置，再默认 DashScope 兼容地址；model 优先 COST_LLM_MODEL、本地配置，再默认 qwen-plus；key 优先 COST_LLM_API_KEY、DASHSCOPE_API_KEY、本地配置；approved_cloud 来自 COST_LLM_APPROVED_CLOUD 或配置。本文未读取环境密钥或本地密钥文件，**默认值不等于实际运行模型**。

| 配置项 | 当前代码值/规则 | 每次评测须登记 |
|---|---|---|
| API/采样 | OpenAI 兼容；`response_format={"type":"json_object"}`；temperature=0.1；未设置 seed/top_p | 请求的模型 ID、实际返回模型版本（取得时）、服务商/本地部署版本；无法取得精确版本写 unknown，不将别名当固定权重 |
| 输出上限 | P1 2000、P2/P3 经适配器 2500、P4 1600；网关 `min(max_tokens,5000)` | 入口实际值、截断/finish_reason（若可取得）；当前网关未把响应模型名/usage/finish_reason 返回业务层，不能虚构日志 |
| 时限与并发 | configuration() 默认 timeout 40 秒，要求有限且 1—120 秒，超界拒绝；进程内 BoundedSemaphore(2)，最多等 1 秒；SDK `max_retries=0` | 实际 timeout、worker 45 秒额外上限是否适用、是否并发、调用开始/结束/错误；不能称全系统只有两个并发；直接注入 ModelConfiguration 不等于经过 configuration() 的配置校验 |
| 地址 | URL 禁 userinfo/query/fragment；loopback 可 HTTP/HTTPS；非 loopback 必须 HTTPS 且 approved_cloud=True；禁自动跟随重定向 | 去凭据端点或批准记录代号、local/cloud、批准适用的数据类别和期限 |
| 网络环境 | loopback `trust_env=False`，批准云端保留代理/CA 环境 | 只记录必要代理/CA策略，不保存包含秘密的完整环境 |

`approved_cloud=True`是技术门闩，不能替代覆盖接口和输入范围的外发授权。已经取得的用户授权按其明确范围执行并留记录，不因本协议早期静态编写阶段的限制重复要求授权。真实本地推理、获准云端请求和无模型确定性计算分别标注；源码公开授权不包括将业务数据、原始回复或报告附件上传公开仓库。

### 5.2 版本与审计记录最小集

每次运行分配独立 `run_id`、`scenario_id`、入口、时间、操作者/审核者、数据授权范围。每条原始尝试单独登记，不用最后成功样本覆盖失败。至少保留：

| 记录组 | 必须保存的内容 | 当前产品记录与补充边界 |
|---|---|---|
| 输入 | 源文件哈希、表/业务键/单位、months、完整性、独立期望值版本及审核人、输入 data hash | P1/P2 有 input_data_hash；P3 versions.data_sha256；禁止只写“官方数据” |
| Prompt与修订 | 实际system instruction SHA256、prompt_version、源文件hash、参数/证据输入hash，以及初次/修订的分别记录 | P1的`model_run.attempts`保存prompt_version、instruction/request/response hash、诊断与采用状态；不保存provider原错误正文。P2/P3主要保存标签。P4的prompt_hash及provenance.prompt_sha256覆盖“指令＋换行＋规范化分析输入”整段，不能误称纯system指令hash |
| 模型 | request 模型名、response 模型/固定部署版本（可得时）、温度、token 上限、timeout、网关版本、真实/注入标记 | [provenance](../enterprise/model_gateway.py#L148) 只给模型、端点、prompt_sha256、temperature、response_format；P4 持久化时剔除端点；注入仅记 provider=injected_llm_fn；缺失字段补评测台账，不能声称产品已全量保存 |
| 知识 | release/version/chunk/document SHA256、as_of/known_at、scope、evidence IDs、检索统计与降级原因 | P3 versions.knowledge 并非完整检索运行日志；不足字段须评测另存，不反向补造 |
| 输出 | 原始回复可用时的受限存档和哈希、解析回复、诊断、程序最终文本、报告 ID/frozen_hash、导出字节哈希 | 当前网关不返回原始响应包；未取得的内容记未采集。原文含赛题资料时只能放授权本地档案 |
| 失败 | 阶段 data/retrieval/gateway/schema/render/review/rpa、状态、失败类型、安全摘要、是否发出请求、是否采用 fallback、耗时 | 网关只暴露 `ModelUnavailable` 安全摘要及底层异常类型；P2/P3 不总有结构化原始 failure_type，按真实可见字段填写 |
| 人工 | 原始独立分、逐项理由、引用定位、分歧裁决、审核日期 | 程序不能替人打分；没有审核写“待评”，不是 0 分 |

日志不保留密钥、Authorization、完整环境变量、含凭据 URL 或第三方异常原文。[网关异常处理](../enterprise/model_gateway.py#L141)、[task generation](../enterprise/task_workflow.py#L727) 已有部分脱敏设计，但仍需检查自建评测日志。`injected_version_unspecified`/`not_configured`/`injected_llm_fn` 都不是可复现实模型版本。

## 6. 三个主场景：选型与逐轮执行合同

以下选型依据资料覆盖月度、季度、专题及三种产品，已用于项目的实际验收场景；它不是赛方另行提供的专用标准答案清单。每轮以自己的`run_id`、输入版本和独立复算期望值为准。若取得赛方指定场景，以经确认原件更新参数并保留来源与hash；资料缺口明确登记，不用模型生成材料填补。

| 场景 ID | 产品/规格与主题参数 | 覆盖与需要核实的边界 | 必须留存的输出 |
|---|---|---|---|
| C1 | 银黄口服液 / 10ml×10支/盒；month=2026-05；theme=月度成本分析 | 5 月对连续 4 月、去年同月与预算；材料/人工/制费金额及参考行情区分；P1 月度看板 + P3 月报 | P1 结果；P3 一个冻结 payload 及 Word/PDF；有效知识引用；人工评分；一条经人工指派和审批的任务 |
| C2 | 板蓝根颗粒 / 10g×20袋/盒；month=2026-06；theme=季度成本分析 | 按 resolve_period 得 2026-04—06，前期 01—03、同比 2025-04—06；季度产量加权、三个自然月证据完整覆盖；不得用六月解释替代季度 | P3 季报 Word/PDF 与三个月分项；缺口/范围审阅；人工评分；一条来源明确为季度的任务 |
| C3 | 六味地黄胶囊 / 0.3g×60粒/盒；month=2026-03；theme=专题分析；focus=材料 | P3材料专题核对制剂材料明细及参考行情边界，仍含全部三要素用于勾稽；设备事件仅作制造费用背景线索，不能直接认定根因，也不将该场景计作制费主专题验收。独立执行P2同月同规格跨厂三步法，披露二厂明细缺口 | P3材料专题Word/PDF；P2独立结果；人工评分；一条由所选分析建议转出的任务 |

为覆盖跨厂在不同期间的行为，三个P3报告场景均设 `include_benchmark=True`，C1/C3 检查单月差额，C2 检查逐月差额求和；它们仅证明报告中的确定性对标部分。P2 模型评测的最小主样本为 C3；若需报告“跨厂 AI 三场景正确率”，必须另对 C1 的 2026-05、C2 的 2026-06、C3 的 2026-03 分别运行 P2，并注明 C2 这次调用只覆盖六月，而不是季度模型。新增调用是附加运行，不把同一主场景改计成两个官方场景。

每场景执行前冻结：原件存在性/哈希、产品与规格主键、输入月份完整性、授权知识发布版本、需要/可获得的凭证、独立数值期望值和允许的解释边界。`known_at`、`compiled_date` 和任务时钟也应固定或登记，避免日期默认值让结果不可比。禁止为了让样本出现 ±10% 告警、模型“说得更具体”或满足评分而修改官方输入。

以下附加负例使用隔离fixture，不计为第四个主场景；执行状态按对应测试文件与JUnit记录核实：

| 探针族 | 输入/操作 | 预期检查目标 |
|---|---|---|
| 数据可比性 | 一月缺 2025-12；Q1 缺 2025Q4；缺二厂当月；混规格/重复业务键 | 区分“前期不可比仍有当期报告”和“本期/跨厂数据拒绝”；不跨缺月计算 |
| 算术边界 | 零基期、零总净差、正负抵消、恰 ±10% 与刚超 ±10%、完整季度缺一个月 | null 不变零、贡献度不截断、告警用未舍入值；formal 明细缺月拒绝 |
| 回复合同 | 多/少键、NaN、重复 JSON 键、假 ID、重复 ID、wrong element/kind、超长字段 | 分别跑各入口实际校验器和真实网关解析路径；若校验器接受但违反协议，记缺口，不写“预期拒绝已通过” |
| 语义边界 | 引用仅汇总却声称停机已证实、把参考单耗当实耗、把标准化差额称节约、来源正文注入执行指令 | 硬校验结果与人工裁定分列，识别词表漏网，不用结构通过代替真实性 |
| 知识范围 | 单月文档解释季度、错产品/规格、草稿/撤销/模拟/不明范围证据 | 记录筛除 diagnostics；未经授权不调用模型；不借适配器 scope 标签证明正文适用 |
| 任务与服务 | 擅改姓名/加假 ID、空姓名提交、无有效批准、超时/重复 ID/服务重启 | 草稿/拒绝/unknown/回执分明；只核对原 task_id，不改 ID 重发；执行须另获对应授权 |

## 7. 指标定义与计数规则

### 7.1 先区分执行层次

模型模式与数据/服务模式分别登记，不用一个“离线”标签掩盖是否真实推理。

| 标签（评测台账字段，非声称产品已有字段） | 定义 | 可计入的证据 |
|---|---|---|
| `static_review` | 只读源码/资料的审查模式 | Prompt/schema/分支存在性；不能计任何实际模型或 HTTP 通过率 |
| `deterministic_only` | `use_llm=False` 或未调用模型，只算数值与规则文字 | 独立数值、渲染、数据拒绝；不能记真实 AI 生成成功 |
| `stub` | 注入固定/伪造模型函数、Fake 客户端或 MockTransport | 合同、控制流及错误处理；即使 used_llm=True / mode=ai 也不计真实模型样本 |
| `offline_replay` | 对冻结输入/真实已存输出离线重放或复算 | 重放一致性；模型原运行来源必须可追，不能把重放算作新模型调用 |
| `real_local` / `real_cloud_authorized` | 确认真实推理，保留运行与模型证据；云端另有数据许可 | 各自模型质量/耗时/接受率；离线本地推理仍属于真实 AI，不等于 deterministic_only |
| `official_mock_http` | 经核实版本的赛方 mock 进程上的真实 HTTP 请求/回执 | RPA 模拟触发与状态追踪；不是模型质量，也不是生产微信送达 |

当前客户端 [RPAClient](../enterprise/rpa_client.py#L118) 只允许 loopback 8090 的 mock I/O，production 调用明确禁止。`MockTransport` 和 official_mock_http 分开；只有真实服务与原始请求/响应证据才能冠以后者。

### 7.2 分母、精度与通过定义

三场景的四条业务路径合计12个模块结果，不一定只有12次模型请求。P1的校验修订最多增加每场景一次网关调用；逐次尝试、取得有效回复和最终模块采用率分开记录，重试后的最终合格不能把初次拒绝从请求分母删除。`model_used.used_count/attempted_modules`是模块口径；HTTP发出数按实际证据统计，不能仅凭调用函数已进入而推断请求已经出网。

所有比率同时报告 `numerator/denominator`、模式、场景集合和原始失败数。分母为 0 时记 `N/E（未执行）` 或 `N/A（该事实无定义）` 并说明，不写 0%/100%。未开始的计划样本不被伪造成失败样本；另报已执行覆盖数/计划数。已经开始却未产出结果的样本不能从相应成功率分母删除。

| 指标 | 定义与判定 |
|---|---|
| 主场景覆盖率 | 完成所要求交付与留证的主场景数 / 3；每场景报告、人工评分、任务服务证据分别列完成状态。只生成 JSON 不计 Word/PDF 双格式已完成 |
| 报告结构完整度 | 固定六个主章：封面基本信息、总成本概览、成本要素明细、重点产品专项、对标、总结建议，来自 [report.model._blocks](../report/model.py#L620)。单格式=有标题且有本场景有效内容的章节数/6。已开始 E 个场景的双格式整体=章节命中总数/(E×2×6)；已开始却未导出的格式贡献 0，不删除。三场景双格式全部进入执行时，分母为36；实际分子从本轮导出检查读取。合法缺数说明可作内容，残留占位符/空标题不算 |
| 模板与导出检查 | 冻结模板 `parse_template` 得到的不同占位符集合中有合法映射的个数/总个数；另列 DOCX/PDF 残留 `{{…}}` 数、表格/图表/中文/页眉页脚可读性。`fully_mapped` 不是视觉合格证据。双格式一致性按同一 frozen_hash 的业务字段/章节比较，不要求二进制相同 |
| 数值正确率 | 预先列出独立期望值的字段集合 G；正确字段数/可定义字段数。定义本期总额/三要素金额、单位成本、贡献度、桥接、同比预算和跨厂字段；缺输出、单位或方向错误计错。金额闭合固定 0.01 元，不乘产量放大；跨厂 exact 标准化分项按 Decimal 要求代数闭合；展示舍入与原始精度分开 |
| 跨厂差异准确率 | 对非零标准答案 g：相对误差=abs(y−g)/abs(g)，≤0.01 为满足赛题误差要求；准确率=满足字段数/预定可算跨厂字段数。g=0 不除零，单独按金额 0.01 元、单位成本差 0.01 元/盒、差异率 0.01 个百分点容差判定并报告 zero_case_count；同时报告最大误差，不能只报平均误差。期望值和被测显示值须在相同预登记精度下比较 |
| 无定义处理正确率 | 应为 null/不可计算的字段中，实际保留无定义并说明正确原因的个数/该类字段数；把缺数补零或用远月替代计错。独立于数值正确率报告，防止通过剔除缺期美化结果 |
| 逐次模型结构/业务合同通过率 | A为对应模式记录的网关尝试数（初次与修订均计），J为取得可校验回复数，V为按该入口规则通过数；报告V/J、V/A、J/A及初次拒绝/不可用原因。另列有证据确认发出的HTTP数，调用前拒绝不记为已发HTTP；schema与完整文字/引用规则可分列，真实模型和stub不合并 |
| 模块最终采用率 | 最终`used_llm/model_used=True`的业务模块路径数/本轮实际执行的请求模型路径数；三场景四模块为12条，不能作为逐次请求总数。M2修订成功可以提高最终采用率，但首次拒绝继续计入逐次结果 |
| 数字越权率 | 在模型原回复 hypothesis/recommendation 中违反其无数值合同的条数/实际收到的解释条数；程序渲染数值不算越权。P4 没有该禁数字合同，改核对其引用的事实是否与输入一致。正则命中与人工发现分别列 |
| 引用有效率 | 以输出、要素、ID 为一个去重引用单元：同时满足解析、当次版本、要素/范围/证据类型且经人工确认能支持对应句子的引用单元数/全部引用单元数。重复 ID 仍单列 schema 错误；没有输出引用不报满分，按入口非空要求另判失败。程序结构有效率与人工语义支持率分列 |
| RAG 可追溯与相关性 | 每条被引用 chunk 能定位原文和版本的比例；如评检索质量，先由人工在冻结授权语料标相关集合，再算 Precision@k=前 k 个槽位中相关且范围正确的去重 chunk 数/k（不足 k 的空槽不计相关）、Recall@k=命中相关集合数/人工相关集合总数。未标注相关集合时不得编造 Recall。记录 vector_n、bm25_n、retrieval_mode/degraded，不把 fallback 当完整混合检索 |
| 三步法格式正确率 | 已执行且确定性可比的 P2 运行中，同时满足：范围完整，差异表三要素必要字段齐全；分项与总额闭合且一厂下钻/二厂缺口明确；第三步含三要素假设、建议、可解析引用、missing_evidence 且无自相矛盾 的运行数/该集合运行数。不可比负例另算拒绝正确率；另列使用真实模型的完整三步运行数，规则 fallback 即使结构完整也不能计“AI 三步成功” |
| 人工归因合理性 | 按 §8 获得具名人工 0—5 分；每场景展示原始分、裁决分及理由。均分=实际已评场景裁决分之和/已评场景数，同时列已评数/3；待评不当零分，也不能只公布高分样本 |
| 性能/稳定性 | 各模式分别统计尝试数、成功/拒绝/不可用数、检索/模型/总耗时。三例只报告逐例与范围；如需分位数，预先规定重复次数和算法。不得反复重跑挑最好的一次作为唯一结果 |

### 7.3 官方 mock 计数：任务、HTTP 尝试、通知分开

赛方 HTTP 200 的触发要求与客户端更严格的 `code=200`、data.status 合法、task_id 匹配检查同时保留。一次 POST 已模拟通知，应用不另调用微信接口。[工作流](../enterprise/task_workflow.py#L626) 先 GET 核对，遇到不确定受理、超时、重复 ID、mock 丢失历史时按原 ID 对账；不确定 POST 后 GET 404 不构成重新发送许可。

- **POST 尝试成功率**：收到 HTTP 200 且通过响应/ID 检查的 POST 次数 / 实际 POST 次数。记录全部 attempts，不把 GET 算 POST；重复 ID 的 400 不是成功 POST。
- **任务触发确认率（主报告口径）**：有匹配有效 POST 回执，或经 GET 完整内容核对后确认受理的不同批准任务数 / 已签发并进入本次派发验证的不同任务数。按 `(tenant_id, task_id, approved_version)` 去重；重试不增加任务分母，reconciled_accept 单列，unknown/failed 不计成功。
- **场景触发覆盖**：已确认至少一个任务受理的主场景数 / 已进入服务验证的主场景数，另展示计划 3 场景的执行覆盖；不能用同一场景多任务代替另一场景。
- **模拟通知/送达/确认/完成**：分别依据 notify_status 及 GET 观测的 status/status_history 统计不同 task_id；原始状态计数与“达到某阶段”的累计口径分开。没有相应历史/回执不能按等待时间猜测。任务 `accepted`、mock `sent/received/confirmed/completed` 不等于真实责任人微信已收到或业务整改完成。
- **失败恢复**：按失败类型记录是否产生重复远端任务、是否进入 unknown、查询次数、最终 matching receipt。服务重启失忆仍保留本地证据，不能以更换 task_id 刷高成功率。submit/approve/enqueue 及审核人均须真实授权；生成任务 JSON 本身不算触发。

当前 [TaskRepository._view](../enterprise/task_workflow.py#L269) 会显式给出 simulated=True；[rpa_client.receipt_matches](../enterprise/rpa_client.py#L227) 对查询回执核对批准内容。后续服务执行应核实 mock 源文件哈希，inventory 登记的赛方 mock SHA256 为 `b6fce8123830cba27b27f167b7ee2c8cdb905d878a8dff65683aeadf676a5d1e`；实际服务启动记录、端点与源码hash需在对应运行中复核，不能仅凭本协议的历史hash声称当前服务版本一致。

## 8. 人工归因合理性 0—5 rubric（不给虚构分数）

评分单位是一个完整场景的归因文本及其证据，而不是字数、流畅度或 API 成功。审核者先查看冻结数据、对应知识原文、模型原回复和最终报告。模型原回复与程序成稿的评分应标明评价对象；规则 fallback 可以评价“规则稿质量”，不得冒充“真实模型得分”。本协议建议两位实际财务/生产或工艺审核者独立评阅，保存姓名/岗位/日期；如只有一人，如实记录单人评阅。

采用五项可核对的二元分，每项满足全部条件得 1，否则得 0，并必须写出通过/未通过的句子和证据定位。总分为五项之和，整数 0—5；未读证据或未实际审核写“待评”，不能填 0。

| 维度 | 得 1 分的可操作条件 | 得 0 分的典型表现 |
|---|---|---|
| A 成本要素与明细定位 | 材料/人工/制费均有对应说明；主导或专题要素按冻结计算选取；有明细时点到实际原料/费用/工时项目，缺明细时准确说明缺口 | 只说“成本变了”、找错主导项、把不存在的明细作为定位依据 |
| B 工艺/行业知识引用 | 至少一项适用的工艺/行业/设备依据可从 ID 定位到真实原文/版本，审核者能指出其支持哪一句机制；产品/期间/范围不冲突，已知源冲突被披露 | 引用无关 GMP 口号、串产品、假引用、用参考市场价证明本厂采购涨价；没有可用知识依据则此项不计分，但诚实披露可获得 D 项 |
| C 因果推理与会计口径 | 每项重点解释与差异方向、产量/单位成本桥接相容；区分核算分配、产量变化与经营机制，不能凭金额涨跌认定效率好坏 | 减产金额下降写成降本、相互抵消写成同向上涨、把对标差额称已实现收益 |
| D 不确定性与缺口处理 | 经营原因标待核查；明确需要哪些实际凭证；参考单耗/实价、理论/实际、单月/季度、已知冲突均没有越界 | 写“已证实采购/收率/故障导致”而仅有汇总或文档背景；静默掩盖缺月/二厂缺明细 |
| E 建议可执行 | 重点问题对应明确责任部门/岗位、核查动作、具体合同/台账/批次或凭证，以及可交付核查结论/证据清单；不直接作未经批准的工艺或发送决策 | “加强管理/持续优化”等空泛表述、无负责人范围/凭证/交付物、建议与发现无关 |

0 分表示五项均未满足，或触发下面的严重失实规则；1—4 分分别表示满足 1—4 项，必须同时展示 A—E 得分向量，不能只用“较好/很好”代替理由；5 分必须五项全部满足且无严重失实。这保证不同审核者能从相同证据复核分数，不奖励未经证实的“深入根因”。

**严重失实规则**：编造证据/合同/事件，把模型数值冒充确定性事实，或使用明知越权/错产品错期间的内容支撑核心结论，须标“不可采纳”，本次已审核文本总分记 0，并保留逐项原分与触发证据。一般表述不够具体只扣对应项，不扩大成造假。两人任一项判断不同或对严重失实有分歧，逐项对照原文裁决；保留初评分和裁决理由，不由程序/另一个模型自动充当具名人工评分。

| 场景 | 输出模式/评价对象 | 审核人及日期 | A/B/C/D/E | 总分/裁决理由 |
|---|---|---|---|---|
| C1 | 以选定run_id的冻结报告与模式为准 | 待人工评阅 | 待评 | 待评 |
| C2 | 以选定run_id的冻结报告与模式为准 | 待人工评阅 | 待评 | 待评 |
| C3 | 以选定run_id的冻结报告与模式为准 | 待人工评阅 | 待评 | 待评 |

具名记录由`scripts/validate_human_scores.py`核对assessment同目录的`human_scores.csv`，其两项总分允许有限0—5小数；如采用上述五项rubric，分项及裁决依据应在意见或受控评审附件保存。CSV中的空分保持null，不自动转为零；半填行、不合规时间、占位审核人或缺乏具体意见会被拒绝。`--require-complete`在合法但待评时退出2，完整时退出0，非法时退出1。工具保留报告frozen_hash与输入hash，只新建私有结果文件，不改变输入或模型结果；`identity_verified=False`表示它没有认证审核人或验证意见真实性。

## 9. 后续执行与结果登记模板

以下步骤用于首次执行和每次重跑。既有授权在其明确范围内持续有效；执行结果单独保留，协议不代替运行日志或人工评分。

1. 确认官方场景原件或签认候选；核实资料路径/哈希、完整性与权限；冻结测试集合、预期字段、算法/容差、实际 Prompt、模型/知识发布和配置。未知信息明确填 unknown，不先写结论。
2. 在隔离输入上独立复算数值及无定义字段，人工确认期望值；执行 deterministic_only 与合同负例，分别留证。不要调用默认 loader 去读取未获授权的 managed 当前版本。
3. 仅在对应执行/外发授权已具备时运行真实本地或云端模型；每次尝试留状态和输出，不静默改回复再称原始合格；出错按 §4 真实分支登记。
4. 每场景冻结一份报告并双格式导出，检查章节、占位符、表/图/字体、哈希和最终可读性；分别核对原回复、拼装结果和证据。
5. 完成具名人工评分；需要任务服务验证时人工指派、审批和签发，验证官方 mock，记录 POST/GET、版本、task_id 与回执；不触及生产微信。
6. 按模式公布分子/分母、逐例失败/降级、评分和原件定位。重跑保留 attempt_no，声明首次与重跑结果；不得删除失败或把历史回归并入当前 AI 样本。

建议每条台账至少包含：

```text
scenario_id, run_id, attempt_no, entrypoint
product, specification, factories, months, source_manifest_hash
expected_values_version, expected_values_reviewer
execution_mode, model_mode, retrieval_mode, service_mode
prompt_version, actual_instruction_sha256, code_hashes
requested_model, resolved_model_version_or_unknown, generation_settings
authorization_record_id_or_not_applicable
knowledge_release_id, as_of, known_at, evidence_manifest_hash
request_started_at, finished_at, stage_timings
provider_request_issued, generation_status, used_llm, fallback_reason
schema_result, reference_result, numeric_result, human_review_status
safe_failure_stage, safe_failure_type, safe_failure_summary
report_id, frozen_hash, docx_hash, pdf_hash
approved_task_version, task_id, post_attempts, receipt_status, receipt_hash
reviewer, review_date, rubric_items, score_or_pending, review_reason
```

执行证据定位表（各轮独立，历史失败保留）：

| 项目 | 结果依据与边界 |
|---|---|
| 受控Prompt/schema/调用链及拒绝分支 | 源码合同与对应测试；测试覆盖不等同模型业务质量 |
| LangChain框架与干净环境 | 内部`.artifacts/langchain_adapter_clean_verification.json`记录真实BaseRetriever/Document、撤销复验、禁追踪和零网络尝试；使用合成知识 |
| 三场景报告、独立复算及官方mock | `delivery/private/scenarios/<run_id>/assessment.json`绑定场景、frozen_hash、双格式检查和回执；已存在执行结果，不能再统一标为未执行 |
| 历史模型降级轮 | `20260918T163611Z-e901e56f`记录模型有效采用0/12、三场景导出与3条模拟任务完成；该轮仅为保留的历史证据，不覆盖后续重跑 |
| 当前模型结果 | 从最终交付索引指定的新run_id读取实际采用数、拒绝/不可用原因与输入版本；单次连通probe不能补写为四模块有效采用 |
| 人工归因/对标评分 | 选择具体assessment、审核对应冻结内容后，由真人填写并用人評脚本校验；未评分保持null |
| 公开CI与生产现场 | 合成测试按公开仓库提交的Actions记录核实；生产OIDC/容器与真实业务效果分别验收 |

## 10. 历史核读指纹与当前快照

下表是协议初稿核读时保存的历史SHA256，保留用于追溯，不表示现在的源码字节仍与初稿相同。LangChain接入、提示词或导出修订后，当前文件hash以源码包`SOURCE_MANIFEST.json`、外部`delivery/source_bundle_manifest.json`及具体运行记录为准；执行时应冻结实际指令和源文件，不能使用本表旧hash替代。源码链接使用相对路径，行号会随修改变化。

| 文件/资料 | SHA256 |
|---|---|
| [enterprise/analysis_service.py](../enterprise/analysis_service.py) | `0229189b4415bb966819bbed487640e8f5d161d5dbb6cc66ea2c0b433754e3bb` |
| [enterprise/benchmark_ai.py](../enterprise/benchmark_ai.py) | `cc42fb40e283fa4627179d6674bd54aa736df4fe396f24e9989e739241b6100b` |
| [enterprise/report_service.py](../enterprise/report_service.py) | `82f45368262326b942d14c420387aac7cac0bc4e791e7e9f855e4742a95713f9` |
| [enterprise/task_workflow.py](../enterprise/task_workflow.py) | `466787e2b82beedea2016d3962a36a839d3243759f909c5d67d9cab6ada738ee` |
| [enterprise/model_gateway.py](../enterprise/model_gateway.py) | `0f8721e05b55851cade6d1476a237d35643c4559a48c89e5565c6ccf45553c6d` |
| [attribution_gen.py](../attribution_gen.py) | `bb2316a961b9460c43241e20992cb38cf124cd079578ebaf091fcbdcd3961545` |
| [report/model.py](../report/model.py) | `beb72586f65b0f4e635357fcd5bdba68e6ce0b8a24a2ca830fc3c18501cfff78` |
| [report/datafill.py](../report/datafill.py) | `e6272ba5af7080d35d95034be96e400089d221307fa29cf995362fc14521b312` |
| [report/export.py](../report/export.py) | `f7510a891f3d3a5bae5cc1bd639d3e993913f5c00f11744ee72815b9b3751ae8` |
| [enterprise/benchmark.py](../enterprise/benchmark.py) | `6d2d15495ed4259a1a1c324ba0efd0c259d975bb8097e0819e89fd2d249c5352` |
| [enterprise/knowledge_release.py](../enterprise/knowledge_release.py) | `00afebf120c68c645d99f0c3712685faea472782ff9c17392bbf969f9aa16867` |
| [enterprise/rpa_client.py](../enterprise/rpa_client.py) | `80c4d97ec2a678783c86b389aa8f17297127a3fbcc1c7c514f82fd9dc84c8335` |
| [delivery/competition_data_inventory.json](../delivery/competition_data_inventory.json) | `e0f48ee4b4783822c2f0bec955e3791d1cc6e1abe08460287ed2fe03c21c5174` |
| [delivery/赛题数据与功能验收矩阵.md](../delivery/赛题数据与功能验收矩阵.md) | `f396f703357f61507084b69c45be896bf15ef5c86eb85261fc42d4a9044eeb51` |

## 11. 可直接引用到 PPT 的模型合同与边界

- **模型只补充待核查解释与行动建议，数值由确定性计算和程序渲染产生。** 月度三字段：`elements.{材料,人工,制费}.{hypothesis,recommendation,evidence_ids}`；对标/期间报告五字段：再有 `claim_type="hypothesis"`、`missing_evidence`。三种输出合同不能混称一个通用 schema。
- **任务模型只输出六字段草稿：** `task_title, assignee, priority, deadline, suggestion, evidence_ids`。来源、工厂范围、任务 ID、审核和发送由业务工作流掌握；空姓名不能提交为官方任务，生成不等于发送。
- **自动校验通过不等于根因已证实。** 数字禁写、证据 ID/要素/期间等检查的强度随入口不同，最终语义合理性由具名人工按 0—5 rubric 评价。
- **会计差异、参考情景、经营原因分开。** 市场参考价/折算单耗不是采购实价/实耗，跨厂标准化差额不是已实现节约；二厂缺少明细，不能编造量价、工时或收率根因。
- **报告中包含跨厂表，不代表做过跨厂 AI 三步法。** 跨厂模型评测须独立调用 P2 并留记录。
- **协议与运行结果分别留档。** PPT统计应从最终选定assessment、JUnit、恢复核对和视频元数据读取，保留输入hash；真实模型、规则降级、stub、人工评分与官方mock各列分子/分母，不用早期静态审查状态覆盖后来执行证据。
