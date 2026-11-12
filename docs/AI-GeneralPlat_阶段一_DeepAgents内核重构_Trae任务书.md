# AI-GeneralPlat 阶段一：基于 DeepAgents 的 Agent 执行内核重构任务书

> **用途**：直接交给 Trae（或其他 Coding Agent）在本地仓库中自主分析、开发、测试和交付。
>
> **任务定位**：企业级通用 Agent 平台的**第一阶段实质性改造**，不是产品规划稿，也不是从零开发新平台。
>
> **目标仓库**：https://github.com/xxiaoxiong/AI-GeneralPlat
>
> **在线代码审阅基线**：`main`，commit `4d0f6b133fb8c0b7a8f2df8e803189c6ed36023a`（2026-03-15）。**Trae 实际执行前必须以本地工作区最新代码为准**；如已发生更新，先重新扫描与调整计划，禁止照搬旧文件路径或覆盖未提交改动。
>
> **设计参照日期**：2026-11-09。DeepAgents/LangChain/LangGraph API 更新较快，开发前核对当前已安装版本与官方文档，锁定相互兼容的版本并记录，不要凭过时示例猜 API。

---

## 0. 发给 Trae 的最高优先级执行指令

你是本项目的**主架构师 + 实施工程师 + 质量负责人**。直接在现有 `AI-GeneralPlat` 项目上进行可运行的代码改造，目标是将原有手写 ReAct Agent 执行内核**完整替换为以 DeepAgents 为核心、LangChain 为模型与工具抽象、LangGraph 为运行时与持久化底座的新内核**。

**不是**“接入一个 DeepAgents 示例”“写出设计文档就结束”，也**不是**继续手写一套 ReAct Loop 后给它换名称。你必须让已有 Agent 的创建、配置、聊天、工具调用、知识库访问、工作流 Agent 节点等真实业务路径走新执行内核，并保证原有非 Agent 业务不被无故破坏。

执行原则：

1. **先读代码再改代码**：扫描目录、调用链、模型接口、DB Schema、API、前端 SSE、工具、测试，建立现状证据；对不清楚的技术细节以官方 SDK 文档和实际运行结果为准。
2. **边界清晰**：第一阶段聚焦 Agent Runtime/Harness；其余模块只做让新内核正常接入所必需的改造，不扩张为整个平台重写。
3. **真实闭环**：逐项落实“实现 → 单测 → 集成测试 → 运行演示 → 记录证据 → 修复”，不能用 TODO、Mock-only、接口空实现代替验收；Mock 用于确定性测试，至少要有一条真实的、支持工具调用的模型端到端路径。
4. **优先原生能力**：规划、上下文压缩、文件后端、SubAgent、Skills、Checkpoint、Interrupt 等优先使用 DeepAgents/LangGraph 原生机制；只开发平台边界、权限控制、模型适配、工具桥接、API/SSE 转换、业务数据映射等差异化代码。
5. **保持兼容与可回滚**：新旧对话数据可读取，现有 API 尽量保持兼容；必要时提供迁移脚本/兼容层/回滚开关。不得直接删数据或执行破坏性数据库操作。
6. **自主推进**：按任务分解持续实施；非阻塞技术选择自行调研、决策、落地并写下理由，不反复等待人工确认；涉及生产数据删除、真实外部副作用、凭证披露、权限扩张或部署生产环境必须停下并明确列示风险。
7. **交付完整**：以可运行代码和可复现证据为完成标准，最终报告应列出完成项、测试结果、未完成项、风险以及下一阶段接口，不得把“准备做”写成“已完成”。

---

## 1. 项目定位与本阶段目标

### 1.1 产品定位

`AI-GeneralPlat` 是一个**可私有化部署、可扩展的企业级通用 Agent 平台**，而不是单用途聊天机器人、单模型 Demo 或单个工作流。平台最终应支持多模型、多用户与租户、Agent 配置与发布、复杂任务执行、多 Agent 协作、工具/MCP、知识库、Skills、记忆、人机协同、工作流、可观测、评测、安全治理及应用市场等能力。

第一阶段的价值，是建立能持续承载上述能力的**可信执行底座**：后续业务功能应能够通过稳定接口接入，不需要再次推翻 Agent 核心。

### 1.2 目标架构的职责划分

