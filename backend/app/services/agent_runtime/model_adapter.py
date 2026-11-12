"""
ModelConfig/ModelProvider -> LangChain ChatModel 适配。

- 支持 OpenAI 兼容（LiteLLM / vLLM / 标准服务）
- 启动/构造时检测原生工具调用能力：supports_function_call=False 显式报错（AT-14），
  不回退文本 ReAct
- TLS 校验默认开启，可配置受信任 CA 证书
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from langchain_openai import ChatOpenAI

from app.core.config import settings


class ModelCapabilityError(Exception):
    """模型能力不满足新内核要求（如不支持工具调用）"""


def build_chat_model(
    provider: Dict[str, Any],
    model_config: Dict[str, Any],
    *,
    streaming: bool = True,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> ChatOpenAI:
    """由平台 ModelProvider/ModelConfig 构造 LangChain ChatOpenAI。"""
    if not model_config.get("supports_function_call", False):
        raise ModelCapabilityError(
            f"模型 '{model_config.get('name')}' 未启用原生工具调用（supports_function_call=false）。"
            "DeepAgents 内核要求 Tool Calling 能力，请在模型配置中启用或更换模型；"
            "系统不会回退到文本 ReAct。"
        )

    base_url = (provider.get("base_url") or "").rstrip("/")
    if not base_url:
        raise ModelCapabilityError(f"模型供应商 '{provider.get('name')}' 缺少 base_url")
    api_key = provider.get("api_key") or "EMPTY"

    extra = dict(model_config.get("extra_params") or {})
    provider_extra = dict(provider.get("extra_config") or {})

    # 温度：Agent 配置优先，其次模型配置，再次全局默认
    if temperature is None:
        temperature = extra.get("temperature", settings.AGENT_TEMPERATURE)
    if max_tokens is None:
        max_tokens = model_config.get("max_tokens") or extra.get("max_tokens")

    verify = getattr(settings, "AGENT_TLS_VERIFY", True)
    ca_bundle = provider_extra.get("ca_bundle") or getattr(settings, "AGENT_CA_BUNDLE", "") or None

    kwargs: Dict[str, Any] = dict(
        model=model_config.get("name"),
        base_url=base_url,
        api_key=api_key,
        temperature=float(temperature),
        streaming=streaming and bool(model_config.get("supports_streaming", True)),
        timeout=float(extra.get("timeout", getattr(settings, "INFERENCE_REQUEST_TIMEOUT", 120))),
        max_retries=int(extra.get("max_retries", 2)),
    )
    if max_tokens:
        kwargs["max_tokens"] = int(max_tokens)

    # TLS：verify=False 仅在显式配置 AGENT_TLS_VERIFY=false 时允许
    import httpx

    if not verify:
        kwargs["http_client"] = httpx.AsyncClient(verify=False)
    elif ca_bundle:
        kwargs["http_client"] = httpx.AsyncClient(verify=ca_bundle)

    return ChatOpenAI(**kwargs)


async def check_model_tool_calling(chat_model: ChatOpenAI, model_name: str) -> None:
    """运行时探测：模型必须能返回 tool_calls 结构（bind_tools 后不报错）。"""
    try:
        bound = chat_model.bind_tools([
            {"type": "function", "function": {"name": "_probe", "description": "probe", "parameters": {"type": "object", "properties": {}}}}
        ])
        _ = bound  # 绑定成功即可；真实探测由冒烟测试覆盖
    except Exception as e:
        raise ModelCapabilityError(f"模型 '{model_name}' 工具调用能力探测失败: {e}") from e
