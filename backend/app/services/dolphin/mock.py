"""Deterministic Dolphin stand-in for tests and local development without the GPU server."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from app.services.dolphin.client import ChatResponse, DolphinUnavailable, ToolCall


class MockDolphinClient:
    """Returns scripted responses in order. Each item is a ChatResponse or a callable(messages)->ChatResponse.
    Records every request so tests can assert what Dolphin received (e.g. no secrets)."""

    def __init__(
        self,
        script: list[ChatResponse | Callable[[list[dict[str, Any]]], ChatResponse]] | None = None,
        *,
        unavailable: bool = False,
    ):
        self.script = list(script or [])
        self.requests: list[dict[str, Any]] = []
        self.unavailable = unavailable

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ChatResponse:
        self.requests.append({"messages": json.loads(json.dumps(messages, default=str)), "tools": tools})
        if self.unavailable:
            raise DolphinUnavailable("mock unavailable")
        if not self.script:
            return ChatResponse(content="¿En qué te puedo ayudar?")
        step = self.script.pop(0)
        return step(messages) if callable(step) else step


def tool(name: str, call_id: str = "call_1", **arguments: Any) -> ChatResponse:
    return ChatResponse(tool_calls=[ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))])


def say(text: str) -> ChatResponse:
    return ChatResponse(content=text)