| 层 | 职责 | 实现原则 |
| --- | --- | --- |
| DeepAgents | Agent Harness：复杂任务规划、上下文压缩、虚拟文件系统、Skills、SubAgent、工具使用策略、HITL 中间件 | 以 `create_deep_agent` 及官方扩展点为核心；禁止重新造主循环 |
| LangChain | Chat Model、Tool、Structured Output、Middleware 等统一抽象 | 适配现有 OpenAI 兼容模型和既有工具 |
| LangGraph | 图执行、State、Thread、Checkpoint、Stream、Interrupt/Resume、持久化执行 | 使用官方公开 API 实现中断恢复、隔离与生命周期 |
| 平台业务层 | Agent 元数据、用户鉴权、模型管理、知识库、配置、会话索引、审计、工作流接入 | 保留现有 FastAPI/SQLAlchemy/MySQL/Vue，按需演进 |
| 资源与基础设施 | 模型网关、持久化存储、沙箱、事件与观测出口 | 可本地与内网部署，外部依赖可配置、可关闭 |

**注意**：DeepAgents 本身不是新的底层图运行时。它构建在 LangChain Agent 和 LangGraph 运行时之上。不能同时维护一套彼此独立的“手写 ReAct 状态机”和“LangGraph 运行时”，否则将产生双重状态、双重恢复与难以排查的执行语义冲突。

### 1.3 本阶段必须达成的成果

- 旧 ReAct 引擎从生产 Agent 请求执行路径**退出**；所有 Agent 执行经统一的 DeepAgents Runtime。
- 具备**真实工具调用、迭代执行、规划、上下文管理、结构化输出、流式事件、会话持续性**。
- 具备**持久化 Checkpoint、异常恢复、人机审批、可控取消、任务状态管理**的可运行基础。
- 具备**多 Agent/SubAgent、Skills、文件工作区、记忆、模型和工具适配层**，可通过配置或 API 扩展，而不是硬编码某个示例 Agent。
- 支持已有聊天页面、Agent 管理页面以及工作流中的 Agent 调用；必要时进行小范围前端联动改造。
- 构建基础**安全隔离、审计事件、自动化测试、运行与迁移说明**，为下一阶段可观测和评测打好基础。

---

## 2. 仓库现状与改造依据（来自在线 `main` 的代码检查）

> 此表是**代码线索，不是对已部署环境的证明**。Trae 必须重新核查本地最新代码以及实际调用路径。

| 现有位置 | 当前作用/观察 | 本阶段处理方式 |
| --- | --- | --- |
| `backend/app/services/agent/engine.py` | 自研 ReAct 循环，解析 `Thought/Action/Final Answer`，自行维护工作内存、工具重试与 SSE 事件 | **核心替换对象**；新执行路径不再依赖文本 ReAct 解析 |
| `backend/app/services/agent/output_parser.py`、`prompt_builder.py` | 旧文本协议解析、模板与输出修复 | 复核后迁移必要业务提示与输出规则；淘汰作为 Loop 依赖的旧协议 |
| `context_manager.py`、`checkpoint.py` | 自研滑窗摘要、每次运行的本地 JSON 检查点 | 用 DeepAgents 上下文机制、LangGraph Checkpointer 接管；旧文件按迁移情况废弃 |
| `memory_manager.py` | SQLite 文件 `memory_v2.db` 中的 fact/rule/skill 等记忆 | 与 LangGraph Store/长期记忆职责分离，提供可迁移的适配方案，不直接删除旧记忆 |
| `tool_executor.py`、`services/tools/` | 内置工具集合、同步/异步/DB/Agent handler、权限标记与重试 | 通过强类型 schema/Tool Adapter 接入；保留业务工具能力，权限在执行侧强制校验 |
| `agent/trace.py`、`hallucination.py` | 自研轨迹、基础规则检查与自校验 | 有价值的业务校验改成可插拔策略；记录可观测事件，但不保留重复内核 |
| `backend/app/api/v1/agents.py` | Agent CRUD、Session、SSE `/chat`、`/invoke`、记忆与监控 API | 替换到统一 Runtime Service；尽量保留 URL 与兼容字段 |
| `backend/app/models/agent.py` | Agent 配置；`AgentSession.messages` 保存历史与步骤 | 保留元数据与历史展示；新的 LangGraph Checkpoint 为执行真相来源，避免两套状态冲突 |
| `backend/app/services/agent_service.py` | 另有较早的 AgentService/手写 ReAct 实现 | 全局搜索引用，确认是否仍被调用，清理重复执行路径 |
| `backend/app/services/workflow_service.py` | 独立 BFS 画布式工作流引擎 | 本阶段**不整体重写工作流编辑器/引擎**，确保 Agent 节点通过统一 Runtime 调用；后续独立阶段再研究 LangGraph 化 |
| `backend/app/services/model_service.py` | 通过 OpenAI SDK 访问 OpenAI 兼容 API | 增加 LangChain ChatModel Adapter，保留现有模型配置表与常规聊天能力 |
| `frontend/src/views/agents/AgentChat.vue` | 消费 `thought/action/observation/final_chunk/final/done` SSE | 建立事件适配层，必要时增量增加执行状态、SubAgent 进度、审批、恢复/取消 UI |
| `frontend/src/views/agents/AgentList.vue` | 创建 Agent、选择模型/工具、配置迭代/知识库/DB | 保留并补充新 Harness 配置项，避免一次性重做整个配置界面 |
| `backend/requirements.txt` | 当前主要依赖 FastAPI、OpenAI SDK、MySQL、Redis；尚无 DeepAgents/LangGraph 依赖 | 引入兼容的 Python SDK 依赖、锁定版本并执行环境/集成验证 |
| `backend/tests/` | 有部分围绕旧引擎/解析器的测试 | 保留可迁移的工具/业务测试，新增 Runtime 生命周期和 E2E 用例 |

