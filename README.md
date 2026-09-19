# project4 制药成本分析工作台

这是单组织、单实例的管理会计辅助系统源码，提供智能报告、产品成本、跨厂对标和整改任务。正式工作台入口是 `enterprise_app.py`，HTTP API入口是 `backend_api.py`。兼容入口 `admin_web.py`只通过runpy转入同一企业身份导航，旧的直接上传/Chroma管理页面不可从此入口访问。UI与API复用已认证Principal及应用服务，SQLite保存版本、审批、分析快照、任务outbox和模拟回执。

**公开范围限于逐文件白名单源码。** 维护者已明确授权将该范围（含少量中文产品名、字段合同、数值例与文档摘要）发布到 `yinyinglv0-lab/pharma-cost-workbench`。项目尚未选定自身开源许可证，源码公开可见不等于取得通用复制、修改或再分发许可；权利仍由相应权利人保留。赛题原件、业务数据、报告模板、私有评测记录及媒体附件不属于本仓库的公开分发范围。打包器只创建本地归档，仓库发布和CI运行结果分别留证。

## 1. 包含与排除

包中只保留打包脚本显式列出的正式Python源码、ECharts资源及第三方通知、五份当前技术/操作文档、部署和空配置示例、依赖清单、九个经过静态筛选的测试、测试隔离文件及 `.github/workflows/ci.yml`。

下列内容**不在源码包内，也不由打包脚本读取**：

- 赛方CSV/XLSX/PDF/DOCX原件、报告模板、赛题正文与官方mock附件。
- 成本/知识原件副本、向量和图索引、SQLite数据库、备份、已生成报告、PPT、视频、审计明细JSON和原文提取。
- `.local`、真实`.env`、真实Streamlit secrets、实际授权用户映射、模型配置与密钥、日志。
- 字体、模型权重、虚拟环境、缓存、历史备份、个人review文档和无关目录。

文档对受控原件清单、内部验收矩阵和截图/报告的相对链接可能指向包外材料，表示按授权另行交接，不是遗漏了应当公开的数据。`deploy/authorization.example.json`只有空users，`deploy/streamlit.secrets.example.toml`只有空值；`.env.example`也是非秘密结构样例，这三项是配置文件白名单例外，不能据此放入真实配置。

默认包不携带旧Chroma/FlagEmbedding实验工具。`requirements-legacy.txt`仅保留为可选历史依赖说明；需要旧工具时，须从作者单独取得经过审查的源码并复核其依赖许可。`rag_fixed_v1/__init__.py`只是保留Docker现有COPY路径的命名空间占位，不代表已附完整旧RAG实现。`attribution_gen.rag_evidence()`是未用于正式链的旧接口，本核心包不支持直接调用它。正式知识链使用 `langchain-core==1.6.2` 的 `BaseRetriever`：`enterprise/knowledge_langchain.py`绑定服务器身份和检索范围，每次调用受控引擎并返回实际 `Document`，复验授权、撤销和发布代际；引擎与SQLite索引仍由 `enterprise/knowledge_release.py`维护。适配器禁止外部callbacks和追踪，不改变900/150字符切片与三路融合规则。

## 2. 环境与依赖

