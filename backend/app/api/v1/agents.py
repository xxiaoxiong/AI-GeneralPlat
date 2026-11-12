"""
Agent 管理与对话 API（v2 增强版）
"""
import json
import logging
from typing import List, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm.attributes import flag_modified
from pydantic import BaseModel

from app.core.database import get_db, AsyncSessionLocal
from app.core.config import settings
from app.api.deps import get_current_user
from app.models.user import User
from app.models.agent import Agent, AgentSession
from app.services.agent import AgentEngine
from app.services.agent_runtime import get_runtime_service
from app.services.agent_runtime.service import (
    ApprovalConflict,
    ApprovalForbidden,
    ApprovalInvalid,
)
from app.services.agent_runtime.repositories import IdempotencyConflict, RunRepository
from app.services.tools import BUILTIN_TOOLS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class ToolConfig(BaseModel):
    name: str
    enabled: bool = True

class AgentCreate(BaseModel):
    name: str
    description: str = ""
    icon: str = "🤖"
    model_config_id: Optional[int] = None
    system_prompt: str = "你是一个智能助手，可以使用工具来帮助用户解决问题。"
    max_iterations: int = 10
    temperature: str = "0.7"
    tools: list = []
    knowledge_base_id: Optional[int] = None
    database_connection_id: Optional[int] = None

class AgentUpdate(AgentCreate):
    pass

class ChatMessage(BaseModel):
    role: str
    content: str

class AgentChatRequest(BaseModel):
    session_id: Optional[int] = None
    message: str
    stream: bool = True
    use_memory: bool = True  # 是否启用跨会话记忆

class MemoryAddRequest(BaseModel):
    content: str
    memory_type: str = "fact"  # fact / rule / skill / preference / episode
    agent_id: Optional[int] = None
    importance: float = 1.0
    tags: str = ""
    is_pinned: bool = False


# ── 记忆管理 API（v2 增强版：结构化记忆）──────────────────────────────────────