**已看到、必须随改造核查的风险**：`agents.py` 中部分读取/删除 Agent 或 Session 的查询未显式绑定当前用户所有权；`model_service.py` 中存在 `httpx.AsyncClient(verify=False, ...)`；旧工具自定义 HTTP 调用同样出现 `verify=False`；旧 Agent 最终输出是一次性获得内容后人为分块发送，并非真实逐 Token 模型流。不得原样把这些行为复制到新内核。具体影响范围要通过调用链、集成测试和安全检查验证。

---

## 3. 建议的目标结构与业务执行链

可参考以下**职责划分**组织代码，文件名由 Trae 根据仓库风格决定，不强制机械照抄：

```text
backend/app/
  api/v1/agents.py                 # 旧 API 保持兼容；新增 runs/approve/resume/cancel 按需设计
  services/agent_runtime/
    service.py                     # 平台唯一 Agent 执行入口，管理 run/会话生命周期
    factory.py                     # create_deep_agent 组装、缓存与版本化配置
    model_adapter.py               # ModelConfig -> LangChain ChatModel
    tools/adapter.py               # 旧工具注册表 -> LangChain Tools
    tools/policy.py                # 工具授权/资源作用域/审批/审计
    persistence.py                 # Checkpointer、Store、thread_id、迁移
    events.py                      # LangGraph Stream -> 平台规范事件/SSE
    context.py                     # 运行身份、AgentConfig、预算与执行上下文
    subagents.py                   # 子 Agent 配置/注册与最小权限
    skills.py                      # Skill 注册、加载与安全约束
    backends.py                    # 虚拟文件系统/持久化与沙箱的后端工厂
    lifecycle.py                   # Run 状态、恢复、取消、幂等
    telemetry.py                   # 基础事件/trace/metrics 接口，供后续 Langfuse 等接入
  models/                           # Agent/Session/Run/Approval 等必要的 DB 变更
  schemas/                          # 输入、输出、事件、状态 DTO
```

核心链路：

```text
Vue / 外部 API / Workflow Agent Node
             │
      FastAPI（鉴权、scope、参数校验）
             │
      AgentRuntimeService（唯一入口）
             ├─ Agent 配置、模型路由与能力检测
             ├─ Tool/Skill/SubAgent Registry + Policy
             ├─ DeepAgents.create_deep_agent(...)
             │      └─ LangChain Model / Tools / Middleware
             │              └─ LangGraph State / Checkpoint / Stream / Interrupt
             ├─ 持久化执行状态 + 文件 Backend / Store
             └─ 标准运行事件 -> SSE / 会话投影 / 审计事件
```

**重要建模边界**：`Agent` 是可配置模板，`AgentSession` 是用户可见的会话索引，`Run` 是一次执行实例，LangGraph `thread_id` 是持续执行状态的标识；`checkpoint` 与 `store` 分别承载线程状态与跨线程记忆。四者可以关联，但不得互相混用。任何运行资源查询必须以 `tenant_id`（当前项目如无租户概念先用用户作用域）+ `agent_id` + `session_id/run_id` 进行授权和隔离。

---

## 4. 实施范围：本阶段功能要求

### P0：必须实现并验收

**A. 统一执行内核与模型能力**