基准环境为Python 3.12，Streamlit固定为1.63.0。使用受控Python环境安装核心依赖；下述安装命令需要部署者自行批准网络/软件来源，本次打包没有运行安装。

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python launch_system.py --check
```

Linux等环境使用对应 `.venv/bin/python`。核心依赖不包含torch、Chroma或PyMuPDF；正式PDF知识解析使用pypdf。本地BGE为可选项，按部署平台选择torch轮子后安装 `requirements-models.txt`，并单独提供已授权本地权重目录。程序不自动下载模型。

可选运维脚本 `scripts/freeze_core_wheels.py`根据已安装核心环境计算Windows CPython 3.12依赖闭包，从官方PyPI源下载并验证wheel，生成带哈希锁文件；默认还安装到独立 `.runtime/clean-venv`。它有网络下载和环境写入，不是只读检查，也不会由源码打包自动执行；运行前须具备对应平台的工作环境和`packaging`依赖，并单独批准软件来源。生成的wheel二进制、环境报告和个人绝对路径不在源码包内。本包附 `requirements-core-win-py312.lock.txt`，当前包含84个依赖及wheel字节SHA256（包括LangChain-Core闭包，实际条目以锁文件为准）。锁文件只适用于Windows CPython 3.12的已记录平台，不保证Linux或其他Python版本兼容；干净安装的执行记录另行受控交付，不把源码包包含锁文件本身等同运行验收。

## 3. 授权数据、模板和私有配置

代码与数据分目录。示例路径需要替换为部署主机上已经授权并按需创建的目录；这些目录不应位于待公开的源码仓库中。

```powershell
$env:COST_DATA_DIR = 'D:\project4-data\business'
$env:COST_MANAGED_DIR = 'D:\project4-data\managed'
$env:COST_LLM_CONFIG_FILE = 'D:\project4-private\llm.json'
$env:COST_AUTH_MODE = 'local'
$env:COST_BIND_HOST = '127.0.0.1'
```

`paths.py`从环境变量取得业务源目录与受控运行目录；未设置时兼容使用源码根目录，公开交付部署应显式覆盖。`.env.example`仅供Compose读取，直接运行Python不会自动装载dotenv。服务器身份映射、OIDC秘密和模型密钥应在私有文件或秘密管理系统中设置，不能把真实值写回示例或提交到版本库。

成本基线按 `dashboard/data_layer.py`中的表名模式发现，当前兼容一厂/二厂2025和2026年度文件，以及一厂预算、材料、人工、制造费用明细；扩展年度不是任意改名即可完成。用户从授权原件取得后，将所需文件放入 `COST_DATA_DIR`，或由正式“月度数据管理”界面预览、校验后独立确认。知识原件在“知识文档库”登记、确认并单独发布，上传不等于生效。

**报告模板是外部输入**：`report/registry.py`的默认路径为 `COST_DATA_DIR/月度成本分析报告模板.docx`，不再依赖源码所在目录。模板含业务占位符，必须另行取得授权版本，保持业务字段合同；本包不提供模板副本。缺少模板/数据不能生成完整正式报告，也不能用空壳文件冒充准备完成。

PDF导出需要可嵌入中文TrueType字体，使用 `REPORT_CJK_FONT`指向部署者合法取得的字体。字体字节不会随源码包分发。未提供本地模型时仍可明确降级为BM25/词项图检索，但不能把词法成功记成向量成功。

## 4. 本机启动与工作流

```powershell
.\.venv\Scripts\python launch_system.py --no-worker
```

启动器选择空闲的loopback端口并输出实际UI/API URL，默认优先8501/8000。第一次查看或配置时可用 `--no-worker`避免自动派发任务；确定需要处理已签发任务后，再使用不带该参数的受控启动。原有8000服务只有名称、版本和 `/api/health`返回的 `source_fingerprint`匹配当前源码时才会复用，健康响应不是完整业务验收。

local模式是当前OS用户全角色演示，只允许本机回环访问，审批为模拟独立审批。企业部署必须配置OIDC与服务端角色、工厂/产品范围映射，不应扩大local绑定范围作为替代。

模块四默认仅能向官方8090 loopback mock发送，不能发送真实微信。官方mock源码未包含，需由用户从授权资料另行提供；`COST_OFFICIAL_MOCK_SCRIPT`指向该脚本后，启动器的 `--with-mock`才可使用。批准、签发、发送受理、模拟确认/完成是不同状态；不明结果按原task_id查询，不能换ID绕过重试保护。

已有数据和任务的恢复应先按运维手册停写备份，恢复到新目录后核对回执门禁。`scripts/verify_restored_system.py`是特定三场景验收基线的离线核对工具：必须显式传入停止所有写入的新恢复目录、私有assessment及独立输出文件；还依赖assessment同目录的批准记录。其固定golden及默认77片段/1024维针对所记录场景，不是任意业务数据的通用验收标准。它保留少量汇总期望值供比对，原报告、原CSV、回执和assessment均未包含。该工具不派发、不清门禁、不生成新报告；核对输出也属于私有运行材料。代码中存在功能不等于真实模型、生产OIDC、目标容器或业务合理性已经验收。

## 5. 容器与企业部署

`Dockerfile`、`compose.yaml`及 `deploy/`为单实例部署材料。容器固定OIDC，入口监管API、UI与任务worker；必需身份配置或服务主体不合格时预检拒绝启动。`COST_WORKER_SUBJECT`必须是服务器映射中的有效发送主体。

Docker构建及依赖安装会访问选定的软件源；镜像同时安装系统包与Debian分发的中文字体，实际版本、字体hash与许可应随构建记录保存。数据卷、私有模型配置卷、OIDC秘密只读挂载分别管理，授权原件不进入构建上下文。`INSTALL_LOCAL_MODELS`生产构建默认保持`true`，需要另行提供已授权模型权重。详见 [部署与运维手册](docs/部署与运维手册.md)。

公开CI另设`core-container-smoke`：在Ubuntu构建时明确传入`INSTALL_LOCAL_MODELS=false`，随后以镜像默认UID/GID 10001运行[deploy/container_smoke.py](deploy/container_smoke.py)。运行容器使用`--network none`、只读根文件系统、临时`/tmp`、禁用健康检查，没有端口映射、主机数据挂载、secrets、镜像推送或artifact上传。仅此一次性smoke覆盖entrypoint并在进程内使用loopback local身份；正式部署入口的OIDC预检保持不变。

smoke检查`pip check`、正式核心模块和真实LangChain类型，进程内API健康/身份/状态/空任务汇总及非loopback身份拒绝；全部数据路径指向新临时目录。字体选择通过`report.export.font_descriptor()`，要求覆盖常规中文、U+2EE9部首和U+2212负号，再生成单页合成PDF，验证嵌入字体stream与提取文本，并用python-docx自带空白文档验证中文/符号的内存解析和临时磁盘读写。缺字或导出失败直接使检查失败，生成文件随临时目录删除。通过仅证明该提交的Linux核心镜像与合成导出检查可运行；生产OIDC、可选模型、完整业务报告及目标环境仍按各自记录验收。实际是否通过，以对应Actions任务日志为准。

## 6. 测试范围

本包只附以下静态筛选用例，不等同原项目全量测试：

- `tests/test_model_gateway.py`：假模型客户端、配置校验与脱敏。
- `tests/test_knowledge_versions.py`：临时合成文档与版本/授权；含AppTest。
- `tests/test_knowledge_release.py`：临时片段与假编码器；不证明真实BGE通过。
- `tests/test_operations.py`：临时SQLite/ZIP/合成原件、维护与恢复，部分平台能力不足可跳过。
- `tests/test_system_observability.py`：进程内API状态与权限，以及兼容admin入口的本机导航/远端拒绝AppTest；导航会读取配置的数据根，必须在没有真实业务数据的新解压目录运行。
- `tests/test_launch_system.py`：短暂绑定loopback端口，不启动官方mock或外网服务。
- `tests/test_knowledge_langchain.py`：合成文档、实际BaseRetriever/Document、身份范围/撤销/代际及禁追踪检查；编码器为合成对象。
- `tests/test_human_scores.py`：临时三场景CSV/JSON，验证实名记录结构、有限0–5分、空分保持null和原子新建输出；不替真实审核者评分。
- `tests/test_attribution_repair.py`：合成三要素、假网关与时钟，验证单次反馈修订、失败审计、责任主体和父级复验；临时worker/PID探针检查超时后实际进程退出，必要时仅清理该测试记录的自有PID。无原表、模型、赛方mock或私有场景脚本依赖。

根 `conftest.py`隔离managed与模型配置，清空测试模型密钥并导入Streamlit。它不隔离所有业务源目录，所以**不要在挂有真实业务目录/秘密的终端中泛跑未知测试**。推荐解压到新目录，在清理业务环境变量的独立测试进程中运行。

```powershell
.\.venv\Scripts\python -m pip install pytest==9.1.1
.\.venv\Scripts\python -m pytest tests -q
```

上述命令供接收者复核；打包器不执行应用测试，实际测试结果由对应运行日志记录。测试里的 `secret`、`private`、LangChain哨兵字符串及 `.invalid` URL是合成负例，不是真实凭据。依赖原表/模板、真实模型、浏览器或官方mock源码的整文件测试不在公开包内。

GitHub Actions 配置位于 `.github/workflows/ci.yml`，由 `push`、`pull_request` 或手动 `workflow_dispatch`触发，使用Ubuntu 24.04和Python 3.12，安装核心依赖与 `pytest==9.1.1`，只运行上述九个指定文件。流程从新的临时工作目录执行，将 `COST_DATA_DIR`指向空目录，使用临时运行库/模型配置和空密钥，禁用自动pytest插件、Hugging Face下载、LangSmith/LangChain追踪及遥测。不启动模型、官方mock或部署服务，不需要仓库secrets；进程内AppTest/TestClient、loopback端口绑定与临时worker子进程仍可使用。Windows wheel锁不用于Ubuntu安装。CI通过状态以对应提交的Actions实际记录为准，配置存在不表示运行已成功。

`scripts/validate_human_scores.py --assessment <受控assessment.json> --require-complete --output <新的私有结果.json>`读取assessment同目录的 `human_scores.csv`；完整记录必须有两项合法分数、具名审核者、带时区时间和具体意见，留空保持待评，不补零或生成评分。工具只验证记录格式与关联，不认证审核者身份，也不证明意见真实；输入、结果和审稿内容都应单独受控保存。

## 7. 重建与验证源码包

打包脚本仅依赖Python标准库，不导入业务模块，也不扫描运行目录或读取`.local`。清单为**逐文件**白名单，新增文件不会因扩展名匹配自动进入ZIP。

```powershell
python -B scripts/build_source_bundle.py
python -B scripts/build_source_bundle.py --verify-only
python -B scripts/build_source_bundle.py --self-check
```

生成 `delivery/project4-source.zip`与 `delivery/source_bundle_manifest.json`。ZIP内有 `SOURCE_MANIFEST.json`，记录全部源文件的相对路径、字节数、SHA256和源树摘要；外部清单额外记录最终ZIP和内部清单SHA256。固定顺序、权限和ZIP时间戳支持同字节源码复现。验证模式只读取生成的ZIP/清单，不导入运行应用。

脚本拒绝非白名单、路径穿越、符号链接/junction、多硬链接、超限文件、明显私钥/长格式token及个人用户名路径，检查空配置样例、Python语法、部分本地导入和动态UI入口，并记录已审的合成测试字符串。归档验证还拒绝注释/extra元数据，核对成员权限与时间戳、全部内外清单字段、唯一记录数及本地构建/授权范围状态。构建前应停止源码编辑，在可信本地工作树运行；路径检查与打开之间不是可抵御其他本地进程并发替换的安全边界。扫描不可能证明不存在所有秘密或取得全部第三方权利；清单记录 `publication_status=local_review_bundle`、`authorization_status=user_authorized_source_only`与明确的目标仓库地址。它表明本地纯源码包已获发布授权，不声称打包器完成了上传；远端提交URL和CI结果由独立交付索引记录，避免发布后改写归档哈希。`project_license=opensource_license_not_selected`表示项目自身开源许可证仍未选定。

## 8. 文档与授权

- [技术方案与接口](docs/技术方案与接口.md)
- [Prompt与模型评测协议](docs/Prompt与模型评测协议.md)
- [用户操作手册](docs/用户操作手册.md)
- [数据字典与交付边界](docs/数据字典与交付边界.md)
- [部署与运维手册](docs/部署与运维手册.md)
- [第三方来源与通知](THIRD_PARTY_NOTICES.md)

本项目未选定自身OSS许可证。ECharts及其内含组件的通知只适用于对应第三方作品，不应把 `assets/echarts-LICENSE.txt`复制成项目根LICENSE以暗示作者已授予Apache-2.0许可。源码、赛题约定、文档摘要、依赖、模型和字体的公开分发决策由各权利人及交付负责人确认。
