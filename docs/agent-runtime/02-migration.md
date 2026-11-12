# 02-migration - 功能映射、数据迁移、兼容与回滚

> 阶段一交付文档之一（任务书 §9.3）。覆盖：旧功能→新内核映射、旧 Session/Memory 迁移
> （含旧 SQLite memory_v2.db 的只读备份与可重复导入方案）、安全回滚、兼容策略。

## 1. 切换开关（回滚路径）

- `AGENT_RUNTIME_ENABLED`（backend/app/core/config.py，默认 true）：
  - `true`：`/chat`、`/invoke`、工作流 `agent_invoke` 走 DeepAgents Runtime；
  - `false`：回退旧 `AgentEngine`（手写 ReAct，保留于 `services/agent/engine.py`）。
- 回滚不丢数据：旧引擎读同一 AgentSession.messages；新内核的 checkpoint/run 表与旧路径互不影响。
- 旧执行路径仅作为兼容/回滚保留，**已退出生产默认执行链路**（阶段二确认稳定后可整体移除）。

## 2. 旧功能 → 新内核映射

| 旧模块 | 新内核承接 | 处置 |
| --- | --- | --- |
| services/agent/engine.py（手写 ReAct 循环） | AgentRuntimeService + create_deep_agent | 保留为回滚路径，不在生产执行链路 |
| output_parser.py（Thought/Action 正则解析） | 原生 tool calling | 保留（旧回滚路径依赖），生产不再解析文本协议 |
| prompt_builder.py | factory._compose_system_prompt（业务提示规则并入） | 保留（旧回滚路径依赖） |
| context_manager.py（滑窗+摘要） | DeepAgents 内置 SummarizationMiddleware | 旧回滚路径依赖；新内核单一摘要源，不并存两份 |
| checkpoint.py（本地 JSON） | LangGraph Checkpointer（memory/sqlite/postgres） | 旧回滚路径依赖；执行真相=checkpoint |
| memory_manager.py（SQLite memory_v2.db） | 保留（跨会话记忆 API 不变），经 system_prompt 注入新内核；可迁移到 LangGraph Store（见 §4） | 不删除旧记忆 |
| tool_executor.py / services/tools/ | tools/adapter.py 桥接（强类型 schema + 策略内联） | 保留为工具执行底座 |
| agent/trace.py / hallucination.py | events.py + telemetry.py（可插拔导出） | 仅旧路径使用；观测由新事件/trace 承接（阶段二 Langfuse） |
| agent_service.py 早期 ReAct 实现 | 统一 Runtime（全局搜索确认无生产调用方） | 保留 re-export 兼容壳 |
| workflow_service Agent 节点 | _exec_agent_invoke 走统一 Runtime | 画布/引擎本阶段不重写 |

## 3. 会话数据兼容

- 旧会话历史（含 ReAct steps）保持可读：会话投影沿用旧消息结构
  （`{role, content, steps}`，steps 为旧事件形状），前端无需迁移。
- 新历史同样以旧结构写入 session.messages，LangGraph checkpoint 为执行真相来源（01 文档 §10）；
  两套状态不混写（session 仅追加展示投影）。
- 无破坏性数据库操作：`agent_runs` / `agent_approvals` 为纯新增表，
  alembic 迁移 `b7c8d9e0f1a2`（downgrade `a1b2c3d4e5f6` 完整回滚）。

## 4. 旧 SQLite memory（memory_v2.db）迁移方案

旧记忆库位置：`persist_dir/memory_v2.db`（memory_manager.py），含三张表：
`memories_v2`（fact/rule/preference 等记忆）、`session_memory`（会话记忆）、
`skill_library`（技能成功率统计）。

**原则：原库全程只读，任何导入可重复执行，任何步骤可回滚。**

### 4.1 只读备份（导入前必做）

```powershell
cd backend
python -c "import sqlite3,hashlib,pathlib; p=pathlib.Path('data/persist/memory_v2.db'); \
b=p.with_suffix('.db.bak-20261109'); s=sqlite3.connect(f'file:{p}?mode=ro',uri=True); \
d=sqlite3.connect(b); s.backup(d); d.close(); s.close(); \
print('backup:', b, 'sha256:', hashlib.sha256(b.read_bytes()).hexdigest())"
```

- 用 `sqlite3 backup` API（在线一致性快照），不是文件复制；
- 记录备份文件名 + SHA-256 到迁移日志；**导入只读源库**（`mode=ro` 连接）。

### 4.2 导入目标与幂等语义