- 统一 `AgentRuntimeService`，所有 Agent 聊天、非流式运行和 `/invoke` 走同一套 DeepAgents Runtime；删除业务调用链中的旧 `for iteration` + 文本 ReAct 输出解析。
- Agent 支持多轮消息、原生 Tool Calling、函数参数 schema 校验、模型流式输出、结构化结果及错误处理。对工具调用失败采用明确状态，而非从字符串中搜索“错误/失败”判定成功。
- 现有数据库中 `ModelProvider/ModelConfig` 继续有效，支持内网 OpenAI-compatible（如 LiteLLM/vLLM）与标准服务；保留 `base_url`、凭证、模型名称、超时、max_tokens/temperature 等兼容配置。
- **启动/配置时检测模型原生工具调用能力**；对于不支持工具调用或 SDK 参数的模型，给出清晰错误或显式路由策略，不能悄悄回退到文本 ReAct 并宣称迁移完成。
- 配置执行预算：最大步骤/递归深度、模型调用次数、工具调用次数、超时、token 或成本预算（数据不足时至少实现可测的次数/时长限制），超限返回明确 `failed/limit_exceeded` 状态。

**B. Thread、State、Checkpoint、恢复与取消**

- 使用 LangGraph 官方 Checkpointer 保存状态，做到同一会话多轮连续、进程重启后恢复、同一会话在多 Worker/实例之间可恢复；并说明 SDK 恢复的具体语义。
- 现有平台业务数据库为 **MySQL**，**不要求在本阶段迁移所有业务表**。建议生产持久化 Checkpoint/Store 采用官方维护且有异步支持的 PostgreSQL 后端（可单独部署），本地测试允许 SQLite/内存模式；如采用其他方案，必须证明 API 兼容、多实例与恢复行为，禁止把单机 JSON 文件当作企业级最终方案。
- 正确管理 `thread_id`、`run_id`、请求去重键、运行状态 `pending/running/waiting_approval/completed/failed/cancelled`，以及最后错误信息与时间戳。状态转移要保证并发安全。
- 支持中断、恢复、取消；API 需区分“用户主动取消”“模型失败”“工具失败”“等待审批”“正常完成”和“空结果正常完成”。SSE 断连后的执行语义和客户端重连/状态查询策略必须定义并测试。
- 对已产生外部副作用的工具（发通知、HTTP POST、DB 写入等）设计幂等键、去重/回执或人工确认，**不能将 Checkpoint 视为保证工具副作用恰好一次执行**。

**C. 工具层、权限与安全**

- 将现有 `BUILTIN_TOOLS`、同步/异步/DB/AGENT handler 适配为 LangChain Tool，保留已有参数、功能和 Agent 关联知识库、数据库的使用逻辑；规范参数模型、返回结构、超时、错误码、审计信息。
- Tool Registry 按 `agent_id/user_id/tenant_id` 下发**实际被授权的工具集合**；模型端隐藏不代表服务端授权，执行前必须再次检查。
- 对文件读写、网络、数据库写入、邮件/通知、代码执行等定义独立权限级别，默认最小授权；高风险工具通过 HITL 保护。
- 内网/生产默认禁用直接宿主机命令与任意 Python 执行。若保留 `python_exec` 或 DeepAgents `execute`，必须接入隔离沙箱或明确关闭，限制文件挂载、网络、资源、执行时间与权限。禁止以仅有 Prompt 提示代替沙箱隔离。
- 修复可复现的 TLS 证书校验关闭问题，阻断 SSRF（尤其内网地址/云元数据/回环访问）、路径穿越、越权访问及凭证泄露；按现有业务提供可配置的受信任证书/目标 allowlist。

**D. Agent Harness：规划、上下文、文件与长任务**

- 使用 DeepAgents 内置规划/任务分解、上下文摘要、文件系统工具与工具输出卸载机制；提供长上下文/大文件演示，不在 Prompt 中塞入整份大文件。
- 选择并落地合适的 `StateBackend/StoreBackend/CompositeBackend/FilesystemBackend` 等官方 Backend 组合，明确文件的作用域、大小/生命周期、存储隔离以及持久化策略；严禁无保护地指向宿主机根目录。
- 将原先“滑动窗口 + JSON 保存 + 摘要”与新机制区分，避免两份相互竞争的摘要和状态；原有额外业务规则可用 Middleware 或策略接口保留。

**E. SubAgent、Skills 与复用**

- 具备主 Agent 注册/委派 SubAgent 的能力，提供**至少两个可实际调用的专用 SubAgent 示例**，并验证角色专用提示词、工具/模型独立配置、上下文隔离、返回结果与父任务继续执行。
- 使用官方 Skills 装载方式，建立可扩展的 Skill Registry（元数据、版本、启用范围、来源、访问控制）；至少提供一个可实际运行且可禁用的 Skill。完整的 Skill 市场和 UI 不在此阶段。
- SubAgent/Skill 必须继承或收窄平台安全边界，不得绕过租户隔离、工具授权、审批、文件路径和网络策略。控制派生深度、并发和预算，防止无限递归。

