"""🔍 知识检索工具：knowledge_search"""
from typing import Dict, Any, List

from sqlalchemy import select


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "knowledge_search",
        "display_name": "🔍 知识库检索",
        "category": "知识检索",
        "description": "在知识库中语义检索相关内容，适合查找企业文档、产品手册、政策法规等",
        "parameters": {
            "query": {"type": "string",  "description": "检索关键词或问题"},
            "top_k": {"type": "integer", "description": "返回结果数量，默认3", "default": 3},
        },
        "required": ["query"],
    },
]


async def execute_knowledge_search(db, agent_config: dict, params: dict) -> str:
    query = params.get("query", "")
    top_k = int(params.get("top_k", 3))
    kb_id = agent_config.get("knowledge_base_id")

    if not kb_id:
        return "未配置知识库，请在 Agent 配置中关联知识库"
    try:
        from app.models.knowledge import KnowledgeBase
        from app.services.knowledge_service import KnowledgeService

        res = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
        kb  = res.scalar_one_or_none()
        if not kb:
            return f"知识库 ID={kb_id} 不存在"

        results = await KnowledgeService.search(
            collection_name=kb.collection_name, query=query, top_k=top_k
        )
        if not results:
            return "知识库中未找到相关内容"
        return "\n\n".join(
            f"[{i + 1}] {r.get('content', '')[:600]}" for i, r in enumerate(results)
        )
    except Exception as e:
        return f"知识库检索失败: {e}"


HANDLERS = {
    "knowledge_search": execute_knowledge_search,
}
