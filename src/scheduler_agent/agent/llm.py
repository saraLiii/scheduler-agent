"""LLM provider boundary (D08: Anthropic). Only structured proposals cross this line; no time math."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

import anthropic


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class LLMTurn:
    text: str
    tool_calls: tuple[ToolCall, ...]
    stop_reason: str
    raw_content: list[Any]


class LLMProvider(Protocol):
    def complete(self, system: str, messages: list[dict[str, Any]], tools: list[ToolSpec]) -> LLMTurn: ...


class AnthropicProvider:
    def __init__(self, api_key: str, model: str = "claude-fable-5-1", max_tokens: int = 4096):
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY 未配置")
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    def complete(self, system: str, messages: list[dict[str, Any]], tools: list[ToolSpec]) -> LLMTurn:
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            messages=messages,
            tools=[{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools] or anthropic.NOT_GIVEN,
        )
        texts, calls = [], []
        for block in resp.content:
            if block.type == "text":
                texts.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(block.id, block.name, dict(block.input)))
        return LLMTurn("\n".join(texts).strip(), tuple(calls), resp.stop_reason or "", [b.model_dump() for b in resp.content])


class LLMUnavailable(Exception):
    pass


def tool_result_message(call: ToolCall, result: Any, is_error: bool = False) -> dict[str, Any]:
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call.id, "content": json.dumps(result, ensure_ascii=False, default=str), "is_error": is_error}]}