**F. 人在回路（HITL）**

- 使用 DeepAgents/LangGraph 的官方 `interrupt` / `interrupt_on` / 恢复能力，做到请求审批 → 后端持久化等待 → 前端查看待批准动作与参数 → 批准/拒绝 → 精确恢复。
- 拒绝、超时、非法审批人、重复审批、重启后审批恢复必须给出确定行为。至少在现有聊天页提供可用的最小 UI；不能只实现一个“返回需要审批”的假按钮。
- 审批记录要包含审批人、资源、操作摘要、结果和时间，不在公开事件中泄漏密钥或完整敏感参数。

**G. API、SSE、工作流兼容**

- 保持已有 `/api/v1/agents/.../chat`、`/invoke`、Session CRUD 的主体业务可用；改动接口需要提供清晰兼容说明和版本策略。
- 新 Runtime 内部以**结构化标准事件**表示 `run_started/model_delta/tool_started/tool_completed/tool_failed/subagent_started/subagent_completed/approval_required/state_changed/run_completed/run_failed/run_cancelled` 等；以单独 Adapter 映射为当前前端需要的 `final_chunk/final/done` 等事件。
- **真正流式**发送模型增量输出；不再用输出完成后 `sleep` + 人为切块模拟流式。提供 SSE 序号/运行 ID、终态和错误可区分字段；对于无法重放的事件明确提供状态查询回补。
- 不向普通用户暴露模型隐藏推理/思维链；以合法可解释的执行摘要、任务计划、工具状态和审批事件替代原 `thought` 展示。
- 将现有 Workflow `ai_chat`/Agent 节点接入统一 `AgentRuntimeService`，并通过集成测试证明工作流仍可执行。**本阶段不重构整个工作流图引擎**。

**H. 基础企业工程能力**

- 所有 Agent/会话/Run/审批/工具调用资源操作进行所有权与作用域校验，覆盖列表、详情、创建、删除、恢复、取消、审批接口。
- 规范错误分类、结构化日志、事件追踪字段（`request_id/trace_id/run_id/thread_id/agent_id/tenant_id/user_id/model_id`）、耗时、token 与工具调用状态；预留 Langfuse/OpenTelemetry 连接点。
- 提供清晰配置及依赖注入，测试可替换模型/Store/工具，避免全局单例混乱和共享可变 State。
- 完成数据库迁移、Docker Compose/本地启动、配置示例、最少权限与安全默认值。不得把生产凭证写入仓库。

### P1：同阶段尽量完成，但不得挤掉 P0 质量

- Agent 配置版本化、模型/工具/Skills 的运行时切换与配置热更新策略；历史 Run 应可识别创建时配置版本。
- 基础多 Agent 运行树可视化、运行事件回放/导出、分段结果查询。
- 开放标准接口预留：MCP 工具接入适配口（若工作量允许实现一个真实 MCP Server 联通演示）；可观测 Span 结构与后续评测数据采集口。
- 简单的执行策略 Middleware：错误重试/指数退避、敏感参数脱敏、超限/失败回退，证明可插拔。

---

## 5. Trae 实施顺序：按里程碑推进，不要一次性大爆改

| 里程碑 | 实际工作 | 验收证据 |
| --- | --- | --- |
| **M0 现状审计与基线** | 画出现有从 API → AgentEngine → Model/Tool → SSE/DB 的调用链；查全部旧 Agent 入口、迁移影响、模型能力与风险；跑当前测试与前后端启动；制定依赖升级方案 | `docs/agent-runtime/00-baseline.md`，基线测试结果与风险清单 |
| **M1 DeepAgents 最小内核可运行** | 创建 Runtime Factory、Model Adapter、统一 Service；接入真实可 Tool Calling 模型；能完成一次多步原生工具调用，无旧文本 ReAct | 最小运行命令、真实模型请求/工具调用记录、单元测试 |
| **M2 业务工具和 API 接入** | 迁移 Tool Registry/参数 schema/权限；把 `/chat`、`/invoke`、Session 接到新 Runtime；保留知识库/DB 能力 | API 集成测试、现有工具回归、前端聊天演示 |
| **M3 持久化与执行生命周期** | 接入 Checkpointer/Store、Run/Thread/Session 映射、恢复/取消/重启/多实例、幂等处理 | 断进程恢复、不同会话隔离、取消/错误/空结果测试 |
| **M4 Harness 能力补齐** | 规划、上下文压缩、文件 Backend、长任务、至少两个 SubAgent、一个 Skill | 长任务、大文件、委派、Skill E2E 演示 |
| **M5 HITL、安全与界面** | 权限/安全策略、真实审批中断恢复、前端显示与操作，修复所有权/TLS/高危工具路径 | 越权拒绝、审批/拒绝、沙箱与 SSRF 安全测试 |
| **M6 事件、观测接口和质量门禁** | 标准事件与 SSE 适配、日志追踪、异常与资源管理、工作流适配、测试覆盖与文档 | 全量回归、构建通过、Docker 启动、可复现验收报告 |
| **M7 清理与正式切换** | 对照引用分析移除或隔离旧执行路径；迁移旧数据；记录回滚；完成部署手册 | 生产调用路径中不依赖旧 `engine.py` ReAct Loop，迁移与回滚演练 |