@router.get("/memories")
async def list_memories(
    agent_id: Optional[int] = None,
    memory_type: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    from app.services.agent.memory_manager import MemoryManager
    items = await MemoryManager.list_memories(
        current_user.id, agent_id=agent_id, memory_type=memory_type
    )
    return {"items": items, "total": len(items)}


@router.post("/memories")
async def add_memory(
    body: MemoryAddRequest,
    current_user: User = Depends(get_current_user),
):
    from app.services.agent.memory_manager import MemoryManager
    mem_id = await MemoryManager.add_memory(
        user_id=current_user.id,
        content=body.content,
        memory_type=body.memory_type,
        agent_id=body.agent_id,
        importance=body.importance,
        tags=body.tags,
        is_pinned=body.is_pinned,
    )
    return {"id": mem_id, "message": "记忆已保存"}


@router.delete("/memories/{memory_id}")
async def delete_memory(
    memory_id: int,
    current_user: User = Depends(get_current_user),
):
    from app.services.agent.memory_manager import MemoryManager
    ok = await MemoryManager.delete_memory(memory_id, current_user.id)
    if not ok:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {"message": "已删除"}


@router.delete("/memories")
async def clear_memories(
    agent_id: Optional[int] = None,
    current_user: User = Depends(get_current_user),
):
    from app.services.agent.memory_manager import MemoryManager
    count = await MemoryManager.clear_memories(current_user.id, agent_id=agent_id)
    return {"message": f"已清除 {count} 条记忆"}


# ── 工具列表 ──────────────────────────────────────────────────────────────────

@router.get("/tools")
async def list_tools():
    """获取所有内置工具列表"""
    return {"items": BUILTIN_TOOLS, "total": len(BUILTIN_TOOLS)}


# ── Agent CRUD ────────────────────────────────────────────────────────────────

@router.get("")
async def list_agents(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    offset = (page - 1) * page_size
    total_res = await db.execute(select(func.count(Agent.id)).where(Agent.is_active == True))
    total = total_res.scalar()
    res = await db.execute(
        select(Agent).where(Agent.is_active == True)
        .order_by(Agent.created_at.desc())
        .offset(offset).limit(page_size)
    )
    agents = res.scalars().all()
    return {
        "items": [_agent_to_dict(a) for a in agents],
        "total": total, "page": page, "page_size": page_size,
    }


@router.post("")
async def create_agent(
    body: AgentCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent = Agent(
        name=body.name,
        description=body.description,
        icon=body.icon,
        model_config_id=body.model_config_id,
        system_prompt=body.system_prompt,
        max_iterations=body.max_iterations,
        temperature=str(body.temperature),
        tools=body.tools,
        knowledge_base_id=body.knowledge_base_id,
        database_connection_id=body.database_connection_id,
        owner_id=current_user.id,
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return _agent_to_dict(agent)


@router.get("/{agent_id}")
async def get_agent(
    agent_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent = await _get_or_404(db, agent_id, current_user)
    return _agent_to_dict(agent)


@router.put("/{agent_id}")
async def update_agent(
    agent_id: int,
    body: AgentUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent = await _get_or_404(db, agent_id, current_user)
    for field, val in body.dict().items():
        setattr(agent, field, val)
    await db.commit()
    await db.refresh(agent)
    return _agent_to_dict(agent)


@router.delete("/{agent_id}")
async def delete_agent(
    agent_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent = await _get_or_404(db, agent_id, current_user)
    agent.is_active = False
    await db.commit()
    return {"message": "已删除"}


# ── 会话管理 ──────────────────────────────────────────────────────────────────

@router.get("/{agent_id}/sessions")
async def list_sessions(
    agent_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _get_or_404(db, agent_id, current_user)
    res = await db.execute(
        select(AgentSession)
        .where(AgentSession.agent_id == agent_id, AgentSession.user_id == current_user.id)
        .order_by(AgentSession.updated_at.desc())
        .limit(50)
    )
    sessions = res.scalars().all()
    return {"items": [_session_to_dict(s) for s in sessions]}


@router.post("/{agent_id}/sessions")
async def create_session(
    agent_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _get_or_404(db, agent_id, current_user)
    session = AgentSession(agent_id=agent_id, user_id=current_user.id, messages=[])
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return _session_to_dict(session)


@router.get("/{agent_id}/sessions/{session_id}/messages")
async def get_session_messages(
    agent_id: int,
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取会话的历史消息列表"""
    res = await db.execute(
        select(AgentSession).where(
            AgentSession.id == session_id,
            AgentSession.agent_id == agent_id,
            AgentSession.user_id == current_user.id,
        )
    )
    session = res.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"messages": session.messages or []}


@router.delete("/{agent_id}/sessions/{session_id}")
async def delete_session(
    agent_id: int,
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    res = await db.execute(
        select(AgentSession).where(
            AgentSession.id == session_id,
            AgentSession.agent_id == agent_id,
            AgentSession.user_id == current_user.id,
        )
    )
    session = res.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    await db.delete(session)
    await db.commit()
    return {"message": "已删除"}


# ── ReAct 对话（SSE 流式） ────────────────────────────────────────────────────

@router.post("/{agent_id}/chat")
async def agent_chat(
    agent_id: int,
    body: AgentChatRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    agent = await _get_or_404(db, agent_id, current_user)
    agent_config = _agent_to_dict(agent)

    # 幂等键命中：在会话/消息变更前先查重（FR-01 / §8.2），
    # 返回原 Run ID 与当前状态，绝不重复执行、不重复追加用户消息
    if idempotency_key:
        async with AsyncSessionLocal() as s:
            existing = await RunRepository(s).get_by_idempotency_key(
                current_user.id, idempotency_key
            )
        if existing is not None:
            return {
                "session_id": existing.session_id,
                "steps": [],
                "content": existing.final_text or "",
                "run_id": existing.id,
                "status": existing.status,
                "idempotent_reuse": True,
            }

    # 加载或创建会话（仅限当前用户的会话）
    session = None
    if body.session_id:
        res = await db.execute(
            select(AgentSession).where(
                AgentSession.id == body.session_id,
                AgentSession.user_id == current_user.id,
            )
        )
        session = res.scalar_one_or_none()
        if not session:
            raise HTTPException(status_code=404, detail="会话不存在")

    if not session:
        session = AgentSession(agent_id=agent_id, user_id=current_user.id, messages=[])
        db.add(session)
        await db.flush()
        # 先提交释放写锁：执行期间运行时会写 agent_runs 等表，
        # 长事务持有写锁会与运行时持久化互相阻塞（SQLite 必需，MySQL/PG 亦更健康）
        await db.commit()

    # 追加用户消息
    messages = list(session.messages or [])
    messages.append({"role": "user", "content": body.message})

    # FR-08.1：用户消息必须在 Run 创建前可靠保存（失败则不启动运行，
    # 不得等整轮 Agent 完成后才首次持久化）
    new_title = body.message[:50] if (not session.title or session.title == "新对话") else None
    await _save_session(session.id, messages, new_title, raise_on_error=True)

    # 数据库连接信息注入：让 Agent 知道它连接的是哪个数据库
    if agent_config.get("database_connection_id"):
        try:
            from app.models.database_connection import DatabaseConnection
            db_conn_res = await db.execute(
                select(DatabaseConnection).where(
                    DatabaseConnection.id == agent_config["database_connection_id"]
                )
            )
            db_conn = db_conn_res.scalar_one_or_none()
            if db_conn:
                agent_config = dict(agent_config)
                db_info = {
                    "name": db_conn.name,
                    "db_type": db_conn.db_type,
                    "database": db_conn.database,
                    "host": db_conn.host,
                    "tables": [],  # 预加载表列表
                }
                # ★ 预加载数据库表列表，避免模型猜测表名
                try:
                    import sqlalchemy as sa
                    db_type = db_conn.db_type
                    if db_type == "mysql":
                        sync_url = f"mysql+pymysql://{db_conn.username}:{db_conn.password}@{db_conn.host}:{db_conn.port}/{db_conn.database}"
                    elif db_type == "postgresql":
                        sync_url = f"postgresql+psycopg2://{db_conn.username}:{db_conn.password}@{db_conn.host}:{db_conn.port}/{db_conn.database}"
                    elif db_type == "sqlite":
                        sync_url = f"sqlite:///{db_conn.database}"
                    else:
                        sync_url = None

                    if sync_url:
                        if db_conn.extra_params:
                            sync_url += f"?{db_conn.extra_params}"
                        sync_engine = sa.create_engine(sync_url, pool_pre_ping=True)
                        insp = sa.inspect(sync_engine)
                        table_names = insp.get_table_names()
                        db_info["tables"] = table_names[:50]
                        sync_engine.dispose()
                        logger.info(f"预加载数据库 {db_conn.database} 表列表: {table_names[:10]}...")
                except Exception as e:
                    logger.warning(f"预加载数据库表列表失败: {e}")

                agent_config["_db_info"] = db_info
        except Exception as e:
            logger.warning(f"加载数据库连接信息失败: {e}")

    # 跨会话记忆：检索相关历史记忆并注入 agent_config
    if body.use_memory:
        from app.services.agent.memory_manager import MemoryManager
        memories = await MemoryManager.search_memories(
            user_id=current_user.id,
            query=body.message,
            agent_id=agent_id,
            top_k=5,
        )
        if memories:
            mem_context = MemoryManager.format_memories_for_prompt(memories)
            agent_config = dict(agent_config)
            agent_config["_memory_context"] = mem_context

    if not body.stream:
        # 非流式：收集所有步骤后返回
        steps = []
        final_content = ""
        if settings.AGENT_RUNTIME_ENABLED:
            try:
                result = await get_runtime_service().run_to_completion(
                    agent_config=agent_config,
                    messages=messages,
                    user_id=current_user.id,
                    session_id=session.id,
                    idempotency_key=idempotency_key,
                    use_memory_context=agent_config.get("_memory_context", ""),
                    db_info=agent_config.get("_db_info"),
                )
            except IdempotencyConflict as e:
                raise HTTPException(status_code=409, detail=str(e))
            steps = result["steps"]
            final_content = result["content"]
            if result["status"] != "completed":
                steps = steps + [{"type": "error", "content": f"运行状态: {result['status']}"}]
        else:
            async for event in AgentEngine.run_stream(agent_config, messages, db):
                if event["type"] == "done":
                    break
                steps.append(event)
                if event["type"] == "final":
                    final_content = event["content"]

        if not settings.AGENT_RUNTIME_ENABLED:
            # 旧引擎路径：仍由 API 层写回会话；Runtime 路径由服务统一写回（FR-08.2）
            messages.append({"role": "assistant", "content": final_content, "steps": steps})
            await _save_session(session.id, messages)

        # 跨会话记忆：从本轮对话中提取并保存记忆
        if body.use_memory:
            from app.services.agent.memory_manager import MemoryManager
            await MemoryManager.extract_and_save_memories(
                user_id=current_user.id,
                messages=[{"role": "user", "content": body.message}],
                agent_id=agent_id,
            )

        return {"session_id": session.id, "steps": steps, "content": final_content}

    # 流式 SSE
    runtime_stream = None
    run_id = None
    if settings.AGENT_RUNTIME_ENABLED:
        # 先启动运行拿到 run_id，供响应头 X-Run-Id（重连/状态查询用）
        try:
            run_id, runtime_stream = await get_runtime_service().start_run(
                agent_config=agent_config,
                messages=messages,
                user_id=current_user.id,
                session_id=session.id,
                idempotency_key=idempotency_key,
                use_memory_context=agent_config.get("_memory_context", ""),
                db_info=agent_config.get("_db_info"),
            )
        except IdempotencyConflict as e:
            raise HTTPException(status_code=409, detail=str(e))

    async def event_stream():
        collected_steps = []
        final_content = ""

        if settings.AGENT_RUNTIME_ENABLED:
            stream = runtime_stream
        else:
            stream = AgentEngine.run_stream(agent_config, messages, db)

        async for event in stream:
            collected_steps.append(event)
            if event["type"] == "final":
                final_content = event["content"]

            if event["type"] == "done":
                if not settings.AGENT_RUNTIME_ENABLED:
                    # 旧引擎路径：仍由 API 层写回会话（独立数据库会话，避免引擎
                    # 工具执行对原 db session 的污染）；Runtime 路径由服务统一写回
                    # （FR-08.2，客户端断线也不丢失）
                    messages.append({
                        "role": "assistant",
                        "content": final_content,
                        "steps": [s for s in collected_steps if s["type"] != "done"],
                    })
                    await _save_session(session.id, messages)

                # 跨会话记忆
                if body.use_memory:
                    try:
                        from app.services.agent.memory_manager import MemoryManager
                        await MemoryManager.extract_and_save_memories(
                            user_id=current_user.id,
                            messages=[{"role": "user", "content": body.message}],
                            agent_id=agent_id,
                        )
                    except Exception:
                        pass

                # 最后 yield done 事件
                data = json.dumps(event, ensure_ascii=False)
                yield f"data: {data}\n\n"
                break

            data = json.dumps(event, ensure_ascii=False)
            yield f"data: {data}\n\n"

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "X-Session-Id": str(session.id),
    }
    if run_id:
        headers["X-Run-Id"] = run_id
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=headers,
    )


# ── 工作流集成：将 Agent 作为工作流节点使用 ──────────────────────────────────

@router.post("/{agent_id}/invoke")
async def invoke_agent(
    agent_id: int,
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    """供工作流引擎调用的同步接口"""
    agent = await _get_or_404(db, agent_id, current_user)
    agent_config = _agent_to_dict(agent)

    # 幂等键命中：返回原 Run ID 与当前状态，不重复执行（FR-01 / §8.2）
    if idempotency_key:
        async with AsyncSessionLocal() as s:
            existing = await RunRepository(s).get_by_idempotency_key(
                current_user.id, idempotency_key
            )
        if existing is not None:
            return {
                "status": "ok" if existing.status == "completed" else existing.status,
                "content": existing.final_text or "",
                "steps": [],
                "iterations": 0,
                "run_id": existing.id,
                "idempotent_reuse": True,
            }

    user_input = body.get("input", "")
    messages = [{"role": "user", "content": user_input}]

    steps = []
    final_content = ""
    run_status = "completed"
    run_id = None
    if settings.AGENT_RUNTIME_ENABLED:
        try:
            result = await get_runtime_service().run_to_completion(
                agent_config=agent_config,
                messages=messages,
                user_id=current_user.id,
                idempotency_key=idempotency_key,
                use_memory_context="",
                db_info=agent_config.get("_db_info"),
            )
        except IdempotencyConflict as e:
            raise HTTPException(status_code=409, detail=str(e))
        steps = result["steps"]
        final_content = result["content"]
        run_status = result["status"]
        run_id = result.get("run_id")
    else:
        async for event in AgentEngine.run_stream(agent_config, messages, db):
            if event["type"] == "done":
                break
            steps.append(event)
            if event["type"] == "final":
                final_content = event["content"]

    return {
        "status": "ok" if run_status == "completed" else run_status,
        "content": final_content,
        "steps": steps,
        "iterations": len([s for s in steps if s["type"] == "thought"]),
        "run_id": run_id,
    }


# ── 运行控制（DeepAgents Runtime：取消 / 审批 / 恢复 / 状态）───────────────────

class ApprovalDecision(BaseModel):
    type: str = "approve"    # approve / reject
    message: str = ""
    action_id: Optional[str] = None  # 稳定审批动作 ID（FR-06）；缺省按登记顺序对齐


class ResumeRequest(BaseModel):
    decisions: List[ApprovalDecision]


def _decisions_payload(body: Optional[ResumeRequest]) -> List[dict]:
    if body is None:
        return [{"type": "approve", "message": "", "action_id": None}]
    return [
        {"type": d.type, "message": d.message, "action_id": d.action_id}
        for d in body.decisions
    ]


async def _precheck_decisions(run_id: str, decisions: List[dict], user_id: int) -> None:
    """审批前置校验：冲突/非法/无权显式映射 HTTP 409/400/403（FR-04）。"""
    try:
        await get_runtime_service().check_decisions(run_id, decisions, user_id)
    except ApprovalForbidden as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ApprovalInvalid as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ApprovalConflict as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/runs/{run_id}")
async def get_run_status(
    run_id: str,
    current_user: User = Depends(get_current_user),
):
    """查询运行状态（SSE 断连后重连 / 轮询用）"""
    info = await get_runtime_service().get_run(run_id, current_user.id)
    if not info:
        raise HTTPException(status_code=404, detail="运行不存在")
    return info


@router.get("/runs/{run_id}/events")
async def stream_run_events(
    run_id: str,
    after_seq: Optional[int] = Query(None),
    last_event_id: Optional[str] = Header(None),
    current_user: User = Depends(get_current_user),
):
    """SSE 事件流回放/订阅（FR-07）：断线后从指定序号继续，不丢失不重复。

    - ``after_seq``：返回 seq 大于该值的事件直到终态；缺省从头回放（含 seq=0）
    - ``Last-Event-ID``：标准 SSE 重连头，优先于 after_seq
    - 旧契约 ``data: {json}`` 不变（type/content 等字段保留），附带 seq 与
      标准 SSE ``id:`` 行；事件事实来源为 agent_run_events，API 与执行
      Worker 不同进程也可消费
    """
    info = await get_runtime_service().get_run(run_id, current_user.id)
    if not info:
        raise HTTPException(status_code=404, detail="运行不存在")
    if last_event_id and last_event_id.strip().isdigit():
        after_seq = int(last_event_id.strip())

    async def event_stream():
        async for event in get_runtime_service().replay_events(run_id, after_seq):
            seq = event.get("seq", 0)
            yield f"id: {seq}\ndata: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Run-Id": run_id},
    )


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    current_user: User = Depends(get_current_user),
):
    """取消运行（区分用户主动取消与失败）"""
    result = await get_runtime_service().cancel_run(run_id, current_user.id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "取消失败"))
    return result


@router.post("/runs/{run_id}/resume")
async def resume_run(
    run_id: str,
    body: ResumeRequest,
    current_user: User = Depends(get_current_user),
):
    """HITL 审批后恢复运行：返回 SSE 流（与 /chat 相同事件契约）"""
    decisions = _decisions_payload(body)
    await _precheck_decisions(run_id, decisions, current_user.id)

    async def event_stream():
        async for event in get_runtime_service().resume_run(run_id, decisions, current_user.id):
            data = json.dumps(event, ensure_ascii=False)
            yield f"data: {data}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Run-Id": run_id},
    )


@router.post("/runs/{run_id}/approve")
async def approve_run(
    run_id: str,
    body: ResumeRequest = None,
    current_user: User = Depends(get_current_user),
):
    """便捷审批：默认全部批准"""
    decisions = _decisions_payload(body)
    await _precheck_decisions(run_id, decisions, current_user.id)

    async def event_stream():
        async for event in get_runtime_service().resume_run(run_id, decisions, current_user.id):
            data = json.dumps(event, ensure_ascii=False)
            yield f"data: {data}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Run-Id": run_id},
    )


# ── 辅助函数 ──────────────────────────────────────────────────────────────────

async def _save_session(
    session_id: int,
    messages: list,
    new_title: str = None,
    *,
    raise_on_error: bool = False,
):
    """使用独立数据库会话保存对话记录，避免引擎执行污染原 session。

    raise_on_error=True 时失败向上抛出（FR-08.1：用户消息在 Run 创建前的
    可靠保存，失败不得带病启动运行）。
    """
    try:
        async with AsyncSessionLocal() as save_db:
            res = await save_db.execute(
                select(AgentSession).where(AgentSession.id == session_id)
            )
            s = res.scalar_one_or_none()
            if s:
                s.messages = messages
                flag_modified(s, "messages")
                if new_title:
                    s.title = new_title
                await save_db.commit()
                logger.info(f"会话 {session_id} 已保存，共 {len(messages)} 条消息")
            else:
                logger.error(f"会话 {session_id} 不存在，无法保存")
                if raise_on_error:
                    raise HTTPException(status_code=500, detail="会话不存在，无法保存消息")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"会话 {session_id} 保存失败: {e}", exc_info=True)
        if raise_on_error:
            raise HTTPException(status_code=500, detail="会话历史保存失败，未启动运行")


async def _get_or_404(db: AsyncSession, agent_id: int, current_user: User = None) -> Agent:
    res = await db.execute(select(Agent).where(Agent.id == agent_id, Agent.is_active == True))
    agent = res.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    # 所有权校验：非公开 Agent 仅所有者可见/可操作
    if current_user is not None and agent.owner_id not in (None, current_user.id):
        raise HTTPException(status_code=404, detail="Agent 不存在")
    return agent


def _agent_to_dict(a: Agent) -> dict:
    return {
        "id": a.id,
        "name": a.name,
        "description": a.description,
        "icon": a.icon,
        "model_config_id": a.model_config_id,
        "system_prompt": a.system_prompt,
        "max_iterations": a.max_iterations,
        "temperature": a.temperature,
        "tools": a.tools or [],
        "knowledge_base_id": a.knowledge_base_id,
        "database_connection_id": a.database_connection_id,
        "is_active": a.is_active,
        "owner_id": a.owner_id,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    }


def _session_to_dict(s: AgentSession) -> dict:
    return {
        "id": s.id,
        "agent_id": s.agent_id,
        "title": s.title,
        "message_count": len(s.messages or []),
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


# ── 监控与诊断 API ───────────────────────────────────────────────────────────

@router.get("/monitor/performance")
async def get_performance_stats(
    current_user: User = Depends(get_current_user),
):
    """获取 Agent 引擎性能统计"""
    from app.services.agent.trace import get_global_monitor
    monitor = get_global_monitor()
    return monitor.get_summary()


@router.get("/monitor/inference")
async def get_inference_stats(
    current_user: User = Depends(get_current_user),
):
    """获取推理队列和模型连接池状态"""
    from app.services.inference import ModelManager, InferenceQueue
    return {
        "model_connections": ModelManager.get_instance().get_stats(),
        "inference_queue": InferenceQueue.get_instance().get_stats(),
    }


@router.post("/monitor/reload")
async def hot_reload(
    current_user: User = Depends(get_current_user),
):
    """热重载：清空模型连接池和工具缓存"""
    from app.services.inference import ModelManager
    await ModelManager.get_instance().reload_config()
    return {"message": "热重载完成"}
