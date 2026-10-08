"""Client for Dolphin Mistral Venice served by an OpenAI-compatible server (vLLM, Ollama, llama.cpp).

Dolphin-Mistral-24B-Venice-Edition v1.2 supports vision and tool calling. The client only ever receives
the system prompt, policies, filtered context, user text/images and tool results — never secrets.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.core.config import get_settings

log = logging.getLogger(__name__)


class DolphinUnavailable(Exception):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string as produced by the model (validated by the backend)


@dataclass
class ChatResponse:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


class DolphinClient(Protocol):
    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ChatResponse: ...


class OpenAICompatibleDolphinClient:
    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        s = get_settings()
        self.base_url = (base_url or s.dolphin_base_url).rstrip("/")
        if not self.base_url:
            raise DolphinUnavailable("DOLPHIN_BASE_URL is not configured")
        self.model = model or s.dolphin_model
        key = api_key if api_key is not None else s.dolphin_api_key.get_secret_value()
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        self._client = httpx.Client(timeout=timeout or s.dolphin_timeout_seconds, headers=headers, transport=transport)

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ChatResponse:
        body = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0.3,
            "max_tokens": 800,
        }
        last: Exception | None = None
        for _ in range(2):
            try:
                r = self._client.post(f"{self.base_url}/chat/completions", json=body)
                r.raise_for_status()
                msg = r.json()["choices"][0]["message"]
                calls = [
                    ToolCall(
                        id=c.get("id") or f"call_{i}",
                        name=c["function"]["name"],
                        arguments=c["function"].get("arguments") or "{}",
                    )
                    for i, c in enumerate(msg.get("tool_calls") or [])
                ]
                return ChatResponse(content=msg.get("content"), tool_calls=calls)
            except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError, ValueError) as e:
                last = e
        log.warning("dolphin request failed: %s", type(last).__name__)
        raise DolphinUnavailable(str(type(last).__name__))

    def health(self) -> bool:
        try:
            return self._client.get(f"{self.base_url}/models", timeout=5).status_code == 200
        except httpx.HTTPError:
            return False