**工作方式**：每个里程碑先给出 3–8 条可执行子任务，完成后在 `docs/agent-runtime/IMPLEMENTATION_STATUS.md` 更新“已实现/已测试/失败/剩余”。有测试失败时先修复再推进，不得仅因为测试为 Mock 就宣布真实能力完成。可以在独立 Git 分支提交小步、可回滚的 commit；**不要未经用户明确要求自动合并到主分支、修改线上服务或删除旧数据**。

---

## 6. 关键架构取舍（必须在实施中落实）

### 6.1 旧系统能力迁移，而不是层层叠加

- **保留**：FastAPI、Vue、模型供应商与模型配置、业务工具、知识检索、数据库连接、现有 Workflow、Agent 配置/API 及必要的业务记忆。
- **交给新框架**：Agent Loop、消息/工具调用协议、上下文摘要、任务规划、LangGraph 状态与 Checkpoint、SubAgent 调度、HITL 中断/恢复及文件 Backend。
- **由平台实现**：多用户/租户身份与授权、Tool Adapter/Policy、Run/Session 映射、SSE DTO、业务模型适配、凭证治理、跨系统幂等及外部副作用隔离。
- **需要谨慎保留**：旧 `hallucination.py` 的规则性校验不是“事实正确性保证”；符合业务需求的规则转为可插拔 Output/Tool Validation，不能宣称能杜绝幻觉。

### 6.2 多轮会话与持久化数据的真相来源

- **LangGraph Checkpoint 是运行态真相**，AgentSession.messages 是 UI 查询/历史展示投影；两者的写入先后、失败恢复及历史兼容要有明确同步策略。
- 历史 Session 的旧消息可读取并展示；旧格式的 `steps` 不是可自动转换的 LangGraph 运行 Checkpoint，**不要假装能从任意旧步骤精确恢复执行**。
- 对旧 SQLite memory 使用只读备份 + 可重复执行导入；提供验证与回滚。记忆不默认跨用户共享，Skill 文档与对话记忆也不能混为同一数据结构。

### 6.3 内网模型兼容与运行成本

- 模型接入优先用 `ChatOpenAI` 或官方适配器连接平台已有 OpenAI-compatible Endpoint，而非把旧 `ModelService.chat()` 包进自研文字协议。
- 对 LiteLLM/vLLM 的 `tool_calls`/JSON Schema/流式 tool delta 做真实契约测试；不同模型能力差异存入能力表或显式配置。能力不足时在 UI/错误信息中明确提示。
- 以可配置的模型限流、并发、超时、负载与重试控制保护推理后端；重试只针对适合重试的错误，不能在有副作用工具上盲目重放。

### 6.4 将来要扩展，但现在不做“大平台全家桶”

本阶段可以为下面这些模块预留接口或事件，但**不得为它们各自搭一套尚未投入使用的空服务**：Langfuse/LangSmith 全链路可观测、DeepEval/Promptfoo 等评测平台、完整 MCP 管理、企业多租户/计费/配额、应用发布与市场、复杂定时任务/队列、完整工作流画布重构、权限管理中台、集群级调度、灰度发布。后续各自独立规划开发。

---

## 7. 建议保留/新增的契约（具体命名可调整）

### 7.1 AgentRuntimeService 内部接口语义

```text
create_run(scope, agent_id, session_id?, input, idempotency_key?) -> RunInfo
stream_run(scope, run_id) -> AsyncIterator[RuntimeEvent]
get_run(scope, run_id) -> RunInfo + SafeStateSummary
resume_run(scope, run_id, approval_or_resume_input) -> RunInfo
cancel_run(scope, run_id) -> RunInfo
get_thread_state(scope, session_id) -> SafeStateSummary
```

只展示**语义契约**，不是强制代码签名。要同时支持现有 `/chat` 一步式体验、非流式调用和工作流以服务接口调用。`scope` 必须从后端鉴权获得，不允许客户端自由传“当前用户 ID”就越权。

