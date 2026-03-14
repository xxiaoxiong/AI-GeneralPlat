"""
增强工具执行器：超时/重试/权限/依赖/状态追踪

核心功能：
1. 工具参数约束 → 必须强类型、强校验
2. 工具返回结果格式化 → 模型看不懂乱格式
3. 多工具组合调用 → 30B 容易乱序
4. 工具依赖关系 → 必须先执行 A 再执行 B
5. 工具权限控制 → 本地 Agent 不能乱删文件
6. 工具执行超时 → 必须有超时机制
7. 工具执行状态追踪 → Agent 要知道工具跑到哪了
8. 失败重试机制 → 工具失败要自动重试
"""
import time
import asyncio
import json
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass, field
from enum import Enum

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("tool")


class ToolPermission(str, Enum):
    """工具权限级别"""
    READ = "read"           # 只读（查询、搜索等）
    WRITE = "write"         # 写入（数据库写入、文件创建等）
    EXECUTE = "execute"     # 执行（代码执行、系统命令等）
    NETWORK = "network"     # 网络（HTTP 请求、邮件发送等）
    DANGEROUS = "dangerous" # 危险操作（删除文件、清空数据等）


@dataclass
class ToolCallRecord:
    """工具调用记录"""
    tool_name: str
    tool_input: Dict[str, Any]
    result: str = ""
    success: bool = True
    error: str = ""
    duration_ms: float = 0
    retry_count: int = 0
    timestamp: float = 0

    def to_dict(self) -> Dict:
        return {
            "tool_name": self.tool_name,
            "tool_input": self.tool_input,
            "result_preview": self.result[:200] if self.result else "",
            "success": self.success,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 2),
            "retry_count": self.retry_count,
        }


# ── 工具权限映射 ─────────────────────────────────────────────────────────────

TOOL_PERMISSIONS: Dict[str, ToolPermission] = {
    # 只读工具
    "datetime_now": ToolPermission.READ,
    "calculator": ToolPermission.READ,
    "knowledge_search": ToolPermission.READ,
    "web_search": ToolPermission.READ,
    "fetch_webpage": ToolPermission.READ,
    "weather": ToolPermission.READ,
    # 网络工具
    "http_get": ToolPermission.NETWORK,
    "http_post": ToolPermission.NETWORK,
    "http_request": ToolPermission.NETWORK,
    "send_email_tool": ToolPermission.NETWORK,
    "webhook_notify": ToolPermission.NETWORK,
    # 数据库工具
    "db_schema": ToolPermission.READ,
    "db_query": ToolPermission.READ,
    "db_count": ToolPermission.READ,
    "db_aggregate": ToolPermission.READ,
    # 执行工具
    "python_exec": ToolPermission.EXECUTE,
}

# ── 工具依赖关系（B 依赖 A 的结果）────────────────────────────────────────────

TOOL_DEPENDENCIES: Dict[str, List[str]] = {
    # "tool_b": ["tool_a"]  # tool_b 需要 tool_a 先执行
}

# ── 默认允许的权限级别 ───────────────────────────────────────────────────────

DEFAULT_ALLOWED_PERMISSIONS = {
    ToolPermission.READ,
    ToolPermission.NETWORK,
    ToolPermission.WRITE,
    ToolPermission.EXECUTE,
}


