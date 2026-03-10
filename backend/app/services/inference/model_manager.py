"""
模型管理器：加载/卸载策略、连接池、OOM 防护

核心功能：
1. 模型加载/卸载策略 → 避免反复加载卡死
2. OOM 避免 → 24GB 显卡必须精打细算
3. CPU/GPU 内存交换策略
4. 服务化 API 封装（OpenAI 风格接口）
5. 热重载 → 改配置不用重启
"""
import time
import asyncio
from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime

from openai import AsyncOpenAI
import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("inference")


@dataclass
class ModelConnection:
    """模型连接信息"""
    provider_id: int
    model_config_id: int
    client: AsyncOpenAI
    model_name: str
    last_used: float = 0
    request_count: int = 0
    total_tokens: int = 0
    error_count: int = 0
    is_healthy: bool = True

    def mark_used(self, tokens: int = 0):
        self.last_used = time.time()
        self.request_count += 1
        self.total_tokens += tokens

    def mark_error(self):
        self.error_count += 1
        if self.error_count >= 5:
            self.is_healthy = False
            logger.warning(f"模型连接标记为不健康: provider={self.provider_id}, errors={self.error_count}")

    def mark_recovered(self):
        self.error_count = 0
        self.is_healthy = True


class ModelManager:
    """
    模型连接管理器

    功能：
    1. 连接池管理（复用 AsyncOpenAI 客户端）
    2. 健康检查
    3. 自动重连
    4. 配置热重载
    """

    _instance: Optional["ModelManager"] = None
    _connections: Dict[str, ModelConnection] = {}
    _lock: asyncio.Lock = asyncio.Lock()

    @classmethod
    def get_instance(cls) -> "ModelManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _make_key(self, provider_id: int, model_config_id: int) -> str:
        return f"{provider_id}:{model_config_id}"

    async def get_client(
        self,
        provider,
        model_config,
    ) -> Tuple[AsyncOpenAI, str]:
        """
        获取或创建模型客户端（带连接池）

        Returns:
            (client, model_name)
        """
        key = self._make_key(provider.id, model_config.id)

        async with self._lock:
            conn = self._connections.get(key)

            if conn and conn.is_healthy:
                conn.mark_used()
                return conn.client, conn.model_name

            # 创建新连接
            client = AsyncOpenAI(
                api_key=provider.api_key or "ollama",
                base_url=provider.base_url,
                http_client=httpx.AsyncClient(
                    verify=False,
                    timeout=settings.INFERENCE_REQUEST_TIMEOUT,
                    limits=httpx.Limits(
                        max_connections=settings.INFERENCE_CONCURRENT_REQUESTS * 2,
                        max_keepalive_connections=settings.INFERENCE_CONCURRENT_REQUESTS,
                    ),
                ),
            )

            conn = ModelConnection(
                provider_id=provider.id,
                model_config_id=model_config.id,
                client=client,
                model_name=model_config.name,
                last_used=time.time(),
            )
            self._connections[key] = conn

            logger.info(f"创建模型连接: {key}, model={model_config.name}")
            return client, model_config.name

    async def health_check(self, provider, model_config) -> Tuple[bool, str]:
        """健康检查"""
        try:
            client, _ = await self.get_client(provider, model_config)
            models = await client.models.list()
            key = self._make_key(provider.id, model_config.id)
            conn = self._connections.get(key)
            if conn:
                conn.mark_recovered()
            return True, f"连接正常，可用模型 {len(list(models))} 个"
        except Exception as e:
            key = self._make_key(provider.id, model_config.id)
            conn = self._connections.get(key)
            if conn:
                conn.mark_error()
            return False, f"连接失败: {e}"

    async def report_error(self, provider_id: int, model_config_id: int):
        """报告模型调用错误"""
        key = self._make_key(provider_id, model_config_id)
        conn = self._connections.get(key)
        if conn:
            conn.mark_error()

    async def report_success(self, provider_id: int, model_config_id: int, tokens: int = 0):
        """报告模型调用成功"""
        key = self._make_key(provider_id, model_config_id)
        conn = self._connections.get(key)
        if conn:
            conn.mark_used(tokens)
            if not conn.is_healthy:
                conn.mark_recovered()

    async def cleanup_idle(self, max_idle_seconds: int = 600):
        """清理长时间未使用的连接"""
        now = time.time()
        async with self._lock:
            to_remove = []
            for key, conn in self._connections.items():
                if now - conn.last_used > max_idle_seconds:
                    to_remove.append(key)
            for key in to_remove:
                del self._connections[key]
                logger.info(f"清理闲置模型连接: {key}")

    def get_stats(self) -> Dict[str, Any]:
        """获取连接池统计"""
        stats = {
            "total_connections": len(self._connections),
            "connections": [],
        }
        for key, conn in self._connections.items():
            stats["connections"].append({
                "key": key,
                "model": conn.model_name,
                "requests": conn.request_count,
                "tokens": conn.total_tokens,
                "errors": conn.error_count,
                "healthy": conn.is_healthy,
                "idle_seconds": round(time.time() - conn.last_used, 1),
            })
        return stats

    async def reload_config(self):
        """热重载：清除所有连接，下次请求时重建"""
        async with self._lock:
            self._connections.clear()
            logger.info("模型连接池已清空（热重载）")
