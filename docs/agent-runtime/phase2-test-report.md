# Phase 2 Durable Execution 测试报告

> 本报告只记录**实际执行**的测试与真实结果；未实测项在 §7/§8 如实标注，不以推断充当验证。

---

## 1. 测试环境与依赖版本

| 项 | 值 |
|---|---|
| 操作系统 | Windows（开发机） |
| Python | 3.11 |
| langgraph / langgraph-checkpoint | 1.2.20 / 4.3.1 |
| langchain / langchain-core | 1.4.9 / 1.7.6 |
| SQLAlchemy | 2.1.8 |
| FastAPI / Pydantic | 0.162.0 / 2.14.3 |
| pytest / pytest-asyncio | 9.2.0 / 1.4.1.post1 |
| 测试业务库 | SQLite（aiosqlite 0.22.1） |
| 测试 Checkpointer | InMemorySaver（会话内）/ AsyncSqliteSaver（重启/多实例用例） |

## 2. 数据库类型与迁移版本

- 业务库（测试）：SQLite；生产目标 MySQL/PostgreSQL（**未实测**，见 §8）；
- 迁移链 head = `f1a2b3c4d5e6`（`add_event_thread_id.py`）；
  本阶段新增 revision：`d4e5f6a7b8c9`（add_durable_execution）、`f1a2b3c4d5e6`（add_event_thread_id）、
  `e5f6a7b8c9d0`（空库基线修复）；
- 迁移验证（SQLite 实跑）：`alembic upgrade head` → 表/列/唯一约束断言 → `alembic downgrade base`
  → 再 `alembic upgrade head` 全部通过（`test_alembic_migration_upgrade_downgrade`）。

## 3. 执行命令与结果

```bash
cd backend
python -m pytest tests/test_agent_runtime.py tests/test_agent_runtime_persistence.py \
    tests/test_agent_runtime_durable.py tests/test_api_smoke.py -q
```

**最终结果：105 passed，0 failed，0 skipped**（2026-11-12）

稳定性说明（如实记录）：开发过程中曾观察到套件偶发"卡住不前"的现象，经排查为**多个 pytest 进程并发争抢共享 SQLite 测试库**所致（遗留后台测试进程未清理时触发），并非用例或被测代码缺陷；清理并发进程后，同套件多轮循环（≥5 轮）运行稳定，捕获到的统计均为 105 passed / 0 failed。CI 中应保证同一测试库上不并发执行本套件。

| 测试文件 | 数量 | 覆盖 |
|---|---|---|
| tests/test_agent_runtime.py | 36 | 运行时/图/工具/E2E 回归 |
| tests/test_agent_runtime_persistence.py | 8 | Checkpoint 重启恢复、多实例共享线程、副作用恰一次、资源泄漏 |
| tests/test_agent_runtime_durable.py | 47 | 本阶段主测试（FR-01~10、T-01~11、故障注入、迁移） |
| tests/test_api_smoke.py | 14 | API 冒烟 + SSE 回放 + 预算/超时状态 + Idempotency-Key（2 项） |

## 4. 集成测试 T-01 ~ T-11 结果

| 编号 | 场景 | 测试函数 | 结果 |
|---|---|---|---|
| T-01 | 正常多步工具调用 | `test_t01_multi_step_tool_ledger_stable_ids_and_keys` | 通过 |
| T-02 | 服务对象重建后的审批恢复 | `test_t02_hitl_approval_persisted_and_resumable_after_restart` | 通过 |
| T-03 | 审批重复提交 | `test_t03_approval_idempotent_and_conflict_semantics` | 通过 |
| T-04 | 工具成功后 Checkpoint 前崩溃 | `test_t04_tool_success_then_crash_replay_returns_cached_result` | 通过 |
| T-05 | 外部 POST 不确定结果 | `test_t05_unknown_result_blocks_replay_without_idempotency`、`test_t05_idempotent_retry_reuses_same_key` | 通过 |
| T-06 | 多 Worker 并发 Claim | `test_t06_two_workers_single_claim_winner`、`test_t06_two_runs_each_executed_once_across_workers` | 通过 |
| T-07 | Worker Lease 过期 | `test_t07_lease_loss_cancels_drive_without_state_write`、`test_claim_next_picks_expired_running_for_recovery` | 通过 |
| T-08 | SSE 断线回放 | `test_t08_sse_replay_after_disconnect_no_loss`、`test_event_replay_after_seq` | 通过 |
| T-09 | Session 与 Checkpoint 不重复 | `test_t09_multi_turn_history_no_duplication_after_restart` | 通过 |
| T-10 | 最终文本竞态 | `test_t10_final_text_authoritative_without_consumer` | 通过 |
| T-11 | 预算统计 | `test_t11_model_call_counting_and_budget_limit` | 通过 |

