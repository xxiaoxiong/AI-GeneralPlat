from datetime import datetime
from sqlalchemy import (
    Column, Integer, BigInteger, String, Text, Boolean, DateTime,
    ForeignKey, JSON, UniqueConstraint, Index,
)
from app.models.base import Base


class Agent(Base):
    __tablename__ = "agents"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, default="")
    icon = Column(String(10), default="🤖")

    # 关联模型配置
    model_config_id = Column(Integer, ForeignKey("model_configs.id"), nullable=True)

    # 系统提示词
    system_prompt = Column(Text, default="你是一个智能助手，可以使用工具来帮助用户解决问题。")

    # ReAct 配置
    max_iterations = Column(Integer, default=10)       # 最大循环次数
    max_tokens = Column(Integer, default=4096)
    temperature = Column(String(10), default="0.7")

    # 工具列表（JSON 数组，存储工具配置）
    tools = Column(JSON, default=list)

    # 知识库关联（可选）
    knowledge_base_id = Column(Integer, ForeignKey("knowledge_bases.id"), nullable=True)

    # 数据库连接关联（可选，用于自然语言查询外部数据库）
    database_connection_id = Column(Integer, ForeignKey("database_connections.id"), nullable=True)

    is_active = Column(Boolean, default=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AgentSession(Base):
    """Agent 对话会话"""
    __tablename__ = "agent_sessions"

    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    title = Column(String(200), default="新对话")

    # 完整对话历史（含 ReAct 思考链）
    messages = Column(JSON, default=list)

    # P1-02：消息写入乐观锁版本（读取-合并-写回须 CAS，冲突合并重试）
    version = Column(Integer, default=0, nullable=False)
    # P1-02：Session 执行权（单 Session 单活跃 Run，数据库原子 Claim）
    active_run_id = Column(String(36), nullable=True)          # 当前持有执行权的 Run
    active_run_lease_expires_at = Column(DateTime, nullable=True)  # 执行权租约（过期可接管）

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SessionResultOutbox(Base):
    """Run 终态 → Session 写回的轻量 Outbox（P1-02 C）。

    与 Run 终态在同一事务入队（Run completed/failed/cancelled + outbox 原子提交）；
    Outbox Worker 异步写回 Assistant 消息，按 source_run_id（run_id 唯一约束 +
    消息内 source_run_id 查重）去重后标记 delivered；写回失败可重试且不会
    重复追加；服务重启后 pending 记录可继续恢复写回。
    """
    __tablename__ = "session_result_outbox"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 不设外键：Session/Run 删除不应被写回任务阻塞（写回按 run_id 去重、
    # 按行内容幂等处理缺失场景）；run_id 唯一约束保证一 Run 一条写回任务
    run_id = Column(String(36), nullable=False, unique=True, index=True)
    session_id = Column(Integer, nullable=False, index=True)
    status = Column(String(24), default="pending", nullable=False, index=True)  # pending / delivered
    attempts = Column(Integer, default=0, nullable=False)   # 写回尝试次数
    last_error = Column(Text, nullable=True)                # 最近一次失败原因

    created_at = Column(DateTime, default=datetime.utcnow)
    delivered_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AgentRun(Base):
    """一次 Agent 执行实例（LangGraph thread 上的运行记录）"""
    __tablename__ = "agent_runs"

    __table_args__ = (
        # 幂等键按用户作用域唯一（MySQL/SQLite 均允许多个 NULL，未传幂等键保持旧行为）
        UniqueConstraint("user_id", "idempotency_key", name="uq_agent_runs_idem"),
    )

    id = Column(String(36), primary_key=True)          # run_id (uuid4 hex)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, index=True)
    session_id = Column(Integer, ForeignKey("agent_sessions.id"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    thread_id = Column(String(128), nullable=False, index=True)

    status = Column(String(24), default="pending", index=True)
    # pending / running / waiting_approval / completed / failed / cancelled
    # / limit_exceeded / needs_reconciliation

    idempotency_key = Column(String(128), nullable=True, index=True)
    error = Column(Text, nullable=True)
    final_text = Column(Text, nullable=True)
    summary = Column(JSON, nullable=True)              # 工具调用数、耗时等

    # ── Phase 2 Durable Execution ──────────────────────────────────────
    request_hash = Column(String(64), nullable=True)   # 规范化请求摘要（幂等冲突判定）
    input_payload = Column(JSON, nullable=True)        # 恢复所需输入快照（无密钥）
    agent_config_snapshot = Column(JSON, nullable=True)  # 脱敏配置快照
    started_at = Column(DateTime, nullable=True)       # 首次开始执行时间
    worker_id = Column(String(128), nullable=True)     # 当前租约持有者
    lease_expires_at = Column(DateTime, nullable=True)  # 租约到期时间
    heartbeat_at = Column(DateTime, nullable=True)     # 最近心跳
    attempt = Column(Integer, default=0, nullable=False)  # 执行尝试次数（每次 Claim +1，1-based）
    lease_version = Column(Integer, default=0, nullable=False)  # fencing token
    last_event_seq = Column(BigInteger, default=0, nullable=False)  # 最近事件序号
    recovery_error = Column(Text, nullable=True)       # 恢复失败原因
    recovery_metadata = Column(JSON, nullable=True)    # 恢复决策元数据（P0-04 §6.3）

    # ── Run 级执行预算（P1-05 §13）：跨崩溃/HITL 恢复不重置 ──────────────
    # 累计值 + 绝对 deadline + CAS 版本；恢复时读取历史累计，不从零开始。
    budget_model_calls = Column(Integer, default=0, nullable=False)  # 累计模型调用
    budget_tool_calls = Column(Integer, default=0, nullable=False)   # 累计工具调用
    budget_steps = Column(Integer, default=0, nullable=False)        # 累计图步数
    budget_tokens = Column(Integer, default=0, nullable=False)       # 累计生成 Token
    budget_first_started_at = Column(DateTime, nullable=True)        # 首次启动（Run 起点）
    budget_deadline_at = Column(DateTime, nullable=True)             # 绝对截止（不随重启重置）
    budget_version = Column(Integer, default=0, nullable=False)      # CAS 版本（并发一致性）
    # HITL 等待起点（P1-05 §13.3）：进入 waiting_approval 时写入；恢复时按
    # AGENT_HITL_WAIT_COUNTS_TO_TIMEOUT 决定是否按等待时长顺延 deadline。
    hitl_wait_started_at = Column(DateTime, nullable=True)

    # 取消请求（P1-04 §12）：DB 是取消意图的唯一真相源。cancel_requested=CAS 写入；
    # 在途外部工具不得伪造取消完成（取消为意图，实现尽力而为）。
    cancel_requested = Column(Boolean, default=False, nullable=False)  # 是否请求取消
    cancel_requested_at = Column(DateTime, nullable=True)              # 取消请求时间
    cancel_requested_by = Column(Integer, ForeignKey("users.id"), nullable=True)  # 请求者

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)


class AgentApproval(Base):
    """HITL 审批记录"""
    __tablename__ = "agent_approvals"

    __table_args__ = (
        UniqueConstraint("run_id", "action_id", name="uq_agent_approvals_action"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(36), ForeignKey("agent_runs.id"), nullable=False, index=True)
    tool_name = Column(String(100), nullable=False)
    action_summary = Column(Text, nullable=True)       # 脱敏后的参数摘要
    decision = Column(String(24), nullable=True)       # approve / reject / timeout
    decided_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    message = Column(Text, nullable=True)

    # ── Phase 2 Durable Execution：稳定动作身份与幂等决策 ──────────────
    action_id = Column(String(64), nullable=True)      # 稳定动作 ID
    tool_call_id = Column(String(64), nullable=True)   # 模型工具调用 ID
    args_hash = Column(String(64), nullable=True)      # 参数摘要
    action_payload = Column(JSON, nullable=True)       # 脱敏恢复信息（可空）
    decision_status = Column(String(24), default="pending")  # pending/approved/rejected/superseded

    created_at = Column(DateTime, default=datetime.utcnow)
    decided_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AgentToolExecution(Base):
    """工具执行账本（FR-05）：副作用先登记，再执行，再确认。"""
    __tablename__ = "agent_tool_executions"

    __table_args__ = (
        UniqueConstraint("run_id", "tool_call_id", name="uq_tool_exec_run_call"),
        Index("ix_tool_exec_run_status", "run_id", "status"),
        Index("ix_tool_exec_status_updated", "status", "updated_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(36), ForeignKey("agent_runs.id"), nullable=False, index=True)
    tool_call_id = Column(String(64), nullable=False)   # LangGraph 工具调用 ID
    tool_name = Column(String(100), nullable=False)
    args_hash = Column(String(64), nullable=True)
    args_snapshot = Column(JSON, nullable=True)         # 脱敏后的必要参数快照
    idempotency_key = Column(String(128), nullable=True)  # 稳定外部幂等键

    status = Column(String(24), default="prepared", index=True)
    # prepared / executing / succeeded / failed / unknown / needs_reconciliation

    attempt_count = Column(Integer, default=0, nullable=False)
    result_payload = Column(JSON, nullable=True)        # 工具结果（按安全策略保存）
    error = Column(Text, nullable=True)
    external_operation_id = Column(String(128), nullable=True)  # 下游业务操作 ID
    worker_id = Column(String(128), nullable=True)
    lease_version = Column(Integer, default=0, nullable=False)

    # 人工核对（P1-03 §11 D）：needs_reconciliation 之后由管理员核对处置的审计留痕
    reconcile_decision = Column(String(32), nullable=True)   # confirmed_succeeded/confirmed_not_executed/unresolved
    reconcile_note = Column(Text, nullable=True)             # 核对说明/证据
    reconciled_by = Column(Integer, ForeignKey("users.id"), nullable=True)  # 核对人（管理员）
    reconciled_at = Column(DateTime, nullable=True)          # 核对时间

    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AgentRunEvent(Base):
    """Run 有序事件日志（FR-07）：SSE 回放的持久化事实来源。"""
    __tablename__ = "agent_run_events"

    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_run_event_seq"),
        Index("ix_run_event_run_seq", "run_id", "sequence"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(36), ForeignKey("agent_runs.id"), nullable=False)
    sequence = Column(Integer, nullable=False)          # Run 内单调递增
    thread_id = Column(String(64), default="")          # Checkpoint thread 归属（§7.2）
    event_type = Column(String(48), nullable=False)
    payload = Column(JSON, nullable=True)               # 脱敏后的事件数据
    created_at = Column(DateTime, default=datetime.utcnow)