class EnhancedToolExecutor:
    """
    增强版工具执行器

    功能增强：
    1. 参数类型校验
    2. 执行超时控制
    3. 自动重试（指数退避）
    4. 权限检查
    5. 依赖关系检查
    6. 执行状态追踪
    7. 结果格式化
    """

    def __init__(
        self,
        db,
        agent_config: dict,
        allowed_permissions: Optional[set] = None,
    ):
        self.db = db
        self.agent_config = agent_config
        self.allowed_permissions = allowed_permissions or DEFAULT_ALLOWED_PERMISSIONS

        # 执行历史追踪
        self.call_history: List[ToolCallRecord] = []
        self.call_counts: Dict[str, int] = {}

        # 外部数据库引擎缓存（避免每次调用都创建新引擎）
        self._ext_engine = None
        self._ext_engine_conn_id = None
        self._active_db_label = "[数据库: 未设置]"

        # 从旧工具系统导入 handler
        from app.services.tools import (
            _SYNC_HANDLERS, _ASYNC_HANDLERS, _DB_HANDLERS, _AGENT_HANDLERS
        )
        self._sync_handlers = _SYNC_HANDLERS
        self._async_handlers = _ASYNC_HANDLERS
        self._db_handlers = _DB_HANDLERS
        self._agent_handlers = _AGENT_HANDLERS

    async def execute(
        self,
        tool_name: str,
        tool_input: Dict[str, Any],
        timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
    ) -> str:
        """
        执行工具调用（带超时、重试、权限检查）

        Returns:
            格式化后的工具执行结果字符串
        """
        timeout = timeout or settings.AGENT_TOOL_TIMEOUT_SECONDS
        max_retries = max_retries or settings.AGENT_MAX_TOOL_RETRIES

        record = ToolCallRecord(
            tool_name=tool_name,
            tool_input=tool_input,
            timestamp=time.time(),
        )

        # ── 1. 权限检查 ──────────────────────────────────────────────────
        perm = TOOL_PERMISSIONS.get(tool_name, ToolPermission.READ)
        if perm not in self.allowed_permissions:
            error_msg = f"权限不足：工具 '{tool_name}' 需要 {perm.value} 权限"
            record.success = False
            record.error = error_msg
            self.call_history.append(record)
            logger.tool_call(tool_name, tool_input, success=False, result_preview=error_msg)
            return error_msg

        # ── 2. 调用次数检查 ──────────────────────────────────────────────
        self.call_counts[tool_name] = self.call_counts.get(tool_name, 0) + 1
        if self.call_counts[tool_name] > settings.AGENT_MAX_SAME_TOOL_CALLS:
            error_msg = f"工具 '{tool_name}' 已调用 {self.call_counts[tool_name]} 次，超过限制 {settings.AGENT_MAX_SAME_TOOL_CALLS}"
            record.success = False
            record.error = error_msg
            self.call_history.append(record)
            return error_msg

        # ── 3. 依赖检查 ──────────────────────────────────────────────────
        deps = TOOL_DEPENDENCIES.get(tool_name, [])
        for dep in deps:
            if dep not in [r.tool_name for r in self.call_history if r.success]:
                error_msg = f"工具 '{tool_name}' 依赖 '{dep}' 的执行结果，请先调用 '{dep}'"
                record.success = False
                record.error = error_msg
                self.call_history.append(record)
                return error_msg

        # ── 4. 参数校验 ──────────────────────────────────────────────────
        validation_error = self._validate_params(tool_name, tool_input)
        if validation_error:
            record.success = False
            record.error = validation_error
            self.call_history.append(record)
            logger.tool_call(tool_name, tool_input, success=False, result_preview=validation_error)
            return f"参数错误：{validation_error}"

        # ── 5. 带重试的执行 ──────────────────────────────────────────────
        last_error = ""
        for attempt in range(max_retries + 1):
            start_time = time.perf_counter()
            try:
                result = await asyncio.wait_for(
                    self._execute_impl(tool_name, tool_input),
                    timeout=timeout,
                )
                duration_ms = (time.perf_counter() - start_time) * 1000

                # 格式化结果
                formatted = self._format_result(tool_name, result)

                record.result = formatted
                record.success = True
                record.duration_ms = duration_ms
                record.retry_count = attempt
                self.call_history.append(record)

                logger.tool_call(
                    tool_name, tool_input,
                    duration_ms=duration_ms,
                    success=True,
                    result_preview=formatted[:200],
                )

                return formatted

            except asyncio.TimeoutError:
                last_error = f"执行超时（{timeout}秒）"
                logger.tool_call(tool_name, tool_input, success=False, result_preview=last_error)
                if attempt < max_retries:
                    await asyncio.sleep(min(2 ** attempt, 5))  # 指数退避

            except Exception as e:
                last_error = str(e)
                logger.tool_call(tool_name, tool_input, success=False, result_preview=last_error)
                if attempt < max_retries:
                    await asyncio.sleep(min(2 ** attempt, 5))

        # 所有重试都失败
        record.success = False
        record.error = last_error
        record.retry_count = max_retries
        record.duration_ms = (time.perf_counter() - start_time) * 1000
        self.call_history.append(record)

        return f"工具执行失败（已重试 {max_retries} 次）: {last_error}"

    async def _get_db_session(self):
        """获取数据库会话：仅允许使用用户配置的外部数据库连接。"""
        conn_id = self.agent_config.get("database_connection_id")
        if not conn_id:
            raise ValueError("未配置外部数据库连接，已阻止访问应用内置数据库。请先在智能体中绑定 database_connection_id")

        logger.info(f"使用外部数据库连接 ID={conn_id}")

        # 复用已缓存的引擎
        if self._ext_engine and self._ext_engine_conn_id == conn_id:
            from sqlalchemy.ext.asyncio import AsyncSession as ExtSession, async_sessionmaker
            factory = async_sessionmaker(self._ext_engine, class_=ExtSession, expire_on_commit=False)
            ext_session = factory()
            return ext_session, True

        # 加载用户配置的外部数据库连接
        from sqlalchemy import select
        from app.models.database_connection import DatabaseConnection
        owner_id = self.agent_config.get("owner_id")
        query = select(DatabaseConnection).where(
            DatabaseConnection.id == conn_id,
            DatabaseConnection.is_active == True,
        )
        if owner_id is not None:
            query = query.where(DatabaseConnection.owner_id == owner_id)

        res = await self.db.execute(query)
        conn = res.scalar_one_or_none()
        if not conn:
            raise ValueError(
                f"数据库连接 ID={conn_id} 不存在、未启用或无权限访问，请在智能体设置中重新配置数据库连接"
            )

        logger.info(f"连接外部数据库: {conn.db_type}://{conn.host}:{conn.port}/{conn.database} (名称: {conn.name})")

        from app.api.v1.databases import _build_engine
        ext_engine = _build_engine(conn)
        self._ext_engine = ext_engine
        self._ext_engine_conn_id = conn_id

        from sqlalchemy.ext.asyncio import AsyncSession as ExtSession, async_sessionmaker
        factory = async_sessionmaker(ext_engine, class_=ExtSession, expire_on_commit=False)
        ext_session = factory()
        self._active_db_label = f"[数据库: {conn.name}/{conn.database}]"
        # 返回外部会话，需要调用者关闭
        return ext_session, True

    async def close_external_engine(self):
        """清理外部数据库引擎（在 Agent 执行结束后调用）"""
        if self._ext_engine:
            try:
                await self._ext_engine.dispose()
                logger.info("外部数据库引擎已释放")
            except Exception as e:
                logger.warning(f"释放外部数据库引擎失败: {e}")
            self._ext_engine = None
            self._ext_engine_conn_id = None

    async def _execute_impl(self, tool_name: str, tool_input: Dict) -> str:
        """实际执行工具调用"""
        if tool_name in self._sync_handlers:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, self._sync_handlers[tool_name], tool_input
            )

        if tool_name in self._async_handlers:
            return await self._async_handlers[tool_name](tool_input)

        if tool_name in self._db_handlers:
            db_session, need_close = await self._get_db_session()
            try:
                result = await self._db_handlers[tool_name](db_session, tool_input)
                return f"{self._active_db_label}\n{result}"
            finally:
                if need_close:
                    await db_session.close()

        if tool_name in self._agent_handlers:
            return await self._agent_handlers[tool_name](self.db, self.agent_config, tool_input)

        # 自定义 HTTP 工具
        custom = next(
            (t for t in self.agent_config.get("custom_tools", []) if t["name"] == tool_name),
            None,
        )
        if custom:
            return await self._custom_http_tool(custom, tool_input)

        raise ValueError(f"未知工具: {tool_name}")

    async def _custom_http_tool(self, tool_config: dict, params: dict) -> str:
        """自定义 HTTP 工具执行"""
        import httpx
        url = tool_config.get("url", "")
        method = tool_config.get("method", "GET").upper()
        for k, v in params.items():
            url = url.replace(f"{{{k}}}", str(v))
        async with httpx.AsyncClient(timeout=15, verify=False) as client:
            resp = await client.get(url) if method == "GET" else await client.post(url, json=params)
        try:
            return json.dumps(resp.json(), ensure_ascii=False)[:2000]
        except Exception:
            return resp.text[:2000]

    def _validate_params(self, tool_name: str, tool_input: Dict) -> Optional[str]:
        """参数类型校验"""
        from app.services.tools import get_tool_by_name
        tool_def = get_tool_by_name(tool_name)

        if not tool_def:
            # 可能是自定义工具，跳过校验
            return None

        required = tool_def.get("required", [])
        parameters = tool_def.get("parameters", {})

        # 检查必填参数
        for req_param in required:
            if req_param not in tool_input:
                return f"缺少必填参数: {req_param}"

        # 检查参数类型
        for param_name, param_value in tool_input.items():
            if param_name in parameters:
                expected_type = parameters[param_name].get("type", "string")
                if not self._check_type(param_value, expected_type):
                    return f"参数 '{param_name}' 类型错误: 期望 {expected_type}, 实际 {type(param_value).__name__}"

        return None

    @staticmethod
    def _check_type(value: Any, expected_type: str) -> bool:
        """类型检查（宽松）"""
        if expected_type == "string":
            return isinstance(value, (str, int, float))  # 允许数字自动转字符串
        elif expected_type == "number" or expected_type == "integer":
            if isinstance(value, str):
                try:
                    float(value)
                    return True
                except ValueError:
                    return False
            return isinstance(value, (int, float))
        elif expected_type == "boolean":
            return isinstance(value, (bool, int))
        elif expected_type == "array":
            return isinstance(value, list)
        elif expected_type == "object":
            return isinstance(value, dict)
        return True  # 未知类型默认通过

    def _format_result(self, tool_name: str, result: str) -> str:
        """
        格式化工具返回结果，让模型更容易理解

        核心原则：
        - 结果不能太长（截断 + 告知总长度）
        - JSON 要格式化为可读形式
        - 错误信息要突出
        """
        if not result:
            return f"工具 {tool_name} 执行完成，无返回结果"

        max_len = 3000

        # 尝试解析 JSON 并格式化
        try:
            parsed = json.loads(result)
            if isinstance(parsed, dict):
                # 提取关键信息，去除冗余
                formatted = json.dumps(parsed, ensure_ascii=False, indent=2)
                if len(formatted) > max_len:
                    formatted = formatted[:max_len] + f"\n...[结果已截断，原始 {len(formatted)} 字符]"
                return formatted
        except (json.JSONDecodeError, TypeError):
            pass

        # 纯文本截断
        if len(result) > max_len:
            return result[:max_len] + f"\n...[结果已截断，原始 {len(result)} 字符]"

        return result

    def get_execution_summary(self) -> Dict[str, Any]:
        """获取执行摘要"""
        total_calls = len(self.call_history)
        successful = sum(1 for r in self.call_history if r.success)
        failed = total_calls - successful
        total_time = sum(r.duration_ms for r in self.call_history)

        return {
            "total_calls": total_calls,
            "successful": successful,
            "failed": failed,
            "total_duration_ms": round(total_time, 2),
            "tools_used": list(self.call_counts.keys()),
            "call_details": [r.to_dict() for r in self.call_history],
        }

    def get_observations_for_verify(self) -> List[Dict[str, str]]:
        """获取所有成功的工具观察结果（供幻觉检查用）"""
        return [
            {"tool_name": r.tool_name, "content": r.result}
            for r in self.call_history
            if r.success and r.result
        ]

    def has_exceeded_tool_limit(self, tool_name: str) -> bool:
        """检查工具是否超过调用限制"""
        return self.call_counts.get(tool_name, 0) >= settings.AGENT_MAX_SAME_TOOL_CALLS
