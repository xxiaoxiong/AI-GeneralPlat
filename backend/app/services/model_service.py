from typing import AsyncGenerator, Dict, Any, Tuple
from openai import AsyncOpenAI
import httpx

from app.models.model_provider import ModelProvider, ModelConfig
from app.schemas.model_provider import ChatRequest


class ModelService:

    @staticmethod
    def _get_client(provider: ModelProvider) -> AsyncOpenAI:
        return AsyncOpenAI(
            api_key=provider.api_key or "ollama",
            base_url=provider.base_url,
            http_client=httpx.AsyncClient(verify=False, timeout=120),
        )

    @staticmethod
    async def test_provider(provider: ModelProvider) -> Tuple[bool, str]:
        try:
            client = ModelService._get_client(provider)
            models = await client.models.list()
            return True, f"连接成功，共 {len(list(models))} 个模型"
        except Exception as e:
            return False, f"连接失败: {str(e)}"

    @staticmethod
    def _build_messages(request: "ChatRequest") -> list:
        """Build OpenAI-compatible messages, supporting multimodal image_urls."""
        messages = []
        for m in request.messages:
            image_urls = getattr(m, "image_urls", None) or []
            if image_urls:
                content = [{"type": "text", "text": m.content}]
                for url in image_urls:
                    content.append({"type": "image_url", "image_url": {"url": url}})
                messages.append({"role": m.role, "content": content})
            else:
                messages.append({"role": m.role, "content": m.content})
        if request.system_prompt:
            messages.insert(0, {"role": "system", "content": request.system_prompt})
        return messages

    @staticmethod
    async def chat_stream(
        provider: ModelProvider,
        model_config: ModelConfig,
        request: "ChatRequest",
    ) -> AsyncGenerator[Dict[str, Any], None]:
        client = ModelService._get_client(provider)
        messages = ModelService._build_messages(request)

        params = {
            "model": model_config.name,
            "messages": messages,
            "temperature": request.temperature,
            "stream": True,
        }
        if request.max_tokens:
            params["max_tokens"] = request.max_tokens

        try:
            stream = await client.chat.completions.create(**params)
            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    yield {"type": "content", "content": delta.content}
            yield {"type": "done"}
        except Exception as e:
            yield {"type": "error", "content": str(e)}

    @staticmethod
    async def chat(
        provider: ModelProvider,
        model_config: ModelConfig,
        request: "ChatRequest",
    ) -> Dict[str, Any]:
        client = ModelService._get_client(provider)
        messages = ModelService._build_messages(request)

        params = {
            "model": model_config.name,
            "messages": messages,
            "temperature": request.temperature,
            "stream": False,
        }
        if request.max_tokens:
            params["max_tokens"] = request.max_tokens

        response = await client.chat.completions.create(**params)
        return {
            "content": response.choices[0].message.content,
            "model": response.model,
            "usage": {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            } if response.usage else None,
        }

    @staticmethod
    async def get_embeddings(
        provider: ModelProvider,
        model_name: str,
        texts: list[str],
    ) -> list[list[float]]:
        client = ModelService._get_client(provider)
        response = await client.embeddings.create(model=model_name, input=texts)
        return [item.embedding for item in response.data]