### 7.2 运行事件最小字段

```json
{
  "event_id": "monotonic-or-uuid",
  "event_type": "tool_completed",
  "run_id": "...",
  "thread_id": "...",
  "agent_id": 1,
  "timestamp": "2026-11-09T12:00:00Z",
  "sequence": 12,
  "status": "running",
  "payload": {"tool_name": "calculator", "success": true},
  "trace_id": "..."
}
```

事件应区分执行事件、UI 可见事件和敏感内部信息。Token、Tool 参数、审批详情经过脱敏；对前端继续提供兼容事件或明确的版本转换。当前 `/chat` 的 `X-Session-Id` 行为应保留或提供等价方式。

### 7.3 必要的运行配置

以下只是配置维度，具体名称由项目配置体系确定：

`AGENT_RUNTIME=deepagents`、`AGENT_MAX_STEPS`、`AGENT_RUN_TIMEOUT`、`AGENT_MAX_TOOL_CALLS`、`AGENT_CHECKPOINT_DSN`、`AGENT_STORE_DSN`、`AGENT_ENABLE_HITL`、`AGENT_SANDBOX_MODE`、`AGENT_ALLOW_NETWORK`、`AGENT_EVENT_RETENTION_DAYS`。

配置项必须有安全默认值、类型校验和文档；不得把凭据、连接口令或生产内网资产写进代码或样例日志。旧配置项应明确兼容/废弃规则。

---

## 8. 必须通过的验收场景

| 编号 | 场景 | 通过条件 |
| --- | --- | --- |
| AT-01 | 旧 API 聊天/会话 | 创建 Agent、对话、历史查询、删除会话及非流式 `/invoke` 可正常调用新 Runtime |
| AT-02 | 原生工具调用 | 至少一次真实模型 function/tool calling，多步调用成功；无文本 ReAct 解析器依赖 |
| AT-03 | 流式 | SSE 在模型生成期间持续收到真实 token delta；终态、错误和会话 ID 正确 |
| AT-04 | 业务工具 | calculator、知识检索、数据库只读、网络工具按授权工作；错误有结构化状态 |
| AT-05 | Thread 隔离 | 两个用户/两个会话并行运行不会串消息、工具结果、文件或记忆 |
| AT-06 | Checkpoint | 多轮状态连续；进程停止/重启后同一 thread 可恢复，语义与文档一致 |
| AT-07 | 多实例 | 两个应用实例共享生产型持久化后端时可以查询/恢复同一合法 Run |
| AT-08 | 任务生命周期 | completed/failed/cancelled/waiting_approval/空结果能明确区分，取消不会被误报成功 |
| AT-09 | HITL | 高风险工具审批前不执行；合法批准后恢复，拒绝后不执行；重启后仍可审批 |
| AT-10 | 幂等副作用 | 人为触发恢复/重试，外部副作用不会因为重复执行而无条件重复发送/写入 |
| AT-11 | SubAgent | 主 Agent 分派至少两类专用 Agent 并收回结果；工具和文件权限隔离 |
| AT-12 | Skill | 能加载、启用、执行、禁用至少一个实际 Skill；非法来源不可越权 |
| AT-13 | 大上下文 | 大文件经 Backend/分段/摘要处理，不向模型直接塞超出窗口的数据；可复现 |
| AT-14 | 模型差异 | 不支持 tool calling 的模型被正确识别并显式报错/路由；支持模型正常执行 |
| AT-15 | 安全 | 非本人无法访问/取消/恢复/审批他人的 Run；TLS 默认验证；SSRF/路径越权被阻止 |
| AT-16 | 工作流 | 现有画布运行和 Agent 节点调用不退化；至少一个实际工作流端到端通过 |
| AT-17 | 资源与回滚 | 无明显未关闭连接/任务泄漏；数据库迁移可复现，兼容开关/回滚步骤可操作 |
| AT-18 | 代码质量 | 后端单测、集成测试、前端构建、lint/typecheck（若仓库已配置）可复现；失败如实记录 |

测试要求：核心状态转换、授权和工具逻辑必须有**确定性 Fake Model 测试**；真实模型冒烟测试以环境变量控制并在接通可用模型后运行。外部不可用的测试要输出 `skipped` 原因，不准伪装成 `passed`。对恢复、副作用、流式、并发做故障注入；至少完成 20 个独立会话的并发隔离测试（性能 SLA 根据现有机器再定）。

**完成门槛**：全部 P0 验收场景通过或有明确、经证据证明的环境阻塞；任何关键项未完成不得宣布“内核重构完成”，只能标注“部分完成”，列出剩余工作。

---