- 目标：LangGraph Store（`AGENT_STORE_DSN`，未启用时保持 memory_manager 直读，**无需导入**）。
- 命名空间：`("memory", user_id, agent_id)`；key = `mem-{source_table}-{source_id}`。
- **可重复执行**：按 key upsert（put 覆盖同 key），重复运行不产生重复记忆；
  断点续传游标 = 源表 `id > last_imported_id`（迁移日志记录游标）。
- 值字段：content/memory_type/importance/tags/is_pinned/created_at + `source`（表名与行 id）+
  `migrated_from`（备份文件名与 SHA-256）。

导入伪代码（可直接改写为 `scripts/import_legacy_memory.py`，幂等）：

```python
import sqlite3
from langgraph.store.base import BaseStore  # 用 AGENT_STORE_DSN 构造的 store

src = sqlite3.connect("file:data/persist/memory_v2.db?mode=ro", uri=True)
rows = src.execute(
    "SELECT id, user_id, agent_id, content, memory_type, importance, tags, is_pinned, created_at "
    "FROM memories_v2 WHERE id > ? ORDER BY id", (last_id,))
for r in rows:
    key = f"mem-memories_v2-{r[0]}"
    store.put(("memory", r[1], r[2]), key, {
        "content": r[3], "memory_type": r[4], "importance": r[5], "tags": r[6],
        "is_pinned": r[7], "created_at": r[8],
        "source": {"table": "memories_v2", "id": r[0]},
        "migrated_from": {"file": "memory_v2.db.bak-20261109", "sha256": "<...>"},
    })   # put 为 upsert：重复执行安全
    last_id = r[0]                      # 每批更新游标，断点续传
# session_memory / skill_library 同法，命名空间 ("memory_session", ...) / ("skill_library", ...)
```

### 4.3 验证（导入后必做）

1. **行数对账**：源表 `SELECT COUNT(*)`（按 user_id 分组）== 目标 Store 同命名空间 key 计数；
2. **内容抽样**：按 id 抽样 ≥10 条，比对 content 哈希一致；
3. **功能抽样**：新内核注入记忆后发一条测试消息，确认参考记忆段包含抽样记忆；
4. 结果写入迁移日志（导入条数/跳过条数/失败条数/游标位置）。

### 4.4 回滚

- 导入数据全部带 `migrated_from` 标记 ⇒ 回滚 = 按标记删除目标命名空间中导入的 key
  （`store.delete(namespace, key)` 遍历），源库未被修改过，直接继续用 memory_manager 读取；
- 若新内核记忆注入异常：`AGENT_RUNTIME_ENABLED=false` 回退旧引擎（旧记忆读取路径不变）；
- 极端情况：从 4.1 备份文件恢复（`sqlite3 memory_v2.db < dump` 或直接替换文件），校验 SHA-256。

### 4.5 失败处理

- 单行导入失败：记录行 id 与错误，继续后续行；重跑同一脚本自动跳过已成功（upsert 幂等）；
- 全量失败：不改任何状态，排查后重跑；备份与日志保留至迁移确认后 30 天。

## 5. 部署迁移步骤

1. `pip install -r backend/requirements.txt`（锁定版本见 03-operations.md）。
2. 备份业务库与 memory_v2.db（§4.1；业务 MySQL 用 mysqldump，禁止破坏性操作）。
3. `alembic upgrade head`（新增 agent_runs / agent_approvals，纯新增表）。
4. 配置环境变量（03-operations.md §3）：生产设置 `AGENT_CHECKPOINT_DSN=postgresql+asyncpg://...`、
   `AGENT_SANDBOX_MODE=disabled`、`AGENT_ENABLE_HITL=true` 等。
5. 模型配置检查：被 Agent 引用的 ModelConfig 必须 `supports_function_call=true`，
   否则运行时显式报错（**不静默回退文本 ReAct**）。
6. （可选）执行 §4 记忆迁移并验证。
7. 重启服务；验证 `/api/v1/agents/{id}/chat` 流式对话 + `/api/v1/agents/tools` + 一条工作流 Agent 节点。

## 6. 已知限制（下一阶段）

- 审批超时无自动过期（依赖人工处置或 cancel）；计划加 TTL。
- 运行事件暂无持久化留存（AGENT_EVENT_RETENTION_DAYS 预留），观测导出（Langfuse）留接口未接。
- 单机 SQLite/memory checkpoint 不支持多 Worker；生产必须 postgres DSN。
- 审批"重启后仍可审批"（AT-09 后半）在 API/DB 级已具备（checkpoint + agent_runs/approvals 持久化
  + GET /runs/{id} + /resume），完整 E2E 待数据库环境（04-validation.md 记录阻塞）。
