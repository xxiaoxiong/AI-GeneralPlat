# AI-GeneralPlat Phase 2.1：Durable Execution 可靠性加固改造任务书

> **任务类型：** Agent Runtime 核心可靠性加固\
> **交付对象：** Trae / Claude Code / Codex 等 Coding Agent\
> **项目：**
> [xxiaoxiong/AI-GeneralPlat](https://github.com/xxiaoxiong/AI-GeneralPlat)\
> **审查基线：**
> `503e1a199c55792ca3144b95b9ce1fa7ba792707`（第二阶段改造）\
> **前置基线：**
> `36fe3fbe11cf8c040bfe25d55c5efbea2ab5550`（第一阶段改造）\
> **任务定位：** Phase 2.1，不是新功能扩张，也不是推翻现有执行引擎\
> **核心目标：** 将当前 Durable Execution
> 主体框架加固为可验证、可部署、可恢复的可靠执行内核

------------------------------------------------------------------------

## 0. 给执行 Agent 的最高优先级指令

你需要直接在 AI-GeneralPlat
代码仓库中实施本任务。不要只输出分析报告或伪代码。

### 0.1 执行原则

1.  开始前先检查当前分支、HEAD、工作区是否干净，并确认当前代码是否仍基于审查基线
    `503e1a19`。如果 HEAD
    已经变化，先分析新增提交，保留已有改造，不得覆盖用户后续工作。
2.  先完成代码结构梳理和修复方案，再分批实施；每一批完成后运行对应测试。
3.  以现有 DeepAgents + LangGraph + LangChain 架构为基础，不重写 Agent
    内核。
4.  不以新增抽象层、增加类数量或增加数据库表数量作为完成标准。优先复用现有模块，只有确实需要持久化事实或隔离并发时才增加模型/表。
5.  不得仅通过修改测试断言、mock 结果或跳过测试来使测试通过。
6.  所有涉及外部副作用、数据库状态、租约和恢复的修改，必须有正向、反向、并发及故障注入测试。
7.  对 MySQL、PostgreSQL、SQLite 的能力边界必须如实标注；不能用 SQLite
    通过代替生产数据库通过。
8.  不得声称实现了
    exactly-once。系统只能在明确的下游幂等契约下实现可控重试；无法确认的外部副作用必须进入人工核对。
9.  API
    现有响应契约尽量向后兼容。确需新增字段或端点时，优先新增，不随意改变旧字段语义。
10. 不要在本阶段顺手引入 Kafka、Celery、Temporal、Langfuse、MCP
    平台或完整 Workflow Runtime。这些属于后续阶段。

### 0.2 本任务不允许做的事

-   不得将 DeepAgents 替换回自研 ReAct Loop。
-   不得将 LangGraph Checkpoint、AgentRun、Tool Ledger、Session
    合并成一个大对象。
-   不得把所有工具统一标记为幂等。
-   不得在 Checkpoint 缺失时无条件重新执行整个 Agent。
-   不得在数据库提交失败或 Fencing 失败后继续发布成功事件。
-   不得用内存 Registry 作为 Run 状态的最终事实来源。
-   不得通过直接手工修改数据库状态作为正式的人工核对流程。
-   不得把 AST 检查或线程超时称为安全 Python 沙箱。

------------------------------------------------------------------------

## 1. 本轮审查结论与改造目标

### 1.1 当前架构已经具备的能力

第二阶段已建立以下主体模块：

  模块                       当前职责
  -------------------------- ----------------------------------------------------
  `AgentRun`                 Run 身份、输入快照、状态、执行归属、Lease、Attempt
  `RunWorker`                Claim、Heartbeat、Lease、恢复扫描、优雅关闭
  `LangGraph Checkpointer`   Graph State、节点断点、Interrupt / Resume
  `AgentToolExecution`       工具调用身份、执行状态、结果和重放决策
  `AgentRunEvent`            Run 事件持久化、事件序号、SSE 回放
  `AgentApproval`            HITL 动作身份、审批记录、决策
  `AgentSession`             用户对话历史
  `RunRegistry`              进程内活跃 Run 句柄与 asyncio Task 缓存

这套拆分方向正确，必须保留。

### 1.2 当前真正的问题

当前问题不在于缺少模块，而在于几个模块之间的**一致性协议没有完全闭环**：

-   Run Lease 失效后，旧 Worker 仍可能提交状态或写入 Tool Ledger。
-   `_commit()` 返回 Fencing 失败后，部分调用路径仍会继续写 Session
    和发布终态事件。
-   Checkpoint 默认是 Memory，但 Docker 使用两个 Uvicorn Worker。
-   Checkpoint
    缺失时，恢复层允许部分外部幂等工具重新从输入执行，可能产生新的
    `tool_call_id`。
-   Tool Ledger 记录了
    `worker_id/lease_version`，但写入条件没有真正校验执行权。
-   工具 Handler 可能把失败包装为普通字符串，导致 Ledger
    将失败误记为成功。
-   API 幂等前置检查没有验证请求摘要，可能把不同请求错误复用为同一个
    Run。
-   HITL 恢复会读取 Run 下所有历史审批决策，没有严格限定当前 Interrupt
    批次。
-   Session JSON 历史没有并发版本控制，结果写回也没有按 Run 幂等。
-   `needs_reconciliation` 缺少可执行的人工核对和恢复 API。
-   执行预算、Session Thread、事件通知、部署迁移还存在跨进程边界问题。

### 1.3 Phase 2.1 的完成定义

本阶段不是追求"所有故障都自动恢复"，而是做到：

1.  任何 Worker 都必须先获得有效执行权，才能改变 Run 或 Tool Ledger。
2.  租约失效、状态提交失败、账本提交失败时，执行必须停止推进。
3.  不确定的外部副作用不会被无条件重放。
4.  Run 状态只能按照数据库级状态机单向迁移，终态不能被普通失败路径覆盖。
5.  Checkpoint、Run、Tool Ledger、Event、Session 之间的恢复边界明确。
6.  同一个请求幂等重试不会重复创建 Run、重复追加消息或改变响应协议。
7.  同一个 Session 的并发执行不会破坏共享 Graph Thread。
8.  人工核对有正式的授权、审计、状态迁移和恢复路径。
9.  Docker 多进程部署使用共享、持久化的 Checkpointer。
10. 关键故障场景能通过真实数据库和多进程测试验证。

------------------------------------------------------------------------

## 2. 优先级总览

  优先级   改造主题                             是否阻塞进入下一阶段
  -------- ------------------------------------ -------------------------------------
  P0       Checkpointer 持久化与多进程部署      是
  P0       Run Lease / Fencing / 状态机闭环     是
  P0       Tool Ledger 执行权与调用身份一致性   是
  P0       Checkpoint 缺失与副作用恢复安全      是
  P0       工具执行结果语义统一                 是
  P0       数据库迁移与部署启动流程             是
  P1       API 幂等与 Session 消息一致性        是，至少完成关键路径
  P1       HITL 多轮审批与人工核对              是
  P1       Worker 生命周期、取消与 Registry     是
  P1       持久化执行预算                       是
  P1       SSE 订阅、事件序号与持久化性能       建议本阶段完成
  P1       工具安全策略基线                     是，尤其是 SSRF、凭据和 Python 执行
  P2       运行数据归档、保留策略和长稳压测     可在本阶段后半段完成
  P2       可观测后端集成                       不属于本阶段，保留接口即可

**执行顺序：先 P0，再 P1，最后 P2。P0 未全部通过验收，不得宣称 Durable
Execution 生产可靠性完成。**

------------------------------------------------------------------------

# 第一部分：P0 核心可靠性加固

## 3. P0-01：Checkpointer 持久化与多进程部署

### 3.1 当前问题

涉及文件：

-   `backend/app/core/config.py`
-   `backend/.env.example`
-   `backend/app/services/agent_runtime/persistence.py`
-   `backend/app/main.py`
-   `backend/Dockerfile`
-   `docker-compose.yml`
-   `backend/requirements.txt`

当前事实：

1.  `AGENT_CHECKPOINT_DSN` 默认值为 `memory`。
2.  `CheckpointerFactory` 在 Memory 模式下创建 `InMemorySaver`。
3.  Dockerfile 使用 `uvicorn ... --workers 2`。
4.  FastAPI lifespan 在每个 Uvicorn Worker 进程中都会启动一个
    RunWorker。
5.  每个进程的 `CheckpointerFactory` 和 `InMemorySaver`
    都是进程内单例，不会跨进程共享。
6.  Docker Compose 没有设置 `AGENT_CHECKPOINT_DSN`。
7.  `.env.example` 没有给出 Checkpointer 的可执行配置。
8.  PostgreSQL 分支使用 `psycopg.AsyncConnection`，但当前 requirements
    没有明确安装 `langgraph-checkpoint-postgres` 和 `psycopg`。
9.  配置注释和运维文档使用 `postgresql+asyncpg://`，而当前 Checkpointer
    代码直接把 DSN 传给 psycopg。两者连接 URL 格式不一致。

这意味着当前默认 Docker 部署不能提供跨进程的持久化 Graph State。

### 3.2 改造要求

**A. 明确环境模式**

建议提供以下模式：

  ----------------------------------------------------------------------------------
  环境                    Checkpointer            约束
  ----------------------- ----------------------- ----------------------------------
  单进程开发/单元测试     Memory 或 SQLite        Memory 只允许非 Durable 测试

  单机持久化测试          SQLite                  文件必须持久化，连接生命周期明确

  Docker 多 Worker / 生产 PostgreSQL              所有 Worker 共享同一个
                                                  Checkpointer

  多容器部署              PostgreSQL              禁止各容器使用各自本地文件
                          或明确支持的共享后端    
  ----------------------------------------------------------------------------------

生产配置必须明确禁止：

``` text
AGENT_RUNTIME_ENABLED=true
AGENT_CHECKPOINT_DSN=memory
```

建议在应用启动阶段进行配置校验。发现 Durable Runtime 启用但 Checkpointer
为 Memory 时，生产环境直接拒绝启动，而不是运行到故障后才发现无法恢复。

**B. 修复 PostgreSQL Checkpointer**

1.  明确使用 LangGraph 官方 PostgreSQL Checkpointer 对应依赖。
2.  在 requirements 中增加并锁定兼容版本：
    -   `langgraph-checkpoint-postgres`
    -   `psycopg[binary,pool]` 或项目实际采用的 psycopg 安装方式。
3.  统一 DSN 规范。若使用 psycopg 原生连接，应使用 psycopg 支持的
    PostgreSQL URI，例如 `postgresql://...`，不要把 SQLAlchemy 的
    `+asyncpg` Driver 标记直接传给 psycopg。
4.  确保 Checkpointer 和 Store 使用的驱动、版本及连接方式一致。
5.  启动时执行 Checkpointer
    `setup()`，并在启动日志中记录后端类型和健康状态，不记录密码。
6.  不允许 Checkpointer 初始化失败后静默降级到 Memory。

**C. 生命周期管理**

在 `CheckpointerFactory` 中补充：

-   `close()` / `aclose()` 生命周期接口。
-   SQLite / PostgreSQL 连接正常关闭。
-   Store 连接正常关闭。
-   DSN 变更时关闭旧连接，避免连接泄漏。
-   初始化失败时释放已创建的连接。
-   并发初始化时只允许创建一个有效实例。

在 FastAPI lifespan 关闭阶段调用关闭接口。

**D. Docker 配置**

-   Docker Compose 必须明确设置 Checkpointer DSN。
-   推荐提供 PostgreSQL Checkpointer 服务或支持外部 PostgreSQL 地址。
-   如果使用 PostgreSQL 服务，增加持久化 Volume、健康检查和启动依赖。
-   不要把数据库密码硬编码在 Dockerfile。
-   Docker 多 Worker 必须共享同一个 PostgreSQL Checkpointer。
-   对 SQLite 单进程开发模式和 PostgreSQL 多进程模式明确区分。

### 3.3 验收标准

-   [ ] Durable Runtime 开启且 Checkpointer 为 Memory
    时，生产模式启动失败并给出明确提示。
-   [ ] 两个独立 Python 进程连接同一 PostgreSQL Checkpointer，进程 A
    写入，进程 B 能读取和恢复。
-   [ ] Worker A 写入 Checkpoint 后被强制终止，Worker B 能从同一 Thread
    恢复。
-   [ ] 同一 Session 的 Graph State 在不同 Uvicorn Worker 间可见。
-   [ ] Checkpointer 初始化失败时不会静默切换 Memory。
-   [ ] 应用正常关闭后 PostgreSQL / SQLite 连接被释放。
-   [ ] Docker Compose 默认配置与 README 部署说明一致。

------------------------------------------------------------------------

## 4. P0-02：Run Lease、Fencing 与数据库状态机闭环

### 4.1 当前问题

涉及文件：

-   `backend/app/services/agent_runtime/worker.py`
-   `backend/app/services/agent_runtime/repositories/run_repository.py`
-   `backend/app/services/agent_runtime/service.py`
-   `backend/app/services/agent_runtime/lifecycle.py`

当前 `RunRepository.update_status()`：

-   可选传入 `lease_version`。
-   传入版本时只检查 `AgentRun.lease_version`。
-   不检查 `worker_id`。
-   不检查 `lease_expires_at` 是否仍有效。
-   不检查当前状态是否允许迁移。
-   当 `lease_version=None` 时，直接按 Run ID 更新状态。

当前 `heartbeat()` 检查 Worker ID、Lease Version 和 Running
状态，但没有检查原租约是否已经过期。因此旧 Worker 在租约到期、但尚未被新
Worker Claim 时，仍可能续租。

此外，`_commit()` 返回 `False` 表示 Fencing 失败，但 `_drive()`
的部分完成、失败和审批路径没有强制停止后续操作。

### 4.2 改造要求

**A. 建立统一执行权校验**

定义统一的执行权身份：

``` text
RunExecutionLease
├── run_id
├── worker_id
├── lease_version
└── lease_expires_at
```

数据库写入必须同时校验：

-   Run ID。
-   Worker ID。
-   Lease Version。
-   Run 当前状态。
-   Lease 未过期。

时间比较优先使用数据库时间，避免不同机器时钟偏差导致租约判断不一致。

**B. Heartbeat 严格续租**

`heartbeat()` 必须满足：

``` text
run_id 匹配
AND worker_id 匹配
AND lease_version 匹配
AND status = running
AND lease_expires_at > database_now
```

只有满足条件才能续租。

租约已经过期，即使尚未被其他 Worker 领取，原 Worker
也不得自行恢复租约；必须重新经过 Claim。

**C. 所有状态提交必须经过 Fencing**

`update_status()` 应要求调用方提供执行身份，或提供明确的无 Lease
状态迁移专用接口。

不能继续允许普通运行状态更新通过 `lease_version=None` 绕过 Fencing。

对于 pending Run 的用户取消等无 Lease 操作，必须采用单独的 CAS：

``` text
UPDATE agent_runs
SET status = cancelled
WHERE id = :run_id
  AND status = pending
```

不得用无条件 Run ID 更新覆盖正在执行的 Run。

**D. 增加数据库级合法状态迁移**

在 Repository 层实现：

-   `expected_status`。
-   `target_status`。
-   当前状态允许迁移的校验。
-   必要的 Lease 校验。
-   失败时返回明确冲突类型。

终态：

``` text
completed
failed
cancelled
limit_exceeded
```

一旦进入终态，普通 Runtime 路径不得再次修改为其他终态。

**E. 引入统一 Lease Lost 异常**

建议引入：

``` python
class RunLeaseLost(Exception):
    pass
```

当状态提交返回 `False` 时：

1.  不得继续 Session 写回。
2.  不得发布 RUN_COMPLETED / RUN_FAILED。
3.  不得继续产生新的工具副作用。
4.  立即中止当前执行。
5.  记录 Fencing 拒绝日志。
6.  保留数据库中当前有效 Worker 的状态。

`_commit()`
不应只返回一个容易被忽略的布尔值。可以返回结果对象，但所有调用点必须显式处理。

**F. 修复失败路径覆盖终态的问题**

当前 `_fail()` 捕获 `transition()` 异常后继续更新数据库状态，可能造成：

``` text
Run 已 completed
    ↓
Session 写回发生异常
    ↓
进入 _fail()
    ↓
内存状态迁移失败被吞掉
    ↓
数据库状态被更新成 failed
```

必须修复：

-   不能吞掉非法状态迁移后继续写库。
-   完成态不能被普通失败路径覆盖。
-   Session 写回失败不能反向改变 Run 的执行结果。
-   已经提交的终态必须保持终态。
-   数据库 CAS 是最终状态权威，不依赖进程内状态机单独保证。

### 4.3 验收标准

-   [ ] Worker A 租约过期后，即使 Worker B 尚未 Claim，A 也不能续租。
-   [ ] Worker A 租约过期后，不能提交 Run 状态。
-   [ ] Worker B Claim 后，Worker A 不能覆盖 Run 状态。
-   [ ] Worker A 不能在 Worker B 接管后修改 Tool Ledger。
-   [ ] `_commit()` Fencing 失败时，不发生 Session 写回或终态事件发布。
-   [ ] `completed → failed`、`failed → completed`
    等非法迁移在数据库层被拒绝。
-   [ ] pending 取消与 Worker Claim 并发时只有一个操作成功。
-   [ ] 多 Worker 并发测试至少覆盖真实独立进程，不仅是同一事件循环中的
    asyncio Task。

------------------------------------------------------------------------

## 5. P0-03：Tool Execution Ledger 的执行权与身份一致性

### 5.1 当前问题

涉及文件：

-   `backend/app/services/agent_runtime/tools/adapter.py`
-   `backend/app/services/agent_runtime/repositories/tool_execution_repository.py`
-   `backend/app/services/agent_runtime/tools/classification.py`
-   `backend/app/models/agent.py`

当前问题：

1.  `mark_executing()` 接收 `worker_id` 和 `lease_version`，但只按
    `exec_id` 更新，没有校验当前 Run 的执行权。
2.  `mark_succeeded()`、`mark_failed()`、`mark_unknown()`
    等终态写入没有执行者校验。
3.  Adapter 的 `_ledger_mark()` 没有把 Worker 身份传给 Repository。
4.  Adapter 多处调用 `_ledger_mark()` 后没有检查返回值。
5.  `begin()` 找到已有 `(run_id, tool_call_id)` 记录时直接复用，没有验证
    `tool_name`、`args_hash` 是否一致。
6.  相同调用 ID、不同参数可能错误返回旧缓存结果。
7.  `mark_executing()` 允许 executing / unknown 状态重新进入
    executing，必须与副作用等级、稳定身份和当前 Lease 联合判断。

### 5.2 改造要求

**A. Ledger 所有写入都要验证 Lease**

至少包括：

-   `begin`
-   `mark_executing`
-   `mark_succeeded`
-   `mark_failed`
-   `mark_unknown`
-   `mark_needs_reconciliation`

每个状态写入必须校验：

``` text
AgentRun.id
AgentRun.worker_id
AgentRun.lease_version
AgentRun.status
AgentRun.lease_expires_at
```

建议在同一数据库事务中完成执行权检查与 Ledger 条件更新，不能先查询
Lease、提交事务，再单独更新 Ledger。

**B. 强制检查调用身份冲突**

发现已有记录时：

``` text
(run_id, tool_call_id)
```

必须比较：

-   tool_name。
-   args_hash。
-   idempotency_key。

任何身份或参数不一致都应返回明确冲突，不能返回缓存结果，也不能继续执行。

**C. 严格状态迁移**

明确工具状态机：

``` text
prepared
   ↓
executing
   ├── succeeded
   ├── failed
   └── unknown
          ↓
   needs_reconciliation
          ↓
   人工核对后进入明确的解决状态
```

要求：

-   `succeeded` 不可被普通路径覆盖。
-   `needs_reconciliation` 不允许普通重试覆盖。
-   重新执行必须经过副作用等级判定。
-   已有执行记录的参数摘要必须匹配。
-   所有状态迁移失败都必须被 Adapter 感知。

**D. Adapter 必须处理状态写入失败**

不能再出现：

``` python
await self._ledger_mark(rec.id, "succeeded")
# 不检查结果，继续发布 TOOL_COMPLETED
```

应当：

-   明确区分状态已成功迁移、状态冲突、Lease 失效和数据库异常。
-   Ledger 关键写入失败时抛出异常并停止 Graph 推进。
-   不能把账本未成功提交的工具调用宣告为成功。
-   不能将旧 Worker 的账本写入错误降级成普通工具错误消息。

### 5.3 验收标准

-   [ ] Worker A 不能修改 Worker B 接管后的 Ledger。
-   [ ] 相同 tool_call_id + 不同 args_hash 必须冲突。
-   [ ] 相同 tool_call_id + 不同 tool_name 必须冲突。
-   [ ] Ledger 更新失败时不发布 TOOL_COMPLETED。
-   [ ] 成功记录不能被失败覆盖。
-   [ ] 未知副作用记录不能被普通 Agent 自动覆盖。
-   [ ] 并发执行同一个 tool_call_id 时，最多只有一个有效执行者。

------------------------------------------------------------------------

## 6. P0-04：Checkpoint 缺失、损坏与副作用恢复

### 6.1 当前问题

涉及文件：

-   `backend/app/services/agent_runtime/service.py`
-   `backend/app/services/agent_runtime/worker.py`
-   `backend/app/services/agent_runtime/persistence.py`
-   `backend/app/services/agent_runtime/tools/classification.py`

当前 `_recovery_check()`：

1.  检查未决 Tool Ledger。
2.  如果工具是 read_only，允许重试。
3.  如果工具是 external_idempotent，允许重试。
4.  查询 Checkpoint。
5.  Checkpoint 不存在时返回 `RECOVERY_FRESH`，从输入快照重新执行。

问题在于：

如果 Tool Ledger 存在一个结果未知的 `http_post`，但 Checkpoint
已经丢失，系统重新调用模型可能产生新的 `tool_call_id`。

此时：

``` text
原 tool_call_id = call-A
原 idempotency_key = hash(run_id + call-A)

恢复后重新推理：
新 tool_call_id = call-B
新 idempotency_key = hash(run_id + call-B)
```

原来的外部幂等键无法复用。

另外，当前 `EXTERNAL_IDEMPOTENT_TOOLS = {"http_post"}` 将任意 HTTP POST
都视为支持下游幂等，缺乏真实下游契约依据。

### 6.2 改造要求

**A. 明确恢复决策表**

  ---------------------------------------------------------------------------------------------------
  Checkpoint              Ledger 状态                恢复策略
  ----------------------- -------------------------- ------------------------------------------------
  存在且可读              全部已确认                 从 Checkpoint 继续

  存在且可读              read_only 未决             允许安全重试

  存在且可读              external_idempotent 未决   仅在下游能力明确且复用原 key 时重试

  存在且可读              non_idempotent 未决        needs_reconciliation

  不存在                  无未决副作用               可按明确策略从输入重新开始，并记录 recovery mode

  不存在                  external_idempotent 未决   默认
                                                     needs_reconciliation，不允许重新推理生成新调用
                                                     ID

  不存在                  non_idempotent 未决        needs_reconciliation

  不存在                  Checkpoint                 安全阻断，记录 recovery_error
                          已损坏且执行进度无法确认   

  读取异常                无法确认恢复安全性         不得直接 fresh restart
  ---------------------------------------------------------------------------------------------------

**B. 幂等能力必须由工具配置声明**

不要全局把 `http_post` 认定为幂等。

需要由具体工具配置声明：

``` text
side_effect_class
downstream_idempotency_supported
idempotency_key_header
retry_policy
external_operation_lookup
```

只有明确配置下游支持幂等键、并且下游确实执行去重的目标，才能标记为
`external_idempotent`。

未声明的 HTTP POST 一律按 `non_idempotent` 处理。

**C. 恢复必须保持原调用身份**

-   有 Checkpoint：从原 Graph ToolCall 恢复，使用原 `tool_call_id`。
-   没有 Checkpoint：不能通过重新推理来冒充原调用恢复。
-   对无法恢复原调用身份的未决副作用，进入人工核对。
-   对已 succeeded 的工具调用，返回缓存结果，不重复执行。

**D. Checkpoint 损坏处理**

补充真实文件/数据库损坏测试，不能只 mock `get_thread_state()` 返回
error。

### 6.3 验收标准

-   [ ] Checkpoint 丢失 + read_only Ledger 能安全恢复。
-   [ ] Checkpoint 丢失 + external_idempotent unknown
    必须阻止重新生成新调用 ID。
-   [ ] Checkpoint 丢失 + non_idempotent unknown 必须进入
    needs_reconciliation。
-   [ ] Checkpoint 可读时，同一个工具重放复用原 tool_call_id 和幂等键。
-   [ ] Checkpoint 损坏不会自动回退到 fresh。
-   [ ] 所有恢复决策写入日志和 Run recovery_error / recovery metadata。

------------------------------------------------------------------------

## 7. P0-05：统一 ToolResult，修复"失败被记为成功"

### 7.1 当前问题

涉及文件：

-   `backend/app/services/agent_runtime/tools/adapter.py`
-   `backend/app/services/tools/__init__.py`
-   `backend/app/services/tools/notify_tools.py`
-   `backend/app/services/tools/network_tools.py`
-   `backend/app/services/tools/code_tools.py`

当前 Adapter 使用 `raise_on_error=True`，但部分 Handler
内部捕获异常并返回普通字符串。

例如：

-   `send_email_tool` 发生异常后返回"邮件发送失败"字符串。
-   `webhook_notify` 发生异常后返回"Webhook 发送失败"字符串。
-   HTTP POST 返回 HTTP 500 时仍然返回响应文本。
-   Python 执行错误和超时会返回普通错误字符串。

Adapter 无法区分正常结果和错误字符串，可能将它们写为：

``` text
status = succeeded
```

这会污染恢复决策。

### 7.2 改造要求

**A. 定义统一工具执行结果**

建议：

``` python
@dataclass
class ToolResult:
    status: str
    output: Any = None
    error: str | None = None
    retryable: bool = False
    outcome_known: bool = True
    external_operation_id: str | None = None
    metadata: dict = field(default_factory=dict)
```

状态至少区分：

-   `succeeded`
-   `failed`
-   `unknown`

注意：`unknown` 不是普通失败，而是无法确认外部操作是否已经生效。

**B. 统一异常语义**

-   确认未产生副作用的参数校验失败：failed。
-   只读工具执行失败：failed，可按策略重试。
-   外部请求超时、连接中断：通常 unknown。
-   远端已确认成功：succeeded。
-   远端返回错误码：按工具契约判断 failed 或 unknown，不得统一视为
    succeeded。
-   账本提交失败：不允许继续执行。

**C. 修复现有 Handler**

-   `send_email_tool` 不得把错误伪装为成功字符串。
-   `webhook_notify` 检查 HTTP 响应状态和远端响应体。
-   `http_post` 不得将所有 HTTP 状态码都当作业务成功。
-   Python 执行错误和超时必须返回明确失败结果。
-   其他工具逐一盘点是否存在 `except Exception: return "错误..."` 语义。

**D. 阻塞操作不得占用 Worker 事件循环**

`send_email_tool` 当前声明为 async，但内部使用同步 `smtplib`。需要改为：

-   使用真正的异步 SMTP 客户端；或
-   将同步 SMTP 调用放入受控线程池。

同时保证超时、取消和结果未知语义明确，避免阻塞 Heartbeat。

### 7.3 验收标准

-   [ ] Handler 返回业务错误时 Ledger 不得标记 succeeded。
-   [ ] HTTP 500 不得被无条件记录为成功。
-   [ ] 邮件服务器连接中断等不确定情况进入 unknown。
-   [ ] Webhook 超时不会被当作成功。
-   [ ] 所有工具均经过统一结果归一化。
-   [ ] 长时间 SMTP 调用不会阻塞 Worker Heartbeat。

------------------------------------------------------------------------

## 8. P0-06：数据库迁移与生产启动流程

### 8.1 当前问题

涉及文件：

-   `backend/app/core/database.py`
-   `backend/app/main.py`
-   `backend/Dockerfile`
-   `docker-compose.yml`
-   `backend/alembic/`
-   `README.md`

当前 `init_db()` 使用：

``` python
Base.metadata.create_all()
```

该方法可以创建不存在的表，但不能自动给已有表增加新字段。

README 的本地启动流程要求执行：

``` bash
alembic upgrade head
```

但 Dockerfile 的启动命令直接运行 Uvicorn，没有执行迁移。

因此，已有数据库升级时不能依赖 `create_all()` 自动完成 Phase 2 Schema
升级。

此外，Docker 使用两个 Uvicorn Worker，不能让两个进程同时承担数据库
Schema 迁移任务。

### 8.2 改造要求

1.  增加独立的生产启动入口或 entrypoint。
2.  在启动多个 Uvicorn Worker 之前执行一次 `alembic upgrade head`。
3.  迁移失败时禁止启动 API。
4.  迁移必须由单一启动步骤负责，不能由每个 Uvicorn Worker 并发执行。
5.  `create_all()` 仅保留给明确的开发/测试场景，生产环境以 Alembic
    为准。
6.  不修改历史迁移的既有语义；新增修复应建立独立 migration。
7.  补充 MySQL 8 真实迁移测试。
8.  PostgreSQL Checkpointer 所需依赖和业务库迁移依赖分别管理。
9.  清理 `backend/runlog.txt` 至 `runlog6.txt` 等临时调试日志，并补充
    `.gitignore` 规则。

### 8.3 验收标准

-   [ ] 全新 MySQL 数据库可以升级到 head。
-   [ ] 已有第二阶段之前的 MySQL 数据库可以升级到 head。
-   [ ] 升级后所有新增字段、索引、唯一约束和外键正确。
-   [ ] Docker 启动前只执行一次迁移。
-   [ ] 迁移失败时服务不会进入可接收请求状态。
-   [ ] SQLite migration 回归继续通过。
-   [ ] downgrade 仅在明确允许时执行，不能作为默认发布流程。

------------------------------------------------------------------------

# 第二部分：P1 业务一致性与执行管理

## 9. P1-01：修复 API 幂等与 Session 写入顺序

### 当前问题

涉及：

-   `backend/app/api/v1/agents.py`
-   `backend/app/services/agent_runtime/service.py`
-   `backend/app/services/agent_runtime/repositories/run_repository.py`

`/chat` 和 `/invoke` 在 API 层提前查询幂等键：

``` python
existing = await RunRepository(s).get_by_idempotency_key(...)
if existing is not None:
    return existing_run
```

这段逻辑没有比较 `request_hash`。

后果：

-   同一 Key、不同请求内容可能错误返回旧 Run。
-   同一个用户跨 Agent 使用相同 Key 可能返回错误 Run。
-   `/chat` 原本是 SSE 请求，幂等命中却直接返回普通
    JSON，响应协议不一致。
-   两个并发请求可能都在查询阶段未命中，随后重复创建 Session
    或写入消息。

### 改造要求

1.  幂等检查统一收敛到 Service / Repository。
2.  同 Key、同请求摘要：返回同一个 Run。
3.  同 Key、不同请求摘要：HTTP 409。
4.  请求摘要必须包含明确的业务范围：
    -   user_id
    -   agent_id
    -   session_id
    -   endpoint / operation type
    -   用户本次输入
    -   必要的 Agent 配置版本
5.  不要把易变的服务端临时字段或明文凭据纳入摘要。
6.  幂等命中时不重复追加 Session 用户消息。
7.  `/chat` 的流式幂等命中必须返回兼容的 SSE 事件流，不能从 SSE 变成
    JSON。
8.  新 Session 创建、Run
    创建和用户消息追加尽量放进同一业务事务；至少保证通过唯一业务请求 ID
    实现幂等。
9.  对并发创建 Session 的情况增加测试，防止同一个幂等请求产生孤立
    Session。

### 验收标准

-   [ ] 相同 Key、相同请求重复 10 次，只存在一个 Run。
-   [ ] 相同 Key、不同 message 返回 409。
-   [ ] 相同 Key、不同 agent_id 返回冲突。
-   [ ] 相同 Key、不同 session_id 返回冲突。
-   [ ] 同一个流式请求重试后仍然返回 SSE。
-   [ ] 同一个请求不会重复追加用户消息。
-   [ ] 并发请求不会产生孤立 Session。

------------------------------------------------------------------------

## 10. P1-02：Session 并发控制与最终结果写回

### 当前问题

涉及：

-   `backend/app/models/agent.py`
-   `backend/app/services/agent_runtime/persistence.py`
-   `backend/app/services/agent_runtime/service.py`
-   `backend/app/api/v1/agents.py`

当前 `make_thread_id()` 在有 session_id 时使用：

``` text
agent-{agent_id}-session-{session_id}
```

同一 Session 的多个 Run 会共享同一个 LangGraph Thread。

这是合理的连续对话设计，但缺少 Session 级并发控制。

同时：

-   Session 的 messages 是 JSON 数组。
-   `_save_session()` 采用读取后整体覆盖。
-   `_write_back_session()` 采用读取后 append 再整体覆盖。
-   没有乐观锁版本号。
-   Assistant 写回没有 `source_run_id` 唯一标识。
-   Run 完成与 Session 写回是两个独立事务。

### 改造要求

**A. 当前阶段先保证一个 Session 同时只有一个主 Run 执行**

不要在本阶段引入同一 Session 的并行主 Agent 执行。

增加 Session 执行协调机制：

-   Claim Session 执行权。
-   当前 Session 有活跃 Run 时，后续 Run 排队或返回明确冲突。
-   Session 执行权使用数据库原子更新。
-   Worker 故障时 Session Lease 可过期接管。
-   Run 完成、失败、取消或进入暂停状态时按规则释放/保留 Session 执行权。

**B. Session 消息写入必须幂等**

为消息增加稳定身份：

``` text
message_id
source_run_id
role
content
created_at
```

如果本阶段继续保留 JSON messages，至少需要：

-   为每条消息生成稳定 message_id。
-   Assistant 消息带 source_run_id。
-   写回前检查 source_run_id 是否已存在。
-   增加 Session version 乐观锁。
-   冲突时基于最新 Session 内容合并重试，不能盲目覆盖。

**C. Run 终态与 Session 写回解耦**

建议使用轻量 Outbox：

``` text
Run completed
    ↓
同一事务提交 Run 终态 + SessionResultOutbox
    ↓
Outbox Worker 写回 Assistant 消息
    ↓
按 source_run_id 去重
    ↓
标记 outbox delivered
```

要求：

-   Session 写回失败不改变 Run 的 completed 状态。
-   写回失败可以重试。
-   重试不会重复追加 Assistant 消息。
-   客户端断线不影响最终写回。

### 验收标准

-   [ ] 同一个 Session 两个 Run 并发提交时只有一个获得执行权。
-   [ ] 两个 Session 之间仍可并行执行。
-   [ ] Assistant 写回失败后 Run 仍保持正确终态。
-   [ ] Session 写回任务重试 3 次只追加一条 Assistant 消息。
-   [ ] 并发用户消息写入不会丢失历史内容。
-   [ ] 服务重启后未完成的 Session 写回可以恢复。

------------------------------------------------------------------------

## 11. P1-03：HITL 多轮审批、并发决策和人工核对

### 当前问题

涉及：

-   `backend/app/services/agent_runtime/service.py`
-   `backend/app/services/agent_runtime/repositories/`
-   `backend/app/api/v1/agents.py`
-   `backend/app/models/agent.py`

当前 `resume_run()` 从一个 Run 的所有 `AgentApproval`
记录中读取所有已经决策的记录，构造 `graph_decisions`。

这可能把历史 Interrupt 的审批决策带入当前 Interrupt。

此外：

-   `_match_approval_row()` 允许没有 action_id 时按历史记录索引匹配。
-   `_record_approval_decision()` 先 SELECT 再写入，没有数据库级 CAS。
-   两个审批人并发提交不同决策时可能产生覆盖。
-   `needs_reconciliation` 没有正式恢复接口。
-   运维文档建议把 Ledger 改成 failed 后调用 `/resume`，但当前
    `resume_run()` 只允许 `waiting_approval`，而且
    `needs_reconciliation` Ledger 状态被普通 `_finish()`
    保护，不能按普通路径改成 failed。

### 改造要求

**A. 当前 Interrupt 批次隔离**

从 Checkpoint 当前 Interrupt 提取：

``` text
interrupt_id
action_id
tool_call_id
tool_name
args_hash
```

审批时只处理当前 Interrupt 对应的动作。

禁止把历史 Interrupt 的决策自动合并进当前 `Command(resume=...)`。

**B. 严格审批动作匹配**

-   action_id 必须精确匹配。
-   不允许跨批次按数组下标猜测审批对象。
-   一个 Interrupt 批次的动作集合必须完整、明确。
-   如支持部分批准，必须定义部分批准的图内语义；否则要求每个动作都有明确决策。
-   重复相同决策返回幂等成功。
-   冲突决策返回 409。

**C. 审批决策 CAS**

采用数据库条件更新：

``` text
WHERE action_id = ?
AND decision IS NULL
```

并发审批时只有一个决策成功。

另一方读取最新结果后： - 相同决策：幂等返回。 -
不同决策：ApprovalConflict。

**D. 正式实现 Reconciliation API**

增加独立于 HITL 的人工核对流程，例如：

``` text
POST /runs/{run_id}/reconcile
```

该接口必须具备：

-   管理员/授权运维角色检查。
-   Run 状态检查。
-   Tool Execution 记录检查。
-   action_id / tool_call_id 精确匹配。
-   人工决策和说明。
-   外部 operation ID。
-   操作人、时间和审计记录。
-   条件状态迁移。

至少支持三种明确处置：

1.  `confirmed_succeeded`：下游已成功，补录结果和外部操作 ID。
2.  `confirmed_not_executed`：确认下游未执行，允许按安全策略恢复。
3.  `unresolved`：仍无法确认，保持阻断。

如果 Checkpoint
已丢失，人工核对完成也不能自动重新推理生成新工具调用；应要求明确的人工重启策略或创建新
Run。

### 验收标准

-   [ ] 同一个 Run 连续出现两个 Interrupt，第二次只恢复第二批动作。
-   [ ] 多个 action_id 的审批结果严格对应。
-   [ ] 两个用户同时提交冲突决策时只有一个成功。
-   [ ] 相同决策重复提交不重复执行工具。
-   [ ] 未授权用户不能进行 Reconciliation。
-   [ ] `needs_reconciliation` 能通过正式接口被解决、终止或保持阻断。
-   [ ] 运维文档不再要求直接手工修改数据库状态。

------------------------------------------------------------------------

## 12. P1-04：Worker、Registry 与取消语义

### 当前问题

涉及：

-   `backend/app/services/agent_runtime/worker.py`
-   `backend/app/services/agent_runtime/lifecycle.py`
-   `backend/app/services/agent_runtime/service.py`

发现：

1.  `RunRegistry.release()` 存在，但当前代码没有实际调用点。
2.  终态 Run 可能长期保留在 `_runs` 和 `_by_idem`。
3.  `get_run()` 优先读取 Registry，可能返回旧状态。
4.  `RunRegistry.attach()` 对已有句柄直接复用，不会刷新数据库状态。
5.  没有本地 Registry 句柄的 Worker 无法直接取消另一个 Worker 正在执行的
    Run。
6.  pending Run 取消与 Worker Claim 之间存在竞态。

### 改造要求

-   Run 状态查询以数据库为事实来源，Registry 只能作为缓存。
-   Registry 缓存需要在终态且 Task 完成后释放。
-   释放后不能影响 DB 查询、事件回放和幂等查询。
-   Registry 增加容量或清理策略，避免长期运行进程内存增长。
-   增加持久化 `cancel_requested` 标记。
-   取消请求通过数据库 CAS 写入。
-   Worker Heartbeat / 执行循环定期检查取消标记。
-   当前 Worker 收到取消请求后停止推进，正确处理在途工具。
-   外部副作用正在执行时，不得假装取消已经撤销远端操作；必要时转入
    unknown。
-   pending 取消与 Claim 必须互斥。

### 验收标准

-   [ ] 完成 1000 个 Run 后 Registry 不持续线性增长。
-   [ ] 数据库状态变化后 `get_run()` 不返回陈旧状态。
-   [ ] Worker A 执行的 Run 可以被 Worker B 所在 API 实例请求取消。
-   [ ] pending Run 取消与 Claim 竞争只有一个成功。
-   [ ] 取消外部工具时正确记录 succeeded / failed /
    unknown，不伪造取消成功。

------------------------------------------------------------------------

## 13. P1-05：执行预算跨恢复持久化

### 当前问题

涉及：

-   `backend/app/services/agent_runtime/context.py`
-   `backend/app/services/agent_runtime/service.py`
-   `backend/app/services/agent_runtime/model_adapter.py`
-   `backend/app/models/agent.py`

当前 `RuntimeContext` 的：

-   `tool_calls`
-   `model_calls`
-   `model_node_runs`
-   `tokens_generated`
-   `steps`
-   `started_at`

都是本次 `_drive()` 创建的内存计数。

Worker 恢复或 HITL Resume 后会重新构造
Context，计数器可能从零开始，`run_timeout_seconds` 也重新计时。

这意味着 Run 重启可能绕过整个 Run 的预算上限。

### 改造要求

把预算从"单次 Drive 预算"升级为"Run 级预算"。

持久化或可可靠重建：

-   total_model_calls
-   total_tool_calls
-   total_steps
-   total_tokens
-   first_started_at
-   deadline_at
-   budget_version

要求：

1.  新 Run 初始化预算。
2.  每次模型调用、工具调用、Graph 主节点推进时累计。
3.  恢复时读取历史累计值，不重置。
4.  `deadline_at` 使用绝对时间，不能每次重启重新获得完整超时时间。
5.  并发更新必须有数据库一致性保护。
6.  明确子 Agent 的预算是否计入父 Run，避免统计漏算或重复计数。
7.  达到上限时持久化 `limit_exceeded`，不继续调用模型和工具。

### 验收标准

-   [ ] Run 执行 10 次模型调用后重启，恢复后预算从 10 继续。
-   [ ] Worker 多次重启不能绕过最大工具调用次数。
-   [ ] HITL 等待时间是否计入 Run Timeout 必须明确配置。
-   [ ] deadline 到期后即使重启也不能重新获得完整时间。
-   [ ] 多 Agent 子任务预算口径一致。

------------------------------------------------------------------------

## 14. P1-06：事件订阅、回放与性能

### 当前问题

涉及：

-   `backend/app/services/agent_runtime/events.py`
-   `backend/app/services/agent_runtime/repositories/event_repository.py`
-   `backend/app/services/agent_runtime/service.py`

当前 EventBus 使用一个共享 asyncio.Queue。多个消费者从同一 Queue
取事件时属于竞争消费，不是广播。

队列满时会删除最旧通知，但当前 `_bus_stream()`
消费者不会自动从数据库补齐被删除的事件。

另外：

-   `LegacyEventAdapter` 自己生成 SSE seq。
-   数据库有独立的 Event sequence。
-   `MODEL_DELTA` 每次都单独开启数据库事务、分配序号并写事件。
-   高 Token 速率下可能形成大量数据库事务和写放大。

### 改造要求

**A. 统一事件序号**

优先使用数据库 `AgentRunEvent.sequence` 作为持久化事件游标和
`Last-Event-ID`。

避免数据库序号和旧 SSE 适配器序号成为两个不一致的事实来源。

**B. 事件总线只做通知，不做事实来源**

推荐：

``` text
Runtime Event
   ↓
Persist Event
   ↓
Notify Subscribers
   ↓
Subscriber reads DB after its own cursor
   ↓
SSE
```

每个订阅者有独立游标。

**C. 队列溢出可补齐**

当本地通知队列满：

-   允许丢弃通知信号。
-   不允许丢失订阅者的可见事件。
-   订阅者通过数据库 sequence 检查和补齐缺口。

**D. 模型增量事件批量持久化**

不要求每个 token chunk 都单独执行一次数据库事务。

可采用：

-   按时间窗口合并。
-   按字符数合并。
-   关键状态事件立即持久化。
-   MODEL_DELTA 批量写入。
-   最终 final_text 单独持久化。
-   保证恢复后最终文本完整。

### 验收标准

-   [ ] 同一个 Run 两个 SSE 订阅者都能收到完整事件。
-   [ ] 事件队列溢出后订阅者能自动补齐。
-   [ ] Last-Event-ID 可以精确续传，无重复、无漏事件。
-   [ ] 跨进程订阅依赖数据库事实日志，不依赖进程内 Queue。
-   [ ] 100 Token/s 的模型流式输出不会产生 100 次/s 的独立数据库事务。
-   [ ] 事件批量化后最终文本恢复仍然完整。

------------------------------------------------------------------------

# 第三部分：P1 安全基线

## 15. P1-07：工具权限、SSRF、路径、SQL 和凭据治理

### 15.1 当前发现

涉及：

-   `backend/app/services/agent_runtime/tools/policy.py`
-   `backend/app/services/agent_runtime/tools/adapter.py`
-   `backend/app/services/agent_runtime/events.py`
-   `backend/app/services/tools/notify_tools.py`
-   `backend/app/services/tools/code_tools.py`
-   `backend/app/services/tools/network_tools.py`

当前问题：

1.  Adapter 对 `http_get/http_post/http_request/fetch_webpage` 检查
    URL，但没有对 `webhook_notify.webhook_url` 做同样检查。
2.  `send_email_tool.smtp_host` 没有网络目标检查。
3.  allowlist 使用 `host.startswith(a)`，可能错误匹配非子域名主机。
4.  仅在请求前解析 DNS，无法完全防止 DNS Rebinding。
5.  路径检查使用 `abspath`，没有验证真实路径和符号链接。
6.  SQL 只读校验仅检查第一个 Token，无法可靠处理 CTE、注释和复杂语句。
7.  `redact_sensitive()` 使用固定 key 集合，未覆盖 `smtp_pass` 等字段。
8.  `webhook_url` 本身可能携带 URL Token。
9.  `ToolRisk` 未明确登记的工具默认 READ，存在 fail-open 风险。
10. `python_exec` 使用进程内 AST 检查和线程执行，不能视为强安全沙箱。
11. `send_email_tool` 虽然是 async 函数，但内部同步 SMTP
    操作会阻塞事件循环。

### 15.2 改造要求

**A. 工具风险分类必须显式登记**

-   未登记工具不得默认 READ。
-   未登记工具不得自动获得网络或执行权限。
-   每个工具都必须声明 side-effect class、risk class、approval policy 和
    retry policy。
-   高危工具不能仅靠模型端隐藏工具来控制权限。

**B. SSRF 统一入口**

所有网络工具都必须走统一的 URL Policy：

-   HTTP GET / POST / Request。
-   Webhook。
-   Custom HTTP。
-   SMTP Host。
-   其他未来网络工具。

修复 allowlist 的字符串前缀匹配，只允许精确域名或合法子域名匹配。

对 DNS Rebinding 采取可靠的网络层防护，例如受控 Egress Proxy、连接阶段
IP 校验或等价机制，不能只依赖一次 `getaddrinfo()`。

**C. 文件路径边界**

-   使用 realpath / resolve 进行最终路径检查。
-   拒绝符号链接逃逸。
-   确保实际读写目标位于 Workspace Root 内。
-   对删除、编辑和写入工具采用相同检查规则。

**D. SQL 只读**

优先采用：

1.  数据库连接账号本身只读。
2.  只读事务或只读数据库权限。
3.  SQL Parser 做语法级校验。
4.  限制多语句和危险语句。

不能只依赖首 Token 字符串判断。

**E. 凭据和日志脱敏**

-   递归识别
    `password/pass/secret/token/api_key/authorization/cookie/credential`
    等敏感字段。
-   对 URL 中可能包含的 token、secret 参数进行脱敏。
-   错误信息、工具输出、审批记录和 Ledger 都必须经过统一脱敏。
-   `smtp_pass` 等真实凭据不得写入普通事件日志和工具账本。
-   后续应以 credential_ref / Secret Manager 替代模型直接持有明文凭据。

**F. Python 执行隔离**

当前 AST 检查不能保证代码安全。

本阶段至少做到：

-   没有外部 Sandbox 时，生产模式默认禁用 `python_exec`。
-   明确区分 `restricted` 与真正的 `external sandbox`。
-   不能用线程 join 超时假装可以终止执行。
-   不能让超时 Python 线程继续无限占用进程资源。
-   外部 Sandbox
    后续采用独立进程/容器、资源限制、网络隔离和文件系统隔离。

### 15.3 验收标准

-   [ ] webhook_notify 访问内网、回环和云元数据地址会被拦截。
-   [ ] SMTP Host 受到统一网络策略限制。
-   [ ] allowlist 不允许 `trusted.example.evil` 通过 `trusted.example`
    前缀匹配。
-   [ ] 路径符号链接不能越过 Workspace Root。
-   [ ] CTE / 注释 / 多语句等 SQL 绕过测试通过。
-   [ ] `smtp_pass`、Authorization、Webhook Token 不进入日志和 Ledger。
-   [ ] 未登记工具默认拒绝。
-   [ ] 未启用外部 Sandbox 时生产模式不会暴露不安全 Python 执行。

------------------------------------------------------------------------

# 第四部分：测试体系与交付

## 16. 必须补充的测试场景

当前 105 项测试作为回归基础保留，不要删除。新增测试必须覆盖以下场景。

### 16.1 Checkpointer / 多进程

  编号    场景                                       预期
  ------- ------------------------------------------ -------------------------
  CP-01   两个独立进程共享 PostgreSQL Checkpointer   状态互相可见
  CP-02   Worker A 写 Checkpoint 后 SIGKILL          Worker B 能恢复
  CP-03   Checkpointer DSN 错误                      启动失败，不降级 Memory
  CP-04   Checkpointer 连接关闭                      无连接泄漏
  CP-05   同一 Session 两个进程访问                  受 Session 执行权约束

### 16.2 Lease / Fencing / 状态机

  编号    场景                              预期
  ------- --------------------------------- ------------------------------
  LF-01   Lease 到期但没有新 Worker Claim   旧 Worker 不能续租
  LF-02   Lease 到期后旧 Worker 提交状态    拒绝
  LF-03   Worker B 接管后 Worker A 提交     拒绝
  LF-04   Fencing 失败后执行完成路径        不写 Session、不发布完成事件
  LF-05   completed 后触发失败路径          不允许降级为 failed
  LF-06   pending Cancel 与 Claim 并发      只有一个成功
  LF-07   Tool Ledger 旧 Worker 写入        拒绝

### 16.3 Tool Ledger / 副作用

  编号    场景                    预期
  ------- ----------------------- ------------------------------
  TL-01   同调用 ID、不同参数     冲突
  TL-02   同调用 ID、不同工具名   冲突
  TL-03   Ledger 写入失败         停止执行
  TL-04   工具返回错误字符串      不得标记 succeeded
  TL-05   HTTP 500                按工具契约判定，不得默认成功
  TL-06   Webhook 超时            unknown
  TL-07   邮件连接中断            unknown 或明确失败
  TL-08   同一外部幂等操作重试    复用同一个下游幂等键
  TL-09   不支持下游幂等的 POST   unknown 后阻止重放

### 16.4 Recovery

  ------------------------------------------------------------------------------
  编号                    场景                        预期
  ----------------------- --------------------------- --------------------------
  RC-01                   Checkpoint                  按策略安全重启
                          缺失、无副作用记录          

  RC-02                   Checkpoint 缺失、read_only  允许安全重试
                          未决                        

  RC-03                   Checkpoint                  阻止重新生成新调用 ID
                          缺失、external_idempotent   
                          unknown                     

  RC-04                   Checkpoint                  needs_reconciliation
                          缺失、non_idempotent        
                          unknown                     

  RC-05                   Checkpoint 文件/数据库损坏  安全阻断

  RC-06                   Checkpoint 存在、Ledger     返回缓存结果，不重复执行
                          succeeded                   
  ------------------------------------------------------------------------------

### 16.5 API / Session / HITL

  编号      场景                            预期
  --------- ------------------------------- --------------------------
  API-01    相同幂等 Key、相同请求          同一个 Run
  API-02    相同 Key、不同请求              409
  API-03    同 Key 跨 Agent                 不得错误复用
  API-04    流式请求重复提交                返回 SSE 回放
  SS-01     同 Session 并发两个 Run         按 Session Lease 串行
  SS-02     Session 写回重复重试            Assistant 消息不重复
  SS-03     Session 写回失败                Run 终态不被改变
  HITL-01   同一个 Run 两轮 Interrupt       只恢复当前批次
  HITL-02   多动作审批                      action_id 精确匹配
  HITL-03   两人同时提交冲突审批            CAS 只有一个成功
  HITL-04   needs_reconciliation 人工核对   正式接口可完成解决或终止

### 16.6 Event / Budget / Security

  编号     场景                          预期
  -------- ----------------------------- ---------------------------------
  EV-01    两个订阅者消费同一 Run        都收到完整事件
  EV-02    慢消费者导致 Queue Overflow   数据库补齐事件
  EV-03    Last-Event-ID 断线续传        不重复、不漏事件
  EV-04    100 Token/s 输出              不产生每 Token 一次独立 DB 事务
  BG-01    多次 Worker 重启              模型/工具预算累计不重置
  BG-02    恢复时超过 deadline           进入 limit_exceeded
  SEC-01   webhook SSRF                  拦截
  SEC-02   SMTP 内网访问                 拦截或受显式策略控制
  SEC-03   路径符号链接逃逸              拦截
  SEC-04   SQL CTE 写入绕过              拦截
  SEC-05   Secret 字段日志               完全脱敏
  SEC-06   无 Sandbox 的 Python 执行     生产默认禁用

------------------------------------------------------------------------

## 17. 测试与 CI 要求

当前报告中的 105 项测试使用 SQLite，命令没有包含
`test_agent_runtime_e2e.py`。

后续测试报告必须区分：

1.  单元测试。
2.  SQLite 集成测试。
3.  MySQL 业务库集成测试。
4.  PostgreSQL Checkpointer 集成测试。
5.  多进程故障恢复测试。
6.  真实模型 E2E 测试。
7.  长稳和性能测试。

### CI 最低要求

建立 GitHub Actions：

-   Python 3.11。
-   安装锁定依赖。
-   单元测试。
-   SQLite Migration 测试。
-   MySQL 8 Migration / Repository 测试。
-   PostgreSQL Checkpointer 测试。
-   确保共享 SQLite 测试库不会被多个 pytest 进程同时争抢。
-   真实模型 E2E 使用独立 Secret 配置；未配置时明确标记为
    skipped，不计入基础 CI 通过数。

不能再只依赖本地测试报告。

------------------------------------------------------------------------

## 18. 交付物要求

本次改造完成后，必须提交：

1.  **代码改造**
    -   Run Lease / Fencing。
    -   Tool Ledger。
    -   Recovery。
    -   Checkpointer。
    -   API 幂等。
    -   Session / HITL。
    -   Event。
    -   Budget。
    -   Security。
2.  **数据库迁移**
    -   新增独立 migration。
    -   不修改历史迁移语义。
3.  **部署配置**
    -   Dockerfile。
    -   Docker Compose。
    -   `.env.example`。
    -   PostgreSQL Checkpointer 配置。
    -   启动迁移入口。
4.  **测试**
    -   新增测试。
    -   原有 105 项回归。
    -   MySQL / PostgreSQL 实测报告。
    -   多进程故障注入报告。
5.  **文档**
    -   `phase2.1-change-log.md`
    -   `phase2.1-test-report.md`
    -   `phase2.1-deployment.md`
    -   更新 `phase2-completion-summary.md` 中与实际代码不一致的表述。
6.  **仓库整理**
    -   清理临时日志。
    -   更新 `.gitignore`。
    -   确保没有提交明文 Secret、开发机路径或临时数据库文件。

### 测试报告必须记录

-   Git Commit SHA。
-   Python / LangGraph / DeepAgents / LangChain 版本。
-   MySQL / PostgreSQL 版本。
-   Checkpointer 类型。
-   Worker 数量和进程数量。
-   测试总数、通过、失败、跳过。
-   哪些是 Mock，哪些是真实服务。
-   故障注入方式。
-   尚未验证的场景。

------------------------------------------------------------------------

# 第五部分：执行顺序与最终验收

## 19. 推荐实施批次

  -----------------------------------------------------------------------------------------
  批次                    主要任务                                  完成门禁
  ----------------------- ----------------------------------------- -----------------------
  Batch 1                 Checkpointer、Docker                      CP-01～05 通过
                          多进程、依赖、迁移启动                    

  Batch 2                 Run                                       LF-01～07 通过
                          Lease、Heartbeat、Fencing、数据库状态机   

  Batch 3                 Tool Ledger、ToolResult、工具恢复身份     TL-01～09 通过

  Batch 4                 Checkpoint 恢复决策与故障恢复             RC-01～06 通过

  Batch 5                 API 幂等、Session 锁、Outbox 写回         API / SS 测试通过

  Batch 6                 HITL 多轮、人工核对流程                   HITL 测试通过

  Batch 7                 Event 订阅、事件批量化、预算持久化        EV / BG 测试通过

  Batch 8                 安全基线、CI、文档和部署验收              SEC 测试与全量回归通过
  -----------------------------------------------------------------------------------------

每个 Batch 应独立提交，提交信息明确，便于审查和回滚。

## 20. 最终验收门禁

### P0 门禁

以下全部通过，才能认为 Phase 2.1 核心可靠性加固完成：

-   [ ] 生产 Durable Runtime 不使用 Memory Checkpointer。
-   [ ] Docker 多进程共享持久化 Checkpointer。
-   [ ] Lease 过期后旧 Worker 无法续租、提交 Run 或写 Tool Ledger。
-   [ ] Fencing 失败后不再产生新的副作用或发布成功事件。
-   [ ] Run 数据库状态机禁止非法迁移和终态覆盖。
-   [ ] Tool Ledger 执行身份和参数指纹一致性检查通过。
-   [ ] Checkpoint 丢失时不会对不确定副作用盲目重放。
-   [ ] 工具错误不能被误记为 succeeded。
-   [ ] MySQL 迁移和 Docker 启动流程真实验证通过。

### P1 门禁

-   [ ] API 幂等请求同请求复用、不同请求冲突。
-   [ ] Session 并发受到控制。
-   [ ] Session 写回具备幂等和失败重试。
-   [ ] 多轮 HITL 恢复正确。
-   [ ] needs_reconciliation 有正式人工处理路径。
-   [ ] Worker Registry 不发生无限增长。
-   [ ] 取消语义跨 Worker 生效。
-   [ ] 预算跨恢复累计。
-   [ ] 多订阅者事件完整回放。
-   [ ] 安全策略贯穿网络、文件、SQL、凭据和代码执行入口。

### P2 门禁

-   [ ] 有基础性能基准。
-   [ ] 有长时间多 Run 并发测试。
-   [ ] 有事件保留、Ledger 归档和数据保留策略。
-   [ ] 有部署、恢复和故障演练手册。

------------------------------------------------------------------------

## 21. Phase 2.1 完成后的架构状态

本阶段完成后，AI-GeneralPlat 应达到以下结构：

``` text
                  API Layer
                     |
           Idempotency / Session
                     |
              Agent Runtime
                     |
               Run Service
                     |
          Run State / Lease / Fencing
                     |
                Run Worker
                     |
           DeepAgents + LangGraph
                     |
        +------------+-------------+
        |            |             |
    Checkpoint   Tool Runtime    Event Runtime
        |            |             |
    Graph State   Tool Ledger   Event Log
        |            |             |
        +------------+-------------+
                     |
             Persistent Storage
                     |
         MySQL + PostgreSQL Checkpointer
                     |
         Session Outbox / Audit / Recovery
```

这套架构仍然由：

-   DeepAgents 承担 Agent Harness。
-   LangGraph 管理 Graph State、Checkpoint、Interrupt、Resume。
-   LangChain 提供 Model、Tool、Middleware。
-   AI-GeneralPlat Runtime Service 负责平台级 Run 生命周期。
-   数据库负责执行事实、租约、事件和副作用记录。

不要将平台 Runtime 与 DeepAgents / LangGraph 的内部执行机制混为一谈。

------------------------------------------------------------------------

## 22. 后续阶段边界

Phase 2.1 完成后，再进入：

  --------------------------------------------------------------------------------------------------
  后续阶段                主题                    主要目标
  ----------------------- ----------------------- --------------------------------------------------
  Phase 3                 Tool Runtime &          MCP、Skills、工具注册、权限、Sandbox、Credential
                          Governance              

  Phase 4                 Observability &         Langfuse / OTel、指标、Trace、评测和回归
                          Evaluation              

  Phase 5                 Workflow & Automation   持久化 Workflow、业务节点、补偿和定时触发

  Phase 6                 Agent Management &      Agent Profile、版本、发布、部署、Workspace
                          Launch                  

  Phase 7                 Multi-Agent             Supervisor、Worker、Delegation、并行协作
                          Collaboration           
  --------------------------------------------------------------------------------------------------

这些阶段不要提前混入 Phase 2.1。

------------------------------------------------------------------------

## 23. 执行 Agent 最终汇报格式

完成后请按以下结构汇报，不要只说"所有任务完成"：

1.  本次实际修改的模块和文件。
2.  每个模块修复了什么问题。
3.  核心架构和数据流发生了什么变化。
4.  数据库新增/修改了哪些字段、索引和迁移。
5.  哪些 P0 / P1 / P2 项已完成，哪些未完成。
6.  每项测试的真实结果及环境。
7.  MySQL / PostgreSQL / 多进程测试是否真实执行。
8.  尚存风险与未验证项。
9.  Git Commit SHA。
10. 推荐下一步。

**最终原则：不要以代码提交数量、测试数量或文档完成度判断可靠性。以故障发生时，系统能否正确判断执行事实、阻止旧
Worker、保护外部副作用、恢复正确状态作为最终标准。**