T-09 要点：3 轮对话、每轮模拟服务重启（清空 RunRegistry 内存态），Graph state 中 6 条消息各恰 1 次、
Session 严格 `user/assistant` 交替、同历史重试注入不产生重复。
T-10 要点：流式 3 chunk、消费者完全不消费 stream，DB `final_text` 仍为完整拼接且与后到 `final` 事件一致。
T-11 要点：3 chunk 单请求 `model_calls=1`（chunk 不重复计数）、`tokens_generated=3`；
工具轮 `model_calls=2 / model_node_runs=2 / tool_calls=1`；预算触顶映射 `limit_exceeded`（非 failed）。

## 5. 单元 / 并发 / 回归结果

| 类别 | 代表测试 | 结果 |
|---|---|---|
| 幂等创建（FR-01） | `test_idempotent_reuse_same_request_hash`、`test_idempotent_conflict_different_request_hash`、`test_db_enforces_idempotency_uniqueness` 等 6 项 | 通过 |
| 幂等键 API 接线（§8.2） | `test_invoke_idempotency_key_hits_original_run`、`test_chat_idempotency_key_no_duplicate_execution`（同键重试返回原 Run 不重复执行） | 通过 |
| 租约/Fencing（FR-02） | `test_claim_only_one_winner`、`test_heartbeat_extends_lease_and_fencing`、`test_release_invalidates_old_worker`、`test_update_status_fencing_requires_match`、`test_claim_next_skips_paused_statuses` 等 8 项 | 通过 |
| 工具账本（FR-05） | `test_ledger_begin_is_idempotent_per_tool_call_id`、`test_ledger_succeeded_is_immutable`、`test_ledger_unknown_blocks_replay`、`test_ledger_no_unsafe_after_success` 等 6 项 | 通过 |
| 事件持久化（FR-07） | `test_event_seq_monotonic_per_run`、`test_event_unique_constraint_on_sequence`、`test_event_sequence_concurrent_appends_unique`、`test_event_overflow_keeps_persisted_and_redacts_payload`、`test_event_cleanup_only_terminal_and_expired` 等 8 项 | 通过 |
| 状态机 | `test_needs_reconciliation_transitions`（非法迁移拒绝） | 通过 |
| 迁移 | `test_alembic_migration_upgrade_downgrade` | 通过 |
| Checkpoint 持久化回归 | `test_restart_recovery_and_continuity`、`test_two_instances_share_thread_state`、`test_tool_executes_exactly_once_across_resumes` 等 8 项 | 通过 |
| API 回归 | `test_chat_sse_stream`、`test_events_replay_after_seq_and_last_event_id`、`test_tool_budget_exceeded_status`、`test_cancel_not_misreported_as_success` 等 12 项 | 通过 |

## 6. 故障注入结果（F-01 ~ F-18 覆盖映射）

