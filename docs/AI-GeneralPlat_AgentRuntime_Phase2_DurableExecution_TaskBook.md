# AI-GeneralPlat Agent Runtime 第二阶段任务书

## Durable Execution：持久化执行、故障恢复与副作用治理

> **交付对象：** Trae（AI 编程执行环境）\
> **项目：**
> [xxiaoxiong/AI-GeneralPlat](https://github.com/xxiaoxiong/AI-GeneralPlat)\
> **任务阶段：** Agent Runtime Phase 2\
> **优先级：** P0 / P1\
> **任务性质：** 基于现有代码进行架构加固与工程实现，不是推倒重写\
> **基线说明：** 本任务书依据此前审阅的 Runtime 代码快照整理（审阅基线
> commit：`36fe3fbe11cf8c0400bfe25d55c5efbea2ab5550`）。开始实施时必须以当前工作区和当前分支为准，重新核对代码，不得假设文件与该快照完全一致。

------------------------------------------------------------------------

# 0. 给 Trae 的执行指令

你现在负责对 AI-GeneralPlat 的 Agent Runtime 进行第二阶段工程升级。

本任务不是写一份设计方案，也不是只补几个单元测试，而是要求你**在现有项目中完成可运行、可验证、可交付的代码改造**。

请严格遵循以下执行规则：

1.  **先审查，后修改。**
    首先读取当前分支、工作区状态、依赖版本、数据库模型、迁移历史、Runtime
    全部核心文件和现有测试。不得直接根据本任务书猜测当前代码。
2.  **保护现有业务。** 保留原有 Agent
    配置、工具注册表、模型路由、DeepAgents / LangGraph
    执行内核和前端聊天交互。不要顺手重构无关模块。
3.  **按阶段实施。**
    先建立基线和问题清单，再完成数据模型与持久化，再完成恢复与幂等，最后处理事件回放和测试。每阶段都要运行测试并修复回归。
4.  **优先复用 PostgreSQL。** 项目已经使用 PostgreSQL 和
    SQLAlchemy。第一版不要为了任务队列额外引入 Redis、Celery、Kafka
    或其他基础设施。确实发现 PostgreSQL
    无法满足某项需求时，先给出证据和替代方案，不要自行扩大技术栈。
5.  **不伪造"恰好一次"。** 对任意第三方 HTTP
    服务，网络超时可能发生在远端已经成功、但本地尚未收到响应的窗口。没有下游幂等键或可查询操作凭证时，不能承诺
    exactly-once。必须将不确定状态显式标记并阻止自动重放。
6.  **保留 API 兼容性。** 现有
    `/agents/{agent_id}/chat`、运行状态、取消、审批恢复等接口及旧 SSE
    事件结构应尽量兼容。若必须扩展，采用可选字段或新增接口，不得无说明地破坏前端。
7.  **所有关键状态写入必须可验证。** Run
    状态、审批决策、工具执行账本、关键事件的持久化失败，不允许仅记录日志后继续执行可能产生副作用的操作。
8.  **先测试再宣称完成。**
    不得仅以"代码已修改"作为完成依据。必须提供测试命令、测试结果、迁移验证结果和故障场景验证证据。
9.  **不覆盖用户已有修改。** 开始前检查
    `git status`。遇到未提交改动，先识别归属，不得 reset、checkout
    覆盖或批量格式化无关文件。
10. **每个阶段结束后汇报。**
    汇报改动文件、设计决策、测试结果、遗留风险和下一阶段计划。除非遇到阻塞，不要停留在分析阶段。

最终交付必须包含： - 可运行的代码改造； - Alembic 数据库迁移； -
自动化测试； - 配置与部署说明； - 故障恢复和副作用治理说明； -
本任务完成报告。

------------------------------------------------------------------------

# 1. 项目背景与本阶段定位

## 1.1 当前架构

当前项目已将 Agent 执行内核升级为 DeepAgents / LangGraph
路线。典型执行链路如下：

``` text
FastAPI agents.py
    |
    v
AgentRuntimeService.start_run()
    |
    v
_start()
    |-- RunRegistry 创建 RunHandle
    |-- 设置 Run 状态
    |-- asyncio.create_task(_drive)
    |
    v
_drive()
    |-- 加载 Agent / Provider / Model 配置
    |-- 构建 RuntimeContext / ExecutionBudget
    |-- 解析工具并创建 ToolPolicy
    |-- 创建工具数据库 Session
    |-- 获取 Checkpointer / Store
    |-- build_agent()
    |
    v
DeepAgents Factory
    |-- ChatModel
    |-- StructuredTool
    |-- Middleware
    |-- SubAgents
    |-- Skills / Backend
    |-- HITL
    |
    v
LangGraph Agent Graph
    |-- Model Node
    |-- Tool Execution
    |-- State Update
    |-- Conditional Routing / Loop
    |-- Checkpoint / Interrupt
    |
    v
_consume_stream()
    |
    v
RuntimeEvent -> EventBus -> LegacyEventAdapter -> SSE
```

本阶段不改变这条主干的基本职责划分，而是解决它在**进程重启、多
Worker、审批恢复、工具副作用、事件重连**方面的可靠性问题。

## 1.2 当前已具备的能力

此前审阅的代码中已经存在以下基础：

-   `AgentRuntimeService`：统一运行入口和执行驱动。
-   `RunHandle` / `RunRegistry`：进程内 Run 管理和状态机。
-   `AgentRun`：Run 数据库记录。
-   `AgentApproval`：人工审批记录。
-   `CheckpointerFactory`：支持 memory、SQLite、PostgreSQL
    Checkpointer。
-   `make_thread_id()`：按 Agent / Session 生成 LangGraph Thread ID。
-   `ToolAdapterFactory`：将平台工具包装为 LangChain `StructuredTool`。
-   `ToolPolicy`：工具授权、安全检查和 HITL 策略。
-   `RuntimeEvent` / `EventBus` / `LegacyEventAdapter`：统一事件和旧 SSE
    兼容。
-   已有 FakeChatModel 测试、工具调用测试、HITL 测试和部分 Checkpointer
    测试。

因此，本阶段是**在现有基础上补齐持久化与可靠性闭环**，不是重新实现 Agent
Loop。

## 1.3 已识别的主要风险

以下问题来自此前代码审阅。实施前必须重新核实，并记录"仍存在 / 已修复 /
设计已变化"。

  -----------------------------------------------------------------------------------------------------------------------------------------------------------------
  编号                    当前风险                                                               影响
  ----------------------- ---------------------------------------------------------------------- ------------------------------------------------------------------
  R-01                    `RunRegistry` 是进程内字典，`RunHandle.task` 是进程内 asyncio Task     进程重启后无法从 Registry 直接恢复运行对象

  R-02                    `resume_run()` 先通过 `registry.get(run_id)` 获取 RunHandle            服务重启后，即使数据库和 Checkpoint
                                                                                                 仍在，也可能无法通过现有恢复入口恢复审批任务

  R-03                    `AGENT_CHECKPOINT_DSN` 默认值为 `memory`                               进程重启后 Checkpoint 丢失；不满足生产持久化恢复要求

  R-04                    `AgentRun` 已有 `idempotency_key`，但索引不是唯一约束；Registry        多进程并发时可能创建重复 Run
                          的幂等映射只存在于内存                                                 

  R-05                    `AgentApproval` 只记录工具名、参数摘要和决策，缺少稳定                 重复审批、审批与具体工具调用的关联、恢复审计仍不够可靠
                          action/tool-call 身份                                                  

  R-06                    工具执行缺少持久化执行账本                                             工具执行成功但进程在结果落盘前崩溃时，恢复可能重复执行外部副作用

  R-07                    `EventBus` 使用内存 Queue，队列满时丢弃最旧事件                        断线重连、多实例订阅、事件回放无法可靠实现

  R-08                    SSE                                                                    客户端断线后无法从指定序号继续消费
                          事件只通过内存总线消费，运行状态查询与事件回放没有统一的持久化事件源   

  R-09                    API 每轮将完整 Session 消息传入已有 Thread 的 Graph                    与 Checkpoint 中已有 messages 叠加时可能重复写入历史消息

  R-10                    `LegacyEventAdapter` 在消费事件时累积最终文本，而 `_drive()`           生产者与消费者存在时序竞态，Run 最终结果可能不完整
                          生产事件后可能先读取结果                                               

  R-11                    `ctx.model_calls` 在收到非空消息文本时递增，流式 Chunk 可能被重复计数  模型调用预算与遥测统计不准确

  R-12                    `_persist_run()` 等部分持久化函数捕获异常后只记录日志                  关键状态持久化失败时，执行流程仍可能继续，破坏可靠性边界
  -----------------------------------------------------------------------------------------------------------------------------------------------------------------

------------------------------------------------------------------------

# 2. 本阶段目标与范围

## 2.1 总体目标

将当前以进程内 `asyncio.Task + RunRegistry + EventBus`
为主要运行协调方式的 Runtime，升级为：

> **PostgreSQL 持久化 Run 状态 + LangGraph 持久化 Checkpoint +
> 数据库租约协调 Worker + 工具执行幂等账本 + 可回放事件日志。**

完成后，应能够处理以下场景：

1.  Agent 正常执行期间，API 连接断开，任务继续执行。
2.  SSE 客户端断线后，按事件序号重新连接并补齐事件。
3.  服务进程在 Agent 等待审批时重启，用户仍能查询并批准该 Run。
4.  服务进程在执行中崩溃，Worker 能识别过期租约并尝试安全恢复。
5.  两个 Worker 同时扫描到同一待执行 Run 时，只有一个 Worker
    获得执行权。
6.  用户重复提交同一个幂等请求，不会产生重复 Run。
7.  用户重复提交相同审批，不会导致工具重复执行。
8.  外部 POST 已经产生副作用但本地结果未知时，不自动重放不安全操作。
9.  Run、Graph Checkpoint、Session
    消息和工具执行记录之间能够建立可审计关联。

## 2.2 本阶段明确包含

-   Run 数据库持久化和恢复；
-   Worker Claim / Lease / Heartbeat；
-   HITL 审批持久化与幂等恢复；
-   Tool Execution Ledger；
-   外部副作用幂等键传递与未知结果治理；
-   Run Event 持久化及 SSE 回放；
-   Session / Checkpoint 消息去重；
-   最终结果收集竞态修复；
-   模型调用计数与预算统计修复；
-   故障注入、并发和恢复测试；
-   配置、迁移、部署文档。

## 2.3 本阶段明确不做

以下内容不要混入本次任务：

-   不重写 DeepAgents / LangGraph 执行内核；
-   不更换 Agent SDK 或模型路由框架；
-   不重新设计所有业务工具；
-   不做新的前端 UI 视觉改版；
-   不引入 Kafka、RabbitMQ 等消息中间件；
-   第一版不引入 Redis / Celery；
-   不实现跨所有第三方系统的"绝对 exactly-once"；
-   不顺手重构 RAG、知识库、模型管理、Agent 配置等无关模块；
-   不将本阶段扩大成完整的 Agent Execution Platform。

------------------------------------------------------------------------

# 3. 目标架构与核心设计原则

## 3.1 目标架构

``` text
                        API Layer
                            |
            +---------------+----------------+
            |               |                |
         Create Run      Get Status       Resume / Cancel
            |               |                |
            +---------------+----------------+
                            |
                            v
                  PostgreSQL Run Repository
                            |
             +--------------+---------------+
             |              |               |
          agent_runs   agent_run_events  approvals
             |              |               |
             |              |               |
             v              v               |
       Worker Claim     SSE Event Replay     |
             |                              |
             v                              |
      Worker Lease / Heartbeat              |
             |                              |
             v                              |
      AgentRuntimeService                   |
             |                              |
             v                              |
      DeepAgents / LangGraph                |
             |                              |
       +-----+------+                        |
       |            |                        |
   Checkpointer  Tool Adapter                |
       |            |                        |
       v            v                        |
   Graph State  Tool Execution Ledger <------+
                    |
          +---------+----------+
          |                    |
      Read-only Tool       Side-effect Tool
          |                    |
          v                    v
       Execute       Idempotency / Reconcile
                               |
                               v
                        External System
```

## 3.2 状态源职责必须明确

  -----------------------------------------------------------------------------------------------------------
  状态载体                唯一职责                                           是否作为恢复依据
  ----------------------- -------------------------------------------------- --------------------------------
  `AgentSession`          产品层会话和用户可见聊天历史                       是，作为会话展示和输入历史来源

  `AgentRun`              一次执行的业务状态、输入快照、租约和最终结果       是

  LangGraph Checkpointer  Graph 内部状态、消息、节点进度、中断恢复           是

  Tool Execution Ledger   每次工具调用的执行意图、执行状态、结果和幂等信息   是，副作用恢复的关键依据

  `AgentRunEvent`         Run 的有序事件日志                                 是，事件回放依据

  `RunRegistry`           当前进程内的 RunHandle / Task 快速索引             否，只能作为本地缓存

  `EventBus`              本进程内通知或唤醒消费者                           否，不得作为事件唯一存储

  Telemetry               指标、Trace、日志                                  否，不得作为业务状态依据
  -----------------------------------------------------------------------------------------------------------

## 3.3 核心原则

### 原则 A：数据库是平台 Run 状态的事实来源

Run
的创建、状态迁移、审批决策、租约、工具执行账本和事件记录都必须有数据库记录。

内存对象只允许作为缓存和执行句柄，不得成为唯一事实来源。

### 原则 B：Checkpoint 与业务 Run 分工

-   Checkpoint 负责恢复 LangGraph 图状态；
-   `AgentRun` 负责恢复平台运行生命周期；
-   工具执行账本负责防止重复副作用；
-   事件日志负责 SSE 回放。

不能指望 Checkpoint 单独解决所有故障恢复问题。

### 原则 C：租约而不是进程内锁，负责跨 Worker 执行权

进程内 `asyncio.Lock` 只能保护单进程。

Worker 执行权必须通过数据库原子 Claim / Lease 获得。推荐 PostgreSQL
`SELECT ... FOR UPDATE SKIP LOCKED` 或等价的原子 UPDATE 方案。

### 原则 D：副作用先登记，再执行，再确认

任何可能改变外部世界的工具，都必须先在执行账本中建立稳定调用记录，再进入执行阶段。

### 原则 E：无法确定是否成功时，不得盲目重试

对不支持幂等键、也无法查询执行结果的外部系统，超时或进程崩溃后必须进入
`unknown / needs_reconciliation`，而不是自动再次 POST。

------------------------------------------------------------------------

# 4. 功能需求

## FR-01：Run 持久化与幂等创建

### 当前问题

目前 `RunRegistry.create()` 主要通过内存 `_by_idem` 判断幂等。数据库
`agent_runs.idempotency_key` 当前为普通索引，不能保证多进程并发下唯一。

### 要求

1.  创建 Run 时必须先写入 `agent_runs`。
2.  Run 创建和幂等检查必须由数据库约束或事务保证。
3.  幂等键按用户（若系统存在 tenant 概念，则按 tenant +
    user）作用域唯一。
4.  同一幂等键、同一请求摘要：返回已有 Run。
5.  同一幂等键、不同请求摘要：返回冲突，不得静默复用。
6.  未提供幂等键时，保持现有行为：每次请求创建新 Run。
7.  幂等命中时，必须能从数据库恢复 Run 状态，而不是只依赖内存 Registry。

### 数据要求

建议在 `agent_runs` 增加：

-   `request_hash`：规范化请求内容摘要；
-   `input_payload`：必要的用户输入快照；
-   `agent_config_snapshot`：不含明文密钥的执行配置快照，或至少保存配置版本/引用；
-   `started_at`；
-   `worker_id`；
-   `lease_expires_at`；
-   `heartbeat_at`；
-   `attempt`；
-   `lease_version` / `fencing_token`；
-   `last_event_seq`；
-   `recovery_error`（如需要）。

字段名称可以根据现有模型风格调整，但必须覆盖上述语义。

**安全要求：** 不得把 Provider API Key、密码、数据库连接明文凭据写入
`input_payload` 或配置快照。恢复时通过受控配置引用重新解析密钥。

## FR-02：持久化 Worker Claim / Lease

### 目标

将"请求进来就由当前 API 进程
`asyncio.create_task(_drive)`"升级为数据库协调的执行权机制。

### 要求

1.  新 Run 首先进入 `pending`。
2.  Worker 从数据库 Claim 可执行 Run。
3.  同一 Run 同一时刻只能有一个有效 Worker Lease。
4.  Claim 必须是原子操作，不能先 SELECT 再无锁 UPDATE。
5.  Worker 定期 Heartbeat 延长 Lease。
6.  Worker 执行前、工具产生副作用前，必须确认 Lease 仍有效。
7.  Worker 丢失 Lease 后必须停止推进任务，不得继续执行新的副作用。
8.  Lease 过期的 Run 可进入恢复检查流程。
9.  `waiting_approval` 不应被普通 Worker 当成待执行任务自动领取。
10. Worker 数量和轮询间隔通过配置控制。

### 推荐配置

``` env
AGENT_WORKER_ENABLED=true
AGENT_WORKER_CONCURRENCY=2
AGENT_WORKER_POLL_INTERVAL=1.0
AGENT_RUN_LEASE_SECONDS=30
AGENT_RUN_HEARTBEAT_SECONDS=10
AGENT_RECOVERY_ENABLED=true
```

具体配置名可调整，但必须有默认值、配置校验和文档。

### 实现要求

-   优先利用 PostgreSQL 行锁与 `SKIP LOCKED`；
-   不引入新的消息中间件；
-   Worker 应有明确的启动、停止和优雅关闭流程；
-   优雅关闭时停止领取新 Run，当前 Run 按安全策略处理；
-   通过 `lease_version` / fencing token 防止旧 Worker
    在租约过期后继续提交状态；
-   数据库事务不能跨越整个 Agent 执行周期，只在
    Claim、状态更新、账本更新等短操作中使用。

## FR-03：进程重启后的 Run 恢复

### 目标

服务重启后，能够根据数据库 Run 记录和 LangGraph Checkpoint 恢复任务。

### 恢复分类

  --------------------------------------------------------------------------
  Run 状态                            恢复策略
  ----------------------------------- --------------------------------------
  `pending`                           可被 Worker 正常领取

  `running` 且 Lease 未过期           不得抢占

  `running` 且 Lease 已过期           进入恢复检查，检查 Checkpoint
                                      和工具账本后决定是否继续

  `waiting_approval`                  保持等待审批；允许 API
                                      从数据库读取并审批

  `completed`                         只读返回结果

  `failed`                            只读返回失败信息，除非有显式重试功能

  `cancelled`                         不自动恢复

  `limit_exceeded`                    不自动恢复

  `needs_reconciliation`              不自动执行，等待人工核对
  --------------------------------------------------------------------------

### 恢复步骤

``` text
Worker 启动
   |
   v
扫描 pending / 过期 running
   |
   v
原子 Claim Run
   |
   v
加载 AgentRun 和执行配置快照
   |
   v
读取同一 thread_id 的 LangGraph Checkpoint
   |
   v
读取该 Run 未完成的 Tool Execution Ledger
   |
   +-------------------------------+
   |                               |
   v                               v
无不确定副作用                存在未知副作用
   |                               |
   v                               v
安全恢复 Graph                needs_reconciliation
   |
   v
继续执行并持久化
```

### 必须遵守

1.  恢复时沿用原 `run_id` 和原 `thread_id`。
2.  不得为同一 Run 重新生成一个 Thread。
3.  不得把整段旧消息历史再次无条件追加到已有 Checkpoint。
4.  恢复前必须检查工具执行账本。
5.  若 Checkpoint
    不存在或无法读取，不得假装恢复成功；应转为明确失败或人工处理状态。
6.  恢复流程必须有最大恢复次数和错误记录。
7.  `waiting_approval` 的恢复不应依赖原进程内的 `RunHandle`。

## FR-04：HITL 审批持久化与幂等恢复

### 当前问题

当前 `resume_run()` 依赖：

``` python
run = self.registry.get(run_id)
```

服务重启后 Registry 丢失，不能仅凭数据库记录完成审批恢复。

### 要求

1.  审批接口通过数据库加载 Run，不得只查询内存 Registry。
2.  校验当前用户是否有权审批该 Run。
3.  校验 Run 当前确实处于 `waiting_approval`。
4.  每个待审批动作必须有稳定 `action_id`，并与 LangGraph tool_call_id
    或等价标识关联。
5.  审批决策必须持久化。
6.  决策提交与 Run 状态变化必须在一个可保证一致性的事务边界内完成。
7.  重复提交相同决策应返回幂等成功或当前状态，不得重复恢复。
8.  对同一 action 已批准后又提交拒绝等冲突决策，必须返回明确冲突。
9.  只有审批决策持久化成功后，才能发送恢复命令。
10. 审批拒绝后不得执行对应工具。
11. 进程重启后仍可查询待审批动作、提交决策并恢复 Graph。

### 数据模型建议

在 `agent_approvals` 增加：

-   `action_id`：稳定动作 ID；
-   `tool_call_id`：模型工具调用 ID；
-   `args_hash`：参数摘要；
-   `action_payload`：必要时保存经过脱敏/加密的恢复信息；
-   `decision_status` 或明确的决策状态；
-   `decided_at`；
-   `decided_by`；
-   `message`。

对 `(run_id, action_id)` 建立唯一约束。

不得为了恢复而无保护地把敏感参数明文写入审批表。

## FR-05：Tool Execution Ledger（工具执行账本）

### 这是本阶段最重要的能力之一

HITL
只解决"执行前是否需要人批准"，并不自动解决"工具执行后进程崩溃怎么办"。

例如：

``` text
Agent 调用 http_post 创建订单
    |
    v
远端订单已经创建成功
    |
    v
本地服务在保存 ToolMessage / Checkpoint 前崩溃
    |
    v
进程重启，Graph 可能重新执行该工具
    |
    v
重复创建订单
```

必须引入持久化工具执行账本。

### 建议新增表

`agent_tool_executions`（命名可按项目风格调整）。

建议字段：

  字段                          说明
  ----------------------------- ---------------------------------
  `id`                          主键
  `run_id`                      所属 Run
  `tool_call_id`                LangChain/LangGraph 工具调用 ID
  `tool_name`                   工具名称
  `args_hash`                   规范化参数摘要
  `args_snapshot`               脱敏或加密后的必要参数快照
  `idempotency_key`             稳定外部幂等键
  `status`                      执行状态
  `attempt_count`               尝试次数
  `result_payload`              工具结果（按安全策略保存）
  `error`                       错误信息
  `external_operation_id`       下游返回的业务操作 ID
  `started_at`                  开始时间
  `finished_at`                 完成时间
  `created_at` / `updated_at`   时间戳

建立唯一约束：

``` text
UNIQUE(run_id, tool_call_id)
```

如存在租户隔离，还需纳入正确的租户作用域。

### 执行状态

建议至少支持：

``` text
prepared
executing
succeeded
failed
unknown
needs_reconciliation
```

状态含义：

-   `prepared`：调用意图已持久化，尚未执行；
-   `executing`：已取得执行权，正在执行；
-   `succeeded`：已确认执行成功，结果已持久化；
-   `failed`：已确认失败，且失败语义明确；
-   `unknown`：无法确认远端是否已经产生副作用；
-   `needs_reconciliation`：需要人工或专门查询逻辑核对。

### 工具调用 ID

必须为每次工具调用获取稳定身份。

优先使用当前安装版本 LangChain 支持的 `InjectedToolCallId`
或等价运行时注入机制，将 tool_call_id 传入 ToolAdapter。

如果当前版本不支持该方式，必须先审查当前 LangChain 工具执行
API，再采用可靠的适配方案。

**禁止仅用 `args_hash` 作为调用唯一 ID。**
相同参数可能是用户有意执行的两次独立操作。

### 工具执行流程

``` text
LangGraph 发起 Tool Call
        |
        v
取得 run_id + tool_call_id + tool_name + args
        |
        v
计算 args_hash / idempotency_key
        |
        v
数据库查询或创建执行账本
        |
        +-------------------------------+
        |               |               |
        v               v               v
   succeeded        executing        unknown
        |               |               |
        v               v               v
返回已存结果       判断 Lease /       不自动重放
不重复执行         恢复策略           进入核对
                        |
                        v
                取得工具执行权
                        |
                        v
                持久化 executing
                        |
                        v
                  执行工具
                        |
                        v
              持久化结果和 succeeded
                        |
                        v
               返回 ToolMessage
```

### 外部副作用治理规则

必须为工具定义副作用等级：

  -----------------------------------------------------------------------------------
  类型                    示例                               恢复策略
  ----------------------- ---------------------------------- ------------------------
  `read_only`             查询数据库、读取文件               通常允许安全重试

  `idempotent`            设置资源状态为固定值               依据工具语义安全重试

  `external_idempotent`   支持下游 Idempotency-Key 的 HTTP   重试时复用同一个幂等键
                          POST                               

  `non_idempotent`        不支持幂等的下单、扣款、发送通知   不确定时禁止自动重放

  `requires_approval`     高风险写操作                       先 HITL，审批后再执行
  -----------------------------------------------------------------------------------

对于支持幂等键的下游服务：

-   生成稳定的幂等键，例如 `run_id + tool_call_id` 的摘要；
-   同一逻辑调用的重试必须使用相同幂等键；
-   将幂等键传递给下游约定的 Header 或参数；
-   保存下游返回的 operation ID；
-   验证下游确实实现幂等语义，不能只因为传了 Header 就假定有效。

对于不支持幂等键、也不能查询业务结果的外部服务：

-   网络超时或进程崩溃后标记 `unknown`；
-   不自动重放；
-   提供人工核对/确认入口或后台 reconciliation 机制；
-   核对完成后才允许继续或人工触发重试。

**严禁在文档、接口或测试中宣称任意第三方副作用都能实现 exactly-once。**

## FR-06：审批与工具账本协同

必须验证以下完整流程：

``` text
模型请求调用高风险工具
    |
    v
HumanInTheLoopMiddleware 中断
    |
    v
持久化 approval action
    |
    v
Run = waiting_approval
    |
    v
服务重启
    |
    v
用户重新查询 Run
    |
    v
提交 approve
    |
    v
事务化保存审批决策
    |
    v
恢复同一 LangGraph Thread
    |
    v
ToolAdapter 查询执行账本
    |
    +--------------------------+
    |                          |
    v                          v
尚未执行                   已执行成功
    |                          |
    v                          v
执行一次                  返回已保存结果
    |                          |
    v                          |
持久化成功结果                 |
    |                          |
    +------------+-------------+
                 |
                 v
             后续模型节点
                 |
                 v
             最终完成
```

必须特别覆盖"审批已批准，但 Worker 尚未真正执行工具时崩溃"的情况。

此时恢复后可以继续执行，但仍要通过执行账本保证同一 tool_call
不重复执行。

## FR-07：Run Event 持久化与 SSE 回放

### 当前问题

当前 `EventBus` 是内存 Queue，队列满时会丢弃最旧事件；SSE
消费依赖当前进程中的 `run.bus`。

这不适合多 Worker、断线恢复和跨进程订阅。

### 要求

新增持久化事件表，例如 `agent_run_events`：

  字段           说明
  -------------- --------------------
  `id`           主键
  `run_id`       所属 Run
  `sequence`     Run 内单调递增序号
  `event_type`   事件类型
  `payload`      脱敏后的事件数据
  `created_at`   创建时间

建立：

``` text
UNIQUE(run_id, sequence)
INDEX(run_id, sequence)
```

### 事件发布

1.  每个 RuntimeEvent 发布时先分配可靠的 Run 内序号。
2.  持久化事件后，再通过本地通知机制唤醒 SSE 消费者。
3.  数据库事件日志是事实来源，内存 EventBus 只作为通知优化。
4.  关键终态事件不能因为内存 Queue 满而丢失。
5.  敏感参数必须在持久化前脱敏。
6.  事件写入失败时必须有明确处理策略，不得静默当作成功。

### SSE 断线重连

支持以下任一兼容方式：

-   标准 SSE `Last-Event-ID`；
-   或 `after_seq` 查询参数；
-   可以同时支持。

客户端重连时：

``` text
GET /runs/{run_id}/events?after_seq=25
```

服务端按 sequence 递增返回 `sequence > 25` 的事件，直到终态。

兼容要求：

-   保留旧事件 JSON 的 `type` / `content` 等字段；
-   可以增加 `seq`、`run_id`、标准 SSE id；
-   不得让旧前端因新增字段而解析失败；
-   运行状态查询接口继续可用。

### 事件保留

增加事件清理策略和配置。默认不能清理仍在运行、等待审批或尚未超过保留期限的
Run 事件。

## FR-08：Session 与 Checkpoint 消息一致性

### 当前问题

当前 `/chat` 读取完整 `session.messages`，追加新用户消息，再把完整
messages 传入已有 `thread_id` 的 LangGraph。

当 Checkpoint 已经保存之前的消息时，可能重复追加历史。

### 要求

先确认当前 LangGraph 消息 Reducer 和 `messages_to_langchain()`
的实际实现，再确定唯一方案。

可选方案：

**方案 A：Checkpoint 作为执行上下文的增量来源**

-   第一次执行时，若 Thread 没有 Checkpoint，可以注入历史消息；
-   后续执行只注入本轮新增用户消息；
-   通过 `aget_state()` 或等价方式判断 Thread 是否已初始化。

**方案 B：稳定消息 ID 去重**

-   平台消息生成稳定 ID；
-   转换为 LangChain Message 时保留 ID；
-   使用 `add_messages` 的 ID 更新/去重语义；
-   确保同一条消息在重试和恢复时不会被重复追加。

最终只能选定一种清晰、可测试的主策略，不能依赖偶然的消息合并行为。

同时：

1.  用户消息必须在 Run 创建前或同一业务事务中可靠保存，不能等整轮 Agent
    完成后才首次持久化。
2.  Assistant 最终结果由 Runtime 统一写回 Session。
3.  Checkpoint 与 Session 的职责要写入设计说明。
4.  历史会话升级时必须兼容已有数据。

## FR-09：最终结果收集与事件适配解耦

### 当前问题

此前 `LegacyEventAdapter` 在消费 `MODEL_DELTA` 时累积
`final_text_parts`，而 `_drive()`
在事件发布后可能先读取该列表，存在生产者/消费者时序竞态。

### 要求

1.  `run.final_text` 的唯一权威收集位置放在 Runtime 执行侧（例如
    `_consume_stream()`）。
2.  事件适配器只负责协议转换，不再承担 Run 最终业务结果的唯一收集职责。
3.  非流式 `run_to_completion()` 与流式 SSE 必须使用同一份权威最终结果。
4.  Run 结束时先确保最终结果已持久化，再发布 `RUN_COMPLETED`。
5.  对结构化、多模态模型内容采用明确序列化/提取规则，不得静默丢失需要保留的文本。

## FR-10：模型调用预算和执行步数统计

### 当前问题

不能将每个非空 `AIMessageChunk` 当作一次模型调用。

### 要求

1.  `model_calls` 按一次模型请求计数，而不是按 Chunk 计数。
2.  使用当前 LangChain 版本支持的 Callback / Runnable
    生命周期事件或等价机制统计模型调用。
3.  区分模型请求次数、生成 Token 数、模型节点执行次数。
4.  `tool_calls` 只统计实际执行或明确发起的工具调用，并定义口径。
5.  `steps` 必须有明确递增位置和定义。
6.  Middleware 的调用上限与 RuntimeContext 统计口径一致。
7.  预算耗尽后 Run 状态应为 `limit_exceeded`，不能误报 `failed` 或
    `completed`。

------------------------------------------------------------------------

# 5. 建议的数据模型与迁移

以下是目标语义，不要求字段名逐字照搬。Trae
必须先检查现有模型、数据库类型和 Alembic 历史，再形成最终迁移。

## 5.1 扩展 `agent_runs`

保留已有字段：

-   `id`
-   `agent_id`
-   `session_id`
-   `user_id`
-   `thread_id`
-   `status`
-   `idempotency_key`
-   `error`
-   `final_text`
-   `summary`
-   `created_at`
-   `updated_at`
-   `finished_at`

建议新增：

  字段                      类型建议      用途
  ------------------------- ------------- --------------------------------
  `request_hash`            String(64)    请求内容摘要
  `input_payload`           JSON          恢复所需输入快照
  `agent_config_snapshot`   JSON          无敏感明文的配置快照或版本引用
  `started_at`              DateTime      首次开始时间
  `worker_id`               String(128)   当前租约持有者
  `lease_expires_at`        DateTime      租约到期时间
  `heartbeat_at`            DateTime      最近心跳
  `attempt`                 Integer       执行/恢复尝试次数
  `lease_version`           Integer       fencing token
  `last_event_seq`          BigInteger    最近事件序号
  `recovery_error`          Text          恢复失败原因

为幂等键建立正确的唯一约束。建议按用户或租户作用域唯一，并对 NULL
的处理符合所用数据库语义。

## 5.2 扩展 `agent_approvals`

保留现有审批字段，并增加稳定动作身份：

-   `action_id`
-   `tool_call_id`
-   `args_hash`
-   `action_payload`（仅在确有恢复需要时保存，必须脱敏或加密）
-   `decision_status`（如需要）
-   `updated_at`

建立 `(run_id, action_id)` 唯一约束。

## 5.3 新增 `agent_tool_executions`

至少包含：

``` text
id
run_id
tool_call_id
tool_name
args_hash
args_snapshot
idempotency_key
status
attempt_count
result_payload
error
external_operation_id
worker_id
lease_version
started_at
finished_at
created_at
updated_at
```

约束：

``` text
UNIQUE(run_id, tool_call_id)
INDEX(run_id, status)
INDEX(status, updated_at)
```

敏感参数、工具结果必须按安全策略脱敏、加密或只存摘要。

## 5.4 新增 `agent_run_events`

至少包含：

``` text
id
run_id
sequence
event_type
payload
created_at
```

约束：

``` text
UNIQUE(run_id, sequence)
INDEX(run_id, sequence)
```

序号分配必须并发安全，不能依赖每个 Worker 的本地计数器。

## 5.5 迁移要求

-   新建独立 Alembic revision，不修改已执行的历史迁移；
-   `upgrade()` / `downgrade()` 都要完整；
-   为已有数据提供安全默认值；
-   在测试数据库执行完整迁移；
-   验证索引、唯一约束和外键；
-   迁移不得删除已有 Run、审批或 Session 数据；
-   对生产部署给出备份和回滚步骤。

------------------------------------------------------------------------

# 6. 核心流程设计要求

## 6.1 创建 Run

``` text
API 收到请求
   |
   v
认证并校验 Agent / Session
   |
   v
生成 request_hash
   |
   v
数据库事务：
  - 检查幂等键
  - 创建 AgentRun(status=pending)
  - 保存必要 input_payload
  - 保存用户消息
   |
   v
提交事务
   |
   v
通知 Worker（或由 Worker 轮询发现）
   |
   v
返回 run_id
```

API 不应再把 `asyncio.create_task(_drive)` 当作跨进程可靠调度机制。

## 6.2 Worker 执行

``` text
Worker poll
   |
   v
原子 Claim pending Run
   |
   v
获得 worker_id + lease_version
   |
   v
加载 Run / Agent 配置 / 输入
   |
   v
加载 Checkpointer / Store
   |
   v
检查 Tool Execution Ledger
   |
   v
组装 DeepAgent
   |
   v
以原 thread_id 启动或恢复 Graph
   |
   v
持续 Heartbeat
   |
   v
持久化 RuntimeEvent
   |
   v
更新 Run 最终状态
   |
   v
释放 Lease
```

## 6.3 工具执行

``` text
收到 tool_call
   |
   v
校验 ToolPolicy
   |
   v
获取稳定 tool_call_id
   |
   v
创建/读取 Tool Execution Ledger
   |
   v
判断是否已有 succeeded 结果
   |
   +---- 有 ----> 返回持久化结果
   |
   v
检查是否存在 unknown / executing
   |
   +---- 不确定 ----> 禁止盲目重放
   |
   v
持久化 executing
   |
   v
执行工具（传递稳定幂等键）
   |
   v
持久化结果与 succeeded
   |
   v
返回工具结果给 LangGraph
```

## 6.4 审批恢复

``` text
用户提交审批
   |
   v
从 DB 加载 Run 和 Approval
   |
   v
验证用户权限与当前状态
   |
   v
事务内锁定 Approval / Run
   |
   v
写入决策（幂等）
   |
   v
将 Run 置为可恢复状态
   |
   v
Worker Claim
   |
   v
Command(resume=...)
   |
   v
同一 thread_id 恢复 Graph
```

审批接口不得仅依赖 `RunRegistry.get(run_id)`。

------------------------------------------------------------------------

# 7. 故障场景与恢复策略

必须针对以下场景设计和测试。

  ----------------------------------------------------------------------------------------------------------------
  编号                    故障窗口                           预期行为
  ----------------------- ---------------------------------- -----------------------------------------------------
  F-01                    Run 创建事务提交前进程崩溃         不留下可执行的半成品 Run；客户端重试可安全创建

  F-02                    Run 创建提交后、Worker Claim       新 Worker 能发现 pending Run 并执行
                          前崩溃                             

  F-03                    Worker Claim 后、Graph 启动前崩溃  Lease 过期后重新 Claim；安全恢复

  F-04                    模型调用期间进程崩溃               依据 Checkpoint
                                                             恢复；允许重新请求模型，但不得伪造已完成节点

  F-05                    只读工具执行期间崩溃               可按工具策略重试

  F-06                    外部 POST 尚未发送前崩溃           账本状态允许安全执行

  F-07                    外部 POST                          有下游幂等键则复用同一键查询/重试；无幂等能力则
                          已成功，响应尚未收到时崩溃         unknown，不自动重放

  F-08                    外部 POST 成功，账本结果提交前崩溃 与 F-07 一样处理，不能直接假定失败

  F-09                    工具结果和账本已成功，Checkpoint   恢复时从账本返回已保存结果，不重复执行副作用
                          尚未写入时崩溃                     

  F-10                    HITL 中断后进程崩溃                Run 保持 waiting_approval，审批信息可从数据库读取

  F-11                    用户重复提交同一审批               幂等返回，不重复恢复、不重复执行工具

  F-12                    两个 Worker 同时 Claim 同一 Run    只有一个获得有效 Lease

  F-13                    Worker Lease 过期但旧 Worker       旧 Worker 的 fencing token
                          恢复运行                           失效，不能继续提交状态或副作用

  F-14                    SSE 客户端断线                     通过 Last-Event-ID / after_seq 从持久化事件继续

  F-15                    EventBus 内存队列满                持久化事件不丢失，通知丢失也能通过回放/轮询补齐

  F-16                    Checkpoint 不存在或损坏            明确进入失败/人工恢复状态，不从头重放有副作用的任务

  F-17                    数据库持久化失败                   关键状态写入失败时停止后续副作用操作并暴露错误

  F-18                    Session 历史与 Checkpoint          不重复追加历史消息
                          同时包含旧消息                     
  ----------------------------------------------------------------------------------------------------------------

------------------------------------------------------------------------

# 8. API 与兼容性要求

## 8.1 现有接口

优先保留：

-   `POST /agents/{agent_id}/chat`
-   `GET /runs/{run_id}`
-   `POST /runs/{run_id}/cancel`
-   `POST /runs/{run_id}/resume`

实际路径以当前代码为准。

## 8.2 幂等键

建议支持标准请求头：

``` http
Idempotency-Key: <client-generated-key>
```

保持可选，不传时保持原有行为。

幂等命中时返回原 Run ID 和当前状态。

## 8.3 状态查询

`GET /runs/{run_id}` 必须直接支持数据库查询，不能因为当前 Worker
进程没有对应 RunHandle 就返回不存在。

返回至少包含：

``` json
{
  "run_id": "...",
  "thread_id": "...",
  "status": "running",
  "created_at": "...",
  "updated_at": "...",
  "started_at": "...",
  "finished_at": null,
  "attempt": 1,
  "error": null,
  "final_text": null
}
```

字段可根据现有 API 兼容情况调整。

## 8.4 SSE 回放

支持从序号继续消费：

``` http
GET /runs/{run_id}/events
Last-Event-ID: 25
```

或：

``` http
GET /runs/{run_id}/events?after_seq=25
```

旧前端仍可消费原有 `type`、`content`、`tool`、`input` 等字段。

## 8.5 审批接口

-   先验证 Run 所属用户/租户权限；
-   校验审批记录与 action_id；
-   决策幂等；
-   冲突决策返回明确 HTTP 错误；
-   不允许仅凭客户端传入 run_id 就恢复任意任务。

------------------------------------------------------------------------

# 9. 安全与资源治理

1.  Worker 恢复 Run 时必须重新执行用户、Agent、工具授权检查。
2.  不得把历史 Checkpoint 当作永久授权凭证。
3.  工具执行前仍需通过 ToolPolicy。
4.  审批信息、工具参数和事件 Payload 必须脱敏。
5.  数据库连接凭据、API Key 不得进入事件日志或配置快照。
6.  对执行账本中的敏感字段按需要加密或只保留摘要。
7.  Worker 必须有租约和 fencing token。
8.  取消操作需要与 Worker 协调，不能只修改内存状态。
9.  对 `unknown` 工具执行状态禁止自动重试。
10. 为事件日志和工具账本定义保留周期，不能在任务仍可恢复时清理。
11. 所有 API 必须校验当前用户对 Run 的访问权。
12. 不能让一个用户通过猜测 run_id
    读取其他用户的运行状态、事件、审批或工具结果。

------------------------------------------------------------------------

# 10. 自动化测试要求

测试必须使用 FakeChatModel
和确定性工具完成大多数场景，不依赖真实模型、不依赖外部网络。

真实模型 E2E 可以继续使用环境变量 Gate，不能成为 CI 的唯一验证手段。

## 10.1 单元测试

至少覆盖：

-   Run 状态迁移合法性；
-   幂等键同请求命中；
-   幂等键不同请求冲突；
-   Tool Execution Ledger 状态机；
-   succeeded 结果复用；
-   unknown 状态禁止自动重放；
-   审批重复提交；
-   冲突审批决策；
-   Event sequence 单调递增；
-   事件脱敏；
-   Session Message ID 去重；
-   最终文本收集；
-   模型调用次数不受 Chunk 数量影响；
-   Lease 过期与 fencing token 校验。

## 10.2 集成测试

至少覆盖：

### T-01：正常多步工具调用

``` text
用户输入
 -> Model Tool Call
 -> Tool Adapter
 -> Ledger
 -> Tool Executor
 -> ToolMessage
 -> Model Final
 -> Run completed
```

验证工具执行一次、账本成功、最终结果持久化。

### T-02：服务对象重建后的审批恢复

1.  FakeChatModel 发起高风险工具调用；
2.  LangGraph 进入 HITL Interrupt；
3.  Run 状态持久化为 waiting_approval；
4.  销毁当前 RuntimeService / Registry；
5.  创建新的 RuntimeService；
6.  通过数据库查询 Run；
7.  提交 approve；
8.  恢复同一 thread_id；
9.  验证工具只执行一次；
10. 验证最终状态 completed。

### T-03：审批重复提交

同一 approve 请求提交两次，验证： - 只存在一个有效决策； -
不会创建两个恢复任务； - 工具副作用只发生一次。

### T-04：工具成功后 Checkpoint 前崩溃

通过故障注入模拟： - 工具真实执行并写入 succeeded 账本； - 在 Graph
Checkpoint 或 ToolMessage 回写前终止； - 重建 Runtime； - 恢复 Graph； -
验证读取账本结果，不重复执行工具。

### T-05：外部 POST 不确定结果

模拟外部系统已经成功，但客户端超时：

-   有下游幂等键：重试复用同一幂等键，逻辑副作用只产生一次；
-   无下游幂等能力：状态进入 unknown /
    needs_reconciliation，自动恢复不得再次 POST。

### T-06：多 Worker 并发 Claim

两个 Worker 并发争抢同一 pending Run： - 只能有一个 Claim 成功； -
只有一个 Worker 能执行 Graph； - 另一个 Worker 不得重复启动。

### T-07：Worker Lease 过期

模拟 Worker A 租约过期、Worker B 获得新 Lease： - A 的 fencing token
失效； - A 不得继续提交状态或执行新的副作用； - B 能按恢复策略继续。

### T-08：SSE 断线回放

1.  发布并持久化至少 30 个事件；
2.  客户端先消费到 seq=12；
3.  断开连接；
4.  重新以 after_seq=12 连接；
5.  收到 seq=13 到终态事件；
6.  不丢失、不重复、不乱序。

### T-09：Session 与 Checkpoint 不重复

连续进行多轮会话： - 每条用户消息在 Graph State 中只出现一次； - 历史
Assistant 消息不重复； - 服务重启后继续会话仍然正确。

### T-10：最终文本竞态

通过控制事件消费者延迟的测试，确保： - `_drive()` 完成时
`run.final_text` 已完整； - 非流式响应和数据库最终结果一致； -
LegacyEventAdapter 不承担唯一结果收集职责。

### T-11：预算统计

FakeChatModel 返回多个 Chunk： - 一次模型请求只计一次 model call； -
Tool Call 计数准确； - 达到预算后 Run 为 limit_exceeded。

## 10.3 故障注入测试

至少在以下位置提供可控故障注入点：

-   Run 创建后；
-   Worker Claim 后；
-   Graph 启动后；
-   Tool Ledger 写入 executing 后；
-   外部工具调用返回后；
-   Ledger 写入 succeeded 后；
-   Checkpoint 更新前后；
-   审批决策提交后；
-   事件持久化后、SSE 发送前。

测试不得依赖人工手动杀进程作为唯一验证方式。应提供可自动执行的故障注入测试；必要时再增加子进程级重启测试。

## 10.4 测试命令

以项目实际测试配置为准，至少提供：

``` bash
pytest backend/tests/test_agent_runtime.py -q
pytest backend/tests/test_agent_runtime_persistence.py -q
pytest backend/tests/test_agent_runtime_durable.py -q
pytest backend/tests/test_agent_runtime_e2e.py -q
```

如果目录或测试文件命名不同，按项目实际结构调整，并在交付报告中列出完整命令和结果。

------------------------------------------------------------------------

# 11. 实施阶段与任务拆分

不要一次性大改。按以下顺序推进。

## Phase 0：基线审查与设计确认

任务：

1.  检查 Git 分支、工作区和未提交改动；
2.  核对 Python、FastAPI、SQLAlchemy、LangChain、LangGraph、DeepAgents
    版本；
3.  读取 Runtime 全部核心代码和测试；
4.  检查 AgentRun / AgentApproval / AgentSession 模型及 Alembic 迁移；
5.  核对应用启动与关闭生命周期；
6.  核对当前 SSE API 和前端事件消费代码；
7.  确认生产数据库类型和 Checkpointer 配置；
8.  输出当前实现与本任务书的差异表。

交付： - `docs/agent-runtime/phase2-baseline-review.md` -
最终数据模型草案； - 关键流程时序图； - 风险和兼容性清单。

完成条件：设计与当前代码一致，不存在凭空假设。

## Phase 1：数据持久化基础

任务：

1.  扩展 AgentRun；
2.  扩展 AgentApproval；
3.  新增 AgentToolExecution；
4.  新增 AgentRunEvent；
5.  建立唯一约束和必要索引；
6.  编写 Alembic migration；
7.  实现 RunRepository / ToolExecutionRepository / EventRepository；
8.  关键持久化异常必须向上抛出；
9.  编写模型、迁移和 Repository 测试。

完成条件： - 数据库迁移可升级、可回滚； - 幂等约束在数据库层生效； -
Tool Ledger 和 Event Log 可持久化； - 原有测试通过。

## Phase 2：Worker Lease 与 Run 恢复

任务：

1.  实现 Worker Claim；
2.  实现租约和 Heartbeat；
3.  实现 fencing token；
4.  将新 Run 以 pending 持久化；
5.  由 Worker 领取并调用现有 `_drive()`；
6.  实现服务启动后的 stale Run 扫描；
7.  恢复前检查 Checkpoint 和 Tool Ledger；
8.  `get_run()` 改为数据库优先；
9.  `cancel_run()` 改为数据库状态和 Worker 协调；
10. 实现优雅关闭；
11. 测试双 Worker 竞争和进程重启。

完成条件： - 不依赖原进程 RunRegistry 也能查询 Run； -
进程重启后能发现并处理可恢复任务； - 同一 Run 不会被两个 Worker
同时有效执行； - waiting_approval 不会被普通 Worker 自动执行。

## Phase 3：Tool Execution Ledger 与副作用治理

任务：

1.  明确工具副作用分类；
2.  为 ToolAdapter 获取稳定 tool_call_id；
3.  执行前创建或读取 Ledger；
4.  成功结果持久化并可复用；
5.  将稳定 idempotency_key 传递给支持该能力的下游；
6.  对无法判断结果的调用标记 unknown；
7.  恢复时禁止重复执行 unknown；
8.  处理 Ledger 与 LangGraph Checkpoint 之间的崩溃窗口；
9.  对高风险工具继续保留 HITL；
10. 增加故障注入测试。

完成条件： -
工具成功后进程崩溃，不会因恢复而重复执行已确认成功的副作用； -
不支持幂等的外部调用在结果未知时不会被自动重放； -
只读工具可按策略安全恢复。

## Phase 4：HITL 审批持久化恢复

任务：

1.  审批动作增加稳定 action_id；
2.  决策提交使用数据库事务和行锁；
3.  审批接口从数据库加载 Run；
4.  重复审批幂等；
5.  冲突审批明确拒绝；
6.  审批通过后将 Run 交给 Worker 恢复；
7.  服务重启后能恢复 waiting_approval；
8.  验证审批与 Tool Ledger 协同。

完成条件： - 服务重启后仍可完成待审批任务； - 重复审批不重复执行工具； -
未批准、已拒绝的工具绝不执行。

## Phase 5：事件持久化和 SSE 回放

任务：

1.  RuntimeEvent 发布时持久化；
2.  sequence 数据库并发安全；
3.  SSE 通过事件仓库消费；
4.  支持 Last-Event-ID / after_seq；
5.  旧事件协议保持兼容；
6.  EventBus 改为通知优化，不再承担唯一事件存储；
7.  事件脱敏和保留策略；
8.  验证多 Worker 下 API 实例可消费事件。

完成条件： - API 实例与 Worker 不同进程时仍能收到事件； - SSE
断线后能从指定序号继续； - 内存队列溢出不导致持久化事件丢失。

## Phase 6：状态一致性、预算和回归

任务：

1.  修复 Session / Checkpoint 消息重复；
2.  修复最终文本收集竞态；
3.  修复模型调用计数；
4.  明确 steps、tool_calls、model_calls 统计口径；
5.  验证 completed / failed / cancelled / limit_exceeded /
    waiting_approval / needs_reconciliation；
6.  完成全量测试；
7.  更新部署和运维文档。

完成条件： - 旧功能无回归； - 所有本任务验收项有测试证据； -
部署配置和恢复操作有文档。

------------------------------------------------------------------------

# 12. 代码改造范围建议

以下是优先检查和可能改动的文件，不是要求机械地全部修改。

  -----------------------------------------------------------------------------------------------------
  文件                                                    预期职责变化
  ------------------------------------------------------- ---------------------------------------------
  `backend/app/services/agent_runtime/service.py`         从直接创建进程内 Task，调整为持久化
                                                          Run、Worker 执行、数据库恢复和事件流协调

  `backend/app/services/agent_runtime/lifecycle.py`       保留状态机和本地
                                                          Handle，补齐恢复状态/租约语义，避免 Registry
                                                          成为唯一来源

  `backend/app/services/agent_runtime/persistence.py`     Checkpointer
                                                          生产配置校验、连接生命周期、关闭与恢复能力

  `backend/app/services/agent_runtime/events.py`          RuntimeEvent 保持统一协议；EventBus
                                                          不再承担唯一持久化职责

  `backend/app/services/agent_runtime/tools/adapter.py`   tool_call_id
                                                          注入、Ledger、幂等键、执行状态管理

  `backend/app/services/agent_runtime/tools/policy.py`    工具副作用分类、权限和恢复策略协同

  `backend/app/services/agent_runtime/factory.py`         继续负责 DeepAgents 组装，不要把 Worker
                                                          调度逻辑塞进 Factory

  `backend/app/services/agent_runtime/context.py`         增加必要的
                                                          worker/lease/执行身份上下文，避免无边界扩张

  `backend/app/api/v1/agents.py`                          Run 创建、状态、取消、审批恢复、SSE 回放 API
                                                          适配

  `backend/app/models/agent.py`                           扩展 Run / Approval / 新增 ToolExecution /
                                                          RunEvent 模型，或拆分模型文件

  `backend/app/core/config.py`                            增加 Worker、Lease、Recovery、Event Retention
                                                          配置

  `backend/alembic/versions/`                             新增迁移，不改历史迁移

  `backend/tests/`                                        新增 durable
                                                          execution、恢复、幂等、故障注入和 SSE
                                                          回放测试
  -----------------------------------------------------------------------------------------------------

允许根据实际项目结构新增独立模块，例如：

``` text
backend/app/services/agent_runtime/
    repositories/
        run_repository.py
        tool_execution_repository.py
        event_repository.py
    worker.py
    recovery.py
    ...
```

具体目录结构由 Trae
根据现有项目风格确定，但必须保持职责清晰，避免把所有逻辑继续堆进
`service.py`。

------------------------------------------------------------------------

# 13. 配置与部署要求

## 13.1 Checkpointer

生产环境必须使用持久化 Checkpointer，推荐 PostgreSQL。

示意：

``` env
AGENT_CHECKPOINT_DSN=postgresql://...
```

注意当前代码使用的 `AsyncPostgresSaver` 依赖 `psycopg` 连接，最终 DSN
格式必须与当前安装版本要求一致，不得直接复制错误的 SQLAlchemy URL。

## 13.2 生产模式保护

当启用 Durable Recovery 或多 Worker 时：

-   `AGENT_CHECKPOINT_DSN=memory` 应启动失败或至少明确给出强告警；
-   不允许把内存 Checkpointer 宣称为持久化恢复；
-   PostgreSQL Checkpointer 的 setup 和连接关闭必须正确；
-   Worker 关闭时释放租约或等待租约过期；
-   数据库不可用时不得继续执行需要持久化保障的副作用工具。

## 13.3 部署文档

提供：

-   新增环境变量说明；
-   Alembic 迁移命令；
-   PostgreSQL Checkpointer 初始化说明；
-   Worker 数量和租约参数建议；
-   单实例部署方式；
-   多实例部署方式；
-   服务重启恢复流程；
-   unknown 副作用的人工核对流程；
-   事件日志保留和清理策略；
-   故障排查方法。

------------------------------------------------------------------------

# 14. 最终验收标准

以下所有 P0 项必须完成，才能认定本阶段完成。

## P0：必须通过

-   [ ] Run 状态以数据库为事实来源，不依赖内存 Registry 才能查询。
-   [ ] 相同幂等键并发请求不会创建两个 Run。
-   [ ] 两个 Worker 竞争同一 Run 时只有一个有效 Claim。
-   [ ] Worker Lease 有 Heartbeat、过期处理和 fencing token。
-   [ ] PostgreSQL Checkpointer 能跨 RuntimeService 实例读取同一 Thread
    状态。
-   [ ] 服务重启后，waiting_approval Run 可被查询和审批恢复。
-   [ ] 审批重复提交不会重复恢复或重复执行工具。
-   [ ] Tool Execution Ledger 有稳定 `(run_id, tool_call_id)` 唯一标识。
-   [ ] 工具已确认成功但 Graph 尚未完成时，恢复不会再次执行该副作用。
-   [ ] 外部副作用结果未知时，不会自动重放不安全工具。
-   [ ] 下游支持幂等键时，同一逻辑调用重试使用相同键。
-   [ ] 事件持久化且序号在多 Worker 下可靠。
-   [ ] SSE 可以从指定序号恢复。
-   [ ] Session / Checkpoint 不会重复追加历史消息。
-   [ ] 最终结果不受事件消费时序影响。
-   [ ] 模型调用统计不按 Chunk 数量累加。
-   [ ] 关键持久化失败会阻止后续不安全操作。
-   [ ] 旧 Agent Chat 和 SSE 事件兼容测试通过。

## P1：应完成

-   [ ] 多 Worker 并发压力测试；
-   [ ] 故障注入覆盖主要崩溃窗口；
-   [ ] Worker 优雅关闭；
-   [ ] Run / Event / Tool Ledger 清理策略；
-   [ ] 运维诊断接口或管理命令；
-   [ ] 可观测指标：队列长度、租约过期、恢复次数、unknown 工具数、Run
    耗时。

## 明确禁止的"伪完成"

以下情况不能算完成：

-   只把 Checkpointer DSN 改成 PostgreSQL；
-   只增加数据库字段，但恢复仍依赖内存 Registry；
-   只保存审批记录，但重启后无法恢复 Graph；
-   只记录 tool_call，但没有执行账本和幂等控制；
-   只传 `Idempotency-Key`，没有验证下游是否真正支持；
-   将 unknown 工具调用直接重试；
-   只在单元测试中证明一次工具调用，没有测试崩溃窗口；
-   只测试单 Worker，没有测试并发 Claim；
-   只测试 SSE 正常连接，没有断线回放；
-   测试未运行或失败，却在报告中写"全部通过"。

------------------------------------------------------------------------

# 15. 交付物清单

Trae 最终必须提交：

1.  **代码：** 完整实现本阶段 P0 能力。
2.  **迁移：** 新增 Alembic revision，升级与回滚均可验证。
3.  **测试：** 单元、集成、并发、恢复和故障注入测试。
4.  **架构说明：**
    `docs/agent-runtime/phase2-durable-execution-design.md`。
5.  **运维说明：**
    `docs/agent-runtime/phase2-deployment-and-recovery.md`。
6.  **测试报告：** `docs/agent-runtime/phase2-test-report.md`。
7.  **变更清单：** `docs/agent-runtime/phase2-change-log.md`。

测试报告至少记录：

-   测试环境与依赖版本；
-   数据库类型和迁移版本；
-   执行命令；
-   通过/失败/跳过数量；
-   故障注入结果；
-   未完成项；
-   已知限制；
-   生产部署前仍需人工确认的事项。

------------------------------------------------------------------------

# 16. 最终完成报告格式

完成后按以下格式汇报，不要只说"已完成"。

## 16.1 实施摘要

-   本阶段完成了什么；
-   哪些架构决策发生了变化；
-   哪些现有机制被保留。

## 16.2 文件变更

  文件   变更内容   变更原因
  ------ ---------- ----------

## 16.3 数据库变更

-   新增/修改表；
-   新增索引和唯一约束；
-   Migration revision；
-   Upgrade / Downgrade 验证结果。

## 16.4 核心流程验证

分别展示： - Run 创建与幂等； - Worker Claim / Lease； -
服务重启恢复； - HITL 审批恢复； - Tool Ledger； -
外部副作用未知状态； - SSE 断线回放； - Session / Checkpoint 去重。

## 16.5 测试结果

  测试类别     通过   失败   跳过 证据
  ---------- ------ ------ ------ ------
  单元测试                        
  集成测试                        
  并发测试                        
  故障注入                        
  回归测试                        

## 16.6 遗留风险

明确列出尚未解决的问题，尤其是： - 不支持幂等的外部系统； -
无法查询执行结果的副作用工具； - Checkpoint
与外部副作用之间无法原子提交的窗口； - 多实例部署限制； -
数据保留和清理策略。

## 16.7 部署步骤

给出从当前版本升级到本阶段版本的可执行命令、环境变量、迁移步骤、启动顺序和回滚步骤。

------------------------------------------------------------------------

# 17. 本阶段的架构判断标准

本阶段完成后，系统应具备以下能力边界：

  -----------------------------------------------------------------------
  能力                                本阶段目标
  ----------------------------------- -----------------------------------
  Agent Harness                       继续由 DeepAgents 提供

  Graph Execution                     继续由 LangGraph 提供

  Run Lifecycle                       平台数据库持久化管理

  Worker Coordination                 PostgreSQL Lease / Claim

  Graph State Recovery                LangGraph PostgreSQL Checkpointer

  Tool Side-effect Safety             Tool Execution Ledger +
                                      下游幂等键 + unknown 治理

  HITL Recovery                       审批记录持久化 + 同 Thread Resume

  Event Delivery                      持久化事件日志 + SSE Replay

  Session Consistency                 稳定消息身份和去重策略

  Observability                       继续由现有 Telemetry / Langfuse
                                      等承担，不替代业务状态存储
  -----------------------------------------------------------------------

**本阶段最终要实现的不是"Agent
能够保存状态"，而是：当服务进程、网络连接、Worker、模型请求或外部工具发生故障时，平台能够明确知道任务执行到了哪里、哪些副作用已经发生、哪些动作可以安全恢复、哪些动作必须人工核对，并且不会因为盲目重试造成重复业务操作。**

这才是企业级 Agent Runtime 的 Durable Execution 能力。
