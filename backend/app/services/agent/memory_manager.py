"""
记忆系统管理器：短期/长期/结构化记忆

核心功能：
1. 短期记忆管理 → 最近 3~6 轮完整保留
2. 历史信息蒸馏 → 超长对话必须压缩
3. 上下文噪声过滤 → 旧内容不干扰新决策
4. 记忆持久化 → 重启不丢失技能
5. 关键信息强制置顶 → 原始任务永不丢失
6. 多轮上下文切片 → 只给模型最有用的信息
7. 记忆冲突解决 → 新旧信息矛盾时听谁的
8. 结构化记忆存储 → 不能只存文本，要存知识/规则/Skill
"""
import re
import json
import sqlite3
import asyncio
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path
from collections import Counter
from datetime import datetime, timedelta
from enum import Enum

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("memory")


class MemoryType(str, Enum):
    """记忆类型"""
    FACT = "fact"           # 事实知识（用户个人信息、偏好等）
    RULE = "rule"           # 规则约束（用户设定的规则）
    SKILL = "skill"         # 技能经验（成功的工具调用模式）
    EPISODE = "episode"     # 情景记忆（对话摘要）
    PREFERENCE = "preference"  # 偏好设置


class ConflictStrategy(str, Enum):
    """记忆冲突解决策略"""
    NEWER = "newer"                     # 新记忆覆盖旧记忆
    HIGHER_IMPORTANCE = "higher_importance"  # 重要性高的优先
    MERGE = "merge"                     # 合并两条记忆


def _get_memory_db_path() -> Path:
    persist_dir = Path(settings.MEMORY_DB_PATH)
    persist_dir.mkdir(parents=True, exist_ok=True)
    return persist_dir / "memory_v2.db"


