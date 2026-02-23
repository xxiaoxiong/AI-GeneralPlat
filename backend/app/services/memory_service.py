"""
跨会话记忆服务：基于 SQLite 存储用户长期记忆摘要
支持：记忆写入、检索（关键词匹配）、自动摘要压缩
"""
import re
import math
import sqlite3
import asyncio
import json
from typing import List, Dict, Any, Optional
from pathlib import Path
from collections import Counter
from datetime import datetime

from app.core.config import settings


def _get_memory_db_path() -> Path:
    persist_dir = Path(settings.CHROMA_PERSIST_DIR)
    persist_dir.mkdir(parents=True, exist_ok=True)
    return persist_dir / "memory.db"


def _get_conn() -> sqlite3.Connection:
    db_path = _get_memory_db_path()
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            agent_id INTEGER,
            content TEXT NOT NULL,
            summary TEXT,
            importance REAL DEFAULT 1.0,
            tags TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_user ON memories(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_agent ON memories(user_id, agent_id)")
    conn.commit()
    return conn


def _tokenize(text: str) -> List[str]:
    text = text.lower()
    tokens = re.findall(r'[\u4e00-\u9fff]|[a-z0-9]+', text)
    chars = re.findall(r'[\u4e00-\u9fff]', text)
    bigrams = [chars[i] + chars[i+1] for i in range(len(chars)-1)]
    return tokens + bigrams


def _score(query_tokens: List[str], doc_tokens: List[str]) -> float:
    if not query_tokens or not doc_tokens:
        return 0.0
    tf_map = Counter(doc_tokens)
    hits = sum(1 for qt in query_tokens if qt in tf_map)
    return hits / len(query_tokens)


class MemoryService:

    @staticmethod
    async def add_memory(
        user_id: int,
        content: str,
        agent_id: Optional[int] = None,
        importance: float = 1.0,
        tags: str = "",
    ) -> int:
        """添加一条记忆"""
        now = datetime.utcnow().isoformat()

        def _insert():
            conn = _get_conn()
            cur = conn.execute(
                "INSERT INTO memories (user_id, agent_id, content, importance, tags, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, agent_id, content, importance, tags, now, now)
            )
            conn.commit()
            mem_id = cur.lastrowid
            conn.close()
            return mem_id

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _insert)

    @staticmethod
    async def search_memories(
        user_id: int,
        query: str,
        agent_id: Optional[int] = None,
        top_k: int = 5,
        score_threshold: float = 0.2,
    ) -> List[Dict[str, Any]]:
        """检索与 query 相关的记忆"""
        def _search():
            conn = _get_conn()
            if agent_id is not None:
                rows = conn.execute(
                    "SELECT id, content, importance, tags, created_at FROM memories "
                    "WHERE user_id=? AND (agent_id=? OR agent_id IS NULL) ORDER BY created_at DESC LIMIT 200",
                    (user_id, agent_id)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, content, importance, tags, created_at FROM memories "
                    "WHERE user_id=? ORDER BY created_at DESC LIMIT 200",
                    (user_id,)
                ).fetchall()
            conn.close()
            return rows

        loop = asyncio.get_event_loop()
        rows = await loop.run_in_executor(None, _search)
        if not rows:
            return []

        query_tokens = _tokenize(query)
        if not query_tokens:
            # 无查询词时返回最近记忆
            return [
                {"id": r[0], "content": r[1], "importance": r[2], "tags": r[3], "created_at": r[4], "score": 1.0}
                for r in rows[:top_k]
            ]

        scored = []
        for row in rows:
            doc_tokens = _tokenize(row[1])
            s = _score(query_tokens, doc_tokens) * row[2]  # 乘以重要性权重
            if s >= score_threshold:
                scored.append((s, row))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {"id": r[0], "content": r[1], "importance": r[2], "tags": r[3], "created_at": r[4], "score": round(s, 4)}
            for s, r in scored[:top_k]
        ]

    @staticmethod
    async def list_memories(
        user_id: int,
        agent_id: Optional[int] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """列出用户的所有记忆"""
        def _list():
            conn = _get_conn()
            if agent_id is not None:
                rows = conn.execute(
                    "SELECT id, content, importance, tags, created_at FROM memories "
                    "WHERE user_id=? AND (agent_id=? OR agent_id IS NULL) ORDER BY created_at DESC LIMIT ?",
                    (user_id, agent_id, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, content, importance, tags, created_at FROM memories "
                    "WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                    (user_id, limit)
                ).fetchall()
            conn.close()
            return rows

        loop = asyncio.get_event_loop()
        rows = await loop.run_in_executor(None, _list)
        return [
            {"id": r[0], "content": r[1], "importance": r[2], "tags": r[3], "created_at": r[4]}
            for r in rows
        ]

    @staticmethod
    async def delete_memory(memory_id: int, user_id: int) -> bool:
        """删除一条记忆"""
        def _delete():
            conn = _get_conn()
            cur = conn.execute(
                "DELETE FROM memories WHERE id=? AND user_id=?", (memory_id, user_id)
            )
            conn.commit()
            conn.close()
            return cur.rowcount > 0

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _delete)

    @staticmethod
    async def clear_memories(user_id: int, agent_id: Optional[int] = None) -> int:
        """清空用户记忆"""
        def _clear():
            conn = _get_conn()
            if agent_id is not None:
                cur = conn.execute(
                    "DELETE FROM memories WHERE user_id=? AND agent_id=?", (user_id, agent_id)
                )
            else:
                cur = conn.execute("DELETE FROM memories WHERE user_id=?", (user_id,))
            conn.commit()
            conn.close()
            return cur.rowcount

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _clear)

    @staticmethod
    async def extract_and_save_memories(
        user_id: int,
        messages: List[Dict[str, Any]],
        agent_id: Optional[int] = None,
    ) -> List[str]:
        """
        从对话消息中提取值得记忆的信息（规则式提取）
        返回提取到的记忆内容列表
        """
        extracted = []
        # 提取用户明确表达偏好/信息的语句
        preference_patterns = [
            # 个人信息
            r'我(喜欢|偏好|习惯|常用|经常|一般|通常|总是|不喜欢|讨厌|不想|不要)[^，。！？\n]{2,40}',
            r'我(是|叫|名字|职业|工作|住在|来自|在)[^，。！？\n]{2,30}',
            r'我的(名字|姓名|职业|公司|邮箱|电话|地址|爱好|目标|需求)[^，。！？\n]{2,40}',
            # 明确要求记忆
            r'(请|帮我|以后|下次|你要|你需要)(记住|记得|注意|知道)[^，。！？\n]{2,60}',
            r'记住[：:，,]?.{2,60}',
            # 偏好与设定
            r'(我|我们)(希望|想要|需要|要求|规定|约定)[^，。！？\n]{2,40}',
            r'(默认|统一|固定)(使用|采用|设置|格式)[^，。！？\n]{2,40}',
        ]

        for msg in messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if not content or len(content) < 4:
                continue
            matched = False
            for pattern in preference_patterns:
                if re.search(pattern, content):
                    mem_text = content[:300]
                    if mem_text not in extracted:
                        extracted.append(mem_text)
                        await MemoryService.add_memory(
                            user_id=user_id,
                            content=mem_text,
                            agent_id=agent_id,
                            importance=1.5,
                            tags="auto_extracted",
                        )
                    matched = True
                    break  # 每条消息只提取一次

        return extracted

    @staticmethod
    def format_memories_for_prompt(memories: List[Dict[str, Any]]) -> str:
        """将记忆格式化为系统提示词片段"""
        if not memories:
            return ""
        lines = ["【用户历史记忆】（请参考以下信息来个性化回答）:"]
        for m in memories:
            lines.append(f"- {m['content']}")
        return "\n".join(lines)