| 编号 | 注入方式（测试内模拟崩溃/断线/并发） | 验证测试 | 结果 |
|---|---|---|---|
| F-01 | 重复/冲突幂等键插入、绕过仓库直接插重 | `test_idempotent_*`、`test_db_enforces_idempotency_uniqueness` | 符合预期 |
| F-02 | 提交后无人领取 | `test_recovery_orphan_run_reclaimed_and_executed` | 符合预期 |
| F-03 | 租约过期模拟（时间戳拨回） | `test_claim_blocked_while_lease_valid_then_recoverable`、`test_recovery_orphan_run_reclaimed_and_executed` | 符合预期 |
| F-04 | 清空内存注册表模拟进程重启 | `test_t09_*`、`test_restart_recovery_and_continuity` | 符合预期 |
| F-05 | 只读工具失败重试路径 | `test_t01_*`、`test_ledger_success_flow` | 符合预期 |
| F-06 | prepared 状态重复 begin | `test_ledger_begin_is_idempotent_per_tool_call_id`、`test_f06_idempotent_duplicate_request_single_execution` | 符合预期 |
| F-07 | 成功但结果未知 + 有/无幂等键两分支 | `test_t05_unknown_result_blocks_replay_without_idempotency`、`test_t05_idempotent_retry_reuses_same_key` | 符合预期 |
| F-08 | 账本 unknown 阻断自动重放 | `test_recovery_unsafe_ledger_blocks_auto_replay`、`test_ledger_unknown_blocks_replay` | 符合预期 |
| F-09 | 账本成功后"崩溃"重放 | `test_t04_tool_success_then_crash_replay_returns_cached_result` | 符合预期 |
| F-10 | 中断后服务对象重建 | `test_t02_hitl_approval_persisted_and_resumable_after_restart`、`test_recovery_checkpoint_interrupt_returns_to_waiting_approval` | 符合预期 |
| F-11 | 重复提交审批 | `test_t03_approval_idempotent_and_conflict_semantics` | 符合预期 |
| F-12 | 双 Worker 并发 claim | `test_t06_two_workers_single_claim_winner`、`test_claim_only_one_winner` | 符合预期 |
| F-13 | 租约丢失后旧 Worker 提交 | `test_t07_lease_loss_cancels_drive_without_state_write`、`test_release_invalidates_old_worker`、`test_update_status_fencing_requires_match` | 符合预期 |
| F-14 | SSE 断线 + after_seq/Last-Event-ID 续传 | `test_t08_sse_replay_after_disconnect_no_loss`、`test_events_replay_after_seq_and_last_event_id` | 符合预期 |
| F-15 | 内存队列溢出 | `test_event_overflow_keeps_persisted_and_redacts_payload` | 符合预期 |
| F-16 | Checkpoint 缺失时副作用阻断 | `test_recovery_unsafe_ledger_blocks_auto_replay`（账本兜底路径） | 部分覆盖，见 §8 |
| F-17 | 持久化失败抛出（不吞异常） | `test_db_enforces_idempotency_uniqueness`（IntegrityError 语义）、`_save_session(raise_on_error=True)` 链路（test_api_smoke 会话保存失败分支） | 符合预期 |
| F-18 | 同历史重复注入 | `test_t09_multi_turn_history_no_duplication_after_restart` | 符合预期 |

另含崩溃/恢复专项：`test_graceful_shutdown_suspends_and_releases_lease`、`test_drive_suspend_keeps_run_recoverable`、
`test_cancel_leaves_no_tasks_and_closes_sqlite`。

## 7. 未完成项

- F-16 的"Checkpoint 文件损坏（非缺失）"场景只覆盖了账本兜底路径，未注入位级损坏的 Checkpoint 文件；
- `test_t07` 模拟租约丢失而非真实时钟漂移；真实 NTP 漂移下的行为未测；
- 负载/长稳测试（长时间多 Run 并发、事件表百万行回放性能）未纳入本阶段。

## 8. 已知限制

1. **MySQL / PostgreSQL 未实测**：迁移与仓储在 SQLite 上全量验证；MySQL/PG 仅有 `batch_alter_table`
   兼容性设计，无实机执行证据；
2. **AsyncPostgresSaver / AsyncPostgresStore 代码路径未实测**（本地无 PG 实例与依赖）；
3. **不承诺"恰好一次"外部副作用**：无下游幂等能力的外部 POST 在结果未知时只保证进入 `unknown`
   并阻断重放，最终一致性依赖人工核对（§5 SOP）；
4. `tokens_generated` 在模型不返回 usage 元数据时用流式 token 计数兜底，可能与计费口径有偏差；
5. `model_node_runs` 依赖 deepagents 图主节点名为 `"model"`（内部约定，内核升级需复核）；
6. 多实例下跨实例 SSE 实时通知不保证（事件通知为进程内），依赖断线回放/轮询补齐；
7. 事件保留清理不覆盖 `agent_tool_executions`（设计使然，审计留痕）。

## 9. 生产部署前仍需人工确认的事项

1. 在**生产同版本** MySQL/PostgreSQL 上预演 `alembic upgrade head` 与 `downgrade`；
2. `AGENT_CHECKPOINT_DSN` 选型（生产建议 PostgreSQL Checkpointer）并确认持久卷；
3. 盘点副作用类工具清单：哪些下游支持幂等键、键如何透传（决定 `idempotency_key` 生成策略）；
4. `needs_reconciliation` 人工核对 SOP 与值班流程落地（见运维说明 §5）；
5. `AGENT_EVENT_RETENTION_DAYS` 与合规/审计留存要求对齐；
6. 租约参数（60s/15s）与实际部署环境的网络/负载匹配，确认 `heartbeat < lease/2`；
7. 多实例部署时 `AGENT_RUNTIME_WORKER_ID` 唯一分配方案；
8. SECRET_KEY、TLS、网络 allowlist 等安全配置复核（既有配置项，本阶段未改动语义）。