def _get_conn() -> sqlite3.Connection:
    db_path = _get_memory_db_path()
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")  # 提升并发性能

    # 结构化记忆表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            agent_id INTEGER,
            memory_type TEXT NOT NULL DEFAULT 'fact',
            content TEXT NOT NULL,
            summary TEXT,
            importance REAL DEFAULT 1.0,
            access_count INTEGER DEFAULT 0,
            last_accessed TEXT,
            tags TEXT DEFAULT '',
            metadata TEXT DEFAULT '{}',
            is_pinned BOOLEAN DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            expires_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mem2_user ON memories_v2(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mem2_agent ON memories_v2(user_id, agent_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mem2_type ON memories_v2(user_id, memory_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mem2_importance ON memories_v2(importance DESC)")

    # 短期记忆/会话缓存表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            turn_index INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            is_summary BOOLEAN DEFAULT 0,
            token_count INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sess_mem ON session_memory(session_id)")

    # 技能库表（成功的工具调用模式）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS skill_library (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            skill_name TEXT NOT NULL,
            description TEXT,
            tool_sequence TEXT NOT NULL,
            success_count INTEGER DEFAULT 1,
            fail_count INTEGER DEFAULT 0,
            avg_duration_ms REAL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_skill_user ON skill_library(user_id)")

    conn.commit()
    return conn


def _tokenize(text: str) -> List[str]:
    """中英混合分词"""
    text = text.lower()
    tokens = re.findall(r'[\u4e00-\u9fff]|[a-z0-9]+', text)
    chars = re.findall(r'[\u4e00-\u9fff]', text)
    bigrams = [chars[i] + chars[i + 1] for i in range(len(chars) - 1)]
    return tokens + bigrams


def _relevance_score(query_tokens: List[str], doc_tokens: List[str]) -> float:
    """BM25-like 相关性评分"""
    if not query_tokens or not doc_tokens:
        return 0.0
    tf_map = Counter(doc_tokens)
    doc_len = len(doc_tokens)
    avg_len = 50  # 假设平均文档长度
    k1, b = 1.5, 0.75

    score = 0.0
    for qt in query_tokens:
        if qt in tf_map:
            tf = tf_map[qt]
            idf = 1.0  # 简化处理
            numerator = tf * (k1 + 1)
            denominator = tf + k1 * (1 - b + b * doc_len / avg_len)
            score += idf * numerator / denominator

    return score / max(len(query_tokens), 1)


class MemoryManager:
    """
    增强版记忆管理器

    记忆层级：
    1. 短期记忆 → 当前会话的最近 N 轮，完整保留
    2. 工作记忆 → 当前 ReAct 循环中的 Thought/Action/Observation
    3. 长期记忆 → 跨会话持久化的知识、规则、技能
    4. 结构化记忆 → 按类型分类存储（fact/rule/skill/episode）
    """

    # ── 长期记忆 CRUD ────────────────────────────────────────────────────────

    @staticmethod
    async def add_memory(
        user_id: int,
        content: str,
        memory_type: str = "fact",
        agent_id: Optional[int] = None,
        importance: float = 1.0,
        tags: str = "",
        metadata: Optional[Dict] = None,
        is_pinned: bool = False,
    ) -> int:
        """添加一条结构化记忆"""
        now = datetime.utcnow().isoformat()

        def _insert():
            conn = _get_conn()
            # 检查重复/冲突
            existing = conn.execute(
                "SELECT id, content, importance FROM memories_v2 "
                "WHERE user_id=? AND memory_type=? AND content LIKE ? LIMIT 1",
                (user_id, memory_type, f"%{content[:50]}%")
            ).fetchone()

            if existing:
                # 记忆冲突解决
                resolved_id = MemoryManager._resolve_conflict_sync(
                    conn, existing, content, importance, now
                )
                conn.commit()
                conn.close()
                return resolved_id

            cur = conn.execute(
                "INSERT INTO memories_v2 "
                "(user_id, agent_id, memory_type, content, importance, tags, metadata, is_pinned, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, agent_id, memory_type, content, importance, tags,
                 json.dumps(metadata or {}, ensure_ascii=False), is_pinned, now, now)
            )
            conn.commit()
            mem_id = cur.lastrowid
            conn.close()
            return mem_id

        loop = asyncio.get_event_loop()
        mem_id = await loop.run_in_executor(None, _insert)
        logger.memory_event("add", memory_type=memory_type, importance=importance)
        return mem_id

    @staticmethod
    def _resolve_conflict_sync(
        conn: sqlite3.Connection,
        existing: tuple,
        new_content: str,
        new_importance: float,
        now: str,
    ) -> int:
        """同步处理记忆冲突"""
        strategy = settings.MEMORY_CONFLICT_STRATEGY
        existing_id, existing_content, existing_importance = existing

        if strategy == "newer":
            conn.execute(
                "UPDATE memories_v2 SET content=?, importance=?, updated_at=? WHERE id=?",
                (new_content, new_importance, now, existing_id)
            )
            return existing_id
        elif strategy == "higher_importance":
            if new_importance > existing_importance:
                conn.execute(
                    "UPDATE memories_v2 SET content=?, importance=?, updated_at=? WHERE id=?",
                    (new_content, new_importance, now, existing_id)
                )
            return existing_id
        else:  # merge
            merged = f"{existing_content}\n[更新] {new_content}"
            merged_importance = max(existing_importance, new_importance)
            conn.execute(
                "UPDATE memories_v2 SET content=?, importance=?, updated_at=? WHERE id=?",
                (merged, merged_importance, now, existing_id)
            )
            return existing_id

    @staticmethod
    async def search_memories(
        user_id: int,
        query: str,
        agent_id: Optional[int] = None,
        memory_type: Optional[str] = None,
        top_k: int = 5,
        min_score: float = 0.1,
    ) -> List[Dict[str, Any]]:
        """检索相关记忆"""

        def _search():
            conn = _get_conn()
            conditions = ["user_id=?"]
            params: list = [user_id]

            if agent_id is not None:
                conditions.append("(agent_id=? OR agent_id IS NULL)")
                params.append(agent_id)
            if memory_type:
                conditions.append("memory_type=?")
                params.append(memory_type)

            where = " AND ".join(conditions)
            rows = conn.execute(
                f"SELECT id, content, memory_type, importance, tags, is_pinned, created_at "
                f"FROM memories_v2 WHERE {where} ORDER BY importance DESC, updated_at DESC LIMIT 200",
                tuple(params)
            ).fetchall()

            # 更新访问计数
            now = datetime.utcnow().isoformat()
            for row in rows:
                conn.execute(
                    "UPDATE memories_v2 SET access_count=access_count+1, last_accessed=? WHERE id=?",
                    (now, row[0])
                )
            conn.commit()
            conn.close()
            return rows

        loop = asyncio.get_event_loop()
        rows = await loop.run_in_executor(None, _search)

        if not rows:
            return []

        # 置顶记忆永远排在前面
        pinned = [r for r in rows if r[5]]  # is_pinned
        unpinned = [r for r in rows if not r[5]]

        query_tokens = _tokenize(query)
        if not query_tokens:
            results = pinned + unpinned[:top_k - len(pinned)]
            return [
                {"id": r[0], "content": r[1], "memory_type": r[2], "importance": r[3],
                 "tags": r[4], "is_pinned": bool(r[5]), "created_at": r[6], "score": 1.0}
                for r in results[:top_k]
            ]

        # 对非置顶记忆进行相关性评分
        scored = []
        for row in unpinned:
            doc_tokens = _tokenize(row[1])
            score = _relevance_score(query_tokens, doc_tokens) * row[3]  # × importance
            if score >= min_score:
                scored.append((score, row))

        scored.sort(key=lambda x: x[0], reverse=True)

        results = []
        # 先加入置顶记忆
        for r in pinned:
            results.append({
                "id": r[0], "content": r[1], "memory_type": r[2], "importance": r[3],
                "tags": r[4], "is_pinned": True, "created_at": r[6], "score": 2.0,
            })

        # 再加入高相关性记忆
        for score, r in scored[:top_k - len(pinned)]:
            results.append({
                "id": r[0], "content": r[1], "memory_type": r[2], "importance": r[3],
                "tags": r[4], "is_pinned": False, "created_at": r[6], "score": round(score, 4),
            })

        return results[:top_k]

    @staticmethod
    async def list_memories(
        user_id: int,
        agent_id: Optional[int] = None,
        memory_type: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """列出用户的所有记忆"""
        def _list():
            conn = _get_conn()
            conditions = ["user_id=?"]
            params: list = [user_id]
            if agent_id is not None:
                conditions.append("(agent_id=? OR agent_id IS NULL)")
                params.append(agent_id)
            if memory_type:
                conditions.append("memory_type=?")
                params.append(memory_type)
            params.append(limit)
            where = " AND ".join(conditions)
            rows = conn.execute(
                f"SELECT id, content, memory_type, importance, tags, is_pinned, created_at "
                f"FROM memories_v2 WHERE {where} "
                f"ORDER BY is_pinned DESC, importance DESC, updated_at DESC LIMIT ?",
                tuple(params)
            ).fetchall()
            conn.close()
            return rows

        loop = asyncio.get_event_loop()
        rows = await loop.run_in_executor(None, _list)
        return [
            {"id": r[0], "content": r[1], "memory_type": r[2], "importance": r[3],
             "tags": r[4], "is_pinned": bool(r[5]), "created_at": r[6]}
            for r in rows
        ]

    @staticmethod
    async def delete_memory(memory_id: int, user_id: int) -> bool:
        def _delete():
            conn = _get_conn()
            cur = conn.execute(
                "DELETE FROM memories_v2 WHERE id=? AND user_id=?", (memory_id, user_id)
            )
            conn.commit()
            conn.close()
            return cur.rowcount > 0
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _delete)

    @staticmethod
    async def clear_memories(user_id: int, agent_id: Optional[int] = None) -> int:
        def _clear():
            conn = _get_conn()
            if agent_id is not None:
                cur = conn.execute(
                    "DELETE FROM memories_v2 WHERE user_id=? AND agent_id=?", (user_id, agent_id)
                )
            else:
                cur = conn.execute("DELETE FROM memories_v2 WHERE user_id=?", (user_id,))
            conn.commit()
            conn.close()
            return cur.rowcount
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _clear)

    # ── 记忆自动提取 ─────────────────────────────────────────────────────────

    @staticmethod
    async def extract_and_save_memories(
        user_id: int,
        messages: List[Dict[str, Any]],
        agent_id: Optional[int] = None,
    ) -> List[str]:
        """从对话中智能提取记忆"""
        extracted = []

        # 规则模式匹配
        patterns_map = {
            MemoryType.PREFERENCE: [
                r'我(喜欢|偏好|习惯|常用|经常|一般|通常|总是|不喜欢|讨厌|不想|不要)[^，。！？\n]{2,40}',
                r'(默认|统一|固定)(使用|采用|设置|格式)[^，。！？\n]{2,40}',
            ],
            MemoryType.FACT: [
                r'我(是|叫|名字|职业|工作|住在|来自|在)[^，。！？\n]{2,30}',
                r'我的(名字|姓名|职业|公司|邮箱|电话|地址|爱好|目标|需求)[^，。！？\n]{2,40}',
            ],
            MemoryType.RULE: [
                r'(请|帮我|以后|下次|你要|你需要)(记住|记得|注意|知道)[^，。！？\n]{2,60}',
                r'记住[：:，,]?.{2,60}',
                r'(我|我们)(希望|想要|需要|要求|规定|约定)[^，。！？\n]{2,40}',
            ],
        }

        for msg in messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if not content or len(content) < 4:
                continue

            for mem_type, patterns in patterns_map.items():
                for pattern in patterns:
                    if re.search(pattern, content):
                        mem_text = content[:300]
                        if mem_text not in extracted:
                            extracted.append(mem_text)
                            await MemoryManager.add_memory(
                                user_id=user_id,
                                content=mem_text,
                                memory_type=mem_type.value,
                                agent_id=agent_id,
                                importance=1.5 if mem_type == MemoryType.RULE else 1.0,
                                tags="auto_extracted",
                            )
                        break

        return extracted

    # ── 技能库管理 ────────────────────────────────────────────────────────────

    @staticmethod
    async def record_skill(
        user_id: int,
        skill_name: str,
        tool_sequence: List[Dict],
        success: bool = True,
        duration_ms: float = 0,
    ):
        """记录工具调用模式到技能库"""
        now = datetime.utcnow().isoformat()
        seq_json = json.dumps(tool_sequence, ensure_ascii=False)

        def _record():
            conn = _get_conn()
            existing = conn.execute(
                "SELECT id, success_count, fail_count, avg_duration_ms FROM skill_library "
                "WHERE user_id=? AND skill_name=?",
                (user_id, skill_name)
            ).fetchone()

            if existing:
                sid, sc, fc, avg_dur = existing
                if success:
                    new_sc = sc + 1
                    new_avg = (avg_dur * sc + duration_ms) / new_sc
                    conn.execute(
                        "UPDATE skill_library SET success_count=?, avg_duration_ms=?, updated_at=? WHERE id=?",
                        (new_sc, new_avg, now, sid)
                    )
                else:
                    conn.execute(
                        "UPDATE skill_library SET fail_count=fail_count+1, updated_at=? WHERE id=?",
                        (now, sid)
                    )
            else:
                conn.execute(
                    "INSERT INTO skill_library (user_id, skill_name, tool_sequence, success_count, fail_count, avg_duration_ms, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (user_id, skill_name, seq_json, 1 if success else 0, 0 if success else 1, duration_ms, now, now)
                )
            conn.commit()
            conn.close()

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _record)

    @staticmethod
    async def get_relevant_skills(user_id: int, query: str, top_k: int = 3) -> List[Dict]:
        """获取相关技能"""
        def _get():
            conn = _get_conn()
            rows = conn.execute(
                "SELECT skill_name, description, tool_sequence, success_count, fail_count "
                "FROM skill_library WHERE user_id=? AND success_count > fail_count "
                "ORDER BY success_count DESC LIMIT ?",
                (user_id, top_k * 3)
            ).fetchall()
            conn.close()
            return rows

        loop = asyncio.get_event_loop()
        rows = await loop.run_in_executor(None, _get)

        query_tokens = _tokenize(query)
        scored = []
        for r in rows:
            doc_tokens = _tokenize(r[0] + " " + (r[1] or ""))
            score = _relevance_score(query_tokens, doc_tokens)
            scored.append((score, r))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {"skill_name": r[0], "description": r[1],
             "tool_sequence": json.loads(r[2]), "success_count": r[3]}
            for _, r in scored[:top_k]
        ]

    # ── 格式化输出 ────────────────────────────────────────────────────────────

    @staticmethod
    def format_memories_for_prompt(memories: List[Dict[str, Any]]) -> str:
        """将记忆格式化为系统提示词片段"""
        if not memories:
            return ""

        sections = {
            "fact": [],
            "rule": [],
            "skill": [],
            "preference": [],
            "episode": [],
        }

        for m in memories:
            mem_type = m.get("memory_type", "fact")
            sections.get(mem_type, sections["fact"]).append(m["content"])

        lines = ["【用户历史记忆】"]

        type_labels = {
            "fact": "已知信息",
            "rule": "用户规则（必须遵守）",
            "preference": "用户偏好",
            "skill": "已学技能",
            "episode": "历史摘要",
        }

        for mem_type, items in sections.items():
            if items:
                label = type_labels.get(mem_type, mem_type)
                lines.append(f"\n## {label}")
                for item in items[:5]:  # 每类最多 5 条
                    lines.append(f"- {item}")

        return "\n".join(lines)