## 9. 开发中必须提交的交付物

仓库内建议新增 `docs/agent-runtime/`：

1. `00-baseline.md`：现有调用链、关键风险、改造前测试基线与代码版本。
2. `01-architecture.md`：架构分层、主要数据流、关键接口、数据库与运行状态、重要取舍。
3. `02-migration.md`：旧功能与新内核映射、旧 Session/Memory 迁移、安全回滚、兼容策略。
4. `03-operations.md`：依赖版本、环境变量、数据库初始化、内网模型配置、Docker/本地运行与故障排查。
5. `04-validation.md`：AT-01～AT-18 每项的执行命令、实际输出/截图或日志、结果、阻塞与复测说明。
6. `IMPLEMENTATION_STATUS.md`：里程碑持续记录、当前状态、遗漏事项以及最终完成报告。

还需交付：实际可运行的代码、数据库 Alembic 迁移、测试用例、示例 Agent/SubAgent/Skill、最小演示脚本或 README 操作步骤。最终在回复中给出最重要的变更文件、如何运行、实际执行了哪些测试、哪一项尚未完成，不得只给出“理论上可用”的描述。

---

## 10. 不允许出现的“伪完成”

- 安装 `deepagents`、写一个 `create_deep_agent()` 示例后就说内核完成；生产 API 仍走 `AgentEngine.run_stream()`。
- 把旧 Loop 包进 LangGraph 一个节点，仍自行用正则解析 `Thought/Action/Final Answer`。
- 一边手写自己的 Checkpoint，一边配置 LangGraph Checkpointer，恢复时又只会重新提交用户输入。
- 把一次性模型输出切成若干文本片段就宣称原生 Streaming。
- 仅有 `task` / `Skills` 的配置项，没有成功委派或执行实例。
- 仅在 Prompt 里写“不要危险操作”，但服务器仍可执行未授权工具或访问宿主系统。
- 将错误包装为自然语言 `Final Answer`，导致调用方把失败当成成功；或者把空结果一概当成错误。
- 仅做前端样式升级、抽象层堆叠、文档扩展，却没有可运行的行为变化。
- 删除旧 AgentSession/Memory/工具、直接迁移主业务数据库或破坏既有接口，却没有备份、验证与回滚。

---

## 11. Trae 现在应该直接开始的工作

**立即执行 M0 → M1 → M2，并依次推进后续里程碑，直到全部 P0 场景完成。**

首轮操作顺序：确认工作区分支与未提交修改 → 全局扫描 `AgentEngine` / `AgentService` / `/chat` / `/invoke` / Workflow Agent 节点 → 运行原项目并记录基线 → 查官方 DeepAgents/LangGraph API、锁定兼容版本 → 画出调用路径和内核切换方案 → 创建小步实施任务 → 编写新的 DeepAgents Runtime 最小可执行路径 → 测试真实 Tool Calling → 替换原 API 后端执行入口。

每完成一个里程碑，更新状态文档并继续实现下一项。遇到版本兼容、连接异常或模型能力不匹配，优先通过读取源码/官方文档、构建最小复现、修复和测试解决；无法解决才记录具体阻塞证据。**不要只向用户返回一个改造计划，必须持续做真正的代码实施。**

---

## 12. 官方参考资料（实施前以最新版本为准）

- DeepAgents Python 入门：https://docs.langchain.com/oss/python/deepagents/quickstart
- DeepAgents API `create_deep_agent`：https://reference.langchain.com/python/deepagents/graph/create_deep_agent
- DeepAgents 架构：https://github.com/langchain-ai/deepagents/blob/main/libs/ARCHITECTURE.md
- DeepAgents Backends：https://docs.langchain.com/oss/python/deepagents/backends
- DeepAgents Skills：https://docs.langchain.com/oss/python/deepagents/skills
- DeepAgents Subagents：https://docs.langchain.com/oss/python/deepagents/subagents
- DeepAgents HITL：https://docs.langchain.com/oss/python/deepagents/human-in-the-loop
- LangGraph Persistence：https://docs.langchain.com/oss/python/langgraph/persistence
- LangGraph Checkpointers：https://docs.langchain.com/oss/python/langgraph/checkpointers
- LangGraph Fault Tolerance：https://docs.langchain.com/oss/python/langgraph/fault-tolerance

**最后提醒**：这次最核心的交付是一个基于 DeepAgents 的、可运行、可恢复、可管理、可扩展并且能承接原平台真实业务请求的 Agent 执行内核。企业平台完整功能不是要求你在这一阶段全部实现，而是要求你的新内核在架构上能承载这些后续模块，且当前能力真实可用、可验证。
