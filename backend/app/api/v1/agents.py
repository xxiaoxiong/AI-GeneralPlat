"""
Agent 管理与对话 API（v2 增强版）
"""
import json
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from pydantic import BaseModel

from app.core.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.agent import Agent, AgentSession
from app.services.agent import AgentEngine
from app.services.tools import BUILTIN_TOOLS

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
    agent = await _get_or_404(db, agent_id)
    return _agent_to_dict(agent)


@router.put("/{agent_id}")
async def update_agent(
    agent_id: int,
    body: AgentUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent = await _get_or_404(db, agent_id)
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
    agent = await _get_or_404(db, agent_id)
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
    await _get_or_404(db, agent_id)
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
    await _get_or_404(db, agent_id)
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
        select(AgentSession).where(AgentSession.id == session_id, AgentSession.agent_id == agent_id)
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
):
    agent = await _get_or_404(db, agent_id)
    agent_config = _agent_to_dict(agent)

    # 加载或创建会话
    session = None
    if body.session_id:
        res = await db.execute(
            select(AgentSession).where(AgentSession.id == body.session_id)
        )
        session = res.scalar_one_or_none()

    if not session:
        session = AgentSession(agent_id=agent_id, user_id=current_user.id, messages=[])
        db.add(session)
        await db.flush()

    # 追加用户消息
    messages = list(session.messages or [])
    messages.append({"role": "user", "content": body.message})

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
        async for event in AgentEngine.run_stream(agent_config, messages, db):
            if event["type"] == "done":
                break
            steps.append(event)
            if event["type"] == "final":
                final_content = event["content"]

        # 保存到会话
        messages.append({"role": "assistant", "content": final_content, "steps": steps})
        session.messages = messages
        if len(messages) == 2:
            session.title = body.message[:50]
        await db.commit()

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
    async def event_stream():
        collected_steps = []
        final_content = ""

        async for event in AgentEngine.run_stream(agent_config, messages, db):
            collected_steps.append(event)
            if event["type"] == "final":
                final_content = event["content"]
            data = json.dumps(event, ensure_ascii=False)
            yield f"data: {data}\n\n"

            if event["type"] == "done":
                break

        # 保存会话（在流结束后）
        messages.append({
            "role": "assistant",
            "content": final_content,
            "steps": [s for s in collected_steps if s["type"] != "done"],
        })
        session.messages = messages
        if len(messages) == 2:
            session.title = body.message[:50]
        try:
            await db.commit()
        except Exception:
            pass

        # 跨会话记忆：从本轮对话中提取并保存记忆
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

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Session-Id": str(session.id),
        },
    )


# ── 工作流集成：将 Agent 作为工作流节点使用 ──────────────────────────────────

@router.post("/{agent_id}/invoke")
async def invoke_agent(
    agent_id: int,
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """供工作流引擎调用的同步接口"""
    agent = await _get_or_404(db, agent_id)
    agent_config = _agent_to_dict(agent)
    user_input = body.get("input", "")
    messages = [{"role": "user", "content": user_input}]

    steps = []
    final_content = ""
    async for event in AgentEngine.run_stream(agent_config, messages, db):
        if event["type"] == "done":
            break
        steps.append(event)
        if event["type"] == "final":
            final_content = event["content"]

    return {
        "status": "ok",
        "content": final_content,
        "steps": steps,
        "iterations": len([s for s in steps if s["type"] == "thought"]),
    }


# ── 辅助函数 ──────────────────────────────────────────────────────────────────

async def _get_or_404(db: AsyncSession, agent_id: int) -> Agent:
    res = await db.execute(select(Agent).where(Agent.id == agent_id, Agent.is_active == True))
    agent = res.scalar_one_or_none()
    if not agent:
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
